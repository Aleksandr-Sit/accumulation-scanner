"""Уровни для лестницы: стоп в единицах волатильности, ступени по профилю объёма,
цели продажи по структуре — что они дают против фиксированных процентов.

Вопросы:
  1. Стоп base_low − k×ATR30 и буфер k×ATR30% (ATR в долях цены) против фиксированных
     −25/−35/−50%. Лестница «4 ступени до пола», пол движется вместе со стопом:
     нижняя ступень = 1.05 × стоп. Сравнение — при сопоставимом хвосте (p10, доля ≤−30%).
  2. Три лимитки на узлах профиля объёма (365 дней, оборот свечи размазан по low–high,
     корзины ~3% цены) между ценой и полом против равномерных ступеней.
  3. Цели продажи у сопротивлений над средней (AVWAP от лоу/хая 365д, SMA200, максимумы
     180/365д, верхние узлы объёма): треть — у первого не ниже +30%, треть — у следующего
     не ниже +100%, остаток — прод-трейл; против прод-целей +50/+150 от средней.

Данные, эпизоды и модель исполнения — backtest/ladder_dca_study.py (импортом): все USDT-пары
Binance с 2017 вкл. делистнутые, сигнал «пружина» (cooldown 90д), рынок по open e+1 (0.15%),
лимитка — если low ≤ цены, по min(цена, open) (0.1%), продажа — если high ≥ цели, стоп —
2 закрытия ниже уровня, трейл 30% после +60%, горизонт 730 дней. `sim` ниже — тот же цикл,
но с явным планом ордеров, стопа и целей; на старте он сверяется с
ladder_dca_study.simulate на всех эпизодах (расхождение P&L должно быть < 1e-9).

Против переобучения:
  • placebo — тот же параметр, взятый у случайного эпизода ТОГО ЖЕ полугодия (ATR%, глубины
    ступеней, набор уровней в долях цены): отделяет «информацию об уровне монеты» от
    «другой ширины/глубины в среднем»;
  • разрезы: гейт качества (ранг оборота ≤150 и ≥1 года торгов) и вне его, циклы CYCLES,
    без окна фитиля 10.10.2025 (сигналы 12.06–09.11.2025: лимитки живы или ATR задет);
  • соседние k, ширина корзины профиля 2/3/5%; блочный бутстрэп по полугодиям.
Побочно: пол лестницы вместе со стопом против пола −25% при том же стопе; портфель из
k монет гейта (ladder_dca_study.portfolio) для ключевых вариантов.

Запуск из корня:  py -3 backtest/levels_study.py  (~5 мин, без сети)
                  [--eps-cache PATH — кэш эпизодов для отладки; по умолчанию не пишется]
Результат: backtest/levels_study_results.json, отчёт — docs/LEVELS_REPORT.md
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from binance_archive import load as load_archive  # noqa: E402
from ladder_dca_study import (BUY, BUY_VALID, CYCLES, DAY, HEAD, HORIZON, INVAL_BUF,  # noqa: E402
                              INVAL_CONFIRM, MAKER, MIN_ORDER, NOTE, SELL, TAKER,
                              build_episodes, fmt, load_hot, portfolio, segments, simulate,
                              summarize, vol_ranks)

OUT = Path(__file__).resolve().parent / "levels_study_results.json"
BUDGET = 100.0
STEPS = 4                       # «4 ступени до пола»: 1 рынком + 3 лимитки
FLOOR_MARGIN = 1.05             # нижняя ступень = 1.05 × стоп (как scanner/ladder.py)
PROD_LEVELS = [(0.5, 1 / 3), (1.5, 1 / 3)]
TRAIL = (0.60, 0.30)
ATR_N = 30
ATR_KS = (2, 3, 4, 5, 6, 8)
FIXED_BUFS = (0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.60, 0.70, 0.80)
BUF_CAP = 0.80                  # буфер стопа не шире −80% от лоу базы (k=8 у самых волатильных)
VP_DAYS = 365
VP_STEP = 0.03                  # корзина профиля ≈ 3% цены (лог-шкала)
VP_GAP = 0.03                   # лимитка не ближе 3% к цене сигнала
VP_SEP = 2                      # узлы не ближе 2 корзин друг к другу
VP_UP = 5                       # сопротивления-узлы: 5 самых объёмных выше цены +10%
T1_MIN, T2_MIN = 0.30, 1.00     # первая треть ≥ +30% от средней, вторая ≥ +100%
WICK = 1760054400               # 2025-10-10 00:00 UTC
WICK_WIN = (WICK - BUY_VALID * DAY, WICK + ATR_N * DAY)
SEEDS = (1, 2, 3)
N_BOOT = 400
EP_KEYS = ("e", "day", "died2y", "age", "vol30", "rank", "hot", "cycle", "half")


# ---------------------------------------------------------------- эпизоды и контекст

def load_episodes(cache: str | None) -> tuple[list[dict], dict]:
    """Эпизоды «пружины» как в ladder_dca_study.main (cache — только для отладки)."""
    coins = load_archive()
    all_segs = segments(coins)                   # ранг оборота — среди всех пар дня
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    meta = {"segments": len(segs), "alive": sum(1 for s in segs if s["alive"])}
    p = Path(cache) if cache else None
    if p and p.exists():
        by_sym = {s["sym"]: s for s in segs}
        raw = json.loads(p.read_text(encoding="utf-8"))
        return [dict(r, seg=by_sym[r["sym"]]) for r in raw], meta
    eps = build_episodes(segs, vol_ranks(all_segs), load_hot(), "spring")
    eps = [ep for ep in eps if ep["cycle"] != "?"]
    if p:
        p.write_text(json.dumps([dict({k: ep[k] for k in EP_KEYS}, sym=ep["seg"]["sym"])
                                 for ep in eps]), encoding="utf-8")
    return eps, meta


def atr(seg: dict, e: int, n: int = ATR_N) -> float:
    """Средний true range за n дней по день сигнала включительно (до входа по open e+1)."""
    h, lo, c = seg["h"], seg["l"], seg["c"]
    return statistics.fmean(max(h[i], c[i - 1]) - min(lo[i], c[i - 1])
                            for i in range(e - n + 1, e + 1))


def profile(seg: dict, e: int, step: float = VP_STEP,
            days: int = VP_DAYS) -> tuple[dict[int, float], float]:
    """Профиль объёма: {корзина: оборот $}. Корзина b — цены [e^(b·w), e^((b+1)·w)),
    w = ln(1+step); оборот свечи размазан равномерно по её диапазону low–high."""
    h, lo, qv = seg["h"], seg["l"], seg["qv"]
    w = math.log(1 + step)
    vp: dict[int, float] = {}
    for i in range(max(1, e - days + 1), e + 1):
        v, a, b = qv[i], lo[i], h[i]
        if v <= 0 or a <= 0 or b <= 0:
            continue
        la, lb = math.log(a), math.log(max(a, b))
        b0, b1 = math.floor(la / w), math.floor(lb / w)
        if b0 == b1:
            vp[b0] = vp.get(b0, 0.0) + v
            continue
        span = lb - la
        for k in range(b0, b1 + 1):
            ov = min(lb, (k + 1) * w) - max(la, k * w)
            if ov > 0:
                vp[k] = vp.get(k, 0.0) + v * ov / span
    return vp, w


def nodes(vp: dict[int, float], w: float, lo_px: float, hi_px: float | None, n: int,
          sep: int = VP_SEP) -> list[float]:
    """Центры n самых объёмных локальных максимумов профиля с центром в [lo_px, hi_px]
    (hi_px=None — без верхней границы), по убыванию цены."""
    if not vp or lo_px <= 0 or (hi_px is not None and hi_px <= lo_px):
        return []
    b_lo = math.ceil(math.log(lo_px) / w - 0.5)
    b_hi = max(vp) if hi_px is None else math.floor(math.log(hi_px) / w - 0.5)
    cand = []
    for b in range(b_lo, b_hi + 1):
        v = vp.get(b, 0.0)
        if v > 0 and v >= vp.get(b - 1, 0.0) and v >= vp.get(b + 1, 0.0):
            cand.append((v, b))
    cand.sort(reverse=True)
    pick: list[int] = []
    for _, b in cand:
        if all(abs(b - x) >= sep for x in pick):
            pick.append(b)
            if len(pick) == n:
                break
    return sorted((math.exp((b + 0.5) * w) for b in pick), reverse=True)


def avwap(seg: dict, a: int, e: int) -> float | None:
    """VWAP от дня a по день e: Σ оборота $ / Σ (оборот / типичная цена (h+l+c)/3)."""
    h, lo, c, qv = seg["h"], seg["l"], seg["c"], seg["qv"]
    num = den = 0.0
    for i in range(a, e + 1):
        tp = (h[i] + lo[i] + c[i]) / 3
        if qv[i] > 0 and tp > 0:
            num += qv[i]
            den += qv[i] / tp
    return num / den if den > 0 else None


def context(seg: dict, e: int) -> dict:
    """Всё, что известно на закрытии дня сигнала e: лоу базы, ATR, профиль, уровни сверху."""
    c = seg["c"]
    p0 = c[e]
    a = max(1, e - VP_DAYS + 1)
    vp, w = profile(seg, e)
    i_lo = min(range(a, e + 1), key=lambda i: c[i])          # якоря — по закрытиям,
    i_hi = max(range(a, e + 1), key=lambda i: c[i])          # чтобы фитиль не сдвигал якорь
    lv = {
        "avwap": [x for x in (avwap(seg, i_lo, e), avwap(seg, i_hi, e)) if x],
        "sma200": [statistics.fmean(c[e - 199:e + 1])],
        "max": [max(c[e - 179:e + 1]), max(c[a:e + 1])],
        "hvn": nodes(vp, w, p0 * 1.10, None, VP_UP),
    }
    lv["all"] = sorted({x for v in lv.values() for x in v})
    at = atr(seg, e)
    return {"p0": p0, "base_low": min(c[max(0, e - 30):e + 1]), "atr": at, "atrp": at / p0,
            "vp": {VP_STEP: (vp, w)}, "lv": lv}


def vp_of(seg: dict, e: int, cx: dict, step: float) -> tuple[dict[int, float], float]:
    if step not in cx["vp"]:
        cx["vp"][step] = profile(seg, e, step)
    return cx["vp"][step]


# ---------------------------------------------------------------- симуляция

def sim(seg: dict, e: int, buys: list[tuple[float | None, float]], stop_px: float | None,
        targets, trail=TRAIL, budget: float = BUDGET, valid: int = BUY_VALID) -> dict:
    """Цикл ladder_dca_study.simulate (без парного режима) с явным планом.
    buys — [(лимит-цена | None = рынком по open e+1, $)]; stop_px — уровень стопа по
    закрытиям (None — без стопа); targets(avg) -> [(цена, доля купленного)] по возрастанию."""
    o, h, lo, c = seg["o"], seg["h"], seg["l"], seg["c"]
    end = min(e + HORIZON, len(c) - 1)
    p0 = c[e]
    orders = [{"mkt": e + 1 if px is None else None, "px": px, "usd": usd, "done": False}
              for px, usd in buys]
    held = bought_qty = cost = spent = proceeds = 0.0
    li = 0
    buys_open = True
    hwm = 0.0
    below = 0
    n_buys = n_sells = 0
    first_day = None
    exit_day, how = end, "horizon"
    mae = 0.0
    small = 0
    tg: list = []
    tg_avg = None

    def buy_fill(od, px, fee, day):
        nonlocal held, bought_qty, cost, spent, n_buys, first_day
        q = od["usd"] / px * (1 - fee)
        held += q
        bought_qty += q
        cost += od["usd"]
        spent += od["usd"]
        n_buys += 1
        od["done"] = True
        if first_day is None:
            first_day = day

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
                if od["done"]:
                    continue
                if od["mkt"] is not None:
                    if od["mkt"] == d:
                        buy_fill(od, o[d], TAKER, d)
                elif d <= e + valid and lo[d] <= od["px"]:
                    buy_fill(od, min(od["px"], o[d]), MAKER, d)
            if d > e + valid and all(od["done"] or od["mkt"] is None for od in orders):
                buys_open = False
        # 2) продажи (только то, что было на начало дня)
        if held_start > 0:
            avg = cost / bought_qty if bought_qty > 0 else p0
            if avg != tg_avg:
                tg, tg_avg = targets(avg), avg
            sellable = held_start
            while li < len(tg) and sellable > 0 and h[d] >= tg[li][0]:
                q = min(tg[li][1] * bought_qty, sellable)
                before = held
                sell_qty(q, max(tg[li][0], o[d]), MAKER)
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
        if bought_qty > 0 and held <= 1e-12:
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
            "how": how, "mae": mae, "small": small, "p0": p0,
            "fills": [od["done"] for od in orders],
            "avg_px": cost / bought_qty if bought_qty > 0 else None, "lv_sold": li}


def prod_targets(avg: float) -> list[tuple[float, float]]:
    return [(avg * (1 + g), f) for g, f in PROD_LEVELS]


def fixed_targets(g1: float, g2: float):
    return lambda avg: [(avg * (1 + g1), 1 / 3), (avg * (1 + g2), 1 / 3)]


def struct_pick(levels: list[float], avg: float, t1_min: float = T1_MIN,
                t2_min: float = T2_MIN) -> tuple[float, float | None, bool, bool]:
    """T1 — первый уровень ≥ avg×(1+t1_min), T2 — следующий выше T1 и ≥ avg×(1+t2_min).
    Нет уровня — прод-цель (+50% / +150%); T2 ниже T1 не ставится (треть уходит в трейл).
    levels — по возрастанию. Возвращает (T1, T2 | None, T1 из структуры, T2 из структуры)."""
    t1 = next((r for r in levels if r >= avg * (1 + t1_min)), None)
    s1 = t1 is not None
    if t1 is None:
        t1 = avg * (1 + PROD_LEVELS[0][0])
    t2 = next((r for r in levels if r >= avg * (1 + t2_min) and r > t1 * 1.0001), None)
    s2 = t2 is not None
    if t2 is None:
        fb = avg * (1 + PROD_LEVELS[1][0])
        t2 = fb if fb > t1 * 1.0001 else None
    return t1, t2, s1, s2


def struct_targets(levels: list[float], t1_min: float = T1_MIN, t2_min: float = T2_MIN):
    lv = sorted(levels)

    def f(avg: float) -> list[tuple[float, float]]:
        t1, t2, _, _ = struct_pick(lv, avg, t1_min, t2_min)
        return [(t1, 1 / 3)] + ([(t2, 1 / 3)] if t2 else [])
    return f


def uniform_rungs(p0: float, bottom: float) -> list[float] | None:
    """3 лимитки ровно от цены до bottom (как ladder_dca_study «4 ступени до пола»).
    None — цена уже у пола (≤ 1.02 × bottom): лестница вырождается в одну покупку (как прод)."""
    if p0 <= bottom * 1.02:
        return None
    step = (p0 - bottom) / (STEPS - 1)
    return [p0 - k * step for k in range(1, STEPS)]


def vp_rungs(p0: float, bottom: float, vp: dict, w: float,
             keep_floor: bool = False) -> tuple[list[float] | None, list[float]]:
    """3 лимитки на самых объёмных узлах между bottom и ценой −3%; нехватка — добивается
    равномерными ступенями, самыми далёкими от уже выбранных. keep_floor — нижняя ступень
    всегда на полу, узлы — только для двух верхних. Возвращает (ступени, найденные узлы)."""
    uni = uniform_rungs(p0, bottom)
    if uni is None:
        return None, []
    want = STEPS - 1 - (1 if keep_floor else 0)
    found = nodes(vp, w, bottom, p0 * (1 - VP_GAP), want)
    rungs = list(found) + ([bottom] if keep_floor else [])
    pool = [u for u in uni if not (keep_floor and u == uni[-1])]
    while len(rungs) < STEPS - 1 and pool:
        best = max(pool, key=lambda u: min((abs(math.log(u / r)) for r in rungs), default=9.0))
        rungs.append(best)
        pool.remove(best)
    return sorted(rungs, reverse=True), found


def buys_of(rungs: list[float] | None) -> list[tuple[float | None, float]]:
    if rungs is None:
        return [(None, BUDGET)]
    per = BUDGET / STEPS
    return [(None, per)] + [(px, per) for px in rungs]


def run(eps: list[dict], ctx: list[dict], make) -> list[dict]:
    """make(i, ep, cx) -> (ступени | None, стоп | None, targets, extra) — по эпизоду."""
    rows = []
    for i, (ep, cx) in enumerate(zip(eps, ctx)):
        rungs, stop_px, tg, extra = make(i, ep, cx)
        r = sim(ep["seg"], ep["e"], buys_of(rungs), stop_px, tg)
        r["ep"] = ep
        r["rungs"] = rungs
        r.update(extra)
        rows.append(r)
    return rows


def v_stop(bufs: list[float | None], floor: str = "move", rungs: str = "uniform",
           tg=prod_targets, step: float = VP_STEP, keep_floor: bool = False,
           depth_from: list | None = None):
    """Вариант: буфер стопа по эпизоду (доля от лоу базы, None — без стопа).
    floor="move" — нижняя ступень = 1.05 × стоп; "fixed25" — пол не глубже −25% (как
    LADDER_REPORT для стопов −35/−50%), но поднимается к стопу, если стоп уже −25%.
    rungs: uniform | vp | depth (глубины ступеней — доли пролёта цена→пол, по эпизоду).
    tg — функция целей avg -> [(цена, доля)] или помеченная per_episode (i, cx) -> функция."""
    def make(i, ep, cx):
        b = bufs[i]
        bl, p0 = cx["base_low"], cx["p0"]
        stop_px = None if b is None else bl * (1 - b)
        if b is None:
            fb = INVAL_BUF
        else:
            fb = b if floor == "move" else min(b, INVAL_BUF)
        bottom = bl * (1 - fb) * FLOOR_MARGIN
        extra = {"buf": b, "bottom": bottom, "nodes": None}
        if rungs == "uniform":
            rr = uniform_rungs(p0, bottom)
        elif rungs == "vp":
            vp, w = vp_of(ep["seg"], ep["e"], cx, step)
            rr, found = vp_rungs(p0, bottom, vp, w, keep_floor)
            extra["nodes"], extra["node_px"] = len(found), found
        else:
            rr = uniform_rungs(p0, bottom)
            fr = depth_from[i]
            if rr is not None and fr:
                rr = sorted((p0 - f * (p0 - bottom) for f in fr), reverse=True)
        t = tg(i, cx) if getattr(tg, "per_episode", False) else tg
        return rr, stop_px, t, extra
    return make


def per_episode(fn):
    fn.per_episode = True
    return fn


# ---------------------------------------------------------------- статистика

def in_gate(ep: dict) -> bool:
    return ep["rank"] <= 150 and ep["age"] >= 365


def in_wick(ep: dict) -> bool:
    return WICK_WIN[0] <= ep["day"] <= WICK_WIN[1]


SLICES = [("все", lambda ep: True), ("гейт", in_gate), ("вне гейта", lambda ep: not in_gate(ep))] + \
    [(c, (lambda ep, c=c: ep["cycle"] == c)) for c, _, _ in CYCLES] + \
    [("без окна 10.10", lambda ep: not in_wick(ep)),
     ("гейт без 10.10", lambda ep: in_gate(ep) and not in_wick(ep))]


def sub(rows: list[dict], pred) -> list[dict]:
    return [r for r in rows if pred(r["ep"])]


def mean_stats(stats: list[dict]) -> dict:
    keys = [k for k, v in stats[0].items() if isinstance(v, (int, float))]
    out = {k: statistics.fmean(s[k] for s in stats) for k in keys}
    for k in ("n", "small"):
        if k in out:
            out[k] = round(out[k])
    return out


def buf_stats(rows: list[dict]) -> dict:
    b = sorted(r["buf"] for r in rows if r["buf"] is not None)
    if not b:
        return {}
    n = len(b)
    return {"buf_med": statistics.median(b), "buf_p10": b[int(n * 0.1)],
            "buf_p90": b[min(n - 1, int(n * 0.9))],
            "capped": sum(1 for x in b if x >= BUF_CAP - 1e-12) / n,
            "single": sum(1 for r in rows if r["rungs"] is None) / len(rows)}


def shuffle_within(eps: list[dict], seed: int) -> list[int]:
    """Перестановка индексов внутри полугодия сигнала (placebo сохраняет эпоху)."""
    rnd = random.Random(seed)
    groups: dict[str, list[int]] = {}
    for i, ep in enumerate(eps):
        groups.setdefault(ep["half"], []).append(i)
    perm = list(range(len(eps)))
    for idx in groups.values():
        sh = idx[:]
        rnd.shuffle(sh)
        for a, b in zip(idx, sh):
            perm[a] = b
    return perm


def idx_sets(eps: list[dict], pred, n_iter: int = N_BOOT, seed: int = 11) -> list[list[int]]:
    """Блочный бутстрэп: полугодия сигнала с возвращением (эпизоды внутри коррелированы)."""
    groups: dict[str, list[int]] = {}
    for i, ep in enumerate(eps):
        if pred(ep):
            groups.setdefault(ep["half"], []).append(i)
    g = list(groups.values())
    rnd = random.Random(seed)
    out = []
    for _ in range(n_iter):
        idx: list[int] = []
        for _ in range(len(g)):
            idx.extend(rnd.choice(g))
        out.append(idx)
    return out


def core(rows: list[dict], idx: list[int]) -> tuple[float, float, float, float]:
    """(медиана, p10, доля ≤−30%, «в год») на подвыборке — как в summarize."""
    rob = sorted(rows[i]["rob"] for i in idx)
    n = len(rob)
    py = sum(rows[i]["pnl"] for i in idx) / sum(rows[i]["budget"] * max(rows[i]["span"], 1) / 365
                                                for i in idx)
    return (statistics.median(rob), rob[min(n - 1, int(n * 0.10))],
            sum(1 for x in rob if x <= -0.30) / n, py)


def envelope(fixed: dict[str, tuple], x: tuple) -> dict:
    """Критерий задачи «при сопоставимом хвосте»: среди фиксированных стопов, у которых
    хвост не хуже, чем у x (p10 не ниже И доля ≤−30% не выше), берётся лучший по медиане
    и лучший по «в год»; d_med / d_py — насколько x лучше них (> 0 — x выигрывает).
    dominated_by — фиксированные, не худшие x по всем четырём метрикам.
    fixed: {имя: core()}, x: core(). Огибающая, а не интерполяция: p10 у стопов с полом,
    идущим за стопом, немонотонен (−15…−50% хуже, −60…−80% снова лучше)."""
    safe = {f: v for f, v in fixed.items() if v[1] >= x[1] - 1e-12 and v[2] <= x[2] + 1e-12}
    out: dict = {"n_safe": len(safe),
                 "dominated_by": [f for f, v in safe.items() if v[0] >= x[0] and v[3] >= x[3]]}
    if safe:
        fm = max(safe, key=lambda f: safe[f][0])
        fp = max(safe, key=lambda f: safe[f][3])
        out.update(best_med=fm, d_med=x[0] - safe[fm][0], best_py=fp, d_py=x[3] - safe[fp][3])
    return out


def quantiles(xs: list[float]) -> dict:
    xs = sorted(xs)
    m = len(xs)
    return {"lo": xs[int(m * 0.05)], "hi": xs[min(m - 1, int(m * 0.95))],
            "p_pos": sum(1 for v in xs if v > 0) / m}


def boot_vs(sets: list[list[int]], full: list[int], x: list[dict],
            ys: list[list[dict]]) -> dict:
    """x − среднее(ys) по (медиана, p10, ≤−30%, «в год»): оценка на full и 90%-интервал
    блочного бутстрэпа (индексы — глобальные, одни и те же эпизоды во всех вариантах)."""
    def diff(idx):
        a = core(x, idx)
        cs = [core(y, idx) for y in ys]
        return [a[j] - statistics.fmean(c[j] for c in cs) for j in range(4)]
    est = diff(full)
    d = [diff(idx) for idx in sets]
    names = ("d_med", "d_p10", "d_loss30", "d_py")
    return {nm: dict(est=est[j], **quantiles([v[j] for v in d])) for j, nm in enumerate(names)}


def boot_envelope(sets: list[list[int]], fx_sets: list[dict[str, tuple]],
                  x: list[dict]) -> dict | None:
    """Бутстрэп envelope(): в каждой реплике свой набор «безопасных» фиксированных."""
    dm, dp, win = [], [], 0
    for idx, fx in zip(sets, fx_sets):
        g = envelope(fx, core(x, idx))
        if "d_med" in g:
            dm.append(g["d_med"])
            dp.append(g["d_py"])
            win += g["d_med"] > 0 and g["d_py"] > 0
    if len(dm) < len(sets) // 2:
        return None
    return {"d_med": quantiles(dm), "d_py": quantiles(dp), "p_win_both": win / len(sets),
            "n_ok": len(dm)}


BINS = [(-9.0, -0.3), (-0.3, -0.2), (-0.2, -0.1), (-0.1, 0.0), (0.0, 0.1), (0.1, 0.3), (0.3, 99.0)]


def shape(rows: list[dict]) -> dict:
    """Форма распределения исхода на бюджет: доли по корзинам, медианы стопов и остальных."""
    n = len(rows)
    st = [r["rob"] for r in rows if r["how"] == "стоп"]
    rest = [r["rob"] for r in rows if r["how"] != "стоп"]
    return {"bins": [sum(1 for r in rows if a < r["rob"] <= b) / n for a, b in BINS],
            "stop_med": statistics.median(st) if st else None,
            "rest_med": statistics.median(rest) if rest else None}


def fmt_d(b: dict, key: str, pct: bool = True) -> str:
    v = b[key]
    m = 100 if pct else 1
    return f"{v['est']*m:+.1f} [{v['lo']*m:+.1f}…{v['hi']*m:+.1f}] P>0 {v['p_pos']*100:.0f}%"


def head(width: int = 44) -> str:
    return HEAD.replace(f"{'вариант':44}", f"{'вариант':{width}}")


# ---------------------------------------------------------------- вопрос 1: стоп

def fmt_line(nm: str, s: dict, width: int = 26) -> str:
    """Короткая строка: медиана / p10 / ≤−30% / «в год» / стоп."""
    return (f"  {nm[:width]:{width}} {s['n']:>5} {s['med_rob']*100:>+7.1f}% {s['p10']*100:>+5.0f}% "
            f"{s['loss30']*100:>4.0f}% {s['per_year']*100:>+6.1f}% {s['stop']*100:>4.0f}% "
            f"{s['deployed']*100:>4.0f}%")


SHORT_HEAD = (f"  {'вариант':26} {'n':>5} {'мед.':>8} {'p10':>6} {'≤−30':>5} {'в год':>7} "
              f"{'стоп':>5} {'влож':>5}")


def q1(eps: list[dict], ctx: list[dict], res: dict, t0: float) -> dict:
    n = len(eps)
    runs: dict[str, list[dict]] = {}
    fam_fixed: dict[str, list[str]] = {"move": [], "fixed25": []}
    fam_atr: dict[str, list[str]] = {"move": [], "fixed25": []}
    for b in FIXED_BUFS:                                 # пол движется со стопом
        nm = f"фикс −{b*100:.0f}%"
        runs[nm] = run(eps, ctx, v_stop([b] * n))
        fam_fixed["move"].append(nm)
        if b <= INVAL_BUF:                               # при стопе ≤ −25% семейства совпадают
            fam_fixed["fixed25"].append(nm)
    for b in FIXED_BUFS:                                 # пол не глубже −25% (как LADDER)
        if b > INVAL_BUF:
            nm = f"фикс −{b*100:.0f}%, пол −25%"
            runs[nm] = run(eps, ctx, v_stop([b] * n, floor="fixed25"))
            fam_fixed["fixed25"].append(nm)
    runs["без стопа, пол −25%"] = run(eps, ctx, v_stop([None] * n, floor="fixed25"))
    for k in ATR_KS:
        nm = f"ATR k={k}"
        runs[nm] = run(eps, ctx, v_stop([min(k * cx["atr"] / cx["base_low"], BUF_CAP) for cx in ctx]))
        fam_atr["move"].append(nm)
    for k in ATR_KS:
        nm = f"ATR% k={k}"
        runs[nm] = run(eps, ctx, v_stop([min(k * cx["atrp"], BUF_CAP) for cx in ctx]))
        fam_atr["move"].append(nm)
    for k in ATR_KS:
        nm = f"ATR% k={k}, пол −25%"
        runs[nm] = run(eps, ctx, v_stop([min(k * cx["atrp"], BUF_CAP) for cx in ctx],
                                        floor="fixed25"))
        fam_atr["fixed25"].append(nm)
    atr_all = fam_atr["move"] + fam_atr["fixed25"]
    fam_of = {nm: f for f, v in fam_atr.items() for nm in v}
    perms = [shuffle_within(eps, s) for s in SEEDS]
    placebo: dict[str, list[list[dict]]] = {}
    for k in ATR_KS:
        placebo[f"placebo ATR% k={k}"] = [
            run(eps, ctx, v_stop([min(k * ctx[pm[i]]["atrp"], BUF_CAP) for i in range(n)]))
            for pm in perms]
    full = list(range(n))
    out: dict = {"variants": {}, "placebo": {}, "shape": {}, "envelope": {}, "vs_placebo": {},
                 "slices": {}, "slice_envelope": {}, "slice_vs_placebo": {}, "boot_envelope": {},
                 "floor_follows_stop": {}}

    print(f"\n=== 1. СТОП: фиксированный % против k×ATR30 — «4 ступени до пола» + прод-продажа "
          f"[{time.time()-t0:.0f} с] ===")
    print("  пол движется со стопом (нижняя ступень = 1.05 × стоп), кроме строк «пол −25%» (пол не")
    print("  глубже −25%, как в LADDER_REPORT); ATR — стоп = лоу базы − k×ATR30; ATR% — буфер =")
    print("  k × ATR30/цена от лоу базы; буфер не шире 80% (cap — доля эпизодов, упёршихся в потолок)")
    print(head() + f" {'буфер мед (p10–p90)':>20} {'cap':>4}")
    for nm, rows in runs.items():
        s = summarize(rows)
        s.update(buf_stats(rows))
        out["variants"][nm] = s
        bs = (f" {s['buf_med']*100:>6.0f}% ({s['buf_p10']*100:.0f}–{s['buf_p90']*100:.0f}%)"
              f" {s['capped']*100:>4.0f}%") if "buf_med" in s else ""
        print(fmt(nm, s) + bs)
    print(NOTE)
    print("\n  placebo: ATR% взят у случайного эпизода того же полугодия (среднее по 3 перестановкам)")
    print(head() + f" {'мед. min…max':>14}")
    for nm, reps in placebo.items():
        ss = [summarize(r) for r in reps]
        s = mean_stats(ss)
        s["med_range"] = (min(x["med_rob"] for x in ss), max(x["med_rob"] for x in ss))
        out["placebo"][nm] = s
        print(fmt(nm, s) + f"  {s['med_range'][0]*100:+.1f}…{s['med_range'][1]*100:+.1f}%")

    # --- форма распределения: почему медиана скачет
    print("\n  Форма распределения исхода на бюджет (доля эпизодов, %), медианы стопов и остальных:")
    print(f"  {'вариант':26} {'≤−30':>5} {'−30…−20':>8} {'−20…−10':>8} {'−10…0':>6} {'0…+10':>6} "
          f"{'+10…+30':>8} {'>+30':>5} {'стоп':>5} {'мед.стопов':>11} {'мед.прочих':>11}")
    for nm in ("фикс −25%", "фикс −35%", "фикс −50%", "фикс −70%", "фикс −50%, пол −25%",
               "без стопа, пол −25%", "ATR% k=5", "ATR% k=8"):
        sh = shape(runs[nm])
        out["shape"][nm] = sh
        sm = f"{sh['stop_med']*100:>+10.1f}%" if sh["stop_med"] is not None else f"{'—':>11}"
        cells = " ".join(f"{x*100:>{w}.0f}" for x, w in zip(sh["bins"], (5, 8, 8, 6, 6, 8, 5)))
        print(f"  {nm[:26]:26} {cells} {out['variants'][nm]['stop']*100:>4.0f}% {sm} "
              f"{sh['rest_med']*100:>+10.1f}%")

    # --- критерий задачи: лучший фиксированный с тем же или лучшим хвостом
    print("\n  Сравнение при сопоставимом хвосте: среди фиксированных стопов ТОЙ ЖЕ конструкции лестницы,")
    print("  у которых p10 не ниже и доля ≤−30% не выше, чем у варианта, — лучший по медиане и лучший")
    print("  по «в год»; Δ = вариант − он, п.п. (> 0 — вариант лучше). «доминируют» — фиксированные,")
    print("  не худшие варианта по всем четырём метрикам.")
    print(f"  {'вариант':20} {'p10':>6} {'≤−30':>5} {'лучший по медиане':>24} {'Δмед':>6} "
          f"{'лучший по «в год»':>24} {'Δгод':>6}  доминируют")
    for nm in atr_all + list(placebo):
        fam = fam_fixed[fam_of.get(nm, "move")]
        fx = {f: core(runs[f], full) for f in fam}
        if nm in placebo:
            gs = [envelope(fx, core(r, full)) for r in placebo[nm]]
            g = dict(gs[0])
            if all("d_med" in x for x in gs):
                g["d_med"] = statistics.fmean(x["d_med"] for x in gs)
                g["d_py"] = statistics.fmean(x["d_py"] for x in gs)
            xs = out["placebo"][nm]
        else:
            g = envelope(fx, core(runs[nm], full))
            xs = out["variants"][nm]
        out["envelope"][nm] = g
        if "d_med" in g:
            bm = f"{g['best_med']} ({out['variants'][g['best_med']]['med_rob']*100:+.1f}%)"
            bp = f"{g['best_py']} ({out['variants'][g['best_py']]['per_year']*100:+.1f}%)"
            tail = (f"{bm[:24]:>24} {g['d_med']*100:>+6.1f} {bp[:24]:>24} {g['d_py']*100:>+6.1f}")
        else:
            tail = f"{'хвост лучше всех фиксированных':>62}"
        print(f"  {nm[:20]:20} {xs['p10']*100:>+5.1f}% {xs['loss30']*100:>4.1f}% {tail}  "
              f"{', '.join(g['dominated_by']) or '—'}")

    # --- против фиксированного стопа той же медианной ширины (ось ширины монотонна, p10 — нет)
    print("\n  Против фиксированного стопа той же медианной ширины буфера (линейная интерполяция")
    print("  фиксированных той же конструкции по ширине): Δ = вариант − фикс, п.п.")
    print("  Строгий критерий выше в семействе «пол −25%» шумит: у фикс −35/−40% с полом −25% кластер")
    print("  стопов сидит ровно у −30% бюджета (доля ≤−30% 27–29%), и они выпадают из сравнения.")
    print(f"  {'вариант':22} {'ширина':>7} {'Δмед':>6} {'Δp10':>6} {'Δ≤−30':>6} {'Δгод':>6}")
    out["same_width"] = {}
    for nm in atr_all + list(placebo):
        f = fam_of.get(nm, "move")
        pts = []
        for b in FIXED_BUFS:
            fn = f"фикс −{b*100:.0f}%" + ("" if (f == "move" or b <= INVAL_BUF) else ", пол −25%")
            pts.append((b, core(runs[fn], full)))
        reps = placebo[nm] if nm in placebo else [runs[nm]]
        bm = buf_stats(reps[0])["buf_med"]
        cs = [core(r, full) for r in reps]
        x = [statistics.fmean(c[j] for c in cs) for j in range(4)]
        itp = None
        for (b0, c0), (b1, c1) in zip(pts, pts[1:]):
            if b0 <= bm <= b1:
                t = (bm - b0) / (b1 - b0)
                itp = [c0[j] + t * (c1[j] - c0[j]) for j in range(4)]
                break
        if itp is None:
            out["same_width"][nm] = None
            print(f"  {nm[:22]:22} {bm*100:>6.0f}%  вне сетки фиксированных")
            continue
        d = {"width": bm, "d_med": x[0] - itp[0], "d_p10": x[1] - itp[1],
             "d_loss30": x[2] - itp[2], "d_py": x[3] - itp[3]}
        out["same_width"][nm] = d
        print(f"  {nm[:22]:22} {bm*100:>6.0f}% {d['d_med']*100:>+6.1f} {d['d_p10']*100:>+6.1f} "
              f"{d['d_loss30']*100:>+6.1f} {d['d_py']*100:>+6.1f}")

    # --- ATR против placebo: есть ли информация в волатильности самой монеты
    print(f"\n  ATR% против placebo того же k (Δ = ATR − placebo, п.п.; блочный бутстрэп по полугодиям, "
          f"{N_BOOT}×, 90%) [{time.time()-t0:.0f} с]")
    for sl, pred in (("все", SLICES[0][1]), ("гейт", in_gate)):
        sets = idx_sets(eps, pred)
        idx = [i for i, ep in enumerate(eps) if pred(ep)]
        out["vs_placebo"][sl] = {}
        for k in ATR_KS:
            b = boot_vs(sets, idx, runs[f"ATR% k={k}"], placebo[f"placebo ATR% k={k}"])
            out["vs_placebo"][sl][f"ATR% k={k}"] = b
            print(f"  [{sl}] k={k}: мед {fmt_d(b, 'd_med')} · p10 {fmt_d(b, 'd_p10')} · "
                  f"≤−30 {fmt_d(b, 'd_loss30')} · «в год» {fmt_d(b, 'd_py')}")

    # --- разрезы: устойчивость по гейту, циклам и окну фитиля
    show = ["фикс −25%", "фикс −35%", "фикс −50%", "фикс −70%", "фикс −50%, пол −25%",
            "ATR% k=4", "ATR% k=5", "ATR% k=6", "ATR% k=8", "ATR k=5", "ATR k=8"]
    print("\n  Разрезы (медиана / p10 / ≤−30% / «в год» / стоп / вложено):")
    for sl, pred in SLICES[1:]:
        print(f"  — {sl}")
        print(SHORT_HEAD)
        out["slices"][sl] = {}
        for nm in show:
            s = summarize(sub(runs[nm], pred))
            out["slices"][sl][nm] = s
            print(fmt_line(nm, s))
        for k in (5, 8):
            nm = f"placebo ATR% k={k}"
            s = mean_stats([summarize(sub(r, pred)) for r in placebo[nm]])
            out["slices"][sl][nm] = s
            print(fmt_line(nm, s))
    print("\n  Разрезы: Δ к лучшему фиксированному с тем же или лучшим хвостом, Δмед/Δ«в год», п.п.")
    print("  («хвост+» — у варианта хвост лучше всех фиксированных этого разреза)")
    print("  " + f"{'вариант':20}" + "".join(f"{sl:>16}" for sl, _ in SLICES))
    slice_idx = {sl: [i for i, ep in enumerate(eps) if pred(ep)] for sl, pred in SLICES}
    for nm in atr_all:
        line = f"  {nm[:20]:20}"
        out["slice_envelope"][nm] = {}
        for sl, _ in SLICES:
            idx = slice_idx[sl]
            fx = {f: core(runs[f], idx) for f in fam_fixed[fam_of[nm]]}
            g = envelope(fx, core(runs[nm], idx))
            out["slice_envelope"][nm][sl] = g
            line += (f"{g['d_med']*100:>+8.1f}/{g['d_py']*100:>+6.1f} " if "d_med" in g
                     else f"{'хвост+':>15} ")
        print(line)
    print("\n  Разрезы: ATR% − placebo того же k, Δмед/Δ«в год», п.п.")
    print("  " + f"{'вариант':20}" + "".join(f"{sl:>16}" for sl, _ in SLICES))
    for k in ATR_KS:
        nm = f"ATR% k={k}"
        line = f"  {nm:20}"
        out["slice_vs_placebo"][nm] = {}
        for sl, _ in SLICES:
            idx = slice_idx[sl]
            a = core(runs[nm], idx)
            cs = [core(r, idx) for r in placebo[f"placebo ATR% k={k}"]]
            d = [a[j] - statistics.fmean(c[j] for c in cs) for j in range(4)]
            out["slice_vs_placebo"][nm][sl] = {"d_med": d[0], "d_p10": d[1], "d_loss30": d[2],
                                               "d_py": d[3]}
            line += f"{d[0]*100:>+8.1f}/{d[3]*100:>+6.1f} "
        print(line)

    # --- бутстрэп критерия задачи
    print(f"\n  Бутстрэп критерия (Δ к лучшему фиксированному с тем же или лучшим хвостом), {N_BOOT}×, "
          f"90% [{time.time()-t0:.0f} с];")
    print("  P(оба>0) — доля реплик, где вариант лучше и по медиане, и по «в год»")
    for sl, pred in (("все", SLICES[0][1]), ("гейт", in_gate)):
        sets = idx_sets(eps, pred)
        fx_sets = {f: [{x: core(runs[x], idx) for x in v} for idx in sets]
                   for f, v in fam_fixed.items()}
        out["boot_envelope"][sl] = {}
        for nm in atr_all:
            b = boot_envelope(sets, fx_sets[fam_of[nm]], runs[nm])
            out["boot_envelope"][sl][nm] = b
            if not b:
                print(f"  [{sl}] {nm:20} — хвост чаще лучше всех фиксированных")
                continue
            q = b["d_med"]
            p = b["d_py"]
            print(f"  [{sl}] {nm:20} Δмед [{q['lo']*100:+.1f}…{q['hi']*100:+.1f}] "
                  f"P>0 {q['p_pos']*100:.0f}% · "
                  f"Δ«в год» [{p['lo']*100:+.1f}…{p['hi']*100:+.1f}] P>0 {p['p_pos']*100:.0f}% · "
                  f"P(оба>0) {b['p_win_both']*100:.0f}%")

    # --- побочное: пол вместе со стопом против пола −25% при том же стопе
    print("\n  Побочное: пол вместе со стопом против пола −25% при том же стопе "
          "(Δ = пол со стопом − пол −25%, п.п., 90%)")
    for sl, pred in (("все", SLICES[0][1]), ("гейт", in_gate)):
        sets = idx_sets(eps, pred)
        idx = [i for i, ep in enumerate(eps) if pred(ep)]
        out["floor_follows_stop"][sl] = {}
        for b in (0.35, 0.50, 0.60, 0.70):
            a = f"фикс −{b*100:.0f}%"
            bb = boot_vs(sets, idx, runs[a], [runs[f"{a}, пол −25%"]])
            out["floor_follows_stop"][sl][a] = bb
            print(f"  [{sl}] стоп −{b*100:.0f}%: мед {fmt_d(bb, 'd_med')} · p10 {fmt_d(bb, 'd_p10')} · "
                  f"≤−30 {fmt_d(bb, 'd_loss30')} · «в год» {fmt_d(bb, 'd_py')}")
    res["q1"] = out
    return runs


# ---------------------------------------------------------------- вопрос 2: ступени

def rung_stats(rows: list[dict], base: list[dict] | None = None) -> dict:
    """Исполнение ступеней и средняя цена входа (к цене сигнала, с комиссиями)."""
    multi = [r for r in rows if r["rungs"] is not None]
    m = len(multi)
    out = {"single": 1 - m / len(rows),
           "fill": [sum(1 for r in multi if r["fills"][k]) / m for k in range(1, STEPS)],
           "depth": [statistics.median(r["rungs"][k - 1] / r["p0"] - 1 for r in multi)
                     for k in range(1, STEPS)],
           "n_lim": statistics.fmean(sum(r["fills"][1:]) for r in rows),
           "avg_rel": statistics.median(r["avg_px"] / r["p0"] - 1 for r in rows if r["avg_px"]),
           "lim_disc": statistics.fmean(r["rungs"][k - 1] / r["p0"] - 1 for r in multi
                                        for k in range(1, STEPS) if r["fills"][k])}
    nodes_found = [r["nodes"] for r in rows if r.get("nodes") is not None]
    if nodes_found:
        out["nodes"] = {str(k): sum(1 for x in nodes_found if x == k) / len(nodes_found)
                        for k in range(STEPS)}
    if base is not None:
        pr = [r["avg_px"] / b["avg_px"] - 1 for r, b in zip(rows, base) if r["avg_px"] and b["avg_px"]]
        same = [r["avg_px"] / b["avg_px"] - 1 for r, b in zip(rows, base)
                if r["avg_px"] and b["avg_px"] and sum(r["fills"]) == sum(b["fills"])]
        out["avg_vs_base"] = statistics.median(pr)
        out["avg_vs_base_same_fills"] = statistics.median(same) if same else None
        out["share_same_fills"] = len(same) / len(pr)
        out["better_avg"] = sum(1 for x in pr if x < -1e-12) / len(pr)
    return out


def touch_stats(rows: list[dict], only: set[int] | None = None) -> dict:
    """Держит ли уровень цену: для каждой лимитки — первое касание (low ≤ цены) в окне
    BUY_VALID; отскок = макс. закрытие за 30 дней после касания / цена исполнения − 1,
    «выше через 30д» — закрытие через 30 дней выше цены исполнения. only — номера эпизодов."""
    reb, up = [], []
    for i, r in enumerate(rows):
        if r["rungs"] is None or (only is not None and i not in only):
            continue
        seg, e = r["ep"]["seg"], r["ep"]["e"]
        o, lo, c = seg["o"], seg["l"], seg["c"]
        last = min(e + BUY_VALID, len(c) - 31)
        for px in r["rungs"]:
            for d in range(e + 1, last + 1):
                if lo[d] <= px:
                    fill = min(px, o[d])
                    reb.append(max(c[d:d + 31]) / fill - 1)
                    up.append(c[d + 30] > fill)
                    break
    return {"touches": len(reb), "rebound30_med": statistics.median(reb) if reb else None,
            "up30": statistics.fmean(up) if up else None}


def fmt_rung(nm: str, rs: dict, width: int = 34) -> str:
    f = "/".join(f"{x*100:.0f}" for x in rs["fill"])
    dp = "/".join(f"{x*100:+.0f}" for x in rs["depth"])
    extra = ""
    if "avg_vs_base" in rs:
        sf = rs["avg_vs_base_same_fills"]
        extra = (f" {rs['avg_vs_base']*100:>+7.1f}% "
                 f"{(sf*100 if sf is not None else 0):>+7.1f}% {rs['better_avg']*100:>5.0f}%")
    nd = ""
    if "nodes" in rs:
        nd = "  узлов 0/1/2/3: " + "/".join(f"{rs['nodes'][str(k)]*100:.0f}" for k in range(STEPS))
    return (f"  {nm:{width}} {f:>12} {dp:>14} {rs['n_lim']:>5.2f} {rs['avg_rel']*100:>+7.1f}% "
            f"{rs['lim_disc']*100:>+7.1f}%{extra}{nd}")


def q2(eps: list[dict], ctx: list[dict], res: dict, t0: float, wide: float) -> dict:
    n = len(eps)
    prod = [INVAL_BUF] * n
    runs = {"равномерно (прод)": run(eps, ctx, v_stop(prod)),
            "узлы объёма ×3": run(eps, ctx, v_stop(prod, rungs="vp")),
            "узлы ×2 + пол": run(eps, ctx, v_stop(prod, rungs="vp", keep_floor=True)),
            "узлы ×3, корзина 2%": run(eps, ctx, v_stop(prod, rungs="vp", step=0.02)),
            "узлы ×3, корзина 5%": run(eps, ctx, v_stop(prod, rungs="vp", step=0.05))}
    vp_rows = runs["узлы объёма ×3"]
    depth = [None if r["rungs"] is None else
             [(r["p0"] - px) / (r["p0"] - r["bottom"]) for px in r["rungs"]] for r in vp_rows]
    placebo = [run(eps, ctx, v_stop(prod, rungs="depth", depth_from=[depth[pm[i]] for i in range(n)]))
               for pm in (shuffle_within(eps, s) for s in SEEDS)]
    wb = [wide] * n
    runs[f"равномерно, стоп −{wide*100:.0f}%"] = run(eps, ctx, v_stop(wb))
    runs[f"узлы ×3, стоп −{wide*100:.0f}%"] = run(eps, ctx, v_stop(wb, rungs="vp"))
    base = runs["равномерно (прод)"]
    out = {"variants": {}, "rungs": {}, "placebo": {}, "slices": {}, "boot": {}}
    print(f"\n=== 2. СТУПЕНИ ПО ПРОФИЛЮ ОБЪЁМА (365д) — стоп прод −25%, пол 1.05×стоп, прод-продажа "
          f"[{time.time()-t0:.0f} с] ===")
    print(head(34))
    for nm, rows in runs.items():
        s = summarize(rows)
        out["variants"][nm] = s
        print(fmt(nm, s, 34))
    ps = [summarize(r) for r in placebo]
    s = mean_stats(ps)
    out["placebo"]["placebo узлов (глубины чужого эпизода)"] = s
    print(fmt("placebo узлов", s, 34) +
          f"  мед. {min(x['med_rob'] for x in ps)*100:+.1f}…{max(x['med_rob'] for x in ps)*100:+.1f}%")
    print(NOTE)
    print("\n  Исполнение ступеней (лимитки сверху вниз) и цена входа:")
    print(f"  {'вариант':34} {'исп. %':>12} {'глубина %':>14} {'лимит':>5} {'сред/цена':>8} "
          f"{'скидка':>8} {'сред/равн':>8} {'то же N':>8} {'дешевле':>6}")
    print("  (глубина — медиана цены ступени к цене сигнала; «лимит» — исполнено лимиток в среднем;")
    print("   сред/цена — медиана средней цены входа к цене сигнала; скидка — средняя скидка исполненных")
    print("   лимиток; сред/равн — медиана отношения средней к равномерной лестнице на том же эпизоде,")
    print("   «то же N» — то же, где исполнилось столько же ступеней; дешевле — доля эпизодов")
    print("   со средней ниже)")
    for nm, rows in runs.items():
        cmp_base = base if "стоп −" not in nm else runs[f"равномерно, стоп −{wide*100:.0f}%"]
        rs = rung_stats(rows, None if rows is cmp_base else cmp_base)
        out["rungs"][nm] = rs
        print(fmt_rung(nm, rs))
    rs = rung_stats(placebo[0], base)
    out["rungs"]["placebo узлов (seed 1)"] = rs
    print(fmt_rung("placebo узлов (seed 1)", rs))

    # узел объёма как поддержка: только эпизоды, где найден хотя бы один узел
    has = {i for i, r in enumerate(vp_rows) if r["nodes"]}
    pos_bl = sorted(px / ctx[i]["base_low"] - 1 for i in has for px in vp_rows[i]["node_px"])
    pos_p0 = sorted(px / ctx[i]["p0"] - 1 for i in has for px in vp_rows[i]["node_px"])
    qs = (0.10, 0.25, 0.50, 0.75, 0.90)
    out["node_position"] = {"vs_base_low": {str(q): pos_bl[int(len(pos_bl) * q)] for q in qs},
                            "vs_price": {str(q): pos_p0[int(len(pos_p0) * q)] for q in qs}}
    print(f"\n  Где стоят найденные узлы ({len(pos_bl)} шт.), квантили 10/25/50/75/90%: к лоу базы "
          + " / ".join(f"{pos_bl[int(len(pos_bl) * q)]*100:+.0f}%" for q in qs)
          + "; к цене сигнала " + " / ".join(f"{pos_p0[int(len(pos_p0) * q)]*100:+.0f}%" for q in qs))
    print(f"\n  Держит ли уровень цену (эпизоды, где найден ≥1 узел: {len(has)} из {n}):")
    print(f"  {'ступени':34} {'касаний':>8} {'отскок 30д (мед.)':>18} {'выше через 30д':>15}")
    out["touch"] = {"episodes": len(has)}
    for nm, rows in (("равномерно (прод)", base), ("узлы объёма ×3", vp_rows),
                     ("placebo узлов (seed 1)", placebo[0])):
        t = touch_stats(rows, has)
        out["touch"][nm] = t
        print(f"  {nm:34} {t['touches']:>8} {t['rebound30_med']*100:>+17.1f}% {t['up30']*100:>14.0f}%")
    s_u = summarize([base[i] for i in sorted(has)])
    s_v = summarize([vp_rows[i] for i in sorted(has)])
    out["touch"]["outcome_uniform"], out["touch"]["outcome_vp"] = s_u, s_v
    print(head(34))
    print(fmt("равномерно — те же эпизоды", s_u, 34))
    print(fmt("узлы ×3 — те же эпизоды", s_v, 34))

    show = ["равномерно (прод)", "узлы объёма ×3", "узлы ×2 + пол"]
    for sl, pred in SLICES[1:]:
        out["slices"][sl] = {}
        for nm in show:
            out["slices"][sl][nm] = summarize(sub(runs[nm], pred))
        out["slices"][sl]["placebo узлов"] = mean_stats([summarize(sub(r, pred)) for r in placebo])
    print("\n  Разрезы: медиана / p10 / ≤−30% / «в год»")
    print("  " + f"{'вариант':20}" + "".join(f"{sl:>27}" for sl, _ in SLICES[1:]))
    for nm in show + ["placebo узлов"]:
        line = f"  {nm[:20]:20}"
        for sl, _ in SLICES[1:]:
            s = out["slices"][sl][nm]
            line += (f"{s['med_rob']*100:>+7.1f}/{s['p10']*100:>+4.0f}/{s['loss30']*100:>3.0f}/"
                     f"{s['per_year']*100:>+6.1f}  ")
        print(line)
    print(f"\n  Бутстрэп по полугодиям ({N_BOOT}×), п.п. [90%]: разница с равномерной лестницей "
          f"и с placebo")
    for sl, pred in (("все", SLICES[0][1]), ("гейт", in_gate)):
        sets = idx_sets(eps, pred)
        idx = [i for i, ep in enumerate(eps) if pred(ep)]
        out["boot"][sl] = {}
        for nm, ys, lab in (("узлы объёма ×3", [base], "− равномерно"),
                            ("узлы ×2 + пол", [base], "− равномерно"),
                            ("узлы объёма ×3", placebo, "− placebo"),
                            (f"узлы ×3, стоп −{wide*100:.0f}%",
                             [runs[f"равномерно, стоп −{wide*100:.0f}%"]], "− равномерно")):
            b = boot_vs(sets, idx, runs[nm], ys)
            out["boot"][sl][f"{nm} {lab}"] = b
            print(f"  [{sl}] {(nm + ' ' + lab)[:34]:34} мед {fmt_d(b, 'd_med')} · p10 {fmt_d(b, 'd_p10')}"
                  f" · ≤−30 {fmt_d(b, 'd_loss30')} · «в год» {fmt_d(b, 'd_py')}")
    res["q2"] = out
    return runs


# ---------------------------------------------------------------- вопрос 3: цели продажи

def q3(eps: list[dict], ctx: list[dict], res: dict, t0: float) -> dict:
    n = len(eps)
    prod = [INVAL_BUF] * n
    fams = {"структура: все уровни": "all", "структура: AVWAP 365д": "avwap",
            "структура: SMA200": "sma200", "структура: макс 180/365": "max",
            "структура: узлы объёма": "hvn"}

    def lv_tg(fam):
        return per_episode(lambda i, cx: struct_targets(cx["lv"][fam]))

    runs = {"прод +50/+150": run(eps, ctx, v_stop(prod)),
            "фикс +30/+100": run(eps, ctx, v_stop(prod, tg=fixed_targets(0.30, 1.00)))}
    for nm, fam in fams.items():
        runs[nm] = run(eps, ctx, v_stop(prod, tg=lv_tg(fam)))
    runs["структура ≥+50/≥+150"] = run(eps, ctx, v_stop(prod, tg=per_episode(
        lambda i, cx: struct_targets(cx["lv"]["all"], 0.50, 1.50))))
    placebo, placebo50 = [], []
    for s in SEEDS:
        pm = shuffle_within(eps, s)
        moved = [[x * ctx[i]["p0"] / ctx[pm[i]]["p0"] for x in ctx[pm[i]]["lv"]["all"]]
                 for i in range(n)]
        placebo.append(run(eps, ctx, v_stop(prod, tg=per_episode(
            lambda i, cx, mv=moved: struct_targets(mv[i])))))
        placebo50.append(run(eps, ctx, v_stop(prod, tg=per_episode(
            lambda i, cx, mv=moved: struct_targets(mv[i], 0.50, 1.50)))))
    base = runs["прод +50/+150"]
    out = {"variants": {}, "diag": {}, "placebo": {}, "slices": {}, "boot": {}}
    print(f"\n=== 3. ЦЕЛИ ПРОДАЖИ ПО СТРУКТУРЕ — «4 ступени до пола», стоп прод −25%, остаток — трейл "
          f"[{time.time()-t0:.0f} с] ===")
    print("  треть — у первого уровня ≥ +30% от средней, треть — у следующего ≥ +100%")
    print("  («≥+50/≥+150» — те же уровни с порогами прода); нет уровня — прод-цель (+50% / +150%)")
    print(head(34))
    for nm, rows in runs.items():
        s = summarize(rows)
        out["variants"][nm] = s
        print(fmt(nm, s, 34))
    for lab, reps in (("placebo структуры", placebo), ("placebo ≥+50/≥+150", placebo50)):
        ps = [summarize(r) for r in reps]
        s = mean_stats(ps)
        out["placebo"][lab] = s
        print(fmt(lab, s, 34) +
              f"  мед. {min(x['med_rob'] for x in ps)*100:+.1f}…{max(x['med_rob'] for x in ps)*100:+.1f}%")
    print(NOTE)

    print("\n  Где стоят цели (по средней на момент выхода; медианы) и как часто исполняются:")
    print(f"  {'вариант':34} {'T1 от ср.':>9} {'T2 от ср.':>9} {'T1 из стр.':>10} {'T2 из стр.':>10} "
          f"{'T1 исп.':>8} {'T2 исп.':>8}")
    pm1 = shuffle_within(eps, SEEDS[0])
    for nm, rows in list(runs.items()) + [("placebo структуры (seed 1)", placebo[0])]:
        t1s, t2s, s1s, s2s = [], [], [], []
        for i, r in enumerate(rows):
            if not r["avg_px"]:
                continue
            a = r["avg_px"]
            if nm == "структура ≥+50/≥+150":
                t1, t2, s1, s2 = struct_pick(ctx[i]["lv"]["all"], a, 0.50, 1.50)
            elif nm.startswith("структура"):
                t1, t2, s1, s2 = struct_pick(sorted(ctx[i]["lv"][fams[nm]]), a)
            elif nm.startswith("placebo"):
                lv = sorted(x * ctx[i]["p0"] / ctx[pm1[i]]["p0"] for x in ctx[pm1[i]]["lv"]["all"])
                t1, t2, s1, s2 = struct_pick(lv, a)
            else:
                g = (0.5, 1.5) if nm.startswith("прод") else (0.3, 1.0)
                t1, t2, s1, s2 = a * (1 + g[0]), a * (1 + g[1]), False, False
            t1s.append(t1 / a - 1)
            if t2:
                t2s.append(t2 / a - 1)
            s1s.append(s1)
            s2s.append(s2)
        d = {"t1_med": statistics.median(t1s), "t2_med": statistics.median(t2s) if t2s else None,
             "t1_struct": statistics.fmean(s1s), "t2_struct": statistics.fmean(s2s),
             "t1_hit": sum(1 for r in rows if r["lv_sold"] >= 1) / len(rows),
             "t2_hit": sum(1 for r in rows if r["lv_sold"] >= 2) / len(rows)}
        out["diag"][nm] = d
        t2m = f"{d['t2_med']*100:>+8.0f}%" if d["t2_med"] is not None else f"{'—':>9}"
        print(f"  {nm:34} {d['t1_med']*100:>+8.0f}% {t2m} {d['t1_struct']*100:>9.0f}% "
              f"{d['t2_struct']*100:>9.0f}% {d['t1_hit']*100:>7.0f}% {d['t2_hit']*100:>7.0f}%")

    show = ["прод +50/+150", "фикс +30/+100", "структура: все уровни", "структура ≥+50/≥+150",
            "структура: макс 180/365", "структура: SMA200"]
    for sl, pred in SLICES[1:]:
        out["slices"][sl] = {nm: summarize(sub(runs[nm], pred)) for nm in show}
        out["slices"][sl]["placebo структуры"] = mean_stats([summarize(sub(r, pred)) for r in placebo])
    print("\n  Разрезы: медиана / p10 / ≤−30% / «в год»")
    print("  " + f"{'вариант':20}" + "".join(f"{sl:>27}" for sl, _ in SLICES[1:]))
    for nm in show + ["placebo структуры"]:
        line = f"  {nm[:20]:20}"
        for sl, _ in SLICES[1:]:
            s = out["slices"][sl][nm]
            line += (f"{s['med_rob']*100:>+7.1f}/{s['p10']*100:>+4.0f}/{s['loss30']*100:>3.0f}/"
                     f"{s['per_year']*100:>+6.1f}  ")
        print(line)
    print(f"\n  Бутстрэп по полугодиям ({N_BOOT}×), п.п. [90%]: разница с прод-целями и с placebo")
    for sl, pred in (("все", SLICES[0][1]), ("гейт", in_gate)):
        sets = idx_sets(eps, pred)
        idx = [i for i, ep in enumerate(eps) if pred(ep)]
        out["boot"][sl] = {}
        for nm, ys, lab in (("фикс +30/+100", [base], "− прод"),
                            ("структура: все уровни", [base], "− прод"),
                            ("структура ≥+50/≥+150", [base], "− прод"),
                            ("структура: макс 180/365", [base], "− прод"),
                            ("структура: все уровни", placebo, "− placebo"),
                            ("структура ≥+50/≥+150", placebo50, "− placebo")):
            b = boot_vs(sets, idx, runs[nm], ys)
            out["boot"][sl][f"{nm} {lab}"] = b
            print(f"  [{sl}] {(nm + ' ' + lab)[:34]:34} мед {fmt_d(b, 'd_med')} · p10 {fmt_d(b, 'd_p10')}"
                  f" · ≤−30 {fmt_d(b, 'd_loss30')} · «в год» {fmt_d(b, 'd_py')}")
    res["q3"] = out
    return runs


# ---------------------------------------------------------------- main

def validate(eps: list[dict], ctx: list[dict]) -> dict:
    """sim с планом «как в ladder_dca_study» должен дать тот же P&L, что simulate."""
    checks = [("4 ступени до пола", "prod", INVAL_BUF), ("4 ступени до пола", "prod", 0.35),
              ("4 ступени до пола", "prod", 0.50), ("4 ступени до пола", None, INVAL_BUF),
              ("разом", "prod", INVAL_BUF)]
    worst, mism = 0.0, 0
    for b, stop, buf in checks:
        for ep, cx in zip(eps, ctx):
            ref = simulate(ep["seg"], ep["e"], BUY[b], SELL["прод +50/+150+трейл"], stop=stop,
                           stop_buf=buf)
            rungs = None if b == "разом" else \
                uniform_rungs(cx["p0"], cx["base_low"] * (1 - INVAL_BUF) * FLOOR_MARGIN)
            mine = sim(ep["seg"], ep["e"], buys_of(rungs),
                       cx["base_low"] * (1 - buf) if stop else None, prod_targets)
            worst = max(worst, abs(ref["pnl"] - mine["pnl"]))
            mism += ref["how"] != mine["how"]
    ok = worst < 1e-9 and mism == 0
    print(f"Сверка sim с ladder_dca_study.simulate: {len(checks)}×{len(eps)} прогонов, "
          f"макс. расхождение P&L {worst:.2e}$, разных исходов {mism} → {'OK' if ok else 'ОШИБКА'}")
    if not ok:
        sys.exit("sim разошёлся с эталонной моделью — исследование не запускаю")
    return {"runs": len(checks) * len(eps), "max_abs_pnl_diff": worst, "how_mismatch": mism}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--eps-cache", help="JSON-кэш эпизодов (отладка; по умолчанию не пишется)")
    ap.add_argument("--wide", type=float, default=0.50,
                    help="второй стоп для проверки ступеней по объёму (доля, по умолчанию 0.50)")
    args = ap.parse_args()
    t0 = time.time()
    eps, meta = load_episodes(args.eps_cache)
    print(f"Пружин: {len(eps)} на {len({ep['seg']['sym'] for ep in eps})} монетах "
          f"(в гейте {sum(1 for ep in eps if in_gate(ep))}, в окне фитиля 10.10.2025 "
          f"{sum(1 for ep in eps if in_wick(ep))}) [{time.time()-t0:.0f} с]")
    ctx = [context(ep["seg"], ep["e"]) for ep in eps]
    ap_ = sorted(cx["atrp"] for cx in ctx)
    m = len(ap_)
    print(f"ATR30 в долях цены: p10 {ap_[int(m*.1)]*100:.1f}% · медиана {ap_[m//2]*100:.1f}% · "
          f"p90 {ap_[int(m*.9)]*100:.1f}%  [{time.time()-t0:.0f} с]")
    res: dict = {"meta": dict(meta, episodes=len(eps), gate=sum(1 for ep in eps if in_gate(ep)),
                              wick_window=sum(1 for ep in eps if in_wick(ep)),
                              atrp={"p10": ap_[int(m * .1)], "med": ap_[m // 2],
                                    "p90": ap_[int(m * .9)]},
                              params={"ATR_N": ATR_N, "ATR_KS": ATR_KS, "FIXED_BUFS": FIXED_BUFS,
                                      "BUF_CAP": BUF_CAP, "VP_DAYS": VP_DAYS, "VP_STEP": VP_STEP,
                                      "VP_GAP": VP_GAP, "VP_SEP": VP_SEP, "VP_UP": VP_UP,
                                      "T1_MIN": T1_MIN, "T2_MIN": T2_MIN, "SEEDS": SEEDS,
                                      "N_BOOT": N_BOOT, "WICK_WIN": WICK_WIN})}
    res["meta"]["validation"] = validate(eps, ctx)
    r1 = q1(eps, ctx, res, t0)
    r2 = q2(eps, ctx, res, t0, args.wide)
    r3 = q3(eps, ctx, res, t0)

    # ---- портфель: k монет одного полугодия (бутстрэп ladder_dca_study.portfolio), гейт
    print(f"\n=== Портфель из k монет гейта (равный бюджет, входы одного полугодия) "
          f"[{time.time()-t0:.0f} с] ===")
    print(f"  {'вариант':34} {'k':>3} {'медиана':>8} {'p10':>7} {'p90':>7} {'P(<0)':>6} {'P(≤−20%)':>8}")
    res["portfolio"] = {}
    pv = {"прод: фикс −25%": r1["фикс −25%"], "фикс −50% (пол со стопом)": r1["фикс −50%"],
          "фикс −50%, пол −25%": r1["фикс −50%, пол −25%"], "фикс −70% (пол со стопом)": r1["фикс −70%"],
          "ATR% k=5": r1["ATR% k=5"], "ATR% k=8": r1["ATR% k=8"],
          "узлы объёма ×3": r2["узлы объёма ×3"], "структура: все уровни": r3["структура: все уровни"]}
    for nm, rows in pv.items():
        g = [r for r in rows if in_gate(r["ep"])]
        for k in (5, 10):
            p = portfolio(g, k)
            res["portfolio"][f"{nm} k={k}"] = p
            print(f"  {nm:34} {k:>3} {p['med']*100:>+7.1f}% {p['p10']*100:>+6.1f}% "
                  f"{p['p90']*100:>+6.1f}% {p['p_loss']*100:>5.0f}% {p['p_loss20']*100:>7.0f}%")
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
