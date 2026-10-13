"""Гейт пробного исполнителя по месту монеты в рейтинге капитализации CMC на дату входа (Q9).

Вопрос владельца (13.10.2026): стоит ли ограничить покупки исполнителя монетами топ-200 /
150 / 100 / 50 по капе CMC или ввести квоту «вне топ-200 не больше N из 15 мест» вместо
запрета. Повод — Q6 (size_band_study): покупки книг вне топ-200 дали R +6% / −1% на $,
H −18% / −13% против R +21…+30% и H +143% в топ-20. Но это разбивка УЖЕ взятых позиций:
при гейте освободившиеся места и деньги займут другие сигналы — это и считаем здесь.

Модель — книги R и H исполнителя, шаг s6 как в Q4/Q6 (portfolio_vs_market.exec_book: 15 мест,
$50 лестница, кулдаун 90 дн., свободный USDT $1500, нижняя четверть по обороту и перегрев,
квота неликвида 5 мест; сверка: база повторяет s6 из portfolio_vs_market_results.json).
Гейт применяется ДО книги: сигнал не берётся — место и деньги свободны для следующих.

Место CMC на входе: последний снимок listings/historical (первое воскресенье месяца,
.cache/cmc_hist_deep, 3000 мест) не позже дня входа; монета Binance сопоставляется со
строкой снимка по тикеру и цене ±20% (top20_basket_study.Prices.match, ALIASES), берётся
лучшее место. Не сопоставлена / ниже 3000 — гейт не проходит (в квоте — «вне топ-200»).

Варианты: база; только топ-500 / 200 / 150 / 100 / 50; квота «вне топ-200 не больше 8 / 5 / 3
мест»; справочно — топ-150 по обороту Binance (гейт из junk_filter_study в той же модели).
Книги независимы: «гейт только для H» = строка варианта у H при базе у R.

Метрики по книге и варианту: позиций, вложено, P&L, P&L на $, против индекса альтов / BTC /
корзины топ-20 с ETH по капе (Q4, top20_basket_study basket_level «cap») по денежным потокам;
J.SEEDS случайных порядков сигналов внутри дня (в скольких лучше базы того же порядка);
умершие за 2 года / делистинги; сколько лучших исходов базы (верхний дециль P&L) пропало;
по циклам 2018–20 / 2020–22 / 2023–26; вне выборки — порог выбран по 2018–2022 (среднее по
порядкам, «P&L − альты»), проверка на 2023–26. Позиция относится к циклу по дню входа.

Оговорки: место — по снимку до ~5 недель до входа (живой гейт видел бы свежее); капа CMC у
монет с малым free float завышена; CMC data-api неофициальный; в 2018–19 на Binance почти
не было монет вне топ-200, поэтому гейт там режет мало; любые цифры — верхняя граница
(выжившие ×0.73, правила книг подобраны на той же истории).

Запуск из корня проекта:  py -3 backtest/cmc_rank_gate_study.py
Результат: backtest/cmc_rank_gate_results.json
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import statistics as st
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import junk_filter_study as J  # noqa: E402
import ladder_dca_study as L  # noqa: E402
import portfolio_vs_market as P  # noqa: E402
import size_band_study as S  # noqa: E402
import top20_basket_study as T  # noqa: E402

OUT = Path(__file__).resolve().parent / "cmc_rank_gate_results.json"
QUOTA_RANK = 200                 # квота считает места «вне топ-200»
IS_CYCLES = ("2018-20", "2020-22")
OOS_CYCLE = "2023-26"

VARIANTS = [
    {"key": "base", "label": "база (прод)"},
    {"key": "le500", "label": "только топ-500", "max_rank": 500},
    {"key": "le200", "label": "только топ-200", "max_rank": 200},
    {"key": "le150", "label": "только топ-150", "max_rank": 150},
    {"key": "le100", "label": "только топ-100", "max_rank": 100},
    {"key": "le50", "label": "только топ-50", "max_rank": 50},
    {"key": "q8", "label": "вне топ-200 ≤ 8 мест", "quota": 8},
    {"key": "q5", "label": "вне топ-200 ≤ 5 мест", "quota": 5},
    {"key": "q3", "label": "вне топ-200 ≤ 3 мест", "quota": 3},
    {"key": "vol150", "label": "справ.: топ-150 по обороту", "max_vol": 150},
]
BENCH = [("alt", "альты"), ("cap", "корз.топ-20/кап"), ("btc", "BTC")]


def outside(ep: dict, n: int) -> bool:
    return ep["cmc"] is None or ep["cmc"] > n


def policy(v: dict):
    """-> (take, quota неликвида, quota2) для exec_book."""
    base_take, illiq = P.EXEC_POLICY
    if "max_rank" in v:
        n = v["max_rank"]
        return (lambda ep: base_take(ep) and not outside(ep, n)), illiq, None
    if "max_vol" in v:
        n = v["max_vol"]
        return (lambda ep: base_take(ep) and ep["rank"] <= n), illiq, None
    if "quota" in v:
        return base_take, illiq, (lambda ep: outside(ep, QUOTA_RANK), v["quota"])
    return base_take, illiq, None


def matched_ranks(snaps, bad, days, px, last_day) -> list[dict[str, int]]:
    """По снимкам: сегмент Binance -> лучшее место CMC (как size_band_study, без сбоев капы)."""
    out = []
    for k, rows in enumerate(snaps):
        if days[k] > last_day:
            break
        mr: dict[str, int] = {}
        for x in sorted(rows, key=lambda r: r["rank"]):
            if f"{x['symbol']}|{x['name']}" in bad[k]:
                continue
            mt = px.match(x, days[k])
            if mt:
                mr.setdefault(mt[0], x["rank"])
        out.append(mr)
    return out


def ranked(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for t in range(i, j + 1):
            r[order[t]] = (i + j) / 2
        i = j + 1
    return r


def spearman(a: list[float], b: list[float]) -> float:
    return st.correlation(ranked(a), ranked(b))


def key_of(ep: dict) -> tuple[str, int]:
    return ep["seg"]["sym"], ep["day"]


def book_metrics(taken, series, base_best: set, gate) -> dict:
    """flows_vs (альты / корзина по капе / BTC, циклы) + умершие, делистинги, вне топ-200,
    пропавшие лучшие исходы базы (и сколько из них отрезал сам гейт)."""
    m = T.flows_vs(taken, series)
    keys = {key_of(ep) for ep, _r in taken}
    m["died2y"] = sum(1 for ep, _r in taken if ep["died2y"])
    m["delist"] = sum(1 for _ep, r in taken if r["how"] == "делистинг")
    m["open"] = sum(1 for _ep, r in taken if r["how"] == "открыта")
    m["out200"] = sum(1 for ep, _r in taken if outside(ep, QUOTA_RANK))
    lost = base_best - keys
    m["best_lost"] = len(lost)
    m["best_lost_by_gate"] = sum(1 for k in lost if not gate(k))
    return m


def seed_row(m: dict) -> dict:
    row = {"pnl": m["pnl"], "spent": m["spent"], **{k: m[k] for k, _n in BENCH}}
    row["cycles"] = {c: {"pnl": v["pnl"], "spent": v["spent"], **{k: v[k] for k, _n in BENCH}}
                     for c, v in m["cycles"].items()}
    return row


def cyc_sum(row: dict, cycles, key: str) -> float:
    """Сумма по циклам: key = 'pnl' или 'ex_<бенчмарк>' (P&L − бенчмарк)."""
    tot = 0.0
    for c in cycles:
        v = row["cycles"].get(c)
        if not v:
            continue
        tot += v["pnl"] - (v[key[3:]] if key.startswith("ex_") else 0.0)
    return tot


def pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


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

    dates = [d for d in T.first_sundays() if T.day_ts(d) <= last_day]
    snaps = [S.deep_snapshot(d) for d in dates]
    days = [T.day_ts(d) for d in dates]
    bad = T.glitches(snaps)
    for k, d in enumerate(dates):
        bad[k] |= {key for dd, key in T.GLITCHES if dd == d.isoformat()}
    mranks = matched_ranks(snaps, bad, days, px, last_day)
    print(f"снимков CMC {len(dates)} ({dates[0]} … {dates[-1]}), архив Binance до "
          f"{dt.datetime.fromtimestamp(last_day, dt.timezone.utc).date()}  [{time.time() - t0:.0f} с]")

    vranks = L.vol_ranks(all_segs)
    npairs = P.pairs_per_day(vranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot = L.load_hot()
    src = [ep for ep in L.signal_days(segs, vranks, hot, {"prod": L.is_spring_prod})["prod"]
           if ep["cycle"] != "?"]
    for ep in src:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
        k = bisect.bisect_right(days, ep["day"]) - 1
        ep["cmc"] = mranks[k].get(ep["seg"]["sym"]) if 0 <= k < len(mranks) else None
    print(f"дней сигнала прода {len(src)}  [{time.time() - t0:.0f} с]")

    out: dict = {"meta": {"seeds": J.SEEDS, "slots": J.SLOTS, "budget": J.BUDGET,
                          "quota_rank": QUOTA_RANK, "is_cycles": IS_CYCLES, "oos_cycle": OOS_CYCLE,
                          "first_snapshot": dates[0].isoformat(), "last_snapshot": dates[-1].isoformat(),
                          "archive_last_day": last_day,
                          "variants": {v["key"]: v["label"] for v in VARIANTS}}}

    # ---- ранг CMC и ранг оборота — разные вещи? (сигналы, прошедшие фильтры прода)
    base_take = P.EXEC_POLICY[0]
    sig = [ep for ep in src if base_take(ep)]
    both = [ep for ep in sig if ep["cmc"] is not None]
    rho = spearman([ep["cmc"] for ep in both], [ep["rank"] for ep in both])
    cross = {}
    for c, _a, _b in L.CYCLES + [("всё", 0, 0)]:
        ss = [ep for ep in sig if c == "всё" or ep["cycle"] == c]
        if not ss:
            continue
        cross[c] = {"n": len(ss),
                    "unmatched": sum(1 for ep in ss if ep["cmc"] is None) / len(ss),
                    **{f"out{n}": sum(1 for ep in ss if outside(ep, n)) / len(ss)
                       for n in (50, 100, 150, 200, 500)},
                    "vol_top150": sum(1 for ep in ss if ep["rank"] <= 150) / len(ss),
                    "vol150_out200": sum(1 for ep in ss if ep["rank"] <= 150 and outside(ep, 200))
                    / max(1, sum(1 for ep in ss if ep["rank"] <= 150)),
                    "cmc200_vol_gt150": sum(1 for ep in ss if not outside(ep, 200) and ep["rank"] > 150)
                    / max(1, sum(1 for ep in ss if not outside(ep, 200)))}
    out["signals"] = {"spearman_cmc_vs_volrank": rho, "by_cycle": cross}
    print(f"\nСигналы, прошедшие фильтры прода: {len(sig)}; связь места CMC и места по обороту "
          f"Binance (Спирмен) {rho:+.2f}")
    print(f"  {'цикл':8} {'сигн.':>6} {'не сопост.':>10} " + " ".join(f"{'вне ' + str(n):>8}"
                                                                for n in (50, 100, 150, 200, 500))
          + f" {'обор.≤150':>9} {'из них вне 200':>14} {'CMC≤200, обор.>150':>18}")
    for c, v in cross.items():
        print(f"  {c:8} {v['n']:>6} {v['unmatched']:>10.0%} "
              + " ".join(f"{v[f'out{n}']:>8.0%}" for n in (50, 100, 150, 200, 500))
              + f" {v['vol_top150']:>9.0%} {v['vol150_out200']:>14.0%} {v['cmc200_vol_gt150']:>18.0%}")

    ref = json.loads((Path(__file__).resolve().parent / "portfolio_vs_market_results.json")
                     .read_text(encoding="utf-8"))["exec"]
    by_key = {key_of(ep): ep for ep in src}
    for b, (sell, _stop) in J.BOOKS.items():
        cache: dict = {}

        def sim(ep, sell=sell, cache=cache):
            k = (ep["seg"]["sym"], ep["e"])
            if k not in cache:
                cache[k] = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET,
                                           horizon=None, rules=L.EXEC_ALL)
            return cache[k]

        base = P.exec_book(src, sim, *P.EXEC_POLICY, cooldown=True, cash=True)
        chk = P.evaluate_flows(base, alt, btc)
        s6 = ref[b]["s6"]
        assert chk["n"] == s6["n"] and abs(chk["pnl"] - s6["pnl"]) < 1e-6 and \
            abs(chk["alt"] - s6["alt"]) < 1e-6, f"{b}: s6 не повторился ({chk['pnl']} / {s6['pnl']})"
        top = sorted(base, key=lambda t: t[1]["pnl"], reverse=True)[:max(1, len(base) // 10)]
        base_best = {key_of(ep) for ep, _r in top}
        base_keys = {key_of(ep) for ep, _r in base}
        ob: dict = {"best_threshold_pnl": top[-1][1]["pnl"],
                    "best": [{"coin": ep["seg"]["sym"].split("#")[0][:-4], "cmc": ep["cmc"],
                              "day": dt.datetime.fromtimestamp(ep["day"], dt.timezone.utc).date()
                              .isoformat(), "pnl": r["pnl"]} for ep, r in top],
                    "variants": {}}
        seeds_base = None
        for v in VARIANTS:
            take, illiq, q2 = policy(v)

            def gate_k(k, take=take):
                return take(by_key[k])
            taken = P.exec_book(src, sim, take, illiq, cooldown=True, cash=True, quota2=q2)
            m = book_metrics(taken, series, base_best, gate_k)
            keys = {key_of(ep) for ep, _r in taken}
            new = [(ep, r) for ep, r in taken if key_of(ep) not in base_keys]
            gone = [(ep, r) for ep, r in base if key_of(ep) not in keys]
            m["new"] = {"n": len(new), "spent": sum(r["spent"] for _e, r in new),
                        "pnl": sum(r["pnl"] for _e, r in new)}
            m["gone"] = {"n": len(gone), "spent": sum(r["spent"] for _e, r in gone),
                         "pnl": sum(r["pnl"] for _e, r in gone)}
            m["lost_best"] = sorted(f"{ep['seg']['sym'].split('#')[0][:-4]} "
                                    f"({ep['cmc'] or '—'}) {r['pnl']:+.0f}$"
                                    for ep, r in top if key_of(ep) not in keys)
            runs = [seed_row(T.flows_vs(P.exec_book(src, sim, take, illiq, cooldown=True,
                                                    cash=True, order="random", seed=sd,
                                                    quota2=q2), series))
                    for sd in range(J.SEEDS)]
            if v["key"] == "base":
                seeds_base = runs
            rnd = {"pnl_mean": st.fmean(x["pnl"] for x in runs),
                   "spent_mean": st.fmean(x["spent"] for x in runs),
                   "beat_base_pnl": sum(1 for x, y in zip(runs, seeds_base) if x["pnl"] > y["pnl"]),
                   "beat_base_ex_alt": sum(1 for x, y in zip(runs, seeds_base)
                                           if x["pnl"] - x["alt"] > y["pnl"] - y["alt"]),
                   "beat_base_per_usd": sum(1 for x, y in zip(runs, seeds_base)
                                            if x["pnl"] / x["spent"] > y["pnl"] / y["spent"])}
            for k, _n in BENCH:
                rnd[f"ex_{k}_mean"] = st.fmean(x["pnl"] - x[k] for x in runs)
                rnd[f"ex_{k}_per_usd"] = st.fmean((x["pnl"] - x[k]) / x["spent"] for x in runs)
                rnd[f"beat_{k}_runs"] = sum(1 for x in runs if x["pnl"] > x[k])
            for part, cyc in (("is", IS_CYCLES), ("oos", (OOS_CYCLE,))):
                rnd[f"{part}_pnl"] = st.fmean(cyc_sum(x, cyc, "pnl") for x in runs)
                for k, _n in BENCH:
                    rnd[f"{part}_ex_{k}"] = st.fmean(cyc_sum(x, cyc, f"ex_{k}") for x in runs)
            m["random"] = rnd
            ob["variants"][v["key"]] = m
        out[b] = ob

        # ---- печать книги
        name = "R правила" if b == "R" else "H держать"
        vb = ob["variants"]["base"]
        print(f"\n=== книга {name}: база поз. {vb['n']}, вложено {vb['spent']:.0f}$, P&L "
              f"{vb['pnl']:+.0f}$ (сверка s6 ✓); лучшие 10% базы — P&L ≥ {ob['best_threshold_pnl']:+.0f}$ "
              f"({len(top)} поз.)")
        print("  по рангу (как прод): книга минус та же сумма в те же дни в рынок, $ (на $)")
        print(f"  {'вариант':28} {'поз.':>4} {'влож.':>6} {'P&L':>6} {'на $':>7} "
              + " ".join(f"{'−' + n:>17}" for _k, n in BENCH)
              + f" {'вне200':>6} {'умерли':>6} {'делист':>6} {'лучш.пропало':>12} {'новые / ушли из базы':>22}")
        for v in VARIANTS:
            m = ob["variants"][v["key"]]
            print(f"  {v['label']:28} {m['n']:>4} {m['spent']:>6.0f} {m['pnl']:>+6.0f} "
                  f"{pct(m['pnl'] / m['spent']):>7} "
                  + " ".join(f"{m['pnl'] - m[k]:+.0f} ({pct((m['pnl'] - m[k]) / m['spent'])})".rjust(17)
                             for k, _n in BENCH)
                  + f" {m['out200']:>6} {m['died2y']:>6} {m['delist']:>6} "
                  + f"{m['best_lost']} (гейт {m['best_lost_by_gate']})".rjust(12)
                  + f"{m['new']['n']} {m['new']['pnl']:+.0f}$ / {m['gone']['n']} {m['gone']['pnl']:+.0f}$"
                  .rjust(23))
        print(f"  {J.SEEDS} случайных порядков сигналов внутри дня (средние; «лучше базы» — "
              f"против базы того же порядка):")
        print(f"  {'вариант':28} {'P&L':>6} {'на $':>7} " + " ".join(f"{'−' + n + ' на $':>21}"
                                                             for _k, n in BENCH)
              + f" {'лучше рынка альт/корз/BTC':>25} {'лучше базы: P&L / −альты / на $':>32}")
        for v in VARIANTS:
            r = ob["variants"][v["key"]]["random"]
            print(f"  {v['label']:28} {r['pnl_mean']:>+6.0f} {pct(r['pnl_mean'] / r['spent_mean']):>7} "
                  + " ".join(f"{r[f'ex_{k}_mean']:+.0f}$ ({pct(r[f'ex_{k}_per_usd'])})".rjust(21)
                             for k, _n in BENCH)
                  + f"{r['beat_alt_runs']}/{r['beat_cap_runs']}/{r['beat_btc_runs']}".rjust(26)
                  + f"{r['beat_base_pnl']} / {r['beat_base_ex_alt']} / {r['beat_base_per_usd']}".rjust(33))
        print("  по циклам (по рангу): P&L книги, в скобках P&L − альты, $")
        print(f"  {'вариант':28} " + " ".join(f"{c:>18}" for c, _a, _b in L.CYCLES))
        for v in VARIANTS:
            m = ob["variants"][v["key"]]
            print(f"  {v['label']:28} " + " ".join(
                f"{m['cycles'][c]['pnl']:>+7.0f} ({m['cycles'][c]['pnl'] - m['cycles'][c]['alt']:>+6.0f})"
                .rjust(18) if m["cycles"][c]["n"] else f"{'—':>18}" for c, _a, _b in L.CYCLES))
        print("  пропали из лучших 10% базы: " + "; ".join(
            f"{v['key']}: {', '.join(ob['variants'][v['key']]['lost_best']) or '—'}"
            for v in VARIANTS if v["key"] != "base"))

        # ---- вне выборки: порог по 2018–2022, проверка на 2023–26 (среднее по порядкам)
        oos = {}
        cand = [v for v in VARIANTS if v["key"] != "vol150"]
        for crit in ("ex_alt", "pnl"):
            pick = max(cand, key=lambda v: ob["variants"][v["key"]]["random"][f"is_{crit}"])
            vb_r = ob["variants"]["base"]["random"]
            vp_r = ob["variants"][pick["key"]]["random"]
            oos[crit] = {"pick": pick["key"],
                         "is": {k: vp_r[f"is_{k}"] - vb_r[f"is_{k}"]
                                for k in ("pnl", "ex_alt", "ex_cap", "ex_btc")},
                         "oos": {k: vp_r[f"oos_{k}"] - vb_r[f"oos_{k}"]
                                 for k in ("pnl", "ex_alt", "ex_cap", "ex_btc")},
                         "oos_abs": {k: vp_r[f"oos_{k}"] for k in ("pnl", "ex_alt", "ex_cap", "ex_btc")},
                         "oos_base": {k: vb_r[f"oos_{k}"] for k in ("pnl", "ex_alt", "ex_cap", "ex_btc")}}
            o = oos[crit]
            print(f"  вне выборки (крит. {'P&L − альты' if crit == 'ex_alt' else 'P&L'} за "
                  f"2018–2022, среднее {J.SEEDS} порядков): выбран «{pick['label']}»; к базе в "
                  f"2018–22 P&L {o['is']['pnl']:+.0f}$, −альты {o['is']['ex_alt']:+.0f}$; в 2023–26 "
                  f"P&L {o['oos']['pnl']:+.0f}$, −альты {o['oos']['ex_alt']:+.0f}$, −корзина "
                  f"{o['oos']['ex_cap']:+.0f}$, −BTC {o['oos']['ex_btc']:+.0f}$ (база 2023–26: P&L "
                  f"{o['oos_base']['pnl']:+.0f}$, −альты {o['oos_base']['ex_alt']:+.0f}$)")
        ob["oos"] = oos
        print("  все варианты, среднее порядков, к базе: 2018–22 −альты | 2023–26 −альты: " + "; ".join(
            f"{v['key']} {ob['variants'][v['key']]['random']['is_ex_alt'] - ob['variants']['base']['random']['is_ex_alt']:+.0f}"
            f" | {ob['variants'][v['key']]['random']['oos_ex_alt'] - ob['variants']['base']['random']['oos_ex_alt']:+.0f}"
            for v in VARIANTS if v["key"] != "base"))
        print(f"  [{time.time() - t0:.0f} с]")

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
