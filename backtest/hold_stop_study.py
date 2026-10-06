"""Стоп книги «держать» (H) пробного исполнителя: −25 / −35 / −50 % от лоу базы или без стопа.

Вопрос: книга H держит монету без частичных продаж и трейла, единственный выход — аварийный
стоп (2 закрытия ниже лоу базы − буфер). Какой буфер брать? Для сравнения — книга R
(текущие правила stage8_exit: +50/+150 по трети, трейл 30% после +60%, стоп −25%).

Вход у всех вариантов одинаковый — как у исполнителя: 5 ступеней по $10 (первая рынком,
остальные лимитками ровно до 1.05 × пола прод-стопа −25%), лимитки живут BUY_VALID дней.
Симулятор — ladder_dca_study.simulate (дневные свечи, комиссии 0.15% рынок / 0.1% лимит,
делистинг — выход по последнему закрытию, продажа меньше $5 сливается с остатком).

Два замера:
  • история (по умолчанию): пружины feature_study.is_spring на архиве Binance 2018–2026,
    ВКЛЮЧАЯ делистнутые монеты; горизонты 180 / 365 / 730 дней; эпизоды, у которых живой ряд
    кончился раньше горизонта, не берутся (иначе горизонты сравнивают разные выборки);
  • форвард (--forward DB): монеты paper-книги A из БД (например, архив v1), вход в день
    paper-позиции, свечи Bybit spot до последнего закрытия (сеть; запускать на VPS).
Оговорки: survivorship в истории снят не полностью (Binance листит не всё, что умирает);
форвард — 18 монет одного растущего окна, статистики нет — только проверка здравого смысла.

Запуск из корня проекта:
  py -3 backtest/hold_stop_study.py                                   # история
  python3 backtest/hold_stop_study.py --forward /opt/scanner-old-2026-10-03/scanner.db
Результат истории: backtest/hold_stop_results.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ladder_dca_study as L  # noqa: E402

OUT = Path(__file__).resolve().parent / "hold_stop_results.json"
BUDGET = 50.0
BUY5 = {"kind": "floor", "n": 5}
SELL_R = L.SELL["прод +50/+150+трейл"]
SELL_H = {"levels": []}                 # без частичных продаж и трейла
VARIANTS = {                            # имя -> (продажа, стоп, буфер стопа)
    "R правила, стоп −25%": (SELL_R, "prod", 0.25),
    "H держать, стоп −25%": (SELL_H, "prod", 0.25),
    "H держать, стоп −35%": (SELL_H, "prod", 0.35),
    "H держать, стоп −50%": (SELL_H, "prod", 0.50),
    "H держать, без стопа": (SELL_H, None, 0.25),
}
HORIZONS = (180, 365, 730)


def run(eps: list[dict], horizon: int) -> dict[str, list[dict]]:
    L.HORIZON = horizon                 # simulate читает горизонт из модуля
    return {name: [dict(L.simulate(ep["seg"], ep["e"], BUY5, sell, budget=BUDGET, stop=stop,
                                   stop_buf=buf), ep=ep) for ep in eps]
            for name, (sell, stop, buf) in VARIANTS.items()}


def complete(ep: dict, horizon: int) -> bool:
    """Горизонт целиком в данных — или ряд оборвался делистингом/свопом внутри него."""
    seg = ep["seg"]
    return ep["e"] + horizon <= len(seg["c"]) - 1 or not seg["alive"]


def paired(res: dict[str, list[dict]], name: str, base: str = "R правила, стоп −25%") -> dict:
    """Поэпизодно вариант минус R: медиана разницы (п.п. бюджета), доля эпизодов, где лучше."""
    d = [a["rob"] - b["rob"] for a, b in zip(res[name], res[base])]
    if not d:
        return {}
    return {"med_diff": statistics.median(d), "mean_diff": statistics.fmean(d),
            "better": sum(1 for x in d if x > 1e-9) / len(d),
            "worse": sum(1 for x in d if x < -1e-9) / len(d)}


def table(title: str, res: dict[str, list[dict]], pick=None, out: dict | None = None) -> None:
    print(f"\n{title}")
    print(L.HEAD + f" {'H−R мед':>8} {'H лучше':>8}")
    for name, rows in res.items():
        sel = [r for r in rows if pick is None or pick(r["ep"])]
        s = L.summarize(sel)
        sub = {k: [r for r in v if pick is None or pick(r["ep"])] for k, v in res.items()}
        p = paired(sub, name) if not name.startswith("R") else {}
        tail = (f" {p['med_diff']*100:>+7.1f}п {p['better']*100:>7.0f}%" if p else "")
        print(L.fmt(name, s) + tail)
        if out is not None and s:
            out[name] = {**s, **p}


def history() -> int:
    t0 = time.time()
    coins = L.load_archive()
    all_segs = L.segments(coins)
    ranks = L.vol_ranks(all_segs)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    eps = [ep for ep in L.build_episodes(segs, ranks, {}, "spring") if ep["cycle"] != "?"]
    print(f"Пружин: {len(eps)} на {len({ep['seg']['sym'] for ep in eps})} монетах "
          f"[{time.time()-t0:.0f} с]; бюджет ${BUDGET:g}, 5 ступеней до пола, "
          f"лимитки живут {L.BUY_VALID} дн.")
    gate = lambda ep: ep["rank"] <= 150 and ep["age"] >= 365  # noqa: E731
    results: dict = {"meta": {"episodes": len(eps), "budget": BUDGET, "buy_valid": L.BUY_VALID}}
    for h in HORIZONS:
        sub = [ep for ep in eps if complete(ep, h)]
        res = run(sub, h)
        blk: dict = {"n": len(sub)}
        table(f"=== Горизонт {h} дн.: все пружины ({len(sub)}, вкл. умершие) "
              f"[{time.time()-t0:.0f} с] ===", res, out=blk.setdefault("all", {}))
        table(f"--- {h} дн.: гейт «ранг оборота ≤150 и листинг ≥1 года» (≈ фильтр качества)",
              res, gate, out=blk.setdefault("gate", {}))
        for cyc, _, _ in L.CYCLES:
            table(f"--- {h} дн.: цикл {cyc}", res, lambda ep, c=cyc: ep["cycle"] == c,
                  out=blk.setdefault(f"cycle {cyc}", {}))
        if h == 365:
            print("\n--- 365 дн.: портфель из 10 монет одного полугодия (бутстрэп)")
            print(f"  {'вариант':44} {'медиана':>8} {'p10':>7} {'p90':>7} {'P(<0)':>6} "
                  f"{'P(≤−20%)':>8}")
            blk["portfolio10"] = {}
            for name, rows in res.items():
                p = L.portfolio(rows, 10)
                blk["portfolio10"][name] = p
                print(f"  {name:44} {p['med']*100:>+7.1f}% {p['p10']*100:>+6.1f}% "
                      f"{p['p90']*100:>+6.1f}% {p['p_loss']*100:>5.0f}% {p['p_loss20']*100:>7.0f}%")
        results[f"h{h}"] = blk
    print(L.NOTE)
    print("  H−R мед — медиана поэпизодной разницы с книгой R (п.п. бюджета); H лучше — доля "
          "эпизодов, где H обогнала R")
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=float),
                   encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


def forward(db: str) -> int:
    """Монеты paper-книги A из db: вход в день позиции, свечи Bybit до последнего закрытия."""
    from scanner import benchmark
    from scanner.config import load_config
    from scanner.pipeline import _make_http
    from scanner.sources import bybit
    book = benchmark.load_book(db)
    if not book:
        print(f"нет БД {db}")
        return 1
    paper, _ = benchmark.book_split(book)
    http = _make_http(load_config(None))
    L.HORIZON = 10_000                  # держим до последнего закрытия
    tot: dict[str, list[float]] = {n: [] for n in VARIANTS}
    print(f"{'монета':8} {'вход':>8} " + " ".join(f"{n[:16]:>16}" for n in VARIANTS))
    miss = []
    for p in paper:
        sym = p["symbol"].upper()
        d = bybit.fetch_daily_ohlcv(http, f"{sym}USDT", 400)
        day0 = int(p["entry_ts"] // 86400) * 86400
        idx = [i for i, t in enumerate(d["ts"]) if t < day0]
        if not idx or idx[-1] < 30 or idx[-1] >= len(d["ts"]) - 1:
            miss.append(sym)
            continue
        e = idx[-1]                      # последнее закрытие до входа; покупка — open дня входа
        seg = {**{k: d[k] for k in ("ts", "o", "h", "l", "c", "qv")},
               "sym": sym, "alive": True, "swap_end": False}
        cells = []
        for name, (sell, stop, buf) in VARIANTS.items():
            r = L.simulate(seg, e, BUY5, sell, budget=BUDGET, stop=stop, stop_buf=buf)
            tot[name].append(r["pnl"])
            cells.append(f"{r['rob']*100:>+7.1f}% {r['how'][:7]:>7}")
        print(f"{sym:8} {time.strftime('%d.%m', time.gmtime(day0)):>8} " + " ".join(
            f"{c:>16}" for c in cells))
    n = len(tot[next(iter(VARIANTS))])
    print(f"\nИтого по {n} монетам (Σ P&L / Σ бюджета ${BUDGET:g}):")
    for name, pnl in tot.items():
        print(f"  {name:28} {sum(pnl) / (BUDGET * n) * 100 if n else 0:>+7.1f}%  "
              f"(в плюсе {sum(1 for x in pnl if x > 0)} из {n})")
    if miss:
        print(f"нет на Bybit spot или мало истории: {', '.join(miss)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Стоп книги «держать» пробного исполнителя")
    ap.add_argument("--forward", default=None, help="БД с paper-книгой A (форвард по свечам Bybit)")
    a = ap.parse_args()
    return forward(a.forward) if a.forward else history()


if __name__ == "__main__":
    sys.exit(main())
