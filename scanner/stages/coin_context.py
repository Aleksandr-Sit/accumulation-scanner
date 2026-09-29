"""Stage 4d — монетный контекст: эмиссия, выручка (P/F), плечо, делистинг, US-тег.

ИНФОРМАЦИОННЫЙ слой: пишет поля кандидата, флаги и заметки, но в скор не входит —
ни один признак не провалидирован на истории (market_regime_study покрыл только
рынок целиком). Единственная эмпирика: монеты с US-ETF в медведе 2025–26 упали
в медиане на −74% против −86% у альтов — защита относительная, не абсолютная.

Пороги — [оценка] в config.coin_context. Чистые функции, тестируются офлайн.
"""
from __future__ import annotations

from ..config import Config


def supply_growth(prices: list, mcaps: list) -> float | None:
    """Рост предложения за окно ряда: (mcap/price) последней точки к первой − 1.
    Нужны ≥2 валидные точки; скачки CoinGecko (пересчёт circulating) не фильтруем."""
    pts = [(m / p) for p, m in zip(prices or [], mcaps or [])
           if isinstance(p, (int, float)) and isinstance(m, (int, float)) and p > 0 and m > 0]
    if len(pts) < 2 or pts[0] <= 0:
        return None
    return round(pts[-1] / pts[0] - 1, 4)


def annotate(c, extras: dict, cfg: Config, prices: list | None = None,
             mcaps: list | None = None) -> None:
    """Заполняет supply_growth / revenue_30d / p_f / oi_mcap / delist / us_tag + заметки."""
    k = cfg.get("coin_context", {}) or {}
    sym = (c.symbol or "").upper()
    notes: list[str] = []

    # 1) Эмиссия (разлоки/инфляция) из ряда капитализации.
    g = supply_growth(prices or [], mcaps or [])
    c.supply_growth = g
    if g is not None and g >= k.get("supply_growth_warn", 0.25):
        c.flags.append("supply_inflation")
        notes.append(f"⚠ предложение +{g*100:.0f}% за окно истории — разлоки/эмиссия давят")

    # 2) Выручка протокола -> P/F (капа к годовой выручке).
    rev = extras.get("revenue") or {}
    r30 = (rev.get("by_gecko") or {}).get(c.coin_id or "") or (rev.get("by_symbol") or {}).get(sym)
    if isinstance(r30, (int, float)) and r30 > 0:
        c.revenue_30d = r30
        if isinstance(c.market_cap, (int, float)) and c.market_cap > 0:
            c.p_f = round(c.market_cap / (r30 * 12), 1)
            tag = " — дёшево к выручке" if c.p_f <= k.get("pf_cheap", 15) else ""
            notes.append(f"выручка ${r30/1e6:.1f}M/30д, P/F {c.p_f:g}{tag}")

    # 3) Плечо на монете: OI перпа к капитализации.
    oi = (extras.get("oi") or {}).get(sym)
    if isinstance(oi, (int, float)) and oi > 0 and isinstance(c.market_cap, (int, float)) and c.market_cap > 0:
        c.oi_mcap = round(oi / c.market_cap, 3)
        if c.oi_mcap >= k.get("oi_mcap_warn", 0.2):
            c.flags.append("high_leverage")
            notes.append(f"⚠ OI перпа {c.oi_mcap*100:.0f}% капы — сквизы в обе стороны")

    # 4) Делистинг / пометка ST на Bybit.
    dl = (extras.get("delist") or {}).get(sym)
    if dl:
        c.delist = dl
        c.flags.append(f"delist_{dl}")
        notes.append({"spot": "🚫 Bybit объявил делистинг спота",
                      "perp": "⚠ Bybit снимает перп (спот остаётся)",
                      "st": "⚠ Bybit: метка ST (под наблюдением, риск делистинга)"}[dl])

    # 5) US-тег: спотовый ETF в США (список в config) > листинг на Coinbase.
    etf = {s.upper() for s in k.get("us_etf", []) or []}
    if sym in etf:
        c.us_tag = "etf"
        notes.append("🇺🇸 есть спотовый ETF в США (в медведе падала меньше альтов)")
    elif sym in (extras.get("coinbase") or set()):
        c.us_tag = "coinbase"

    c.zone_signals += notes
