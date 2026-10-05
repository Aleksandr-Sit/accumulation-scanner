"""Paper-книга против рынка на тех же окнах (недельная сводка, backtest/forward_vs_benchmark.py).

Плюс книги в растущем рынке — ещё не заслуга отбора: альты могли вырасти сильнее. Поэтому
каждой позиции сопоставляется рынок на её окне — вход → закрытие (closed_ts) для закрытых,
вход → последний снапшот для открытых:
  • альты = total × (1 − BTC.D/100) − стейблы (alt_ex из regime.market_series);
  • BTC   = total × BTC.D/100.
Значение рынка — на ближайший день market_daily ≤ даты. P&L позиции — net-of-fees, как paper
P&L: realized + position_pnl (stages/exit) по цене последнего снапшота; стоимость = вход ×
initial_qty. Итог книги — Σ P&L / Σ стоимость, рынок — среднее, взвешенное той же стоимостью.
Отбор полезен, только если книга обгоняет альты.

Чистые функции + тонкий слой чтения БД. БД открываются ТОЛЬКО на чтение (sqlite mode=ro):
Store/PositionStore при открытии пишут схему и миграции — архив так портить нельзя.
"""
from __future__ import annotations

import sqlite3
from bisect import bisect_right
from pathlib import Path
from typing import Any

from . import regime
from .stages.exit import position_pnl

DAY = 86400
STALE_DAYS = 2      # рынок отстал от конца окна больше — помечаем датой (как в сводке дня)


def day_of(ts: float) -> int:
    return int(ts // DAY) * DAY


# ---------------------------------------------------------------- чистые функции

def market_points(rows: list[dict]) -> dict[str, list]:
    """Строки market_daily -> {"days", "alt", "btc"} по дням, где посчитан альт-рынок
    (нужны total, BTC.D и стейблы — альты и BTC всегда на одну дату)."""
    S = regime.market_series(rows)
    alt, btc = S.get("alt_ex", {}), S.get("btc_m", {})
    days = sorted(d for d in alt if d in btc)
    return {"days": days, "alt": [alt[d] for d in days], "btc": [btc[d] for d in days]}


def market_at(mkt: dict, ts: float) -> tuple[int, float, float] | None:
    """(день, альты, BTC) на ближайший день ≤ даты ts; None — рынка на эту дату ещё нет."""
    days = mkt.get("days") or []
    i = bisect_right(days, day_of(ts)) - 1
    if i < 0:
        return None
    return days[i], mkt["alt"][i], mkt["btc"][i]


def is_main(p: dict) -> bool:
    """Позиция основной книги, а не близнец A/B-тестов (variant B/S с twin_of). В старой
    схеме (архив v1) колонок variant/twin_of нет — все позиции основные."""
    return (p.get("variant") or "A") == "A" and not p.get("twin_of")


def position_outcome(p: dict, last: dict | None) -> dict | None:
    """Окно и итог позиции: {start, end, cost, pnl}. Закрытая — до closed_ts, итог = realized;
    открытая — до последнего снапшота: realized + position_pnl по его цене (те же комиссии,
    что в paper P&L). None — открытая без снапшота: оценить нечем."""
    cost = p["entry_price"] * (p.get("initial_qty") or p["qty"])
    realized = p.get("realized_usdt") or 0.0
    if p.get("status") == "closed":
        end, pnl = p.get("closed_ts"), realized
    else:
        if not last or last.get("price") is None:
            return None
        end = last["ts"]
        unreal = position_pnl(p, last["price"])["pnl_usdt"] if p["qty"] > 0 else 0.0
        pnl = realized + unreal
    if end is None or cost <= 0:
        return None
    return {"start": p["entry_ts"], "end": end, "cost": cost, "pnl": pnl}


def compare_book(positions: list[dict], last: dict[int, dict], mkt: dict) -> dict[str, Any]:
    """Книга против рынка на тех же окнах. positions — уже отобранная книга (A без близнецов
    или real), last — {position_id: последний снапшот {ts, price}}.

    -> {positions, n, rows, cost, pnl, book_pct, alt_pct, btc_pct, diff_pp, last_end,
        market_day, stale}; проценты — в процентах, diff_pp = книга − альты (п.п.).
    Позиции без снапшота или без рынка на даты окна в итог не входят (n < positions);
    n == 0 — итоговых полей нет. stale: рынок на конец самого позднего окна старше его
    даты больше чем на STALE_DAYS дней."""
    rows = []
    for p in positions:
        o = position_outcome(p, last.get(p["id"]))
        m0 = market_at(mkt, o["start"]) if o else None
        m1 = market_at(mkt, o["end"]) if o else None
        if not (m0 and m1):
            continue
        rows.append({"id": p["id"], "symbol": p["symbol"], **o,
                     "book_pct": o["pnl"] / o["cost"] * 100,
                     "alt_pct": (m1[1] / m0[1] - 1) * 100,
                     "btc_pct": (m1[2] / m0[2] - 1) * 100,
                     "market_day": m1[0]})
    out: dict[str, Any] = {"positions": len(positions), "n": len(rows), "rows": rows}
    if not rows:
        return out
    cost = sum(r["cost"] for r in rows)
    pnl = sum(r["pnl"] for r in rows)
    book = pnl / cost * 100
    alt = sum(r["cost"] * r["alt_pct"] for r in rows) / cost
    btc = sum(r["cost"] * r["btc_pct"] for r in rows) / cost
    latest = max(rows, key=lambda r: r["end"])
    lag = day_of(latest["end"]) - latest["market_day"]
    out.update(cost=cost, pnl=pnl, book_pct=book, alt_pct=alt, btc_pct=btc,
               diff_pp=book - alt, last_end=latest["end"], market_day=latest["market_day"],
               stale=lag > STALE_DAYS * DAY)
    return out


# ---------------------------------------------------------------- чтение БД (mode=ro)

def connect_ro(path) -> sqlite3.Connection | None:
    """Соединение только на чтение; нет файла -> None (без создания пустой БД)."""
    p = Path(path)
    if not p.is_file():
        return None
    return sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=30)


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def load_market(path) -> dict[str, list]:
    """Ряд рынка из market_daily БД path; нет файла или таблицы — пустой ряд."""
    con = connect_ro(path)
    rows: list[dict] = []
    if con is not None:
        try:
            if "market_daily" in _tables(con):
                rows = [{"day": r[0], "total_mcap": r[1], "btc_dominance": r[2],
                         "stables_usd": r[3]} for r in con.execute(
                    "SELECT day, total_mcap, btc_dominance, stables_usd FROM market_daily "
                    "ORDER BY day")]
        finally:
            con.close()
    return market_points(rows)


def load_book(path) -> dict | None:
    """Позиции и последние снапшоты (с ценой) БД path: {"positions", "last"}.
    Нет файла -> None; нет таблиц позиций -> пустая книга."""
    con = connect_ro(path)
    if con is None:
        return None
    try:
        t = _tables(con)
        if "positions" not in t:
            return {"positions": [], "last": {}}
        cur = con.execute("SELECT * FROM positions ORDER BY id")
        names = [d[0] for d in cur.description]
        positions = [dict(zip(names, r)) for r in cur.fetchall()]
        last: dict[int, dict] = {}
        if "position_snapshots" in t:
            last = {r[0]: {"ts": r[1], "price": r[2]} for r in con.execute(
                "SELECT s.position_id, s.ts, s.price FROM position_snapshots s JOIN ("
                "  SELECT position_id, MAX(ts) mt FROM position_snapshots "
                "  WHERE price IS NOT NULL GROUP BY position_id"
                ") m ON s.position_id=m.position_id AND s.ts=m.mt WHERE s.price IS NOT NULL")}
    finally:
        con.close()
    return {"positions": positions, "last": last}


def book_split(book: dict) -> tuple[list[dict], list[dict]]:
    """(paper A, real) без близнецов B/S."""
    main = [p for p in book["positions"] if is_main(p)]
    return ([p for p in main if p.get("is_paper")], [p for p in main if not p.get("is_paper")])


def weekly_books(cfg) -> list[dict]:
    """Строки блока «Против рынка» недельной сводки: книга A текущей БД, real — если есть,
    архивная книга — если в config задан benchmark.archive_db и файл существует (иначе
    молча пропускается). Рынок для всех — из market_daily ТЕКУЩЕЙ БД (в архиве его нет)."""
    if not cfg.get("benchmark.enabled", True):
        return []
    db = cfg["output"]["db_path"]
    mkt = load_market(db)
    out = []
    cur = load_book(db) or {"positions": [], "last": {}}
    paper, real = book_split(cur)
    if paper:
        out.append({"key": "paper", "emoji": "📝", "label": "книга A",
                    **compare_book(paper, cur["last"], mkt)})
    if real:
        out.append({"key": "real", "emoji": "💰", "label": "real",
                    **compare_book(real, cur["last"], mkt)})
    path = cfg.get("benchmark.archive_db") or ""
    if path:
        try:
            arch = load_book(path)
        except sqlite3.Error as e:
            print(f"[report] архив {path} не прочитан ({e}) — строка пропущена")
            arch = None
        a_paper = book_split(arch)[0] if arch else []
        if a_paper:
            out.append({"key": "archive", "emoji": "🗄",
                        "label": cfg.get("benchmark.archive_label") or "архив",
                        **compare_book(a_paper, arch["last"], mkt)})
    return out
