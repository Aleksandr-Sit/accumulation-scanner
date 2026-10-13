"""Ротация «на дне только BTC -> потом ETH -> потом альты»: проследить на истории и посчитать.

Вопрос владельца (12.10.2026): теория инвесторов — на дне откупают только биткоин, потому что
сначала растёт он, затем деньги переливаются в эфир, а после — из эфира в альткоины. Насколько
это рабочий вариант.

Данные: снимки CMC listings/historical на первое воскресенье месяца 2015-01 … (кэш
.cache/cmc_hist_deep, как Q6; цена снимка по id монеты, выбросы цены сглажены). Помесячно —
зато три цикла: 2015–18, 2018–22, 2022–26 (архив Binance начинается только с 2017-08).
Ряды: BTC, ETH, ALT20 — топ-20 альтов без BTC/ETH/стейблов/обёрток (исключения Q4) по капе
и равными долями, ALT200 — топ-200 по капе (широкий рынок без ETH); ребаланс раз в месяц,
комиссия 0.15% на оборот, монета выпала из 3000 мест — последняя виденная цена.

Что считаем:
  1. по циклам (дно = месячный минимум BTC в окне цикла, вершина = максимум BTC после него):
     дно и вершина каждого ряда, рост «дно -> вершина», минимумы/максимумы ETH/BTC и ALT/ETH,
     лидер каждого полугодия после дна — есть ли порядок BTC -> ETH -> альты;
  2. стратегии от дна до вершины BTC и на 12/24/36 мес.: держать BTC / ETH / ALT20 / поровну
     1/3; ротация по правилу (переход BTC -> ETH, когда ETH/BTC выше своей средней за n мес. и
     выше, чем n мес. назад; ETH -> ALT20 — то же для ALT20/ETH; только вперёд), n = 3 и 6;
     «оракул» — лучшие из возможных месяцы переходов BTC -> ETH -> ALT20 задним числом
     (потолок ротации, в жизни недостижим). Переход — 0.3% (продажа + покупка);
  3. все месяцы «у дна» (BTC ≤ 1.3 × минимума за 12 мес.): держать 24 мес. — правило против
     держать BTC / ETH / ALT20 / поровну.

Оговорки: три цикла — три наблюдения; в 2015–17 ETH сам был новорождённой монетой (рост
×1000 не повторяем); месячный шаг сглаживает внутримесячные развороты; CMC data-api
неофициальный.

Запуск из корня проекта:  py -3 backtest/rotation_study.py
Результат: backtest/rotation_results.json
"""
from __future__ import annotations

import datetime as dt
import json
import statistics as st
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import size_band_study as S  # noqa: E402
import top20_basket_study as T  # noqa: E402

OUT = Path(__file__).resolve().parent / "rotation_results.json"
BTC_ID, ETH_ID = 1, 1027
FEE = 0.0015
SWITCH = 2 * FEE
START = (2015, 1)
# окна циклов (дно ищется внутри окна как месячный минимум BTC)
CYCLES = [("2015–18", dt.date(2015, 1, 1), dt.date(2018, 6, 30)),
          ("2018–22", dt.date(2018, 7, 1), dt.date(2022, 6, 30)),
          ("2022–26", dt.date(2022, 7, 1), dt.date(2030, 1, 1))]
EXTRA = [("после обвала 2020-03", dt.date(2020, 4, 5), dt.date(2021, 11, 7))]
RULE_N = (3, 6)
HORIZONS = (12, 24, 36)
LOW_K = 1.3


def first_sundays(today: dt.date) -> list[dt.date]:
    out, (y, m) = [], START
    while True:
        d = dt.date(y, m, 1)
        d += dt.timedelta(days=(6 - d.weekday()) % 7)
        if d > today:
            return out
        out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def price_at(s: dict[int, float], k: int) -> float | None:
    if k in s:
        return s[k]
    prev = [j for j in s if j <= k]
    return s[max(prev)] if prev else None


def alt_level(snaps, bad, ser, n: int, weights: str) -> list[float | None]:
    """Уровень корзины n крупнейших альтов без ETH, ребаланс раз в месяц (None до старта)."""
    out: list[float | None] = []
    v, hold = 1.0, {}
    for k, rows in enumerate(snaps):
        drift = {}
        if hold:
            rets = {i: price_at(ser[i], k) / ser[i][k - 1] for i in hold}
            gross = sum(w * rets[i] for i, w in hold.items())
            v *= gross
            drift = {i: w * rets[i] / gross for i, w in hold.items()}
        mem = []
        for x in sorted(rows, key=lambda r: r["rank"]):
            if len(mem) >= n:
                break
            if x["id"] in (BTC_ID, ETH_ID) or T.excluded(x) or \
                    f"{x['symbol']}|{x['name']}" in bad[k] or k not in ser.get(x["id"], {}):
                continue
            mem.append(x)
        tot = sum(x["mcap"] or 0.0 for x in mem)
        tgt = {x["id"]: (1 / len(mem) if weights == "eq" or not tot else (x["mcap"] or 0.0) / tot)
               for x in mem}
        v *= 1 - FEE * sum(abs(tgt.get(i, 0.0) - drift.get(i, 0.0)) for i in set(tgt) | set(drift))
        hold = tgt
        out.append(v if hold else None)
    return out


# ---------------------------------------------------------------- стратегии

def hold(s: list, a: int, b: int) -> float | None:
    return s[b] / s[a] * (1 - FEE) ** 2 if s[a] and s[b] else None


def rule_rotation(btc, eth, alt, a: int, b: int, n: int) -> tuple[float, list[str]]:
    """Только вперёд: BTC -> ETH -> ALT. Решение на закрытии месяца k, сделка по цене k."""
    eb = [e / x if e and x else None for e, x in zip(eth, btc)]
    ae = [al / e if al and e else None for al, e in zip(alt, eth)]
    series = {"BTC": btc, "ETH": eth, "ALT": alt}
    state, v, log = "BTC", (1 - FEE), []
    for k in range(a + 1, b + 1):
        v *= series[state][k] / series[state][k - 1]
        r = eb if state == "BTC" else ae if state == "ETH" else None
        if r is None or k - n < 0 or any(r[j] is None for j in range(k - n, k + 1)):
            continue
        ma = st.fmean(r[k - n + 1:k + 1])
        if r[k] > ma and r[k] > r[k - n]:
            state = "ETH" if state == "BTC" else "ALT"
            v *= 1 - SWITCH
            log.append(f"{state}@{k}")
    return v * (1 - FEE), log


def oracle(btc, eth, alt, a: int, b: int) -> tuple[float, int, int]:
    """Лучшие месяцы перехода BTC -> ETH (m1) -> ALT (m2), a ≤ m1 ≤ m2 ≤ b."""
    best = (0.0, a, a)
    for m1 in range(a, b + 1):
        f1 = btc[m1] / btc[a]
        for m2 in range(m1, b + 1):
            if not alt[m2]:
                continue
            v = f1 * eth[m2] / eth[m1] * alt[b] / alt[m2]
            v *= (1 - SWITCH) ** ((m1 < b) + (m2 < b))
            if v > best[0]:
                best = (v, m1, m2)
    return best[0] * (1 - FEE) ** 2, best[1], best[2]


def strategies(btc, eth, alt, alt_eq, a: int, b: int) -> dict:
    o = {"держать BTC": hold(btc, a, b), "держать ETH": hold(eth, a, b),
         "держать ALT20 кап": hold(alt, a, b), "держать ALT20 равн": hold(alt_eq, a, b)}
    parts = [o["держать BTC"], o["держать ETH"], o["держать ALT20 кап"]]
    o["поровну BTC/ETH/ALT20"] = st.fmean(parts) if None not in parts else None
    for n in RULE_N:
        v, log = rule_rotation(btc, eth, alt, a, b, n)
        o[f"ротация n={n}"] = v
        o[f"ротация n={n} log"] = log
    v, m1, m2 = oracle(btc, eth, alt, a, b)
    o["оракул"], o["оракул m1"], o["оракул m2"] = v, m1, m2
    return o


# ---------------------------------------------------------------- main

def main() -> int:
    t0 = time.time()
    today = dt.datetime.now(dt.timezone.utc).date()
    dates = first_sundays(today)
    snaps = [S.deep_snapshot(d) for d in dates]
    bad = T.glitches(snaps)
    for k, d in enumerate(dates):
        bad[k] |= {key for dd, key in T.GLITCHES if dd == d.isoformat()}
    ser = S.clean_series(snaps)
    K = len(dates)
    lab = [d.strftime("%Y-%m") for d in dates]
    btc = [ser[BTC_ID].get(k) for k in range(K)]
    eth = [ser.get(ETH_ID, {}).get(k) for k in range(K)]
    alt = alt_level(snaps, bad, ser, 20, "cap")
    alt_eq = alt_level(snaps, bad, ser, 20, "eq")
    alt200 = alt_level(snaps, bad, ser, 200, "cap")
    k_eth = next(k for k in range(K) if eth[k])
    print(f"снимков {K} ({lab[0]} … {lab[-1]}), ETH в CMC с {lab[k_eth]}  [{time.time() - t0:.0f} с]")
    # ряды выровнены по старту ETH: до него ротация невозможна
    for s in (alt, alt_eq, alt200):
        for k in range(k_eth):
            s[k] = None
    rows = {"BTC": btc, "ETH": eth, "ALT20 кап": alt, "ALT20 равн": alt_eq, "ALT200 кап": alt200}
    out: dict = {"meta": {"first": lab[0], "last": lab[-1], "eth_from": lab[k_eth], "fee": FEE,
                          "rule_n": RULE_N}, "cycles": {}, "cohorts": {}}

    # ---- 1. по циклам
    def kidx(d: dt.date) -> int:
        return max((k for k in range(K) if dates[k] <= d), default=0)
    cyc = []
    for name, a, b in CYCLES:
        ka, kb = max(kidx(a), k_eth), min(kidx(b), K - 1)
        kbot = min(range(ka, kb + 1), key=lambda k: btc[k])
        ktop = max(range(kbot, kb + 1), key=lambda k: btc[k])
        cyc.append((name, kbot, ktop, kb))
    for name, a, b in EXTRA:
        cyc.append((name, kidx(a), kidx(b), kidx(b)))
    for name, kbot, ktop, kend in cyc:
        o: dict = {"bottom": lab[kbot], "btc_top": lab[ktop]}
        print(f"\n=== цикл {name}: дно BTC {lab[kbot]} (${btc[kbot]:,.0f}), вершина BTC {lab[ktop]} "
              f"(${btc[ktop]:,.0f})")
        print("  ряд          дно ряда   пик ряда    дно→пик  от дна BTC до вершины BTC")
        o["series"] = {}
        for rn, s in rows.items():
            lo_a = max(kbot - 6, 0)
            klo = min((k for k in range(lo_a, ktop + 1) if s[k]), key=lambda k: s[k])
            khi = max((k for k in range(klo, kend + 1) if s[k]), key=lambda k: s[k])
            m = s[ktop] / s[kbot] if s[kbot] and s[ktop] else None
            o["series"][rn] = {"low": lab[klo], "peak": lab[khi], "low_to_peak": s[khi] / s[klo],
                               "btc_bottom_to_btc_top": m}
            print(f"  {rn:12} {lab[klo]:>9} {lab[khi]:>10} {s[khi] / s[klo]:>9.1f}× "
                  f"{'—' if m is None else f'{m:.1f}×':>10}")
        rat = {"ETH/BTC": [eth[k] / btc[k] if eth[k] else None for k in range(K)],
               "ALT20/ETH": [alt[k] / eth[k] if alt[k] and eth[k] else None for k in range(K)],
               "ALT20/BTC": [alt[k] / btc[k] if alt[k] else None for k in range(K)]}
        o["ratios"] = {}
        for rn, r in rat.items():
            ks = [k for k in range(kbot, kend + 1) if r[k]]
            kmin = min(ks, key=lambda k: r[k])
            kmax = max((k for k in ks if k >= kmin), key=lambda k: r[k])
            o["ratios"][rn] = {"min": lab[kmin], "max_after": lab[kmax], "mult": r[kmax] / r[kmin],
                               "at_btc_top_vs_bottom": r[ktop] / r[kbot] if r[kbot] and r[ktop] else None}
            print(f"  {rn:10} минимум {lab[kmin]}, максимум после него {lab[kmax]} "
                  f"(×{r[kmax] / r[kmin]:.1f}); на вершине BTC к дну: "
                  f"{o['ratios'][rn]['at_btc_top_vs_bottom']:.2f}×")
        print("  лидер полугодий после дна (рост за полугодие: BTC / ETH / ALT20 кап / ALT20 равн):")
        o["halves"] = []
        k = kbot
        while k < min(ktop, kend):
            k2 = min(k + 6, kend)
            g = {rn: (rows[rn][k2] / rows[rn][k] - 1) if rows[rn][k] and rows[rn][k2] else None
                 for rn in ("BTC", "ETH", "ALT20 кап", "ALT20 равн")}
            lead = max((rn for rn in g if g[rn] is not None), key=lambda rn: g[rn])
            o["halves"].append({"from": lab[k], "to": lab[k2], **g, "leader": lead})
            print(f"    {lab[k]}→{lab[k2]}: " + " / ".join(f"{g[rn]:+.0%}" for rn in g) +
                  f"  лидер {lead}")
            k = k2
        # ---- 2. стратегии от дна
        o["strategies"] = {}
        ends = [("до вершины BTC", ktop)] + [(f"{h} мес.", kbot + h) for h in HORIZONS
                                              if kbot + h <= K - 1]
        print("  стратегии от дна (кратное капитала; ротация — месяцы переходов):")
        for en, ke in ends:
            sres = strategies(btc, eth, alt, alt_eq, kbot, ke)
            o["strategies"][en] = {**sres, "оракул m1": lab[sres["оракул m1"]],
                                   "оракул m2": lab[sres["оракул m2"]], "end": lab[ke]}
            logs = {n: ", ".join(f"{x.split('@')[0]} {lab[int(x.split('@')[1])]}"
                                 for x in sres[f"ротация n={n} log"]) for n in RULE_N}
            print(f"    {en:15} BTC ×{sres['держать BTC']:.1f}  ETH ×{sres['держать ETH']:.1f}  "
                  f"ALT20 кап ×{sres['держать ALT20 кап']:.1f}  равн ×{sres['держать ALT20 равн']:.1f}  "
                  f"поровну ×{sres['поровну BTC/ETH/ALT20']:.1f} | " +
                  "  ".join(f"ротация n={n} ×{sres[f'ротация n={n}']:.1f} [{logs[n] or 'весь срок BTC'}]"
                            for n in RULE_N) +
                  f" | оракул ×{sres['оракул']:.1f} (ETH {lab[sres['оракул m1']]}, ALT {lab[sres['оракул m2']]})")
        out["cycles"][name] = o

    # ---- 3. все месяцы «у дна» по BTC, держать 24 мес.
    h = 24
    lows = [k for k in range(max(k_eth, 12), K - h)
            if btc[k] <= LOW_K * min(btc[j] for j in range(k - 12, k + 1)) and alt[k]]
    res = {}
    for k in lows:
        sres = strategies(btc, eth, alt, alt_eq, k, k + h)
        for key, v in sres.items():
            if isinstance(v, float):
                res.setdefault(key, []).append(v)
    names = [k for k in res if k != "оракул"]
    print(f"\n=== все месяцы «у дна» (BTC ≤ {LOW_K}× минимума за 12 мес.), держать {h} мес.: "
          f"{len(lows)} мес. — {', '.join(lab[k] for k in lows)}")
    base = res["держать BTC"]
    for key in names + ["оракул"]:
        v = res[key]
        print(f"  {key:24} медиана ×{st.median(v):.2f}  лучше «держать BTC» в "
              f"{sum(x > y for x, y in zip(v, base)) / len(v):.0%}, лучше «поровну» в "
              f"{sum(x > y for x, y in zip(v, res['поровну BTC/ETH/ALT20'])) / len(v):.0%}")
        out["cohorts"][key] = {"median": st.median(v), "values": v}
    out["cohorts"]["months"] = [lab[k] for k in lows]
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
