"""Stage 4 — гейт доступа для граждан РФ. Чистая функция, тестируется офлайн.

Логика: Bybit spot (по паспорту РФ) = лучший венью (CEX-ликвидность + фиат RUB).
Иначе — on-chain токен берётся на DEX своим кошельком (без KYC). Полный отказ (FAIL)
только если нет ни CEX-листинга, ни торгуемого on-chain адреса.
"""
from __future__ import annotations


def rf_gate(symbol: str, chain: str, address: str, bybit_spot: set[str]) -> tuple[bool, str]:
    """Возвращает (доступен, венью)."""
    if symbol and symbol.upper() in bybit_spot:
        return True, "Bybit spot"
    if chain and address:
        return True, "DEX only"
    return False, "нет"
