"""Месячная сводка: раз в месяц (1-го числа, шаг report ежедневного прогона, run.py report
--if-due) — книги пробного исполнителя с начала против альтов, BTC и контрольной корзины
по денежным потокам (scanner/benchmark.flow_pnl), что книги сделали за прошлый месяц и
здоровье источников по неделям (scanner/health.py).

Месяц — не статистика: позиций единицы, сравнение с рынком показывает знак, а не оценку.
Время — локальное, как у недельной сводки (telegram.weekly_due).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
          "сентябрь", "октябрь", "ноябрь", "декабрь")
GRACE_DAYS = 3   # первой сводки ещё не было — шлём, только если месяц начался не позже


def month_start(ts: float) -> datetime:
    return datetime.fromtimestamp(ts).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def prev_month(now: float) -> tuple[float, float, str]:
    """(начало, конец, «сентябрь 2026») прошлого календарного месяца по локальному времени."""
    end = month_start(now)
    start = (end.replace(year=end.year - 1, month=12) if end.month == 1
             else end.replace(month=end.month - 1))
    return start.timestamp(), end.timestamp(), f"{MONTHS[start.month - 1]} {start.year}"


def due(last_ts: float | None, now: float, grace_days: int = GRACE_DAYS) -> bool:
    """Пора ли месячной сводке: в этом месяце её ещё не было. Первая (флага нет) — только в
    первые grace_days дней месяца: выкатка посреди месяца не шлёт сводку за неполный месяц.
    VPS был выключен 1-го — уйдёт в первый прогон после."""
    start = month_start(now).timestamp()
    if last_ts is None:
        return now < start + grace_days * 86400
    return last_ts < start


def collect(cfg, now: float) -> dict[str, Any]:
    """Данные сводки: месяц, книги и тень (executor.weekly_books/weekly_shadow — с начала, по
    потокам), активность за месяц (executor.period_activity). Сбой блока — пустой блок и
    строка в errors: сводка уходит всё равно (это ещё и признак жизни)."""
    from . import executor, health
    t0, t1, label = prev_month(now)
    db = cfg["output"]["db_path"]
    out: dict[str, Any] = {"month": label, "t0": t0, "t1": t1, "books": [], "shadow": [],
                           "activity": None, "health": None, "errors": []}
    for key, fn in (("books", executor.weekly_books), ("shadow", executor.weekly_shadow),
                    ("activity", lambda c: executor.period_activity(c, t0, t1)),
                    ("health", lambda c: health.summarize(health.load(db, t0, t1) or [],
                                                          t0, t1, 7))):
        try:
            out[key] = fn(cfg)
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{key}: {type(e).__name__}: {e}")
    return out
