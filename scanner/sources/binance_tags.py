"""Метка Binance «Monitoring Tag» (кандидаты на делистинг) — для пробного исполнителя и watch.

Правило (замер Q12 16.10.2026, backtest/monitoring_tag_study.py; решение владельца 16.10.2026):
монету под меткой исполнитель не покупает, открытую позицию продаёт по первому дневному
закрытию Bybit после дня метки, реальной позиции — громкая карточка. С 07.2023 метку получили
166 монет: 76% потом делистнуты (71% — в течение года), цена через год −57% (−76% к альтам).
Повод — BICO: метка 09.10.2026, пробный исполнитель и демо-счёт купили её 10.10.

Источник — публичный CMS Binance без ключа (проверено 16.10.2026):
  .../bapi/composite/v1/public/cms/article/list/query?type=1&catalogId=<c>&pageNo=<p>&pageSize=50
Каталоги: 49 «Latest Binance News» (метки и снятия), 161 «Delisting» (одна метка — 18.06.2026 ACT,
BLUR, PIVX, QKC); 48 / 93 / 157 в замере меток не содержали. Заголовки: «Binance Will Extend the
Monitoring Tag to Include X, Y & Z on DATE», снятие — «Remove the Monitoring Tag for …» (8 монет
с 07.2023; MLN снята и помечена снова — снова под меткой). Дата метки — из заголовка («on DATE»),
иначе день публикации. Вводная статья 26.07.2023 перечисляет 26 монет только в тексте — список
из article/detail (не ответил — INTRO_Q12, тот же список из замера). Тикеры бывают из 1 символа (D).

Обновление — не чаще раза в UTC-день (кто первым: scan --notify, watch, execute), остальные
читают базу. Первый раз — полный обход каталогов до 07.2023 (~50 страниц; по BUDGET_S за
прогон, недообход продолжается со своей страницы), дальше — первые страницы до уже
просмотренных статей (hwm каталога − OVERLAP): обычно 2 запроса в день. CMS отвечает 429 —
HttpClient ждёт окно и повторяет; темп — http.rate_limits_per_min["www.binance.com"]. Сбой —
последнее известное состояние остаётся, view() несёт err; счётчики хоста — в source_health.

Тёзки: метка — по тикеру Binance. Цена Binance (api/v3/ticker/price, один запрос на все пары,
только если под меткой есть интересная монета) против цены нашей монеты в пределах price_tol —
та же; явное расхождение — другая монета, метку не применяем; пары на Binance нет или цены нет —
та же (тикер совпал, монета пришла карточкой / позицией под этим тикером): вслепую — в
безопасную сторону. Таблицы: exchange_tag_articles (статьи с меткой), exchange_tags (интервалы
«под меткой с … по …»), exchange_tag_sync (прогресс обхода и итог последней попытки).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any, Callable

DAY = 86400
TAG = "bn_monitor"
CMS = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
DETAIL = "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query"
PRICES = "https://api.binance.com/api/v3/ticker/price"
CATALOGS = (49, 161)
PAGE_SIZE = 50
SINCE_TS = 1688169600            # 2023-07-01 00:00 UTC: метки появились 26.07.2023
MAX_PAGES = 80                   # предохранитель на каталог (49 до 07.2023 — ~40 страниц)
OVERLAP = 3 * DAY                # дообход: до статей старше hwm каталога − 3 дн.
BUDGET_S = 240.0                 # полный обход за прогон не дольше (дальше — в следующий)
RETRY_S = 1800.0                 # неудачная попытка — следующая не раньше чем через 30 мин
INTRO = "Introducing Seed Tags & Monitoring Tags"
# Вводная статья 26.07.2023 (Q12, article/detail): 26 монет — запас, если detail не ответил.
INTRO_Q12 = ("AKRO AMB ARK BTS CREAM DREP ELF FTT GFT JASMY LINA LOOM MLN MULTI OAX OMG OOKI "
             "PERL PNT REN SNM SRM TORN VGX WRX YFII").split()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exchange_tag_articles (
    code       TEXT PRIMARY KEY,      -- код статьи CMS Binance
    catalog    INTEGER,
    release_ts REAL,                  -- публикация, сек.
    day        INTEGER,               -- день события 00:00 UTC: «on DATE» заголовка или публикации
    title      TEXT,
    added      TEXT DEFAULT '',       -- тикеры, получившие метку (через запятую)
    removed    TEXT DEFAULT ''        -- тикеры, с которых метку сняли
);
CREATE TABLE IF NOT EXISTS exchange_tags (
    tag        TEXT NOT NULL,         -- bn_monitor
    symbol     TEXT NOT NULL,         -- тикер Binance
    since_ts   INTEGER NOT NULL,      -- день метки 00:00 UTC
    removed_ts INTEGER,               -- день снятия (NULL — под меткой)
    title      TEXT DEFAULT '',       -- заголовок статьи с меткой
    fetched_ts REAL,                  -- когда пересчитано из статей
    PRIMARY KEY (tag, symbol, since_ts)
);
CREATE TABLE IF NOT EXISTS exchange_tag_sync (
    tag        TEXT PRIMARY KEY,
    ok_ts      REAL,                  -- последнее успешное обновление (все каталоги до конца)
    try_ts     REAL,                  -- последняя попытка
    err        TEXT DEFAULT '',       -- её сбой ('' — прошла)
    progress   TEXT DEFAULT '{}'      -- JSON {каталог: {hwm, page, done}}
);
"""


def utc_day(ts: float) -> int:
    return int(ts // DAY) * DAY


def ddmm(ts: float) -> str:
    return time.strftime("%d.%m", time.gmtime(ts))


# ---------------------------------------------------------------- разбор (чистые функции)

_END = r"(?=,?\s+(?:and\s+)?(?:Remove|the Seed|Extend)|\s+on\s+\d{4}-\d{2}-\d{2}|$)"
_ADD = re.compile(r"Extend the Monitoring Tag to (?:Include )?(.+?)" + _END)
_REM = re.compile(r"Remove the Monitoring Tag for (.+?)" + _END)
_ON = re.compile(r"\bon (\d{4})-(\d{2})-(\d{2})")


def tickers(s: str) -> list[str]:
    """«BICO & CVC», «A, B and C», «ACA, D, DATA & FLOW» -> тикеры (скобки — выкинуты)."""
    s = re.sub(r"\([^)]*\)", "", s or "")
    out = []
    for x in re.split(r",\s*|\s+&\s+|\s+and\s+", s):
        x = x.strip().upper()
        if re.fullmatch(r"[A-Z0-9]{1,15}", x):
            out.append(x)
    return out


def parse_title(title: str) -> tuple[list[str], list[str], int | None]:
    """Заголовок CMS -> (кому ставят метку, с кого снимают, день из «on DATE» или None)."""
    t = title or ""
    m = _ADD.search(t)
    add = tickers(m.group(1)) if m else []
    m = _REM.search(t)
    rem = tickers(m.group(1)) if m else []
    m = _ON.search(t)
    day = None
    if m:
        import calendar
        day = calendar.timegm((int(m.group(1)), int(m.group(2)), int(m.group(3)), 0, 0, 0))
    return add, rem, day


def intro_tickers(body: str) -> list[str]:
    """Тело вводной статьи (JSON-дерево строкой) -> «(TICKER)» между «applied to the following
    tokens» и «Binance will conduct»."""
    text = " ".join(re.findall(r'"text"\s*:\s*"([^"]*)"', body or ""))
    m = re.search(r"applied to the following tokens(.+?)Binance will conduct", text)
    return re.findall(r"\(([A-Z0-9]{1,15})\)", m.group(1)) if m else []


def intervals(articles: list[dict]) -> dict[str, list[dict]]:
    """Статьи -> {тикер: [{since, removed, title}]}: снятие в тот же день, что метка, идёт
    раньше неё (как tag_intervals замера); повторная метка после снятия — новый интервал."""
    marks: dict[str, list[tuple[int, int, str]]] = {}
    for a in articles:
        for t in [x for x in (a.get("added") or "").split(",") if x]:
            marks.setdefault(t, []).append((a["day"], 1, a.get("title") or ""))
        for t in [x for x in (a.get("removed") or "").split(",") if x]:
            marks.setdefault(t, []).append((a["day"], 0, a.get("title") or ""))
    out: dict[str, list[dict]] = {}
    for t, ms in marks.items():
        cur = None
        for day, kind, title in sorted(ms, key=lambda x: (x[0], x[1])):
            if kind and cur is None:
                cur = {"since": day, "removed": None, "title": title}
            elif not kind and cur is not None:
                cur["removed"] = day
                out.setdefault(t, []).append(cur)
                cur = None
        if cur is not None:
            out.setdefault(t, []).append(cur)
    return out


def same_coin(bn_price: float | None, ref_price: float | None, tol: float) -> bool | None:
    """Та же ли монета под тикером: цены в пределах tol — да; None — сравнить не с чем."""
    if not bn_price or not ref_price or bn_price <= 0 or ref_price <= 0:
        return None
    return abs(bn_price / ref_price - 1) <= tol


def check(view: dict | None, symbol: str, ref_price: float | None,
          tol: float = 0.2) -> dict | None:
    """Метка на тикере: None — не под меткой (или состояния нет). Иначе {since, title, bn_price,
    verdict (True/False/None — сравнение цен), same (False только при явном расхождении),
    note — для лога и карточки}."""
    t = ((view or {}).get("tagged") or {}).get((symbol or "").upper())
    if not t:
        return None
    getp = (view or {}).get("price")
    try:
        bn = getp(symbol.upper()) if getp else None
    except Exception:  # noqa: BLE001 — цена Binance — справка, её сбой = «не с чем сравнить»
        bn = None
    v = same_coin(bn, ref_price, tol)
    note = (f"тикер {symbol.upper()} под меткой Monitoring Binance с {ddmm(t['since'])}, но цена "
            f"Binance {bn:.6g} против {ref_price:.6g} (расхождение больше {tol * 100:.0f}%) — "
            f"другая монета, метку не применяю" if v is False else "")
    return {**t, "bn_price": bn, "verdict": v, "same": v is not False, "note": note}


# ---------------------------------------------------------------- база

def _connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def _sync_row(con) -> dict:
    r = con.execute("SELECT * FROM exchange_tag_sync WHERE tag=?", (TAG,)).fetchone()
    d = dict(r) if r else {"ok_ts": None, "try_ts": None, "err": "", "progress": "{}"}
    try:
        d["progress"] = json.loads(d.get("progress") or "{}")
    except ValueError:
        d["progress"] = {}
    return d


def _save_sync(con, row: dict) -> None:
    con.execute("INSERT OR REPLACE INTO exchange_tag_sync(tag, ok_ts, try_ts, err, progress) "
                "VALUES (?,?,?,?,?)", (TAG, row.get("ok_ts"), row.get("try_ts"),
                                       row.get("err") or "", json.dumps(row.get("progress") or {})))


def _rebuild(con, now: float) -> int:
    """exchange_tags заново из всех статей (пересчёт детерминирован: порядок статей не важен)."""
    arts = [dict(r) for r in con.execute("SELECT * FROM exchange_tag_articles")]
    iv = intervals(arts)
    con.execute("DELETE FROM exchange_tags WHERE tag=?", (TAG,))
    rows = [(TAG, t, x["since"], x["removed"], x["title"], now)
            for t, xs in iv.items() for x in xs]
    con.executemany("INSERT OR REPLACE INTO exchange_tags(tag, symbol, since_ts, removed_ts, "
                    "title, fetched_ts) VALUES (?,?,?,?,?,?)", rows)
    return sum(1 for _t, xs in iv.items() if xs[-1]["removed"] is None)


# ---------------------------------------------------------------- сеть

def fetch_page(http, catalog: int, page: int) -> list[dict] | None:
    """Страница каталога -> [{code, title, release_ts}] (пусто — страниц больше нет);
    None — сбой (сеть, 429 после повторов, чужой ответ): пустой ответ из-за сбоя нельзя
    принять за «статей больше нет»."""
    d = http.get_json(CMS, params={"type": 1, "catalogId": catalog, "pageNo": page,
                                   "pageSize": PAGE_SIZE},
                      headers={"User-Agent": "Mozilla/5.0"}, use_cache=False, retries=4)
    if not isinstance(d, dict) or str(d.get("code")) != "000000" or \
            not isinstance(d.get("data"), dict):
        return None
    cats = d["data"].get("catalogs") or []
    arts = (cats[0].get("articles") or []) if cats else []
    out = []
    for a in arts:
        try:
            out.append({"code": str(a["code"]), "title": a.get("title") or "",
                        "release_ts": float(a["releaseDate"]) / 1000.0})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_intro(http, code: str) -> list[str]:
    d = http.get_json(DETAIL, params={"articleCode": code},
                      headers={"User-Agent": "Mozilla/5.0"}, retries=4)
    body = ((d or {}).get("data") or {}).get("body") if isinstance(d, dict) else None
    return intro_tickers(body) if isinstance(body, str) else []


def fetch_prices(http) -> dict[str, float] | None:
    """Все пары Binance spot одним запросом -> {ТИКЕР: цена в USDT}; None — сбой."""
    d = http.get_json(PRICES, use_cache=False, retries=3)
    if not isinstance(d, list):
        return None
    out: dict[str, float] = {}
    for x in d:
        sym = (x or {}).get("symbol") or ""
        if sym.endswith("USDT"):
            try:
                out[sym[:-4]] = float(x["price"])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def refresh(db_path: str, http, now: float | None = None, *, budget_s: float = BUDGET_S,
            clock: Callable[[], float] = time.monotonic) -> dict:
    """Обход каталогов: полный (первый раз, с места недообхода) или дообход новых статей.
    -> {ok, err, pages, new}. Статьи с меткой пишутся по ходу; ok_ts — только если все
    каталоги дошли до конца; сбой — err и прежнее состояние (exchange_tags не трогается)."""
    now = now if now is not None else time.time()
    t0 = clock()
    con = _connect(db_path)
    pages = new = 0
    err = ""
    try:
        sync = _sync_row(con)
        prog = sync["progress"]
        known = {r[0] for r in con.execute("SELECT code FROM exchange_tag_articles")}
        for cat in CATALOGS:
            pg = prog.setdefault(str(cat), {"hwm": None, "page": 1, "done": False})
            inc = bool(pg.get("done"))
            p = 1 if inc else int(pg.get("page") or 1)
            stop_ts = (pg["hwm"] or 0) - OVERLAP if inc else SINCE_TS
            newest = pg.get("hwm") or 0
            while True:
                if p > MAX_PAGES:             # предохранитель: дальше не идём, обход закончен
                    pg.update(done=True, page=None, hwm=newest)
                    break
                arts = fetch_page(http, cat, p)
                if arts is None:
                    err = f"CMS Binance не ответил: каталог {cat}, стр. {p}"
                    break
                pages += 1
                for a in arts:
                    newest = max(newest, a["release_ts"])
                    if a["code"] in known or "Monitoring Tag" not in a["title"]:
                        continue
                    add, rem, day = parse_title(a["title"])
                    if a["title"].startswith(INTRO):
                        add = fetch_intro(http, a["code"]) or INTRO_Q12
                    if not (add or rem):
                        continue              # «Monitoring Tag» без тикеров (правила, FAQ)
                    con.execute("INSERT OR REPLACE INTO exchange_tag_articles(code, catalog, "
                                "release_ts, day, title, added, removed) VALUES (?,?,?,?,?,?,?)",
                                (a["code"], cat, a["release_ts"],
                                 day if day is not None else utc_day(a["release_ts"]),
                                 a["title"], ",".join(add), ",".join(rem)))
                    known.add(a["code"])
                    new += 1
                if not arts or arts[-1]["release_ts"] < stop_ts:
                    pg.update(done=True, page=None, hwm=newest)
                    break
                p += 1
                if not inc:                       # недообход продолжится с этой страницы;
                    pg.update(page=p, hwm=newest)  # hwm — самая новая с начала обхода
                if clock() - t0 > budget_s:
                    err = (f"обход CMS Binance не уложился в {budget_s:.0f} с: каталог {cat}, "
                           f"продолжу со стр. {p}")
                    break
            if inc and not err:                   # дообход: hwm сдвигается только целиком
                pg["hwm"] = newest
            if err:
                break
        sync.update(try_ts=now, err=err, progress=prog)
        if not err:
            sync["ok_ts"] = now
            _rebuild(con, now)
        _save_sync(con, sync)
        con.commit()
    except Exception as e:  # noqa: BLE001 — метки не должны ронять шаг; состояние — прежнее
        con.rollback()
        err = f"{type(e).__name__}: {e}"
        try:
            sync = _sync_row(con)
            sync.update(try_ts=now, err=err)
            _save_sync(con, sync)
            con.commit()
        except sqlite3.Error:
            pass
    finally:
        con.close()
    return {"ok": not err, "err": err, "pages": pages, "new": new}


# ---------------------------------------------------------------- состояние для потребителей

def view(db_path: str, now: float | None = None, max_age_days: float = 3,
         ro: bool = False) -> dict:
    """Состояние без сети: {status, tagged {ТИКЕР: {since, title}}, count, updated_ts, err,
    note}. status: none — статей нет (fail-open: не блокируем, сводка «⚠ метки Binance не
    загружены»); partial — полный обход не закончен (блок по известным); stale — последнее
    успешное обновление старше max_age_days (блок по последнему известному, «⚠ метки Binance
    от DD.MM»); ok. ro — только чтение (сводки): нет файла или таблиц — none."""
    now = now if now is not None else time.time()
    out: dict[str, Any] = {"status": "none", "tagged": {}, "count": 0, "updated_ts": None,
                           "err": "", "note": "⚠ метки Binance не загружены"}
    if ro:
        from ..benchmark import connect_ro
        con = connect_ro(db_path)
        if con is None:
            return out
        con.row_factory = sqlite3.Row
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"exchange_tags", "exchange_tag_sync", "exchange_tag_articles"} <= names:
            con.close()
            return out
    else:
        con = _connect(db_path)
    try:
        sync = _sync_row(con)
        n_art = con.execute("SELECT COUNT(*) FROM exchange_tag_articles").fetchone()[0]
        if not n_art:
            if sync.get("err"):
                out["err"] = sync["err"]
                out["note"] += f" ({sync['err']})"
            return out
        if sync.get("ok_ts"):
            rows = con.execute("SELECT symbol, since_ts, title FROM exchange_tags WHERE tag=? "
                               "AND removed_ts IS NULL", (TAG,)).fetchall()
            tagged = {r[0]: {"since": r[1], "title": r[2]} for r in rows}
        else:                                  # недообход: интервалы из того, что успели
            arts = [dict(r) for r in con.execute("SELECT * FROM exchange_tag_articles")]
            tagged = {t: {"since": xs[-1]["since"], "title": xs[-1]["title"]}
                      for t, xs in intervals(arts).items() if xs[-1]["removed"] is None}
    finally:
        con.close()
    ok_ts = sync.get("ok_ts")
    last_err = sync.get("err") if (sync.get("try_ts") or 0) >= (ok_ts or 0) else ""
    out.update(tagged=tagged, count=len(tagged), updated_ts=ok_ts, err=last_err or "")
    if not ok_ts:
        out.update(status="partial", note="⚠ метки Binance загружены не полностью")
    elif (utc_day(now) - utc_day(ok_ts)) / DAY > max_age_days:
        out.update(status="stale", note=f"⚠ метки Binance от {ddmm(ok_ts)}")
    else:
        out.update(status="ok", note="")
    return out


def due(db_path: str, now: float | None = None) -> bool:
    """Пора ли в сеть: сегодня (UTC) успешного обновления не было и неудачная попытка — не
    моложе RETRY_S (иначе шаг за шагом ждали бы 429 заново)."""
    now = now if now is not None else time.time()
    con = _connect(db_path)
    try:
        s = _sync_row(con)
    finally:
        con.close()
    if s.get("ok_ts") and utc_day(s["ok_ts"]) == utc_day(now):
        return False
    return not (s.get("err") and s.get("try_ts") and now - s["try_ts"] < RETRY_S)


def with_prices(v: dict, http) -> dict:
    """Ленивая цена Binance для сверки тёзок: один запрос на все пары — при первом вопросе."""
    cache: dict = {}

    def price(sym: str) -> float | None:
        if "all" not in cache:
            try:
                cache["all"] = fetch_prices(http) if http is not None else None
            except Exception:  # noqa: BLE001
                cache["all"] = None
        return (cache["all"] or {}).get((sym or "").upper())
    v["price"] = price
    return v


def ensure(db_path: str, http, now: float | None = None, max_age_days: float = 3,
           log: Callable[[str], None] = print) -> dict:
    """Шаги прогона: обновить, если пора (раз в UTC-день), и вернуть view с ленивой ценой
    Binance. Сбой сети — последнее известное состояние, view["err"] и строка в log."""
    now = now if now is not None else time.time()
    if due(db_path, now):
        r = refresh(db_path, http, now)
        log(f"[binance_tags] обновлено: страниц {r['pages']}, новых статей {r['new']}"
            if r["ok"] else f"[binance_tags] ⚠ {r['err']} — беру последнее известное")
    return with_prices(view(db_path, now, max_age_days), http)


def line(v: dict | None) -> str:
    """Одна строка о состоянии меток для лога и сводок."""
    if not v:
        return ""
    if v.get("status") in (None, "none"):
        return v.get("note") or "⚠ метки Binance не загружены"
    base = f"метки Monitoring Binance: под меткой {v['count']}"
    if v.get("status") == "ok":
        return base + f", обновлено {ddmm(v['updated_ts'])}" + (
            f" (последняя попытка: {v['err']})" if v.get("err") else "")
    return f"{v['note']} — {base} по последнему известному" + (
        f" ({v['err']})" if v.get("err") else "")
