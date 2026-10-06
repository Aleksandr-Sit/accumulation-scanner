"""Место монеты по обороту среди USDT-пар Bybit spot — для фильтров пробного исполнителя.

Замер 06.10.2026 (backtest/junk_filter_study.py, 3222 пружины Binance 2018–2026 с умершими):
нижняя четверть по 30-дн. обороту — 42% монет умерли за 2 года, медиана −16%, её отсев почти
бесплатен; квота «вне топ-150 ≤ 5 из 15 мест» — покупок столько же, умерших на ~30% меньше.
Замер — на Binance (~400 пар), поэтому на Bybit порог переносится долей: место / число пар.

Подсчёт — как vol_ranks в backtest/quality_screen.py: instruments-info spot, котировка USDT,
статус Trading; без стейблов, фиата и обёрток (NON_ALTS), плечевых токенов и токенизированных
акций (symbolType xstocks — не монеты, в замере на Binance их не было); kline D limit 31, живая
свеча отброшена, оборот — поле [6] (turnover, USDT); среднее за закрытые свечи, не меньше 20.
Место 1 — самый большой оборот.

Раз в UTC-день (дневные свечи Bybit — по UTC) таблица vol_ranks заполняется тем, кому место
понадобилось первым (карточка в scan --notify или execute), остальные читают её: карточка,
исполнитель и сводки видят одно и то же. Сбой — пусто и «нет данных оборота»: ничего не
отсекается. Без карточек сеть не трогается (~390 запросов к Bybit: подряд — 4 мин с ноутбука,
в WORKERS потоков — меньше минуты; лимит Bybit — 600 запросов за 5 с).
"""
from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

DAY = 86400
_BASE = "https://api.bybit.com"
WINDOW = 30                      # закрытых дневных свечей в среднем
MIN_DAYS = 20                    # меньше — пара не ранжируется (свежий листинг)
MIN_PAIRS = 100                  # меньше пар — сбой источника, а не рынок: не сохраняем
MAX_FAILED_SHARE = 0.10          # не ответило больше 10% пар — сбой, не сохраняем
WORKERS = 6                      # параллельных запросов свечей
LEVERAGED = ("2L", "3L", "5L", "2S", "3S", "5S")
# Стейблы, фиат и обёртки — копия backtest/binance_archive.NON_ALTS (модуль сканера не
# импортирует backtest).
NON_ALTS = {
    "USDC", "BUSD", "TUSD", "USDP", "PAX", "DAI", "FDUSD", "UST", "USTC", "SUSD",
    "USDS", "USDSB", "BFUSD", "USD1", "RLUSD", "XUSD", "USDE", "PYUSD", "EURC", "U",
    "EUR", "GBP", "AUD", "AEUR", "EURI", "BRL", "TRY", "RUB", "UAH", "NGN", "ZAR",
    "BKRW", "IDRT", "BIDR", "BVND", "PAXG", "XAUT", "WBTC", "WBETH", "BETH", "WETH",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vol_ranks (
    day      INTEGER NOT NULL,   -- UTC-день подсчёта (00:00): свечи по вчерашнюю включительно
    symbol   TEXT NOT NULL,      -- baseCoin: LINK
    turnover REAL,               -- средний дневной оборот, USDT
    rank     INTEGER,            -- 1 — самый большой оборот
    n_pairs  INTEGER,            -- пар в ранжировании этого дня
    PRIMARY KEY (day, symbol)
);
"""


def utc_day(ts: float) -> int:
    return int(ts // DAY) * DAY


# ---------------------------------------------------------------- чистые функции

def universe(instruments: Any) -> list[tuple[str, str]]:
    """Ответ instruments-info spot -> [(пара, монета)] ранжируемых USDT-пар."""
    rows = (((instruments or {}).get("result") or {}).get("list") or []
            if isinstance(instruments, dict) else [])
    out = []
    for x in rows:
        base = (x.get("baseCoin") or "").upper()
        if (x.get("quoteCoin") != "USDT" or x.get("status") != "Trading" or not base
                or base in NON_ALTS or base.endswith(LEVERAGED)
                or x.get("symbolType") == "xstocks"):
            continue
        out.append((x.get("symbol") or f"{base}USDT", base))
    return out


def avg_turnover(kline: Any, now: float) -> float | None:
    """Ответ kline D -> средний оборот (USDT) за последние WINDOW закрытых свечей; живая
    свеча (start + сутки > now) отброшена. Меньше MIN_DAYS свечей — None."""
    rows = (((kline or {}).get("result") or {}).get("list") or []
            if isinstance(kline, dict) else [])
    vals = []
    for r in rows:                       # Bybit отдаёт newest-first
        try:
            if int(r[0]) / 1000 + DAY > now:
                continue
            vals.append(float(r[6]))
        except (ValueError, IndexError, TypeError):
            continue
    vals = vals[:WINDOW]
    return sum(vals) / len(vals) if len(vals) >= MIN_DAYS else None


def rank_table(turnover: dict[str, float]) -> dict[str, dict]:
    """{монета: оборот} -> {монета: {rank, n, share, usd}}; share = место / число пар."""
    order = sorted(turnover.items(), key=lambda kv: (-kv[1], kv[0]))
    n = len(order)
    return {sym: {"rank": i, "n": n, "share": i / n, "usd": usd}
            for i, (sym, usd) in enumerate(order, 1)}


def lookup(ranks: dict | None, symbol: str) -> dict | None:
    """Место монеты {rank, n, share, usd} или None (нет данных оборота / пары нет)."""
    return ((ranks or {}).get("by_sym") or {}).get((symbol or "").upper())


# ---------------------------------------------------------------- сеть и БД

def fetch_turnover(http, now: float | None = None) -> tuple[dict[str, float], dict]:
    """Сеть: средний оборот всех ранжируемых пар -> ({монета: оборот}, {pairs, failed, short})."""
    now = now if now is not None else time.time()
    inst = http.get_json(f"{_BASE}/v5/market/instruments-info",
                         params={"category": "spot", "limit": 1000}, use_cache=False)
    pairs = universe(inst)

    def kline(pair: str):
        return http.get_json(f"{_BASE}/v5/market/kline",
                             params={"category": "spot", "symbol": pair, "interval": "D",
                                     "limit": WINDOW + 1}, use_cache=False)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        got = list(pool.map(kline, [p for p, _ in pairs]))
    out: dict[str, float] = {}
    failed = short = 0
    for (_pair, base), k in zip(pairs, got):
        if not isinstance(k, dict) or k.get("retCode") not in (0, None):
            failed += 1
            continue
        v = avg_turnover(k, now)
        if v is None:
            short += 1
        else:
            out[base] = v
    return out, {"pairs": len(pairs), "failed": failed, "short": short}


def _connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30)
    con.executescript(_SCHEMA)
    return con


def load_ranks(db_path: str, now: float | None = None) -> dict:
    """Места за UTC-день now из БД, без сети: {ok, day, n, by_sym, note}."""
    day = utc_day(now if now is not None else time.time())
    con = _connect(db_path)
    try:
        rows = con.execute("SELECT symbol, turnover, rank, n_pairs FROM vol_ranks WHERE day=?",
                           (day,)).fetchall()
    finally:
        con.close()
    if not rows:
        return {"ok": False, "day": day, "n": 0, "by_sym": {}, "note": "нет данных оборота"}
    n = rows[0][3]
    return {"ok": True, "day": day, "n": n, "note": "",
            "by_sym": {s: {"rank": r, "n": k, "share": r / k, "usd": t}
                       for s, t, r, k in rows}}


def ensure_ranks(db_path: str, http, now: float | None = None) -> dict:
    """Места за сегодня (UTC): из БД, а если их ещё нет — подсчёт по сети и запись. Сбой
    (сеть, мало пар, много неответов) не бросает: {ok: False, note} — фильтр не отсекает."""
    now = now if now is not None else time.time()
    got = load_ranks(db_path, now)
    if got["ok"]:
        return got
    try:
        turnover, st = fetch_turnover(http, now)
    except Exception as e:  # noqa: BLE001 — сбой подсчёта не роняет скан и исполнитель
        return {**got, "note": f"нет данных оборота: {type(e).__name__}: {e}"}
    if (len(turnover) < MIN_PAIRS or not st["pairs"]
            or st["failed"] > MAX_FAILED_SHARE * st["pairs"]):
        return {**got, "note": f"нет данных оборота: Bybit ответил по {len(turnover)} из "
                               f"{st['pairs']} пар (не ответили {st['failed']})"}
    table = rank_table(turnover)
    day = utc_day(now)
    con = _connect(db_path)
    try:
        con.executemany("INSERT OR REPLACE INTO vol_ranks(day, symbol, turnover, rank, n_pairs) "
                        "VALUES (?,?,?,?,?)",
                        [(day, s, r["usd"], r["rank"], r["n"]) for s, r in table.items()])
        con.commit()
    finally:
        con.close()
    return {"ok": True, "day": day, "n": len(table), "by_sym": table,
            "note": f"посчитано: {len(table)} пар, свежих листингов без 20 дней {st['short']}"}
