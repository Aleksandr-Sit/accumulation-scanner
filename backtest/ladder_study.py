"""Эмпирическое сравнение вариантов лестницы выхода на реальных данных Bybit spot.

Методика:
1. Топ-N USDT-пар Bybit spot по обороту (public API, без ключа).
2. Дневные свечи (~1000 шт = окт-2023..сейчас).
3. Детектор входа "пружина" (упрощённый аналог scanner/stages/zone.py, только данные <= t):
   dd от ATH окна >= 70%, range_pos(180д) <= 0.5, сжатие волатильности (чистая база),
   не падающий нож (30д тренд > -15%).
4. Симуляция вариантов выхода от каждого входа: лестницы + трейлинг от HWM +
   инвалидация (пробой лоу базы -5%). Издержки 0.15%/сторона (Bybit 0.1% + спред).
5. Сравнение: медиана/среднее net-return, win-rate, дни удержания, capture ratio.

Честные ограничения: survivorship bias (делистнутые не видны), одно рыночное окно,
закрытия дневных свечей (без внутридневных), вход — прокси детектора сканера.
"""
from __future__ import annotations

import json
import statistics
import time
import urllib.parse
import urllib.request

API = "https://api.bybit.com"
FEE = 0.0015          # за сторону: 0.1% комиссия + ~0.05% спред
HORIZON = 365         # макс. дней удержания
COOLDOWN = 90         # мин. дней между эпизодами одного тикера
TOP_N = 120

_STABLE_BASES = {"USDC", "DAI", "TUSD", "USDE", "FDUSD", "USDD", "FRAX", "PYUSD",
                 "USDP", "SUSD", "USDS", "GHO", "USD0", "BUSD", "EUR", "BRZ", "XUSD"}
_WRAP = {"WBTC", "WETH", "STETH", "WSTETH", "RETH", "CBETH", "WEETH", "MSOL", "JITOSOL"}


def get(path: str, params: dict) -> dict | None:
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "ladder-study/0.1"})
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


def daily_closes(symbol: str) -> tuple[list[float], list[int]]:
    data = get("/v5/market/kline",
               {"category": "spot", "symbol": symbol, "interval": "D", "limit": 1000})
    rows = (data or {}).get("result", {}).get("list", [])
    rows.reverse()  # newest-first -> oldest-first
    closes, tss = [], []
    for r in rows:
        try:
            closes.append(float(r[4])); tss.append(int(r[0]))
        except (ValueError, IndexError):
            return [], []
    return closes, tss


def find_entries(closes: list[float]) -> list[int]:
    """Индексы входов 'пружина' — только данные <= t, кулдаун между эпизодами."""
    n = len(closes)
    entries = []
    t = 200
    while t < n - 30:  # нужен хоть месяц форварда
        c = closes[t]
        ath = max(closes[:t + 1])
        dd = 1 - c / ath if ath > 0 else 0
        if dd < 0.70:
            t += 1; continue
        w = closes[t - 179:t + 1]
        lo, hi = min(w), max(w)
        rp = (c - lo) / (hi - lo) if hi > lo else 0.5
        if rp > 0.5:
            t += 1; continue
        rets = [w[i] / w[i - 1] - 1 for i in range(1, len(w)) if w[i - 1] > 0]
        if len(rets) < 60:
            t += 1; continue
        base, recent = rets[:-30], rets[-30:]
        bv = statistics.pstdev(base)
        contr = statistics.pstdev(recent) / bv if bv > 0 else None
        if contr is None or contr > 0.75:
            t += 1; continue
        if closes[t - 30] > 0 and c / closes[t - 30] - 1 <= -0.15:
            t += 1; continue
        entries.append(t)
        t += COOLDOWN
    return entries


def simulate(closes: list[float], e: int, ladder: list[tuple[float, float]],
             trail: float | None, use_invalidation: bool = True) -> dict:
    entry = closes[e]
    base_low = min(closes[max(0, e - 30):e + 1]) * 0.95
    end = min(e + HORIZON, len(closes) - 1)
    remaining, proceeds, hwm, li = 1.0, 0.0, entry, 0
    exit_day, closed = end, False
    for t in range(e + 1, end + 1):
        c = closes[t]
        hwm = max(hwm, c)
        if use_invalidation and c < base_low:
            proceeds += remaining * c; remaining = 0.0
            exit_day, closed = t, True
            break
        while li < len(ladder) and c >= entry * (1 + ladder[li][0]) and remaining > 1e-9:
            sell = min(ladder[li][1], remaining)
            proceeds += sell * c; remaining -= sell; li += 1
        if remaining <= 1e-9:
            exit_day, closed = t, True
            break
        if trail is not None and c <= hwm * (1 - trail):
            proceeds += remaining * c; remaining = 0.0
            exit_day, closed = t, True
            break
    if remaining > 1e-9:
        proceeds += remaining * closes[end]  # горизонт: продажа по последней цене
    net = proceeds * (1 - FEE) / (entry * (1 + FEE)) - 1
    maxc = max(closes[e + 1:end + 1]) if end > e else entry
    potential = maxc / entry - 1
    return {"net": net, "days": exit_day - e, "closed": closed, "potential": potential}


VARIANTS: dict[str, dict] = {
    "hold90 (наив. база)":      {"ladder": [], "trail": None, "hold": 90},
    "trail20":                  {"ladder": [], "trail": 0.20},
    "trail25":                  {"ladder": [], "trail": 0.25},
    "trail30":                  {"ladder": [], "trail": 0.30},
    "L+30/+60, trail25":        {"ladder": [(0.30, 1/3), (0.60, 1/3)], "trail": 0.25},
    "L+50/+100, trail25":       {"ladder": [(0.50, 1/3), (1.00, 1/3)], "trail": 0.25},
    "L+100/+200, trail30":      {"ladder": [(1.00, 1/3), (2.00, 1/3)], "trail": 0.30},
}


def simulate_hold(closes: list[float], e: int, days: int) -> dict:
    end = min(e + days, len(closes) - 1)
    net = closes[end] * (1 - FEE) / (closes[e] * (1 + FEE)) - 1
    hend = min(e + HORIZON, len(closes) - 1)
    maxc = max(closes[e + 1:hend + 1]) if hend > e else closes[e]
    return {"net": net, "days": end - e, "closed": True,
            "potential": maxc / closes[e] - 1}


def btc_regime_map() -> dict[int, bool]:
    """ts -> бычий ли режим BTC (30д тренд > 0 И цена > SMA50) на этот день."""
    closes, tss = daily_closes("BTCUSDT")
    out: dict[int, bool] = {}
    for i, ts in enumerate(tss):
        if i < 50:
            continue
        trend_up = closes[i] > closes[i - 30]
        above_sma = closes[i] > statistics.fmean(closes[i - 49:i + 1])
        out[ts] = trend_up and above_sma
    return out


def main() -> int:
    regime = btc_regime_map()
    syms = top_symbols(TOP_N)
    print(f"symbols: {len(syms)}")
    episodes: list[tuple[str, list[float], int, bool | None, int]] = []
    for i, s in enumerate(syms):
        closes, tss = daily_closes(s)
        if len(closes) < 400:
            continue
        for e in find_entries(closes):
            ts = tss[e]
            year = time.gmtime(ts / 1000).tm_year
            episodes.append((s, closes, e, regime.get(ts), year))
        time.sleep(0.05)
        if (i + 1) % 30 == 0:
            print(f"  ...{i + 1}/{len(syms)} scanned, episodes={len(episodes)}")
    print(f"episodes: {len(episodes)} on "
          f"{len(set(s for s, *_ in episodes))} symbols")
    if not episodes:
        print("no episodes found"); return 1

    years: dict[int, int] = {}
    for *_, y in episodes:
        years[y] = years.get(y, 0) + 1
    print("по годам:", dict(sorted(years.items())))
    n_bull = sum(1 for *_, r, _ in episodes if r is True)
    print(f"режим BTC на входе: бычий {n_bull}, медвежий/флэт {len(episodes) - n_bull}")

    results: dict[str, list[dict]] = {k: [] for k in VARIANTS}
    for sym, closes, e, reg, year in episodes:
        for name, v in VARIANTS.items():
            if "hold" in v:
                r = simulate_hold(closes, e, v["hold"])
            else:
                r = simulate(closes, e, v["ladder"], v["trail"])
            r["sym"], r["regime"], r["year"] = sym, reg, year
            results[name].append(r)

    print(f"\n{'вариант':24} {'n':>4} {'медиана':>8} {'среднее':>8} "
          f"{'win%':>5} {'p10':>7} {'p90':>7} {'дни':>4} {'capt%':>6}")
    summary = {}
    for name, rows in results.items():
        nets = [r["net"] for r in rows]
        wins = sum(1 for x in nets if x > 0) / len(nets) * 100
        days = statistics.fmean(r["days"] for r in rows)
        qs = statistics.quantiles(nets, n=10)
        caps = [r["net"] / r["potential"] for r in rows if r["potential"] > 0.2]
        cap = statistics.fmean(caps) * 100 if caps else 0.0
        med, mean = statistics.median(nets), statistics.fmean(nets)
        print(f"{name:24} {len(nets):>4} {med * 100:>7.1f}% {mean * 100:>7.1f}% "
              f"{wins:>4.0f}% {qs[0] * 100:>6.1f}% {qs[8] * 100:>6.1f}% "
              f"{days:>4.0f} {cap:>5.1f}%")
        summary[name] = {"n": len(nets), "median": med, "mean": mean, "win": wins,
                         "p10": qs[0], "p90": qs[8], "days": days, "capture": cap}

    # разрез по режиму BTC на входе — ценность фильтра режима (У4)
    print("\n=== разрез по режиму BTC на входе ===")
    print(f"{'вариант':24} {'реж.':6} {'n':>4} {'медиана':>8} {'среднее':>8} {'win%':>5}")
    for name in ("trail20", "L+30/+60, trail25", "L+50/+100, trail25"):
        for label, want in (("BULL", True), ("BEAR", False)):
            rows = [r for r in results[name]
                    if (r["regime"] is True) == want and r["regime"] is not None]
            if len(rows) < 5:
                continue
            nets = [r["net"] for r in rows]
            wins = sum(1 for x in nets if x > 0) / len(nets) * 100
            print(f"{name:24} {label:6} {len(nets):>4} "
                  f"{statistics.median(nets) * 100:>7.1f}% "
                  f"{statistics.fmean(nets) * 100:>7.1f}% {wins:>4.0f}%")

    # распределение потенциала отскоков (сколько вообще давали эпизоды)
    pots = sorted(r["potential"] for r in results["trail25"])
    print(f"\nпотенциал (max за {HORIZON}д): медиана {statistics.median(pots)*100:.0f}%, "
          f"p25 {pots[len(pots)//4]*100:.0f}%, p75 {pots[3*len(pots)//4]*100:.0f}%, "
          f"max {pots[-1]*100:.0f}%")
    with open("ladder_study_results.json", "w", encoding="utf-8") as f:
        json.dump({"episodes": len(episodes), "summary": summary}, f,
                  ensure_ascii=False, indent=2)
    print("\nsaved -> ladder_study_results.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
