"""Walk-forward сравнение детекторов входа «пружина»: старый vs P2 (анти-зомби + dry-up).

Данные: Bybit spot public API (дневные свечи, ~1000 шт = окт-2023..сейчас),
turnover (объём в USDT) — для vol_trend. Только данные <= t (без лукахеда).

Отвечает на вопросы:
  1. Подняли ли P2-фильтры (days_since_low >= N, vol_trend <= X) качество входов?
     Метрика: медианный потенциал (max за 365д) и net-return под фикс. выходом.
  2. Какие пороги оптимальны? Сетка days_since_low x dryup.
  3. Как ведёт себя hodl-выход (лестница +100/+300, взводимый трейлинг) vs свинг.

Честные ограничения: одно рыночное окно, survivorship bias, дневные закрытия.
"""
from __future__ import annotations

import json
import statistics
import time
import urllib.parse
import urllib.request

API = "https://api.bybit.com"
FEE = 0.0015
HORIZON = 365
COOLDOWN = 90
TOP_N = 120

_STABLE_BASES = {"USDC", "DAI", "TUSD", "USDE", "FDUSD", "USDD", "FRAX", "PYUSD",
                 "USDP", "SUSD", "USDS", "GHO", "USD0", "BUSD", "EUR", "BRZ", "XUSD"}
_WRAP = {"WBTC", "WETH", "STETH", "WSTETH", "RETH", "CBETH", "WEETH", "MSOL", "JITOSOL"}


def get(path: str, params: dict) -> dict | None:
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "detector-study/0.1"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return None


def top_symbols(n: int) -> list[str]:
    data = get("/v5/market/tickers", {"category": "spot"})
    rows = (data or {}).get("result", {}).get("list", [])
    cands = []
    for r in rows:
        sym = r.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in _STABLE_BASES or base in _WRAP:
            continue
        if any(base.endswith(s) for s in ("3L", "3S", "2L", "2S")):
            continue
        try:
            cands.append((float(r.get("turnover24h", 0)), sym))
        except ValueError:
            continue
    cands.sort(reverse=True)
    return [s for _, s in cands[:n]]


def daily(symbol: str) -> tuple[list[float], list[float]]:
    """(closes, turnover_usdt), oldest -> newest."""
    data = get("/v5/market/kline",
               {"category": "spot", "symbol": symbol, "interval": "D", "limit": 1000})
    rows = (data or {}).get("result", {}).get("list", [])
    rows.reverse()
    closes, turn = [], []
    for r in rows:
        try:
            closes.append(float(r[4])); turn.append(float(r[6]))
        except (ValueError, IndexError):
            return [], []
    return closes, turn


def entry_features(closes: list[float], turn: list[float], t: int) -> dict | None:
    """Фичи детектора на день t (только данные <= t). None = мало истории."""
    if t < 200:
        return None
    c = closes[t]
    ath = max(closes[:t + 1])
    w = closes[t - 179:t + 1]
    v = turn[t - 179:t + 1]
    lo, hi = min(w), max(w)
    rets = [w[i] / w[i - 1] - 1 for i in range(1, len(w)) if w[i - 1] > 0]
    if len(rets) < 60:
        return None
    base, recent = rets[:-30], rets[-30:]
    bv = statistics.pstdev(base)
    lo_i = min(range(len(w)), key=lambda i: w[i])
    vv = [x for x in v if x > 0]
    vol_trend = None
    if len(vv) > 40:
        bvol = statistics.fmean(vv[:-30])
        vol_trend = statistics.fmean(vv[-30:]) / bvol if bvol > 0 else None
    return {
        "dd": 1 - c / ath if ath > 0 else 0.0,
        "range_pos": (c - lo) / (hi - lo) if hi > lo else 0.5,
        "contraction": statistics.pstdev(recent) / bv if bv > 0 else None,
        "trend30": c / closes[t - 30] - 1 if closes[t - 30] > 0 else 0.0,
        "days_since_low": (len(w) - 1) - lo_i,
        "vol_trend": vol_trend,
    }


def is_entry(f: dict, min_dsl: int, max_dryup: float | None) -> bool:
    """Базовый детектор + P2-фильтры (min_dsl=0 / max_dryup=None = выключены)."""
    if f["dd"] < 0.70 or f["range_pos"] > 0.5:
        return False
    if f["contraction"] is None or f["contraction"] > 0.75:
        return False
    if f["trend30"] <= -0.15:
        return False
    if f["days_since_low"] < min_dsl:
        return False
    if max_dryup is not None and (f["vol_trend"] is None or f["vol_trend"] > max_dryup):
        return False
    return True


def find_entries(closes, turn, min_dsl: int, max_dryup: float | None) -> list[int]:
    out, t, n = [], 200, len(closes)
    while t < n - 30:
        f = entry_features(closes, turn, t)
        if f and is_entry(f, min_dsl, max_dryup):
            out.append(t)
            t += COOLDOWN
        else:
            t += 1
    return out


def sim_swing(closes, e) -> float:
    """Свинг-выход: лестница +30/+60 по 1/3 + trail 25% + инвалидация."""
    return _sim(closes, e, [(0.30, 1 / 3), (0.60, 1 / 3)], 0.25, arm=0.0)


def sim_hodl(closes, e) -> float:
    """Hodl-выход (config stage8_exit): +100%->25%, +300%->25%, трейлинг 30%
    взводится после пика +60%, инвалидация base_low-5%."""
    return _sim(closes, e, [(1.0, 0.25), (3.0, 0.25)], 0.30, arm=0.60)


def _sim(closes, e, ladder, trail, arm: float) -> float:
    entry = closes[e]
    base_low = min(closes[max(0, e - 30):e + 1]) * 0.95
    end = min(e + HORIZON, len(closes) - 1)
    remaining, proceeds, hwm, li = 1.0, 0.0, entry, 0
    for t in range(e + 1, end + 1):
        c = closes[t]
        hwm = max(hwm, c)
        if c < base_low:
            proceeds += remaining * c; remaining = 0.0
            break
        while li < len(ladder) and c >= entry * (1 + ladder[li][0]) and remaining > 1e-9:
            sell = min(ladder[li][1], remaining)
            proceeds += sell * c; remaining -= sell; li += 1
        if remaining <= 1e-9:
            break
        armed = (hwm / entry - 1) >= arm
        if armed and c <= hwm * (1 - trail):
            proceeds += remaining * c; remaining = 0.0
            break
    if remaining > 1e-9:
        proceeds += remaining * closes[end]
    return proceeds * (1 - FEE) / (entry * (1 + FEE)) - 1


def stats(nets: list[float]) -> str:
    if not nets:
        return "   —"
    med = statistics.median(nets) * 100
    mean = statistics.fmean(nets) * 100
    win = sum(1 for x in nets if x > 0) / len(nets) * 100
    return f"med {med:+6.1f}%  mean {mean:+6.1f}%  win {win:3.0f}%"


def main() -> int:
    syms = top_symbols(TOP_N)
    print(f"symbols: {len(syms)}, загрузка...")
    data: list[tuple[str, list[float], list[float]]] = []
    for i, s in enumerate(syms):
        closes, turn = daily(s)
        if len(closes) >= 400:
            data.append((s, closes, turn))
        time.sleep(0.05)
    print(f"с историей >=400д: {len(data)}\n")

    # --- 1. Старый vs P2-детектор (пороги из config: dsl=14, dryup=0.6) ---
    variants = [
        ("старый (без P2)",        0,  None),
        ("+гейт лоу>=14д",         14, None),
        ("+dry-up<=0.6",           0,  0.6),
        ("P2: гейт+dry-up",        14, 0.6),
    ]
    print(f"{'детектор':22} {'n':>4} {'потенц.med':>10} {'p75':>7}   "
          f"{'свинг-выход':>34}   {'hodl-выход':>34}")
    results = {}
    for name, dsl, dry in variants:
        pots, swing, hodl = [], [], []
        for s, closes, turn in data:
            for e in find_entries(closes, turn, dsl, dry):
                end = min(e + HORIZON, len(closes) - 1)
                pots.append(max(closes[e + 1:end + 1]) / closes[e] - 1 if end > e else 0)
                swing.append(sim_swing(closes, e))
                hodl.append(sim_hodl(closes, e))
        pm = statistics.median(pots) * 100 if pots else 0
        p75 = (sorted(pots)[3 * len(pots) // 4] * 100) if pots else 0
        print(f"{name:22} {len(pots):>4} {pm:>9.0f}% {p75:>6.0f}%   "
              f"{stats(swing):>34}   {stats(hodl):>34}")
        results[name] = {"n": len(pots), "pot_med": pm,
                         "swing": stats(swing), "hodl": stats(hodl)}

    # --- 2. Сетка порогов (метрика: hodl-выход mean + n) ---
    print("\n=== сетка порогов (hodl-выход) ===")
    print(f"{'dsl\\dryup':>9} | " + " | ".join(f"{d if d else 'выкл':>22}" for d in [None, 0.8, 0.6, 0.5]))
    for dsl in (0, 7, 14, 21, 30):
        cells = []
        for dry in (None, 0.8, 0.6, 0.5):
            nets = []
            for s, closes, turn in data:
                for e in find_entries(closes, turn, dsl, dry):
                    nets.append(sim_hodl(closes, e))
            if nets:
                cells.append(f"n={len(nets):>3} m={statistics.fmean(nets)*100:+6.1f}%")
            else:
                cells.append("n=  0       —")
        print(f"{dsl:>9} | " + " | ".join(f"{c:>22}" for c in cells))

    with open("detector_study_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\nsaved -> detector_study_results.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
