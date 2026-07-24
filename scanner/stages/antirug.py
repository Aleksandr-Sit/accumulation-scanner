"""Stage 2 — интерпретация анти-рага (GoPlus/honeypot). Чистые функции evaluate_*.

Разделение fetch/evaluate: сеть — в sources/goplus.py, решение — здесь (тестируется
на фикстурах офлайн). Возвращает (passed, reasons, flags).
"""
from __future__ import annotations

from ..config import Config

# GoPlus кодирует булевы поля строками "0"/"1".


def _is_true(v) -> bool:
    return str(v) == "1"


def _to_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _top10_pct(sec: dict) -> float | None:
    holders = sec.get("holders")
    if not isinstance(holders, list):
        return None
    total = 0.0
    for h in holders[:10]:
        total += _to_float(h.get("percent"))
    return total * 100.0 if total <= 1.0 else total  # percent бывает долей или %


_BURN_ADDR = {"0x000000000000000000000000000000000000dead",
              "0x0000000000000000000000000000000000000000"}


def lp_locked_pct(sec: dict) -> float | None:
    """% LP заблокирован или сожжён. None = нет данных о LP (не пенализируем).

    Разблокированный LP — раг в один клик: создатель выдёргивает ликвидность.
    Считаем locked = is_locked ИЛИ адрес/тег «burn».
    """
    lps = sec.get("lp_holders")
    if not isinstance(lps, list) or not lps:
        return None
    locked = 0.0
    for h in lps:
        tag = (h.get("tag") or "").lower()
        addr = (h.get("address") or "").lower()
        is_locked = str(h.get("is_locked")) == "1"
        burned = addr in _BURN_ADDR or "burn" in tag
        if is_locked or burned:
            locked += _to_float(h.get("percent"))
    return round(locked * 100.0 if locked <= 1.0 else locked, 1)


def holder_count(sec: dict) -> int | None:
    v = sec.get("holder_count")
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def evaluate_goplus_evm(sec: dict, cfg: Config) -> tuple[bool, list[str], list[str]]:
    """EVM token_security -> (passed, reject_reasons, flags)."""
    a = cfg["stage2_antirug"]
    reasons: list[str] = []
    flags: list[str] = []

    if _is_true(sec.get("is_honeypot")):
        reasons.append("honeypot")
    if _is_true(sec.get("cannot_sell_all")):
        reasons.append("cannot_sell_all")
    if _is_true(sec.get("transfer_pausable")):
        flags.append("transfer_pausable")
    if _is_true(sec.get("owner_change_balance")):
        reasons.append("owner_can_change_balance")
    if _is_true(sec.get("hidden_owner")):
        reasons.append("hidden_owner")
    if _is_true(sec.get("selfdestruct")):
        reasons.append("selfdestruct")
    if _is_true(sec.get("is_blacklisted")):
        flags.append("has_blacklist")

    buy_tax = _to_float(sec.get("buy_tax"))
    sell_tax = _to_float(sec.get("sell_tax"))
    if buy_tax > a["max_buy_tax"]:
        reasons.append(f"buy_tax={buy_tax:.2f}")
    if sell_tax > a["max_sell_tax"]:
        reasons.append(f"sell_tax={sell_tax:.2f}")

    if a.get("require_open_source", True) and sec.get("is_open_source") == "0":
        reasons.append("not_open_source")

    if _is_true(sec.get("is_mintable")):
        (reasons if a.get("fail_on_mintable") else flags).append("mintable")
    if _is_true(sec.get("is_proxy")):
        flags.append("proxy")

    top10 = _top10_pct(sec)
    if top10 is not None and top10 > a["max_top10_holders_pct"]:
        reasons.append(f"top10_holders={top10:.0f}%")

    # LP-lock: разблокированный пул ликвидности — раг в один клик (флаг, не хард-стоп:
    # данные о LP есть не всегда, качество разное).
    lp = lp_locked_pct(sec)
    if lp is not None and lp < a.get("min_lp_locked_pct", 50):
        flags.append("lp_unlocked")

    return (len(reasons) == 0, reasons, flags)


def evaluate_honeypot_is(hp: dict) -> tuple[bool, list[str]]:
    """honeypot.is /IsHoneypot -> (passed, reasons). Вторичное подтверждение (EVM)."""
    reasons: list[str] = []
    result = hp.get("honeypotResult", {})
    if result.get("isHoneypot") is True:
        reasons.append("honeypot.is:honeypot")
    return (len(reasons) == 0, reasons)


def evaluate_solana(sec: dict, cfg: Config) -> tuple[bool, list[str], list[str]]:
    """Solana token_security — набор полей уже, помечаем ограниченность данных."""
    reasons: list[str] = []
    flags: list[str] = ["solana:limited_antirug"]

    if _is_true(sec.get("mintable", {}).get("status") if isinstance(sec.get("mintable"), dict) else sec.get("mintable")):
        flags.append("mint_authority_active")
    if _is_true(sec.get("freezable", {}).get("status") if isinstance(sec.get("freezable"), dict) else sec.get("freezable")):
        reasons.append("freeze_authority_active")

    return (len(reasons) == 0, reasons, flags)
