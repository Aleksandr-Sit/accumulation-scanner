"""Stage 4: доступность на Bybit spot — прокси доступа для граждан РФ.

Bybit работает по паспорту РФ (спот/перпы/P2P RUB) — см. STRATEGY_RESEARCH.md.
/v5/market/instruments-info?category=spot возвращает все спот-пары; строим множество
baseCoin. Наличие токена там = есть CEX-ликвидность и фиатный вход для РФ.
Отсутствие ≠ недоступен: on-chain токен всё равно берётся на DEX своим кошельком.

Актуальность ToS Bybit для РФ проверять на дату запуска (может меняться).
"""
from __future__ import annotations

import time

from ..http import HttpClient

_BASE = "https://api.bybit.com"
_DAY_MS = 86_400_000


def fetch_spot_basecoins(http: HttpClient) -> set[str]:
    data = http.get_json(f"{_BASE}/v5/market/instruments-info",
                        params={"category": "spot", "limit": 1000})
    out: set[str] = set()
    if isinstance(data, dict):
        for it in (data.get("result", {}) or {}).get("list", []) or []:
            base = (it.get("baseCoin") or "").upper()
            if base:
                out.add(base)
    return out


def fetch_daily_closes(http: HttpClient, symbol: str = "BTCUSDT",
                       limit: int = 1000) -> list[float]:
    """Дневные закрытия (oldest→newest) с Bybit spot. Надёжнее CoinGecko (лимиты
    выше, без 429) — для BTC-drawdown, сильнейшего предиктора качества пружины."""
    data = http.get_json(f"{_BASE}/v5/market/kline",
                         params={"category": "spot", "symbol": symbol,
                                 "interval": "D", "limit": limit})
    return parse_daily_closes(data, int(time.time() * 1000))


def parse_daily_closes(data, now_ms: int) -> list[float]:
    """Ответ /v5/market/kline (D) -> закрытия oldest→newest, только ЗАКРЫТЫЕ свечи.

    Текущая дневная свеча (start + 1д > now) — live-тик, отбрасывается: та же
    дисциплина закрытых свечей, что coingecko.closed_daily. Чистая, офлайн-тест.
    """
    rows = ((data.get("result") or {}).get("list") or []) if isinstance(data, dict) else []
    out: list[float] = []
    for r in reversed(rows):     # Bybit отдаёт newest-first
        try:
            if int(r[0]) + _DAY_MS > now_ms:
                continue
            out.append(float(r[4]))
        except (ValueError, IndexError, TypeError):
            continue
    return out


def fetch_instrument(http: HttpClient, symbol: str) -> dict | None:
    """Правила спот-пары: tickSize, basePrecision, minOrderQty/minOrderAmt, stTag, status."""
    data = http.get_json(f"{_BASE}/v5/market/instruments-info",
                         params={"category": "spot", "symbol": symbol}, use_cache=False)
    rows = (data or {}).get("result", {}).get("list", []) if isinstance(data, dict) else []
    if not rows:
        return None
    it = rows[0]
    lot, pf = it.get("lotSizeFilter") or {}, it.get("priceFilter") or {}

    def _f(v, default=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default
    return {"symbol": it.get("symbol"), "status": it.get("status"),
            "st": it.get("stTag") == "1", "tick": _f(pf.get("tickSize")),
            "qty_step": _f(lot.get("basePrecision")), "min_qty": _f(lot.get("minOrderQty")),
            "min_amt": _f(lot.get("minOrderAmt"), 5.0)}


def fetch_last_price(http: HttpClient, symbol: str) -> float | None:
    """Последняя цена спот-пары (без кэша)."""
    data = http.get_json(f"{_BASE}/v5/market/tickers",
                         params={"category": "spot", "symbol": symbol}, use_cache=False)
    rows = (data or {}).get("result", {}).get("list", []) if isinstance(data, dict) else []
    try:
        return float(rows[0]["lastPrice"]) if rows else None
    except (KeyError, TypeError, ValueError):
        return None
