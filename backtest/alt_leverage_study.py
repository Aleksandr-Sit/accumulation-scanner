"""Плечо в альтах как 9-й флаг перегрева: фандинг и OI альтовых перпов, 2020–2026.

Вопрос: даёт ли плечо в АЛЬТАХ (а не только в BTC — флаги fund30 / oi_rel365) информацию
сверх 8 текущих флагов перегрева (regime.hot_flags):
  1. Вход: слабее ли пружины, если при «0 текущих флагов» плечо в альтах уже высокое?
  2. Выход: меняет ли 9-й флаг сигнал перегрева (exit_block market_regime_study и форвард
     альт-рынка после сигнала)?

Признаки (дневные, walk-forward — только данные ≤ дня; на вход в пружину — за день ДО входа):
  alt_fund30  — медиана по топ-50 альтовых перпов Binance USDT-M (по обороту за 30д НА ТОТ
                день, вкл. позже делистнутые) их среднего фандинга за 30д, %/8ч;
  alt_fund_hi — доля этих перпов со средним фандингом за 30д ≥ 0.03%/8ч;
  alt_oi_rel  — OI топ-50 альтовых перпов Bybit в USD (OI в монетах × закрытие), цепной
                индекс (состав меняется — растёт только на общих для соседних дней перпах),
                к своему среднему за 365д;
  alt_oi_mcap — тот же индекс, делённый на капу альт-рынка без стейблов (alt_ex), к среднему
                за 365д: плечо без «ценовой» составляющей.
Контроли: те же признаки на сегодняшнем списке перпов (survivor-only, как в постановке),
фандинг альтов Bybit, 30д-среднее дневной медианы (как можно хранить в market_daily),
медиана OI в монетах по перпам, сырая сумма OI.
Фандинг каждой выплаты приводится к 8ч по шагу до предыдущей выплаты (у альтов интервал
8/4/2/1ч, биржи меняют его на ходу).

Данные (публичные API без ключей, проверено 03.10.2026 с этой машины):
  Binance fapi — exchangeInfo (в т.ч. SETTLING = делистинг), полный список USDT-M символов
  из листинга S3 data.binance.vision (архив фандинга: есть и давно удалённые LUNA/SRM/MATIC…),
  /fapi/v1/klines (оборот), /fapi/v1/fundingRate (с 2019-09, отдаёт и делистнутые).
  Bybit v5 — instruments-info, /v5/market/kline (оборот, цена), /v5/market/open-interest
  (1d, отдаёт и делистнутые), /v5/market/funding/history.
  Binance /futures/data/openInterestHist — только последние 30 дней, для истории не годится.
Сырые ответы — .cache/alt_leverage/raw (gzip, без срока годности) на фиксированную дату среза
(.cache/alt_leverage/asof.json): повторный запуск в сеть не ходит; --refresh сдвигает срез.
scanner.db только читается (sqlite mode=ro).

ВНИМАНИЕ: пружины — survivor-only (живые пары Binance spot, как в market_regime_study) → P —
ВЕРХНЯЯ граница, сравнивать варианты между собой. Пороги 9-го флага — по сетке на тех же
данных (in-sample, [оценка]); «априорный» порог — по доле дней, когда горят текущие флаги.
Эпизоды одного квартала коррелированы — интервалы считаются кластерным бутстрепом по кварталам.

Запуск из корня проекта:  py -3 backtest/alt_leverage_study.py [--offline] [--refresh]
                          py -3 backtest/alt_leverage_study.py --fetch-only binance|bybit
Результат: backtest/alt_leverage_results.json; отчёт — docs/ALT_LEVERAGE_REPORT.md
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import gzip
import hashlib
import json
import random
import re
import sqlite3
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_left as _bisect_left
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from feature_study import _EXCL, _tagged_non_alts  # noqa: E402
from market_regime_study import (CYCLES, MIN_CELL, _p, _stats,  # noqa: E402
                                 btc_dd_by_day, build_episodes, sim_prod_exit,
                                 tercile_report)
from scanner import regime  # noqa: E402
from scanner.config import Config, load_config  # noqa: E402
from scanner.stages.entry_quality import spring_quality  # noqa: E402

CACHE = ROOT / ".cache" / "alt_leverage"
RAW = CACHE / "raw"
COINS = ROOT / ".cache" / "binance_1d"
OUT = Path(__file__).resolve().parent / "alt_leverage_results.json"

DAY = 86400
DAY_MS = DAY * 1000
BN = "https://fapi.binance.com"
BB = "https://api.bybit.com"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BN_START_MS = 1567296000000            # 2019-09-01 — старт USDT-M Binance
MAP_FROM = 1577836800                  # 2020-01-01 — карта признаков и сигналы

TOP_N = 50            # перпов в корзине на дату
VOL_WIN = 30          # окно оборота для отбора топ-N
MIN_LIFE = 30         # дней торгов до попадания в отбор (всплеск оборота в дни листинга)
FUND_WIN, FUND_MIN = 30, 20
FUND_HI = 0.0003      # 0.03%/8ч — «высокий» фандинг перпа
MIN_PERPS = 10        # меньше перпов в корзине — признака нет
OI_WIN, OI_MIN = 365, 300
OI_LINK_MIN = 5       # перпов с OI в оба соседних дня для звена цепного индекса

# Не-альты: BTC/ETH, стейблы, золото, обёртки, индексы (BTCDOM, DEFI, ALL…).
NON_ALT = _EXCL | {"BTC", "ETH", "USDT", "USDC", "USDE", "FDUSD", "BUSD", "TUSD", "DAI",
                   "USD1", "RLUSD", "PYUSD", "USDD", "USDQ", "USDR", "USDTB", "BFUSD", "XUSD",
                   "EURI", "EUR", "EURC", "PAXG", "XAUT", "XAU", "XAG", "WBTC", "WETH", "STETH",
                   "WBETH", "CBETH", "CBBTC", "LBTC", "BTCDOM", "DEFI", "ALL", "FOOTBALL",
                   "BLUEBIRD", "DOTECO"}

# Кандидаты в 9-й флаг: (подпись, сетка порогов). Единицы — как в regime (фандинг в %/8ч).
FEATS = {
    "alt_fund30": ("фандинг альтов (медиана 30д, топ-50 Binance), %/8ч",
                   [0.0, 0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]),
    "alt_fund_hi": ("доля топ-50 с 30д фандингом ≥0.03%/8ч", [0.05, 0.1, 0.2, 0.3, 0.4, 0.5]),
    "alt_oi_rel": ("OI альтов Bybit в USD к среднему за 365д", [1.2, 1.3, 1.5, 1.75, 2.0]),
    "alt_oi_mcap": ("OI альтов / капа альтов к среднему за 365д", [1.1, 1.2, 1.3, 1.5]),
}
CONTROLS = ["alt_fund30_surv", "alt_fund30_bybit", "alt_fund30_dm", "alt_oi_rel_surv",
            "alt_oi_rel_med", "alt_oi_sum_rel"]


def _d(day: int | None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(day)) if day is not None else "—"


def _ms_day(ms) -> int:
    return (int(ms) // 1000) // DAY * DAY


def _quarter(day: int) -> str:
    g = time.gmtime(day)
    return f"{g.tm_year}Q{(g.tm_mon - 1) // 3 + 1}"


def _r(x, k: int = 3):
    return round(x, k) if isinstance(x, (int, float)) else None


# ---------------------------------------------------------------- сеть и кэш

class CacheMiss(RuntimeError):
    pass


class Net:
    """GET с дисковым кэшем сырых ответов (gzip, ключ = URL [+ дата среза для «живых»
    эндпоинтов]) и паузой по «корзине» лимита. Ошибки 4xx с детерминированным ответом
    (неверный символ) тоже кэшируются, временные (429/5xx/таймаут) — нет."""

    # мин. интервал между СТАРТАМИ запросов, сек: Binance klines (вес 10, лимит 2400/мин) — 2/с;
    # fundingRate (500 за 5 мин на IP) — 1.25/с; Bybit public — ~6.5/с (<10/с). Запросы идут
    # в несколько потоков (задержка ответа ~0.7 с), но старты разнесены по интервалу корзины.
    INTERVAL = {"bn": 1.0, "bn_kl": 0.5, "bn_fr": 0.8, "bb": 0.15, "s3": 0.5}

    def __init__(self, offline: bool = False):
        self.offline = offline
        self._last: dict[str, float] = {}
        self._locks = {b: threading.Lock() for b in self.INTERVAL}
        self._stat = threading.Lock()
        self.fetched = 0
        self.cached = 0

    def _throttle(self, bucket: str) -> None:
        with self._locks.setdefault(bucket, threading.Lock()):
            wait = self.INTERVAL.get(bucket, 0.5) - (time.monotonic() - self._last.get(bucket, 0.0))
            if wait > 0:
                time.sleep(wait)
            self._last[bucket] = time.monotonic()

    @staticmethod
    def _path(key: str) -> Path:
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return RAW / h[:2] / f"{h}.json.gz"

    def get(self, url: str, params: dict | None = None, bucket: str = "bn",
            tag: str = "", text: bool = False):
        full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        p = self._path(full + (f"#{tag}" if tag else ""))
        if p.exists():
            with self._stat:
                self.cached += 1
            body = gzip.decompress(p.read_bytes()).decode("utf-8")
        else:
            if self.offline:
                raise CacheMiss(full)
            body = self._fetch(full, bucket)
            if body is None:
                return None
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f"{p.name}.{threading.get_ident()}.tmp")
            tmp.write_bytes(gzip.compress(body.encode("utf-8"), 6))
            tmp.replace(p)
            with self._stat:
                self.fetched += 1
        if text:
            return body
        try:
            return json.loads(body)
        except ValueError:
            return None

    def _fetch(self, url: str, bucket: str) -> str | None:
        backoff = 2.0
        for _ in range(7):
            self._throttle(bucket)
            req = urllib.request.Request(url, headers={"User-Agent": "alt-leverage-study/0.1",
                                                       "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    body = r.read().decode("utf-8")
                    used = r.headers.get("X-MBX-USED-WEIGHT-1M")
                if used and used.isdigit() and int(used) > 1500:
                    time.sleep(20)          # Binance: половина минутного веса уже съедена
                if bucket == "bb":          # Bybit: ошибки лимита приходят с HTTP 200
                    try:
                        rc = json.loads(body).get("retCode")
                    except ValueError:
                        rc = None
                    if rc in (10002, 10006, 10016, 10018):
                        time.sleep(max(backoff, 5.0))
                        backoff *= 2
                        continue
                return body
            except urllib.error.HTTPError as e:
                if e.code == 418:
                    raise SystemExit("Binance: IP временно заблокирован (418) — остановка, "
                                     "повторить позже (кэш сохранён)")
                if e.code == 400:           # неверный символ и т.п. — ответ детерминирован
                    return e.read().decode("utf-8", "replace")
                if e.code in (403, 429, 500, 502, 503, 504):
                    ra = e.headers.get("Retry-After") if e.headers else None
                    pause = float(ra) + 1 if ra and ra.isdigit() else (
                        65.0 if e.code in (403, 429) else backoff)
                    time.sleep(min(pause, 120.0))
                    backoff *= 2
                    continue
                print(f"[net] {e.code} {url}")
                return None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                time.sleep(backoff)
                backoff *= 2
        print(f"[net] fail {url}")
        return None


def pmap(fn, items: list, workers: int, label: str, net: Net, t0: float, every: int = 100) -> dict:
    """{item: fn(item)} в несколько потоков (лимит соблюдает Net); прогресс каждые every."""
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, it): it for it in items}
        for i, f in enumerate(as_completed(futs)):
            out[futs[f]] = f.result()
            if (i + 1) % every == 0 or i + 1 == len(items):
                print(f"  {label} {i + 1}/{len(items)}  (сеть {net.fetched}, кэш {net.cached}, "
                      f"{time.time() - t0:.0f} с)")
    return out


def asof_day(refresh: bool) -> int:
    """Дата среза (00:00 UTC, сек): запрашиваются только закрытые дни < asof. Фиксируется при
    первом запуске — кэш сырых ответов детерминирован; --refresh сдвигает срез на сегодня."""
    p = CACHE / "asof.json"
    if p.exists() and not refresh:
        return int(json.loads(p.read_text(encoding="utf-8"))["asof"])
    a = int(time.time()) // DAY * DAY
    CACHE.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"asof": a, "date": _d(a)}), encoding="utf-8")
    return a


# ---------------------------------------------------------------- вселенная перпов

def _core(base: str) -> str:
    """1000PEPE / SHIB1000 / 1000000MOG -> базовый тикер (только для проверки «не альт»)."""
    b = re.sub(r"^\d+", "", base)
    return re.sub(r"1000+$", "", b) or base


def non_alt(base: str) -> bool:
    return base in NON_ALT or _core(base) in NON_ALT


def s3_um_symbols(net: Net, tag: str) -> list[str]:
    """Все символы USDT-M, когда-либо имевшие фандинг (листинг бакета data.binance.vision)."""
    prefix = "data/futures/um/monthly/fundingRate/"
    out, marker = [], ""
    for _ in range(20):
        params = {"delimiter": "/", "prefix": prefix}
        if marker:
            params["marker"] = marker
        x = net.get(S3, params, bucket="s3", tag=tag, text=True)
        if not x:
            break
        got = re.findall(r"<Prefix>" + re.escape(prefix) + r"([^<]+)/</Prefix>", x)
        out += got
        if "<IsTruncated>true</IsTruncated>" not in x or not got:
            break
        nm = re.findall(r"<NextMarker>([^<]+)</NextMarker>", x)
        marker = nm[0] if nm else prefix + got[-1] + "/"
    return out


def binance_universe(net: Net, tag: str) -> dict[str, dict]:
    ei = net.get(BN + "/fapi/v1/exchangeInfo", bucket="bn", tag=tag) or {}
    info = {s["symbol"]: s for s in ei.get("symbols", [])}
    uni = {}
    for sym in sorted(set(info) | set(s3_um_symbols(net, tag))):
        s = info.get(sym)
        if s is not None:
            if (s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT"
                    or s.get("underlyingType") != "COIN" or s.get("status") == "PENDING_TRADING"):
                continue
            base, status, onboard = s["baseAsset"], s["status"], int(s.get("onboardDate") or 0)
        else:                               # нет в exchangeInfo — давно делистнут
            if not sym.endswith("USDT") or "_" in sym:
                continue
            base, status, onboard = sym[:-4], "GONE", 0
        if not non_alt(base):
            uni[sym] = {"base": base, "status": status, "onboard": onboard}
    return uni


def _bb_list(d) -> list:
    if not isinstance(d, dict) or d.get("retCode") != 0:
        return []
    return (d.get("result") or {}).get("list") or []


def bybit_universe(net: Net, tag: str, asof: int, extra: set[str]) -> dict[str, dict]:
    """Линейные USDT-перпы Bybit (без акций/ETF/сырья/форекса и премаркета) + делистнутые,
    найденные по именам делистнутых перпов Binance (Bybit отдаёт их историю)."""
    items, cursor = [], ""
    for _ in range(10):
        params = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        d = net.get(BB + "/v5/market/instruments-info", params, bucket="bb", tag=tag)
        res = (d or {}).get("result") or {}
        items += res.get("list") or []
        cursor = res.get("nextPageCursor") or ""
        if not cursor:
            break
    seen = {it.get("symbol") for it in items}
    uni = {}
    for it in items:
        if it.get("contractType") != "LinearPerpetual" or it.get("quoteCoin") != "USDT":
            continue
        if (it.get("symbolType") or "") not in ("", "innovation") or it.get("isPreListing"):
            continue
        if not non_alt(it.get("baseCoin", "")):
            uni[it["symbol"]] = {"base": it["baseCoin"], "status": it.get("status", ""),
                                 "end_ms": asof * 1000 - 1}

    # Делистнутые: свечи/OI Bybit ищутся только в окне перед end, а фандинг — нет, поэтому
    # дату последней выплаты берём из фандинга и дальше запрашиваем историю до неё.
    def probe(sym: str):
        return _bb_list(net.get(BB + "/v5/market/funding/history",
                                {"category": "linear", "symbol": sym, "limit": 200,
                                 "endTime": asof * 1000 - 1}, bucket="bb"))
    for sym, lst in pmap(probe, sorted(extra - seen), 6, "делистнутые?", net, time.time()).items():
        if lst:
            last = max(int(r["fundingRateTimestamp"]) for r in lst)
            uni[sym] = {"base": sym[:-4], "status": "GONE",
                        "end_ms": min(asof * 1000, (_ms_day(last) + 2 * DAY) * 1000) - 1}
    return uni


# ---------------------------------------------------------------- история по символу

def bn_klines(net: Net, sym: str, start_ms: int, asof: int) -> dict[int, tuple[float, float]]:
    """{day: (close, оборот USDT)} — дневные свечи USDT-M, только закрытые дни."""
    out: dict[int, tuple[float, float]] = {}
    st, end = start_ms, asof * 1000 - 1
    for _ in range(12):
        d = net.get(BN + "/fapi/v1/klines", {"symbol": sym, "interval": "1d", "startTime": st,
                                             "endTime": end, "limit": 1500}, bucket="bn_kl")
        if not isinstance(d, list) or not d:
            break
        for k in d:
            try:
                out[_ms_day(k[0])] = (float(k[4]), float(k[7]))
            except (ValueError, IndexError, TypeError):
                continue
        if len(d) < 1500:
            break
        st = int(d[-1][0]) + DAY_MS
    return out


def bn_funding(net: Net, sym: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    st = start_ms
    for _ in range(300):
        d = net.get(BN + "/fapi/v1/fundingRate", {"symbol": sym, "startTime": st,
                                                  "endTime": end_ms, "limit": 1000},
                    bucket="bn_fr")
        if not isinstance(d, list) or not d:
            break
        for r in d:
            try:
                out.append((int(r["fundingTime"]), float(r["fundingRate"])))
            except (KeyError, ValueError, TypeError):
                continue
        if len(d) < 1000:
            break
        st = int(d[-1]["fundingTime"]) + 1
    return out


BB_EARLIEST_MS = 1577836800000        # 2020-01-01: раньше USDT-перпов Bybit нет


def bb_klines(net: Net, sym: str, end_ms: int) -> dict[int, tuple[float, float]]:
    """{day: (close, оборот USDT)}. Bybit ищет свечи только в окне [end − limit·интервал, end]
    (проверено 03.10.2026: у делистнутого MATIC «end=сегодня» даёт пусто), поэтому идём явными
    окнами по 1000 дней назад, пока история не кончится."""
    out: dict[int, tuple[float, float]] = {}
    span = 1000 * DAY_MS
    end = end_ms
    while end > BB_EARLIEST_MS:
        start = end - span + 1
        lst = _bb_list(net.get(BB + "/v5/market/kline",
                               {"category": "linear", "symbol": sym, "interval": "D",
                                "start": start, "end": end, "limit": 1000}, bucket="bb"))
        for r in lst:
            try:
                out[_ms_day(r[0])] = (float(r[4]), float(r[6]))
            except (ValueError, IndexError, TypeError):
                continue
        if not lst and out:
            break
        end = start - 1
    return out


def bb_oi(net: Net, sym: str, start_ms: int, end_ms: int) -> dict[int, float]:
    """{day: OI в монетах} — снимок на 00:00 UTC дня (как oi_btc в market_daily). Окнами по
    190 дней назад (как у свечей, Bybit ищет только в окне перед endTime)."""
    out: dict[int, float] = {}
    span = 190 * DAY_MS
    end = end_ms
    floor = max(start_ms, BB_EARLIEST_MS)
    while end > floor:
        start = end - span + 1
        lst = _bb_list(net.get(BB + "/v5/market/open-interest",
                               {"category": "linear", "symbol": sym, "intervalTime": "1d",
                                "startTime": start, "endTime": end, "limit": 200}, bucket="bb"))
        for r in lst:
            try:
                out[_ms_day(r["timestamp"])] = float(r["openInterest"])
            except (KeyError, ValueError, TypeError):
                continue
        if not lst and out:
            break
        end = start - 1
    return out


def bb_funding(net: Net, sym: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    end = end_ms
    for _ in range(600):
        lst = _bb_list(net.get(BB + "/v5/market/funding/history",
                               {"category": "linear", "symbol": sym, "limit": 200,
                                "endTime": end}, bucket="bb"))
        if not lst:
            break
        for r in lst:
            try:
                out.append((int(r["fundingRateTimestamp"]), float(r["fundingRate"])))
            except (KeyError, ValueError, TypeError):
                continue
        oldest = min(int(r["fundingRateTimestamp"]) for r in lst)
        if oldest <= start_ms or oldest - 1 >= end:
            break
        end = oldest - 1
    return out


# ---------------------------------------------------------------- дневные ряды

def daily_funding(raw: list[tuple[int, float]]) -> dict[int, float]:
    """{day: средний за день фандинг, доля за 8ч}. Каждая выплата приводится к 8ч по шагу до
    предыдущей (8/4/2/1ч); дубликаты в пределах минуты схлопываются."""
    pts = sorted({t // 60000 * 60000: r for t, r in raw}.items())
    sums: dict[int, float] = {}
    cnt: dict[int, int] = {}
    for i, (t, r) in enumerate(pts):
        if i:
            gap = (t - pts[i - 1][0]) / 3.6e6
        else:
            gap = (pts[1][0] - t) / 3.6e6 if len(pts) > 1 else 8.0
        h = 8.0 if gap >= 6 else 4.0 if gap >= 3 else 2.0 if gap >= 1.5 else 1.0
        d = _ms_day(t)
        sums[d] = sums.get(d, 0.0) + r * 8.0 / h
        cnt[d] = cnt.get(d, 0) + 1
    return {d: sums[d] / cnt[d] for d in sums}


def rolling_mean(s: dict[int, float], win: int, min_n: int) -> dict[int, float]:
    """Среднее за окно win дней, заканчивающееся днём d (данные ≤ d), если точек ≥ min_n."""
    if not s:
        return {}
    out: dict[int, float] = {}
    q: deque = deque()
    acc = 0.0
    d, last = min(s), max(s)
    while d <= last:
        if d in s:
            q.append((d, s[d]))
            acc += s[d]
        while q and q[0][0] <= d - win * DAY:
            acc -= q.popleft()[1]
        if len(q) >= min_n:
            out[d] = acc / len(q)
        d += DAY
    return out


def top_members(vol: dict[str, dict[int, float]], days: list[int], n: int = TOP_N,
                only: set[str] | None = None) -> dict[int, list[str]]:
    """Корзина на каждый день: топ-n по среднему обороту за VOL_WIN дней среди перпов, которые
    торгуются в этот день и прожили ≥ MIN_LIFE дней (только данные ≤ дня)."""
    roll, first = {}, {}
    for s, v in vol.items():
        if only is not None and s not in only:
            continue
        roll[s] = rolling_mean(v, VOL_WIN, VOL_WIN * 2 // 3)
        first[s] = min(v)
    life = (MIN_LIFE - 1) * DAY
    out = {}
    for d in days:
        c = [(r[d], s) for s, r in roll.items()
             if d in r and d in vol[s] and d - first[s] >= life]
        c.sort(reverse=True)
        out[d] = [s for _, s in c[:n]]
    return out


def member_windows(members: dict[int, list[str]]) -> dict[str, tuple[int, int]]:
    w: dict[str, list[int]] = {}
    for d, mem in members.items():
        for s in mem:
            if s in w:
                w[s][1] = d
            else:
                w[s] = [d, d]
    return {s: (a, b) for s, (a, b) in w.items()}


def fund_block(fund: dict[str, dict[int, float]], members: dict[int, list[str]]) -> dict[str, dict]:
    """alt_fund30 (медиана 30д-средних, %/8ч), hi (доля ≥0.03%), dm (30д-среднее дневной
    медианы, %/8ч — вариант для хранения в market_daily), n."""
    roll = {s: rolling_mean(v, FUND_WIN, FUND_MIN) for s, v in fund.items()}
    med, hi, n, dmed = {}, {}, {}, {}
    for d, mem in members.items():
        vals = [roll[s][d] for s in mem if s in roll and d in roll[s]]
        if len(vals) >= MIN_PERPS:
            med[d] = statistics.median(vals) * 100
            hi[d] = sum(v >= FUND_HI for v in vals) / len(vals)
            n[d] = len(vals)
        today = [fund[s][d] for s in mem if s in fund and d in fund[s]]
        if len(today) >= MIN_PERPS:
            dmed[d] = statistics.median(today)
    dm = {d: v * 100 for d, v in rolling_mean(dmed, FUND_WIN, FUND_MIN).items()}
    return {"med": med, "hi": hi, "n": n, "dm": dm, "daily_med": dmed}


def rel_window(s: dict[int, float], win: int = OI_WIN, min_n: int = OI_MIN) -> dict[int, float]:
    m = rolling_mean(s, win, min_n)
    return {d: s[d] / m[d] for d in s if d in m and m[d] > 0}


def oi_block(oi: dict[str, dict[int, float]], close: dict[str, dict[int, float]],
             members: dict[int, list[str]], alt_ex: dict[int, float]) -> dict[str, dict]:
    """Цепной индекс OI в USD по корзине, его rel365, OI/капа альтов rel365, медиана per-perp
    rel365 (OI в монетах), сырая сумма OI USD rel365."""
    usd = {s: {d: v * close[s][d - DAY] for d, v in o.items()
               if (d - DAY) in close.get(s, {}) and v > 0}
           for s, o in oi.items()}
    idx, level, raw_sum, cnt = {}, None, {}, {}
    for d in sorted(members):
        mem = members[d]
        a = b = 0.0
        k = 0
        for s in mem:
            u = usd.get(s)
            if not u or d not in u or (d - DAY) not in u:
                continue
            ratio = u[d] / u[d - DAY]
            if 1 / 3 <= ratio <= 3:          # скачок ×3 за день — сбой данных/редоминация
                a += u[d]
                b += u[d - DAY]
                k += 1
        here = [usd[s][d] for s in mem if s in usd and d in usd[s]]
        if here:
            raw_sum[d] = sum(here)
            cnt[d] = len(here)
        if level is None:
            if k >= OI_LINK_MIN:
                level = 100.0
                idx[d] = level
            continue
        if k >= OI_LINK_MIN and b > 0:
            level *= a / b
        idx[d] = level
    per = {s: rel_window(o) for s, o in oi.items()}
    med = {}
    for d, mem in members.items():
        vals = [per[s][d] for s in mem if s in per and d in per[s]]
        if len(vals) >= MIN_PERPS:
            med[d] = statistics.median(vals)
    ratio = {d: v / alt_ex[d] for d, v in idx.items() if alt_ex.get(d)}
    full = {d: v for d, v in raw_sum.items() if cnt.get(d, 0) >= 45}   # корзина заполнена
    return {"rel": rel_window(idx), "mcap": rel_window(ratio), "med": med,
            "sum_rel": rel_window(full), "n": cnt, "idx": idx}


# ---------------------------------------------------------------- сбор по биржам

def collect_binance(net: Net, asof: int) -> dict:
    t0 = time.time()
    tag = _d(asof)
    uni = binance_universe(net, tag)
    n_tr = sum(u["status"] == "TRADING" for u in uni.values())
    print(f"[binance] альтовых USDT-M перпов {len(uni)}: торгуются {n_tr}, "
          f"делистнуты {len(uni) - n_tr}")
    close, vol = {}, {}

    def kl(sym: str):
        start = max(BN_START_MS, (uni[sym]["onboard"] or BN_START_MS) - DAY_MS)
        return bn_klines(net, sym, start, asof)
    for sym, k in pmap(kl, sorted(uni), 3, "свечи", net, t0).items():
        if k:
            close[sym] = {d: c for d, (c, _) in k.items()}
            vol[sym] = {d: v for d, (_, v) in k.items() if v > 0}
    vol = {s: v for s, v in vol.items() if v}
    days = list(range(min(min(v) for v in vol.values()), asof, DAY))
    pit = top_members(vol, days)
    surv = set(pit[days[-1]])
    surv_m = top_members(vol, days, only=surv)
    win = member_windows(pit)
    for s in surv:
        win[s] = (min(vol[s]), days[-1])
    print(f"[binance] корзина топ-{TOP_N}: за всё время {len(win)} разных перпов; фандинг…")
    fund = {}

    def fr(sym: str):
        a, b = win[sym]
        start = max(BN_START_MS, (a - (FUND_WIN + 15) * DAY) * 1000)
        return bn_funding(net, sym, start, min(b + DAY, asof) * 1000 - 1)
    for sym, raw in pmap(fr, sorted(win), 3, "фандинг", net, t0, 50).items():
        if raw:
            fund[sym] = daily_funding(raw)
    print(f"[binance] готово за {time.time() - t0:.0f} с: сеть {net.fetched}, кэш {net.cached}")
    return {"uni": uni, "close": close, "vol": vol, "days": days, "pit": pit, "surv": surv,
            "surv_m": surv_m, "fund": fund}


def collect_bybit(net: Net, asof: int, bn_uni: dict[str, dict]) -> dict:
    t0 = time.time()
    tag = _d(asof)
    gone = {s for s, u in bn_uni.items() if u["status"] != "TRADING"}
    uni = bybit_universe(net, tag, asof, gone)
    n_gone = sum(u["status"] == "GONE" for u in uni.values())
    print(f"[bybit] альтовых USDT-перпов {len(uni)}: торгуются {len(uni) - n_gone}, "
          f"делистнутые (найдены по именам Binance) {n_gone}")
    close, vol = {}, {}
    for sym, k in pmap(lambda s: bb_klines(net, s, uni[s]["end_ms"]), sorted(uni), 6,
                       "свечи", net, t0, 150).items():
        if k:
            close[sym] = {d: c for d, (c, _) in k.items()}
            vol[sym] = {d: v for d, (_, v) in k.items() if v > 0}
    vol = {s: v for s, v in vol.items() if v}
    days = list(range(min(min(v) for v in vol.values()), asof, DAY))
    pit = top_members(vol, days)
    surv = set(pit[days[-1]])
    surv_m = top_members(vol, days, only=surv)
    win = member_windows(pit)
    for s in surv:
        win[s] = (min(vol[s]), days[-1])
    print(f"[bybit] корзина топ-{TOP_N}: за всё время {len(win)} разных перпов; OI и фандинг…")
    oi, fund = {}, {}

    def oifr(sym: str):
        a, b = win[sym]
        end = min(min(b + 2 * DAY, asof) * 1000 - 1, uni[sym]["end_ms"])
        return (bb_oi(net, sym, (a - (OI_WIN + 40) * DAY) * 1000, end),
                bb_funding(net, sym, (a - (FUND_WIN + 15) * DAY) * 1000, end))
    for sym, (o, raw) in pmap(oifr, sorted(win), 6, "OI/фандинг", net, t0, 50).items():
        if o:
            oi[sym] = o
        if raw:
            fund[sym] = daily_funding(raw)
    print(f"[bybit] готово за {time.time() - t0:.0f} с: сеть {net.fetched}, кэш {net.cached}")
    return {"uni": uni, "close": close, "vol": vol, "days": days, "pit": pit, "surv": surv,
            "surv_m": surv_m, "oi": oi, "fund": fund}


# ---------------------------------------------------------------- рынок и пружины

def load_market(cfg: Config) -> tuple[dict, dict]:
    """market_daily только на чтение (Store при открытии пишет схему — не используем)."""
    db = (ROOT / cfg["output"]["db_path"]).resolve()
    con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM market_daily ORDER BY day")]
    con.close()
    if not rows:
        sys.exit("market_daily пуста — сначала `py -3 run.py market`")
    S = regime.market_series(rows)
    return S, regime.market_feature_table(S)


def load_coins(offline: bool) -> dict[str, tuple[list[int], list[float], list[float]]]:
    """Как market_regime_study.load_coins, но теги не-альтов Binance кэшируются на диск."""
    p = CACHE / "binance_non_alt_tags.json"
    if p.exists():
        tagged = set(json.loads(p.read_text(encoding="utf-8")))
    elif offline:
        print("[coins] нет кэша тегов Binance — только список _EXCL")
        tagged = set()
    else:
        tagged = _tagged_non_alts()
        if tagged:
            p.write_text(json.dumps(sorted(tagged)), encoding="utf-8")
    excl = _EXCL | tagged
    out = {}
    for f in sorted(COINS.glob("*.json")):
        base = f.stem[:-4] if f.stem.endswith("USDT") else f.stem
        if f.stem == "BTCUSDT" or base in excl:
            continue
        d = json.loads(f.read_text())
        if len(d["cl"]) >= 300:
            out[f.stem] = ([int(t) for t in d["ts"]], d["cl"], d["vol"])
    return out


def cfg_with(cfg: Config, flag: tuple[str, list] | None = None, alert: float | None = None) -> Config:
    d = copy.deepcopy(cfg._d)
    if flag:
        d["market_regime"]["hot_flags"][flag[0]] = flag[1]
    if alert is not None:
        d["stage8_exit"]["market_hot_alert"] = alert
    return Config(d)


def cycles() -> list[tuple[str, int, int]]:
    """Циклы с данными по плечу альтов (2018-20 — до перпов) + всё вместе."""
    cs = [c for c in CYCLES if c[0] != "2018-20"]
    return cs + [("всё", cs[0][1], cs[-1][2])]


# ---------------------------------------------------------------- статистика

def boot_diff(on: list[dict], off: list[dict], reps: int = 2000, seed: int = 7) -> list | None:
    """90% интервал P(on) − P(off) кластерным бутстрепом по кварталам входа."""
    if len(on) < MIN_CELL or len(off) < MIN_CELL:
        return None
    rng = random.Random(seed)
    qs = sorted({e["q"] for e in on + off})
    by = {q: ([e["hit365"] for e in on if e["q"] == q], [e["hit365"] for e in off if e["q"] == q])
          for q in qs}
    diffs = []
    for _ in range(reps):
        a, b = [], []
        for _ in qs:
            q = qs[rng.randrange(len(qs))]
            a += by[q][0]
            b += by[q][1]
        if a and b:
            diffs.append(sum(a) / len(a) - sum(b) / len(b))
    if len(diffs) < reps // 2:
        return None
    diffs.sort()
    return [round(diffs[int(0.05 * len(diffs))], 3), round(diffs[int(0.95 * len(diffs)) - 1], 3)]


def split(eps: list[dict], key: str, thr: float, group) -> dict:
    """P(+100/365д) при признаке ≥ порога и ниже — внутри группы, по циклам."""
    res = {}
    for cname, a, b in cycles():
        E = [e for e in eps if a <= e["day"] < b and group(e)]
        have = [e for e in E if e["alt"].get(key) is not None]
        on = [e for e in have if e["alt"][key] >= thr]
        off = [e for e in have if e["alt"][key] < thr]
        res[cname] = {"n_group": len(E), "n_on": len(on), "n_off": len(off),
                      "q_on": len({e["q"] for e in on}), "q_on_list": sorted({e["q"] for e in on}),
                      "q_off_list": sorted({e["q"] for e in off}),
                      "p_on": _r(_p(on)), "p_off": _r(_p(off)),
                      "diff": _r(_p(on) - _p(off)) if on and off else None,
                      "ci90": boot_diff(on, off)}
    return res


def lit_share(feats: dict[int, dict], name: str, op: str, thr: float, d0: int, d1: int) -> float | None:
    v = [f[name] for d, f in feats.items() if d0 <= d < d1 and isinstance(f.get(name), (int, float))]
    if not v:
        return None
    return sum((x >= thr) if op == ">=" else (x <= thr) for x in v) / len(v)


def quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(q * len(s))))]


def extremes(alt: dict[int, float], d0: int, last: int, w: int = 90) -> list[tuple[int, str]]:
    ds = sorted(d for d in alt if d >= d0 - w * DAY)
    out = []
    for i, d in enumerate(ds):
        if d < d0 or d > last - 20 * DAY:
            continue
        win = [alt[x] for x in ds[max(0, i - w):i + w + 1]]
        if alt[d] == max(win):
            out.append((d, "вершина"))
        elif alt[d] == min(win):
            out.append((d, "дно"))
    return out


def peak_offset(s: dict[int, float], d: int, kind: str, before: int = 120, after: int = 30):
    """Где был экстремум признака вокруг экстремума рынка: (смещение дней, значение)."""
    w = [(x, s[x]) for x in range(d - before * DAY, d + after * DAY + 1, DAY) if x in s]
    if len(w) < 20:
        return None, None
    x, v = (max if kind == "вершина" else min)(w, key=lambda t: t[1])
    return (x - d) // DAY, v


def first_cross(s: dict[int, float], d: int, thr: float, before: int = 180) -> int | None:
    """За сколько дней до вершины признак впервые (в окне) дошёл до порога; None — не дошёл."""
    for x in range(d - before * DAY, d + 1, DAY):
        if s.get(x) is not None and s[x] >= thr:
            return (x - d) // DAY
    return None


def runs(on_days: list[int], gap: int = 15) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for d in sorted(on_days):
        if out and d - out[-1][1] <= gap * DAY:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [(a, b) for a, b in out]


def corr(a: dict[int, float], b: dict[int, float]) -> tuple[float | None, int]:
    ks = [k for k in a if k in b]
    if len(ks) < 30:
        return None, len(ks)
    return round(statistics.correlation([a[k] for k in ks], [b[k] for k in ks]), 3), len(ks)


# ---------------------------------------------------------------- блоки исследования

def map_block(feats, alt_ex, last, apriori) -> dict:
    """Карта признаков у вершин и днищ альт-рынка, опережение против BTC-флагов."""
    pairs = [("alt_fund30", "fund30"), ("alt_oi_rel", "oi_rel365")]
    keys = ["alt_fund30", "alt_fund_hi", "alt_oi_rel", "alt_oi_mcap", "fund30", "oi_rel365"]
    series = {k: {d: f[k] for d, f in feats.items() if isinstance(f.get(k), (int, float))}
              for k in keys + CONTROLS}
    thr = dict(apriori)
    thr["fund30"], thr["oi_rel365"] = 0.02, 1.3
    rows = []
    print("\n=== 1. КАРТА: признаки на вершинах и днищах альт-рынка (без стейблов) ===")
    print("  дата        тип      alt_ex | фанд.альт (пик, дн) | доля≥.03 | фанд.BTC (пик, дн) | "
          "OI альт (пик) | OI/капа | OI BTC (пик) | флаги")
    for d, kind in extremes(alt_ex, MAP_FROM, last):
        f = feats.get(d, {})
        row = {"day": _d(d), "kind": kind, "alt_ex_bn": _r(alt_ex[d] / 1e9, 0)}
        for k in keys:
            off, v = peak_offset(series[k], d, kind)
            row[k] = _r(f.get(k), 4)
            row[k + "_peak_off"] = off
            row[k + "_peak"] = _r(v, 4)
            if kind == "вершина":
                row[k + "_first_cross"] = first_cross(series[k], d, thr[k])
        h = regime.hot_flags(f, CFG8)
        row["hot8"] = f"{h['n_lit']}/{h['avail']}"
        rows.append(row)

        def fmt(k, nd=3):
            v = row[k]
            if v is None:
                return "   —   "
            return f"{v:.{nd}f}" + (f"({row[k + '_peak_off']:+d})" if row[k + "_peak_off"] is not None else "")
        print(f"  {row['day']}  {kind:7} {row['alt_ex_bn']:6.0f} | {fmt('alt_fund30'):>17} | "
              f"{fmt('alt_fund_hi', 2):>11} | {fmt('fund30'):>17} | {fmt('alt_oi_rel', 2):>12} | "
              f"{fmt('alt_oi_mcap', 2):>11} | {fmt('oi_rel365', 2):>11} | {row['hot8']}")
    # опережение: на вершинах — пик признака относительно вершины; альт против BTC
    lead = {}
    for a, b in pairs:
        tops = [r for r in rows if r["kind"] == "вершина"
                and r[a + "_peak_off"] is not None and r[b + "_peak_off"] is not None]
        lead[a] = {"tops": len(tops),
                   "alt_peak_off": [r[a + "_peak_off"] for r in tops],
                   "btc_peak_off": [r[b + "_peak_off"] for r in tops],
                   "alt_first_cross": [r.get(a + "_first_cross") for r in tops],
                   "btc_first_cross": [r.get(b + "_first_cross") for r in tops],
                   "alt_minus_btc_peak": [r[a + "_peak_off"] - r[b + "_peak_off"] for r in tops]}
        print(f"  {a} против {b}: пик на вершинах (дней от вершины) альт {lead[a]['alt_peak_off']} "
              f"/ BTC {lead[a]['btc_peak_off']}; первое касание порога альт "
              f"{lead[a]['alt_first_cross']} / BTC {lead[a]['btc_first_cross']}")
    # корреляции с BTC-аналогами и контролями (уровни)
    cors = {}
    for a, b in [("alt_fund30", "fund30"), ("alt_fund30", "alt_fund30_surv"),
                 ("alt_fund30", "alt_fund30_bybit"), ("alt_fund30", "alt_fund30_dm"),
                 ("alt_fund30", "alt_fund_hi"), ("alt_oi_rel", "oi_rel365"),
                 ("alt_oi_rel", "alt_oi_rel_surv"), ("alt_oi_rel", "alt_oi_sum_rel"),
                 ("alt_oi_rel", "alt_oi_mcap"), ("alt_oi_mcap", "alt_oi_rel_med"),
                 ("alt_oi_rel", "alt_vs_sma200"), ("alt_fund30", "alt_vs_sma200")]:
        src_b = series.get(b) or {d: f[b] for d, f in feats.items() if isinstance(f.get(b), (int, float))}
        c, n = corr(series[a], src_b)
        cors[f"{a}~{b}"] = {"r": c, "days": n}
    print("  корреляции уровней: " + ", ".join(f"{k} {v['r']}" for k, v in cors.items()))
    monthly = {}
    for d in sorted(series["alt_fund30"]) + sorted(series["alt_oi_rel"]):
        if time.gmtime(d + DAY).tm_mday == 1:            # последний день месяца
            monthly[_d(d)[:7]] = {k: _r(series[k].get(d), 4) for k in keys + CONTROLS}
    return {"extremes": rows, "lead": lead, "corr": cors,
            "monthly": dict(sorted(monthly.items()))}


def overlap_block(feats: dict, hot8: dict) -> dict:
    """Пересечение с текущим индексом по ДНЯМ (не только дням пружин): сколько дней 9-й флаг
    горит при «0 из 8» — только в эти дни он может что-то добавить ко входу."""
    out = {}
    print("\n=== 0. ПЕРЕСЕЧЕНИЕ С ИНДЕКСОМ (дни с 2020): 9-й флаг горит, а из 8 — ни одного ===")
    for k, (_, grid) in FEATS.items():
        days = [d for d, f in feats.items() if d >= MAP_FROM and isinstance(f.get(k), (int, float))
                and (hot8.get(d) or {}).get("score") is not None]
        if not days:
            continue
        cold = [d for d in days if hot8[d]["n_lit"] == 0]
        cv = sorted(feats[d][k] for d in cold)
        row = {"days": len(days), "cold_days": len(cold),
               "cold_max": _r(cv[-1], 4) if cv else None,
               "cold_p90": _r(quantile(cv, 0.9), 4) if cv else None,
               "cold_max_day": _d(max(cold, key=lambda d: feats[d][k])) if cold else None,
               "by_thr": {}}
        for t in grid:
            on = [d for d in days if feats[d][k] >= t]
            oc = [d for d in on if hot8[d]["n_lit"] == 0]
            row["by_thr"][str(t)] = {"on": len(on), "on_cold": len(oc),
                                     "on_cold_quarters": sorted({_quarter(d) for d in oc})}
        # какие из 8 флагов горят, когда горит 9-й (на априорном пороге)
        on = [d for d in days if feats[d][k] >= APRIORI[k]]
        cnt: dict[str, int] = {}
        for d in on:
            for name in hot8[d]["lit"]:
                cnt[name] = cnt.get(name, 0) + 1
        row["lit_with"] = {n: _r(c / len(on), 2) for n, c in sorted(cnt.items(), key=lambda x: -x[1])} if on else {}
        out[k] = row
        print(f"  {k:12} дней {len(days)}, из них холодных {len(cold)}; в холодные дни макс. "
              f"{row['cold_max']} ({row['cold_max_day']}), p90 {row['cold_p90']} | горит/из них при 0 флагов: "
              + ", ".join(f"≥{t}: {v['on']}/{v['on_cold']}" for t, v in row["by_thr"].items()))
        print(f"      при {k} ≥{APRIORI[k]} горят (доля дней): "
              + ", ".join(f"{n} {v}" for n, v in row["lit_with"].items()))
    return out


def entry_block(eps: list[dict], apriori: dict[str, float]) -> dict:
    full = [e for e in eps if e["fwd"] >= 365 and e["cand"].market_hot_score is not None]
    cold = lambda e: e["cand"].market_hot_score == 0           # noqa: E731
    warm = lambda e: e["cand"].market_hot_score > 0            # noqa: E731
    anyg = lambda e: True                                      # noqa: E731
    res = {"episodes": len(full), "base": {}, "grid": {}, "warm_grid": {}, "all_grid": {},
           "controls": {}}
    print(f"\n=== 2. ВХОД: пружины с форвардом ≥365д и известным индексом перегрева: {len(full)} ===")
    for cname, a, b in cycles():
        E = [e for e in full if a <= e["day"] < b]
        c, w = [e for e in E if cold(e)], [e for e in E if warm(e)]
        cov = {k: _r(sum(e["alt"].get(k) is not None for e in c) / len(c), 2) if c else None
               for k in FEATS}
        res["base"][cname] = {"cold_n": len(c), "cold_p": _r(_p(c)), "warm_n": len(w),
                              "warm_p": _r(_p(w)), "cold_coverage": cov}
        print(f"  {cname}: 0 флагов P {(_p(c) or 0)*100:3.0f}% (n{len(c)}) | ≥1 флаг "
              f"P {(_p(w) or 0)*100:3.0f}% (n{len(w)}) | покрытие признаками в «0 флагов»: "
              + ", ".join(f"{k} {v}" for k, v in cov.items()))
    print("\n--- внутри «0 текущих флагов»: P(+100/365д) признак ≥ порога (on) против < (off) ---")
    print("      (n_on/кварталов_on; разница on−off, 90% CI кластерного бутстрепа по кварталам)")
    for k, (label, grid) in FEATS.items():
        res["grid"][k] = {}
        res["warm_grid"][k] = {}
        res["all_grid"][k] = {}
        for t in grid:
            r = split(full, k, t, cold)
            res["grid"][k][str(t)] = r
            res["warm_grid"][k][str(t)] = split(full, k, t, warm)
            res["all_grid"][k][str(t)] = split(full, k, t, anyg)
            mark = " ←априори" if abs(t - apriori[k]) < 1e-9 else ""
            print(f"  {k:12} ≥{t:<5}: " + " | ".join(
                f"{c}: on {('%.0f%%' % (v['p_on']*100)) if v['p_on'] is not None else '—':>4} "
                f"(n{v['n_on']}/{v['q_on']}кв) off {('%.0f%%' % (v['p_off']*100)) if v['p_off'] is not None else '—':>4} "
                f"(n{v['n_off']})"
                + (f" Δ{v['diff']*100:+.0f}пп" if v["diff"] is not None else "")
                + (f" [{v['ci90'][0]*100:+.0f};{v['ci90'][1]*100:+.0f}]" if v["ci90"] else "")
                for c, v in r.items()) + mark)
    print("\n--- то же внутри «≥1 флаг» и по всем пружинам (априорные пороги) ---")
    for k in FEATS:
        t = str(apriori[k])
        for nm, g in (("≥1 флаг", res["warm_grid"]), ("все", res["all_grid"])):
            r = g[k][t]
            print(f"  {k:12} ≥{t:<5} [{nm:7}]: " + " | ".join(
                f"{c}: on {('%.0f%%' % (v['p_on']*100)) if v['p_on'] is not None else '—'} (n{v['n_on']}) "
                f"off {('%.0f%%' % (v['p_off']*100)) if v['p_off'] is not None else '—'} (n{v['n_off']})"
                for c, v in r.items()))
    print("\n--- контроли (внутри «0 флагов», порог как у основного признака) ---")
    for k, base in (("alt_fund30_surv", "alt_fund30"), ("alt_fund30_bybit", "alt_fund30"),
                    ("alt_fund30_dm", "alt_fund30"), ("alt_oi_rel_surv", "alt_oi_rel"),
                    ("alt_oi_rel_med", "alt_oi_rel"), ("alt_oi_sum_rel", "alt_oi_rel")):
        t = apriori[base]
        r = split(full, k, t, cold)
        res["controls"][k] = {"thr": t, **r}
        print(f"  {k:16} ≥{t:<5}: " + " | ".join(
            f"{c}: on {('%.0f%%' % (v['p_on']*100)) if v['p_on'] is not None else '—'} (n{v['n_on']}) "
            f"off {('%.0f%%' % (v['p_off']*100)) if v['p_off'] is not None else '—'} (n{v['n_off']})"
            for c, v in r.items()))
    return res


def nine_flag_block(cfg: Config, eps: list[dict], feats: dict, apriori: dict) -> dict:
    """Индекс из 9 флагов: «0 vs ≥1» и терцили прод-множителя spring_quality (как alt_prod)."""
    full = [e for e in eps if e["fwd"] >= 365 and e["cand"].market_hot_score is not None]
    res = {}
    print("\n=== 2б. ИНДЕКС ИЗ 9 ФЛАГОВ: «0 флагов» и прод-множитель spring_quality ===")
    base = tercile_report(full, "q8")
    res["8 флагов"] = {"terciles": base, "cut": cut_9(full, "hot8")}
    print("  8 флагов (прод): " + tercile_line(base) + " | " + cut_line(res["8 флагов"]["cut"]))
    for k in FEATS:
        key = f"hot9_{k}"
        res[k] = {"thr": apriori[k], "terciles": tercile_report(full, f"q9_{k}"),
                  "cut": cut_9(full, key)}
        print(f"  +{k:11} ≥{apriori[k]:<5}: " + tercile_line(res[k]["terciles"]) + " | "
              + cut_line(res[k]["cut"]))
    return res


def swap_block(cfg: Config, eps: list[dict], feats: dict, apriori: dict) -> dict:
    """oi_rel365 (OI BTC в монетах) горел весь медвежий 2022: в монетах OI растёт, когда цена
    падает. Пружины, где горел ТОЛЬКО он, и индекс с заменой oi_rel365 на альтовый OI в USD."""
    full = [e for e in eps if e["fwd"] >= 365 and e["cand"].market_hot_score is not None]
    res = {"only_oi_rel365": {}, "swap": {}}
    print("\n=== 2в. ЗАМЕНА oi_rel365 (BTC, в монетах) НА OI АЛЬТОВ (в USD) ===")
    for cname, a, b in cycles():
        E = [e for e in full if a <= e["day"] < b]
        only = [e for e in E if e["cand"].market_hot_lit == ["oi_rel365"]]
        cold = [e for e in E if e["cand"].market_hot_score == 0]
        res["only_oi_rel365"][cname] = {"n": len(only), "p": _r(_p(only)),
                                        "cold_n": len(cold), "cold_p": _r(_p(cold))}
        print(f"  {cname}: горел только oi_rel365 — P {(_p(only) or 0)*100:3.0f}% (n{len(only)}, "
              f"кварталов {len({e['q'] for e in only})}) | 0 флагов — P {(_p(cold) or 0)*100:3.0f}% "
              f"(n{len(cold)})")
    for k in ("alt_oi_rel", "alt_oi_mcap"):
        d = copy.deepcopy(cfg._d)
        flags = d["market_regime"]["hot_flags"]
        flags.pop("oi_rel365", None)
        flags[k] = [">=", apriori[k], round(apriori[k] * 0.8, 4)]
        c = Config(d)
        out = {}
        for cname, a, b in cycles():
            E = [e for e in full if a <= e["day"] < b]
            hs = [(e, regime.hot_flags(feats.get(e["day"] - DAY, {}), c)["score"]) for e in E]
            cold = [e for e, s in hs if s == 0]
            warm = [e for e, s in hs if s is not None and s > 0]
            out[cname] = {"cold_n": len(cold), "cold_p": _r(_p(cold)), "warm_n": len(warm),
                          "warm_p": _r(_p(warm))}
        res["swap"][k] = out
        print(f"  oi_rel365 → {k} ≥{apriori[k]}: " + cut_line(out))
    return res


def cut_9(full: list[dict], key: str) -> dict:
    out = {}
    for cname, a, b in cycles():
        E = [e for e in full if a <= e["day"] < b and e[key] is not None]
        c = [e for e in E if e[key] == 0]
        w = [e for e in E if e[key] > 0]
        out[cname] = {"cold_n": len(c), "cold_p": _r(_p(c)), "warm_n": len(w), "warm_p": _r(_p(w))}
    return out


def cut_line(c: dict) -> str:
    return " ".join(f"{k}: 0ф {v['cold_p']*100 if v['cold_p'] is not None else float('nan'):.0f}%"
                    f"(n{v['cold_n']}) ≥1 {v['warm_p']*100 if v['warm_p'] is not None else float('nan'):.0f}%"
                    for k, v in c.items())


def tercile_line(r: dict) -> str:
    t = r["terciles"]
    sp = r["spread_high_minus_low_by_cycle"]
    return (f"P низ/верх {t['low']['p365']*100:.0f}%/{t['high']['p365']*100:.0f}%, спред "
            + ", ".join(f"{k} {v*100:+.0f}пп" for k, v in sp.items()))


def same_denominator(hot8: dict, hot9: dict) -> dict:
    """9-й флаг добавляет горящий, но не доступный флаг: score = n_lit(9) / avail(8).
    Порог «≥3 горящих» не поднимается — чистый эффект дополнительной информации."""
    out = {}
    for d, h in hot8.items():
        h9 = hot9.get(d) or {}
        if h.get("score") is None:
            out[d] = {"score": None, "lit": []}
        else:
            out[d] = {"score": round(h9.get("n_lit", 0) / h["avail"], 3), "lit": h9.get("lit", [])}
    return out


def exit_block(cfg: Config, eps: list[dict], coins: dict, hot8: dict, hot9s: dict,
               hot9n: dict) -> dict:
    """A (прод) / B8 (сужение трейла при ≥0.375 по 8 флагам) / B9 (9-й флаг в config как есть:
    доля из 9 — нужно 4 горящих) / B9n (знаменатель 8 — по-прежнему 3 горящих)."""
    alert = cfg["stage8_exit"]["market_hot_alert"]
    variants = {"A": (cfg, hot8, "A"), "B8": (cfg, hot8, "B")}
    for k, h in hot9s.items():
        variants[f"B9_{k}"] = (cfg, h, "B")
        variants[f"B9n_{k}"] = (cfg, hot9n[k], "B")
    print(f"\n=== 3. ВЫХОД: прод evaluate_exit, A vs B (трейл "
          f"{cfg['stage8_exit']['market_hot_trailing_pct']}% при перегреве ≥{alert}), "
          f"{len(eps)} пружин ===")
    t0 = time.time()
    for name, (c, h, var) in variants.items():
        al = c["stage8_exit"]["market_hot_alert"]
        # дни, где сигнал B9 отличается от B8: вне них результат B9 = B8 (экономия времени)
        diff_days = sorted(d for d in set(h) | set(hot8)
                           if (((h.get(d) or {}).get("score") or 0) >= al)
                           != (((hot8.get(d) or {}).get("score") or 0) >= alert))
        for e in eps:
            ts, cl, _ = coins[e["sym"]]
            if name.startswith("B9"):
                lo, hi = e["day"] - DAY, e["day"] + (730 + 1) * DAY
                i = _bisect_left(diff_days, lo)
                if i >= len(diff_days) or diff_days[i] > hi:
                    e.setdefault("net", {})[name] = e["net"]["B8"]
                    continue
            e.setdefault("net", {})[name] = sim_prod_exit(c, cl, ts, e["e"], h, var)
    res = {}
    for name in variants:
        r = {"all": _stats([e["net"][name] for e in eps]), "by_cycle": {}}
        if name != "A":
            touched = [e for e in eps if abs(e["net"][name] - e["net"]["A"]) > 1e-9]
            r["touched"] = len(touched)
            r["better_than_A"] = sum(e["net"][name] > e["net"]["A"] for e in touched)
            if name != "B8":
                diff = [e for e in eps if abs(e["net"][name] - e["net"]["B8"]) > 1e-9]
                r["differs_from_B8"] = len(diff)
                r["better_than_B8"] = sum(e["net"][name] > e["net"]["B8"] for e in diff)
        for cname, a, b in CYCLES:
            E = [e for e in eps if a <= e["day"] < b]
            if len(E) >= MIN_CELL:
                r["by_cycle"][cname] = _stats([e["net"][name] for e in E])
        res[name] = r
        s = r["all"]
        extra = (f" | затронуто {r['touched']}, лучше A в {r['better_than_A']}" if name != "A" else "")
        if "differs_from_B8" in r:
            extra += f" | отличается от B8 в {r['differs_from_B8']}, лучше в {r['better_than_B8']}"
        print(f"  {name:22} mean {s['mean']*100:+6.1f}% med {s['median']*100:+6.1f}% "
              f"p25 {s['p25']*100:+6.1f}% win {s['win']*100:3.0f}%{extra}")
        print("      " + " | ".join(f"{c}: mean {v['mean']*100:+.0f}% med {v['median']*100:+.1f}%"
                                    for c, v in r["by_cycle"].items()))
    print(f"  ({time.time() - t0:.0f} с)")
    return res


def market_signal_block(cfg: Config, feats: dict, alt_ex: dict, hot8: dict, hot9s: dict,
                        hot9n: dict, last: int) -> dict:
    """Сигнал «перегрев» как сигнал вершины альт-рынка: форвард alt_ex после дней с сигналом."""
    alert = cfg["stage8_exit"]["market_hot_alert"]
    tops = [d for d, k in extremes(alt_ex, MAP_FROM, last) if k == "вершина"]
    res = {}
    print(f"\n=== 3б. СИГНАЛ ВЕРШИНЫ: альт-рынок (без стейблов) после дней с перегревом ≥{alert} ===")
    sets = {"8 флагов": (hot8, alert)}
    for k, h in hot9s.items():
        sets[f"+{k}"] = (h, alert)
        sets[f"+{k} (знам. 8)"] = (hot9n[k], alert)
    span0 = MAP_FROM
    for name, (h, al) in sets.items():
        days = [d for d in sorted(h) if d >= span0 and h[d].get("score") is not None
                and d + 180 * DAY in alt_ex and d in alt_ex]
        on = [d for d in days if h[d]["score"] >= al]
        f90 = {d: alt_ex[d + 90 * DAY] / alt_ex[d] - 1 for d in days if d + 90 * DAY in alt_ex}
        f180 = {d: alt_ex[d + 180 * DAY] / alt_ex[d] - 1 for d in days}
        r = {"days": len(days), "on_days": len(on),
             "fwd90_on_med": _r(statistics.median(f90[d] for d in on if d in f90)) if on else None,
             "fwd180_on_med": _r(statistics.median(f180[d] for d in on)) if on else None,
             "fwd180_all_med": _r(statistics.median(f180.values())) if f180 else None,
             "p_fwd180_le_m30_on": _r(sum(f180[d] <= -0.3 for d in on) / len(on)) if on else None,
             "p_fwd180_le_m30_all": _r(sum(v <= -0.3 for v in f180.values()) / len(f180)) if f180 else None,
             "runs": [], "tops": {}}
        all_on = [d for d in sorted(h) if d >= span0 and h[d].get("score") is not None
                  and h[d]["score"] >= al]
        for a, b in runs(all_on):
            r["runs"].append({"from": _d(a), "to": _d(b), "days": (b - a) // DAY + 1})
        for t in tops:
            fire = [d for d in all_on if t - 120 * DAY <= d <= t + 14 * DAY]
            r["tops"][_d(t)] = (fire[0] - t) // DAY if fire else None
        res[name] = r
        print(f"  {name:22} дней с сигналом {len(on):4}/{len(days)} | форвард 180д: медиана "
              f"{(r['fwd180_on_med'] or 0)*100:+5.0f}% (все дни {(r['fwd180_all_med'] or 0)*100:+4.0f}%), "
              f"P(≤−30%) {(r['p_fwd180_le_m30_on'] or 0)*100:3.0f}% (все {(r['p_fwd180_le_m30_all'] or 0)*100:3.0f}%) | "
              f"первый сигнал до вершин (дн): {r['tops']}")
    res["single"] = single_flag_signals(feats, alt_ex, tops)
    return res


def single_flag_signals(feats: dict, alt_ex: dict, tops: list[int]) -> dict:
    """Каждый признак сам по себе как сигнал вершины — альтовый против BTC-аналога на ОДНИХ
    И ТЕХ ЖЕ днях (где есть оба): форвард альт-рынка 180д после дней «горит»."""
    hf = CFG8["market_regime"]["hot_flags"]
    pairs = [("alt_fund30", "fund30", APRIORI["alt_fund30"], hf["fund30"][1]),
             ("alt_fund_hi", "fund30", APRIORI["alt_fund_hi"], hf["fund30"][1]),
             ("alt_oi_rel", "oi_rel365", APRIORI["alt_oi_rel"], hf["oi_rel365"][1]),
             ("alt_oi_mcap", "oi_rel365", APRIORI["alt_oi_mcap"], hf["oi_rel365"][1])]
    ref, tref = "alt_vs_sma200", hf["alt_vs_sma200"][1]     # ценовой флаг — для сравнения
    out = {}
    print("\n--- по одному признаку (те же дни): форвард альт-рынка 180д, когда признак горит ---")
    for a, b, ta, tb in pairs:
        days = [d for d, f in sorted(feats.items()) if d >= MAP_FROM
                and isinstance(f.get(a), (int, float)) and isinstance(f.get(b), (int, float))
                and isinstance(f.get(ref), (int, float))
                and d in alt_ex and d + 180 * DAY in alt_ex]
        if len(days) < 100:
            continue
        f180 = {d: alt_ex[d + 180 * DAY] / alt_ex[d] - 1 for d in days}
        row = {"span": f"{_d(days[0])}…{_d(days[-1])}", "days": len(days),
               "all_med": _r(statistics.median(f180.values())),
               "all_p_le_m30": _r(sum(v <= -0.3 for v in f180.values()) / len(f180))}
        for k, t in ((a, ta), (b, tb), (ref, tref)):
            on = [d for d in days if feats[d][k] >= t]
            on_all = [d for d, f in feats.items() if d >= MAP_FROM
                      and isinstance(f.get(k), (int, float)) and f[k] >= t]
            row[k] = {"thr": t, "on_days": len(on),
                      "med": _r(statistics.median(f180[d] for d in on)) if on else None,
                      "p_le_m30": _r(sum(f180[d] <= -0.3 for d in on) / len(on)) if on else None,
                      "tops": {_d(tp): next(((d - tp) // DAY for d in sorted(on_all)
                                             if tp - 120 * DAY <= d <= tp + 14 * DAY), None)
                               for tp in tops}}
        out[f"{a}~{b}"] = row
        print(f"  {row['span']} ({row['days']} дн; все дни: медиана {row['all_med']*100:+.0f}%, "
              f"P(≤−30%) {row['all_p_le_m30']*100:.0f}%)")
        for k in (a, b, ref):
            v = row[k]
            if v["on_days"]:
                print(f"     {k:12} ≥{v['thr']:<5} горит {v['on_days']:4} дн: медиана "
                      f"{v['med']*100:+5.0f}%, P(≤−30%) {v['p_le_m30']*100:3.0f}% | первое касание "
                      f"до вершин (дн): {v['tops']}")
            else:
                print(f"     {k:12} ≥{v['thr']:<5} ни разу не горел на этих днях")
    return out


def current_block(feats: dict, last: int, apriori: dict, cfg: Config) -> dict:
    f = feats.get(last, {})
    out = {"day": _d(last), "hot8": regime.hot_flags(f, cfg)}
    print(f"\n=== ТЕКУЩИЕ ПОКАЗАНИЯ ({_d(last)}) ===")
    print(f"  индекс перегрева (8 флагов): {out['hot8']['n_lit']}/{out['hot8']['avail']}, "
          f"близко: {out['hot8']['near']}")
    for k in list(FEATS) + CONTROLS + ["fund30", "oi_rel365"]:
        v = f.get(k)
        out[k] = _r(v, 4)
        thr = apriori.get(k)
        print(f"  {k:16} {v if v is None else round(v, 4)}"
              + (f"  (априорный порог {thr})" if thr is not None else ""))
    return out


# ---------------------------------------------------------------- main

CFG8: Config | None = None
APRIORI: dict[str, float] = {}


def main() -> int:
    global CFG8
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="только кэш, без сети")
    ap.add_argument("--refresh", action="store_true", help="сдвинуть дату среза на сегодня")
    ap.add_argument("--fetch-only", choices=["binance", "bybit"],
                    help="только скачать историю одной биржи (для параллельной загрузки)")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)   # прогресс виден и при выводе в файл
    except (AttributeError, ValueError):
        pass
    t0 = time.time()
    asof = asof_day(args.refresh)
    last = asof - DAY
    print(f"срез данных: закрытые дни до {_d(last)} включительно")
    net = Net(offline=args.offline)
    if args.fetch_only == "bybit":
        bn_uni = binance_universe(net, _d(asof))
        collect_bybit(net, asof, bn_uni)
        return 0
    bn = collect_binance(net, asof)
    if args.fetch_only == "binance":
        return 0
    bb = collect_bybit(net, asof, bn["uni"])

    cfg = load_config()
    CFG8 = cfg
    S, feats = load_market(cfg)
    alt_ex = S.get("alt_ex", {})

    # --- признаки плеча альтов
    fb = fund_block(bn["fund"], bn["pit"])
    fs = fund_block(bn["fund"], bn["surv_m"])
    fy = fund_block(bb["fund"], bb["pit"])
    # цена для OI в USD: закрытие Bybit, при пропуске — Binance того же символа (у перезапущенных
    # перпов Bybit, напр. SOLUSDT, история OI начинается раньше свечей)
    px = {s: {**bn["close"].get(s, {}), **c} for s, c in bb["close"].items()}
    for s in bb["oi"]:
        px.setdefault(s, dict(bn["close"].get(s, {})))
    no_px = sum(1 for s, o in bb["oi"].items() for d in o if (d - DAY) not in bb["close"].get(s, {}))
    no_px2 = sum(1 for s, o in bb["oi"].items() for d in o if (d - DAY) not in px.get(s, {}))
    print(f"[bybit] дней OI без закрытия Bybit: {no_px}, после подстановки Binance: {no_px2}")
    ob = oi_block(bb["oi"], px, bb["pit"], alt_ex)
    osv = oi_block(bb["oi"], px, bb["surv_m"], alt_ex)
    alt = {"alt_fund30": fb["med"], "alt_fund_hi": fb["hi"], "alt_fund30_dm": fb["dm"],
           "alt_fund30_surv": fs["med"], "alt_fund30_bybit": fy["med"],
           "alt_oi_rel": ob["rel"], "alt_oi_mcap": ob["mcap"], "alt_oi_rel_med": ob["med"],
           "alt_oi_sum_rel": ob["sum_rel"], "alt_oi_rel_surv": osv["rel"]}
    for k, s in alt.items():
        for d, v in s.items():
            if d in feats and d <= last:
                feats[d][k] = v
    cover = {k: {"from": _d(min(s)) if s else None, "to": _d(max(s)) if s else None, "days": len(s)}
             for k, s in alt.items()}
    print("\nпокрытие признаков: " + "; ".join(f"{k} {v['from']}…{v['to']} ({v['days']} дн)"
                                               for k, v in cover.items()))
    n_hist = {"binance_fund_basket": {y: _r(statistics.fmean(v for d, v in fb["n"].items()
                                                             if _d(d)[:4] == y), 1)
                                      for y in sorted({_d(d)[:4] for d in fb["n"]})},
              "bybit_oi_basket": {y: _r(statistics.fmean(v for d, v in ob["n"].items()
                                                         if _d(d)[:4] == y), 1)
                                  for y in sorted({_d(d)[:4] for d in ob["n"]})}}
    print(f"средний размер корзины по годам: фандинг Binance {n_hist['binance_fund_basket']}; "
          f"OI Bybit {n_hist['bybit_oi_basket']}")

    # --- априорные пороги: доля дней, когда горит 9-й флаг, как у текущих флагов (медиана)
    span = (1609459200, last)                      # 2021-01-01 … — все признаки уже есть
    shares = {}
    for name, spec in cfg["market_regime"]["hot_flags"].items():
        shares[name] = _r(lit_share(feats, name, spec[0], spec[1], *span))
    target = statistics.median(v for v in shares.values() if v is not None)
    apriori, qthr = {}, {}
    for k, (_, grid) in FEATS.items():
        xs = [f[k] for d, f in feats.items() if span[0] <= d < span[1]
              and isinstance(f.get(k), (int, float))]
        qthr[k] = quantile(xs, 1 - target)
        apriori[k] = min(grid, key=lambda t: abs(t - qthr[k]))
    APRIORI.update(apriori)
    qshow = {k: _r(v, 4) for k, v in qthr.items()}
    print(f"доля дней «флаг горит» (с 2021): {shares}; медиана {target:.2f} → квантильные пороги "
          f"{qshow} → ближайшие в сетке {apriori}")

    hot8 = {d: regime.hot_flags(f, cfg) for d, f in feats.items()}
    cfg9 = {k: cfg_with(cfg, (k, [">=", apriori[k], round(apriori[k] * 0.8, 4)])) for k in FEATS}
    hot9 = {k: {d: regime.hot_flags(f, c) for d, f in feats.items()} for k, c in cfg9.items()}
    hot9n = {k: same_denominator(hot8, h) for k, h in hot9.items()}

    mp = map_block(feats, alt_ex, last, apriori)
    mp["overlap"] = overlap_block(feats, hot8)

    # --- пружины
    coins = load_coins(args.offline)
    btc_dd = btc_dd_by_day()
    eps = build_episodes(coins, feats, hot8, btc_dd)
    for e in eps:
        f = feats.get(e["day"] - DAY, {})
        e["alt"] = {k: f.get(k) for k in alt}
        e["hot8"] = e["cand"].market_hot_score
        for k in FEATS:
            h = hot9[k].get(e["day"] - DAY, {})
            e[f"hot9_{k}"] = h.get("score")
            e[f"cand9_{k}"] = dataclasses.replace(e["cand"], market_hot_score=h.get("score"),
                                                  market_hot_lit=list(h.get("lit") or []))
    print(f"\nмонет {len(coins)}, пружин {len(eps)} (market_regime_study 29.09: 161 / 1175)")
    ex = exit_block(cfg, eps, coins, hot8, hot9, hot9n)
    full = [e for e in eps if e["fwd"] >= 365]
    for e in full:
        e["net_a"] = e["net"]["A"]
        e["q8"] = spring_quality(e["cand"], cfg)[0]
        for k in FEATS:
            e[f"q9_{k}"] = spring_quality(e[f"cand9_{k}"], cfg)[0]
    en = entry_block(eps, apriori)
    nine = nine_flag_block(cfg, eps, feats, apriori)
    nine["swap_oi"] = swap_block(cfg, eps, feats, apriori)
    sig = market_signal_block(cfg, feats, alt_ex, hot8, hot9, hot9n, last)
    cur = current_block(feats, last, apriori, cfg)
    cur["hot9"] = {k: hot9[k].get(last) for k in FEATS}

    OUT.write_text(json.dumps({
        "generated": time.strftime("%Y-%m-%d"), "asof_last_day": _d(last),
        "params": {"top_n": TOP_N, "vol_win": VOL_WIN, "min_life": MIN_LIFE, "fund_win": FUND_WIN,
                   "fund_hi": FUND_HI, "min_perps": MIN_PERPS, "oi_win": OI_WIN},
        "universe": {"binance_alt_perps": len(bn["uni"]),
                     "binance_delisted": sum(u["status"] != "TRADING" for u in bn["uni"].values()),
                     "binance_basket_ever": len(member_windows(bn["pit"])),
                     "binance_with_funding": len(bn["fund"]),
                     "bybit_alt_perps": len(bb["uni"]),
                     "bybit_delisted_found": sum(u["status"] == "GONE" for u in bb["uni"].values()),
                     "bybit_basket_ever": len(member_windows(bb["pit"])),
                     "bybit_with_oi": len(bb["oi"]), "bybit_with_funding": len(bb["fund"]),
                     "survivor_binance": sorted(bn["surv"]), "survivor_bybit": sorted(bb["surv"]),
                     "basket_size_by_year": n_hist},
        "coverage": cover, "flag_lit_share_since_2021": shares, "apriori_thresholds": apriori,
        "quantile_thresholds": {k: _r(v, 4) for k, v in qthr.items()},
        "coins": len(coins), "springs": len(eps),
        "map": mp, "entry": en, "nine_flags": nine, "exit": ex, "market_signal": sig,
        "current": cur}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n→ {OUT.name} ({time.time() - t0:.0f} с; сеть {net.fetched}, кэш {net.cached})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
