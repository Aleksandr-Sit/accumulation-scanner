"""Ранжирующая сила балла: доходность монет watchlist по квинтилям score внутри прогона.

Вопрос: топ по баллу (paper-книга v1) отстал от всех монет своего же watchlist. Это случай
или балл вообще не ранжирует (или ранжирует наоборот)? Для каждого полного прогона (≥ --min-run
монет в candidates, без ручных тестовых) монеты watchlist делятся на 5 корзин по score ВНУТРИ
прогона (Q1 — нижние 20%, Q5 — верхние): одна дата, один рынок, поэтому разница корзин —
не рост рынка. Отдельно — корзины по абсолютному порогу (алерт сканера с 70).

Прокси доходности — капа CoinGecko из candidates, как в контрольной корзине
forward_vs_benchmark.py: капа в прогоне входа → капа в последнем прогоне ≤ входа + H дней.
Монеты нет в прогонах последних DROP_DAYS дней до конца горизонта — выпала из топа: в основной
расчёт не входит (выжившие), отдельно — вариант «по последнему появлению». Прогоны, чей
горизонт не уместился в данные, для этого H не берутся.

Соседние ежедневные прогоны почти повторяют друг друга (те же монеты, перекрытые окна),
поэтому наблюдения не независимы: печатается и подвыборка непересекающихся прогонов
(шаг ≥ H дней), где окна не перекрываются, — по ней честнее судить о повторяемости.

БД только читается (sqlite mode=ro). Запуск из корня проекта (на VPS — PYTHONPATH=/opt/scanner):
  py -3 backtest/score_quintiles.py --db /opt/scanner-old-2026-10-03/scanner.db
  py -3 backtest/score_quintiles.py --db copy.db --horizons 14 30 60 --out res.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from bisect import bisect_right
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scanner import benchmark  # noqa: E402

DAY = 86400
DROP_DAYS = 2        # как в forward_vs_benchmark: нет в прогонах последних 2 дней — выпала
SLACK = 0.5 * DAY    # прогоны идут раз в сутки в одно время: конец горизонта ± полсуток
QN = 5
ABS_EDGES = (70, 60, 50)   # ≥70 (порог алерта), 60–70, 50–60, <50


def _d(ts) -> str:
    return time.strftime("%d.%m", time.gmtime(ts)) if ts else "—"


def _f(x, digits: int = 1) -> str:
    return "—" if x is None else f"{x:+.{digits}f}"


def _coin_key(coin_id, symbol, chain, address) -> str:
    return coin_id or f"{symbol}|{chain or ''}|{address or ''}"


def load(path, min_run: int) -> dict | None:
    """-> {"runs": [(ts, run_id)] полных прогонов с watchlist по времени,
          "watch": {run_id: [(ключ, тикер, капа, score)]} — watchlist с капой и баллом,
          "caps": {ключ: ([ts], [капа])} — капа в каждом прогоне (любой стадии),
          "last_ts": время последнего прогона в БД, "no_cap": монет watchlist без капы/балла}."""
    con = benchmark.connect_ro(path)
    if con is None:
        return None
    n_run: dict[int, list] = {}
    watch: dict[int, dict] = {}
    caps: dict[str, tuple[list, list]] = {}
    no_cap = 0
    try:
        q = ("SELECT c.run_id, r.ts, c.coin_id, c.symbol, c.chain, c.address, c.stage, "
             "c.market_cap, c.score FROM candidates c JOIN runs r ON r.id=c.run_id ORDER BY r.ts")
        for rid, ts, cid, sym, chain, addr, stage, cap, score in con.execute(q):
            n_run.setdefault(rid, [ts, 0])[1] += 1
            key = _coin_key(cid, sym, chain, addr)
            ok = isinstance(cap, (int, float)) and cap > 0
            if stage == "watchlist":
                if ok and isinstance(score, (int, float)):
                    watch.setdefault(rid, {}).setdefault(key, (key, sym, cap, float(score)))
                else:
                    no_cap += 1
            if ok:
                t, v = caps.setdefault(key, ([], []))
                if not t or t[-1] != ts:
                    t.append(ts)
                    v.append(cap)
    finally:
        con.close()
    runs = sorted((ts, rid) for rid, (ts, n) in n_run.items() if n >= min_run and rid in watch)
    return {"runs": runs, "watch": {r: list(c.values()) for r, c in watch.items()},
            "caps": caps, "last_ts": max(ts for ts, _ in n_run.values()) if n_run else 0,
            "no_cap": no_cap}


def fwd_return(caps: dict, key: str, ts0: float, cap0: float, horizon_days: float):
    """-> (доходность или None, статус): found — монета есть у конца горизонта; seen — выпала,
    доходность по последнему появлению; gone — после входа не встречалась."""
    t, v = caps.get(key, ([], []))
    end = ts0 + horizon_days * DAY
    k = bisect_right(t, end + SLACK) - 1
    if k < 0 or t[k] <= ts0:
        return None, "gone"
    r = v[k] / cap0 - 1
    return r, ("found" if t[k] >= end - DROP_DAYS * DAY else "seen")


def quintiles(coins: list[tuple]) -> list[int]:
    """Номер корзины 1..QN по score внутри прогона (1 — нижние, QN — верхние). Корзины
    равного размера по рангу; равные баллы на границе разводятся по тикеру (стабильно)."""
    order = sorted(range(len(coins)), key=lambda i: (coins[i][3], coins[i][1]))
    n = len(coins)
    q = [0] * n
    for rank, i in enumerate(order):
        q[i] = rank * QN // n + 1
    return q


def abs_bucket(score: float) -> str:
    if score >= ABS_EDGES[0]:
        return f"≥{ABS_EDGES[0]}"
    if score >= ABS_EDGES[1]:
        return f"{ABS_EDGES[1]}–{ABS_EDGES[0]}"
    if score >= ABS_EDGES[2]:
        return f"{ABS_EDGES[2]}–{ABS_EDGES[1]}"
    return f"<{ABS_EDGES[2]}"


ABS_NAMES = [f"<{ABS_EDGES[2]}", f"{ABS_EDGES[2]}–{ABS_EDGES[1]}",
             f"{ABS_EDGES[1]}–{ABS_EDGES[0]}", f"≥{ABS_EDGES[0]}"]


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Ранговая корреляция (средние ранги при равных значениях); < 5 точек — None."""
    n = len(xs)
    if n < 5:
        return None

    def ranks(a):
        order = sorted(range(n), key=lambda i: a[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and a[order[j + 1]] == a[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    return sxy / (sxx * syy) ** 0.5 if sxx and syy else None


def non_overlapping(runs: list[tuple], horizon_days: float) -> list[tuple]:
    """Жадно с первого: следующий прогон не раньше, чем через horizon_days − полсуток."""
    out, nxt = [], None
    for ts, rid in runs:
        if nxt is None or ts >= nxt:
            out.append((ts, rid))
            nxt = ts + horizon_days * DAY - SLACK
    return out


def study(uni: dict, horizon_days: float, runs: list[tuple] | None = None,
          use_seen: bool = False) -> dict:
    """Доходности по корзинам на горизонте H для прогонов runs (по умолчанию — все, чей
    горизонт уместился в данные). use_seen: выпавшие из топа — по последнему появлению.

    -> {"runs": [...], "q": {1..5: stats}, "abs": {имя: stats}, "spread": {...}, "ic": {...}}
    stats: n (монето-наблюдений), runs_n (прогонов с монетами в корзине), mean/median (%),
    hit (% в плюсе), run_mean (среднее по прогонам средних, %), dropped (выпало из топа)."""
    fits = [(ts, rid) for ts, rid in uni["runs"]
            if ts + horizon_days * DAY <= uni["last_ts"] + SLACK]
    runs = fits if runs is None else [r for r in runs if r in fits]
    q_obs: dict[int, list] = {i: [] for i in range(1, QN + 1)}
    q_run: dict[int, list] = {i: [] for i in range(1, QN + 1)}
    q_drop = {i: 0 for i in range(1, QN + 1)}
    a_obs: dict[str, list] = {k: [] for k in ABS_NAMES}
    a_run: dict[str, list] = {k: [] for k in ABS_NAMES}
    a_drop = {k: 0 for k in ABS_NAMES}
    spreads, ics, top_vs_all = [], [], []
    for ts0, rid in runs:
        coins = uni["watch"][rid]
        qs = quintiles(coins)
        per_q: dict[int, list] = {}
        per_a: dict[str, list] = {}
        xs, ys, all_r = [], [], []
        for (key, _sym, cap0, score), q in zip(coins, qs):
            r, st = fwd_return(uni["caps"], key, ts0, cap0, horizon_days)
            ab = abs_bucket(score)
            if st != "found":
                q_drop[q] += 1
                a_drop[ab] += 1
                if not (use_seen and st == "seen"):
                    continue
            per_q.setdefault(q, []).append(r)
            per_a.setdefault(ab, []).append(r)
            xs.append(score)
            ys.append(r)
            all_r.append(r)
        for q, rs in per_q.items():
            q_obs[q].extend(rs)
            q_run[q].append(statistics.fmean(rs))
        for ab, rs in per_a.items():
            a_obs[ab].extend(rs)
            a_run[ab].append(statistics.fmean(rs))
        if 1 in per_q and QN in per_q:
            spreads.append(statistics.fmean(per_q[QN]) - statistics.fmean(per_q[1]))
        if QN in per_q and all_r:
            top_vs_all.append(statistics.fmean(per_q[QN]) - statistics.fmean(all_r))
        ic = spearman(xs, ys)
        if ic is not None:
            ics.append(ic)

    def stats(obs: list, run_means: list, dropped: int) -> dict:
        if not obs:
            return {"n": 0, "runs_n": 0, "dropped": dropped}
        return {"n": len(obs), "runs_n": len(run_means), "dropped": dropped,
                "mean": statistics.fmean(obs) * 100, "median": statistics.median(obs) * 100,
                "hit": sum(r > 0 for r in obs) / len(obs) * 100,
                "run_mean": statistics.fmean(run_means) * 100}

    def series(xs: list, scale: float = 100) -> dict:
        if not xs:
            return {"n": 0}
        return {"n": len(xs), "mean": statistics.fmean(xs) * scale,
                "median": statistics.median(xs) * scale,
                "pos_share": sum(x > 0 for x in xs) / len(xs) * 100}

    return {"horizon_days": horizon_days, "use_seen": use_seen,
            "runs": [rid for _, rid in runs],
            "first": runs[0][0] if runs else None, "last": runs[-1][0] if runs else None,
            "q": {q: stats(q_obs[q], q_run[q], q_drop[q]) for q in q_obs},
            "abs": {k: stats(a_obs[k], a_run[k], a_drop[k]) for k in ABS_NAMES},
            "spread_q5_q1": series(spreads), "top_minus_all": series(top_vs_all),
            "ic": series(ics, scale=1)}


def print_study(title: str, res: dict) -> None:
    n_runs = len(res["runs"])
    span = f"{_d(res['first'])}–{_d(res['last'])}" if n_runs else "—"
    print(f"\n--- {title}: прогонов {n_runs} (входы {span}) ---")
    if not n_runs:
        print("горизонт не умещается в данные")
        return
    print(f"{'корзина':<8} {'n':>5} {'прог.':>5} {'выпало':>6} {'среднее %':>10} "
          f"{'медиана %':>10} {'в плюсе %':>9} {'ср. по прог. %':>15}")
    rows = [(f"Q{q}", res["q"][q]) for q in range(1, QN + 1)]
    rows += [("—", None)] + [(k, res["abs"][k]) for k in ABS_NAMES]
    for name, s in rows:
        if s is None:
            print("  по абсолютному баллу:")
            continue
        if not s["n"]:
            print(f"{name:<8} {0:>5} {0:>5} {s['dropped']:>6}")
            continue
        print(f"{name:<8} {s['n']:>5} {s['runs_n']:>5} {s['dropped']:>6} {_f(s['mean']):>10} "
              f"{_f(s['median']):>10} {s['hit']:>9.0f} {_f(s['run_mean']):>15}")
    sp, tv, ic = res["spread_q5_q1"], res["top_minus_all"], res["ic"]
    if sp["n"]:
        print(f"Q5 − Q1 по прогонам: среднее {_f(sp['mean'])} п.п., медиана {_f(sp['median'])} "
              f"п.п., Q5 лучше Q1 в {sp['pos_share']:.0f}% прогонов (n={sp['n']})")
    if tv["n"]:
        print(f"Q5 − весь watchlist: среднее {_f(tv['mean'])} п.п., медиана {_f(tv['median'])} "
              f"п.п., Q5 лучше всех в {tv['pos_share']:.0f}% прогонов")
    if ic["n"]:
        print(f"ранговая корреляция балл↔доходность (Спирмен) по прогонам: средняя "
              f"{ic['mean']:+.3f}, медиана {ic['median']:+.3f}, > 0 в {ic['pos_share']:.0f}% "
              f"прогонов (n={ic['n']})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Доходность watchlist по квинтилям балла (БД ro)")
    ap.add_argument("--db", default=str(ROOT / "scanner.db"))
    ap.add_argument("--horizons", type=float, nargs="+", default=[14, 30, 60])
    ap.add_argument("--min-run", type=int, default=500,
                    help="полный прогон — не меньше стольких монет в candidates (ручные "
                         "тестовые прогоны по 20–40 монет не берутся)")
    ap.add_argument("--out", default=None, help="записать итоги в JSON")
    args = ap.parse_args()
    uni = load(args.db, args.min_run)
    if uni is None:
        print(f"нет файла {args.db}")
        return 1
    if not uni["runs"]:
        print("полных прогонов с watchlist нет")
        return 1
    n_obs = sum(len(uni["watch"][r]) for _, r in uni["runs"])
    print(f"БД: {args.db}")
    print(f"полных прогонов с watchlist: {len(uni['runs'])} ({_d(uni['runs'][0][0])}–"
          f"{_d(uni['runs'][-1][0])}), монет watchlist в них: {n_obs} "
          f"(в среднем {n_obs / len(uni['runs']):.0f} на прогон); данные капы до "
          f"{_d(uni['last_ts'])}; без капы/балла вне расчёта: {uni['no_cap']}")
    print("Q1 — нижние 20% балла в прогоне, Q5 — верхние. Доходность — по капе CoinGecko, без "
          "комиссий; «выпало» — нет в топе у конца горизонта (в среднее не входят).")
    out = {"db": args.db, "min_run": args.min_run, "runs_n": len(uni["runs"]), "results": []}
    for h in args.horizons:
        full = study(uni, h)
        print_study(f"горизонт {h:g} дн., все прогоны (окна перекрываются)", full)
        nov = study(uni, h, runs=non_overlapping(uni["runs"], h))
        print_study(f"горизонт {h:g} дн., непересекающиеся прогоны", nov)
        seen = study(uni, h, use_seen=True)
        print_study(f"горизонт {h:g} дн., выпавшие — по последнему появлению", seen)
        out["results"].append({"horizon_days": h, "all_runs": full, "non_overlapping": nov,
                               "with_dropped": seen})
    print("\nОговорки: один отрезок рынка (июль–октябрь 2026, альты росли); капа ≠ цена "
          "(эмиссия растит капу без роста цены, сильнее у молодых монет с разлоками); выжившие — "
          "выпавшие из топа в основной расчёт не входят; балл v1 (код ~01.08) не равен текущему; "
          "соседние прогоны почти повторяют друг друга — независимых наблюдений намного меньше n.")
    if args.out:
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"итоги → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
