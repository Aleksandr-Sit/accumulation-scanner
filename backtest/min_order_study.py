"""Тейк дешевле минимума биржи: продавать всё или поднять долю до минимума; ступени $10 или $12.

Вопрос владельца (10.10.2026): лестница 5 × $10, тейк +50% продаёт 0.33 купленного. Если
исполнилась одна ступень, треть = $10 × 1.5 × 0.33 = $4.95 < $5 (minOrderAmt Bybit), и по
правилу «доля дешевле минимума — весь остаток» продаётся ВСЁ — монета, пошедшая вверх сразу
(без докупок), выходит целиком на +50%. Варианты:
  base — как сейчас (прод после блока C, ladder_dca_study.EXEC_ALL), 5 × $10 = $50;
  A    — доля дешевле минимума поднимается до минимума ($5), если остаток после неё ≥ минимума;
         иначе — как раньше, весь остаток (правило sell_up в simulate_exec);
  B12  — ступени по $12: 5 × $12 = $60 на позицию, правило продаж как сейчас;
  B12A — $12 и правило A.
Мерила: (1) эпизоды — все пружины 2018–2026 (e90, горизонт 365, как таблица расхождений
LADDER_REPORT «После блока C»): Σ P&L, P&L на $ вложенного, сколько раз доля ушла целиком;
(2) портфель «как исполнитель» (portfolio_vs_market, шаг s6: детектор прода, кулдаун 90 дн.,
свободный USDT, без горизонта) против альтов и BTC по денежным потокам, по рангу и при 30
случайных порядках сигналов внутри дня. У B12 деньги в 1.2 раза больше — сравнивать P&L на $
вложенного и (P&L − рынок) на $; абсолютные $ — справочно.
Книга H тейков не продаёт: A на неё не влияет (проверка), B12 — только масштаб.

Запуск из корня проекта:  py -3 backtest/min_order_study.py
Результат: backtest/min_order_results.json
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import junk_filter_study as J  # noqa: E402
import ladder_dca_study as L  # noqa: E402
import portfolio_vs_market as P  # noqa: E402

OUT = Path(__file__).resolve().parent / "min_order_results.json"
VARIANTS = [
    ("base", "как сейчас: 5 × $10, доля < $5 → весь остаток", 50.0, {}),
    ("A", "5 × $10, доля < $5 → поднять до $5", 50.0, {"sell_up": True}),
    ("B12", "5 × $12 = $60, доля < $5 → весь остаток", 60.0, {}),
    ("B12A", "5 × $12 = $60, доля < $5 → поднять до $5", 60.0, {"sell_up": True}),
]


def episodes(eps: list[dict], sell: dict, budget: float, rules: dict) -> dict:
    rows = [r for r in (L.simulate_exec(ep["seg"], ep["e"], sell, budget=budget, horizon=365,
                                        rules={**L.EXEC_ALL, **rules}) for ep in eps) if r]
    spent = sum(r["spent"] for r in rows)
    pnl = sum(r["pnl"] for r in rows)
    one = [r for r in rows if r["n_buys"] == 1]
    return {"n": len(rows), "spent": spent, "pnl": pnl, "pnl_per_usd": pnl / spent,
            "mean_rod": statistics.fmean(r["rod"] for r in rows if r["spent"] > 0),
            "median_rod": statistics.median(r["rod"] for r in rows if r["spent"] > 0),
            "win": sum(1 for r in rows if r["pnl"] > 0) / len(rows),
            "small": sum(r["small"] for r in rows),
            "one_step": len(one), "one_step_pnl": sum(r["pnl"] for r in one),
            "hows": {h: sum(1 for r in rows if r["how"] == h) for h in sorted({r["how"] for r in rows})}}


def portfolio(cands: list[dict], sell: dict, budget: float, rules: dict, alt, btc,
              seeds: int) -> dict:
    cache: dict = {}

    def sim(ep):
        k = (ep["seg"]["sym"], ep["e"])
        if k not in cache:
            cache[k] = L.simulate_exec(ep["seg"], ep["e"], sell, budget=budget, horizon=None,
                                       rules={**L.EXEC_ALL, **rules})
        return cache[k]
    m = P.evaluate_flows(P.exec_book(cands, sim, *P.EXEC_POLICY, cooldown=True, cash=True),
                         alt, btc)
    runs = []
    for sd in range(seeds):
        x = P.evaluate_flows(P.exec_book(cands, sim, *P.EXEC_POLICY, cooldown=True, cash=True,
                                         order="random", seed=sd), alt, btc)
        runs.append({"pnl": x["pnl"], "spent": x["spent"], "vs_alt": x["pnl"] - x["alt"],
                     "vs_btc": x["pnl"] - x["btc"]})
    m.pop("cycles", None)
    m["random"] = {
        "pnl_mean": statistics.fmean(r["pnl"] for r in runs),
        "vs_alt_mean": statistics.fmean(r["vs_alt"] for r in runs),
        "vs_btc_mean": statistics.fmean(r["vs_btc"] for r in runs),
        "vs_alt_range": [min(r["vs_alt"] for r in runs), max(r["vs_alt"] for r in runs)],
        "pnl_per_usd_mean": statistics.fmean(r["pnl"] / r["spent"] for r in runs),
        "vs_alt_per_usd_mean": statistics.fmean(r["vs_alt"] / r["spent"] for r in runs),
        "beat_alt_runs": sum(1 for r in runs if r["vs_alt"] > 0)}
    return m


def main() -> int:
    t0 = time.time()
    coins = L.load_archive()
    all_segs = L.segments(coins)
    ranks = L.vol_ranks(all_segs)
    npairs = P.pairs_per_day(ranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot = L.load_hot()
    eps = [ep for ep in L.build_episodes(segs, ranks, hot, "spring") if ep["cycle"] != "?"]
    src = L.signal_days(segs, ranks, hot, {"prod": L.is_spring_prod})["prod"]
    src = [ep for ep in src if ep["cycle"] != "?"]
    for ep in src:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
    alt, _ = P.market_index(ROOT / "scanner.db")
    btc = P.btc_closes(all_segs)
    print(f"эпизодов {len(eps)}, дней сигнала прода {len(src)}  [{time.time() - t0:.0f} с]")
    out: dict = {"meta": {"episodes": len(eps), "signal_days": len(src), "seeds": J.SEEDS,
                          "variants": {k: lab for k, lab, _b, _r in VARIANTS}}}
    for b, (sell, _stop) in J.BOOKS.items():
        name = "R правила" if b == "R" else "H держать"
        print(f"\n=== книга {name} ===")
        print("  эпизоды (горизонт 365):")
        for key, lab, budget, rules in VARIANTS:
            e = episodes(eps, sell, budget, rules)
            out.setdefault(b, {}).setdefault(key, {})["episodes"] = e
            print(f"    {key:5} {lab:44} n {e['n']}  вложено {e['spent']:>7.0f}$  Σ P&L "
                  f"{e['pnl']:>+7.0f}$  на $ {e['pnl_per_usd'] * 100:>+5.1f}%  сред. "
                  f"{e['mean_rod'] * 100:>+5.1f}%  мед. {e['median_rod'] * 100:>+5.1f}%  win "
                  f"{e['win'] * 100:.0f}%  «всё целиком» {e['small']}  одна ступень "
                  f"{e['one_step']} ({e['one_step_pnl']:+.0f}$)  [{time.time() - t0:.0f} с]")
        print("  портфель как исполнитель (15 слотов, без горизонта, потоки):")
        for key, lab, budget, rules in VARIANTS:
            m = portfolio(src, sell, budget, rules, alt, btc, J.SEEDS)
            out[b][key]["portfolio"] = m
            ro = m["random"]
            print(f"    {key:5} поз. {m['n']}  вложено {m['spent']:>6.0f}$  P&L {m['pnl']:>+6.0f}$ "
                  f"({m['pnl'] / m['spent'] * 100:+.1f}% на $)  −альты {m['pnl'] - m['alt']:>+6.0f}$  "
                  f"−BTC {m['pnl'] - m['btc']:>+6.0f}$ | {J.SEEDS} порядков: P&L {ro['pnl_mean']:+.0f}$ "
                  f"({ro['pnl_per_usd_mean'] * 100:+.1f}% на $), −альты {ro['vs_alt_mean']:+.0f}$ "
                  f"({ro['vs_alt_per_usd_mean'] * 100:+.1f}% на $, {ro['vs_alt_range'][0]:+.0f}…"
                  f"{ro['vs_alt_range'][1]:+.0f}), лучше альтов {ro['beat_alt_runs']}/{J.SEEDS}, "
                  f"−BTC {ro['vs_btc_mean']:+.0f}$  [{time.time() - t0:.0f} с]")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
