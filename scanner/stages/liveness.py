"""Stage 3b — «живость» проекта: разработка + широта листингов + санити объёма.

Мотивация (survivorship-анализ 16.07.2026): 60% даже топ-200 монет умерли НЕ от
скама, а просто встали (команда ушла, ликвидность утекла). Анти-раг это не ловит.
Здесь — дешёвые бесплатные сигналы «жив или труп»:

  • dev-активность (CoinGecko developer_data, GitHub): 0 коммитов за 4 недели = труп;
  • широта листингов (tickers): монета на CEX прошла чью-то due-diligence + твой выход;
  • wash-ratio: объём, невозможный при такой ликвидности = накрученный мёртвый объём.

Чистые функции — тестируются офлайн на фикстурах. Недостающие данные -> None
(режут confidence, не врут баллом). Мемкоин без GitHub не штрафуется по dev —
его несёт сигнал листингов.
"""
from __future__ import annotations

from ..config import Config

# CEX, чьё присутствие считаем сигналом качества/ликвидности (усечённый список).
_CEX = {"binance", "bybit", "okx", "coinbase", "kraken", "kucoin", "gate",
        "gate.io", "bitget", "mexc", "htx", "huobi", "upbit", "bitfinex",
        "crypto.com exchange", "gemini"}


def extract_dev_listings(detail: dict) -> dict:
    """Из `/coins/{id}` вытаскивает dev-активность и широту листингов."""
    dev = detail.get("developer_data") or {}
    tickers = detail.get("tickers") or []
    ex_names = set()
    for t in tickers:
        mkt = ((t.get("market") or {}).get("name") or "").strip().lower()
        if mkt:
            ex_names.add(mkt)
    on_cex = any(x in _CEX for x in ex_names)
    return {
        "dev_commits_4w": dev.get("commit_count_4_weeks"),
        "dev_contributors": dev.get("pull_request_contributors"),
        "dev_stars": dev.get("stars"),
        "n_exchanges": len(ex_names) if tickers else None,
        "on_cex": on_cex,
    }


def wash_ratio(volume_24h: float | None, liquidity_usd: float | None) -> float | None:
    """volume_24h / liquidity. Аномально высокое = накрутка объёма (мёртвый объём)."""
    if not isinstance(volume_24h, (int, float)) or not isinstance(liquidity_usd, (int, float)):
        return None
    if liquidity_usd <= 0:
        return None
    return round(volume_24h / liquidity_usd, 1)


def assess_liveness(c, detail: dict, cfg: Config) -> None:
    """Проставляет c.dev_*, c.n_exchanges, c.on_cex, c.wash_ratio, c.liveness_score,
    c.liveness_notes (in place). Чистая логика поверх detail + полей кандидата."""
    L = cfg.get("stage3b_liveness", {}) or {}
    dl = extract_dev_listings(detail) if detail else {}
    c.dev_commits_4w = dl.get("dev_commits_4w")
    c.dev_contributors = dl.get("dev_contributors")
    c.dev_stars = dl.get("dev_stars")
    c.n_exchanges = dl.get("n_exchanges")
    c.on_cex = bool(dl.get("on_cex"))
    c.wash_ratio = wash_ratio(c.volume_24h, c.liquidity_usd)

    notes: list[str] = []
    s = 0.0
    present = False

    # --- Разработка (если репозиторий привязан) ---
    commits = c.dev_commits_4w
    if commits is not None:
        present = True
        if commits >= L.get("commits_active", 20):
            s += 3.0; notes.append(f"[проверено] dev активен: {commits} коммитов/4нед")
        elif commits >= L.get("commits_alive", 3):
            s += 2.0; notes.append(f"[проверено] dev жив: {commits} коммитов/4нед")
        elif commits >= 1:
            s += 1.0; notes.append(f"[проверено] dev вялый: {commits} коммитов/4нед")
        else:
            notes.append("⚠ dev встал: 0 коммитов/4нед")
        if isinstance(c.dev_contributors, int) and c.dev_contributors >= 3:
            s += 1.0
        if isinstance(c.dev_stars, int) and c.dev_stars >= 500:
            s += 1.0
    else:
        notes.append("[нет данных] GitHub не привязан (нормально для мемкоинов)")

    # --- Широта листингов / CEX ---
    if c.n_exchanges is not None:
        present = True
        if c.n_exchanges >= L.get("exchanges_broad", 10):
            s += 2.0; notes.append(f"[проверено] {c.n_exchanges} рынков")
        elif c.n_exchanges >= L.get("exchanges_min", 3):
            s += 1.0; notes.append(f"[проверено] {c.n_exchanges} рынков")
        else:
            notes.append(f"⚠ узкий листинг: {c.n_exchanges} рынков")
        if c.on_cex:
            s += 3.0; notes.append("[проверено] есть CEX-листинг")
        else:
            notes.append("⚠ только DEX (нет CEX due-diligence)")

    # --- Санити объёма (накрутка) ---
    if c.wash_ratio is not None and c.wash_ratio > L.get("wash_ratio_max", 50):
        c.flags.append("wash_suspect")
        notes.append(f"⚠ объём ×{c.wash_ratio} от ликвидности — возможна накрутка")

    c.liveness_score = round(min(10.0, s), 1) if present else None
    c.liveness_notes = notes
