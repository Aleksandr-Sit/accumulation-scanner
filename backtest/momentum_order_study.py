"""Моментум / сила к рынку как порядок выбора, гейт и квота среди пружин дня (Q13).

Повод — Q10 (13.10.2026, код не в репозитории): выбор монеты — слабое место исполнителя. R по
обороту (прод) −$186 к альтам, случайный порядок −$278, по баллу сканера −$410; внутри дня
IC балла ≈ 0. Сканер покупает слабость («пружины» после падения), а в крипте устойчивее
краткосрочный моментум (Liu, Tsyvinski, Wu 2022). Проверяем: если среди пружин дня брать
сначала сильные к рынку, обгоняет ли книга прод и случайный порядок — в нескольких циклах,
а не только в 2023–26.

Модель — книги R и H, шаг s6 (portfolio_vs_market.exec_book: 15 мест, лестница $50, кулдаун
90 дн., свободный USDT $1500, нижняя четверть по обороту и перегрев, квота неликвида 5 мест);
сверка: база повторяет s6 (R +$1503, H +$1903), «−альты» у R −$186. Книги независимы.

Признаки на день сигнала e (только закрытия Binance по e включительно, .cache/binance_1d_full,
и индекс альтов market_daily по день сигнала — P.market_index: total × (1 − BTC.D) − стейблы,
пропуски до 7 дней — прошлым значением):
  rs7 / rs30 / rs90 — c[e]/c[e−k] − 1 минус рост индекса альтов за те же k дней;
  sma20 / sma50 — c[e] / среднее последних 20 / 50 закрытий − 1 (>0 — «над SMA»);
  bounce — c[e] / лоу базы − 1, лоу базы как в модели (simulate_exec, правило base30): минимум
           30 закрытий e−29…e.
  Индекс одинаков для всех монет дня, поэтому ПОРЯДОК по rsK внутри дня = порядок по
  собственной доходности за K дней; индекс важен только для гейтов «rsK > 0» и квоты.
  Сигналы детектора прода начинаются с 200-го закрытия отрезка — история на 90 дн. есть
  всегда; неизвестен признак только без индекса (на сигналах 2018+ не встречается) —
  в порядке такие идут в конец, гейт их пропускает, квота не считает.

Варианты:
  1. ПОРЯДОК внутри дня по признаку (exec_book(order=ключ)): сильные первыми по rs7 / rs30 /
     rs90 / sma20 / sma50 / bounce, обратный — слабые первыми по rs30; грубые порядки «над SMA50
     первыми» и «rs30 > 0 первыми» (внутри групп — случайно или по обороту, как прод).
  2. ГЕЙТ «только сильные» ДО книги (как Q9/Q11): rs7 > 0, rs30 > 0, rs90 > 0, над SMA20, над
     SMA50, рост от лоу базы ≥ 10 / 20 / 30%.
  3. КВОТА: «слабых» (rs30 < 0) не больше 5 / 8 / 10 мест из 15 (exec_book quota2).
  4. IC внутри дня: Спирмен признака к исходу эпизода (P&L / вложено строки simulate_exec книги)
     по дням с ≥ 3 сигналами, прошедшими фильтры прода; среднее по дням, по циклам, t = ср./σ·√n;
     IC «по всем» сигналам — для сравнения; справочно IC оборота (−место, как порядок прода).

Как сравнивать порядок (он детерминирован). Ключ сортировки устойчив, равные ключи — в
случайном порядке: exec_book сначала перемешивает сигналы дня тем же random.Random(seed), что
случайная база, потом сортирует по ключу. Поэтому прогон варианта с seed s и случайная база
с seed s сравниваются попарно: «лучше базы в K из 30» = в скольких из J.SEEDS случайных
порядков вариант лучше. У непрерывных ключей (rsK, sma, bounce) равенств почти нет и 30 прогонов
совпадают — K тогда есть место варианта среди 30 случайных порядков (перцентиль); у грубых
порядков и гейтов это обычное попарное сравнение. Отдельно — разница с продом «по обороту»
(одна цифра) и строка «первый прогон» (seed 0 для порядков, порядок по обороту для гейтов и
квот): позиций, умерших за 2 года, пропало из лучших 10% исходов прода.

Метрики как Q9/Q11: P&L, P&L − альты / корзина топ-20 по капе / BTC по денежным потокам,
в скольких порядках лучше базы, вложено, позиций, умерших за 2 года, сколько из лучших 10%
исходов базы пропало; по циклам 2018–20 / 2020–22 / 2023–26 (цикл — по дню входа); вне
выборки — лучший вариант каждого признака по 2018–22 (среднее порядков, P&L − альты) и он же
в 2023–26.

Оговорки: архив только Binance (выжившие ×0.73 по прошлым замерам — цифры верхняя граница);
пружина — детектор прода на закрытиях Binance, а не балл ≥ 70; сигналы одной монеты в соседние
дни коррелированы (IC по дням — не независимые наблюдения); пороги гейтов и правила книг
подобраны на той же истории; день индекса market_daily и закрытие Binance могут расходиться
на часы (в сторону прошлого, не будущего).

Запуск из корня проекта:  PYTHONIOENCODING=utf-8 python backtest/momentum_order_study.py
Время прогона: ~7 мин (423 с 16.10.2026, из них ~190 с — архив и сигналы; 24 варианта ×
  31 прогон × 2 книги); сеть не нужна.
Результат: backtest/momentum_order_results.json
"""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics as st
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cmc_rank_gate_study as G  # noqa: E402
import junk_filter_study as J  # noqa: E402
import ladder_dca_study as L  # noqa: E402
import portfolio_vs_market as P  # noqa: E402
import supply_age_gate_study as Q  # noqa: E402
import top20_basket_study as T  # noqa: E402

OUT = Path(__file__).resolve().parent / "momentum_order_results.json"
DAY = 86400
IS_CYCLES = G.IS_CYCLES
OOS_CYCLE = G.OOS_CYCLE
BENCH = G.BENCH
FEATS = ("rs7", "rs30", "rs90", "sma20", "sma50", "bounce")
FAMILIES = {"rs7": "сила к альтам 7 дн.", "rs30": "сила к альтам 30 дн.",
            "rs90": "сила к альтам 90 дн.", "sma20": "к SMA20", "sma50": "к SMA50",
            "bounce": "рост от лоу базы"}
INF = math.inf
MIN_DAY_SIGNALS = 3


# ---------------------------------------------------------------- признаки

def features(ep: dict, alt: dict) -> dict:
    """Признаки на закрытие дня сигнала (без заглядывания вперёд)."""
    c, e, day = ep["seg"]["c"], ep["e"], ep["day"]
    f: dict = {}
    a0 = P.at(alt, day)
    for k in (7, 30, 90):
        own = c[e] / c[e - k] - 1 if e - k >= 0 and c[e - k] > 0 else None
        ak = P.at(alt, day - k * DAY)
        f[f"ret{k}"] = own
        f[f"rs{k}"] = own - (a0 / ak - 1) if own is not None and a0 and ak else None
    for k in (20, 50):
        f[f"sma{k}"] = c[e] / st.fmean(c[e - k + 1:e + 1]) - 1 if e - k + 1 >= 0 else None
    lo = min(c[max(0, e - 29):e + 1])
    f["bounce"] = c[e] / lo - 1 if lo > 0 else None
    return f


def strong_first(feat: str):
    """Ключ: больший признак раньше, неизвестный — в конец."""
    return lambda ep: -ep["f"][feat] if ep["f"][feat] is not None else INF


def weak_first(feat: str):
    return lambda ep: ep["f"][feat] if ep["f"][feat] is not None else INF


def flag_first(feat: str, by_rank: bool):
    """Грубый порядок: признак > 0 первыми; внутри групп — по обороту или случайно."""
    def key(ep):
        v = ep["f"][feat]
        g = 0 if v is not None and v > 0 else 1
        return (g, ep["rank"]) if by_rank else g
    return key


def gate(feat: str, thr: float):
    """Пропускает признак > thr; неизвестный — пропускает."""
    return lambda ep: ep["f"][feat] is None or ep["f"][feat] > thr


def weak30(ep: dict) -> bool:
    return ep["f"]["rs30"] is not None and ep["f"]["rs30"] < 0


def variants() -> list[dict]:
    vs = [{"key": "base", "label": "база (прод, по обороту)", "fam": None, "kind": "base"}]
    for ft in FEATS:
        vs.append({"key": f"o_{ft}", "label": f"порядок: {ft} сильные первыми", "fam": ft,
                   "kind": "order", "order": strong_first(ft)})
    vs.append({"key": "o_rs30_weak", "label": "порядок: rs30 слабые первыми", "fam": "rs30",
               "kind": "order", "order": weak_first("rs30")})
    for ft, nm in (("sma50", "над SMA50"), ("rs30", "rs30>0")):
        vs.append({"key": f"o_{ft}_flag", "label": f"порядок: {nm} первыми, внутри случ.",
                   "fam": ft, "kind": "order", "order": flag_first(ft, False)})
        vs.append({"key": f"o_{ft}_flag_rank", "label": f"порядок: {nm} первыми, внутри обор.",
                   "fam": ft, "kind": "order", "order": flag_first(ft, True)})
    for ft, nm in (("rs7", "rs7 > 0"), ("rs30", "rs30 > 0"), ("rs90", "rs90 > 0"),
                   ("sma20", "над SMA20"), ("sma50", "над SMA50")):
        vs.append({"key": f"g_{ft}", "label": f"гейт: {nm}", "fam": ft, "kind": "gate",
                   "g": gate(ft, 0.0)})
    for thr in (0.10, 0.20, 0.30):
        vs.append({"key": f"g_bounce{int(thr * 100)}", "label": f"гейт: от лоу базы ≥ {thr:.0%}",
                   "fam": "bounce", "kind": "gate", "g": gate("bounce", thr - 1e-12)})
    for n in (5, 8, 10):
        vs.append({"key": f"q_weak{n}", "label": f"квота: rs30<0 ≤ {n} мест", "fam": "rs30",
                   "kind": "quota", "q2": (weak30, n)})
    return vs


# ---------------------------------------------------------------- IC

def ic_block(sig: list[dict], y: dict, feats) -> dict:
    """Спирмен признака к исходу внутри дня (дни с ≥ MIN_DAY_SIGNALS) и по всем сигналам."""
    by_day: dict[int, list[dict]] = {}
    for ep in sig:
        if G.key_of(ep) in y:
            by_day.setdefault(ep["day"], []).append(ep)
    out = {}
    for ft in feats:
        val = (lambda ep: -ep["rank"]) if ft == "turnover" else (lambda ep, ft=ft: ep["f"][ft])
        per_day: list[tuple[str, float]] = []
        for _d, ee in by_day.items():
            ee = [ep for ep in ee if val(ep) is not None]
            if len(ee) < MIN_DAY_SIGNALS:
                continue
            xs = [val(ep) for ep in ee]
            ys = [y[G.key_of(ep)] for ep in ee]
            if len(set(xs)) < 2 or len(set(ys)) < 2:
                continue
            per_day.append((ee[0]["cycle"], G.spearman(xs, ys)))
        allx = [(val(ep), y[G.key_of(ep)], ep["cycle"]) for ee in by_day.values() for ep in ee
                if val(ep) is not None]

        def summ(vals):
            if len(vals) < 2:
                return {"n_days": len(vals), "ic": None, "t": None}
            m, sd = st.fmean(vals), st.stdev(vals)
            return {"n_days": len(vals), "ic": m, "t": m / sd * math.sqrt(len(vals)) if sd else None,
                    "pos_share": sum(1 for v in vals if v > 0) / len(vals)}
        o = {"all": summ([v for _c, v in per_day]),
             "cycles": {c: summ([v for cc, v in per_day if cc == c]) for c, _a, _b in L.CYCLES},
             "pooled": G.spearman([a for a, _b, _c in allx], [b for _a, b, _c in allx]),
             "pooled_cycles": {}}
        for c, _a, _b in L.CYCLES:
            xx = [(a, b) for a, b, cc in allx if cc == c]
            o["pooled_cycles"][c] = G.spearman([a for a, _ in xx], [b for _, b in xx]) if len(xx) > 2 else None
        out[ft] = o
    return out


# ---------------------------------------------------------------- main

def pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def fnum(x, w=6, fmt="+.2f") -> str:
    return f"{x:{fmt}}".rjust(w) if x is not None else "—".rjust(w)


def main() -> int:
    t0 = time.time()
    coins = L.load_archive()
    all_segs = L.segments(coins)
    px = T.Prices(all_segs)
    btc = P.btc_closes(all_segs)
    last_day = max(btc)
    alt, _ = P.market_index(ROOT / "scanner.db")
    comps = T.compositions(px, last_day)
    cap, _ = T.basket_level(px, comps, "cap", last_day)
    series = {"alt": alt, "cap": cap, "btc": btc}
    print(f"архив Binance до {dt.datetime.fromtimestamp(last_day, dt.timezone.utc).date()}, индекс "
          f"альтов {len(alt)} дней  [{time.time() - t0:.0f} с]")

    vranks = L.vol_ranks(all_segs)
    npairs = P.pairs_per_day(vranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot = L.load_hot()
    src = [ep for ep in L.signal_days(segs, vranks, hot, {"prod": L.is_spring_prod})["prod"]
           if ep["cycle"] != "?"]
    for ep in src:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
        ep["f"] = features(ep, alt)
    print(f"дней сигнала прода {len(src)}  [{time.time() - t0:.0f} с]")

    # ---- признаки сигналов, прошедших фильтры прода
    base_take, illiq = P.EXEC_POLICY
    sig = [ep for ep in src if base_take(ep)]
    per_day: dict[int, int] = {}
    for ep in sig:
        per_day[ep["day"]] = per_day.get(ep["day"], 0) + 1
    dist = {}
    for c, _a, _b in L.CYCLES + [("всё", 0, 0)]:
        ss = [ep for ep in sig if c == "всё" or ep["cycle"] == c]
        d: dict = {"n": len(ss), "days": len({ep["day"] for ep in ss}),
                   "days_ge3": sum(1 for dd in {ep["day"] for ep in ss} if per_day[dd] >= 3)}
        for ft in FEATS:
            xs = sorted(ep["f"][ft] for ep in ss if ep["f"][ft] is not None)
            d[ft] = {"unknown": len(ss) - len(xs), "pos": sum(1 for x in xs if x > 0) / len(xs),
                     "q25": xs[len(xs) // 4], "med": xs[len(xs) // 2], "q75": xs[3 * len(xs) // 4]}
        d["bounce_ge"] = {f"{t:.0%}": sum(1 for ep in ss if (ep["f"]["bounce"] or 0) >= t) / len(ss)
                          for t in (0.10, 0.20, 0.30)}
        dist[c] = d
    pairs = [(ep["f"]["rs30"], ep["f"]["rs90"], ep["f"]["sma50"], ep["f"]["bounce"], -ep["rank"])
             for ep in sig if None not in (ep["f"]["rs30"], ep["f"]["rs90"])]
    corr = {"rs30_rs90": G.spearman([p[0] for p in pairs], [p[1] for p in pairs]),
            "rs30_sma50": G.spearman([p[0] for p in pairs], [p[2] for p in pairs]),
            "rs30_bounce": G.spearman([p[0] for p in pairs], [p[3] for p in pairs]),
            "rs30_turnover": G.spearman([p[0] for p in pairs], [p[4] for p in pairs])}
    print(f"\nСигналы, прошедшие фильтры прода: {len(sig)}; дней {dist['всё']['days']}, из них с ≥ 3 "
          f"сигналами {dist['всё']['days_ge3']}; Спирмен rs30/rs90 {corr['rs30_rs90']:+.2f}, rs30/SMA50 "
          f"{corr['rs30_sma50']:+.2f}, rs30/от лоу {corr['rs30_bounce']:+.2f}, rs30/оборот {corr['rs30_turnover']:+.2f}")
    print(f"  {'цикл':8} {'сигн.':>6} {'дней≥3':>6} " + " ".join(f"{ft + ' >0 / мед.':>17}" for ft in FEATS)
          + f" {'от лоу ≥10/20/30%':>18}")
    for c, d in dist.items():
        print(f"  {c:8} {d['n']:>6} {d['days_ge3']:>6} "
              + " ".join(f"{d[ft]['pos']:>6.0%} / {pct(d[ft]['med']):>7}".rjust(17) for ft in FEATS)
              + " " + "/".join(f"{v:.0%}" for v in d["bounce_ge"].values()).rjust(18))

    VARIANTS = variants()
    out: dict = {"meta": {"seeds": J.SEEDS, "slots": J.SLOTS, "budget": J.BUDGET,
                          "is_cycles": IS_CYCLES, "oos_cycle": OOS_CYCLE, "archive_last_day": last_day,
                          "min_day_signals": MIN_DAY_SIGNALS,
                          "variants": {v["key"]: v["label"] for v in VARIANTS},
                          "families": FAMILIES},
                 "signals": {"n": len(sig), "dist": dist, "spearman": corr}}

    ref = json.loads((Path(__file__).resolve().parent / "portfolio_vs_market_results.json")
                     .read_text(encoding="utf-8"))["exec"]
    by_key = {G.key_of(ep): ep for ep in src}
    for b, (sell, _stop) in J.BOOKS.items():
        cache: dict = {}

        def sim(ep, sell=sell, cache=cache):
            k = (ep["seg"]["sym"], ep["e"])
            if k not in cache:
                cache[k] = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET,
                                           horizon=None, rules=L.EXEC_ALL)
            return cache[k]
        fl = Q.Flows(series)

        base = P.exec_book(src, sim, *P.EXEC_POLICY, cooldown=True, cash=True)
        chk = P.evaluate_flows(base, alt, btc)
        s6 = ref[b]["s6"]
        assert chk["n"] == s6["n"] and abs(chk["pnl"] - s6["pnl"]) < 1e-6 and \
            abs(chk["alt"] - s6["alt"]) < 1e-6, f"{b}: s6 не повторился ({chk['pnl']} / {s6['pnl']})"
        print(f"\nсверка {b}: база s6 n={chk['n']} P&L {chk['pnl']:+.0f}$ (эталон {s6['pnl']:+.0f}$), "
              f"−альты {chk['pnl'] - chk['alt']:+.0f}$ ✓  [{time.time() - t0:.0f} с]")
        top = sorted(base, key=lambda t: t[1]["pnl"], reverse=True)[:max(1, len(base) // 10)]
        base_best = {G.key_of(ep) for ep, _r in top}

        # ---- IC внутри дня: исход каждого сигнала, прошедшего фильтры прода
        y = {}
        for ep in sig:
            r = sim(ep)
            if r is not None and r["spent"] > 0:
                y[G.key_of(ep)] = r["pnl"] / r["spent"]
        ic = ic_block(sig, y, FEATS + ("turnover",))
        print(f"  IC внутри дня (Спирмен признака к P&L/вложено, дни с ≥ {MIN_DAY_SIGNALS} сигналами; "
              f"исход есть у {len(y)} из {len(sig)})  [{time.time() - t0:.0f} с]")
        print(f"  {'признак':9} {'дней':>5} {'IC':>6} {'t':>6} {'IC>0':>5} "
              + " ".join(f"{c + ' IC (t)':>16}" for c, _a, _b in L.CYCLES) + f" {'по всем':>8} "
              + " ".join(f"{c:>8}" for c, _a, _b in L.CYCLES))
        for ft, o in ic.items():
            a = o["all"]
            print(f"  {ft:9} {a['n_days']:>5} {fnum(a['ic'])} {fnum(a['t'], 6, '+.1f')} "
                  f"{a.get('pos_share', 0):>5.0%} "
                  + " ".join(f"{fnum(o['cycles'][c]['ic'], 6)} ({fnum(o['cycles'][c]['t'], 5, '+.1f')})".rjust(16)
                             for c, _a, _b in L.CYCLES)
                  + f" {fnum(o['pooled'], 8)} " + " ".join(fnum(o["pooled_cycles"][c], 8) for c, _a, _b in L.CYCLES))

        ob: dict = {"best_threshold_pnl": top[-1][1]["pnl"], "ic": ic, "ic_n_outcomes": len(y),
                    "best": [{"coin": ep["seg"]["sym"].split("#")[0][:-4],
                              **{ft: ep["f"][ft] for ft in FEATS},
                              "day": dt.datetime.fromtimestamp(ep["day"], dt.timezone.utc).date()
                              .isoformat(), "pnl": r["pnl"]} for ep, r in top],
                    "variants": {}}
        seeds_base = None
        base_main = None
        for v in VARIANTS:
            take = base_take if v["kind"] != "gate" else (lambda ep, g=v["g"]: base_take(ep) and g(ep))
            q2 = v.get("q2")
            order = v.get("order", "rank")

            def gate_k(k, take=take):
                return take(by_key[k])
            taken = P.exec_book(src, sim, take, illiq, cooldown=True, cash=True, quota2=q2,
                                order=order, seed=0)
            m = book_metrics_ext(fl, taken, base_best, gate_k)
            if v["key"] == "base":
                base_main = m
            m["vs_prod"] = {"pnl": m["pnl"] - base_main["pnl"],
                            **{f"ex_{k}": (m["pnl"] - m[k]) - (base_main["pnl"] - base_main[k])
                               for k, _n in BENCH}}
            m["lost_best"] = sorted(f"{ep['seg']['sym'].split('#')[0][:-4]} {r['pnl']:+.0f}$"
                                    for ep, r in top if G.key_of(ep) not in
                                    {G.key_of(e) for e, _r in taken})
            runs, died = [], []
            for sd in range(J.SEEDS):
                tk = P.exec_book(src, sim, take, illiq, cooldown=True, cash=True, quota2=q2,
                                 order="random" if v["kind"] != "order" else order, seed=sd)
                runs.append(G.seed_row(fl.vs(tk)))
                died.append(sum(1 for ep, _r in tk if ep["died2y"]))
            if v["key"] == "base":
                seeds_base = runs
            rnd = {"pnl_mean": st.fmean(x["pnl"] for x in runs),
                   "pnl_min": min(x["pnl"] for x in runs), "pnl_max": max(x["pnl"] for x in runs),
                   "spent_mean": st.fmean(x["spent"] for x in runs),
                   "died2y_mean": st.fmean(died),
                   "beat_base_pnl": sum(1 for x, y_ in zip(runs, seeds_base) if x["pnl"] > y_["pnl"]),
                   "beat_base_ex_alt": sum(1 for x, y_ in zip(runs, seeds_base)
                                           if x["pnl"] - x["alt"] > y_["pnl"] - y_["alt"]),
                   "beat_prod_ex_alt": sum(1 for x in runs if x["pnl"] - x["alt"] >
                                           base_main["pnl"] - base_main["alt"])}
            for k, _n in BENCH:
                rnd[f"ex_{k}_mean"] = st.fmean(x["pnl"] - x[k] for x in runs)
                rnd[f"ex_{k}_per_usd"] = st.fmean((x["pnl"] - x[k]) / x["spent"] for x in runs)
                rnd[f"beat_{k}_runs"] = sum(1 for x in runs if x["pnl"] > x[k])
            for part, cyc in (("is", IS_CYCLES), ("oos", (OOS_CYCLE,))):
                rnd[f"{part}_pnl"] = st.fmean(G.cyc_sum(x, cyc, "pnl") for x in runs)
                for k, _n in BENCH:
                    rnd[f"{part}_ex_{k}"] = st.fmean(G.cyc_sum(x, cyc, f"ex_{k}") for x in runs)
                rnd[f"{part}_beat_base_ex_alt"] = sum(
                    1 for x, y_ in zip(runs, seeds_base)
                    if G.cyc_sum(x, cyc, "ex_alt") > G.cyc_sum(y_, cyc, "ex_alt"))
            rnd["cycles"] = {c: {"pnl": st.fmean(G.cyc_sum(x, (c,), "pnl") for x in runs),
                                 "ex_alt": st.fmean(G.cyc_sum(x, (c,), "ex_alt") for x in runs),
                                 "beat_base_ex_alt": sum(
                                     1 for x, y_ in zip(runs, seeds_base)
                                     if G.cyc_sum(x, (c,), "ex_alt") > G.cyc_sum(y_, (c,), "ex_alt"))}
                             for c, _a, _b in L.CYCLES}
            m["random"] = rnd
            ob["variants"][v["key"]] = m
        out[b] = ob

        # ---- печать книги
        name = "R правила" if b == "R" else "H держать"
        vb = ob["variants"]["base"]
        rb = vb["random"]
        print(f"=== книга {name}: прод поз. {vb['n']}, вложено {vb['spent']:.0f}$, P&L {vb['pnl']:+.0f}$, "
              f"−альты {vb['pnl'] - vb['alt']:+.0f}$, −корз. {vb['pnl'] - vb['cap']:+.0f}$, −BTC "
              f"{vb['pnl'] - vb['btc']:+.0f}$; случайный порядок (ср. {J.SEEDS}): P&L {rb['pnl_mean']:+.0f}$, "
              f"−альты {rb['ex_alt_mean']:+.0f}$; лучшие 10% прода — P&L ≥ {ob['best_threshold_pnl']:+.0f}$ "
              f"({len(top)} поз.)")
        print(f"  средние {J.SEEDS} прогонов (порядки — равные ключи случайно; гейты/квоты — случайный "
              f"порядок); «лучше базы» — против случайной базы того же seed; «к проду» — первый прогон "
              f"минус прод по обороту (−альты)")
        print(f"  {'вариант':40} {'P&L':>6} {'влож.':>6} {'−альты':>13} {'−корз.':>13} {'−BTC':>13} "
              f"{'лучше б. P&L/−альт':>18} {'2018-20':>8} {'2020-22':>8} {'2023-26':>8} "
              f"{'к проду':>7} {'поз.':>4} {'†':>3} {'†ср.':>5} {'лучш.':>6}")
        for v in VARIANTS:
            m = ob["variants"][v["key"]]
            r = m["random"]
            print(f"  {v['label']:40} {r['pnl_mean']:>+6.0f} {r['spent_mean']:>6.0f} "
                  + " ".join(f"{r[f'ex_{k}_mean']:+.0f} ({pct(r[f'ex_{k}_per_usd'])})".rjust(13)
                             for k, _n in BENCH)
                  + f"{r['beat_base_pnl']}/{r['beat_base_ex_alt']}".rjust(19)
                  + "".join(f"{r['cycles'][c]['ex_alt']:>+9.0f}" for c, _a, _b in L.CYCLES)
                  + f" {m['vs_prod']['ex_alt']:>+7.0f} {m['n']:>4} {m['died2y']:>3} {r['died2y_mean']:>5.1f} "
                  + f"{m['best_lost']}({m['best_lost_by_gate']})".rjust(6))

        # ---- вне выборки
        oos = {}
        vb_r = ob["variants"]["base"]["random"]
        for fam in FAMILIES:
            cand = [v for v in VARIANTS if v["fam"] == fam]
            pick = max(cand, key=lambda v: ob["variants"][v["key"]]["random"]["is_ex_alt"])
            vp_r = ob["variants"][pick["key"]]["random"]
            oos[fam] = {"pick": pick["key"],
                        "is": {k: vp_r[f"is_{k}"] - vb_r[f"is_{k}"] for k in ("pnl", "ex_alt", "ex_cap", "ex_btc")},
                        "oos": {k: vp_r[f"oos_{k}"] - vb_r[f"oos_{k}"] for k in ("pnl", "ex_alt", "ex_cap", "ex_btc")},
                        "is_beat": vp_r["is_beat_base_ex_alt"], "oos_beat": vp_r["oos_beat_base_ex_alt"],
                        "oos_base": {k: vb_r[f"oos_{k}"] for k in ("pnl", "ex_alt", "ex_btc")}}
            o = oos[fam]
            print(f"  вне выборки [{FAMILIES[fam]}]: по 2018–22 выбран «{pick['label']}» — к случ. базе в "
                  f"2018–22 −альты {o['is']['ex_alt']:+.0f}$ ({o['is_beat']}/{J.SEEDS}); в 2023–26 "
                  f"−альты {o['oos']['ex_alt']:+.0f}$ ({o['oos_beat']}/{J.SEEDS}), P&L {o['oos']['pnl']:+.0f}$, "
                  f"−BTC {o['oos']['ex_btc']:+.0f}$")
        ob["oos"] = oos
        print("  пропали из лучших 10% прода: " + "; ".join(
            f"{v['key']}: {', '.join(ob['variants'][v['key']]['lost_best']) or '—'}"
            for v in VARIANTS if v["key"] in ("o_rs30", "o_rs7", "g_rs30", "g_sma50", "q_weak5")))
        print(f"  [{time.time() - t0:.0f} с]")

    out["runtime_s"] = time.time() - t0
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


def book_metrics_ext(fl, taken, base_best: set, gate_k) -> dict:
    """Q.book_metrics + число «слабых» (rs30 < 0) в книге."""
    m = Q.book_metrics(fl, taken, base_best, gate_k)
    m["weak30"] = sum(1 for ep, _r in taken if weak30(ep))
    return m


if __name__ == "__main__":
    sys.exit(main())
