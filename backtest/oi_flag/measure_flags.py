"""Блок B: горение флагов перегрева V0/V1/V2 (п.1) и дни выхода score ≥ 0.375 (п.4).

Каждый вариант считается ПРОД-функциями своей копии кода (code_vX/scanner/regime.py +
code_vX/config.json): market_series → market_feature_table → hot_flags на каждый день
market_daily. Флаги — на сам день d (исполнитель смотрит свежий контекст).

Запуск:  python measure_flags.py            → таблица по трём вариантам + flags_results.json
         python measure_flags.py --variant v0  (внутренний режим, печатает JSON)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

B = Path(__file__).resolve().parent
VARIANTS = ("v0", "v1", "v2")
YEARS = range(2020, 2027)
DAY = 86400


def ymd(d: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(d))


def one(v: str) -> dict:
    root = B / f"code_{v}"
    os.chdir(root)
    sys.path.insert(0, str(root))
    from scanner import regime
    from scanner.config import load_config
    from scanner.db import Store
    cfg = load_config(None)
    st = Store(cfg["output"]["db_path"])
    rows = st.market_rows()
    st.close()
    S = regime.market_series(rows)
    feats = regime.market_feature_table(S)          # alt_dd_min_days=0, как в бэктестах
    hot = {d: regime.hot_flags(f, cfg) for d, f in feats.items()}
    exit_thr = cfg["stage8_exit"].get("market_hot_alert", 0.375)

    def share(days, pred):
        days = [d for d in days if d in hot]
        return (sum(1 for d in days if pred(hot[d])) / len(days)) if days else None, len(days)

    out: dict = {"flags": list(cfg["market_regime"]["hot_flags"]), "exit_thr": exit_thr,
                 "years": {}, "periods": {}}
    for y in YEARS:
        ds = [d for d in hot if time.gmtime(d).tm_year == y and hot[d]["score"] is not None]
        ge1, n = share(ds, lambda h: h["n_lit"] >= 1)
        ge3, _ = share(ds, lambda h: h["n_lit"] >= 3)
        sc, _ = share(ds, lambda h: h["score"] >= exit_thr)
        oi_on, _ = share(ds, lambda h: "oi_rel365" in h["lit"])
        oi_only, _ = share(ds, lambda h: h["lit"] == ["oi_rel365"])
        out["years"][y] = {"n": n, "ge1": ge1, "ge3": ge3, "score_exit": sc,
                           "days_ge3": sum(1 for d in ds if hot[d]["n_lit"] >= 3),
                           "days_exit": sum(1 for d in ds if hot[d]["score"] >= exit_thr),
                           "oi_on": oi_on, "oi_only": oi_only,
                           "avail_mean": sum(hot[d]["avail"] for d in ds) / len(ds) if ds else None}

    def period(name, ds):
        ds = [d for d in ds if d in hot and hot[d]["score"] is not None]
        ge1, n = share(ds, lambda h: h["n_lit"] >= 1)
        ge3, _ = share(ds, lambda h: h["n_lit"] >= 3)
        sc, _ = share(ds, lambda h: h["score"] >= exit_thr)
        oi_only, _ = share(ds, lambda h: h["lit"] == ["oi_rel365"])
        out["periods"][name] = {"n": n, "ge1": ge1, "ge3": ge3, "score_exit": sc,
                                "oi_only": oi_only,
                                "first_ge1": ymd(min((d for d in ds if hot[d]["n_lit"] >= 1), default=0)) if ds else None}

    y22 = [d for d in hot if time.gmtime(d).tm_year == 2022]
    period("2022 дно alt_dd≥0.65", [d for d in y22 if (feats[d].get("alt_dd") or 0) >= 0.65])
    period("2022-06..2022-12", [d for d in y22 if time.gmtime(d).tm_mon >= 6])
    period("2022 весь", y22)
    period("вершины 2021-01..2021-11", [d for d in hot if time.gmtime(d).tm_year == 2021
                                       and time.gmtime(d).tm_mon <= 11])
    period("2024-01..2024-03 (ралли ETF)", [d for d in hot if time.gmtime(d).tm_year == 2024
                                          and time.gmtime(d).tm_mon <= 3])
    period("2024-11..2024-12", [d for d in hot if time.gmtime(d).tm_year == 2024
                                and time.gmtime(d).tm_mon >= 11])
    # ряд oi_rel365 по месяцам (среднее) — для понимания, что меряет флаг
    mon: dict[str, list[float]] = {}
    for d, f in feats.items():
        if "oi_rel365" in f and time.gmtime(d).tm_year >= 2021:
            mon.setdefault(time.strftime("%Y-%m", time.gmtime(d)), []).append(f["oi_rel365"])
    out["oi_rel_monthly"] = {k: round(sum(v) / len(v), 2) for k, v in sorted(mon.items())}
    out["oi_first_day"] = ymd(min(d for d, f in feats.items() if "oi_rel365" in f))
    out["oi_last_day"] = ymd(max(d for d, f in feats.items() if "oi_rel365" in f))
    out["last_day"] = ymd(max(hot))
    out["last_hot"] = hot[max(hot)]
    return out


def pc(x):
    return "  —  " if x is None else f"{x * 100:5.0f}%"


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--variant":
        print(json.dumps(one(sys.argv[2]), ensure_ascii=False, default=str))
        return 0
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    R = {}
    for v in VARIANTS:
        p = subprocess.run([sys.executable, __file__, "--variant", v], capture_output=True,
                           text=True, encoding="utf-8", env=env)
        if p.returncode:
            print(p.stderr)
            return 1
        R[v] = json.loads(p.stdout.strip().splitlines()[-1])
    (B / "flags_results.json").write_text(json.dumps(R, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    for v in VARIANTS:
        print(f"{v}: флагов {len(R[v]['flags'])}; OI-флаг доступен {R[v]['oi_first_day']}…"
              f"{R[v]['oi_last_day']}; последний день {R[v]['last_day']}: "
              f"{R[v]['last_hot']['n_lit']}/{R[v]['last_hot']['avail']} {R[v]['last_hot']['lit']}")
    print("\n1. Горение по годам: ≥1 флаг / ≥3 флага / score≥0.375 (выход) / OI горит / OI единственный")
    print(f"  {'год':6} {'дн.':>4} | " + " | ".join(f"{v:^33}" for v in VARIANTS))
    for y in YEARS:
        y = str(y)
        cells = []
        for v in VARIANTS:
            r = R[v]["years"][y]
            cells.append(f"{pc(r['ge1'])}{pc(r['ge3'])}{pc(r['score_exit'])}"
                         f"{pc(r['oi_on'])}{pc(r['oi_only'])}")
        print(f"  {y:6} {R['v0']['years'][y]['n']:>4} | " + " | ".join(cells))
    print("\n   дни выхода score≥0.375 (и ≥3 флагов) по годам:")
    for y in YEARS:
        y = str(y)
        print(f"  {y}: " + "   ".join(f"{v} {R[v]['years'][y]['days_exit']:>3} ({R[v]['years'][y]['days_ge3']:>3})"
                                    for v in VARIANTS))
    print("\n   периоды: ≥1 флаг / ≥3 / score≥0.375 / OI единственный")
    for name in R["v0"]["periods"]:
        print(f"  {name:32} n{R['v0']['periods'][name]['n']:>4} | " + " | ".join(
            f"{v} {pc(R[v]['periods'][name]['ge1'])}{pc(R[v]['periods'][name]['ge3'])}"
            f"{pc(R[v]['periods'][name]['score_exit'])}{pc(R[v]['periods'][name]['oi_only'])}"
            for v in VARIANTS))
    print("\n   oi_rel365 по месяцам (среднее), V0 монеты / V2 USD:")
    for k in R["v0"]["oi_rel_monthly"]:
        a, b = R["v0"]["oi_rel_monthly"].get(k), R["v2"]["oi_rel_monthly"].get(k)
        if k.endswith(("-01", "-04", "-07", "-10")) or (a and a >= 1.3) or (b and b >= 1.3):
            print(f"    {k}: {a}  {b}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
