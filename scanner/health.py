"""Здоровье источников данных: что отвечало, что сбоило и насколько опаздывают закрытия.

После каждого шага прогона (run.py main → record) счётчики всех HttpClient процесса
(scanner/http.py: запросы, сбои после повторов, коды 429/404/5xx, сеть, retCode Bybit,
секунды ожидания повторов) и отставание дневных закрытий (closes.load в watch, графики скана)
пишутся в таблицу source_health. Недельная и месячная сводки показывают итог за период и тренд
по неделям: один плохой день — шум, растущая доля сбоев — повод разбираться.

Хосты сведены к источникам (SOURCE_NAMES): у DeFiLlama и CoinGecko их несколько.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DAY = 86400

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_health (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,          -- конец шага
    step    TEXT NOT NULL,          -- scan | watch | execute | market | sync | ...
    kind    TEXT NOT NULL,          -- http — запросы к хосту; lag — отставание закрытий
    source  TEXT NOT NULL,          -- http: хост; lag: источник закрытий (CoinGecko, Bybit)
    req     INTEGER DEFAULT 0,      -- http: запросов в сеть; lag: рядов (позиций, графиков)
    ok      INTEGER DEFAULT 0,
    fail    INTEGER DEFAULT 0,      -- http: не ответил после повторов; lag: рядов с lag ≥ 1
    cache   INTEGER DEFAULT 0,
    codes   TEXT DEFAULT '',        -- JSON {код: попыток}: 429, 404, net, ret10006…
    wait_s  REAL DEFAULT 0,         -- пауз перед повторами, сек.
    lag_max INTEGER                 -- lag: наибольшее отставание, дней
);
CREATE INDEX IF NOT EXISTS idx_source_health_ts ON source_health(ts);
"""

SOURCE_NAMES = {
    "api.coingecko.com": "CoinGecko", "pro-api.coingecko.com": "CoinGecko",
    "api.bybit.com": "Bybit", "api-demo.bybit.com": "Bybit demo",
    "api.llama.fi": "DeFiLlama", "stablecoins.llama.fi": "DeFiLlama",
    "coins.llama.fi": "DeFiLlama", "yields.llama.fi": "DeFiLlama",
    "community-api.coinmetrics.io": "CoinMetrics",
    "api.github.com": "GitHub", "github.com": "GitHub",
    "api.gopluslabs.io": "GoPlus", "api.honeypot.is": "honeypot.is",
    "api.alternative.me": "F&G", "api.dexscreener.com": "DexScreener",
}


def source_name(host: str) -> str:
    return SOURCE_NAMES.get(host, host)


# ---------------------------------------------------------------- запись

def collect(clients) -> list[dict]:
    """Строки для записи из клиентов процесса: http — по хосту (сумма по клиентам),
    lag — по источнику закрытий. Хост без запросов и без кэша не пишется."""
    hosts: dict[str, dict] = {}
    lags: dict[str, list[int]] = {}
    for c in list(clients):
        for host, st in (getattr(c, "stats", None) or {}).items():
            h = hosts.setdefault(host, {"req": 0, "ok": 0, "fail": 0, "cache": 0, "codes": {},
                                        "wait_s": 0.0})
            for k in ("req", "ok", "fail", "cache", "wait_s"):
                h[k] += st.get(k, 0)
            for code, n in (st.get("codes") or {}).items():
                h["codes"][code] = h["codes"].get(code, 0) + n
        for src, xs in (getattr(c, "lags", None) or {}).items():
            lags.setdefault(src, []).extend(xs)
    rows = [{"kind": "http", "source": host, **h} for host, h in sorted(hosts.items())
            if h["req"] or h["cache"]]
    rows += [{"kind": "lag", "source": src, "req": len(xs), "ok": sum(1 for x in xs if x < 1),
              "fail": sum(1 for x in xs if x >= 1), "cache": 0, "codes": {}, "wait_s": 0.0,
              "lag_max": max(xs)} for src, xs in sorted(lags.items()) if xs]
    return rows


def record(db_path, step: str, clients=None, now: float | None = None) -> int:
    """Записать счётчики шага в source_health; сколько строк записано. Нечего — 0 (базу не
    трогаем). Сбой записи — исключение: run.py ловит и пишет в лог, шаг от этого не падает."""
    if clients is None:
        from .http import CLIENTS
        clients = CLIENTS
    rows = collect(clients)
    if not rows:
        return 0
    ts = now if now is not None else time.time()
    con = sqlite3.connect(str(db_path), timeout=30)
    try:
        con.executescript(SCHEMA)
        con.executemany(
            "INSERT INTO source_health(ts, step, kind, source, req, ok, fail, cache, codes, "
            "wait_s, lag_max) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(ts, step, r["kind"], r["source"], r["req"], r["ok"], r["fail"], r["cache"],
              json.dumps(r["codes"], sort_keys=True) if r["codes"] else "",
              round(r["wait_s"], 1), r.get("lag_max")) for r in rows])
        con.commit()
    finally:
        con.close()
    for c in list(clients):          # второй record того же процесса не задвоит
        if hasattr(c, "stats"):
            c.stats = {}
        if hasattr(c, "lags"):
            c.lags = {}
    return len(rows)


# ---------------------------------------------------------------- чтение и сводка

def load(db_path, t0: float, t1: float) -> list[dict] | None:
    """Строки source_health за [t0, t1) (только чтение); нет файла или таблицы — None."""
    p = Path(db_path)
    if not p.is_file():
        return None
    con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                           "name='source_health'").fetchone():
            return None
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM source_health WHERE ts>=? AND ts<? ORDER BY ts", (t0, t1))]
    finally:
        con.close()
    for r in rows:
        try:
            r["codes"] = json.loads(r["codes"]) if r["codes"] else {}
        except ValueError:
            r["codes"] = {}
    return rows


def _bucket(ts: float, t0: float, bucket_days: int | None, n: int) -> int:
    if not bucket_days:
        return 0
    return min(int((ts - t0) // (bucket_days * DAY)), n - 1)


def summarize(rows: list[dict], t0: float, t1: float,
              bucket_days: int | None = 7) -> dict[str, Any]:
    """Итог по источникам за [t0, t1) и по корзинам bucket_days (None — без тренда).

    -> {"http": [{source, req, ok, fail, cache, codes, wait_s, buckets: [{req, fail, n429}]}]
        — сначала источники со сбоями и кодами, "lag": [{source, step, n, late, runs, late_runs,
        lag_max, buckets: [{n, late}]}], "buckets": число корзин, "runs": прогонов (дней с
        записями)}. Прогон шага — одна запись (ts); late_runs — прогоны, где хоть один ряд
        опоздал."""
    nb = max(1, -(-int(t1 - t0) // (bucket_days * DAY))) if bucket_days else 1
    http: dict[str, dict] = {}
    lag: dict[str, dict] = {}
    days = set()
    for r in rows:
        days.add(int(r["ts"] // DAY))
        b = _bucket(r["ts"], t0, bucket_days, nb)
        if r["kind"] == "http":
            name = source_name(r["source"])
            h = http.setdefault(name, {"source": name, "req": 0, "ok": 0, "fail": 0, "cache": 0,
                                       "codes": {}, "wait_s": 0.0,
                                       "buckets": [{"req": 0, "fail": 0, "n429": 0}
                                                   for _ in range(nb)]})
            for k in ("req", "ok", "fail", "cache", "wait_s"):
                h[k] += r[k] or 0
            for code, n in r["codes"].items():
                h["codes"][code] = h["codes"].get(code, 0) + n
            hb = h["buckets"][b]
            hb["req"] += r["req"] or 0
            hb["fail"] += r["fail"] or 0
            hb["n429"] += r["codes"].get("429", 0)
        elif r["kind"] == "lag":
            g = lag.setdefault((r["source"], r["step"]), {
                                             "source": r["source"], "step": r["step"],
                                             "n": 0, "late": 0,
                                             "runs": set(), "late_runs": set(), "lag_max": 0,
                                             "buckets": [{"n": 0, "late": 0}
                                                         for _ in range(nb)]})
            g["n"] += r["req"] or 0
            g["late"] += r["fail"] or 0
            g["runs"].add(r["ts"])
            if r["fail"]:
                g["late_runs"].add(r["ts"])
            g["lag_max"] = max(g["lag_max"], r["lag_max"] or 0)
            g["buckets"][b]["n"] += r["req"] or 0
            g["buckets"][b]["late"] += r["fail"] or 0
    for g in lag.values():
        g["runs"], g["late_runs"] = len(g["runs"]), len(g["late_runs"])
    hs = sorted(http.values(), key=lambda h: (-(h["fail"] + sum(h["codes"].values()) > 0),
                                              -h["fail"], -h["req"], h["source"]))
    return {"http": hs, "lag": sorted(lag.values(), key=lambda g: (g["source"], g["step"])),
            "buckets": nb if bucket_days else 0, "runs": len(days)}
