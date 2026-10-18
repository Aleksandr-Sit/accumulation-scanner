"""Гейт «не догонять»: рост от лоу базы как порог, порядок и квота среди пружин дня (Q17).

Повод — Q13 (momentum_order_study, 16.10.2026): внутри дня у признака bounce (рост от лоу
базы) устойчиво отрицательный IC — H −0.30 (t −27; 2018–20 −0.32, 2020–22 −0.21, 2023–26
−0.32, положительный лишь в 20% дней), R −0.07 (2018–20 −0.10, 2020–22 0.00, 2023–26 −0.08):
уже отскочившие от лоу монеты дальше хуже. Но в книге эффекта почти нет: «больше отскочившие
первыми» R −$382 к альтам (случайный порядок −$278), H −$1102 (−$1176); гейты «отскок ≥
10/20/30%» (догонять) режут P&L. Обратное — «не догонять» — не проверялось: гейт «отскок ≤ X»,
порядок «меньше отскочившие первыми», квота на отскочившие. И отдельно — почему сильный IC
эпизодов не доходит до книги.

Модель — книги R и H, шаг s6 (portfolio_vs_market.exec_book: 15 мест, лестница $50, кулдаун
90 дн., USDT $1500, нижняя четверть по обороту, перегрев, квота неликвида 5 мест). Сверки:
база повторяет s6 (R +$1503 / −$186 к альтам, H +$1903), гейт «X = ∞» = база (прод и все
30 порядков), IC bounce повторяет Q13. Книги независимы.

Признак — momentum_order_study.features (импорт): bounce = c[e] / лоу базы − 1, лоу базы —
минимум 30 закрытий e−29…e (как simulate_exec, правило base30), только закрытия Binance по
день сигнала включительно. Неизвестный (лоу ≤ 0) гейт пропускает, порядок ставит в конец,
квота не считает; на сигналах 2018+ не встречается.

Варианты (каждый отдельно; гейт — до книги, как Q9/Q13: место и деньги отсеянных достаются
следующим сигналам):
  1. ГЕЙТ «не догонять»: bounce ≤ 5 / 10 / 15 / 20 / 30%. По квантилям (выбор — по
     распределению bounce у сигналов, см. вывод скрипта: сетка 5–30% покрывает q35…q92):
     ≤ q95 сигналов всей истории (≈ 35%, срезать только крайний хвост) и ≤ q80 сигналов
     прошлых 365 дн. (помесячно, без заглядывания; меньше 100 сигналов в окне — без гейта):
     доля bounce > 20% по годам 11…35%, фиксированный порог в разных рынках режет по-разному.
     С порядком по обороту (прод) и с J.SEEDS случайными порядками.
  2. ПОРЯДОК: bounce по возрастанию (momentum_order_study.weak_first); «сначала bounce ≤ 15%,
     внутри по обороту»; то же «внутри случайно».
  3. КВОТА: сигналов с bounce > 20% не больше 3 / 5 / 8 мест из 15 (exec_book quota2).

Разбор расхождения «сильный IC эпизодов, слабый эффект книги» (обе книги):
  a) решает ли порядок: книга по дням (прод и 30 порядков) — допускаемые сигналы (фильтры
     прода, монеты нет в книге, нет кулдауна, план не отказан): книга полна / есть место —
     взяты все / ни один (деньги, квота неликвида) / порядок решает (взят ≥ 1, но не все);
     IC на днях выбора; «статический» эффект на днях выбора: k наименее отскочивших вместо
     взятых и среднее допускаемых (случайный выбор), без пути книги; доля позиций базы, которые
     есть не во всех 31 прогоне (по входу и по серии сигналов);
  b) IC внутри дня (дни с ≥ 3 сигналами) на всех сигналах: исход в % (P&L / вложено, как Q13)
     и в $ (P&L позиции = P&L на место $50), взвешенный по вложенным $; на взятых позициях
     базы (по всем и внутри месяца); на днях выбора;
  c) хвост: 5 лучших позиций базы (прод-порядок) — доля P&L; IC и варианты без сигналов этих
     монет от начала серии до выхода позиции (книги заново, прод и 30 порядков, правило 1–4);
     у каждого варианта — P&L без 5 лучших собственных позиций;
  d) лестница по корзинам bounce: исполнено ступеней, вложено $, средняя цена входа и первая
     ступень к лоу базы, длительность, стопы, P&L в % и в $, P&L на место-год;
  e) корреляция сигналов: серия — сигналы сегмента в 90 дн. от первого (как кулдаун); IC на
     первых сигналах серий (внутри дня и внутри месяца) против всех; t по месячным блокам и
     Ньюи–Уэст (лаг 30 дней с IC) против наивного t по дням; IC внутри серии одной монеты
     (та же монета в разные дни серии — тайминг входа, а не выбор между монетами).
  Плюс к каждому варианту: позиции против прода по сериям (тот же вход / та же серия, другой
  день / серия только у прода / только у варианта) и разница с базой по году входа.

Метрики как Q9/Q11/Q13: P&L, P&L − альты / корзина топ-20 по капе / BTC по денежным потокам,
вложено, P&L на $ и (P&L − альты) на $, в скольких из 30 порядков лучше базы того же порядка
(P&L, −альты, −альты на $), позиций, умерших за 2 года, сколько из лучших 10% исходов базы
пропало (прод-порядок), по циклам 2018–20 / 2020–22 / 2023–26 (по дню входа). Средние по
порядкам — среднее по прогонам (на $ — среднее отношений). У порядков с непрерывным ключом
30 прогонов почти совпадают — «лучше базы в K из 30» есть место среди случайных порядков;
у детерминированного «≤ 15% первыми, внутри по обороту» сравнение со случайной базой включает
и выигрыш порядка по обороту как такового — разница с продом (одна цифра) в таблице B.
Вне выборки: в каждом семействе (гейт / порядок / квота) по 2018–22 выбран вариант с
наибольшим (P&L − альты) на $ среди тех, у кого P&L 2018–22 не ниже базы (нет таких —
наибольший, с пометкой), и он же в 2023–26.

Правило решения (записано до прогона, не подгоняется): «вводить» только если вариант
одновременно (1) P&L (среднее 30 порядков) не ниже базы; (2) (P&L − альты) на $ (среднее
30 порядков) выше базы; (3) (P&L − альты) на $ выше базы в ≥ 2 из 3 циклов (средние по
порядкам); (4) в 2023–26 (P&L − альты) на $ выше базы того же порядка в ≥ 20 из 30. Иначе —
«не вводить». Прошедший все четыре — кандидат в теневую книгу (наблюдение без изменения
прода), а не сразу в правило.

Оговорки: архив только Binance (выжившие ×0.73 по прошлым замерам — цифры верхняя граница);
пружина — детектор прода на закрытиях Binance, а не балл ≥ 70; сигналы монеты в соседние дни
почти одинаковы (см. e); пороги, правила книг и выбор вариантов — на той же истории, вне
выборки — один цикл 2023–26; q95 всей истории — лёгкое заглядывание (порог по всей выборке);
исход сигнала для IC — позиция, как если бы её взяли в тот день (без пути книги).

Запуск из корня проекта:  PYTHONIOENCODING=utf-8 python backtest/bounce_gate_study.py
Время прогона: ~8 мин (470 с 18.10.2026, из них ~185 с — архив и сигналы; 15 вариантов ×
  31 прогон × 2 книги + 12 вариантов без хвоста × 31 × 2 + разбор); сеть не нужна.
Результат: backtest/bounce_gate_results.json
"""
from __future__ import annotations

import bisect
import datetime as dt
import inspect
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
import momentum_order_study as M  # noqa: E402
import portfolio_vs_market as P  # noqa: E402
import supply_age_gate_study as Q  # noqa: E402
import top20_basket_study as T  # noqa: E402
from scanner.ladder import round_step  # noqa: E402

OUT = Path(__file__).resolve().parent / "bounce_gate_results.json"
REF_Q13 = Path(__file__).resolve().parent / "momentum_order_results.json"
DAY = 86400
IS_CYCLES, OOS_CYCLE, BENCH = G.IS_CYCLES, G.OOS_CYCLE, G.BENCH
CYC = [c for c, _a, _b in L.CYCLES]
INF = math.inf
EPS = 1e-12
GATES = (0.05, 0.10, 0.15, 0.20, 0.30)
Q_TAIL = 0.95                    # порог по квантилю всей истории: только крайний хвост
Q_ROLL, ROLL_DAYS, ROLL_MIN = 0.80, 365, 100
FLAG = 0.15                      # порядок «сначала bounce ≤ 15%»
QUOTA_THR, QUOTAS = 0.20, (3, 5, 8)
TAIL_N = 5
SERIES_DAYS = P.COOLDOWN_DAYS    # серия сигналов сегмента — 90 дн. от первого
NW_LAG = 30
OOS_MIN_BEAT = 20
STEPS = inspect.signature(L.simulate_exec).parameters["steps"].default
BUCKETS = [("≤ 2%", -INF, 0.02), ("2–5%", 0.02, 0.05), ("5–10%", 0.05, 0.10),
           ("10–15%", 0.10, 0.15), ("15–20%", 0.15, 0.20), ("20–30%", 0.20, 0.30),
           ("> 30%", 0.30, INF)]
FAMILIES = {"gate": "гейт «не догонять»", "order": "порядок", "quota": "квота > 20%"}
TAIL_VARIANTS = ("base", "g_le5", "g_le10", "g_le15", "g_le20", "g_le30", "g_roll80", "o_asc",
                 "o_le15_rank", "o_le15_rnd", "q_gt20_3", "q_gt20_5")


# ---------------------------------------------------------------- признак, гейты, порядки

def bounce(ep: dict) -> float | None:
    return ep["f"]["bounce"]


def coin_of(ep: dict) -> str:
    return ep["seg"]["sym"].split("#")[0]


def date_of(day: int) -> str:
    return dt.datetime.fromtimestamp(day, dt.timezone.utc).date().isoformat()


def month_start(day: int) -> int:
    d = dt.datetime.fromtimestamp(day, dt.timezone.utc)
    return int(dt.datetime(d.year, d.month, 1, tzinfo=dt.timezone.utc).timestamp())


def le(thr: float):
    """Гейт «не догонять»: bounce ≤ thr; неизвестный — пропускает."""
    return lambda ep: bounce(ep) is None or bounce(ep) <= thr + EPS


def le_roll(thr_by_month: dict[int, float | None]):
    """Гейт по скользящему квантилю месяца входа; порога нет (мало истории) — пропускает."""
    def g(ep):
        b, t = bounce(ep), thr_by_month.get(month_start(ep["day"]))
        return b is None or t is None or b <= t + EPS
    return g


def chased(ep: dict) -> bool:
    """В квоте «уже отскочившие»: bounce > 20% (дополнение гейта ≤ 20%)."""
    return bounce(ep) is not None and bounce(ep) > QUOTA_THR + EPS


def flag_first(by_rank: bool):
    """Порядок: bounce ≤ 15% первыми; внутри групп — по обороту или случайно."""
    def key(ep):
        b = bounce(ep)
        g = 0 if b is not None and b <= FLAG + EPS else 1
        return (g, ep["rank"]) if by_rank else g
    return key


def quantile(xs: list[float], q: float) -> float:
    """Как Q13: элемент отсортированного списка с индексом int(q·n)."""
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def rolling_thresholds(sig: list[dict]) -> dict[int, float | None]:
    """Месяц -> q80 bounce сигналов за ROLL_DAYS дн. до начала месяца (None — мало истории)."""
    pts = sorted((ep["day"], bounce(ep)) for ep in sig if bounce(ep) is not None)
    ds = [d for d, _b in pts]
    out: dict[int, float | None] = {}
    for m0 in sorted({month_start(ep["day"]) for ep in sig}):
        i0, i1 = bisect.bisect_left(ds, m0 - ROLL_DAYS * DAY), bisect.bisect_left(ds, m0)
        xs = sorted(b for _d, b in pts[i0:i1])
        out[m0] = quantile(xs, Q_ROLL) if len(xs) >= ROLL_MIN else None
    return out


def series_of(sig: list[dict]) -> dict[tuple[str, int], tuple[str, int]]:
    """G.key_of(ep) -> (сегмент, день начала серии): серия — сигналы сегмента в SERIES_DAYS дн.
    от первого (как кулдаун исполнителя от входа); следующий сигнал позже — новая серия."""
    by_seg: dict[str, list[int]] = {}
    for ep in sig:
        by_seg.setdefault(ep["seg"]["sym"], []).append(ep["day"])
    out = {}
    for sym, dd in by_seg.items():
        start = None
        for d in sorted(set(dd)):
            if start is None or d >= start + SERIES_DAYS * DAY:
                start = d
            out[(sym, d)] = (sym, start)
    return out


def variants(thr95: float, roll: dict) -> list[dict]:
    vs = [{"key": "base", "label": "база (прод, по обороту)", "fam": None, "kind": "base"},
          {"key": "g_inf", "label": "сверка: гейт X = ∞", "fam": None, "kind": "gate", "g": le(INF)}]
    for x in GATES:
        vs.append({"key": f"g_le{round(x * 100)}", "label": f"гейт: от лоу ≤ {x:.0%}", "fam": "gate",
                   "kind": "gate", "g": le(x)})
    vs.append({"key": "g_q95", "label": f"гейт: ≤ q95 сигналов ({thr95:.1%})", "fam": "gate",
               "kind": "gate", "g": le(thr95)})
    vs.append({"key": "g_roll80", "label": "гейт: ≤ q80 прошлых 365 дн.", "fam": "gate",
               "kind": "gate", "g": le_roll(roll)})
    vs.append({"key": "o_asc", "label": "порядок: меньше отскочившие первыми", "fam": "order",
               "kind": "order", "order": M.weak_first("bounce")})
    vs.append({"key": "o_le15_rank", "label": "порядок: ≤ 15% первыми, внутри обор.", "fam": "order",
               "kind": "order", "order": flag_first(True)})
    vs.append({"key": "o_le15_rnd", "label": "порядок: ≤ 15% первыми, внутри случ.", "fam": "order",
               "kind": "order", "order": flag_first(False)})
    for n in QUOTAS:
        vs.append({"key": f"q_gt20_{n}", "label": f"квота: от лоу > 20% ≤ {n} мест", "fam": "quota",
                   "kind": "quota", "q2": (chased, n)})
    return vs


# ---------------------------------------------------------------- статистика

def tstat(vals: list[float]) -> float | None:
    if len(vals) < 2:
        return None
    sd = st.stdev(vals)
    return st.fmean(vals) / sd * math.sqrt(len(vals)) if sd else None


def nw_t(vals: list[float], lag: int) -> float | None:
    """t среднего с поправкой Ньюи–Уэста (веса Бартлетта) на автокорреляцию ряда."""
    n = len(vals)
    if n < lag + 2:
        return None
    m = st.fmean(vals)
    d = [v - m for v in vals]
    lrv = sum(x * x for x in d) / n
    for k in range(1, lag + 1):
        lrv += 2 * (1 - k / (lag + 1)) * sum(d[i] * d[i - k] for i in range(k, n)) / n
    return m / math.sqrt(lrv / n) if lrv > 0 else None


def ar1(vals: list[float]) -> float | None:
    return st.correlation(vals[:-1], vals[1:]) if len(vals) > 3 else None


def wspearman(xs: list[float], ys: list[float], ws: list[float]) -> float | None:
    """Спирмен со взвешенной корреляцией рангов (веса — вложенные $)."""
    a, b = G.ranked(xs), G.ranked(ys)
    sw = sum(ws)
    if sw <= 0:
        return None
    ma, mb = sum(w * x for w, x in zip(ws, a)) / sw, sum(w * x for w, x in zip(ws, b)) / sw
    cov = sum(w * (x - ma) * (y - mb) for w, x, y in zip(ws, a, b))
    va = sum(w * (x - ma) ** 2 for w, x in zip(ws, a))
    vb = sum(w * (y - mb) ** 2 for w, y in zip(ws, b))
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else None


def by_day(ep: dict) -> int:
    return ep["day"]


def by_month(ep: dict) -> int:
    return month_start(ep["day"])


def group_ics(sig: list[dict], y: dict, group, w: dict | None = None,
              min_n: int = M.MIN_DAY_SIGNALS) -> list[tuple[int, str, float]]:
    """IC (Спирмен bounce к исходу) внутри групп group(ep) (день / месяц / серия монеты) с
    ≥ min_n сигналами, как M.ic_block: [(первый день группы, цикл, IC)] по возрастанию дня."""
    by: dict = {}
    for ep in sig:
        if G.key_of(ep) in y and bounce(ep) is not None:
            by.setdefault(group(ep), []).append(ep)
    out = []
    for ee in by.values():
        if len(ee) < min_n:
            continue
        xs = [bounce(ep) for ep in ee]
        ys = [y[G.key_of(ep)] for ep in ee]
        if len(set(xs)) < 2 or len(set(ys)) < 2:
            continue
        ic = G.spearman(xs, ys) if w is None else wspearman(xs, ys, [w[G.key_of(ep)] for ep in ee])
        if ic is not None:
            out.append((min(ep["day"] for ep in ee), ee[0]["cycle"], ic))
    out.sort(key=lambda t: t[0])
    return out


def ic_summary(rows: list[tuple[int, str, float]], blocks: bool = True) -> dict:
    vals = [v for _g, _c, v in rows]
    o: dict = {"n": len(vals), "ic": st.fmean(vals) if vals else None, "t": tstat(vals),
               "pos_share": sum(1 for v in vals if v > 0) / len(vals) if vals else None,
               "cycles": {c: {"n": sum(1 for _g, cc, _v in rows if cc == c),
                              "ic": st.fmean([v for _g, cc, v in rows if cc == c])
                              if any(cc == c for _g, cc, _v in rows) else None,
                              "t": tstat([v for _g, cc, v in rows if cc == c])} for c in CYC}}
    if blocks:
        by_m: dict[int, list[float]] = {}
        for g, _c, v in rows:
            by_m.setdefault(month_start(g), []).append(v)
        mm = [st.fmean(v) for _m, v in sorted(by_m.items())]
        o.update({"n_months": len(mm), "t_month": tstat(mm), "t_nw": nw_t(vals, NW_LAG),
                  "ar1": ar1(vals)})
    return o


def pooled(eps: list[dict], y: dict) -> float | None:
    xx = [(bounce(ep), y[G.key_of(ep)]) for ep in eps if G.key_of(ep) in y and bounce(ep) is not None]
    return G.spearman([a for a, _ in xx], [b for _, b in xx]) if len(xx) > 2 else None


# ---------------------------------------------------------------- метрики книги

def cyc_spent(row: dict, cycles) -> float:
    return sum(row["cycles"][c]["spent"] for c in cycles if c in row["cycles"])


def ex_usd(row: dict, k: str = "alt", cycles=None) -> float:
    """(P&L − бенчмарк) / вложено строки G.seed_row (cycles — по сумме циклов)."""
    if cycles is None:
        return (row["pnl"] - row[k]) / row["spent"] if row["spent"] else 0.0
    sp = cyc_spent(row, cycles)
    return G.cyc_sum(row, cycles, f"ex_{k}") / sp if sp else 0.0


def summ(rows: list[dict], brows: list[dict], died: list[int]) -> dict:
    """Средние J.SEEDS прогонов и сравнение с базой того же порядка (попарно по seed)."""
    pair = list(zip(rows, brows))
    o: dict = {"pnl_mean": st.fmean(x["pnl"] for x in rows),
               "pnl_min": min(x["pnl"] for x in rows), "pnl_max": max(x["pnl"] for x in rows),
               "spent_mean": st.fmean(x["spent"] for x in rows),
               "pnl_per_usd": st.fmean(x["pnl"] / x["spent"] for x in rows),
               "died2y_mean": st.fmean(died),
               "beat_base_pnl": sum(1 for x, y in pair if x["pnl"] > y["pnl"]),
               "beat_base_ex_alt": sum(1 for x, y in pair if x["pnl"] - x["alt"] > y["pnl"] - y["alt"]),
               "beat_base_ex_alt_usd": sum(1 for x, y in pair if ex_usd(x) > ex_usd(y))}
    for k, _n in BENCH:
        o[f"ex_{k}_mean"] = st.fmean(x["pnl"] - x[k] for x in rows)
        o[f"ex_{k}_per_usd"] = st.fmean(ex_usd(x, k) for x in rows)
        o[f"beat_{k}_runs"] = sum(1 for x in rows if x["pnl"] > x[k])
    o["cycles"] = {c: {"pnl": st.fmean(G.cyc_sum(x, (c,), "pnl") for x in rows),
                       "spent": st.fmean(cyc_spent(x, (c,)) for x in rows),
                       "ex_alt": st.fmean(G.cyc_sum(x, (c,), "ex_alt") for x in rows),
                       "ex_alt_usd": st.fmean(ex_usd(x, "alt", (c,)) for x in rows),
                       "beat_base_ex_alt_usd": sum(1 for x, y in pair
                                                   if ex_usd(x, "alt", (c,)) > ex_usd(y, "alt", (c,)))}
                   for c in CYC}
    for part, cyc in (("is", IS_CYCLES), ("oos", (OOS_CYCLE,))):
        o[f"{part}_pnl"] = st.fmean(G.cyc_sum(x, cyc, "pnl") for x in rows)
        o[f"{part}_spent"] = st.fmean(cyc_spent(x, cyc) for x in rows)
        for k, _n in BENCH:
            o[f"{part}_ex_{k}"] = st.fmean(G.cyc_sum(x, cyc, f"ex_{k}") for x in rows)
        o[f"{part}_ex_alt_usd"] = st.fmean(ex_usd(x, "alt", cyc) for x in rows)
        o[f"{part}_beat_base_ex_alt"] = sum(1 for x, y in pair if G.cyc_sum(x, cyc, "ex_alt")
                                            > G.cyc_sum(y, cyc, "ex_alt"))
        o[f"{part}_beat_base_ex_alt_usd"] = sum(1 for x, y in pair
                                                if ex_usd(x, "alt", cyc) > ex_usd(y, "alt", cyc))
        o[f"{part}_beat_base_pnl"] = sum(1 for x, y in pair if G.cyc_sum(x, cyc, "pnl")
                                         > G.cyc_sum(y, cyc, "pnl"))
    return o


def rule(r: dict, rb: dict) -> dict:
    """Правило решения (1)–(4) по средним 30 порядков против базы."""
    n3 = sum(1 for c in CYC if r["cycles"][c]["ex_alt_usd"] > rb["cycles"][c]["ex_alt_usd"])
    o = {"c1_pnl": r["pnl_mean"] >= rb["pnl_mean"] - 1e-9,
         "c2_ex_alt_usd": r["ex_alt_per_usd"] > rb["ex_alt_per_usd"],
         "c3_cycles": n3 >= 2, "c3_n": n3,
         "c4_oos": r["oos_beat_base_ex_alt_usd"] >= OOS_MIN_BEAT, "c4_n": r["oos_beat_base_ex_alt_usd"]}
    o["pass"] = o["c1_pnl"] and o["c2_ex_alt_usd"] and o["c3_cycles"] and o["c4_oos"]
    return o


def years_of(fl, taken) -> dict[str, dict]:
    """P&L, вложено и альты по потокам по году входа (позиции без бенчмарка — как fl.vs)."""
    out: dict[str, dict] = {}
    for ep, r in taken:
        if r["spent"] <= 0:
            continue
        bm = fl.row(ep, r)
        if None in bm.values():
            continue
        o = out.setdefault(date_of(ep["day"])[:4], {"n": 0, "spent": 0.0, "pnl": 0.0, "alt": 0.0})
        o["n"] += 1
        o["spent"] += r["spent"]
        o["pnl"] += r["pnl"]
        o["alt"] += bm["alt"]
    return out


def years_mean(yy: list[dict]) -> dict[str, dict]:
    ys = sorted({y for x in yy for y in x})
    return {y: {k: st.fmean(x.get(y, {}).get(k, 0.0) for x in yy) for k in ("n", "spent", "pnl", "alt")}
            for y in ys}


def ex_top(taken, n: int = TAIL_N) -> float:
    """P&L книги без n лучших собственных позиций."""
    pp = sorted((r["pnl"] for _e, r in taken), reverse=True)
    return sum(pp[n:])


def series_decomp(base_taken, var_taken, ser: dict) -> dict:
    """Позиции варианта против прода по сериям: та же серия и тот же вход / та же серия, другой
    день (гейт ждёт возврата к лоу) / серия только у прода (гейт её пропустил) / только у варианта."""
    bm = {ser[G.key_of(ep)]: (ep, r) for ep, r in base_taken}
    vm = {ser[G.key_of(ep)]: (ep, r) for ep, r in var_taken}
    same = [(bm[k], vm[k]) for k in bm if k in vm]
    moved = [(b_, v_) for b_, v_ in same if G.key_of(b_[0]) != G.key_of(v_[0])]
    ob_, ov_ = [bm[k] for k in bm if k not in vm], [vm[k] for k in vm if k not in bm]

    def tot(pp):
        return {"n": len(pp), "pnl": sum(r["pnl"] for _e, r in pp), "spent": sum(r["spent"] for _e, r in pp)}
    return {"same_entry": len(same) - len(moved),
            "moved": {"n": len(moved), "pnl_base": sum(b_[1]["pnl"] for b_, _v in moved),
                      "pnl_var": sum(v_[1]["pnl"] for _b, v_ in moved),
                      "spent_base": sum(b_[1]["spent"] for b_, _v in moved),
                      "spent_var": sum(v_[1]["spent"] for _b, v_ in moved),
                      "delay_days": st.fmean((v_[0]["day"] - b_[0]["day"]) / DAY for b_, v_ in moved)
                      if moved else None,
                      "price_ratio": st.fmean(v_[0]["seg"]["o"][v_[0]["e"] + 1] / b_[0]["seg"]["o"][b_[0]["e"] + 1]
                                              for b_, v_ in moved) if moved else None},
            "only_base": tot(ob_), "only_var": tot(ov_)}


def max_open(taken: list[tuple[dict, dict]], pred) -> int:
    """Наибольшее число одновременно открытых позиций с pred(ep) (выход раньше входа в день)."""
    ev = []
    for ep, r in taken:
        if pred(ep):
            ev += [(ep["day"], 1), (ep["day"] + max(r["span"], 1) * DAY, -1)]
    cur = mx = 0
    for _d, v in sorted(ev):
        cur += v
        mx = max(mx, cur)
    return mx


def prod_metrics(fl, taken, base_best, gate_k, base_main, base_taken, top, period_s: int) -> dict:
    """Q.book_metrics + на $, к проду, новые / ушли из базы, отскок взятых, занятость мест."""
    m = Q.book_metrics(fl, taken, base_best, gate_k)
    m["pnl_per_usd"] = m["pnl"] / m["spent"] if m["spent"] else 0.0
    m["ex_alt_usd"] = (m["pnl"] - m["alt"]) / m["spent"] if m["spent"] else 0.0
    for c in CYC:
        v = m["cycles"][c]
        v["ex_alt_usd"] = (v["pnl"] - v["alt"]) / v["spent"] if v["spent"] else 0.0
    keys = {G.key_of(ep) for ep, _r in taken}
    if base_main is not None:
        m["vs_prod"] = {"pnl": m["pnl"] - base_main["pnl"],
                        "ex_alt": (m["pnl"] - m["alt"]) - (base_main["pnl"] - base_main["alt"]),
                        "ex_alt_usd": m["ex_alt_usd"] - base_main["ex_alt_usd"]}
        bkeys = {G.key_of(ep) for ep, _r in base_taken}
        new = [(ep, r) for ep, r in taken if G.key_of(ep) not in bkeys]
        gone = [(ep, r) for ep, r in base_taken if G.key_of(ep) not in keys]
        for nm, pp in (("new", new), ("gone", gone)):
            m[nm] = {"n": len(pp), "spent": sum(r["spent"] for _e, r in pp),
                     "pnl": sum(r["pnl"] for _e, r in pp),
                     "bounce_med": quantile(sorted(bounce(ep) for ep, _r in pp), 0.5) if pp else None}
    m["lost_best"] = sorted(f"{coin_of(ep)[:-4]} {r['pnl']:+.0f}$" for ep, r in top
                            if G.key_of(ep) not in keys)
    bb = sorted(bounce(ep) for ep, _r in taken if bounce(ep) is not None)
    m["bounce_med"] = quantile(bb, 0.5) if bb else None
    m["chased_n"] = sum(1 for ep, _r in taken if chased(ep))
    m["chased_max_open"] = max_open(taken, chased)
    m["slots_mean"] = sum(max(r["span"], 1) for _e, r in taken) * DAY / period_s
    return m


# ---------------------------------------------------------------- разбор: книга по дням

def book_days(day_sigs: dict, taken, sim, take, illiq, y_pct: dict) -> dict:
    """Реконструкция книги по дням (тот же порядок проверок, что exec_book): допускаемые
    сигналы — прошли take, монеты нет в книге, нет кулдауна, план не отказан. «Ни один не
    взят» при свободном месте — квота неликвида (все допускаемые неликвидны, квота полна) или
    деньги (свободного USDT меньше лестницы)."""
    tk_day: dict[int, list] = {}
    for ep, r in taken:
        tk_day.setdefault(ep["day"], []).append((ep, r))
    held: list[tuple[int, str, bool]] = []
    last: dict[str, int] = {}
    asc_key = M.weak_first("bounce")
    s = dict.fromkeys(DAY_KEYS, 0)
    dec_ic_pct, dec_ic_usd = [], []
    stat = {"taken": 0.0, "asc": 0.0, "mean": 0.0, "taken_pct": [], "asc_pct": [], "mean_pct": []}
    for day in sorted(day_sigs):
        held = [x for x in held if x[0] > day]
        coins = {x[1] for x in held}
        cand = [ep for ep in day_sigs[day] if take(ep)]
        if cand:
            s["days_sig"] += 1
        adm = [ep for ep in cand if coin_of(ep) not in coins
               and not (coin_of(ep) in last and day < last[coin_of(ep)] + P.COOLDOWN_DAYS * DAY)
               and sim(ep) is not None]
        tk = tk_day.get(day, [])
        free = J.SLOTS - len(held)
        if adm:
            s["days_adm"] += 1
            s["adm"] += len(adm)
            if free <= 0:
                s["full"] += 1
            else:
                s["cap"] += 1
                s["adm_cap"] += len(adm)
                if len(tk) == len(adm):
                    s["all_taken"] += 1
                elif not tk:
                    s["none_taken"] += 1
                    s["none_quota"] += all(illiq(ep) for ep in adm) and \
                        sum(1 for x in held if x[2]) >= P.ILLIQUID_SLOTS
                else:
                    s["decide"] += 1
                    s["dec_adm"] += len(adm)
                    s["dec_free"] += free
                    s["dec_taken"] += len(tk)
                    s["dec_slots"] += len(held) + len(tk) >= J.SLOTS
                    rr = [(ep, sim(ep)) for ep in adm]
                    ok = [(ep, r) for ep, r in rr if r["spent"] > 0]
                    if len(ok) >= M.MIN_DAY_SIGNALS:
                        xs = [bounce(ep) for ep, _r in ok]
                        for ys, acc in (([r["pnl"] / r["spent"] for _e, r in ok], dec_ic_pct),
                                        ([r["pnl"] for _e, r in ok], dec_ic_usd)):
                            if len(set(xs)) > 1 and len(set(ys)) > 1:
                                acc.append((day, ok[0][0]["cycle"], G.spearman(xs, ys)))
                    k = len(tk)
                    asc = sorted(rr, key=lambda t: asc_key(t[0]))[:k]
                    stat["taken"] += sum(r["pnl"] for _e, r in tk)
                    stat["asc"] += sum(r["pnl"] for _e, r in asc)
                    stat["mean"] += k * st.fmean(r["pnl"] for _e, r in rr)
                    for nm, sel in (("taken_pct", tk), ("asc_pct", asc)):
                        stat[nm] += [y_pct[G.key_of(ep)] for ep, _r in sel if G.key_of(ep) in y_pct]
                    stat["mean_pct"] += [y_pct[G.key_of(ep)] for ep, _r in rr if G.key_of(ep) in y_pct]
        for ep, r in tk:
            held.append((day + max(r["span"], 1) * DAY, coin_of(ep), illiq(ep)))
            last[coin_of(ep)] = day
    for nm in ("taken_pct", "asc_pct", "mean_pct"):
        stat[nm] = st.fmean(stat[nm]) if stat[nm] else None
    s["static"] = stat
    s["ic_pct"] = ic_summary(dec_ic_pct, blocks=False)
    s["ic_usd"] = ic_summary(dec_ic_usd, blocks=False)
    return s


DAY_KEYS = ("days_sig", "days_adm", "full", "cap", "all_taken", "none_taken", "none_quota", "decide",
            "adm", "adm_cap", "dec_adm", "dec_free", "dec_taken", "dec_slots")


# ---------------------------------------------------------------- разбор: лестница

def ladder_stats(ep: dict, r: dict) -> dict:
    """Ступени, средняя цена входа и первая ступень к лоу базы. План — как simulate_exec с
    EXEC_ALL (base30, anchor_open, grid): цены ступеней из плана, количество — из $ покупок
    (ступени исполняются сверху вниз, порядок покупок в flows = порядок ступеней)."""
    seg, e = ep["seg"], ep["e"]
    c, o = seg["c"], seg["o"]
    low = min(c[max(0, e - 29):e + 1])
    p0 = o[e + 1]
    bottom = low * (1 - L.INVAL_BUF) * L.FLOOR_MARGIN
    nr = 1 if p0 <= bottom * 1.02 else STEPS
    tick, qstep = L.synth_rules(p0)
    prices = [p0] + [round_step(p0 - k * (p0 - bottom) / (nr - 1), tick) for k in range(1, nr)]
    buys = [usd for _d, sd, usd, _f in r["flows"] if sd == "Buy"]
    bad = sum(1 for u, px in zip(buys[1:], prices[1:])
              if abs(u - round_step(J.BUDGET / nr / px, qstep) * px) > 1e-9)
    qty = sum(u / px for u, px in zip(buys, prices))
    return {"n_buys": len(buys), "avg_low": sum(buys) / qty / low if qty else None,
            "p0_low": p0 / low, "bad": bad + (len(buys) > nr)}


def bucket_of(v: float | None) -> str:
    if v is None:
        return "неизв."
    for nm, a, b in BUCKETS:
        if a + EPS < v <= b + EPS:
            return nm
    return BUCKETS[-1][0]


def ladder_table(pairs: list[tuple[dict, dict]]) -> dict:
    """По корзинам bounce: ступени, вложено, цена входа к лоу, длительность, исходы в % и $."""
    out = {}
    for nm in [b[0] for b in BUCKETS]:
        pp = [(ep, r) for ep, r in pairs if bucket_of(bounce(ep)) == nm]
        if not pp:
            out[nm] = {"n": 0}
            continue
        ls = [ladder_stats(ep, r) for ep, r in pp]
        sp = [r["spent"] for _e, r in pp]
        pn = [r["pnl"] for _e, r in pp]
        span = [max(r["span"], 1) for _e, r in pp]
        pos = [(r["pnl"] / r["spent"]) for _e, r in pp if r["spent"] > 0]
        out[nm] = {"n": len(pp), "share": len(pp) / len(pairs),
                   "n_buys": st.fmean(x["n_buys"] for x in ls),
                   "spent": st.fmean(sp), "pnl": st.fmean(pn),
                   "pnl_per_usd": sum(pn) / sum(sp) if sum(sp) else 0.0,
                   "pnl_per_budget": st.fmean(pn) / J.BUDGET,
                   "ret_med": st.median(pos) if pos else None,
                   "win": sum(1 for x in pn if x > 0) / len(pp),
                   "stop": sum(1 for _e, r in pp if r["how"] == "стоп") / len(pp),
                   "avg_low": st.fmean(x["avg_low"] for x in ls if x["avg_low"] is not None),
                   "p0_low": st.fmean(x["p0_low"] for x in ls),
                   "span": st.fmean(span), "span_med": st.median(span),
                   "pnl_slot_year": sum(pn) / (sum(span) / 365),
                   "bad": sum(x["bad"] for x in ls)}
    return out


# ---------------------------------------------------------------- данные

def load() -> dict:
    """Архив, бенчмарки и сигналы прода с признаками — как momentum_order_study.main."""
    t0 = time.time()
    coins = L.load_archive()
    all_segs = L.segments(coins)
    px = T.Prices(all_segs)
    btc = P.btc_closes(all_segs)
    last_day = max(btc)
    alt, _ = P.market_index(ROOT / "scanner.db")
    comps = T.compositions(px, last_day)
    cap, _ = T.basket_level(px, comps, "cap", last_day)
    print(f"архив Binance до {date_of(last_day)}, индекс альтов {len(alt)} дней  [{time.time() - t0:.0f} с]")
    vranks = L.vol_ranks(all_segs)
    npairs = P.pairs_per_day(vranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot = L.load_hot()
    src = [ep for ep in L.signal_days(segs, vranks, hot, {"prod": L.is_spring_prod})["prod"]
           if ep["cycle"] != "?"]
    for ep in src:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
        ep["f"] = M.features(ep, alt)
    print(f"дней сигнала прода {len(src)}  [{time.time() - t0:.0f} с]")
    return {"src": src, "series": {"alt": alt, "cap": cap, "btc": btc}, "alt": alt, "btc": btc,
            "last_day": last_day}


# ---------------------------------------------------------------- печать

def pct(x: float | None, w: int = 0) -> str:
    return (f"{x * 100:+.1f}%" if x is not None else "—").rjust(w)


def fic(o: dict, key: str = "ic") -> str:
    v, t = o.get(key), o.get("t")
    return f"{v:+.3f} (t {t:+.1f}, n {o['n']})" if v is not None and t is not None else \
        (f"{v:+.3f} (n {o['n']})" if v is not None else "—")


# ---------------------------------------------------------------- книга

def analyze_book(b: str, sell: dict, D: dict, sig: list[dict], VARIANTS: list[dict],
                 ser: dict, t0: float) -> dict:
    src, series, alt, btc = D["src"], D["series"], D["alt"], D["btc"]
    base_take, illiq = P.EXEC_POLICY
    name = "R правила" if b == "R" else "H держать"
    labels = {v["key"]: v["label"] for v in VARIANTS}
    cache: dict = {}

    def sim(ep):
        k = (ep["seg"]["sym"], ep["e"])
        if k not in cache:
            cache[k] = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET, horizon=None,
                                       rules=L.EXEC_ALL)
        return cache[k]
    fl = Q.Flows(series)
    ref = json.loads((Path(__file__).resolve().parent / "portfolio_vs_market_results.json")
                     .read_text(encoding="utf-8"))["exec"][b]["s6"]
    base = P.exec_book(src, sim, *P.EXEC_POLICY, cooldown=True, cash=True)
    chk = P.evaluate_flows(base, alt, btc)
    assert chk["n"] == ref["n"] and abs(chk["pnl"] - ref["pnl"]) < 1e-6 and \
        abs(chk["alt"] - ref["alt"]) < 1e-6, f"{b}: s6 не повторился ({chk['pnl']} / {ref['pnl']})"
    ref_vs, mine = T.flows_vs(base, series), fl.vs(base)
    assert all(abs(ref_vs[k] - mine[k]) < 1e-6 for k in ("pnl", "alt", "cap", "btc")), "кэш потоков"
    print(f"\n{'=' * 100}\nКНИГА {name}: сверка s6 n={chk['n']} P&L {chk['pnl']:+.0f}$ (эталон "
          f"{ref['pnl']:+.0f}$), −альты {chk['pnl'] - chk['alt']:+.0f}$ ✓  [{time.time() - t0:.0f} с]")
    top = sorted(base, key=lambda t: t[1]["pnl"], reverse=True)[:max(1, len(base) // 10)]
    base_best = {G.key_of(ep) for ep, _r in top}
    by_key = {G.key_of(ep): ep for ep in src}

    # ---- исходы сигналов (позиция, как если бы её взяли в день сигнала)
    y_pct, y_usd, w_usd = {}, {}, {}
    for ep in sig:
        r = sim(ep)
        if r is not None and r["spent"] > 0:
            k = G.key_of(ep)
            y_pct[k], y_usd[k], w_usd[k] = r["pnl"] / r["spent"], r["pnl"], r["spent"]
    q13 = M.ic_block(sig, y_pct, ("bounce",))["bounce"]
    ref13 = json.loads(REF_Q13.read_text(encoding="utf-8"))[b]["ic"]["bounce"]["all"]
    assert abs(q13["all"]["ic"] - ref13["ic"]) < 1e-9 and q13["all"]["n_days"] == ref13["n_days"], \
        f"{b}: IC bounce не повторил Q13 ({q13['all']['ic']} / {ref13['ic']})"
    print(f"  сверка IC bounce с Q13: {q13['all']['ic']:+.4f} (t {q13['all']['t']:+.1f}, дней "
          f"{q13['all']['n_days']}) ✓; исход есть у {len(y_pct)} из {len(sig)} сигналов")

    bq = sorted(bounce(ep) for ep, _r in base if bounce(ep) is not None)
    taken_dist = {"n": len(base),
                  "quantiles": {f"q{round(q * 100)}": quantile(bq, q) for q in (0.10, 0.25, 0.50, 0.75, 0.90, 0.95)},
                  "share_le": {f"{x:.0%}": sum(1 for v in bq if v <= x + EPS) / len(bq) for x in GATES},
                  "spent_share_gt20": sum(r["spent"] for ep, r in base if chased(ep)) / sum(r["spent"] for _e, r in base),
                  "pnl_gt20": sum(r["pnl"] for ep, r in base if chased(ep)),
                  "chased_max_open": max_open(base, chased)}
    tq = taken_dist["quantiles"]
    print(f"  отскок у позиций прода (n {len(base)}): q10 {pct(tq['q10'])}, q25 {pct(tq['q25'])}, медиана "
          f"{pct(tq['q50'])}, q75 {pct(tq['q75'])}, q90 {pct(tq['q90'])}, q95 {pct(tq['q95'])}; доля ≤ "
          + ", ".join(f"{k} {v:.0%}" for k, v in taken_dist["share_le"].items())
          + f"; > 20%: вложено {taken_dist['spent_share_gt20']:.0%} $, P&L {taken_dist['pnl_gt20']:+.0f}$, "
          f"одновременно не больше {taken_dist['chased_max_open']}")

    # ---- варианты
    ob: dict = {"best_threshold_pnl": top[-1][1]["pnl"], "taken_bounce": taken_dist,
                "best": [{"coin": coin_of(ep)[:-4], "bounce": bounce(ep), "day": date_of(ep["day"]),
                          "pnl": r["pnl"]} for ep, r in top], "variants": {}}
    base_main = base_rows = base_runs = None
    base_taken = base
    period_s = D["last_day"] - min(ep["day"] for ep in src)
    for v in VARIANTS:
        take = base_take if v["kind"] != "gate" else (lambda ep, g=v["g"]: base_take(ep) and g(ep))
        q2 = v.get("q2")

        def gate_k(k, take=take):
            return take(by_key[k])
        taken = P.exec_book(src, sim, take, illiq, cooldown=True, cash=True, quota2=q2,
                            order=v.get("order", "rank"), seed=0)
        if v["key"] in ("base", "g_inf"):
            assert [G.key_of(e) for e, _r in taken] == [G.key_of(e) for e, _r in base], \
                f"{v['key']} != база s6"
        m = prod_metrics(fl, taken, base_best, gate_k, base_main, base_taken, top, period_s)
        if v["key"] == "base":
            base_main = m
        runs = [P.exec_book(src, sim, take, illiq, cooldown=True, cash=True, quota2=q2,
                            order=v["order"] if v["kind"] == "order" else "random", seed=sd)
                for sd in range(J.SEEDS)]
        rows = [G.seed_row(fl.vs(tk)) for tk in runs]
        died = [sum(1 for ep, _r in tk if ep["died2y"]) for tk in runs]
        if v["key"] == "base":
            base_rows, base_runs = rows, runs
        if v["key"] == "g_inf":
            assert all(abs(x["pnl"] - y["pnl"]) < 1e-9 and x["spent"] == y["spent"]
                       for x, y in zip(rows, base_rows)), "гейт X = ∞ != база по порядкам"
        m["random"] = summ(rows, base_rows, died)
        if v["fam"]:
            m["rule"] = rule(m["random"], ob["variants"]["base"]["random"])
        m["random"]["chased_n_mean"] = st.fmean(sum(1 for ep, _r in tk if chased(ep)) for tk in runs)
        # хвост своей книги и годы
        xt = [ex_top(tk) for tk in runs]
        if v["key"] == "base":
            base_xt = xt
        m["pnl_ex_top5"] = ex_top(taken)
        m["random"]["pnl_ex_top5_mean"] = st.fmean(xt)
        m["random"]["beat_base_pnl_ex_top5"] = sum(1 for a_, b_ in zip(xt, base_xt) if a_ > b_)
        m["years"] = years_of(fl, taken)
        m["random"]["years"] = years_mean([years_of(fl, tk) for tk in runs])
        if v["key"] != "base":
            m["series"] = series_decomp(base_taken, taken, ser)
        ob["variants"][v["key"]] = m
    print(f"  варианты: {len(VARIANTS)} × {J.SEEDS + 1} прогонов; гейт X = ∞ = база (прод и "
          f"{J.SEEDS} порядков) ✓  [{time.time() - t0:.0f} с]")

    # ---- печать вариантов
    vb, rb = ob["variants"]["base"], ob["variants"]["base"]["random"]
    print(f"  прод: поз. {vb['n']}, вложено {vb['spent']:.0f}$, P&L {vb['pnl']:+.0f}$ ({pct(vb['pnl_per_usd'])} "
          f"на $), −альты {vb['pnl'] - vb['alt']:+.0f}$ ({pct(vb['ex_alt_usd'])}), −BTC {vb['pnl'] - vb['btc']:+.0f}$; "
          f"случайный порядок (ср. {J.SEEDS}): P&L {rb['pnl_mean']:+.0f}$ ({pct(rb['pnl_per_usd'])}), −альты "
          f"{rb['ex_alt_mean']:+.0f}$ ({pct(rb['ex_alt_per_usd'])}); лучшие 10% прода — P&L ≥ "
          f"{ob['best_threshold_pnl']:+.0f}$ ({len(top)} поз.)")
    print(f"  A) средние {J.SEEDS} порядков (гейты/квоты — случайный порядок, порядки — свой ключ; «лучше "
          f"базы» — против случайной базы того же seed: P&L / −альты / −альты на $; циклы — −альты на $; "
          f"без 5 — P&L без 5 лучших своих позиций (лучше базы); правило 1–4: + выполнено)")
    print(f"  {'вариант':38} {'P&L':>6} {'влож.':>6} {'на $':>7} {'−альты':>7} {'на $':>7} {'−корз.':>7} "
          f"{'−BTC':>7} {'лучше базы':>11} " + " ".join(f"{c:>7}" for c in CYC) + f" {'без 5':>10} "
          f"{'†ср.':>5} {'1234':>5}")
    for v in VARIANTS:
        r = ob["variants"][v["key"]]["random"]
        ru = ob["variants"][v["key"]].get("rule")
        flags = "".join("+" if ru[k] else "·" for k in ("c1_pnl", "c2_ex_alt_usd", "c3_cycles", "c4_oos")) \
            if ru else ""
        print(f"  {v['label']:38} {r['pnl_mean']:>+6.0f} {r['spent_mean']:>6.0f} {pct(r['pnl_per_usd'], 7)} "
              f"{r['ex_alt_mean']:>+7.0f} {pct(r['ex_alt_per_usd'], 7)} {r['ex_cap_mean']:>+7.0f} "
              f"{r['ex_btc_mean']:>+7.0f} " + f"{r['beat_base_pnl']}/{r['beat_base_ex_alt']}/"
              f"{r['beat_base_ex_alt_usd']}".rjust(11) + " "
              + " ".join(pct(r["cycles"][c]["ex_alt_usd"], 7) for c in CYC)
              + f"{r['pnl_ex_top5_mean']:+.0f} ({r['beat_base_pnl_ex_top5']})".rjust(11)
              + f" {r['died2y_mean']:>5.1f} {flags:>5}")
    print(f"  B) порядок по обороту (прод) / первый прогон ключа: «к проду» — минус прод (P&L, −альты); "
          f">20% — позиций с отскоком > 20% (макс. одновременно); мест — занято в среднем из {J.SLOTS}; "
          f"новые / ушли — позиции не из прода и позиции прода, которых нет (n, P&L $); † — умерли за "
          f"2 года; лучш. — пропало из лучших 10% прода (из них гейтом)")
    print(f"  {'вариант':38} {'поз.':>4} {'влож.':>6} {'P&L':>6} {'на $':>7} {'−альты':>7} {'на $':>7} "
          f"{'−BTC':>6} {'к проду':>12} {'без 5':>6} {'>20%':>7} {'мед.отск.':>9} {'мест':>5} "
          f"{'новые / ушли':>20} {'†':>3} {'лучш.':>6}")
    for v in VARIANTS:
        m = ob["variants"][v["key"]]
        vp = m.get("vs_prod", {"pnl": 0.0, "ex_alt": 0.0})
        ng = (f"{m['new']['n']} {m['new']['pnl']:+.0f} / {m['gone']['n']} {m['gone']['pnl']:+.0f}"
              if "new" in m else "—")
        print(f"  {v['label']:38} {m['n']:>4} {m['spent']:>6.0f} {m['pnl']:>+6.0f} {pct(m['pnl_per_usd'], 7)} "
              f"{m['pnl'] - m['alt']:>+7.0f} {pct(m['ex_alt_usd'], 7)} {m['pnl'] - m['btc']:>+6.0f} "
              + f"{vp['pnl']:+.0f}/{vp['ex_alt']:+.0f}".rjust(12) + f" {m['pnl_ex_top5']:>+6.0f}"
              + f"{m['chased_n']}({m['chased_max_open']})".rjust(8) + f" {pct(m['bounce_med'], 9)} "
              f"{m['slots_mean']:>5.1f} {ng:>20} {m['died2y']:>3} "
              + f"{m['best_lost']}({m['best_lost_by_gate']})".rjust(6))
    # серии: чем гейт / порядок / квота заменяет позиции прода (прод-порядок)
    print("  C) позиции против прода по сериям (прод-порядок): тот же вход | та же серия, другой день: n, "
          "P&L прод → вариант, ср. сдвиг дней, цена 1-й ступени к проду | серия только у прода | только у варианта")
    for v in VARIANTS:
        sd_ = ob["variants"][v["key"]].get("series")
        if not sd_ or v["key"] == "g_inf":
            continue
        mv = sd_["moved"]
        print(f"  {v['label']:38} {sd_['same_entry']:>4} | {mv['n']:>3} {mv['pnl_base']:+.0f} → {mv['pnl_var']:+.0f}$"
              + (f", {mv['delay_days']:+.0f} дн., ×{mv['price_ratio']:.3f}" if mv["n"] else "")
              + f" | {sd_['only_base']['n']:>3} {sd_['only_base']['pnl']:+.0f}$ | {sd_['only_var']['n']:>3} "
              f"{sd_['only_var']['pnl']:+.0f}$")
    # годы: база и варианты (среднее порядков), P&L и −альты, $
    keys_y = ["base"] + [v["key"] for v in VARIANTS if v["key"] in ("g_le5", "g_le10", "g_le20", "o_asc")
                         or (v["fam"] and ob["variants"][v["key"]]["rule"]["pass"])]
    keys_y = list(dict.fromkeys(keys_y))
    yrs = sorted(ob["variants"]["base"]["random"]["years"])
    print(f"  D) по году входа, среднее {J.SEEDS} порядков: P&L / −альты ($); варианты — разница с базой")
    print(f"  {'вариант':38} " + " ".join(f"{y:>11}" for y in yrs))
    for k in keys_y:
        yy = ob["variants"][k]["random"]["years"]
        by_ = ob["variants"]["base"]["random"]["years"]
        cells = []
        for y in yrs:
            a_ = yy.get(y, {"pnl": 0.0, "alt": 0.0})
            if k == "base":
                cells.append(f"{a_['pnl']:+.0f}/{a_['pnl'] - a_['alt']:+.0f}")
            else:
                b_ = by_.get(y, {"pnl": 0.0, "alt": 0.0})
                cells.append(f"{a_['pnl'] - b_['pnl']:+.0f}/{(a_['pnl'] - a_['alt']) - (b_['pnl'] - b_['alt']):+.0f}")
        print(f"  {labels[k]:38} " + " ".join(f"{c:>11}" for c in cells))

    # ---- вне выборки
    oos: dict = {}
    for fam, fname in FAMILIES.items():
        cand = [v for v in VARIANTS if v["fam"] == fam]
        R_ = {v["key"]: ob["variants"][v["key"]]["random"] for v in cand}
        ok = [v for v in cand if R_[v["key"]]["is_pnl"] >= rb["is_pnl"] - 1e-9]
        pick = max(ok or cand, key=lambda v: R_[v["key"]]["is_ex_alt_usd"])
        rp = R_[pick["key"]]
        oos[fam] = {"pick": pick["key"], "pnl_ok": bool(ok),
                    "is": {"pnl": rp["is_pnl"] - rb["is_pnl"], "ex_alt": rp["is_ex_alt"] - rb["is_ex_alt"],
                           "ex_alt_usd": rp["is_ex_alt_usd"] - rb["is_ex_alt_usd"],
                           "beat_usd": rp["is_beat_base_ex_alt_usd"]},
                    "oos": {"pnl": rp["oos_pnl"] - rb["oos_pnl"], "ex_alt": rp["oos_ex_alt"] - rb["oos_ex_alt"],
                            "ex_alt_usd": rp["oos_ex_alt_usd"] - rb["oos_ex_alt_usd"],
                            "ex_btc": rp["oos_ex_btc"] - rb["oos_ex_btc"],
                            "beat_usd": rp["oos_beat_base_ex_alt_usd"], "beat_alt": rp["oos_beat_base_ex_alt"],
                            "beat_pnl": rp["oos_beat_base_pnl"]},
                    "base_oos": {"pnl": rb["oos_pnl"], "ex_alt": rb["oos_ex_alt"],
                                 "ex_alt_usd": rb["oos_ex_alt_usd"]}}
        o = oos[fam]
        print(f"  вне выборки [{fname}]: по 2018–22 выбран «{pick['label']}»"
              f"{'' if ok else ' (P&L 2018–22 ниже базы у всех)'} — к базе 2018–22: P&L {o['is']['pnl']:+.0f}$, "
              f"−альты {o['is']['ex_alt']:+.0f}$, на $ {o['is']['ex_alt_usd'] * 100:+.1f} п.п. "
              f"({o['is']['beat_usd']}/{J.SEEDS}); 2023–26: P&L {o['oos']['pnl']:+.0f}$, −альты "
              f"{o['oos']['ex_alt']:+.0f}$, на $ {o['oos']['ex_alt_usd'] * 100:+.1f} п.п. ({o['oos']['beat_usd']}/"
              f"{J.SEEDS}), −BTC {o['oos']['ex_btc']:+.0f}$")
    ob["oos"] = oos
    passed = [v["key"] for v in VARIANTS if v["fam"] and ob["variants"][v["key"]]["rule"]["pass"]]
    ob["rule_passed"] = passed
    print(f"  правило решения: все 4 условия — {', '.join(passed) or 'ни один вариант'}")

    # ================================================================ разбор расхождения
    print(f"\n  РАЗБОР «сильный IC — слабая книга» [{name}]  [{time.time() - t0:.0f} с]")
    diag: dict = {}
    day_sigs: dict[int, list[dict]] = {}
    for ep in src:
        day_sigs.setdefault(ep["day"], []).append(ep)

    # ---- a) решает ли порядок
    bd_prod = book_days(day_sigs, base_taken, sim, base_take, illiq, y_pct)
    bd_rnd = [book_days(day_sigs, tk, sim, base_take, illiq, y_pct) for tk in base_runs]
    rnd_mean = {k: st.fmean(x[k] for x in bd_rnd) for k in DAY_KEYS}
    a = {"prod": {k: bd_prod[k] for k in DAY_KEYS}, "random_mean": rnd_mean,
         "prod_static": bd_prod["static"],
         "random_static": {k: st.fmean(x["static"][k] for x in bd_rnd if x["static"][k] is not None)
                           for k in ("taken", "asc", "mean", "taken_pct", "asc_pct", "mean_pct")},
         "prod_ic_pct": bd_prod["ic_pct"], "prod_ic_usd": bd_prod["ic_usd"],
         "random_ic_pct": {"ic_mean": st.fmean(x["ic_pct"]["ic"] for x in bd_rnd if x["ic_pct"]["ic"] is not None),
                           "n_mean": st.fmean(x["ic_pct"]["n"] for x in bd_rnd),
                           "neg_runs": sum(1 for x in bd_rnd if (x["ic_pct"]["ic"] or 0) < 0)}}
    p_, ss = a["prod"], a["prod_static"]
    print(f"  a) дни с сигналами, прошедшими фильтры: {p_['days_sig']}; с допускаемыми {p_['days_adm']}: книга полна "
          f"{p_['full']} ({p_['full'] / p_['days_adm']:.0%}), есть место {p_['cap']} — взяты все {p_['all_taken']}, "
          f"ни один {p_['none_taken']} (из них квота неликвида {p_['none_quota']}, остальное — деньги), "
          f"ПОРЯДОК РЕШАЕТ {p_['decide']} "
          f"({p_['decide'] / p_['days_adm']:.0%} дней с допускаемыми, {p_['decide'] / p_['days_sig']:.0%} дней с сигналами); "
          f"случайные порядки — {rnd_mean['decide']:.0f} дн. из {rnd_mean['days_adm']:.0f}")
    print(f"     на днях выбора: допускаемых {p_['dec_adm'] / max(1, p_['decide']):.1f}, свободных мест "
          f"{p_['dec_free'] / max(1, p_['decide']):.1f}, взято {p_['dec_taken'] / max(1, p_['decide']):.1f} "
          f"(всего {p_['dec_taken']} из {vb['n']} позиций прода = {p_['dec_taken'] / vb['n']:.0%}); "
          f"заполнили книгу {p_['dec_slots']} дн., остальные — деньги/квота")
    ri = a["random_ic_pct"]
    print(f"     IC на днях выбора (≥ {M.MIN_DAY_SIGNALS} доп.): прод % {fic(a['prod_ic_pct'])}, $ "
          f"{fic(a['prod_ic_usd'])}; {J.SEEDS} порядков: ср. % {ri['ic_mean']:+.3f} (дней {ri['n_mean']:.0f}, "
          f"отрицательный в {ri['neg_runs']}/{J.SEEDS})")
    rs_ = a["random_static"]
    print(f"     статически на днях выбора (без пути книги): Σ P&L взятых {ss['taken']:+.0f}$, k наименее "
          f"отскочивших {ss['asc']:+.0f}$, среднего допускаемого {ss['mean']:+.0f}$; в %: {pct(ss['taken_pct'])} / "
          f"{pct(ss['asc_pct'])} / {pct(ss['mean_pct'])}; 30 порядков: {rs_['taken']:+.0f} / {rs_['asc']:+.0f} / "
          f"{rs_['mean']:+.0f}$")
    # доля позиций базы, зависящих от порядка
    all_runs = [base_taken] + base_runs
    cnt_key: dict = {}
    cnt_ser: dict = {}
    for tk in all_runs:
        for k in {G.key_of(ep) for ep, _r in tk}:
            cnt_key[k] = cnt_key.get(k, 0) + 1
        for k in {ser[G.key_of(ep)] for ep, _r in tk}:
            cnt_ser[k] = cnt_ser.get(k, 0) + 1
    nrun = len(all_runs)
    core_key = [(ep, r) for ep, r in base_taken if cnt_key[G.key_of(ep)] == nrun]
    core_ser = [(ep, r) for ep, r in base_taken if cnt_ser[ser[G.key_of(ep)]] == nrun]
    a["positions"] = {"runs": nrun, "union_key": len(cnt_key), "union_series": len(cnt_ser),
                      "mean_n": st.fmean(len(tk) for tk in all_runs),
                      "core_key": len(core_key), "core_series": len(core_ser),
                      "core_series_pnl": sum(r["pnl"] for _e, r in core_ser),
                      "always_series_any_run": sum(1 for v in cnt_ser.values() if v == nrun),
                      "top": [{"coin": coin_of(ep)[:-4], "pnl": r["pnl"],
                               "runs_series": cnt_ser[ser[G.key_of(ep)]], "runs_key": cnt_key[G.key_of(ep)]}
                              for ep, r in top[:TAIL_N]]}
    pz = a["positions"]
    print(f"     позиций прода {vb['n']}: во всех {nrun} прогонах (прод + {J.SEEDS} порядков) тот же вход "
          f"{pz['core_key']} ({pz['core_key'] / vb['n']:.0%}), та же серия {pz['core_series']} "
          f"({pz['core_series'] / vb['n']:.0%}, P&L {pz['core_series_pnl']:+.0f}$ из {vb['pnl']:+.0f}$); "
          f"зависят от порядка {vb['n'] - pz['core_series']} ({1 - pz['core_series'] / vb['n']:.0%}); "
          f"разных входов за {nrun} прогонов {pz['union_key']}, серий {pz['union_series']} "
          f"(ср. {pz['mean_n']:.0f} позиций на прогон)")
    print("     5 лучших прода — в скольких из 31 прогона та же серия: " + ", ".join(
        f"{x['coin']} {x['pnl']:+.0f}$ {x['runs_series']}" for x in pz["top"]))
    diag["a"] = a

    # ---- b) IC: всё / в $ / по $ / взятые
    rows_pct = group_ics(sig, y_pct, by_day)
    rows_usd = group_ics(sig, y_usd, by_day)
    rows_w = group_ics(sig, y_pct, by_day, w=w_usd)
    assert abs(st.fmean(v for _g, _c, v in rows_pct) - q13["all"]["ic"]) < 1e-12, "IC по дням != M.ic_block"
    base_eps = [ep for ep, _r in base_taken]
    rnd_eps = [ep for tk in base_runs for ep, _r in tk]
    bm = group_ics(base_eps, y_pct, by_month, min_n=3)
    bm_usd = group_ics(base_eps, y_usd, by_month, min_n=3)
    bb = {"all_pct": ic_summary(rows_pct), "all_usd": ic_summary(rows_usd), "all_w": ic_summary(rows_w),
          "pooled_all_pct": pooled(sig, y_pct), "pooled_all_usd": pooled(sig, y_usd),
          "pooled_base_pct": pooled(base_eps, y_pct), "pooled_base_usd": pooled(base_eps, y_usd),
          "pooled_runs_pct": pooled(rnd_eps, y_pct), "pooled_runs_usd": pooled(rnd_eps, y_usd),
          "base_month_pct": ic_summary(bm, blocks=False), "base_month_usd": ic_summary(bm_usd, blocks=False),
          "n_base": len(base_eps),
          "spearman_bounce_spent": G.spearman([bounce(ep) for ep in sig if G.key_of(ep) in w_usd],
                                              [w_usd[G.key_of(ep)] for ep in sig if G.key_of(ep) in w_usd])}
    print(f"  b) IC внутри дня, все сигналы: исход в % {fic(bb['all_pct'])}; в $ {fic(bb['all_usd'])}; "
          f"взвешенный по вложенным $ {fic(bb['all_w'])}")
    print("     по циклам %: " + ", ".join(f"{c} {fic(bb['all_pct']['cycles'][c])}" for c in CYC))
    print("     по циклам $: " + ", ".join(f"{c} {fic(bb['all_usd']['cycles'][c])}" for c in CYC))
    print(f"     Спирмен по всем сигналам (без деления на дни): % {bb['pooled_all_pct']:+.3f}, $ "
          f"{bb['pooled_all_usd']:+.3f}; по взятым позициям прода (n {len(base_eps)}): % "
          f"{bb['pooled_base_pct']:+.3f}, $ {bb['pooled_base_usd']:+.3f}; позиции 30 порядков вместе: % "
          f"{bb['pooled_runs_pct']:+.3f}, $ {bb['pooled_runs_usd']:+.3f}; внутри месяца по позициям прода: % "
          f"{fic(bb['base_month_pct'])}, $ {fic(bb['base_month_usd'])}")
    print(f"     связь отскока с вложенными $ (Спирмен по сигналам): {bb['spearman_bounce_spent']:+.3f}")
    diag["b"] = bb

    # ---- c) хвост
    top5 = top[:TAIL_N]
    drop: list[tuple[str, int, int]] = []
    for ep, r in top5:
        sym, start = ser[G.key_of(ep)]
        drop.append((sym, start, ep["day"] + max(r["span"], 1) * DAY))

    def dropped(ep):
        return any(ep["seg"]["sym"] == sym and a0 <= ep["day"] <= a1 for sym, a0, a1 in drop)
    src_ex = [ep for ep in src if not dropped(ep)]
    sig_ex = [ep for ep in sig if not dropped(ep)]
    cc = {"top5": [{"coin": coin_of(ep)[:-4], "day": date_of(ep["day"]), "bounce": bounce(ep),
                    "pnl": r["pnl"], "how": r["how"], "span": r["span"]} for ep, r in top5],
          "top5_share": sum(r["pnl"] for _e, r in top5) / vb["pnl"],
          "signals_removed": len(src) - len(src_ex),
          "ic_pct": ic_summary(group_ics(sig_ex, y_pct, by_day), blocks=False),
          "ic_usd": ic_summary(group_ics(sig_ex, y_usd, by_day), blocks=False),
          "variants": {}}
    print("  c) 5 лучших позиций прода: " + ", ".join(
        f"{x['coin']} {x['day']} от лоу {pct(x['bounce'])} {x['pnl']:+.0f}$ ({x['how']})" for x in cc["top5"])
          + f" — {cc['top5_share']:.0%} P&L прода; убрано сигналов {cc['signals_removed']}")
    print(f"     IC без них: % {fic(cc['ic_pct'])}, $ {fic(cc['ic_usd'])}")
    ex_rows_base = ex_base_summ = None
    for vk in TAIL_VARIANTS:
        v = next(x for x in VARIANTS if x["key"] == vk)
        take = base_take if v["kind"] != "gate" else (lambda ep, g=v["g"]: base_take(ep) and g(ep))
        q2 = v.get("q2")
        m0 = fl.vs(P.exec_book(src_ex, sim, take, illiq, cooldown=True, cash=True, quota2=q2,
                               order=v.get("order", "rank"), seed=0))
        runs = [P.exec_book(src_ex, sim, take, illiq, cooldown=True, cash=True, quota2=q2,
                            order=v["order"] if v["kind"] == "order" else "random", seed=sd)
                for sd in range(J.SEEDS)]
        rows = [G.seed_row(fl.vs(tk)) for tk in runs]
        died = [sum(1 for ep, _r in tk if ep["died2y"]) for tk in runs]
        if vk == "base":
            ex_rows_base = rows
        r = summ(rows, ex_rows_base, died)
        if vk == "base":
            ex_base_summ = r
        cc["variants"][vk] = {"prod": {"n": m0["n"], "spent": m0["spent"], "pnl": m0["pnl"],
                                       "ex_alt": m0["pnl"] - m0["alt"]},
                              "random": {k: r[k] for k in ("pnl_mean", "spent_mean", "pnl_per_usd", "ex_alt_mean",
                                                           "ex_alt_per_usd", "beat_base_pnl", "beat_base_ex_alt",
                                                           "beat_base_ex_alt_usd", "oos_beat_base_ex_alt_usd")},
                              "rule": rule(r, ex_base_summ) if vk != "base" else None}
    xb0 = cc["variants"]["base"]
    print(f"     база без них: прод P&L {xb0['prod']['pnl']:+.0f}$ (−альты {xb0['prod']['ex_alt']:+.0f}$), "
          f"{J.SEEDS} порядков P&L {xb0['random']['pnl_mean']:+.0f}$, −альты на $ {pct(xb0['random']['ex_alt_per_usd'])}")
    print(f"     варианты без них, {J.SEEDS} порядков (Δ к базе без них | с ними): P&L, −альты на $; лучше базы "
          f"P&L / −альты на $; правило 1–4")
    for vk in TAIL_VARIANTS[1:]:
        x, xb = cc["variants"][vk]["random"], xb0["random"]
        w_ = ob["variants"][vk]["random"]
        ru = cc["variants"][vk]["rule"]
        flags = "".join("+" if ru[k] else "·" for k in ("c1_pnl", "c2_ex_alt_usd", "c3_cycles", "c4_oos"))
        print(f"     {next(v['label'] for v in VARIANTS if v['key'] == vk):38} P&L {x['pnl_mean']:>+6.0f}$ "
              f"(Δ {x['pnl_mean'] - xb['pnl_mean']:+.0f} | {w_['pnl_mean'] - rb['pnl_mean']:+.0f}), −альты на $ "
              f"{pct(x['ex_alt_per_usd'])} (Δ {(x['ex_alt_per_usd'] - xb['ex_alt_per_usd']) * 100:+.1f} | "
              f"{(w_['ex_alt_per_usd'] - rb['ex_alt_per_usd']) * 100:+.1f} п.п.); "
              f"{x['beat_base_pnl']}/{x['beat_base_ex_alt_usd']}; {flags}")
    diag["c"] = cc

    # ---- d) лестница по корзинам
    pairs_sig = [(ep, sim(ep)) for ep in sig if G.key_of(ep) in y_pct]
    dd = {"signals": ladder_table(pairs_sig), "base": ladder_table(base_taken),
          "spearman": {}}
    lst = [(bounce(ep), ladder_stats(ep, r), r) for ep, r in pairs_sig]
    for nm, f_ in (("n_buys", lambda s_, r: s_["n_buys"]), ("spent", lambda s_, r: r["spent"]),
                   ("avg_low", lambda s_, r: s_["avg_low"]), ("span", lambda s_, r: max(r["span"], 1)),
                   ("pnl_usd", lambda s_, r: r["pnl"]), ("pnl_pct", lambda s_, r: r["pnl"] / r["spent"])):
        xx = [(bv, f_(s_, r)) for bv, s_, r in lst if f_(s_, r) is not None]
        dd["spearman"][nm] = G.spearman([a_ for a_, _ in xx], [b_ for _, b_ in xx])
    bad = sum(x["bad"] for x in dd["signals"].values() if x["n"])
    assert bad == 0, f"лестница восстановлена неверно у {bad} сигналов"
    print("  d) лестница по корзинам отскока (все сигналы с исходом; восстановление плана сверено: 0 расхождений)")
    print(f"     {'корзина':8} {'сигн.':>6} {'ступ.':>5} {'влож.$':>6} {'1-я/лоу':>7} {'ср.вход/лоу':>11} "
          f"{'дней':>5} {'стоп':>5} {'win':>5} {'P&L/влож.':>9} {'мед.%':>7} {'P&L $':>6} {'на $50':>7} "
          f"{'$/место-год':>11} | позиции прода: n, P&L/влож., P&L $")
    for nm in [x[0] for x in BUCKETS]:
        x, y_ = dd["signals"][nm], dd["base"][nm]
        if not x["n"]:
            continue
        print(f"     {nm:8} {x['n']:>6} {x['n_buys']:>5.2f} {x['spent']:>6.1f} {x['p0_low']:>7.3f} "
              f"{x['avg_low']:>11.3f} {x['span']:>5.0f} {x['stop']:>5.0%} {x['win']:>5.0%} "
              f"{pct(x['pnl_per_usd'], 9)} {pct(x['ret_med'], 7)} {x['pnl']:>+6.2f} {pct(x['pnl_per_budget'], 7)} "
              f"{x['pnl_slot_year']:>+11.2f} | "
              + (f"{y_['n']:>3} {pct(y_['pnl_per_usd'], 7)} {y_['pnl']:>+7.2f}" if y_["n"] else "  —"))
    print("     Спирмен отскока по сигналам: " + ", ".join(f"{k} {v:+.3f}" for k, v in dd["spearman"].items()))
    diag["d"] = dd

    # ---- e) корреляция сигналов: первые сигналы серий
    first = [ep for ep in sig if ser[G.key_of(ep)][1] == ep["day"]]
    ee = {"n_signals": len(sig), "n_series": len(first),
          "first_day_pct": ic_summary(group_ics(first, y_pct, by_day)),
          "first_day_usd": ic_summary(group_ics(first, y_usd, by_day)),
          "first_month_pct": ic_summary(group_ics(first, y_pct, by_month), blocks=False),
          "first_month_usd": ic_summary(group_ics(first, y_usd, by_month), blocks=False),
          "first_pooled_pct": pooled(first, y_pct), "first_pooled_usd": pooled(first, y_usd),
          "all_month_pct": ic_summary(group_ics(sig, y_pct, by_month), blocks=False),
          # та же монета в разные дни своей серии: вход позже / выше лоу против раньше / у лоу
          "series_pct": ic_summary(group_ics(sig, y_pct, lambda ep: ser[G.key_of(ep)]), blocks=False),
          "series_usd": ic_summary(group_ics(sig, y_usd, lambda ep: ser[G.key_of(ep)]), blocks=False)}
    ap = bb["all_pct"]
    print(f"  e) серий (90 дн. от первого сигнала) {len(first)} на {len(sig)} сигналов (в среднем "
          f"{len(sig) / len(first):.1f} сигнала на серию); все сигналы, IC по дням {ap['ic']:+.3f}: t наивный "
          f"{ap['t']:+.1f} (дней {ap['n']}), по месячным блокам {ap['t_month']:+.1f} (мес. {ap['n_months']}), "
          f"Ньюи–Уэст {ap['t_nw']:+.1f}; автокорреляция IC соседних дней {ap['ar1']:+.2f}")
    print(f"     первые сигналы серий: внутри дня % {fic(ee['first_day_pct'])}, $ {fic(ee['first_day_usd'])}; "
          f"внутри месяца % {fic(ee['first_month_pct'])}, $ {fic(ee['first_month_usd'])}; без деления % "
          f"{ee['first_pooled_pct']:+.3f}, $ {ee['first_pooled_usd']:+.3f}; все сигналы внутри месяца % "
          f"{fic(ee['all_month_pct'])}")
    print(f"     внутри серии одной монеты (дни серии, ≥ {M.MIN_DAY_SIGNALS}): % {fic(ee['series_pct'])}, $ "
          f"{fic(ee['series_usd'])}; по циклам %: " + ", ".join(
              f"{c} {fic(ee['series_pct']['cycles'][c])}" for c in CYC))
    diag["e"] = ee
    ob["diag"] = diag
    print(f"  [{time.time() - t0:.0f} с]")
    return ob


# ---------------------------------------------------------------- main

def analyze(D: dict, t0: float) -> dict:
    src = D["src"]
    base_take, _illiq = P.EXEC_POLICY
    sig = [ep for ep in src if base_take(ep)]
    bb = sorted(bounce(ep) for ep in sig if bounce(ep) is not None)
    thr95 = quantile(bb, Q_TAIL)
    roll = rolling_thresholds(sig)
    rv = [t for t in roll.values() if t is not None]
    ser = series_of(sig)
    dist: dict = {"n": len(sig), "days": len({ep["day"] for ep in sig}), "unknown": len(sig) - len(bb),
                  "quantiles": {f"q{round(q * 100)}": quantile(bb, q) for q in
                                (0.05, 0.10, 0.25, 0.50, 0.75, 0.80, 0.90, 0.95, 0.99)},
                  "share_le": {f"{x:.0%}": sum(1 for v in bb if v <= x + EPS) / len(bb) for x in GATES + (thr95,)},
                  "cycles": {}, "years": {}}
    for c in CYC:
        xs = sorted(bounce(ep) for ep in sig if ep["cycle"] == c)
        dist["cycles"][c] = {"n": len(xs), "q50": quantile(xs, 0.5), "q80": quantile(xs, 0.8),
                             "q95": quantile(xs, 0.95), "gt20": sum(1 for v in xs if v > QUOTA_THR + EPS) / len(xs)}
    for ep in sig:
        dist["years"].setdefault(date_of(ep["day"])[:4], []).append(bounce(ep))
    dist["years"] = {y: {"n": len(v), "q50": quantile(sorted(v), 0.5), "q80": quantile(sorted(v), 0.8),
                         "gt20": sum(1 for x in v if x > QUOTA_THR + EPS) / len(v)}
                     for y, v in sorted(dist["years"].items())}
    dist["roll80"] = {"months": len(roll), "no_threshold": sum(1 for t in roll.values() if t is None),
                      "min": min(rv), "mean": st.fmean(rv), "max": max(rv)}
    q = dist["quantiles"]
    print(f"\nСигналы, прошедшие фильтры прода: {len(sig)} (дней {dist['days']}); от лоу базы: q10 {pct(q['q10'])}, "
          f"q25 {pct(q['q25'])}, медиана {pct(q['q50'])}, q75 {pct(q['q75'])}, q90 {pct(q['q90'])}, q95 "
          f"{pct(q['q95'])}, q99 {pct(q['q99'])}")
    print("  доля сигналов ≤ порога: " + ", ".join(f"{k} {v:.0%}" for k, v in dist["share_le"].items()))
    print("  по циклам (мед. / q80 / q95 / доля > 20%): " + "; ".join(
        f"{c} {pct(x['q50'])} / {pct(x['q80'])} / {pct(x['q95'])} / {x['gt20']:.0%}" for c, x in dist["cycles"].items()))
    print("  по годам доля > 20%: " + ", ".join(f"{y} {x['gt20']:.0%}" for y, x in dist["years"].items()))
    print(f"  скользящий q80 прошлых 365 дн.: месяцев {dist['roll80']['months']} (без порога "
          f"{dist['roll80']['no_threshold']}), порог {pct(dist['roll80']['min'])} … {pct(dist['roll80']['max'])}, "
          f"ср. {pct(dist['roll80']['mean'])}; q95 всей истории {pct(thr95)}")

    VARIANTS = variants(thr95, roll)
    out: dict = {"meta": {"seeds": J.SEEDS, "slots": J.SLOTS, "budget": J.BUDGET, "is_cycles": IS_CYCLES,
                          "oos_cycle": OOS_CYCLE, "archive_last_day": D["last_day"], "gates": GATES,
                          "q_tail": Q_TAIL, "thr95": thr95, "q_roll": Q_ROLL, "roll_days": ROLL_DAYS,
                          "roll_min": ROLL_MIN, "flag": FLAG, "quota_thr": QUOTA_THR, "quotas": QUOTAS,
                          "tail_n": TAIL_N, "series_days": SERIES_DAYS, "nw_lag": NW_LAG,
                          "oos_min_beat": OOS_MIN_BEAT, "min_day_signals": M.MIN_DAY_SIGNALS,
                          "variants": {v["key"]: v["label"] for v in VARIANTS}, "families": FAMILIES,
                          "tail_variants": TAIL_VARIANTS},
                 "signals": dist}
    for b, (sell, _stop) in J.BOOKS.items():
        out[b] = analyze_book(b, sell, D, sig, VARIANTS, ser, t0)
    print()
    for b in J.BOOKS:
        print(f"ИТОГ {b}: правило решения (1–4) прошли — {', '.join(out[b]['rule_passed']) or 'ни один вариант'}")
    out["runtime_s"] = time.time() - t0
    return out


def main() -> int:
    t0 = time.time()
    D = load()
    out = analyze(D, t0)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
