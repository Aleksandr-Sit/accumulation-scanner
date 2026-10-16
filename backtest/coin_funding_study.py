"""Фандинг перпа монеты на входе и в выходе против исхода эпизода (P12).

Что в проде (не валидировано на истории до этого замера):
  • scanner/sources/funding.py — ОДИН вызов Bybit /v5/market/tickers?category=linear в момент
    прогона (таймер 06:00 UTC): поле fundingRate — ставка ТЕКУЩЕГО интервала (её спишут в
    ближайший расчёт), без усреднения, приведённая к 8 ч по fundingIntervalHour (блок A,
    08.10). Мультипликаторные контракты (1000PEPE) — на базовый тикер. Нет перпа — None.
  • scanner/stages/entry_quality.py п.5 — множитель качества пружины: фандинг ≤ −0.0003 →
    ×1.08 («капитуляция»), ≥ 0.0005 → ×0.92 («эйфория»); множитель режется полом 0.4 и
    потолком 1.25 и умножает под-балл зоны 9 (clamp 10, вес 0.28) — scanner/stages/score.py.
  • scanner/stages/exit.py п.4 — фандинг ≥ 0.0005 (stage8_exit.funding_euphoria) при позиции
    в плюсе даёт сигнал peak_zone «ЗОНА РАСПРЕДЕЛЕНИЯ — рассмотреть фиксацию» (urgency low,
    повтор не раньше 30 дн.). Это ИНФОРМАЦИОННЫЙ алерт: paper продаёт только по стопу и
    трейлу, исполнитель (scanner/executor.py) peak_zone не исполняет. Фандинг в watch берётся
    только к последнему закрытию (pipeline._watch_position).

История: Binance USDⓈ-M /fapi/v1/fundingRate (публично, с 2019-09; делистнутые перпы тоже
отдаются), кэш .cache/binance_funding/<SYMBOL>.json. Перпы монеты: exchangeInfo (текущие и
SETTLING) по базе без множителя 10…0 + проба BASEUSDT (делистнутые старые; делистнутые
мультипликаторные 1000X вне exchangeInfo не пробуются — их почти нет).
Ставка приводится к 8 ч по фактическому шагу меток (rate × 8 / часы от прошлого расчёта).
Тикер мог перейти к другой монете — перп отбрасывается для отрезка, если markPrice (где он
есть) расходится с закрытием спота больше чем в 1.5 раза в > 20% меток отрезка.

«Как прод» на день сигнала t: прогон в 06:00 UTC после закрытия t → ставка расчёта, в
интервал которого попадает 06:00 (у 8-часовых — расчёт 08:00; Binance считает её по
премии 00:00–08:00, к 06:00 известно ~75% — заглядывание ≤ 2 ч, на исход за месяцы не
влияет). Проверки: «строго» — последний расчёт ≤ 06:00 (без заглядывания); «24 ч» —
среднее за сутки до 06:00 (не прод, сглаживание). Перпа нет или последний расчёт старше
суток — None (как прод: нейтрально).

Эпизоды и модель: пружины архива Binance 2018–2026 с умершими, детектор прода
(ladder_dca_study.is_spring_prod), исход эпизода — simulate_exec книги R (прод-выходы) и H
(только стоп −25%), $50 лестницей, без горизонта, все правила исполнителя EXEC_ALL.
Эпизод для корзин — первый день сигнала отрезка, следующий не раньше 90 дн. (кулдаун прода);
«фильтр исполнителя» — нижняя четверть по обороту и перегрев рынка отсеяны (EXEC_POLICY).
IC внутри дня — Спирмен фандинга и исхода среди ВСЕХ монет в пружине в один день (дни с ≥ 5
монетами с перпом), среднее по дням; пул — по эпизодам цикла.

Выход: simulate_exec(extra_exit=…) — гипотетическое исполнение peak_zone по фандингу
(в проде это алерт): при фандинге «как прод» к закрытию дня ≥ порога и позиции в плюсе
(закрытие к средней) — продать весь остаток / треть купленного (повтор через 30 дн.,
как кулдаун peak_zone) по закрытию, 0.15%. Портфель — portfolio_vs_market.exec_book шаг s6
(15 мест, $1500, кулдаун 90 дн., фильтры исполнителя), P&L по денежным потокам против
индекса альтов и BTC, J.SEEDS случайных порядков сигналов внутри дня. Сверка: база
повторяет s6 (R +$1503, H +$1903).

Оговорки: прод читает Bybit, история — Binance (ставки близки, но не равны; у Bybit
дефолт +0.01%/8 ч так же); на Binance-спот без перпа Binance монета могла иметь перп Bybit
(покрытие прода выше); до 2019-09 перпов нет — 2018–19 без фандинга; цифры — верхняя
граница (выжившие ×0.73, правила книг подобраны на той же истории), эпизоды коррелированы.

Запуск из корня проекта (PYTHONIOENCODING=utf-8):
  python backtest/coin_funding_study.py --fetch   # только скачать фандинг (≈25–30 мин с нуля)
  python backtest/coin_funding_study.py           # замер (кэш есть; ≈ см. «время прогона»)
Время прогона 16.10.2026: скачивание с нуля ≈ 59 мин (568 перпов-кандидатов, 456 баз с перпом,
кэш ~ .cache/binance_funding), замер ≈ 4.6 мин (278 с; база s6 повторилась: R +$1503, H +$1903).
Выход по фандингу продаёт по закрытию дня, а ставка «как прод» известна в 06:00 следующего
дня — вариант all5s (расчёт ≤ закрытия) проверяет, что вывод не от заглядывания.
Хук ladder_dca_study.simulate_exec(extra_exit=…) — необязательный, без него всё как раньше.
Результат: backtest/coin_funding_results.json
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import sqlite3
import statistics as st
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import junk_filter_study as J  # noqa: E402
import ladder_dca_study as L  # noqa: E402
import portfolio_vs_market as P  # noqa: E402

OUT = Path(__file__).resolve().parent / "coin_funding_results.json"
CACHE = ROOT / ".cache" / "binance_funding"
FAPI = "https://fapi.binance.com"
UA = {"User-Agent": "funding-research/0.1"}
DAY = 86400
H8 = 8 * 3600
START_MS = 1567296000000          # 2019-09-01: первые перпы USDⓈ-M
PAUSE = 0.35                      # fundingRate: 500 запросов / 5 мин на IP — с запасом
SCAN_OFFSET = 6 * 3600            # прогон прода 06:00 UTC после закрытия дня
MAX_AGE = DAY                     # последний расчёт старше — перпа нет
CAPITULATION, EUPHORIA = -0.0003, 0.0005    # stage4b_quality / stage8_exit
BONUS, PENALTY = 1.08, 0.92
BUCKETS = [("< −0.03%", -9.0, -0.0003), ("−0.03…0%", -0.0003, 0.0), ("0…0.01%", 0.0, 0.0001),
           ("0.01…0.05%", 0.0001, 0.0005), ("> 0.05%", 0.0005, 9.0)]
CYCLE_NAMES = {"2018-20": "2019-09…2020-03", "2020-22": "2020-04…2022", "2023-26": "2023-26"}
MIN_DAY = 5                       # монет с перпом в день для IC внутри дня
EXIT_VARIANTS = [
    {"key": "base", "label": "база (прод: фандинг — только алерт)"},
    {"key": "all5", "label": "фандинг ≥ 0.05% и плюс → продать всё", "thr": 0.0005, "frac": 1.0},
    {"key": "third5", "label": "фандинг ≥ 0.05% и плюс → треть, повтор 30 дн.", "thr": 0.0005,
     "frac": 0.33},
    {"key": "all10", "label": "справ.: фандинг ≥ 0.10% и плюс → всё", "thr": 0.0010, "frac": 1.0},
    {"key": "all5s", "label": "справ.: ≥ 0.05% → всё, расчёт ≤ закрытия", "thr": 0.0005, "frac": 1.0,
     "strict": True},
]


# ---------------------------------------------------------------- скачивание

def _get_json(url: str, tries: int = 6):
    """-> разобранный JSON | None (400 — символа нет). 429/418/5xx — пауза и повтор."""
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 400:
                return None
            wait = 70 if e.code in (418, 429) else 5 * (k + 1)
            print(f"  HTTP {e.code} — пауза {wait} с")
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  сеть: {e} — повтор")
            time.sleep(5 * (k + 1))
    raise RuntimeError(f"не скачалось: {url}")


def fetch_symbol(sym: str) -> list:
    """Вся история фандинга перпа [[ms, rate, mark|0], ...] (кэш на диск). [] — перпа не было."""
    p = CACHE / f"{sym}.json"
    if p.exists():
        return json.loads(p.read_text())["rows"]
    rows: list = []
    start = START_MS
    while True:
        data = _get_json(f"{FAPI}/fapi/v1/fundingRate?symbol={sym}&startTime={start}&limit=1000")
        time.sleep(PAUSE)
        if not data:
            break
        for x in data:
            rows.append([int(x["fundingTime"]), float(x["fundingRate"]),
                         float(x.get("markPrice") or 0.0)])
        if len(data) < 1000:
            break
        start = rows[-1][0] + 1
    CACHE.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"sym": sym, "fetched": time.time(), "rows": rows}))
    return rows


def norm_base(b: str) -> tuple[str, float]:
    """1000PEPE -> (PEPE, 1000): множитель контракта/спот-тикера."""
    i = 0
    while i < len(b) and b[i] in "0" + "1":
        i += 1
    head = b[:i]
    if len(head) >= 4 and head[0] == "1" and set(head[1:]) == {"0"} and i < len(b):
        return b[i:], float(head)
    return b, 1.0


def perp_candidates(spot_syms: dict[str, float]) -> dict[str, list[tuple[str, float]]]:
    """спот-база -> [(перп, множитель перпа / множитель спота)] — exchangeInfo + пробы."""
    info = _get_json(f"{FAPI}/fapi/v1/exchangeInfo") or {}
    by_norm: dict[str, list[tuple[str, float]]] = {}
    for s in info.get("symbols", []):
        if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT":
            continue
        nb, m = norm_base(s["baseAsset"])
        by_norm.setdefault(nb, []).append((s["symbol"], m))
    out: dict[str, list[tuple[str, float]]] = {}
    for base, last_px in spot_syms.items():
        nb, ms = norm_base(base)
        cand = {sym: m / ms for sym, m in by_norm.get(nb, [])}
        cand.setdefault(f"{base}USDT", 1.0)                 # делистнутые нет в exchangeInfo
        out[base] = sorted(cand.items(), key=lambda kv: (kv[1] != 1.0, kv[0]))
    return out


def fetch_all(bases: dict[str, float]) -> dict[str, list[tuple[str, float, list]]]:
    """спот-база -> [(перп, множитель, rows)] только с данными."""
    t0 = time.time()
    cands = perp_candidates(bases)
    n_req = sum(len(v) for v in cands.values())
    print(f"баз {len(bases)}, перпов-кандидатов {n_req}; в кэше "
          f"{sum(1 for v in cands.values() for s, _m in v if (CACHE / f'{s}.json').exists())}")
    out: dict = {}
    k = 0
    for base, lst in sorted(cands.items()):
        for sym, m in lst:
            k += 1
            rows = fetch_symbol(sym)
            if rows:
                out.setdefault(base, []).append((sym, m, rows))
            if k % 50 == 0:
                print(f"  ...{k}/{n_req} ({time.time() - t0:.0f} с)", flush=True)
    return out


# ---------------------------------------------------------------- ряд фандинга

class Funding:
    """Ряд ставок перпа, приведённых к 8 ч, для отрезка спота (времена в секундах)."""

    def __init__(self, rows: list):
        rows = sorted(rows)
        self.t: list[int] = []
        self.r: list[float] = []
        prev = None
        for ms, rate, _mark in rows:
            t = ms // 1000
            step = (t - prev) / 3600 if prev is not None else 8.0
            hours = min((h for h in (1, 2, 4, 8) if abs(step - h) < 0.25 * h), default=8)
            self.t.append(t)
            self.r.append(rate * 8.0 / hours)
            prev = t

    def prod(self, T: int) -> float | None:
        """Ставка интервала, в который попадает T (ближайший расчёт после T)."""
        i = bisect.bisect_right(self.t, T)
        if i == 0 or T - self.t[i - 1] > MAX_AGE or i >= len(self.t):
            return None
        return self.r[i] if self.t[i] - T <= H8 else None

    def strict(self, T: int) -> float | None:
        i = bisect.bisect_right(self.t, T)
        if i == 0 or T - self.t[i - 1] > MAX_AGE:
            return None
        return self.r[i - 1]

    def mean24(self, T: int) -> float | None:
        i = bisect.bisect_right(self.t, T)
        j = bisect.bisect_right(self.t, T - DAY)
        if i == 0 or T - self.t[i - 1] > MAX_AGE or i <= j:
            return None
        return st.fmean(self.r[j:i])


def seg_funding(seg: dict, perps: list[tuple[str, float, list]]) -> tuple[Funding | None, str]:
    """Перп для отрезка: метки внутри отрезка, сверка markPrice с закрытием спота."""
    ts, c = seg["ts"], seg["c"]
    t0, t1 = ts[0], ts[-1] + DAY
    day_px = dict(zip(ts, c))
    for sym, mult, rows in perps:
        inside = [x for x in rows if t0 * 1000 <= x[0] <= t1 * 1000]
        if not inside:
            continue
        chk = bad = 0
        for ms, _r, mark in inside:
            if mark <= 0:
                continue
            spot = day_px.get(ms // 1000 // DAY * DAY)
            if not spot:
                continue
            chk += 1
            ratio = mark / mult / spot
            bad += not (1 / 1.5 < ratio < 1.5)
        if chk and bad / chk > 0.2:
            continue
        return Funding(inside), sym
    return None, ""


# ---------------------------------------------------------------- статистика

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


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3 or len(set(a)) < 2 or len(set(b)) < 2:
        return None
    return st.correlation(ranked(a), ranked(b))


def within_day_ic(rows: list[tuple[int, float, float]]) -> dict:
    """rows (день, фандинг, исход) -> средний Спирмен по дням с ≥ MIN_DAY монетами, t-стат."""
    by: dict[int, list] = {}
    for d, f, y in rows:
        by.setdefault(d, []).append((f, y))
    ics = []
    for d, xs in by.items():
        if len(xs) >= MIN_DAY:
            ic = spearman([x[0] for x in xs], [x[1] for x in xs])
            if ic is not None:
                ics.append(ic)
    if len(ics) < 2:
        return {"days": len(ics), "ic": st.fmean(ics) if ics else None, "t": None}
    m, sd = st.fmean(ics), st.stdev(ics)
    return {"days": len(ics), "ic": m, "t": m / sd * len(ics) ** 0.5 if sd else None,
            "pos_share": sum(1 for x in ics if x > 0) / len(ics)}


def bucket_of(f: float) -> str:
    return next(name for name, lo, hi in BUCKETS if lo <= f < hi) if f >= -9 else BUCKETS[0][0]


def cell(rows: list[dict], book: str) -> dict:
    if not rows:
        return {"n": 0}
    rod = [x[book]["rod"] for x in rows]
    exa = [x[book]["ex_alt"] for x in rows if x[book]["ex_alt"] is not None]
    return {"n": len(rows), "med": st.median(rod), "mean": st.fmean(rod),
            "win": sum(1 for v in rod if v > 0) / len(rod),
            "ex_alt_med": st.median(exa) if exa else None,
            "ex_alt_mean": st.fmean(exa) if exa else None,
            "died": sum(1 for x in rows if x["died2y"]) / len(rows),
            "p100": sum(1 for v in rod if v >= 1.0) / len(rod)}


def ex_alt(ep: dict, r: dict, alt: dict) -> float | None:
    m = P.evaluate_flows([(ep, r)], alt, alt)
    return (m["pnl"] - m["alt"]) / m["spent"] if m else None


def pct(x: float | None, w: int = 7) -> str:
    return f"{x * 100:+.1f}%".rjust(w) if isinstance(x, (int, float)) else "—".rjust(w)


def iso(day: int) -> str:
    return dt.datetime.fromtimestamp(day, dt.timezone.utc).date().isoformat()


# ---------------------------------------------------------------- живой журнал прода

def live_cards(db: Path) -> dict:
    """Кандидаты зоны дна в scanner.db: как часто фандинг сработал и сколько баллов он сдвинул
    у порога 70 (оценка: под-балл зоны = clamp(9 × q), вес 0.28, знаменатель — вес блоков с
    данными; on-chain недоступен → 0.90)."""
    if not db.exists():
        return {}
    con = sqlite3.connect(db)
    rows = con.execute("select c.symbol, c.score, c.confidence, c.spring_quality, c.funding_rate, "
                       "r.ts from candidates c join runs r on r.id = c.run_id "
                       "where c.zone = 'ПРУЖИНА/ДНО'").fetchall()
    con.close()
    out = {"spring_rows": len(rows), "with_funding": 0, "capitulation": 0, "euphoria": 0,
           "flips": [], "first": None, "last": None, "funding_values": {}}
    if not rows:
        return out
    out["first"] = iso(int(min(r[5] for r in rows)))
    out["last"] = iso(int(max(r[5] for r in rows)))
    for sym, score, conf, q, fr, ts in rows:
        if fr is None:
            continue
        out["with_funding"] += 1
        k = f"{fr:.5f}"
        out["funding_values"][k] = out["funding_values"].get(k, 0) + 1
        mult = BONUS if fr <= CAPITULATION else PENALTY if fr >= EUPHORIA else None
        if mult is None or q is None or score is None:
            continue
        out["capitulation" if mult > 1 else "euphoria"] += 1
        q0 = q / mult
        dz = min(10, 9 * q) - min(10, 9 * q0)
        s0 = score - dz * 0.28 / 0.90 * 10
        if (score >= 70) != (s0 >= 70):
            out["flips"].append({"symbol": sym, "day": iso(int(ts)), "score": score,
                                 "without": round(s0, 1), "funding": fr})
    return out


# ---------------------------------------------------------------- main

def main() -> int:
    t0 = time.time()
    fetch_only = "--fetch" in sys.argv
    coins = L.load_archive()
    all_segs = L.segments(coins)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    bases: dict[str, float] = {}
    for s in segs:
        if s["ts"][-1] >= START_MS // 1000:
            b = s["sym"].split("#")[0][:-4]
            bases[b] = min(bases.get(b, 9e9), s["c"][-1])
    perps = fetch_all(bases)
    print(f"фандинг: баз с перпом {len(perps)} из {len(bases)}  [{time.time() - t0:.0f} с]")
    if fetch_only:
        return 0

    btc = P.btc_closes(all_segs)
    alt, _ = P.market_index(ROOT / "scanner.db")
    vranks = L.vol_ranks(all_segs)
    npairs = P.pairs_per_day(vranks)
    hot = L.load_hot()
    src = [ep for ep in L.signal_days(segs, vranks, hot, {"prod": L.is_spring_prod})["prod"]
           if ep["cycle"] != "?"]
    fseg: dict[str, Funding | None] = {}
    fsym: dict[str, str] = {}
    for s in segs:
        f, sym = seg_funding(s, perps.get(s["sym"].split("#")[0][:-4], []))
        fseg[s["sym"]], fsym[s["sym"]] = f, sym
    for ep in src:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
        f = fseg.get(ep["seg"]["sym"])
        T = ep["day"] + DAY + SCAN_OFFSET
        ep["fr"] = f.prod(T) if f else None
        ep["fr_strict"] = f.strict(T) if f else None
        ep["fr_24h"] = f.mean24(T) if f else None
    print(f"дней сигнала прода {len(src)}, с фандингом {sum(1 for ep in src if ep['fr'] is not None)}"
          f"  [{time.time() - t0:.0f} с]")

    sims: dict[str, dict] = {b: {} for b in J.BOOKS}

    def sim(ep, b, extra=None, key="base"):
        k = (ep["seg"]["sym"], ep["e"], key)
        if k not in sims[b]:
            sims[b][k] = L.simulate_exec(ep["seg"], ep["e"], J.BOOKS[b][0], budget=J.BUDGET,
                                         horizon=None, rules=L.EXEC_ALL, extra_exit=extra)
        return sims[b][k]

    # ---- эпизоды: первый сигнал отрезка, следующий не раньше 90 дн.
    eps = []
    last: dict[str, int] = {}
    for ep in sorted(src, key=lambda x: (x["seg"]["sym"], x["day"])):
        s = ep["seg"]["sym"]
        if s in last and ep["day"] < last[s] + P.COOLDOWN_DAYS * DAY:
            continue
        last[s] = ep["day"]
        rr = {b: sim(ep, b) for b in J.BOOKS}
        if any(r is None or r["spent"] <= 0 for r in rr.values()):
            continue
        ep["out"] = {b: {"rod": r["pnl"] / r["spent"], "ex_alt": ex_alt(ep, r, alt)}
                     for b, r in rr.items()}
        eps.append(ep)
    take = P.EXEC_POLICY[0]
    print(f"эпизодов (кулдаун 90 дн. от сигнала) {len(eps)}, из них проходят фильтр исполнителя "
          f"{sum(1 for ep in eps if take(ep))}  [{time.time() - t0:.0f} с]")

    out: dict = {"meta": {"scan_offset_h": SCAN_OFFSET / 3600, "buckets": [b[0] for b in BUCKETS],
                          "thresholds": {"capitulation": CAPITULATION, "euphoria": EUPHORIA},
                          "seeds": J.SEEDS, "slots": J.SLOTS, "budget": J.BUDGET,
                          "perp_bases": len(perps), "spot_bases": len(bases),
                          "signal_days": len(src), "episodes": len(eps),
                          "first_funding": START_MS // 1000}}

    # ---- 1. покрытие по годам
    cov = {}
    for ep in eps:
        y = time.strftime("%Y", time.gmtime(ep["day"]))
        c = cov.setdefault(y, {"n": 0, "perp": 0, "n_exec": 0, "perp_exec": 0})
        c["n"] += 1
        c["perp"] += ep["fr"] is not None
        if take(ep):
            c["n_exec"] += 1
            c["perp_exec"] += ep["fr"] is not None
    out["coverage"] = cov
    print("\n1. Доля эпизодов с перпом Binance на день входа (все / прошедшие фильтр исполнителя):")
    print("   " + "  ".join(f"{y}: {v['perp'] / v['n']:.0%} из {v['n']} / "
                            f"{(v['perp_exec'] / v['n_exec']) if v['n_exec'] else 0:.0%} из {v['n_exec']}"
                            for y, v in sorted(cov.items())))
    fe = [ep["fr"] for ep in eps if ep["fr"] is not None]
    if fe:
        q = sorted(fe)
        dist = {"n": len(q), "p05": q[len(q) // 20], "p25": q[len(q) // 4], "med": q[len(q) // 2],
                "p75": q[3 * len(q) // 4], "p95": q[19 * len(q) // 20],
                "le_cap": sum(1 for v in q if v <= CAPITULATION) / len(q),
                "ge_eup": sum(1 for v in q if v >= EUPHORIA) / len(q),
                "eq_default": sum(1 for v in q if abs(v - 0.0001) < 1e-9) / len(q)}
        out["distribution"] = dist
        print(f"   фандинг эпизодов, %/8ч: p5 {dist['p05']*100:+.4f} p25 {dist['p25']*100:+.4f} "
              f"мед. {dist['med']*100:+.4f} p75 {dist['p75']*100:+.4f} p95 {dist['p95']*100:+.4f}; "
              f"≤ −0.03% {dist['le_cap']:.1%}, ≥ +0.05% {dist['ge_eup']:.1%}, ровно +0.01% "
              f"{dist['eq_default']:.0%}")
        agree = [(ep["fr"], ep["fr_strict"]) for ep in eps
                 if ep["fr"] is not None and ep["fr_strict"] is not None]
        same = sum(1 for a, b in agree if bucket_of(a) == bucket_of(b)) / max(1, len(agree))
        out["prod_vs_strict_same_bucket"] = same
        print(f"   «как прод» и «строго» (последний расчёт) в одной корзине: {same:.0%}")

    # ---- 2. корзины и IC
    out["buckets"] = {}
    for scope, sel in (("all", lambda ep: True), ("exec", take)):
        E = [ep for ep in eps if sel(ep) and ep["fr"] is not None]
        tbl = {}
        for name, lo, hi in BUCKETS:
            rows = [ep for ep in E if lo <= ep["fr"] < hi]
            tbl[name] = {b: cell([{**ep["out"], "died2y": ep["died2y"]} for ep in rows], b)
                         for b in J.BOOKS}
        for name, f in (("  =0.01% дефолт", lambda v: abs(v - 0.0001) < 1e-9),
                        ("  0.01<…0.05%", lambda v: 0.0001 + 1e-9 < v < EUPHORIA)):
            rows = [ep for ep in E if f(ep["fr"])]
            tbl[name] = {b: cell([{**ep["out"], "died2y": ep["died2y"]} for ep in rows], b)
                         for b in J.BOOKS}
        tbl["нет перпа"] = {b: cell([{**ep["out"], "died2y": ep["died2y"]} for ep in eps
                                     if sel(ep) and ep["fr"] is None and ep["day"] >= START_MS // 1000], b)
                            for b in J.BOOKS}
        out["buckets"][scope] = tbl
        print(f"\n2. Корзины фандинга на входе («как прод», %/8ч) — эпизоды "
              f"{'все' if scope == 'all' else 'после фильтра исполнителя'}; исход на вложенное, "
              f"«−альты» — к индексу альтов по денежным потокам")
        print(f"   {'корзина':12} {'n':>5} | R: {'мед.':>7} {'средн.':>7} {'win':>4} {'−альты мед.':>11}"
              f" | H: {'мед.':>7} {'средн.':>7} {'win':>4} {'−альты мед.':>11} | {'умерли 2г':>9}")
        for name, v in tbl.items():
            r, h = v["R"], v["H"]
            if not r["n"]:
                print(f"   {name:12} {0:>5}")
                continue
            print(f"   {name:12} {r['n']:>5} | {pct(r['med'])} {pct(r['mean'])} {r['win']:>4.0%} "
                  f"{pct(r['ex_alt_med'], 11)} | {pct(h['med'])} {pct(h['mean'])} {h['win']:>4.0%} "
                  f"{pct(h['ex_alt_med'], 11)} | {r['died']:>9.0%}")

    # IC: пул по циклам и внутри дня
    ic: dict = {}
    print("\n   IC (Спирмен фандинга и исхода; знак «−» = чем ниже фандинг, тем лучше — как "
          "задумано в проде)")
    print(f"   {'срез':24} " + " ".join(f"{c:>26}" for c in list(CYCLE_NAMES.values()) + ['всё']))
    for b in J.BOOKS:
        for metric in ("rod", "ex_alt"):
            row = {}
            for c in list(CYCLE_NAMES) + ["всё"]:
                E = [ep for ep in eps if ep["fr"] is not None and ep["out"][b][metric] is not None
                     and (c == "всё" or ep["cycle"] == c)]
                Ex = [ep for ep in E if take(ep)]
                row[c] = {"pool": spearman([ep["fr"] for ep in E], [ep["out"][b][metric] for ep in E]),
                          "pool_n": len(E),
                          "pool_exec": spearman([ep["fr"] for ep in Ex],
                                                [ep["out"][b][metric] for ep in Ex]),
                          "pool_exec_n": len(Ex)}
            ic[f"{b}_{metric}_pool"] = row
            print(f"   {b} {'исход' if metric == 'rod' else '−альты'} пул (все / фильтр) "
                  + " ".join((f"{row[c]['pool'] or 0:+.3f} ({row[c]['pool_n']}) / "
                              f"{row[c]['pool_exec'] or 0:+.3f}").rjust(26)
                             for c in list(CYCLE_NAMES) + ["всё"]))
    # внутри дня — по всем дням сигнала (не только эпизодам), исход R/H
    wd_rows: dict[str, list] = {b: [] for b in J.BOOKS}
    wd_rows_strict: dict[str, list] = {b: [] for b in J.BOOKS}
    for ep in src:
        if ep["fr"] is None:
            continue
        for b in J.BOOKS:
            r = sim(ep, b)
            if r is None or r["spent"] <= 0:
                continue
            wd_rows[b].append((ep["day"], ep["fr"], r["pnl"] / r["spent"], ep["cycle"]))
            if ep["fr_strict"] is not None:
                wd_rows_strict[b].append((ep["day"], ep["fr_strict"], r["pnl"] / r["spent"], ep["cycle"]))
    for b in J.BOOKS:
        row = {}
        for c in list(CYCLE_NAMES) + ["всё"]:
            rr = [(d, f, y) for d, f, y, cy in wd_rows[b] if c == "всё" or cy == c]
            row[c] = within_day_ic(rr)
            rs = [(d, f, y) for d, f, y, cy in wd_rows_strict[b] if c == "всё" or cy == c]
            row[c]["strict"] = within_day_ic(rs)["ic"]
        ic[f"{b}_within_day"] = row
        print(f"   {b} внутри дня (дней; t; строго) "
              + " ".join((f"{row[c]['ic'] or 0:+.3f} ({row[c]['days']}; "
                          f"{row[c]['t'] or 0:+.1f}; {row[c]['strict'] or 0:+.3f})").rjust(26)
                         for c in list(CYCLE_NAMES) + ["всё"]))
    out["ic"] = ic
    # знак порогов: исход корзин «капитуляция» и «эйфория» против средней части
    sign = {}
    for b in J.BOOKS:
        for scope in ("all", "exec"):
            t = out["buckets"][scope]
            mid = [ep for ep in eps if ep["fr"] is not None and CAPITULATION < ep["fr"] < EUPHORIA
                   and (scope == "all" or take(ep))]
            mc = cell([{**ep["out"], "died2y": ep["died2y"]} for ep in mid], b)
            capc, eupc = t[BUCKETS[0][0]][b], t[BUCKETS[-1][0]][b]
            sign[f"{b}_{scope}"] = {
                "mid": mc, "cap_minus_mid_med": (capc["med"] - mc["med"]) if capc["n"] else None,
                "eup_minus_mid_med": (eupc["med"] - mc["med"]) if eupc["n"] else None,
                "cap_minus_mid_exalt": (capc["ex_alt_med"] - mc["ex_alt_med"]) if capc["n"] else None,
                "eup_minus_mid_exalt": (eupc["ex_alt_med"] - mc["ex_alt_med"]) if eupc["n"] else None,
                "by_cycle": {}}
            for c in CYCLE_NAMES:
                E = [ep for ep in eps if ep["fr"] is not None and ep["cycle"] == c
                     and (scope == "all" or take(ep))]
                parts = {k: cell([{**ep["out"], "died2y": ep["died2y"]} for ep in E if f(ep["fr"])], b)
                         for k, f in (("cap", lambda v: v <= CAPITULATION),
                                      ("mid", lambda v: CAPITULATION < v < EUPHORIA),
                                      ("eup", lambda v: v >= EUPHORIA))}
                sign[f"{b}_{scope}"]["by_cycle"][c] = parts
    out["threshold_sign"] = sign
    print("\n   Знак порогов (медиана исхода корзины минус середина −0.03…+0.05%; по циклам n/мед.):")
    for k, v in sign.items():
        cyc = " | ".join(
            f"{CYCLE_NAMES[c]}: кап. {p['cap']['n']}/{pct(p['cap'].get('med'), 6)} сер. "
            f"{p['mid']['n']}/{pct(p['mid'].get('med'), 6)} эйф. {p['eup']['n']}/{pct(p['eup'].get('med'), 6)}"
            for c, p in v["by_cycle"].items())
        print(f"   {k:7} кап.−сер. {pct(v['cap_minus_mid_med'])} (−альты {pct(v['cap_minus_mid_exalt'])}),"
              f" эйф.−сер. {pct(v['eup_minus_mid_med'])} (−альты {pct(v['eup_minus_mid_exalt'])})  ‖ {cyc}")

    # ---- 3. влияние множителя на отбор
    ex_sig = [ep for ep in src if take(ep)]
    fired = {"signals_exec": len(ex_sig),
             "bonus": sum(1 for ep in ex_sig if ep["fr"] is not None and ep["fr"] <= CAPITULATION),
             "penalty": sum(1 for ep in ex_sig if ep["fr"] is not None and ep["fr"] >= EUPHORIA),
             "with_perp": sum(1 for ep in ex_sig if ep["fr"] is not None)}
    live = live_cards(ROOT / "scanner.db")
    out["selection"] = {"history": fired, "live_db": live}
    print(f"\n3. Отбор: из {fired['signals_exec']} дней сигнала после фильтров исполнителя перп есть у "
          f"{fired['with_perp']}; ×1.08 сработал бы {fired['bonus']} ({fired['bonus'] / max(1, fired['with_perp']):.1%}),"
          f" ×0.92 — {fired['penalty']} ({fired['penalty'] / max(1, fired['with_perp']):.1%}). Сдвиг балла ≈ ±2.2 "
          f"(под-балл зоны 9×q, вес 0.28/0.90; при 9×q > 10 бонус не действует)")
    if live:
        print(f"   живой журнал scanner.db ({live['first']} … {live['last']}): строк зоны дна "
              f"{live['spring_rows']}, с фандингом {live['with_funding']}, ×1.08 {live['capitulation']}, "
              f"×0.92 {live['euphoria']}, перевернул порог 70: {len(live['flips'])} "
              f"{live['flips'] or ''}; значения {live['funding_values']}")

    # ---- 4. выход по фандингу
    ref = json.loads((Path(__file__).resolve().parent / "portfolio_vs_market_results.json")
                     .read_text(encoding="utf-8"))["exec"]

    def make_extra(ep, v):
        f = fseg.get(ep["seg"]["sym"])
        if f is None:
            return None
        ts = ep["seg"]["ts"]
        lastf = [-10 ** 9]

        def extra(d, gain, f=f, ts=ts, lastf=lastf):
            if gain <= 0 or d < lastf[0] + 30:
                return 0.0
            fr = f.strict(ts[d] + DAY) if v.get("strict") else f.prod(ts[d] + DAY + SCAN_OFFSET)
            if fr is None or fr < v["thr"]:
                return 0.0
            lastf[0] = d
            return v["frac"]
        return extra

    out["exit"] = {}
    for b in J.BOOKS:
        ob: dict = {"variants": {}}
        for v in EXIT_VARIANTS:
            def simv(ep, v=v, b=b):
                if v["key"] == "base":
                    return sim(ep, b)
                ex = make_extra(ep, v)
                return sim(ep, b) if ex is None else sim(ep, b, ex, v["key"])
            taken = P.exec_book(src, simv, *P.EXEC_POLICY, cooldown=True, cash=True)
            m = P.evaluate_flows(taken, alt, btc)
            if v["key"] == "base":
                s6 = ref[b]["s6"]
                assert m["n"] == s6["n"] and abs(m["pnl"] - s6["pnl"]) < 1e-6 and \
                    abs(m["alt"] - s6["alt"]) < 1e-6, f"{b}: s6 не повторился ({m['pnl']} / {s6['pnl']})"
            m["fund_exits"] = sum(1 for _ep, r in taken if r["how"] == "доп. выход")
            m["changed"] = sum(1 for ep_, r in taken if abs(r["pnl"] - sim(ep_, b)["pnl"]) > 1e-9)
            runs = []
            for sd in range(J.SEEDS):
                tk = P.exec_book(src, simv, *P.EXEC_POLICY, cooldown=True, cash=True,
                                 order="random", seed=sd)
                e2 = P.evaluate_flows(tk, alt, btc)
                runs.append({"pnl": e2["pnl"], "alt": e2["alt"], "btc": e2["btc"], "spent": e2["spent"],
                             "cycles": {c: {"pnl": x["pnl"], "alt": x["alt"]} for c, x in e2["cycles"].items()}})
            m["random"] = {"pnl_mean": st.fmean(x["pnl"] for x in runs),
                           "ex_alt_mean": st.fmean(x["pnl"] - x["alt"] for x in runs),
                           "ex_btc_mean": st.fmean(x["pnl"] - x["btc"] for x in runs),
                           "per_usd": st.fmean(x["pnl"] / x["spent"] for x in runs),
                           "beat_alt_runs": sum(1 for x in runs if x["pnl"] > x["alt"]),
                           "runs": runs}
            ob["variants"][v["key"]] = m
        base_runs = ob["variants"]["base"]["random"]["runs"]
        for v in EXIT_VARIANTS:
            r = ob["variants"][v["key"]]["random"]
            r["beat_base_pnl"] = sum(1 for x, y in zip(r["runs"], base_runs) if x["pnl"] > y["pnl"])
            r["beat_base_ex_alt"] = sum(1 for x, y in zip(r["runs"], base_runs)
                                        if x["pnl"] - x["alt"] > y["pnl"] - y["alt"])
        # эпизоды: где выход по фандингу что-то продал — P&L варианта против базы
        epi = {}
        for v in EXIT_VARIANTS[1:]:
            d_pnl, n_fire, better = [], 0, 0
            for ep in eps:
                ex = make_extra(ep, v)
                if ex is None:
                    continue
                r0, r1 = sim(ep, b), sim(ep, b, ex, v["key"])
                if r1 is None or r0 is None or r1["n_sells"] == r0["n_sells"] and \
                        abs(r1["pnl"] - r0["pnl"]) < 1e-9:
                    continue
                n_fire += 1
                d_pnl.append(r1["pnl"] - r0["pnl"])
                better += r1["pnl"] > r0["pnl"]
            epi[v["key"]] = {"n_changed": n_fire, "sum_delta": sum(d_pnl),
                             "med_delta": st.median(d_pnl) if d_pnl else None,
                             "better_share": better / n_fire if n_fire else None,
                             "of_episodes": len(eps)}
        ob["episodes"] = epi
        out["exit"][b] = ob
        name = "R правила" if b == "R" else "H держать"
        print(f"\n4. Выход по фандингу — книга {name} (s6: 15 мест, $1500; по рангу; средние "
              f"{J.SEEDS} порядков)")
        print(f"   {'вариант':48} {'поз.':>4} {'влож.':>6} {'P&L':>6} {'−альты':>7} {'−BTC':>6} "
              f"{'вых./изм.':>12} | {'P&L ср.':>7} {'−альты ср.':>10} {'лучше базы P&L/−альты':>22}")
        for v in EXIT_VARIANTS:
            m = ob["variants"][v["key"]]
            r = m["random"]
            print(f"   {v['label']:48} {m['n']:>4} {m['spent']:>6.0f} {m['pnl']:>+6.0f} "
                  f"{m['pnl'] - m['alt']:>+7.0f} {m['pnl'] - m['btc']:>+6.0f} {(str(m['fund_exits']) + '/' + str(m['changed'])):>12} | "
                  f"{r['pnl_mean']:>+7.0f} {r['ex_alt_mean']:>+10.0f} "
                  f"{(str(r['beat_base_pnl']) + ' / ' + str(r['beat_base_ex_alt'])):>22}")
        print("   по циклам (по рангу), P&L (−альты): " + "; ".join(
            f"{v['key']}: " + ", ".join(
                f"{c} {x['pnl']:+.0f} ({x['pnl'] - x['alt']:+.0f})"
                for c, x in sorted(ob["variants"][v["key"]]["cycles"].items()))
            for v in EXIT_VARIANTS))
        print("   эпизоды (кулдаун 90 дн.), где выход по фандингу изменил исход: " + "; ".join(
            f"{k}: {e['n_changed']} из {e['of_episodes']}, Σ {e['sum_delta']:+.0f}$, медиана "
            f"{e['med_delta'] or 0:+.2f}$, лучше базы {e['better_share'] or 0:.0%}" for k, e in epi.items()))
        for v in EXIT_VARIANTS:
            ob["variants"][v["key"]]["random"].pop("runs")
        print(f"   [{time.time() - t0:.0f} с]")

    out["meta"]["runtime_s"] = round(time.time() - t0)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
