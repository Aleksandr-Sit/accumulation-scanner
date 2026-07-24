"""Stage 3 — фундамент/оценка. Чистая функция assess_valuation тестируется офлайн.

Free-тариф: TVL/категория/MC-TVL (DeFiLlama) + FDV/MC (CoinGecko, уже есть).
Точные unlocks/инвесторы — pro-only, помечаются «нет данных».
Ничего не выдумываем: каждый вывод тегирован источником.
"""
from __future__ import annotations

import statistics

from ..config import Config


def attach_tvl(candidate, index, min_tvl: float = 0.0) -> None:
    """Дешёвый шаг: матч к DeFiLlama, проставить tvl/category/mc_tvl (in place).

    mc_tvl = market_cap(CoinGecko) / TVL(DeFiLlama). Считается только если TVL выше
    порога min_tvl — иначе копеечный/мёртвый протокол даёт бредовый MC/TVL
    (XPR: TVL $7 → MC/TVL 8e6). Выполняется по всему набору ДО построения медиан.
    """
    proto, match_type = index.find(candidate.coin_id, candidate.symbol)
    if proto:
        candidate.tvl = proto.get("tvl")
        candidate.category = proto.get("category") or ""
        if match_type == "symbol":
            # матч по тикеру — риск однофамильца, на ручную перепроверку
            candidate.flags.append("defillama_symbol_match")
            candidate.manual_review = True
    if (isinstance(candidate.tvl, (int, float)) and candidate.tvl >= max(min_tvl, 1.0)
            and isinstance(candidate.market_cap, (int, float)) and candidate.market_cap > 0):
        candidate.mc_tvl = round(candidate.market_cap / candidate.tvl, 2)
    elif isinstance(candidate.tvl, (int, float)) and 0 < candidate.tvl < min_tvl:
        candidate.flags.append("tvl_below_floor")


def build_category_medians(candidates) -> dict[str, tuple[float, int]]:
    """Медиана MC/TVL по сектору из НАШИХ кандидатов (у кого mc_tvl посчитан)."""
    buckets: dict[str, list[float]] = {}
    for c in candidates:
        if c.category and isinstance(c.mc_tvl, (int, float)):
            buckets.setdefault(c.category, []).append(c.mc_tvl)
    return {cat: (statistics.median(v), len(v)) for cat, v in buckets.items() if v}


def assess_valuation(market_cap: float | None, fdv: float | None, tvl: float | None,
                     category: str, category_median_mc_tvl: float | None,
                     peer_count: int, cfg: Config) -> tuple[dict, list[str], list[str]]:
    """Возвращает (metrics, notes, flags). Не решает вход — только описывает оценку."""
    s = cfg["stage3_fundamentals"]
    metrics: dict = {"mc_tvl": None, "fdv_mc": None}
    notes: list[str] = []
    flags: list[str] = []

    # --- Навес дилюции: FDV/MC (free, из CoinGecko) ---
    if isinstance(fdv, (int, float)) and isinstance(market_cap, (int, float)) and market_cap > 0:
        fdv_mc = fdv / market_cap
        metrics["fdv_mc"] = round(fdv_mc, 2)
        if fdv_mc >= s["fdv_mc_high"]:
            flags.append("high_dilution")
            notes.append(f"[проверено источником] FDV/MC={fdv_mc:.1f}× — крупный навес будущей эмиссии")
        else:
            notes.append(f"[проверено источником] FDV/MC={fdv_mc:.1f}× — умеренная дилюция")
    else:
        notes.append("[нет данных] FDV/MC — нет FDV")

    # --- Оценка к залоченной стоимости: MC/TVL vs медиана сектора ---
    if isinstance(tvl, (int, float)) and tvl > 0 and isinstance(market_cap, (int, float)) and market_cap > 0:
        mc_tvl = market_cap / tvl
        metrics["mc_tvl"] = round(mc_tvl, 2)
        if category_median_mc_tvl and peer_count >= s["min_category_peers"]:
            over = category_median_mc_tvl * s["mc_tvl_overvalued_mult"]
            under = category_median_mc_tvl * s["mc_tvl_undervalued_mult"]
            base = (f"[проверено источником] MC/TVL={mc_tvl:.1f} vs медиана «{category}» "
                    f"={category_median_mc_tvl:.1f} (n={peer_count})")
            if mc_tvl <= under:
                flags.append("undervalued_vs_sector")
                notes.append(base + " → дёшево к сектору")
            elif mc_tvl >= over:
                flags.append("overvalued_vs_sector")
                notes.append(base + " → дорого к сектору")
            else:
                notes.append(base + " → в норме сектора")
        else:
            notes.append(f"[оценка] MC/TVL={mc_tvl:.1f}, но пиров мало (n={peer_count}) — без вывода")
    else:
        notes.append("[нет данных] MC/TVL — нет TVL (не DeFi-протокол или не сматчен)")

    # --- Честные пробелы free-тарифа ---
    notes.append("[нет данных] разблокировки (точный график) — DeFiLlama pro-only, нужен Tokenomist")
    notes.append("[нет данных] раунды/инвесторы — DeFiLlama pro-only")

    return metrics, notes, flags


def assess(candidate, medians: dict[str, tuple[float, int]], cfg: Config) -> None:
    """Финальная оценка кандидата с учётом медиан сектора (in place).

    Предполагает, что attach_tvl уже вызван (tvl/category/mc_tvl проставлены).
    """
    if not candidate.category:
        candidate.flags.append("no_defillama_match")
    median, peers = medians.get(candidate.category or "", (None, 0))

    # Если TVL ниже порога, mc_tvl не посчитан — не подсовываем сырой TVL в оценку.
    tvl_for_val = candidate.tvl if candidate.mc_tvl is not None else None
    metrics, notes, flags = assess_valuation(
        candidate.market_cap, candidate.fdv, tvl_for_val,
        candidate.category or "Unknown", median, peers, cfg,
    )
    candidate.fdv_mc = metrics["fdv_mc"]
    candidate.val_notes = notes
    candidate.flags += flags
