"""Шаг 2 автоматизации: пробный исполнитель (dry-run) — что поставил бы, без единого ордера.

Зачем: прежде чем доверять деньги автомату (шаг 3 — демо-счёт, шаг 4 — живой счёт с
потолками), проверить на живом рынке механику исполнения и собрать данные о двух выходах:
  R «правила» — stage8_exit целиком (evaluate_exit: стоп, +50/+150 по трети, трейл);
  H «держать» — без частичных продаж и трейла, только аварийный стоп (тот же evaluate_exit,
     из сигналов берётся только инвалидация; ширина — executor.books.H.stop_pct).
Выбор стопа H — backtest/hold_stop_study.py (06.10.2026: −25%, как у R: шире — медиана не
лучше, хвост тяжелее). Книга v1 отстала от «держать те же монеты» на 9 п.п. из-за выходов,
а на истории «держать» хуже правил по медиане — книга H нужна как живые данные.

Правила:
  • вход — монеты, пришедшие сегодня карточкой (alert_log ∩ watchlist.json); лестница —
    ladder.plan_ladder (5 × $10: ступень 1 рынком, 2–5 лимитками до 1.05 × пола стопа), шаги
    цены и количества и минимумы — из /v5/market/instruments-info; ST и не Trading — отказ;
  • книги получают монету парно (обе или ни одна): разница R−H — только выходы;
  • проверки на входе (отказ пишется в журнал с причиной): лимит монет в книге, свободный USDT
    виртуального счёта книги (capital_usdt + выручка − траты − резерв лимиток), risk_check
    (лимит позиции и worst-case просадка по капиталу книги). Реальный баланс USDT (ключ
    Read-Only) — только справкой в лог: «для реального режима хватило бы / нет»;
  • журнал «поставил бы»: dry_orders с детерминированным orderLinkId ≤ 36 символов
    (dry-<книга><позиция>-B<n> покупки, dry-<книга><позиция>-S-<L0|L1|TR|INV> продажи);
    позиция уникальна по (книга, пара, день карточки) — повтор запуска ничего не дублирует;
  • исполнение: ступень 1 — по цене запуска, комиссия + спред 0.15% (в монете); лимитка —
    если low часовой свечи, начавшейся после постановки, СТРОГО ниже цены (касание не
    гарантирует место в очереди; fill_on_touch — касание), по цене лимитки, 0.1%; лимитки
    живут buy_valid_days (как в ladder_dca_study), потом снимаются;
  • выходы — по закрытым дневным свечам Bybit, после исполнений того же дня: сигнал
    исполняется по закрытию, 0.15%; доли лестницы — от всего купленного (как paper в watch);
    остаток дешевле минимума биржи продаётся вместе с долей; полный выход снимает лимитки;
  • P&L = выручка + остаток × последнее закрытие × (1 − 0.15%) − потрачено.
Ордеров на биржу модуль не отправляет: в нём нет ни одного POST и ни одного приватного вызова.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from typing import Any

from .config import Config
from .ladder import LIMIT_FEE, MARKET_FEE, plan_ladder
from .positions import risk_check
from .stages.exit import compute_base_low, evaluate_exit

DAY = 86400
HOUR = 3600
LINK_MAX = 36                     # предел orderLinkId Bybit V5
FULL_EXIT = ("invalidation", "trailing")
SIGNAL_LABEL = {"invalidation": "стоп", "trailing": "трейл"}

DEFAULTS: dict[str, Any] = {
    "enabled": True, "steps": 5, "budget_usdt": 50.0, "min_order_usdt": 10.0,
    "max_coins": 15, "capital_usdt": 1500.0, "buy_valid_days": 120,
    "fill_on_touch": False, "skip_st": True,
    "books": {"R": {"label": "правила", "emoji": "📏", "exits": "rules", "stop_pct": 25},
              "H": {"label": "держать", "emoji": "✋", "exits": "stop_only", "stop_pct": 25}},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dry_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    book          TEXT NOT NULL,        -- R | H
    pair          TEXT NOT NULL,        -- LINKUSDT
    symbol        TEXT NOT NULL,
    coin_id       TEXT DEFAULT '',
    card_day      INTEGER NOT NULL,     -- день карточки, 00:00 UTC — ключ идемпотентности
    status        TEXT NOT NULL,        -- open | closed | rejected
    reason        TEXT DEFAULT '',      -- причина отказа или выхода
    created_ts    REAL NOT NULL,
    base_low      REAL,
    stop_pct      REAL,
    min_amt       REAL DEFAULT 5,       -- минимальный ордер биржи, USDT
    qty           REAL DEFAULT 0,       -- монет на счёте (комиссии покупок — в монете)
    bought_qty    REAL DEFAULT 0,       -- всего куплено: доли лестницы продаж — от него
    spent_usdt    REAL DEFAULT 0,
    proceeds_usdt REAL DEFAULT 0,       -- выручка продаж за вычетом комиссии
    hwm           REAL,
    last_price    REAL,                 -- последнее обработанное дневное закрытие
    last_day      INTEGER,              -- его день (00:00 UTC)
    fills_until   REAL DEFAULT 0,       -- часовые свечи до этого времени уже просмотрены
    first_fill_ts REAL,
    closed_ts     REAL,
    UNIQUE(book, pair, card_day)
);
CREATE TABLE IF NOT EXISTS dry_orders (
    link_id     TEXT PRIMARY KEY,       -- orderLinkId: тот же id ушёл бы на биржу
    position_id INTEGER NOT NULL,
    book        TEXT NOT NULL,
    pair        TEXT NOT NULL,
    side        TEXT NOT NULL,          -- Buy | Sell
    kind        TEXT NOT NULL,          -- Market | Limit
    step        INTEGER,                -- ступень покупки 1..N
    signal      TEXT DEFAULT '',        -- продажа: тип сигнала (ladder_0, trailing, …)
    price       REAL,
    qty         REAL,
    usd         REAL,
    status      TEXT NOT NULL,          -- new | filled | cancelled
    created_ts  REAL NOT NULL,
    filled_ts   REAL,
    fee_usdt    REAL DEFAULT 0,
    note        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_dry_orders_pos ON dry_orders(position_id, side, status);
"""


# ---------------------------------------------------------------- настройки и хранилище

def settings(cfg) -> dict[str, Any]:
    s = {**DEFAULTS, **(cfg.get("executor", {}) or {})}
    s["books"] = {k: {**DEFAULTS["books"].get(k, {}), **v}
                  for k, v in (s.get("books") or DEFAULTS["books"]).items()
                  if not str(k).startswith("_")}
    return s


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    con.commit()
    return con


def link_id(book: str, pid: int, tail: str) -> str:
    """Детерминированный orderLinkId: позиция + ступень/сигнал. Повтор запроса с тем же id
    биржа отвергнет, а не исполнит второй раз (bybit-private-api-facts)."""
    lid = f"dry-{book}{pid}-{tail}"
    if len(lid) > LINK_MAX:
        raise ValueError(f"orderLinkId длиннее {LINK_MAX}: {lid}")
    return lid


def signal_tail(stype: str) -> str:
    if stype.startswith("ladder_"):
        return f"S-L{stype.split('_')[1]}"
    return "S-" + {"invalidation": "INV", "trailing": "TR"}.get(stype, stype[:8].upper())


def utc_day(ts: float) -> int:
    return int(ts // DAY) * DAY


def local_day_start(now: float) -> float:
    d = datetime.fromtimestamp(now)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def pnl_usdt(p: dict, price: float | None = None) -> float:
    """Полный P&L позиции net-of-fees: выручка + остаток по закрытию − потрачено."""
    price = price if price is not None else (p.get("last_price") or 0.0)
    return (p.get("proceeds_usdt") or 0.0) + (p.get("qty") or 0.0) * price * (1 - MARKET_FEE) \
        - (p.get("spent_usdt") or 0.0)


def _rows(con, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def reserved_usdt(con, book: str) -> float:
    """Резерв неисполненных лимиток книги (на бирже эти USDT заблокированы)."""
    r = con.execute("SELECT COALESCE(SUM(usd),0) FROM dry_orders WHERE book=? AND side='Buy' "
                    "AND status='new'", (book,)).fetchone()
    return float(r[0] or 0.0)


def free_usdt(con, book: str, capital: float) -> float:
    """Свободный USDT виртуального счёта книги: капитал + выручка − траты − резерв лимиток."""
    r = con.execute("SELECT COALESCE(SUM(proceeds_usdt - spent_usdt),0) FROM dry_positions "
                    "WHERE book=? AND status!='rejected'", (book,)).fetchone()
    return capital + float(r[0] or 0.0) - reserved_usdt(con, book)


def book_exposure(con, book: str) -> list[dict]:
    """Открытые позиции книги для risk_check: стоимость = остаток × закрытие + резерв лимиток
    (entry_price = 1, qty = $ — risk_check берёт entry × qty)."""
    out = []
    for p in _rows(con, "SELECT * FROM dry_positions WHERE book=? AND status='open'", (book,)):
        px = p["last_price"] or (p["spent_usdt"] / p["bought_qty"] if p["bought_qty"] else 0.0)
        res = con.execute("SELECT COALESCE(SUM(usd),0) FROM dry_orders WHERE position_id=? AND "
                          "side='Buy' AND status='new'", (p["id"],)).fetchone()[0] or 0.0
        out.append({"id": p["id"], "entry_price": 1.0, "qty": p["qty"] * px + res,
                    "is_paper": 0})
    return out


def entry_checks(con, cfg, s: dict, book: str, need_usd: float) -> list[str]:
    """Причины отказа книге (пусто — можно): лимит монет, свободный USDT, risk_check."""
    why = []
    n_open = con.execute("SELECT COUNT(*) FROM dry_positions WHERE book=? AND status='open'",
                         (book,)).fetchone()[0]
    if n_open >= s["max_coins"]:
        why.append(f"лимит {s['max_coins']} монет")
    free = free_usdt(con, book, s["capital_usdt"])
    if free + 1e-9 < need_usd:
        why.append(f"нехватка USDT: свободно {free:.2f} < нужно {need_usd:.2f}")
    rc = Config({"stage7_positions": {**(cfg.get("stage7_positions", {}) or {}),
                                      "capital_usdt": s["capital_usdt"]}})
    why += risk_check(book_exposure(con, book), need_usd, rc)
    return why


# ---------------------------------------------------------------- рынок (сеть)

class BybitMarket:
    """Публичные данные Bybit spot для исполнителя. В selftest подменяется фикстурой с теми же
    методами: instrument, last_price, daily (закрытые дневные), hourly (закрытые часовые)."""

    def __init__(self, http):
        from .sources import bybit as src
        self.http, self._src = http, src

    def instrument(self, pair: str) -> dict | None:
        return self._src.fetch_instrument(self.http, pair)

    def last_price(self, pair: str) -> float | None:
        return self._src.fetch_last_price(self.http, pair)

    def daily(self, pair: str) -> dict:
        return self._src.fetch_daily_ohlcv(self.http, pair, 400)

    def hourly(self, pair: str, start_ts: float) -> dict:
        return self._src.fetch_klines(self.http, pair, "60", int(start_ts * 1000))


# ---------------------------------------------------------------- вход

def todays_cards(cfg, con, now: float) -> list[dict]:
    """Монеты, пришедшие сегодня карточкой: alert_log с начала местного дня, coin_id — из
    watchlist.json (может не найтись: карточка из вчерашнего списка — не страшно)."""
    t0 = local_day_start(now)
    try:
        rows = con.execute("SELECT symbol, MIN(ts), MAX(score) FROM alert_log WHERE ts>=? "
                           "GROUP BY symbol ORDER BY MIN(ts)", (t0,)).fetchall()
    except sqlite3.OperationalError:          # нет alert_log — скан ещё ни разу не шёл
        return []
    wl = {}
    try:
        from .pipeline import load_watchlist
        wl = {(c.symbol or "").upper(): c for c in load_watchlist(cfg["output"]["watchlist_json"])}
    except Exception:  # noqa: BLE001 — без watchlist coin_id просто пустой
        pass
    out = []
    for sym, ts, score in rows:
        c = wl.get((sym or "").upper())
        out.append({"symbol": (sym or "").upper(), "ts": ts, "score": score or 0.0,
                    "coin_id": getattr(c, "coin_id", "") or ""})
    return out


def _insert_position(con, book: str, card: dict, pair: str, now: float, status: str,
                     reason: str = "", **kw) -> int | None:
    cur = con.execute(
        "INSERT OR IGNORE INTO dry_positions(book, pair, symbol, coin_id, card_day, status, "
        "reason, created_ts, base_low, stop_pct, min_amt) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (book, pair, card["symbol"], card.get("coin_id", ""), utc_day(card["ts"]), status,
         reason, now, kw.get("base_low"), kw.get("stop_pct"), kw.get("min_amt", 5.0)))
    return int(cur.lastrowid) if cur.rowcount else None


def open_card(cfg, con, market, s: dict, card: dict, now: float) -> list[str]:
    """Лестница по карточке — в обе книги или отказ обеим (с причиной в журнале)."""
    sym = card["symbol"]
    pair = f"{sym}USDT"
    books = list(s["books"])
    day = utc_day(card["ts"])
    done = con.execute("SELECT COUNT(*) FROM dry_positions WHERE pair=? AND card_day=?",
                       (pair, day)).fetchone()[0]
    if done:
        return [f"{sym}: карточка уже обработана — повтор не дублирует"]
    held = con.execute("SELECT book, created_ts FROM dry_positions WHERE pair=? AND "
                       "status='open' LIMIT 1", (pair,)).fetchone()
    if held:
        return [f"{sym}: уже в книге {held[0]} с {time.strftime('%d.%m', time.localtime(held[1]))}"
                f" — вторую лестницу не ставлю"]

    def reject(reason: str) -> list[str]:
        for b in books:
            _insert_position(con, b, card, pair, now, "rejected", reason)
        con.commit()
        return [f"{sym}: ОТКАЗ — {reason}"]

    inst = market.instrument(pair)
    if not inst:
        return reject(f"{pair} нет на Bybit spot")
    if inst.get("status") != "Trading" or (s["skip_st"] and inst.get("st")):
        return reject(f"Bybit: статус {inst.get('status')}{', метка ST' if inst.get('st') else ''}")
    price = market.last_price(pair)
    closes = (market.daily(pair) or {}).get("c") or []
    base_low = compute_base_low(closes, 30)
    if not price or not base_low:
        return [f"{sym}: нет цены или истории закрытий Bybit — пропуск до следующего прогона"]
    e = cfg["stage8_exit"]
    plan = plan_ladder(price, base_low, s["budget_usdt"], steps=s["steps"],
                       min_order=s["min_order_usdt"], tick=inst["tick"],
                       qty_step=inst["qty_step"], exch_min_amt=inst["min_amt"],
                       exch_min_qty=inst["min_qty"],
                       floor_pct=e["invalidation_below_base_low_pct"], sell="prod",
                       prod_levels=e["ladder"])
    if not plan["ok"]:
        return reject(plan["error"])
    why = {b: entry_checks(con, cfg, s, b, plan["spent"]) for b in books}
    if any(why.values()):
        return reject("; ".join(f"{b}: {', '.join(w)}" for b, w in why.items() if w))

    lines = []
    for b in books:
        pid = _insert_position(con, b, card, pair, now, "open", base_low=base_low,
                               stop_pct=s["books"][b]["stop_pct"], min_amt=inst["min_amt"])
        if pid is None:
            continue
        for o in plan["buys"]:
            market_step = o["type"] == "рынок"
            con.execute(
                "INSERT OR IGNORE INTO dry_orders(link_id, position_id, book, pair, side, kind, "
                "step, price, qty, usd, status, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (link_id(b, pid, f"B{o['n']}"), pid, b, pair, "Buy",
                 "Market" if market_step else "Limit", o["n"], o["price"], o["qty"], o["usd"],
                 "new", now))
        mkt = con.execute("SELECT * FROM dry_orders WHERE position_id=? AND kind='Market' "
                          "AND status='new'", (pid,)).fetchone()
        if mkt:
            _fill_buy(con, dict(mkt), price, MARKET_FEE, now)
        con.commit()
    lims = ", ".join(f"{o['price']:.6g}" for o in plan["buys"][1:])
    lines.append(f"{sym}: поставил бы в книги {'/'.join(books)} — {len(plan['buys'])} ступ. "
                 f"на ${plan['spent']:.2f}: рынок ~{price:.6g}" + (f", лимитки {lims}" if lims else "")
                 + f"; лоу базы {base_low:.6g}")
    lines += [f"  ⚠ {w}" for w in plan["warns"]]
    return lines


# ---------------------------------------------------------------- исполнение и выходы

def _fill_buy(con, order: dict, price: float, fee: float, ts: float) -> None:
    q_net = order["qty"] * (1 - fee)
    usd = order["qty"] * price
    con.execute("UPDATE dry_orders SET status='filled', filled_ts=?, price=?, usd=?, fee_usdt=? "
                "WHERE link_id=? AND status='new'", (ts, price, usd, usd * fee, order["link_id"]))
    con.execute("UPDATE dry_positions SET qty=qty+?, bought_qty=bought_qty+?, "
                "spent_usdt=spent_usdt+?, first_fill_ts=COALESCE(first_fill_ts, ?) WHERE id=?",
                (q_net, q_net, usd, ts, order["position_id"]))


def _sell(con, p: dict, stype: str, qty: float, price: float, ts: float, note: str) -> float:
    qty = min(qty, p["qty"])
    if qty <= 0:
        return 0.0
    lid = link_id(p["book"], p["id"], signal_tail(stype))
    if con.execute("SELECT 1 FROM dry_orders WHERE link_id=?", (lid,)).fetchone():
        return 0.0                               # этот сигнал уже исполнен
    usd = qty * price
    con.execute("INSERT INTO dry_orders(link_id, position_id, book, pair, side, kind, signal, "
                "price, qty, usd, status, created_ts, filled_ts, fee_usdt, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lid, p["id"], p["book"], p["pair"], "Sell", "Market", stype, price, qty, usd,
                 "filled", ts, ts, usd * MARKET_FEE, note))
    con.execute("UPDATE dry_positions SET qty=MAX(0, qty-?), proceeds_usdt=proceeds_usdt+? "
                "WHERE id=?", (qty, usd * (1 - MARKET_FEE), p["id"]))
    _reload(con, p)
    return usd * (1 - MARKET_FEE)


def _cancel_buys(con, pid: int, why: str) -> int:
    cur = con.execute("UPDATE dry_orders SET status='cancelled', note=? WHERE position_id=? "
                      "AND side='Buy' AND status='new'", (why, pid))
    return cur.rowcount


def process_position(cfg, con, market, s: dict, pos: dict, now: float) -> list[str]:
    """Исполнения лимиток по часовым свечам и выходы по дневным закрытиям с прошлого раза."""
    p = dict(pos)
    pair, book = p["pair"], p["book"]
    bk = s["books"].get(book) or {}
    lines: list[str] = []
    daily = market.daily(pair) or {}
    d_ts, d_c = daily.get("ts") or [], daily.get("c") or []
    pending = _rows(con, "SELECT * FROM dry_orders WHERE position_id=? AND side='Buy' AND "
                         "status='new' ORDER BY step", (p["id"],))
    expire_at = p["created_ts"] + s["buy_valid_days"] * DAY
    start = max(p["fills_until"] or 0.0, p["created_ts"])
    hours: list[tuple[int, float]] = []
    if pending:
        hk = market.hourly(pair, start) or {}
        hours = [(t, lo) for t, lo in zip(hk.get("ts") or [], hk.get("l") or [])
                 if t >= start and t + HOUR <= now]
    hi = 0
    touch = bool(s.get("fill_on_touch"))

    def fills_until(limit_ts: float) -> None:
        """Часовые свечи, начавшиеся до limit_ts: лимитка исполнена, если low прошёл сквозь."""
        nonlocal hi
        while hi < len(hours) and hours[hi][0] < limit_ts:
            t, lo = hours[hi]
            hi += 1
            if t >= expire_at:
                continue
            for o in pending:
                if o["status"] == "new" and (lo < o["price"] or (touch and lo <= o["price"])):
                    o["status"] = "filled"
                    _fill_buy(con, o, o["price"], LIMIT_FEE, t + HOUR)
                    lines.append(f"{book} {p['symbol']}: исполнилась бы ступень {o['step']} "
                                 f"по {o['price']:.6g}")
            p["fills_until"] = t + HOUR
        _reload(con, p)

    ecfg = Config({"stage8_exit": {**cfg["stage8_exit"],
                                   "invalidation_below_base_low_pct": bk.get("stop_pct", 25)}})
    triggered = {r["signal"] for r in _rows(con, "SELECT signal FROM dry_orders WHERE "
                                                 "position_id=? AND side='Sell'", (p["id"],))}
    closed = False
    for i, (day, close) in enumerate(zip(d_ts, d_c)):
        if day + DAY <= p["created_ts"] or (p["last_day"] is not None and day <= p["last_day"]):
            continue
        fills_until(day + DAY)
        p["last_day"], p["last_price"] = day, close
        if p["qty"] <= 0:
            continue
        p["hwm"] = max(p["hwm"] or 0.0, close)
        avg = p["spent_usdt"] / p["bought_qty"]
        sigs = evaluate_exit({"entry_price": avg, "base_low": p["base_low"], "variant": "A"},
                             close, p["hwm"], None, triggered, ecfg, recent_closes=d_c[:i + 1])
        if bk.get("exits") == "stop_only":
            sigs = [x for x in sigs if x["type"] == "invalidation"]
        for sig in sigs:
            stype = sig["type"]
            if stype in FULL_EXIT:
                q = p["qty"]
            elif stype.startswith("ladder_"):
                idx = int(stype.split("_")[1])
                lad = cfg["stage8_exit"]["ladder"]
                q = min(p["qty"], (lad[idx][1] if idx < len(lad) else 0.0) * p["bought_qty"])
                if (p["qty"] - q) * close < (p["min_amt"] or 5.0):
                    q = p["qty"]                 # остаток меньше минимума биржи — вместе с долей
            else:
                continue                         # информационные сигналы ордеров не дают
            got = _sell(con, p, stype, q, close, day + DAY, sig["note"])
            triggered.add(stype)
            lines.append(f"{book} {p['symbol']}: продал бы {q:.6g} по закрытию {close:.6g} "
                         f"({SIGNAL_LABEL.get(stype, stype)}) → ${got:.2f}")
            if p["qty"] <= 1e-12:
                p["qty"] = 0.0
                closed = True
                p["status"], p["closed_ts"] = "closed", day + DAY
                p["reason"] = SIGNAL_LABEL.get(stype, stype)
                n = _cancel_buys(con, p["id"], "позиция закрыта")
                pending = []
                if n:
                    lines.append(f"{book} {p['symbol']}: снял бы неисполненные лимитки ({n})")
                break
        _save(con, p)
        if closed:
            break
    if not closed:
        fills_until(now)
        if pending and now >= expire_at:
            n = _cancel_buys(con, p["id"], f"срок {s['buy_valid_days']} дн.")
            if n:
                lines.append(f"{book} {p['symbol']}: снял бы лимитки по сроку ({n})")
    _save(con, p)
    con.commit()
    return lines


def _reload(con, p: dict) -> None:
    """Количество и деньги — всегда из БД (покупки и продажи пишутся туда сразу); в p
    остаются только поля прогона (hwm, последний день, курсор свечей, статус)."""
    r = con.execute("SELECT qty, bought_qty, spent_usdt, proceeds_usdt, first_fill_ts "
                    "FROM dry_positions WHERE id=?", (p["id"],)).fetchone()
    p["qty"], p["bought_qty"], p["spent_usdt"], p["proceeds_usdt"], p["first_fill_ts"] = \
        r[0], r[1], r[2], r[3], r[4]


def _save(con, p: dict) -> None:
    con.execute("UPDATE dry_positions SET hwm=?, last_price=?, last_day=?, fills_until=?, "
                "status=?, closed_ts=?, reason=? WHERE id=?",
                (p["hwm"], p["last_price"], p["last_day"], p["fills_until"], p["status"],
                 p["closed_ts"], p["reason"], p["id"]))


# ---------------------------------------------------------------- шаг прогона

def run_dry(cfg, market, *, db_path: str | None = None, now: float | None = None,
            wallet_usdt: float | None = None, wallet_note: str = "") -> tuple[int, list[str]]:
    """Шаг ежедневного прогона -> (код выхода, строки лога). Сбой одной позиции не валит
    остальные, но код станет 1 (сводка дня: «⚠ пробный исполнитель упал»)."""
    s = settings(cfg)
    if not s.get("enabled", True):
        return 0, ["executor.enabled = false — пропуск"]
    now = now if now is not None else time.time()
    con = connect(db_path or cfg["output"]["db_path"])
    lines: list[str] = []
    code = 0
    try:
        for pos in _rows(con, "SELECT * FROM dry_positions WHERE status='open' ORDER BY id"):
            try:
                lines += process_position(cfg, con, market, s, pos, now)
            except Exception as e:  # noqa: BLE001 — одна позиция не валит остальные
                con.rollback()
                code = 1
                lines.append(f"⚠ {pos['book']} {pos['symbol']}: {type(e).__name__}: {e}")
        cards = todays_cards(cfg, con, now)
        need = 0.0
        for card in cards:
            try:
                got = open_card(cfg, con, market, s, card, now)
            except Exception as e:  # noqa: BLE001
                con.rollback()
                code = 1
                got = [f"⚠ {card['symbol']}: {type(e).__name__}: {e}"]
            lines += got
            if any("поставил бы" in x for x in got):
                need += s["budget_usdt"]
        if not cards:
            lines.append("сегодня карточек не было — новых лестниц нет")
        if wallet_usdt is not None:
            enough = "хватило бы" if wallet_usdt >= need else "НЕ хватило бы"
            lines.append(f"реальный счёт: свободно {wallet_usdt:.2f} USDT; сегодняшние лестницы "
                         f"одной книги — ${need:.2f} → для реального режима {enough}")
        elif wallet_note:
            lines.append(wallet_note)
        for b, bk in s["books"].items():
            st = book_summary(con, b)
            lines.append(f"книга {b} «{bk['label']}»: открыто {st['open']}, вложено "
                         f"${st['spent']:.2f}, P&L {st['pnl']:+.2f} USDT, свободно "
                         f"{free_usdt(con, b, s['capital_usdt']):.2f}")
    finally:
        con.close()
    return code, lines


# ---------------------------------------------------------------- сводки (без сети)

def book_summary(con, book: str) -> dict[str, Any]:
    ps = _rows(con, "SELECT * FROM dry_positions WHERE book=? AND status!='rejected'", (book,))
    return {"open": sum(1 for p in ps if p["status"] == "open"),
            "closed": sum(1 for p in ps if p["status"] == "closed"),
            "spent": sum(p["spent_usdt"] for p in ps),
            "pnl": sum(pnl_usdt(p) for p in ps)}


def brief_state(cfg, now: float | None = None) -> dict | None:
    """Для сводки дня: книги и действия за сегодня. Таблиц нет — None (строки не будет)."""
    from .benchmark import connect_ro
    now = now if now is not None else time.time()
    con = connect_ro(cfg["output"]["db_path"])
    if con is None:
        return None
    con.row_factory = sqlite3.Row
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "dry_positions" not in names:
            return None
        t0 = local_day_start(now)
        s = settings(cfg)
        books = {b: {**book_summary(con, b), "label": bk["label"], "emoji": bk.get("emoji", "•")}
                 for b, bk in s["books"].items()}
        opened = [r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM dry_positions WHERE status!='rejected' AND created_ts>=?",
            (t0,))]
        rejected = [(r[0], r[1]) for r in con.execute(
            "SELECT symbol, MIN(reason) FROM dry_positions WHERE status='rejected' AND "
            "created_ts>=? GROUP BY symbol", (t0,))]
        fills = con.execute("SELECT COUNT(*) FROM dry_orders WHERE side='Buy' AND kind='Limit' "
                            "AND status='filled' AND filled_ts>=?", (t0 - DAY,)).fetchone()[0]
        sells = [(r[0], r[1], r[2]) for r in con.execute(
            "SELECT o.book, p.symbol, o.signal FROM dry_orders o JOIN dry_positions p "
            "ON p.id=o.position_id WHERE o.side='Sell' AND o.created_ts>=? ORDER BY o.created_ts",
            (t0 - DAY,))]
    finally:
        con.close()
    return {"books": books, "opened": opened, "rejected": rejected, "fills": fills,
            "sells": sells}


def outcomes(con, book: str) -> list[dict]:
    """Окна и итоги позиций книги для сравнения с рынком: start — первое исполнение, end —
    закрытие позиции или конец последнего обработанного дня; cost — потрачено."""
    out = []
    for p in _rows(con, "SELECT * FROM dry_positions WHERE book=? AND status!='rejected' "
                        "AND spent_usdt>0", (book,)):
        end = p["closed_ts"] if p["status"] == "closed" else (
            p["last_day"] + DAY if p["last_day"] is not None else None)
        if end is None or not p["first_fill_ts"]:
            continue
        out.append({"id": p["id"], "symbol": p["symbol"], "coin_id": p["coin_id"],
                    "start": p["first_fill_ts"], "end": end, "cost": p["spent_usdt"],
                    "pnl": pnl_usdt(p)})
    return out


def weekly_books(cfg) -> list[dict]:
    """Строки блока недельной сводки: каждая книга против альтов, BTC и контрольной корзины
    (все монеты watchlist прогона входа поровну, прокси — капа) на тех же окнах."""
    from . import benchmark
    db = cfg["output"]["db_path"]
    con = benchmark.connect_ro(db)
    if con is None:
        return []
    con.row_factory = sqlite3.Row
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "dry_positions" not in names:
            return []
        rows = {b: outcomes(con, b) for b in settings(cfg)["books"]}
    finally:
        con.close()
    if not any(rows.values()):
        return []
    mkt = benchmark.load_market(db)
    uni = benchmark.load_universe(db)
    s = settings(cfg)
    out = []
    for b, rs in rows.items():
        res = benchmark.compare_outcomes(rs, mkt)
        res.update(benchmark.basket_for(uni, res.get("rows") or []))
        bk = s["books"][b]
        out.append({"key": b, "label": bk["label"], "emoji": bk.get("emoji", "•"), **res})
    return out
