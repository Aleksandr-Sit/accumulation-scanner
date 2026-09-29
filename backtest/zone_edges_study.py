"""Проверка трёх «слабых мест» детектора зоны (stages/zone.py) на истории.

  1. Смешение таймфреймов: deep_dd — от ATH всей истории, range_pos/сжатие — по 180д.
     1a. «Медленное истечение»: пружина, у которой тренд 90д глубоко вниз, но 30д > −15%
         (проходит мимо ножа). Хуже ли она обычной пружины?
     1b. «Выброшенные восстановления»: dd_ath ≥70% + сжатие + не нож, но range_pos 0.5–0.8.
         Хуже ли они пружины (т.е. оправдан ли гейт range_pos ≤ 0.5)?
  2. base_len_days — сумма разрозненных дней у лоу, а не непрерывная база.
     Даёт ли бонус «длинная база ≥50д» то же самое, если база разорвана?
  3. Капитуляционный пролив ≥15% за 30д = НОЖ, даже если уже стабилизировался.
     Что лучше: входить после стабилизации пролива или ждать сигнала пружины?

Данные: Binance daily klines с 2017 (как feature_study), walk-forward (данные <= t).
Пороги детектора — из config.json (stage4_zone), выход — из stage8_exit (как в проде).
ВНИМАНИЕ: survivor-only → все P — ВЕРХНЯЯ граница; сравнивать группы между собой.
Эпизоды одного рыночного периода коррелированы → реальная n меньше номинальной
(в выводе есть число различных кварталов).

Запуск из корня проекта:  py -3 backtest/zone_edges_study.py
Свечи кэшируются в .cache/binance_1d/ (в .gitignore) — повторный прогон без сети.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feature_study import full_history, outcomes, sim_exit, sma, top_symbols  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "binance_1d"
TOP_N = 200
COOLDOWN = 90
MIN_GROUP = 15

CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
Z = CFG["stage4_zone"]
X = CFG["stage8_exit"]
Q = CFG["stage4b_quality"]
LADDER = [tuple(x) for x in X["ladder"]]
TRAIL = X["trailing_from_hwm_pct"] / 100
ARM = X["trailing_arm_after_gain_pct"] / 100
INVAL = X["invalidation_below_base_low_pct"] / 100
CONFIRM = X["invalidation_confirm_days"]


# ---------- данные ----------

def history(sym: str):
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{sym}.json"
    if f.exists() and time.time() - f.stat().st_mtime < 3 * 86400:
        d = json.loads(f.read_text())
        return d["ts"], d["cl"], d["vol"]
    ts, cl, vol = full_history(sym)
    if cl:
        f.write_text(json.dumps({"ts": ts, "cl": cl, "vol": vol}))
    return ts, cl, vol


# ---------- признаки на день t (только данные <= t) ----------

def _runs_near_low(w: list[float], lo: float) -> tuple[int, int, int]:
    """(всего дней ≤ lo*1.15, самый длинный непрерывный отрезок, отрезок, идущий до t)."""
    thr = lo * 1.15
    total = longest = cur = 0
    for p in w:
        if p <= thr:
            total += 1; cur += 1; longest = max(longest, cur)
        else:
            cur = 0
    return total, longest, cur


def feats(cl: list[float], vol: list[float], t: int) -> dict | None:
    if t < 200:
        return None
    c = cl[t]
    w = cl[t - 179:t + 1]
    lo, hi = min(w), max(w)
    rets = [w[i] / w[i - 1] - 1 for i in range(1, len(w)) if w[i - 1] > 0]
    if len(rets) < 60 or lo <= 0:
        return None
    base_sd = statistics.pstdev(rets[:-30])
    if base_sd <= 0:
        return None
    total, longest, now = _runs_near_low(w, lo)
    v = [x for x in vol[t - 179:t + 1] if x > 0]
    vtrend = None
    if len(v) > 40:
        bvol = statistics.fmean(v[:-30])
        vtrend = statistics.fmean(v[-30:]) / bvol if bvol > 0 else None
    return {
        "dd_ath": 1 - c / max(cl[:t + 1]),
        "dd_local": 1 - c / hi,
        "range_pos": (c - lo) / (hi - lo) if hi > lo else 0.5,
        "contr": statistics.pstdev(rets[-30:]) / base_sd,
        "contr10": statistics.pstdev(rets[-10:]) / base_sd,
        "trend10": c / cl[t - 10] - 1,
        "trend30": c / cl[t - 30] - 1,
        "trend90": c / cl[t - 90] - 1,
        "days_since_low": (len(w) - 1) - min(range(len(w)), key=lambda i: w[i]),
        "base_len": total,
        "base_contig": longest,
        "base_now": now,
        "vtrend": vtrend,
    }


# ---------- предикаты ----------

def _deep_squeezed(f):
    return f["dd_ath"] * 100 >= Z["spring_min_drawdown_pct"] and f["contr"] <= Z["vol_contraction_ratio"]


def is_knife(f):
    return f["trend30"] * 100 <= -Z["downtrend_drop_pct"]


def is_spring(f):
    """Точная копия правила ПРУЖИНА/ДНО из zone.classify_zone (dsl-гейт выключен в конфиге)."""
    return (not is_knife(f) and _deep_squeezed(f)
            and f["range_pos"] <= Z["spring_max_range_pos"]
            and f["days_since_low"] >= Z.get("spring_min_days_since_low", 0))


def is_recovery(f):
    """1b: всё как у пружины, но range_pos 0.5–0.8 (сейчас это СЕРЕДИНА)."""
    return (not is_knife(f) and _deep_squeezed(f)
            and Z["spring_max_range_pos"] < f["range_pos"] <= 0.8)


def is_deep_knife(f):
    return is_knife(f) and f["dd_ath"] * 100 >= Z["spring_min_drawdown_pct"]


def is_postcap(f, stab_days=10):
    """3: пролив ≥15% за 30д у глубокого дна, но последние дни стабилизировались:
    нет нового падения за 10д (> −5%) и волатильность 10д сжата к базе."""
    return (is_deep_knife(f) and f["range_pos"] <= 0.3
            and f["trend10"] > -0.05 and f["contr10"] <= Z["vol_contraction_ratio"])


PREDICATES = {"spring": is_spring, "recovery": is_recovery,
              "deep_knife": is_deep_knife, "postcap": is_postcap}


# ---------- статистика ----------

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def net(ep) -> float:
    return sim_exit(ep["cl"], ep["e"], LADDER, TRAIL, ARM, INVAL, CONFIRM)


def summarize(eps: list[dict]) -> dict | None:
    n = len(eps)
    if n == 0:
        return None
    k = sum(ep["o"]["hit100"] for ep in eps)
    lo, hi = wilson(k, n)
    nets = [ep["net"] for ep in eps]
    return {
        "n": n, "coins": len({ep["sym"] for ep in eps}),
        "quarters": len({ep["q"] for ep in eps}),
        "p100": k / n, "p100_ci": (lo, hi),
        "net_mean": statistics.fmean(nets), "net_med": statistics.median(nets),
        "win": sum(x > 0 for x in nets) / n,
        "inval1st": sum(ep["o"]["inval_first"] for ep in eps) / n,
        "btc_dd": statistics.fmean(ep["btc_dd"] for ep in eps),
    }


def row(name: str, eps: list[dict]) -> dict | None:
    s = summarize(eps)
    if s is None or s["n"] < MIN_GROUP:
        print(f"  {name:44} n={len(eps):>4}  (мало для вывода)")
        return s
    print(f"  {name:44} n={s['n']:>4} мон={s['coins']:>3} кв={s['quarters']:>2}  "
          f"P(+100)={s['p100']*100:3.0f}% [{s['p100_ci'][0]*100:2.0f}–{s['p100_ci'][1]*100:2.0f}]  "
          f"net mean {s['net_mean']*100:+6.1f}% med {s['net_med']*100:+6.1f}%  "
          f"win {s['win']*100:3.0f}%  inval1st {s['inval1st']*100:3.0f}%  BTCdd {s['btc_dd']*100:3.0f}%")
    return s


def diff(a: list[dict], b: list[dict], label: str) -> None:
    """Разница P(+100) a−b с 95% CI (нормальное приближение) — при |z|<2 это шум."""
    if len(a) < MIN_GROUP or len(b) < MIN_GROUP:
        return
    pa = sum(e["o"]["hit100"] for e in a) / len(a)
    pb = sum(e["o"]["hit100"] for e in b) / len(b)
    se = math.sqrt(pa * (1 - pa) / len(a) + pb * (1 - pb) / len(b)) or 1e-9
    print(f"    Δ P(+100) {label}: {(pa-pb)*100:+.0f} п.п. ± {1.96*se*100:.0f}  (z={(pa-pb)/se:+.1f})")


def by_btc(eps: list[dict], name: str) -> None:
    """Контроль главного конфаундера: стресс рынка (BTC dd) сильнее всего влияет на P."""
    row(f"{name} | BTC dd <30%", [e for e in eps if e["btc_dd"] < 0.30])
    row(f"{name} | BTC dd ≥30%", [e for e in eps if e["btc_dd"] >= 0.30])


# ---------- main ----------

def main() -> int:
    t0 = time.time()
    bts, bcl, _ = history("BTCUSDT")
    btc_dd, peak = {}, 0.0
    for i, ts in enumerate(bts):
        peak = max(peak, bcl[i]); btc_dd[ts] = 1 - bcl[i] / peak

    syms = [s for s in top_symbols(TOP_N) if s != "BTCUSDT"]
    print(f"символов {len(syms)}; детектор из config: dd≥{Z['spring_min_drawdown_pct']}% "
          f"contr≤{Z['vol_contraction_ratio']} range≤{Z['spring_max_range_pos']} "
          f"нож≤−{Z['downtrend_drop_pct']}%; выход ladder={LADDER} trail {TRAIL:.0%}/arm {ARM:.0%}")

    eps: dict[str, list[dict]] = {k: [] for k in PREDICATES}
    postcap_wait: list[dict] = []
    for i, s in enumerate(syms):
        ts, cl, vol = history(s)
        if len(cl) < 300:
            continue
        F = [feats(cl, vol, t) if t < len(cl) - 30 else None for t in range(len(cl))]
        for key, pred in PREDICATES.items():
            t = 200
            while t < len(cl) - 30:
                f = F[t]
                if f and pred(f):
                    q = time.gmtime(ts[t]); q = f"{q.tm_year}Q{(q.tm_mon - 1) // 3 + 1}"
                    ep = {"sym": s, "e": t, "cl": cl, "f": f, "q": q,
                          "btc_dd": btc_dd.get(ts[t], 0.0), "o": outcomes(cl, t)}
                    ep["net"] = net(ep)
                    eps[key].append(ep)
                    if key == "postcap":
                        # Цена ожидания: когда (и по какой цене) тот же случай стал бы пружиной?
                        sp = next((u for u in range(t + 1, min(t + 61, len(cl) - 30))
                                   if F[u] and is_spring(F[u])), None)
                        postcap_wait.append({"ep": ep, "spring_t": sp,
                                             "px_ratio": cl[sp] / cl[t] if sp else None})
                    t += COOLDOWN
                else:
                    t += 1
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(syms)}  пружин={len(eps['spring'])}  {time.time()-t0:.0f}s")

    spring = eps["spring"]
    res: dict = {}
    print("\n=== БАЗА: пружина по текущему правилу ===")
    res["spring"] = row("пружина (текущее правило)", spring)
    by_btc(spring, "пружина")

    # ---------- 1a. медленное истечение ----------
    print("\n########## 1a. МЕДЛЕННОЕ ИСТЕЧЕНИЕ (тренд 90д глубоко вниз, 30д не нож) ##########")
    for thr in (-0.20, -0.30, -0.40):
        bleed = [e for e in spring if e["f"]["trend90"] <= thr]
        rest = [e for e in spring if e["f"]["trend90"] > thr]
        res[f"bleed_{thr}"] = row(f"пружина, тренд90 ≤ {thr:+.0%}", bleed)
        row(f"пружина, тренд90 > {thr:+.0%}", rest)
        diff(bleed, rest, "истечение − остальные")
    bleed = [e for e in spring if e["f"]["trend90"] <= -0.30]
    by_btc(bleed, "истечение ≤−30%")
    print("  -- сам факт свежего лоу при истечении (dsl<10 и тренд90≤−30%):")
    fresh = [e for e in bleed if e["f"]["days_since_low"] < 10]
    row("истечение + лоу обновлён <10д назад", fresh)
    row("истечение + лоу держится ≥10д", [e for e in bleed if e["f"]["days_since_low"] >= 10])

    # ---------- 1b. выброшенные восстановления ----------
    print("\n########## 1b. ГЕЙТ range_pos ≤ 0.5 — что выбрасывается (0.5–0.8) ##########")
    rec = eps["recovery"]
    res["recovery"] = row("«восстановление» range_pos 0.5–0.8", rec)
    row("пружина (для сравнения)", spring)
    diff(rec, spring, "восстановление − пружина")
    by_btc(rec, "восстановление")
    print("  -- пружина по бакетам range_pos:")
    for lo_, hi_ in ((0, 0.15), (0.15, 0.30), (0.30, 0.50)):
        row(f"range_pos {lo_:.2f}–{hi_:.2f}", [e for e in spring if lo_ < e["f"]["range_pos"] <= hi_ or (lo_ == 0 and e["f"]["range_pos"] == 0)])

    # ---------- 2. разрозненная vs непрерывная база ----------
    print("\n########## 2. ДЛИНА БАЗЫ: сумма дней vs непрерывный отрезок ##########")
    strong, weak = Q["base_len_strong"], Q["base_len_weak"]
    frag = [e["f"]["base_contig"] / e["f"]["base_len"] for e in spring if e["f"]["base_len"]]
    print(f"  доля непрерывного отрезка в base_len: медиана {statistics.median(frag):.2f} "
          f"(1.0 = база сплошная)")
    long_all = [e for e in spring if e["f"]["base_len"] >= strong]
    real = [e for e in long_all if e["f"]["base_contig"] >= strong]
    scat = [e for e in long_all if e["f"]["base_contig"] < strong]
    res["base_long_contig"] = row(f"base_len≥{strong}, непрерывно ≥{strong}", real)
    res["base_long_scattered"] = row(f"base_len≥{strong}, непрерывно <{strong} (ложный бонус)", scat)
    res["base_mid"] = row(f"base_len {weak+1}–{strong-1} (без бонуса/штрафа)",
                          [e for e in spring if weak < e["f"]["base_len"] < strong])
    res["base_short"] = row(f"base_len ≤{weak} (штраф)", [e for e in spring if e["f"]["base_len"] <= weak])
    diff(real, scat, "сплошная − разрозненная")
    print("  -- бакеты по НЕПРЕРЫВНОЙ базе:")
    for lo_, hi_ in ((0, 20), (20, 50), (50, 100), (100, 999)):
        row(f"непрерывно {lo_+1}–{hi_}д", [e for e in spring if lo_ < e["f"]["base_contig"] <= hi_])
    print("  -- бакеты по отрезку, идущему ДО сегодня (base_now):")
    for lo_, hi_ in ((-1, 0), (0, 20), (20, 50), (50, 999)):
        row(f"сейчас у лоу {lo_+1}–{hi_}д подряд", [e for e in spring if lo_ < e["f"]["base_now"] <= hi_])

    # ---------- 3. пролив -> нож ----------
    print("\n########## 3. КАПИТУЛЯЦИЯ: пролив ≥15%/30д, но стабилизация 10д ##########")
    pc = eps["postcap"]
    res["postcap"] = row("пролив + стабилизация (сейчас НОЖ)", pc)
    res["deep_knife"] = row("все глубокие ножи (dd≥70%, тренд30≤−15%)", eps["deep_knife"])
    row("пружина (для сравнения)", spring)
    diff(pc, spring, "пролив+стаб − пружина")
    by_btc(pc, "пролив+стаб")
    became = [w for w in postcap_wait if w["spring_t"] is not None]
    print(f"  стали пружиной за ≤60д: {len(became)}/{len(postcap_wait)} "
          f"({len(became)/max(1,len(postcap_wait))*100:.0f}%)")
    if became:
        waits = [w["spring_t"] - w["ep"]["e"] for w in became]
        ratios = [w["px_ratio"] for w in became]
        cheaper = sum(r < 1 for r in ratios) / len(ratios)
        print(f"  ожидание до сигнала пружины: медиана {statistics.median(waits):.0f}д; "
              f"цена на сигнале пружины / цена на проливе: медиана {statistics.median(ratios):.2f}, "
              f"дешевле в {cheaper*100:.0f}% случаев")
        res["postcap_wait"] = {"n": len(became), "wait_med": statistics.median(waits),
                               "px_ratio_med": statistics.median(ratios), "cheaper": cheaper}
    row("  …из них стали пружиной", [w["ep"] for w in became])
    row("  …так и не стали пружиной за 60д", [w["ep"] for w in postcap_wait if w["spring_t"] is None])

    out = ROOT / "backtest" / "zone_edges_results.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved -> {out.relative_to(ROOT)}  ({time.time()-t0:.0f}s; survivor-upper-bound)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
