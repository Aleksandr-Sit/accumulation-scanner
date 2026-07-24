"""Многоцикловая калибровка вероятностей входа/выхода. Binance klines с 2017 (free).

Зачем: ladder_study/detector_study видели одно окно 2023–2026 (~1 медвежий цикл) —
вероятности отскока и hodl-уровни (+100/+300) на нём не откалибровать. Здесь —
полная дневная история С ЛИСТИНГА каждой монеты (пагинация /api/v3/klines),
циклы 2017-18 / 2019-20 / 2021 / 2022 / 2023-24 / 2025-26.

Считает (walk-forward, данные <= t):
  1. P(max форвард >= X% за 730д | вход-пружина) для X in {30,50,100,200,300} —
     по эпохам входа и по режиму BTC на входе.
  2. P(инвалидация раньше +30%) — пробой лоу базы -5% до первого +30%.
  3. Сплит по dry-up объёма (vol_trend <= 0.6 vs >) — проверка фильтра на всех циклах.
  4. Симуляция hodl-выхода (лестница +100/+300, трейлинг 30% после arm +60%,
     инвалидация) net-of-fees по эпохам.
  5. Калибровка трейлинга: распределение макс. отката от HWM после достижения +100%.

Честные ограничения: survivorship bias (Binance отдаёт только живые сегодня пары —
делистнутые-2018/2022 невидимы, вероятности = ВЕРХНЯЯ граница, помечено в отчёте);
дневные закрытия; издержки 0.15%/сторона.
"""
from __future__ import annotations

import json
import statistics
import time
import urllib.parse
import urllib.request

API = "https://api.binance.com"
FEE = 0.0015
HORIZON = 730          # hodl-горизонт: 2 года
COOLDOWN = 90
TOP_N = 150
LEVELS = (0.30, 0.50, 1.00, 2.00, 3.00)

_EXCLUDE_BASE = {"USDC", "DAI", "TUSD", "FDUSD", "BUSD", "USDP", "EUR", "GBP",
                 "AEUR", "PAX", "SUSD", "USDS", "WBTC", "WETH", "WBETH", "BETH"}
_EXCLUDE_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")


def get(path: str, params: dict) -> object:
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "history-calibration/0.1"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(2.0 * (attempt + 1))
    return None


def top_symbols(n: int) -> list[str]:
    tick = get("/api/v3/ticker/24hr", {})
    rows = tick if isinstance(tick, list) else []
    cands = []
    for r in rows:
        sym = r.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in _EXCLUDE_BASE or any(base.endswith(s) for s in _EXCLUDE_SUFFIX):
            continue
        try:
            cands.append((float(r.get("quoteVolume", 0)), sym))
        except (ValueError, TypeError):
            continue
    cands.sort(reverse=True)
    return [s for _, s in cands[:n]]


def full_history(symbol: str) -> tuple[list[int], list[float], list[float]]:
    """(day_ts_sec, closes, quote_volumes) с листинга. Пагинация по 1000 свечей."""
    ts, closes, qvol = [], [], []
    start = 1483228800000  # 2017-01-01
    while True:
        data = get("/api/v3/klines", {"symbol": symbol, "interval": "1d",
                                      "startTime": start, "limit": 1000})
        if not isinstance(data, list) or not data:
            break
        for k in data:
            try:
                ts.append(int(k[0]) // 1000)
                closes.append(float(k[4]))
                qvol.append(float(k[7]))
            except (ValueError, IndexError):
                return [], [], []
        if len(data) < 1000:
            break
        start = int(data[-1][0]) + 86400000
        time.sleep(0.05)
    return ts, closes, qvol


def era_of(ts_sec: int) -> str:
    y = time.gmtime(ts_sec).tm_year
    if y <= 2018:
        return "2017-18"
    if y <= 2020:
        return "2019-20"
    if y == 2021:
        return "2021"
    if y == 2022:
        return "2022"
    if y <= 2024:
        return "2023-24"
    return "2025-26"


ERAS = ("2017-18", "2019-20", "2021", "2022", "2023-24", "2025-26")


def btc_regime_map() -> dict[int, bool]:
    ts, closes, _ = full_history("BTCUSDT")
    out = {}
    for i in range(50, len(ts)):
        bull = closes[i] > closes[i - 30] and closes[i] > statistics.fmean(closes[i - 49:i + 1])
        out[ts[i]] = bull
    return out


def find_entries(closes: list[float], qvol: list[float]) -> list[tuple[int, bool]]:
    """[(индекс входа, dry_up)] — детектор пружины сканера (пороги config)."""
    out, t, n = [], 200, len(closes)
    while t < n - 30:
        c = closes[t]
        ath = max(closes[:t + 1])
        if ath <= 0 or 1 - c / ath < 0.70:
            t += 1; continue
        w = closes[t - 179:t + 1]
        lo, hi = min(w), max(w)
        if hi > lo and (c - lo) / (hi - lo) > 0.5:
            t += 1; continue
        rets = [w[i] / w[i - 1] - 1 for i in range(1, len(w)) if w[i - 1] > 0]
        if len(rets) < 60:
            t += 1; continue
        bv = statistics.pstdev(rets[:-30])
        if bv <= 0 or statistics.pstdev(rets[-30:]) / bv > 0.70:
            t += 1; continue
        if closes[t - 30] > 0 and c / closes[t - 30] - 1 <= -0.15:
            t += 1; continue
        vv = [x for x in qvol[t - 179:t + 1] if x > 0]
        dry = False
        if len(vv) > 40:
            bvol = statistics.fmean(vv[:-30])
            dry = bvol > 0 and statistics.fmean(vv[-30:]) / bvol <= 0.6
        out.append((t, dry))
        t += COOLDOWN
    return out


def episode_metrics(closes: list[float], e: int) -> dict:
    """Форвардные метрики эпизода (данные ПОСЛЕ t — это оценка, не сигнал)."""
    entry = closes[e]
    base_low = min(closes[max(0, e - 30):e + 1]) * 0.95
    end = min(e + HORIZON, len(closes) - 1)
    hit = {x: None for x in LEVELS}       # день достижения уровня
    inval_day = None
    hwm = entry
    max_retrace_after_100 = 0.0
    reached_100 = False
    for t in range(e + 1, end + 1):
        c = closes[t]
        hwm = max(hwm, c)
        g = c / entry - 1
        for x in LEVELS:
            if hit[x] is None and g >= x:
                hit[x] = t - e
        if hit[1.00] is not None:
            reached_100 = True
        if reached_100 and hwm > 0:
            max_retrace_after_100 = max(max_retrace_after_100, 1 - c / hwm)
        if inval_day is None and c < base_low:
            inval_day = t - e
    inval_first = (inval_day is not None
                   and (hit[0.30] is None or inval_day < hit[0.30]))
    return {"hit": hit, "inval_first": inval_first,
            "retrace_after_100": max_retrace_after_100 if reached_100 else None,
            "truncated": (end - e) < HORIZON}


def sim_hodl(closes: list[float], e: int, inval_buf: float | None = 0.05,
             confirm_days: int = 1) -> float:
    """Hodl-выход из config: +100->25%, +300->25%, trail 30% после arm +60%.

    inval_buf: буфер инвалидации под лоу базы (None = инвалидация выключена).
    confirm_days: сколько закрытий подряд ниже пола нужно для выхода.
    """
    entry = closes[e]
    base_low = (min(closes[max(0, e - 30):e + 1]) * (1 - inval_buf)
                if inval_buf is not None else 0.0)
    end = min(e + HORIZON, len(closes) - 1)
    remaining, proceeds, hwm, li = 1.0, 0.0, entry, 0
    ladder = [(1.0, 0.25), (3.0, 0.25)]
    below = 0
    for t in range(e + 1, end + 1):
        c = closes[t]
        hwm = max(hwm, c)
        below = below + 1 if (base_low > 0 and c < base_low) else 0
        if below >= confirm_days and base_low > 0:
            proceeds += remaining * c; remaining = 0.0; break
        while li < len(ladder) and c >= entry * (1 + ladder[li][0]) and remaining > 1e-9:
            proceeds += min(ladder[li][1], remaining) * c
            remaining -= min(ladder[li][1], remaining); li += 1
        if remaining <= 1e-9:
            break
        if (hwm / entry - 1) >= 0.60 and c <= hwm * 0.70:
            proceeds += remaining * c; remaining = 0.0; break
    if remaining > 1e-9:
        proceeds += remaining * closes[end]
    return proceeds * (1 - FEE) / (entry * (1 + FEE)) - 1


def pct(part: int, total: int) -> str:
    return f"{part / total * 100:4.0f}%" if total else "   —"


def main() -> int:
    t0 = time.time()
    regime = btc_regime_map()
    syms = top_symbols(TOP_N)
    print(f"symbols: {len(syms)}; BTC история: {len(regime)} дней")

    episodes = []   # dict: sym, era, bull, dry, metrics, hodl_net
    total_days = 0
    for i, s in enumerate(syms):
        if s == "BTCUSDT":
            continue
        ts, closes, qvol = full_history(s)
        if len(closes) < 300:
            continue
        total_days += len(closes)
        for e, dry in find_entries(closes, qvol):
            episodes.append({
                "sym": s, "era": era_of(ts[e]), "bull": regime.get(ts[e]),
                "dry": dry, "m": episode_metrics(closes, e),
                "hodl": sim_hodl(closes, e),
                "_closes": closes, "_e": e,   # для сетки инвалидации (4b)
            })
        if (i + 1) % 30 == 0:
            print(f"  ...{i + 1}/{len(syms)}, эпизодов {len(episodes)}, "
                  f"{time.time() - t0:.0f}s")

    n_syms = len({ep["sym"] for ep in episodes})
    print(f"\nПокрытие: {n_syms} монет, {total_days / 365:.0f} монето-лет, "
          f"{len(episodes)} эпизодов. ВНИМАНИЕ: survivorship bias — только "
          f"живые сегодня пары, вероятности = верхняя граница.\n")

    # --- 1. Вероятности достижения уровней по эпохам ---
    header = f"{'эпоха':8} {'n':>4} " + " ".join(f"P(+{int(x*100)})" for x in LEVELS) \
             + "  P(инв.первой)  усечён"
    print("=== P(достижение уровня за 730д | вход-пружина), по эпохе входа ===")
    print(header)
    out = {"eras": {}, "regime": {}, "dryup": {}, "retrace": {}}
    for era in ERAS:
        eps = [ep for ep in episodes if ep["era"] == era]
        if not eps:
            continue
        n = len(eps)
        row = [pct(sum(1 for ep in eps if ep["m"]["hit"][x] is not None), n)
               for x in LEVELS]
        invf = pct(sum(1 for ep in eps if ep["m"]["inval_first"]), n)
        trunc = pct(sum(1 for ep in eps if ep["m"]["truncated"]), n)
        print(f"{era:8} {n:>4} " + "   ".join(row) + f"      {invf}      {trunc}")
        out["eras"][era] = {"n": n, "rows": row, "inval_first": invf}

    # --- 2. По режиму BTC ---
    print("\n=== то же по режиму BTC на входе ===")
    for label, want in (("BULL", True), ("BEAR", False)):
        eps = [ep for ep in episodes if ep["bull"] is want]
        if not eps:
            continue
        n = len(eps)
        row = [pct(sum(1 for ep in eps if ep["m"]["hit"][x] is not None), n)
               for x in LEVELS]
        print(f"{label:8} {n:>4} " + "   ".join(row))
        out["regime"][label] = {"n": n, "rows": row}

    # --- 3. Dry-up на всех циклах ---
    print("\n=== dry-up объёма (vol_trend<=0.6) на всех циклах ===")
    for label, want in (("dry", True), ("не dry", False)):
        eps = [ep for ep in episodes if ep["dry"] is want]
        if not eps:
            continue
        n = len(eps)
        p100 = pct(sum(1 for ep in eps if ep["m"]["hit"][1.00] is not None), n)
        invf = pct(sum(1 for ep in eps if ep["m"]["inval_first"]), n)
        hodl = statistics.fmean(ep["hodl"] for ep in eps) * 100
        print(f"{label:8} {n:>4}  P(+100)={p100}  P(инв.первой)={invf}  "
              f"hodl mean {hodl:+.1f}%")
        out["dryup"][label] = {"n": n, "p100": p100, "hodl_mean": round(hodl, 1)}

    # --- 4. Hodl-выход по эпохам ---
    print("\n=== hodl-выход (+100/+300, trail30 после +60) net-of-fees, по эпохам ===")
    for era in ERAS:
        nets = [ep["hodl"] for ep in episodes if ep["era"] == era]
        if len(nets) < 5:
            continue
        win = sum(1 for x in nets if x > 0) / len(nets) * 100
        print(f"{era:8} n={len(nets):>4}  med {statistics.median(nets)*100:+7.1f}%  "
              f"mean {statistics.fmean(nets)*100:+7.1f}%  win {win:3.0f}%")

    # --- 4b. Сетка инвалидации: буфер × подтверждение (главный вопрос — shakeout) ---
    print("\n=== сетка инвалидации (hodl-выход, все эпохи кроме усечённой 2025-26) ===")
    full = [(ep["sym"], ep["era"]) for ep in episodes]  # noqa: F841 (документация)
    cases = [("буфер 5%, 1д (текущий)", 0.05, 1), ("буфер 15%, 1д", 0.15, 1),
             ("буфер 25%, 1д", 0.25, 1), ("буфер 5%, 5д подряд", 0.05, 5),
             ("буфер 15%, 5д подряд", 0.15, 5), ("без инвалидации", None, 1)]
    # пере-симулируем: нужен доступ к рядам -> кэшируем при основном проходе
    print(f"{'вариант':26} {'mean':>8} {'med':>8} {'win':>5} | {'2022 mean':>9}")
    for name, buf, conf in cases:
        nets, nets22 = [], []
        for ep in episodes:
            if ep["era"] == "2025-26":
                continue
            net = sim_hodl(ep["_closes"], ep["_e"], buf, conf)
            nets.append(net)
            if ep["era"] == "2022":
                nets22.append(net)
        if not nets:
            continue
        win = sum(1 for x in nets if x > 0) / len(nets) * 100
        m22 = statistics.fmean(nets22) * 100 if nets22 else 0.0
        print(f"{name:26} {statistics.fmean(nets)*100:+7.1f}% "
              f"{statistics.median(nets)*100:+7.1f}% {win:4.0f}% | {m22:+8.1f}%")

    # --- 5. Калибровка трейлинга: откат от HWM после достижения +100% ---
    rets = sorted(ep["m"]["retrace_after_100"] for ep in episodes
                  if ep["m"]["retrace_after_100"] is not None)
    if rets:
        q = lambda f: rets[min(len(rets) - 1, int(len(rets) * f))] * 100
        print(f"\n=== макс. откат от HWM после +100% (n={len(rets)}) ===")
        print(f"p25 {q(0.25):.0f}%  p50 {q(0.5):.0f}%  p75 {q(0.75):.0f}%  "
              f"p90 {q(0.9):.0f}%  -> трейлинг 30%: выбьет "
              f"{sum(1 for r in rets if r >= 0.30) / len(rets) * 100:.0f}% позиций")
        out["retrace"] = {"p25": q(0.25), "p50": q(0.5), "p75": q(0.75), "p90": q(0.9)}

    with open("history_calibration_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\nsaved -> history_calibration_results.json ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
