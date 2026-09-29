"""Stage 5 — композитный скор 0–100. Чистые функции, тестируется офлайн.

Сводит блоки воронки в единый ранг по весам (из PROMPT_ACCUMULATION_SYSTEM §3,
адаптировано под доступные на free данные). Ключевой честный момент: недостающие
блоки (on-chain — платно) НЕ штрафуют балл произвольно, а снижают **confidence** —
доверие к баллу. Балл — приоритизация, НЕ сигнал на вход.
"""
from __future__ import annotations

from ..config import Config

# Риск-флаги анти-рага и штраф к под-баллу safety (0–10).
_SAFETY_PENALTY = {
    "mintable": 2.0,
    "transfer_pausable": 2.0,
    "proxy": 1.5,
    "has_blacklist": 1.5,
    "lp_unlocked": 2.5,          # разблокированный LP — раг в один клик
    "wash_suspect": 1.5,         # накрученный объём
    "solana:limited_antirug": 1.0,
}
_ZONE_SCORE = {
    "ПРУЖИНА/ДНО": 9.0,
    "СЕРЕДИНА": 5.0,
    "ПИК": 2.0,
    "ПАДАЮЩИЙ_НОЖ": 1.0,
}


def _clamp(x: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, x))


def _sub_zone(c) -> float | None:
    base = _ZONE_SCORE.get(c.zone)          # "?"/"" -> None (нет данных)
    if base is None:
        return None
    # Пружина взвешивается качеством (feature_study): контекст рынка (BTC-dd),
    # длина базы, полоса объёма, риск обнуления. Множитель ~[0.4,1.25].
    q = getattr(c, "spring_quality", None)
    if c.zone == "ПРУЖИНА/ДНО" and isinstance(q, (int, float)):
        return _clamp(base * q)
    return base


def _sub_valuation(c) -> float | None:
    if c.mc_tvl is None and c.fdv_mc is None:
        return None
    s = 5.0
    if "undervalued_vs_sector" in c.flags:
        s += 3.0
    if "overvalued_vs_sector" in c.flags:
        s -= 3.0
    if c.fdv_mc is not None:
        if c.fdv_mc < 1.5:
            s += 1.0
        elif c.fdv_mc >= 3.0:
            s -= 2.0
    return _clamp(s)


def _sub_safety(c) -> float:
    s = 10.0
    for f in c.flags:
        s -= _SAFETY_PENALTY.get(f, 0.0)
    return _clamp(s)


def _sub_tradability(c) -> float:
    s = 3.0
    if c.rf_venue == "Bybit spot":
        s += 4.0
    elif c.rf_venue == "DEX only":
        s += 1.0
    vol = c.volume_24h or 0
    if vol >= 1e7:
        s += 3.0
    elif vol >= 1e6:
        s += 2.0
    elif vol >= 1e5:
        s += 1.0
    return _clamp(s)


def _sub_liveness(c) -> float | None:
    return c.liveness_score          # 0–10 или None (нет detail-данных)


def _sub_onchain(c) -> float | None:
    return getattr(c, "onchain_score", None)   # 0–10 из Dune или None (не настроен)


_SUBS = {
    "zone": _sub_zone,
    "valuation": _sub_valuation,
    "safety": _sub_safety,
    "liveness": _sub_liveness,
    "tradability": _sub_tradability,
    "onchain": _sub_onchain,
}


def compute_score(c, cfg: Config) -> tuple[float, float, dict]:
    """Возвращает (score 0–100, confidence 0–1, breakdown)."""
    weights: dict[str, float] = cfg["stage5_score"]["weights"]
    total_w = sum(weights.values())
    acc = 0.0
    present_w = 0.0
    breakdown: dict[str, dict] = {}

    for name, w in weights.items():
        v = _SUBS[name](c)
        breakdown[name] = {"sub": v, "weight": w}
        if v is not None:
            acc += v * w
            present_w += w

    score = round(acc / present_w * 10, 1) if present_w > 0 else 0.0
    confidence = round(present_w / total_w, 2) if total_w > 0 else 0.0
    return score, confidence, breakdown
