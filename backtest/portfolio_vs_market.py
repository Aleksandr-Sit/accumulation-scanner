"""Портфель пробного исполнителя на истории против рынка на тех же окнах.

Вопрос (08.10.2026): прежде чем давать исполнителю живые деньги — обгоняет ли его книга
простое «купить рынок» на те же деньги и те же дни? Если нет, отбор и выходы ничего не
добавляют к бете рынка, а риски (делистинги, неликвид, ошибки исполнения) остаются.

Книги — как в junk_filter_study (тот же код): 5 ступеней по $10 до пола прод-стопа, R —
stage8_exit целиком, H — только стоп −25%; 15 слотов по $50, одна позиция на монету, слот
занят до выхода. Правила отбора:
  • «все пружины» — без отсева, порядок внутри дня случайный (среднее по SEEDS порядкам);
  • «как исполнитель» — нижняя четверть по обороту (место / число пар дня > 0.75) не
    покупается, ≥ 1 флага перегрева или нет данных рынка — не покупается, вне топ-39% по
    обороту — не больше 5 слотов, внутри дня ликвидные первыми (executor.py, config executor).

Бенчмарк позиции: та же вложенная сумма (spent) в BTC (закрытия BTCUSDT Binance) или в
индекс альтов (total × (1 − BTC.D) − стейблы, market_daily) с закрытия дня сигнала до
закрытия дня выхода, комиссия 0.15% на вход и на выход. Лестница вкладывает деньги
постепенно, бенчмарк — сразу: в растущем рынке это в пользу бенчмарка, в падающем — против.

Оговорки: архив только Binance; пружина — исследовательский детектор, а не балл ≥ 70;
эпизоды коррелированы; пороги частично in-sample. Индекс альтов взвешен по капе — его
нельзя купить один в один, это ориентир «рынок альтов в целом».

Запуск из корня проекта:  py -3 backtest/portfolio_vs_market.py
Результат: backtest/portfolio_vs_market_results.json
"""
from __future__ import annotations

import json
import random
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import junk_filter_study as J  # noqa: E402
import ladder_dca_study as L  # noqa: E402

OUT = Path(__file__).resolve().parent / "portfolio_vs_market_results.json"
DAY = 86400
FEE = 0.0015
VOL_BOTTOM_SHARE = 0.75          # executor.vol_bottom_share
ILLIQUID_SHARE = 0.39            # executor.illiquid_share
ILLIQUID_SLOTS = 5               # executor.illiquid_max_slots


def market_index(db: Path) -> tuple[dict[int, float], dict[int, float]]:
    """day -> индекс альтов и капа BTC из market_daily (пропуски — прошлым значением)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("SELECT day, total_mcap, btc_dominance, stables_usd FROM market_daily "
                       "WHERE total_mcap IS NOT NULL AND btc_dominance IS NOT NULL "
                       "ORDER BY day").fetchall()
    alt, btc = {}, {}
    for day, total, dom, st in rows:
        alt[day] = total * (1 - dom / 100) - (st or 0)
        btc[day] = total * dom / 100
    return alt, btc


def at(series: dict[int, float], day: int, back: int = 7) -> float | None:
    for k in range(back + 1):
        v = series.get(day - k * DAY)
        if v:
            return v
    return None


def btc_closes(segs: list[dict]) -> dict[int, float]:
    out: dict[int, float] = {}
    for s in segs:
        if s["sym"].split("#")[0] == "BTCUSDT":
            out.update(dict(zip(s["ts"], s["c"])))
    return out


def pairs_per_day(ranks: dict) -> dict[int, int]:
    n: dict[int, int] = {}
    for (_, day), _r in ranks.items():
        n[day] = n.get(day, 0) + 1
    return n


def taken_rows(rows: list[dict], take, order: str, quota, seed: int) -> list[dict]:
    """junk_filter_study.book_sim, но возвращает сами взятые позиции."""
    rnd = random.Random(seed)
    by_day: dict[int, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["ep"]["day"], []).append(r)
    held: list[tuple[int, bool, str]] = []
    taken = []
    for day in sorted(by_day):
        held = [x for x in held if x[0] > day]
        cands = [r for r in by_day[day] if take(r["ep"])]
        if order == "rank":
            cands.sort(key=lambda r: r["ep"]["rank"])
        else:
            rnd.shuffle(cands)
        for r in cands:
            coin = r["ep"]["seg"]["sym"].split("#")[0]
            if any(x[2] == coin for x in held):
                continue
            in_q = quota is not None and quota(r["ep"])
            if len(held) >= J.SLOTS or (in_q and sum(1 for x in held if x[1]) >= ILLIQUID_SLOTS):
                continue
            held.append((day + max(r["span"], 1) * DAY, in_q, coin))
            taken.append(r)
    return taken


def bench_pnl(spent: float, series: dict[int, float], d0: int, d1: int) -> float | None:
    a, b = at(series, d0), at(series, d1)
    if not a or not b:
        return None
    return spent * (b / a * (1 - FEE) * (1 - FEE) - 1)


def evaluate(taken: list[dict], alt: dict, btc: dict) -> dict:
    rows = []
    for r in taken:
        spent = r["deployed"] * r["budget"]
        if spent <= 0:
            continue                       # ни одна ступень не исполнилась — денег не было
        d0 = r["ep"]["day"]
        d1 = d0 + max(r["span"], 1) * DAY
        ba, bb = bench_pnl(spent, alt, d0, d1), bench_pnl(spent, btc, d0, d1)
        if ba is None or bb is None:
            continue
        rows.append({"d0": d0, "d1": d1, "spent": spent, "pnl": r["pnl"], "alt": ba, "btc": bb,
                     "cycle": r["ep"]["cycle"], "how": r["how"], "days": max(r["span"], 1)})
    if not rows:
        return {}
    s = lambda k, rr=rows: sum(x[k] for x in rr)  # noqa: E731
    exc = [(x["pnl"] - x["alt"]) / x["spent"] for x in rows]
    cyc = {}
    for c in sorted({x["cycle"] for x in rows}):
        rr = [x for x in rows if x["cycle"] == c]
        cyc[c] = {"n": len(rr), "spent": s("spent", rr), "pnl": s("pnl", rr),
                  "alt": s("alt", rr), "btc": s("btc", rr)}
    years = (max(x["d1"] for x in rows) - min(x["d0"] for x in rows)) / DAY / 365
    cap_days = sum(x["spent"] * x["days"] for x in rows)
    return {"n": len(rows), "spent": s("spent"), "pnl": s("pnl"), "alt": s("alt"), "btc": s("btc"),
            "beat_alt_share": sum(1 for x in rows if x["pnl"] > x["alt"]) / len(rows),
            "excess_med": statistics.median(exc), "years": years,
            "avg_capital": cap_days / (years * 365), "cycles": cyc}


def mean_runs(runs: list[dict]) -> dict:
    keys = ["n", "spent", "pnl", "alt", "btc", "beat_alt_share", "excess_med", "years",
            "avg_capital"]
    out = {k: statistics.fmean(r[k] for r in runs) for k in keys}
    out["pnl_min"] = min(r["pnl"] for r in runs)
    out["pnl_max"] = max(r["pnl"] for r in runs)
    out["excess_min"] = min(r["pnl"] - r["alt"] for r in runs)
    out["excess_max"] = max(r["pnl"] - r["alt"] for r in runs)
    cyc: dict = {}
    for c in runs[0]["cycles"]:
        cyc[c] = {k: statistics.fmean(r["cycles"].get(c, {}).get(k, 0) for r in runs)
                  for k in ("n", "spent", "pnl", "alt", "btc")}
    out["cycles"] = cyc
    return out


def main() -> int:
    t0 = time.time()
    coins = L.load_archive()
    all_segs = L.segments(coins)
    ranks = L.vol_ranks(all_segs)
    npairs = pairs_per_day(ranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot = L.load_hot()
    eps_all = [ep for ep in L.build_episodes(segs, ranks, hot, "spring") if ep["cycle"] != "?"]
    J.enrich(eps_all, ranks)
    for ep in eps_all:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
    alt, _btc_cap = market_index(ROOT / "scanner.db")
    btc = btc_closes(all_segs)
    print(f"Пружин: {len(eps_all)}, индекс альтов: {len(alt)} дней, BTC: {len(btc)} дней "
          f"[{time.time() - t0:.0f} с]")
    res_all = J.run_books(eps_all)

    policies = [
        ("все пружины", lambda ep: True, "random", None),
        ("как исполнитель", lambda ep: ep["share"] <= VOL_BOTTOM_SHARE and ep["hot"] == 0,
         "rank", lambda ep: ep["share"] > ILLIQUID_SHARE),
    ]
    out: dict = {"meta": {"episodes": len(eps_all), "slots": J.SLOTS, "budget": J.BUDGET,
                          "fee": FEE, "seeds": J.SEEDS}}
    for b in res_all:
        name = "R правила" if b == "R" else "H держать"
        print(f"\n=== книга {name}: {J.SLOTS} слотов × ${J.BUDGET:g} ===")
        print(f"  {'отбор':18} {'поз.':>5} {'вложено':>8} {'P&L':>8} {'альты':>8} {'BTC':>8} "
              f"{'P&L−альты':>10} {'лучше альтов':>12} {'мед. избыток':>12} {'в год':>7} "
              f"{'работало $':>10}")
        for pname, take, order, quota in policies:
            runs = [evaluate(taken_rows(res_all[b], take, order, quota, sd), alt, btc)
                    for sd in range(J.SEEDS if order == "random" else 1)]
            runs = [r for r in runs if r]
            m = mean_runs(runs)
            out.setdefault(b, {})[pname] = m
            cap = J.SLOTS * J.BUDGET
            print(f"  {pname:18} {m['n']:>5.0f} {m['spent']:>7.0f}$ {m['pnl']:>+7.0f}$ "
                  f"{m['alt']:>+7.0f}$ {m['btc']:>+7.0f}$ {m['pnl'] - m['alt']:>+9.0f}$ "
                  f"{m['beat_alt_share'] * 100:>11.0f}% {m['excess_med'] * 100:>+11.1f}% "
                  f"{m['pnl'] / m['years'] / cap * 100:>+6.1f}% {m['avg_capital']:>9.0f}$")
            for c, v in m["cycles"].items():
                print(f"      {c:12} поз. {v['n']:>4.0f}  вложено {v['spent']:>6.0f}$  "
                      f"P&L {v['pnl']:>+6.0f}$  альты {v['alt']:>+6.0f}$  BTC {v['btc']:>+6.0f}$")
            if order == "random":
                print(f"      разброс по порядку сигналов: P&L {m['pnl_min']:+.0f}…{m['pnl_max']:+.0f}$, "
                      f"P&L−альты {m['excess_min']:+.0f}…{m['excess_max']:+.0f}$")
    print("\n«альты»/«BTC» — та же вложенная сумма в рынок на тех же окнах (вход → выход позиции);"
          " «мед. избыток» — медиана (P&L − альты) / вложено по позициям; «в год» — Σ P&L / лет /"
          f" ${J.SLOTS * J.BUDGET:g} (простая); «работало $» — средняя сумма в позициях.")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
