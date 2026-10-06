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
DROP_DAYS = benchmark.DROP_DAYS


def _d(ts) -> str:
    return time.strftime("%d.%m.%y", time.gmtime(ts)) if ts else "—"


def _f(x, digits: int = 1) -> str:
    return "—" if x is None else f"{x:+.{digits}f}"


_coin_key = benchmark.coin_key
# Корзина живёт в scanner/benchmark.py (её же берёт недельная сводка пробного исполнителя).
load_universe = benchmark.load_universe
basket = benchmark.basket


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
