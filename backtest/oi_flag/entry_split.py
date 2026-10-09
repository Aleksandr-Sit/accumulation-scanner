"""Блок B, п.2: вход в холодный (0 флагов) и тёплый (≥1) рынок по V0/V1/V2 — по циклам и по годам.

Эпизоды и книги — ровно junk_filter_study (5×$10 до пола, R/H, 365 дн., только с полным
горизонтом или умершие); исходы от варианта не зависят, поэтому считаются один раз (код
code_v0), а метка hot = n_lit дня d−1 берётся из L.load_hot() КАЖДОЙ копии (её config/regime).
Проверка: у каждого варианта hot загрузился (не {}) и у эпизодов нет hot=None.

Запуск:  python entry_split.py   → печать + entry_split_results.json
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

B = Path(__file__).resolve().parent
VARIANTS = ("v0", "v1", "v2")
CYC = ("2020-22", "2023-26")


def dump_hot(v: str) -> dict[int, int]:
    code = ("import sys,json;sys.path.insert(0,'backtest');sys.path.insert(0,'.');"
            "import ladder_dca_study as L;h=L.load_hot();"
            "print(json.dumps({str(k):v for k,v in h.items()}))")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    p = subprocess.run([sys.executable, "-c", code], cwd=B / f"code_{v}", capture_output=True,
                       text=True, encoding="utf-8", env=env)
    h = {int(k): x for k, x in json.loads(p.stdout.strip().splitlines()[-1]).items()}
    assert h, f"{v}: load_hot вернул пусто — {p.stdout[:300]}"
    return h


def st(rows: list[dict], key: str) -> dict:
    if not rows:
        return {"n": 0}
    x = sorted(r[key] for r in rows)
    return {"n": len(x), "mean": statistics.fmean(x), "med": statistics.median(x),
            "win": sum(1 for v in x if v > 0) / len(x), "p100": sum(1 for v in x if v >= 1) / len(x)}


def f(s: dict) -> str:
    if not s.get("n"):
        return f"{'n0':>26}"
    return f"n{s['n']:<4} {s['mean']*100:+6.1f} {s['med']*100:+6.1f} {s['win']*100:3.0f}%"


def main() -> int:
    hots = {v: dump_hot(v) for v in VARIANTS}
    root = B / "code_v0"
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "backtest"))
    import junk_filter_study as J
    import ladder_dca_study as L
    t0 = time.time()
    coins = L.load_archive()
    all_segs = L.segments(coins)
    ranks = L.vol_ranks(all_segs)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    eps_all = [ep for ep in L.build_episodes(segs, ranks, hots["v0"], "spring") if ep["cycle"] != "?"]
    eps = [ep for ep in eps_all if J.complete(ep)]
    res = J.run_books(eps)
    rows = []
    for r_, h_ in zip(res["R"], res["H"]):
        ep = r_["ep"]
        y = time.gmtime(ep["day"]).tm_year
        row = {"day": ep["day"], "year": y, "cycle": ep["cycle"], "R": r_["rob"], "H": h_["rob"]}
        for v in VARIANTS:
            row[v] = hots[v].get(ep["day"] - L.DAY)
        rows.append(row)
    for v in VARIANTS:
        miss = sum(1 for r in rows if r[v] is None and r["year"] >= 2020)
        print(f"{v}: дней с флагами {len(hots[v])}, эпизодов 2020+ без hot: {miss}")
    print(f"эпизодов с полным горизонтом: {len(rows)} [{time.time()-t0:.0f} с]")

    out: dict = {"n": len(rows), "variants": {}}
    for v in VARIANTS:
        o = out["variants"].setdefault(v, {"cycles": {}, "years": {}})
        print(f"\n=== {v}: книга R | книга H — n, средняя %, медиана %, win (0 флагов против ≥1) ===")
        for c in CYC:
            E = [r for r in rows if r["cycle"] == c and r[v] is not None]
            cold = [r for r in E if r[v] == 0]
            warm = [r for r in E if r[v] >= 1]
            o["cycles"][c] = {b: {"cold": st(cold, b), "warm": st(warm, b)} for b in ("R", "H")}
            print(f"  {c}  R холод {f(st(cold,'R'))} | ≥1 {f(st(warm,'R'))}   "
                  f"H холод {f(st(cold,'H'))} | ≥1 {f(st(warm,'H'))}")
        print("  по годам (R):")
        for y in range(2020, 2027):
            E = [r for r in rows if r["year"] == y and r[v] is not None]
            cold = [r for r in E if r[v] == 0]
            warm = [r for r in E if r[v] >= 1]
            o["years"][y] = {b: {"cold": st(cold, b), "warm": st(warm, b)} for b in ("R", "H")}
            print(f"    {y}  холод {f(st(cold,'R'))} | ≥1 {f(st(warm,'R'))}   "
                  f"H холод {f(st(cold,'H'))} | ≥1 {f(st(warm,'H'))}")
        # внутригодовой (стратифицированный) разрыв: средневзвешенная по годам разница медиан
        diffs = []
        for y in range(2020, 2027):
            yy = o["years"][y]["R"]
            if yy["cold"].get("n", 0) >= 15 and yy["warm"].get("n", 0) >= 15:
                diffs.append((yy["cold"]["n"] + yy["warm"]["n"],
                              yy["cold"]["med"] - yy["warm"]["med"],
                              yy["cold"]["mean"] - yy["warm"]["mean"]))
        if diffs:
            w = sum(d[0] for d in diffs)
            o["within_year_R"] = {"years": len(diffs),
                                  "d_med": sum(d[0] * d[1] for d in diffs) / w,
                                  "d_mean": sum(d[0] * d[2] for d in diffs) / w}
            print(f"  внутри года (годы с n≥15 в обеих корзинах: {len(diffs)}): холод − тепло R "
                  f"медиана {o['within_year_R']['d_med']*100:+.1f}пп, средняя "
                  f"{o['within_year_R']['d_mean']*100:+.1f}пп")
    (B / "entry_split_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1,
                                                           default=float), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
