"""Рыночный контекст: история и ежедневное обновление таблицы market_daily (free).

Источники (проверены 28.09.2026, все без ключа):
  • CMC data-api global-metrics — total mcap + BTC.D с 2013. НЕОФИЦИАЛЬНЫЙ эндпоинт
    сайта CMC (лимит 2200 точек/запрос): на нём откалиброваны пороги, поэтому он
    основной. При сбое — CoinGecko /global (официальный free) × поправка CMC/CG
    по последним дням, где есть оба (расхождение ~1%).
  • DeFiLlama stablecoins — сумма стейблов; до 2020 неполная (нет Omni-USDT) ->
    берём max(DeFiLlama, USDT+USDC из CoinMetrics).
  • CoinMetrics community — MVRV BTC/ETH, предложение USDT/USDC.
  • alternative.me — Fear & Greed (с 2018-02).
  • Bybit v5 — фандинг и open interest BTCUSDT (с 2020), дневные свечи топ спот-альтов
    для ширины рынка (% выше SMA200; токенизированные акции/стейблы исключены).
Сеть — только здесь; расчёт признаков — чистые функции в regime.py.
"""
from __future__ import annotations

import statistics
import time

from ..http import HttpClient

DAY = 86400
_CMC = "https://api.coinmarketcap.com/data-api/v3/global-metrics/quotes/historical"
_CG_GLOBAL = "https://api.coingecko.com/api/v3/global"
_LLAMA_ST = "https://stablecoins.llama.fi/stablecoincharts/all"
_CM = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
_FNG = "https://api.alternative.me/fng/"
_BYBIT = "https://api.bybit.com"

# Не-крипта и стейблы среди спот-пар Bybit (для ширины рынка).
_NOT_ALTS = {"BTC", "USDC", "USDE", "FDUSD", "DAI", "USD1", "RLUSD", "PYUSD", "TUSD", "USDD",
             "BFUSD", "XUSD", "EURI", "EUR", "EURC", "USDQ", "USDR", "USDTB", "PAXG", "XAUT",
             "WBTC", "WETH", "STETH", "CMETH", "METH", "BBSOL", "LBTC", "CBBTC"}


def day_of(ts: float) -> int:
    return int(ts // DAY) * DAY


def _utc_day(iso: str) -> int:
    """'2026-09-23T00:00:00...' -> unix 00:00 UTC (без локальной таймзоны)."""
    import calendar
    return calendar.timegm(time.strptime(iso[:10], "%Y-%m-%d"))


# ---------------------------------------------------------------- капа и BTC.D

def fetch_cmc_global(http: HttpClient, start_ts: int, end_ts: int) -> dict[int, dict]:
    """{day: {total_mcap, btc_dominance}} из CMC data-api (чанки по 365д)."""
    out: dict[int, dict] = {}
    s = start_ts
    while s < end_ts:
        e = min(s + 365 * DAY, end_ts)
        d = http.get_json(_CMC, params={"format": "chart_crypto_details", "interval": "1d",
                                        "timeStart": s, "timeEnd": e},
                          headers={"User-Agent": "Mozilla/5.0"})
        quotes = (((d or {}).get("data") or {}).get("quotes") or []) if isinstance(d, dict) else []
        for q in quotes:
            qq = (q.get("quote") or [{}])[0]
            total, btc_d = qq.get("totalMarketCap"), q.get("btcDominance")
            if total and btc_d:
                out[_utc_day(q["timestamp"])] = {"total_mcap": float(total),
                                                 "btc_dominance": float(btc_d),
                                                 "total_source": "cmc"}
        s = e
    return out


def fetch_cg_global(http: HttpClient, demo_key: str = "") -> dict[int, dict]:
    """Сегодняшний срез CoinGecko /global (истории на free нет)."""
    d = http.get_json(_CG_GLOBAL, headers={"x-cg-demo-api-key": demo_key} if demo_key else None)
    g = (d or {}).get("data") if isinstance(d, dict) else None
    if not isinstance(g, dict):
        return {}
    total = (g.get("total_market_cap") or {}).get("usd")
    btc_d = (g.get("market_cap_percentage") or {}).get("btc")
    if not (total and btc_d):
        return {}
    return {day_of(time.time()): {"cg_total_mcap": float(total), "cg_btc_dominance": float(btc_d)}}


# ---------------------------------------------------------------- стейблы

def fetch_llama_stables(http: HttpClient) -> dict[int, float]:
    d = http.get_json(_LLAMA_ST)
    out: dict[int, float] = {}
    for r in d if isinstance(d, list) else []:
        v = (r.get("totalCirculatingUSD") or {}).get("peggedUSD")
        if v:
            out[day_of(int(r["date"]))] = float(v)
    return out


def fetch_coinmetrics(http: HttpClient, assets: str, metrics: str,
                      start: str) -> dict[str, dict[int, dict]]:
    """{asset: {day: {metric: value}}} — community API, с пагинацией."""
    out: dict[str, dict[int, dict]] = {}
    d = http.get_json(_CM, params={"assets": assets, "metrics": metrics, "frequency": "1d",
                                   "page_size": 10000, "start_time": start})
    pages = 0
    while isinstance(d, dict) and pages < 20:
        for r in d.get("data", []) or []:
            day = _utc_day(r["time"])
            rec = out.setdefault(r["asset"], {}).setdefault(day, {})
            for m in metrics.split(","):
                if r.get(m) not in (None, ""):
                    rec[m] = float(r[m])
        nxt = d.get("next_page_url")
        d = http.get_json(nxt) if nxt else None
        pages += 1
    return out


# ---------------------------------------------------------------- сентимент и плечо

def fetch_fng(http: HttpClient, limit: int = 0) -> dict[int, float]:
    d = http.get_json(_FNG, params={"limit": limit, "format": "json"})
    out: dict[int, float] = {}
    for r in (d or {}).get("data", []) if isinstance(d, dict) else []:
        try:
            out[day_of(int(r["timestamp"]))] = float(r["value"])
        except (KeyError, ValueError, TypeError):
            continue
    return out


def fetch_bybit_funding(http: HttpClient, symbol: str = "BTCUSDT",
                        since_ts: float | None = None, max_pages: int = 1) -> dict[int, float]:
    """{day: средний фандинг за день (доля/8ч)}. Пагинация назад по endTime."""
    raw: dict[int, float] = {}
    end = int(time.time() * 1000)
    for _ in range(max_pages):
        d = http.get_json(f"{_BYBIT}/v5/market/funding/history",
                          params={"category": "linear", "symbol": symbol,
                                  "limit": 200, "endTime": end})
        lst = ((d or {}).get("result") or {}).get("list") or [] if isinstance(d, dict) else []
        if not lst:
            break
        for r in lst:
            try:
                raw[int(r["fundingRateTimestamp"])] = float(r["fundingRate"])
            except (KeyError, ValueError, TypeError):
                continue
        oldest = min(int(r["fundingRateTimestamp"]) for r in lst)
        if since_ts is not None and oldest / 1000 <= since_ts:
            break
        end = oldest - 1
    days: dict[int, list[float]] = {}
    for t, v in raw.items():
        days.setdefault(day_of(t / 1000), []).append(v)
    return {k: sum(v) / len(v) for k, v in days.items()}


def fetch_bybit_oi(http: HttpClient, symbol: str = "BTCUSDT",
                   since_ts: float | None = None, max_pages: int = 2) -> dict[int, float]:
    """{day: open interest (в монетах)} дневной, пагинация назад по endTime."""
    out: dict[int, float] = {}
    end = int(time.time() * 1000)
    for _ in range(max_pages):
        d = http.get_json(f"{_BYBIT}/v5/market/open-interest",
                          params={"category": "linear", "symbol": symbol,
                                  "intervalTime": "1d", "limit": 200, "endTime": end})
        lst = ((d or {}).get("result") or {}).get("list") or [] if isinstance(d, dict) else []
        if not lst:
            break
        for r in lst:
            try:
                out[day_of(int(r["timestamp"]) / 1000)] = float(r["openInterest"])
            except (KeyError, ValueError, TypeError):
                continue
        oldest = min(int(r["timestamp"]) for r in lst)
        if (since_ts is not None and oldest / 1000 <= since_ts) or oldest >= end:
            break
        end = oldest - 1
    return out


# ---------------------------------------------------------------- ширина рынка

def top_spot_alts(http: HttpClient, n: int) -> list[str]:
    """Топ-n спот-альтов Bybit (…USDT) по обороту 24ч; без стейблов, токенизированных
    акций (symbolType=xstocks) и пар с пометкой ST (под угрозой делистинга)."""
    inst = http.get_json(f"{_BYBIT}/v5/market/instruments-info",
                         params={"category": "spot", "limit": 1000})
    skip = set()
    for it in ((inst or {}).get("result") or {}).get("list") or [] if isinstance(inst, dict) else []:
        if it.get("symbolType") or it.get("stTag") == "1":
            skip.add(it.get("symbol", ""))
    tick = http.get_json(f"{_BYBIT}/v5/market/tickers", params={"category": "spot"})
    rows = []
    for t in ((tick or {}).get("result") or {}).get("list") or [] if isinstance(tick, dict) else []:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym in skip or sym[:-4] in _NOT_ALTS:
            continue
        try:
            rows.append((float(t.get("turnover24h") or 0), sym))
        except (ValueError, TypeError):
            continue
    rows.sort(reverse=True)
    return [s for _, s in rows[:n]]


def fetch_spot_closes(http: HttpClient, symbol: str, limit: int) -> dict[int, float]:
    d = http.get_json(f"{_BYBIT}/v5/market/kline",
                      params={"category": "spot", "symbol": symbol, "interval": "D",
                              "limit": limit})
    out: dict[int, float] = {}
    for r in ((d or {}).get("result") or {}).get("list") or [] if isinstance(d, dict) else []:
        try:
            out[day_of(int(r[0]) / 1000)] = float(r[4])
        except (ValueError, IndexError, TypeError):
            continue
    return out


def breadth_from_closes(series: list[dict[int, float]], days: list[int],
                        min_coins: int = 30) -> dict[int, float]:
    """% монет с закрытием выше своей SMA200 на каждый день (нужно ≥200 дней истории).
    Почти неподвижные ряды (σ дневных доходностей < 0.3%) — стейблы, пропускаем."""
    wanted = set(days)
    up: dict[int, int] = {}
    cnt: dict[int, int] = {}
    for s in series:
        ks = sorted(s)
        closes = [s[k] for k in ks]
        rets = [closes[i] / closes[i - 1] - 1 for i in range(max(1, len(closes) - 60), len(closes))
                if closes[i - 1] > 0]
        if len(rets) >= 20 and statistics.pstdev(rets) < 0.003:
            continue
        acc = 0.0
        for i, (k, c) in enumerate(zip(ks, closes)):
            acc += c
            if i >= 200:
                acc -= closes[i - 200]
            if i >= 199 and k in wanted:
                cnt[k] = cnt.get(k, 0) + 1
                up[k] = up.get(k, 0) + (c > acc / 200)
    return {d: round(up[d] / cnt[d] * 100, 2) for d in cnt if cnt[d] >= min_coins}


# ---------------------------------------------------------------- оркестрация

def _merge(dst: dict[int, dict], src: dict[int, float] | dict[int, dict], col: str | None = None):
    for d, v in src.items():
        if col is None:
            dst.setdefault(d, {}).update(v)
        elif v is not None:
            dst.setdefault(d, {})[col] = v


def collect(http: HttpClient, since_ts: int, demo_key: str = "",
            breadth_top_n: int = 150, full: bool = False) -> dict[int, dict]:
    """Собирает {day: {колонка: значение}} за период с since_ts. full=True — бэкфилл
    (глубокая пагинация Bybit, свечи на 1000 дней для ширины)."""
    now = int(time.time())
    rows: dict[int, dict] = {}
    _merge(rows, fetch_cmc_global(http, since_ts, now))
    _merge(rows, fetch_cg_global(http, demo_key))

    cm_start = time.strftime("%Y-%m-%d", time.gmtime(since_ts - 5 * DAY))
    cm = fetch_coinmetrics(http, "btc,eth,usdt,usdc", "CapMVRVCur,SplyCur", cm_start)
    for d, rec in (cm.get("btc") or {}).items():
        if "CapMVRVCur" in rec:
            rows.setdefault(d, {})["mvrv_btc"] = rec["CapMVRVCur"]
    for d, rec in (cm.get("eth") or {}).items():
        if "CapMVRVCur" in rec:
            rows.setdefault(d, {})["mvrv_eth"] = rec["CapMVRVCur"]
    llama = fetch_llama_stables(http)
    cm_st: dict[int, float] = {}
    for a in ("usdt", "usdc"):
        for d, rec in (cm.get(a) or {}).items():
            if rec.get("SplyCur"):
                cm_st[d] = cm_st.get(d, 0.0) + rec["SplyCur"]
    for d in set(llama) | set(cm_st):
        if d >= since_ts - DAY:
            rows.setdefault(d, {})["stables_usd"] = max(llama.get(d, 0.0), cm_st.get(d, 0.0))

    fng_limit = 0 if full else max(10, int((now - since_ts) / DAY) + 3)
    _merge(rows, {d: v for d, v in fetch_fng(http, fng_limit).items() if d >= since_ts - DAY},
           "fng")
    pages = 40 if full else 1
    _merge(rows, fetch_bybit_funding(http, "BTCUSDT", since_ts, pages), "funding_btc")
    _merge(rows, fetch_bybit_oi(http, "BTCUSDT", since_ts, 20 if full else 2), "oi_btc")

    if breadth_top_n > 0:
        limit = 1000 if full else 260
        syms = top_spot_alts(http, breadth_top_n)
        series = [fetch_spot_closes(http, s, limit) for s in syms]
        days = sorted({d for s in series for d in s if d >= since_ts - DAY})
        _merge(rows, breadth_from_closes(series, days), "breadth200")
    return rows


def fill_total_from_cg(rows: dict[int, dict], history: list[dict]) -> None:
    """Дни без CMC, но с CoinGecko -> total/BTC.D из CG × медианная поправка CMC/CG
    по дням, где есть оба источника (чтобы просадка альт-рынка не прыгала на смене источника)."""
    both = [(r["total_mcap"] / r["cg_total_mcap"], r["btc_dominance"] - r["cg_btc_dominance"])
            for r in history + [dict(day=d, **v) for d, v in rows.items()]
            if r.get("total_mcap") and r.get("cg_total_mcap") and r.get("total_source") == "cmc"
            and r.get("btc_dominance") is not None and r.get("cg_btc_dominance") is not None]
    ratio = statistics.median(x[0] for x in both[-30:]) if both else 1.0
    dshift = statistics.median(x[1] for x in both[-30:]) if both else 0.0
    have_cmc = {r["day"] for r in history if r.get("total_source") == "cmc"}
    for d, v in rows.items():
        if v.get("total_mcap") is None and v.get("cg_total_mcap") and d not in have_cmc:
            v["total_mcap"] = v["cg_total_mcap"] * ratio
            v["btc_dominance"] = v["cg_btc_dominance"] + dshift
            v["total_source"] = "coingecko"


def update_market_daily(cfg, http: HttpClient, store, force: bool = False,
                        backfill: bool = False) -> dict:
    """Дообновляет market_daily. Бэкфилл — автоматически, если истории < 400 дней.
    Не ходит в сеть чаще update_min_interval_hours (scan и watch вызывают оба)."""
    m = cfg.get("market_regime", {}) or {}
    st = store.market_stats()
    need_backfill = backfill or st["days_alt"] < 400
    fresh = (st["last_update"] and time.time() - st["last_update"]
             < m.get("update_min_interval_hours", 6) * 3600)
    if fresh and not (force or need_backfill):
        return {"updated": 0, "mode": "cache"}
    demo = cfg.get("api_keys.coingecko_demo", "")
    top_n = m.get("breadth_top_n", 150)
    if need_backfill:
        start = _utc_day(m.get("backfill_start", "2017-01-01"))
        rows = collect(http, day_of(start), demo, top_n, full=True)
        mode = "backfill"
    else:
        since = day_of(time.time()) - m.get("update_lookback_days", 10) * DAY
        rows = collect(http, since, demo, top_n, full=False)
        mode = "update"
    fill_total_from_cg(rows, store.market_rows(day_of(time.time()) - 60 * DAY))
    return {"updated": store.upsert_market(rows), "mode": mode}


def load_context(cfg, http: HttpClient, store) -> dict:
    """Обновить (если пора) и посчитать контекст рынка на последний день. {} при сбое."""
    from .. import regime
    if not cfg.get("market_regime.enabled", True):
        return {}
    try:
        info = update_market_daily(cfg, http, store)
    except Exception as e:  # noqa: BLE001 — рыночный контекст не должен ронять скан
        print(f"[market] обновление не удалось: {e}")
        info = {"updated": 0, "mode": "error"}
    ctx = regime.market_context(store.market_rows(), cfg)
    if ctx:
        ctx["update"] = info
    return ctx
