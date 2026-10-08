"""Хранилище позиций (Stage 7). Ручной ввод на этапе сбора статистики.

Таблицы в том же scanner.db: positions (открытые/закрытые) + position_events
(лестница/трейлинг/инвалидация/ручные действия — журнал для идемпотентности
алертов и последующего анализа качества выходов).

Профиль стратегии: откуп у дна -> холд/накопление -> распределение в бычьей
фазе (горизонт 1-2 года ок). Не свинг.

watch (pipeline.run_watch): positions.last_close_ts — последнее оценённое дневное закрытие
(следующий прогон оценивает все более новые по порядку), trail_armed_ts — защёлка трейла
(закрытие ≥ +arm от средней). notify_outbox — очередь карточек выхода: кладётся той же
транзакцией, что события, помечается после доставки (сбой Telegram — повтор позже).
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
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
    is_paper    INTEGER DEFAULT 0,
    variant     TEXT DEFAULT 'A',
    twin_of     INTEGER,
    last_close_ts  REAL,
    trail_armed_ts REAL
);
CREATE TABLE IF NOT EXISTS position_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    ts          REAL NOT NULL,
    type        TEXT NOT NULL,
    price       REAL,
    note        TEXT DEFAULT '',
    close_ts    REAL
);
CREATE INDEX IF NOT EXISTS idx_events_pos ON position_events(position_id, type);
CREATE TABLE IF NOT EXISTS position_snapshots (
    position_id INTEGER NOT NULL,
    ts          REAL NOT NULL,
    price       REAL,
    pnl_pct     REAL,
    hwm         REAL,
    close_ts    REAL
);
CREATE INDEX IF NOT EXISTS idx_snap_pos ON position_snapshots(position_id, ts);
CREATE TABLE IF NOT EXISTS notify_outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    position_id INTEGER,
    close_ts    REAL,
    payload     TEXT NOT NULL,
    created_ts  REAL NOT NULL,
    sent_ts     REAL,
    tries       INTEGER DEFAULT 0,
    last_error  TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS exchange_fills (
    exec_id     TEXT PRIMARY KEY,
    venue       TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    price       REAL NOT NULL,
    qty         REAL NOT NULL,
    fee_usdt    REAL DEFAULT 0,
    base_delta  REAL NOT NULL,
    ts          REAL NOT NULL,
    order_id    TEXT DEFAULT '',
    position_id INTEGER,
    note        TEXT DEFAULT ''
);
"""


def _row_to_dict(cur: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {d[0]: row[i] for i, d in enumerate(cur.description)}


# Порядок снапшотов: по дню закрытия; у старых (до close_ts) — день записи (так их и
# подписывала сводка дня).
_SNAP_KEY = "COALESCE(close_ts, (CAST(ts AS INTEGER) / 86400) * 86400)"


class PositionStore:
    def __init__(self, db_path: str):
        # timeout: cron scan (07:00) может ещё писать БД, когда стартует watch (07:30)
        self.conn = sqlite3.connect(db_path, timeout=30)
        self._tx = 0
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
        # A/B выхода на paper: B — близнец A с сужением трейла при перегреве рынка,
        # S — близнец со стопом −50% (только монеты фильтра качества).
        if "variant" not in cols:
            self.conn.execute("ALTER TABLE positions ADD COLUMN variant TEXT DEFAULT 'A'")
        if "twin_of" not in cols:
            self.conn.execute("ALTER TABLE positions ADD COLUMN twin_of INTEGER")
        # watch по закрытиям с датой: последнее оценённое закрытие и защёлка трейла.
        # NULL у старых позиций -> первый watch переоценит закрытия с момента входа.
        if "last_close_ts" not in cols:
            self.conn.execute("ALTER TABLE positions ADD COLUMN last_close_ts REAL")
        if "trail_armed_ts" not in cols:
            self.conn.execute("ALTER TABLE positions ADD COLUMN trail_armed_ts REAL")
        for table in ("position_snapshots", "position_events"):
            tcols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if "close_ts" not in tcols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN close_ts REAL")

    # --- транзакции ---
    @contextmanager
    def atomic(self):
        """Несколько записей одной транзакцией: внутри методы не коммитят, исключение —
        откат всего блока (sync: позиция и exchange_fills; watch: события, paper-исполнение,
        снапшот и очередь карточек — вместе или никак)."""
        self._tx += 1
        try:
            yield self
        except BaseException:
            self._tx -= 1
            if not self._tx:
                self.conn.rollback()
            raise
        self._tx -= 1
        if not self._tx:
            self.conn.commit()

    def _commit(self) -> None:
        if not self._tx:
            self.conn.commit()

    # --- CRUD ---
    def add(self, symbol: str, entry_price: float, qty: float,
            coin_id: str = "", chain: str = "", address: str = "",
            venue: str = "", base_low: float | None = None,
            entry_ts: float | None = None, notes: str = "",
            paper: bool = False, variant: str = "A",
            twin_of: int | None = None) -> int:
        ts = entry_ts if entry_ts is not None else time.time()
        cur = self.conn.execute(
            """INSERT INTO positions(symbol, coin_id, chain, address, venue,
                   entry_price, qty, initial_qty, entry_ts, base_low, hwm, notes, is_paper,
                   variant, twin_of)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol.upper(), coin_id, chain, address, venue,
             entry_price, qty, qty, ts, base_low, entry_price, notes, int(paper),
             variant, twin_of))
        self._commit()
        return int(cur.lastrowid)

    def find_open_real(self, symbol: str) -> dict[str, Any] | None:
        """Открытая РЕАЛЬНАЯ позиция по тикеру (для pos add --merge: paper не трогаем)."""
        cur = self.conn.execute(
            "SELECT * FROM positions WHERE symbol=? AND status='open' AND is_paper=0 "
            "ORDER BY id ASC", (symbol.upper(),))
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None

    def merge(self, position_id: int, price: float, qty: float) -> dict[str, Any] | None:
        """Докупка (ступень лестницы) в существующую позицию: qty суммируется, entry —
        средневзвешенная по количеству. Комиссия в проекте берётся в P&L пропорционально
        стоимости (position_pnl/sell: entry×(1+fee)), поэтому средняя по сырым ценам +
        этот учёт = ровно средняя с комиссиями; вшивать fee в entry — считать её дважды.
        base_low, entry_ts, hwm и realized не меняются; initial_qty растёт (доли лестницы
        paper-исполнителя считаются от всей набранной позиции)."""
        pos = self.get(position_id)
        if not pos or pos["status"] != "open" or price <= 0 or qty <= 0:
            return None
        new_qty = pos["qty"] + qty
        entry = (pos["entry_price"] * pos["qty"] + price * qty) / new_qty
        self.conn.execute(
            "UPDATE positions SET entry_price=?, qty=?, initial_qty=COALESCE(initial_qty,0)+?, "
            "hwm=MAX(COALESCE(hwm,0), ?) WHERE id=?",
            (entry, new_qty, qty, entry, position_id))
        self.record_event(position_id, "merge", price,
                          f"ступень +{qty:.6g} @ {price:.6g}: средняя "
                          f"{pos['entry_price']:.6g} → {entry:.6g}, кол-во {new_qty:.6g}")
        return self.get(position_id)

    def has_open_for(self, coin_id: str, symbol: str = "") -> bool:
        """Есть ли уже открытая позиция по монете (paper или реальная). Близнецы A/B-тестов
        (variant B/S) не считаются: S со стопом −50% живёт дольше A и иначе блокировал бы
        переоткрытие основной книги."""
        cur = self.conn.execute(
            "SELECT 1 FROM positions WHERE status='open' AND COALESCE(variant,'A')='A' AND "
            "((coin_id != '' AND coin_id=?) OR (?!='' AND symbol=?)) LIMIT 1",
            (coin_id, symbol.upper(), symbol.upper()))
        return cur.fetchone() is not None

    def count_open_paper(self) -> int:
        """Открытые paper-позиции без близнецов B/S (лимит paper_max_open — по монетам)."""
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status='open' AND is_paper=1 "
            "AND COALESCE(variant,'A')='A'")
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
                "ORDER BY is_paper ASC, COALESCE(variant,'A') ASC, id ASC", (ref.upper(),))
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None

    # --- снапшоты (дневная история P&L — для дайджеста/недельной сводки/анализа) ---
    def snapshot(self, position_id: int, price: float, pnl_pct: float, hwm: float,
                 close_ts: float | None = None, ts: float | None = None) -> None:
        """Снапшот на дневное закрытие close_ts. Повтор того же закрытия и старый снапшот
        того же дня без close_ts заменяются (переоценка после миграции не двоит историю)."""
        if close_ts is not None:
            self.conn.execute(
                f"DELETE FROM position_snapshots WHERE position_id=? AND (close_ts=? OR "
                f"(close_ts IS NULL AND {_SNAP_KEY}=?))",
                (position_id, close_ts, int(close_ts)))
        self.conn.execute(
            "INSERT INTO position_snapshots(position_id, ts, price, pnl_pct, hwm, close_ts) "
            "VALUES (?,?,?,?,?,?)",
            (position_id, ts if ts is not None else time.time(), price, pnl_pct, hwm, close_ts))
        self._commit()

    def last_snapshots(self) -> dict[int, dict[str, Any]]:
        """position_id -> последний по дню закрытия снапшот (без сети — сводки)."""
        cur = self.conn.execute(f"SELECT * FROM position_snapshots "
                                f"ORDER BY position_id, {_SNAP_KEY}, ts, rowid")
        out: dict[int, dict[str, Any]] = {}
        for r in cur.fetchall():
            d = _row_to_dict(cur, r)
            out[d["position_id"]] = d
        return out

    def snapshot_prices(self, position_id: int) -> list[tuple[float, float]]:
        """[(день закрытия, цена)] всех снапшотов позиции по порядку — спарклайн сводки дня."""
        cur = self.conn.execute(f"SELECT {_SNAP_KEY}, price FROM position_snapshots "
                                f"WHERE position_id=? AND price IS NOT NULL "
                                f"ORDER BY {_SNAP_KEY}, ts, rowid", (position_id,))
        return [(r[0], r[1]) for r in cur.fetchall()]

    def closed_since(self, ts: float) -> list[dict[str, Any]]:
        """Позиции, закрытые с момента ts (paper, выбитые сигналом в сегодняшнем watch)."""
        cur = self.conn.execute("SELECT * FROM positions WHERE status='closed' AND closed_ts>=? "
                                "ORDER BY id", (ts,))
        return [_row_to_dict(cur, r) for r in cur.fetchall()]

    def events_since(self, ts: float) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT e.*, p.symbol, p.is_paper, COALESCE(p.variant,'A') AS variant "
            "FROM position_events e "
            "JOIN positions p ON p.id=e.position_id WHERE e.ts>=? ORDER BY e.ts", (ts,))
        return [_row_to_dict(cur, r) for r in cur.fetchall()]

    def first_paper_ts(self) -> float | None:
        """Время самой ранней paper-позиции — точка отсчёта наблюдения."""
        cur = self.conn.execute(
            "SELECT MIN(entry_ts) FROM positions WHERE is_paper=1")
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else None

    # --- исполнения биржи (run.py sync: идемпотентность по exec_id) ---
    def has_fill(self, exec_id: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM exchange_fills WHERE exec_id=?", (exec_id,))
        return cur.fetchone() is not None

    def record_fill(self, f: dict[str, Any], position_id: int | None, note: str = "") -> None:
        """Записать исполнение (после того как оно применено к позиции). Повтор exec_id —
        IntegrityError: вызывающий проверяет has_fill заранее."""
        self.conn.execute(
            "INSERT INTO exchange_fills(exec_id, venue, symbol, side, price, qty, fee_usdt, "
            "base_delta, ts, order_id, position_id, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["exec_id"], f["venue"], f["symbol"], f["side"], f["price"], f["qty"],
             f["fee_usdt"], f["base_delta"], f["ts"], f.get("order_id", ""), position_id, note))
        self._commit()

    def fills(self, position_id: int | None = None) -> list[dict[str, Any]]:
        if position_id is None:
            cur = self.conn.execute("SELECT * FROM exchange_fills ORDER BY ts, exec_id")
        else:
            cur = self.conn.execute("SELECT * FROM exchange_fills WHERE position_id=? "
                                    "ORDER BY ts, exec_id", (position_id,))
        return [_row_to_dict(cur, r) for r in cur.fetchall()]

    # --- системные флаги (идемпотентность одноразовых событий, position_id=0) ---
    def system_flag(self, name: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM position_events WHERE position_id=0 AND type=? LIMIT 1", (name,))
        return cur.fetchone() is not None

    def set_system_flag(self, name: str, note: str = "", ts: float | None = None) -> None:
        self.conn.execute(
            "INSERT INTO position_events(position_id, ts, type, note) VALUES (0,?,?,?)",
            (ts if ts is not None else time.time(), name, note))
        self._commit()

    def set_watch_state(self, position_id: int, last_close_ts: float, hwm: float,
                        trail_armed_ts: float | None) -> None:
        """Итог оценки закрытия: что оценено, максимум, защёлка трейла."""
        self.conn.execute("UPDATE positions SET last_close_ts=?, hwm=?, trail_armed_ts=? "
                          "WHERE id=?", (last_close_ts, hwm, trail_armed_ts, position_id))
        self._commit()

    # --- очередь уведомлений (доставка с повтором) ---
    def enqueue(self, kind: str, position_id: int | None, close_ts: float | None,
                payload: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO notify_outbox(kind, position_id, close_ts, payload, created_ts) "
            "VALUES (?,?,?,?,?)",
            (kind, position_id, close_ts,
             json.dumps(payload, ensure_ascii=False, default=_json_default), time.time()))
        self._commit()
        return int(cur.lastrowid)

    def outbox_pending(self, kind: str) -> list[dict[str, Any]]:
        """Недоставленные по порядку: [{id, position_id, close_ts, tries, payload}]."""
        cur = self.conn.execute(
            "SELECT id, position_id, close_ts, tries, payload FROM notify_outbox "
            "WHERE kind=? AND sent_ts IS NULL ORDER BY id", (kind,))
        out = []
        for r in cur.fetchall():
            d = _row_to_dict(cur, r)
            try:
                d["payload"] = json.loads(d["payload"])
            except ValueError:
                d["payload"] = {}
            out.append(d)
        return out

    def outbox_count(self, kind: str) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind=? AND "
                                "sent_ts IS NULL", (kind,))
        return int(cur.fetchone()[0])

    def outbox_mark(self, item_id: int, delivered: bool, error: str = "") -> None:
        if delivered:
            self.conn.execute("UPDATE notify_outbox SET sent_ts=?, tries=tries+1, last_error='' "
                              "WHERE id=?", (time.time(), item_id))
        else:
            self.conn.execute("UPDATE notify_outbox SET tries=tries+1, last_error=? WHERE id=?",
                              (error[:300], item_id))
        self._commit()

    def update_hwm(self, position_id: int, hwm: float) -> None:
        self.conn.execute("UPDATE positions SET hwm=? WHERE id=?", (hwm, position_id))
        self._commit()

    def set_base_low(self, position_id: int, base_low: float) -> None:
        self.conn.execute("UPDATE positions SET base_low=? WHERE id=?",
                          (base_low, position_id))
        self._commit()

    def get(self, position_id: int) -> dict[str, Any] | None:
        cur = self.conn.execute("SELECT * FROM positions WHERE id=?", (position_id,))
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None

    def sell(self, position_id: int, sold_qty: float, price: float, reason: str,
             note: str = "", fee: float = 0.0015, close_ts: float | None = None) -> float:
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
                          note or f"продано {sold_qty:.6g} @ {price:.6g}, realized {realized:+.2f}",
                          close_ts=close_ts)
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
            "AND COALESCE(variant,'A')='A' "
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
                     price: float | None = None, note: str = "",
                     close_ts: float | None = None) -> None:
        """close_ts — дневное закрытие, на котором сработал сигнал (watch); ручные и sync — None."""
        self.conn.execute(
            "INSERT INTO position_events(position_id, ts, type, price, note, close_ts) "
            "VALUES (?,?,?,?,?,?)", (position_id, time.time(), etype, price, note, close_ts))
        self._commit()

    def event_types(self, position_id: int) -> set[str]:
        cur = self.conn.execute(
            "SELECT DISTINCT type FROM position_events WHERE position_id=?", (position_id,))
        return {r[0] for r in cur.fetchall()}

    def events(self, position_id: int) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM position_events WHERE position_id=? ORDER BY ts", (position_id,))
        return [_row_to_dict(cur, r) for r in cur.fetchall()]

    def ab_pairs(self, variant: str = "B") -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Пары (A, близнец) paper для сравнения выхода: twin.twin_of = A.id.
        variant: B — сужение трейла при перегреве, S — стоп −50% (монеты фильтра качества)."""
        cur = self.conn.execute(
            "SELECT * FROM positions WHERE is_paper=1 AND variant=? AND twin_of IS NOT NULL "
            "ORDER BY id", (variant,))
        pairs = []
        for b in [_row_to_dict(cur, r) for r in cur.fetchall()]:
            a = self.get(b["twin_of"])
            if a:
                pairs.append((a, b))
        return pairs

    def close_db(self) -> None:
        self.conn.close()


def _json_default(o):
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"{type(o).__name__} не сериализуется")


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
