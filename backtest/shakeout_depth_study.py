"""Глубина пробоя базы перед ростом и «ложные» стопы — замер 10.10.2026.

Вопросы:
  1. Монеты из «пружины», которые потом выросли (+50% / +100% от цены сигнала): насколько
     глубоко они закрывались ниже лоу базы ДО роста? Какую долю из них выбил бы стоп
     с буфером 10…70% (как прод: 2 закрытия подряд ниже лоу базы × (1 − буфер))?
  2. Стопы, которые сработали (все и только до первого +50%): какая доля монет потом
     вернулась к цене сигнала, дала +100% от неё или ×2 от цены стопа («ложный стоп»),
     умерла, и что дало бы «держать ещё 730 дн.» вместо продажи по стопу?
  3. Что делает сканер после стопа: был ли новый сигнал «пружина» на той же монете
     и чем закончился вход по нему.

Это замер пути цены, не торговая симуляция: без лестницы, комиссий и частичных продаж.
Цена отсчёта — закрытие дня сигнала (средняя лестницы ниже, так что «+100% от сигнала»
для лестницы — больше +100%). Цели — по закрытиям, не по high (консервативно).

Данные и эпизоды — как в ladder_dca_study (раздел 1–5 LADDER_REPORT): архив Binance
с делистнутыми, сигнал feature_study.is_spring, кулдаун 90 дн., горизонт 730 дн.
Цензура: в доли входят только эпизоды (и стопы), у которых все 730 дн. окна уже есть
в данных (до последнего дня архива), — живые и умершие монеты на равных. Иначе неполные
окна живых монет выпадают, а умерших — остаются, и доля смертей завышается.

Запуск из корня проекта:  python backtest/shakeout_depth_study.py
Результат: backtest/shakeout_depth_results.json
"""
from __future__ import annotations

import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ladder_dca_study import (CYCLES, DAY, HORIZON, build_episodes, load_archive,  # noqa: E402
                              segments, vol_ranks)

OUT = Path(__file__).resolve().parent / "shakeout_depth_results.json"
TARGETS = (0.5, 1.0)                       # +50% / +100% от закрытия дня сигнала
BUFS = (0.10, 0.15, 0.25, 0.35, 0.50, 0.70)
PROD_BUF = 0.25                            # stage8_exit.invalidation_below_base_low_pct
DEPTH_BINS = (0.0, 0.05, 0.15, 0.25, 0.35, 0.50, 0.70)
SAVED_DROP = 0.50                          # после стопа упала ещё на 50% ниже цены стопа
TOP_RANK = 150                             # «ликвидные» ≈ executor.illiquid_share 0.39


def _died_by(ep: dict, day_idx: int) -> bool:
    """Ряд оборвался делистингом (не переименованием) не позже day_idx."""
    s = ep["seg"]
    return not s["alive"] and not s["swap_end"] and len(s["c"]) - 1 <= day_idx


DATA_END = 0                               # последний день архива (ставит main)


def _window_done(ep: dict, start: int) -> bool:
    """Окно HORIZON от дня start целиком лежит в данных архива (смерть внутри — тоже исход)."""
    return ep["seg"]["ts"][start] + HORIZON * DAY <= DATA_END


def depth_before(c: list[float], bl: float, a: int, b: int) -> tuple[float, float]:
    """Глубина под лоу базы на закрытиях дней a..b: (одно закрытие, 2 закрытия подряд).
    Стоп с буфером x срабатывает на этом отрезке ⇔ глубина «2 подряд» > x."""
    if b < a:
        return 0.0, 0.0
    d1 = max(0.0, 1 - min(c[a:b + 1]) / bl)
    if b == a:
        return d1, 0.0
    m2 = min(max(c[i], c[i + 1]) for i in range(a, b))
    return d1, max(0.0, 1 - m2 / bl)


def first_stop(c: list[float], floor: float, a: int, b: int) -> int | None:
    """Первый день, когда второе закрытие подряд ниже пола (как stage8_exit, confirm 2)."""
    for i in range(a + 1, b + 1):
        if c[i - 1] < floor and c[i] < floor:
            return i
    return None


def analyse(ep: dict) -> dict:
    s = ep["seg"]
    c = s["c"]
    e, n = ep["e"], len(c)
    end = min(e + HORIZON, n - 1)
    p0 = c[e]
    bl = min(c[max(0, e - 30):e + 1])
    r: dict = {"p0": p0, "bl": bl, "below_bl_at_entry": p0 / bl - 1,
               "cens": not _window_done(ep, e)}
    for k in TARGETS:
        hit = next((i for i in range(e + 1, end + 1) if c[i] >= p0 * (1 + k)), None)
        last = hit - 1 if hit is not None else end
        d1, d2 = depth_before(c, bl, e + 1, last)
        r[k] = {"hit": hit, "d1": d1, "d2": d2,
                "censored": r["cens"]}
    stops = {}
    for buf in BUFS:
        floor = bl * (1 - buf)
        sd = first_stop(c, floor, e + 1, end)
        if sd is None:
            stops[buf] = None
            continue
        px = c[sd]
        w_end = min(sd + HORIZON, n - 1)
        after = c[sd + 1:w_end + 1]
        done = _window_done(ep, sd)
        ev = {
            "day": sd - e, "px": px, "loss_vs_p0": px / p0 - 1,
            "back_p0": any(x >= p0 for x in after),
            "p50": any(x >= p0 * 1.5 for x in after),
            "p100": any(x >= p0 * 2.0 for x in after),
            "x2_from_stop": any(x >= px * 2.0 for x in after),
            "fell50": any(x <= px * (1 - SAVED_DROP) for x in after),
            "died": _died_by(ep, w_end),
            "done": done,
            # стоп до первого +50% от сигнала — пробой базы, а не обвал после роста
            "pre50": r[0.5]["hit"] is None or sd < r[0.5]["hit"],
            # держать вместо продажи: закрытие через 730 дн. (или последнее перед смертью)
            "final_vs_stop": c[w_end] / px - 1,
        }
        stops[buf] = ev
    r["stops"] = stops
    return r


def share(xs: list[bool]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


def pct(x: float | None) -> str:
    return "  —  " if x is None else f"{x * 100:5.1f}%"


def depth_table(rows: list[dict], k: float) -> dict:
    """Распределение глубины пробоя до цели k у дошедших до неё."""
    win = [r[k] for r in rows if r[k]["hit"] is not None and not r["cens"]]
    out = {"n": len(win)}
    for key in ("d1", "d2"):
        ds = [w[key] for w in win]
        out[key] = {f">{b * 100:.0f}%": share([d > b for d in ds]) for b in DEPTH_BINS}
        if ds:
            q = statistics.quantiles(ds, n=20) if len(ds) >= 20 else None
            out[key]["p50"] = round(statistics.median(ds), 3)
            out[key]["p90"] = round(q[17], 3) if q else None
    return out


def stop_table(rows: list[dict], pre50: bool = False) -> dict:
    """По буферу: доля выбитых будущих раннеров и исходы сработавших стопов.
    pre50 — только стопы до первого +50% от сигнала (пробой базы до роста)."""
    rows = [r for r in rows if not r["cens"]]
    out = {}
    for buf in BUFS:
        res = {}
        for k in TARGETS:
            known = [r for r in rows if not r[k]["censored"]]
            win = [r for r in known if r[k]["hit"] is not None]
            res[f"winners_{k}"] = len(win)
            res[f"killed_{k}"] = share([r[k]["d2"] > buf for r in win])
            # P(цель | стопа до цели не было) против P(цель) — сколько стоп «отбирает»
            res[f"p_hit_{k}"] = share([r[k]["hit"] is not None for r in known])
        evs = [r["stops"][buf] for r in rows if r["stops"][buf] is not None
               and (r["stops"][buf]["pre50"] or not pre50)]
        res["stopped"] = round(len(evs) / len(rows), 3) if rows else None
        fin = [e["final_vs_stop"] for e in evs if e["done"]]
        res["n_final"] = len(fin)
        res["hold_med"] = round(statistics.median(fin), 3) if fin else None
        res["hold_better"] = share([f > 0 for f in fin])
        res["hold_half"] = share([f <= -0.5 for f in fin])
        res["n_stops"] = len(evs)
        res["loss_vs_p0_med"] = (round(statistics.median(e["loss_vs_p0"] for e in evs), 3)
                                 if evs else None)
        for key in ("back_p0", "p50", "p100", "x2_from_stop", "fell50", "died"):
            # событие наступило — считаем; не наступило и окно не дожито — исход неизвестен
            known = [e for e in evs if e["done"]]
            res[key] = share([e[key] for e in known])
            res[f"n_{key}"] = len(known)
        res["saved"] = share([e["fell50"] or e["died"] for e in evs if e["done"]])
        out[buf] = res
    return out


def reentry(eps: list[dict], rows: list[dict]) -> dict:
    """После стопа −25%: новая пружина на той же монете и её исход (+50/+100 от неё)."""
    by_seg: dict[int, list[int]] = {}
    for i, ep in enumerate(eps):
        by_seg.setdefault(id(ep["seg"]), []).append(i)
    n_stop, nxt, gaps, out_rows = 0, 0, [], []
    for i, (ep, r) in enumerate(zip(eps, rows)):
        ev = r["stops"][PROD_BUF]
        if ev is None:
            continue
        n_stop += 1
        sd = ep["e"] + ev["day"]
        later = [j for j in by_seg[id(ep["seg"])] if eps[j]["e"] > sd]
        if not later:
            continue
        j = later[0]
        nxt += 1
        gaps.append(eps[j]["e"] - sd)
        out_rows.append(rows[j])
        rows[j]["_reentry_vs_stop"] = eps[j]["seg"]["c"][eps[j]["e"]] / ev["px"] - 1
    res = {"n_stops": n_stop, "with_new_signal": nxt,
           "gap_days_med": statistics.median(gaps) if gaps else None,
           "new_entry_vs_stop_med": (round(statistics.median(
               r["_reentry_vs_stop"] for r in out_rows), 3) if out_rows else None)}
    for k in TARGETS:
        known = [r for r in out_rows if not r[k]["censored"]]
        res[f"p_hit_{k}"] = share([r[k]["hit"] is not None for r in known])
        res[f"n_{k}"] = len(known)
    return res


def print_depth(title: str, d: dict) -> None:
    print(f"\n  {title} (n={d['n']})")
    print("  глубина под лоу базы      " + " ".join(f"{b:>6}" for b in d["d1"] if b[0] == ">"))
    for key, name in (("d1", "хоть одно закрытие"), ("d2", "2 закрытия подряд")):
        vals = [d[key][b] for b in d[key] if b[0] == ">"]
        print(f"  {name:26}" + " ".join(f"{pct(v):>6}" for v in vals)
              + f"   медиана {pct(d[key].get('p50'))}, p90 {pct(d[key].get('p90'))}")


def print_stops(t: dict) -> None:
    print("  буфер  стопов  выбито+50 выбито+100 | после: к цене сигн.  +100%  ×2 от стопа "
          " умерла | держать 730д: медиана  лучше стопа  ≤−50%")
    for buf, r in t.items():
        print(f"  −{buf * 100:3.0f}%  {pct(r['stopped'])}  {pct(r['killed_0.5'])}    "
              f"{pct(r['killed_1.0'])}   |     {pct(r['back_p0'])}   {pct(r['p100'])}  "
              f"{pct(r['x2_from_stop'])}    {pct(r['died'])} |         {pct(r['hold_med'])}"
              f"      {pct(r['hold_better'])}     {pct(r['hold_half'])}")


def main() -> int:
    t0 = time.time()
    coins = load_archive()
    all_segs = segments(coins)
    ranks = vol_ranks(all_segs)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    eps = [ep for ep in build_episodes(segs, ranks, {}, "spring") if ep["cycle"] != "?"]
    global DATA_END
    DATA_END = max(s["ts"][-1] for s in all_segs)
    rows = [analyse(ep) for ep in eps]
    print(f"Пружин: {len(eps)} на {len({ep['seg']['sym'] for ep in eps})} монетах "
          f"[{time.time() - t0:.0f} с]")
    for k in TARGETS:
        cen = sum(r["cens"] for r in rows)
        hit = sum(r[k]["hit"] is not None and not r["cens"] for r in rows)
        print(f"  +{k * 100:.0f}%: дошли {hit}, не дошли {len(rows) - hit - cen}, "
              f"окно 730 дн. ещё не дожито (в доли не входят) {cen}")
    res: dict = {"meta": {"episodes": len(eps), "horizon": HORIZON, "bufs": BUFS,
                          "date": time.strftime("%Y-%m-%d")}}

    print("\n=== 1. Глубина пробоя ДО роста у монет, дошедших до цели ===")
    res["depth"] = {}
    for k in TARGETS:
        d = depth_table(rows, k)
        res["depth"][str(k)] = d
        print_depth(f"дошли до +{k * 100:.0f}% от сигнала", d)
    nonwin = [r for r in rows if r[1.0]["hit"] is None and not r["cens"]]
    d2n = [r[1.0]["d2"] for r in nonwin]
    res["depth"]["not_hit_1.0"] = {"n": len(nonwin),
                                   "d2": {f">{b * 100:.0f}%": share([d > b for d in d2n])
                                          for b in DEPTH_BINS}}
    print(f"\n  для сравнения — НЕ дошли до +100% (n={len(nonwin)}), 2 закрытия подряд: "
          + " ".join(f"{b} {pct(v)}" for b, v in res["depth"]["not_hit_1.0"]["d2"].items()))

    print("\n=== 2. Стоп по буферу: кого выбивает и что потом с монетой (окно 730 дн. от стопа) ===")
    print("  «выбито» — доля монет, дошедших до цели, у которых до неё сработал бы стоп;")
    print("  «держать 730д» — закрытие через 730 дн. после стопа к цене стопа (умершие — последнее).")
    res["stops"] = {str(b): v for b, v in stop_table(rows).items()}
    print("\n  а) все стопы за 730 дн., включая обвал ПОСЛЕ роста (так стоп работает в книге H):")
    print_stops(stop_table(rows))
    res["stops_pre50"] = {str(b): v for b, v in stop_table(rows, pre50=True).items()}
    print("\n  б) только стопы ДО первого +50% — пробой базы до роста (вопрос замера):")
    print_stops(stop_table(rows, pre50=True))
    p100 = res["stops"][str(PROD_BUF)]["p_hit_1.0"]
    print(f"\n  P(+100% за 730 дн.) у всех пружин: {pct(p100)}")

    print("\n=== 2в. Стопы до +50% по циклам и ликвидности, буфер −25% ===")
    res["by"] = {}
    groups = [(name, [i for i, ep in enumerate(eps) if ep["cycle"] == name])
              for name, _, _ in CYCLES]
    groups += [(f"топ-{TOP_RANK} по обороту", [i for i, ep in enumerate(eps)
                                              if ep["rank"] <= TOP_RANK]),
               (f"вне топ-{TOP_RANK}", [i for i, ep in enumerate(eps) if ep["rank"] > TOP_RANK])]
    for name, idx in groups:
        sub = [rows[i] for i in idx]
        t = stop_table(sub, pre50=True)[PROD_BUF]
        res["by"][name] = t
        print(f"  {name:20} n={len(sub):4}  стопов до +50% {pct(t['stopped'])}  выбито+100 "
              f"{pct(t['killed_1.0'])}  после: +100% {pct(t['p100'])}, ×2 от стопа "
              f"{pct(t['x2_from_stop'])}, умерла {pct(t['died'])}; держать 730д медиана "
              f"{pct(t['hold_med'])}, лучше {pct(t['hold_better'])}")

    print("\n=== 3. Что после стопа −25%: новый сигнал «пружина» на той же монете ===")
    re = reentry(eps, rows)
    res["reentry"] = re
    print(f"  стопов {re['n_stops']}, из них новая пружина позже — {re['with_new_signal']} "
          f"(медиана через {re['gap_days_med']} дн.; цена нового входа к цене стопа "
          f"{pct(re['new_entry_vs_stop_med'])})")
    print(f"  исход входа по новой пружине: +50% {pct(re['p_hit_0.5'])} (n={re['n_0.5']}), "
          f"+100% {pct(re['p_hit_1.0'])} (n={re['n_1.0']})")

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1,
                              default=lambda x: None if isinstance(x, float) and math.isnan(x)
                              else str(x)), encoding="utf-8")
    print(f"\n→ {OUT.name}  [{time.time() - t0:.0f} с]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
