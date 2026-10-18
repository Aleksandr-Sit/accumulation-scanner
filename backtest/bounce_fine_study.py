"""Устойчивость гейта «не догонять» (Q17): мелкая сетка порога 0–10%, годы, окно лоу, вне выборки.

Повод — вопрос владельца после Q17 (bounce_gate_study, 18.10.2026): «нужно ли проверить строже?».
R с гейтом «рост от лоу базы ≤ 5%» прошёл правило 1–4 (30 порядков: P&L +$1809 / к альтам
+$123 / на $ +1.2% против базы +$1440 / −$278 / −2.8%), но 5% — край сетки 5/10/15/20/30%,
строже не проверяли. Цель — НЕ подобрать новый порог, а понять форму зависимости: плато (соседние
пороги дают похожее) или одиночный пик на краю сетки (признак подгонки / шума). Порог теневой
книги остаётся 5% (задан заранее), если проверка не покажет, что 5% — случайный пик.

Модель, признак, книги и метрики — как Q17 (импорт bounce_gate_study; его файлы не меняются):
книги R и H шага s6 (15 мест, лестница $50, кулдаун 90 дн., USDT $1500, нижняя четверть по
обороту, перегрев, квота неликвида 5 мест); гейт — до книги, место и деньги отсеянных достаются
следующим сигналам; J.SEEDS случайных порядков внутри дня + прод-порядок по обороту; bounce =
c[e] / лоу базы − 1, лоу — минимум 30 закрытий e−29…e. Сверки: база = база Q17 (она = s6),
пороги 5% и 10% совпадают с bounce_gate_results.json (средние порядков, циклы, годы, прод,
разбор по сериям, правило 1–4).

Что считается (книги R и H; каждый вариант — прод-порядок + J.SEEDS случайных порядков):
  1. Мелкая сетка X = 0 (закрытие дня сигнала — минимум 30 закрытий), 1, 2, …, 10%: P&L, вложено,
     на $, (P&L − альты) и на $, −BTC, лучше базы в K из 30 (P&L, −альты на $), позиций; серии
     (как Q17: сигналы сегмента в 90 дн. от первого) к проду: тот же вход / вошёл позже / пропущено
     / новые; задержка входа от первого сигнала серии и цена входа (open следующего дня) к цене
     входа по первому сигналу серии; умерших за 2 года; P&L без 5 лучших своих позиций; циклы
     2018–20 / 2020–22 / 2023–26; прод-порядок; правило 1–4 Q17 — справочно (для сетки не решает).
  2. Форма: X → Δ(−альты на $) и X → Δ P&L к базе; плато — среднее Δ по 3–7% против точки 5%;
     монотонность на 0–10% (знаки соседних разностей, Спирмен X ↔ Δ).
  3. Годы: для 3 / 5 / 7% — Δ к базе по году входа (P&L и −альты, $, среднее порядков; сколько лет
     из 9 в плюсе) и «без одного года» (leave-one-year-out): Δ −альты на $ и Δ P&L без каждого
     года, попарно по seed (лучше базы в K из 30).
  4. Определение лоу: X = 5% при лоу за 20 / 30 / 60 закрытий. В признаке Q13
     (momentum_order_study.features) окно не параметр — 30 закрытий жёстко, как правило base30
     лестницы исполнителя. Здесь рост от лоу за 20 и 60 закрытий считается прямо по закрытиям
     Binance по день сигнала (у всех сигналов ≥ 200 закрытий истории), а лестница, стоп и пол
     остаются от лоу 30 закрытий, как в проде, — меняется только определение гейта.
  5. Вне выборки: по 2018–22 на сетке 0–10% выбран порог с наибольшим (P&L − альты) на $ среди
     тех, у кого P&L 2018–22 не ниже базы (как Q17), — он же в 2023–26; то же для 3 / 5 / 7%;
     Спирмен формы Δ(X) 2018–22 к 2023–26.
  6. Практичность: по сериям сигналов (без книги) — доля серий, где за 90 дн. ни один сигнал не
     прошёл гейт, и дней от первого сигнала серии до первого прошедшего (ср., медиана, q75, q90),
     серий с входом в год; по книге — задержка входа и доля серий прода, в которые вариант не вошёл.

Вывод о форме (критерий записан до прогона): «случайный пик» — Δ(−альты на $) на 4% или 6% ≤ 0
либо обе меньше половины Δ на 5%; иначе «внутри плато» — у всех 3–7% Δ > 0 и P&L не ниже базы, а
Δ на 5% не больше 1.5 × среднего по 3–7% (с пометкой наклона, если |Спирмен X ↔ Δ на 0–10%| ≥
0.8); иначе «на склоне» — если |Спирмен| ≥ 0.8; иначе «неясно».

Множественная проверка: Q17 — 13 вариантов на книгу (5 порогов, q95, скользящий q80, 3 порядка,
3 квоты); здесь — 9 новых порогов (0–4, 6–9%; 5 и 10% — повтор Q17) и 2 окна лоу на книгу: всего
24 варианта на книгу, 48 на обе книги, из них гейтов по отскоку 18 на книгу. Лучший порог сетки
выбран задним числом — его преимущество завышено; честнее смотреть на плато и вне выборки.

Оговорки — как Q17: архив только Binance (выжившие ×0.73 — цифры верхняя граница); пружина —
детектор прода на закрытиях Binance, а не балл ≥ 70; сигналы монеты в соседние дни почти
одинаковы; правила книг и пороги — на той же истории, вне выборки — один цикл 2023–26; прод
берёт только карточки последних 24 ч — ждать возврата к лоу он не умеет (для живой книги нужен
отложенный вход).

Запуск из корня проекта:  PYTHONIOENCODING=utf-8 python backtest/bounce_fine_study.py
Время прогона: ~5 мин (280 с 18.10.2026, из них ~185 с — архив и сигналы; 14 вариантов × 31 прогон
  × 2 книги); сеть не нужна.
Результат: backtest/bounce_fine_results.json
"""
from __future__ import annotations

import json
import statistics as st
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bounce_gate_study as B  # noqa: E402
import cmc_rank_gate_study as G  # noqa: E402
import junk_filter_study as J  # noqa: E402
import ladder_dca_study as L  # noqa: E402
import portfolio_vs_market as P  # noqa: E402
import supply_age_gate_study as Q  # noqa: E402

OUT = Path(__file__).resolve().parent / "bounce_fine_results.json"
REF_Q17 = Path(__file__).resolve().parent / "bounce_gate_results.json"
DAY = 86400
EPS = B.EPS
CYC = B.CYC
GRID = tuple(i / 100 for i in range(11))          # 0, 1, …, 10%
CHECK = {0.05: "g_le5", 0.10: "g_le10"}          # сверка с Q17
PLATEAU = (0.03, 0.04, 0.05, 0.06, 0.07)
POINT = 0.05
YEAR_X = (0.03, 0.05, 0.07)
WINDOWS = (20, 30, 60)
WIN_X = 0.05
PRACT_X = (0.0, 0.02, 0.05, 0.10)
SLOPE_RHO = 0.8
N_Q17 = {"variants_per_book": 13, "gates_per_book": 7}
ENTRY_KEYS = ("n", "delay_mean", "delay_med", "late_share", "late_delay_mean", "disc_mean",
              "late_disc_mean")
RANDOM_CHECK = ("pnl_mean", "pnl_min", "pnl_max", "spent_mean", "pnl_per_usd", "died2y_mean",
                "beat_base_pnl", "beat_base_ex_alt", "beat_base_ex_alt_usd", "ex_alt_mean",
                "ex_alt_per_usd", "ex_cap_mean", "ex_btc_mean", "is_pnl", "is_ex_alt_usd", "oos_pnl",
                "oos_ex_alt_usd", "oos_beat_base_ex_alt_usd", "pnl_ex_top5_mean", "beat_base_pnl_ex_top5")


# ---------------------------------------------------------------- признак, варианты

def xkey(x: float) -> str:
    return f"x{round(x * 100)}"


def bounce_w(ep: dict, w: int) -> float | None:
    """Рост закрытия дня сигнала от минимума последних w закрытий (w = 30 — признак Q13)."""
    c, e = ep["seg"]["c"], ep["e"]
    lo = min(c[e - w + 1:e + 1])
    return c[e] / lo - 1 if lo > 0 else None


def gate_w(x: float, w: int):
    """Гейт «не догонять» по лоу за w закрытий; неизвестный — пропускает (как B.le)."""
    return lambda ep: ep["bw"][w] is None or ep["bw"][w] <= x + EPS


def variants() -> list[dict]:
    vs = [{"key": "base", "label": "база (прод)", "x": None, "w": None, "g": None}]
    for x in GRID:
        vs.append({"key": xkey(x), "label": f"от лоу ≤ {x:.0%}", "x": x, "w": 30, "g": B.le(x)})
    for w in WINDOWS:
        if w != 30:
            vs.append({"key": f"w{w}", "label": f"лоу {w} закр. ≤ {WIN_X:.0%}", "x": WIN_X, "w": w,
                       "g": gate_w(WIN_X, w)})
    return vs


def mean_of(dicts: list[dict], k: str) -> float | None:
    vals = [d[k] for d in dicts if d.get(k) is not None]
    return st.fmean(vals) if vals else None


# ---------------------------------------------------------------- серии и вход

def entry_stats(taken, ser: dict, by_key: dict) -> dict:
    """Задержка входа от первого сигнала серии (дн.) и цена входа (open следующего дня) к цене
    входа по первому сигналу серии − 1 (отрицательная — дешевле)."""
    dl, ds = [], []
    for ep, _r in taken:
        sym, start = ser[G.key_of(ep)]
        e0 = by_key[(sym, start)]["e"]
        o = ep["seg"]["o"]
        dl.append((ep["day"] - start) / DAY)
        ds.append(o[ep["e"] + 1] / o[e0 + 1] - 1)
    late = [(d, p) for d, p in zip(dl, ds) if d > 0]
    return {"n": len(dl), "delay_mean": st.fmean(dl) if dl else None,
            "delay_med": st.median(dl) if dl else None,
            "late_share": len(late) / len(dl) if dl else None,
            "late_delay_mean": st.fmean(d for d, _p in late) if late else None,
            "disc_mean": st.fmean(ds) if ds else None,
            "late_disc_mean": st.fmean(p for _d, p in late) if late else None}


def decomp_row(sd_: dict) -> dict:
    """B.series_decomp в плоском виде."""
    mv = sd_["moved"]
    return {"same_entry": sd_["same_entry"], "moved": mv["n"], "moved_delay": mv["delay_days"],
            "moved_price": mv["price_ratio"], "moved_pnl_base": mv["pnl_base"], "moved_pnl_var": mv["pnl_var"],
            "only_base": sd_["only_base"]["n"], "only_base_pnl": sd_["only_base"]["pnl"],
            "only_var": sd_["only_var"]["n"], "only_var_pnl": sd_["only_var"]["pnl"]}


def series_wait(sig: list[dict], ser: dict, gate, span_years: float) -> dict:
    """Без книги: по сериям сигналов (прошедших фильтры прода) — доля серий без единого сигнала,
    прошедшего гейт, и дней от первого сигнала серии до первого прошедшего."""
    by: dict = {}
    for ep in sig:
        by.setdefault(ser[G.key_of(ep)], []).append(ep)
    waits, never = [], 0
    for (_sym, start), ee in by.items():
        ok = [ep["day"] for ep in ee if gate(ep)]
        if ok:
            waits.append((min(ok) - start) / DAY)
        else:
            never += 1
    ws = sorted(waits)
    return {"series": len(by), "entered": len(ws), "never": never, "never_share": never / len(by),
            "wait_mean": st.fmean(ws) if ws else None, "wait_med": B.quantile(ws, 0.5) if ws else None,
            "wait_q75": B.quantile(ws, 0.75) if ws else None, "wait_q90": B.quantile(ws, 0.90) if ws else None,
            "wait0_share": sum(1 for w in ws if w == 0) / len(ws) if ws else None,
            "series_per_year": len(by) / span_years, "entered_per_year": len(ws) / span_years,
            "signals_pass": sum(1 for ep in sig if gate(ep))}


# ---------------------------------------------------------------- метрики книги

def prod_row(fl, taken, base_m: dict | None) -> dict:
    pv = fl.vs(taken)
    m = {k: pv[k] for k in ("n", "spent", "pnl", "alt", "cap", "btc")}
    m["pnl_per_usd"] = pv["pnl"] / pv["spent"] if pv["spent"] else 0.0
    m["ex_alt"] = pv["pnl"] - pv["alt"]
    m["ex_alt_usd"] = m["ex_alt"] / pv["spent"] if pv["spent"] else 0.0
    m["ex_btc"] = pv["pnl"] - pv["btc"]
    m["died2y"] = sum(1 for ep, _r in taken if ep["died2y"])
    m["pnl_ex_top5"] = B.ex_top(taken)
    m["cycles"] = {c: {"n": v["n"], "spent": v["spent"], "pnl": v["pnl"], "ex_alt": v["pnl"] - v["alt"],
                       "ex_alt_usd": (v["pnl"] - v["alt"]) / v["spent"] if v["spent"] else 0.0}
                   for c, v in pv["cycles"].items()}
    if base_m is not None:
        m["vs_prod"] = {"pnl": m["pnl"] - base_m["pnl"], "ex_alt": m["ex_alt"] - base_m["ex_alt"],
                        "ex_alt_usd": m["ex_alt_usd"] - base_m["ex_alt_usd"]}
    return m


def check_q17(b: str, key: str, mine: dict, q: dict) -> None:
    """Порог 5% / 10% и база совпадают с bounce_gate_results.json."""
    r, m = mine["random"], mine["prod"]
    for k in RANDOM_CHECK:
        assert abs(r[k] - q["random"][k]) < 1e-6, f"{b} {key}: {k} {r[k]} != Q17 {q['random'][k]}"
    for c in CYC:
        assert abs(r["cycles"][c]["ex_alt_usd"] - q["random"]["cycles"][c]["ex_alt_usd"]) < 1e-9, \
            f"{b} {key}: цикл {c} != Q17"
    for y, v in q["random"]["years"].items():
        assert all(abs(r["years"][y][k] - v[k]) < 1e-6 for k in v), f"{b} {key}: год {y} != Q17"
    for k in ("n", "spent", "pnl", "alt", "btc", "died2y", "pnl_ex_top5"):
        assert abs(m[k] - q[k]) < 1e-6, f"{b} {key}: прод {k} {m[k]} != Q17 {q[k]}"
    if "series" in q:
        s, qs = m["series"], q["series"]
        assert (s["same_entry"], s["moved"], s["only_base"], s["only_var"]) == \
            (qs["same_entry"], qs["moved"]["n"], qs["only_base"]["n"], qs["only_var"]["n"]), \
            f"{b} {key}: серии != Q17"
    if "rule" in q:
        assert r["rule"] == q["rule"], f"{b} {key}: правило {r['rule']} != Q17 {q['rule']}"


# ---------------------------------------------------------------- годы, форма, вне выборки

def year_tot(yd: dict, skip: str | None) -> tuple[float, float, float]:
    sp = sum(v["spent"] for y, v in yd.items() if y != skip)
    pn = sum(v["pnl"] for y, v in yd.items() if y != skip)
    al = sum(v["alt"] for y, v in yd.items() if y != skip)
    return sp, pn, al


def years_block(yv: list[dict], yb: list[dict], years: list[str]) -> dict:
    """Δ к базе по году входа и «без одного года», попарно по seed (B.years_of каждого прогона)."""
    def g(yd, y, k):
        return yd.get(y, {}).get(k, 0.0)
    per_year = {}
    for y in years:
        dp = [g(a, y, "pnl") - g(b_, y, "pnl") for a, b_ in zip(yv, yb)]
        dx = [(g(a, y, "pnl") - g(a, y, "alt")) - (g(b_, y, "pnl") - g(b_, y, "alt")) for a, b_ in zip(yv, yb)]
        per_year[y] = {"d_pnl": st.fmean(dp), "d_ex_alt": st.fmean(dx),
                       "beat_pnl": sum(1 for v in dp if v > 0), "beat_ex_alt": sum(1 for v in dx if v > 0)}
    loyo = {}
    for skip in [None] + years:
        du, dp, dx = [], [], []
        for a, b_ in zip(yv, yb):
            sa, pa, aa = year_tot(a, skip)
            sb, pb, ab = year_tot(b_, skip)
            du.append((pa - aa) / sa - (pb - ab) / sb)
            dp.append(pa - pb)
            dx.append((pa - aa) - (pb - ab))
        loyo["all" if skip is None else skip] = {
            "d_ex_alt_usd": st.fmean(du), "beat_usd": sum(1 for v in du if v > 0),
            "d_pnl": st.fmean(dp), "beat_pnl": sum(1 for v in dp if v > 0), "d_ex_alt": st.fmean(dx)}
    lo = {y: loyo[y] for y in years}
    wu = min(lo, key=lambda y: lo[y]["d_ex_alt_usd"])
    wp = min(lo, key=lambda y: lo[y]["d_pnl"])
    return {"per_year": per_year, "loyo": loyo,
            "years_pos_ex_alt": sum(1 for v in per_year.values() if v["d_ex_alt"] > 0),
            "years_pos_pnl": sum(1 for v in per_year.values() if v["d_pnl"] > 0), "n_years": len(years),
            "loyo_all_pos_usd": all(v["d_ex_alt_usd"] > 0 for v in lo.values()),
            "loyo_all_pos_pnl": all(v["d_pnl"] >= 0 for v in lo.values()),
            "loyo_worst_usd": {"year": wu, **lo[wu]}, "loyo_worst_pnl": {"year": wp, **lo[wp]}}


def shape_block(R_: dict, rb: dict) -> dict:
    xs = list(GRID)
    keys = [xkey(x) for x in xs]
    d_usd = [R_[k]["ex_alt_per_usd"] - rb["ex_alt_per_usd"] for k in keys]
    d_pnl = [R_[k]["pnl_mean"] - rb["pnl_mean"] for k in keys]
    d_is = [R_[k]["is_ex_alt_usd"] - rb["is_ex_alt_usd"] for k in keys]
    d_oos = [R_[k]["oos_ex_alt_usd"] - rb["oos_ex_alt_usd"] for k in keys]
    at = {x: d_usd[i] for i, x in enumerate(xs)}
    pl = [at[x] for x in PLATEAU]
    plp = [d_pnl[xs.index(x)] for x in PLATEAU]
    d5, d4, d6 = at[POINT], at[0.04], at[0.06]
    diffs = [d_usd[i + 1] - d_usd[i] for i in range(len(xs) - 1)]
    rho = G.spearman(xs, d_usd)
    peak = d5 <= 0 or d4 <= 0 or d6 <= 0 or (d4 < 0.5 * d5 and d6 < 0.5 * d5)
    plateau_ok = all(v > 0 for v in pl) and all(v >= -1e-9 for v in plp) and d5 <= 1.5 * st.fmean(pl)
    slope = abs(rho) >= SLOPE_RHO
    tilt = "строже — лучше" if rho < 0 else "мягче — лучше"
    if peak:
        verdict = "случайный пик" if d5 > 0 else "эффекта на 5% нет"
    elif plateau_ok:
        verdict = "внутри плато" + (f" (наклон: {tilt})" if slope else "")
    elif slope:
        verdict = f"на склоне ({tilt})"
    else:
        verdict = "неясно"
    imax = max(range(len(xs)), key=lambda i: d_usd[i])
    return {"x": xs, "d_ex_alt_usd": d_usd, "d_pnl": d_pnl, "d_is_ex_alt_usd": d_is, "d_oos_ex_alt_usd": d_oos,
            "plateau_mean": st.fmean(pl), "plateau_min": min(pl), "plateau_max": max(pl),
            "plateau_pnl_mean": st.fmean(plp), "plateau_pnl_min": min(plp),
            "point": d5, "point_to_plateau": d5 / st.fmean(pl) if st.fmean(pl) else None,
            "d4": d4, "d6": d6, "rho": rho, "n_down": sum(1 for d in diffs if d < 0),
            "n_up": sum(1 for d in diffs if d > 0), "monotone": all(d <= 0 for d in diffs) or all(d >= 0 for d in diffs),
            "argmax": xs[imax], "max": d_usd[imax], "best_minus_plateau": d_usd[imax] - st.fmean(pl),
            "rho_is_oos": G.spearman(d_is, d_oos),
            "n_pos": sum(1 for u, p in zip(d_usd, d_pnl) if u > 0 and p >= 0),
            "peak": peak, "plateau_ok": plateau_ok, "slope": slope, "verdict": verdict}


def oos_block(R_: dict, rb: dict) -> dict:
    """Как Q17: по 2018–22 — наибольший (P&L − альты) на $ среди порогов с P&L 2018–22 не ниже
    базы (нет таких — наибольший, с пометкой); он же и фиксированные 3 / 5 / 7% в 2023–26."""
    keys = [xkey(x) for x in GRID]
    ok = [k for k in keys if R_[k]["is_pnl"] >= rb["is_pnl"] - 1e-9]
    pick = max(ok or keys, key=lambda k: R_[k]["is_ex_alt_usd"])

    def d(k):
        r = R_[k]
        return {"is_pnl": r["is_pnl"] - rb["is_pnl"], "is_ex_alt": r["is_ex_alt"] - rb["is_ex_alt"],
                "is_ex_alt_usd": r["is_ex_alt_usd"] - rb["is_ex_alt_usd"], "is_beat_usd": r["is_beat_base_ex_alt_usd"],
                "oos_pnl": r["oos_pnl"] - rb["oos_pnl"], "oos_ex_alt": r["oos_ex_alt"] - rb["oos_ex_alt"],
                "oos_ex_alt_usd": r["oos_ex_alt_usd"] - rb["oos_ex_alt_usd"], "oos_ex_btc": r["oos_ex_btc"] - rb["oos_ex_btc"],
                "oos_beat_usd": r["oos_beat_base_ex_alt_usd"], "oos_beat_pnl": r["oos_beat_base_pnl"]}
    is_rank = sorted(keys, key=lambda k: -R_[k]["is_ex_alt_usd"])
    oos_rank = sorted(keys, key=lambda k: -R_[k]["oos_ex_alt_usd"])
    return {"pick": pick, "pnl_ok": bool(ok), "n_ok": len(ok), "picked": d(pick),
            "fixed": {xkey(x): d(xkey(x)) for x in YEAR_X},
            "rank_is_5": is_rank.index(xkey(POINT)) + 1, "rank_oos_5": oos_rank.index(xkey(POINT)) + 1,
            "best_oos": oos_rank[0]}


# ---------------------------------------------------------------- печать

def pp(x: float | None, w: int = 0, nd: int = 1) -> str:
    """Доля в процентных пунктах со знаком."""
    return (f"{x * 100:+.{nd}f}" if x is not None else "—").rjust(w)


def num(x: float | None, w: int = 0, fmt: str = "+.0f") -> str:
    return (f"{x:{fmt}}" if x is not None else "—").rjust(w)


# ---------------------------------------------------------------- книга

def analyze_book(b: str, sell: dict, D: dict, sig: list[dict], ser: dict, VARIANTS: list[dict],
                 ref17: dict, waits: dict, t0: float) -> dict:
    src = D["src"]
    base_take, illiq = P.EXEC_POLICY
    name = "R правила" if b == "R" else "H держать"
    cache: dict = {}

    def sim(ep):
        k = (ep["seg"]["sym"], ep["e"])
        if k not in cache:
            cache[k] = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET, horizon=None,
                                       rules=L.EXEC_ALL)
        return cache[k]
    fl = Q.Flows(D["series"])
    by_key = {G.key_of(ep): ep for ep in src}
    q17 = ref17[b]["variants"]
    same_seeds = ref17["meta"]["seeds"] == J.SEEDS
    res: dict = {}
    keep_years: dict = {}
    base_prod = base_runs = base_rows = base_xt = base_m = base_r = None
    checked = []
    print(f"\n{'=' * 100}\nКНИГА {name}  [{time.time() - t0:.0f} с]")
    for v in VARIANTS:
        is_base = v["key"] == "base"
        take = base_take if is_base else (lambda ep, g=v["g"]: base_take(ep) and g(ep))
        prod = P.exec_book(src, sim, take, illiq, cooldown=True, cash=True, order="rank", seed=0)
        runs = [P.exec_book(src, sim, take, illiq, cooldown=True, cash=True, order="random", seed=sd)
                for sd in range(J.SEEDS)]
        vv = [fl.vs(tk) for tk in runs]
        rows = [G.seed_row(x) for x in vv]
        xt = [B.ex_top(tk) for tk in runs]
        if is_base:
            base_prod, base_runs, base_rows, base_xt = prod, runs, rows, xt
        died = [sum(1 for ep, _r in tk if ep["died2y"]) for tk in runs]
        r = B.summ(rows, base_rows, died)
        r["n_mean"] = st.fmean(x["n"] for x in vv)
        r["pnl_ex_top5_mean"] = st.fmean(xt)
        r["beat_base_pnl_ex_top5"] = sum(1 for a_, b_ in zip(xt, base_xt) if a_ > b_)
        yy = [B.years_of(fl, tk) for tk in runs]
        r["years"] = B.years_mean(yy)
        if is_base or (v["w"] == 30 and v["x"] in YEAR_X):
            keep_years[v["key"]] = yy
        r["entry"] = {k: mean_of([entry_stats(tk, ser, by_key) for tk in runs], k) for k in ENTRY_KEYS}
        m = prod_row(fl, prod, base_m)
        m["entry"] = entry_stats(prod, ser, by_key)
        if is_base:
            base_m, base_r = m, r
        else:
            m["series"] = decomp_row(B.series_decomp(base_prod, prod, ser))
            dd = [decomp_row(B.series_decomp(bt, tk, ser)) for bt, tk in zip(base_runs, runs)]
            r["series"] = {k: mean_of(dd, k) for k in dd[0]}
            r["rule"] = B.rule(r, base_r)
        res[v["key"]] = {"label": v["label"], "x": v["x"], "w": v["w"], "prod": m, "random": r}
        qk = "base" if is_base else (CHECK.get(v["x"]) if v["w"] == 30 else None)
        if qk and same_seeds:
            check_q17(b, v["key"], res[v["key"]], q17[qk])
            checked.append(qk)
    print(f"  вариантов {len(VARIANTS)} × {J.SEEDS + 1} прогонов; сверка с Q17 ({', '.join(checked) or 'пропущена'}): "
          f"средние {J.SEEDS} порядков, циклы, годы, прод, серии, правило 1–4 совпали ✓  [{time.time() - t0:.0f} с]")

    rb, mb = res["base"]["random"], res["base"]["prod"]
    grid_keys = [xkey(x) for x in GRID]
    R_ = {k: res[k]["random"] for k in res}

    # ---- A) средние порядков
    print(f"  A) средние {J.SEEDS} случайных порядков; «лучше б.» — в скольких из {J.SEEDS} лучше базы того же "
          f"порядка: P&L / −альты на $; циклы — −альты на $; без 5 — P&L без 5 лучших своих позиций (лучше базы "
          f"в K); † — умерших за 2 года; 1234 — правило Q17 (справочно, сетку не решает)")
    print(f"  {'вариант':20} {'поз.':>4} {'влож.':>6} {'P&L':>6} {'на $':>6} {'−альты':>6} {'на $':>6} {'−BTC':>6} "
          f"{'лучше б.':>8} " + " ".join(f"{c:>7}" for c in CYC) + f" {'без 5':>10} {'†':>4} {'1234':>5}")
    for k in ["base"] + grid_keys + [f"w{w}" for w in WINDOWS if w != 30]:
        x, r = res[k], res[k]["random"]
        ru = r.get("rule")
        flags = "".join("+" if ru[c] else "·" for c in ("c1_pnl", "c2_ex_alt_usd", "c3_cycles", "c4_oos")) if ru else ""
        print(f"  {x['label']:20} {r['n_mean']:>4.0f} {r['spent_mean']:>6.0f} {r['pnl_mean']:>+6.0f} "
              f"{B.pct(r['pnl_per_usd'], 6)} {r['ex_alt_mean']:>+6.0f} {B.pct(r['ex_alt_per_usd'], 6)} "
              f"{r['ex_btc_mean']:>+6.0f} " + f"{r['beat_base_pnl']}/{r['beat_base_ex_alt_usd']}".rjust(8) + " "
              + " ".join(B.pct(r["cycles"][c]["ex_alt_usd"], 7) for c in CYC)
              + f"{r['pnl_ex_top5_mean']:+.0f} ({r['beat_base_pnl_ex_top5']})".rjust(11)
              + f" {r['died2y_mean']:>4.1f} {flags:>5}")

    # ---- B) прод-порядок и серии
    print("  B) прод-порядок (по обороту): позиций, P&L, −альты (на $), к проду P&L / −альты; серии к проду: тот же "
          "вход | позже (ср. дней, цена 1-й ступени к проду) | пропущено серий прода (их P&L в проде) | новые (P&L); "
          "от первого сигнала серии: задержка ср. / мед. дн., доля позже, цена входа к входу по 1-му сигналу; "
          f"{J.SEEDS} порядков: задержка ср., пропущено / новые серий к базе того же порядка")
    for k in ["base"] + grid_keys + [f"w{w}" for w in WINDOWS if w != 30]:
        x, m, r = res[k], res[k]["prod"], res[k]["random"]
        e_, er = m["entry"], r["entry"]
        vp = m.get("vs_prod", {"pnl": 0.0, "ex_alt": 0.0})
        s_ = m.get("series")
        if s_:
            ser_txt = (f"{s_['same_entry']:>3} | {s_['moved']:>3}" +
                       (f" ({s_['moved_delay']:+.0f} дн. ×{s_['moved_price']:.3f})" if s_["moved"] else " " * 15) +
                       f" | {s_['only_base']:>3} ({s_['only_base_pnl']:+5.0f}$)"
                       f" | {s_['only_var']:>3} ({s_['only_var_pnl']:+5.0f}$)")
        else:
            ser_txt = " " * 58
        rs_ = r.get("series")
        rnd_txt = f"{er['delay_mean']:.1f} дн." + (f", {rs_['only_base']:.0f}/{rs_['only_var']:.0f}" if rs_ else "")
        print(f"  {x['label']:20} {m['n']:>4} {m['pnl']:>+6.0f} {m['ex_alt']:>+6.0f} ({B.pct(m['ex_alt_usd'])}) "
              + f"{vp['pnl']:+.0f}/{vp['ex_alt']:+.0f}".rjust(10) + f"  {ser_txt} | {e_['delay_mean']:>4.1f}/"
              f"{e_['delay_med']:>2.0f} {e_['late_share']:>4.0%} {B.pct(e_['disc_mean'])} | {rnd_txt}")

    # ---- C) форма
    sh = shape_block(R_, rb)
    print(f"  C) форма: X → Δ к базе (средние {J.SEEDS} порядков): −альты на $ (п.п.), P&L ($); −альты на $ в "
          f"2018–22 и 2023–26 (п.п.); лучше базы по −альтам на $ (из {J.SEEDS})")
    print("     X           " + " ".join(f"{x:>6.0%}" for x in GRID))
    print("     −альты/$    " + " ".join(pp(d, 6) for d in sh["d_ex_alt_usd"]))
    print("     P&L $       " + " ".join(num(d, 6) for d in sh["d_pnl"]))
    print("     2018–22     " + " ".join(pp(d, 6) for d in sh["d_is_ex_alt_usd"]))
    print("     2023–26     " + " ".join(pp(d, 6) for d in sh["d_oos_ex_alt_usd"]))
    print("     лучше/30    " + " ".join(f"{R_[k]['beat_base_ex_alt_usd']:>6}" for k in grid_keys))
    print(f"     плато 3–7%: Δ −альты на $ ср. {pp(sh['plateau_mean'])} п.п. (мин. {pp(sh['plateau_min'])}, макс. "
          f"{pp(sh['plateau_max'])}), Δ P&L ср. {sh['plateau_pnl_mean']:+.0f}$ (мин. {sh['plateau_pnl_min']:+.0f}$); "
          f"точка 5% {pp(sh['point'])} п.п. (×{sh['point_to_plateau']:.2f} к плато), соседи 4% {pp(sh['d4'])}, "
          f"6% {pp(sh['d6'])}; Спирмен X↔Δ на 0–10% {sh['rho']:+.2f} (разностей вниз/вверх {sh['n_down']}/{sh['n_up']}"
          f"{', монотонно' if sh['monotone'] else ''}); максимум {pp(sh['max'])} п.п. на {sh['argmax']:.0%} "
          f"(выше плато на {pp(sh['best_minus_plateau'])} п.п. — завышение выбора); Спирмен формы 2018–22 к 2023–26 "
          f"{sh['rho_is_oos']:+.2f}; Δ > 0 и P&L ≥ базы у {sh['n_pos']} из {len(GRID)} порогов")
    print(f"     ВЫВОД о форме (критерий до прогона): 5% — {sh['verdict']}")

    # ---- D) годы
    years = sorted(rb["years"])
    yb = keep_years["base"]
    yblocks = {xkey(x): years_block(keep_years[xkey(x)], yb, years) for x in YEAR_X}
    print(f"  D) по году входа: база P&L / −альты ($), пороги — Δ к базе P&L / −альты ($), среднее {J.SEEDS} порядков")
    print(f"     {'год':6} {'база':>11} " + " ".join(f"{'≤ ' + format(x, '.0%'):>13}" for x in YEAR_X))
    for y in years:
        by_ = rb["years"][y]
        cells = [f"{yblocks[xkey(x)]['per_year'][y]['d_pnl']:+.0f}/{yblocks[xkey(x)]['per_year'][y]['d_ex_alt']:+.0f}"
                 for x in YEAR_X]
        print(f"     {y:6} " + f"{by_['pnl']:+.0f}/{by_['pnl'] - by_['alt']:+.0f}".rjust(11) + " "
              + " ".join(c.rjust(13) for c in cells))
    print("     лет в плюсе (Δ −альты / Δ P&L): " + "; ".join(
        f"≤ {x:.0%} {yblocks[xkey(x)]['years_pos_ex_alt']}/{yblocks[xkey(x)]['years_pos_pnl']} из {len(years)}"
        for x in YEAR_X))
    print("     без одного года: Δ −альты на $ п.п. (лучше базы в K/30) / Δ P&L $")
    print(f"     {'без':6} " + " ".join(f"{'≤ ' + format(x, '.0%'):>20}" for x in YEAR_X))
    for skip in ["all"] + years:
        cells = []
        for x in YEAR_X:
            o = yblocks[xkey(x)]["loyo"][skip]
            cells.append(f"{pp(o['d_ex_alt_usd'])} ({o['beat_usd']}) / {o['d_pnl']:+.0f}")
        print(f"     {('все' if skip == 'all' else skip):6} " + " ".join(c.rjust(20) for c in cells))
    for x in YEAR_X:
        yk = yblocks[xkey(x)]
        assert abs(yk["loyo"]["all"]["d_ex_alt_usd"] - (R_[xkey(x)]["ex_alt_per_usd"] - rb["ex_alt_per_usd"])) < 1e-9, \
            "годы не складываются в итог"
    print("     без любого года: " + "; ".join(
        f"≤ {x:.0%} −альты на $ {'всегда > 0' if yk['loyo_all_pos_usd'] else 'НЕ всегда > 0'} (худший без "
        f"{yk['loyo_worst_usd']['year']} {pp(yk['loyo_worst_usd']['d_ex_alt_usd'])} п.п.), P&L "
        f"{'всегда ≥ базы' if yk['loyo_all_pos_pnl'] else 'НЕ всегда ≥ базы'} (худший без {yk['loyo_worst_pnl']['year']} "
        f"{yk['loyo_worst_pnl']['d_pnl']:+.0f}$)" for x, yk in ((x, yblocks[xkey(x)]) for x in YEAR_X)))

    # ---- E) окно лоу
    print(f"  E) лоу базы за 20 / 30 / 60 закрытий при X = {WIN_X:.0%} (лестница, стоп и пол — от лоу 30, как прод): "
          f"сигналов ≤ X, серий без входа (без книги); средние {J.SEEDS} порядков: поз., P&L, −альты на $, лучше базы "
          f"P&L / −альты на $, циклы −альты на $, 2023–26 лучше базы, без 5")
    win = {}
    for w in WINDOWS:
        k = xkey(WIN_X) if w == 30 else f"w{w}"
        r, sw = R_[k], waits["windows"][str(w)]
        win[str(w)] = {"key": k, "signals_share": sw["signals_pass"] / len(sig), "never_share": sw["never_share"],
                       "d_pnl": r["pnl_mean"] - rb["pnl_mean"], "d_ex_alt_usd": r["ex_alt_per_usd"] - rb["ex_alt_per_usd"]}
        print(f"     лоу {w:>2} закр.: сигналов {sw['signals_pass'] / len(sig):>4.0%}, серий без входа "
              f"{sw['never_share']:>4.0%}; поз. {r['n_mean']:.0f}, P&L {r['pnl_mean']:+.0f}$ "
              f"(Δ {r['pnl_mean'] - rb['pnl_mean']:+.0f}), "
              f"−альты на $ {B.pct(r['ex_alt_per_usd'])} (Δ {pp(r['ex_alt_per_usd'] - rb['ex_alt_per_usd'])} п.п.), "
              f"лучше {r['beat_base_pnl']}/{r['beat_base_ex_alt_usd']}, циклы "
              + "/".join(pp(r["cycles"][c]["ex_alt_usd"] - rb["cycles"][c]["ex_alt_usd"]) for c in CYC)
              + f" п.п., 2023–26 лучше {r['oos_beat_base_ex_alt_usd']}/{J.SEEDS}, без 5 "
              f"{r['pnl_ex_top5_mean'] - rb['pnl_ex_top5_mean']:+.0f}$ ({r['beat_base_pnl_ex_top5']})")

    # ---- F) вне выборки
    oo = oos_block(R_, rb)
    pk = oo["picked"]
    print(f"  F) вне выборки: по 2018–22 на сетке 0–10% выбран «{res[oo['pick']]['label']}»"
          f"{'' if oo['pnl_ok'] else ' (P&L 2018–22 ниже базы у всех)'} (P&L 2018–22 не ниже базы у {oo['n_ok']} из "
          f"{len(GRID)}) — 2018–22: Δ P&L {pk['is_pnl']:+.0f}$, Δ −альты на $ {pp(pk['is_ex_alt_usd'])} п.п. "
          f"({pk['is_beat_usd']}/{J.SEEDS}); 2023–26: Δ P&L {pk['oos_pnl']:+.0f}$, Δ −альты {pk['oos_ex_alt']:+.0f}$, "
          f"на $ {pp(pk['oos_ex_alt_usd'])} п.п. ({pk['oos_beat_usd']}/{J.SEEDS}), −BTC {pk['oos_ex_btc']:+.0f}$")
    for x in YEAR_X:
        f_ = oo["fixed"][xkey(x)]
        print(f"     фикс. ≤ {x:.0%}: 2018–22 Δ P&L {f_['is_pnl']:+.0f}$, −альты на $ {pp(f_['is_ex_alt_usd'])} п.п. "
              f"({f_['is_beat_usd']}/{J.SEEDS}); 2023–26 Δ P&L {f_['oos_pnl']:+.0f}$ ({f_['oos_beat_pnl']}/{J.SEEDS}), "
              f"−альты {f_['oos_ex_alt']:+.0f}$, на $ {pp(f_['oos_ex_alt_usd'])} п.п. ({f_['oos_beat_usd']}/{J.SEEDS})")
    print(f"     место 5% среди {len(GRID)} порогов: 2018–22 — {oo['rank_is_5']}-е, 2023–26 — {oo['rank_oos_5']}-е "
          f"(лучший в 2023–26 — {res[oo['best_oos']]['label']})")

    # ---- G) практичность в книге (серия = позиция: кулдаун 90 дн. от входа ≥ длины серии)
    prac = {}
    n_bp, n_br = len(base_prod), st.fmean(len(tk) for tk in base_runs)
    for x in PRACT_X:
        k = xkey(x)
        m, r = res[k]["prod"], R_[k]
        prac[k] = {"prod_delay_mean": m["entry"]["delay_mean"], "prod_delay_med": m["entry"]["delay_med"],
                   "prod_late_share": m["entry"]["late_share"], "prod_n": m["n"],
                   "prod_skipped_share": m["series"]["only_base"] / n_bp,
                   "random_delay_mean": r["entry"]["delay_mean"], "random_n": r["n_mean"],
                   "random_skipped_share": r["series"]["only_base"] / n_br}
    span = (D["last_day"] - min(ep["day"] for ep in src)) / DAY / 365.25
    print(f"  G) практичность в книге (прод-порядок | {J.SEEDS} порядков): позиций в год, задержка входа от 1-го "
          f"сигнала серии ср. / мед. дн., доля серий прода без входа варианта; база: {mb['n'] / span:.1f} поз./год, "
          f"задержка {mb['entry']['delay_mean']:.1f} / {mb['entry']['delay_med']:.0f} дн.")
    for x in PRACT_X:
        o = prac[xkey(x)]
        print(f"     ≤ {x:>3.0%}: {o['prod_n'] / span:>4.1f} поз./год, задержка {o['prod_delay_mean']:>4.1f} / "
              f"{o['prod_delay_med']:>2.0f} дн., без входа {o['prod_skipped_share']:>4.0%} серий прода | "
              f"{o['random_n'] / span:.1f} поз./год, {o['random_delay_mean']:.1f} дн., {o['random_skipped_share']:.0%}")
    print(f"  [{time.time() - t0:.0f} с]")
    return {"variants": res, "shape": sh, "years": yblocks, "windows": win, "oos": oo, "practical_book": prac,
            "checked_q17": checked, "span_years": span}


# ---------------------------------------------------------------- main

def analyze(D: dict, t0: float) -> dict:
    src = D["src"]
    base_take, _illiq = P.EXEC_POLICY
    assert all(ep["e"] >= max(WINDOWS) - 1 for ep in src), "мало истории для лоу за 60 закрытий"
    for ep in src:
        ep["bw"] = {w: bounce_w(ep, w) for w in WINDOWS}
        assert ep["bw"][30] == ep["f"]["bounce"], "лоу 30 закрытий != признак Q13"
    sig = [ep for ep in src if base_take(ep)]
    ser = B.series_of(sig)
    span = (max(ep["day"] for ep in sig) - min(ep["day"] for ep in sig)) / DAY / 365.25
    n_unk = sum(1 for ep in sig if B.bounce(ep) is None)

    # ---- сигналы и серии без книги
    waits = {"grid": {xkey(x): series_wait(sig, ser, B.le(x), span) for x in GRID},
             "windows": {str(w): series_wait(sig, ser, gate_w(WIN_X, w), span) for w in WINDOWS}}
    assert waits["windows"]["30"] == waits["grid"][xkey(WIN_X)], "окно 30 != сетка"
    diff = {str(w): sum(1 for ep in sig if gate_w(WIN_X, w)(ep) != gate_w(WIN_X, 30)(ep)) / len(sig)
            for w in WINDOWS if w != 30}
    s0 = waits["grid"][xkey(GRID[0])]
    print(f"\nСигналы, прошедшие фильтры прода: {len(sig)} (рост от лоу неизвестен у {n_unk}); серий (90 дн. от "
          f"первого сигнала) {s0['series']}, {s0['series_per_year']:.1f} в год за {span:.1f} лет")
    print("  доля сигналов ≤ X (лоу 30 закр.): " + ", ".join(
        f"{x:.0%} {waits['grid'][xkey(x)]['signals_pass'] / len(sig):.0%}" for x in GRID))
    print(f"  при X = {WIN_X:.0%}: лоу 20 / 30 / 60 закр. — сигналов " + " / ".join(
        f"{waits['windows'][str(w)]['signals_pass'] / len(sig):.0%}" for w in WINDOWS)
          + "; решение гейта отличается от лоу 30 у " + ", ".join(f"{w} закр. {v:.0%}" for w, v in diff.items())
          + " сигналов")
    print("  практичность без книги (по сериям): доля серий, где за 90 дн. ни один сигнал не прошёл гейт; дней от "
          "первого сигнала серии до первого прошедшего ср. / мед. / q75 / q90; доля с входом в день первого сигнала; "
          "серий с входом в год")
    for x in GRID:
        o = waits["grid"][xkey(x)]
        print(f"     ≤ {x:>3.0%}: без входа {o['never_share']:>4.0%}, ждать {o['wait_mean']:>4.1f} / {o['wait_med']:>2.0f} "
              f"/ {o['wait_q75']:>2.0f} / {o['wait_q90']:>2.0f} дн., сразу {o['wait0_share']:>4.0%}, "
              f"{o['entered_per_year']:>5.1f} серий в год")
    for w in WINDOWS:
        if w != 30:
            o = waits["windows"][str(w)]
            print(f"     лоу {w} закр. ≤ {WIN_X:.0%}: без входа {o['never_share']:>4.0%}, ждать {o['wait_mean']:>4.1f} / "
                  f"{o['wait_med']:>2.0f} / {o['wait_q75']:>2.0f} / {o['wait_q90']:>2.0f} дн., сразу {o['wait0_share']:>4.0%}, "
                  f"{o['entered_per_year']:>5.1f} серий в год")

    VARIANTS = variants()
    ref17 = json.loads(REF_Q17.read_text(encoding="utf-8"))
    n_new = sum(1 for v in VARIANTS if v["key"] != "base" and not (v["w"] == 30 and v["x"] in CHECK))
    mt = {"q17_variants_per_book": N_Q17["variants_per_book"], "q17_gates_per_book": N_Q17["gates_per_book"],
          "fine_new_per_book": n_new, "total_per_book": N_Q17["variants_per_book"] + n_new,
          "total_both_books": 2 * (N_Q17["variants_per_book"] + n_new),
          "gates_per_book": N_Q17["gates_per_book"] + n_new}
    out: dict = {"meta": {"seeds": J.SEEDS, "slots": J.SLOTS, "budget": J.BUDGET, "grid": GRID, "plateau": PLATEAU,
                          "point": POINT, "year_x": YEAR_X, "windows": WINDOWS, "win_x": WIN_X, "pract_x": PRACT_X,
                          "slope_rho": SLOPE_RHO, "is_cycles": B.IS_CYCLES, "oos_cycle": B.OOS_CYCLE,
                          "series_days": B.SERIES_DAYS, "archive_last_day": D["last_day"],
                          "variants": {v["key"]: v["label"] for v in VARIANTS}},
                 "signals": {"n": len(sig), "unknown": n_unk, "span_years": span, "window_diff_share": diff},
                 "series_wait": waits, "multiple_testing": mt}
    for b, (sell, _stop) in J.BOOKS.items():
        out[b] = analyze_book(b, sell, D, sig, ser, VARIANTS, ref17, waits, t0)

    print(f"\nМножественная проверка: Q17 — {mt['q17_variants_per_book']} вариантов на книгу, эта сетка — ещё "
          f"{mt['fine_new_per_book']} (9 порогов + 2 окна лоу): всего {mt['total_per_book']} на книгу, "
          f"{mt['total_both_books']} на обе; гейтов по отскоку {mt['gates_per_book']} на книгу. Лучший порог сетки "
          f"выбран задним числом — его Δ завышена (см. «выше плато на …»)")
    for b in J.BOOKS:
        sh, oo = out[b]["shape"], out[b]["oos"]
        y5 = out[b]["years"][xkey(POINT)]
        print(f"ИТОГ {b}: 5% — {sh['verdict']}; плато 3–7% Δ −альты на $ {pp(sh['plateau_mean'])} п.п. (точка 5% "
              f"{pp(sh['point'])}), Спирмен X↔Δ {sh['rho']:+.2f}; годы ≤ 5% в плюсе {y5['years_pos_ex_alt']}/{y5['n_years']}, "
              f"без любого года −альты на $ {'> 0' if y5['loyo_all_pos_usd'] else 'НЕ всегда > 0'}; вне выборки "
              f"выбран {oo['pick']} → 2023–26 {pp(oo['picked']['oos_ex_alt_usd'])} п.п. "
              f"({oo['picked']['oos_beat_usd']}/{J.SEEDS})")
    out["runtime_s"] = time.time() - t0
    return out


def main() -> int:
    t0 = time.time()
    D = B.load()
    out = analyze(D, t0)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
