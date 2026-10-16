"""«Ядро + спутник»: X% денег в ядро (BTC / корзина топ-20 / BTC+ETH) + остальное в книгу (Q15).

Вопрос владельца (16.10.2026): книги R и H на истории не лучше BTC и корзины альтов ни в
одном порядке сигналов (Q4). Как выглядит портфель «X% в ядро + (100 − X)% в книгу» на тех
же сигналах — P&L, P&L на $, просадка, по циклам? Только цифры: доля X за владельцем.

Книги — как в portfolio_vs_market, шаг s6 (exec_book: 15 мест, лестница $50, кулдаун 90 дн.,
свободный USDT $1500 с реинвестом выручки, нижняя четверть по обороту и перегрев — не
покупаются, квота неликвида 5 мест, детектор прода, без горизонта, EXEC_ALL), по рангу и при
J.SEEDS случайных порядках сигналов внутри дня. Сверка: X = 0 повторяет s6 из
portfolio_vs_market_results.json (R +$1503 / H +$1903).

Ядра:
  btc     — BTC (закрытия BTCUSDT Binance);
  top20   — корзина топ-20 альтов по капе CMC с равными весами из Q4
            (top20_basket_study.basket_level «eq»: без BTC/стейблов/обёрток, ребаланс в первое
            воскресенье месяца, 0.15% на оборот; в 2018 корзина неполная — 5–17 монет);
  btceth  — BTC 50% + ETH 50%, ребаланс к 50/50 в те же первые воскресенья, 0.15% на оборот
            (тот же basket_level); справочно btceth_cap — веса по капе CMC BTC и ETH на снимке
            (BTC 63–90%, медиана 81%), ребаланс так же.
Комиссия 0.15% на каждую покупку и продажу ядра (как бенчмарк по потокам) плюс оборот
ребаланса внутри корзин.

Как покупается ядро (k = X / (100 − X) $ ядра на каждый $ покупок книги; «вложено» = сумма
всех покупок, как у книги: книга реинвестирует выручку, поэтому её вложено > $1500):
  flows   — зеркало потоков книги: каждая ступень книги покупает k × ту же сумму ядра в тот же
            день, продажа книги продаёт ту же долю ядра этой позиции, остаток — по уровню дня
            выхода позиции (scanner.benchmark.flow_pnl ×k). При X = 100 это ровно бенчмарк по
            потокам из portfolio_vs_market (сверка);
  hold    — те же покупки, что flows, но ядро не продаётся до конца архива (так обычно и держат
            ядро); вложено то же, но деньги работают дольше — справочно к flows;
  dca     — раз в месяц равными суммами: та же сумма k × Σ покупок книги (своего порядка
            сигналов) делится поровну на M дат — день первой покупки книги и 1-е числа
            следующих месяцев до месяца последней покупки книги; держать до конца архива.
X = 0 — только книга, X = 100 — только ядро (вложено = Σ покупок книги × 1), справочно.

Как смешиваем и меряем. Портфель = счёт книги + ядро. Стоимость по дням: книга — $1500 +
накопленный P&L позиций (выручка и свободный USDT — внутри счёта; позиции — монеты × закрытие
дня за вычетом комиссии выхода 0.15%; после выхода позиции её итог заморожен), ядро — единицы
× уровень дня × (1 − 0.15%). Внешние потоки: +$1500 книги в первый день сигнала, +покупки
ядра, −выручка продаж ядра (flows). P&L = итоговая стоимость − Σ внешних потоков (совпадает
с Σ P&L позиций книги + P&L ядра); «итог» = вложено + P&L. P&L на $ = P&L / вложено (книга +
ядро). Просадка — по дневному NAV с учётом потоков (time-weighted: взнос не «поднимает»
стоимость), макс. падение от пика, %; справочно — макс. падение кривой P&L, $. IRR (MWR) —
годовая ставка по внешним потокам и итоговой стоимости (только порядок по рангу). По циклам
(ladder_dca_study.CYCLES: 2018-07-15 — первый сигнал прода …2020-03 / 2020-04…2022-12 /
2023-01…конец архива 2026-09-28) — окно по времени: P&L в окне, доходность NAV в
окне (это «на $» цикла: что стал $1 портфеля на начало окна) и просадка внутри окна.
Доля ядра — средняя по дням доля ядра в стоимости.
Количество монет позиции по дням восстанавливается копией L.simulate_exec, которая
дополнительно отдаёт количество каждой покупки (исходник не меняется; сверка: P&L каждой
позиции и итоговая оценка совпадают с оригиналом).

Оговорки: 3–4 рыночных цикла — мало; рост BTC 2018–21 доминирует в любом варианте с ядром
BTC; корзина топ-20 в 2018 неполная (5–17 монет); выжившие — архив только Binance, книга на
выживших монетах (Q6/Q9: цифры книги — верхняя граница); доля X по покупкам ≠ доля по
стоимости (книга держит $1500 и в основном USDT, ядро hold/dca копит монеты — см. долю ядра);
просадка книги мала, потому что счёт в основном в USDT; это не рекомендация доли.

Запуск из корня проекта:  py -3 backtest/core_satellite_study.py
Результат: backtest/core_satellite_results.json
"""
from __future__ import annotations

import datetime as dt
import inspect
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
import top20_basket_study as T  # noqa: E402
from scanner.benchmark import flow_pnl  # noqa: E402

OUT = Path(__file__).resolve().parent / "core_satellite_results.json"
DAY = 86400
FEE = 0.0015
XS = [0, 50, 70, 90, 100]
CORES = [("btc", "BTC"), ("top20", "корзина топ-20"), ("btceth", "BTC+ETH 50/50"),
         ("btceth_cap", "BTC+ETH по капе")]
METHODS = [("flows", "те же суммы, зеркало"), ("hold", "те же покупки, держать"),
           ("dca", "DCA раз в месяц")]


# ---------------------------------------------------------------- симуляция с количеством

def _sim_with_qty():
    """Копия L.simulate_exec, которая дополнительно отдаёт buy_q — количество монет каждой
    покупки (в порядке flows «Buy»). Исходник не меняется; результат сверяется с оригиналом."""
    src = inspect.getsource(L.simulate_exec)
    reps = [('flows.append((d, "Buy", od["usd"], 0.0))',
             'flows.append((d, "Buy", od["usd"], 0.0)); buy_q.append(q)'),
            ("cash: list[tuple[int, float]] = [", "buy_q: list[float] = []\n    cash: list[tuple[int, float]] = ["),
            ('"flows": flows, "cash": cash, "mark": mark}',
             '"flows": flows, "cash": cash, "mark": mark, "buy_q": buy_q}')]
    for a, b in reps:
        assert src.count(a) == 1, f"simulate_exec изменился: нет «{a}»"
        src = src.replace(a, b)
    ns = dict(vars(L))
    exec(compile(src, "<simulate_exec_q>", "exec"), ns)  # noqa: S102
    return ns["simulate_exec"]


SIM_Q = _sim_with_qty()


# ---------------------------------------------------------------- уровни ядер

def dense(series: dict[int, float], days: list[int]) -> list[float | None]:
    """Уровень на каждый день сетки, пропуски — прошлым значением."""
    out, cur = [], None
    for d in days:
        cur = series.get(d, cur)
        out.append(cur)
    return out


def btceth_levels(px: T.Prices, comps: list[dict], last_day: int) -> dict[str, dict]:
    """BTC+ETH 50/50 и по капе CMC с ребалансом в даты корзины (basket_level, 0.15% оборот)."""
    def seg_of(base: str, day: int) -> str:
        for s in px.by_base[base]:
            if s["ts"][0] <= day <= s["ts"][-1]:
                return s["sym"]
        raise RuntimeError(f"{base}: нет отрезка на {day}")
    cc = []
    for c in comps:
        rows = {x["symbol"]: x for x in T.snapshot(dt.date.fromisoformat(c["date"]))
                if x["symbol"] in ("BTC", "ETH")}
        cc.append({"day": c["day"], "date": c["date"],
                   "basket": [{"sym": seg_of(b, c["day"]), "mcap": rows[b]["mcap"]}
                              for b in ("BTC", "ETH")]})
    eq, _ = T.basket_level(px, cc, "eq", last_day)
    cap, _ = T.basket_level(px, cc, "cap", last_day)
    w = [c["basket"][0]["mcap"] / (c["basket"][0]["mcap"] + c["basket"][1]["mcap"]) for c in cc]
    return {"btceth": eq, "btceth_cap": cap,
            "cap_btc_weight": [min(w), statistics.median(w), max(w)]}


# ---------------------------------------------------------------- книга и ядро по дням

class Grid:
    def __init__(self, d0: int, d1: int):
        self.d0, self.n = d0, (d1 - d0) // DAY + 1
        self.days = [d0 + i * DAY for i in range(self.n)]

    def gi(self, ts: int) -> int:
        return (ts - self.d0) // DAY


def position_days(ep: dict, r: dict, g: Grid) -> dict:
    """P&L позиции нарастающим итогом по дням (дельты на сетке) и покупки по дням.
    Стоимость дня = выручка − траты + монеты × закрытие × (1 − TAKER)."""
    seg, ts, c = ep["seg"], ep["seg"]["ts"], ep["seg"]["c"]
    by_d: dict[int, list] = {}
    qi = 0
    for d, sd, usd, fr in r["flows"]:
        q = None
        if sd == "Buy":
            q = r["buy_q"][qi]
            qi += 1
        by_d.setdefault(d, []).append((sd, usd, fr, q))
    held = spent = back = 0.0
    prev = 0.0
    deltas, buys = [], []
    for d in range(ep["e"] + 1, r["exit_d"] + 1):
        for sd, usd, fr, q in by_d.get(d, []):
            if sd == "Buy":
                held += q
                spent += usd
                buys.append((g.gi(ts[d]), usd))
            else:
                held -= held * min(max(fr, 0.0), 1.0)
                back += usd
        v = back - spent + held * c[d] * (1 - L.TAKER)
        if v != prev:
            deltas.append((g.gi(ts[d]), v - prev))
            prev = v
    assert abs(prev - r["pnl"]) < 1e-6, f"{seg['sym']}: P&L по дням {prev} ≠ {r['pnl']}"
    return {"deltas": deltas, "buys": buys}


def core_mirror(ep: dict, r: dict, level: dict, g: Grid) -> dict | None:
    """Ядро flows для одной позиции, k = 1: единицы и внешние потоки по дням (flow_pnl)."""
    ts = ep["seg"]["ts"]
    d1 = ep["day"] + max(r["span"], 1) * DAY
    u = 0.0
    du, fl = [], []
    for d, sd, usd, fr in r["flows"]:
        lv = P.at(level, ts[d])
        if not lv:
            return None
        if sd == "Buy":
            x = usd * (1 - FEE) / lv
            u += x
            du.append((g.gi(ts[d]), x))
            fl.append((g.gi(ts[d]), usd))
        else:
            sold = u * min(max(fr, 0.0), 1.0)
            u -= sold
            du.append((g.gi(ts[d]), -sold))
            fl.append((g.gi(ts[d]), -sold * lv * (1 - FEE)))
    if u > 0:
        lv = P.at(level, d1)
        if not lv:
            return None
        du.append((g.gi(d1), -u))
        fl.append((g.gi(d1), -u * lv * (1 - FEE)))
    return {"du": du, "fl": fl}


def core_hold(ep: dict, r: dict, level: dict, g: Grid) -> dict | None:
    ts = ep["seg"]["ts"]
    du, fl = [], []
    for d, sd, usd, _fr in r["flows"]:
        if sd != "Buy":
            continue
        lv = P.at(level, ts[d])
        if not lv:
            return None
        du.append((g.gi(ts[d]), usd * (1 - FEE) / lv))
        fl.append((g.gi(ts[d]), usd))
    return {"du": du, "fl": fl}


def dca_dates(first: int, last: int) -> list[int]:
    a = dt.datetime.fromtimestamp(first, dt.timezone.utc).date()
    b = dt.datetime.fromtimestamp(last, dt.timezone.utc).date()
    out = [first]
    y, m = a.year, a.month
    while True:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        if (y, m) > (b.year, b.month):
            break
        out.append(T.day_ts(dt.date(y, m, 1)))
    return out


def accumulate(n: int, items) -> list[float]:
    arr = [0.0] * n
    for i, v in items:
        arr[i] += v
    return arr


def cumsum(a: list[float]) -> list[float]:
    out, s = [], 0.0
    for v in a:
        s += v
        out.append(s)
    return out


def portfolio(vb, fb, sb, vc, fc, sc, k, g: Grid, irr: bool) -> dict:
    """Метрики смеси книга + k × ядро; k = None — только ядро (×1), k = 0 — только книга."""
    if k is None:
        V = vc
        F = fc
        S = sc
    else:
        V = [a + k * b for a, b in zip(vb, vc)]
        F = [a + k * b for a, b in zip(fb, fc)]
        S = [a + k * b for a, b in zip(sb, sc)]
    n = g.n
    nav = [0.0] * n
    pl = [0.0] * n
    cur, vprev, csum = 1.0, 0.0, 0.0
    peak, mdd = 1.0, 0.0
    plpeak, mdd_usd = 0.0, 0.0
    share_sum, share_n = 0.0, 0
    for i in range(n):
        csum += F[i]
        if vprev > 1e-9:
            cur *= (V[i] - F[i]) / vprev
        nav[i] = cur
        if cur > peak:
            peak = cur
        elif 1 - cur / peak > mdd:
            mdd = 1 - cur / peak
        p = V[i] - csum
        pl[i] = p
        if p > plpeak:
            plpeak = p
        elif plpeak - p > mdd_usd:
            mdd_usd = plpeak - p
        if V[i] > 1e-9 and k is not None:
            share_sum += k * vc[i] / V[i]
            share_n += 1
        vprev = V[i]
    spent = sum(S)
    out = {"spent": spent, "pnl": pl[-1], "final": spent + pl[-1],
           "pnl_per_usd": pl[-1] / spent if spent else 0.0, "mdd_pct": mdd, "mdd_usd": mdd_usd,
           "core_share": (share_sum / share_n if share_n else 0.0) if k is not None else 1.0}
    cyc = {}
    for c, a, b in L.CYCLES:
        ia = max(0, g.gi(a) if a >= g.d0 else 0)
        ib = min(n - 1, g.gi(b) - 1)
        if ib <= ia:
            continue
        n0 = nav[ia - 1] if ia > 0 else 1.0
        p0 = pl[ia - 1] if ia > 0 else 0.0
        pk, md = n0, 0.0
        for i in range(ia, ib + 1):
            if nav[i] > pk:
                pk = nav[i]
            elif pk > 0 and 1 - nav[i] / pk > md:
                md = 1 - nav[i] / pk
        cyc[c] = {"pnl": pl[ib] - p0, "spent": sum(S[ia:ib + 1]),
                  "twr": nav[ib] / n0 - 1 if n0 else 0.0, "mdd_pct": md}
    out["cycles"] = cyc
    if irr:
        cf = [-f for f in F]
        cf[-1] += V[-1]
        out["irr"] = xirr(cf)
    return out


def xirr(cf: list[float]) -> float | None:
    """Годовая ставка r: Σ cf_i / (1 + r)^(i/365) = 0, бисекция."""
    pts = [(i / 365, v) for i, v in enumerate(cf) if v]

    def npv(r):
        return sum(v / (1 + r) ** t for t, v in pts)
    lo, hi = -0.99, 10.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def run(taken, g: Grid, levels: dict, dense_lv: dict, pos_cache: dict, core_cache: dict,
        irr: bool) -> dict:
    """Все варианты одного прогона книги. -> {"book": метрики X=0, core: {method: {X: …}}}."""
    n = g.n
    deltas, buys = [], []
    tk = []
    for ep, r in taken:
        if r["spent"] <= 0:
            continue
        key = (ep["seg"]["sym"], ep["e"])
        if key not in pos_cache:
            pos_cache[key] = position_days(ep, r, g)
        pd = pos_cache[key]
        deltas += pd["deltas"]
        buys += pd["buys"]
        tk.append((key, ep, r))
    pnl_cum = cumsum(accumulate(n, deltas))
    vb = [P.CAPITAL + x for x in pnl_cum]
    fb = [0.0] * n
    fb[0] = P.CAPITAL
    sb = accumulate(n, buys)
    book_spent = sum(sb)
    first = min(i for i, v in enumerate(sb) if v)
    last = max(i for i, v in enumerate(sb) if v)
    out: dict = {"book": portfolio(vb, fb, sb, [0.0] * n, [0.0] * n, [0.0] * n, 0.0, g, irr),
                 "dropped": {}}
    for core, _cn in CORES:
        lvl, lvd = levels[core], dense_lv[core]
        out[core] = {}
        for meth, _mn in METHODS:
            dropped = 0
            du_all, fl_all = [], []
            if meth == "dca":
                dates = dca_dates(g.days[first], g.days[last])
                amt = book_spent / len(dates)
                for d in dates:
                    lv = P.at(lvl, d)
                    if not lv:
                        dropped += 1
                        continue
                    du_all.append((g.gi(d), amt * (1 - FEE) / lv))
                    fl_all.append((g.gi(d), amt))
            else:
                fn = core_mirror if meth == "flows" else core_hold
                for key, ep, r in tk:
                    ck = (core, meth) + key
                    if ck not in core_cache:
                        core_cache[ck] = fn(ep, r, lvl, g)
                    x = core_cache[ck]
                    if x is None:
                        dropped += 1
                        continue
                    du_all += x["du"]
                    fl_all += x["fl"]
            U = cumsum(accumulate(n, du_all))
            vc = [u * lv * (1 - FEE) if u > 1e-15 and lv else 0.0 for u, lv in zip(U, lvd)]
            fc = accumulate(n, fl_all)
            sc = accumulate(n, ((i, v) for i, v in fl_all if v > 0))
            out["dropped"][f"{core}/{meth}"] = dropped
            out[core][meth] = {
                str(x): portfolio(vb, fb, sb, vc, fc, sc,
                                  None if x == 100 else x / (100 - x), g, irr)
                for x in XS if x > 0}
    return out


def summarize(runs: list[dict]) -> dict:
    """Среднее и разброс по прогонам: P&L, на $, просадка, циклы."""
    def agg(get):
        v = [get(r) for r in runs]
        return {"mean": statistics.fmean(v), "min": min(v), "max": max(v)}

    def block(path):
        def g(r):
            x = r
            for p in path:
                x = x[p]
            return x
        m = {k: agg(lambda r, k=k: g(r)[k]) for k in ("spent", "pnl", "pnl_per_usd", "mdd_pct",
                                                       "mdd_usd", "core_share")}
        m["cycles"] = {c: {k: agg(lambda r, c=c, k=k: g(r)["cycles"][c][k])
                           for k in ("pnl", "twr", "mdd_pct")}
                       for c in g(runs[0])["cycles"]}
        return m
    out = {"book": block(["book"])}
    for core, _ in CORES:
        out[core] = {meth: {str(x): block([core, meth, str(x)]) for x in XS if x > 0}
                     for meth, _ in METHODS}
    return out


def pct(v):
    return f"{v * 100:+.0f}%"


def main() -> int:
    t0 = time.time()
    coins = L.load_archive()
    all_segs = L.segments(coins)
    px = T.Prices(all_segs)
    btc = P.btc_closes(all_segs)
    last_day = max(btc)
    comps = T.compositions(px, last_day)
    top20, _ = T.basket_level(px, comps, "eq", last_day)
    be = btceth_levels(px, comps, last_day)
    levels = {"btc": btc, "top20": top20, "btceth": be["btceth"], "btceth_cap": be["btceth_cap"]}
    print(f"архив до {dt.datetime.fromtimestamp(last_day, dt.timezone.utc).date()}, корзина "
          f"{comps[0]['date']}…{comps[-1]['date']}, вес BTC в BTC+ETH по капе "
          f"{be['cap_btc_weight'][0] * 100:.0f}…{be['cap_btc_weight'][2] * 100:.0f}% "
          f"(медиана {be['cap_btc_weight'][1] * 100:.0f}%)  [{time.time() - t0:.0f} с]")

    alt, _ = P.market_index(ROOT / "scanner.db")
    ranks = L.vol_ranks(all_segs)
    npairs = P.pairs_per_day(ranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot = L.load_hot()
    src = [ep for ep in L.signal_days(segs, ranks, hot, {"prod": L.is_spring_prod})["prod"]
           if ep["cycle"] != "?"]
    for ep in src:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
    g = Grid(min(ep["day"] for ep in src), last_day)
    dense_lv = {k: dense(v, g.days) for k, v in levels.items()}
    ref = json.loads((Path(__file__).resolve().parent / "portfolio_vs_market_results.json")
                     .read_text(encoding="utf-8"))["exec"]
    ref_q4 = json.loads((Path(__file__).resolve().parent / "top20_basket_results.json")
                        .read_text(encoding="utf-8"))
    print(f"дней сигнала прода {len(src)}, сетка {g.n} дней с "
          f"{dt.datetime.fromtimestamp(g.d0, dt.timezone.utc).date()}  [{time.time() - t0:.0f} с]")

    out: dict = {"meta": {"fee": FEE, "xs": XS, "cores": dict(CORES), "methods": dict(METHODS),
                          "seeds": J.SEEDS, "capital": P.CAPITAL, "grid_start": g.d0,
                          "archive_last_day": last_day, "cycles": L.CYCLES,
                          "btceth_cap_btc_weight": be["cap_btc_weight"],
                          "growth_whole": {k: T.growth(v, g.d0, last_day)
                                           for k, v in levels.items()}},
                 "checks": {}}
    for b, (sell, _stop) in J.BOOKS.items():
        name = "R правила" if b == "R" else "H держать"
        sim_cache: dict = {}

        def sim(ep, sell=sell, cache=sim_cache):
            k = (ep["seg"]["sym"], ep["e"])
            if k not in cache:
                r = SIM_Q(ep["seg"], ep["e"], sell, budget=J.BUDGET, horizon=None,
                          rules=L.EXEC_ALL)
                if r is not None:
                    o = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET,
                                        horizon=None, rules=L.EXEC_ALL)
                    assert o["pnl"] == r["pnl"] and o["flows"] == r["flows"] and \
                        o["mark"] == r["mark"], f"копия simulate_exec разошлась: {k}"
                cache[k] = r
            return cache[k]
        pos_cache: dict = {}
        core_cache: dict = {}
        taken = P.exec_book(src, sim, *P.EXEC_POLICY, cooldown=True, cash=True)
        chk = P.evaluate_flows(taken, alt, btc)
        s6 = ref[b]["s6"]
        assert chk["n"] == s6["n"] and abs(chk["pnl"] - s6["pnl"]) < 1e-6 and \
            abs(chk["btc"] - s6["btc"]) < 1e-6, f"{b}: s6 не повторился ({chk['pnl']} / {s6['pnl']})"
        base = run(taken, g, levels, dense_lv, pos_cache, core_cache, irr=True)
        # сверки: X = 0 = s6; X = 100 flows = бенчмарк по потокам (BTC — s6, корзина — Q4)
        fp_top = sum(flow_pnl([{"ts": ep["seg"]["ts"][d], "side": sd, "usd": u, "frac": f}
                               for d, sd, u, f in r["flows"]],
                              lambda t: P.at(top20, t), ep["day"] + max(r["span"], 1) * DAY, FEE)
                     for ep, r in taken if r["spent"] > 0)
        c = {"s6_pnl": s6["pnl"], "x0_pnl": base["book"]["pnl"],
             "s6_btc_flows": s6["btc"], "x100_btc_flows": base["btc"]["flows"]["100"]["pnl"],
             "q4_top20_flows": ref_q4[b]["eq"], "x100_top20_flows": base["top20"]["flows"]["100"]["pnl"],
             "flow_pnl_top20_direct": fp_top, "book_spent": base["book"]["spent"],
             "s6_spent": s6["spent"], "dropped": base["dropped"]}
        assert abs(c["x0_pnl"] - s6["pnl"]) < 1e-6, f"{b}: X=0 {c['x0_pnl']} ≠ s6 {s6['pnl']}"
        assert abs(c["x100_btc_flows"] - s6["btc"]) < 1e-6, f"{b}: X=100 BTC ≠ бенчмарк"
        assert abs(c["x100_top20_flows"] - fp_top) < 1e-6, f"{b}: X=100 корзина ≠ flow_pnl"
        out["checks"][b] = c
        runs = []
        for sd in range(J.SEEDS):
            tk = P.exec_book(src, sim, *P.EXEC_POLICY, cooldown=True, cash=True,
                             order="random", seed=sd)
            runs.append(run(tk, g, levels, dense_lv, pos_cache, core_cache, irr=False))
        out[b] = {"rank": base, "random": summarize(runs)}
        print(f"\n=== книга {name}: вложено {base['book']['spent']:.0f}$, P&L "
              f"{base['book']['pnl']:+.0f}$ (s6 {s6['pnl']:+.0f}$), просадка счёта "
              f"{base['book']['mdd_pct'] * 100:.1f}%, IRR {base['book']['irr'] * 100:+.1f}%; "
              f"X=100 BTC зеркало {c['x100_btc_flows']:+.0f}$ (s6 {s6['btc']:+.0f}$), корзина "
              f"{c['x100_top20_flows']:+.0f}$ (Q4 {ref_q4[b]['eq']:+.0f}$)  [{time.time() - t0:.0f} с]")
        ro = out[b]["random"]
        print(f"  {'ядро / способ':36} " + " | ".join(f"X={x:<3} P&L    на $  просад" for x in XS))
        bk, bkr = base["book"], ro["book"]
        for core, cn in CORES:
            for meth, mn in METHODS:
                cells = [f"{bk['pnl']:>+6.0f}$ {pct(bk['pnl_per_usd']):>5} {bk['mdd_pct'] * 100:>5.1f}%"]
                for x in XS[1:]:
                    m = base[core][meth][str(x)]
                    cells.append(f"{m['pnl']:>+6.0f}$ {pct(m['pnl_per_usd']):>5} "
                                 f"{m['mdd_pct'] * 100:>5.1f}%")
                print(f"  {cn + ', ' + mn:36} " + " | ".join(cells))
        print(f"  {J.SEEDS} порядков, на $ среднее (мин…макс) и просадка среднее:")
        print(f"    книга: {pct(bkr['pnl_per_usd']['mean'])} ({pct(bkr['pnl_per_usd']['min'])}…"
              f"{pct(bkr['pnl_per_usd']['max'])}), просадка {bkr['mdd_pct']['mean'] * 100:.1f}%")
        for core, cn in CORES:
            for meth, mn in METHODS:
                print(f"    {cn + ', ' + mn:36} " + "  ".join(
                    f"X={x}: {pct(ro[core][meth][str(x)]['pnl_per_usd']['mean'])} "
                    f"({pct(ro[core][meth][str(x)]['pnl_per_usd']['min'])}…"
                    f"{pct(ro[core][meth][str(x)]['pnl_per_usd']['max'])}) "
                    f"{ro[core][meth][str(x)]['mdd_pct']['mean'] * 100:.0f}%"
                    for x in XS[1:]))
        print("  по циклам (по рангу): доходность NAV в окне / просадка в окне; X = 0 | 50 | 70 | 90 | 100")
        for c_, _a, _b in L.CYCLES:
            bc = bk["cycles"].get(c_)
            if not bc:
                continue
            print(f"    {c_}: книга {pct(bc['twr'])}/{bc['mdd_pct'] * 100:.0f}% (P&L {bc['pnl']:+.0f}$)")
            for core, cn in CORES[:3]:
                for meth, mn in METHODS:
                    print(f"      {cn + ', ' + mn:36} " + "  ".join(
                        f"{pct(base[core][meth][str(x)]['cycles'][c_]['twr'])}/"
                        f"{base[core][meth][str(x)]['cycles'][c_]['mdd_pct'] * 100:.0f}%"
                        for x in XS[1:]))
        print("  доля ядра в стоимости (средняя по дням), X = 50 | 70 | 90; IRR X = 0 | 50 | 70 | 90 | 100:")
        for core, cn in CORES[:3]:
            for meth, mn in METHODS:
                v = base[core][meth]
                print(f"    {cn + ', ' + mn:36} " + " ".join(
                    f"{v[str(x)]['core_share'] * 100:>3.0f}%" for x in (50, 70, 90)) + "  | IRR "
                    + " ".join(f"{(m['irr'] or 0) * 100:+.1f}%"
                               for m in [bk] + [v[str(x)] for x in XS[1:]]))
    out["meta"]["seconds"] = time.time() - t0
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
