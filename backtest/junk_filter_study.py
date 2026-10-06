"""Отсев мусора перед автопокупкой: что срежет фильтр — мусор или прибыль и входы.

Вопрос (06.10.2026): пробный исполнитель (scanner/executor.py) покупает всё, что пришло
карточкой: зона дна, балл ≥ 70, пара на Bybit spot без метки ST. Стоит ли дополнительно
отсеивать «мусорные» монеты, или фильтр срежет вместе с мусором прибыль и входы?

Мусор здесь — не скам (его режут анти-раг и сам листинг на Bybit), а монеты, которые тихо
умирают: оборот утекает, биржа делистит. На истории видно только то, что было в архиве
Binance на день сигнала: ранг 30-дн. оборота среди всех пар дня, возраст листинга, просадка
от ATH, куда ранг ушёл за полгода; плюс перегрев рынка (прод-флаги, market_daily).

Вход как у исполнителя: 5 ступеней по $10 до пола прод-стопа (ladder_dca_study.simulate:
дневные свечи, комиссии, делистинг — выход по последнему закрытию), две книги:
  R — stage8_exit целиком (+50/+150 по трети, трейл 30% после +60%, стоп −25%);
  H — без частичных продаж и трейла, только стоп −25%.
Горизонт 365 дн.; эпизоды, у которых живой ряд кончился раньше горизонта, не берутся.

Замеры:
  1. корзины признаков — где смерти и где прибыль;
  2. фильтры — доля оставленных входов, медиана/средняя/p10, смерти, доля срезанных лучших
     исходов (верхний дециль всех пружин), сумма P&L срезанных; разница медиан по циклам;
  3. портфель с лимитом 15 монет (executor.max_coins) по $50: сигналы по дням, слот занят до
     выхода, одна позиция на монету — сколько входов и прибыли даёт каждое правило за 2018–2026;
  4. форвард v1: 18 монет paper-книги (июль–сентябрь 2026) — ранг оборота на день входа и итог.
Оговорки: архив только Binance (живой сканер берёт топ-1000 CoinGecko и покупает на Bybit —
мусора там больше, чем на Binance); пружина — исследовательский детектор, а не балл ≥ 70;
эпизоды коррелированы (пачки сигналов).

Запуск из корня проекта:  py -3 backtest/junk_filter_study.py
Результат: backtest/junk_filter_results.json
"""
from __future__ import annotations

import calendar
import json
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ladder_dca_study as L  # noqa: E402
from feature_study import features  # noqa: E402

OUT = Path(__file__).resolve().parent / "junk_filter_results.json"
DAY = 86400
BUDGET = 50.0                    # executor.budget_usdt: 5 ступеней по $10
SLOTS = 15                       # executor.max_coins
SEEDS = 30                       # порядок сигналов внутри дня в портфеле — случайный
HORIZON = 365
DRIFT_LAG = 180                  # ранг сейчас против ранга полгода назад
BUY5 = {"kind": "floor", "n": 5}
BOOKS = {"R": (L.SELL["прод +50/+150+трейл"], "prod"),
         "H": ({"levels": []}, "prod")}

BUCKETS = {
    "ранг 30-дн. оборота на Binance": ("rank", [(1, 50, "1–50"), (51, 150, "51–150"),
                                                (151, 300, "151–300"), (301, 10**9, "> 300")]),
    "возраст листинга": ("age", [(0, 364, "< 1 года"), (365, 729, "1–2 года"),
                                 (730, 1459, "2–4 года"), (1460, 10**9, "> 4 лет")]),
    "просадка от ATH": ("dd", [(0.70, 0.85, "70–85%"), (0.85, 0.94, "85–94%"),
                               (0.94, 1.01, "≥ 94%")]),
    "ранг сейчас / полгода назад": ("drift", [(0, 0.67, "оборот рос (×<0.67)"),
                                              (0.67, 1.5, "на месте"),
                                              (1.5, 2.0, "просел ×1.5–2"),
                                              (2.0, 10**9, "просел ×2+")]),
    "перегрев рынка на входе": ("hot", [(0, 0, "0 флагов"), (1, 99, "≥ 1 флага")]),
}

FILTERS = [
    ("все пружины", lambda ep: True),
    ("ранг ≤ 300 (срезать худшую корзину)", lambda ep: ep["rank"] <= 300),
    ("ранг ≤ 150", lambda ep: ep["rank"] <= 150),
    ("гейт трека Q: ранг ≤ 150 и ≥ 1 года", lambda ep: ep["rank"] <= 150 and ep["age"] >= 365),
    ("ранг ≤ 50", lambda ep: ep["rank"] <= 50),
    ("без просадки ≥ 94%", lambda ep: (ep["dd"] or 0) < 0.94),
    ("оборот не просел ×2 за полгода", lambda ep: ep["drift"] is None or ep["drift"] < 2),
    ("холодный рынок (0 флагов)", lambda ep: ep["hot"] == 0),
    ("ранг ≤ 300 и холодный рынок", lambda ep: ep["rank"] <= 300 and ep["hot"] == 0),
]

# Портфель: (имя, пропускает ли монету, порядок внутри дня, квота (условие, максимум слотов)).
POLICIES = [
    ("все подряд", lambda ep: True, "random", None),
    ("все, лучший ранг первым", lambda ep: True, "rank", None),
    ("ранг ≤ 300", lambda ep: ep["rank"] <= 300, "random", None),
    ("ранг ≤ 150", lambda ep: ep["rank"] <= 150, "random", None),
    ("гейт: ранг ≤ 150 и ≥ 1 года", lambda ep: ep["rank"] <= 150 and ep["age"] >= 365,
     "random", None),
    ("квота: ранг > 150 — не больше 5 слотов", lambda ep: True, "rank",
     (lambda ep: ep["rank"] > 150, 5)),
    ("холодный рынок", lambda ep: ep["hot"] == 0, "random", None),
]

# Форвард v1 (/opt/scanner-old-2026-10-03): вход paper-позиции, площадка по коду v1 и итог на
# 02–03.10.2026 из backtest/forward_vs_benchmark.py (запуск на VPS 06.10.2026): книга % —
# с выходами и комиссиями, «держать» % — капа без выходов. «DEX» = не было на Bybit spot:
# исполнитель такую монету не купил бы (отказ «нет на Bybit spot»).
V1 = [("LINK", "2026-07-13", "Bybit", 64.8, 74.5), ("GRAM", "2026-07-13", "Bybit", -7.2, -4.3),
      ("CAKE", "2026-07-14", "Bybit", 71.8, 81.5), ("GRT", "2026-07-14", "Bybit", 59.5, 67.0),
      ("REI", "2026-07-14", "DEX", -40.6, -24.4), ("AUDIO", "2026-07-14", "DEX", 23.0, 26.0),
      ("ORE", "2026-07-14", "DEX", 35.9, 65.5), ("RED", "2026-07-14", "Bybit", 46.9, 70.2),
      ("SOON", "2026-07-14", "DEX", 11.8, 16.9), ("SUPER", "2026-07-14", "DEX", 138.1, 182.8),
      ("PNUT", "2026-07-15", "Bybit", 25.5, 24.8), ("1INCH", "2026-07-15", "Bybit", 34.7, 35.4),
      ("HYPER", "2026-07-15", "Bybit", 4.5, 5.3), ("MOODENG", "2026-07-15", "DEX", 21.8, 25.3),
      ("ZK", "2026-07-15", "Bybit", -30.5, -28.6), ("MASK", "2026-08-14", "Bybit", 39.0, 40.9),
      ("SYRUP", "2026-08-18", "DEX", 47.2, 45.5), ("REQ", "2026-09-11", "DEX", 6.8, 6.1)]
V1_ALIAS = {"GRAM": ["GRAMUSDT", "TONUSDT"]}   # TON → GRAM: на Binance TONUSDT до 30.06.2026


# ---------------------------------------------------------------- эпизоды и исходы

def enrich(eps: list[dict], ranks: dict) -> None:
    """Признаки дня сигнала сверх ladder_dca_study: просадка от ATH и дрейф ранга."""
    for ep in eps:
        seg = ep["seg"]
        f = features(seg["c"], seg["qv"], ep["e"])
        ep["dd"] = f["dd_ath"] if f else None
        prev = ranks.get((seg["sym"], ep["day"] - DRIFT_LAG * DAY))
        ep["rank_prev"] = prev
        ep["drift"] = ep["rank"] / prev if prev else None


def complete(ep: dict) -> bool:
    seg = ep["seg"]
    return ep["e"] + HORIZON <= len(seg["c"]) - 1 or not seg["alive"]


def run_books(eps: list[dict]) -> dict[str, list[dict]]:
    L.HORIZON = HORIZON                       # simulate читает горизонт из модуля
    return {b: [dict(L.simulate(ep["seg"], ep["e"], BUY5, sell, budget=BUDGET, stop=stop,
                                stop_buf=0.25), ep=ep) for ep in eps]
            for b, (sell, stop) in BOOKS.items()}


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {}
    rob = sorted(r["rob"] for r in rows)
    n = len(rob)
    return {"n": n, "med": statistics.median(rob), "mean": statistics.fmean(rob),
            "p10": rob[int(n * 0.1)], "p90": rob[min(n - 1, int(n * 0.9))],
            "win": sum(1 for x in rob if x > 0) / n,
            "died": sum(1 for r in rows if r["ep"]["died2y"]) / n,
            "delist": sum(1 for r in rows if r["how"] == "делистинг") / n,
            "pnl": sum(r["pnl"] for r in rows)}


def top_threshold(rows: list[dict]) -> float:
    rob = sorted(r["rob"] for r in rows)
    return rob[int(len(rob) * 0.9)]


def pc(x: float | None, w: int = 7) -> str:
    return f"{'—':>{w}}" if x is None else f"{x * 100:>+{w - 1}.1f}%"


def sh(x: float | None, w: int = 5) -> str:
    return f"{'—':>{w}}" if x is None else f"{x * 100:>{w - 1}.0f}%"


# ---------------------------------------------------------------- 1. корзины

def buckets(res: dict[str, list[dict]], thr: dict[str, float], out: dict) -> None:
    n_all = len(res["R"])
    for title, (key, edges) in BUCKETS.items():
        print(f"\n--- {title}")
        print(f"  {'корзина':22} {'n':>5} {'доля':>5} | {'R мед':>7} {'R сред':>7} {'R p10':>7} "
              f"| {'H мед':>7} {'H сред':>7} {'H p10':>7} | {'умерла≤2г':>9} "
              f"{'лучших R':>8} {'лучших H':>8}")
        blk = out.setdefault(title, {})
        for a, b, lab in edges:
            def pick(ep, a=a, b=b):
                v = ep.get(key)
                return v is not None and a <= v <= b
            sr = stats([r for r in res["R"] if pick(r["ep"])])
            shh = stats([r for r in res["H"] if pick(r["ep"])])
            if not sr:
                continue
            top = {k: sum(1 for r in res[k] if pick(r["ep"]) and r["rob"] >= thr[k])
                   / max(1, sum(1 for r in res[k] if r["rob"] >= thr[k])) for k in res}
            blk[lab] = {"R": sr, "H": shh, "share": sr["n"] / n_all, "top_share": top}
            print(f"  {lab:22} {sr['n']:>5} {sh(sr['n'] / n_all)} | {pc(sr['med'])} "
                  f"{pc(sr['mean'])} {pc(sr['p10'])} | {pc(shh['med'])} {pc(shh['mean'])} "
                  f"{pc(shh['p10'])} | {sh(sr['died'], 9)} {sh(top['R'], 8)} {sh(top['H'], 8)}")
        miss = sum(1 for r in res["R"] if r["ep"].get(key) is None)
        if miss:
            print(f"  (нет признака: {miss} эпизодов — для дрейфа это монеты моложе "
                  f"{DRIFT_LAG + 30} дн.)")
    print("  «лучших» — доля верхнего дециля исходов всех пружин, попавшая в корзину; "
          "сравнивать с колонкой «доля»")


# ---------------------------------------------------------------- 2. фильтры

def filters(res: dict[str, list[dict]], thr: dict[str, float], out: dict) -> None:
    base = {b: stats(rows) for b, rows in res.items()}
    for b in res:
        name = "R правила" if b == "R" else "H держать"
        print(f"\n--- книга {name}: что оставляет фильтр (365 дн., бюджет ${BUDGET:g})")
        print(f"  {'фильтр':38} {'входов':>6} {'доля':>5} {'мед.':>7} {'сред.':>7} {'p10':>7} "
              f"{'win':>4} {'умерла':>6} | {'срезано':>7} {'Σ P&L срезанных':>16} "
              f"{'прибыль':>8} {'убыток':>8}")
        for fname, pred in FILTERS:
            keep = [r for r in res[b] if pred(r["ep"])]
            cut = [r for r in res[b] if not pred(r["ep"])]
            s = stats(keep)
            if not s:
                continue
            n_top = sum(1 for r in res[b] if r["rob"] >= thr[b])
            top_cut = sum(1 for r in cut if r["rob"] >= thr[b]) / max(1, n_top)
            gains = sum(r["pnl"] for r in cut if r["pnl"] > 0)
            losses = sum(r["pnl"] for r in cut if r["pnl"] < 0)
            cyc = {}
            for c, _, _ in L.CYCLES:
                k = stats([r for r in keep if r["ep"]["cycle"] == c])
                a = stats([r for r in res[b] if r["ep"]["cycle"] == c])
                cyc[c] = {"n": k.get("n", 0), "d_med": (k["med"] - a["med"]) if k and a else None,
                          "d_mean": (k["mean"] - a["mean"]) if k and a else None}
            out.setdefault(fname, {})[b] = {
                **s, "kept": s["n"] / len(res[b]), "top_cut": top_cut,
                "cut_pnl": gains + losses, "cut_gains": gains, "cut_losses": losses,
                "cycles": cyc}
            print(f"  {fname:38} {s['n']:>6} {sh(s['n'] / len(res[b]))} {pc(s['med'])} "
                  f"{pc(s['mean'])} {pc(s['p10'])} {sh(s['win'], 4)} {sh(s['died'], 6)} | "
                  f"{sh(top_cut, 7)} {gains + losses:>+15.0f}$ {gains:>+7.0f}$ {losses:>+7.0f}$")
        print(f"  «срезано» — доля верхнего дециля исходов (≥ {thr[b] * 100:+.0f}% бюджета), "
              f"которую фильтр выбросил; Σ P&L срезанных > 0 — фильтр выбрасывает прибыль")
    print("\n--- по циклам: медиана оставленных минус медиана всех, п.п. (R / H)")
    print(f"  {'фильтр':38} " + " ".join(f"{c:>19}" for c, _, _ in L.CYCLES))
    for fname, _ in FILTERS[1:]:
        cells = []
        for c, _, _ in L.CYCLES:
            r, h = out[fname]["R"]["cycles"][c], out[fname]["H"]["cycles"][c]
            cells.append(f"{pc(r['d_med'], 6)} /{pc(h['d_med'], 6)} n{r['n']:<4}")
        print(f"  {fname:38} " + " ".join(f"{x:>19}" for x in cells))
    _ = base


# ---------------------------------------------------------------- 3. портфель со слотами

def book_sim(rows: list[dict], take, order: str, quota, seed: int) -> dict:
    """Хронологически: в день сигнала кандидаты, прошедшие take, занимают свободные слоты
    (не больше SLOTS, одна позиция на монету); слот свободен с дня выхода."""
    rnd = random.Random(seed)
    by_day: dict[int, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["ep"]["day"], []).append(r)
    held: list[tuple[int, bool, str]] = []          # (день выхода, в квоте, монета)
    taken: list[dict] = []
    no_slot = 0
    for day in sorted(by_day):
        held = [x for x in held if x[0] > day]
        cands = [r for r in by_day[day] if take(r["ep"])]
        if order == "rank":
            cands.sort(key=lambda r: r["ep"]["rank"])
        else:
            rnd.shuffle(cands)
        for r in cands:
            coin = r["ep"]["seg"]["sym"].split("#")[0]
            if any(x[2] == coin for x in held):
                continue
            in_q = quota is not None and quota[0](r["ep"])
            if len(held) >= SLOTS or (in_q and sum(1 for x in held if x[1]) >= quota[1]):
                no_slot += 1
                continue
            held.append((day + max(r["span"], 1) * DAY, in_q, coin))
            taken.append(r)
    slot_years = sum(BUDGET * max(r["span"], 1) / 365 for r in taken)
    return {"n": len(taken), "pnl": sum(r["pnl"] for r in taken), "no_slot": no_slot,
            "died": sum(1 for r in taken if r["ep"]["died2y"]),
            "delist": sum(1 for r in taken if r["how"] == "делистинг"),
            "med": statistics.median(r["rob"] for r in taken) if taken else 0.0,
            "per_slot_year": sum(r["pnl"] for r in taken) / slot_years if slot_years else 0.0,
            "big": sum(1 for r in taken if r["rob"] >= 1.0)}


def portfolio(res_all: dict[str, list[dict]], out: dict) -> None:
    days = [r["ep"]["day"] for r in res_all["R"]]
    years = (max(days) - min(days)) / DAY / 365
    print(f"\n=== 3. Портфель: {SLOTS} слотов по ${BUDGET:g} (${SLOTS * BUDGET:g}), "
          f"{time.strftime('%m.%Y', time.gmtime(min(days)))}–"
          f"{time.strftime('%m.%Y', time.gmtime(max(days)))} ({years:.1f} г.), "
          f"среднее по {SEEDS} случайным порядкам сигналов внутри дня ===")
    for b in res_all:
        name = "R правила" if b == "R" else "H держать"
        print(f"\n--- книга {name}")
        print(f"  {'правило отбора':40} {'входов':>6} {'нет слота':>9} {'умерло':>6} "
              f"{'делист':>6} {'мед.':>7} {'≥+100%':>6} {'Σ P&L':>8} {'в год':>7} "
              f"{'на слот-год':>11}")
        blk = out.setdefault(b, {})
        for pname, take, order, quota in POLICIES:
            runs = [book_sim(res_all[b], take, order, quota, s)
                    for s in range(SEEDS if order == "random" else 1)]
            avg = {k: statistics.fmean(x[k] for x in runs) for k in runs[0]}
            avg["pnl_min"] = min(x["pnl"] for x in runs)
            avg["pnl_max"] = max(x["pnl"] for x in runs)
            blk[pname] = avg
            print(f"  {pname:40} {avg['n']:>6.0f} {avg['no_slot']:>9.0f} {avg['died']:>6.0f} "
                  f"{avg['delist']:>6.0f} {pc(avg['med'])} {avg['big']:>6.0f} "
                  f"{avg['pnl']:>+7.0f}$ {avg['pnl'] / years / (SLOTS * BUDGET) * 100:>+6.1f}% "
                  f"{pc(avg['per_slot_year'], 11)}")
    print("  «в год» — Σ P&L / лет / капитал книги (простая, без реинвестирования); «на слот-год»"
          " — P&L на $ бюджета за год занятого слота; «нет слота» — сигналы, прошедшие правило,"
          " но слоты были заняты")


# ---------------------------------------------------------------- 4. форвард v1

def v1_forward(segs: list[dict], ranks: dict, out: dict) -> None:
    n_day: dict[int, int] = {}
    for (_, d) in ranks:
        n_day[d] = n_day.get(d, 0) + 1
    first = {}
    for s in segs:
        base = s["sym"].split("#")[0]
        first[base] = min(first.get(base, 10**12), s["ts"][0])
    print("\n=== 4. Форвард v1: монеты paper-книги — ранг оборота Binance на день до входа ===")
    print(f"  {'монета':8} {'вход':>6} {'площадка':>8} {'ранг':>10} {'листинг':>8} "
          f"{'гейт Q':>6} {'книга':>7} {'держать':>8}")
    rows = []
    for sym, d, venue, book, hold in V1:
        day = calendar.timegm(time.strptime(d, "%Y-%m-%d")) - DAY   # последнее закрытие до входа
        pairs = V1_ALIAS.get(sym, [f"{sym}USDT"])
        rk, rk_day = None, None
        # ранг на день до входа; нет (свежий тикер после ребрендинга) — последний за 30 дней
        for back in range(0, 31):
            for p in pairs:
                for k in range(0, 4):
                    v = ranks.get((p if k == 0 else f"{p}#{k}", day - back * DAY))
                    if v is not None and (rk is None or v < rk):
                        rk, rk_day = v, day - back * DAY
            if rk is not None:
                break
        f0 = min((first[p] for p in pairs if p in first), default=None)
        age = (day - f0) / DAY if f0 else None
        gate = rk is not None and rk <= 150 and age is not None and age >= 365
        note = "нет на Binance" if f0 is None else "делистнута" if rk is None else ""
        rows.append({"sym": sym, "venue": venue, "rank": rk, "age": age, "gate": gate,
                     "book": book, "hold": hold, "note": note})
        rk_s = (f"{'≈' if rk_day != day else ''}{rk}/{n_day.get(rk_day, 0)}" if rk
                else note or "—")
        print(f"  {sym:8} {d[8:10]}.{d[5:7]} {venue:>8} {rk_s:>14} "
              f"{(f'{age / 365:.1f} г' if age is not None else '—'):>8} "
              f"{'да' if gate else 'нет':>6} {book:>+6.1f}% {hold:>+7.1f}%")
    out["rows"] = rows
    for lab, sel in (("все 18", rows), ("только Bybit spot (купил бы исполнитель)",
                                         [r for r in rows if r["venue"] == "Bybit"])):
        g = [r for r in sel if r["gate"]]
        ng = [r for r in sel if not r["gate"]]
        line = f"  {lab}: в гейте {len(g)}"
        if g:
            line += (f" — книга {statistics.fmean(r['book'] for r in g):+.1f}%, "
                     f"держать {statistics.fmean(r['hold'] for r in g):+.1f}%")
        line += f"; вне гейта {len(ng)}"
        if ng:
            line += (f" — книга {statistics.fmean(r['book'] for r in ng):+.1f}%, "
                     f"держать {statistics.fmean(r['hold'] for r in ng):+.1f}%")
        print(line)
        out[lab] = {"gate": [r["sym"] for r in g], "out": [r["sym"] for r in ng]}
    print("  (одно растущее окно и 18 монет — иллюстрация, не статистика; книга — с выходами и "
          "комиссиями, держать — по капе)")


# ---------------------------------------------------------------- main

def main() -> int:
    t0 = time.time()
    coins = L.load_archive()
    all_segs = L.segments(coins)
    ranks = L.vol_ranks(all_segs)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot = L.load_hot()
    eps_all = [ep for ep in L.build_episodes(segs, ranks, hot, "spring") if ep["cycle"] != "?"]
    enrich(eps_all, ranks)
    eps = [ep for ep in eps_all if complete(ep)]
    print(f"Пружин: {len(eps_all)} на {len({ep['seg']['sym'] for ep in eps_all})} монетах, с "
          f"полным горизонтом {HORIZON} дн. или умерших: {len(eps)}  [{time.time()-t0:.0f} с]")
    res = run_books(eps)
    thr = {b: top_threshold(rows) for b, rows in res.items()}
    results: dict = {"meta": {"episodes": len(eps), "episodes_all": len(eps_all),
                              "budget": BUDGET, "horizon": HORIZON, "slots": SLOTS,
                              "top_decile": thr}}
    print(f"[{time.time()-t0:.0f} с] верхний дециль: R ≥ {thr['R']*100:+.0f}%, "
          f"H ≥ {thr['H']*100:+.0f}% бюджета")

    print(f"\n=== 1. Корзины признаков (5×$10 до пола, {HORIZON} дн.) ===")
    buckets(res, thr, results.setdefault("buckets", {}))
    print(f"\n=== 2. Фильтры [{time.time()-t0:.0f} с] ===")
    filters(res, thr, results.setdefault("filters", {}))

    # портфель — все эпизоды (у недосчитанных хвостов span короче, слот освобождается раньше)
    res_all = run_books(eps_all)
    portfolio(res_all, results.setdefault("portfolio", {}))
    v1_forward(all_segs, ranks, results.setdefault("v1", {}))

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=float),
                   encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
