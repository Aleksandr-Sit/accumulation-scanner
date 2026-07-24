"""Хранилище позиций (Stage 7). Ручной ввод на этапе сбора статистики.

Таблицы в том же scanner.db: positions (открытые/закрытые) + position_events
(лестница/трейлинг/инвалидация/ручные действия — журнал для идемпотентности
алертов и последующего анализа качества выходов).

Профиль стратегии: откуп у дна -> холд/накопление -> распределение в бычьей
фазе (горизонт 1-2 года ок). Не свинг.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    coin_id     TEXT DEFAULT '',
    chain       TEXT DEFAULT '',
    address     TEXT DEFAULT '',
    venue       TEXT DEFAULT '',
    entry_price REAL NOT NULL,
    qty         REAL NOT NULL,
    initial_qty REAL,
    entry_ts    REAL NOT NULL,
    base_low    REAL,
    hwm         REAL,
    status      TEXT DEFAULT 'open',
    exit_price  REAL,
    closed_ts   REAL,
    realized_usdt REAL DEFAULT 0,
    notes       TEXT DEFAULT '',
    is_paper    INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS position_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    ts          REAL NOT NULL,
    type        TEXT NOT NULL,
    price       REAL,
    note        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_pos ON position_events(position_id, type);
CREATE TABLE IF NOT EXISTS position_snapshots (
    position_id INTEGER NOT NULL,
    ts          REAL NOT NULL,
    price       REAL,
    pnl_pct     REAL,
    hwm         REAL
);
CREATE INDEX IF NOT EXISTS idx_snap_pos ON position_snapshots(position_id, ts);
"""


def _row_to_dict(cur: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {d[0]: row[i] for i, d in enumerate(cur.description)}


class PositionStore:
    def __init__(self, db_path: str):
        # timeout: cron scan (07:00) может ещё писать БД, когда стартует watch (07:30)
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(positions)")}
        if "is_paper" not in cols:
            self.conn.execute("ALTER TABLE positions ADD COLUMN is_paper INTEGER DEFAULT 0")
        if "initial_qty" not in cols:
            self.conn.execute("ALTER TABLE positions ADD COLUMN initial_qty REAL")
            self.conn.execute("UPDATE positions SET initial_qty=qty WHERE initial_qty IS NULL")
        if "realized_usdt" not in cols:
            self.conn.execute("ALTER TABLE positions ADD COLUMN realized_usdt REAL DEFAULT 0")

    # --- CRUD ---
    def add(self, symbol: str, entry_price: float, qty: float,
            coin_id: str = "", chain: str = "", address: str = "",
            venue: str = "", base_low: float | None = None,
            entry_ts: float | None = None, notes: str = "",
            paper: bool = False) -> int:
        ts = entry_ts if entry_ts is not None else time.time()
        cur = self.conn.execute(
            """INSERT INTO positions(symbol, coin_id, chain, address, venue,
                   entry_price, qty, initial_qty, entry_ts, base_low, hwm, notes, is_paper)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol.upper(), coin_id, chain, address, venue,
             entry_price, qty, qty, ts, base_low, entry_price, notes, int(paper)))
        self.conn.commit()
        return int(cur.lastrowid)

    def has_open_for(self, coin_id: str, symbol: str = "") -> bool:
        """Есть ли уже открытая позиция по монете (paper или реальная)."""
        cur = self.conn.execute(
            "SELECT 1 FROM positions WHERE status='open' AND "
            "((coin_id != '' AND coin_id=?) OR (?!='' AND symbol=?)) LIMIT 1",
            (coin_id, symbol.upper(), symbol.upper()))
        return cur.fetchone() is not None

    def count_open_paper(self) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status='open' AND is_paper=1")
        return int(cur.fetchone()[0])

    def open_positions(self) -> list[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM positions WHERE status='open' ORDER BY id")
        return [_row_to_dict(cur, r) for r in cur.fetchall()]

    def all_positions(self) -> list[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM positions ORDER BY id")
        return [_row_to_dict(cur, r) for r in cur.fetchall()]

    def find_open(self, ref: str) -> dict[str, Any] | None:
        """Ищет открытую позицию по id или тикеру. По тикеру РЕАЛЬНАЯ приоритетнее
        paper — иначе `pos close ARB` мог закрыть виртуальную вместо купленной."""
        if ref.isdigit():
            cur = self.conn.execute(
                "SELECT * FROM positions WHERE id=? AND status='open'", (int(ref),))
        else:
            cur = self.conn.execute(
                "SELECT * FROM positions WHERE symbol=? AND status='open' "
                "ORDER BY is_paper ASC, id ASC", (ref.upper(),))
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None

    # --- снапшоты (дневная история P&L — для дайджеста/недельной сводки/анализа) ---
    def snapshot(self, position_id: int, price: float, pnl_pct: float, hwm: float) -> None:
        self.conn.execute(
            "INSERT INTO position_snapshots(position_id, ts, price, pnl_pct, hwm) "
            "VALUES (?,?,?,?,?)", (position_id, time.time(), price, pnl_pct, hwm))
        self.conn.commit()

    def last_snapshots(self) -> dict[int, dict[str, Any]]:
        """position_id -> последний снапшот (без сети — для недельной сводки)."""
        cur = self.conn.execute(
            "SELECT s.* FROM position_snapshots s JOIN ("
            "  SELECT position_id, MAX(ts) mt FROM position_snapshots GROUP BY position_id"
            ") m ON s.position_id=m.position_id AND s.ts=m.mt")
        return {r[0]: _row_to_dict(cur, r) for r in cur.fetchall()}

    def events_since(self, ts: float) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT e.*, p.symbol, p.is_paper FROM position_events e "
            "JOIN positions p ON p.id=e.position_id WHERE e.ts>=? ORDER BY e.ts", (ts,))
        return [_row_to_dict(cur, r) for r in cur.fetchall()]

    def first_paper_ts(self) -> float | None:
        """Время самой ранней paper-позиции — точка отсчёта наблюдения."""
        cur = self.conn.execute(
            "SELECT MIN(entry_ts) FROM positions WHERE is_paper=1")
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else None

    # --- системные флаги (идемпотентность одноразовых событий, position_id=0) ---
    def system_flag(self, name: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM position_events WHERE position_id=0 AND type=? LIMIT 1", (name,))
        return cur.fetchone() is not None

    def set_system_flag(self, name: str, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO position_events(position_id, ts, type, note) VALUES (0,?,?,?)",
            (time.time(), name, note))
        self.conn.commit()

    def update_hwm(self, position_id: int, hwm: float) -> None:
        self.conn.execute("UPDATE positions SET hwm=? WHERE id=?", (hwm, position_id))
        self.conn.commit()

    def set_base_low(self, position_id: int, base_low: float) -> None:
        self.conn.execute("UPDATE positions SET base_low=? WHERE id=?",
                          (base_low, position_id))
        self.conn.commit()

    def get(self, position_id: int) -> dict[str, Any] | None:
        cur = self.conn.execute("SELECT * FROM positions WHERE id=?", (position_id,))
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None

    def sell(self, position_id: int, sold_qty: float, price: float, reason: str,
             note: str = "", fee: float = 0.0015) -> float:
        """Продажа части/всей позиции с учётом realized P&L net-of-fees. Ядро для
        paper-executor, лестницы, ручного pos reduce/close. Закрывает при qty≈0.
        Возвращает realized_delta USDT."""
        pos = self.get(position_id)
        if not pos or pos["status"] != "open":
            return 0.0
        entry = pos["entry_price"]
        sold_qty = min(sold_qty, pos["qty"])
        if sold_qty <= 0:
            return 0.0
        realized = sold_qty * price * (1 - fee) - sold_qty * entry * (1 + fee)
        new_qty = pos["qty"] - sold_qty
        closed = new_qty <= 1e-9
        self.conn.execute(
            "UPDATE positions SET qty=?, realized_usdt=COALESCE(realized_usdt,0)+?, "
            "status=?, exit_price=?, closed_ts=? WHERE id=?",
            (max(0.0, new_qty), realized,
             "closed" if closed else "open",
             price if closed else pos["exit_price"],
             time.time() if closed else pos["closed_ts"],
             position_id))
        self.record_event(position_id, reason, price,
                          note or f"продано {sold_qty:.6g} @ {price:.6g}, realized {realized:+.2f}")
        return realized

    def close(self, position_id: int, exit_price: float, note: str = "",
              reason: str = "manual_close") -> float:
        pos = self.get(position_id)
        if not pos:
            return 0.0
        return self.sell(position_id, pos["qty"], exit_price, reason, note)

    def recent_closed_paper(self, coin_id: str, symbol: str, days: float) -> bool:
        """Была ли paper-позиция по монете закрыта в последние `days` (cooldown re-open)."""
        cutoff = time.time() - days * 86400
        cur = self.conn.execute(
            "SELECT 1 FROM positions WHERE status='closed' AND is_paper=1 AND closed_ts>=? "
            "AND ((coin_id!='' AND coin_id=?) OR (?!='' AND symbol=?)) LIMIT 1",
            (cutoff, coin_id, symbol.upper(), symbol.upper()))
        return cur.fetchone() is not None

    def last_event_ts(self, position_id: int, etype: str) -> float | None:
        cur = self.conn.execute(
            "SELECT MAX(ts) FROM position_events WHERE position_id=? AND type=?",
            (position_id, etype))
        r = cur.fetchone()
        return r[0] if r and r[0] is not None else None

    # --- события (журнал + идемпотентность алертов) ---
    def record_event(self, position_id: int, etype: str,
                     price: float | None = None, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO position_events(position_id, ts, type, price, note) VALUES (?,?,?,?,?)",
            (position_id, time.time(), etype, price, note))
        self.conn.commit()

    def event_types(self, position_id: int) -> set[str]:
        cur = self.conn.execute(
            "SELECT DISTINCT type FROM position_events WHERE position_id=?", (position_id,))
        return {r[0] for r in cur.fetchall()}

    def events(self, position_id: int) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM position_events WHERE position_id=? ORDER BY ts", (position_id,))
        return [_row_to_dict(cur, r) for r in cur.fetchall()]

    def close_db(self) -> None:
        self.conn.close()


def risk_check(open_positions: list[dict], new_value_usdt: float, cfg,
               current_prices: dict[int, float] | None = None) -> list[str]:
    """Проверка бюджета риска (DD-лимит 30% портфеля). Возвращает предупреждения.

    Логика: на микрокапах стоп не гарантирован (гэп/делистинг), поэтому риск
    позиции = размер * worst_case_loss. capital_usdt=0 -> проверка выключена.
    current_prices (id->цена) — текущая рыночная оценка экспозиции; без неё берётся
    entry (устаревает, если позиция выросла — недооценка риска, см. аудит B6).
    """
    p = cfg.get("stage7_positions", {}) or {}
    capital = p.get("capital_usdt", 0) or 0
    if capital <= 0:
        return []
    warns: list[str] = []
    max_pos = capital * p.get("max_position_pct_of_capital", 10) / 100.0
    if new_value_usdt > max_pos:
        warns.append(f"позиция {new_value_usdt:.0f} USDT > лимита "
                     f"{max_pos:.0f} ({p.get('max_position_pct_of_capital')}% капитала)")
    worst = p.get("worst_case_loss_pct", 60) / 100.0
    cp = current_prices or {}
    # paper-позиции виртуальные — в экспозицию не входят; оценка по текущей цене.
    exposure = sum((cp.get(pos["id"], pos["entry_price"])) * pos["qty"]
                   for pos in open_positions if not pos.get("is_paper"))
    exposure += new_value_usdt
    worst_dd = exposure * worst / capital * 100
    if worst_dd > 30:
        warns.append(f"worst-case просадка портфеля {worst_dd:.0f}% > лимита 30% "
                     f"(экспозиция {exposure:.0f} USDT × {worst * 100:.0f}%)")
    return warns
