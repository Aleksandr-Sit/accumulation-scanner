"""Лимит пробного исполнителя по сектору: не больше K монет одного сектора в книге (Q14).

Вопрос (16.10.2026): книга из 15 мест может набрать несколько монет одного сектора (мемы,
AI, L1…), которые падают и растут вместе — тогда 15 мест на деле меньше 15 независимых
ставок. Правило: не больше K (2 / 3 / 4) открытых позиций одного сектора; сигнал сверх лимита
пропускается, место и деньги — следующим сигналам дня (как квота). Меняет ли это P&L, P&L к
рынку, связанность книги и просадку?

Модель — книги R и H, шаг s6 (portfolio_vs_market.exec_book: 15 мест, лестница $50, кулдаун
90 дн., свободный USDT $1500, нижняя четверть по обороту и перегрев, квота неликвида 5 мест);
лимит — новый параметр exec_book group_cap=(сектор(ep), K), None — как раньше (бит-в-бит);
сверка: база повторяет s6 (R +$1503, H +$1903), «−альты» у R −$186. Книги независимы.

Сектор — по тегам CMC (поле tags снимков .cache/cmc_hist_deep, первое воскресенье месяца).
Монета Binance сопоставляется со строкой снимка ровно как Q9/Q11: последний снимок не позже
дня входа, тикер + цена ±20% (top20_basket_study.Prices.match), лучшее место, без сбоев капы;
берём id CMC этой строки. Покрытие тегов: до 2020-06 CMC отдаёт только «mineable» (или пусто)
— секторной таксономии нет; с 2020-07 — секторные теги (defi, platform, ai-big-data, memes,
gaming…), «layer-1» — только с 2023-05, широкие ярлыки бирж/экосистем — с 2025. Поэтому:
  теги на входе — снимок входа, если он не раньше TAGS_FROM (2020-07-05) и теги у id есть;
  иначе ближайший ЛЕТНИЙ снимок ≥ TAGS_FROM с тегами у того же id (заглядывание вперёд — только
  для классификации, не для цен и не для отбора: все входы 2018–20.06 классифицированы так);
  иначе ближайший прошлый снимок с тегами; иначе «unknown». Не сопоставлена — «unknown».
Сведение тегов к секторам — список RULES, первое совпадение сверху вниз (теги *-portfolio,
*-ecosystem, кроме перечисленных, и ярлыки бирж не используются):
  1. exchange (биржевые токены): centralized-exchange;
  2. memes: memes, doggone-doggerel, animal/cat/ai/political/celebrity/ip/startup-memes,
     pump-fun-ecosystem, four-meme-ecosystem;
  3. ai: ai-big-data, generative-ai, ai-agents, defai, ai-applications, ai-agent-launchpad,
     virtuals-protocol-ecosystem, bittensor-ecosystem / -subnets;
  4. rwa: real-world-assets, tokenized-assets/-gold/-commodities/-stock/-etfs, real-estate,
     asset-backed-token;
  5. gaming (игры/метавселенные/NFT): gaming, metaverse, play-to-earn, gaming-guild, vr-ar,
     move-to-earn, tap-to-earn, trading-card-game;
  6. layer-2: layer-2, rollups, sidechain, rollups-as-a-service;
  7. layer-1: явный тег layer-1;
  8. defi: defi, (decentralized-)exchange-dex/dex/amm, yield-farming, lending-borowing,
     derivatives, yield-aggregator, liquid-staking-derivatives, synthetics, perpetuals,
     perp-dex-coins, options, insurance, stablecoin-protocol, restaking, defi-index,
     cross-chain-dex-aggregator, defi-2;
  9. infra (хранение/вычисления/инфраструктура): storage, filesharing, ipfs,
     distributed-computing, depin, oracles, iot, data-availability, hardware;
  затем слабые (широкие) теги — только если сильных нет:
 10. gaming: collectibles-nfts, fan-token;   11. rwa: real-world-assets-protocols;
 12. layer-1: platform, smart-contracts;     13. layer-2: scaling;
 14. infra: interoperability, cross-chain, modular-blockchain;
  иначе — other («прочее»: платёжные/PoW/приватность — BTC, LTC, XRP, XMR…, enterprise…).
  state-channel(s) не используется: его носит BTC (Lightning) — иначе BTC попадал бы в L2.
Почему такой порядок: узкие «нарративные» теги (биржа, мем, AI, RWA, игры) раньше широких —
у монеты с мем- и defi-тегом цена ходит как мем; явный «layer-1» раньше «defi», потому что CMC
со временем навешивает defi на платформы (NEO, QTUM, ICX, XLM), а layer-1 ставит точечно;
широкие ярлыки (platform, smart-contracts, collectibles-nfts, real-world-assets-protocols —
последний в 2025 получили ALGO, XLM, VET, IOTA) — только запасные. Предложенный владельцем
порядок (… defi > infra > layer-1) отличается только местом явного layer-1.

Варианты (книги независимы): база (прод, по обороту); K = 2 / 3 / 4 при unknown без лимита
(«K3»), с лимитом как свой сектор («K3u»); справочно K = 3, где other и unknown без лимита
(«K3o»). «Прочее» (other) по умолчанию — сектор с лимитом, как остальные.

Метрики как Q9/Q11/Q13: P&L, P&L − альты / корзина топ-20 по капе / BTC по денежным потокам,
J.SEEDS случайных порядков сигналов внутри дня (в скольких лучше базы того же порядка),
вложено, позиций, умерших за 2 года, сколько из лучших 10% исходов базы пропало (прод по
обороту), новые / ушедшие позиции к базе; по циклам 2018–20 / 2020–22 / 2023–26 (по дню
входа); вне выборки — K выбран по 2018–22 (среднее порядков, P&L − альты), проверка на 2023–26.
Плюс:
  связанность книги — средняя попарная корреляция дневных лог-доходностей монет, которые книга
    держит (от первой исполненной покупки до дня выхода), по дням сетки через каждые 7 дней
    (CORR_STRIDE), где таких монет ≥ 2; окно — 30 дневных доходностей по день включительно
    (закрытия Binance, у монеты нужны все 31 закрытия, иначе в этот день она не считается);
    среднее по парам = (Σ_t (Σ_i z_it)² / T − n) / (n(n − 1)), z — стандартизованные
    доходности; число в таблице — среднее по таким дням;
  макс. монет одного сектора в книге (места, без unknown) и среднее по дням этого максимума;
  просадка книги — по дневной стоимости счёта ($1500 + накопленный P&L позиций: монеты ×
    закрытие за вычетом комиссии выхода + свободный USDT), макс. падение от пика, % (и $):
    core_satellite_study (Q15) — Grid, position_days, portfolio, SIM_Q (копия simulate_exec с
    количеством монет, сверяется с оригиналом на каждой позиции).

Оговорки: теги CMC — ярлыки, а не измеренная связанность; таксономия менялась (секторы
2018–20 — по тегам 2020-07, т.е. взгляд из будущего, а теги 2025+ шире и шумнее); у монеты
с несколькими нарративами сектор зависит от порядка RULES; совпадение тикера и цены иногда
даёт чужой id (как в Q9); выжившие ×0.73, правила книг подобраны на той же истории — все
цифры верхняя граница; 3 цикла — мало для вывода о вне выборки.

Запуск из корня проекта:  PYTHONIOENCODING=utf-8 python backtest/sector_cap_study.py
Время прогона: ~4.5 мин (275 с 16.10.2026, из них ~190 с — архив и сигналы; 8 вариантов ×
  31 прогон × 2 книги); сеть не нужна (все снимки в кэше).
Результат: backtest/sector_cap_results.json
"""
from __future__ import annotations

import bisect
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
import core_satellite_study as C  # noqa: E402
import junk_filter_study as J  # noqa: E402
import ladder_dca_study as L  # noqa: E402
import portfolio_vs_market as P  # noqa: E402
import size_band_study as S  # noqa: E402
import supply_age_gate_study as Q  # noqa: E402
import top20_basket_study as T  # noqa: E402

OUT = Path(__file__).resolve().parent / "sector_cap_results.json"
DAY = 86400
TAGS_FROM = dt.date(2020, 7, 5)          # первый снимок с секторной таксономией CMC
CORR_WIN = 30
CORR_STRIDE = 7
IS_CYCLES = G.IS_CYCLES
OOS_CYCLE = G.OOS_CYCLE
BENCH = G.BENCH
KS = (2, 3, 4)

# (сектор, теги) — первое совпадение сверху вниз; сектор может встретиться дважды:
# сначала сильные теги, затем широкие запасные.
RULES: list[tuple[str, set[str]]] = [
    ("exchange", {"centralized-exchange"}),
    ("memes", {"memes", "doggone-doggerel", "animal-memes", "cat-themed", "ai-memes",
               "political-memes", "celebrity-memes", "ip-memes", "startup-memes",
               "pump-fun-ecosystem", "four-meme-ecosystem"}),
    ("ai", {"ai-big-data", "generative-ai", "ai-agents", "defai", "ai-applications",
            "ai-agent-launchpad", "virtuals-protocol-ecosystem", "bittensor-ecosystem",
            "bittensor-subnets"}),
    ("rwa", {"real-world-assets", "tokenized-assets", "tokenized-gold", "tokenized-commodities",
             "tokenized-stock", "tokenized-etfs", "real-estate", "asset-backed-token"}),
    ("gaming", {"gaming", "metaverse", "play-to-earn", "gaming-guild", "vr-ar", "move-to-earn",
                "tap-to-earn", "trading-card-game"}),
    ("layer-2", {"layer-2", "rollups", "sidechain", "rollups-as-a-service"}),
    ("layer-1", {"layer-1"}),
    ("defi", {"defi", "decentralized-exchange", "decentralized-exchange-dex-token", "dex", "amm",
              "yield-farming", "lending-borowing", "derivatives", "yield-aggregator",
              "liquid-staking-derivatives", "synthetics", "perpetuals", "perp-dex-coins",
              "options", "insurance", "stablecoin-protocol", "restaking", "defi-index",
              "cross-chain-dex-aggregator", "defi-2"}),
    ("infra", {"storage", "filesharing", "ipfs", "distributed-computing", "depin", "oracles",
               "iot", "data-availability", "hardware"}),
    # широкие запасные теги
    ("gaming", {"collectibles-nfts", "fan-token"}),
    ("rwa", {"real-world-assets-protocols"}),
    ("layer-1", {"platform", "smart-contracts"}),
    ("layer-2", {"scaling"}),
    ("infra", {"interoperability", "cross-chain", "modular-blockchain"}),
]
SECTORS = ["exchange", "memes", "ai", "rwa", "gaming", "layer-2", "layer-1", "defi", "infra",
           "other", "unknown"]
SECTOR_RU = {"exchange": "биржевые", "memes": "мемы", "ai": "AI", "rwa": "RWA",
             "gaming": "игры/NFT", "layer-2": "L2", "layer-1": "L1", "defi": "DeFi",
             "infra": "хранение/инфра", "other": "прочее", "unknown": "unknown"}


def sector_of(tags: list[str]) -> tuple[str, int]:
    """-> (сектор, номер правила) по RULES; ничего не совпало — ("other", −1)."""
    ts = set(tags)
    for i, (sec, tt) in enumerate(RULES):
        if ts & tt:
            return sec, i
    return "other", -1


def variants() -> list[dict]:
    vs = [{"key": "base", "label": "база (прод, по обороту)", "cap": None}]
    for k in KS:
        vs.append({"key": f"K{k}", "label": f"≤ {k} на сектор, unknown без лимита", "cap": k,
                   "free": {"unknown"}})
    for k in KS:
        vs.append({"key": f"K{k}u", "label": f"≤ {k} на сектор, unknown с лимитом", "cap": k,
                   "free": set()})
    vs.append({"key": "K3o", "label": "справ.: ≤ 3, прочее и unknown без лимита", "cap": 3,
               "free": {"unknown", "other"}})
    return vs


def cap_of(v: dict):
    if v["cap"] is None:
        return None
    free = v["free"]
    return (lambda ep: None if ep["sector"] in free else ep["sector"]), v["cap"]


# ---------------------------------------------------------------- теги и сектор на входе

def classify(src, snaps_ids, days, dates, ddates, dtags) -> dict:
    """ep["sector"], ep["sec_src"] (entry / ahead / behind / no_tags / unmatched), ep["tags"]."""
    kpos = {d: i for i, d in enumerate(ddates)}
    first_tagged = next(i for i, d in enumerate(ddates) if d >= TAGS_FROM)
    rule_hits: dict[int, int] = {}
    for ep in src:
        k = bisect.bisect_right(days, ep["day"]) - 1
        hit = snaps_ids[k].get(ep["seg"]["sym"]) if 0 <= k < len(snaps_ids) else None
        ep["cmc"] = hit[0] if hit else None
        ep["cmc_id"] = hit[1] if hit else None
        if not hit:
            ep["sector"], ep["sec_src"], ep["tags"], ep["rule"] = "unknown", "unmatched", None, None
            continue
        cid, kk = hit[1], kpos[dates[k]]
        j, srcname = None, None
        if kk >= first_tagged and dtags[kk].get(cid):
            j, srcname = kk, "entry"
        else:
            for jj in range(max(kk + 1, first_tagged), len(ddates)):
                if dtags[jj].get(cid):
                    j, srcname = jj, "ahead"
                    break
            if j is None:
                for jj in range(kk - 1, first_tagged - 1, -1):
                    if dtags[jj].get(cid):
                        j, srcname = jj, "behind"
                        break
        if j is None:
            ep["sector"], ep["sec_src"], ep["tags"], ep["rule"] = "unknown", "no_tags", None, None
            continue
        tags = dtags[j][cid]
        sec, rule = sector_of(tags)
        ep["sector"], ep["sec_src"], ep["tags"], ep["rule"] = sec, srcname, ddates[j].isoformat(), rule
        rule_hits[rule] = rule_hits.get(rule, 0) + 1
    return rule_hits


# ---------------------------------------------------------------- связанность и просадка

class Corr:
    """Средняя попарная корреляция дневных лог-доходностей монет книги (см. докстринг)."""

    def __init__(self, segs: list[dict], g: C.Grid):
        self.close = {s["sym"]: dict(zip(s["ts"], s["c"])) for s in segs}
        self.g = g
        self.z: dict = {}
        self.days = [d for i, d in enumerate(g.days) if i % CORR_STRIDE == 0]

    def zvec(self, sym: str, day: int):
        k = (sym, day)
        if k in self.z:
            return self.z[k]
        cl = self.close[sym]
        px = [cl.get(day - (CORR_WIN - i) * DAY) for i in range(CORR_WIN + 1)]
        out = None
        if all(p and p > 0 for p in px):
            r = [math.log(px[i + 1] / px[i]) for i in range(CORR_WIN)]
            m = sum(r) / CORR_WIN
            sd = math.sqrt(sum((x - m) ** 2 for x in r) / CORR_WIN)
            if sd > 1e-12:
                out = [(x - m) / sd for x in r]
        self.z[k] = out
        return out

    def book(self, taken) -> dict:
        iv = []
        for ep, r in taken:
            if r["spent"] <= 0:
                continue
            ts = ep["seg"]["ts"]
            d0 = next(d for d, sd, _u, _f in r["flows"] if sd == "Buy")
            iv.append((ts[d0], ts[r["exit_d"]], ep["seg"]["sym"], ep["cycle"]))
        iv.sort()
        vals, ns = [], []
        by_cyc: dict[str, list[float]] = {}
        starts = [x[0] for x in iv]
        for day in self.days:
            hi = bisect.bisect_right(starts, day)
            zs = [z for a, b, sym, _c in iv[:hi] if a <= day < b
                  for z in [self.zvec(sym, day)] if z is not None]
            n = len(zs)
            if n < 2:
                continue
            tot = 0.0
            for t in range(CORR_WIN):
                s = 0.0
                for z in zs:
                    s += z[t]
                tot += s * s
            rho = (tot / CORR_WIN - n) / (n * (n - 1))
            vals.append(rho)
            ns.append(n)
            for c, a, b in L.CYCLES:
                if a <= day < b:
                    by_cyc.setdefault(c, []).append(rho)
        return {"corr": st.fmean(vals) if vals else None, "days": len(vals),
                "n_mean": st.fmean(ns) if ns else 0.0,
                "cycles": {c: st.fmean(v) for c, v in by_cyc.items()}}


def sector_load(taken, g: C.Grid) -> dict:
    """Места одного сектора одновременно (без unknown): максимум и среднее дневного максимума."""
    iv = [(ep["day"], ep["day"] + max(r["span"], 1) * DAY, ep["sector"]) for ep, r in taken]
    best, best_sec = 0, None
    per_sec: dict[str, int] = {}
    for d0, _d1, _s in iv:
        cnt: dict[str, int] = {}
        for a, b, s in iv:
            if a <= d0 < b and s != "unknown":
                cnt[s] = cnt.get(s, 0) + 1
        for s, c in cnt.items():
            per_sec[s] = max(per_sec.get(s, 0), c)
            if c > best:
                best, best_sec = c, s
    daily = []
    for day in g.days[::CORR_STRIDE]:
        cnt = {}
        for a, b, s in iv:
            if a <= day < b and s != "unknown":
                cnt[s] = cnt.get(s, 0) + 1
        if cnt:
            daily.append(max(cnt.values()))
    return {"max": best, "max_sector": best_sec, "per_sector": per_sec,
            "daily_max_mean": st.fmean(daily) if daily else 0.0}


def drawdown(taken, g: C.Grid, pos_cache: dict) -> dict:
    """Просадка дневной стоимости счёта книги (C.portfolio без ядра)."""
    deltas, buys = [], []
    for ep, r in taken:
        if r["spent"] <= 0:
            continue
        key = (ep["seg"]["sym"], ep["e"])
        if key not in pos_cache:
            pos_cache[key] = C.position_days(ep, r, g)
        deltas += pos_cache[key]["deltas"]
        buys += pos_cache[key]["buys"]
    vb = [P.CAPITAL + x for x in C.cumsum(C.accumulate(g.n, deltas))]
    fb = [0.0] * g.n
    fb[0] = P.CAPITAL
    sb = C.accumulate(g.n, buys)
    z = [0.0] * g.n
    m = C.portfolio(vb, fb, sb, z, z, z, 0.0, g, False)
    return {"mdd_pct": m["mdd_pct"], "mdd_usd": m["mdd_usd"], "pnl": m["pnl"],
            "cycles": {c: v["mdd_pct"] for c, v in m["cycles"].items()}}


# ---------------------------------------------------------------- метрики

def pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def breakdown(fl: Q.Flows, taken) -> dict:
    out = {}
    n_all = len(taken)
    for sec in SECTORS:
        tt = [(ep, r) for ep, r in taken if ep["sector"] == sec]
        if not tt:
            out[sec] = {"n": 0}
            continue
        spent = sum(r["spent"] for _e, r in tt)
        pnl = sum(r["pnl"] for _e, r in tt)
        ok = [(ep, r) for ep, r in tt if r["spent"] > 0 and fl.row(ep, r)["alt"] is not None]
        ex_alt = sum(r["pnl"] - fl.row(ep, r)["alt"] for ep, r in ok)
        ok_spent = sum(r["spent"] for _e, r in ok)
        cyc = {c: sum(1 for ep, _r in tt if ep["cycle"] == c) for c, _a, _b in L.CYCLES}
        out[sec] = {"n": len(tt), "share": len(tt) / n_all, "spent": spent, "pnl": pnl,
                    "per_usd": pnl / spent if spent else 0.0,
                    "ex_alt_per_usd": ex_alt / ok_spent if ok_spent else 0.0,
                    "died2y": sum(1 for ep, _r in tt if ep["died2y"]), "cycles": cyc,
                    "ahead": sum(1 for ep, _r in tt if ep["sec_src"] == "ahead"),
                    "coins": sorted({ep["seg"]["sym"].split("#")[0][:-4] for ep, _r in tt})[:40]}
    return out


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

    # снимки как Q9 (с 2018-01) — сопоставление; все deep — теги
    dates = [d for d in T.first_sundays() if T.day_ts(d) <= last_day]
    snaps = [S.deep_snapshot(d) for d in dates]
    days = [T.day_ts(d) for d in dates]
    bad = T.glitches(snaps)
    for k, d in enumerate(dates):
        bad[k] |= {key for dd, key in T.GLITCHES if dd == d.isoformat()}
    mids = Q.matched_ids(snaps, bad, days, px, last_day)
    ddates = Q.deep_dates()
    dtags = [{x["id"]: x.get("tags") or [] for x in S.deep_snapshot(d)} for d in ddates]
    tag_cov = {}
    for d, tg in zip(ddates, dtags):
        y = str(d.year)
        c = tag_cov.setdefault(y, {"rows": 0, "tagged": 0, "sector": 0})
        c["rows"] += len(tg)
        c["tagged"] += sum(1 for v in tg.values() if v)
        c["sector"] += sum(1 for v in tg.values() if v and sector_of(v)[0] != "other")
    print(f"снимков CMC: сопоставление {len(dates)} ({dates[0]} … {dates[-1]}), теги {len(ddates)}; "
          f"архив Binance до {dt.datetime.fromtimestamp(last_day, dt.timezone.utc).date()}  "
          f"[{time.time() - t0:.0f} с]")
    print("  покрытие тегов CMC по годам (строки снимков): с тегами / с секторным тегом — "
          + "; ".join(f"{y} {v['tagged'] / v['rows']:.0%}/{v['sector'] / v['rows']:.0%}"
                      for y, v in tag_cov.items()))

    vranks = L.vol_ranks(all_segs)
    npairs = P.pairs_per_day(vranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot = L.load_hot()
    src = [ep for ep in L.signal_days(segs, vranks, hot, {"prod": L.is_spring_prod})["prod"]
           if ep["cycle"] != "?"]
    for ep in src:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
    rule_hits = classify(src, mids, days, dates, ddates, dtags)
    print(f"дней сигнала прода {len(src)}  [{time.time() - t0:.0f} с]")

    base_take, illiq = P.EXEC_POLICY
    sig = [ep for ep in src if base_take(ep)]
    sig_cov = {}
    for c, _a, _b in L.CYCLES + [("всё", 0, 0)]:
        ss = [ep for ep in sig if c == "всё" or ep["cycle"] == c]
        sig_cov[c] = {"n": len(ss), **{s: sum(1 for ep in ss if ep["sec_src"] == s) / len(ss)
                                       for s in ("entry", "ahead", "behind", "no_tags", "unmatched")},
                      "sectors": {s: sum(1 for ep in ss if ep["sector"] == s) / len(ss) for s in SECTORS}}
    print(f"\nСигналы, прошедшие фильтры прода: {len(sig)}; источник тегов (доля): "
          + "; ".join(f"{c}: на входе {v['entry']:.0%}, из будущего {v['ahead']:.0%}, из прошлого "
                      f"{v['behind']:.0%}, без тегов {v['no_tags']:.0%}, не сопост. {v['unmatched']:.0%}"
                      for c, v in sig_cov.items()))
    print("  секторы сигналов: " + " ".join(
        f"{SECTOR_RU[s]} {sig_cov['всё']['sectors'][s]:.0%}" for s in SECTORS))
    print("  срабатывания правил RULES (номер: раз): " + ", ".join(
        f"{i + 1 if i >= 0 else '—'}:{n}" for i, n in sorted(rule_hits.items())))

    VARIANTS = variants()
    g = C.Grid(min(ep["day"] for ep in src), last_day)
    corr = Corr(all_segs, g)
    out: dict = {"meta": {"seeds": J.SEEDS, "slots": J.SLOTS, "budget": J.BUDGET, "ks": KS,
                          "is_cycles": IS_CYCLES, "oos_cycle": OOS_CYCLE, "tags_from": TAGS_FROM.isoformat(),
                          "corr_window": CORR_WIN, "corr_stride": CORR_STRIDE,
                          "rules": [[s, sorted(t)] for s, t in RULES], "archive_last_day": last_day,
                          "variants": {v["key"]: v["label"] for v in VARIANTS}},
                 "tag_coverage_by_year": tag_cov,
                 "signals": {"n": len(sig), "coverage": sig_cov,
                             "rule_hits": {str(k): v for k, v in rule_hits.items()}}}

    ref = json.loads((Path(__file__).resolve().parent / "portfolio_vs_market_results.json")
                     .read_text(encoding="utf-8"))["exec"]
    for b, (sell, _stop) in J.BOOKS.items():
        cache: dict = {}

        def sim(ep, sell=sell, cache=cache):
            k = (ep["seg"]["sym"], ep["e"])
            if k not in cache:
                r = C.SIM_Q(ep["seg"], ep["e"], sell, budget=J.BUDGET, horizon=None, rules=L.EXEC_ALL)
                if r is not None:
                    o = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET, horizon=None,
                                        rules=L.EXEC_ALL)
                    assert o["pnl"] == r["pnl"] and o["flows"] == r["flows"] and \
                        o["cash"] == r["cash"], f"копия simulate_exec разошлась: {k}"
                cache[k] = r
            return cache[k]
        fl = Q.Flows(series)
        pos_cache: dict = {}

        base = P.exec_book(src, sim, *P.EXEC_POLICY, cooldown=True, cash=True)
        chk = P.evaluate_flows(base, alt, btc)
        s6 = ref[b]["s6"]
        assert chk["n"] == s6["n"] and abs(chk["pnl"] - s6["pnl"]) < 1e-6 and \
            abs(chk["alt"] - s6["alt"]) < 1e-6, f"{b}: s6 не повторился ({chk['pnl']} / {s6['pnl']})"
        nocap = P.exec_book(src, sim, *P.EXEC_POLICY, cooldown=True, cash=True,
                            group_cap=(lambda ep: None, 1))
        assert [G.key_of(e) for e, _r in nocap] == [G.key_of(e) for e, _r in base], "group_cap None ≠ база"
        print(f"\nсверка {b}: база s6 n={chk['n']} P&L {chk['pnl']:+.0f}$ (эталон {s6['pnl']:+.0f}$), "
              f"−альты {chk['pnl'] - chk['alt']:+.0f}$ ✓; group_cap с пустой группой = база ✓  "
              f"[{time.time() - t0:.0f} с]")
        top = sorted(base, key=lambda t: t[1]["pnl"], reverse=True)[:max(1, len(base) // 10)]
        base_best = {G.key_of(ep) for ep, _r in top}
        base_keys = {G.key_of(ep) for ep, _r in base}
        ob: dict = {"best_threshold_pnl": top[-1][1]["pnl"],
                    "best": [{"coin": ep["seg"]["sym"].split("#")[0][:-4], "sector": ep["sector"],
                              "day": dt.datetime.fromtimestamp(ep["day"], dt.timezone.utc).date()
                              .isoformat(), "pnl": r["pnl"]} for ep, r in top],
                    "breakdown": breakdown(fl, base), "variants": {}}
        seeds_base = None
        for v in VARIANTS:
            gc = cap_of(v)
            taken = P.exec_book(src, sim, base_take, illiq, cooldown=True, cash=True, group_cap=gc)
            m = Q.book_metrics(fl, taken, base_best, lambda k: True)
            keys = {G.key_of(ep) for ep, _r in taken}
            new = [(ep, r) for ep, r in taken if G.key_of(ep) not in base_keys]
            gone = [(ep, r) for ep, r in base if G.key_of(ep) not in keys]
            m["new"] = {"n": len(new), "pnl": sum(r["pnl"] for _e, r in new)}
            m["gone"] = {"n": len(gone), "pnl": sum(r["pnl"] for _e, r in gone)}
            m["lost_best"] = sorted(f"{ep['seg']['sym'].split('#')[0][:-4]} ({ep['sector']}) "
                                    f"{r['pnl']:+.0f}$" for ep, r in top if G.key_of(ep) not in keys)
            m["corr"] = corr.book(taken)
            m["load"] = sector_load(taken, g)
            m["dd"] = drawdown(taken, g, pos_cache)
            assert abs(m["dd"]["pnl"] - sum(r["pnl"] for _e, r in taken if r["spent"] > 0)) < 1e-6
            runs, died, cors, dds, loads = [], [], [], [], []
            for sd in range(J.SEEDS):
                tk = P.exec_book(src, sim, base_take, illiq, cooldown=True, cash=True,
                                 order="random", seed=sd, group_cap=gc)
                runs.append(G.seed_row(fl.vs(tk)))
                died.append(sum(1 for ep, _r in tk if ep["died2y"]))
                cors.append(corr.book(tk)["corr"])
                dds.append(drawdown(tk, g, pos_cache)["mdd_pct"])
                loads.append(sector_load(tk, g)["max"])
            if v["key"] == "base":
                seeds_base = runs
                seeds_corr, seeds_dd = cors, dds
            rnd = {"pnl_mean": st.fmean(x["pnl"] for x in runs),
                   "pnl_min": min(x["pnl"] for x in runs), "pnl_max": max(x["pnl"] for x in runs),
                   "spent_mean": st.fmean(x["spent"] for x in runs),
                   "died2y_mean": st.fmean(died),
                   "corr_mean": st.fmean(cors), "mdd_mean": st.fmean(dds),
                   "max_sector_mean": st.fmean(loads), "max_sector_max": max(loads),
                   "lower_corr_than_base": sum(1 for x, y_ in zip(cors, seeds_corr) if x < y_),
                   "lower_mdd_than_base": sum(1 for x, y_ in zip(dds, seeds_dd) if x < y_),
                   "beat_base_pnl": sum(1 for x, y_ in zip(runs, seeds_base) if x["pnl"] > y_["pnl"]),
                   "beat_base_ex_alt": sum(1 for x, y_ in zip(runs, seeds_base)
                                           if x["pnl"] - x["alt"] > y_["pnl"] - y_["alt"])}
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
            print(f"  {b} {v['key']}: готово  [{time.time() - t0:.0f} с]")
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
        print("  покупки прода по секторам: доля / поз. / P&L / на $ / −альты на $ / умерли / из будущего")
        for sec, x in ob["breakdown"].items():
            if x["n"]:
                print(f"    {SECTOR_RU[sec]:15} {x['share']:>4.0%} {x['n']:>4} {x['pnl']:>+6.0f}$ "
                      f"{pct(x['per_usd']):>7} {pct(x['ex_alt_per_usd']):>7} †{x['died2y']:<3} "
                      f"⏩{x['ahead']:<3} " + "/".join(str(x["cycles"][c]) for c, _a, _b in L.CYCLES)
                      + "  " + ", ".join(x["coins"][:12]))
        print("  прод по обороту: P&L, −альты, −BTC, поз., умерли, лучш. пропало, новые/ушли, корр., "
              "макс. в секторе (ср. дн.), просадка")
        for v in VARIANTS:
            m = ob["variants"][v["key"]]
            print(f"  {v['label']:42} {m['pnl']:>+6.0f} {m['pnl'] - m['alt']:>+6.0f} {m['pnl'] - m['btc']:>+6.0f} "
                  f"{m['n']:>4} {m['died2y']:>3} {m['best_lost']:>3} "
                  f"{m['new']['n']:>3} {m['new']['pnl']:>+5.0f}$/{m['gone']['n']:>3} {m['gone']['pnl']:>+5.0f}$ "
                  f"{m['corr']['corr']:>5.2f} {m['load']['max']:>2} {m['load']['max_sector'] or '—':>8} "
                  f"({m['load']['daily_max_mean']:.1f}) {m['dd']['mdd_pct'] * 100:>5.1f}%")
        print(f"  средние {J.SEEDS} случайных порядков; «лучше базы» — против случайной базы того же seed")
        print(f"  {'вариант':42} {'P&L':>6} {'влож.':>6} {'−альты':>13} {'−корз.':>13} {'−BTC':>13} "
              f"{'лучше б. P&L/−альт':>18} {'2018-20':>8} {'2020-22':>8} {'2023-26':>8} "
              f"{'†ср.':>5} {'корр.':>5} {'<б.':>3} {'просад.':>7} {'<б.':>3} {'макс.с.':>7}")
        for v in VARIANTS:
            r = ob["variants"][v["key"]]["random"]
            print(f"  {v['label']:42} {r['pnl_mean']:>+6.0f} {r['spent_mean']:>6.0f} "
                  + " ".join(f"{r[f'ex_{k}_mean']:+.0f} ({pct(r[f'ex_{k}_per_usd'])})".rjust(13)
                             for k, _n in BENCH)
                  + f"{r['beat_base_pnl']}/{r['beat_base_ex_alt']}".rjust(19)
                  + "".join(f"{r['cycles'][c]['ex_alt']:>+9.0f}" for c, _a, _b in L.CYCLES)
                  + f" {r['died2y_mean']:>5.1f} {r['corr_mean']:>5.2f} {r['lower_corr_than_base']:>3} "
                  f"{r['mdd_mean'] * 100:>6.1f}% {r['lower_mdd_than_base']:>3} {r['max_sector_mean']:>7.1f}")

        # ---- вне выборки: K по 2018–22, проверка на 2023–26
        vb_r = ob["variants"]["base"]["random"]
        cand = [v for v in VARIANTS if v["key"] not in ("base", "K3o")]
        pick = max(cand, key=lambda v: ob["variants"][v["key"]]["random"]["is_ex_alt"])
        vp_r = ob["variants"][pick["key"]]["random"]
        oos = {"pick": pick["key"],
               "is": {k: vp_r[f"is_{k}"] - vb_r[f"is_{k}"] for k in ("pnl", "ex_alt", "ex_cap", "ex_btc")},
               "oos": {k: vp_r[f"oos_{k}"] - vb_r[f"oos_{k}"] for k in ("pnl", "ex_alt", "ex_cap", "ex_btc")},
               "is_beat": vp_r["is_beat_base_ex_alt"], "oos_beat": vp_r["oos_beat_base_ex_alt"],
               "oos_base": {k: vb_r[f"oos_{k}"] for k in ("pnl", "ex_alt", "ex_btc")},
               "all": {v["key"]: {"is_ex_alt": ob["variants"][v["key"]]["random"]["is_ex_alt"] - vb_r["is_ex_alt"],
                                  "oos_ex_alt": ob["variants"][v["key"]]["random"]["oos_ex_alt"] - vb_r["oos_ex_alt"]}
                       for v in VARIANTS if v["key"] != "base"}}
        ob["oos"] = oos
        print(f"  вне выборки: по 2018–22 выбран «{pick['label']}» — к случ. базе в 2018–22 −альты "
              f"{oos['is']['ex_alt']:+.0f}$ ({oos['is_beat']}/{J.SEEDS}); в 2023–26 −альты "
              f"{oos['oos']['ex_alt']:+.0f}$ ({oos['oos_beat']}/{J.SEEDS}), P&L {oos['oos']['pnl']:+.0f}$, "
              f"−BTC {oos['oos']['ex_btc']:+.0f}$ (база 2023–26: P&L {oos['oos_base']['pnl']:+.0f}$, "
              f"−альты {oos['oos_base']['ex_alt']:+.0f}$)")
        print("  все варианты к случ. базе, −альты 2018–22 | 2023–26: " + "; ".join(
            f"{k} {x['is_ex_alt']:+.0f} | {x['oos_ex_alt']:+.0f}" for k, x in oos["all"].items()))
        print("  пропали из лучших 10% прода: " + "; ".join(
            f"{v['key']}: {', '.join(ob['variants'][v['key']]['lost_best']) or '—'}"
            for v in VARIANTS if v["key"] in ("K2", "K3", "K4", "K3u")))
        print(f"  [{time.time() - t0:.0f} с]")

    out["runtime_s"] = time.time() - t0
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
