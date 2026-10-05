"""Форвард-тест против рынка: paper-книга vs альты/BTC на тех же окнах + контрольная корзина.

Вопрос: плюс книги — заслуга отбора или просто рост рынка? Каждой позиции сопоставляется
рынок на её окне (вход → закрытие или последний снапшот) — та же логика, что в блоке
«📊 Против рынка» недельной сводки (scanner/benchmark.py), но с таблицей по позициям.
Рынок — из market_daily (--market-db; в архивной БД его нет).

Контрольная корзина: для каждой даты входа — ВСЕ монеты того же прогона, дошедшие до
watchlist (candidates.stage = 'watchlist': у них reject_reasons пуст, число совпадает с
runs.n_watchlist), равновзвешенно. Цен в candidates нет — прокси доходности = капа
CoinGecko: капа в прогоне входа → капа в последнем прогоне этой же БД (не позже конца окна
позиции), где монета есть. Монеты нет в прогонах последних 2 дней окна — выпала из топа и
в корзину не входит (покрытие печатается). Корзина отвечает на вопрос «а если бы купить всё,
что сканер показал в тот день, поровну»: книга лучше корзины — отбор по баллу что-то даёт.

Оговорки (печатаются в конце):
  • выпавшие из топа не учтены — корзина завышена (вариант «по последнему появлению» тоже
    завышен, но меньше: выпавшие считаются до выпадения);
  • эмиссия (разлоки, инфляция) растит капу без роста цены — корзина по капе завышена;
  • книга — net-of-fees (как paper P&L), рынок и корзина — без комиссий.
«держать %» — та же монета на том же окне по капе: книга против «держать» — цена выходов,
«держать» против корзины — сам отбор (один прокси у обеих сторон).

БД только читаются (sqlite mode=ro). Запуск из корня проекта:
  py -3 backtest/forward_vs_benchmark.py                          # книга текущей scanner.db
  py -3 backtest/forward_vs_benchmark.py --archive /opt/scanner-old-2026-10-03/scanner.db
  py -3 backtest/forward_vs_benchmark.py --db copy.db --market-db scanner.db
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from bisect import bisect_right
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scanner import benchmark  # noqa: E402

DAY = 86400
DROP_DAYS = 2   # монеты нет в прогонах последних DROP_DAYS дней окна — выпала из топа


def _d(ts) -> str:
    return time.strftime("%d.%m.%y", time.gmtime(ts)) if ts else "—"


def _f(x, digits: int = 1) -> str:
    return "—" if x is None else f"{x:+.{digits}f}"


def _coin_key(coin_id, symbol, chain, address) -> str:
    return coin_id or f"{symbol}|{chain or ''}|{address or ''}"


# ---------------------------------------------------------------- контрольная корзина

def load_universe(path) -> dict | None:
    """Прогоны и капы монет из candidates БД path (только чтение); нет таблиц — None.

    -> {"runs": [(ts, run_id, монет в прогоне)] по времени — только прогоны с кандидатами,
        "wl_runs": те же, но только с непустым watchlist,
        "watch": {run_id: {ключ: (тикер, капа)}} — дошедшие до watchlist (с капой),
        "caps": {ключ: ([ts], [капа])} — капа монеты в каждом прогоне, где она есть,
        "no_cap": сколько монет watchlist было без капы (в корзину не входят)}.
    Ключ монеты — coin_id CoinGecko, без него — тикер|сеть|адрес."""
    con = benchmark.connect_ro(path)
    if con is None:
        return None
    runs_n: dict[int, list] = {}
    watch: dict[int, dict] = {}
    caps: dict[str, tuple[list, list]] = {}
    no_cap = 0
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"runs", "candidates"} <= names:
            return None
        q = ("SELECT c.run_id, r.ts, c.coin_id, c.symbol, c.chain, c.address, c.stage, "
             "c.market_cap FROM candidates c JOIN runs r ON r.id=c.run_id ORDER BY r.ts")
        for rid, ts, cid, sym, chain, addr, stage, cap in con.execute(q):
            runs_n.setdefault(rid, [ts, 0])[1] += 1
            key = _coin_key(cid, sym, chain, addr)
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


def hold_return(uni: dict, p: dict, start: float, end: float) -> float | None:
    """«Держать ту же монету» на окне позиции тем же прокси, что корзина: капа в последнем
    прогоне ≤ входа → капа в последнем прогоне ≤ конца окна, %; монеты нет в капах — None.
    Книга против «держать» — цена выходов (лестница, трейл, стоп), комиссий и капы ≠ цены
    (эмиссия); «держать» против корзины — сам отбор при одинаковом способе измерения."""
    t, v = uni["caps"].get(_coin_key(p.get("coin_id"), p["symbol"], p.get("chain"),
                                     p.get("address")), ([], []))
    i0 = bisect_right(t, start) - 1
    k = bisect_right(t, end) - 1
    if i0 < 0 or k <= i0:
        return None
    return (v[k] / v[i0] - 1) * 100


def _weighted(rows: list[dict], key: str) -> tuple[float | None, float]:
    """Взвешенное стоимостью среднее корзины по позициям, где она посчитана; (ret, cost)."""
    rs = [r for r in rows if r["basket"] and r["basket"][key] is not None]
    cost = sum(r["cost"] for r in rs)
    if not cost:
        return None, 0.0
    return sum(r["cost"] * r["basket"][key] for r in rs) / cost, cost


# ---------------------------------------------------------------- отчёт по книге

def report_book(title: str, positions: list[dict], last: dict, mkt: dict,
                uni: dict | None) -> None:
    print(f"\n=== {title}: {len(positions)} поз. ===")
    if not positions:
        print("позиций нет")
        return
    res = benchmark.compare_book(positions, last, mkt)
    rows = res["rows"]
    done = {r["id"] for r in rows}
    miss = [p["symbol"] for p in positions if p["id"] not in done]
    if miss:
        print(f"⚠ без снапшота или рынка на даты окна (в итог не входят): {', '.join(miss)}")
    if not rows:
        return
    by_id = {p["id"]: p for p in positions}
    for r in rows:
        r["basket"] = basket(uni, r["start"], r["end"]) if uni else None
        r["hold"] = hold_return(uni, by_id[r["id"]], r["start"], r["end"]) if uni else None
    print(f"{'символ':<9} {'вход':<9} {'конец':<9} {'книга %':>8} {'держать %':>10} "
          f"{'альты %':>8} {'BTC %':>7} {'корзина %':>10} {'нашлось':>8}")
    for r in sorted(rows, key=lambda x: (x["start"], x["id"])):
        b = r["basket"]
        p = by_id[r["id"]]
        mark = ""
        if b and _coin_key(p.get("coin_id"), p["symbol"], p.get("chain"),
                           p.get("address")) not in uni["watch"][b["run"]]:
            mark = "*"
        cov = f"{b['found']}/{b['n']}" if b else "—"
        print(f"{r['symbol'] + mark:<9} {_d(r['start']):<9} {_d(r['end']):<9} "
              f"{_f(r['book_pct']):>8} {_f(r['hold']):>10} {_f(r['alt_pct']):>8} "
              f"{_f(r['btc_pct']):>7} {_f(b['ret'] if b else None):>10} {cov:>8}")
    stale = f" ⚠ рынок на {_d(res['market_day'])}" if res["stale"] else ""
    print(f"итог по стоимости (${res['cost']:.0f}): книга {_f(res['book_pct'], 2)}% · "
          f"альты {_f(res['alt_pct'], 2)}% · BTC {_f(res['btc_pct'], 2)}% → "
          f"{_f(res['diff_pp'], 2)} п.п. к альтам{stale}")
    if not uni:
        print("контрольная корзина: в БД нет таблиц runs/candidates")
        return
    held = [r for r in rows if r["hold"] is not None]
    if held:
        h_cost = sum(r["cost"] for r in held)
        print(f"держать те же монеты (капа, без выходов и комиссий): "
              f"{_f(sum(r['cost'] * r['hold'] for r in held) / h_cost, 2)}% против книги "
              f"{_f(sum(r['pnl'] for r in held) / h_cost * 100, 2)}% на {len(held)} поз. — "
              f"разница: выходы + комиссии + капа≠цена; «держать» против корзины — сам отбор")
    ret, cost = _weighted(rows, "ret")
    if ret is None:
        print("контрольная корзина: не посчитана (после входа прогонов не было)")
        return
    with_b = [r for r in rows if r["basket"] and r["basket"]["ret"] is not None]
    book_sub = sum(r["pnl"] for r in with_b) / cost * 100
    sub = "" if len(with_b) == len(rows) else f" (на {len(with_b)} поз. с корзиной)"
    n_all = sum(r["basket"]["n"] for r in with_b)
    n_found = sum(r["basket"]["found"] for r in with_b)
    print(f"контрольная корзина: {_f(ret, 2)}% → книга {_f(book_sub - ret, 2)} п.п. к корзине"
          f"{sub}; нашлось в конце окна {n_found} из {n_all} ({n_found / n_all:.0%}, "
          f"сумма по позициям)")
    ret_seen, _ = _weighted(rows, "ret_seen")
    n_seen = sum(r["basket"]["seen"] for r in with_b)
    print(f"  с выпавшими по последнему появлению: {_f(ret_seen, 2)}% "
          f"(встречались после входа {n_seen} из {n_all})")
    groups: dict[int, dict] = {}
    for r in with_b:
        b = r["basket"]
        g = groups.setdefault(b["run"], {"ts": b["run_ts"], "n": b["n"], "run_n": b["run_n"],
                                         "pos": 0})
        g["pos"] += 1
    print("  корзины по датам входа: " + " · ".join(
        f"{_d(g['ts'])} #{rid} — {g['n']} мон. watchlist из {g['run_n']} в прогоне, "
        f"поз. {g['pos']}" for rid, g in sorted(groups.items(), key=lambda x: x[1]["ts"])))
    if uni["no_cap"]:
        print(f"  (монет watchlist без капы в этой БД, вне корзин: {uni['no_cap']})")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Paper-книга против рынка и контрольной корзины (БД только читаются)")
    ap.add_argument("--db", default=str(ROOT / "scanner.db"),
                    help="БД сканера с книгой (по умолчанию scanner.db в корне проекта)")
    ap.add_argument("--market-db", default=None, help="БД с market_daily (по умолчанию = --db)")
    ap.add_argument("--archive", default=None,
                    help="архивная БД (книга v1): своя таблица и корзина, рынок — из --market-db")
    args = ap.parse_args()
    cur = benchmark.load_book(args.db)
    arch = benchmark.load_book(args.archive) if args.archive else None
    for path, book in ((args.db, cur), (args.archive, arch)):
        if path and book is None:
            print(f"нет файла {path}")
            return 1
    market_db = args.market_db or args.db
    mkt = benchmark.load_market(market_db)
    if not mkt["days"]:
        print(f"market_daily в {market_db} нет или пуста — укажи --market-db с рынком")
        return 1
    print(f"Книга: {args.db}" + (f" · архив: {args.archive}" if args.archive else ""))
    print(f"Рынок: market_daily из {market_db}, {_d(mkt['days'][0])}–{_d(mkt['days'][-1])}; "
          f"альты = total×(1−BTC.D) − стейблы, BTC = total×BTC.D, на ближайший день ≤ даты.")
    print("Книга — net-of-fees (как paper P&L), итог по стоимости; рынок взвешен той же "
          "стоимостью. «*» — монеты позиции нет в watchlist прогона входа.")

    paper, real = benchmark.book_split(cur)
    uni = load_universe(args.db)
    name = Path(args.db).name
    books = [(f"📝 книга A — {name}", paper, cur["last"], uni)]
    if real:
        books.append((f"💰 real — {name}", real, cur["last"], uni))
    if arch is not None:
        books.append((f"🗄 архив — {Path(args.archive).name}", benchmark.book_split(arch)[0],
                      arch["last"], load_universe(args.archive)))
    for title, positions, last, u in books:
        report_book(title, positions, last, mkt, u)

    print("\nОговорки:")
    print(f"  • корзина: прокси доходности — капа CoinGecko из candidates (цен там нет); "
          f"монеты, которых нет в прогонах последних {DROP_DAYS} дн. окна, считаются выпавшими "
          f"из топа и не учтены — корзина завышена; «по последнему появлению» тоже завышена;")
    print("  • эмиссия (разлоки, инфляция предложения) растит капу без роста цены — корзина "
          "по капе завышена у монет с растущим предложением;")
    print("  • книга — net-of-fees, рынок и корзина — без комиссий; маленькая выборка и одно "
          "окно рынка — не статистика, а сверка направления.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
