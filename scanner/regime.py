"""Режим рынка BTC — контекст для интерпретации пружин. 1 вызов API на прогон.

Эмпирика (backtest/ladder_study.py, 215 эпизодов 2023–2026): входы «у дна» при
МЕДВЕЖЬЕМ BTC исторически лучше, чем при бычьем — монета на −70%, когда весь
рынок растёт, чаще относительная слабость (зомби), чем недооценка. Поэтому режим
НЕ блокирует вход, а размечает контекст:
  BULL + пружина -> флаг «проверить силу» (нужно доп. подтверждение: RS, объём);
  BEAR + пружина -> нормальный mean-reversion кандидат.
Чистые функции — тестируются офлайн.
"""
from __future__ import annotations

import statistics


def classify_regime(btc_prices: list[float], recent_days: int = 30,
                    sma_days: int = 50) -> dict:
    """{regime, trend_recent_pct, above_sma, drawdown} из дневного ряда BTC.

    drawdown (0..1) — просадка BTC от его ATH в окне: СИЛЬНЕЙШИЙ предиктор качества
    альт-пружины (feature_study: BTC у ATH → P(+100)=22%, BTC −55%+ → 74%).
    """
    n = len(btc_prices)
    if n < sma_days + 5:
        return {"regime": "?", "trend_recent_pct": None, "above_sma": None, "drawdown": None}
    last = btc_prices[-1]
    lb = btc_prices[-recent_days] if n > recent_days else btc_prices[0]
    trend = (last / lb - 1) * 100 if lb > 0 else 0.0
    sma = statistics.fmean(btc_prices[-sma_days:])
    above = last > sma
    ath = max(btc_prices)
    drawdown = round(1 - last / ath, 3) if ath > 0 else None
    regime = "BULL" if (trend > 0 and above) else "BEAR"
    return {"regime": regime, "trend_recent_pct": round(trend, 1),
            "above_sma": above, "drawdown": drawdown}


def rs_vs_btc(coin_trend_pct: float | None, btc_trend_pct: float | None) -> float | None:
    """Относительная сила: тренд монеты минус тренд BTC за тот же период, п.п."""
    if not isinstance(coin_trend_pct, (int, float)) or not isinstance(btc_trend_pct, (int, float)):
        return None
    return round(coin_trend_pct - btc_trend_pct, 1)


# ---------------------------------------------------------------------------
# Контекст рынка альтов (backtest/market_regime_study.py, 2017–2026).
#
# Устойчиво в обоих циклах с данными (2020-22, 2023-25) и на горизонтах 365/730д:
#   • просадка альт-рынка БЕЗ стейблкоинов (total − BTC − стейблы) от своего ATH —
#     лучше просадки BTC и даёт информацию сверх неё;
#   • «индекс перегрева» на входе: доля горящих флагов из 8 (холодный рынок —
#     пружины заметно лучше).
# Знак меняется между циклами (в скор НЕ берём, только показываем): уровень BTC.D,
# рост стейблов / SSR, Fear & Greed сам по себе, ETH/BTC.
# Все функции чистые: на вход — ряды из таблицы market_daily, тестируются офлайн.
# ---------------------------------------------------------------------------

DAY = 86400

# Подписи флагов для алертов.
FLAG_LABELS = {
    "alt_vs_sma200": "альты над SMA200",
    "mvrv_btc": "MVRV BTC",
    "mvrv_eth": "MVRV ETH",
    "fng30": "F&G",
    "breadth200": "ширина рынка",
    "fund30": "фандинг",
    "oi_rel365": "плечо (OI)",
    "altbtc_chg90": "альты vs BTC",
}


def market_series(rows: list[dict]) -> dict[str, dict[int, float]]:
    """Строки market_daily -> базовые ряды {имя: {день: значение}}.

    Производные: btc_m = total·BTC.D, alt_ex = total − btc_m − стейблы (альт-рынок
    без стейблкоинов: со стейблами TOTAL2 рисует «новые хаи» за счёт роста USDT/USDC),
    altbtc = alt_ex / btc_m.
    """
    S: dict[str, dict[int, float]] = {}

    def put(k: str, d: int, v) -> None:
        if isinstance(v, (int, float)) and v == v:      # v == v отсекает NaN
            S.setdefault(k, {})[d] = float(v)

    for r in rows:
        d = int(r["day"])
        total, btc_d, stables = r.get("total_mcap"), r.get("btc_dominance"), r.get("stables_usd")
        for k in ("fng", "mvrv_btc", "mvrv_eth", "funding_btc", "oi_btc", "breadth200"):
            put(k, d, r.get(k))
        put("btc_d", d, btc_d)
        put("stables", d, stables)
        put("total", d, total)
        if isinstance(total, (int, float)) and isinstance(btc_d, (int, float)) and total > 0:
            btc_m = total * btc_d / 100.0
            put("btc_m", d, btc_m)
            if isinstance(stables, (int, float)):
                alt = total - btc_m - stables
                if alt > 0:
                    put("alt_ex", d, alt)
                    if btc_m > 0:
                        put("altbtc", d, alt / btc_m)
    return S


def _latest(s: dict[int, float], d: int, max_lag_days: int = 3) -> float | None:
    """Значение на день d или последнее в пределах max_lag_days (CoinMetrics отстаёт на день)."""
    for i in range(max_lag_days + 1):
        v = s.get(d - i * DAY)
        if v is not None:
            return v
    return None


def _window(s: dict[int, float], d: int, n: int) -> list[float]:
    return [s[d - i * DAY] for i in range(n) if (d - i * DAY) in s]


def market_feature_table(S: dict[str, dict[int, float]],
                         days: list[int] | None = None,
                         alt_dd_min_days: int = 0) -> dict[int, dict]:
    """Признаки рынка на каждый день (только данные <= дня, walk-forward).

    alt_dd_min_days — alt_dd только при истории альт-рынка не короче (прод: 365; бэктесты
    идут с 2017 и передают 0).

    alt_dd        — просадка альт-рынка без стейблов от его ATH (0..1)
    alt_vs_sma200 — альт-рынок к своей SMA200 − 1
    altbtc_chg90  — изменение отношения альты/BTC за 90д (альтсезон > 0)
    fng30, fund30 — средние за 30д (F&G; фандинг BTC в %/8ч)
    oi_rel365     — OI BTC к среднему за год (плечо рынка)
    mvrv_btc/eth, breadth200 — точечные значения
    info: btc_d, stables_chg90, fng, alt_ex, total
    """
    alt = S.get("alt_ex", {})
    all_days = sorted(days if days is not None else
                      set().union(*[set(v) for v in S.values()]) if S else [])
    out: dict[int, dict] = {}
    run_max = 0.0
    alt_days = sorted(alt)
    j = 0
    for d in all_days:
        while j < len(alt_days) and alt_days[j] <= d:   # ATH альт-рынка до дня d
            run_max = max(run_max, alt[alt_days[j]])
            j += 1
        f: dict = {}
        a = alt.get(d)
        if a is not None and run_max > 0:
            f["alt_ex"] = a
            if j >= alt_dd_min_days:          # j — дней истории альт-рынка до d включительно
                f["alt_dd"] = 1 - a / run_max
            w = _window(alt, d, 200)
            if len(w) >= 190:
                f["alt_vs_sma200"] = a / (sum(w) / len(w)) - 1
        ab, ab0 = S.get("altbtc", {}).get(d), S.get("altbtc", {}).get(d - 90 * DAY)
        if ab and ab0:
            f["altbtc_chg90"] = ab / ab0 - 1
        w = _window(S.get("fng", {}), d, 30)
        if len(w) >= 25:
            f["fng30"] = sum(w) / len(w)
        w = _window(S.get("funding_btc", {}), d, 30)
        if len(w) >= 25:
            f["fund30"] = sum(w) / len(w) * 100          # доля/8ч -> %/8ч
        oi = S.get("oi_btc", {})
        w = _window(oi, d, 365)
        if oi.get(d) and len(w) >= 300:
            f["oi_rel365"] = oi[d] / (sum(w) / len(w))
        for k in ("mvrv_btc", "mvrv_eth", "breadth200", "fng", "btc_d", "total"):
            v = _latest(S.get(k, {}), d)
            if v is not None:
                f[k] = v
        st, st0 = S.get("stables", {}).get(d), S.get("stables", {}).get(d - 90 * DAY)
        if st and st0:
            f["stables_chg90"] = st / st0 - 1
        if f:
            out[d] = f
    return out


def _flag_on(op: str, value: float, thr: float) -> bool:
    return value >= thr if op == ">=" else value <= thr


def hot_flags(features: dict, cfg) -> dict:
    """Индекс перегрева: {score, n_lit, avail, lit, near}.

    score = доля горящих флагов среди ДОСТУПНЫХ (None, если доступно < hot_min_available).
    near  — флаги, подошедшие к порогу (3-й элемент в config) — «рынок теплеет».
    Пороги — in-sample по вершинам 2018–2025 [оценка], см. config market_regime.
    """
    m = cfg.get("market_regime", {}) or {}
    flags = m.get("hot_flags", {}) or {}
    lit, near, avail = [], [], 0
    for name, spec in flags.items():
        v = features.get(name)
        if not isinstance(v, (int, float)):
            continue
        avail += 1
        op, thr = spec[0], spec[1]
        if _flag_on(op, v, thr):
            lit.append(name)
        elif len(spec) > 2 and _flag_on(op, v, spec[2]):
            near.append(name)
    min_avail = m.get("hot_min_available", 4)
    score = round(len(lit) / avail, 3) if avail >= min_avail else None
    return {"score": score, "n_lit": len(lit), "avail": avail, "lit": lit, "near": near}


def market_context(rows: list[dict], cfg) -> dict:
    """Контекст рынка на последний день с данными по альт-рынку. {} если данных нет."""
    S = market_series(rows)
    alt = S.get("alt_ex", {})
    if not alt:
        return {}
    last = max(alt)
    f = market_feature_table(S, [last], cfg.get("market_regime.alt_dd_min_days", 365)
                             ).get(last, {})
    return {"day": last, **f, "hot": hot_flags(f, cfg)}


def fresh_context(ctx: dict, cfg, now: float) -> dict:
    """ctx, если последний день данных не старше market_regime.max_age_days, иначе {}:
    устаревший контекст (источник молчит неделю) не должен выдаваться за сегодняшний."""
    day = (ctx or {}).get("day")
    if not isinstance(day, (int, float)):
        return {}
    age = (int(now) // DAY * DAY - day) / DAY
    if age > cfg.get("market_regime.max_age_days", 3):
        print(f"[market] данные рынка за {age:.0f} дн. назад — контекст не используется")
        return {}
    return ctx


def context_line(ctx: dict, btc_dd: float | None = None) -> str:
    """«альты −42% · BTC −33% · перегрев 0/8 (близко: F&G, ширина рынка) · F&G 45 · BTC.D 58%».

    ctx — результат market_context (может быть {}); btc_dd — просадка BTC из режима
    (Bybit-свечи), если в ctx её нет. Пустая строка, если показать нечего.
    """
    parts = []
    add = ctx.get("alt_dd")
    if isinstance(add, (int, float)):
        parts.append(f"альты −{add*100:.0f}%")
    bdd = ctx.get("btc_dd", btc_dd)
    if isinstance(bdd, (int, float)):
        parts.append(f"BTC −{bdd*100:.0f}%")
    hot = ctx.get("hot") or {}
    if hot.get("avail"):
        s = f"перегрев {hot.get('n_lit', 0)}/{hot['avail']}"
        if hot.get("lit"):
            s += ": " + ", ".join(FLAG_LABELS.get(k, k) for k in hot["lit"])
        if hot.get("near"):
            s += " (близко: " + ", ".join(FLAG_LABELS.get(k, k) for k in hot["near"]) + ")"
        parts.append(s)
    fng = ctx.get("fng")
    if isinstance(fng, (int, float)):
        parts.append(f"F&G {fng:.0f}")
    bd = ctx.get("btc_d")
    if isinstance(bd, (int, float)):
        parts.append(f"BTC.D {bd:.0f}%")
    return " · ".join(parts)
