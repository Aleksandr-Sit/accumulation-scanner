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

После блока C (09.10.2026, раздел «ПОСЛЕ БЛОКА C»): та же книга «как исполнитель», приводимая
к проду по шагам STEPS — бенчмарк по денежным потокам (scanner/benchmark.flow_pnl, как
месячная сводка: каждая ступень покупает ту же сумму в рынок в тот же день, продажа — ту же
долю), правила исполнения (ladder_dca_study.simulate_exec, EXEC_RULES), кулдаун 90 дн. от
взятого входа по всем дням сигнала (а не эпизоды через 90 дн. от сигнала), свободный USDT
книги $1500, без горизонта (открытые в конце данных — по последнему закрытию), детектор
зоны прода. Шаг «было» обязан повторить замер 08.10; разброс по случайному порядку
сигналов и вклад 10 лучших позиций — мерила хрупкости.

Оговорки: архив только Binance; пружина — исследовательский детектор, а не балл ≥ 70;
эпизоды коррелированы; пороги частично in-sample. Индекс альтов взвешен по капе — его
нельзя купить один в один, это ориентир «рынок альтов в целом».

Запуск из корня проекта:  py -3 backtest/portfolio_vs_market.py
Результат: backtest/portfolio_vs_market_results.json
"""
from __future__ import annotations

import heapq
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
from scanner.benchmark import flow_pnl  # noqa: E402

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


# ---------------------------------------------------------------- как исполнитель (блок C)

CAPITAL = 1500.0                 # executor.capital_usdt — виртуальный счёт книги
COOLDOWN_DAYS = 90               # executor.reentry_cooldown_days, от входа (created_ts)
EXEC_POLICY = (lambda ep: ep["share"] <= VOL_BOTTOM_SHARE and ep["hot"] == 0,
               lambda ep: ep["share"] > ILLIQUID_SHARE)
# Шаги приведения к проду: (ключ, подпись, эпизоды, правила simulate_exec, горизонт,
# кулдаун от взятого входа, проверка свободного USDT, бенчмарк по потокам).
STEPS = [
    ("s0", "было: замер 08.10 (сумма в рынок сразу)", "e90", None, 365, False, False, False),
    ("s1", "+ бенчмарк по денежным потокам", "e90", None, 365, False, False, True),
    ("s2", "+ правила исполнения (EXEC_RULES)", "e90", "all", 365, False, False, True),
    ("s3", "+ кулдаун 90 дн. от взятого входа", "every", "all", 365, True, False, True),
    ("s4", "+ свободный USDT книги ($1500)", "every", "all", 365, True, True, True),
    ("s5", "+ без горизонта 365 дн.", "every", "all", None, True, True, True),
    ("s6", "+ детектор зоны прода (0.70, 29 дн.)", "prod", "all", None, True, True, True),
]


def exec_book(cands: list[dict], sim, take, quota, *, cooldown: bool, cash: bool,
              order: str = "rank", seed: int = 0) -> list[tuple[dict, dict]]:
    """Книга исполнителя по дням сигналов: слот занят до выхода (одна позиция на монету,
    не больше J.SLOTS, квота неликвида), кулдаун — COOLDOWN_DAYS от прошлого ВЗЯТОГО входа
    по монете (cooldown=False — его заменяет шаг эпизодов 90 дн. от сигнала, как раньше),
    cash — свободный USDT книги (CAPITAL + выручка − траты − резерв лимиток) не меньше
    стоимости лестницы. risk_check исполнителя (экспозиция по вложенному ≤ 30% / 60% × $1500
    = $750) при 15 × $50 не строже лимита монет — отдельно не считается.
    sim(ep) -> строка simulate_exec | None (отказ плана). -> [(эпизод, строка)]."""
    rnd = random.Random(seed)
    by_day: dict[int, list[dict]] = {}
    for ep in cands:
        by_day.setdefault(ep["day"], []).append(ep)
    held: list[tuple[int, bool, str]] = []
    last: dict[str, int] = {}
    events: list[tuple[int, float]] = []
    free = CAPITAL
    taken = []
    for day in sorted(by_day):
        held = [x for x in held if x[0] > day]
        while events and events[0][0] <= day:
            free += heapq.heappop(events)[1]
        cc = [ep for ep in by_day[day] if take(ep)]
        if order == "rank":
            cc.sort(key=lambda ep: ep["rank"])
        else:
            rnd.shuffle(cc)
        for ep in cc:
            coin = ep["seg"]["sym"].split("#")[0]
            if any(x[2] == coin for x in held):
                continue
            if cooldown and coin in last and day < last[coin] + COOLDOWN_DAYS * DAY:
                continue
            in_q = quota is not None and quota(ep)
            if len(held) >= J.SLOTS or (in_q and sum(1 for x in held if x[1]) >= ILLIQUID_SLOTS):
                continue
            r = sim(ep)
            if r is None:
                continue
            if cash:
                need = -r["cash"][0][1]
                if free + 1e-9 < need:
                    continue
                ts = ep["seg"]["ts"]
                free -= need
                for d, v in r["cash"][1:]:
                    heapq.heappush(events, (ts[d], v))
            held.append((day + max(r["span"], 1) * DAY, in_q, coin))
            last[coin] = day
            taken.append((ep, r))
    return taken


def evaluate_flows(taken: list[tuple[dict, dict]], alt: dict, btc: dict) -> dict:
    """Как evaluate, плюс бенчмарк по денежным потокам (scanner/benchmark.flow_pnl, как
    месячная сводка): каждая покупка — та же сумма в рынок в тот же день, продажа — та же
    доля, комиссия FEE на вход и выход, остаток — по уровню дня выхода."""
    rows = []
    for ep, r in taken:
        spent = r["spent"]
        if spent <= 0:
            continue
        ts = ep["seg"]["ts"]
        d0 = ep["day"]
        d1 = d0 + max(r["span"], 1) * DAY
        la, lb = bench_pnl(spent, alt, d0, d1), bench_pnl(spent, btc, d0, d1)
        flows = [{"ts": ts[d], "side": sd, "usd": usd, "frac": fr} for d, sd, usd, fr in r["flows"]]
        fa = flow_pnl(flows, lambda t: at(alt, t), d1, FEE)
        fb = flow_pnl(flows, lambda t: at(btc, t), d1, FEE)
        if None in (la, lb, fa, fb):
            continue
        rows.append({"d0": d0, "d1": d1, "spent": spent, "pnl": r["pnl"], "alt": fa, "btc": fb,
                     "alt_lump": la, "btc_lump": lb, "cycle": ep["cycle"], "how": r["how"],
                     "days": max(r["span"], 1)})
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
    hows: dict[str, int] = {}
    for x in rows:
        hows[x["how"]] = hows.get(x["how"], 0) + 1
    return {"n": len(rows), "spent": s("spent"), "pnl": s("pnl"), "alt": s("alt"),
            "btc": s("btc"), "alt_lump": s("alt_lump"), "btc_lump": s("btc_lump"),
            "beat_alt_share": sum(1 for x in rows if x["pnl"] > x["alt"]) / len(rows),
            "excess_med": statistics.median(exc), "years": years,
            "avg_capital": sum(x["spent"] * x["days"] for x in rows) / (years * 365),
            "hows": hows, "held_2y": sum(1 for x in rows if x["days"] > 730), "cycles": cyc}


def exec_steps(cands: dict[str, list[dict]], alt: dict, btc: dict, t0: float) -> dict:
    """Приведение замера к исполнителю по шагам STEPS, книги R и H независимо."""
    out: dict = {}
    cache: dict = {}
    for b, (sell, _stop) in J.BOOKS.items():
        name = "R правила" if b == "R" else "H держать"
        print(f"\n--- книга {name}, отбор «как исполнитель», {J.SLOTS} × ${J.BUDGET:g}")
        print(f"  {'шаг':44} {'поз.':>5} {'вложено':>8} {'P&L':>7} {'альты':>7} {'BTC':>7} "
              f"{'P&L−альты':>9} {'P&L−BTC':>8} {'лучше альт':>10} {'работало $':>10}")
        for key, label, src, rules, hz, cool, cash, flows in STEPS:
            rr = L.EXEC_ALL if rules == "all" else None

            def sim(ep, rr=rr, hz=hz, sell=sell, b=b):
                k = (b, ep["seg"]["sym"], ep["e"], rr is not None, hz)
                if k not in cache:
                    cache[k] = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET,
                                               horizon=hz, rules=rr)
                return cache[k]
            taken = exec_book(cands[src], sim, *EXEC_POLICY, cooldown=cool, cash=cash)
            last_taken = taken
            m = evaluate_flows(taken, alt, btc)
            if not flows:
                m = {**m, "alt": m["alt_lump"], "btc": m["btc_lump"]}
            m["label"] = label
            out.setdefault(b, {})[key] = m
            print(f"  {label:44} {m['n']:>5} {m['spent']:>7.0f}$ {m['pnl']:>+6.0f}$ "
                  f"{m['alt']:>+6.0f}$ {m['btc']:>+6.0f}$ {m['pnl'] - m['alt']:>+8.0f}$ "
                  f"{m['pnl'] - m['btc']:>+7.0f}$ {m['beat_alt_share'] * 100:>9.0f}% "
                  f"{m['avg_capital']:>9.0f}$  [{time.time() - t0:.0f} с]")
        fin = out[b][STEPS[-1][0]]
        # хрупкость: тот же финальный шаг при случайном порядке сигналов внутри дня
        key, _l, src, rules, hz, cool, cash, _f = STEPS[-1]
        runs = []
        for sd in range(J.SEEDS):
            def sim(ep, hz=hz, sell=sell, b=b):
                k = (b, ep["seg"]["sym"], ep["e"], True, hz)
                if k not in cache:
                    cache[k] = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET,
                                               horizon=hz, rules=L.EXEC_ALL)
                return cache[k]
            m = evaluate_flows(exec_book(cands[src], sim, *EXEC_POLICY, cooldown=cool,
                                         cash=cash, order="random", seed=sd), alt, btc)
            runs.append((m["pnl"], m["pnl"] - m["alt"], m["pnl"] - m["btc"]))
        fin["random_order"] = {"seeds": J.SEEDS,
                               "pnl": [min(r[0] for r in runs), max(r[0] for r in runs)],
                               "vs_alt": [min(r[1] for r in runs), max(r[1] for r in runs)],
                               "vs_btc": [min(r[2] for r in runs), max(r[2] for r in runs)],
                               "vs_alt_mean": statistics.fmean(r[1] for r in runs),
                               "beat_alt_runs": sum(1 for r in runs if r[1] > 0)}
        ro = fin["random_order"]
        tops = sorted((r["pnl"] for _, r in last_taken), reverse=True)
        fin["top10_pnl"] = sum(tops[:10])
        print(f"      случайный порядок сигналов ({J.SEEDS}): P&L {ro['pnl'][0]:+.0f}…"
              f"{ro['pnl'][1]:+.0f}$, P&L−альты {ro['vs_alt'][0]:+.0f}…{ro['vs_alt'][1]:+.0f}$ "
              f"(лучше альтов в {ro['beat_alt_runs']} из {J.SEEDS}), P&L−BTC {ro['vs_btc'][0]:+.0f}…"
              f"{ro['vs_btc'][1]:+.0f}$; 10 лучших позиций дают {fin['top10_pnl']:+.0f}$")
        for c, v in fin["cycles"].items():
            print(f"      {c:12} поз. {v['n']:>4}  вложено {v['spent']:>6.0f}$  P&L {v['pnl']:>+6.0f}$"
                  f"  альты {v['alt']:>+6.0f}$  BTC {v['btc']:>+6.0f}$")
        print("      выходы: " + ", ".join(f"{k} {v}" for k, v in sorted(fin["hows"].items()))
              + f"; держались > 2 лет: {fin['held_2y']}")
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

    # ---- после блока C: замер «как исполнитель» по шагам приведения к проду
    src = L.signal_days(segs, ranks, hot, {"every": lambda f, cl, t: L.is_spring(f),
                                           "prod": L.is_spring_prod})
    for k in src:
        src[k] = [ep for ep in src[k] if ep["cycle"] != "?"]
        for ep in src[k]:
            n = npairs.get(ep["day"]) or 0
            ep["share"] = ep["rank"] / n if n else 1.0
    src["e90"] = eps_all
    print(f"\n=== ПОСЛЕ БЛОКА C: книги как исполнитель, по шагам приведения к проду ===\n"
          f"  дней сигнала: детектор исследований {len(src['every'])}, детектор прода "
          f"{len(src['prod'])} (эпизодов через 90 дн.: {len(eps_all)})  [{time.time() - t0:.0f} с]")
    out["exec"] = exec_steps(src, alt, btc, t0)
    for b in J.BOOKS:                    # шаг «было» — тот же расчёт, что таблица выше
        a, z = out[b]["как исполнитель"], out["exec"][b]["s0"]
        assert a["n"] == z["n"] and abs(a["pnl"] - z["pnl"]) < 1e-6 and \
            abs(a["alt"] - z["alt"]) < 1e-6, f"{b}: шаг «было» разошёлся с замером ({a['pnl']} / {z['pnl']})"
    out["exec"]["meta"] = {"rules": dict(L.EXEC_RULES), "capital": CAPITAL,
                           "cooldown_days": COOLDOWN_DAYS,
                           "signal_days": {k: len(v) for k, v in src.items()}}
    print("\nПосле блока C «альты»/«BTC» — по денежным потокам (scanner/benchmark.flow_pnl): каждая"
          " ступень покупает ту же сумму в рынок в тот же день, продажа книги продаёт ту же долю;"
          " шаг «было» — вся вложенная сумма в рынок в день сигнала до дня выхода.")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
