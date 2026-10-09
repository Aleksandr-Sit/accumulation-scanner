"""Блок B: сводка V0/V1/V2 из результатов прод-бэктестов в копиях code_vX/backtest/*.json.

Сначала: ./run_backtests.sh v0|v1|v2 (market_regime_study, junk_filter_study,
portfolio_vs_market, hot_exit_study), python measure_flags.py, python entry_split.py.
Запуск:  python compare.py
"""
from __future__ import annotations

import json
from pathlib import Path

B = Path(__file__).resolve().parent
V = ("v0", "v1", "v2")
COLD = "холодный рынок (0 флагов)"


def load(v: str, name: str) -> dict:
    return json.loads((B / f"code_{v}" / "backtest" / name).read_text(encoding="utf-8"))


def main() -> None:
    print("=== market_regime_study: P(+100/365д), 0 флагов vs ≥1 (hot_cut_by_cycle) ===")
    for v in V:
        r = load(v, "market_regime_results.json")["entry"]["hot_cut_by_cycle"]
        print(f"  {v}: " + " | ".join(
            f"{c}: холод {x['cold_p']*100:.0f}% n{x['cold_n']} / ≥1 {x['warm_p']*100:.0f}% n{x['warm_n']}"
            for c, x in r.items()))

    print("\n=== junk_filter_study: фильтр «холодный рынок» (эпизоды, 365 дн.) ===")
    for v in V:
        r = load(v, "junk_filter_results.json")
        for b in "RH":
            x = r["filters"][COLD][b]
            print(f"  {v} {b}: оставлено {x['n']} ({x['kept']*100:.0f}%), мед {x['med']*100:+.1f}%, "
                  f"сред {x['mean']*100:+.1f}%, win {x['win']*100:.0f}%, срезано лучших "
                  f"{x['top_cut']*100:.0f}%, Σ P&L срезанных {x['cut_pnl']:+.0f}$")
        for b in "RH":
            p = r["portfolio"][b]
            print(f"  {v} портфель {b}: холодный рынок n {p['холодный рынок']['n']:.0f} P&L "
                  f"{p['холодный рынок']['pnl']:+.0f}$ (все подряд {p['все подряд']['pnl']:+.0f}$)")

    print("\n=== portfolio_vs_market: «как исполнитель» ===")
    for v in V:
        r = load(v, "portfolio_vs_market_results.json")
        for b in "RH":
            m = r[b]["как исполнитель"]
            cyc = "; ".join(f"{c} n{x['n']:.0f} P&L {x['pnl']:+.0f} альты {x['alt']:+.0f} BTC {x['btc']:+.0f}"
                            for c, x in m["cycles"].items())
            print(f"  {v} {b}: n {m['n']:.0f}, вложено {m['spent']:.0f}$, P&L {m['pnl']:+.0f}$, "
                  f"альты {m['alt']:+.0f}$, BTC {m['btc']:+.0f}$, лучше альтов "
                  f"{m['beat_alt_share']*100:.0f}%\n      {cyc}")

    print("\n=== hot_exit_study: портфель «как исполнитель» (холод по флагам варианта) и выход по флагам ===")
    for v in V:
        r = load(v, "hot_exit_results.json")
        P = r["portfolio"]
        print(f"  {v}: cold_entries {r['meta']['cold_entries']}")
        for var in ("H", "H+hot≥3 с OI", "H+hot≥3 без OI", "H+hot≥2 с OI", "H+hot≥2 без OI",
                    "R", "R+hot≥3 с OI", "R+hot≥3 без OI"):
            if var not in P:
                continue
            e = r["episodes"][var]
            cells = " | ".join(f"{pol}: n{P[var][pol]['n']:.0f} {P[var][pol]['pnl']:+.0f}$ "
                               f"(альты {P[var][pol]['alt']:+.0f})"
                               for pol in ("все пружины", "как исполнитель", "исполнитель, холод без OI"))
            print(f"    {var:16} эпизоды Σ {e['pnl']:+.0f}$ мед {e['med']*100:+.1f}% | {cells}")


if __name__ == "__main__":
    main()
