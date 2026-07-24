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
    """{regime: BULL|BEAR|?, trend_recent_pct, above_sma} из дневного ряда BTC."""
    n = len(btc_prices)
    if n < sma_days + 5:
        return {"regime": "?", "trend_recent_pct": None, "above_sma": None}
    last = btc_prices[-1]
    lb = btc_prices[-recent_days] if n > recent_days else btc_prices[0]
    trend = (last / lb - 1) * 100 if lb > 0 else 0.0
    sma = statistics.fmean(btc_prices[-sma_days:])
    above = last > sma
    regime = "BULL" if (trend > 0 and above) else "BEAR"
    return {"regime": regime, "trend_recent_pct": round(trend, 1), "above_sma": above}


def rs_vs_btc(coin_trend_pct: float | None, btc_trend_pct: float | None) -> float | None:
    """Относительная сила: тренд монеты минус тренд BTC за тот же период, п.п."""
    if not isinstance(coin_trend_pct, (int, float)) or not isinstance(btc_trend_pct, (int, float)):
        return None
    return round(coin_trend_pct - btc_trend_pct, 1)
