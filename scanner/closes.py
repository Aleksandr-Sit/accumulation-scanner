"""Дневные закрытия для контура позиций (watch) с датой каждого закрытия.

Источник: монеты с парой Bybit (venue позиции — Bybit) — закрытые дневные свечи Bybit без
кэша; остальные (DEX) и сбой/отставание Bybit — точки CoinGecko 00:00 UTC. Берётся ряд
с самым свежим закрытием (при равенстве — Bybit).

close_ts — момент закрытия дня, 00:00 UTC следующего дня: свеча Bybit со стартом S закрыта
в S + 1д; точка CoinGecko 00:00 — закрытие предыдущего дня. Подпись дня закрытия —
close_ts − 1д (telegram._close_day). Живой тик CoinGecko (точка не на 00:00) отброшен.

lag_days — на сколько дней последнее закрытие отстаёт от ожидаемого (00:00 UTC сегодня):
0 — свежее; ≥1 — CoinGecko ещё не выложил точку дня (замер 08.10: в 07:52 UTC точки нет
ни у одной из 45 монет) или ряд замёрз (монета умирает). Замер 08.10: уровень цен
CoinGecko ≈ Bybit (|Δ| медиана 0.07%), проблема только в свежести.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from .sources import bybit, coingecko

DAY = 86400
_ALIGN_TOL = 300   # точка CoinGecko «00:00» с небольшим сдвигом; живой тик в 06:00 — не она


def expected_close_ts(now: float) -> int:
    """Последнее закрытие, которое уже должно существовать: 00:00 UTC сегодня."""
    return int(now) // DAY * DAY


def close_label(close_ts: float) -> str:
    """«07.10» — день, который закрылся в close_ts."""
    return datetime.fromtimestamp(close_ts - DAY, timezone.utc).strftime("%d.%m")


def from_bybit(ohlcv: dict) -> dict:
    """Закрытые свечи Bybit (bybit.parse_klines) -> {close_ts, prices, volumes (USDT)}."""
    ts = ohlcv.get("ts") or []
    return {"close_ts": [int(t) + DAY for t in ts], "prices": list(ohlcv.get("c") or []),
            "volumes": list(ohlcv.get("qv") or [None] * len(ts))}


def from_coingecko(chart: dict) -> dict:
    """market_chart CoinGecko -> {close_ts, prices, volumes}: только точки 00:00 UTC; повтор
    дня — последняя точка; цена None/≤0 отброшена."""
    ts, prices = chart.get("ts") or [], chart.get("prices") or []
    vols = chart.get("volumes") or [None] * len(ts)
    by_day: dict[int, tuple[float, float | None]] = {}
    for t, p, v in zip(ts, prices, vols):
        off = int(round(t)) % DAY
        if not isinstance(p, (int, float)) or p <= 0 or _ALIGN_TOL < off < DAY - _ALIGN_TOL:
            continue
        by_day[int(round(t / DAY)) * DAY] = (p, v)
    days = sorted(by_day)
    return {"close_ts": days, "prices": [by_day[d][0] for d in days],
            "volumes": [by_day[d][1] for d in days]}


def chart_age_days(chart: dict, now: float) -> int | None:
    """Сколько дней назад было последнее закрытие ряда CoinGecko (None — ряда нет). Скан:
    замёрзший график (memetoon до 02.09) не должен считаться текущим."""
    s = from_coingecko(chart)
    if not s["close_ts"]:
        return None
    return max(0, (expected_close_ts(now) - s["close_ts"][-1]) // DAY)


def is_bybit(pos: dict) -> bool:
    """Позиция на Bybit: карточка скана (venue «Bybit spot», тикер сверен по цене) или
    исполнение Bybit (sync, venue «bybit»)."""
    return "bybit" in (pos.get("venue") or "").lower()


def load(http, pos: dict, demo: str = "", now: float | None = None, days: int = 400) -> dict:
    """Закрытия позиции -> {close_ts, prices, volumes, src, lag_days, note}. Пустые списки —
    ни один источник не ответил (note — почему)."""
    now = now if now is not None else time.time()
    exp = expected_close_ts(now)
    sym = (pos.get("symbol") or "").upper()
    cands: list[dict] = []
    notes: list[str] = []
    if is_bybit(pos) and sym:
        s = from_bybit(bybit.fetch_daily_ohlcv(http, f"{sym}USDT", days))
        if s["prices"]:
            cands.append({**s, "src": "Bybit"})
        else:
            notes.append(f"Bybit {sym}USDT не отдал свечи")
    fresh = cands and cands[0]["close_ts"][-1] >= exp
    if not fresh and pos.get("coin_id"):
        s = from_coingecko(coingecko.fetch_market_chart(http, pos["coin_id"], min(days, 365),
                                                        demo))
        if s["prices"]:
            cands.append({**s, "src": "CoinGecko"})
        else:
            notes.append("CoinGecko не отдал ряд (429/новый coin_id?)")
    if not cands:
        if not pos.get("coin_id") and not is_bybit(pos):
            notes.append("нет coin_id — цену не достать (задай при pos add)")
        return {"close_ts": [], "prices": [], "volumes": [], "src": "", "lag_days": None,
                "note": "; ".join(notes)}
    best = max(cands, key=lambda c: c["close_ts"][-1])     # при равенстве — первый (Bybit)
    lag = max(0, (exp - best["close_ts"][-1]) // DAY)
    note_lag = getattr(http, "note_lag", None)      # здоровье источников (scanner/health.py)
    if note_lag:
        note_lag(best["src"], lag)
    if lag:
        notes.append(f"закрытие {close_label(exp)} ещё не вышло ({best['src']}), последнее — "
                     f"{close_label(best['close_ts'][-1])}")
    return {**best, "lag_days": lag, "note": "; ".join(notes)}
