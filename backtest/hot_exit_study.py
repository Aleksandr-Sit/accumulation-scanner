"""Выход по перегреву РЫНКА: продавать у хаёв, когда перегрет весь рынок, а не монета.

Вопрос (08.10.2026): portfolio_vs_market показал — книга R отстаёт от индекса альтов на тех
же окнах, H обгоняет его, но лучше альтов лишь треть позиций и почти весь избыток H — дюжина
позиций 2020-22; в 2023-26 обе книги хуже BTC. Улучшит ли книги против рынка выход «у хаёв»
по рыночному сигналу — числу горящих флагов перегрева (L.load_hot: 8 прод-флагов market_daily)?

Вход и книги — как в junk_filter_study / portfolio_vs_market: 5 ступеней по $10 до пола
прод-стопа, горизонт 365 дн., 15 слотов по $50. Варианты выхода:
  H            — база: только стоп (2 закрытия ниже лоу базы −25%);
  H+hot≥3      — плюс продать всё на первом закрытии, когда горит ≥ 3 флагов;
  H+hot≥2      — то же при ≥ 2 флагах;
  H+hot≥3 ½    — половину на первом ≥ 3, остаток на первом ≥ 4 (или стоп / горизонт);
  R            — база: stage8_exit (+50/+150 по трети, трейл 30% после +60%, стоп);
  R+hot≥3      — правила R плюс остаток на первом ≥ 3.
Каждое правило — в двух наборах флагов: «с OI» (прод, 8 флагов) и «без OI» (без oi_rel365:
OI BTCUSDT Bybit в BTC к среднему за год растёт с ростом самой биржи и с ПАДЕНИЕМ цены BTC —
в 2022, на дне альтов, он горел каждый день и чаще всего один). n_lit без OI = n_lit − [OI
горит] — то же, что прод-функция regime.hot_flags без этого флага. Политика «как исполнитель»
— и с прод-холодом (0 флагов с OI на входе), и с холодом без OI.

Время: решение на закрытии дня d по флагам дня d − 1, как у входа (build_episodes:
hot.get(day − DAY)) — строка market_daily дня d на его закрытии ещё не готова (CoinMetrics
публикует день с опозданием). Флаг смотрится, только пока позиция держит монеты (с открытия
e+1); продажа — по закрытию дня d рыночным ордером (L.TAKER); продажа по флагу снимает
неисполненные лимитки на покупку, как обычный выход. Чувствительность: флаги того же дня d
(lag 0) — оптимистичная граница.

simulate ниже — копия ladder_dca_study.simulate с правилом флага. Без правила она сверяется с
L.simulate на всех эпизодах (|Δ P&L| ≤ 1e-9, остальные поля — точно), с правилом — эпизоды
без продажи по флагу обязаны совпасть с базой той же книги (Δ = 0).

ВНИМАНИЕ: пороги флагов подобраны in-sample по тем же вершинам альт-рынка 2018–2025 (config
market_regime) — любое улучшение здесь ВЕРХНЯЯ граница; набор «без OI» выбран после взгляда на
2022 — тоже in-sample. Вершин в истории единицы (см. список периодов), эпизоды коррелированы.
Бенчмарк позиции (альты / BTC на тех же окнах) с выходом по флагу тоже «продаёт у хая»:
P&L − альты меряет отбор монет на укороченном окне, а сдвиг самого Σ P&L против базы — эффект
тайминга. Прочие оговорки — как в portfolio_vs_market.

Запуск из корня проекта:  py -3 backtest/hot_exit_study.py
Результат: backtest/hot_exit_results.json
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import junk_filter_study as J  # noqa: E402
import ladder_dca_study as L  # noqa: E402
import portfolio_vs_market as PV  # noqa: E402

OUT = Path(__file__).resolve().parent / "hot_exit_results.json"
PV_OUT = Path(__file__).resolve().parent / "portfolio_vs_market_results.json"
DAY = 86400
LAG = 1                  # на закрытии дня d известны флаги дня d − 1 (как у входа)
TOP_GAP = 30             # периоды с разрывом ≤ 30 дн. — одна вершина рынка
QUICK = 2                # продажа по флагу на 1–2 день после сигнала = вход в уже горячий рынок
OI_FLAG = "oi_rel365"
FLAGSETS = ("с OI", "без OI")

BASES = {"H": "H", "R": "R"}
# (имя, книга, правило): all — продать всё при ≥ all флагах; half — половину при ≥ half
RULES = [
    ("H+hot≥3", "H", {"all": 3}),
    ("H+hot≥2", "H", {"all": 2}),
    ("H+hot≥3 ½", "H", {"half": 3, "all": 4}),
    ("R+hot≥3", "R", {"all": 3}),
]
# порядок строк: база книги, затем её правила в обоих наборах флагов
VARIANTS = ([("H", "H", None, None)]
            + [(f"{n} {fs}", b, r, fs) for n, b, r in RULES if b == "H" for fs in FLAGSETS]
            + [("R", "R", None, None)]
            + [(f"{n} {fs}", b, r, fs) for n, b, r in RULES if b == "R" for fs in FLAGSETS])


def _cold(key: str):
    return lambda ep: ep["share"] <= PV.VOL_BOTTOM_SHARE and ep[key] == 0


_QUOTA = lambda ep: ep["share"] > PV.ILLIQUID_SHARE  # noqa: E731
POLICIES = [   # первые две — как в portfolio_vs_market.main
    ("все пружины", lambda ep: True, "random", None),
    ("как исполнитель", _cold("hot"), "rank", _QUOTA),
    ("исполнитель, холод без OI", _cold("hot_x"), "rank", _QUOTA),
]


def ymd(d: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(d))


# ---------------------------------------------------------------- флаги рынка

def load_flags() -> tuple[dict[int, int], dict[int, int], dict[int, dict]]:
    """(n_lit прод = L.load_hot(), n_lit без OI, полные hot_flags по дням)."""
    from market_regime_study import load_market
    from scanner.config import load_config
    _, full = load_market(load_config(None))
    hot = L.load_hot()
    assert hot == {d: h.get("n_lit", 0) for d, h in full.items()}, "L.load_hot ≠ load_market"
    assert all(h["n_lit"] == len(h["lit"]) for h in full.values())
    hot_x = {d: h["n_lit"] - (OI_FLAG in (h.get("lit") or [])) for d, h in full.items()}
    return hot, hot_x, full


# ---------------------------------------------------------------- симуляция

def simulate(seg: dict, e: int, buy: dict, sell: dict, budget: float = 100.0,
             stop: str | None = "prod", valid: int = L.BUY_VALID,
             stop_buf: float = L.INVAL_BUF, hot: dict[int, int] | None = None,
             rule: dict | None = None, lag: int = LAG) -> dict:
    """Копия L.simulate (горизонт — L.HORIZON на момент вызова) + выход по флагам рынка.

    rule: None — ровно L.simulate; {"all": k} — продать всё на закрытии первого дня, когда
    горит ≥ k флагов; {"half": k, "all": m} — половину остатка на первом ≥ k, всё на ≥ m.
    Флаги — дня ts[d] − lag·DAY; проверка только пока позиция держит монеты."""
    o, h, lo, c = seg["o"], seg["h"], seg["l"], seg["c"]
    ts = seg["ts"]
    n = len(c)
    end = min(e + L.HORIZON, n - 1)
    p0 = c[e]
    base_low = min(c[max(0, e - 30):e + 1])
    floor = base_low * (1 - L.INVAL_BUF)

    # --- план покупок: (день рыночной покупки | None, лимит-цена | None, $)
    kind = buy["kind"]
    orders: list[dict] = []
    if kind == "single":
        orders.append({"mkt": e + 1, "px": None, "usd": budget})
    elif kind == "dca":
        per = budget / buy["n"]
        for k in range(buy["n"]):
            orders.append({"mkt": e + 1 + k * buy["every"], "px": None, "usd": per})
    else:
        nr = buy["n"]
        per = budget / nr
        orders.append({"mkt": e + 1, "px": None, "usd": per})
        if kind == "floor":
            bottom = floor * 1.05
            step_px = (p0 - bottom) / (nr - 1)
            prices = [p0 - k * step_px for k in range(1, nr)]
        else:
            prices = [p0 * (1 - k * buy["step"]) for k in range(1, nr)]
        for px in prices:
            orders.append({"mkt": None, "px": px, "usd": per})
    lowest = min([x["px"] for x in orders if x["px"]] or [p0])
    stop_px = None
    if stop == "prod":
        stop_px = base_low * (1 - stop_buf)
    elif stop == "below":
        stop_px = min(floor, lowest * 0.85)

    paired = sell.get("paired")
    levels = sell.get("levels") or []
    trail = sell.get("trail")
    hot = hot or {}

    held = 0.0
    bought_qty = 0.0
    cost = 0.0                  # сумма потраченных $ (для средней цены)
    spent = 0.0
    proceeds = 0.0
    lots: list[dict] = []       # парный режим: {qty, tp, order}
    li = 0                      # индекс следующей ступени продажи
    buys_open = True
    hwm = 0.0
    below = 0
    n_buys = n_sells = 0
    first_day = None
    exit_day = end
    how = "horizon"
    mae = 0.0
    small = 0
    hot_day = None              # день флагов, по которым была первая продажа по перегреву
    hot_d = None                # через сколько дней после сигнала
    half_done = False

    def buy_fill(order, px, fee, day):
        nonlocal held, bought_qty, cost, spent, n_buys, first_day
        q = order["usd"] / px * (1 - fee)
        held += q
        bought_qty += q
        cost += order["usd"]
        spent += order["usd"]
        n_buys += 1
        order["done"] = True
        if first_day is None:
            first_day = day
        if paired:
            lots.append({"qty": q, "tp": px * (1 + paired), "order": order, "day": day})

    def sell_qty(q, px, fee):
        nonlocal held, proceeds, n_sells, small
        q = min(q, held)
        if q <= 0:
            return
        if q * px < L.MIN_ORDER:            # меньше минимума биржи — продаём остаток целиком
            small += 1
            q = held
            if q * px < L.MIN_ORDER:         # пыль: конвертация остатков с потерей ~5%
                proceeds += q * px * 0.95
                held = 0.0
                return
        proceeds += q * px * (1 - fee)
        held -= q
        n_sells += 1

    for d in range(e + 1, end + 1):
        held_start = held
        # 1) покупки
        if buys_open:
            for od in orders:
                if od.get("done"):
                    continue
                if od["mkt"] is not None:
                    if od["mkt"] == d:
                        buy_fill(od, o[d], L.TAKER, d)
                elif d <= e + valid and lo[d] <= od["px"]:
                    buy_fill(od, min(od["px"], o[d]), L.MAKER, d)
            if d > e + valid and all(od.get("done") or od["mkt"] is None for od in orders) \
                    and not paired:
                buys_open = False
        # 2) продажи (только то, что было на начало дня)
        if held_start > 0:
            if paired:
                for lot in list(lots):
                    if lot["day"] >= d or h[d] < lot["tp"]:
                        continue
                    sell_qty(lot["qty"], max(lot["tp"], o[d]), L.MAKER)
                    lots.remove(lot)
                    if held <= 0:
                        lots.clear()
                        break
            elif li < len(levels):
                avg = cost / bought_qty if bought_qty > 0 else p0
                sellable = held_start
                while li < len(levels) and sellable > 0 and h[d] >= avg * (1 + levels[li][0]):
                    q = min(levels[li][1] * bought_qty, sellable)
                    before = held
                    sell_qty(q, max(avg * (1 + levels[li][0]), o[d]), L.MAKER)
                    sellable -= before - held
                    li += 1
                    buys_open = False            # начали продавать — докупать поздно
        # 3) по закрытию: стоп, трейл, просадка позиции
        if bought_qty > 0:
            hwm = max(hwm, c[d])
            mtm = (proceeds + held * c[d]) / spent - 1 if spent > 0 else 0.0
            mae = min(mae, mtm)
        if stop_px is not None and held > 0:
            below = below + 1 if c[d] < stop_px else 0
            if below >= L.INVAL_CONFIRM:
                sell_qty(held, c[d], L.TAKER)
                exit_day, how = d, "стоп"
                break
        if trail and held > 0 and bought_qty > 0:
            avg = cost / bought_qty
            if hwm >= avg * (1 + trail[0]) and c[d] <= hwm * (1 - trail[1]):
                sell_qty(held, c[d], L.TAKER)
                exit_day, how = d, "трейл"
                break
        # 3б) НОВОЕ: перегрев рынка — флаги, известные на закрытии дня d
        if rule and held > 0:
            lit = hot.get(ts[d] - lag * DAY) or 0
            sell_all = lit >= rule["all"]
            if sell_all or ("half" in rule and not half_done and lit >= rule["half"]):
                if hot_day is None:
                    hot_day, hot_d = ts[d] - lag * DAY, d - e
                buys_open = False                # лимитки на покупку снимаются
                if sell_all:
                    sell_qty(held, c[d], L.TAKER)
                else:
                    half_done = True
                    sell_qty(held / 2, c[d], L.TAKER)   # < $5 — sell_qty продаст всё
                if held <= 1e-12:
                    exit_day, how = d, "перегрев"
                    break
        if bought_qty > 0 and held <= 1e-12 and not (paired and buys_open and d <= e + valid):
            exit_day, how = d, "продано"
            break
    else:
        if held > 0:
            sell_qty(held, c[end], L.TAKER)
        if end < e + L.HORIZON:
            how = ("конец данных" if seg["alive"] else
                   "переименование" if seg["swap_end"] else "делистинг")
    pnl = proceeds - spent
    return {"pnl": pnl, "rob": pnl / budget, "rod": pnl / spent if spent else 0.0,
            "deployed": spent / budget, "n_buys": n_buys, "n_sells": n_sells,
            "days": exit_day - (first_day or e), "span": exit_day - e, "budget": budget,
            "how": how, "mae": mae, "small": small,
            "hot_day": hot_day, "hot_d": hot_d, "half": half_done}


def run_variant(eps: list[dict], book: str, rule: dict | None, hot: dict[int, int] | None,
                lag: int = LAG) -> list[dict]:
    """Как J.run_books для одной книги, но своей simulate с правилом флага."""
    sell, stop = J.BOOKS[book]
    return [dict(simulate(ep["seg"], ep["e"], J.BUY5, sell, budget=J.BUDGET, stop=stop,
                          stop_buf=0.25, hot=hot, rule=rule, lag=lag), ep=ep) for ep in eps]


def verify(mine: list[dict], ref: list[dict], book: str) -> dict:
    """Копия без правила против L.simulate: P&L ± 1e-9, остальные поля L.simulate — точно."""
    assert len(mine) == len(ref), f"{book}: {len(mine)} эпизодов против {len(ref)}"
    worst, bad, first = 0.0, 0, None
    for a, b in zip(mine, ref):
        dp = abs(a["pnl"] - b["pnl"])
        worst = max(worst, dp)
        same = dp <= 1e-9 and all(
            (abs(a[k] - b[k]) <= 1e-9 if isinstance(b[k], float) else a[k] == b[k])
            for k in b if k != "ep")
        if not same:
            bad += 1
            first = first or (b["ep"]["seg"]["sym"], ymd(b["ep"]["day"]))
    assert bad == 0, f"{book}: копия simulate расходится с L.simulate в {bad} эпизодах ({first})"
    return {"episodes": len(ref), "mismatch": bad, "max_abs_dpnl": worst}


def check_untouched(rows: list[dict], base: list[dict], name: str) -> None:
    """Без продажи по флагу путь варианта обязан совпасть с базой той же книги."""
    bad = sum(1 for r, b in zip(rows, base) if r["hot_day"] is None
              and (abs(r["pnl"] - b["pnl"]) > 1e-9 or r["span"] != b["span"]))
    assert bad == 0, f"{name}: {bad} эпизодов без флага разошлись с базой"


# ---------------------------------------------------------------- метрики

def ep_stats(rows: list[dict]) -> dict:
    s = J.stats(rows)
    n = s["n"]
    s["hot_any"] = sum(1 for r in rows if r["hot_day"] is not None) / n
    s["hot_exit"] = sum(1 for r in rows if r["how"] == "перегрев") / n
    s["hot_quick"] = sum(1 for r in rows if r["hot_d"] is not None and r["hot_d"] <= QUICK) / n
    s["stop"] = sum(1 for r in rows if r["how"] == "стоп") / n
    s["span"] = statistics.fmean(max(r["span"], 1) for r in rows)
    return s


def book_vs_market(rows: list[dict], alt: dict, btc: dict) -> dict[str, dict]:
    """portfolio_vs_market для одного варианта: все политики, среднее по порядкам сигналов."""
    out = {}
    for pname, take, order, quota in POLICIES:
        runs, hot_sh = [], []
        for sd in range(J.SEEDS if order == "random" else 1):
            taken = PV.taken_rows(rows, take, order, quota, sd)
            r = PV.evaluate(taken, alt, btc)
            if r:
                runs.append(r)
                hot_sh.append(sum(1 for x in taken if x["hot_day"] is not None) / len(taken))
        m = PV.mean_runs(runs)
        m["hot_share"] = statistics.fmean(hot_sh)
        out[pname] = m
    return out


def spells(hot: dict[int, int], thr: int) -> list[list[int]]:
    """Подряд идущие дни с ≥ thr флагов: [[начало, конец], ...]."""
    out: list[list[int]] = []
    for d in sorted(x for x, v in hot.items() if v >= thr):
        if out and d - out[-1][1] == DAY:
            out[-1][1] = d
        else:
            out.append([d, d])
    return out


def tops(sp: list[list[int]], gap: int = TOP_GAP) -> list[dict]:
    """Периоды, между которыми ≤ gap дней, — одна вершина: {a, b, lit_days, spells}."""
    out: list[dict] = []
    for a, b in sp:
        k = (b - a) // DAY + 1
        if out and a - out[-1]["b"] <= gap * DAY:
            out[-1]["b"] = b
            out[-1]["lit_days"] += k
            out[-1]["spells"] += 1
        else:
            out.append({"a": a, "b": b, "lit_days": k, "spells": 1})
    return out


def attribution(rows: list[dict], base: list[dict], tp: list[dict], cold_key: str) -> list[dict]:
    """ΣΔ P&L варианта против базы той же книги по вершине, где сработал первый флаг: все
    пружины и фильтр исполнителя без слотов (оборот не в нижней четверти, холод на входе —
    по тому же набору флагов)."""
    out = []
    for t in tp:
        sel = [(r, b) for r, b in zip(rows, base)
               if r["hot_day"] is not None and t["a"] <= r["hot_day"] <= t["b"]]
        if not sel:
            continue
        ex = [(r, b) for r, b in sel
              if r["ep"]["share"] <= PV.VOL_BOTTOM_SHARE and r["ep"][cold_key] == 0]
        out.append({"top": f"{ymd(t['a'])} – {ymd(t['b'])}", "n": len(sel),
                    "d_pnl": sum(r["pnl"] - b["pnl"] for r, b in sel),
                    "n_exec": len(ex), "d_pnl_exec": sum(r["pnl"] - b["pnl"] for r, b in ex)})
    return out


# ---------------------------------------------------------------- печать

def pc(x: float) -> str:
    return f"{x * 100:+.1f}%"


def print_episodes(title: str, st: dict[str, dict]) -> None:
    print(f"\n=== {title} ===")
    print(f"  {'вариант':16} {'n':>5} {'мед.':>7} {'средн.':>7} {'p10':>7} {'win':>4} "
          f"{'по флагу':>8} {'закрыто флагом':>14} {'сразу':>5} {'стоп':>5} {'дни':>4} "
          f"{'Σ P&L':>8}")
    for name, s in st.items():
        print(f"  {name:16} {s['n']:>5} {pc(s['med']):>7} {pc(s['mean']):>7} {pc(s['p10']):>7} "
              f"{s['win'] * 100:>3.0f}% {s['hot_any'] * 100:>7.0f}% {s['hot_exit'] * 100:>13.0f}% "
              f"{s['hot_quick'] * 100:>4.0f}% {s['stop'] * 100:>4.0f}% {s['span']:>4.0f} "
              f"{s['pnl']:>+7.0f}$")
    print(f"  (доходность на бюджет ${J.BUDGET:g}, net-of-fees; «по флагу» — была продажа по "
          f"перегреву (для ½ — хотя бы половина), «закрыто флагом» — позиция закрыта флагом "
          f"целиком, «сразу» — продажа по флагу на 1–{QUICK} день после сигнала (вход в уже "
          f"горячий рынок), «дни» — средняя длина позиции)")


def print_portfolio(pname: str, table: dict[str, dict]) -> None:
    cap = J.SLOTS * J.BUDGET
    print(f"\n=== Портфель «{pname}»: {J.SLOTS} слотов × ${J.BUDGET:g}, рынок — те же $ "
          f"на тех же окнах ===")
    print(f"  {'вариант':16} {'поз.':>5} {'вложено':>8} {'P&L':>8} {'альты':>8} {'BTC':>8} "
          f"{'P&L−альты':>10} {'лучше альтов':>12} {'мед. избыток':>12} {'по флагу':>8} "
          f"{'в год':>7} {'работало $':>10}")
    for name, m in table.items():
        print(f"  {name:16} {m['n']:>5.0f} {m['spent']:>7.0f}$ {m['pnl']:>+7.0f}$ "
              f"{m['alt']:>+7.0f}$ {m['btc']:>+7.0f}$ {m['pnl'] - m['alt']:>+9.0f}$ "
              f"{m['beat_alt_share'] * 100:>11.0f}% {m['excess_med'] * 100:>+11.1f}% "
              f"{m['hot_share'] * 100:>7.0f}% {m['pnl'] / m['years'] / cap * 100:>+6.1f}% "
              f"{m['avg_capital']:>9.0f}$")
    print("  по циклам входа — P&L / альты / BTC, $ (поз.):")
    for name, m in table.items():
        cells = [f"{c} {v['pnl']:+5.0f} / {v['alt']:+5.0f} / {v['btc']:+5.0f} ({v['n']:.0f})"
                 for c, v in m["cycles"].items()]
        print(f"  {name:16} " + " · ".join(cells))
    if any(m.get("pnl_min") != m.get("pnl_max") for m in table.values()):
        print("  разброс по порядку сигналов, P&L−альты: " + ", ".join(
            f"{name} {m['excess_min']:+.0f}…{m['excess_max']:+.0f}$" for name, m in table.items()))


# ---------------------------------------------------------------- main

def main() -> int:
    t0 = time.time()
    coins = L.load_archive()
    all_segs = L.segments(coins)
    ranks = L.vol_ranks(all_segs)
    npairs = PV.pairs_per_day(ranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot, hot_x, full = load_flags()
    flagsets = {"с OI": hot, "без OI": hot_x}
    eps_all = [ep for ep in L.build_episodes(segs, ranks, hot, "spring") if ep["cycle"] != "?"]
    J.enrich(eps_all, ranks)
    for ep in eps_all:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
        ep["hot_x"] = hot_x.get(ep["day"] - DAY)            # вход: флаги дня до сигнала
    alt, _btc_cap = PV.market_index(ROOT / "scanner.db")
    btc = PV.btc_closes(all_segs)
    print(f"Пружин: {len(eps_all)}, флаги рынка: {len(hot)} дней ({ymd(min(hot))} – "
          f"{ymd(max(hot))}), индекс альтов: {len(alt)} дней, BTC: {len(btc)} дней "
          f"[{time.time() - t0:.0f} с]")

    out: dict = {"meta": {"episodes": len(eps_all), "slots": J.SLOTS, "budget": J.BUDGET,
                          "horizon": J.HORIZON, "fee_bench": PV.FEE, "taker": L.TAKER,
                          "seeds": J.SEEDS, "lag_days": LAG, "top_gap_days": TOP_GAP,
                          "caveat": "пороги флагов in-sample по вершинам 2018–2025, набор "
                                    "без OI выбран после взгляда на 2022: улучшение — "
                                    "верхняя граница"}}

    # ---- флаг OI и холод на входе
    oi_rows = {}
    for y in sorted({time.gmtime(d).tm_year for d in full}):
        ds = [d for d in full if time.gmtime(d).tm_year == y]
        lit = [d for d in ds if OI_FLAG in full[d]["lit"]]
        if lit:
            oi_rows[y] = {"days": len(ds), "oi_lit": len(lit),
                          "oi_only": sum(1 for d in lit if full[d]["lit"] == [OI_FLAG])}
    out["oi_flag"] = oi_rows
    print(f"\n{OI_FLAG} горел: " + ", ".join(
        f"{y} — {v['oi_lit'] / v['days'] * 100:.0f}% дней (единственный — "
        f"{v['oi_only'] / v['days'] * 100:.0f}%)" for y, v in oi_rows.items()))
    cold = {fs: sum(1 for ep in eps_all if ep["hot" if fs == "с OI" else "hot_x"] == 0)
            for fs in FLAGSETS}
    print("Пружин с холодным рынком на входе (0 флагов): " + ", ".join(
        f"{fs} {v}" for fs, v in cold.items()) + f" из {len(eps_all)}")
    out["meta"]["cold_entries"] = cold

    # ---- периоды горящих флагов
    sp = {(fs, k): spells(hs, k) for fs, hs in flagsets.items() for k in (2, 3, 4)}
    tp = {key: tops(v) for key, v in sp.items()}
    first_ep = min(ep["day"] for ep in eps_all)
    print(f"\n=== Периоды горящих флагов (подряд идущие дни; первая пружина {ymd(first_ep)}) ===")
    for fs in FLAGSETS:
        for k in (3, 2):
            days_lit = sum((b - a) // DAY + 1 for a, b in sp[(fs, k)])
            print(f"  {fs}, ≥ {k} флагов: {len(sp[(fs, k)])} периодов, {days_lit} дн.; слив "
                  f"разрывов ≤ {TOP_GAP} дн. — {len(tp[(fs, k)])} вершин:")
            for t in tp[(fs, k)]:
                print(f"    вершина {ymd(t['a'])} – {ymd(t['b'])}: горело {t['lit_days']} дн. "
                      f"в {t['spells']} период(ах)")
            print("    периоды: " + ", ".join(
                f"{ymd(a)}–{ymd(b)[5:]}" if a != b else ymd(a) for a, b in sp[(fs, k)]))
    out["spells"] = {f"{fs} hot≥{k}": [[ymd(a), ymd(b), (b - a) // DAY + 1] for a, b in v]
                     for (fs, k), v in sp.items()}
    out["tops"] = {f"{fs} hot≥{k}": [{**t, "a": ymd(t["a"]), "b": ymd(t["b"])} for t in v]
                   for (fs, k), v in tp.items()}

    # ---- варианты и сверка копии simulate с L.simulate
    ref = J.run_books(eps_all)                  # L.simulate, ставит L.HORIZON = 365
    assert L.HORIZON == J.HORIZON
    res = {name: run_variant(eps_all, book, rule, flagsets.get(fs))
           for name, book, rule, fs in VARIANTS}
    out["verify"] = {b: verify(res[b], ref[b], b) for b in BASES}
    for name, book, rule, _fs in VARIANTS:
        if rule:
            check_untouched(res[name], res[book], name)
    print(f"\nСверка копии simulate с L.simulate (без правила флага): " + "; ".join(
        f"{b} — {v['episodes'] - v['mismatch']}/{v['episodes']} совпали, макс. |ΔP&L| "
        f"{v['max_abs_dpnl']:.1e}" for b, v in out["verify"].items())
          + f"; эпизоды без продажи по флагу = база [{time.time() - t0:.0f} с]")

    # ---- 1. эпизоды
    st = {name: ep_stats(rows) for name, rows in res.items()}
    out["episodes"] = st
    print_episodes(f"1. Эпизоды: все пружины, 5×$10 до пола, {J.HORIZON} дн., флаги дня d−{LAG}",
                   st)

    # ---- 2. портфель против рынка
    pf = {name: book_vs_market(rows, alt, btc) for name, rows in res.items()}
    out["portfolio"] = pf
    for pname in ("как исполнитель", "исполнитель, холод без OI", "все пружины"):
        print_portfolio(pname, {name: pf[name][pname] for name in pf})
    print("  «альты»/«BTC» — та же вложенная сумма в рынок на тех же окнах (вход → выход "
          "позиции; с выходом по флагу окно короче — рынок тоже «продан у хая»); «по флагу» — "
          "доля взятых позиций с продажей по перегреву; «в год» — Σ P&L / лет / "
          f"${J.SLOTS * J.BUDGET:g}; «холод без OI» — вход при 0 флагов без {OI_FLAG}.")
    try:   # база обязана совпасть с portfolio_vs_market_results.json
        prev = json.loads(PV_OUT.read_text(encoding="utf-8"))
        diffs = {f"{b} {p}": pf[b][p]["pnl"] - prev[b][p]["pnl"]
                 for b in BASES for p in ("все пружины", "как исполнитель")}
        ok = all(abs(v) < 1e-6 for v in diffs.values())
        out["verify"]["portfolio_vs_market"] = {"match": ok, "d_pnl": diffs}
        print(f"  сверка базы с {PV_OUT.name}: " + ("совпало" if ok else f"РАСХОЖДЕНИЕ {diffs}"))
    except (OSError, KeyError, ValueError) as e:
        print(f"  сверка с {PV_OUT.name} пропущена: {e}")

    # ---- 3. где сработал флаг: вклад вершин
    print("\n=== 3. Вклад вершин: ΣΔ P&L против базы той же книги по эпизодам (без слотов), "
          "вершина — где сработал первый флаг ===")
    out["attribution"] = {}
    for name, book, rule, fs in VARIANTS:
        if not rule:
            continue
        k = min(rule.values())
        att = attribution(res[name], res[book], tp[(fs, k)],
                          "hot" if fs == "с OI" else "hot_x")
        out["attribution"][name] = att
        print(f"  {name} (вершины ≥ {k}): Σ все пружины {sum(a['d_pnl'] for a in att):+.0f}$, "
              f"фильтр исполнителя {sum(a['d_pnl_exec'] for a in att):+.0f}$")
        for a in att:
            print(f"    {a['top']}: продаж {a['n']:>4}, ΔP&L {a['d_pnl']:>+7.0f}$ | "
                  f"исполнитель: {a['n_exec']:>3}, {a['d_pnl_exec']:>+6.0f}$")

    # ---- 4. чувствительность ко времени флага
    print("\n=== 4. Чувствительность: флаги того же дня d (lag 0) — оптимистично, данные дня "
          "на его закрытии ещё не опубликованы ===")
    print(f"  {'вариант':16} {'мед.':>7} {'средн.':>7} {'win':>4} {'по флагу':>8} {'Σ P&L':>8} | "
          f"{'исполн. P&L':>11} {'−альты':>7} | {'холод без OI':>12} {'−альты':>7} | "
          f"{'все пружины':>11} {'−альты':>7}")
    out["lag0"] = {}
    for name, book, rule, fs in VARIANTS:
        if not rule:
            continue
        rows0 = run_variant(eps_all, book, rule, flagsets[fs], lag=0)
        check_untouched(rows0, res[book], name + " lag0")
        s0, p0 = ep_stats(rows0), book_vs_market(rows0, alt, btc)
        out["lag0"][name] = {"episodes": s0, "portfolio": p0}
        cells = " | ".join(f"{p0[p]['pnl']:>+10.0f}$ {p0[p]['pnl'] - p0[p]['alt']:>+6.0f}$"
                           for p in ("как исполнитель", "исполнитель, холод без OI",
                                     "все пружины"))
        print(f"  {name:16} {pc(s0['med']):>7} {pc(s0['mean']):>7} {s0['win'] * 100:>3.0f}% "
              f"{s0['hot_any'] * 100:>7.0f}% {s0['pnl']:>+7.0f}$ | {cells}")

    print("\nВНИМАНИЕ: пороги флагов перегрева подобраны in-sample по тем же вершинам альт-рынка "
          "2018–2025 (а набор без OI — после взгляда на 2022) — любое улучшение от выхода по "
          "флагу здесь ВЕРХНЯЯ граница; вершин единицы.")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
