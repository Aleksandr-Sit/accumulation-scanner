"""Корзина топ-20 альтов БЕЗ ETH: веса, частота ребаланса, сравнение с BTC/ETH по циклам.

Вопрос владельца (12.10.2026) после Q6: «откуп топ-20 без стейблов, BTC, ETH и обёрток выглядит
оптимальным». Плюс замечание: сравнивать с BTC на всей истории некорректно — рост BTC от цикла к
циклу в процентах меньше, ранний рост не повторится. Поэтому, кроме всего периода, смотрим:
  - по циклам (L.CYCLES) и скользящими окнами 12/24 мес. с каждого месяца — в какой доле окон
    корзина обгоняла BTC / ETH / индекс альтов;
  - рост «от минимума до максимума после него» в двух последних циклах — убывает ли он у BTC,
    ETH, индекса альтов и корзины одинаково.

Состав — как в Q4 (top20_basket_study.compositions: снимки CMC на 1-е воскресенье месяца,
первые 20 покупаемых на Binance альтов не глубже 50-го места), но ETH тоже исключён.
Варианты: веса равные / по капе × ребаланс раз в месяц / квартал / год / ни разу (купить в
2018-01 и держать — зависит от старта, справочно). «Купить и держать 12/24 мес.» — когорты с
каждого месяца без ребаланса. Справочно — та же корзина С ETH (Q4). Комиссия 0.15% на оборот.
Книги R и H (шаг s6) — против вариантов по денежным потокам, J.SEEDS порядков сигналов.

Оговорки: индекс альтов (market_daily) включает ETH; архив только Binance (в 2018 в корзине
5–17 монет); перекрывающиеся окна — не независимые наблюдения.

Запуск из корня проекта:  py -3 backtest/top20_ex_eth_study.py
Результат: backtest/top20_ex_eth_results.json
"""
from __future__ import annotations

import datetime as dt
import json
import statistics as st
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import junk_filter_study as J  # noqa: E402
import ladder_dca_study as L  # noqa: E402
import portfolio_vs_market as P  # noqa: E402
import size_band_study as S  # noqa: E402
import top20_basket_study as T  # noqa: E402

OUT = Path(__file__).resolve().parent / "top20_ex_eth_results.json"
DAY = 86400
FREQS = [(1, "мес"), (3, "кв"), (12, "год")]
WINDOWS = (12, 24)
# «от минимума до максимума после него»: окна вокруг двух последних циклов
BULLS = [("цикл 2018–22", 1514764800, 1669852800),    # 2018-01-01 … 2022-12-01
         ("цикл 2022–26", 1654041600, 1893456000)]    # 2022-06-01 … дальше


def closes(segs: list[dict], pair: str) -> dict[int, float]:
    out: dict[int, float] = {}
    for s in segs:
        if s["sym"].split("#")[0] == pair:
            out.update(dict(zip(s["ts"], s["c"])))
    return out


def compositions_ex_eth(px, last_day):
    orig = T.excluded
    T.excluded = lambda x: "ETH" if x["symbol"].upper() == "ETH" else orig(x)
    try:
        return T.compositions(px, last_day)
    finally:
        T.excluded = orig


def max_drawdown(series: dict[int, float], a: int, b: int) -> float:
    peak, dd = 0.0, 0.0
    for d in sorted(series):
        if a <= d <= b:
            peak = max(peak, series[d])
            dd = min(dd, series[d] / peak - 1)
    return dd


def low_to_peak(series: dict[int, float], a: int, b: int) -> tuple[float, int, int] | None:
    """Наибольший рост «минимум -> максимум после него» внутри окна (не обязательно от
    абсолютного минимума: корзина могла уйти ниже прошлого дна уже после пика)."""
    pts = [(d, series[d]) for d in sorted(series) if a <= d <= b]
    if not pts:
        return None
    best, lo, i_lo = (1.0, pts[0][0], pts[0][0]), pts[0][1], pts[0][0]
    for d, v in pts:
        if v < lo:
            lo, i_lo = v, d
        if v / lo > best[0]:
            best = (v / lo, i_lo, d)
    return best


def hold_cohort(px, comp: dict, end_day: int, weights: str) -> float | None:
    """Купить состав сборки в её день и держать до end_day без ребаланса -> кратное."""
    b = comp["basket"]
    if not b:
        return None
    tot = sum(x["mcap"] for x in b)
    w = {x["sym"]: (1 / len(b) if weights == "eq" else x["mcap"] / tot) for x in b}
    m = sum(wt * (px.last(s, end_day) or 0.0) / px.close[s][comp["day"]] for s, wt in w.items())
    return m * (1 - T.FEE) ** 2


def iso(d: int) -> str:
    return dt.datetime.fromtimestamp(d, dt.timezone.utc).strftime("%Y-%m")


def main() -> int:
    t0 = time.time()
    coins = L.load_archive()
    all_segs = L.segments(coins)
    px = T.Prices(all_segs)
    btc = P.btc_closes(all_segs)
    eth = closes(all_segs, "ETHUSDT")
    last_day = max(btc)
    alt, _ = P.market_index(ROOT / "scanner.db")
    comps = compositions_ex_eth(px, last_day)
    comps_eth = T.compositions(px, last_day)
    days = [c["day"] for c in comps]
    print(f"сборок {len(comps)} ({comps[0]['date']} … {comps[-1]['date']}); меньше 20 монет: " +
          ", ".join(f"{c['date'][:7]} {len(c['basket'])}" for c in comps if len(c["basket"]) < T.N))
    print("  состав 2026-09: " + ", ".join(b["cmc"] for b in comps[-1]["basket"]))

    series: dict[str, dict[int, float]] = {}
    for w, wn in (("eq", "равн"), ("cap", "кап")):
        for f, fn in FREQS:
            series[f"{wn}/{fn}"] = T.basket_level(px, comps[::f], w, last_day)[0]
        series[f"{wn}/ни разу"] = T.basket_level(px, comps[:1], w, last_day)[0]
    series["с ETH равн/мес"] = T.basket_level(px, comps_eth, "eq", last_day)[0]
    series["с ETH кап/мес"] = T.basket_level(px, comps_eth, "cap", last_day)[0]
    bench = {"BTC": btc, "ETH": eth, "альты": alt}
    allser = {**series, **bench}

    out: dict = {"meta": {"first": comps[0]["date"], "last": comps[-1]["date"],
                          "archive_last_day": last_day, "fee": T.FEE, "seeds": J.SEEDS,
                          "compositions": [{"date": c["date"], "coins": [b["cmc"] for b in c["basket"]]}
                                           for c in comps]}}

    # ---- рост и просадка по периодам
    marks = [("всё время", days[0], last_day)] + \
        [(c, max(a, days[0]), min(b, last_day)) for c, a, b in L.CYCLES]
    print(f"\nРост за период (макс. просадка за всё время)  [{time.time() - t0:.0f} с]")
    print(f"  {'вариант':16} " + " ".join(f"{m[0]:>10}" for m in marks) + f" {'просадка':>9}")
    out["growth"] = {}
    for k, s in allser.items():
        g = [T.growth(s, a, b) for _l, a, b in marks]
        dd = max_drawdown(s, days[0], last_day)
        out["growth"][k] = {"marks": dict(zip([m[0] for m in marks], g)), "max_dd": dd}
        print(f"  {k:16} " + " ".join("         —" if v is None else f"{v:>+10.0%}" for v in g) +
              f" {dd:>+9.0%}")

    # ---- от минимума до максимума после него
    print("\nОт минимума до максимума после него (кратное, даты)")
    out["low_to_peak"] = {}
    keys = ["равн/мес", "кап/мес", "равн/год", "кап/год", "BTC", "ETH", "альты"]
    for lab, a, b in BULLS:
        b = min(b, last_day)
        row = {}
        for k in keys:
            r = low_to_peak(allser[k], a, b)
            if r:
                row[k] = {"mult": r[0], "low": iso(r[1]), "peak": iso(r[2])}
        out["low_to_peak"][lab] = row
        print(f"  {lab}: " + "; ".join(f"{k} x{v['mult']:.1f} ({v['low']}→{v['peak']})"
                                       for k, v in row.items()))

    # ---- скользящие окна
    out["rolling"] = {}
    for h in WINDOWS:
        print(f"\nСкользящие окна {h} мес. с каждого месяца: медиана роста, доля окон, где вариант "
              f"лучше BTC / ETH / альтов (все окна | окна со старта 2022-06 и позже)")
        res = {}
        cand = {k: s for k, s in series.items() if "ни разу" not in k}
        for k in list(cand) + ["держать равн", "держать кап"]:
            rows = []
            for i in range(len(comps) - h):
                a, b = days[i], days[i + h]
                if k.startswith("держать"):
                    m = hold_cohort(px, comps[i], b, "eq" if k.endswith("равн") else "cap")
                    g = None if m is None else m - 1
                else:
                    g = T.growth(cand[k], a, b)
                gb = {n: T.growth(s, a, b) for n, s in bench.items()}
                if g is None or None in gb.values():
                    continue
                rows.append({"a": a, "g": g, **gb})
            if not rows:
                continue

            def summ(rr):
                return {"n": len(rr), "median": st.median(x["g"] for x in rr),
                        **{f"beat_{n}": sum(x["g"] > x[n] for x in rr) / len(rr) for n in bench},
                        **{f"median_{n}": st.median(x[n] for x in rr) for n in bench}}
            late = [x for x in rows if x["a"] >= 1654041600]
            res[k] = {"all": summ(rows), "since_2022_06": summ(late) if late else None}
            sa, sl = res[k]["all"], res[k]["since_2022_06"]
            print(f"  {k:16} n {sa['n']:>3} медиана {sa['median']:>+6.0%} | лучше BTC "
                  f"{sa['beat_BTC']:>4.0%} ETH {sa['beat_ETH']:>4.0%} альтов {sa['beat_альты']:>4.0%}"
                  + (f" | с 2022-06: n {sl['n']:>2} медиана {sl['median']:>+5.0%}, лучше BTC "
                     f"{sl['beat_BTC']:>4.0%} ETH {sl['beat_ETH']:>4.0%} альтов {sl['beat_альты']:>4.0%}"
                     if sl else ""))
        r0 = next(iter(res.values()))
        print(f"  (для сравнения медиана окна: BTC {r0['all']['median_BTC']:+.0%}, ETH "
              f"{r0['all']['median_ETH']:+.0%}, альты {r0['all']['median_альты']:+.0%}; с 2022-06: "
              f"BTC {r0['since_2022_06']['median_BTC']:+.0%}, ETH {r0['since_2022_06']['median_ETH']:+.0%}, "
              f"альты {r0['since_2022_06']['median_альты']:+.0%})")
        out["rolling"][str(h)] = res

    # ---- книги R и H против вариантов по денежным потокам
    book_series = {k: allser[k] for k in ("равн/мес", "кап/мес", "равн/год", "кап/год",
                                          "с ETH кап/мес", "BTC", "ETH", "альты")}
    vranks = L.vol_ranks(all_segs)
    npairs = P.pairs_per_day(vranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot = L.load_hot()
    src = [ep for ep in L.signal_days(segs, vranks, hot, {"prod": L.is_spring_prod})["prod"]
           if ep["cycle"] != "?"]
    for ep in src:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
    ref = json.loads((Path(__file__).resolve().parent / "portfolio_vs_market_results.json")
                     .read_text(encoding="utf-8"))["exec"]
    for b, (sell, _stop) in J.BOOKS.items():
        cache: dict = {}

        def sim(ep, sell=sell, cache=cache):
            k = (ep["seg"]["sym"], ep["e"])
            if k not in cache:
                cache[k] = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET,
                                           horizon=None, rules=L.EXEC_ALL)
            return cache[k]
        taken = P.exec_book(src, sim, *P.EXEC_POLICY, cooldown=True, cash=True)
        chk = P.evaluate_flows(taken, alt, btc)
        assert chk["n"] == ref[b]["s6"]["n"] and abs(chk["pnl"] - ref[b]["s6"]["pnl"]) < 1e-6, \
            f"{b}: s6 не повторился"
        m = S.flows_each(taken, book_series)
        runs = [S.flows_each(P.exec_book(src, sim, *P.EXEC_POLICY, cooldown=True, cash=True,
                                         order="random", seed=sd), book_series)
                for sd in range(J.SEEDS)]
        m["random"] = {k: {"vs_per_usd_mean": st.fmean((x[k]["pnl"] - x[k]["bench"]) / x[k]["spent"]
                                                       for x in runs),
                           "beat_runs": sum(x[k]["pnl"] > x[k]["bench"] for x in runs)}
                       for k in book_series}
        out[b] = m
        print(f"\n=== книга {'R правила' if b == 'R' else 'H держать'}: поз. {m['n_all']}, вложено "
              f"{m['spent_all']:.0f}$, P&L {m['pnl_all']:+.0f}$; та же сумма в те же дни в…")
        for k in book_series:
            v, ro = m[k], m["random"][k]
            print(f"  {k:16} поз. {v['n']:>3} книга {v['pnl']:>+6.0f}$ рынок {v['bench']:>+6.0f}$ "
                  f"разница на $ {(v['pnl'] - v['bench']) / v['spent']:>+6.1%} | {J.SEEDS} порядков "
                  f"{ro['vs_per_usd_mean']:+.1%}, книга лучше {ro['beat_runs']}/{J.SEEDS}")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
