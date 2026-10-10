"""Шаг 3 автоматизации (блок F): демо-счёт Bybit — настоящие ордера на учебные деньги
(api-demo.bybit.com) зеркально пробному исполнителю (scanner/executor.py).

Зачем: пробный исполнитель моделирует исполнение по свечам (лимитка — если low часовой свечи
строго ниже цены, тейк — по high дня). Демо ставит те же ордера на настоящую биржу — с очередью,
спредом и фактической комиссией; расхождение «модель против биржи» и проверяется перед живыми
деньгами (шаг 4, DECISIONS U3 — здесь не включается: TradeClient работает только с демо).

Решения принимает пробный исполнитель (шаг execute идёт раньше): отбор карточек, фильтры и
тень, книги R/H, лимиты, кулдаун, стоп, трейл, делистинг. Демо их не пересчитывает, а повторяет:
  • вход — позиция основной книги пробного (не тень), открытая не раньше demo.since и не позже
    open_late_days назад: те же ступени — 1 рынком на ту же сумму USDT, 2–5 лимитками GTC по тем
    же ценам и количествам (orderLinkId dmo-<книга><id пробной позиции>-B<n>);
  • тейк (книга с exits «rules», R) — лимитки на продажу ЗАРАНЕЕ на бирже, как тейк лимиткой в
    бэктесте: цель +50/+150% от СВОЕЙ средней (вверх к tick), доля — от своего купленного
    (executor.sell_qty: шаг количества, минимум биржи); средняя сдвинулась после докупки —
    тейк переставляется (-L0v2 …); первая продажа снимает лимитки покупки;
  • стоп, трейл, делистинг — любое закрытие пробной позиции → снять все ордера позиции и продать
    остаток рынком (-X); остаток дешевле минимума биржи — «пыль, не продать»;
  • пробный снял лимитку покупки (начал продавать, срок buy_valid_days) — демо снимает свою;
  • демо-счёт хранит ордера 7 дней (docs/v5/demo): пропавшая или снятая не нами лимитка покупки
    ставится заново на неисполненный остаток (-B2v2 …), пока пробный её держит.
Количества и деньги позиции — только из ответов биржи (cumExecQty/Value, комиссия из
cumFeeDetail: покупка платит в монете, продажа — в USDT), пересчётом по всем ордерам позиции:
повтор прогона ничего не задваивает, а повтор ордера с тем же orderLinkId биржа отвергает
(170141 — тогда состояние читается с биржи).

Потолки (жёстко: сверх них ордер не уходит): max_order_usdt на одну покупку, max_day_usdt
новых покупок за UTC-день, max_coins открытых позиций в книге, max_open_usdt вложено и стоит в
лимитках по всем книгам, свободных USDT демо-счёта меньше лестницы — не входим. Продаётся только
своё (по ордерам позиции), остальные монеты счёта не трогаются. Стоп-кран (data/HALT) — ничего
не делает. Ключ проверяется каждый прогон (bybit.check_key, trade, demo): право вывода или ключ
без IP — ни одного ордера.
"""
from __future__ import annotations

import sqlite3
import time
from calendar import timegm
from typing import Any, Callable

from . import bybit
from .executor import DAY, connect as dry_connect, ddmm, pnl_usdt, sell_qty, utc_day
from .executor import settings as dry_settings
from .ladder import MARKET_FEE, fmt_step, round_step

DEFAULTS: dict[str, Any] = {
    "enabled": True, "since": "2026-10-10", "open_late_days": 2,
    "max_order_usdt": 11.0, "max_day_usdt": 250.0, "max_coins": 15, "max_open_usdt": 1600.0,
    "max_price_drift": 0.2, "recv_window_ms": 5000, "key_warn_days": 14, "settle_s": 0.5,
}
LIVE = ("sending", "new", "cancel_sent")          # ордер ещё может исполниться
OURS = "снят: "                                    # префикс заметки — ордер сняли мы
_STATUS = {"New": "new", "PartiallyFilled": "new", "Untriggered": "new", "Triggered": "new",
           "Filled": "filled", "Cancelled": "cancelled", "PartiallyFilledCanceled": "cancelled",
           "Deactivated": "cancelled", "Rejected": "rejected"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_positions (
    dry_id        INTEGER PRIMARY KEY,  -- dry_positions.id: та же позиция пробного исполнителя
    book          TEXT NOT NULL,
    pair          TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    status        TEXT NOT NULL,        -- open | closing | closed | skipped
    reason        TEXT DEFAULT '',
    created_ts    REAL NOT NULL,
    closed_ts     REAL,
    qty_step      REAL,
    tick          REAL,
    min_amt       REAL DEFAULT 5,
    qty           REAL DEFAULT 0,       -- ниже — по ордерам на конец прогона (сводка без сети)
    bought_qty    REAL DEFAULT 0,
    spent_usdt    REAL DEFAULT 0,
    proceeds_usdt REAL DEFAULT 0,
    last_price    REAL,
    updated_ts    REAL
);
CREATE TABLE IF NOT EXISTS demo_orders (
    link_id     TEXT PRIMARY KEY,       -- orderLinkId на бирже
    dry_id      INTEGER NOT NULL,
    book        TEXT NOT NULL,
    pair        TEXT NOT NULL,
    side        TEXT NOT NULL,          -- Buy | Sell
    kind        TEXT NOT NULL,          -- Market | Limit
    role        TEXT NOT NULL,          -- B1..B5 покупки, L0/L1 тейки, X полный выход
    ver         INTEGER DEFAULT 1,      -- перестановка той же роли: -B2v2, -L0v3
    price       REAL,
    qty         REAL,                   -- рыночная покупка — NULL (заявка в USDT, usd)
    usd         REAL,
    status      TEXT NOT NULL,          -- sending | new | cancel_sent | filled | cancelled |
                                        -- rejected | gone (пропал с биржи) | error
    order_id    TEXT DEFAULT '',
    cum_qty     REAL DEFAULT 0,         -- исполнено (монет, до комиссии)
    cum_value   REAL DEFAULT 0,         -- исполнено (USDT)
    fee_base    REAL DEFAULT 0,         -- комиссия в монете (покупка)
    fee_quote   REAL DEFAULT 0,         -- комиссия в USDT (продажа)
    created_ts  REAL NOT NULL,
    updated_ts  REAL,
    filled_ts   REAL,
    note        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_demo_orders_pos ON demo_orders(dry_id, role);
CREATE TABLE IF NOT EXISTS demo_runs (
    ts        REAL PRIMARY KEY,
    code      INTEGER,
    key_level TEXT,
    key_line  TEXT,
    note      TEXT DEFAULT ''
);
"""


class Skip(Exception):
    """Позицию в этот прогон не трогаем (нет цены и т. п.) — попробуем в следующий."""


def settings(cfg) -> dict[str, Any]:
    return {**DEFAULTS, **(cfg.get("demo", {}) or {})}


def since_ts(s: dict) -> float:
    return float(timegm(time.strptime(str(s["since"]), "%Y-%m-%d")))


def connect(db_path: str) -> sqlite3.Connection:
    con = dry_connect(db_path)                    # таблицы пробного (и их миграции)
    con.executescript(_SCHEMA)
    con.commit()
    return con


def link(book: str, dry_id: int, role: str, ver: int = 1) -> str:
    lid = f"dmo-{book}{dry_id}-{role}" + (f"v{ver}" if ver > 1 else "")
    if len(lid) > 36:
        raise ValueError(f"orderLinkId длиннее 36: {lid}")
    return lid


def _rows(con, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def numbers(con, dry_id: int) -> dict[str, float]:
    """Позиция по исполнениям её ордеров: bought — монет пришло (за вычетом комиссии в монете),
    spent — USDT потрачено, proceeds — USDT получено (за вычетом комиссии), qty — на счёте."""
    b = con.execute("SELECT COALESCE(SUM(cum_qty),0), COALESCE(SUM(cum_value),0), "
                    "COALESCE(SUM(fee_base),0), COALESCE(SUM(fee_quote),0) FROM demo_orders "
                    "WHERE dry_id=? AND side='Buy'", (dry_id,)).fetchone()
    s = con.execute("SELECT COALESCE(SUM(cum_qty),0), COALESCE(SUM(cum_value),0), "
                    "COALESCE(SUM(fee_base),0), COALESCE(SUM(fee_quote),0) FROM demo_orders "
                    "WHERE dry_id=? AND side='Sell'", (dry_id,)).fetchone()
    bought = b[0] - b[2]
    sold = s[0] + s[2]
    return {"bought": bought, "spent": b[1] + b[3], "sold": sold,
            "proceeds": s[1] - s[3], "qty": max(bought - sold, 0.0)}


def pnl(d: dict, price: float | None = None) -> float:
    px = price if price is not None else (d.get("last_price") or 0.0)
    return (d.get("proceeds_usdt") or 0.0) + (d.get("qty") or 0.0) * px * (1 - MARKET_FEE) \
        - (d.get("spent_usdt") or 0.0)


# ---------------------------------------------------------------- ордера

def _apply(con, o: dict, r: dict | None, now: float) -> dict:
    """Состояние ордера с биржи (r — ответ order(); None — на бирже его нет) -> строка БД."""
    base = o["pair"][:-4] if o["pair"].endswith("USDT") else o["pair"]
    if r is None:
        st = {"sending": "error", "cancel_sent": "cancelled"}.get(o["status"], "gone")
        note = {"error": "на бирже нет — запрос не дошёл",
                "gone": "пропал с биржи (демо хранит ордера 7 дней)"}.get(st, o["note"])
        con.execute("UPDATE demo_orders SET status=?, note=?, updated_ts=? WHERE link_id=?",
                    (st, note, now, o["link_id"]))
        o.update(status=st, note=note)
        return o
    st = _STATUS.get(str(r.get("orderStatus")), "new")
    if st == "new" and o["status"] == "cancel_sent":
        st = "cancel_sent"
    note = o["note"] or ""
    if st == "cancelled" and not note.startswith(OURS):
        note = f"снят биржей ({r.get('cancelType') or r.get('rejectReason') or '?'})"
    if st == "rejected":
        note = f"отклонён ({r.get('rejectReason') or '?'})"
    fee_b = fee_q = 0.0
    for coin, amt in (r.get("cumFeeDetail") or {}).items():
        if coin == base:
            fee_b += _f(amt)
        elif coin == "USDT":
            fee_q += _f(amt)
    filled_ts = o.get("filled_ts") or (now if st == "filled" else None)
    con.execute("UPDATE demo_orders SET status=?, order_id=?, cum_qty=?, cum_value=?, fee_base=?, "
                "fee_quote=?, updated_ts=?, filled_ts=?, note=? WHERE link_id=?",
                (st, str(r.get("orderId") or o.get("order_id") or ""), _f(r.get("cumExecQty")),
                 _f(r.get("cumExecValue")), fee_b, fee_q, now, filled_ts, note, o["link_id"]))
    o.update(status=st, cum_qty=_f(r.get("cumExecQty")), cum_value=_f(r.get("cumExecValue")),
             fee_base=fee_b, fee_quote=fee_q, filled_ts=filled_ts, note=note)
    return o


def refresh(con, client, o: dict, now: float) -> dict:
    return _apply(con, o, client.order(o["link_id"]), now)


def place(con, client, d: dict, *, role: str, side: str, kind: str, now: float,
          price: float | None = None, qty: float | None = None, usd: float | None = None,
          ver: int = 1) -> dict:
    """Один ордер позиции d: строка «sending» в БД (с коммитом) → POST → статус. Цена — к tick,
    количество — вниз к шагу (строкой без экспоненты); рыночная покупка — в USDT."""
    lid = link(d["book"], d["dry_id"], role, ver)
    tick, step = d.get("tick") or 0.0, d.get("qty_step") or 0.0
    if kind == "Limit":
        price = round_step(price, tick, up=side == "Sell") if tick else price
    if qty is not None:
        qty = round_step(qty, step) if step else qty
    if usd is None and price and qty:
        usd = price * qty
    con.execute("INSERT INTO demo_orders(link_id, dry_id, book, pair, side, kind, role, ver, "
                "price, qty, usd, status, created_ts, updated_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lid, d["dry_id"], d["book"], d["pair"], side, kind, role, ver, price, qty, usd,
                 "sending", now, now))
    con.commit()                          # след на случай падения между записью и ответом
    o = dict(con.execute("SELECT * FROM demo_orders WHERE link_id=?", (lid,)).fetchone())
    if kind == "Market" and side == "Buy":
        q_str, unit, p_str = f"{usd:.2f}", "quoteCoin", None
    else:
        q_str = fmt_step(qty, step) if step else repr(qty)
        unit = None
        p_str = (fmt_step(price, tick) if tick else repr(price)) if kind == "Limit" else None
    try:
        r = client.create_order(d["pair"], side, kind, q_str, lid, price=p_str, market_unit=unit)
    except bybit.BybitError as e:
        # ответ не получен или ключ/подпись: ордер мог дойти — состояние прочитаем с биржи
        con.execute("UPDATE demo_orders SET note=? WHERE link_id=?", (f"POST: {e}", lid))
        con.commit()
        raise
    if r["retCode"] == 0:
        oid = str((r.get("result") or {}).get("orderId") or "")
        con.execute("UPDATE demo_orders SET status='new', order_id=? WHERE link_id=?", (oid, lid))
        o.update(status="new", order_id=oid)
    elif r["retCode"] == bybit.DUPLICATE:
        o.update(status="new")
        con.execute("UPDATE demo_orders SET status='new' WHERE link_id=?", (lid,))
    else:
        note = f"отклонён: {r['retCode']} {r.get('retMsg', '')}"
        con.execute("UPDATE demo_orders SET status='rejected', note=? WHERE link_id=?",
                    (note, lid))
        o.update(status="rejected", note=note)
    con.commit()
    return o


def cancel(con, client, o: dict, why: str, now: float) -> dict:
    """Снять ордер (наш: заметка «снят: …») и сразу прочитать, чем он кончился — до снятия
    могло исполниться."""
    r = client.cancel_order(o["pair"], o["link_id"])
    note = OURS + why
    if r["retCode"] in (0, bybit.NOT_EXISTS):
        con.execute("UPDATE demo_orders SET status=CASE WHEN status IN ('sending','new') "
                    "THEN 'cancel_sent' ELSE status END, note=? WHERE link_id=?",
                    (note, o["link_id"]))
        o.update(status="cancel_sent" if o["status"] in ("sending", "new") else o["status"],
                 note=note)
    else:
        raise bybit.BybitError(f"снятие {o['link_id']}: {r['retCode']} {r.get('retMsg', '')}",
                               r["retCode"])
    return refresh(con, client, o, now)


# ---------------------------------------------------------------- позиция

def _orders(con, dry_id: int, role: str | None = None) -> list[dict]:
    if role is None:
        return _rows(con, "SELECT * FROM demo_orders WHERE dry_id=? ORDER BY created_ts, ver",
                     (dry_id,))
    return _rows(con, "SELECT * FROM demo_orders WHERE dry_id=? AND role=? ORDER BY ver",
                 (dry_id, role))


def _day_buys(con, now: float) -> float:
    r = con.execute("SELECT COALESCE(SUM(usd),0) FROM demo_orders WHERE side='Buy' AND "
                    "created_ts>=? AND status NOT IN ('rejected','error')",
                    (utc_day(now),)).fetchone()
    return float(r[0] or 0.0)


def _exposure(con) -> float:
    """Вложено (исполненные покупки) + стоит в лимитках покупки, по всем открытым позициям."""
    r = con.execute("SELECT COALESCE(SUM(CASE WHEN o.status IN ('sending','new','cancel_sent') "
                    "THEN MAX(COALESCE(o.usd,0) - o.cum_value, 0) ELSE 0 END + o.cum_value),0) "
                    "FROM demo_orders o JOIN demo_positions p ON p.dry_id=o.dry_id "
                    "WHERE o.side='Buy' AND p.status IN ('open','closing')").fetchone()
    return float(r[0] or 0.0)


def _free_usdt(client) -> float:
    u = client.wallet_balance().get("USDT") or {}
    return max(0.0, u.get("balance", 0.0) - u.get("locked", 0.0))


def _insert(con, dp: dict, status: str, reason: str, now: float) -> dict:
    con.execute("INSERT OR IGNORE INTO demo_positions(dry_id, book, pair, symbol, status, reason, "
                "created_ts, closed_ts, qty_step, tick, min_amt, updated_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (dp["id"], dp["book"], dp["pair"], dp["symbol"], status, reason, now,
                 now if status == "skipped" else None, dp.get("qty_step"), dp.get("tick"),
                 dp.get("min_amt") or 5.0, now))
    con.commit()
    return dict(con.execute("SELECT * FROM demo_positions WHERE dry_id=?", (dp["id"],)).fetchone())


def open_position(con, client, s: dict, dp: dict, now: float,
                  sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Лестница пробной позиции dp на демо-счёт — после всех потолков; отказ — позиция
    «skipped» с причиной (повторять не будем: вход — только в свой день)."""
    tag = f"{dp['book']} {dp['symbol']}"
    if now - dp["created_ts"] > s["open_late_days"] * DAY:
        _insert(con, dp, "skipped", f"опоздал: пробная позиция от {ddmm(dp['created_ts'])}", now)
        return [f"{tag}: пропуск — пробная позиция от {ddmm(dp['created_ts'])}, демо не "
                f"догоняет старые входы"]
    buys = _rows(con, "SELECT * FROM dry_orders WHERE position_id=? AND side='Buy' AND "
                      "status!='cancelled' ORDER BY step", (dp["id"],))
    if not buys:
        _insert(con, dp, "skipped", "у пробной позиции нет ордеров покупки", now)
        return [f"{tag}: пропуск — у пробной позиции нет ордеров покупки"]
    px = client.last_price(dp["pair"])
    if not px:
        raise Skip(f"нет цены {dp['pair']} на Bybit")
    mkt = next((o for o in buys if o["kind"] == "Market"), None)
    total = sum(o["usd"] or 0.0 for o in buys)
    why = []
    if mkt and mkt["price"] and abs(px / mkt["price"] - 1) > s["max_price_drift"]:
        why.append(f"цена ушла: {px:.6g} против {mkt['price']:.6g} у пробного")
    big = [o for o in buys if (o["usd"] or 0.0) > s["max_order_usdt"] + 1e-9]
    if big:
        why.append(f"ступень ${max(o['usd'] for o in big):.2f} > потолка "
                   f"${s['max_order_usdt']:g} на ордер")
    day = _day_buys(con, now)
    if day + total > s["max_day_usdt"] + 1e-9:
        why.append(f"потолок дня: уже ${day:.2f} + ${total:.2f} > ${s['max_day_usdt']:g}")
    n = con.execute("SELECT COUNT(*) FROM demo_positions WHERE book=? AND status IN "
                    "('open','closing')", (dp["book"],)).fetchone()[0]
    if n >= s["max_coins"]:
        why.append(f"в книге уже {n} монет (потолок {s['max_coins']})")
    expo = _exposure(con)
    if expo + total > s["max_open_usdt"] + 1e-9:
        why.append(f"потолок вложений: ${expo:.2f} + ${total:.2f} > ${s['max_open_usdt']:g}")
    if not why:
        free = _free_usdt(client)
        if free + 1e-9 < total:
            why.append(f"на демо-счёте свободно {free:.2f} USDT < лестницы {total:.2f}")
    if why:
        _insert(con, dp, "skipped", "; ".join(why), now)
        return [f"{tag}: НЕ ставлю — " + "; ".join(why)]
    d = _insert(con, dp, "open", "", now)
    out = []
    for o in buys:
        role = f"B{o['step']}"
        if o["kind"] == "Market":
            r = place(con, client, d, role=role, side="Buy", kind="Market", usd=o["usd"], now=now)
        else:
            r = place(con, client, d, role=role, side="Buy", kind="Limit", price=o["price"],
                      qty=o["qty"], now=now)
        if r["status"] == "rejected":
            out.append(f"  ⚠ {role}: {r['note']}")
    sleep(s["settle_s"])                  # рыночная ступень исполняется за доли секунды
    _settle(con, client, d, now)
    lims = ", ".join(f"{o['price']:.6g}" for o in buys if o["kind"] == "Limit")
    return [f"{tag}: поставил на демо {len(buys)} ступ. на ${total:.2f} (рынок ~{px:.6g}"
            + (f", лимитки {lims}" if lims else "") + ")"] + out


def _settle(con, client, d: dict, now: float) -> None:
    for o in _orders(con, d["dry_id"]):
        if o["status"] in LIVE:
            refresh(con, client, o, now)
    con.commit()


def manage_buys(con, client, s: dict, d: dict, now: float) -> list[str]:
    """Лимитки покупки вслед за пробным: снял он (или начали продавать) — снимаем; наша пропала
    или снята не нами, а пробный её держит — ставим заново на неисполненный остаток."""
    out = []
    tag = f"{d['book']} {d['symbol']}"
    selling = any(o["side"] == "Sell" and o["cum_qty"] > 0 for o in _orders(con, d["dry_id"]))
    for do in _rows(con, "SELECT * FROM dry_orders WHERE position_id=? AND side='Buy' "
                         "ORDER BY step", (d["dry_id"],)):
        role = f"B{do['step']}"
        mine = _orders(con, d["dry_id"], role)
        if not mine:
            continue
        live = [o for o in mine if o["status"] in LIVE]
        if do["status"] == "cancelled" or selling:
            why = "начали продавать" if selling else f"пробный снял ({do['note'] or 'срок'})"
            for o in live:
                cancel(con, client, o, why, now)
                out.append(f"{tag}: снял лимитку {role} — {why}")
            continue
        last = mine[-1]
        lost = last["status"] in ("gone", "error") or (
            last["status"] == "cancelled" and not (last["note"] or "").startswith(OURS))
        if live or do["kind"] != "Limit" or not lost:
            continue
        rem = (do["qty"] or 0.0) - sum(o["cum_qty"] for o in mine)
        rem = round_step(rem, d.get("qty_step") or 0.0) if d.get("qty_step") else rem
        if rem * (do["price"] or 0.0) < (d.get("min_amt") or 5.0):
            continue
        usd = rem * do["price"]
        if _day_buys(con, now) + usd > s["max_day_usdt"] + 1e-9:
            out.append(f"{tag}: {role} не переставляю — потолок дня ${s['max_day_usdt']:g}")
            continue
        r = place(con, client, d, role=role, side="Buy", kind="Limit", price=do["price"],
                  qty=rem, now=now, ver=last["ver"] + 1)
        out.append(f"{tag}: лимитка {role} {last['note'] or last['status']} — поставил заново "
                   f"({r['status']})")
    return out


def manage_takes(con, client, cfg, d: dict, now: float, settle: float = 0.0,
                 sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Тейки книги «rules» лимитками на бирже: цель от своей средней вверх к tick, доля — от
    своего купленного по правилам биржи; уже исполнявшийся уровень не трогаем; стоящий с нужной
    ценой и количеством — оставляем, иначе переставляем."""
    out = []
    tag = f"{d['book']} {d['symbol']}"
    num = numbers(con, d["dry_id"])
    if num["bought"] <= 0:
        return out
    avg = num["spent"] / num["bought"]
    tick, step = d.get("tick") or 0.0, d.get("qty_step") or 0.0
    avail = num["qty"]
    for idx, (level, frac) in enumerate(cfg["stage8_exit"]["ladder"]):
        role = f"L{idx}"
        mine = _orders(con, d["dry_id"], role)
        if any(o["cum_qty"] > 0 and o["status"] not in LIVE for o in mine):
            continue                                       # уровень уже продавался
        live = [o for o in mine if o["status"] in LIVE]
        part = [o for o in live if o["cum_qty"] > 0]
        if part:                                           # исполняется сейчас — не трогаем
            avail -= sum((o["qty"] or 0.0) - o["cum_qty"] for o in part)
            continue
        target = round_step(avg * (1 + level), tick, up=True) if tick else avg * (1 + level)
        want = sell_qty({"qty": max(avail, 0.0), "qty_step": step, "min_amt": d.get("min_amt")},
                        frac * num["bought"], target)
        keep = next((o for o in live if o["status"] != "cancel_sent" and want > 0
                     and abs((o["price"] or 0) - target) <= (tick or target * 1e-9) / 2
                     and abs((o["qty"] or 0) - want) <= (step or want * 1e-9) / 2), None)
        gone = [cancel(con, client, o, "переставляю тейк", now) for o in live if o is not keep]
        if gone:
            sleep(settle)                 # снятие асинхронно: монеты освобождаются не сразу
            for o in gone:
                if o["status"] in LIVE:
                    refresh(con, client, o, now)
            if any(o["cum_qty"] > 0 for o in gone):
                break                     # тейк успел исполниться — пересчёт в след. прогон
        if keep:
            avail -= keep["qty"] or 0.0
            continue
        if want <= 0:
            continue
        ver = max((o["ver"] for o in mine), default=0) + 1
        r = place(con, client, d, role=role, side="Sell", kind="Limit", price=target, qty=want,
                  now=now, ver=ver)
        avail -= want
        tail = "" if r["status"] == "new" else " — " + r["note"]
        out.append(f"{tag}: тейк +{level * 100:.0f}% — {fmt_step(want, step) if step else want} "
                   f"по {target:.6g} (средняя {avg:.6g}){tail}")
    return out


def exit_position(con, client, d: dict, reason: str, now: float, settle: float = 0.0,
                  sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Полный выход: снять все ордера позиции, продать остаток рынком; пыль — закрыть."""
    out = []
    tag = f"{d['book']} {d['symbol']}"
    gone = [cancel(con, client, o, f"выход: {reason}", now)
            for o in _orders(con, d["dry_id"]) if o["status"] in LIVE and o["role"] != "X"]
    if gone:
        sleep(settle)                     # монеты под снятыми тейками освобождаются не сразу
        for o in gone:
            if o["status"] in LIVE:
                refresh(con, client, o, now)
    if any(o["status"] in LIVE for o in _orders(con, d["dry_id"], "X")):
        _set(con, d, status="closing", reason=reason)
        return [f"{tag}: продажа остатка ещё исполняется"]
    num = numbers(con, d["dry_id"])
    step = d.get("qty_step") or 0.0
    held = round_step(num["qty"], step) if step else num["qty"]
    px = client.last_price(d["pair"]) or d.get("last_price") or 0.0
    if held <= 0 or held * px < (d.get("min_amt") or 5.0):
        why = reason + ("; пыль, не продать" if held > 0 else "")
        _set(con, d, status="closed", reason=why, closed_ts=now)
        return out + [f"{tag}: закрыта на демо — {why}"]
    ver = max((o["ver"] for o in _orders(con, d["dry_id"], "X")), default=0) + 1
    r = place(con, client, d, role="X", side="Sell", kind="Market", qty=held, now=now, ver=ver)
    if r["status"] == "new":
        sleep(settle)
        r = refresh(con, client, r, now)
    left = numbers(con, d["dry_id"])["qty"]
    if r["status"] == "filled" and (left < max(step, 1e-12) or left * px < (d.get("min_amt") or 5)):
        _set(con, d, status="closed", reason=reason, closed_ts=now)
        out.append(f"{tag}: {reason} — продал на демо {fmt_step(held, step) if step else held} "
                   f"рынком → ${r['cum_value'] - r['fee_quote']:.2f}")
    else:
        _set(con, d, status="closing", reason=reason)
        out.append(f"{tag}: {reason} — продажа остатка {r['status']}"
                   + (f" ({r['note']})" if r.get("note") else ""))
    return out


def _set(con, d: dict, **kw) -> None:
    cols = ", ".join(f"{k}=?" for k in kw)
    con.execute(f"UPDATE demo_positions SET {cols} WHERE dry_id=?", (*kw.values(), d["dry_id"]))
    d.update(kw)
    con.commit()


def process(con, client, cfg, s: dict, dp: dict, now: float,
            sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Одна пробная позиция: открыть на демо, довести ордера до решения пробного, обновить
    числа для сводки."""
    row = con.execute("SELECT * FROM demo_positions WHERE dry_id=?", (dp["id"],)).fetchone()
    lines: list[str] = []
    if row is None:
        if dp["status"] != "open":
            return []                          # закрылась раньше, чем демо её увидело
        lines = open_position(con, client, s, dp, now, sleep)
        row = con.execute("SELECT * FROM demo_positions WHERE dry_id=?", (dp["id"],)).fetchone()
    d = dict(row)
    if d["status"] in ("closed", "skipped"):
        return lines
    _settle(con, client, d, now)
    books = dry_settings(cfg)["books"]
    rules = (books.get(d["book"]) or {}).get("exits") != "stop_only"
    if dp["status"] == "closed" or d["status"] == "closing":
        why = dp["reason"] or d["reason"] or "закрыта пробным"
        lines += exit_position(con, client, d, why, now, s["settle_s"], sleep)
    else:
        lines += manage_buys(con, client, s, d, now)
        if rules:
            lines += manage_takes(con, client, cfg, d, now, s["settle_s"], sleep)
        num = numbers(con, d["dry_id"])
        step = d.get("qty_step") or 0.0
        if num["sold"] > 0 and num["qty"] < max(step, 1e-12):
            _set(con, d, status="closed", reason="продано тейками", closed_ts=now)
            lines.append(f"{d['book']} {d['symbol']}: всё продано тейками — позиция закрыта")
    num = numbers(con, d["dry_id"])
    px = client.last_price(d["pair"]) if d["status"] != "closed" else None
    _set(con, d, qty=num["qty"], bought_qty=num["bought"], spent_usdt=num["spent"],
         proceeds_usdt=num["proceeds"], last_price=px or d.get("last_price"), updated_ts=now)
    return lines


# ---------------------------------------------------------------- шаг прогона

def run_demo(cfg, client, *, db_path: str | None = None, now: float | None = None,
             halt_base=None, sleep: Callable[[float], None] = time.sleep) -> tuple[int, list[str]]:
    """Шаг ежедневного прогона после execute -> (код, строки лога). Код 1 — ключ опасен,
    ответ биржи не прочитан или позиция упала (остальные идут дальше); 0 — всё прошло."""
    s = settings(cfg)
    if not s.get("enabled", True):
        return 0, ["demo.enabled = false — пропуск"]
    from . import control
    halt = control.halted(halt_base)
    if halt:
        return 0, [control.halt_line(halt) + " — на демо ордеров нет; снять: python3 run.py "
                   "resume (на сервере)"]
    now = now if now is not None else time.time()
    con = connect(db_path or cfg["output"]["db_path"])
    lines: list[str] = []
    code = 0
    try:
        try:
            kc = bybit.check_key(client.api_key_info(), "trade", s["key_warn_days"], demo=True)
        except (bybit.BybitError, OSError) as e:
            kc = {"level": "error", "issues": [f"не проверен: {e}"], "facts": []}
        kline = ("ключ демо: " + " · ".join(kc["facts"]) if kc["level"] == "ok" else
                 {"danger": "⛔", "warn": "⚠"}.get(kc["level"], "⚠") + " ключ демо: "
                 + "; ".join(kc["issues"]))
        lines.append(kline)
        if kc["level"] in ("danger", "error"):
            con.execute("INSERT OR REPLACE INTO demo_runs(ts, code, key_level, key_line, note) "
                        "VALUES (?,?,?,?,?)", (now, 1, kc["level"], kline, "ордеров нет"))
            con.commit()
            return 1, lines + ["ордеров нет — сначала ключ"]
        dps = _rows(con, "SELECT * FROM dry_positions WHERE COALESCE(shadow,0)=0 AND "
                         "status!='rejected' AND created_ts>=? ORDER BY id", (since_ts(s),))
        for dp in dps:
            try:
                lines += process(con, client, cfg, s, dp, now, sleep)
            except Skip as e:
                lines.append(f"{dp['book']} {dp['symbol']}: пропуск до следующего прогона — {e}")
            except (bybit.BybitError, OSError) as e:
                con.rollback()
                code = 1
                lines.append(f"⚠ {dp['book']} {dp['symbol']}: биржа: {e}")
            except Exception as e:  # noqa: BLE001 — одна позиция не валит остальные
                con.rollback()
                code = 1
                lines.append(f"⚠ {dp['book']} {dp['symbol']}: {type(e).__name__}: {e}")
        st = summary(con, cfg)
        for b, x in st.items():
            lines.append(f"демо {b} «{x['label']}»: открыто {x['open']}, вложено "
                         f"${x['spent']:.2f}, P&L {x['pnl']:+.2f} USDT · пробный по тем же "
                         f"позициям {x['dry_pnl']:+.2f}")
        con.execute("INSERT OR REPLACE INTO demo_runs(ts, code, key_level, key_line, note) "
                    "VALUES (?,?,?,?,?)", (now, code, kc["level"], kline, ""))
        con.commit()
    finally:
        con.close()
    return code, lines


def summary(con, cfg) -> dict[str, dict]:
    """По книгам: открыто, вложено, P&L демо и P&L пробного по тем же позициям (без skipped)."""
    out = {}
    for b, bk in dry_settings(cfg)["books"].items():
        ds = _rows(con, "SELECT * FROM demo_positions WHERE book=? AND status!='skipped'", (b,))
        dry = 0.0
        for d in ds:
            r = con.execute("SELECT * FROM dry_positions WHERE id=?", (d["dry_id"],)).fetchone()
            if r is not None:
                dry += pnl_usdt(dict(r))
        out[b] = {"label": bk["label"], "emoji": bk.get("emoji", "•"),
                  "open": sum(1 for d in ds if d["status"] in ("open", "closing")),
                  "closed": sum(1 for d in ds if d["status"] == "closed"),
                  "spent": sum(d["spent_usdt"] or 0.0 for d in ds),
                  "pnl": sum(pnl(d) for d in ds), "dry_pnl": dry}
    return out


def brief_state(cfg, now: float | None = None) -> dict | None:
    """Для сводки дня (без сети): итог прогона демо сегодня, книги, действия за сутки, пропуски.
    Таблиц нет или демо ни разу не запускалось — None (блока нет)."""
    from .benchmark import connect_ro
    now = now if now is not None else time.time()
    con = connect_ro(cfg["output"]["db_path"])
    if con is None:
        return None
    con.row_factory = sqlite3.Row
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"demo_runs", "demo_positions", "demo_orders"} <= names:
            return None
        run = con.execute("SELECT * FROM demo_runs ORDER BY ts DESC LIMIT 1").fetchone()
        if run is None:
            return None
        t0 = now - DAY
        books = summary(con, cfg)
        placed = con.execute("SELECT COUNT(DISTINCT dry_id) FROM demo_orders WHERE role='B1' "
                             "AND created_ts>=? AND status NOT IN ('rejected','error')",
                             (t0,)).fetchone()[0]
        fills = con.execute("SELECT COUNT(*) FROM demo_orders WHERE side='Buy' AND kind='Limit' "
                            "AND filled_ts>=?", (t0,)).fetchone()[0]
        sells = [(r[0], r[1], r[2]) for r in con.execute(
            "SELECT o.book, p.symbol, o.role FROM demo_orders o JOIN demo_positions p ON "
            "p.dry_id=o.dry_id WHERE o.side='Sell' AND o.filled_ts>=? ORDER BY o.filled_ts",
            (t0,))]
        skipped = [(r[0], r[1]) for r in con.execute(
            "SELECT symbol, MIN(reason) FROM demo_positions WHERE status='skipped' AND "
            "created_ts>=? GROUP BY symbol", (t0,))]
        rejected = con.execute("SELECT COUNT(*) FROM demo_orders WHERE status='rejected' AND "
                               "created_ts>=?", (t0,)).fetchone()[0]
    finally:
        con.close()
    return {"run_ts": run["ts"], "run_today": run["ts"] >= utc_day(now),
            "code": run["code"], "key_level": run["key_level"], "key_line": run["key_line"],
            "books": books, "placed": placed, "fills": fills, "sells": sells,
            "skipped": skipped, "rejected": rejected}


def client_from_cfg(cfg) -> "bybit.TradeClient":
    s = settings(cfg)
    return bybit.TradeClient(cfg.get("api_keys.bybit_demo_key", ""),
                             cfg.get("api_keys.bybit_demo_secret", ""),
                             recv_window=s["recv_window_ms"])


def status_lines(cfg) -> list[str]:
    """run.py demo --status: позиции и ордера из БД, без сети."""
    con = connect(cfg["output"]["db_path"])
    try:
        out = []
        for d in _rows(con, "SELECT * FROM demo_positions ORDER BY dry_id"):
            out.append(f"{d['book']} {d['symbol']} (пробная #{d['dry_id']}): {d['status']}"
                       + (f" — {d['reason']}" if d["reason"] else "")
                       + f"; на счёте {d['qty'] or 0:.6g}, вложено ${d['spent_usdt'] or 0:.2f}, "
                         f"P&L {pnl(d):+.2f}")
            for o in _orders(con, d["dry_id"]):
                out.append(f"   {o['link_id']}: {o['side']} {o['kind']} "
                           + (f"${o['usd']:.2f}" if o["qty"] is None else
                              f"{o['qty']:.6g} по {o['price'] or 0:.6g}")
                           + f" — {o['status']}, исполнено {o['cum_qty']:.6g}"
                           + (f" ({o['note']})" if o["note"] else ""))
        return out or ["на демо позиций нет"]
    finally:
        con.close()
