"""Stage 4 — детектор зоны (дно/пружина vs пик/распределение) и гейт доступа РФ.

Реализует SCANNER_SPEC §2–3. Зона — это СОВПАДЕНИЕ сигналов, не одна метрика.
Считается из бесплатной истории цены CoinGecko. Чистые функции — тестируются офлайн.

НЕ покрыто на free (помечается «нет данных»): on-chain приток smart money, MVRV,
реализованная цена, соц-объём — это Stage 4b (Nansen/Glassnode/Santiment/Dune).
"""
from __future__ import annotations

import statistics

from ..config import Config


def compute_indicators(prices: list[float], recent_days: int, sma_days: int,
                       volumes: list[float | None] | None = None) -> dict | None:
    """Из ряда дневных цен (oldest→newest) считает индикаторы сжатия/тренда/позиции.

    volumes (опц.) — total_volumes из того же ответа market_chart (0 доп. вызовов):
    vol_trend = ср.объём recent / ср.объём базы. <0.6 = усыхание (продавцы кончились,
    признак накопления), >1.6 = расширение (разгон/раздача — сигнал для выхода).
    """
    n = len(prices)
    if n < 15:
        return None
    returns = [prices[i] / prices[i - 1] - 1 for i in range(1, n) if prices[i - 1] > 0]
    if len(returns) < 10:
        return None

    # База — только ДО recent-окна: иначе знаменатель загрязнён числителем и
    # сжатие систематически недооценивается (реальная пружина 0.55 выглядит как 0.72).
    base_ret = returns[:-recent_days]
    recent_ret = returns[-recent_days:]
    if len(base_ret) >= 10:
        base_vol = statistics.pstdev(base_ret)
        recent_vol = statistics.pstdev(recent_ret)
        contraction = (recent_vol / base_vol) if base_vol > 0 else None  # <1 = сжатие
    else:
        contraction = None  # истории едва больше recent-окна — база шумовая, честнее «нет данных»

    window = prices[-sma_days:] if n >= sma_days else prices
    sma = statistics.fmean(window)
    last = prices[-1]
    pct_above_sma = (last / sma - 1) * 100 if sma > 0 else 0.0

    lo, hi = min(prices), max(prices)
    range_pos = (last - lo) / (hi - lo) if hi > lo else 0.5      # 0=низ диапазона, 1=верх

    lb = prices[-recent_days] if n > recent_days else prices[0]
    trend_recent_pct = (last / lb - 1) * 100 if lb > 0 else 0.0  # тренд за recent_days

    # Стабилизация лоу: сколько дней минимум не обновлялся + длина базы у дна.
    # Настоящее дно перестаёт делать lower lows; медленное истечение — нет.
    lo_i = min(range(n), key=lambda i: prices[i])
    days_since_low = (n - 1) - lo_i
    base_len_days = sum(1 for p in prices if lo > 0 and p <= lo * 1.15)

    # Локальная просадка (от максимума окна) — согласована с range_pos по таймфрейму,
    # в отличие от dd от all-time ATH (который может быть пузырём прошлого цикла).
    dd_local_pct = (1 - last / hi) * 100 if hi > 0 else 0.0

    # Объёмный тренд (если volumes переданы).
    vol_trend = None
    if volumes:
        vv = [v for v in volumes if isinstance(v, (int, float)) and v > 0]
        if len(vv) > recent_days + 10:
            base_v = statistics.fmean(vv[:-recent_days])
            recent_v = statistics.fmean(vv[-recent_days:])
            vol_trend = recent_v / base_v if base_v > 0 else None

    return {
        "vol_contraction": round(contraction, 3) if contraction is not None else None,
        "pct_above_sma": round(pct_above_sma, 1),
        "range_pos": round(range_pos, 3),
        "trend_recent_pct": round(trend_recent_pct, 1),
        "days_since_low": days_since_low,
        "base_len_days": base_len_days,
        "dd_local_pct": round(dd_local_pct, 1),
        "vol_trend": round(vol_trend, 3) if vol_trend is not None else None,
        "n_days": n,
    }


def classify_zone(ind: dict | None, drawdown_pct: float | None,
                  cfg: Config) -> tuple[str, list[str]]:
    """Возвращает (зона, сигналы). Зона — по совпадению нескольких условий."""
    z = cfg["stage4_zone"]
    if ind is None:
        return "?", ["[нет данных] недостаточно истории цены"]

    sig: list[str] = []
    contr = ind["vol_contraction"]
    trend = ind["trend_recent_pct"]
    rangep = ind["range_pos"]
    above = ind["pct_above_sma"]
    dd = drawdown_pct if isinstance(drawdown_pct, (int, float)) else None

    squeezed = contr is not None and contr <= z["vol_contraction_ratio"]
    downtrend = trend <= -z["downtrend_drop_pct"]
    deep_dd = dd is not None and dd >= z["spring_min_drawdown_pct"]
    near_ath = dd is not None and dd <= z["peak_max_drawdown_pct"]

    # 1) Падающий нож — активная просадка прямо сейчас (важнее «дешевизны»).
    if downtrend:
        sig.append(f"тренд {trend:+.0f}% за период — активное падение")
        return "ПАДАЮЩИЙ_НОЖ", sig

    # 2) Пружина/дно — глубокая просадка + сжатие волатильности + низ диапазона + не падает
    #    + анти-зомби: лоу не обновлялся минимум N дней (медленное истечение — не дно).
    dsl = ind.get("days_since_low")
    stable_low = dsl is None or dsl >= z.get("spring_min_days_since_low", 14)
    if deep_dd and squeezed and rangep <= z["spring_max_range_pos"] and stable_low:
        sig.append(f"просадка от ATH {dd:.0f}% — не восстановился")
        sig.append(f"сжатие волатильности {contr:.2f} (< {z['vol_contraction_ratio']})")
        sig.append(f"низ диапазона (range_pos={rangep:.2f}), тренд {trend:+.0f}% (стабилизация)")
        if dsl is not None:
            sig.append(f"лоу не обновлялся {dsl}д (база {ind.get('base_len_days', '?')}д)")
        vt = ind.get("vol_trend")
        if vt is not None:
            if vt <= z.get("vol_dryup_ratio", 0.6):
                sig.append(f"объём усох ×{vt:.2f} к базе — продавцы выдыхаются (накопление)")
            elif vt >= 1.2:
                sig.append(f"⚠ объём растёт у дна (×{vt:.2f}) — проверить: капитуляция или раздача")
        else:
            sig.append("[нет данных] объём — vol_trend недоступен")
        return "ПРУЖИНА/ДНО", sig

    # 3) Пик/распределение — близко к ATH + цена высоко над SMA + верх диапазона.
    if near_ath and above >= z["peak_min_above_sma_pct"] and rangep >= z["peak_min_range_pos"]:
        sig.append(f"близко к ATH (просадка {dd:.0f}%)")
        sig.append(f"цена +{above:.0f}% над SMA, верх диапазона (range_pos={rangep:.2f})")
        return "ПИК", sig

    # 4) Иначе — середина. Отметим частичные сигналы.
    if deep_dd and squeezed and rangep <= z["spring_max_range_pos"] and not stable_low:
        sig.append(f"похоже на дно, но лоу обновлён {dsl}д назад — медленное истечение, не стабилизация")
    elif deep_dd:
        sig.append(f"глубокая просадка {dd:.0f}%, но нет сжатия/стабилизации — не пружина")
    if squeezed:
        sig.append(f"есть сжатие {contr:.2f}, но не в зоне дна")
    sig.append("[нет данных] on-chain накопление / MVRV / соц-объём — Stage 4b (платно)")
    return "СЕРЕДИНА", sig
