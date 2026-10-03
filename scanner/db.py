"""SQLite-хранилище результатов прогона. Схема — расширяемая под Stage 3+."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Iterable

from .models import Candidate

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL,
    n_ingested INTEGER,
    n_stage1   INTEGER,
    n_watchlist INTEGER,
    cfg_version TEXT
);
CREATE TABLE IF NOT EXISTS candidates (
    run_id      INTEGER,
    symbol      TEXT,
    name        TEXT,
    coin_id     TEXT,
    chain       TEXT,
    address     TEXT,
    track       TEXT,
    stage       TEXT,
    market_cap  REAL,
    fdv         REAL,
    volume_24h  REAL,
    liquidity_usd REAL,
    drawdown_from_ath_pct REAL,
    age_days    REAL,
    spring_prefilter INTEGER,
    manual_review INTEGER,
    tvl         REAL,
    mc_tvl      REAL,
    fdv_mc      REAL,
    category    TEXT,
    val_notes   TEXT,
    zone        TEXT,
    rf_venue    TEXT,
    score       REAL,
    confidence  REAL,
    flags       TEXT,
    reject_reasons TEXT,
    security    TEXT,
    market_dd   REAL,
    spring_quality REAL,
    funding_rate REAL,
    onchain_score REAL,
    net_flow_usd_7d REAL,
    holders_change_pct_7d REAL,
    PRIMARY KEY (run_id, chain, address, symbol, coin_id)
);
CREATE INDEX IF NOT EXISTS idx_candidates_run ON candidates(run_id, stage);
CREATE TABLE IF NOT EXISTS candidate_metrics (
    run_id        INTEGER,
    ts            REAL,
    coin_id       TEXT,
    symbol        TEXT,
    liquidity_usd REAL,
    volume_24h    REAL,
    holder_count  INTEGER,
    dev_commits_4w INTEGER,
    n_exchanges   INTEGER,
    liveness_score REAL,
    score         REAL
);
CREATE INDEX IF NOT EXISTS idx_metrics_coin ON candidate_metrics(coin_id, ts);
CREATE TABLE IF NOT EXISTS alert_log (
    symbol TEXT,
    ts     REAL,
    score  REAL
);
CREATE INDEX IF NOT EXISTS idx_alert_sym ON alert_log(symbol, ts);
CREATE TABLE IF NOT EXISTS market_daily (
    day           INTEGER PRIMARY KEY,   -- unix-секунды 00:00 UTC
    total_mcap    REAL,                  -- вся капа (CMC; при сбое — CoinGecko × поправка)
    btc_dominance REAL,                  -- BTC.D, %
    cg_total_mcap REAL,                  -- CoinGecko /global — для поправки CMC↔CG
    cg_btc_dominance REAL,
    stables_usd   REAL,                  -- предложение стейблов: max(DeFiLlama, USDT+USDC CoinMetrics)
    mvrv_btc      REAL,
    mvrv_eth      REAL,
    fng           REAL,                  -- Fear & Greed
    funding_btc   REAL,                  -- средний фандинг BTCUSDT Bybit за день, доля/8ч
    oi_btc        REAL,                  -- open interest BTCUSDT Bybit, BTC
    breadth200    REAL,                  -- % альтов (топ Bybit spot) выше своей SMA200
    total_source  TEXT,                  -- cmc | coingecko
    updated_ts    REAL
);
"""

# Колонки, добавленные после первого релиза (для миграции старых БД).
_MIGRATIONS = [
    ("tvl", "REAL"), ("mc_tvl", "REAL"), ("fdv_mc", "REAL"),
    ("category", "TEXT"), ("val_notes", "TEXT"),
    ("zone", "TEXT"), ("rf_venue", "TEXT"),
    ("score", "REAL"), ("confidence", "REAL"),
    # Stage 4b/4c: контекст рынка, качество пружины, фандинг, on-chain (Dune)
    ("market_dd", "REAL"), ("spring_quality", "REAL"), ("funding_rate", "REAL"),
    ("onchain_score", "REAL"), ("net_flow_usd_7d", "REAL"),
    ("holders_change_pct_7d", "REAL"),
]


class Store:
    def __init__(self, path: str):
        # timeout: параллельный watch/pos-команды не должны падать на locked db
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(candidates)")}
        for name, typ in _MIGRATIONS:
            if name not in cols:
                self.conn.execute(f"ALTER TABLE candidates ADD COLUMN {name} {typ}")
        rcols = {r[1] for r in self.conn.execute("PRAGMA table_info(runs)")}
        if "cfg_version" not in rcols:
            self.conn.execute("ALTER TABLE runs ADD COLUMN cfg_version TEXT")
        # Сводка дня (run.py brief) по ним понимает, дошёл ли скан до конца и что было
        # недоступно — без сети и без парсинга логов.
        if "finished_ts" not in rcols:
            self.conn.execute("ALTER TABLE runs ADD COLUMN finished_ts REAL")
        if "summary" not in rcols:
            self.conn.execute("ALTER TABLE runs ADD COLUMN summary TEXT")

    def new_run(self, cfg_version: str = "") -> int:
        cur = self.conn.execute("INSERT INTO runs(ts, cfg_version) VALUES (?,?)",
                                (time.time(), cfg_version))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, n_ingested: int, n_stage1: int, n_watchlist: int,
                   summary: dict | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET n_ingested=?, n_stage1=?, n_watchlist=?, finished_ts=?, summary=? "
            "WHERE id=?",
            (n_ingested, n_stage1, n_watchlist, time.time(),
             json.dumps(summary or {}, ensure_ascii=False, default=str), run_id),
        )
        self.conn.commit()

    def last_run(self) -> dict | None:
        """Последний прогон: {id, ts, finished_ts, n_watchlist, summary (dict)}."""
        r = self.conn.execute("SELECT id, ts, finished_ts, n_watchlist, summary FROM runs "
                              "ORDER BY id DESC LIMIT 1").fetchone()
        if not r:
            return None
        try:
            summ = json.loads(r[4]) if r[4] else {}
        except ValueError:
            summ = {}
        return {"id": r[0], "ts": r[1], "finished_ts": r[2], "n_watchlist": r[3],
                "summary": summ}

    def alerts_since(self, ts: float) -> dict[str, float]:
        """symbol -> score карточек, ушедших с момента ts (для сводки дня)."""
        cur = self.conn.execute(
            "SELECT symbol, MAX(score) FROM alert_log WHERE ts>=? GROUP BY symbol", (ts,))
        return {r[0]: (r[1] or 0.0) for r in cur.fetchall()}

    def save_candidates(self, run_id: int, candidates: Iterable[Candidate]) -> None:
        rows = []
        for c in candidates:
            rows.append((
                run_id, c.symbol, c.name, c.coin_id, c.chain, c.address, c.track, c.stage,
                c.market_cap, c.fdv, c.volume_24h, c.liquidity_usd,
                c.drawdown_from_ath_pct, c.age_days,
                int(c.spring_prefilter), int(c.manual_review),
                c.tvl, c.mc_tvl, c.fdv_mc, c.category,
                json.dumps(c.val_notes, ensure_ascii=False),
                c.zone, c.rf_venue, c.score, c.confidence,
                json.dumps(c.flags), json.dumps(c.reject_reasons, ensure_ascii=False),
                json.dumps(c.security),
                c.market_dd, c.spring_quality, c.funding_rate,
                c.onchain_score, c.net_flow_usd_7d, c.holders_change_pct_7d,
            ))
        self.conn.executemany(
            """INSERT OR REPLACE INTO candidates(
                   run_id, symbol, name, coin_id, chain, address, track, stage,
                   market_cap, fdv, volume_24h, liquidity_usd,
                   drawdown_from_ath_pct, age_days, spring_prefilter, manual_review,
                   tvl, mc_tvl, fdv_mc, category, val_notes, zone, rf_venue,
                   score, confidence, flags, reject_reasons, security,
                   market_dd, spring_quality, funding_rate,
                   onchain_score, net_flow_usd_7d, holders_change_pct_7d)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                       ?,?,?,?,?,?)""", rows,
        )
        self.conn.commit()

    def recent_alerts(self, days: float) -> dict[str, float]:
        """symbol -> макс. score за последние `days` (для mute повторных алертов)."""
        cutoff = time.time() - days * 86400
        cur = self.conn.execute(
            "SELECT symbol, MAX(score) FROM alert_log WHERE ts>=? GROUP BY symbol", (cutoff,))
        return {r[0]: (r[1] or 0.0) for r in cur.fetchall()}

    def record_alert(self, symbol: str, score: float) -> None:
        self.conn.execute("INSERT INTO alert_log(symbol, ts, score) VALUES (?,?,?)",
                          (symbol, time.time(), score))
        self.conn.commit()

    def save_metrics(self, run_id: int, candidates: Iterable[Candidate]) -> None:
        """Снапшот метрик watchlist-кандидатов — для трендов (живость/ликвидность
        со временем предсказывают выживание лучше разового замера)."""
        rows = [(run_id, time.time(), c.coin_id, c.symbol, c.liquidity_usd,
                 c.volume_24h, c.holder_count, c.dev_commits_4w, c.n_exchanges,
                 c.liveness_score, c.score) for c in candidates if c.coin_id]
        if not rows:
            return
        self.conn.executemany(
            "INSERT INTO candidate_metrics(run_id, ts, coin_id, symbol, liquidity_usd,"
            " volume_24h, holder_count, dev_commits_4w, n_exchanges, liveness_score, score)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    # --- рыночный контекст (market_regime) ---
    MARKET_COLS = ("total_mcap", "btc_dominance", "cg_total_mcap", "cg_btc_dominance",
                   "stables_usd", "mvrv_btc", "mvrv_eth", "fng", "funding_btc", "oi_btc",
                   "breadth200", "total_source")

    def upsert_market(self, rows: dict[int, dict]) -> int:
        """{day: {col: value}} -> market_daily. None НЕ затирает уже записанное
        (каждый источник обновляет только свои поля). Возвращает число дней."""
        n = 0
        now = time.time()
        for day, vals in rows.items():
            cols = [c for c in self.MARKET_COLS if vals.get(c) is not None]
            if not cols:
                continue
            placeholders = ",".join("?" for _ in cols)
            updates = ",".join(f"{c}=excluded.{c}" for c in cols)
            self.conn.execute(
                f"INSERT INTO market_daily(day,{','.join(cols)},updated_ts) "
                f"VALUES (?,{placeholders},?) "
                f"ON CONFLICT(day) DO UPDATE SET {updates}, updated_ts=excluded.updated_ts",
                (int(day), *[vals[c] for c in cols], now))
            n += 1
        self.conn.commit()
        return n

    def market_rows(self, since_day: int = 0) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM market_daily WHERE day>=? ORDER BY day", (since_day,))
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def market_stats(self) -> dict:
        """{days_alt, last_day, last_update} — для решения «нужен ли бэкфилл/обновление»."""
        r = self.conn.execute(
            "SELECT COUNT(*), MAX(day), MAX(updated_ts) FROM market_daily "
            "WHERE total_mcap IS NOT NULL AND stables_usd IS NOT NULL").fetchone()
        return {"days_alt": r[0] or 0, "last_day": r[1], "last_update": r[2]}

    def close(self) -> None:
        self.conn.close()
