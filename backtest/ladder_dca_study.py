"""Лестница входа и выхода ордерами от $10: что она даёт против входа разом.

Вопросы:
  1. Покупка лестницей (лимитки ниже цены / покупки по времени) против покупки разом
     по сигналу «пружина» — на весь бюджет и на реально вложенные деньги.
  2. Продажа лестницей: прод-выход (+50/+150 по трети, трейл) против ровной лестницы,
     «циклической» и парных ордеров (у каждой ступени своя цель продажи).
  3. Нужна ли инвалидация, когда в данных есть умершие монеты.
  4. Какие признаки на входе отсекают трупы: ликвидность (ранг оборота), возраст листинга.
  5. Сколько монет держать: бутстрэп портфелей из эпизодов одного полугодия.

Данные: backtest/binance_archive.py — дневные OHLCV всех USDT-пар Binance с 2017,
ВКЛЮЧАЯ ДЕЛИСТНУТЫЕ (data.binance.vision). Позиция по монете, которую делистнули,
закрывается по последнему закрытию перед делистингом. Ряды режутся на отрезки по
разрывам > 10 дней (старый LUNA и новый LUNA — разные монеты).

Модель исполнения (дневные свечи, консервативно):
  • рыночная покупка — по open следующего дня, 0.15% (комиссия 0.1% + спред);
  • лимитка на покупку исполняется, если low ≤ цены, по min(цена, open), 0.1%;
  • лимитка на продажу — если high ≥ цели, по max(цель, open), 0.1%;
  • купленное сегодня продаётся не раньше завтра; инвалидация и трейл — по закрытиям
    (как в проде: 2 закрытия ниже лоу базы −25%, трейл 30% после +60%);
  • ордер продажи меньше $5 (минимум Bybit) сливается с остатком позиции.
Не учтено: проскальзывание крупных ордеров (при $10–100 несущественно), налоги,
доход на свободный кэш (считается нулевым).

Пороги сигнала — feature_study.is_spring (детектор исследований, cooldown 90д).
Запуск из корня проекта:  python backtest/ladder_dca_study.py
Результат: backtest/ladder_dca_results.json
"""
from __future__ import annotations

import json
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from binance_archive import load as load_archive  # noqa: E402
from feature_study import COOLDOWN, features, is_spring  # noqa: E402

OUT = Path(__file__).resolve().parent / "ladder_dca_results.json"
DAY = 86400
HORIZON = 730
TAKER = 0.0015           # рыночный ордер: 0.1% комиссия + ~0.05% спред
MAKER = 0.0010           # лимитный ордер: 0.1% (Bybit spot, базовый уровень)
MIN_ORDER = 5.0          # минимальный ордер Bybit spot, USDT
INVAL_BUF = 0.25         # как stage8_exit.invalidation_below_base_low_pct
INVAL_CONFIRM = 2
BUY_VALID = 120          # сколько дней живут лимитки на покупку
GAP_SPLIT = 10           # разрыв ряда > N дней = другая монета/релистинг
CYCLES = [("2018-20", 1514764800, 1585699200),
          ("2020-22", 1585699200, 1672531200),
          ("2023-26", 1672531200, 1893456000)]
# Ряд оборвался не от смерти, а от переименования/свопа (стоимость перешла в новый тикер).
# Выход по последнему закрытию для них честный, но в счётчик делистингов их не берём.
RENAMES = {"NPXSUSDT", "ERDUSDT", "STRATUSDT", "LENDUSDT", "KEEPUSDT", "NUUSDT", "XZCUSDT",
           "RNDRUSDT", "BCHABCUSDT", "BCCUSDT", "AGIXUSDT", "OCEANUSDT", "ANYUSDT", "MCOUSDT",
           "TOMOUSDT", "MKRUSDT", "POLYUSDT", "BTTUSDT", "FTMUSDT", "GALUSDT", "MATICUSDT",
           "TVKUSDT", "MFTUSDT", "MCUSDT", "EOSUSDT", "STORMUSDT", "RGTUSDT", "COCOSUSDT",
           "DARUSDT", "VENUSDT"}

# ---------------------------------------------------------------- варианты

BUY = {
    "разом":              {"kind": "single"},
    "4 ступени до пола":  {"kind": "floor", "n": 4},
    "5 ступеней −10%":    {"kind": "ladder", "n": 5, "step": 0.10},
    "8 недель по $":      {"kind": "dca", "n": 8, "every": 7},
}
SELL = {
    "прод +50/+150+трейл": {"levels": [(0.5, 1 / 3), (1.5, 1 / 3)], "trail": (0.60, 0.30)},
    "ровная +30/60/100/200": {"levels": [(0.3, .25), (0.6, .25), (1.0, .25), (2.0, .25)]},
    "цикл +100/200/400+трейл": {"levels": [(1.0, .25), (2.0, .25), (4.0, .25)],
                                "trail": (0.60, 0.30)},
    "парная +40% каждой":  {"paired": 0.40},
}


# ---------------------------------------------------------------- данные

def _split_points(d: dict) -> tuple[list[int], set[int]]:
    """Где резать ряд: релистинг (разрыв > GAP_SPLIT дней) или редоминация — торги стояли
    2+ дня, а open после паузы отличается от последнего закрытия в 3+ раза (COCOS ×1295,
    DREP ×107, BNX /100, QUICK и SUN /1000, VIDT /10). Обвал без паузы (LUNA 05.2022) —
    настоящий, не режется. Возвращает (точки, точки-свопы)."""
    ts, o, c = d["ts"], d["o"], d["c"]
    cuts, swaps = [], set()
    for i in range(1, len(ts)):
        gap = (ts[i] - ts[i - 1]) // DAY
        if gap > GAP_SPLIT:
            cuts.append(i)
        elif gap > 2 and c[i - 1] > 0 and not (1 / 3 < o[i] / c[i - 1] < 3):
            cuts.append(i)
            swaps.add(i)
    return cuts, swaps


def segments(coins: dict, min_len: int = 30) -> list[dict]:
    """Режет ряды по разрывам и редоминациям: {sym, ts, o, h, l, c, qv, alive, swap_end}.
    alive — торгуется сейчас (только последний отрезок живой пары); swap_end — отрезок
    оборвался свопом/переименованием, а не делистингом."""
    out = []
    for sym, d in coins.items():
        pts, swaps = _split_points(d)
        cut = [0] + pts + [len(d["ts"])]
        for k in range(len(cut) - 1):
            a, b = cut[k], cut[k + 1]
            if b - a < min_len:
                continue
            seg = {key: d[key][a:b] for key in ("ts", "o", "h", "l", "c", "qv")}
            seg["sym"] = sym if k == 0 else f"{sym}#{k}"
            last = k == len(cut) - 2
            seg["alive"] = bool(d.get("trading")) and last
            seg["swap_end"] = (b in swaps) or (last and sym in RENAMES)
            out.append(seg)
    return out


def vol_ranks(segs: list[dict]) -> dict[tuple[str, int], int]:
    """(sym, day) -> ранг 30-дневного среднего оборота среди всех пар в этот день (1 = max)."""
    by_day: dict[int, list[tuple[float, str]]] = {}
    for s in segs:
        qv, ts = s["qv"], s["ts"]
        acc = 0.0
        for i in range(len(qv)):
            acc += qv[i]
            if i >= 30:
                acc -= qv[i - 30]
            if i >= 29:
                by_day.setdefault(ts[i], []).append((acc / 30, s["sym"]))
    ranks = {}
    for day, rows in by_day.items():
        rows.sort(reverse=True)
        for r, (_, sym) in enumerate(rows, 1):
            ranks[(sym, day)] = r
    return ranks


def load_hot() -> dict[int, int]:
    """day -> число горящих флагов перегрева рынка (прод-функции, market_daily)."""
    try:
        from market_regime_study import load_market
        from scanner.config import load_config
        _, hot = load_market(load_config(None))
        return {d: h.get("n_lit", 0) for d, h in hot.items()}
    except (SystemExit, Exception) as e:  # нет market_daily — разрез по рынку пропускаем
        print(f"⚠ флаги рынка недоступны: {e}")
        return {}


def build_episodes(segs, ranks, hot, mode: str) -> list[dict]:
    eps = []
    for s in segs:
        cl, qv, ts = s["c"], s["qv"], s["ts"]
        n, t = len(cl), 200
        while t < n - 30:
            if mode == "spring":
                f = features(cl, qv, t)
                if not (f and is_spring(f)):
                    t += 1
                    continue
            day = ts[t]
            eps.append({
                "seg": s, "e": t, "day": day,
                "died2y": (not s["alive"] and not s["swap_end"] and n - 1 < t + HORIZON),
                "age": t, "vol30": statistics.fmean(qv[t - 29:t + 1]),
                "rank": ranks.get((s["sym"], day), 9999),
                "hot": hot.get(day - DAY),
                "cycle": next((c for c, a, b in CYCLES if a <= day < b), "?"),
                "half": time.strftime("%Y", time.gmtime(day)) + ("H1" if time.gmtime(day).tm_mon <= 6 else "H2"),
            })
            t += COOLDOWN
    return eps


# ---------------------------------------------------------------- симуляция

def simulate(seg: dict, e: int, buy: dict, sell: dict, budget: float = 100.0,
             stop: str | None = "prod", valid: int = BUY_VALID,
             stop_buf: float = INVAL_BUF) -> dict:
    o, h, lo, c = seg["o"], seg["h"], seg["l"], seg["c"]
    n = len(c)
    end = min(e + HORIZON, n - 1)
    p0 = c[e]
    base_low = min(c[max(0, e - 30):e + 1])
    floor = base_low * (1 - INVAL_BUF)

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
        if q * px < MIN_ORDER:              # меньше минимума биржи — продаём остаток целиком
            small += 1
            q = held
            if q * px < MIN_ORDER:           # пыль: конвертация остатков с потерей ~5%
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
                        buy_fill(od, o[d], TAKER, d)
                elif d <= e + valid and lo[d] <= od["px"]:
                    buy_fill(od, min(od["px"], o[d]), MAKER, d)
            if d > e + valid and all(od.get("done") or od["mkt"] is None for od in orders) \
                    and not paired:
                buys_open = False
        # 2) продажи (только то, что было на начало дня)
        if held_start > 0:
            if paired:
                for lot in list(lots):
                    if lot["day"] >= d or h[d] < lot["tp"]:
                        continue
                    sell_qty(lot["qty"], max(lot["tp"], o[d]), MAKER)
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
                    sell_qty(q, max(avg * (1 + levels[li][0]), o[d]), MAKER)
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
            if below >= INVAL_CONFIRM:
                sell_qty(held, c[d], TAKER)
                exit_day, how = d, "стоп"
                break
        if trail and held > 0 and bought_qty > 0:
            avg = cost / bought_qty
            if hwm >= avg * (1 + trail[0]) and c[d] <= hwm * (1 - trail[1]):
                sell_qty(held, c[d], TAKER)
                exit_day, how = d, "трейл"
                break
        if bought_qty > 0 and held <= 1e-12 and not (paired and buys_open and d <= e + valid):
            exit_day, how = d, "продано"
            break
    else:
        if held > 0:
            sell_qty(held, c[end], TAKER)
        if end < e + HORIZON:
            how = ("конец данных" if seg["alive"] else
                   "переименование" if seg["swap_end"] else "делистинг")
    pnl = proceeds - spent
    return {"pnl": pnl, "rob": pnl / budget, "rod": pnl / spent if spent else 0.0,
            "deployed": spent / budget, "n_buys": n_buys, "n_sells": n_sells,
            "days": exit_day - (first_day or e), "span": exit_day - e, "budget": budget,
            "how": how, "mae": mae, "small": small}


# ---------------------------------------------------------------- отчёт

def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {}
    rob = sorted(r["rob"] for r in rows)
    rod = [r["rod"] for r in rows]
    n = len(rob)
    q = lambda f: rob[min(n - 1, int(n * f))]
    return {
        "n": n,
        "med_rob": statistics.median(rob), "mean_rob": statistics.fmean(rob),
        "win": sum(1 for x in rob if x > 0) / n,
        "p10": q(0.10), "p90": q(0.90),
        "loss30": sum(1 for x in rob if x <= -0.30) / n,
        "med_rod": statistics.median(rod), "mean_rod": statistics.fmean(rod),
        "deployed": statistics.fmean(r["deployed"] for r in rows),
        "days": statistics.fmean(r["days"] for r in rows),
        "mae_med": statistics.median(r["mae"] for r in rows),
        "stop": sum(1 for r in rows if r["how"] == "стоп") / n,
        "delist": sum(1 for r in rows if r["how"] == "делистинг") / n,
        "died2y": sum(1 for r in rows if r["ep"]["died2y"]) / n,
        # доход на $-год бюджета: бюджет занят от сигнала до выхода (включая резерв лимиток)
        "per_year": sum(r["pnl"] for r in rows)
        / sum(r["budget"] * max(r["span"], 1) / 365 for r in rows),
        "small": sum(r["small"] for r in rows),
    }


def fmt(name: str, s: dict, width: int = 44) -> str:
    if not s:
        return f"  {name:{width}} —"
    return (f"  {name:{width}} {s['n']:>5} {s['med_rob']*100:>+7.1f}% {s['mean_rob']*100:>+7.1f}% "
            f"{s['win']*100:>4.0f}% {s['p10']*100:>+6.0f}% {s['p90']*100:>+6.0f}% "
            f"{s['loss30']*100:>4.0f}% {s['deployed']*100:>4.0f}% {s['days']:>4.0f} "
            f"{s['mae_med']*100:>+5.0f}% {s['stop']*100:>4.0f}% {s['delist']*100:>4.1f}% "
            f"{s['per_year']*100:>+6.1f}%")


HEAD = (f"  {'вариант':44} {'n':>5} {'мед.':>8} {'средн.':>8} {'win':>5} {'p10':>7} "
        f"{'p90':>7} {'≤−30':>5} {'влож':>5} {'дни':>4} {'MAE':>6} {'стоп':>5} {'делист':>6} "
        f"{'в год':>7}")
NOTE = ("  (доходность на БЮДЖЕТ монеты, net-of-fees; «влож» — доля бюджета, реально "
        "потраченная;\n   MAE — медиана худшей просадки позиции на вложенное; win — доля в плюсе;"
        "\n   «в год» — сумма P&L / сумма бюджет×годы от сигнала до выхода, резерв лимиток тоже занят)")


def run_variants(eps, variants, **kw) -> dict[str, list[dict]]:
    out = {}
    for name, spec in variants.items():
        b, s, stop = spec[:3]
        buf = spec[3] if len(spec) > 3 else INVAL_BUF
        out[name] = [dict(simulate(ep["seg"], ep["e"], BUY[b], SELL[s], stop=stop,
                                   stop_buf=buf, **kw), ep=ep) for ep in eps]
    return out


def portfolio(rows: list[dict], k: int, n_iter: int = 4000, seed: int = 7) -> dict:
    """Бутстрэп: k монет с одинаковым бюджетом, входы из одного полугодия (корреляция)."""
    rnd = random.Random(seed)
    by_half: dict[str, list[float]] = {}
    for r in rows:
        by_half.setdefault(r["ep"]["half"], []).append(r["rob"])
    halves = [h for h, v in by_half.items() if len(v) >= k]
    weights = [len(by_half[h]) for h in halves]
    res = []
    for _ in range(n_iter):
        hh = rnd.choices(halves, weights=weights)[0]
        res.append(statistics.fmean(rnd.sample(by_half[hh], k)))
    res.sort()
    m = len(res)
    return {"k": k, "med": res[m // 2], "p10": res[int(m * 0.1)], "p90": res[int(m * 0.9)],
            "p_loss": sum(1 for x in res if x < 0) / m,
            "p_loss20": sum(1 for x in res if x <= -0.20) / m}


def main() -> int:
    t0 = time.time()
    coins = load_archive()
    all_segs = segments(coins)                   # ранг оборота — среди всех пар дня
    ranks = vol_ranks(all_segs)
    segs = [s for s in all_segs if len(s["c"]) >= 231]   # сигналу нужно ≥200д + 30д форварда
    alive = sum(1 for s in segs if s["alive"])
    print(f"Отрезков рядов: {len(segs)} (живых {alive}, делистнутых/старых {len(segs) - alive})")
    hot = load_hot()
    eps = build_episodes(segs, ranks, hot, "spring")
    eps = [ep for ep in eps if ep["cycle"] != "?"]
    ctrl = build_episodes(segs, ranks, hot, "every90")
    print(f"Пружин: {len(eps)} на {len({ep['seg']['sym'] for ep in eps})} монетах; "
          f"контроль (вход каждые 90д без сигнала): {len(ctrl)}  [{time.time()-t0:.0f} с]")
    results: dict = {"meta": {"episodes": len(eps), "segments": len(segs), "alive": alive}}

    # ---- 1-2. Покупка × продажа (бюджет $100 на монету)
    variants = {f"{b} | {s}": (b, s, "prod") for b in BUY for s in SELL}
    variants["разом | прод +50/+150+трейл | без стопа"] = ("разом", "прод +50/+150+трейл", None)
    variants["4 ступени до пола | прод +50/+150+трейл | без стопа"] = \
        ("4 ступени до пола", "прод +50/+150+трейл", None)
    variants["5 ступеней −10% | прод +50/+150+трейл | стоп под лестницей"] = \
        ("5 ступеней −10%", "прод +50/+150+трейл", "below")
    for buf in (0.35, 0.50):
        variants[f"4 ступени до пола | прод +50/+150+трейл | стоп −{buf*100:.0f}%"] = \
            ("4 ступени до пола", "прод +50/+150+трейл", "prod", buf)
        variants[f"разом | прод +50/+150+трейл | стоп −{buf*100:.0f}%"] = \
            ("разом", "прод +50/+150+трейл", "prod", buf)
    res = run_variants(eps, variants)
    print(f"\n=== 1-2. ВХОД × ВЫХОД: пружины, бюджет $100/монета, все монеты вкл. умершие "
          f"[{time.time()-t0:.0f} с] ===")
    print(HEAD)
    results["variants"] = {}
    for name, rows in res.items():
        s = summarize(rows)
        results["variants"][name] = s
        print(fmt(name, s))
    print(NOTE)

    # ---- survivorship: те же варианты только на живых сегодня
    print("\n=== Survivorship: только монеты, живые сегодня (как в прошлых исследованиях) ===")
    print(HEAD)
    for name in ("разом | прод +50/+150+трейл", "4 ступени до пола | прод +50/+150+трейл"):
        s = summarize([r for r in res[name] if r["ep"]["seg"]["alive"]])
        print(fmt(name + " (живые)", s))
        s2 = summarize([r for r in res[name] if not r["ep"]["seg"]["alive"]])
        print(fmt(name + " (умершие)", s2))

    # ---- по циклам
    key_vars = ["разом | прод +50/+150+трейл", "4 ступени до пола | прод +50/+150+трейл",
                "5 ступеней −10% | прод +50/+150+трейл", "8 недель по $ | прод +50/+150+трейл",
                "4 ступени до пола | ровная +30/60/100/200"]
    print("\n=== По циклам входа ===")
    print(HEAD)
    results["cycles"] = {}
    for cyc, _, _ in CYCLES:
        for name in key_vars:
            s = summarize([r for r in res[name] if r["ep"]["cycle"] == cyc])
            results["cycles"][f"{cyc} {name}"] = s
            print(fmt(f"{cyc} {name}", s, 60))

    # ---- контроль: сигнал vs вход без сигнала
    print(f"\n=== Нужен ли сигнал: вход по пружине vs каждые 90 дней без сигнала "
          f"[{time.time()-t0:.0f} с] ===")
    print(HEAD)
    cv = {"контроль: разом | прод": ("разом", "прод +50/+150+трейл", "prod"),
          "контроль: 4 до пола | прод": ("4 ступени до пола", "прод +50/+150+трейл", "prod")}
    cres = run_variants(ctrl, cv)
    results["control"] = {}
    for name, rows in cres.items():
        s = summarize(rows)
        results["control"][name] = s
        print(fmt(name, s))

    # ---- 4. качество монеты на входе: ранг оборота и возраст листинга
    base = res["4 ступени до пола | прод +50/+150+трейл"]
    print("\n=== 4. Что отсекает трупы (4 ступени до пола | прод): ранг оборота на Binance ===")
    print("  (в конце строки — доля эпизодов, где монету делистнули в течение 2 лет после сигнала)")
    print(HEAD)
    results["quality"] = {}

    def qline(lab: str, rows: list[dict]) -> None:
        s = summarize(rows)
        results["quality"][lab] = s
        print(fmt(lab, s) + (f"  умерла≤2г {s['died2y']*100:.0f}%" if s else ""))

    for lab, a, b in (("ранг 1–50", 1, 50), ("ранг 51–150", 51, 150),
                      ("ранг 151–300", 151, 300), ("ранг >300", 301, 99999)):
        qline(lab, [r for r in base if a <= r["ep"]["rank"] <= b])
    print("  — возраст листинга на Binance:")
    for lab, a, b in (("< 1 года", 0, 364), ("1–2 года", 365, 729),
                      ("2–4 года", 730, 1459), ("> 4 лет", 1460, 99999)):
        qline(lab, [r for r in base if a <= r["ep"]["age"] <= b])
    print("  — средний дневной оборот на Binance за 30д (в $ — смешан с эпохой, ранг честнее):")
    for lab, a, b in (("< $1M", 0, 1e6), ("$1–5M", 1e6, 5e6), ("$5–20M", 5e6, 2e7),
                      ("> $20M", 2e7, 1e18)):
        qline(lab, [r for r in base if a <= r["ep"]["vol30"] < b])
    if hot:
        print("  — перегрев рынка на входе (прод-флаги):")
        qline("0 флагов (холодный)", [r for r in base if r["ep"]["hot"] == 0])
        qline("≥1 флага", [r for r in base if (r["ep"]["hot"] or 0) >= 1])
    in_gate = lambda ep: ep["rank"] <= 150 and ep["age"] >= 365  # noqa: E731
    gate = [r for r in base if in_gate(r["ep"])]
    print("  — гейт «ранг ≤150 и возраст ≥1 года»:")
    qline("все пружины (для сравнения)", base)
    qline("гейт ранг≤150 и возраст≥1г", gate)
    if hot:
        qline("гейт + холодный рынок", [r for r in gate if r["ep"]["hot"] == 0])
    ns = "4 ступени до пола | прод +50/+150+трейл | без стопа"
    qline("гейт, без стопа", [r for r in res[ns] if in_gate(r["ep"])])
    qline("вне гейта, без стопа", [r for r in res[ns] if not in_gate(r["ep"])])

    # ---- 5. сколько монет
    print("\n=== 5. Портфель из k монет (равный бюджет, входы одного полугодия) ===")
    print(f"  {'вариант':44} {'k':>3} {'медиана':>8} {'p10':>7} {'p90':>7} {'P(<0)':>6} {'P(≤−20%)':>8}")
    results["portfolio"] = {}
    for name, rows in (("4 ступени до пола | прод (все)", base),
                       ("4 ступени до пола | прод (гейт)", gate)):
        for k in (3, 5, 10, 20):
            p = portfolio(rows, k)
            results["portfolio"][f"{name} k={k}"] = p
            print(f"  {name:44} {k:>3} {p['med']*100:>+7.1f}% {p['p10']*100:>+6.1f}% "
                  f"{p['p90']*100:>+6.1f}% {p['p_loss']*100:>5.0f}% {p['p_loss20']*100:>7.0f}%")

    # ---- $-ограничения: маленький бюджет на монету
    print("\n=== Малый бюджет: продажи меньше $5 сливаются с остатком ===")
    print(HEAD)
    results["budget"] = {}
    for bud in (30, 50, 100):
        rows = [dict(simulate(ep["seg"], ep["e"], BUY["4 ступени до пола"] if bud >= 40
                              else {"kind": "floor", "n": 3},
                              SELL["прод +50/+150+трейл"], budget=bud), ep=ep) for ep in eps]
        s = summarize(rows)
        results["budget"][str(bud)] = s
        print(fmt(f"${bud}: {'4' if bud >= 40 else '3'} ступени по ${bud / (4 if bud >= 40 else 3):.0f} | прод", s)
              + f"  слитых мелких продаж: {s['small']}")

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=float),
                   encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
