"""Живой гейт качества монет — срез рынка на сегодня, НЕ бэктест: что останется от
топ-500 по капитализации, если отсечь то, чего нет у большинства известных проектов.
Только бесплатные API: CMC data-api (список, теги, аудиты), Bybit, Binance, Coinbase, OKX,
Kraken, Upbit, DeFiLlama; dev-активность — ссылки на код из CMC и дата последнего коммита
GitHub (commits.atom репозитория; для ссылки на организацию — GitHub API, 60 запросов/час
без GITHUB_TOKEN). CoinGecko commit_count_4_weeks с 09.2026 отдаёт null — не используется.

Жёсткие критерии (все обязательны) + мягкие пометки (dev-активность, аудиты, фонды, зона).
Зона (пружина/середина/пик) — прод-функции scanner.stages.zone на дневных закрытиях
Binance (кэш backtest/binance_archive.py) или Bybit.
Пороги GATE — [оценка]; на истории проверен только ранг оборота (ladder_dca_study):
топ-150 по 30-дн. обороту Binance. Для монет вне Binance — ранг среди пар Bybit (приближение).
Монеты сопоставляются с биржами по тикеру: из дублей тикера в топ-500 берётся старший по капе.

Запуск из корня проекта:  py -3 -u backtest/quality_screen.py   (~6 мин: Bybit klines)
Результат: --out PATH (по умолчанию backtest/quality_screen_results.json — закоммиченный
срез); запись атомарная (временный файл → rename): сбой не оставит битый файл. Ежедневный
прогон пишет data/quality_screen_live.json сам: run.py quality --refresh-if-due.
Прошли гейт меньше --min-passed — сбой источников, а не рынок: срез не записывается, код 1.
Дозаполнить «коммит» после сброса лимита GitHub:  py -3 backtest/quality_screen.py --dev-only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(PROJ / "backtest"))
from scanner.config import load_config  # noqa: E402
from scanner.quality import write_slice  # noqa: E402
from scanner.stages import zone  # noqa: E402
from binance_archive import NON_ALTS  # noqa: E402  стейблы/фиат/обёртки — вне вселенной ранга

OUTDIR = Path(__file__).resolve().parent
RESULT = OUTDIR / "quality_screen_results.json"
# 29.09.2026 гейт прошли 77 из 500 (34 из них без контракта — трек Q). Единицы — не рынок,
# а отказ источника (Bybit не ответил — «нет на Bybit spot» у всех): такой срез затёр бы
# трек Q — резолвер берёт свежий.
MIN_PASSED = 10
CG = "https://api.coingecko.com/api/v3"
UA = {"User-Agent": "quality-screen/0.1"}
NOW = time.time()

GATE = {
    "min_mcap": 100e6,         # капитализация
    "min_vol24": 1e6,          # суточный оборот по всем площадкам (CMC)
    "min_top_cex": 2,          # листинг на топ-биржах (Binance/Coinbase/OKX/Bybit/Kraken/Upbit)
    "min_age_days": 365,       # нижняя оценка возраста торгов
    "max_fdv_mc": 3.0,         # навес будущей эмиссии
    "need_bybit": True,        # доступ из РФ (Bybit spot) и без ST-метки
    "exclude_meme": True,      # мемы — нет продукта и разработки по определению
    "max_vol_rank": 150,       # ранг 30-дн. оборота: Binance USDT, вне Binance — Bybit USDT
}
# Теги CMC «не монета»: стейблы, токенизированные акции/золото/ETF, LP-токены.
NON_COIN_TAGS = {"stablecoin", "usd-stablecoin", "fiat-stablecoin", "asset-backed-stablecoin",
                 "eur-stablecoin", "algorithmic-stablecoin", "tokenized-assets",
                 "tokenized-stock", "tokenized-etfs", "tokenized-gold", "tokenized-commodities",
                 "asset-backed-token", "lp-tokens"}
CMC_LIST = "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/listing"
CMC_DETAIL = "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail"


def get(url: str, params: dict | None = None, tries: int = 5, pause: float = 0.0,
        headers: dict | None = None, raw: bool = False):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    back = 8.0
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read().decode()
            if pause:
                time.sleep(pause)
            return body if raw else json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(back)
                back *= 1.6
                continue
            if e.code in (403, 404, 451):
                return None
            time.sleep(3)
        except Exception:
            time.sleep(3)
    return None


def cmc_listing(limit: int = 500) -> list[dict]:
    d = get(CMC_LIST, {"start": 1, "limit": limit, "sortBy": "market_cap", "sortType": "desc",
                       "convert": "USD", "cryptoType": "all", "tagType": "all", "audited": "false",
                       "aux": "ath,date_added,circulating_supply,total_supply,max_supply,tags"})
    return ((d or {}).get("data") or {}).get("cryptoCurrencyList") or []


def dev_activity(cmc_id: int) -> dict:
    """Ссылки на код (CMC detail) -> дней с последнего коммита на GitHub, пометки CMC."""
    d = ((get(CMC_DETAIL, {"id": cmc_id}) or {}).get("data")) or {}
    urls = [u for u in ((d.get("urls") or {}).get("source_code") or [])
            if u and u.rstrip("/").count("/") >= 3]      # «https://github.com» без пути — заглушка
    out = {"repos": urls, "notice": (d.get("notice") or "").strip()[:200],
           "last_commit_days": None, "dev_src": None}
    gh = [u for u in urls if "github.com/" in u]
    tok = os.getenv("GITHUB_TOKEN")
    hdr = {"Authorization": f"Bearer {tok}"} if tok else None
    best = None
    for u in gh[:3]:
        parts = [x for x in u.split("github.com/", 1)[1].split("/") if x]
        stamp = None
        if len(parts) >= 2:                    # репозиторий: публичная лента коммитов
            atom = get(f"https://github.com/{parts[0]}/{parts[1]}/commits.atom", tries=2, raw=True)
            m = re.search(r"<updated>([^<]+)</updated>", atom or "")
            stamp = m.group(1) if m else None
        elif parts:                            # организация/пользователь: API (лимит)
            for kind in ("orgs", "users"):
                r = get(f"https://api.github.com/{kind}/{parts[0]}/repos",
                        {"sort": "pushed", "per_page": 1}, tries=1, headers=hdr)
                if r:
                    stamp = r[0].get("pushed_at")
                    break
        if stamp:
            ts = time.mktime(time.strptime(stamp[:10], "%Y-%m-%d"))
            days = (NOW - ts) / 86400
            best = days if best is None else min(best, days)
    if best is not None:
        out["last_commit_days"], out["dev_src"] = round(best), "github"
    return out


def exchanges() -> dict[str, set[str]]:
    ex: dict[str, set[str]] = {}
    d = get("https://api.bybit.com/v5/market/instruments-info", {"category": "spot", "limit": 1000})
    lst = ((d or {}).get("result") or {}).get("list") or []
    ex["bybit"] = {x["baseCoin"].upper() for x in lst if x.get("status") == "Trading"}
    ex["bybit_st"] = {x["baseCoin"].upper() for x in lst if x.get("stTag") == "1"}
    d = get("https://api.binance.com/api/v3/exchangeInfo", {"permissions": "SPOT"})
    ex["binance"] = {s["baseAsset"].upper() for s in (d or {}).get("symbols", [])
                     if s.get("status") == "TRADING"}
    d = get("https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products")
    rows = (d or {}).get("data") or []
    ex["binance_monitor"] = {r["b"].upper() for r in rows
                             if "Monitoring" in (r.get("tags") or [])}
    d = get("https://api.exchange.coinbase.com/products")
    ex["coinbase"] = {p["base_currency"].upper() for p in (d or [])
                      if isinstance(p, dict) and p.get("status") == "online"}
    d = get("https://www.okx.com/api/v5/public/instruments", {"instType": "SPOT"})
    ex["okx"] = {x["baseCcy"].upper() for x in (d or {}).get("data", []) if x.get("state") == "live"}
    d = get("https://api.kraken.com/0/public/AssetPairs")
    kr = set()
    for v in ((d or {}).get("result") or {}).values():
        ws = v.get("wsname") or ""
        if "/" in ws:
            b = ws.split("/")[0].upper()
            kr.add({"XBT": "BTC", "XDG": "DOGE"}.get(b, b))
    ex["kraken"] = kr
    d = get("https://api.upbit.com/v1/market/all")
    ex["upbit"] = {m["market"].split("-")[1].upper() for m in (d or []) if "-" in m.get("market", "")}
    return ex


def binance_first_listing() -> dict[str, float]:
    idx = PROJ / ".cache" / "binance_1d_full" / "_index.json"
    if not idx.exists():
        return {}
    out = {}
    for sym, m in json.loads(idx.read_text()).items():
        base = sym[:-4].split("#")[0]
        out[base] = min(out.get(base, 9e18), m["first"])
    return out


def vol_ranks() -> tuple[dict[str, int], dict[str, int]]:
    """Ранги среднего оборота за 30 закрытых дней (1 = max), как vol_ranks в ladder_dca_study:
    Binance — по кэшу архива, Bybit — по klines API. Стейблы и плечевые токены исключены."""
    bn = []
    idx = PROJ / ".cache" / "binance_1d_full" / "_index.json"
    for sym, m in (json.loads(idx.read_text()) if idx.exists() else {}).items():
        if not m.get("trading") or "#" in sym:
            continue
        d = json.loads((idx.parent / f"{sym}.json").read_text())
        if NOW - d["ts"][-1] < 5 * 86400 and len(d["qv"]) >= 30:
            bn.append((sum(d["qv"][-30:]) / 30, sym[:-4]))
    d = get("https://api.bybit.com/v5/market/instruments-info", {"category": "spot", "limit": 1000})
    by = []
    for x in ((d or {}).get("result") or {}).get("list") or []:
        b = x["baseCoin"].upper()
        if (x.get("quoteCoin") != "USDT" or x.get("status") != "Trading" or b in NON_ALTS
                or b[-2:] in ("2L", "3L", "5L", "2S", "3S", "5S")):
            continue
        k = get("https://api.bybit.com/v5/market/kline",
                {"category": "spot", "symbol": x["symbol"], "interval": "D", "limit": 31})
        rows = (((k or {}).get("result") or {}).get("list") or [])[1:31]  # без живой свечи
        if len(rows) >= 20:
            by.append((sum(float(r[6]) for r in rows) / len(rows), b))
    rank = lambda xs: {s: i for i, (_, s) in enumerate(sorted(xs, reverse=True), 1)}  # noqa: E731
    return rank(bn), rank(by)


def closes_for(sym: str) -> tuple[list[float], list[float]]:
    """(закрытия, обороты) последних ~400 дней: архив Binance, иначе Bybit."""
    p = PROJ / ".cache" / "binance_1d_full" / f"{sym}USDT.json"
    if p.exists():
        d = json.loads(p.read_text())
        if NOW - d["ts"][-1] < 5 * 86400:
            return d["c"][-400:], d["qv"][-400:]
    d = get("https://api.bybit.com/v5/market/kline",
            {"category": "spot", "symbol": f"{sym}USDT", "interval": "D", "limit": 400})
    rows = list(reversed(((d or {}).get("result") or {}).get("list") or []))[:-1]  # без живой свечи
    return [float(r[4]) for r in rows], [float(r[6]) for r in rows]


def main(out: Path = RESULT, min_passed: int = MIN_PASSED) -> int:
    t0 = time.time()
    cfg = load_config(None)
    mk = cmc_listing(500)
    print(f"CMC топ: {len(mk)}  [{time.time()-t0:.0f} с]")
    if not mk:
        print("CMC не ответил — выход")
        return 1
    ex = exchanges()
    print("биржи:", {k: len(v) for k, v in ex.items()})
    first_bn = binance_first_listing()
    rk_bn, rk_by = vol_ranks()
    print(f"ранги оборота: Binance {len(rk_bn)} пар, Bybit {len(rk_by)} пар  [{time.time()-t0:.0f} с]")
    audits: dict[int, int] = {}
    for pr in get("https://api.llama.fi/protocols") or []:
        try:
            cmc_id, a = int(pr.get("cmcId") or 0), int(pr.get("audits") or 0)
        except (ValueError, TypeError):
            continue
        if cmc_id:
            audits[cmc_id] = max(audits.get(cmc_id, 0), a)

    top_cex = ("binance", "coinbase", "okx", "bybit", "kraken", "upbit")
    rows, reasons, seen = [], {}, set()
    for i, r in enumerate(mk, 1):
        q = (r.get("quotes") or [{}])[0]
        sym, tags = (r.get("symbol") or "").upper(), set(r.get("tags") or [])
        mcap, vol = q.get("marketCap") or 0, q.get("volume24h") or 0
        fdv, price, ath = q.get("fullyDilluttedMarketCap"), q.get("price") or 0, r.get("ath") or 0
        added = r.get("dateAdded")
        ts = time.mktime(time.strptime(added[:10], "%Y-%m-%d")) if added else NOW
        age = max((NOW - ts) / 86400, (NOW - first_bn[sym]) / 86400 if sym in first_bn else 0)
        cex = [e for e in top_cex if sym in ex.get(e, set())]
        fdv_mc = (fdv / mcap) if fdv and mcap else None
        vrank, vsrc = ((rk_bn[sym], "Binance") if sym in rk_bn else
                       (rk_by[sym], "Bybit") if sym in rk_by else (None, None))
        noncoin = bool(tags & NON_COIN_TAGS) or sym in NON_ALTS
        meme = any("meme" in t for t in tags)
        fails = []
        if sym in seen:
            fails.append("дубль тикера (биржи не сопоставить)")
        seen.add(sym)
        if noncoin:
            fails.append("не монета (стейбл/токенизированный актив)")
        elif GATE["exclude_meme"] and meme:
            fails.append("мем")
        if mcap < GATE["min_mcap"]:
            fails.append("капа < $100M")
        if vol < GATE["min_vol24"]:
            fails.append("оборот < $1M/сут")
        if len(cex) < GATE["min_top_cex"]:
            fails.append("< 2 топ-бирж")
        if age < GATE["min_age_days"]:
            fails.append("торгуется < 1 года")
        if fdv_mc is not None and fdv_mc > GATE["max_fdv_mc"]:
            fails.append("FDV/MC > 3 (большой навес разлоков)")
        if GATE["need_bybit"] and sym not in ex["bybit"]:
            fails.append("нет на Bybit spot")
        if sym in ex["bybit_st"] or sym in ex["binance_monitor"]:
            fails.append("метка риска делистинга (ST/Monitoring)")
        if vrank is None or vrank > GATE["max_vol_rank"]:
            fails.append("ранг оборота > 150")
        for f in fails:
            reasons[f] = reasons.get(f, 0) + 1
        n_aud = len([a for a in r.get("auditInfoList") or [] if a.get("auditStatus") == 2])
        rows.append({"cmc_id": r["id"], "slug": r.get("slug"), "sym": sym, "name": r.get("name"),
                     "rank": r.get("cmcRank") or i, "mcap": mcap, "vol": vol, "fdv_mc": fdv_mc,
                     "age": age, "cex": cex, "ath_dd": (1 - price / ath) * 100 if ath else 0,
                     "fails": fails, "meme": meme, "noncoin": noncoin,
                     "audits_cmc": n_aud, "audits_llama": audits.get(r["id"]),
                     "funds": len([t for t in tags if t.endswith("-portfolio")]),
                     "vol_rank": vrank, "vol_rank_src": vsrc})
    passed = [x for x in rows if not x["fails"]]
    print(f"\nПрошли жёсткий гейт: {len(passed)} из {len(rows)}  [{time.time()-t0:.0f} с]")
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {v:>4}  {k}")
    sole: dict[str, int] = {}
    for x in rows:
        if len(x["fails"]) == 1:
            sole[x["fails"][0]] = sole.get(x["fails"][0], 0) + 1
    print("Единственная причина отсева (что отпустит монету, если убрать критерий):")
    for k, v in sorted(sole.items(), key=lambda kv: -kv[1]):
        print(f"  {v:>4}  {k}")
    if len(passed) < min_passed:
        print(f"\n⚠ прошли гейт только {len(passed)} (< {min_passed}) — похоже на сбой "
              f"источников, а не на рынок: срез НЕ записан, прежний остаётся")
        return 1

    # мягкие признаки: dev-активность (CMC + GitHub) и зона (прод-функции)
    for i, x in enumerate(passed):
        x.update(dev_activity(x["cmc_id"]))
        cl, qv = closes_for(x["sym"])
        if len(cl) >= 60:
            ind = zone.compute_indicators(cl[-180:], 30, 50, qv[-180:])
            x["zone"], _ = zone.classify_zone(ind, x["ath_dd"], cfg)
        else:
            x["zone"] = "?"
        if (i + 1) % 20 == 0:
            print(f"  ...dev/зона {i+1}/{len(passed)}  [{time.time()-t0:.0f} с]")
    write_slice(out, {"date": time.strftime("%Y-%m-%d"), "gate": GATE, "rows": rows,
                      "reasons": reasons, "sole": sole})
    print_table(passed)
    print(f"\nsaved -> {out} ({time.time()-t0:.0f} с)")
    return 0


def print_table(passed: list[dict]) -> None:
    print(f"\n{'#':>4} {'монета':8} {'капа $M':>9} {'оборот $M':>9} {'лет':>4} {'CEX':>3} "
          f"{'FDV/MC':>6} {'V-ранг':>7} {'коммит':>8} {'аудит':>5} {'фонды':>5} {'от ATH':>7}  зона")
    for x in passed:
        dev = ("нет кода" if not x.get("repos") else "н/д" if x.get("last_commit_days") is None
               else f"{x['last_commit_days']}д")
        aud = max(x["audits_cmc"], x["audits_llama"] or 0)
        print(f"{x['rank']:>4} {x['sym']:8} {x['mcap']/1e6:>9.0f} {x['vol']/1e6:>9.1f} "
              f"{x['age']/365:>4.1f} {len(x['cex']):>3} {(x['fdv_mc'] or 0):>6.2f} "
              f"{x['vol_rank']:>5}{x['vol_rank_src'][:2]} {dev:>8} {aud or '—':>5} "
              f"{x['funds']:>5} {-x['ath_dd']:>+6.0f}%  {x.get('zone', '?')}"
              + (f"  ⚠ CMC: {x['notice'][:60]}" if x.get("notice") else ""))


def dev_only(out: Path = RESULT) -> int:
    """Дозаполнить «коммит» там, где упёрлись в лимит GitHub API (без повторного скана)."""
    d = json.loads(Path(out).read_text(encoding="utf-8"))
    passed = [x for x in d["rows"] if not x["fails"]]
    todo = [x for x in passed if x.get("repos") and x.get("last_commit_days") is None]
    for x in todo:
        x.update(dev_activity(x["cmc_id"]))
    left = sum(1 for x in todo if x.get("last_commit_days") is None)
    write_slice(out, d)
    print_table(passed)
    print(f"\nдозаполнено {len(todo) - left} из {len(todo)}; осталось н/д: {left}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Срез фильтра качества: топ-500 CMC через жёсткий гейт → JSON для трека Q "
                    "(~6 мин сети).")
    ap.add_argument("--out", type=Path, default=RESULT,
                    help="куда записать срез (по умолчанию backtest/quality_screen_results.json); "
                         "запись атомарная, каталог создаётся")
    ap.add_argument("--dev-only", action="store_true",
                    help="дозаполнить «коммит» в готовом срезе --out (после сброса лимита "
                         "GitHub), без повторного скана")
    ap.add_argument("--min-passed", type=int, default=MIN_PASSED,
                    help=f"прошли гейт меньше — срез не записывается, код 1 (сбой источников); "
                         f"по умолчанию {MIN_PASSED}")
    return ap.parse_args(argv)


if __name__ == "__main__":
    _a = parse_args()
    sys.exit(dev_only(_a.out) if _a.dev_only else main(_a.out, _a.min_passed))
