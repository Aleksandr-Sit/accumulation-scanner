"""Шаг 2 автоматизации: пробный исполнитель (dry-run) — что поставил бы, без единого ордера.

Зачем: прежде чем доверять деньги автомату (шаг 3 — демо-счёт, шаг 4 — живой счёт с
потолками), проверить на живом рынке механику исполнения и собрать данные о двух выходах:
  R «правила» — stage8_exit целиком (evaluate_exit: стоп, +50/+150 по трети, трейл);
  H «держать» — без частичных продаж и трейла, только аварийный стоп (тот же evaluate_exit,
     из сигналов берётся только инвалидация; ширина — executor.books.H.stop_pct).
Выбор стопа H — backtest/hold_stop_study.py (06.10.2026: −25%, как у R: шире — медиана не
лучше, хвост тяжелее). Книга v1 отстала от «держать те же монеты» на 9 п.п. из-за выходов,
а на истории «держать» хуже правил по медиане — книга H нужна как живые данные.

Правила:
  • вход — монеты, пришедшие сегодня карточкой (alert_log ∩ watchlist.json); лестница —
    ladder.plan_ladder (5 × $10: ступень 1 рынком, 2–5 лимитками до 1.05 × пола стопа), шаги
    цены и количества и минимумы — из /v5/market/instruments-info; ST и не Trading — отказ;
  • книги получают монету парно (обе или ни одна): разница R−H — только выходы;
  • проверки на входе (отказ пишется в журнал с причиной): лимит монет в книге, свободный USDT
    виртуального счёта книги (capital_usdt + выручка − траты − резерв лимиток), risk_check
    (лимит позиции и worst-case просадка по капиталу книги). Реальный баланс USDT (ключ
    Read-Only) — только справкой в лог: «для реального режима хватило бы / нет»;
  • журнал «поставил бы»: dry_orders с детерминированным orderLinkId ≤ 36 символов
    (dry-<книга><позиция>-B<n> покупки, dry-<книга><позиция>-S-<L0|L1|TR|INV> продажи);
    позиция уникальна по (книга, пара, день карточки) — повтор запуска ничего не дублирует;
  • исполнение: ступень 1 — по цене запуска, комиссия + спред 0.15% (в монете); лимитка —
    если low часовой свечи, начавшейся после постановки, СТРОГО ниже цены (касание не
    гарантирует место в очереди; fill_on_touch — касание), по цене лимитки, 0.1%; лимитки
    живут buy_valid_days (как в ladder_dca_study), потом снимаются;
  • выходы — по закрытым дневным свечам Bybit, после исполнений того же дня: сигнал
    исполняется по закрытию, 0.15%; доли лестницы — от всего купленного (как paper в watch);
    остаток дешевле минимума биржи продаётся вместе с долей; полный выход снимает лимитки;
  • P&L = выручка + остаток × последнее закрытие × (1 − 0.15%) − потрачено.
Ордеров на биржу модуль не отправляет: в нём нет ни одного POST и ни одного приватного вызова.

Отсев перед покупкой (замер 06.10.2026, backtest/junk_filter_study.py: 3222 пружины Binance
2018–2026 с умершими; строгие гейты качества режут 63–89% покупок и до 88% лучших исходов,
поэтому режется только то, что почти бесплатно):
  1. нижняя четверть по 30-дн. обороту среди USDT-пар Bybit (scanner/liquidity.py; 42% таких
     монет умерли за 2 года, медиана −16%) — не покупать;
  2. квота: монеты вне топ-~150 по обороту (доля illiquid_share от числа пар) — не больше
     illiquid_max_slots мест книги; считается по метке позиции (место на день входа), карточки
     дня идут по месту — ликвидные первыми;
  4. перегрев: горит ≥ hot_min_lit флагов market_regime.hot_flags («близко» не в счёт; без OI,
     R 2023-26: медиана −21% против +10% в холодном рынке) — новых лестниц нет; данных рынка нет или они старше
     market_max_age_days — тоже нет (вслепую не покупаем).
Фильтры действуют только на новые входы: открытые позиции, их лимитки и выходы не трогаются.
Нет данных оборота — фильтры 1–2 не отсекают («нет данных оборота» в логе, метки пустые).
Порядок проверок: повтор/уже в книге → другая монета под тикером → Bybit (нет пары, ST, не
Trading) → цена и план лестницы → фильтры 1 и 4 → лимит монет, USDT, risk_check → квота (2).
Отсеянные фильтрами 1, 2 и 4 уходят в теневую книгу (shadow=1): та же лестница и те же выходы
R/H, парно, но без мест, денег и risk_check основной; до shadow_max_coins открытых монет, ордера
с префиксом shd-. Обычные отказы (лимит монет, нет на Bybit, ST, другая монета) в тень не идут.
Метки на входе в каждой позиции: место по обороту, число пар, доля, оборот, горящие флаги.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from typing import Any

from .config import Config
from .ladder import LIMIT_FEE, MARKET_FEE, plan_ladder
from .positions import risk_check
from .stages.exit import compute_base_low, evaluate_exit

DAY = 86400
HOUR = 3600
LINK_MAX = 36                     # предел orderLinkId Bybit V5
FULL_EXIT = ("invalidation", "trailing")
SIGNAL_LABEL = {"invalidation": "стоп", "trailing": "трейл"}

DEFAULTS: dict[str, Any] = {
    "enabled": True, "steps": 5, "budget_usdt": 50.0, "min_order_usdt": 10.0,
    "max_coins": 15, "capital_usdt": 1500.0, "buy_valid_days": 120,
    "fill_on_touch": False, "skip_st": True,
    # отсев перед покупкой (замер 06.10.2026) и теневая книга
    "vol_bottom_share": 0.75, "illiquid_share": 0.39, "illiquid_max_slots": 5,
    "hot_block": True, "hot_min_lit": 1, "market_max_age_days": 3,
    "shadow": True, "shadow_max_coins": 30,
    "books": {"R": {"label": "правила", "emoji": "📏", "exits": "rules", "stop_pct": 25},
              "H": {"label": "держать", "emoji": "✋", "exits": "stop_only", "stop_pct": 25}},
}
# Почему монета в тени (dry_positions.shadow_why, через запятую).
WHY_LABEL = {"bottom": "нижняя четверть по обороту", "quota": "квота неликвида",
             "hot": "перегрев рынка", "nodata": "нет свежих данных рынка"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dry_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    book          TEXT NOT NULL,        -- R | H
    pair          TEXT NOT NULL,        -- LINKUSDT
    symbol        TEXT NOT NULL,
    coin_id       TEXT DEFAULT '',
    card_day      INTEGER NOT NULL,     -- день карточки, 00:00 UTC — ключ идемпотентности
    status        TEXT NOT NULL,        -- open | closed | rejected
    reason        TEXT DEFAULT '',      -- причина отказа или выхода
    created_ts    REAL NOT NULL,
    base_low      REAL,
    stop_pct      REAL,
    min_amt       REAL DEFAULT 5,       -- минимальный ордер биржи, USDT
    qty           REAL DEFAULT 0,       -- монет на счёте (комиссии покупок — в монете)
    bought_qty    REAL DEFAULT 0,       -- всего куплено: доли лестницы продаж — от него
    spent_usdt    REAL DEFAULT 0,
    proceeds_usdt REAL DEFAULT 0,       -- выручка продаж за вычетом комиссии
    hwm           REAL,
    last_price    REAL,                 -- последнее обработанное дневное закрытие
    last_day      INTEGER,              -- его день (00:00 UTC)
    fills_until   REAL DEFAULT 0,       -- часовые свечи до этого времени уже просмотрены
    first_fill_ts REAL,
    closed_ts     REAL,
    shadow        INTEGER DEFAULT 0,    -- 1 — теневая книга: отсеяно фильтрами, мест и денег нет
    shadow_why    TEXT DEFAULT '',      -- почему в тени: bottom,quota,hot,nodata (WHY_LABEL)
    vol_rank      INTEGER,              -- метки на входе: место по 30-дн. обороту Bybit
    vol_pairs     INTEGER,              --   среди скольких USDT-пар
    vol_share     REAL,                 --   место / число пар (NULL — нет данных оборота)
    vol_usd       REAL,                 --   средний дневной оборот, USDT
    hot_n         INTEGER,              --   горящих флагов перегрева (NULL — нет свежих данных)
    hot_flags     TEXT DEFAULT '',      --   какие (ключи market_regime.hot_flags)
    hot_day       INTEGER,              --   день данных рынка, 00:00 UTC
    UNIQUE(book, pair, card_day)
);
CREATE TABLE IF NOT EXISTS dry_orders (
    link_id     TEXT PRIMARY KEY,       -- orderLinkId: тот же id ушёл бы на биржу
    position_id INTEGER NOT NULL,
    book        TEXT NOT NULL,
    pair        TEXT NOT NULL,
    side        TEXT NOT NULL,          -- Buy | Sell
    kind        TEXT NOT NULL,          -- Market | Limit
    step        INTEGER,                -- ступень покупки 1..N
    signal      TEXT DEFAULT '',        -- продажа: тип сигнала (ladder_0, trailing, …)
    price       REAL,
    qty         REAL,
    usd         REAL,
    status      TEXT NOT NULL,          -- new | filled | cancelled
    created_ts  REAL NOT NULL,
    filled_ts   REAL,
    fee_usdt    REAL DEFAULT 0,
    note        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_dry_orders_pos ON dry_orders(position_id, side, status);
"""
# Колонки dry_positions после ec02fc7 — миграция старых БД (как db._MIGRATIONS).
_MIGRATIONS = [("shadow", "INTEGER DEFAULT 0"), ("shadow_why", "TEXT DEFAULT ''"),
               ("vol_rank", "INTEGER"), ("vol_pairs", "INTEGER"), ("vol_share", "REAL"),
               ("vol_usd", "REAL"), ("hot_n", "INTEGER"), ("hot_flags", "TEXT DEFAULT ''"),
               ("hot_day", "INTEGER")]


# ---------------------------------------------------------------- настройки и хранилище

def settings(cfg) -> dict[str, Any]:
    s = {**DEFAULTS, **(cfg.get("executor", {}) or {})}
    s["books"] = {k: {**DEFAULTS["books"].get(k, {}), **v}
                  for k, v in (s.get("books") or DEFAULTS["books"]).items()
                  if not str(k).startswith("_")}
    return s


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(dry_positions)")}
    for name, typ in _MIGRATIONS:
        if name not in cols:
            con.execute(f"ALTER TABLE dry_positions ADD COLUMN {name} {typ}")
    con.commit()
    return con


def _shadow_col(con) -> str:
    """Выражение «теневая ли позиция» для запросов сводок: БД только на чтение могла ещё не
    пройти миграцию (execute после выкатки не запускался) — тогда все позиции основные."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(dry_positions)")}
    return "COALESCE(shadow, 0)" if "shadow" in cols else "0"


def link_id(book: str, pid: int, tail: str, shadow: bool = False) -> str:
    """Детерминированный orderLinkId: позиция + ступень/сигнал. Повтор запроса с тем же id
    биржа отвергнет, а не исполнит второй раз (bybit-private-api-facts). Теневая книга —
    префикс shd-: такие ордера не ушли бы на биржу и в реальном режиме."""
    lid = f"{'shd' if shadow else 'dry'}-{book}{pid}-{tail}"
    if len(lid) > LINK_MAX:
        raise ValueError(f"orderLinkId длиннее {LINK_MAX}: {lid}")
    return lid


def signal_tail(stype: str) -> str:
    if stype.startswith("ladder_"):
        return f"S-L{stype.split('_')[1]}"
    return "S-" + {"invalidation": "INV", "trailing": "TR"}.get(stype, stype[:8].upper())


def utc_day(ts: float) -> int:
    return int(ts // DAY) * DAY


def local_day_start(now: float) -> float:
    d = datetime.fromtimestamp(now)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def pnl_usdt(p: dict, price: float | None = None) -> float:
    """Полный P&L позиции net-of-fees: выручка + остаток по закрытию − потрачено. До первого
    обработанного закрытия (день входа) остаток оценивается по средней цене покупок — иначе
    сводка дня показала бы всю вложенную сумму убытком."""
    if price is None:
        bought = p.get("bought_qty") or 0.0
        price = p.get("last_price") or ((p.get("spent_usdt") or 0.0) / bought if bought else 0.0)
    return (p.get("proceeds_usdt") or 0.0) + (p.get("qty") or 0.0) * price * (1 - MARKET_FEE) \
        - (p.get("spent_usdt") or 0.0)


def _rows(con, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def reserved_usdt(con, book: str) -> float:
    """Резерв неисполненных лимиток книги (на бирже эти USDT заблокированы). Теневые лимитки
    денег основной книги не занимают."""
    r = con.execute("SELECT COALESCE(SUM(o.usd),0) FROM dry_orders o JOIN dry_positions p "
                    "ON p.id=o.position_id WHERE o.book=? AND o.side='Buy' AND o.status='new' "
                    "AND COALESCE(p.shadow,0)=0", (book,)).fetchone()
    return float(r[0] or 0.0)


def free_usdt(con, book: str, capital: float) -> float:
    """Свободный USDT виртуального счёта книги: капитал + выручка − траты − резерв лимиток
    (только основная книга: тень мест и денег не занимает)."""
    r = con.execute("SELECT COALESCE(SUM(proceeds_usdt - spent_usdt),0) FROM dry_positions "
                    "WHERE book=? AND status!='rejected' AND COALESCE(shadow,0)=0",
                    (book,)).fetchone()
    return capital + float(r[0] or 0.0) - reserved_usdt(con, book)


def book_exposure(con, book: str) -> list[dict]:
    """Открытые позиции книги для risk_check: стоимость = остаток × закрытие + резерв лимиток
    (entry_price = 1, qty = $ — risk_check берёт entry × qty). Тень не входит."""
    out = []
    for p in _rows(con, "SELECT * FROM dry_positions WHERE book=? AND status='open' AND "
                        "COALESCE(shadow,0)=0", (book,)):
        px = p["last_price"] or (p["spent_usdt"] / p["bought_qty"] if p["bought_qty"] else 0.0)
        res = con.execute("SELECT COALESCE(SUM(usd),0) FROM dry_orders WHERE position_id=? AND "
                          "side='Buy' AND status='new'", (p["id"],)).fetchone()[0] or 0.0
        out.append({"id": p["id"], "entry_price": 1.0, "qty": p["qty"] * px + res,
                    "is_paper": 0})
    return out


def entry_checks(con, cfg, s: dict, book: str, need_usd: float) -> list[str]:
    """Причины отказа книге (пусто — можно): лимит монет, свободный USDT, risk_check."""
    why = []
    n_open = con.execute("SELECT COUNT(*) FROM dry_positions WHERE book=? AND status='open' "
                         "AND COALESCE(shadow,0)=0", (book,)).fetchone()[0]
    if n_open >= s["max_coins"]:
        why.append(f"лимит {s['max_coins']} монет")
    free = free_usdt(con, book, s["capital_usdt"])
    if free + 1e-9 < need_usd:
        why.append(f"нехватка USDT: свободно {free:.2f} < нужно {need_usd:.2f}")
    rc = Config({"stage7_positions": {**(cfg.get("stage7_positions", {}) or {}),
                                      "capital_usdt": s["capital_usdt"]}})
    why += risk_check(book_exposure(con, book), need_usd, rc)
    return why


# ---------------------------------------------------------------- отсев перед покупкой

def market_gate(cfg, s: dict, ctx: dict | None, now: float) -> dict:
    """Фильтр 4 — перегрев на входе по контексту рынка (regime.market_context / sources.market.
    load_context): {status: cold | hot | nodata, lit, avail, day, note}. hot — горит не меньше
    hot_min_lit флагов market_regime.hot_flags («близко к порогу» не считается); nodata — данных
    нет, они старше market_max_age_days (флаги — средние за 30–200 дней, пару дней им можно
    верить) или флагов с данными меньше market_regime.hot_min_available."""
    from .regime import FLAG_LABELS
    ctx = ctx or {}
    hot = ctx.get("hot") or {}
    day = ctx.get("day")
    lit = list(hot.get("lit") or [])
    avail = hot.get("avail") or 0
    out = {"status": "nodata", "lit": [], "avail": avail, "day": day, "note": "данных рынка нет"}
    if not isinstance(day, (int, float)):
        return out
    max_age = s["market_max_age_days"]
    if (utc_day(now) - utc_day(day)) / DAY > max_age:
        out["note"] = (f"данные рынка на {time.strftime('%d.%m', time.gmtime(day))} — "
                       f"старше {max_age:g} дн.")
        return out
    if lit and len(lit) >= s["hot_min_lit"]:
        out.update(status="hot", lit=lit, note=f"перегрев {len(lit)}/{avail}, горят: "
                   + ", ".join(FLAG_LABELS.get(k, k) for k in lit))
        return out
    need = cfg.get("market_regime.hot_min_available", 4)
    if avail < need:
        out["note"] = f"флагов перегрева с данными {avail} — меньше {need}"
        return out
    out.update(status="cold", lit=lit, note=f"перегрев {len(lit)}/{avail}")
    return out


def entry_gates(s: dict, symbol: str, ranks: dict | None, heat: dict) -> dict:
    """Фильтры 1 и 4 (свойства монеты и рынка) и метки позиции на входе.
    -> {vol, why, illiquid, labels}: why — что отсекает сразу (bottom — нижняя четверть по
    обороту; hot / nodata — перегрев или нет свежих данных рынка, если hot_block); illiquid —
    монета вне топ-~150 (квоту книги проверяет open_card после лимитов). Нет данных оборота —
    vol None: не отсекается."""
    from .liquidity import lookup
    v = lookup(ranks, symbol)
    why = []
    if v and v["share"] > s["vol_bottom_share"]:
        why.append("bottom")
    if s.get("hot_block", True) and heat["status"] != "cold":
        why.append("hot" if heat["status"] == "hot" else "nodata")
    valid = heat["status"] != "nodata"
    labels = {"vol_rank": v["rank"] if v else None, "vol_pairs": v["n"] if v else None,
              "vol_share": v["share"] if v else None, "vol_usd": v["usd"] if v else None,
              "hot_n": len(heat["lit"]) if valid else None,
              "hot_flags": ",".join(heat["lit"]) if valid else "", "hot_day": heat.get("day")}
    return {"vol": v, "why": why, "labels": labels,
            "illiquid": bool(v and v["share"] > s["illiquid_share"])}


def why_text(keys: list[str] | str, labels: dict) -> str:
    """Причины тени человеческими словами из ключей shadow_why и меток позиции."""
    from .regime import FLAG_LABELS
    if isinstance(keys, str):
        keys = [k for k in keys.split(",") if k]
    out = []
    for k in keys:
        txt = WHY_LABEL.get(k, k)
        if k in ("bottom", "quota") and labels.get("vol_rank"):
            txt += f" ({labels['vol_rank']}-е место из {labels['vol_pairs']})"
        elif k == "hot" and labels.get("hot_flags"):
            txt += " (" + ", ".join(FLAG_LABELS.get(f, f)
                                    for f in labels["hot_flags"].split(",")) + ")"
        out.append(txt)
    return "; ".join(out)


def illiquid_used(con, s: dict, book: str) -> int:
    """Мест основной книги, занятых монетами вне топ-~150 НА ДЕНЬ ВХОДА — по метке vol_share
    позиции, а не по сегодняшнему месту (монета могла с тех пор подняться или упасть)."""
    return con.execute("SELECT COUNT(*) FROM dry_positions WHERE book=? AND status='open' AND "
                       "COALESCE(shadow,0)=0 AND vol_share>?",
                       (book, s["illiquid_share"])).fetchone()[0]


def card_notes(cfg, c, ranks: dict | None, ctx: dict | None, now: float | None = None) -> list[str]:
    """Строки карточки scan --notify: что исполнитель сделает с монетой (те же market_gate и
    entry_gates, что в open_card). Только для Bybit spot: DEX-монету и тёзку под тикером
    исполнитель не купит и так. Квоты здесь нет — она зависит от книги на момент execute."""
    s = settings(cfg)
    if (not s.get("enabled", True) or getattr(c, "rf_venue", "") != "Bybit spot"
            or "bybit_ticker_mismatch" in (getattr(c, "flags", None) or [])):
        return []
    heat = market_gate(cfg, s, ctx, now if now is not None else time.time())
    g = entry_gates(s, c.symbol, ranks, heat)
    out = []
    if "bottom" in g["why"]:
        out.append(f"⚠ нижняя четверть по обороту — исполнитель не покупает "
                   f"({g['vol']['rank']}-е место из {g['vol']['n']} пар Bybit)")
    if "hot" in g["why"]:
        out.append(f"🔥 перегрев — исполнитель пропускает ({heat['note']})")
    elif "nodata" in g["why"]:
        out.append(f"⚠ нет свежих данных рынка — исполнитель пропускает ({heat['note']})")
    return out


# ---------------------------------------------------------------- рынок (сеть)

class BybitMarket:
    """Публичные данные Bybit spot для исполнителя. В selftest подменяется фикстурой с теми же
    методами: instrument, last_price, daily (закрытые дневные), hourly (закрытые часовые),
    vol_ranks (места по обороту на сегодня)."""

    def __init__(self, http, db_path: str | None = None):
        from .sources import bybit as src
        self.http, self._src, self.db_path = http, src, db_path

    def vol_ranks(self, now: float) -> dict:
        """Места по 30-дн. обороту (scanner/liquidity.py): из БД за сегодня или подсчёт."""
        from . import liquidity
        if not self.db_path:
            return {}
        return liquidity.ensure_ranks(self.db_path, self.http, now)

    def instrument(self, pair: str) -> dict | None:
        return self._src.fetch_instrument(self.http, pair)

    def last_price(self, pair: str) -> float | None:
        return self._src.fetch_last_price(self.http, pair)

    def daily(self, pair: str) -> dict:
        return self._src.fetch_daily_ohlcv(self.http, pair, 400)

    def hourly(self, pair: str, start_ts: float) -> dict:
        return self._src.fetch_klines(self.http, pair, "60", int(start_ts * 1000))


# ---------------------------------------------------------------- вход

def todays_cards(cfg, con, now: float) -> list[dict]:
    """Монеты, пришедшие сегодня карточкой: alert_log с начала местного дня, coin_id и флаг
    «тикер на Bybit — другая монета» — из watchlist.json (может не найтись: карточка из
    вчерашнего списка — не страшно)."""
    t0 = local_day_start(now)
    try:
        rows = con.execute("SELECT symbol, MIN(ts), MAX(score) FROM alert_log WHERE ts>=? "
                           "GROUP BY symbol ORDER BY MIN(ts)", (t0,)).fetchall()
    except sqlite3.OperationalError:          # нет alert_log — скан ещё ни разу не шёл
        return []
    wl = {}
    try:
        from .pipeline import load_watchlist
        wl = {(c.symbol or "").upper(): c for c in load_watchlist(cfg["output"]["watchlist_json"])}
    except Exception:  # noqa: BLE001 — без watchlist coin_id просто пустой
        pass
    out = []
    for sym, ts, score in rows:
        c = wl.get((sym or "").upper())
        out.append({"symbol": (sym or "").upper(), "ts": ts, "score": score or 0.0,
                    "coin_id": getattr(c, "coin_id", "") or "",
                    "mismatch": "bybit_ticker_mismatch" in (getattr(c, "flags", None) or [])})
    return out


def _insert_position(con, book: str, card: dict, pair: str, now: float, status: str,
                     reason: str = "", *, shadow: bool = False, why: str = "",
                     labels: dict | None = None, **kw) -> int | None:
    lab = labels or {}
    cur = con.execute(
        "INSERT OR IGNORE INTO dry_positions(book, pair, symbol, coin_id, card_day, status, "
        "reason, created_ts, base_low, stop_pct, min_amt, shadow, shadow_why, vol_rank, "
        "vol_pairs, vol_share, vol_usd, hot_n, hot_flags, hot_day) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (book, pair, card["symbol"], card.get("coin_id", ""), utc_day(card["ts"]), status,
         reason, now, kw.get("base_low"), kw.get("stop_pct"), kw.get("min_amt", 5.0),
         int(shadow), why, lab.get("vol_rank"), lab.get("vol_pairs"), lab.get("vol_share"),
         lab.get("vol_usd"), lab.get("hot_n"), lab.get("hot_flags") or "", lab.get("hot_day")))
    return int(cur.lastrowid) if cur.rowcount else None


def open_card(cfg, con, market, s: dict, card: dict, now: float, *,
              ranks: dict | None = None, heat: dict | None = None) -> list[str]:
    """Лестница по карточке — в обе книги или отказ обеим (с причиной в журнале); отсеянное
    фильтрами 1, 2, 4 — в обе теневые книги. ranks — места по обороту (liquidity), heat —
    market_gate; heat не передан — данных рынка нет."""
    sym = card["symbol"]
    pair = f"{sym}USDT"
    books = list(s["books"])
    day = utc_day(card["ts"])
    done = con.execute("SELECT COUNT(*) FROM dry_positions WHERE pair=? AND card_day=?",
                       (pair, day)).fetchone()[0]
    if done:
        return [f"{sym}: карточка уже обработана — повтор не дублирует"]
    held = con.execute("SELECT book, created_ts FROM dry_positions WHERE pair=? AND "
                       "status='open' AND COALESCE(shadow,0)=0 LIMIT 1", (pair,)).fetchone()
    if held:
        return [f"{sym}: уже в книге {held[0]} с {time.strftime('%d.%m', time.localtime(held[1]))}"
                f" — вторую лестницу не ставлю"]
    heat = heat or market_gate(cfg, s, None, now)
    g = entry_gates(s, sym, ranks, heat)
    lab = g["labels"]

    def reject(reason: str, shadow: bool = False, why: str = "") -> list[str]:
        for b in books:
            _insert_position(con, b, card, pair, now, "rejected", reason, shadow=shadow,
                             why=why, labels=lab)
        con.commit()
        return [f"{sym}: ОТКАЗ — {reason}"]

    if card.get("mismatch"):
        return reject(f"на Bybit под тикером {sym} другая монета (цена не совпала со сканером)")
    inst = market.instrument(pair)
    if not inst:
        return reject(f"{pair} нет на Bybit spot")
    if inst.get("status") != "Trading" or (s["skip_st"] and inst.get("st")):
        return reject(f"Bybit: статус {inst.get('status')}{', метка ST' if inst.get('st') else ''}")
    price = market.last_price(pair)
    closes = (market.daily(pair) or {}).get("c") or []
    base_low = compute_base_low(closes, 30)
    if not price or not base_low:
        return [f"{sym}: нет цены или истории закрытий Bybit — пропуск до следующего прогона"]
    e = cfg["stage8_exit"]
    plan = plan_ladder(price, base_low, s["budget_usdt"], steps=s["steps"],
                       min_order=s["min_order_usdt"], tick=inst["tick"],
                       qty_step=inst["qty_step"], exch_min_amt=inst["min_amt"],
                       exch_min_qty=inst["min_qty"],
                       floor_pct=e["invalidation_below_base_low_pct"], sell="prod",
                       prod_levels=e["ladder"])
    if not plan["ok"]:
        return reject(plan["error"])

    def place(shadow: bool, why: str = "") -> None:
        """Позиции в обе книги (основные или теневые), ордера лестницы, ступень 1 — рынком."""
        for b in books:
            pid = _insert_position(con, b, card, pair, now, "open", shadow=shadow, why=why,
                                   labels=lab, base_low=base_low,
                                   stop_pct=s["books"][b]["stop_pct"], min_amt=inst["min_amt"])
            if pid is None:
                continue
            for o in plan["buys"]:
                market_step = o["type"] == "рынок"
                con.execute(
                    "INSERT OR IGNORE INTO dry_orders(link_id, position_id, book, pair, side, "
                    "kind, step, price, qty, usd, status, created_ts) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (link_id(b, pid, f"B{o['n']}", shadow), pid, b, pair, "Buy",
                     "Market" if market_step else "Limit", o["n"], o["price"], o["qty"],
                     o["usd"], "new", now))
            mkt = con.execute("SELECT * FROM dry_orders WHERE position_id=? AND kind='Market' "
                              "AND status='new'", (pid,)).fetchone()
            if mkt:
                _fill_buy(con, dict(mkt), price, MARKET_FEE, now)
            con.commit()

    lims = ", ".join(f"{o['price']:.6g}" for o in plan["buys"][1:])
    ladder = (f"{len(plan['buys'])} ступ. на ${plan['spent']:.2f}: рынок ~{price:.6g}"
              + (f", лимитки {lims}" if lims else "") + f"; лоу базы {base_low:.6g}")

    def to_shadow(keys: list[str], extra: str = "") -> list[str]:
        """Отсеяно фильтрами: в теневые книги (те же лестница и выходы, без мест и денег)."""
        text = why_text(keys, lab) + extra
        if not s.get("shadow", True):
            return reject(text)
        sheld = con.execute("SELECT created_ts FROM dry_positions WHERE pair=? AND "
                            "status='open' AND shadow=1 LIMIT 1", (pair,)).fetchone()
        if sheld:
            return [f"{sym}: НЕ покупаю — {text}; в тени уже с "
                    f"{time.strftime('%d.%m', time.localtime(sheld[0]))} — вторую не ставлю"]
        n_sh = con.execute("SELECT COUNT(DISTINCT pair) FROM dry_positions WHERE shadow=1 AND "
                           "status='open'").fetchone()[0]
        if n_sh >= s["shadow_max_coins"]:
            return reject(f"{text}; теневая книга заполнена ({s['shadow_max_coins']} монет)",
                          shadow=True, why=",".join(keys))
        place(True, ",".join(keys))
        return [f"{sym}: НЕ покупаю — {text} → в тень {'/'.join(books)}: {ladder}"]

    if g["why"]:                          # фильтры 1 и 4: свойства монеты и рынка
        return to_shadow(g["why"])
    why = {b: entry_checks(con, cfg, s, b, plan["spent"]) for b in books}
    if any(why.values()):
        return reject("; ".join(f"{b}: {', '.join(w)}" for b, w in why.items() if w))
    if g["illiquid"]:                     # фильтр 2: квота держится, только если место есть
        used = max(illiquid_used(con, s, b) for b in books)
        if used >= s["illiquid_max_slots"]:
            return to_shadow(["quota"], f": вне топ-{s['illiquid_share'] * 100:.0f}% уже "
                                        f"{used} из {s['illiquid_max_slots']} мест")

    place(False)
    v = g["vol"]
    lines = [f"{sym}: поставил бы в книги {'/'.join(books)} — {ladder}",
             "  метки: " + (f"оборот {v['rank']}-е место из {v['n']}" if v
                            else "нет данных оборота") + f", рынок {heat['note']}"]
    lines += [f"  ⚠ {w}" for w in plan["warns"]]
    return lines


# ---------------------------------------------------------------- исполнение и выходы

def _fill_buy(con, order: dict, price: float, fee: float, ts: float) -> None:
    q_net = order["qty"] * (1 - fee)
    usd = order["qty"] * price
    con.execute("UPDATE dry_orders SET status='filled', filled_ts=?, price=?, usd=?, fee_usdt=? "
                "WHERE link_id=? AND status='new'", (ts, price, usd, usd * fee, order["link_id"]))
    con.execute("UPDATE dry_positions SET qty=qty+?, bought_qty=bought_qty+?, "
                "spent_usdt=spent_usdt+?, first_fill_ts=COALESCE(first_fill_ts, ?) WHERE id=?",
                (q_net, q_net, usd, ts, order["position_id"]))


def _sell(con, p: dict, stype: str, qty: float, price: float, ts: float, note: str) -> float:
    qty = min(qty, p["qty"])
    if qty <= 0:
        return 0.0
    lid = link_id(p["book"], p["id"], signal_tail(stype), bool(p.get("shadow")))
    if con.execute("SELECT 1 FROM dry_orders WHERE link_id=?", (lid,)).fetchone():
        return 0.0                               # этот сигнал уже исполнен
    usd = qty * price
    con.execute("INSERT INTO dry_orders(link_id, position_id, book, pair, side, kind, signal, "
                "price, qty, usd, status, created_ts, filled_ts, fee_usdt, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lid, p["id"], p["book"], p["pair"], "Sell", "Market", stype, price, qty, usd,
                 "filled", ts, ts, usd * MARKET_FEE, note))
    con.execute("UPDATE dry_positions SET qty=MAX(0, qty-?), proceeds_usdt=proceeds_usdt+? "
                "WHERE id=?", (qty, usd * (1 - MARKET_FEE), p["id"]))
    _reload(con, p)
    return usd * (1 - MARKET_FEE)


def _cancel_buys(con, pid: int, why: str) -> int:
    cur = con.execute("UPDATE dry_orders SET status='cancelled', note=? WHERE position_id=? "
                      "AND side='Buy' AND status='new'", (why, pid))
    return cur.rowcount


def process_position(cfg, con, market, s: dict, pos: dict, now: float) -> list[str]:
    """Исполнения лимиток по часовым свечам и выходы по дневным закрытиям с прошлого раза."""
    p = dict(pos)
    pair, book = p["pair"], p["book"]
    tag = f"тень {book}" if p.get("shadow") else book       # подпись в логе
    bk = s["books"].get(book) or {}
    lines: list[str] = []
    daily = market.daily(pair) or {}
    d_ts, d_c = daily.get("ts") or [], daily.get("c") or []
    pending = _rows(con, "SELECT * FROM dry_orders WHERE position_id=? AND side='Buy' AND "
                         "status='new' ORDER BY step", (p["id"],))
    expire_at = p["created_ts"] + s["buy_valid_days"] * DAY
    start = max(p["fills_until"] or 0.0, p["created_ts"])
    hours: list[tuple[int, float]] = []
    if pending:
        hk = market.hourly(pair, start) or {}
        hours = [(t, lo) for t, lo in zip(hk.get("ts") or [], hk.get("l") or [])
                 if t >= start and t + HOUR <= now]
    hi = 0
    touch = bool(s.get("fill_on_touch"))

    def fills_until(limit_ts: float) -> None:
        """Часовые свечи, начавшиеся до limit_ts: лимитка исполнена, если low прошёл сквозь."""
        nonlocal hi
        while hi < len(hours) and hours[hi][0] < limit_ts:
            t, lo = hours[hi]
            hi += 1
            if t >= expire_at:
                continue
            for o in pending:
                if o["status"] == "new" and (lo < o["price"] or (touch and lo <= o["price"])):
                    o["status"] = "filled"
                    _fill_buy(con, o, o["price"], LIMIT_FEE, t + HOUR)
                    lines.append(f"{tag} {p['symbol']}: исполнилась бы ступень {o['step']} "
                                 f"по {o['price']:.6g}")
            p["fills_until"] = t + HOUR
        _reload(con, p)

    ecfg = Config({"stage8_exit": {**cfg["stage8_exit"],
                                   "invalidation_below_base_low_pct": bk.get("stop_pct", 25)}})
    triggered = {r["signal"] for r in _rows(con, "SELECT signal FROM dry_orders WHERE "
                                                 "position_id=? AND side='Sell'", (p["id"],))}
    closed = False
    for i, (day, close) in enumerate(zip(d_ts, d_c)):
        if day + DAY <= p["created_ts"] or (p["last_day"] is not None and day <= p["last_day"]):
            continue
        fills_until(day + DAY)
        p["last_day"], p["last_price"] = day, close
        if p["qty"] <= 0:
            continue
        p["hwm"] = max(p["hwm"] or 0.0, close)
        avg = p["spent_usdt"] / p["bought_qty"]
        sigs = evaluate_exit({"entry_price": avg, "base_low": p["base_low"], "variant": "A"},
                             close, p["hwm"], None, triggered, ecfg, recent_closes=d_c[:i + 1])
        if bk.get("exits") == "stop_only":
            sigs = [x for x in sigs if x["type"] == "invalidation"]
        for sig in sigs:
            stype = sig["type"]
            if stype in FULL_EXIT:
                q = p["qty"]
            elif stype.startswith("ladder_"):
                idx = int(stype.split("_")[1])
                lad = cfg["stage8_exit"]["ladder"]
                q = min(p["qty"], (lad[idx][1] if idx < len(lad) else 0.0) * p["bought_qty"])
                if (p["qty"] - q) * close < (p["min_amt"] or 5.0):
                    q = p["qty"]                 # остаток меньше минимума биржи — вместе с долей
            else:
                continue                         # информационные сигналы ордеров не дают
            got = _sell(con, p, stype, q, close, day + DAY, sig["note"])
            triggered.add(stype)
            lines.append(f"{tag} {p['symbol']}: продал бы {q:.6g} по закрытию {close:.6g} "
                         f"({SIGNAL_LABEL.get(stype, stype)}) → ${got:.2f}")
            if p["qty"] <= 1e-12:
                p["qty"] = 0.0
                closed = True
                p["status"], p["closed_ts"] = "closed", day + DAY
                p["reason"] = SIGNAL_LABEL.get(stype, stype)
                n = _cancel_buys(con, p["id"], "позиция закрыта")
                pending = []
                if n:
                    lines.append(f"{tag} {p['symbol']}: снял бы неисполненные лимитки ({n})")
                break
        _save(con, p)
        if closed:
            break
    if not closed:
        fills_until(now)
        if pending and now >= expire_at:
            n = _cancel_buys(con, p["id"], f"срок {s['buy_valid_days']} дн.")
            if n:
                lines.append(f"{tag} {p['symbol']}: снял бы лимитки по сроку ({n})")
    _save(con, p)
    con.commit()
    return lines


def _reload(con, p: dict) -> None:
    """Количество и деньги — всегда из БД (покупки и продажи пишутся туда сразу); в p
    остаются только поля прогона (hwm, последний день, курсор свечей, статус)."""
    r = con.execute("SELECT qty, bought_qty, spent_usdt, proceeds_usdt, first_fill_ts "
                    "FROM dry_positions WHERE id=?", (p["id"],)).fetchone()
    p["qty"], p["bought_qty"], p["spent_usdt"], p["proceeds_usdt"], p["first_fill_ts"] = \
        r[0], r[1], r[2], r[3], r[4]


def _save(con, p: dict) -> None:
    con.execute("UPDATE dry_positions SET hwm=?, last_price=?, last_day=?, fills_until=?, "
                "status=?, closed_ts=?, reason=? WHERE id=?",
                (p["hwm"], p["last_price"], p["last_day"], p["fills_until"], p["status"],
                 p["closed_ts"], p["reason"], p["id"]))


# ---------------------------------------------------------------- шаг прогона

def db_context(con, cfg) -> dict:
    """Контекст рынка из market_daily той же БД, без сети (таблицы нет — {})."""
    from .regime import market_context
    try:
        cur = con.execute("SELECT * FROM market_daily ORDER BY day")
    except sqlite3.OperationalError:
        return {}
    names = [d[0] for d in cur.description]
    return market_context([dict(zip(names, r)) for r in cur.fetchall()], cfg)


def _vol_ranks(market, now: float) -> dict:
    """Места по обороту на сегодня; сбой подсчёта не роняет исполнитель — фильтры 1–2 молчат."""
    get = getattr(market, "vol_ranks", None)
    if get is None:
        return {"ok": False, "by_sym": {}, "note": "нет данных оборота: источника нет"}
    try:
        return get(now) or {"ok": False, "by_sym": {}, "note": "нет данных оборота"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "by_sym": {}, "note": f"нет данных оборота: {type(e).__name__}: {e}"}


def run_dry(cfg, market, *, db_path: str | None = None, now: float | None = None,
            wallet_usdt: float | None = None, wallet_note: str = "",
            mctx: dict | None = None, halt_base=None) -> tuple[int, list[str]]:
    """Шаг ежедневного прогона -> (код выхода, строки лога). Сбой одной позиции не валит
    остальные, но код станет 1 (сводка дня: «⚠ пробный исполнитель упал»). mctx — контекст
    рынка (run.py execute: sources.market.load_context); None — из market_daily этой БД.
    Места по обороту (market.vol_ranks) считаются, только если сегодня есть карточки.
    Стоп-кран (scanner/control.py, data/HALT; halt_base — каталог для тестов) — исполнитель
    не делает ничего: ни новых лестниц, ни исполнений и продаж по открытым; база не меняется.
    После `run.py resume` открытые досчитываются со своего места (fills_until, last_day)."""
    s = settings(cfg)
    if not s.get("enabled", True):
        return 0, ["executor.enabled = false — пропуск"]
    from . import control
    halt = control.halted(halt_base)
    if halt:
        return 0, [control.halt_line(halt) + " — новых лестниц и исполнений нет; снять: "
                   "python3 run.py resume (на сервере)"]
    now = now if now is not None else time.time()
    con = connect(db_path or cfg["output"]["db_path"])
    lines: list[str] = []
    code = 0
    try:
        for pos in _rows(con, "SELECT * FROM dry_positions WHERE status='open' ORDER BY id"):
            try:
                lines += process_position(cfg, con, market, s, pos, now)
            except Exception as e:  # noqa: BLE001 — одна позиция не валит остальные
                con.rollback()
                code = 1
                tag = f"тень {pos['book']}" if pos["shadow"] else pos["book"]
                lines.append(f"⚠ {tag} {pos['symbol']}: {type(e).__name__}: {e}")
        cards = todays_cards(cfg, con, now)
        need = 0.0
        if cards:
            heat = market_gate(cfg, s, mctx if mctx is not None else db_context(con, cfg), now)
            ranks = _vol_ranks(market, now)
            n = ranks.get("n") or 0
            block = s.get("hot_block", True) and heat["status"] != "cold"
            lines.append(f"рынок: {heat['note']}"
                         + (" — новых лестниц нет, карточки уходят в тень" if block else ""))
            lines.append(f"оборот: {n} пар Bybit, нижняя четверть — место > "
                         f"{s['vol_bottom_share'] * n:.0f}, вне топа — > "
                         f"{s['illiquid_share'] * n:.0f}" if ranks.get("ok") else
                         f"{ranks.get('note') or 'нет данных оборота'} — фильтры по обороту "
                         f"не отсекают")
            # ликвидные первыми: квота и лимит монет достаются лучшему месту по обороту
            from .liquidity import lookup

            def order(card: dict) -> tuple:
                v = lookup(ranks, card["symbol"])
                return (0, v["rank"], card["ts"]) if v else (1, 0, card["ts"])
            cards.sort(key=order)
            for card in cards:
                try:
                    got = open_card(cfg, con, market, s, card, now, ranks=ranks, heat=heat)
                except Exception as e:  # noqa: BLE001
                    con.rollback()
                    code = 1
                    got = [f"⚠ {card['symbol']}: {type(e).__name__}: {e}"]
                lines += got
                if any("поставил бы" in x for x in got):
                    need += s["budget_usdt"]
        else:
            lines.append("сегодня карточек не было — новых лестниц нет")
        if wallet_usdt is not None:
            enough = "хватило бы" if wallet_usdt >= need else "НЕ хватило бы"
            lines.append(f"реальный счёт: свободно {wallet_usdt:.2f} USDT; сегодняшние лестницы "
                         f"одной книги — ${need:.2f} → для реального режима {enough}")
        elif wallet_note:
            lines.append(wallet_note)
        for b, bk in s["books"].items():
            st = book_summary(con, b)
            lines.append(f"книга {b} «{bk['label']}»: открыто {st['open']}, вложено "
                         f"${st['spent']:.2f}, P&L {st['pnl']:+.2f} USDT, свободно "
                         f"{free_usdt(con, b, s['capital_usdt']):.2f}")
        sh = {b: book_summary(con, b, shadow=True) for b in s["books"]}
        if any(x["open"] or x["closed"] for x in sh.values()):
            lines.append("тень (мест и денег не занимает): " + "; ".join(
                f"{b} открыто {x['open']}, вложено ${x['spent']:.2f}, P&L {x['pnl']:+.2f}"
                for b, x in sh.items()))
    finally:
        con.close()
    return code, lines


# ---------------------------------------------------------------- сводки (без сети)

def book_summary(con, book: str, shadow: bool = False,
                 sh: str = "COALESCE(shadow, 0)") -> dict[str, Any]:
    """Позиции книги (основной или теневой) без отказов: открыто, закрыто, вложено, P&L.
    sh — выражение _shadow_col (БД без миграции: «0», теневых нет)."""
    ps = _rows(con, f"SELECT * FROM dry_positions WHERE book=? AND status!='rejected' "
                    f"AND {sh}=?", (book, int(shadow)))
    return {"open": sum(1 for p in ps if p["status"] == "open"),
            "closed": sum(1 for p in ps if p["status"] == "closed"),
            "spent": sum(p["spent_usdt"] for p in ps),
            "pnl": sum(pnl_usdt(p) for p in ps)}


def brief_state(cfg, now: float | None = None) -> dict | None:
    """Для сводки дня: книги (основные и теневые) и действия за сегодня; отказы — с причиной,
    отсеянное фильтрами — отдельным списком «в тени». Таблиц нет — None (строки не будет)."""
    from .benchmark import connect_ro
    now = now if now is not None else time.time()
    con = connect_ro(cfg["output"]["db_path"])
    if con is None:
        return None
    con.row_factory = sqlite3.Row
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "dry_positions" not in names:
            return None
        sh = _shadow_col(con)
        psh = sh.replace("shadow", "p.shadow")
        t0 = local_day_start(now)
        s = settings(cfg)
        books = {b: {**book_summary(con, b, False, sh), "label": bk["label"],
                     "emoji": bk.get("emoji", "•")} for b, bk in s["books"].items()}
        shadow = {b: {**book_summary(con, b, True, sh), "label": bk["label"],
                      "emoji": bk.get("emoji", "•")} for b, bk in s["books"].items()}
        opened = [r[0] for r in con.execute(
            f"SELECT DISTINCT symbol FROM dry_positions WHERE status!='rejected' AND {sh}=0 "
            f"AND created_ts>=?", (t0,))]
        rejected = [(r[0], r[1]) for r in con.execute(
            "SELECT symbol, MIN(reason) FROM dry_positions WHERE status='rejected' AND "
            "created_ts>=? GROUP BY symbol", (t0,))]
        shadowed: list[tuple[str, str]] = []
        if sh != "0":
            for r in _rows(con, "SELECT * FROM dry_positions WHERE shadow=1 AND "
                                "status!='rejected' AND created_ts>=? ORDER BY id", (t0,)):
                if all(r["symbol"] != x[0] for x in shadowed):
                    shadowed.append((r["symbol"], why_text(r["shadow_why"], r)))
        fills = con.execute(f"SELECT COUNT(*) FROM dry_orders o JOIN dry_positions p ON "
                            f"p.id=o.position_id WHERE o.side='Buy' AND o.kind='Limit' AND "
                            f"o.status='filled' AND o.filled_ts>=? AND {psh}=0",
                            (t0 - DAY,)).fetchone()[0]
        sells = [(r[0], r[1], r[2]) for r in con.execute(
            f"SELECT o.book, p.symbol, o.signal FROM dry_orders o JOIN dry_positions p "
            f"ON p.id=o.position_id WHERE o.side='Sell' AND o.created_ts>=? AND {psh}=0 "
            f"ORDER BY o.created_ts", (t0 - DAY,))]
    finally:
        con.close()
    return {"books": books, "shadow": shadow, "opened": opened, "rejected": rejected,
            "shadowed": shadowed, "fills": fills, "sells": sells}


def outcomes(con, book: str, shadow: bool = False,
             sh: str = "COALESCE(shadow, 0)") -> list[dict]:
    """Окна и итоги позиций книги для сравнения с рынком: start — первое исполнение, end —
    закрытие позиции или конец последнего обработанного дня; cost — потрачено; why — причины
    тени (ключи WHY_LABEL)."""
    out = []
    for p in _rows(con, f"SELECT * FROM dry_positions WHERE book=? AND status!='rejected' "
                        f"AND spent_usdt>0 AND {sh}=?", (book, int(shadow))):
        end = p["closed_ts"] if p["status"] == "closed" else (
            p["last_day"] + DAY if p["last_day"] is not None else None)
        if end is None or not p["first_fill_ts"]:
            continue
        out.append({"id": p["id"], "symbol": p["symbol"], "coin_id": p["coin_id"],
                    "start": p["first_fill_ts"], "end": end, "cost": p["spent_usdt"],
                    "pnl": pnl_usdt(p),
                    "why": [k for k in (p.get("shadow_why") or "").split(",") if k]})
    return out


def _weekly(cfg, shadow: bool) -> list[dict]:
    """Книги (основные или теневые) против альтов на тех же окнах: compare_outcomes + P&L в
    USDT по всем позициям (pnl_usd); у основных — контрольная корзина, у теневых — разбивка
    по причинам (by_why: {ключ: {n, cost, pnl}}; позиция с двумя причинами — в обеих)."""
    from . import benchmark
    db = cfg["output"]["db_path"]
    con = benchmark.connect_ro(db)
    if con is None:
        return []
    con.row_factory = sqlite3.Row
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "dry_positions" not in names:
            return []
        sh = _shadow_col(con)
        if shadow and sh == "0":
            return []
        rows = {b: outcomes(con, b, shadow, sh) for b in settings(cfg)["books"]}
    finally:
        con.close()
    if not any(rows.values()):
        return []
    mkt = benchmark.load_market(db)
    uni = None if shadow else benchmark.load_universe(db)
    s = settings(cfg)
    out = []
    for b, rs in rows.items():
        res = benchmark.compare_outcomes(rs, mkt)
        if not shadow:
            res.update(benchmark.basket_for(uni, res.get("rows") or []))
        by: dict[str, dict] = {}
        for r in rs:
            for k in r["why"]:
                d = by.setdefault(k, {"n": 0, "cost": 0.0, "pnl": 0.0})
                d["n"] += 1
                d["cost"] += r["cost"]
                d["pnl"] += r["pnl"]
        bk = s["books"][b]
        out.append({"key": b, "label": bk["label"], "emoji": bk.get("emoji", "•"), **res,
                    "pnl_usd": sum(r["pnl"] for r in rs), "by_why": by})
    return out


def weekly_books(cfg) -> list[dict]:
    """Строки блока недельной сводки: каждая основная книга против альтов, BTC и контрольной
    корзины (все монеты watchlist прогона входа поровну, прокси — капа) на тех же окнах."""
    return _weekly(cfg, False)


def weekly_shadow(cfg) -> list[dict]:
    """Теневые книги для недельной сводки: против альтов на тех же окнах и по причинам."""
    return _weekly(cfg, True)
