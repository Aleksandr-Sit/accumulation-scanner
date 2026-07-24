"""Stage 4: доступность на Bybit spot — прокси доступа для граждан РФ.

Bybit работает по паспорту РФ (спот/перпы/P2P RUB) — см. STRATEGY_RESEARCH.md.
/v5/market/instruments-info?category=spot возвращает все спот-пары; строим множество
baseCoin. Наличие токена там = есть CEX-ликвидность и фиатный вход для РФ.
Отсутствие ≠ недоступен: on-chain токен всё равно берётся на DEX своим кошельком.

Актуальность ToS Bybit для РФ проверять на дату запуска (может меняться).
"""
from __future__ import annotations

from ..http import HttpClient

_BASE = "https://api.bybit.com"


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
