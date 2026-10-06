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
import statistics
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
    outs = []
    for p in positions:
        o = position_outcome(p, last.get(p["id"]))
        if o:
            outs.append({"id": p["id"], "symbol": p["symbol"], **o})
    res = compare_outcomes(outs, mkt)
    res["positions"] = len(positions)
    return res


def compare_outcomes(outcomes: list[dict], mkt: dict) -> dict[str, Any]:
    """То же, что compare_book, для готовых окон {id, symbol, start, end, cost, pnl} (книги
    пробного исполнителя считают их сами: scanner/executor.outcomes)."""
    rows = []
    for o in outcomes:
        m0 = market_at(mkt, o["start"])
        m1 = market_at(mkt, o["end"])
        if not (m0 and m1):
            continue
        rows.append({**o, "book_pct": o["pnl"] / o["cost"] * 100,
                     "alt_pct": (m1[1] / m0[1] - 1) * 100,
                     "btc_pct": (m1[2] / m0[2] - 1) * 100,
                     "market_day": m1[0]})
    out: dict[str, Any] = {"positions": len(outcomes), "n": len(rows), "rows": rows}
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


# ---------------------------------------------------------------- контрольная корзина
# Для каждой даты входа — ВСЕ монеты того же прогона, дошедшие до watchlist, поровну. Цен в
# candidates нет — прокси доходности = капа CoinGecko: капа в прогоне входа → капа в последнем
# прогоне той же БД не позже конца окна, где монета есть. Монеты нет в прогонах последних
# DROP_DAYS дней окна — выпала из топа и в корзину не входит. Корзина отвечает на вопрос «а
# если бы купить всё, что сканер показал в тот день, поровну». Оговорки: выпавшие не учтены и
# эмиссия растит капу без роста цены — корзина завышена; книга — net-of-fees, корзина — нет.

DROP_DAYS = 2


def coin_key(coin_id, symbol, chain, address) -> str:
    return coin_id or f"{symbol}|{chain or ''}|{address or ''}"


def load_universe(path) -> dict | None:
    """Прогоны и капы монет из candidates БД path (только чтение); нет таблиц — None.

    -> {"runs": [(ts, run_id, монет в прогоне)] по времени — только прогоны с кандидатами,
        "wl_runs": те же, но только с непустым watchlist,
        "watch": {run_id: {ключ: (тикер, капа)}} — дошедшие до watchlist (с капой),
        "caps": {ключ: ([ts], [капа])} — капа монеты в каждом прогоне, где она есть,
        "no_cap": сколько монет watchlist было без капы (в корзину не входят)}.
    Ключ монеты — coin_id CoinGecko, без него — тикер|сеть|адрес."""
    con = connect_ro(path)
    if con is None:
        return None
    runs_n: dict[int, list] = {}
    watch: dict[int, dict] = {}
    caps: dict[str, tuple[list, list]] = {}
    no_cap = 0
    try:
        if not {"runs", "candidates"} <= _tables(con):
            return None
        q = ("SELECT c.run_id, r.ts, c.coin_id, c.symbol, c.chain, c.address, c.stage, "
             "c.market_cap FROM candidates c JOIN runs r ON r.id=c.run_id ORDER BY r.ts")
        for rid, ts, cid, sym, chain, addr, stage, cap in con.execute(q):
            runs_n.setdefault(rid, [ts, 0])[1] += 1
            key = coin_key(cid, sym, chain, addr)
            ok = isinstance(cap, (int, float)) and cap > 0
            if stage == "watchlist":
                if ok:
                    watch.setdefault(rid, {}).setdefault(key, (sym, cap))
                else:
                    no_cap += 1
            if ok:
                t, v = caps.setdefault(key, ([], []))
                if not t or t[-1] != ts:          # одна точка на прогон
                    t.append(ts)
                    v.append(cap)
    finally:
        con.close()
    runs = sorted((ts, rid, n) for rid, (ts, n) in runs_n.items())
    return {"runs": runs, "wl_runs": [r for r in runs if r[1] in watch],
            "watch": watch, "caps": caps, "no_cap": no_cap}


def basket(uni: dict, start: float, end: float) -> dict | None:
    """Корзина на окне позиции [start, end]: монеты watchlist прогона входа (последний
    прогон с watchlist не позже входа), доходность монеты = капа в последнем прогоне
    ≤ конца окна, где она есть, / капа в прогоне входа − 1; среднее — равновзвешенно.

    -> {run, run_ts, run_n, n, found, ret, seen, ret_seen}; None — прогона входа нет.
    found/ret — монеты, которые есть в прогонах последних DROP_DAYS дней окна (от последнего
    прогона ≤ конца); seen/ret_seen — все, что встречались после входа (выпавшие из топа —
    по последнему появлению). ret — в процентах; None — после входа прогонов не было."""
    wl = uni["wl_runs"]
    i = bisect_right([r[0] for r in wl], start) - 1
    if i < 0:
        return None
    ts0, rid0, n0 = wl[i]
    coins = uni["watch"][rid0]
    out = {"run": rid0, "run_ts": ts0, "run_n": n0, "n": len(coins),
           "found": 0, "ret": None, "seen": 0, "ret_seen": None}
    all_ts = [r[0] for r in uni["runs"]]
    j = bisect_right(all_ts, end) - 1
    if j < 0 or all_ts[j] <= ts0:
        return out
    ref = all_ts[j] - DROP_DAYS * DAY
    found, seen = [], []
    for key, (_sym, cap0) in coins.items():
        t, v = uni["caps"].get(key, ([], []))
        k = bisect_right(t, end) - 1
        if k < 0 or t[k] <= ts0:
            continue
        r = v[k] / cap0 - 1
        seen.append(r)
        if t[k] >= ref:
            found.append(r)
    out.update(found=len(found), seen=len(seen),
               ret=statistics.fmean(found) * 100 if found else None,
               ret_seen=statistics.fmean(seen) * 100 if seen else None)
    return out


def basket_for(uni: dict | None, rows: list[dict]) -> dict[str, Any]:
    """Корзина, взвешенная стоимостью позиций книги, по строкам compare_outcomes (окна
    start..end): {basket_pct, basket_n} — basket_n позиций, где корзина посчитана; нет — {}."""
    if not uni:
        return {}
    got = [(r["cost"], b["ret"]) for r in rows
           for b in [basket(uni, r["start"], r["end"])] if b and b["ret"] is not None]
    cost = sum(c for c, _ in got)
    if not cost:
        return {}
    return {"basket_pct": sum(c * x for c, x in got) / cost, "basket_n": len(got)}


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
