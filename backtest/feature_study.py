"""Дата-исследование входа/выхода: какие признаки предсказывают удачную пружину,
помогает ли вход по подтверждению, и какая структура выхода захватывает больше.

Данные: Binance daily klines с 2017 (закрытые свечи, walk-forward, данные <= t).
ВНИМАНИЕ: survivor-only (Binance отдаёт живые сегодня пары) → все P — ВЕРХНЯЯ
граница (см. survivorship_study: haircut ×0.73). Числа сравнивать МЕЖДУ собой
(какой признак/тактика лучше), а не как абсолютную вероятность.

Три блока:
  A. Univariate: P(+100%) и mean hodl-net по бакетам каждого признака входа —
     что реально разделяет победителей и трупы.
  B. Вход по подтверждению: пружина-сейчас vs пружина+подтверждение (реклейм
     SMA20 / первый отскок +15% за 20д) — стоит ли ждать.
  C. Структура выхода на «раннерах» (дошли до +50%): текущая vs альтернативы.
"""
from __future__ import annotations

import json
import statistics
import time
import urllib.parse
import urllib.request

API = "https://api.binance.com"
FEE = 0.0015
HORIZON = 730
COOLDOWN = 90
TOP_N = 200
CONFIRM_WINDOW = 20

# Не-альты: стейблы, фиат, обёртки, золото. Токенизированные акции (NVDAB, TSLAB…)
# режутся по тегам Binance (_tagged_non_alts), список ниже — фолбэк без сети.
_EXCL = {"USDC", "DAI", "TUSD", "FDUSD", "BUSD", "USDP", "EUR", "GBP", "AEUR",
         "PAX", "SUSD", "USDS", "WBTC", "WETH", "WBETH", "BETH", "EURI",
         "USD1", "RLUSD", "XUSD", "BFUSD", "USDE", "PYUSD", "EURC", "U",
         "PAXG", "XAUT"}
_NON_ALT_TAGS = {"bStocks", "tCommodities", "stablecoin"}
_PRODUCTS = "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products"


def get(path: str, params: dict):
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "feat/0.1"})
    for a in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(2.0 * (a + 1))
    return None


def _tagged_non_alts() -> set[str]:
    """Базовые активы с тегами Binance bStocks / tCommodities / stablecoin (публичный
    эндпоинт сайта, проверен 29.09.2026). Пустое множество при сбое — останется _EXCL."""
    try:
        req = urllib.request.Request(_PRODUCTS, headers={"User-Agent": "feat/0.1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = json.loads(r.read().decode()).get("data") or []
    except Exception:
        return set()
    return {r["b"] for r in rows if r.get("b") and _NON_ALT_TAGS & set(r.get("tags") or [])}


def top_symbols(n: int) -> list[str]:
    rows = get("/api/v3/ticker/24hr", {}) or []
    excl = _EXCL | _tagged_non_alts()
    c = []
    for r in rows:
        s = r.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        b = s[:-4]
        if b in excl or any(b.endswith(x) for x in ("UP", "DOWN", "BULL", "BEAR")):
            continue
        try:
            c.append((float(r.get("quoteVolume", 0)), s))
        except (ValueError, TypeError):
            continue
    c.sort(reverse=True)
    return [s for _, s in c[:n]]


def full_history(symbol: str):
    ts, cl, vol = [], [], []
    start = 1483228800000
    while True:
        d = get("/api/v3/klines", {"symbol": symbol, "interval": "1d",
                                   "startTime": start, "limit": 1000})
        if not isinstance(d, list) or not d:
            break
        for k in d:
            try:
                ts.append(int(k[0]) // 1000); cl.append(float(k[4])); vol.append(float(k[7]))
            except (ValueError, IndexError):
                return [], [], []
        if len(d) < 1000:
            break
        start = int(d[-1][0]) + 86400000
        time.sleep(0.04)
    return ts, cl, vol


def sma(xs, i, n):
    w = xs[max(0, i - n + 1):i + 1]
    return statistics.fmean(w) if w else xs[i]


def features(cl, vol, t) -> dict | None:
    """Признаки входа на день t (данные <= t)."""
    if t < 200:
        return None
    c = cl[t]
    ath = max(cl[:t + 1])
    w = cl[t - 179:t + 1]
    v = vol[t - 179:t + 1]
    lo, hi = min(w), max(w)
    rets = [w[i] / w[i - 1] - 1 for i in range(1, len(w)) if w[i - 1] > 0]
    if len(rets) < 60:
        return None
    bv = statistics.pstdev(rets[:-30])
    contr = statistics.pstdev(rets[-30:]) / bv if bv > 0 else None
    lo_i = min(range(len(w)), key=lambda i: w[i])
    vv = [x for x in v if x > 0]
    vtrend = None
    if len(vv) > 40:
        bvol = statistics.fmean(vv[:-30])
        vtrend = statistics.fmean(vv[-30:]) / bvol if bvol > 0 else None
    base_len = sum(1 for p in w if lo > 0 and p <= lo * 1.15)
    return {
        "dd_ath": 1 - c / ath if ath > 0 else 0.0,
        "dd_local": 1 - c / hi if hi > 0 else 0.0,
        "range_pos": (c - lo) / (hi - lo) if hi > lo else 0.5,
        "contr": contr,
        "vtrend": vtrend,
        "days_since_low": (len(w) - 1) - lo_i,
        "base_len": base_len,
        "trend30": c / cl[t - 30] - 1 if cl[t - 30] > 0 else 0.0,
        "trend90": c / cl[t - 90] - 1 if cl[t - 90] > 0 else 0.0,
        "above_sma50": c / sma(cl, t, 50) - 1 if sma(cl, t, 50) > 0 else 0.0,
        "age_days": t,
    }


def is_spring(f: dict) -> bool:
    return (f["dd_ath"] >= 0.70 and f["range_pos"] <= 0.5
            and f["contr"] is not None and f["contr"] <= 0.75
            and f["trend30"] > -0.15)


def outcomes(cl, e: int) -> dict:
    entry = cl[e]
    base_low = min(cl[max(0, e - 30):e + 1]) * 0.95
    end = min(e + HORIZON, len(cl) - 1)
    mx = max(cl[e + 1:end + 1]) if end > e else entry
    hit100 = any(cl[t] >= entry * 2 for t in range(e + 1, end + 1))
    # инвалидация раньше +30%
    inval_first = False
    for t in range(e + 1, end + 1):
        if cl[t] < base_low:
            inval_first = True; break
        if cl[t] >= entry * 1.30:
            break
    return {"max_gain": mx / entry - 1, "hit100": hit100,
            "inval_first": inval_first, "trunc": (end - e) < HORIZON}


def sim_exit(cl, e, ladder, trail, arm, inval_buf=0.25, confirm=2):
    entry = cl[e]
    base_low = min(cl[max(0, e - 30):e + 1]) * (1 - inval_buf) if inval_buf else 0.0
    end = min(e + HORIZON, len(cl) - 1)
    rem, proc, hwm, li, below = 1.0, 0.0, entry, 0, 0
    for t in range(e + 1, end + 1):
        c = cl[t]; hwm = max(hwm, c)
        below = below + 1 if (base_low > 0 and c < base_low) else 0
        if base_low > 0 and below >= confirm:
            proc += rem * c; rem = 0.0; break
        while li < len(ladder) and c >= entry * (1 + ladder[li][0]) and rem > 1e-9:
            proc += min(ladder[li][1], rem) * c; rem -= min(ladder[li][1], rem); li += 1
        if rem <= 1e-9:
            break
        if trail and (hwm / entry - 1) >= arm and c <= hwm * (1 - trail):
            proc += rem * c; rem = 0.0; break
    if rem > 1e-9:
        proc += rem * cl[end]
    return proc * (1 - FEE) / (entry * (1 + FEE)) - 1


def find_springs(cl, vol):
    out, t, n = [], 200, len(cl)
    while t < n - 30:
        f = features(cl, vol, t)
        if f and is_spring(f):
            out.append((t, f)); t += COOLDOWN
        else:
            t += 1
    return out


def confirm_day(cl, e):
    """Первый день реклейма SMA20 или отскока +15% в окне подтверждения."""
    end = min(e + CONFIRM_WINDOW, len(cl) - 1)
    for t in range(e + 1, end + 1):
        if cl[t] >= cl[e] * 1.15 or cl[t] > sma(cl, t, 20):
            return t
    return None


def bucket_report(episodes, feat, edges, label):
    """P(+100) и mean hodl-net по бакетам признака."""
    def hodl(ep):
        return sim_exit(ep["cl"], ep["e"], [(1.0, 0.25), (3.0, 0.25)], 0.30, 0.60)
    print(f"\n{label}:")
    print(f"  {'бакет':>14} {'n':>4} {'P(+100)':>8} {'mean net':>9} {'inval1st':>9}")
    lo = -1e9
    names = []
    for hi in edges + [1e9]:
        b = [ep for ep in episodes if lo < ep["f"].get(feat, -1e9) <= hi
             and ep["f"].get(feat) is not None]
        rng = (f"≤{hi:g}" if lo < -1e8 else (f">{lo:g}" if hi > 1e8 else f"{lo:g}..{hi:g}"))
        if len(b) >= 15:
            p100 = sum(1 for ep in b if ep["o"]["hit100"]) / len(b) * 100
            net = statistics.fmean(hodl(ep) for ep in b) * 100
            iv = sum(1 for ep in b if ep["o"]["inval_first"]) / len(b) * 100
            print(f"  {rng:>14} {len(b):>4} {p100:>7.0f}% {net:>+8.1f}% {iv:>8.0f}%")
        lo = hi
        names.append(rng)


def main():
    t0 = time.time()
    # BTC режим/стресс
    bts, bcl, _ = full_history("BTCUSDT")
    btc = {bts[i]: {"reg": bcl[i] > bcl[i - 30] and bcl[i] > sma(bcl, i, 50),
                    "dd": 1 - bcl[i] / max(bcl[:i + 1])} for i in range(50, len(bts))}
    syms = top_symbols(TOP_N)
    print(f"symbols {len(syms)}; BTC {len(btc)} дней")

    episodes = []
    for i, s in enumerate(syms):
        if s == "BTCUSDT":
            continue
        ts, cl, vol = full_history(s)
        if len(cl) < 300:
            continue
        for e, f in find_springs(cl, vol):
            bt = btc.get(ts[e], {})
            f = dict(f)
            f["btc_bull"] = 1 if bt.get("reg") else 0
            f["btc_dd"] = bt.get("dd", 0.0)
            f["rs_btc30"] = f["trend30"] - (
                bcl[bts.index(ts[e])] / bcl[bts.index(ts[e]) - 30] - 1
                if ts[e] in bts and bts.index(ts[e]) >= 30 else 0.0)
            episodes.append({"sym": s, "e": e, "cl": cl, "f": f, "o": outcomes(cl, e)})
        if (i + 1) % 40 == 0:
            print(f"  ...{i+1}/{len(syms)} эп={len(episodes)} {time.time()-t0:.0f}s")

    N = len(episodes)
    base_p100 = sum(1 for ep in episodes if ep["o"]["hit100"]) / N * 100
    print(f"\n=== {N} пружин на {len({ep['sym'] for ep in episodes})} монетах. "
          f"База P(+100%)={base_p100:.0f}% (survivor-upper-bound) ===")

    # A. Univariate по признакам
    print("\n########## A. ЧТО РАЗДЕЛЯЕТ ПОБЕДИТЕЛЕЙ (univariate) ##########")
    bucket_report(episodes, "dd_ath", [0.80, 0.88, 0.94], "Просадка от ATH")
    bucket_report(episodes, "dd_local", [0.15, 0.30, 0.45], "Локальная просадка (180д)")
    bucket_report(episodes, "range_pos", [0.15, 0.30], "Позиция в диапазоне")
    bucket_report(episodes, "contr", [0.4, 0.55, 0.68], "Сжатие волатильности")
    bucket_report(episodes, "vtrend", [0.5, 0.8, 1.2], "Объёмный тренд (dry-up<1)")
    bucket_report(episodes, "days_since_low", [10, 30, 60], "Дней с обновления лоу")
    bucket_report(episodes, "base_len", [20, 50, 100], "Длина базы у дна")
    bucket_report(episodes, "trend30", [-0.05, 0.05, 0.20], "Тренд 30д")
    bucket_report(episodes, "trend90", [-0.20, 0.0, 0.25], "Тренд 90д")
    bucket_report(episodes, "above_sma50", [-0.10, 0.0, 0.15], "Цена vs SMA50")
    bucket_report(episodes, "rs_btc30", [-0.10, 0.0, 0.15], "Относительная сила к BTC 30д")
    bucket_report(episodes, "btc_dd", [0.10, 0.30, 0.55], "Просадка BTC (стресс рынка)")
    bucket_report(episodes, "age_days", [400, 800, 1500], "Возраст монеты (дней истории)")

    # B. Вход по подтверждению
    print("\n########## B. ВХОД: ПРУЖИНА vs ПОДТВЕРЖДЕНИЕ ##########")
    conf_eps, base_net, conf_net = [], [], []
    n_conf = 0
    for ep in episodes:
        cl, e = ep["cl"], ep["e"]
        base_net.append(sim_exit(cl, e, [(1.0, 0.25), (3.0, 0.25)], 0.30, 0.60))
        cd = confirm_day(cl, e)
        if cd is not None and cd < len(cl) - 30:
            n_conf += 1
            conf_net.append(sim_exit(cl, cd, [(1.0, 0.25), (3.0, 0.25)], 0.30, 0.60))
            o2 = outcomes(cl, cd)
            conf_eps.append(o2["hit100"])
    bp = base_p100
    cp = sum(conf_eps) / len(conf_eps) * 100 if conf_eps else 0
    print(f"  вход сразу:        n={N:>4}  P(+100)={bp:.0f}%  mean net {statistics.fmean(base_net)*100:+.1f}%")
    print(f"  вход по подтвержд: n={n_conf:>4}  P(+100)={cp:.0f}%  mean net "
          f"{statistics.fmean(conf_net)*100:+.1f}%  (подтвердилось {n_conf/N*100:.0f}%)")

    # C. Структура выхода на раннерах (дошли до +50%)
    print("\n########## C. СТРУКТУРА ВЫХОДА (раннеры: max≥+50%) ##########")
    runners = [ep for ep in episodes if ep["o"]["max_gain"] >= 0.50]
    print(f"  раннеров: {len(runners)} из {N} ({len(runners)/N*100:.0f}%)")
    variants = {
        "текущая (L100/300+tr30/arm60)": ([(1.0, 0.25), (3.0, 0.25)], 0.30, 0.60),
        "трейл 30 (arm60), без лестницы": ([], 0.30, 0.60),
        "трейл 40 (arm60)":              ([], 0.40, 0.60),
        "трейл 50 (arm40)":              ([], 0.50, 0.40),
        "L50/150 по 1/3 + tr30":         ([(0.5, 0.33), (1.5, 0.33)], 0.30, 0.60),
        "L100/300 + tr40/arm80":         ([(1.0, 0.25), (3.0, 0.25)], 0.40, 0.80),
        "без выхода (hold 730д)":        ([], 0.0, 0.0),
    }
    print(f"  {'вариант':32} {'med':>8} {'mean':>8} {'win':>5}")
    for name, (lad, tr, arm) in variants.items():
        nets = [sim_exit(ep["cl"], ep["e"], lad, tr, arm) for ep in runners]
        win = sum(1 for x in nets if x > 0) / len(nets) * 100
        print(f"  {name:32} {statistics.median(nets)*100:>+7.1f}% "
              f"{statistics.fmean(nets)*100:>+7.1f}% {win:>4.0f}%")

    print(f"\n(elapsed {time.time()-t0:.0f}s; survivor-upper-bound, одно окно данных)")


if __name__ == "__main__":
    main()
