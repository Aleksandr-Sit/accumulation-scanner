"""«Случайный вход с теми же выходами»: добавляет ли отбор пружин что-то к лестнице и выходам (Q18).

Вопрос (шаг 1 разбора docs/LAB_IDEAS_REPORT.md, метод лаборатории): на истории книги R и H
исполнителя рынок не обгоняют (s6: R +$1503 / −$186 к альтам по прод-порядку, H +$1903 /
−$1083). Добавляет ли сам ОТБОР — какая монета и в какой день — что-нибудь к лестнице 5 × $10 и
выходам R/H, или случайные входы с теми же правилами дают то же самое?

Модель — шаг s6 (portfolio_vs_market.exec_book: 15 мест, лестница $50, EXEC_POLICY — нижняя
четверть по обороту и перегрев не покупаются, квота неликвида 5 мест, свободный USDT $1500,
кулдаун 90 дн. от взятого входа, без горизонта), бенчмарк по денежным потокам (как
P.evaluate_flows). Книги R и H независимы. Сверка: прод-порядок повторяет s6, 30 случайных
порядков — R ≈ +$1440 / −$278, H ≈ +$1864 / −$1176 (Q13, Q17).

Пул покупаемых монет по дням: все (сегмент, t), t ∈ [200, len(c) − 30), где считаются признаки
зоны (feature_study.features — та же функция и тот же проход, что L.signal_days), цикл не "?",
и сигнал прошёл бы фильтр take из EXEC_POLICY: доля места по обороту share ≤ 0.75 и перегрев
hot == 0 (hot — флаги рынка вчера, как _episode). Флаги дня: dd70 — просадка от максимума
закрытий сегмента по t включительно ≥ 70% (features dd_ath), spring — пружина прода
(L.is_spring_prod) — то есть реальный сигнал этого дня.

Серии реальных сигналов: дни сигнала прода, прошедшие take, по сегменту; подряд идущие дни
одной монеты с разрывом ≤ 7 дней — одна серия (монета c, дни d1…dk).

Плацебо (каждое — DRAWS = 100 розыгрышей; розыгрыш i — random.Random(1000 + i) для выбора и
тот же seed для случайного порядка сигналов внутри дня в exec_book):
  Д — на каждую реальную серию одна случайная монета c' равновероятно из пула дня d1 (кроме c);
      кандидаты — (c', di) для тех di серии, где c' есть в пуле дня di. Сохраняет число «имён»,
      дни и структуру серий; меняется только монета.
  П — то же, но c' — из монет пула дня d1 с dd70 и не пружина в этот день (таких нет — серия
      пропускается, счётчик skipped).
  М — та же монета c, серия сдвинута: d1' равновероятно из дней пула этого сегмента,
      di' = d1' + (di − d1), остаются di', которые есть в пуле.
  Кандидат-плацебо, совпавший с реальным сигналом, не исключается (доля таких печатается).

Метрики прогона (реальные 30 порядков и каждый розыгрыш): позиций, вложено, P&L, альты и BTC по
потокам, M = (P&L − альты) / вложено — в целом и по циклам L.CYCLES (цикл позиции — день входа),
средний работающий капитал, доля позиций лучше альтов, M без 5 лучших позиций (5 позиций с
наибольшим P&L убираются вместе с их бенчмарком). Строки позиций — копия логики
P.evaluate_flows (сверено assert'ом на всех реальных прогонах); portfolio_vs_market не менялся.

Правило решения (docs/DECISIONS.md, Q18; записано 23.10.2026 до прогона, после прогона не
менять): Метрика M = (P&L − альты) / вложено по книге за 2018–2026. Реальная книга — среднее по
30 случайным порядкам сигналов внутри дня; каждое плацебо — 100 розыгрышей (свой случайный выбор
монет/сдвигов и порядок). «Правило решения (записано до прогона, после прогона не менять): для
пары (книга, плацебо) «отбор добавляет», если одновременно (1) M реальной книги выше 95-го
перцентиля плацебо (хуже — не меньше 96 из 100 розыгрышей); (2) M выше медианы плацебо минимум в
2 из 3 циклов (2018-20, 2020-22, 2023-26); (3) M выше медианы плацебо в 2023–26 отдельно; (4) без
5 лучших позиций реальной книги M всё ещё выше медианы плацебо. «Отбор хуже случайного» — M ниже
5-го перцентиля плацебо. Иначе — «не отличим от случайного входа». Главное плацебо — Д. Если по Д
и R, и H «не отличим» — отбор пружин к лестнице и выходам ничего не добавляет; шаг 2 (части
пружины против контроля по цене) делать как объяснение и для упрощения балла, а не как поиск
эффекта. Если «добавляет» хотя бы в одной книге — шаг 2 ищет, какая часть зоны это даёт».
Механика в скрипте: (1) — реальное M выше M не меньше 96 розыгрышей из 100; «хуже случайного» —
симметрично, M ниже не меньше 96 из 100; (2)/(3) — среднее по 30 порядкам M цикла против медианы
M цикла по розыгрышам; (4) — среднее по 30 порядкам M без 5 лучших собственных позиций против
медианы M плацебо (целиком, как написано; справочно — и против медианы «без 5 лучших» плацебо).

Уточнение к спецификации: пул требует и hot == 0 (весь фильтр take, а не только share ≤ 0.75).
Для Д и П это ничего не меняет — дни реальных серий уже прошли take; для М иначе сдвинутый день
мог попасть в день перегрева, exec_book его отбросил бы и вход сместился бы на первый день после
перегрева (hot отсекает ~43% дней с share ≤ 0.75 — печатается). Для сравнения считается
справочное плацебо М0 — М буквально по спецификации (пул только share ≤ 0.75, дни перегрева
отбрасывает take в exec_book); в правило и итог оно не входит.

Оговорки: архив только Binance (выжившие, цифры — верхняя граница для обеих сторон); пул —
все пары архива длиной ≥ 231 закрытия (та же вселенная, что у детектора), без отсева сканера
(топ по капе, теги) — «случайная монета» шире, чем то, что сканер вообще смотрит; день и бенчмарк
— дневные закрытия; плацебо сохраняют структуру серий, но не одновременность сигналов разных
монет в один день (у Д и П она сохраняется по дням d1…dk, у М — нет); 100 розыгрышей дают
перцентиль с шагом 1% и шумом ~±2 п.п. у краёв.

Сверка s6: прод-порядок обязан совпасть с backtest/portfolio_vs_market_results.json (assert, как
bounce_gate_study — в квартальном перезапуске он пересчитан раньше); с замером 23.10.2026
(R +$1503 / −$186, H +$1903 / −$1083) — ✓/⚠ в выводе, assert с допуском $1 — ключ --strict.

Запуск из корня проекта:  PYTHONUTF8=1 py -3 backtest/random_entry_study.py [--draws N] [--strict]
Время прогона: ~7 мин (434 с 23.10.2026: архив и проход признаков ~185 с, сверка s6 и 30
порядков ~5 с, 100 розыгрышей × 4 плацебо × 2 книги ~245 с); сеть не нужна.
Результат: backtest/random_entry_results.json
"""
from __future__ import annotations

import argparse
import json
import random
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
from scanner.benchmark import flow_pnl  # noqa: E402

OUT = Path(__file__).resolve().parent / "random_entry_results.json"
DAY = 86400
CYC = [c for c, _a, _b in L.CYCLES]
OOS = "2023-26"
DRAWS = 100
SEED0 = 1000
SERIES_GAP = 7                   # дней: разрыв внутри серии сигналов одной монеты
DD = 0.70                        # dd70 для плацебо П
TAIL_N = 5
PASS_N = 96                      # (1): реальное M выше не меньше 96 из 100 розыгрышей
REF = {"R": {"prod": (1503, -186), "rand": (1440, -278)},
       "H": {"prod": (1903, -1083), "rand": (1864, -1176)}}
PLACEBOS = {"D": "Д: случайная монета дня", "P": "П: просадка ≥ 70%, не пружина",
            "M": "М: та же монета, сдвиг серии",
            "M0": "М0: сдвиг, пул без hot (спр.)"}
RULE_PLACEBOS = ("D", "P", "M")          # М0 — справочно: пул буквально share ≤ 0.75
SHORT = {"D": "Д", "P": "П", "M": "М", "M0": "М0"}
F_DD, F_SPRING = 1, 2            # биты кода пула: code = t * 4 + флаги
_MISS = object()


# ---------------------------------------------------------------- данные

def cycle_of(day: int) -> str:
    return next((c for c, a, b in L.CYCLES if a <= day < b), "?")


def load(t0: float) -> dict:
    """Архив, бенчмарки, сигналы прода (как L.signal_days(…{"prod": L.is_spring_prod}) — тот же
    проход и порядок) и пул покупаемых (сегмент, день) с флагами — за один проход признаков."""
    coins = L.load_archive()
    all_segs = L.segments(coins)
    ranks = L.vol_ranks(all_segs)
    npairs = P.pairs_per_day(ranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot = L.load_hot()
    alt, _btc_cap = P.market_index(ROOT / "scanner.db")
    btc = P.btc_closes(all_segs)
    take, _illiq = P.EXEC_POLICY
    print(f"архив: сегментов ≥ 231 закр. {len(segs)}, индекс альтов {len(alt)} дн., BTC {len(btc)} дн.  "
          f"[{time.time() - t0:.0f} с]")
    src: list[dict] = []
    pool: list[dict[int, int]] = []          # si -> {день: t*4 + флаги} — пул (весь take)
    pool_s: list[dict[int, int]] = []        # si -> {день: t} — только share ≤ 0.75 (для М0)
    day_pool: dict[int, list[int]] = {}      # день -> [si]
    n_feat = n_share = n_hot = 0
    for si, s in enumerate(segs):
        cl, qv, ts, sym = s["c"], s["qv"], s["ts"], s["sym"]
        codes: dict[int, int] = {}
        codes_s: dict[int, int] = {}
        for t in range(200, len(cl) - 30):
            f = L.features(cl, qv, t)
            if not f:
                continue
            spring = L.is_spring_prod(f, cl, t)
            day = ts[t]
            if spring:
                src.append(L._episode(s, t, ranks, hot))
            if cycle_of(day) == "?":
                continue
            n_feat += 1
            n = npairs.get(day) or 0
            share = ranks.get((sym, day), 9999) / n if n else 1.0
            if share > P.VOL_BOTTOM_SHARE:
                continue
            n_share += 1
            codes_s[day] = t
            if not take({"share": share, "hot": hot.get(day - DAY)}):
                continue
            n_hot += 1
            codes[day] = t * 4 + (F_DD if f["dd_ath"] >= DD else 0) + (F_SPRING if spring else 0)
            day_pool.setdefault(day, []).append(si)
        pool.append(codes)
        pool_s.append(codes_s)
    src = [ep for ep in src if ep["cycle"] != "?"]
    for ep in src:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
    pool_days = [sorted(c) for c in pool]
    pool_days_s = [sorted(c) for c in pool_s]
    print(f"дней сигнала прода {len(src)}; пул: (сегмент, день) с признаками и циклом {n_feat}, "
          f"share ≤ 0.75 {n_share}, и hot == 0 {n_hot} (hot отсёк {n_share - n_hot}); дней пула "
          f"{len(day_pool)}, монет в дне медиана {st.median(len(v) for v in day_pool.values()):.0f}  "
          f"[{time.time() - t0:.0f} с]")
    return {"segs": segs, "ranks": ranks, "npairs": npairs, "hot": hot, "alt": alt, "btc": btc,
            "src": src, "pool": pool, "pool_days": pool_days, "day_pool": day_pool,
            "pool_s": pool_s, "pool_days_s": pool_days_s,
            "seg_idx": {s["sym"]: i for i, s in enumerate(segs)},
            "pool_n": {"feat_cycle": n_feat, "share": n_share, "share_hot": n_hot}}


def make_ep(D: dict, si: int, day: int) -> dict:
    """Эпизод (сегмент, день) как в L.signal_days (L._episode) + share."""
    s = D["segs"][si]
    ep = L._episode(s, D["pool_s"][si][day], D["ranks"], D["hot"])
    n = D["npairs"].get(day) or 0
    ep["share"] = ep["rank"] / n if n else 1.0
    return ep


def build_series(D: dict) -> list[tuple[int, list[int]]]:
    """Серии реальных сигналов (после take): [(si, [d1…dk])] по возрастанию d1."""
    take, _ = P.EXEC_POLICY
    by: dict[str, set[int]] = {}
    for ep in D["src"]:
        if take(ep):
            by.setdefault(ep["seg"]["sym"], set()).add(ep["day"])
    out = []
    for sym, dd in by.items():
        si = D["seg_idx"][sym]
        cur: list[int] = []
        for d in sorted(dd):
            if cur and d - cur[-1] > SERIES_GAP * DAY:
                out.append((si, cur))
                cur = []
            cur.append(d)
        out.append((si, cur))
    out.sort(key=lambda x: (x[1][0], D["segs"][x[0]]["sym"]))
    for si, days in out:                      # реальные дни серии обязаны быть в пуле
        assert all(D["pool"][si].get(d, 0) & F_SPRING for d in days), "день сигнала не в пуле"
    return out


# ---------------------------------------------------------------- плацебо

def draw(D: dict, series, kind: str, rnd: random.Random, p_cache: dict) -> tuple[list[dict], dict]:
    """Кандидаты одного розыгрыша плацебо kind (D / P / M) -> (эпизоды, счётчики)."""
    pool, day_pool = D["pool"], D["day_pool"]
    cands: list[tuple[int, int]] = []
    skipped = used = 0
    for si, days in series:
        d1 = days[0]
        if kind in ("M", "M0"):
            pd_, pm_ = (D["pool_days"][si], pool[si]) if kind == "M" else (D["pool_days_s"][si], D["pool_s"][si])
            d1n = rnd.choice(pd_)
            for d in days:
                dd = d1n + (d - d1)
                if dd in pm_:
                    cands.append((si, dd))
            used += 1
            continue
        if kind == "D":
            lst = day_pool[d1]
            if len(lst) < 2:
                skipped += 1
                continue
            x = rnd.choice(lst)
            while x == si:
                x = rnd.choice(lst)
        else:
            lst = p_cache.get(d1)
            if lst is None:          # c сама — пружина в d1, поэтому в список не попадает
                lst = [x for x in day_pool[d1] if pool[x][d1] & F_DD and not pool[x][d1] & F_SPRING]
                p_cache[d1] = lst
            if not lst:
                skipped += 1
                continue
            x = rnd.choice(lst)
        used += 1
        for d in days:
            if d in pool[x]:
                cands.append((x, d))
    real = sum(1 for si, d in cands if pool[si].get(d, 0) & F_SPRING)
    hot_c = sum(1 for si, d in cands if d not in pool[si])      # только М0: отбросит take
    eps = [make_ep(D, si, d) for si, d in cands]
    return eps, {"cands": len(cands), "series_used": used, "skipped": skipped, "cands_real_sig": real,
                 "cands_hot": hot_c}


# ---------------------------------------------------------------- книга и метрики

def make_sim(sell: dict, cache: dict | None):
    """simulate_exec как в s6; cache — по (sym, e) (None — без кэша, для розыгрышей)."""
    def sim(ep):
        k = (ep["seg"]["sym"], ep["e"])
        r = cache.get(k, _MISS) if cache is not None else _MISS
        if r is _MISS:
            r = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET, horizon=None,
                                rules=L.EXEC_ALL)
            if cache is not None:
                cache[k] = r
        return r
    return sim


def rows_of(taken, alt: dict, btc: dict, bcache: dict | None = None) -> list[tuple]:
    """Строки позиций как P.evaluate_flows: (d0, d1, вложено, P&L, альты, BTC, цикл, дней);
    позиции без вложений или без уровня бенчмарка (в т.ч. «всё сразу») — пропускаются."""
    rows = []
    for ep, r in taken:
        spent = r["spent"]
        if spent <= 0:
            continue
        d0 = ep["day"]
        d1 = d0 + max(r["span"], 1) * DAY
        k = (ep["seg"]["sym"], ep["e"])
        b = bcache.get(k, _MISS) if bcache is not None else _MISS
        if b is _MISS:
            la, lb = P.bench_pnl(spent, alt, d0, d1), P.bench_pnl(spent, btc, d0, d1)
            ts = ep["seg"]["ts"]
            flows = [{"ts": ts[d], "side": sd, "usd": usd, "frac": fr} for d, sd, usd, fr in r["flows"]]
            fa = flow_pnl(flows, lambda t: P.at(alt, t), d1, P.FEE)
            fb = flow_pnl(flows, lambda t: P.at(btc, t), d1, P.FEE)
            b = None if None in (la, lb, fa, fb) else (fa, fb)
            if bcache is not None:
                bcache[k] = b
        if b is None:
            continue
        rows.append((d0, d1, spent, r["pnl"], b[0], b[1], ep["cycle"], max(r["span"], 1)))
    return rows


def _m(pnl: float, alt: float, spent: float) -> float | None:
    return (pnl - alt) / spent if spent > 0 else None


def metrics(rows: list[tuple]) -> dict:
    sp = sum(x[2] for x in rows)
    pn = sum(x[3] for x in rows)
    al = sum(x[4] for x in rows)
    bt = sum(x[5] for x in rows)
    period = (max(x[1] for x in rows) - min(x[0] for x in rows)) / DAY
    o = {"n": len(rows), "spent": sp, "pnl": pn, "alt": al, "btc": bt, "M": _m(pn, al, sp),
         "avg_cap": sum(x[2] * x[7] for x in rows) / period,
         "beat_alt": sum(1 for x in rows if x[3] > x[4]) / len(rows)}
    rest = sorted(rows, key=lambda x: x[3], reverse=True)[TAIL_N:]
    o["M_ex5"] = _m(sum(x[3] for x in rest), sum(x[4] for x in rest), sum(x[2] for x in rest))
    o["pnl_ex5"] = sum(x[3] for x in rest)
    o["cyc"] = {}
    for c in CYC:
        rr = [x for x in rows if x[6] == c]
        s_, p_, a_ = sum(x[2] for x in rr), sum(x[3] for x in rr), sum(x[4] for x in rr)
        o["cyc"][c] = {"n": len(rr), "spent": s_, "pnl": p_, "alt": a_, "M": _m(p_, a_, s_)}
    return o


def check_vs_evaluate(m: dict, ref: dict, what: str) -> None:
    """Моя обёртка обязана совпасть с P.evaluate_flows."""
    for k_m, k_r in (("n", "n"), ("spent", "spent"), ("pnl", "pnl"), ("alt", "alt"), ("btc", "btc"),
                     ("avg_cap", "avg_capital"), ("beat_alt", "beat_alt_share")):
        assert abs(m[k_m] - ref[k_r]) < 1e-6, f"{what}: {k_m} {m[k_m]} != evaluate_flows {ref[k_r]}"
    for c, v in ref["cycles"].items():
        assert abs(m["cyc"][c]["pnl"] - v["pnl"]) < 1e-6 and abs(m["cyc"][c]["alt"] - v["alt"]) < 1e-6, \
            f"{what}: цикл {c}"


def compact(m: dict, extra: dict | None = None) -> dict:
    r2 = lambda v: None if v is None else round(v, 2)      # noqa: E731
    r5 = lambda v: None if v is None else round(v, 5)      # noqa: E731
    o = {"n": m["n"], "spent": r2(m["spent"]), "pnl": r2(m["pnl"]), "alt": r2(m["alt"]),
         "btc": r2(m["btc"]), "M": r5(m["M"]), "M_ex5": r5(m["M_ex5"]), "pnl_ex5": r2(m["pnl_ex5"]),
         "avg_cap": r2(m["avg_cap"]), "beat_alt": r5(m["beat_alt"]),
         "cyc": {c: {"n": v["n"], "spent": r2(v["spent"]), "pnl": r2(v["pnl"]), "alt": r2(v["alt"]),
                     "M": r5(v["M"])} for c, v in m["cyc"].items()}}
    if extra:
        o.update(extra)
    return o


# ---------------------------------------------------------------- статистика и правило

def pctl(xs: list[float], q: float) -> float:
    """Перцентиль с линейной интерполяцией (как numpy «linear»)."""
    s = sorted(xs)
    pos = q * (len(s) - 1)
    i = int(pos)
    return s[i] if i + 1 >= len(s) else s[i] + (s[i + 1] - s[i]) * (pos - i)


def dist(xs: list[float]) -> dict:
    return {"med": st.median(xs), "p5": pctl(xs, 0.05), "p95": pctl(xs, 0.95),
            "mean": st.fmean(xs), "min": min(xs), "max": max(xs)}


def real_summary(runs: list[dict]) -> dict:
    """Среднее по 30 порядкам (M — среднее отношений по прогонам)."""
    o = {k: st.fmean(r[k] for r in runs) for k in ("n", "spent", "pnl", "alt", "btc", "M", "M_ex5",
                                                    "pnl_ex5", "avg_cap", "beat_alt")}
    o["ex_alt"] = st.fmean(r["pnl"] - r["alt"] for r in runs)
    o["cyc"] = {c: {"M": st.fmean(r["cyc"][c]["M"] for r in runs if r["cyc"][c]["M"] is not None),
                    "spent": st.fmean(r["cyc"][c]["spent"] for r in runs),
                    "pnl": st.fmean(r["cyc"][c]["pnl"] for r in runs),
                    "ex_alt": st.fmean(r["cyc"][c]["pnl"] - r["cyc"][c]["alt"] for r in runs)}
                for c in CYC}
    return o


def placebo_summary(runs: list[dict]) -> dict:
    o = {k: dist([r[k] for r in runs]) for k in ("n", "spent", "pnl", "M", "M_ex5", "avg_cap",
                                                  "beat_alt")}
    o["ex_alt"] = dist([r["pnl"] - r["alt"] for r in runs])
    o["cyc"] = {c: {"M": dist([r["cyc"][c]["M"] for r in runs if r["cyc"][c]["M"] is not None]),
                    "spent": dist([r["cyc"][c]["spent"] for r in runs]),
                    "ex_alt": dist([r["cyc"][c]["pnl"] - r["cyc"][c]["alt"] for r in runs])}
                for c in CYC}
    return o


def verdict(real: dict, plc_runs: list[dict]) -> dict:
    """Правило Q18 (1)–(4) механически: реальное (среднее 30 порядков) против 100 розыгрышей."""
    pm = [r["M"] for r in plc_runs]
    med = st.median(pm)
    below = sum(1 for v in pm if v < real["M"])
    above = sum(1 for v in pm if v > real["M"])
    cyc_med = {c: st.median([r["cyc"][c]["M"] for r in plc_runs if r["cyc"][c]["M"] is not None])
               for c in CYC}
    cyc_win = {c: real["cyc"][c]["M"] > cyc_med[c] for c in CYC}
    n2 = sum(cyc_win.values())
    ex5_med = st.median(r["M_ex5"] for r in plc_runs)
    o = {"M_real": real["M"], "M_med": med, "M_p5": pctl(pm, 0.05), "M_p95": pctl(pm, 0.95),
         "pctile": 100 * below / len(pm), "below": below, "above": above, "draws": len(pm),
         "c1": below >= PASS_N * len(pm) / DRAWS,
         "cyc_real": {c: real["cyc"][c]["M"] for c in CYC}, "cyc_med": cyc_med, "cyc_win": cyc_win,
         "c2": n2 >= 2, "c2_n": n2, "c3": cyc_win[OOS],
         "M_ex5_real": real["M_ex5"], "c4": real["M_ex5"] > med,
         "ref_M_ex5_med": ex5_med, "ref_c4_vs_ex5": real["M_ex5"] > ex5_med}
    o["worse"] = above >= PASS_N * len(pm) / DRAWS
    o["verdict"] = ("добавляет" if o["c1"] and o["c2"] and o["c3"] and o["c4"] else
                    "хуже случайного" if o["worse"] else "не отличим")
    xs = [r["pnl"] - r["alt"] for r in plc_runs]
    o["usd"] = {"ex_alt_real": real["ex_alt"], "ex_alt": dist(xs),
                "ex_alt_pctile": 100 * sum(1 for v in xs if v < real["ex_alt"]) / len(xs),
                "pnl_real": real["pnl"], "pnl": dist([r["pnl"] for r in plc_runs]),
                "pnl_pctile": 100 * sum(1 for r in plc_runs if r["pnl"] < real["pnl"]) / len(xs)}
    return o


# ---------------------------------------------------------------- печать

def pc(x: float | None, w: int = 0) -> str:
    return (f"{x * 100:+.1f}%" if x is not None else "—").rjust(w)


def yn(b: bool) -> str:
    return "да" if b else "нет"


def series_stats(series) -> dict:
    lens = sorted(len(d) for _si, d in series)
    spans = sorted((d[-1] - d[0]) // DAY + 1 for _si, d in series)
    buckets = [("1", 1, 1), ("2–3", 2, 3), ("4–7", 4, 7), ("8–15", 8, 15), ("16–30", 16, 30),
               ("31–60", 31, 60), ("> 60", 61, 10**9)]
    o = {"n": len(series), "days": sum(lens), "coins": len({si for si, _d in series}),
         "len_mean": st.fmean(lens), "len_med": st.median(lens), "len_p90": pctl(lens, 0.9),
         "len_max": lens[-1], "span_med": st.median(spans), "span_p90": pctl(spans, 0.9),
         "len_buckets": {nm: sum(1 for x in lens if a <= x <= b) for nm, a, b in buckets},
         "by_cycle": {c: sum(1 for si, d in series if cycle_of(d[0]) == c) for c in CYC}}
    return o


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=DRAWS, help="розыгрышей на плацебо (правило — 100)")
    ap.add_argument("--strict", action="store_true",
                    help="assert s6 = замер 23.10.2026 (R +1503/−186, H +1903/−1083) с допуском $1")
    args = ap.parse_args()
    nd = args.draws
    t0 = time.time()
    D = load(t0)
    alt, btc, src = D["alt"], D["btc"], D["src"]
    series = build_series(D)
    ss = series_stats(series)
    print(f"\nСерии реальных сигналов (после take, разрыв ≤ {SERIES_GAP} дн.): {ss['n']} серий, "
          f"{ss['days']} дней сигнала, монет {ss['coins']}; длина (дней сигнала) ср. {ss['len_mean']:.1f}, "
          f"медиана {ss['len_med']:.0f}, q90 {ss['len_p90']:.0f}, макс. {ss['len_max']}; от d1 до dk "
          f"медиана {ss['span_med']:.0f} дн., q90 {ss['span_p90']:.0f}")
    print("  длины: " + ", ".join(f"{k}: {v}" for k, v in ss["len_buckets"].items())
          + "; по циклам d1: " + ", ".join(f"{k} {v}" for k, v in ss["by_cycle"].items()))

    out: dict = {"meta": {"draws": nd, "seed0": SEED0, "orders": J.SEEDS, "series_gap_days": SERIES_GAP,
                          "dd": DD, "tail_n": TAIL_N, "pass_n": PASS_N, "slots": J.SLOTS,
                          "budget": J.BUDGET, "capital": P.CAPITAL, "cooldown_days": P.COOLDOWN_DAYS,
                          "signal_days": len(src), "pool": D["pool_n"],
                          "pool_rule": "share ≤ 0.75 и hot == 0 (весь take EXEC_POLICY), цикл не ?",
                          "rule": "docs/DECISIONS.md Q18 (записано 23.10.2026 до прогона)"},
                 "series": ss, "books": {}}
    s6_ref = {b: v["s6"] for b, v in json.loads((Path(__file__).resolve().parent /
                                                 "portfolio_vs_market_results.json")
                                                .read_text(encoding="utf-8"))["exec"].items()
              if b in J.BOOKS}
    sims = {b: make_sim(sell, {}) for b, (sell, _s) in J.BOOKS.items()}
    sims_nc = {b: make_sim(sell, None) for b, (sell, _s) in J.BOOKS.items()}
    bcache = {b: {} for b in J.BOOKS}

    # ---- 1. сверка s6 и реальная книга (30 порядков)
    real: dict = {}
    for b in J.BOOKS:
        tk = P.exec_book(src, sims[b], *P.EXEC_POLICY, cooldown=True, cash=True)
        ref = P.evaluate_flows(tk, alt, btc)
        m = metrics(rows_of(tk, alt, btc, bcache[b]))
        check_vs_evaluate(m, ref, f"{b} прод")
        s6 = s6_ref[b]                       # как bounce_gate_study: эталон — results s6
        assert m["n"] == s6["n"] and abs(m["pnl"] - s6["pnl"]) < 1e-6 and abs(m["alt"] - s6["alt"]) < 1e-6, \
            f"{b}: s6 не повторился ({m['pnl']:+.2f} / {s6['pnl']:+.2f})"
        ep_, ea_ = REF[b]["prod"]            # замер 23.10.2026; на свежих данных (квартал) сдвинется
        same = abs(m["pnl"] - ep_) < 1 and abs(m["pnl"] - m["alt"] - ea_) < 1
        if args.strict:
            assert same, f"{b}: s6 разошёлся с замером 23.10: P&L {m['pnl']:+.1f} / −альты " \
                         f"{m['pnl'] - m['alt']:+.1f}"
        runs = []
        for sd in range(J.SEEDS):
            tk = P.exec_book(src, sims[b], *P.EXEC_POLICY, cooldown=True, cash=True,
                             order="random", seed=sd)
            mm = metrics(rows_of(tk, alt, btc, bcache[b]))
            check_vs_evaluate(mm, P.evaluate_flows(tk, alt, btc), f"{b} seed {sd}")
            runs.append(mm)
        rs = real_summary(runs)
        rp, ra = REF[b]["rand"]
        warn = "" if abs(rs["pnl"] - rp) <= 2 and abs(rs["ex_alt"] - ra) <= 2 else "  ⚠ расходится > $2"
        print(f"\nКНИГА {b}: сверка s6 прод-порядок P&L {m['pnl']:+.1f}$ / −альты {m['pnl'] - m['alt']:+.1f}$ "
              f"= portfolio_vs_market_results ✓, замер 23.10 {ep_:+d} / {ea_:+d} {'✓' if same else '⚠ другой'}; "
              f"{J.SEEDS} порядков: P&L ср. {rs['pnl']:+.1f}$ / −альты "
              f"{rs['ex_alt']:+.1f}$ (эталон ≈ {rp:+d} / {ra:+d}){warn}; M прод {pc(m['M'])}, ср. "
              f"порядков {pc(rs['M'])}  [{time.time() - t0:.0f} с]")
        real[b] = {"prod": m, "runs": runs, "sum": rs}
        out["books"][b] = {"check_s6": {"prod": compact(m), "rand_pnl": rs["pnl"], "rand_ex_alt": rs["ex_alt"],
                                        "ref": REF[b]},
                           "real": {"summary": rs, "runs": [compact(r) for r in runs]}, "placebo": {}}

    # ---- 2. плацебо
    p_cache: dict = {}
    tp = time.time()
    for kind, label in PLACEBOS.items():
        tk0 = time.time()
        res = {b: [] for b in J.BOOKS}
        cnt = []
        for i in range(nd):
            rnd = random.Random(SEED0 + i)
            eps, c = draw(D, series, kind, rnd, p_cache)
            cnt.append(c)
            for b in J.BOOKS:
                tk = P.exec_book(eps, sims_nc[b], *P.EXEC_POLICY, cooldown=True, cash=True,
                                 order="random", seed=SEED0 + i)
                rows = rows_of(tk, alt, btc)
                mm = metrics(rows)
                mm["pos_real_sig"] = sum(1 for ep, _r in tk if D["pool"][D["seg_idx"][ep["seg"]["sym"]]]
                                         .get(ep["day"], 0) & F_SPRING)
                res[b].append(mm)
            if i == 0 or (i + 1) % 10 == 0:
                el = time.time() - tk0
                print(f"  {label}: розыгрыш {i + 1}/{nd}, кандидатов {c['cands']}, серий {c['series_used']} "
                      f"(пропущено {c['skipped']}); R поз. {res['R'][-1]['n']}, H поз. {res['H'][-1]['n']}  "
                      f"[{el:.0f} с, ~{el / (i + 1) * (nd - i - 1):.0f} с до конца плацебо]", flush=True)
        cs = {k: st.fmean(x[k] for x in cnt) for k in cnt[0]}
        cs["skipped_max"] = max(x["skipped"] for x in cnt)
        for b in J.BOOKS:
            out["books"][b]["placebo"][kind] = {
                "label": label, "counts": cs, "summary": placebo_summary(res[b]),
                "verdict": verdict(real[b]["sum"], res[b]),
                "pos_real_sig_mean": st.fmean(r["pos_real_sig"] for r in res[b]),
                "draws": [compact(r, {"pos_real_sig": r["pos_real_sig"], **cnt[i]})
                          for i, r in enumerate(res[b])]}
        print(f"  {label}: {nd} розыгрышей за {time.time() - tk0:.0f} с; кандидатов ср. {cs['cands']:.0f}, "
              f"серий ср. {cs['series_used']:.0f}, пропущено ср. {cs['skipped']:.1f} (макс. {cs['skipped_max']}), "
              f"кандидатов — реальных сигналов ср. {cs['cands_real_sig']:.0f}, в дни перегрева (отбросит take) "
              f"{cs['cands_hot']:.0f}", flush=True)
    t_plc = time.time() - tp

    # ---- 3. таблицы и вердикты
    n_real_cands = sum(len(d) for _si, d in series)
    for b in J.BOOKS:
        rb, ob = real[b]["sum"], out["books"][b]
        print(f"\n{'=' * 118}\nКНИГА {b} ({'R правила' if b == 'R' else 'H держать'}): реальная — среднее "
              f"{J.SEEDS} порядков; плацебо — медиана [5…95%] по {nd} розыгрышам; кандидатов у реальной "
              f"{n_real_cands} (дней серий)")
        print(f"  {'':26} {'M = (P&L−альты)/вложено':>28} {'P&L−альты $':>22} {'P&L $':>20} "
              f"{'поз.':>5} {'вложено':>8} {'работало':>8} {'лучше альт':>10} {'M без 5':>8}")
        print(f"  {'реальная (отбор пружин)':26} {pc(rb['M']):>28} {rb['ex_alt']:>+21.0f}$ {rb['pnl']:>+19.0f}$ "
              f"{rb['n']:>5.0f} {rb['spent']:>7.0f}$ {rb['avg_cap']:>7.0f}$ {rb['beat_alt'] * 100:>9.0f}% "
              f"{pc(rb['M_ex5']):>8}")
        for kind, pl in ob["placebo"].items():
            s = pl["summary"]
            mtxt = f"{pc(s['M']['med'])} [{pc(s['M']['p5'])}…{pc(s['M']['p95'])}]"
            etxt = f"{s['ex_alt']['med']:+.0f} [{s['ex_alt']['p5']:+.0f}…{s['ex_alt']['p95']:+.0f}]"
            ptxt = f"{s['pnl']['med']:+.0f} [{s['pnl']['p5']:+.0f}…{s['pnl']['p95']:+.0f}]"
            print(f"  {pl['label']:26} {mtxt:>28} {etxt:>22} {ptxt:>20} {s['n']['med']:>5.0f} "
                  f"{s['spent']['med']:>7.0f}$ {s['avg_cap']['med']:>7.0f}$ {s['beat_alt']['med'] * 100:>9.0f}% "
                  f"{pc(s['M_ex5']['med']):>8}")
        print("  по циклам, M: реальная / медиана плацебо " + " · ".join(SHORT[k] for k in ob["placebo"]))
        for c in CYC:
            print(f"    {c:8} {pc(rb['cyc'][c]['M'], 7)} / "
                  + " · ".join(pc(ob['placebo'][k]['verdict']['cyc_med'][c]) for k in ob["placebo"])
                  + f"   (вложено реальная {rb['cyc'][c]['spent']:.0f}$; плацебо мед. "
                  + " · ".join(f"{ob['placebo'][k]['summary']['cyc'][c]['spent']['med']:.0f}$" for k in ob["placebo"])
                  + ")")
        print(f"  правило Q18: (1) M выше ≥ {PASS_N} из {nd}; (2) выше медианы в ≥ 2 из 3 циклов; (3) в {OOS}; "
              f"(4) без {TAIL_N} лучших выше медианы")
        for kind, pl in ob["placebo"].items():
            v = pl["verdict"]
            print(f"    {pl['label']:30} перцентиль реального {v['pctile']:.0f} (выше {v['below']}, ниже {v['above']} "
                  f"из {v['draws']}); (1) {yn(v['c1'])}; (2) {yn(v['c2'])} ({v['c2_n']}/3); (3) {yn(v['c3'])}; "
                  f"(4) {yn(v['c4'])} ({pc(v['M_ex5_real'])} против {pc(v['M_med'])}; справочно к медиане «без 5» "
                  f"плацебо {pc(v['ref_M_ex5_med'])} — {yn(v['ref_c4_vs_ex5'])}) → «{v['verdict']}»")
            u = v["usd"]
            print(f"      справочно $: P&L−альты {u['ex_alt_real']:+.0f}$ против медианы {u['ex_alt']['med']:+.0f}$ "
                  f"[{u['ex_alt']['p5']:+.0f}…{u['ex_alt']['p95']:+.0f}] (перцентиль {u['ex_alt_pctile']:.0f}); P&L "
                  f"{u['pnl_real']:+.0f}$ против {u['pnl']['med']:+.0f}$ [{u['pnl']['p5']:+.0f}…{u['pnl']['p95']:+.0f}] "
                  f"(перцентиль {u['pnl_pctile']:.0f}); позиций-реальных сигналов в плацебо ср. "
                  f"{pl['pos_real_sig_mean']:.1f}")
        print(f"  ({SHORT['M0']} — справочно, в правило не входит: пул буквально share ≤ 0.75 без hot)")
    vd = {b: out["books"][b]["placebo"]["D"]["verdict"]["verdict"] for b in J.BOOKS}
    if all(v == "не отличим" for v in vd.values()):
        concl = ("по главному плацебо Д и R, и H «не отличим» — отбор пружин к лестнице и выходам ничего не "
                 "добавляет; шаг 2 — как объяснение и для упрощения балла, а не поиск эффекта")
    elif any(v == "добавляет" for v in vd.values()):
        concl = ("по Д «добавляет» хотя бы в одной книге (" + ", ".join(f"{b} — {v}" for b, v in vd.items())
                 + ") — шаг 2 ищет, какая часть зоны это даёт")
    else:
        concl = "по Д: " + ", ".join(f"{b} — {v}" for b, v in vd.items()) + " (ни одна книга не «добавляет»)"
    out["conclusion"] = {"main_placebo_D": vd, "text": concl,
                         "table": {b: {k: out["books"][b]["placebo"][k]["verdict"]["verdict"]
                                       for k in RULE_PLACEBOS} for b in J.BOOKS},
                         "reference_M0": {b: out["books"][b]["placebo"]["M0"]["verdict"]["verdict"]
                                          for b in J.BOOKS}}
    total = time.time() - t0
    out["meta"]["seconds"] = {"total": round(total), "placebo": round(t_plc)}
    print(f"\nИТОГ Q18: {concl}")
    print("  вердикты: " + "; ".join(f"{b}: " + ", ".join(f"{SHORT[k]} {v}" for k, v in t.items())
                                     for b, t in out["conclusion"]["table"].items())
          + "; справочно М0: " + ", ".join(f"{b} {v}" for b, v in out["conclusion"]["reference_M0"].items()))
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nВремя: {total:.0f} с (плацебо {t_plc:.0f} с)\nsaved -> {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
