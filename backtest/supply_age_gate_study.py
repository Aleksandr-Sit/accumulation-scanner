"""Гейт пробного исполнителя по возрасту монеты, росту предложения и FDV/MC на дату входа (Q11).

Повод — проба 16.10 (купить-и-держать по снимкам CMC, не модель исполнителя): монеты «моложе
года» (id не было в топ-3000 снимков 12 мес. назад) и с ростом предложения за 12 мес. >100%
держались хуже медианы месяца (0.66× / 0.71×). Противоречие — junk_filter_study (06.10): на
пружинах Binance с правилами исполнителя возраст листинга Binance < 1 года хуже не был.
Здесь — гейт ДО книги в модели исполнителя, как Q9 (cmc_rank_gate_study): сигнал не берётся,
место и деньги свободны для следующих.

Модель — книги R и H, шаг s6 (portfolio_vs_market.exec_book: 15 мест, лестница $50, кулдаун
90 дн., свободный USDT $1500, нижняя четверть по обороту и перегрев, квота неликвида 5 мест);
сверка: база повторяет s6 (R +$1503, H +$1903), гейт «пропускать всё» = база.

Сопоставление монеты Binance со строкой CMC — ровно как Q9: последний снимок
.cache/cmc_hist_deep (первое воскресенье месяца, 3000 мест) не позже дня входа, тикер + цена
±20% (top20_basket_study.Prices.match), лучшее место, без сбоев капы. Берём id CMC этой строки.

Признаки на дату входа (по снимку входа k):
  age   — месяцев (снимков) с первого появления id в топ-3000 снимков deep (с 2015-01; id из
          первого снимка 2015-01 — цензура: возраст «не меньше», для порогов ≤ 18 мес. у
          входов 2018+ не важно);
  added — месяцев с dateAdded CMC (снимки .cache/cmc_hist_supply);
  sup12 / sup6 — рост предложения (mcap/price из deep) за 12 / 6 снимков: s_k / s_{k−12} − 1;
          одиночные выбросы предложения (в 5 раз выше/ниже обоих соседей) заменены средним
          соседей; если в окне есть редоминация (за месяц цена ÷8 и предложение ×6 или
          наоборот) или id нет в снимке k−12 / нет капы — признак неизвестен;
  fdv   — (maxSupply, иначе totalSupply) / circulatingSupply из снимка с полями предложения
          на ту же дату; circ = 0 / нет — неизвестен; < 1 (данные CMC) считаем 1.
Неизвестный признак (несопоставлена, нет данных) — два варианта гейта: пропускать (+) и
отсеивать (−).

Гейты (каждый отдельно, книги независимы): age ≥ 6/12/18; added ≥ 6/12/18; sup12 ≤ 25/50/100%;
fdv ≤ 1.5/2/3/5; комбинация age ≥ 12 и sup12 ≤ 50%. Справочно — разбивка покупок базы по
корзинам признака (доля позиций, P&L на $, P&L − альты на $, умершие).

Метрики как Q9: P&L, P&L − альты / корзина топ-20 по капе / BTC по денежным потокам, J.SEEDS
случайных порядков сигналов внутри дня (в скольких лучше базы того же порядка), вложено,
позиций, умерших за 2 года, сколько из лучших 10% исходов базы пропало; по циклам; вне выборки —
лучший вариант каждого признака по 2018–2022 (среднее порядков, P&L − альты) и он же в 2023–26.

Оговорки: признак по снимку до ~5 недель до входа; «рост предложения» CMC включает правки
самой CMC (перерасчёт circulating supply), а не только эмиссию; id CMC при миграции токена
иногда новый — тогда «молодая» монета на самом деле старая; FDV/MC по max/total supply CMC
неполон (у части монет нет max, total бывает «всё, что когда-либо будет»); выжившие ×0.73 и
правила книг подобраны на той же истории — все цифры верхняя граница.

Запуск из корня проекта:  PYTHONIOENCODING=utf-8 python backtest/supply_age_gate_study.py
  (первый прогон качает 113 снимков с полями предложения 2017-06…2026-10 в
  .cache/cmc_hist_supply, ~3 с на снимок; без сети — FDV/added неизвестны)
Время прогона: ~8 мин (460 с с готовым кэшем, из них ~200 с — архив и сигналы); докачка
  снимков предложения — ещё ~5.5 мин (327 с, 113 снимков, 16.10.2026).
Результат: backtest/supply_age_gate_results.json
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import statistics as st
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cmc_rank_gate_study as G  # noqa: E402
import junk_filter_study as J  # noqa: E402
import ladder_dca_study as L  # noqa: E402
import portfolio_vs_market as P  # noqa: E402
import size_band_study as S  # noqa: E402
import top20_basket_study as T  # noqa: E402
from scanner.benchmark import flow_pnl  # noqa: E402

OUT = Path(__file__).resolve().parent / "supply_age_gate_results.json"
SUPPLY = ROOT / ".cache" / "cmc_hist_supply"
URL = ("https://api.coinmarketcap.com/data-api/v3/cryptocurrency/listings/historical"
       "?date={d}&limit=3000&start=1&convertId=2781")
SUPPLY_FROM = dt.date(2017, 6, 1)
DAY = 86400
SPIKE = 5.0
REDENOM_PRICE, REDENOM_SUPPLY = 8.0, 6.0
IS_CYCLES = G.IS_CYCLES
OOS_CYCLE = G.OOS_CYCLE
BENCH = G.BENCH


# ---------------------------------------------------------------- данные

def supply_snapshot(d: dt.date, net: bool = True) -> list[dict] | None:
    """Снимок CMC с полями предложения (кэш .cache/cmc_hist_supply; пауза 1 с, повторы).
    net=False — только кэш (нет файла -> None)."""
    f = SUPPLY / f"{d.isoformat()}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    if not net:
        return None
    SUPPLY.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(URL.format(d=d.isoformat()), headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(5):
        try:
            data = json.loads(urllib.request.urlopen(req, timeout=90).read())["data"]
            break
        except Exception as e:  # noqa: BLE001 — сеть: повтор с паузой
            print(f"  CMC {d}: {e}; повтор")
            time.sleep(10 * (attempt + 1))
    else:
        print(f"  CMC {d}: нет ответа — без снимка предложения")
        return None
    rows = []
    for x in data:
        q = (x.get("quotes") or [{}])[0]
        rows.append({"id": x["id"], "rank": x.get("cmcRank"), "symbol": x.get("symbol"),
                     "name": x.get("name"), "price": q.get("price"), "mcap": q.get("marketCap"),
                     "circ": x.get("circulatingSupply"), "total": x.get("totalSupply"),
                     "max": x.get("maxSupply"), "added": x.get("dateAdded")})
    f.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    time.sleep(1.0)
    return rows


def deep_dates() -> list[dt.date]:
    return sorted(dt.date.fromisoformat(p.stem) for p in S.DEEP.glob("*.json"))


def matched_ids(snaps, bad, days, px, last_day) -> list[dict[str, tuple[int, int]]]:
    """Как G.matched_ranks, но сегмент Binance -> (лучшее место, id CMC этой строки)."""
    out = []
    for k, rows in enumerate(snaps):
        if days[k] > last_day:
            break
        mr: dict[str, tuple[int, int]] = {}
        for x in sorted(rows, key=lambda r: r["rank"]):
            if f"{x['symbol']}|{x['name']}" in bad[k]:
                continue
            mt = px.match(x, days[k])
            if mt:
                mr.setdefault(mt[0], (x["rank"], x["id"]))
        out.append(mr)
    return out


def supply_series(snaps: list[list[dict]]) -> tuple[dict, dict, int]:
    """id -> {k: предложение mcap/price}, id -> {k: цена}; одиночные выбросы предложения
    заменены средним геометрическим соседей. -> (sup, price, исправлено)."""
    sup: dict[int, dict[int, float]] = {}
    prc: dict[int, dict[int, float]] = {}
    for k, rows in enumerate(snaps):
        for x in rows:
            p, m = x.get("price"), x.get("mcap")
            if p and p > 0:
                prc.setdefault(x["id"], {})[k] = p
                if m and m > 0:
                    sup.setdefault(x["id"], {})[k] = m / p
    fixed = 0
    for s in sup.values():
        fix = {}
        for k, v in s.items():
            a, b = s.get(k - 1), s.get(k + 1)
            if a and b and (v > SPIKE * max(a, b) or v < min(a, b) / SPIKE):
                fix[k] = (a * b) ** 0.5
        s.update(fix)
        fixed += len(fix)
    return sup, prc, fixed


def growth(sup: dict[int, float], prc: dict[int, float], k: int, back: int) -> tuple[float | None, str]:
    """Рост предложения за back снимков к снимку k. -> (рост | None, причина неизвестности)."""
    if k not in sup:
        return None, "нет капы"
    if k - back not in sup:
        return None, "нет точки t−N"
    pts = [j for j in range(k - back, k + 1) if j in sup and j in prc]
    for a, b in zip(pts, pts[1:]):
        pr, sr = prc[b] / prc[a], sup[b] / sup[a]
        if (pr < 1 / REDENOM_PRICE and sr > REDENOM_SUPPLY) or \
                (pr > REDENOM_PRICE and sr < 1 / REDENOM_SUPPLY):
            return None, "редоминация"
    return sup[k] / sup[k - back] - 1, ""


def fdv_ratio(row: dict | None) -> tuple[float | None, str]:
    if row is None:
        return None, "нет в снимке предложения"
    circ = row.get("circ") or 0
    num = row.get("max") if (row.get("max") or 0) > 0 else row.get("total")
    if not circ or circ <= 0:
        return None, "нет circulating"
    if not num or num <= 0:
        return None, "нет max/total"
    return num / circ, ""


def months_between(d0: dt.date, d1: dt.date) -> float:
    return (d1 - d0).days / 30.4375


# ---------------------------------------------------------------- гейты

def gate(feat: str, op: str, thr: float, unk_pass: bool):
    def g(ep):
        v = ep["f"][feat]
        if v is None:
            return unk_pass
        return v >= thr if op == ">=" else v <= thr
    return g


def combo(unk_pass: bool):
    a, b = gate("age", ">=", 12, unk_pass), gate("sup12", "<=", 0.50, unk_pass)
    return lambda ep: a(ep) and b(ep)


FAMILIES = {"age": "возраст в топ-3000 CMC, мес.", "added": "возраст по dateAdded CMC, мес.",
            "sup12": "рост предложения за 12 мес.", "fdv": "FDV/MC", "combo": "возраст ≥12 и рост ≤50%"}


def variants() -> list[dict]:
    vs = [{"key": "base", "label": "база (прод)", "fam": None, "g": None},
          {"key": "all", "label": "сверка: пропускать всё", "fam": None, "g": lambda ep: True}]
    for unk, sfx, word in ((True, "+", "неизв. пропуск"), (False, "−", "неизв. отсев")):
        for t in (6, 12, 18):
            vs.append({"key": f"age{t}{sfx}", "label": f"возраст ≥ {t} мес. ({word})", "fam": "age",
                       "g": gate("age", ">=", t, unk)})
        for t in (6, 12, 18):
            vs.append({"key": f"added{t}{sfx}", "label": f"dateAdded ≥ {t} мес. ({word})",
                       "fam": "added", "g": gate("added", ">=", t, unk)})
        for t in (0.25, 0.50, 1.00):
            vs.append({"key": f"sup{int(t * 100)}{sfx}", "label": f"рост предл. ≤ {t:.0%} ({word})",
                       "fam": "sup12", "g": gate("sup12", "<=", t, unk)})
        for t in (1.5, 2, 3, 5):
            vs.append({"key": f"fdv{t:g}{sfx}", "label": f"FDV/MC ≤ {t:g} ({word})", "fam": "fdv",
                       "g": gate("fdv", "<=", t, unk)})
        vs.append({"key": f"combo{sfx}", "label": f"возр.≥12 и рост≤50% ({word})", "fam": "combo",
                   "g": combo(unk)})
    return vs


BUCKETS = {
    "age": [("< 6", 0, 6), ("6–12", 6, 12), ("12–18", 12, 18), ("18–36", 18, 36), ("≥ 36", 36, 1e9)],
    "added": [("< 6", 0, 6), ("6–12", 6, 12), ("12–18", 12, 18), ("18–36", 18, 36), ("≥ 36", 36, 1e9)],
    "sup12": [("≤ 0%", -1e9, 0.0), ("0–25%", 0.0, 0.25), ("25–50%", 0.25, 0.5), ("50–100%", 0.5, 1.0),
              ("> 100%", 1.0, 1e9)],
    "sup6": [("≤ 0%", -1e9, 0.0), ("0–25%", 0.0, 0.25), ("25–50%", 0.25, 0.5), ("50–100%", 0.5, 1.0),
             ("> 100%", 1.0, 1e9)],
    "fdv": [("≤ 1.5", 0, 1.5), ("1.5–2", 1.5, 2), ("2–3", 2, 3), ("3–5", 3, 5), ("> 5", 5, 1e18)],
}


def bucket_of(feat: str, v: float | None) -> str:
    if v is None:
        return "неизв."
    for name, a, b in BUCKETS[feat]:
        if (a <= v < b) if feat in ("age", "added") else (a < v <= b):
            return name
    return BUCKETS[feat][-1][0]


# ---------------------------------------------------------------- метрики

class Flows:
    """T.flows_vs с кэшем бенчмарков по позиции (позиция однозначна: монета, вход, книга)."""

    def __init__(self, series: dict[str, dict]):
        self.series = series
        self.cache: dict = {}

    def row(self, ep, r):
        k = (ep["seg"]["sym"], ep["e"], id(r))
        if k not in self.cache:
            ts = ep["seg"]["ts"]
            d1 = ep["day"] + max(r["span"], 1) * DAY
            flows = [{"ts": ts[d], "side": sd, "usd": usd, "frac": fr} for d, sd, usd, fr in r["flows"]]
            self.cache[k] = {kk: flow_pnl(flows, lambda t, s=s: P.at(s, t), d1, T.FEE)
                             for kk, s in self.series.items()}
        return self.cache[k]

    def vs(self, taken) -> dict:
        rows, dropped = [], 0
        for ep, r in taken:
            if r["spent"] <= 0:
                continue
            b = self.row(ep, r)
            if None in b.values():
                dropped += 1
                continue
            rows.append({"spent": r["spent"], "pnl": r["pnl"], "cycle": ep["cycle"], **b})

        def tot(rr):
            o = {"n": len(rr), "spent": sum(x["spent"] for x in rr), "pnl": sum(x["pnl"] for x in rr)}
            for k in self.series:
                o[k] = sum(x[k] for x in rr)
                o[f"beat_{k}_share"] = sum(1 for x in rr if x["pnl"] > x[k]) / len(rr) if rr else 0.0
            return o
        out = tot(rows)
        out["dropped"] = dropped
        out["cycles"] = {c: tot([x for x in rows if x["cycle"] == c]) for c, _a, _b in L.CYCLES}
        return out


def book_metrics(fl: Flows, taken, base_best: set, gate_k) -> dict:
    m = fl.vs(taken)
    keys = {G.key_of(ep) for ep, _r in taken}
    m["died2y"] = sum(1 for ep, _r in taken if ep["died2y"])
    m["delist"] = sum(1 for _ep, r in taken if r["how"] == "делистинг")
    lost = base_best - keys
    m["best_lost"] = len(lost)
    m["best_lost_by_gate"] = sum(1 for k in lost if not gate_k(k))
    return m


def breakdown(fl: Flows, taken, feat: str) -> dict:
    out = {}
    names = [b[0] for b in BUCKETS[feat]] + ["неизв."]
    n_all = len(taken)
    for nm in names:
        tt = [(ep, r) for ep, r in taken if bucket_of(feat, ep["f"][feat]) == nm]
        if not tt:
            out[nm] = {"n": 0}
            continue
        spent = sum(r["spent"] for _e, r in tt)
        pnl = sum(r["pnl"] for _e, r in tt)
        ok = [(ep, r) for ep, r in tt if r["spent"] > 0 and fl.row(ep, r)["alt"] is not None]
        ex_alt = sum(r["pnl"] - fl.row(ep, r)["alt"] for ep, r in ok)
        ok_spent = sum(r["spent"] for _e, r in ok)
        out[nm] = {"n": len(tt), "share": len(tt) / n_all, "spent": spent, "pnl": pnl,
                   "per_usd": pnl / spent if spent else 0.0,
                   "ex_alt_per_usd": ex_alt / ok_spent if ok_spent else 0.0,
                   "died2y": sum(1 for ep, _r in tt if ep["died2y"]),
                   "win": sum(1 for _e, r in tt if r["pnl"] > 0) / len(tt),
                   "median_ret": st.median(r["pnl"] / r["spent"] for _e, r in tt if r["spent"] > 0)}
    return out


def pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


# ---------------------------------------------------------------- main

def main() -> int:
    t0 = time.time()
    coins = L.load_archive()
    all_segs = L.segments(coins)
    px = T.Prices(all_segs)
    btc = P.btc_closes(all_segs)
    last_day = max(btc)
    alt, _ = P.market_index(ROOT / "scanner.db")
    comps = T.compositions(px, last_day)
    cap, _ = T.basket_level(px, comps, "cap", last_day)
    series = {"alt": alt, "cap": cap, "btc": btc}

    # снимки как в Q9 (с 2018-01) — для сопоставления; все deep (с 2015-01) — для признаков
    dates = [d for d in T.first_sundays() if T.day_ts(d) <= last_day]
    snaps = [S.deep_snapshot(d) for d in dates]
    days = [T.day_ts(d) for d in dates]
    bad = T.glitches(snaps)
    for k, d in enumerate(dates):
        bad[k] |= {key for dd, key in T.GLITCHES if dd == d.isoformat()}
    mids = matched_ids(snaps, bad, days, px, last_day)

    ddates = deep_dates()
    dsnaps = [S.deep_snapshot(d) for d in ddates]
    kpos = {d: i for i, d in enumerate(ddates)}
    assert all(d in kpos for d in dates), "даты deep не совпали с first_sundays"
    first_k: dict[int, int] = {}
    for k, rows in enumerate(dsnaps):
        for x in rows:
            first_k.setdefault(x["id"], k)
    sup, prc, fixed = supply_series(dsnaps)

    # снимки с полями предложения
    sdates = [d for d in ddates if d >= SUPPLY_FROM]
    srows: dict[dt.date, dict[int, dict]] = {}
    missing = []
    for d in sdates:
        rows = supply_snapshot(d)
        if rows is None:
            missing.append(d.isoformat())
            continue
        srows[d] = {x["id"]: x for x in rows}
    added_any: dict[int, str] = {}
    for d in sdates:
        for i, x in srows.get(d, {}).items():
            if x.get("added"):
                added_any[i] = x["added"]
    print(f"снимков: deep {len(ddates)} ({ddates[0]} … {ddates[-1]}), сопоставление {len(dates)}, "
          f"с полями предложения {len(srows)}/{len(sdates)} (нет: {missing or '—'}); выбросов "
          f"предложения исправлено {fixed}  [{time.time() - t0:.0f} с]")

    vranks = L.vol_ranks(all_segs)
    npairs = P.pairs_per_day(vranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot = L.load_hot()
    src = [ep for ep in L.signal_days(segs, vranks, hot, {"prod": L.is_spring_prod})["prod"]
           if ep["cycle"] != "?"]
    why: dict[str, dict[str, int]] = {f: {} for f in ("sup12", "sup6", "fdv")}
    fdv_lt1 = 0
    for ep in src:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
        k = bisect.bisect_right(days, ep["day"]) - 1
        hit = mids[k].get(ep["seg"]["sym"]) if 0 <= k < len(mids) else None
        ep["cmc"] = hit[0] if hit else None
        ep_why: dict[str, str] = {}
        f = {"age": None, "age_cens": False, "added": None, "sup12": None, "sup6": None, "fdv": None}
        if hit:
            cid, d = hit[1], dates[k]
            kk = kpos[d]
            f["age"] = kk - first_k[cid]
            f["age_cens"] = first_k[cid] == 0
            row = srows.get(d, {}).get(cid)
            ad = (row or {}).get("added") or added_any.get(cid)
            if ad:
                f["added"] = months_between(dt.date.fromisoformat(ad[:10]), d)
            for nm, back in (("sup12", 12), ("sup6", 6)):
                f[nm], reason = growth(sup.get(cid, {}), prc.get(cid, {}), kk, back)
                if reason:
                    ep_why[nm] = reason
            if d in srows:
                f["fdv"], reason = fdv_ratio(row)
                if reason:
                    ep_why["fdv"] = reason
                elif f["fdv"] < 0.99:
                    fdv_lt1 += 1
                if f["fdv"] is not None:
                    f["fdv"] = max(f["fdv"], 1.0)
            else:
                ep_why["fdv"] = "нет снимка предложения"
        else:
            ep_why = {nm: "не сопоставлена" for nm in why}
        ep["f"] = f
        ep["why"] = ep_why
    print(f"дней сигнала прода {len(src)}  [{time.time() - t0:.0f} с]")

    base_take, illiq = P.EXEC_POLICY
    sig = [ep for ep in src if base_take(ep)]
    for ep in sig:
        for nm, reason in ep["why"].items():
            why[nm][reason] = why[nm].get(reason, 0) + 1
    cov = {}
    for c, _a, _b in L.CYCLES + [("всё", 0, 0)]:
        ss = [ep for ep in sig if c == "всё" or ep["cycle"] == c]
        cov[c] = {"n": len(ss), "matched": sum(1 for ep in ss if ep["cmc"] is not None) / len(ss),
                  **{nm: sum(1 for ep in ss if ep["f"][nm] is not None) / len(ss)
                     for nm in ("age", "added", "sup12", "sup6", "fdv")},
                  "age_lt12": sum(1 for ep in ss if ep["f"]["age"] is not None and ep["f"]["age"] < 12)
                  / len(ss),
                  "sup12_gt100": sum(1 for ep in ss if ep["f"]["sup12"] is not None
                                     and ep["f"]["sup12"] > 1) / len(ss),
                  "fdv_gt3": sum(1 for ep in ss if ep["f"]["fdv"] is not None and ep["f"]["fdv"] > 3)
                  / len(ss)}
    both = [ep for ep in sig if ep["f"]["age"] is not None and ep["f"]["added"] is not None]
    rho_age = G.spearman([ep["f"]["age"] for ep in both], [ep["f"]["added"] for ep in both])
    both2 = [ep for ep in sig if ep["f"]["age"] is not None and ep["f"]["sup12"] is not None]
    rho_as = G.spearman([ep["f"]["age"] for ep in both2], [ep["f"]["sup12"] for ep in both2])
    cens = sum(1 for ep in sig if ep["f"]["age_cens"])
    print(f"\nСигналы, прошедшие фильтры прода: {len(sig)}; возраст цензурирован (id в снимке 2015-01) "
          f"у {cens}; Спирмен возраст-по-снимкам / dateAdded {rho_age:+.2f}, возраст / рост "
          f"предложения {rho_as:+.2f}; FDV/MC < 1 в данных CMC (взято 1): {fdv_lt1}")
    print("  причины «неизвестно»: " + "; ".join(f"{k}: {v}" for k, v in why.items()))
    print(f"  {'цикл':8} {'сигн.':>6} {'сопост.':>7} {'возр.':>6} {'dAdded':>6} {'рост12':>6} "
          f"{'рост6':>6} {'FDV':>6} {'возр<12':>7} {'рост>100%':>9} {'FDV>3':>6}")
    for c, v in cov.items():
        print(f"  {c:8} {v['n']:>6} {v['matched']:>7.0%} {v['age']:>6.0%} {v['added']:>6.0%} "
              f"{v['sup12']:>6.0%} {v['sup6']:>6.0%} {v['fdv']:>6.0%} {v['age_lt12']:>7.0%} "
              f"{v['sup12_gt100']:>9.0%} {v['fdv_gt3']:>6.0%}")

    VARIANTS = variants()
    out: dict = {"meta": {"seeds": J.SEEDS, "slots": J.SLOTS, "budget": J.BUDGET,
                          "is_cycles": IS_CYCLES, "oos_cycle": OOS_CYCLE,
                          "deep_snapshots": [ddates[0].isoformat(), ddates[-1].isoformat()],
                          "supply_snapshots": len(srows), "supply_missing": missing,
                          "supply_spikes_fixed": fixed, "archive_last_day": last_day,
                          "variants": {v["key"]: v["label"] for v in VARIANTS},
                          "families": FAMILIES},
                 "signals": {"n": len(sig), "age_censored": cens, "spearman_age_added": rho_age,
                             "spearman_age_sup12": rho_as, "fdv_lt1": fdv_lt1, "unknown_why": why,
                             "coverage_by_cycle": cov}}

    ref = json.loads((Path(__file__).resolve().parent / "portfolio_vs_market_results.json")
                     .read_text(encoding="utf-8"))["exec"]
    by_key = {G.key_of(ep): ep for ep in src}
    for b, (sell, _stop) in J.BOOKS.items():
        cache: dict = {}

        def sim(ep, sell=sell, cache=cache):
            k = (ep["seg"]["sym"], ep["e"])
            if k not in cache:
                cache[k] = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET,
                                           horizon=None, rules=L.EXEC_ALL)
            return cache[k]
        fl = Flows(series)

        base = P.exec_book(src, sim, *P.EXEC_POLICY, cooldown=True, cash=True)
        chk = P.evaluate_flows(base, alt, btc)
        s6 = ref[b]["s6"]
        assert chk["n"] == s6["n"] and abs(chk["pnl"] - s6["pnl"]) < 1e-6 and \
            abs(chk["alt"] - s6["alt"]) < 1e-6, f"{b}: s6 не повторился ({chk['pnl']} / {s6['pnl']})"
        ref_vs = T.flows_vs(base, series)
        mine = fl.vs(base)
        assert all(abs(ref_vs[k] - mine[k]) < 1e-6 for k in ("pnl", "alt", "cap", "btc")), "кэш потоков"
        print(f"\nсверка {b}: база s6 n={chk['n']} P&L {chk['pnl']:+.0f}$ (эталон {s6['pnl']:+.0f}$) ✓")
        top = sorted(base, key=lambda t: t[1]["pnl"], reverse=True)[:max(1, len(base) // 10)]
        base_best = {G.key_of(ep) for ep, _r in top}
        ob: dict = {"best_threshold_pnl": top[-1][1]["pnl"],
                    "best": [{"coin": ep["seg"]["sym"].split("#")[0][:-4], "cmc": ep["cmc"],
                              "age": ep["f"]["age"], "sup12": ep["f"]["sup12"], "fdv": ep["f"]["fdv"],
                              "day": dt.datetime.fromtimestamp(ep["day"], dt.timezone.utc).date()
                              .isoformat(), "pnl": r["pnl"]} for ep, r in top],
                    "breakdown": {feat: breakdown(fl, base, feat) for feat in BUCKETS},
                    "variants": {}}
        seeds_base = None
        base_taken = None
        for v in VARIANTS:
            take = base_take if v["g"] is None else (lambda ep, g=v["g"]: base_take(ep) and g(ep))

            def gate_k(k, take=take):
                return take(by_key[k])
            taken = P.exec_book(src, sim, take, illiq, cooldown=True, cash=True)
            if v["key"] == "base":
                base_taken = taken
            if v["key"] == "all":
                assert [G.key_of(e) for e, _r in taken] == [G.key_of(e) for e, _r in base_taken], \
                    "гейт «пропускать всё» != база"
            m = book_metrics(fl, taken, base_best, gate_k)
            m["lost_best"] = sorted(f"{ep['seg']['sym'].split('#')[0][:-4]} {r['pnl']:+.0f}$"
                                    for ep, r in top if G.key_of(ep) not in
                                    {G.key_of(e) for e, _r in taken})
            runs, died = [], []
            for sd in range(J.SEEDS):
                tk = P.exec_book(src, sim, take, illiq, cooldown=True, cash=True,
                                 order="random", seed=sd)
                runs.append(G.seed_row(fl.vs(tk)))
                died.append(sum(1 for ep, _r in tk if ep["died2y"]))
            if v["key"] == "base":
                seeds_base = runs
            if v["key"] == "all":
                assert all(abs(x["pnl"] - y["pnl"]) < 1e-9 for x, y in zip(runs, seeds_base)), \
                    "«пропускать всё» != база по порядкам"
            rnd = {"pnl_mean": st.fmean(x["pnl"] for x in runs),
                   "spent_mean": st.fmean(x["spent"] for x in runs),
                   "died2y_mean": st.fmean(died),
                   "beat_base_pnl": sum(1 for x, y in zip(runs, seeds_base) if x["pnl"] > y["pnl"]),
                   "beat_base_ex_alt": sum(1 for x, y in zip(runs, seeds_base)
                                           if x["pnl"] - x["alt"] > y["pnl"] - y["alt"])}
            for k, _n in BENCH:
                rnd[f"ex_{k}_mean"] = st.fmean(x["pnl"] - x[k] for x in runs)
                rnd[f"ex_{k}_per_usd"] = st.fmean((x["pnl"] - x[k]) / x["spent"] for x in runs)
                rnd[f"beat_{k}_runs"] = sum(1 for x in runs if x["pnl"] > x[k])
            for part, cyc in (("is", IS_CYCLES), ("oos", (OOS_CYCLE,))):
                rnd[f"{part}_pnl"] = st.fmean(G.cyc_sum(x, cyc, "pnl") for x in runs)
                for k, _n in BENCH:
                    rnd[f"{part}_ex_{k}"] = st.fmean(G.cyc_sum(x, cyc, f"ex_{k}") for x in runs)
                rnd[f"{part}_beat_base_ex_alt"] = sum(
                    1 for x, y in zip(runs, seeds_base)
                    if G.cyc_sum(x, cyc, "ex_alt") > G.cyc_sum(y, cyc, "ex_alt"))
            rnd["cycles"] = {c: {"pnl": st.fmean(G.cyc_sum(x, (c,), "pnl") for x in runs),
                                 "ex_alt": st.fmean(G.cyc_sum(x, (c,), "ex_alt") for x in runs),
                                 "beat_base_ex_alt": sum(
                                     1 for x, y in zip(runs, seeds_base)
                                     if G.cyc_sum(x, (c,), "ex_alt") > G.cyc_sum(y, (c,), "ex_alt"))}
                             for c, _a, _b in L.CYCLES}
            m["random"] = rnd
            ob["variants"][v["key"]] = m
        out[b] = ob

        # ---- печать книги
        name = "R правила" if b == "R" else "H держать"
        vb = ob["variants"]["base"]
        print(f"=== книга {name}: база поз. {vb['n']}, вложено {vb['spent']:.0f}$, P&L {vb['pnl']:+.0f}$; "
              f"лучшие 10% базы — P&L ≥ {ob['best_threshold_pnl']:+.0f}$ ({len(top)} поз.)")
        print("  разбивка покупок базы (по рангу) по признаку: доля / P&L на $ / −альты на $ / "
              "медиана / win / умерли")
        for feat, bd in ob["breakdown"].items():
            print(f"  {feat:6} " + " | ".join(
                f"{nm} {x['share']:.0%} {pct(x['per_usd'])} {pct(x['ex_alt_per_usd'])} "
                f"{pct(x['median_ret'])} {x['win']:.0%} †{x['died2y']}"
                for nm, x in bd.items() if x["n"]))
        print(f"  {J.SEEDS} случайных порядков (средние; «лучше базы» — против базы того же порядка); "
              f"по рангу: поз., умерли, лучш.пропало (гейтом)")
        print(f"  {'вариант':34} {'P&L':>6} {'влож.':>6} {'−альты':>13} {'−корз.':>13} {'−BTC':>13} "
              f"{'лучше базы P&L/−альт':>20} {'2018-20':>8} {'2020-22':>8} {'2023-26':>8} "
              f"{'поз.':>4} {'†':>3} {'†пор.':>5} {'лучш.':>8}")
        for v in VARIANTS:
            m = ob["variants"][v["key"]]
            r = m["random"]
            print(f"  {v['label']:34} {r['pnl_mean']:>+6.0f} {r['spent_mean']:>6.0f} "
                  + " ".join(f"{r[f'ex_{k}_mean']:+.0f} ({pct(r[f'ex_{k}_per_usd'])})".rjust(13)
                             for k, _n in BENCH)
                  + f"{r['beat_base_pnl']}/{r['beat_base_ex_alt']}".rjust(21)
                  + "".join(f"{r['cycles'][c]['ex_alt']:>+9.0f}" for c, _a, _b in L.CYCLES)
                  + f" {m['n']:>4} {m['died2y']:>3} {r['died2y_mean']:>5.1f} "
                  + f"{m['best_lost']}({m['best_lost_by_gate']})".rjust(8))

        # ---- вне выборки
        oos = {}
        vb_r = ob["variants"]["base"]["random"]
        for fam in FAMILIES:
            cand = [v for v in VARIANTS if v["fam"] == fam]
            pick = max(cand, key=lambda v: ob["variants"][v["key"]]["random"]["is_ex_alt"])
            vp_r = ob["variants"][pick["key"]]["random"]
            oos[fam] = {"pick": pick["key"],
                        "is": {k: vp_r[f"is_{k}"] - vb_r[f"is_{k}"] for k in ("pnl", "ex_alt", "ex_btc")},
                        "oos": {k: vp_r[f"oos_{k}"] - vb_r[f"oos_{k}"] for k in ("pnl", "ex_alt", "ex_btc")},
                        "is_beat": vp_r["is_beat_base_ex_alt"], "oos_beat": vp_r["oos_beat_base_ex_alt"],
                        "oos_base": {k: vb_r[f"oos_{k}"] for k in ("pnl", "ex_alt", "ex_btc")}}
            o = oos[fam]
            print(f"  вне выборки [{FAMILIES[fam]}]: по 2018–22 выбран «{pick['label']}» — к базе в "
                  f"2018–22 −альты {o['is']['ex_alt']:+.0f}$ ({o['is_beat']}/{J.SEEDS}); в 2023–26 "
                  f"−альты {o['oos']['ex_alt']:+.0f}$ ({o['oos_beat']}/{J.SEEDS}), P&L {o['oos']['pnl']:+.0f}$, "
                  f"−BTC {o['oos']['ex_btc']:+.0f}$")
        ob["oos"] = oos
        print("  пропали из лучших 10%: " + "; ".join(
            f"{v['key']}: {', '.join(ob['variants'][v['key']]['lost_best']) or '—'}"
            for v in VARIANTS if v["key"] in ("age12-", "sup50+", "fdv3+", "combo-")))
        print(f"  [{time.time() - t0:.0f} с]")

    out["runtime_s"] = time.time() - t0
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
