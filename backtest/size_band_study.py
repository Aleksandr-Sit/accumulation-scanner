"""Размер капитализации и доходность: полосы рейтинга CMC против книг R и H.

Вопрос владельца (11.10.2026): сравнить книги с корзинами топ-20 / 50 / 150 / 200 альтов и
проверить теорию «с маленьким капиталом крупные монеты неинтересны: x10–50 от дна вероятнее у
малой капы». Корзины топ-N вкладываются друг в друга (топ-200 содержит топ-20), поэтому теорию
проверяем на НЕПЕРЕСЕКАЮЩИХСЯ полосах места CMC: 2–20 / 21–50 / 51–150 / 151–200 (1-е — BTC).

Состав: снимки CMC listings/historical на первое воскресенье месяца 2018-01 … (кэш
.cache/cmc_hist_deep, глубина 3000 мест, с id монеты). Исключения — как в Q4
(top20_basket_study: BTC, стейблкоины, обёртки/стейкинг, токенизированное золото, сбои капы).

Два источника цены:
  A — архив Binance (USDT-пары, дневные закрытия): только то, что реально можно было купить.
      Сверка тикера по цене ±20% (как Q4). Ряд оборвался (делистинг) — последнее закрытие.
      Минус: в 2018–19 мелких монет на Binance почти не было (покрытие в таблице).
  B — цена самого снимка CMC по id монеты: все монеты, без перекоса биржи, но помесячно.
      Монета выпала из 3000 мест или сменила id (ребрендинг) — последняя виденная цена;
      справочно — оценка «выпавшие = 0». Выбросы цены (точка в 5× выше/ниже обоих соседних
      месяцев) заменяются средним соседей.

Что считаем:
  2 — полосы: форвардная доходность монет, купленных в день снимка, через 12 и 24 мес. (медиана,
      среднее = корзина равных весов без ребаланса, доли x2/x10/−90%/выпавших), все месяцы,
      месяцы «у дна» (индекс альтов ≤ 1.3× минимума за год), по году старта; плюс корзины
      полос с ребалансом раз в месяц (комиссия 0.15% и проверка 0.5% на оборот).
      Справочно (A): те же полосы по обороту Binance вместо капы.
  3 — маленький капитал: K = 5 и 15 случайных монет полосы равными долями, держать 12/24 мес.,
      REPS случайных наборов на месяц; месяцы — общие для всех полос (в каждой ≥ K монет).
  4 — книги R и H (шаг s6, как в Q4 и portfolio_vs_market) против корзины каждой полосы (A, по
      денежным потокам: та же сумма в те же дни) и разбивка покупок бота по месту CMC на дату
      входа. J.SEEDS случайных порядков сигналов внутри дня.

Оговорки: CMC data-api неофициальный; месячные когорты перекрываются (независимых циклов по сути
2–3); капа CMC у монет с малым free float завышена; издержки — комиссия без спреда (0.5% —
грубая проверка спреда мелких).

Запуск из корня проекта:  py -3 backtest/size_band_study.py
Результат: backtest/size_band_results.json
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import random
import statistics as st
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import junk_filter_study as J  # noqa: E402
import ladder_dca_study as L  # noqa: E402
import portfolio_vs_market as P  # noqa: E402
import top20_basket_study as T  # noqa: E402
from scanner.benchmark import flow_pnl  # noqa: E402

OUT = Path(__file__).resolve().parent / "size_band_results.json"
DEEP = ROOT / ".cache" / "cmc_hist_deep"
URL = ("https://api.coinmarketcap.com/data-api/v3/cryptocurrency/listings/historical"
       "?date={d}&limit=3000&start=1&convertId=2781")
DAY = 86400
FEE, FEE_HI = 0.0015, 0.005
BANDS = [("2–20", 2, 20), ("21–50", 21, 50), ("51–150", 51, 150), ("151–200", 151, 200)]
HORIZONS = (12, 24)
KS = (5, 15)
REPS = 300
SPIKE = 5.0
LOW_K = 1.3          # «у дна»: индекс альтов ≤ 1.3 × минимума за 365 дней


def band_of(rank: int) -> str | None:
    for name, a, b in BANDS:
        if a <= rank <= b:
            return name
    return None


# ---------------------------------------------------------------- данные

def deep_snapshot(d: dt.date) -> list[dict]:
    """Снимок CMC на 3000 мест (кэш на диске, пауза 1 с между запросами к сети)."""
    f = DEEP / f"{d.isoformat()}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    DEEP.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(URL.format(d=d.isoformat()), headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(4):
        try:
            data = json.loads(urllib.request.urlopen(req, timeout=90).read())["data"]
            break
        except Exception as e:  # noqa: BLE001 — сеть: повтор с паузой
            print(f"  CMC {d}: {e}; повтор")
            time.sleep(10 * (attempt + 1))
    else:
        raise RuntimeError(f"CMC {d}: нет ответа")
    rows = [{"id": x["id"], "rank": x["cmcRank"], "symbol": x["symbol"], "name": x["name"],
             "tags": x.get("tags") or [], "price": x["quotes"][0].get("price"),
             "mcap": x["quotes"][0].get("marketCap"), "vol": x["quotes"][0].get("volume24h")}
            for x in data]
    f.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    time.sleep(1.0)
    return rows


def clean_series(snaps: list[list[dict]]) -> dict[int, dict[int, float]]:
    """id -> {номер снимка: цена}; одиночные выбросы (в SPIKE раз выше/ниже обоих соседей)
    заменены средним геометрическим соседей."""
    ser: dict[int, dict[int, float]] = {}
    for k, rows in enumerate(snaps):
        for x in rows:
            if x["price"] and x["price"] > 0:
                ser.setdefault(x["id"], {})[k] = x["price"]
    fixed = 0
    for s in ser.values():
        fix = {}
        for k, p in s.items():
            a, b = s.get(k - 1), s.get(k + 1)
            if a and b and (p > SPIKE * max(a, b) or p < min(a, b) / SPIKE):
                fix[k] = (a * b) ** 0.5
        s.update(fix)
        fixed += len(fix)
    print(f"  выбросов цены CMC исправлено: {fixed}")
    return ser


def near_low_flags(alt: dict[int, float], days: list[int]) -> list[bool]:
    ad = sorted(alt)
    out = []
    for day in days:
        i = bisect.bisect_right(ad, day) - 1
        j = bisect.bisect_left(ad, day - 365 * DAY)
        out.append(i >= 0 and alt[ad[i]] <= LOW_K * min(alt[ad[q]] for q in range(j, i + 1)))
    return out


# ---------------------------------------------------------------- когорты (вариант 2)

def members(snaps, bad) -> list[list[dict]]:
    """По снимкам: монеты полос (место ≤ 200) без исключений и сбоев, по рангу."""
    out = []
    for rows, gl in zip(snaps, bad):
        m = []
        for x in sorted(rows, key=lambda r: r["rank"]):
            b = band_of(x["rank"])
            if b and not T.excluded(x) and f"{x['symbol']}|{x['name']}" not in gl:
                m.append({**x, "band": b})
        out.append(m)
    return out


def cohorts_B(mem, ser, ndays) -> list[dict]:
    rows = []
    for k, m in enumerate(mem):
        for x in m:
            s = ser.get(x["id"], {})
            if k not in s:
                continue
            p0 = s[k]
            for h in HORIZONS:
                if k + h >= ndays:
                    continue
                seen = [j for j in range(k, k + h + 1) if j in s]
                gone = (k + h) not in s
                rows.append({"k": k, "h": h, "band": x["band"], "sym": x["symbol"],
                             "mult": s[seen[-1]] / p0, "gone": gone,
                             "peak": max(s[j] for j in seen) / p0})
    return rows


def cohorts_A(mem, px, days, last_day, vranks) -> tuple[list[dict], list[dict], dict]:
    """-> строки когорт A, сопоставленные монеты по снимкам (для корзин), покрытие."""
    rows, matched, cov = [], [], {}
    for k, m in enumerate(mem):
        day = days[k]
        if day > last_day:
            break
        got, seen = [], set()
        for x in m:
            c = cov.setdefault((x["band"], dt.datetime.fromtimestamp(day, dt.timezone.utc).year),
                               [0, 0])
            c[0] += 1
            mt = px.match(x, day)
            if not mt or mt[0] in seen:
                continue
            seen.add(mt[0])
            c[1] += 1
            got.append({"sym": mt[0], "band": x["band"], "rank": x["rank"], "cmc": x["symbol"],
                        "mcap": x["mcap"] or 0.0, "vr": vranks.get((mt[0], day), 10 ** 6)})
        # справочно: полосы по обороту среди сопоставленных альтов этого дня
        for pos, g in enumerate(sorted(got, key=lambda r: r["vr"]), start=2):
            g["vband"] = band_of(pos)
        matched.append(got)
        for g in got:
            seg = px.seg[g["sym"]]
            e = px.close[g["sym"]][day]
            for h in HORIZONS:
                if k + h >= len(days) or days[k + h] > last_day:
                    continue
                end = days[k + h]
                i0 = bisect.bisect_left(seg["ts"], day)
                i1 = bisect.bisect_right(seg["ts"], end)
                gone = seg["ts"][-1] < end and not seg.get("swap_end") and not seg.get("alive")
                rows.append({"k": k, "h": h, "band": g["band"], "vband": g["vband"],
                             "sym": g["cmc"], "mult": px.last(g["sym"], end) / e, "gone": gone,
                             "peak": max(seg["c"][i0:i1]) / e})
    return rows, matched, cov


def fee_mult(m: float, fee: float) -> float:
    return m * (1 - fee) * (1 - fee)


def band_stats(rows: list[dict], key: str = "band", fee: float = FEE) -> dict:
    out = {}
    for name, _a, _b in BANDS:
        r = [x for x in rows if x[key] == name]
        if not r:
            continue
        m = [fee_mult(x["mult"], fee) for x in r]
        m0 = [0.0 if x["gone"] else v for x, v in zip(r, m)]
        n = len(r)
        out[name] = {"n": n, "median": st.median(m) - 1, "mean": st.fmean(m) - 1,
                     "mean_gone0": st.fmean(m0) - 1,
                     "x2": sum(v >= 2 for v in m) / n, "x10": sum(v >= 10 for v in m) / n,
                     "peak10": sum(x["peak"] >= 10 for x in r) / n,
                     "m90": sum(v <= 0.1 for v in m) / n, "gone": sum(x["gone"] for x in r) / n}
    return out


def by_start_year(rows: list[dict], days: list[int], h: int) -> dict:
    """Средний результат корзины полосы (равные веса, без ребаланса) по году старта."""
    out: dict = {}
    coh: dict = {}
    for x in rows:
        if x["h"] == h:
            coh.setdefault((x["band"], x["k"]), []).append(fee_mult(x["mult"], FEE))
    for (b, k), v in coh.items():
        y = dt.datetime.fromtimestamp(days[k], dt.timezone.utc).year
        out.setdefault(b, {}).setdefault(y, []).append(st.fmean(v) - 1)
    best = {name: 0 for name, _a, _b in BANDS}
    ks = sorted({k for _b, k in coh})
    for k in ks:
        means = {b: st.fmean(coh[(b, k)]) for b, _a, _c in BANDS if (b, k) in coh}
        if len(means) == len(BANDS):
            best[max(means, key=means.get)] += 1
    return {"years": {b: {y: st.fmean(v) for y, v in ys.items()} for b, ys in out.items()},
            "best_count": best, "cohorts_all_bands": sum(best.values())}


# ---------------------------------------------------------------- маленький капитал (вариант 3)

def monte_carlo(rows: list[dict], h: int, k_coins: int, seed: int = 1) -> dict:
    rnd = random.Random(seed)
    coh: dict = {}
    for x in rows:
        if x["h"] == h:
            coh.setdefault(x["k"], {}).setdefault(x["band"], []).append(x["mult"])
    common = [k for k, bb in coh.items()
              if all(len(bb.get(b, [])) >= k_coins for b, _a, _c in BANDS)]
    out = {"cohorts": len(common)}
    for b, _a, _c in BANDS:
        res, has10 = [], 0
        for k in common:
            v = coh[k][b]
            for _ in range(REPS):
                pick = rnd.sample(v, k_coins)
                res.append(fee_mult(st.fmean(pick), FEE))
                has10 += any(p >= 10 for p in pick)
        if not res:
            continue
        res.sort()
        n = len(res)
        out[b] = {"median": res[n // 2] - 1, "p10": res[n // 10] - 1, "p90": res[n * 9 // 10] - 1,
                  "x2": sum(r >= 2 for r in res) / n, "x3": sum(r >= 3 for r in res) / n,
                  "loss50": sum(r <= 0.5 for r in res) / n, "has10": has10 / n}
    return out


# ---------------------------------------------------------------- корзины полос с ребалансом

def levels_A(px, matched, days, last_day, fee) -> dict[str, dict[int, float]]:
    """Дневной уровень корзины каждой полосы (равные веса, ребаланс раз в месяц) по архиву A."""
    old, T.FEE = T.FEE, fee
    try:
        out = {}
        for b, _a, _c in BANDS:
            comps = [{"day": days[k], "basket": [g for g in got if g["band"] == b]}
                     for k, got in enumerate(matched)]
            out[b] = T.basket_level(px, comps, "eq", last_day)[0]
        return out
    finally:
        T.FEE = old


def levels_B(mem, ser, ndays, fee) -> dict[str, dict[int, float]]:
    """Помесячный уровень корзины полосы по ценам CMC: номер снимка -> уровень."""
    out = {}
    for b, _a, _c in BANDS:
        lvl, v, hold = {}, 1.0, {}
        for k in range(ndays):
            drift = {}
            if hold:
                rets = {i: ser[i].get(k, ser[i][max(j for j in ser[i] if j <= k)]) / ser[i][k - 1]
                        for i in hold}
                gross = sum(w * rets[i] for i, w in hold.items())
                v *= gross
                drift = {i: w * rets[i] / gross for i, w in hold.items()}
            ids = [x["id"] for x in mem[k] if x["band"] == b and k in ser.get(x["id"], {})] \
                if k < len(mem) else []
            if not ids:
                lvl[k] = v
                hold = {}
                continue
            tgt = {i: 1 / len(ids) for i in ids}
            v *= 1 - fee * sum(abs(tgt.get(i, 0.0) - drift.get(i, 0.0)) for i in set(tgt) | set(drift))
            lvl[k] = v
            hold = tgt
        out[b] = lvl
    return out


def growth_marks(days: list[int], last_day: int) -> list[tuple[str, int, int]]:
    return [("всё время", days[0], last_day)] + \
        [(c, max(a, days[0]), min(b, last_day)) for c, a, b in L.CYCLES]


def growth_B(lvl: dict[int, float], days: list[int], a: int, b: int) -> float | None:
    ka = bisect.bisect_left(days, a)
    kb = bisect.bisect_right(days, b) - 1
    if ka >= len(days) or kb <= ka or ka not in lvl or kb not in lvl:
        return None
    return lvl[kb] / lvl[ka] - 1


# ---------------------------------------------------------------- книги (вариант 4)

def flows_each(taken, series: dict[str, dict]) -> dict:
    """Книга против каждого бенчмарка по денежным потокам; сравнение только на позициях, где у
    бенчмарка есть уровень (корзины мелких полос на Binance начинаются позже)."""
    out = {}
    rows = []
    for ep, r in taken:
        if r["spent"] <= 0:
            continue
        ts = ep["seg"]["ts"]
        d1 = ep["day"] + max(r["span"], 1) * DAY
        flows = [{"ts": ts[d], "side": sd, "usd": usd, "frac": fr} for d, sd, usd, fr in r["flows"]]
        b = {k: flow_pnl(flows, lambda t, s=s: P.at(s, t), d1, T.FEE) for k, s in series.items()}
        rows.append({"spent": r["spent"], "pnl": r["pnl"], **b})
    for k in series:
        rr = [x for x in rows if x[k] is not None]
        sp = sum(x["spent"] for x in rr)
        out[k] = {"n": len(rr), "spent": sp, "pnl": sum(x["pnl"] for x in rr),
                  "bench": sum(x[k] for x in rr),
                  "beat_share": sum(x["pnl"] > x[k] for x in rr) / len(rr) if rr else 0.0}
    out["n_all"] = len(rows)
    out["spent_all"] = sum(x["spent"] for x in rows)
    out["pnl_all"] = sum(x["pnl"] for x in rows)
    return out


def entry_group(rank: int | None) -> str:
    if rank is None:
        return "нет в 3000 / не сопоставлена"
    b = band_of(rank)
    if b:
        return b
    if rank == 1:
        return "BTC"
    return "201–500" if rank <= 500 else "501+"


def coverage(taken, matched_rank: list[dict], days: list[int], series) -> dict:
    groups: dict[str, list] = {}
    for ep, r in taken:
        k = bisect.bisect_right(days, ep["day"]) - 1
        rk = matched_rank[k].get(ep["seg"]["sym"]) if 0 <= k < len(matched_rank) else None
        groups.setdefault(entry_group(rk), []).append((ep, r))
    out = {}
    for g, tk in groups.items():
        m = flows_each(tk, series)
        m["coins"] = sorted({ep["seg"]["sym"].split("#")[0][:-4] for ep, _r in tk})
        out[g] = m
    return out


# ---------------------------------------------------------------- печать

def pr_stats(title: str, s: dict) -> None:
    print(f"\n{title}")
    print(f"  {'полоса':8} {'n':>5} {'медиана':>8} {'среднее':>8} {'выпав.=0':>8} {'≥x2':>5} "
          f"{'≥x10':>5} {'пик≥x10':>7} {'≤−90%':>6} {'выпала':>6}")
    for b, v in s.items():
        print(f"  {b:8} {v['n']:>5} {v['median']:>+7.0%} {v['mean']:>+7.0%} {v['mean_gone0']:>+7.0%} "
              f"{v['x2']:>5.0%} {v['x10']:>5.1%} {v['peak10']:>7.1%} {v['m90']:>6.1%} {v['gone']:>6.1%}")


def pr_mc(title: str, s: dict) -> None:
    print(f"\n{title} — общих месяцев {s['cohorts']}")
    print(f"  {'полоса':8} {'медиана':>8} {'p10':>6} {'p90':>7} {'≥x2':>5} {'≥x3':>5} {'≤−50%':>6} "
          f"{'есть x10':>8}")
    for b, _a, _c in BANDS:
        v = s.get(b)
        if v:
            print(f"  {b:8} {v['median']:>+7.0%} {v['p10']:>+5.0%} {v['p90']:>+6.0%} {v['x2']:>5.0%} "
                  f"{v['x3']:>5.0%} {v['loss50']:>6.0%} {v['has10']:>8.0%}")


def pr_years(title: str, s: dict) -> None:
    years = sorted({y for v in s["years"].values() for y in v})
    print(f"\n{title}")
    print("  полоса   " + " ".join(f"{y:>6}" for y in years))
    for b, _a, _c in BANDS:
        v = s["years"].get(b, {})
        print(f"  {b:8} " + " ".join(f"{v[y]:>+6.0%}" if y in v else "     —" for y in years))
    print(f"  лучшая полоса за год (из {s['cohorts_all_bands']} месяцев, где есть все полосы): " +
          ", ".join(f"{b} {n}" for b, n in s["best_count"].items()))


# ---------------------------------------------------------------- main

def main() -> int:
    t0 = time.time()
    today = dt.datetime.now(dt.timezone.utc).date()
    dates = [d for d in T.first_sundays() if d <= today]
    snaps = [deep_snapshot(d) for d in dates]
    days = [T.day_ts(d) for d in dates]
    bad = T.glitches(snaps)
    for k, d in enumerate(dates):
        bad[k] |= {key for dd, key in T.GLITCHES if dd == d.isoformat()}
    print(f"снимков CMC {len(dates)} ({dates[0]} … {dates[-1]}), глубина "
          f"{min(len(s) for s in snaps)}–{max(len(s) for s in snaps)} мест  [{time.time() - t0:.0f} с]")
    mem = members(snaps, bad)
    ser = clean_series(snaps)

    coins = L.load_archive()
    all_segs = L.segments(coins)
    px = T.Prices(all_segs)
    btc = P.btc_closes(all_segs)
    last_day = max(btc)
    alt, _ = P.market_index(ROOT / "scanner.db")
    low = near_low_flags(alt, days)
    vranks = L.vol_ranks(all_segs)
    print(f"архив Binance до {dt.datetime.fromtimestamp(last_day, dt.timezone.utc).date()}; "
          f"месяцы «у дна»: {', '.join(d.strftime('%Y-%m') for d, f in zip(dates, low) if f)}  "
          f"[{time.time() - t0:.0f} с]")

    rows_B = cohorts_B(mem, ser, len(dates))
    rows_A, matched, cov = cohorts_A(mem, px, days, last_day, vranks)
    years = sorted({y for _b, y in cov})
    print("\nПокрытие A: доля монет полосы, у которых была пара USDT на Binance")
    print("  полоса   " + " ".join(f"{y:>6}" for y in years))
    for b, _a, _c in BANDS:
        print(f"  {b:8} " + " ".join(f"{cov[(b, y)][1] / cov[(b, y)][0]:>6.0%}" if (b, y) in cov
                                     else "     —" for y in years))

    out: dict = {"meta": {"bands": [b for b, _a, _c in BANDS], "horizons": HORIZONS, "fee": FEE,
                          "fee_hi": FEE_HI, "reps": REPS, "first": dates[0].isoformat(),
                          "last": dates[-1].isoformat(), "archive_last_day": last_day,
                          "near_low": [d.isoformat() for d, f in zip(dates, low) if f],
                          "coverage_A": {f"{b}|{y}": v for (b, y), v in cov.items()}}}

    # ---- вариант 2: полосы
    for src, rows in (("A", rows_A), ("B", rows_B)):
        o = out.setdefault(src, {})
        for h in HORIZONS:
            r = [x for x in rows if x["h"] == h]
            rl = [x for x in r if low[x["k"]]]
            o[f"bands_{h}"] = band_stats(r)
            o[f"bands_{h}_low"] = band_stats(rl)
            o[f"bands_{h}_fee05"] = band_stats(r, fee=FEE_HI)
            o[f"years_{h}"] = by_start_year(rows, days, h)
            name = "Binance" if src == "A" else "все монеты CMC"
            pr_stats(f"=== {src} ({name}), держать {h} мес., все месяцы", o[f"bands_{h}"])
            pr_stats(f"--- {src}, держать {h} мес., только месяцы «у дна» "
                     f"({len({x['k'] for x in rl})})", o[f"bands_{h}_low"])
            pr_years(f"--- {src}, держать {h} мес.: средний результат корзины полосы по году старта",
                     o[f"years_{h}"])
        if src == "A":
            for h in HORIZONS:
                r = [x for x in rows if x["h"] == h and x["vband"]]
                o[f"vbands_{h}"] = band_stats(r, key="vband")
                pr_stats(f"--- справочно A: полосы по ОБОРОТУ Binance среди альтов, {h} мес.",
                         o[f"vbands_{h}"])
        # ---- вариант 3: маленький капитал
        for h in HORIZONS:
            for kc in KS:
                o[f"mc_{h}_{kc}"] = monte_carlo(rows, h, kc)
                pr_mc(f"=== {src}: {kc} случайных монет полосы, держать {h} мес.", o[f"mc_{h}_{kc}"])
        print(f"  [{time.time() - t0:.0f} с]")

    # ---- корзины полос с ребалансом раз в месяц
    lv_A = {f: levels_A(px, matched, days, last_day, f) for f in (FEE, FEE_HI)}
    lv_B = {f: levels_B(mem, ser, len(dates), f) for f in (FEE, FEE_HI)}
    print("\nКорзины полос, равные веса, ребаланс раз в месяц: рост (комиссия 0.15% / 0.5%)")
    out["rebalanced"] = {}
    for lab, a, b in growth_marks(days, last_day):
        row = {}
        for bn, _x, _y in BANDS:
            row[f"A {bn}"] = [T.growth(lv_A[f][bn], a, b) for f in (FEE, FEE_HI)]
            row[f"B {bn}"] = [growth_B(lv_B[f][bn], days, a, b) for f in (FEE, FEE_HI)]
        row["альты"] = [T.growth(alt, a, b)] * 2
        row["BTC"] = [T.growth(btc, a, b)] * 2
        out["rebalanced"][lab] = row
        print(f"  {lab:10} " + "  ".join(
            f"{k} " + "/".join("—" if v is None else f"{v:+.0%}" for v in vs) for k, vs in row.items()))
    first_A = {bn: min(lv_A[FEE][bn]) if lv_A[FEE][bn] else None for bn, _a, _c in BANDS}
    print("  первый день корзины A: " + ", ".join(
        f"{bn} {dt.datetime.fromtimestamp(d, dt.timezone.utc).date()}" for bn, d in first_A.items() if d))

    # ---- вариант 4: книги против корзин полос (A) и разбивка покупок бота
    series = {"alt": alt, "btc": btc, **{bn: lv_A[FEE][bn] for bn, _a, _c in BANDS}}
    matched_rank = []
    for k, rows in enumerate(snaps):
        if days[k] > last_day:
            break
        mr = {}
        for x in sorted(rows, key=lambda r: r["rank"]):
            mt = px.match(x, days[k])
            if mt:
                mr.setdefault(mt[0], x["rank"])
        matched_rank.append(mr)
    npairs = P.pairs_per_day(vranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot = L.load_hot()
    src_eps = [ep for ep in L.signal_days(segs, vranks, hot, {"prod": L.is_spring_prod})["prod"]
               if ep["cycle"] != "?"]
    for ep in src_eps:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
    ref = json.loads((Path(__file__).resolve().parent / "portfolio_vs_market_results.json")
                     .read_text(encoding="utf-8"))["exec"]
    print(f"\nдней сигнала прода {len(src_eps)}  [{time.time() - t0:.0f} с]")
    labels = {"alt": "индекс альтов", "btc": "BTC", **{bn: f"корзина {bn}" for bn, _a, _c in BANDS}}
    for b, (sell, _stop) in J.BOOKS.items():
        cache: dict = {}

        def sim(ep, sell=sell, cache=cache):
            k = (ep["seg"]["sym"], ep["e"])
            if k not in cache:
                cache[k] = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET,
                                           horizon=None, rules=L.EXEC_ALL)
            return cache[k]
        taken = P.exec_book(src_eps, sim, *P.EXEC_POLICY, cooldown=True, cash=True)
        chk = P.evaluate_flows(taken, alt, btc)
        s6 = ref[b]["s6"]
        assert chk["n"] == s6["n"] and abs(chk["pnl"] - s6["pnl"]) < 1e-6, \
            f"{b}: s6 не повторился ({chk['pnl']} / {s6['pnl']})"
        m = flows_each(taken, series)
        runs = [flows_each(P.exec_book(src_eps, sim, *P.EXEC_POLICY, cooldown=True, cash=True,
                                       order="random", seed=sd), series) for sd in range(J.SEEDS)]
        m["random"] = {k: {"vs_per_usd_mean": st.fmean((x[k]["pnl"] - x[k]["bench"]) / x[k]["spent"]
                                                       for x in runs if x[k]["spent"]),
                           "beat_runs": sum(1 for x in runs if x[k]["pnl"] > x[k]["bench"])}
                       for k in series}
        m["coverage"] = coverage(taken, matched_rank, days, series)
        out[b] = m
        name = "R правила" if b == "R" else "H держать"
        print(f"\n=== книга {name}: поз. {m['n_all']}, вложено {m['spent_all']:.0f}$, "
              f"P&L {m['pnl_all']:+.0f}$ ({m['pnl_all'] / m['spent_all']:+.1%} на $)")
        print(f"  {'та же сумма в те же дни в…':26} {'поз.':>5} {'книга':>8} {'рынок':>8} "
              f"{'разница на $':>12} {'поз. лучше':>10} | {J.SEEDS} порядков: на $, книга лучше")
        for k in series:
            v, ro = m[k], m["random"][k]
            print(f"  {labels[k]:26} {v['n']:>5} {v['pnl']:>+7.0f}$ {v['bench']:>+7.0f}$ "
                  f"{(v['pnl'] - v['bench']) / v['spent']:>+11.1%} {v['beat_share']:>10.0%} | "
                  f"{ro['vs_per_usd_mean']:+.1%}, {ro['beat_runs']}/{J.SEEDS}")
        print("  покупки бота по месту CMC на дату входа (P&L на $; та же сумма в корзину своей "
              "полосы / в альты):")
        order = [bn for bn, _a, _c in BANDS] + ["201–500", "501+", "нет в 3000 / не сопоставлена", "BTC"]
        for g in order:
            v = m["coverage"].get(g)
            if not v:
                continue
            own = v.get(g) if g in v else None
            own_s = (f"корзина {(own['bench']) / own['spent']:+.0%} (поз. {own['n']})"
                     if own and own["spent"] else "корзины нет")
            print(f"    {g:28} поз. {v['n_all']:>3} ({v['n_all'] / m['n_all']:>3.0%}) вложено "
                  f"{v['spent_all']:>6.0f}$ P&L {v['pnl_all'] / v['spent_all']:+.0%}; {own_s}; альты "
                  f"{v['alt']['bench'] / v['alt']['spent']:+.0%}  "
                  f"[{', '.join(v['coins'][:10])}{'…' if len(v['coins']) > 10 else ''}]")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
