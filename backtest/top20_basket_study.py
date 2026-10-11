"""Корзина топ-20 альтов — покупаемый бенчмарк для книг R и H.

Вопрос владельца (10.10.2026): индекс альтов (market_daily: total × (1 − BTC.D) − стейблы)
купить нельзя. Сравнить выбранное ботом с тем, что реально можно купить, — с корзиной 20
крупнейших альтов, которая раз в месяц пересобирается по рейтингу капитализации.

Состав: снимки CoinMarketCap listings/historical (неофициальный data-api, без ключа; кэш
.cache/cmc_hist/<дата>.json) на первое воскресенье месяца 2018-01 … (последний день архива
Binance). Цена в снимке CMC совпадает с закрытием дневной свечи Binance того же воскресенья
(медиана расхождения 0.05–0.4%, у соседних дней 1–8%), по нему корзина и собирается. Сбои
капы CMC (монета дороже 5× соседних снимков: Syscoin 02.2025) пропускаются. Из списка
убираются BTC, стейблкоины, обёрнутые и стейкинг-деривативы (WBTC, stETH …),
токенизированное золото/акции; дальше по рангу берутся первые 20 монет, которые есть в архиве Binance (USDT-пара) и чья цена на дату
снимка сходится с ценой CMC в пределах ±20% (так отсекаются тёзки: CMC «BCC» = BitConnect, а
Binance BCCUSDT = Bitcoin Cash 2017–18). В снимках есть погибшие монеты (LUNA, UST, FTT) —
корзина без «ошибки выживших». Глубже 50-го места CMC не берём: если там покупаемых меньше 20
(2018 — на Binance было мало USDT-пар), корзина меньше — это видно в таблице состава.

Варианты: eq — равные веса, cap — веса по капитализации CMC (справочно). Ребаланс раз в месяц,
комиссия 0.15% на оборот (продажи + покупки). Монета, чей ряд оборвался внутри месяца
(делистинг, переименование, редоминация), до ребаланса оценивается по последнему закрытию.
Уровень корзины по дням → scanner.benchmark.flow_pnl, как в месячной сводке: каждая покупка
книги — та же сумма в корзину в тот же день, продажа — та же доля.

Книги — как в portfolio_vs_market, шаг s6 (детектор прода L.is_spring_prod, кулдаун 90 дн.,
свободный USDT $1500, без горизонта, правила исполнения EXEC_ALL), по рангу и при J.SEEDS
случайных порядках сигналов внутри дня. Сверка: по рангу R обязан повторить s6 из
portfolio_vs_market_results.json. Дополнительно: была ли монета каждой покупки бота в
корзине топ-20 на дату входа (и на каком месте CMC).

Оговорки: CMC data-api неофициальный; архив только Binance (монет, которых не было на Binance,
в корзине нет — их заменяют следующие по рангу); веса считаются по закрытию суток без
проскальзывания; корзина держит ETH/BNB/XRP так же, как мелкие альты (равные веса).

Запуск из корня проекта:  py -3 backtest/top20_basket_study.py
Результат: backtest/top20_basket_results.json
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import junk_filter_study as J  # noqa: E402
import ladder_dca_study as L  # noqa: E402
import portfolio_vs_market as P  # noqa: E402
from scanner.benchmark import flow_pnl  # noqa: E402

OUT = Path(__file__).resolve().parent / "top20_basket_results.json"
CACHE = ROOT / ".cache" / "cmc_hist"
URL = ("https://api.coinmarketcap.com/data-api/v3/cryptocurrency/listings/historical"
       "?date={d}&limit=200&start=1&convertId=2781")
DAY = 86400
FEE = 0.0015
N = 20
MAX_RANK = 50                    # глубже 50-го места CMC монета уже не «топ», корзина меньше
PRICE_TOL = 0.20
START, END = (2018, 1), (2026, 10)

STABLES = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "PAX", "USDP", "GUSD", "UST", "USTC", "FDUSD",
           "USDE", "USDS", "PYUSD", "USDD", "USD1", "FRAX", "LUSD", "EURS", "HUSD", "USDN", "SUSD",
           "USDK", "USDX", "RSV", "EURC", "USDB", "USDY", "USD0", "RLUSD", "USDG", "USDF",
           "BSC-USD", "USDT0", "USDTB", "USDC.E", "SUSDE", "SUSDS", "SDAI", "BFUSD", "USYC",
           "BUIDL", "OUSG", "USTB", "USDL", "GHO", "CRVUSD", "DOLA", "MIM", "FEI", "TRIBE"}
WRAPPED = {"WBTC", "WETH", "STETH", "WSTETH", "WEETH", "EETH", "CBBTC", "RETH", "CBETH",
           "WBETH", "METH", "EZETH", "RSETH", "JITOSOL", "MSOL", "BNSOL", "JUPSOL", "STSOL",
           "SFRXETH", "LSETH", "OSETH", "BTCB", "HBTC", "RENBTC", "LBTC", "SOLVBTC", "CLBTC",
           "TBTC", "WBNB", "WTRX", "WEOS", "BETH", "WHBAR", "XAUT", "PAXG", "DGX", "STX.B",
           "SAVAX", "SOLVBTC.BBN", "BBSOL", "PUMPBTC", "UNIBTC", "ENZOBTC", "FBTC", "WBT.B"}
# Точные теги CMC: «*-stablecoin-algorithmically-stabilized» стоит у LUNA, «tokenized-stock» —
# у LINK, «liquid-staking-derivatives» — у LDO; это обычные альты, их теги не трогаем.
STABLE_TAGS = {"stablecoin", "usd-stablecoin", "fiat-stablecoin", "asset-backed-stablecoin",
               "stablecoin-asset-backed"}
BAD_TAGS = {"wrapped-tokens", "tokenized-gold", "tokenized-commodities"}
# CMC-тикер -> базы Binance (по порядку); любая кандидатура проходит сверку цены.
ALIASES = {"MIOTA": ["IOTA"], "BCH": ["BCC", "BCHABC", "BCH"], "BSV": ["BCHSV", "BSV"],
           "BCHSV": ["BCHSV"], "XRB": ["NANO"], "MATIC": ["MATIC", "POL"], "POL": ["POL", "MATIC"],
           "VET": ["VET", "VEN"], "VEN": ["VEN", "VET"], "RNDR": ["RNDR", "RENDER"],
           "RENDER": ["RENDER", "RNDR"], "LUNC": ["LUNC", "LUNA"], "LUNA": ["LUNA"],
           "FTM": ["FTM", "S"], "S": ["S", "FTM"], "EOS": ["EOS", "A"], "A": ["A", "EOS"],
           "KLAY": ["KLAY", "KAIA"], "KAIA": ["KAIA", "KLAY"], "ERD": ["ERD", "EGLD"],
           "EGLD": ["EGLD", "ERD"], "BTT": ["BTT", "BTTC"], "BTTC": ["BTTC", "BTT"],
           "NPXS": ["NPXS", "PUNDIX"], "XZC": ["XZC", "FIRO"], "LEND": ["LEND", "AAVE"],
           "MKR": ["MKR"], "SKY": ["SKY", "MKR"], "STRAT": ["STRAT", "STRAX"],
           "HOT": ["HOT"], "TON": ["TON"], "XEC": ["XEC"], "BCHA": ["BCHA", "XEC"]}


# ---------------------------------------------------------------- данные

def first_sundays() -> list[dt.date]:
    out = []
    y, m = START
    while (y, m) <= END:
        d = dt.date(y, m, 1)
        out.append(d + dt.timedelta(days=(6 - d.weekday()) % 7))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def day_ts(d: dt.date) -> int:
    """Метка дневной свечи Binance (00:00 UTC), секунды."""
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp())


def snapshot(d: dt.date) -> list[dict]:
    """Снимок CMC на дату (кэш на диске, пауза 1 с между запросами к сети)."""
    f = CACHE / f"{d.isoformat()}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    CACHE.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(URL.format(d=d.isoformat()), headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        try:
            data = json.loads(urllib.request.urlopen(req, timeout=40).read())["data"]
            break
        except Exception as e:  # noqa: BLE001 — сеть: повтор с паузой
            print(f"  CMC {d}: {e}; повтор")
            time.sleep(5 * (attempt + 1))
    else:
        raise RuntimeError(f"CMC {d}: нет ответа")
    rows = [{"rank": x["cmcRank"], "symbol": x["symbol"], "name": x["name"],
             "tags": x.get("tags") or [], "price": x["quotes"][0]["price"],
             "mcap": x["quotes"][0]["marketCap"]} for x in data]
    f.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    time.sleep(1.0)
    return rows


def excluded(x: dict) -> str | None:
    s, name = x["symbol"].upper(), x["name"].lower()
    if s == "BTC":
        return "BTC"
    if s in STABLES or STABLE_TAGS & set(x["tags"]):
        return "стейблкоин"
    if s in WRAPPED or name.startswith(("wrapped ", "staked ", "bridged ")) or \
            BAD_TAGS & set(x["tags"]):
        return "обёртка/стейкинг/токенизация"
    return None


class Prices:
    """Отрезки архива Binance по базе тикера: close на день и последнее закрытие ≤ дня."""

    def __init__(self, segs: list[dict]):
        self.by_base: dict[str, list[dict]] = {}
        for s in segs:
            base = s["sym"].split("#")[0]
            if base.endswith("USDT"):
                self.by_base.setdefault(base[:-4], []).append(s)
        self.close = {s["sym"]: dict(zip(s["ts"], s["c"])) for s in segs}
        self.seg = {s["sym"]: s for s in segs}

    def match(self, x: dict, day: int) -> tuple[str, float] | None:
        """Отрезок Binance, торгующийся в day с ценой в пределах PRICE_TOL от CMC."""
        s = x["symbol"].upper()
        for base in ALIASES.get(s, []) + [s]:
            for seg in self.by_base.get(base, []):
                c = self.close[seg["sym"]].get(day)
                if c and x["price"] and abs(c / x["price"] - 1) <= PRICE_TOL:
                    return seg["sym"], c / x["price"]
        return None

    def last(self, sym: str, day: int) -> float | None:
        """Последнее закрытие отрезка не позже day (после обрыва — замороженная цена)."""
        seg = self.seg[sym]
        i = bisect.bisect_right(seg["ts"], day) - 1
        return seg["c"][i] if i >= 0 else None


def glitches(snaps: list[list[dict]]) -> list[set[str]]:
    """Сбои капы CMC: монета в снимке дороже 5× и прошлого, и следующего своего снимка (оба
    должны быть в топ-200) или в ручном списке GLITCHES. -> по снимкам множества symbol|name."""
    caps = [{f"{x['symbol']}|{x['name']}": x["mcap"] or 0.0 for x in rows} for rows in snaps]
    out = []
    for k, cm in enumerate(caps):
        bad = set()
        for key, v in cm.items():
            nb = [caps[j].get(key) for j in (k - 1, k + 1) if 0 <= j < len(caps)]
            if len(nb) == 2 and all(nb) and v > 5 * max(nb):
                bad.add(key)
        out.append(bad)
    return out


# Syscoin 02.02.2025: 10-е место с капой $23 млрд (реально ~$0.06 млрд); в соседних снимках
# его нет в топ-200, поэтому автоматическая проверка его не ловит.
GLITCHES = {("2025-02-02", "SYS|Syscoin")}


def compositions(px: Prices, last_day: int) -> list[dict]:
    """Состав по месяцам: {date, day, basket:[{sym, rank, cmc, mcap}], ranked:{sym: rank},
    skipped:[…], depth}. ranked — все сопоставленные монеты снимка (для доли покупок бота).
    Снимки позже последнего дня архива не берутся."""
    days = [d for d in first_sundays() if day_ts(d) <= last_day]
    snaps = [snapshot(d) for d in days]
    bad = glitches(snaps)
    out = []
    for d, rows, gl in zip(days, snaps, bad):
        gl = gl | {k for dd, k in GLITCHES if dd == d.isoformat()}
        # цена снимка CMC = закрытие дневной свечи Binance того же дня (проверено: медиана
        # расхождения 0.05–0.4% против 1–8% у соседних дней)
        day = day_ts(d)
        basket, ranked, skipped = [], {}, []
        for x in sorted(rows, key=lambda r: r["rank"]):
            if f"{x['symbol']}|{x['name']}" in gl:
                skipped.append(f"{x['symbol']}#{x['rank']}(сбой капы)")
                continue
            m = px.match(x, day)
            if m:
                ranked.setdefault(m[0], x["rank"])
            if len(basket) >= N or x["rank"] > MAX_RANK or excluded(x):
                continue
            if not m:
                skipped.append(f"{x['symbol']}#{x['rank']}")
                continue
            if any(b["sym"] == m[0] for b in basket):
                continue
            basket.append({"sym": m[0], "rank": x["rank"], "cmc": x["symbol"],
                           "mcap": x["mcap"] or 0.0, "ratio": m[1]})
        out.append({"date": d.isoformat(), "day": day, "basket": basket, "ranked": ranked,
                    "skipped": skipped, "depth": basket[-1]["rank"] if basket else None})
    return out


# ---------------------------------------------------------------- уровень корзины

def basket_level(px: Prices, comps: list[dict], weights: str, end_day: int) -> tuple[dict, dict]:
    """day -> уровень корзины (1.0 в день первой сборки), и сводка оборота/комиссий."""
    level: dict[int, float] = {}
    units: dict[str, float] = {}
    value = 1.0
    turn = fees = 0.0
    for k, c in enumerate(comps):
        day = c["day"]
        nxt = comps[k + 1]["day"] if k + 1 < len(comps) else end_day + DAY
        b = [x for x in c["basket"]]
        if not b:
            continue
        cur = {s: u * (px.last(s, day) or 0.0) for s, u in units.items()}
        if units:
            value = sum(cur.values())
        if weights == "eq":
            w = {x["sym"]: 1 / len(b) for x in b}
        else:
            tot = sum(x["mcap"] for x in b)
            w = {x["sym"]: x["mcap"] / tot for x in b}
        if units:
            tr = sum(abs(value * w.get(s, 0.0) - cur.get(s, 0.0)) for s in set(w) | set(cur))
            fee = FEE * tr
            value -= fee
            turn += tr / (value + fee)
            fees += fee / (value + fee)
        units = {s: value * wt / px.close[s][day] for s, wt in w.items()}
        t = day
        while t < nxt and t <= end_day:
            level[t] = sum(u * (px.last(s, t) or 0.0) for s, u in units.items())
            t += DAY
    n_reb = max(len(comps) - 1, 1)
    return level, {"turnover_avg": turn / n_reb, "fee_share_total": fees}


def growth(series: dict[int, float], d0: int, d1: int) -> float | None:
    a, b = P.at(series, d0), P.at(series, d1)
    return b / a - 1 if a and b else None


# ---------------------------------------------------------------- книги против бенчмарков

BENCH = [("alt", "индекс альтов"), ("eq", "корзина топ-20, равные веса"),
         ("cap", "корзина топ-20, по капе"), ("btc", "BTC")]
SHORT = {"alt": "альты", "eq": "корз.", "cap": "корз./кап", "btc": "BTC"}


def flows_vs(taken: list[tuple[dict, dict]], series: dict[str, dict]) -> dict:
    """P&L книги и каждого бенчмарка по денежным потокам (как P.evaluate_flows), в целом и по
    циклам. Позиция без уровня какого-то бенчмарка на дату потока — в dropped."""
    rows, dropped = [], 0
    for ep, r in taken:
        if r["spent"] <= 0:
            continue
        ts = ep["seg"]["ts"]
        d1 = ep["day"] + max(r["span"], 1) * DAY
        flows = [{"ts": ts[d], "side": sd, "usd": usd, "frac": fr} for d, sd, usd, fr in r["flows"]]
        b = {k: flow_pnl(flows, lambda t, s=s: P.at(s, t), d1, FEE) for k, s in series.items()}
        if None in b.values():
            dropped += 1
            continue
        rows.append({"spent": r["spent"], "pnl": r["pnl"], "cycle": ep["cycle"], **b})

    def tot(rr):
        o = {"n": len(rr), "spent": sum(x["spent"] for x in rr), "pnl": sum(x["pnl"] for x in rr)}
        for k in series:
            o[k] = sum(x[k] for x in rr)
            o[f"beat_{k}_share"] = sum(1 for x in rr if x["pnl"] > x[k]) / len(rr) if rr else 0.0
        return o
    out = tot(rows)
    out["dropped"] = dropped
    out["cycles"] = {c: tot([x for x in rows if x["cycle"] == c]) for c, _a, _b in L.CYCLES}
    return out


def coverage(taken: list[tuple[dict, dict]], comps: list[dict], series: dict[str, dict]) -> dict:
    """Где была монета каждой покупки бота в рейтинге CMC на последнюю сборку корзины ≤ дня
    входа: в корзине топ-20 / место ≤ 50 вне корзины / 51–100 / 101–200 / нет в топ-200."""
    days = [c["day"] for c in comps]
    groups: dict[str, list] = {}
    for ep, r in taken:
        k = bisect.bisect_right(days, ep["day"]) - 1
        if k < 0:
            g = "до первой корзины"
        else:
            c, sym = comps[k], ep["seg"]["sym"]
            rk = c["ranked"].get(sym)
            if any(b["sym"] == sym for b in c["basket"]):
                g = "в корзине топ-20"
            elif rk is None:
                g = "нет в топ-200 CMC"
            elif rk <= 50:
                g = "место CMC ≤ 50, не в корзине"
            elif rk <= 100:
                g = "место CMC 51–100"
            else:
                g = "место CMC 101–200"
        groups.setdefault(g, []).append((ep, r))
    out = {}
    for g, tk in groups.items():
        m = flows_vs(tk, series)
        m.pop("cycles")
        m["coins"] = sorted({ep["seg"]["sym"].split("#")[0][:-4] for ep, _r in tk})
        out[g] = m
    return out


def main() -> int:
    t0 = time.time()
    coins = L.load_archive()
    all_segs = L.segments(coins)
    px = Prices(all_segs)
    btc = P.btc_closes(all_segs)
    last_day = max(btc)
    comps = compositions(px, last_day)
    print(f"сборок корзины: {len(comps)} ({comps[0]['date']} … {comps[-1]['date']}), архив до "
          f"{dt.datetime.fromtimestamp(last_day, dt.timezone.utc).date()}  [{time.time() - t0:.0f} с]")
    small = [(c["date"], len(c["basket"])) for c in comps if len(c["basket"]) < N]
    print("  меньше 20 монет: " + ", ".join(f"{d} {n}" for d, n in small))
    alt, _ = P.market_index(ROOT / "scanner.db")
    lv_eq, st_eq = basket_level(px, comps, "eq", last_day)
    lv_cap, st_cap = basket_level(px, comps, "cap", last_day)
    series = {"alt": alt, "eq": lv_eq, "cap": lv_cap, "btc": btc}
    print(f"  оборот за ребаланс: равные веса {st_eq['turnover_avg'] * 100:.0f}%, по капе "
          f"{st_cap['turnover_avg'] * 100:.0f}%; комиссии ребаланса за всё время "
          f"{st_eq['fee_share_total'] * 100:.1f}% / {st_cap['fee_share_total'] * 100:.1f}% капитала")

    # сами бенчмарки: рост за циклы (купить в начале цикла, продать в конце)
    print("\nРост бенчмарков, купить и держать:")
    marks = [("всё время", comps[0]["day"], last_day)] + \
        [(c, max(a, comps[0]["day"]), min(b, last_day)) for c, a, b in L.CYCLES]
    out: dict = {"meta": {"fee": FEE, "n": N, "max_rank": MAX_RANK, "price_tol": PRICE_TOL,
                          "first": comps[0]["date"], "last": comps[-1]["date"],
                          "archive_last_day": last_day, "seeds": J.SEEDS,
                          "turnover": {"eq": st_eq, "cap": st_cap}},
                 "growth": {}, "compositions": [
                     {"date": c["date"], "n": len(c["basket"]), "depth": c["depth"],
                      "coins": [b["cmc"] for b in c["basket"]], "skipped": c["skipped"]}
                     for c in comps]}
    for lab, a, b in marks:
        g = {k: growth(s, a, b) for k, s in series.items()}
        out["growth"][lab] = g
        print(f"  {lab:10} " + "  ".join(f"{n} {g[k] * 100:+.0f}%" for k, n in BENCH
                                         if g[k] is not None))

    # книги, шаг s6 как в portfolio_vs_market
    ranks = L.vol_ranks(all_segs)
    npairs = P.pairs_per_day(ranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot = L.load_hot()
    src = [ep for ep in L.signal_days(segs, ranks, hot, {"prod": L.is_spring_prod})["prod"]
           if ep["cycle"] != "?"]
    for ep in src:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
    ref = json.loads((Path(__file__).resolve().parent / "portfolio_vs_market_results.json")
                     .read_text(encoding="utf-8"))["exec"]
    print(f"\nдней сигнала прода {len(src)}  [{time.time() - t0:.0f} с]")
    for b, (sell, _stop) in J.BOOKS.items():
        name = "R правила" if b == "R" else "H держать"
        cache: dict = {}

        def sim(ep, sell=sell, cache=cache):
            k = (ep["seg"]["sym"], ep["e"])
            if k not in cache:
                cache[k] = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET,
                                           horizon=None, rules=L.EXEC_ALL)
            return cache[k]
        taken = P.exec_book(src, sim, *P.EXEC_POLICY, cooldown=True, cash=True)
        chk = P.evaluate_flows(taken, alt, btc)
        s6 = ref[b]["s6"]
        assert chk["n"] == s6["n"] and abs(chk["pnl"] - s6["pnl"]) < 1e-6 and \
            abs(chk["alt"] - s6["alt"]) < 1e-6, f"{b}: s6 не повторился ({chk['pnl']} / {s6['pnl']})"
        m = flows_vs(taken, series)
        runs = [flows_vs(P.exec_book(src, sim, *P.EXEC_POLICY, cooldown=True, cash=True,
                                     order="random", seed=sd), series) for sd in range(J.SEEDS)]
        m["random"] = {k: {"vs_mean": statistics.fmean(x["pnl"] - x[k] for x in runs),
                           "vs_range": [min(x["pnl"] - x[k] for x in runs),
                                        max(x["pnl"] - x[k] for x in runs)],
                           "vs_per_usd_mean": statistics.fmean((x["pnl"] - x[k]) / x["spent"]
                                                               for x in runs),
                           "beat_runs": sum(1 for x in runs if x["pnl"] > x[k])}
                       for k, _n in BENCH}
        m["random"]["pnl_mean"] = statistics.fmean(x["pnl"] for x in runs)
        m["coverage"] = coverage(taken, comps, series)
        out[b] = m
        print(f"\n=== книга {name}: поз. {m['n']} (без уровня бенчмарка {m['dropped']}), вложено "
              f"{m['spent']:.0f}$, P&L {m['pnl']:+.0f}$ ({m['pnl'] / m['spent'] * 100:+.1f}% на $); "
              f"сверка s6: P&L {chk['pnl']:+.0f}$, к альтам {chk['pnl'] - chk['alt']:+.0f}$")
        print(f"  {'тот же $ в те же дни в…':30} {'P&L рынка':>10} {'книга − рынок':>14} "
              f"{'на $':>7} {'позиций лучше':>13} | {J.SEEDS} порядков: книга − рынок, лучше")
        for k, n in BENCH:
            ro = m["random"][k]
            print(f"  {n:30} {m[k]:>+9.0f}$ {m['pnl'] - m[k]:>+13.0f}$ "
                  f"{(m['pnl'] - m[k]) / m['spent'] * 100:>+6.1f}% "
                  f"{m[f'beat_{k}_share'] * 100:>12.0f}% | {ro['vs_mean']:+.0f}$ "
                  f"({ro['vs_range'][0]:+.0f}…{ro['vs_range'][1]:+.0f}), {ro['beat_runs']}/{J.SEEDS}")
        print("  по циклам (по рангу), P&L книги и та же сумма в рынок:")
        for c, v in m["cycles"].items():
            if v["n"]:
                print(f"    {c:8} поз. {v['n']:>3} вложено {v['spent']:>6.0f}$ P&L {v['pnl']:>+6.0f}$ "
                      f"({v['pnl'] / v['spent'] * 100:+.0f}% на $) | "
                      + "  ".join(f"{SHORT[k]} {v[k]:+.0f}$" for k, _n in BENCH))
        print("  где были монеты покупок бота в рейтинге CMC на дату входа:")
        for g, v in sorted(m["coverage"].items(), key=lambda kv: -kv[1]["n"]):
            if v["n"]:
                print(f"    {g:30} поз. {v['n']:>3} ({v['n'] / m['n'] * 100:>3.0f}%) вложено "
                      f"{v['spent']:>6.0f}$ P&L {v['pnl']:>+6.0f}$ ({v['pnl'] / v['spent'] * 100:+.0f}% "
                      f"на $), та же сумма в корзину {v['eq']:+.0f}$, в альты {v['alt']:+.0f}$  "
                      f"[{', '.join(v['coins'][:12])}{'…' if len(v['coins']) > 12 else ''}]")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
