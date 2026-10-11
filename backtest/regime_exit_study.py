"""Выход по стадии рынка: в «бычьем» рынке тейков +50/+150 нет — только трейл.

Вопрос владельца (11.10.2026): на растущем рынке продавать на +50% или +150% рано — вершины
намного выше и позже, резоннее трейл; в боковике +50/+150 уже интересны. Учитывает ли это
стратегия и можно ли распознать стадию так, чтобы подстраивать под неё выход?

Модель — исполнитель (ladder_dca_study.simulate_exec, правила EXEC_ALL, без горизонта), книга
R целиком (stage8_exit: +50% → 0.33, +150% → 0.33, трейл 30% после взвода +60%, стоп −25% от
лоу базы, 2 закрытия). Переключатель «R⇄T»: в день d тейков нет, если детектор на закрытии
дня d − 1 говорит «бычий» — high дня d для тейка обнуляется (в simulate_exec high читает
только проверка тейка); трейл и стоп работают всегда. Тейк, пропущенный в бычий день,
исполняется в первый не бычий день с high ≥ цели — лимитку переставили. Сверка на всех
эпизодах: разметка «никогда» = R (simulate_exec как есть), «всегда» = T (только трейл) — точно.

Детекторы стадии (решение по дню d − 1, только прошлые данные):
  BTC бычий      — прод regime.classify_regime: BTC выше SMA50 и выше, чем 29 дн. назад;
  альты > SMA200 — индекс альтов без стейблов выше своей SMA200 (regime.market_feature_table);
  перегрев ≥1/≥3 — горит ≥ 1 / ≥ 3 из 7 прод-флагов перегрева (regime.hot_flags);
  ОРАКУЛ         — ЗАГЛЯДЫВАЕТ В БУДУЩЕЕ: индекс альтов в ближайшие 180 дн. будет ≥ ×1.5 —
                   потолок идеи «стадия известна заранее» (последние 180 дн. данных без
                   разметки — там как R).
Варианты: R (правила), T (только трейл), H (только стоп), R⇄T по каждому детектору.

Мерила: (0) доля «бычьих» дней по годам и P(оракул | детектор) — предсказывает ли признак
стадии сильный рост впереди; (1) эпизоды — все пружины 2018–2026 через 90 дн.: медиана /
средняя / win / Σ P&L по циклам, сколько эпизодов переключатель изменил, Δ к R по году
сигнала; (2) портфель «как исполнитель» (portfolio_vs_market, шаг s6) против индекса альтов
и BTC по денежным потокам — по рангу и при 30 случайных порядках сигналов. База R/H обязана
совпасть с portfolio_vs_market_results.json (шаг s6). Итог и решение — docs/DECISIONS.md, Q5.

ВНИМАНИЕ: архив только Binance; бычьих фаз в истории 3–4, эпизоды коррелированы; детекторов
четыре, и «лучший» выбирается задним числом — in-sample. Лишние $ от более долгого удержания —
это и рост рынка за те же дни: сравнивать с «альтами» и «BTC» по тем же потокам, а не только P&L.

Запуск из корня проекта:  py -3 backtest/regime_exit_study.py
Результат: backtest/regime_exit_results.json
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
from scanner import regime  # noqa: E402

OUT = Path(__file__).resolve().parent / "regime_exit_results.json"
PV_OUT = Path(__file__).resolve().parent / "portfolio_vs_market_results.json"
DAY = 86400
LAG = 1                          # тейк дня d — по стадии на закрытии дня d − 1
ORACLE_DAYS, ORACLE_MULT = 180, 1.5
FROM_2018 = 1514764800           # 2018-01-01: P(оракул | детектор) — с первой пружины
YEARS = list(range(2018, 2027))
R_SELL = L.EXEC_BOOKS["R"]       # stage8_exit: +50/+150 по 0.33 + трейл (0.60, 0.30)
T_SELL = {"levels": [], "trail": R_SELL["trail"]}
H_SELL = L.EXEC_BOOKS["H"]


def ymd(d: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(d))


# ---------------------------------------------------------------- детекторы стадии

def btc_bull(btc: dict[int, float]) -> dict[int, bool]:
    """Прод regime.classify_regime на каждый день: окна 60 закрытий хватает (SMA50 и цена
    29 дн. назад); первые 59 дней истории BTC — без разметки."""
    days = sorted(btc)
    out = {}
    for i in range(59, len(days)):
        r = regime.classify_regime([btc[x] for x in days[i - 59:i + 1]])
        if r["regime"] != "?":
            out[days[i]] = r["regime"] == "BULL"
    return out


def alt_above_sma200(feats: dict[int, dict]) -> dict[int, bool]:
    return {d: f["alt_vs_sma200"] > 0 for d, f in feats.items() if "alt_vs_sma200" in f}


def oracle(alt: dict[int, float], ahead: int = ORACLE_DAYS,
           mult: float = ORACLE_MULT) -> dict[int, bool]:
    """День d «бычий», если индекс альтов в ближайшие ahead дн. хоть раз будет ≥ mult × уровня
    дня d. Знает будущее — потолок, а не детектор. Дни без полного окна вперёд не размечены."""
    days = sorted(alt)
    out = {}
    for i, d in enumerate(days):
        if days[-1] - d < ahead * DAY:
            break
        fut = [alt[x] for x in days[i + 1:i + 1 + ahead + 7] if x - d <= ahead * DAY]
        out[d] = bool(fut) and max(fut) >= mult * alt[d]
    return out


def detector_stats(det: dict[int, bool], orc: dict[int, bool]) -> dict:
    share = {}
    for y in YEARS:
        v = [b for d, b in det.items() if time.gmtime(d).tm_year == y]
        share[str(y)] = sum(v) / len(v) if v else None
    both = [(b, orc[d]) for d, b in det.items() if d in orc and d >= FROM_2018]
    on = [o for b, o in both if b]
    off = [o for b, o in both if not b]
    return {"bull_share": share, "days_on": len(on), "days_off": len(off),
            "p_rise_on": sum(on) / len(on) if on else None,
            "p_rise_off": sum(off) / len(off) if off else None}


# ---------------------------------------------------------------- симуляция

def masked(seg: dict, bull: dict[int, bool]) -> dict:
    """Копия сегмента: high дня d = 0, если стадия дня d − LAG «бычья» — тейк в этот день не
    исполнится (simulate_exec читает high только проверкой тейка; сверка «всегда» = T это
    стережёт). Список новый, числа — те же объекты: память — 8 байт на день."""
    ts, h = seg["ts"], seg["h"]
    return {**seg, "h": [0.0 if bull.get(ts[d] - LAG * DAY, False) else h[d]
                         for d in range(len(h))]}


def make_sim(sell: dict, bull: dict[int, bool] | None):
    """sim(ep) -> строка simulate_exec | None, с кэшем по эпизоду (общий для эпизодов
    исследования и дней сигнала портфеля: сегменты — одни и те же объекты)."""
    segs: dict[int, dict] = {}
    cache: dict = {}

    def sim(ep):
        k = (ep["seg"]["sym"], ep["e"])
        if k not in cache:
            seg = ep["seg"]
            if bull is not None:
                if id(seg) not in segs:
                    segs[id(seg)] = masked(seg, bull)
                seg = segs[id(seg)]
            cache[k] = L.simulate_exec(seg, ep["e"], sell, budget=J.BUDGET, horizon=None,
                                       rules=L.EXEC_ALL)
        return cache[k]
    return sim


def same_rows(a: list[dict | None], b: list[dict | None], what: str) -> int:
    """Строки simulate_exec совпадают целиком (P&L ± 1e-9, остальное — точно)."""
    assert len(a) == len(b)
    bad, first = 0, None
    for x, y, in zip(a, b):
        if x is None or y is None:
            ok = x is None and y is None
        else:
            ok = abs(x["pnl"] - y["pnl"]) <= 1e-9 and all(
                (abs(x[k] - y[k]) <= 1e-9 if isinstance(y[k], float) else x[k] == y[k])
                for k in y if k != "pnl")
        if not ok:
            bad += 1
            first = first or (x, y)
    assert bad == 0, f"{what}: расхождение в {bad} эпизодах"
    return len(a)


# ---------------------------------------------------------------- метрики

def ep_stats(rows: list[dict | None], eps: list[dict]) -> dict:
    rr = [(r, ep) for r, ep in zip(rows, eps) if r]
    rob = sorted(r["rob"] for r, _ in rr)
    cyc = {}
    for c, *_ab in L.CYCLES:
        cc = [r for r, ep in rr if ep["cycle"] == c]
        if cc:
            cyc[c] = {"n": len(cc), "pnl": sum(r["pnl"] for r in cc),
                      "med": statistics.median(r["rob"] for r in cc)}
    return {"n": len(rr), "med": statistics.median(rob), "mean": statistics.fmean(rob),
            "p10": rob[len(rob) // 10], "win": sum(1 for x in rob if x > 0) / len(rob),
            "pnl": sum(r["pnl"] for r, _ in rr), "cycles": cyc}


def changed(rows: list[dict | None], base: list[dict | None]) -> dict:
    d = [x["pnl"] - b["pnl"] for x, b in zip(rows, base)
         if x and b and abs(x["pnl"] - b["pnl"]) > 1e-9]
    return {"n": len(d), "share": len(d) / len(base), "better": sum(1 for x in d if x > 0),
            "worse": sum(1 for x in d if x < 0), "med_delta": statistics.median(d) if d else 0.0,
            "sum_delta": sum(d)}


def by_year(rows: list[dict | None], base: list[dict | None], eps: list[dict]) -> dict:
    out = {}
    for y in YEARS:
        out[str(y)] = sum((x["pnl"] if x else 0.0) - (b["pnl"] if b else 0.0)
                          for x, b, ep in zip(rows, base, eps)
                          if time.gmtime(ep["day"]).tm_year == y)
    return out


def portfolio(cands: list[dict], sim, alt: dict, btc: dict) -> dict:
    """Шаг s6 portfolio_vs_market: по рангу + J.SEEDS случайных порядков сигналов в дне."""
    m = P.evaluate_flows(P.exec_book(cands, sim, *P.EXEC_POLICY, cooldown=True, cash=True),
                         alt, btc)
    runs = []
    for sd in range(J.SEEDS):
        x = P.evaluate_flows(P.exec_book(cands, sim, *P.EXEC_POLICY, cooldown=True, cash=True,
                                         order="random", seed=sd), alt, btc)
        runs.append({"pnl": x["pnl"], "vs_alt": x["pnl"] - x["alt"], "vs_btc": x["pnl"] - x["btc"]})
    m["random"] = {
        "pnl_mean": statistics.fmean(r["pnl"] for r in runs),
        "pnl_range": [min(r["pnl"] for r in runs), max(r["pnl"] for r in runs)],
        "vs_alt_mean": statistics.fmean(r["vs_alt"] for r in runs),
        "vs_alt_range": [min(r["vs_alt"] for r in runs), max(r["vs_alt"] for r in runs)],
        "beat_alt_runs": sum(1 for r in runs if r["vs_alt"] > 0),
        "vs_btc_mean": statistics.fmean(r["vs_btc"] for r in runs),
        "beat_btc_runs": sum(1 for r in runs if r["vs_btc"] > 0)}
    return m


def pc(x: float) -> str:
    return f"{x * 100:+.1f}%"


# ---------------------------------------------------------------- main

def main() -> int:
    t0 = time.time()
    from market_regime_study import load_market
    from scanner.config import load_config
    feats, full = load_market(load_config(None))
    hot = {d: h.get("n_lit", 0) for d, h in full.items()}       # = L.load_hot()
    coins = L.load_archive()
    all_segs = L.segments(coins)
    ranks = L.vol_ranks(all_segs)
    npairs = P.pairs_per_day(ranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    eps = [ep for ep in L.build_episodes(segs, ranks, hot, "spring") if ep["cycle"] != "?"]
    src = [ep for ep in L.signal_days(segs, ranks, hot, {"prod": L.is_spring_prod})["prod"]
           if ep["cycle"] != "?"]
    for ep in src:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
    alt, _ = P.market_index(ROOT / "scanner.db")
    btc = P.btc_closes(all_segs)
    print(f"пружин (через 90 дн.) {len(eps)}, дней сигнала прода {len(src)}; индекс альтов "
          f"{ymd(min(alt))}–{ymd(max(alt))}, BTC {ymd(min(btc))}–{ymd(max(btc))}  "
          f"[{time.time() - t0:.0f} с]")

    dets = {
        "BTC бычий (прод)": btc_bull(btc),
        "альты > SMA200": alt_above_sma200(feats),
        "перегрев ≥1": {d: v >= 1 for d, v in hot.items()},
        "перегрев ≥3": {d: v >= 3 for d, v in hot.items()},
        "ОРАКУЛ (будущее)": oracle(alt),
    }
    orc = dets["ОРАКУЛ (будущее)"]
    out: dict = {"meta": {
        "episodes": len(eps), "signal_days": len(src), "seeds": J.SEEDS, "budget": J.BUDGET,
        "slots": J.SLOTS, "lag_days": LAG, "oracle": {"days": ORACLE_DAYS, "mult": ORACLE_MULT},
        "oracle_last_day": ymd(max(orc)),
        "caveat": "архив Binance; бычьих фаз 3–4; лучший из детекторов выбран задним числом; "
                  "оракул знает будущее — потолок, а не детектор"}}

    # ---- 0. детекторы
    print(f"\n=== 0. Детекторы: доля «бычьих» дней по годам; P(индекс альтов за {ORACLE_DAYS} дн. "
          f"будет ≥ ×{ORACLE_MULT:g}) после «бычьего» дня и после остальных, с 2018 ===")
    print(f"  {'детектор':18} " + " ".join(f"{y:>5}" for y in YEARS)
          + f" | {'P(рост|бычий)':>13} {'P(рост|нет)':>11}")
    out["detectors"] = {}
    for name, det in dets.items():
        st = detector_stats(det, orc)
        out["detectors"][name] = st
        cells = [f"{v * 100:>4.0f}%" if v is not None else "    —"
                 for v in st["bull_share"].values()]
        on = "—" if st["p_rise_on"] is None else f"{st['p_rise_on'] * 100:.0f}%"
        off = "—" if st["p_rise_off"] is None else f"{st['p_rise_off'] * 100:.0f}%"
        print(f"  {name:18} " + " ".join(cells) + f" | {on:>13} {off:>11}")
    base_rate = [v for d, v in orc.items() if d >= FROM_2018]
    out["meta"]["oracle_base_rate"] = sum(base_rate) / len(base_rate)
    print(f"  «рост» — оракул; доля таких дней с 2018: {out['meta']['oracle_base_rate'] * 100:.0f}% "
          f"(размечено до {out['meta']['oracle_last_day']})")

    variants = ([("R правила", R_SELL, None), ("T только трейл", T_SELL, None),
                 ("H держать", H_SELL, None)]
                + [(f"R⇄T {n}", R_SELL, det) for n, det in dets.items()])
    sims = {name: make_sim(sell, det) for name, sell, det in variants}

    # ---- сверка механизма: разметка «никогда» = R, «всегда» = T
    lo = min(s["ts"][0] for s in segs) - DAY
    hi = max(s["ts"][-1] for s in segs) + DAY
    never = {d: False for d in range(lo, hi, DAY)}
    always = {d: True for d in range(lo, hi, DAY)}
    rows = {name: [sims[name](ep) for ep in eps] for name, *_ in variants}
    out["verify"] = {
        "never_eq_R": same_rows([make_sim(R_SELL, never)(ep) for ep in eps], rows["R правила"],
                                "разметка «никогда» ≠ R"),
        "always_eq_T": same_rows([make_sim(R_SELL, always)(ep) for ep in eps],
                                 rows["T только трейл"], "разметка «всегда» ≠ T")}
    print(f"\nсверка: «никогда» = R и «всегда» = T на всех {len(eps)} эпизодах "
          f"[{time.time() - t0:.0f} с]")

    # ---- 1. эпизоды
    print(f"\n=== 1. Эпизоды: все пружины 2018–2026 ({len(eps)}), 5×$10 до пола, без горизонта, "
          f"net-of-fees, доходность на бюджет ${J.BUDGET:g} ===")
    print(f"  {'вариант':28} {'n':>5} {'мед.':>7} {'средн.':>7} {'p10':>7} {'win':>4} {'Σ P&L':>8} | "
          + " | ".join(f"{c:>17}" for c, *_ab in L.CYCLES))
    out["episodes"], out["changed"], out["by_year"] = {}, {}, {}
    base = rows["R правила"]
    for name, *_ in variants:
        s = ep_stats(rows[name], eps)
        out["episodes"][name] = s
        cyc = [f"{v['pnl']:>+7.0f}$ мед {v['med'] * 100:>+4.0f}%" for v in s["cycles"].values()]
        print(f"  {name:28} {s['n']:>5} {pc(s['med']):>7} {pc(s['mean']):>7} {pc(s['p10']):>7} "
              f"{s['win'] * 100:>3.0f}% {s['pnl']:>+7.0f}$ | " + " | ".join(cyc))
        if name != "R правила":
            out["changed"][name] = changed(rows[name], base)
            out["by_year"][name] = by_year(rows[name], base, eps)
    print("\n  изменено против R (эпизоды с другим P&L): лучше / хуже, медиана и Σ Δ:")
    for name, c in out["changed"].items():
        if name.startswith("R⇄T"):
            print(f"  {name:28} {c['n']:>4} ({c['share'] * 100:>3.0f}%): лучше {c['better']:>4}, хуже "
                  f"{c['worse']:>4}, медиана Δ {c['med_delta']:>+6.2f}$, Σ Δ {c['sum_delta']:>+6.0f}$")
    print("\n  Δ Σ P&L против R по году сигнала, $:")
    print(f"  {'вариант':28} " + " ".join(f"{y:>7}" for y in YEARS))
    for name, v in out["by_year"].items():
        print(f"  {name:28} " + " ".join(f"{x:>+7.0f}" for x in v.values()))
    print(f"  [{time.time() - t0:.0f} с]")

    # ---- 2. портфель «как исполнитель»
    print(f"\n=== 2. Портфель «как исполнитель» (шаг s6): {J.SLOTS} × ${J.BUDGET:g}, детектор прода, "
          f"кулдаун 90 дн., USDT $1500, без горизонта; рынок — те же $ в те же дни (потоки) ===")
    print(f"  {'вариант':28} {'поз.':>5} {'вложено':>8} {'P&L':>7} {'−альты':>7} {'−BTC':>6} | "
          f"{J.SEEDS} порядков: {'P&L сред. (мин…макс)':>24} {'−альты сред.':>12} {'>альт':>6} "
          f"{'−BTC сред.':>10} {'>BTC':>5}")
    out["portfolio"] = {}
    for name, *_ in variants:
        m = portfolio(src, sims[name], alt, btc)
        out["portfolio"][name] = m
        ro = m["random"]
        print(f"  {name:28} {m['n']:>5} {m['spent']:>7.0f}$ {m['pnl']:>+6.0f}$ "
              f"{m['pnl'] - m['alt']:>+6.0f}$ {m['pnl'] - m['btc']:>+5.0f}$ | "
              f"{ro['pnl_mean']:>+8.0f}$ ({ro['pnl_range'][0]:>+5.0f}…{ro['pnl_range'][1]:>+5.0f}) "
              f"{ro['vs_alt_mean']:>+11.0f}$ {ro['beat_alt_runs']:>3}/{J.SEEDS} "
              f"{ro['vs_btc_mean']:>+9.0f}$ {ro['beat_btc_runs']:>2}/{J.SEEDS}  "
              f"[{time.time() - t0:.0f} с]")
    print("  по циклам входа, по рангу — P&L / альты / BTC, $ (поз.):")
    for name, m in out["portfolio"].items():
        print(f"  {name:28} " + " · ".join(
            f"{c} {v['pnl']:+5.0f} / {v['alt']:+5.0f} / {v['btc']:+5.0f} ({v['n']})"
            for c, v in m["cycles"].items()))
    try:   # база R/H обязана совпасть с portfolio_vs_market (шаг s6)
        prev = json.loads(PV_OUT.read_text(encoding="utf-8"))["exec"]
        diffs = {b: out["portfolio"][name]["pnl"] - prev[b]["s6"]["pnl"]
                 for b, name in (("R", "R правила"), ("H", "H держать"))}
        ok = all(abs(v) < 1e-6 for v in diffs.values())
        out["verify"]["portfolio_vs_market_s6"] = {"match": ok, "d_pnl": diffs}
        print(f"  сверка базы с {PV_OUT.name} (s6): "
              + ("совпало" if ok else f"РАСХОЖДЕНИЕ {diffs} — сначала portfolio_vs_market"))
    except (OSError, KeyError, ValueError) as e:
        print(f"  сверка с {PV_OUT.name} пропущена: {e}")

    print("\nВНИМАНИЕ: оракул знает будущее — это потолок идеи, а не детектор; лучший из четырёх "
          "детекторов выбран задним числом; лишние $ от удержания сравнивать с рынком по тем же "
          "потокам («−альты», «−BTC»).")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
