"""Офлайн-проверка логики воронки на фикстурах (без сети).

Покрывает чистые функции: Stage 1 фильтры и Stage 2 интерпретацию анти-рага.
Запуск: python run.py selftest  (или python -m tests.selftest)
"""
from __future__ import annotations

import time

from scanner.config import load_config
from scanner.models import Candidate
from scanner.positions import PositionStore, risk_check
from scanner.stages.filters import apply_filters
from scanner.stages import antirug
from scanner.stages import exit as exit_stage
from scanner.stages.fundamentals import assess_valuation
from scanner.stages import zone
from scanner.stages.access import rf_gate
from scanner.stages.score import compute_score
from scanner.notify.telegram import format_weekly

_PASS = "\033[92mPASS\033[0m"
_FAIL = "\033[91mFAIL\033[0m"


def _check(name: str, cond: bool, failures: list[str]) -> None:
    print(f"  [{_PASS if cond else _FAIL}] {name}")
    if not cond:
        failures.append(name)


def test_filters(cfg, failures: list[str]) -> None:
    print("Stage 1 — фильтры:")
    cands = [
        # прошёл: живой альт, глубокая просадка -> пружина
        Candidate(source="coingecko", track="A", symbol="ARB", name="Arbitrum",
                  chain="arbitrum", address="0xabc", volume_24h=5_000_000,
                  drawdown_from_ath_pct=85.0),
        # стейбл -> reject
        Candidate(source="coingecko", track="A", symbol="USDT", name="Tether",
                  chain="ethereum", address="0xdef", volume_24h=9e9,
                  drawdown_from_ath_pct=1.0),
        # wrapped -> reject
        Candidate(source="coingecko", track="A", symbol="WBTC", name="Wrapped Bitcoin",
                  chain="ethereum", address="0x111", volume_24h=1e8),
        # тонкий объём -> reject
        Candidate(source="coingecko", track="A", symbol="DUST", name="Dust",
                  chain="ethereum", address="0x222", volume_24h=100.0,
                  drawdown_from_ath_pct=90.0),
        # нет контракта -> reject (анти-раг невозможен)
        Candidate(source="coingecko", track="A", symbol="NOADDR", name="NoAddr",
                  chain="", address="", volume_24h=1e6, drawdown_from_ath_pct=80.0),
        # Track B: слишком старый -> reject
        Candidate(source="geckoterminal", track="B", symbol="OLD", name="Old pool",
                  chain="base", address="0x333", liquidity_usd=500_000,
                  volume_24h=1e6, age_days=400.0),
        # Track B: ни объёма, ни ликвидности (DEXScreener-профиль) -> manual_review
        Candidate(source="dexscreener", track="B", symbol="BLIND", name="",
                  chain="solana", address="mint111"),
    ]
    passed, rejected = apply_filters(cands, cfg)
    psyms = {c.symbol for c in passed}

    _check("ARB прошёл", "ARB" in psyms, failures)
    _check("ARB помечен как пружина", any(c.symbol == "ARB" and c.spring_prefilter for c in passed), failures)
    _check("USDT отклонён (стейбл)", "USDT" not in psyms, failures)
    _check("WBTC отклонён (wrapped)", "WBTC" not in psyms, failures)
    _check("DUST отклонён (объём)", "DUST" not in psyms, failures)
    _check("NOADDR отклонён (нет контракта)", "NOADDR" not in psyms, failures)
    _check("OLD отклонён (возраст)", "OLD" not in psyms, failures)
    blind = next(c for c in rejected if c.symbol == "BLIND")
    _check("BLIND (без vol/liq) отклонён + manual_review",
           blind.manual_review and "no_tradability_data" in blind.flags, failures)
    _check("итог: ровно 1 прошёл", len(passed) == 1, failures)


def test_track_q(cfg, failures: list[str]) -> None:
    print("Трек Q — монеты без контракта через гейт качества:")
    import json as _json
    import os
    import tempfile
    import time as _t
    from scanner.pipeline import stage2_antirug
    from scanner.quality import load_quality
    qual = {"BTC": {"sym": "BTC", "mcap": 1.6e12, "fails": []},
            "FAKE": {"sym": "FAKE", "mcap": 5e9, "fails": []}}
    mk = lambda sym, mcap, **kw: Candidate(source="coingecko", track="A", symbol=sym,  # noqa: E731
                                           coin_id=sym.lower(), volume_24h=1e9,
                                           market_cap=mcap, **kw)
    cands = [mk("BTC", 1.65e12, drawdown_from_ath_pct=33.0),
             mk("BTC", 1.65e12),                        # тёзка/дубль — только первое вхождение
             mk("XRP", 1.5e11),                         # без контракта и не в срезе
             mk("FAKE", 4e7),                           # тикер совпал, капа в 125 раз меньше
             mk("ARB", 1e9, chain="arbitrum", address="0xabc")]
    passed, rejected = apply_filters(cands, cfg, qual)
    q = [c for c in passed if c.track == "Q"]
    _check("BTC без контракта из среза -> трек Q", len(q) == 1 and q[0].symbol == "BTC"
           and "no_contract_quality_gate" in q[0].flags, failures)
    _check("дубль тикера, XRP вне среза, тёзка по капе -> отклонены",
           sorted(c.symbol for c in rejected) == ["BTC", "FAKE", "XRP"], failures)
    _check("монета с контрактом — обычный Track A", any(c.symbol == "ARB" and c.track == "A"
                                                          for c in passed), failures)
    _check("без среза поведение прежнее (BTC отклонён)",
           not any(c.track == "Q" for c in apply_filters([mk("BTC", 1.65e12)], cfg)[0]), failures)
    wl, rj = stage2_antirug(cfg, None, q)               # сети нет: Q анти-раг не вызывает
    _check("Stage 2: трек Q идёт в watchlist без запроса анти-рага", len(wl) == 1 and not rj,
           failures)
    wl[0].zone, wl[0].rf_venue = "ПРУЖИНА/ДНО", "Bybit spot"
    s, conf, brk = compute_score(wl[0], cfg)
    _check("скор Q считается (safety 10 — гейт вместо контракта)",
           brk["safety"]["sub"] == 10.0 and s > 0, failures)

    import copy
    from scanner.config import Config
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    day = _t.strftime("%Y-%m-%d", _t.localtime())
    _json.dump({"date": day, "rows": [{"sym": "BTC", "mcap": 1, "fails": []},
                                      {"sym": "DOGE", "mcap": 1, "fails": ["мем"]}]}, tmp)
    tmp.close()
    d = copy.deepcopy(cfg._d)
    d["track_q"].update(source=tmp.name, live_source="nope/none_live.json", max_age_days=30)
    fresh = load_quality(Config(d))
    stale = load_quality(Config(d), now=_t.time() + 60 * 86400)
    os.unlink(tmp.name)
    _check("срез: берутся только прошедшие (BTC, не DOGE)",
           fresh["ok"] and list(fresh["by_sym"]) == ["BTC"], failures)
    _check("срез старше max_age_days -> пусто + подсказка обновить",
           not stale["ok"] and not stale["by_sym"] and "обнови" in stale["note"], failures)
    d["track_q"]["source"] = "nope/none.json"
    _check("нет файла -> пусто, без падения", not load_quality(Config(d))["ok"], failures)

    # Авто-paper: близнец S (стоп −50%) — только монетам фильтра качества.
    from scanner.pipeline import _open_paper_positions
    spring = lambda sym, mcap, track="A": Candidate(  # noqa: E731
        source="t", track=track, symbol=sym, coin_id=sym.lower(), price_usd=2.0,
        market_cap=mcap, zone="ПРУЖИНА/ДНО", score=80.0, confidence=0.9)
    wl = [spring("BTC", 1.6e12, "Q"), spring("LINK", 9e9), spring("JUNK", 5e8),
          spring("FAKE", 4e7)]                              # FAKE: тикер в срезе, капа нет
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()
    saved = cfg["output"]["db_path"]
    cfg["output"]["db_path"] = tmp_db.name
    try:
        n = _open_paper_positions(cfg, wl, {**qual, "LINK": {"sym": "LINK", "mcap": 1e10}})
        ps = PositionStore(tmp_db.name)
        rows = ps.all_positions()
        ps.close_db()
    finally:
        cfg["output"]["db_path"] = saved
        os.unlink(tmp_db.name)
    by = {}
    for r in rows:
        by.setdefault(r["variant"], []).append(r["symbol"])
    _check("paper: 4 позиции A и 4 близнеца B", n == 4 and len(by.get("A", [])) == 4
           and len(by.get("B", [])) == 4, failures)
    _check("paper: близнецы S — только BTC (трек Q) и LINK (срез + капа)",
           sorted(by.get("S", [])) == ["BTC", "LINK"], failures)


def test_quality_refresh(cfg, failures: list[str]) -> None:
    print("Трек Q — свежий срез, автообновление, пометки сводки:")
    import copy
    import json as _json
    import subprocess
    import tempfile
    import types
    from datetime import datetime as _dt
    from pathlib import Path as _P
    from scanner import quality as q
    from scanner.config import Config
    from scanner.notify import telegram as tg
    rows = [{"sym": "BTC", "mcap": 1, "fails": []}, {"sym": "DOGE", "mcap": 1, "fails": ["мем"]}]
    # возраст — от локальной полуночи даты среза: смещения в секундах не зависят от пояса/DST
    at = lambda days: _dt(2026, 9, 29).timestamp() + days * 86400  # noqa: E731
    now = at(26.4)                                       # срезу от 29.09 — 26.4 дн. (25.10)
    with tempfile.TemporaryDirectory() as tmp:
        live, src = _P(tmp) / "data" / "live.json", _P(tmp) / "src.json"
        d = copy.deepcopy(cfg._d)
        d["track_q"].update(source=str(src), live_source=str(live), max_age_days=30,
                            refresh_after_days=25)
        c = Config(d)

        def put(p, date, rows_=rows):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_json.dumps({"date": date, "rows": rows_}), encoding="utf-8")

        put(src, "2026-09-29")
        s1 = q.fresh_slice(c, now)
        put(live, "2026-10-20")
        s2 = q.fresh_slice(c, now)
        put(live, "2026-09-29")
        s3 = q.fresh_slice(c, now)
        live.write_text("{битый", encoding="utf-8")
        s4 = q.fresh_slice(c, now)
        put(live, "")
        s5 = q.fresh_slice(c, now)
        _check("резолвер: live нет -> закоммиченный (src source, 26.4 дн.)",
               s1["ok"] and s1["src"] == "source" and abs(s1["age_days"] - 26.4) < 1e-6, failures)
        _check("резолвер: live свежее -> live", s2["src"] == "live_source"
               and s2["date"] == "2026-10-20", failures)
        _check("резолвер: даты равны -> live", s3["src"] == "live_source", failures)
        _check("резолвер: live битый или без даты -> закоммиченный, причина видна",
               s4["src"] == "source" and s5["src"] == "source"
               and any("повреждён" in t["note"] for t in s4["tried"])
               and any("без даты" in t["note"] for t in s5["tried"]), failures)
        src.unlink()
        live.unlink()
        s6 = q.fresh_slice(c, now)
        _check("резолвер: обоих нет -> ok False, в причине оба файла",
               not s6["ok"] and s6["note"].count("нет файла") == 2, failures)

        # Пороги: обновление с refresh_after_days (≥), трек пуст после max_age_days (>).
        put(src, "2026-09-29")
        due = [q.refresh_due(q.fresh_slice(c, at(x)), c) for x in (24.9, 25.0)]
        _check("обновлять: 24.9 дн. — рано, 25 — пора, среза нет — пора",
               due == [False, True] and q.refresh_due(s6, c), failures)
        lq = [q.load_quality(c, at(x)) for x in (30.0, 30.1)]
        _check("трек: 30 дн. ещё жив (BTC), 30.1 — пуст + «обнови»",
               lq[0]["ok"] and list(lq[0]["by_sym"]) == ["BTC"] and not lq[1]["ok"]
               and "обнови" in lq[1]["note"], failures)

        # Автообновление: подпроцесс подменён — сети нет.
        calls = []

        def runner(write=None, rc=0, exc=None):
            def _run(cmd, **kw):
                calls.append((cmd, kw))
                if exc:
                    raise exc
                if write:
                    put(live, write)
                return types.SimpleNamespace(returncode=rc)
            return _run

        early = q.refresh_if_due(c, now=at(10), runner=runner("2026-10-09"))
        _check("не пора (10 дн.) -> код 0, подпроцесс не запускался, дата следующего",
               early[0] == 0 and not calls and "рано" in early[1] and "24.10" in early[1],
               failures)
        bad = q.refresh_if_due(c, now=now, runner=runner(rc=1))
        _check("пора, quality_screen упал -> код 1, остаётся прежний срез",
               bad[0] == 1 and "кодом 1" in bad[1] and q.fresh_slice(c, now)["src"] == "source",
               failures)
        cmd, kw = calls[-1]
        _check("подпроцесс: quality_screen.py --out <live_source>, таймаут 30 мин, UTF-8",
               cmd[-3].endswith("quality_screen.py") and cmd[-2:] == ["--out", str(live)]
               and kw["timeout"] == 1800 and kw["env"].get("PYTHONIOENCODING") == "utf-8",
               failures)
        hung = q.refresh_if_due(c, now=now, runner=runner(
            exc=subprocess.TimeoutExpired("quality_screen.py", 1800)))
        _check("подпроцесс завис -> код 1 «не уложился в 30 мин»",
               hung[0] == 1 and "не уложился в 30 мин" in hung[1], failures)
        silent = q.refresh_if_due(c, now=now, runner=runner())
        _check("код 0, но файл не появился -> код 1 «не обновился»",
               silent[0] == 1 and "не обновился" in silent[1], failures)
        good = q.refresh_if_due(c, now=now, runner=runner("2026-10-25"))
        after = q.fresh_slice(c, now)
        _check("обновил -> код 0, дальше читается live от сегодня",
               good[0] == 0 and after["src"] == "live_source" and after["date"] == "2026-10-25",
               failures)

        # Атомарная запись: сбой посреди записи не портит прежний срез и не оставляет хвост.
        before = live.read_text(encoding="utf-8")
        try:
            q.write_slice(live, {"date": "2026-10-26", "rows": [object()]})
            raised = False
        except TypeError:
            raised = True
        _check("write_slice: сбой -> прежний файл цел, .tmp нет",
               raised and live.read_text(encoding="utf-8") == before
               and not list(live.parent.glob("*.tmp")), failures)
        q.write_slice(_P(tmp) / "new" / "x.json", {"date": "2026-10-26", "rows": rows})
        _check("write_slice: каталог создаётся, срез читается",
               q.read_slice(_P(tmp) / "new" / "x.json", now)["ok"], failures)

        # brief_state: срез трека Q и код бэкапа — без сети.
        from scanner.db import Store
        from scanner.notify.deliver import brief_state
        put(live, "2026-09-29")
        d["output"] = {"db_path": str(_P(tmp) / "t.db"), "watchlist_json": str(_P(tmp) / "w.json")}
        Store(d["output"]["db_path"]).close()
        b0 = brief_state(Config(d), now=now)
        b1 = brief_state(Config(d), now=now, backup_exit=1)
        b3 = brief_state(Config(d), now=now, backup_exit=3)
        d["track_q"]["enabled"] = False
        boff = brief_state(Config(d), now=now, backup_exit=0)
    _check("brief_state: срез трека Q (дата, возраст), бэкап не запускали -> молчим",
           b0["track_q"]["date"] == "2026-09-29" and abs(b0["track_q"]["age_days"] - 26.4) < 1e-6
           and b0["backup"] is None, failures)
    _check("brief_state: backup-exit 1 -> fail, 3 -> send_fail, 0 -> нет пометки",
           b1["backup"] == "fail" and b3["backup"] == "send_fail" and boff["backup"] is None,
           failures)
    _check("brief_state: трек Q выключен в конфиге -> пометки о срезе нет",
           boff["track_q"] is None, failures)

    # Тексты пометок (DD.MM — по дате среза).
    tq = lambda age, ok=True: {"ok": ok, "date": "2026-09-29", "age_days": age}  # noqa: E731
    _check("пометка: свежий (10 дн.) или трек выключен в конфиге -> пусто",
           tg.track_q_note(tq(10), cfg) == "" and tg.track_q_note(None, cfg) == "", failures)
    _check("пометка: 26 дн. -> автообновление не прошло, выключится 29.10",
           tg.track_q_note(tq(26.4), cfg)
           == "⚠ срез трека Q от 29.09: автообновление не прошло, трек выключится 29.10", failures)
    _check("пометка: 31 дн. -> трек Q выключен, старше 30 дн.",
           tg.track_q_note(tq(31), cfg) == "⚠ трек Q выключен: срез от 29.09 старше 30 дн.",
           failures)
    _check("пометка: среза нет -> трек Q выключен",
           tg.track_q_note({"ok": False, "date": "", "age_days": None}, cfg)
           .startswith("⚠ трек Q выключен: среза нет"), failures)

    # Сводка: бэкап — в строке статуса, срез — служебной строкой после подвала.
    st = {"scan": {"ran": True, "ok": True, "elapsed_min": 20.0, "watchlist": 80},
          "watch_ok": True, "market": {}, "new": [], "muted": [], "near": [], "positions": [],
          "signals_today": [], "unavailable": ["onchain"], "dev_github": "59 из 80"}
    day = _dt(2026, 10, 25, 10, 0).timestamp()
    ok_b = tg.format_brief({**st, "track_q": tq(10)}, cfg, now=day)
    fail_b = tg.format_brief({**st, "backup": "fail", "track_q": tq(26.4)}, cfg, now=day)
    send_b = tg.format_brief({**st, "backup": "send_fail"}, cfg, now=day)
    _check("сводка: всё в порядке -> ни «бэкап», ни «трек Q»",
           "бэкап" not in ok_b and "трек Q" not in ok_b, failures)
    _check("сводка: бэкап упал -> «⚠ бэкап не сделан» в первой строке",
           "⚠ бэкап не сделан" in fail_b.split("\n")[0], failures)
    _check("сводка: копия не ушла -> «⚠ бэкап не ушёл в Telegram» в первой строке",
           "⚠ бэкап не ушёл в Telegram" in send_b.split("\n")[0]
           and "не сделан" not in send_b, failures)
    _check("сводка: старый срез -> строка после подвала с on-chain",
           fail_b.split("\n")[-2].endswith("on-chain недоступен</i>")
           and fail_b.split("\n")[-1].startswith("⚠ срез трека Q от 29.09"), failures)


def test_backup(cfg, failures: list[str]) -> None:
    print("Бэкап базы — копия, gzip, integrity, восстановление, ротация, неделя в Telegram:")
    import copy
    import gzip
    import sqlite3
    import tempfile
    from pathlib import Path as _P
    from scanner import backup as bk
    from scanner.config import Config
    from scanner.db import Store
    from scanner.notify import telegram as tg
    t0 = 1791180000.0                                    # 2026-10-05 ~10:00 по Самаре
    with tempfile.TemporaryDirectory() as tmp:
        db, out = _P(tmp) / "scanner.db", _P(tmp) / "backups"
        st = Store(str(db))
        st.finish_run(st.new_run("x"), 10, 5, 3, {"elapsed_sec": 60})
        st.close()
        ps = PositionStore(str(db))
        for sym in ("GRAM", "LUNC"):
            ps.add(sym, 1.0, 100, coin_id=sym.lower(), paper=True)
        ps.close_db()
        info = bk.make_backup(db, out, keep=3, now=t0)
        name = info["path"].name
        restored = _P(tmp) / "restored.db"
        with gzip.open(info["path"], "rb") as fi:
            restored.write_bytes(fi.read())
        con = sqlite3.connect(restored)
        integ = con.execute("PRAGMA integrity_check").fetchone()[0]
        n_pos = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        con.close()
        _check("копия: scanner-YYYYMMDD-HHMM.db.gz, без временных файлов рядом",
               bk.NAME_RE.match(name) is not None and name.startswith("scanner-2026100")
               and [p.name for p in out.iterdir()] == [name], failures)
        _check("восстановление: gunzip -> integrity ok, 2 позиции, размер как у базы",
               integ == "ok" and n_pos == 2 and restored.stat().st_size == info["db_size"],
               failures)
        _check("строка лога: backup ok, размер, сколько хранится",
               bk.ok_line(info).startswith("backup ok: ") and "хранится 1 из 3" in bk.ok_line(info),
               failures)

        # Ротация: 5 копий при keep=3 -> 3 самые новые; чужие файлы не трогаются.
        (out / "notes.txt").write_text("x", encoding="utf-8")
        (out / "scanner-manual.db.gz").write_bytes(b"x")
        infos = [bk.make_backup(db, out, keep=3, now=t0 + i * 3600) for i in range(1, 5)]
        left = [p.name for p in bk.list_backups(out)]
        _check("ротация: осталось 3 самых новых",
               left == [i["path"].name for i in reversed(infos[-3:])]
               and infos[-1]["kept"] == 3 and len(infos[-1]["removed"]) == 1, failures)
        _check("ротация: чужие файлы в каталоге целы",
               (out / "notes.txt").exists() and (out / "scanner-manual.db.gz").exists(), failures)

        # Сбои: код ≠ 0 (исключение), старые копии и каталог не портятся.
        errs = []
        broken = _P(tmp) / "broken.db"
        broken.write_bytes(b"not a database " * 100)
        for args, kw in (((_P(tmp) / "none.db", out), {"keep": 3}), ((broken, out), {"keep": 3}),
                         ((db, out), {"keep": 0})):
            try:
                bk.make_backup(*args, now=t0 + 9 * 3600, **kw)
                errs.append(None)
            except Exception as e:  # noqa: BLE001
                errs.append(type(e).__name__)
        _check("сбой: нет базы / не база / keep=0 -> исключение",
               errs[0] == "BackupError" and errs[1] == "DatabaseError"
               and errs[2] == "BackupError", failures)
        _check("сбой: битая копия не записана, ротация не тронула прежние 3, хвостов нет",
               [p.name for p in bk.list_backups(out)] == left
               and not [p for p in out.iterdir() if p.name.startswith(".")], failures)

        # Недельная отправка: отправитель подменён — сети нет.
        d = copy.deepcopy(cfg._d)
        d["output"] = {"db_path": str(db), "watchlist_json": str(_P(tmp) / "w.json")}
        d["api_keys"].update(telegram_token="T", telegram_chat_id="C")
        c = Config(d)
        sent, msgs = [], []

        def doc(ok=True):
            def _send(token, chat, data, filename, caption, silent=False):
                sent.append({"data": data, "filename": filename, "caption": caption,
                             "silent": silent})
                return ok
            return _send

        def msg(token, chat, text, silent=False):
            msgs.append({"text": text, "silent": silent})
            return True

        def flags():
            p = PositionStore(str(db))
            n = p.conn.execute("SELECT COUNT(*) FROM position_events WHERE position_id=0 "
                               "AND type=?", (bk.FLAG,)).fetchone()[0]
            p.close_db()
            return n

        last = infos[-1]
        now = time.time()
        r1 = bk.send_weekly(c, last, notify=True, now=now, send_document=doc())
        _check("неделя: первая отправка -> документом, без звука, отметка записана",
               r1[0] == 0 and len(sent) == 1 and sent[0]["silent"] is True
               and sent[0]["filename"] == last["path"].name
               and sent[0]["data"] == last["path"].read_bytes() and flags() == 1, failures)
        cap = sent[0]["caption"]
        _check("подпись: дата, размер, как восстановить (gunzip → scanner.db)",
               "Бэкап базы сканера" in cap and last["path"].name in cap and " КБ" in cap
               and "gunzip -c" in cap and "scanner.db" in cap and not cap.startswith("🧪"),
               failures)
        r2 = bk.send_weekly(c, last, notify=True, now=now, send_document=doc())
        _check("неделя: уже отправляли на этой неделе -> пропуск",
               r2[0] == 0 and len(sent) == 1 and "уже" in r2[1], failures)
        r3 = bk.send_weekly(c, last, notify=True, test=True, now=now, send_document=doc())
        _check("--test: шлёт несмотря на отметку, «🧪 ТЕСТ», отметку не пишет",
               r3[0] == 0 and len(sent) == 2 and sent[1]["caption"].startswith("🧪 ТЕСТ")
               and flags() == 1, failures)
        week = now + 7 * 86400
        r4 = bk.send_weekly(c, last, notify=False, now=week, send_document=doc())
        _check("новая неделя без --notify -> «пора», ничего не шлёт и не отмечает",
               r4[0] == 0 and len(sent) == 2 and "без --notify" in r4[1] and flags() == 1,
               failures)
        r5 = bk.send_weekly(c, last, notify=True, now=week, send_document=doc(ok=False))
        _check("Telegram не принял -> код 3 (сводка: «не ушёл»), отметки нет — повтор завтра",
               r5[0] == bk.EXIT_SEND_FAILED == 3 and flags() == 1, failures)
        r6 = bk.send_weekly(c, last, notify=True, now=week, max_bytes=10,
                            send_document=doc(), send_message=msg)
        _check("больше лимита -> файл не шлём, предупреждение текстом без звука",
               r6[0] == 0 and len(sent) == 3 and len(msgs) == 1 and msgs[0]["silent"]
               and "не отправлен" in msgs[0]["text"] and flags() == 2, failures)

    # Транспорт sendDocument: multipart с файлом и disable_notification (сеть подменена).
    posts = []
    real_post = tg._post
    tg._post = lambda token, method, data, ctype, timeout=30: (
        posts.append((method, data, ctype, timeout)) or {"ok": True})
    try:
        ok = tg.send_document("T", "C", b"\x1f\x8bGZ", "scanner-x.db.gz", "<b>cap</b>",
                              silent=True)
        no_token = tg.send_document("", "C", b"x", "a.gz", "cap")
    finally:
        tg._post = real_post
    method, body, ctype, tmo = posts[0]
    _check("sendDocument: файл, подпись, без звука, длинный таймаут",
           ok and method == "sendDocument" and b'name="document"; filename="scanner-x.db.gz"'
           in body and b"\x1f\x8bGZ" in body and b'name="disable_notification"' in body
           and ctype.startswith("multipart/form-data") and tmo >= 300, failures)
    _check("sendDocument: нет token -> не шлёт", no_token is False and len(posts) == 1, failures)


def test_antirug_evm(cfg, failures: list[str]) -> None:
    print("Stage 2 — анти-раг (EVM):")

    clean = {"is_honeypot": "0", "buy_tax": "0.03", "sell_tax": "0.03",
             "is_open_source": "1", "cannot_sell_all": "0",
             "holders": [{"percent": "0.05"}, {"percent": "0.04"}]}
    ok, reasons, _ = antirug.evaluate_goplus_evm(clean, cfg)
    _check("чистый токен проходит", ok and not reasons, failures)

    honey = {"is_honeypot": "1", "buy_tax": "0", "sell_tax": "0", "is_open_source": "1"}
    ok, reasons, _ = antirug.evaluate_goplus_evm(honey, cfg)
    _check("honeypot отклонён", not ok and "honeypot" in reasons, failures)

    taxed = {"is_honeypot": "0", "buy_tax": "0.40", "sell_tax": "0.50", "is_open_source": "1"}
    ok, reasons, _ = antirug.evaluate_goplus_evm(taxed, cfg)
    _check("грабительский налог отклонён", not ok and any("sell_tax" in r for r in reasons), failures)

    concentrated = {"is_honeypot": "0", "buy_tax": "0", "sell_tax": "0", "is_open_source": "1",
                    "holders": [{"percent": "0.85"}]}
    ok, reasons, _ = antirug.evaluate_goplus_evm(concentrated, cfg)
    _check("концентрация холдеров отклонена", not ok and any("top10" in r for r in reasons), failures)

    unverified = {"is_honeypot": "0", "buy_tax": "0", "sell_tax": "0", "is_open_source": "0"}
    ok, reasons, _ = antirug.evaluate_goplus_evm(unverified, cfg)
    _check("неверифицированный контракт отклонён", not ok and "not_open_source" in reasons, failures)

    mintable = {"is_honeypot": "0", "buy_tax": "0", "sell_tax": "0",
                "is_open_source": "1", "is_mintable": "1"}
    ok, reasons, flags = antirug.evaluate_goplus_evm(mintable, cfg)
    _check("mintable проходит но флагается (дефолт)", ok and "mintable" in flags, failures)


def test_honeypot_is(cfg, failures: list[str]) -> None:
    print("Stage 2 — honeypot.is (вторичное подтверждение):")
    ok, _ = antirug.evaluate_honeypot_is({"honeypotResult": {"isHoneypot": False}})
    _check("не-ханипот проходит", ok, failures)
    ok, reasons = antirug.evaluate_honeypot_is({"honeypotResult": {"isHoneypot": True}})
    _check("ханипот отклонён", not ok and reasons, failures)


def test_fundamentals(cfg, failures: list[str]) -> None:
    print("Stage 3 — фундамент/оценка:")

    # Недооценён к сектору: MC/TVL сильно ниже медианы.
    m, notes, flags = assess_valuation(
        market_cap=100e6, fdv=120e6, tvl=400e6,  # MC/TVL=0.25
        category="Dexes", category_median_mc_tvl=1.0, peer_count=50, cfg=cfg)
    _check("MC/TVL посчитан", m["mc_tvl"] == 0.25, failures)
    _check("недооценён к сектору флаг", "undervalued_vs_sector" in flags, failures)
    _check("умеренная дилюция (FDV/MC=1.2)", "high_dilution" not in flags, failures)

    # Переоценён + высокая дилюция.
    m, notes, flags = assess_valuation(
        market_cap=100e6, fdv=500e6, tvl=20e6,   # MC/TVL=5.0, FDV/MC=5
        category="Dexes", category_median_mc_tvl=1.0, peer_count=50, cfg=cfg)
    _check("переоценён к сектору флаг", "overvalued_vs_sector" in flags, failures)
    _check("высокая дилюция флаг (FDV/MC=5)", "high_dilution" in flags, failures)

    # Нет TVL (не DeFi) — честный «нет данных», без падения.
    m, notes, flags = assess_valuation(
        market_cap=100e6, fdv=100e6, tvl=None,
        category="Unknown", category_median_mc_tvl=None, peer_count=0, cfg=cfg)
    _check("нет TVL -> mc_tvl None", m["mc_tvl"] is None, failures)
    _check("есть пометка про unlocks pro-only", any("разблокировки" in n for n in notes), failures)

    # Мало пиров -> без вывода недо/пере.
    m, notes, flags = assess_valuation(
        market_cap=100e6, fdv=100e6, tvl=50e6,
        category="Niche", category_median_mc_tvl=0.5, peer_count=2, cfg=cfg)
    _check("мало пиров -> нет флага оценки", not ({"undervalued_vs_sector", "overvalued_vs_sector"} & set(flags)), failures)


def test_zone(cfg, failures: list[str]) -> None:
    print("Stage 4 — детектор зоны:")
    z = cfg["stage4_zone"]
    rec, sma = z["recent_days"], z["sma_days"]

    # Пружина: рваный обвал 100→20 (высокая vol), затем 30 дней ровно у дна (сжатие).
    spring = [100 - 80 * i / 149 + (3 if i % 2 else -3) for i in range(150)] + [20.0] * 30
    ind = zone.compute_indicators(spring, rec, sma)
    zl, _ = zone.classify_zone(ind, 85.0, cfg)
    _check("глубокое дно + сжатие -> ПРУЖИНА/ДНО", zl == "ПРУЖИНА/ДНО", failures)

    # Пик: рост с ускорением в конце, у максимума, около ATH.
    peak = [20 + 30 * i / 149 for i in range(150)] + [50 + 50 * i / 29 for i in range(30)]
    ind = zone.compute_indicators(peak, rec, sma)
    zl, _ = zone.classify_zone(ind, 3.0, cfg)
    _check("около ATH + над SMA + верх диапазона -> ПИК", zl == "ПИК", failures)

    # Падающий нож: плоско, затем резкий обвал -30% за последние 30 дней.
    knife = [100.0] * 150 + [100 - 30 * i / 29 for i in range(30)]
    ind = zone.compute_indicators(knife, rec, sma)
    zl, _ = zone.classify_zone(ind, 40.0, cfg)
    _check("активный обвал -> ПАДАЮЩИЙ_НОЖ", zl == "ПАДАЮЩИЙ_НОЖ", failures)

    # Нет истории -> '?'
    zl, _ = zone.classify_zone(None, 80.0, cfg)
    _check("нет истории -> ?", zl == "?", failures)

    # Гейт days_since_low: конфигурируемый, ПО УМОЛЧАНИЮ ВЫКЛЮЧЕН (0) — walk-forward
    # (detector_study) показал, что фильтр «лоу давно не обновлялся» ухудшает форвард.
    # Проверяем механику гейта с явным порогом, затем дефолт.
    bleed = ([100 - 60 * i / 149 + (4 if i % 2 else -4) for i in range(150)]  # рваный спад до ~40
             + [40 - 4 * i / 29 for i in range(30)])                          # тихое сползание к 36
    ind = zone.compute_indicators(bleed, rec, sma)
    _check("истечение: лоу обновлён недавно (days_since_low<14)",
           ind["days_since_low"] < 14, failures)
    saved = cfg["stage4_zone"].get("spring_min_days_since_low", 0)
    cfg["stage4_zone"]["spring_min_days_since_low"] = 14      # включаем гейт явно
    zl, _ = zone.classify_zone(ind, 85.0, cfg)
    _check("гейт=14: свежий лоу блокирует пружину", zl != "ПРУЖИНА/ДНО", failures)
    cfg["stage4_zone"]["spring_min_days_since_low"] = saved   # дефолт (выключен)
    zl, _ = zone.classify_zone(ind, 85.0, cfg)
    _check("гейт выключен (дефолт по данным) -> пружина допускается",
           zl == "ПРУЖИНА/ДНО", failures)

    # Пружина с усыханием объёма: сигнал накопления попадает в описание.
    spring2 = [100 - 80 * i / 149 + (3 if i % 2 else -3) for i in range(150)] + [20.0] * 30
    vols = [1e6] * 150 + [3e5] * 30   # объём усох втрое у дна
    ind = zone.compute_indicators(spring2, rec, sma, vols)
    _check("vol_trend посчитан и <0.6", ind["vol_trend"] is not None and ind["vol_trend"] < 0.6, failures)
    zl, sigs = zone.classify_zone(ind, 85.0, cfg)
    _check("пружина + dry-up -> сигнал накопления",
           zl == "ПРУЖИНА/ДНО" and any("усох" in s for s in sigs), failures)

    # Без volumes индикаторы работают как раньше (vol_trend=None, зона не ломается).
    ind = zone.compute_indicators(spring2, rec, sma)
    _check("без объёмов vol_trend=None, зона работает",
           ind["vol_trend"] is None and zone.classify_zone(ind, 85.0, cfg)[0] == "ПРУЖИНА/ДНО",
           failures)


def test_closed_daily(cfg, failures: list[str]) -> None:
    print("closed_daily — отбрасывание live-тика:")
    from scanner.sources.coingecko import closed_daily
    day = 86400
    # два закрытия 00:00 UTC + live-тик 18:37 -> должна отвалиться последняя точка
    live = {"ts": [10 * day, 11 * day, 11 * day + 67020],
            "prices": [100.0, 101.0, 95.0], "volumes": [1, 2, 3]}
    out = closed_daily(live)
    _check("live-тик отброшен (осталось 2 закрытия)", out["prices"] == [100.0, 101.0], failures)
    # все точки выровнены на полночь -> ничего не трогаем
    clean = {"ts": [10 * day, 11 * day], "prices": [100.0, 101.0], "volumes": [1, 2]}
    _check("чистые закрытия не трогаем", closed_daily(clean)["prices"] == [100.0, 101.0], failures)


def test_retry_wait(cfg, failures: list[str]) -> None:
    print("http.retry_wait — пауза перед повтором:")
    import tempfile
    from scanner.http import retry_wait
    _check("429 без Retry-After ждёт окно 60 с", retry_wait(429, None, 2.0) == 60.0, failures)
    _check("429 с Retry-After: 30 -> 31 с", retry_wait(429, "30", 2.0) == 31.0, failures)
    _check("429 Retry-After сверху ограничен 90 с", retry_wait(429, "600", 2.0) == 90.0, failures)
    _check("429 Retry-After в виде даты -> окно",
           retry_wait(429, "Wed, 30 Sep 2026 09:00:00 GMT", 2.0) == 60.0, failures)
    _check("5xx — обычный backoff", retry_wait(503, None, 4.0) == 4.0, failures)

    # Лимит CoinGecko: конфиг под demo-ключ, без ключа — coingecko_no_key_per_min.
    import copy
    from scanner.config import Config
    from scanner.pipeline import _make_http
    data = copy.deepcopy(cfg._d)
    data["http"]["cache_dir"] = tempfile.mkdtemp()
    data["http"]["rate_limits_per_min"]["api.coingecko.com"] = 25
    data["http"]["coingecko_no_key_per_min"] = 10
    data.setdefault("api_keys", {})["coingecko_demo"] = "CG-x"
    with_key = _make_http(Config(data))._min_interval["api.coingecko.com"]
    data["api_keys"]["coingecko_demo"] = ""
    no_key = _make_http(Config(data))._min_interval["api.coingecko.com"]
    _check("с demo-ключом CG 25/мин (2.4 с)", abs(with_key - 2.4) < 1e-9, failures)
    _check("без ключа CG 10/мин (6 с)", abs(no_key - 6.0) < 1e-9, failures)


def test_regime(cfg, failures: list[str]) -> None:
    print("Режим BTC (контекст, не блокер):")
    from scanner.regime import classify_regime, rs_vs_btc

    bull = [50000 + 200 * i for i in range(120)]          # устойчивый рост
    r = classify_regime(bull)
    _check("рост + выше SMA -> BULL", r["regime"] == "BULL", failures)

    bear = [100000 - 400 * i for i in range(120)]         # устойчивое снижение
    r = classify_regime(bear)
    _check("снижение + ниже SMA -> BEAR", r["regime"] == "BEAR", failures)

    r = classify_regime([50000.0] * 10)
    _check("мало истории -> ?", r["regime"] == "?", failures)

    _check("RS: монета -5% при BTC -20% -> +15 п.п.", rs_vs_btc(-5.0, -20.0) == 15.0, failures)
    _check("RS: нет данных -> None", rs_vs_btc(None, -20.0) is None, failures)


def test_rf_gate(cfg, failures: list[str]) -> None:
    print("Stage 4 — гейт доступа РФ:")
    bybit = {"BTC", "ETH", "ARB"}
    ok, venue = rf_gate("ARB", "arbitrum", "0xabc", bybit)
    _check("на Bybit -> Bybit spot", ok and venue == "Bybit spot", failures)
    ok, venue = rf_gate("XYZ", "ethereum", "0xdef", bybit)
    _check("не на Bybit, но on-chain -> DEX only", ok and venue == "DEX only", failures)
    ok, venue = rf_gate("NOPE", "", "", bybit)
    _check("нет венью -> FAIL", (not ok) and venue == "нет", failures)


def test_liveness(cfg, failures: list[str]) -> None:
    print("Stage 3b — живость (dev/listings/wash) + LP-lock:")
    from scanner.stages import liveness, antirug

    # Живой проект: коммиты + широкий листинг + CEX.
    detail = {"developer_data": {"commit_count_4_weeks": 40, "pull_request_contributors": 5,
                                 "stars": 800},
              "tickers": [{"market": {"name": "Binance"}}, {"market": {"name": "Bybit"}},
                          {"market": {"name": "OKX"}}] + [{"market": {"name": f"dex{i}"}}
                                                          for i in range(8)]}
    c = Candidate(source="t", track="A", symbol="LIVE", volume_24h=1e6, liquidity_usd=5e5)
    liveness.assess_liveness(c, detail, cfg)
    _check("живой: dev_commits проставлен", c.dev_commits_4w == 40, failures)
    _check("живой: on_cex True", c.on_cex is True, failures)
    _check("живой: liveness_score высокий (>7)", c.liveness_score is not None and c.liveness_score > 7, failures)

    # Труп: 0 коммитов, только DEX.
    dead = {"developer_data": {"commit_count_4_weeks": 0},
            "tickers": [{"market": {"name": "someswap"}}]}
    c2 = Candidate(source="t", track="B", symbol="DEAD", volume_24h=1e5, liquidity_usd=2e5)
    liveness.assess_liveness(c2, dead, cfg)
    _check("труп: liveness низкий (<4)", c2.liveness_score is not None and c2.liveness_score < 4, failures)
    _check("труп: заметка про 0 коммитов", any("встал" in n for n in c2.liveness_notes), failures)

    # Нет detail -> liveness None (режет confidence, не врёт).
    c3 = Candidate(source="t", track="A", symbol="NODATA")
    liveness.assess_liveness(c3, {}, cfg)
    _check("нет detail -> liveness_score None", c3.liveness_score is None, failures)

    # Wash-trading: объём ≫ ликвидности.
    c4 = Candidate(source="t", track="B", symbol="WASH", volume_24h=1e8, liquidity_usd=1e5)
    liveness.assess_liveness(c4, {}, cfg)
    _check("wash: ratio посчитан (1000)", c4.wash_ratio == 1000.0, failures)
    _check("wash: флаг wash_suspect", "wash_suspect" in c4.flags, failures)

    # LP-lock интерпретация.
    locked = {"lp_holders": [{"is_locked": "1", "percent": "0.95"},
                             {"is_locked": "0", "percent": "0.05"}]}
    _check("LP заблокирован 95%", antirug.lp_locked_pct(locked) == 95.0, failures)
    burned = {"lp_holders": [{"address": "0x000000000000000000000000000000000000dead",
                             "percent": "1.0"}]}
    _check("LP сожжён (dead-адрес) = 100%", antirug.lp_locked_pct(burned) == 100.0, failures)
    unlocked = {"lp_holders": [{"is_locked": "0", "percent": "0.9"}]}
    ok, reasons, flags = antirug.evaluate_goplus_evm(
        {"is_honeypot": "0", "buy_tax": "0", "sell_tax": "0", "is_open_source": "1",
         **unlocked}, cfg)
    _check("разблокированный LP -> флаг lp_unlocked", "lp_unlocked" in flags, failures)
    _check("нет lp_holders -> None (не пенализируем)", antirug.lp_locked_pct({}) is None, failures)


def test_onchain(cfg, failures: list[str]) -> None:
    print("Stage 4c — on-chain накопление (Dune):")
    from scanner.stages.onchain import assess_onchain
    from scanner.sources.dune import build_onchain_map

    # Накопление: сильный отток (−20% нед.оборота при vol=1M) + рост холдеров.
    s, notes = assess_onchain(-1_400_000, 8.0, cfg, volume_24h=1_000_000)
    _check("отток+рост холдеров -> балл высокий (>8)", s is not None and s > 8, failures)
    _check("заметка про отток + долю оборота", any("отток" in n and "оборота" in n for n in notes), failures)
    # Распределение: сильный приток (+20% оборота) + исход холдеров -> низкий.
    s2, _ = assess_onchain(1_400_000, -8.0, cfg, volume_24h=1_000_000)
    _check("приток+исход -> балл низкий (<3)", s2 is not None and s2 < 3, failures)
    # Нормировка: тот же $ поток, но огромный объём -> сигнал слабый (крупнокап
    # не штрафуется абсолютом). LINK-кейс: $302M при vol $400M/д = ~11% оборота.
    s_big, _ = assess_onchain(1_400_000, None, cfg, volume_24h=100_000_000)
    _check("нормировка: малая доля оборота -> балл ~нейтрален (>4)", s_big > 4, failures)
    # Нет объёма -> fallback на абсолютный порог.
    s_abs, _ = assess_onchain(-3_000_000, None, cfg, volume_24h=None)
    _check("без объёма -> абсолютный порог работает (>6)", s_abs > 6, failures)
    # Санити-кап: аномальный поток (BNB-кейс +108% оборота) -> игнор -> None без холдеров.
    s_anom, n_anom = assess_onchain(5_130_000_000, None, cfg, volume_24h=680_000_000)
    _check("аномальный поток (>50% оборота) -> None (не врём)", s_anom is None, failures)
    _check("аномалия помечена в заметке", any("аномален" in n for n in n_anom), failures)
    # но если при аномальном потоке есть холдеры -> оцениваем по холдерам.
    s_anh, _ = assess_onchain(5_130_000_000, 7.0, cfg, volume_24h=680_000_000)
    _check("аномальный поток + холдеры -> балл по холдерам (>5)", s_anh is not None and s_anh > 5, failures)
    # Нет данных -> None (режет confidence, не врёт).
    s3, _ = assess_onchain(None, None, cfg)
    _check("нет данных Dune -> None", s3 is None, failures)

    # build_onchain_map: индексирует по symbol и адресу.
    rows = [{"symbol": "arb", "token_address": "0xABC", "net_flow_usd_7d": -5e5,
             "holders_change_pct_7d": 3.0}]
    m = build_onchain_map(rows)
    _check("map по symbol (UPPER)", "ARB" in m and m["ARB"]["net_flow_usd_7d"] == -5e5, failures)
    _check("map по адресу (lower)", "0xabc" in m, failures)

    # match_onchain: адрес приоритетнее тикера (однофамильцы из других сетей).
    from scanner.stages.onchain import flow_ratio, match_onchain
    m2 = {"ARB": {"net_flow_usd_7d": 1.0}, "0xabc": {"net_flow_usd_7d": 2.0}}
    _check("match: по адресу раньше тикера",
           match_onchain(m2, "ARB", "0xABC")["net_flow_usd_7d"] == 2.0, failures)
    _check("match: fallback по тикеру", match_onchain(m2, "arb", "")["net_flow_usd_7d"] == 1.0, failures)
    _check("match: пустая карта -> None", match_onchain({}, "ARB", "0xabc") is None, failures)

    # flow_ratio = net_flow / (vol*7); нет объёма -> None.
    _check("flow_ratio: -7M при vol 10M/д = -10%", abs(flow_ratio(-7e6, 1e7) + 0.10) < 1e-9, failures)
    _check("flow_ratio: vol=0 -> None", flow_ratio(-7e6, 0) is None, failures)
    _check("flow_ratio: нет потока -> None", flow_ratio(None, 1e7) is None, failures)

    # Без DUNE_API_KEY -> {} без сетевых вызовов (блок onchain остаётся None).
    import copy
    from scanner.config import Config
    from scanner.sources.dune import fetch_onchain
    nokey = copy.deepcopy(cfg._d)
    nokey.setdefault("api_keys", {})["dune"] = ""
    _check("нет Dune-ключа -> {} (нейтрально)", fetch_onchain(Config(nokey)) == {}, failures)

    # Свежесть кэша Dune: результат 01.08 не должен выдаваться за «поток за 7 дней».
    from scanner.sources import dune as dune_mod
    t_end = 1785557555.0   # 2026-08-01T04:12:35Z
    age = dune_mod.result_age_hours({"execution_ended_at": "2026-08-01T04:12:35.665028366Z"},
                                    now=t_end + 3 * 3600)
    _check("возраст Dune: наносекунды+Z разбираются, 3 ч", age is not None and abs(age - 3) < 0.01,
           failures)
    _check("возраст Dune: нет поля -> None", dune_mod.result_age_hours({}) is None, failures)

    def _fake_dune(cached_ended: str, exec_ok: bool):
        calls: list[str] = []
        def fake(url, key, method="GET", timeout=20):
            calls.append(method + " " + url.split("/api/v1")[-1])
            if url.endswith("/results?limit=1000") and "/query/" in url:
                return {"execution_ended_at": cached_ended,
                        "result": {"rows": [{"symbol": "OLD"}]}}
            if url.endswith("/execute"):
                return {"execution_id": "E1"} if exec_ok else None
            if url.endswith("/status"):
                return {"state": "QUERY_STATE_COMPLETED"}
            return {"result": {"rows": [{"symbol": "NEW"}]}}
        return fake, calls

    real_req = dune_mod._req
    try:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 3600))
        dune_mod._req, calls = _fake_dune(now_iso, exec_ok=True)
        rows = dune_mod.fetch_query_rows("k", 1, max_age_hours=20)
        _check("Dune: свежий кэш (1 ч) -> без перезапуска",
               rows == [{"symbol": "OLD"}] and not any("execute" in c for c in calls), failures)
        dune_mod._req, calls = _fake_dune("2026-08-01T04:12:35.665Z", exec_ok=True)
        rows = dune_mod.fetch_query_rows("k", 1, max_age_hours=20)
        _check("Dune: старый кэш -> перезапуск и новые строки",
               rows == [{"symbol": "NEW"}] and any("execute" in c for c in calls), failures)
        dune_mod._req, _ = _fake_dune("2026-08-01T04:12:35.665Z", exec_ok=False)
        _check("Dune: старый кэш и перезапуск не удался -> [] (старое не берём)",
               dune_mod.fetch_query_rows("k", 1, max_age_hours=20) == [], failures)
    finally:
        dune_mod._req = real_req

    # Блок onchain в скоре теперь читает onchain_score (не всегда None).
    from scanner.stages.score import compute_score
    c = Candidate(source="t", track="A", symbol="OC", zone="ПРУЖИНА/ДНО",
                  rf_venue="Bybit spot", volume_24h=5e7, mc_tvl=0.3, fdv_mc=1.2,
                  liveness_score=8.0)
    c.onchain_score = 9.0
    _, conf, brk = compute_score(c, cfg)
    _check("onchain-блок наполнен -> confidence 1.0", conf == 1.0, failures)
    _check("onchain sub в breakdown = 9.0", brk["onchain"]["sub"] == 9.0, failures)


def test_sources_parse(cfg, failures: list[str]) -> None:
    print("Парсинг источников (фандинг Bybit, klines, SQLite):")
    from scanner.sources.funding import parse_funding
    from scanner.sources.bybit import parse_daily_closes

    data = {"result": {"list": [
        {"symbol": "ARBUSDT", "fundingRate": "-0.0004"},
        {"symbol": "1000PEPEUSDT", "fundingRate": "0.0006"},
        {"symbol": "BTCPERP", "fundingRate": "0.0001"},       # не USDT -> мимо
        {"symbol": "ETHUSDT", "fundingRate": ""},             # пустая ставка -> мимо
        {"symbol": "1000BONKUSDT", "fundingRate": "0.0002"},
        {"symbol": "BONKUSDT", "fundingRate": "0.0003"},      # прямой листинг приоритетнее
    ]}}
    fm = parse_funding(data)
    _check("фандинг: строка -> float", fm.get("ARB") == -0.0004, failures)
    _check("фандинг: 1000PEPE -> PEPE", fm.get("PEPE") == 0.0006, failures)
    _check("фандинг: прямой BONK приоритетнее 1000BONK", fm.get("BONK") == 0.0003, failures)
    _check("фандинг: не-USDT и пустые пропущены", "BTC" not in fm and "ETH" not in fm, failures)
    _check("фандинг: мусорный ответ -> {}", parse_funding(None) == {} and parse_funding({}) == {}, failures)

    day = 86_400_000
    now = 12 * day + 3_600_000                     # 12-й день, 01:00 UTC — свеча дня 12 не закрыта
    kl = {"result": {"list": [[str(12 * day), "0", "0", "0", "99"],     # newest-first, live
                              [str(11 * day), "0", "0", "0", "101"],
                              [str(10 * day), "0", "0", "0", "100"]]}}
    _check("klines: oldest->newest, live-свеча отброшена",
           parse_daily_closes(kl, now) == [100.0, 101.0], failures)
    _check("klines: пустой ответ -> []", parse_daily_closes(None, now) == [], failures)

    # SQLite: поля Stage 4b/4c пишутся в candidates.
    from scanner.db import Store
    st = Store(":memory:")
    c = Candidate(source="t", track="A", symbol="Q", coin_id="q", chain="ethereum",
                  address="0x1", market_dd=0.42, spring_quality=1.1, funding_rate=-0.0004,
                  onchain_score=7.5, net_flow_usd_7d=-2e6, holders_change_pct_7d=3.0)
    rid = st.new_run("t")
    st.save_candidates(rid, [c])
    row = st.conn.execute("SELECT market_dd, spring_quality, funding_rate, onchain_score,"
                          " net_flow_usd_7d, holders_change_pct_7d FROM candidates").fetchone()
    st.close()
    _check("SQLite: поля Stage 4b/4c сохранены", row == (0.42, 1.1, -0.0004, 7.5, -2e6, 3.0), failures)


def test_entry_quality(cfg, failures: list[str]) -> None:
    print("Stage 4b — качество пружины (feature_study):")
    from scanner.stages.entry_quality import spring_quality
    from scanner.stages.score import _sub_zone

    # Сильная: рынок на дне (BTC −60%), длинная база, объём в полосе.
    good = Candidate(source="t", track="A", symbol="G", zone="ПРУЖИНА/ДНО",
                     drawdown_from_ath_pct=85.0, market_dd=0.60,
                     indicators={"base_len_days": 60, "vol_trend": 0.65})
    mg, ng = spring_quality(good, cfg)
    good.spring_quality = mg
    _check("сильная пружина: множитель > 1.05", mg > 1.05, failures)
    _check("сильная: заметка про рынок на дне", any("рынок на дне" in n for n in ng), failures)
    _check("сильная: sub_zone поднят выше базовых 9", _sub_zone(good) > 9.0, failures)

    # Слабая: BTC у ATH, короткая база, мёртвый объём, обнуление.
    bad = Candidate(source="t", track="A", symbol="B", zone="ПРУЖИНА/ДНО",
                    drawdown_from_ath_pct=96.0, market_dd=0.05,
                    indicators={"base_len_days": 12, "vol_trend": 0.2})
    mb, nb = spring_quality(bad, cfg)
    bad.spring_quality = mb
    _check("слабая пружина: множитель < 0.8", mb < 0.8, failures)
    _check("слабая: sub_zone занижен (< 8)", _sub_zone(bad) < 8.0, failures)
    _check("сильная ранжируется выше слабой по зоне", _sub_zone(good) > _sub_zone(bad), failures)

    # Нет market_dd/indicators -> нейтрально (множитель ~1, не штрафуем вслепую).
    neutral = Candidate(source="t", track="A", symbol="N", zone="ПРУЖИНА/ДНО")
    mn, _ = spring_quality(neutral, cfg)
    _check("нет данных -> множитель 1.0 (нейтрально)", mn == 1.0, failures)

    # Фандинг: капитуляция шортов (отрицательный) = бонус.
    fc = Candidate(source="t", track="A", symbol="F", zone="ПРУЖИНА/ДНО",
                   funding_rate=-0.001)
    mf, nf = spring_quality(fc, cfg)
    _check("отрицательный фандинг -> бонус (>1)", mf > 1.0, failures)
    _check("фандинг: заметка про капитуляцию", any("капитуляц" in n for n in nf), failures)
    # эйфория лонгов на пружине = штраф
    fe = Candidate(source="t", track="A", symbol="FE", zone="ПРУЖИНА/ДНО",
                   funding_rate=0.001)
    me, _ = spring_quality(fe, cfg)
    _check("положительный фандинг на пружине -> штраф (<1)", me < 1.0, failures)


def test_market_regime(cfg, failures: list[str]) -> None:
    print("Контекст рынка альтов (market_daily -> признаки -> перегрев):")
    from scanner import regime
    from scanner.stages.entry_quality import spring_quality
    D = regime.DAY
    # 400 дней: альт-рынок (total − BTC − стейблы) растёт до пика и падает на 40%.
    rows = []
    for i in range(400):
        alt = 1000 + 5 * i if i < 300 else 2500 * (1 - 0.004 * (i - 299))
        btc = 1000.0
        rows.append({"day": i * D, "total_mcap": alt + btc + 200, "btc_dominance": btc / (alt + btc + 200) * 100,
                     "stables_usd": 200, "fng": 40, "mvrv_btc": 1.5, "mvrv_eth": 0.9,
                     "breadth200": 80, "funding_btc": 0.00005})
    S = regime.market_series(rows)
    _check("alt_ex = total − BTC − стейблы", abs(S["alt_ex"][0] - 1000) < 1e-6, failures)
    ctx = regime.market_context(rows, cfg)
    _check("alt_dd на последний день ≈ 0.40", abs(ctx.get("alt_dd", 0) - 0.40) < 0.01, failures)
    hot = ctx["hot"]
    _check("холодный рынок: 0 горящих флагов", hot["n_lit"] == 0 and hot["score"] == 0, failures)
    _check("ширина 80% попала в «близко»", "breadth200" in hot["near"], failures)
    f = {"alt_vs_sma200": 0.5, "mvrv_btc": 2.5, "fng30": 50, "breadth200": 60}
    h = regime.hot_flags(f, cfg)
    _check("перегрев: 2 из 4 доступных = 0.5", h["score"] == 0.5 and h["n_lit"] == 2, failures)
    _check("мало доступных флагов -> score None", regime.hot_flags({"fng30": 80}, cfg)["score"] is None, failures)
    line = regime.context_line({**ctx, "btc_dd": 0.33})
    _check("строка контекста: альты, BTC, перегрев, F&G",
           "альты −40%" in line and "BTC −33%" in line and "перегрев 0/" in line
           and "близко: ширина рынка" in line and "F&G 40" in line, failures)
    _check("пустой контекст -> пустая строка", regime.context_line({}) == "", failures)

    # spring_quality: альт-рынок на дне > у хаёв; BTC — фолбэк при отсутствии alt.
    def sq(**kw):
        return spring_quality(Candidate(source="t", track="A", symbol="Q", zone="ПРУЖИНА/ДНО", **kw), cfg)
    bottom, nb = sq(alt_market_dd=0.70, market_dd=0.10)
    top, nt = sq(alt_market_dd=0.20, market_dd=0.60)
    _check("источник alt: альты −70% > альты −20% (BTC игнорируется)", bottom > 1.1 > 0.7 > top, failures)
    _check("заметка про альт-рынок на дне", any("альт-рынок на дне" in n for n in nb), failures)
    fb, _ = sq(market_dd=0.60)
    _check("нет alt_dd -> фолбэк на BTC-dd", fb > 1.1, failures)
    cold, _ = sq(alt_market_dd=0.5)
    warm, nw = sq(alt_market_dd=0.5, market_hot_score=0.125, market_hot_lit=["fng30"])
    hotm, nh = sq(alt_market_dd=0.5, market_hot_score=0.375,
                  market_hot_lit=["fng30", "mvrv_btc", "breadth200"])
    _check("перегрев режет множитель: холодный > тёплый ≥ горячий", cold > warm >= hotm, failures)
    _check("заметки: теплеет / перегрет", any("теплеет" in n for n in nw)
           and any("перегрет" in n and "MVRV BTC" in n for n in nh), failures)


def test_coin_context(cfg, failures: list[str]) -> None:
    print("Stage 4d — монетный контекст (информационный):")
    from scanner.stages.coin_context import annotate, supply_growth
    from scanner.sources.coin_extras import parse_delist_title
    from scanner.notify.telegram import risk_lines
    _check("предложение: mcap/price 100→130 = +30%",
           supply_growth([1.0, 2.0], [100.0, 260.0]) == 0.3, failures)
    _check("предложение: мало точек -> None", supply_growth([1.0], [100.0]) is None, failures)
    _check("делистинг: список тикеров", parse_delist_title("Delisting of L3,VIC") == (["L3", "VIC"], False),
           failures)
    _check("делистинг: только перп", parse_delist_title("Delisting of ICXUSDT Perpetual Contract")
           == (["ICX"], True), failures)
    _check("делистинг: без тикеров в заголовке",
           parse_delist_title("Bybit to Delist 4 Token(s) on Sep 24, 2026")[0] == [], failures)
    extras = {"revenue": {"by_gecko": {}, "by_symbol": {"UNI": 10e6}},
              "oi": {"UNI": 30e6, "ZZZ": 50e6}, "delist": {"ZZZ": "spot"},
              "coinbase": {"UNI", "ZZZ"}}
    u = Candidate(source="t", track="A", symbol="UNI", coin_id="uniswap", market_cap=1.2e9)
    annotate(u, extras, cfg, [1.0, 1.0], [1.0e9, 1.1e9])
    _check("P/F = капа / (выручка30д·12) = 10", u.p_f == 10.0, failures)
    _check("P/F ≤ pf_cheap -> заметка «дёшево»", any("дёшево" in n for n in u.zone_signals), failures)
    _check("эмиссия +10% без флага, US-тег coinbase",
           u.supply_growth == 0.1 and "supply_inflation" not in u.flags and u.us_tag == "coinbase", failures)
    z = Candidate(source="t", track="A", symbol="ZZZ", market_cap=100e6)
    annotate(z, extras, cfg, [1.0, 1.0], [100e6, 140e6])
    _check("делистинг спота -> флаг + заметка", z.delist == "spot" and "delist_spot" in z.flags, failures)
    _check("OI 50% капы -> high_leverage", "high_leverage" in z.flags, failures)
    _check("эмиссия +40% -> supply_inflation", "supply_inflation" in z.flags, failures)
    risks = " · ".join(risk_lines(z, cfg))
    _check("риски карточки: делистинг, эмиссия, OI", "снимает спот" in risks
           and "эмиссия +40%" in risks and "OI 50% капы" in risks, failures)
    sol = Candidate(source="t", track="A", symbol="SOL")
    annotate(sol, {"coinbase": {"SOL"}}, cfg)
    _check("SOL в списке ETF -> us_tag etf", sol.us_tag == "etf", failures)
    from scanner.stages.score import compute_score
    a, b = Candidate(source="t", track="A", symbol="A1", zone="ПРУЖИНА/ДНО"),         Candidate(source="t", track="A", symbol="A1", zone="ПРУЖИНА/ДНО")
    b.flags += ["supply_inflation", "high_leverage", "delist_spot"]
    _check("монетные флаги не меняют скор (информационно)",
           compute_score(a, cfg)[0] == compute_score(b, cfg)[0], failures)


def test_score(cfg, failures: list[str]) -> None:
    print("Stage 5 — композитный скор:")

    strong = Candidate(source="t", track="A", symbol="GOOD", zone="ПРУЖИНА/ДНО",
                       rf_venue="Bybit spot", volume_24h=5e7, mc_tvl=0.3, fdv_mc=1.2,
                       flags=["undervalued_vs_sector"], liveness_score=8.0)
    s, conf, _ = compute_score(strong, cfg)
    _check("сильный кандидат: высокий балл (>70)", s > 70, failures)
    _check("confidence 0.90 (есть liveness, нет on-chain)", conf == 0.90, failures)

    # Тот же без liveness-данных -> confidence ниже (liveness режет доверие).
    no_live = Candidate(source="t", track="A", symbol="NOLIVE", zone="ПРУЖИНА/ДНО",
                        rf_venue="Bybit spot", volume_24h=5e7, mc_tvl=0.3, fdv_mc=1.2,
                        flags=["undervalued_vs_sector"])
    _, conf2, _ = compute_score(no_live, cfg)
    _check("без liveness confidence ниже (0.74)", conf2 == 0.74, failures)

    weak = Candidate(source="t", track="A", symbol="BAD", zone="ПАДАЮЩИЙ_НОЖ",
                     rf_venue="DEX only", volume_24h=2e5, mc_tvl=8.0, fdv_mc=4.0,
                     flags=["overvalued_vs_sector", "mintable"])
    sw, _, _ = compute_score(weak, cfg)
    _check("слабый кандидат: низкий балл (<40)", sw < 40, failures)
    _check("сильный ранжируется выше слабого", s > sw, failures)

    # Нет зоны и оценки -> только safety+tradability -> confidence ниже.
    thin = Candidate(source="t", track="B", symbol="NEW", zone="?", rf_venue="DEX only")
    _, ct, _ = compute_score(thin, cfg)
    _check("тонкий кандидат: confidence < 0.85", ct < 0.85, failures)


def test_telegram_format(cfg, failures: list[str]) -> None:
    print("Stage 6 — Telegram: отбор, рынок, карточка монеты, транспорт:")
    from scanner.ladder import plan_ladder
    from scanner.notify import telegram as tg
    cands = [
        Candidate(source="t", track="A", symbol="ICP", zone="ПРУЖИНА/ДНО",
                  rf_venue="Bybit spot", score=84.7, confidence=0.85,
                  drawdown_from_ath_pct=90.0),
        Candidate(source="t", track="A", symbol="KITE", zone="ПАДАЮЩИЙ_НОЖ",
                  rf_venue="DEX only", score=44.0, confidence=0.85),
        Candidate(source="t", track="A", symbol="MID", zone="СЕРЕДИНА",
                  rf_venue="Bybit spot", score=75.0, confidence=0.85),
    ]
    # select_picks = ровно те, кто придёт карточкой (по ним пишется mute)
    _check("select_picks: только ICP (нож и СЕРЕДИНА не идут)",
           [c.symbol for c in tg.select_picks(cands, cfg)] == ["ICP"], failures)
    low = [Candidate(source="t", track="A", symbol="X", zone="ПРУЖИНА/ДНО", score=10.0)]
    _check("ниже порога -> карточек нет", tg.select_picks(low, cfg) == [], failures)
    thin = [Candidate(source="t", track="B", symbol="THIN", zone="ПРУЖИНА/ДНО",
                      score=90.0, confidence=0.3)]
    _check("низкий confidence отсечён гейтом", tg.select_picks(thin, cfg) == [], failures)
    many = [Candidate(source="t", track="A", symbol=f"S{i}", zone="ПРУЖИНА/ДНО",
                      score=90.0 - i, confidence=0.9) for i in range(15)]
    _check("select_picks: не больше max_alerts",
           len(tg.select_picks(many, cfg)) == cfg["stage6_telegram"]["max_alerts"], failures)

    # Рынок: фаза по просадке альтов + «температура» по флагам; значения рядом с порогом.
    day = 1790812800.0   # 2026-10-01 00:00 UTC
    warm = {"alt_dd": 0.43, "btc_dd": 0.32, "fng30": 67.9, "breadth200": 83.45, "day": day,
            "hot": {"n_lit": 0, "avail": 8, "lit": [], "near": ["fng30", "breadth200"]}}
    mb = "\n".join(tg.market_block(warm, cfg, now=day + 3600))
    _check("рынок: «середина цикла, теплеет 🟡»", "середина цикла, теплеет</b> 🟡" in mb, failures)
    _check("рынок: альты со шкалой дна + BTC",
           "Альты −43% от пика (дно цикла — от −65%) · BTC −32%" in mb, failures)
    _check("рынок: у порога — значение/порог", "у порога: F&amp;G 68/70, ширина 83/85%" in mb,
           failures)
    hot = {"alt_dd": 0.2, "mvrv_btc": 2.5, "day": day,
           "hot": {"avail": 8, "lit": ["mvrv_btc", "fng30", "breadth200"], "near": []}}
    mh = "\n".join(tg.market_block(hot, cfg, now=day))
    _check("рынок: 3/8 флагов -> «близко к хаям, перегрет 🔴», горит MVRV 2.50/2.20",
           "близко к хаям, перегрет</b> 🔴" in mh and "MVRV BTC 2.50/2.20" in mh, failures)
    calm = {"alt_dd": 0.7, "day": day, "hot": {"avail": 8, "lit": [], "near": []}}
    _check("рынок: альты −70% без флагов -> «дно цикла, спокойный»",
           "дно цикла, спокойный" in tg.market_block(calm, cfg, now=day)[0], failures)
    _check("рынок: данные старше 2 дней помечены датой",
           any("данные рынка на 01.10" in x for x in tg.market_block(warm, cfg, now=day + 5 * 86400)),
           failures)
    _check("рынок: нет данных -> так и сказано", "нет данных" in tg.market_block({}, cfg)[0], failures)

    # Карточка монеты у дна: лестница/стоп/цели моноширинно, свёрнутые «почему/риски».
    ind = {"days_since_low": 178, "base_len_days": 71, "vol_contraction": 0.66,
           "vol_trend": 0.70, "trend_recent_pct": 7.4, "rs_vs_btc_pct": 3.4}
    g = Candidate(source="t", track="A", symbol="GRAM", name="Gram (prev. Toncoin)",
                  zone="ПРУЖИНА/ДНО", score=70.0, confidence=0.9, rf_venue="Bybit spot",
                  coin_id="the-open-network", drawdown_from_ath_pct=81.9, indicators=ind,
                  alt_market_dd=0.43, supply_growth=0.14, price_usd=1.5,
                  flags=["mintable", "dd_in_bull_market"])
    plan = plan_ladder(1.5, 1.317, 40, steps=4, min_order=10, tick=0.001, qty_step=0.01,
                       exch_min_amt=5, floor_pct=25, sell="prod",
                       prod_levels=cfg["stage8_exit"]["ladder"])
    P = lambda x: f"{x:.3f}"  # noqa: E731
    card = tg.format_coin_card(g, plan, cfg, price=1.5, price_src="Bybit", P=P,
                               paper={"entry_price": 1.5, "stake": 100})
    first = card.split("\n")[0]
    _check("карточка: первая строка — суть (у дна, балл, биржа)",
           "GRAM у дна — 70/100" in first and "Bybit" in first, failures)
    _check("карточка: 4 ступени ценами + стоп в <pre>",
           "<pre>" in card and all(P(b["price"]) in card for b in plan["buys"])
           and "0.988" in card and "2 закрытия ниже" in card, failures)
    _check("карточка: цели +50/+150 и трейл остатка",
           "+50% → ⅓" in card and "+150% → ⅓" in card and "трейл 30%" in card, failures)
    _check("карточка: свёрнутый блок «почему/риски»",
           "<blockquote expandable>" in card and "Почему у дна" in card
           and "178 дн. без нового минимума" in card and "эмиссия +14% за полгода" in card,
           failures)
    _check("карточка: paper-пометка и дисклеймер",
           "paper-позиция $100 по 1.500" in card and "не рекомендация" in card, failures)
    noisy = Candidate(**{**g.__dict__, "flags": list(_FLAG_KEYS),
                         "liveness_notes": [f"⚠ заметка {i} " + "x" * 60 for i in range(6)]})
    cap = tg.card_caption(noisy, plan, cfg, price=1.5, P=P)
    _check("подпись к фото ≤ 1024 видимых символов, лестница на месте",
           len(tg.strip_html(cap)) <= tg.CAPTION_MAX and "<pre>" in cap, failures)
    bad = Candidate(source="t", track="A", symbol="X&Y", name="A&B <x>", zone="ПРУЖИНА/ДНО",
                    score=71.0)
    _check("карточка: имена экранированы", "A&amp;B &lt;x&gt;" in tg.format_coin_card(
        bad, None, cfg), failures)
    broken = plan_ladder(0.9, 1.317, 40, floor_pct=25)
    _check("цена ниже стопа -> «лестница не построена»",
           "Лестница не построена" in tg.format_coin_card(g, broken, cfg), failures)
    manual = Candidate(source="manual", track="", symbol="LINK", rf_venue="Bybit spot")
    _check("монета вне watchlist -> «план лестницы» без балла",
           "LINK — план лестницы" in tg.format_coin_card(manual, plan, cfg), failures)

    # Ссылки и транспорт — без сети.
    links = tg.coin_links("GRAM", "Bybit spot", "the-open-network")
    urls = " ".join(u for _, u in links)
    _check("ссылки Bybit: TradingView, Bybit, CoinGecko",
           "BYBIT:GRAMUSDT" in urls and "bybit.com/en/trade/spot/GRAM/USDT" in urls
           and "coingecko.com/en/coins/the-open-network" in urls, failures)
    dex = tg.coin_links("XYO", "DEX only", "xyo-network", "ethereum", "0xabc")
    _check("ссылки DEX: DEXScreener по сети и адресу",
           any("dexscreener.com/ethereum/0xabc" in u for _, u in dex), failures)
    pl = tg.build_payload("1", "t", silent=True, buttons=[links])
    _check("тихое сообщение с кнопками: disable_notification + inline_keyboard",
           pl.get("disable_notification") == "true" and "inline_keyboard" in pl.get("reply_markup", "")
           and pl["parse_mode"] == "HTML", failures)
    pl2 = tg.build_payload("1", "t")
    _check("обычное: со звуком, без кнопок",
           "disable_notification" not in pl2 and "reply_markup" not in pl2, failures)
    body = tg.multipart_body({"chat_id": "1", "caption": "тест"},
                             {"photo": ("a.png", b"\x89PNG", "image/png")}, "BND")
    _check("multipart для sendPhoto: файл, подпись, закрывающая граница",
           b'name="photo"; filename="a.png"' in body and "тест".encode() in body
           and body.endswith(b"--BND--\r\n"), failures)
    _check("strip_html: видимый текст", tg.strip_html("<b>A&amp;B</b>") == "A&B", failures)
    _check("цены: 4 значащие цифры",
           [tg.fmt_price(x) for x in (84579, 1.4996, 0.00058621, 150.26)]
           == ["84 579", "1.500", "0.0005862", "150.3"], failures)
    _check("балл: 70.0 -> 70, 69.8 -> 69.8 (не «70» ниже порога)",
           tg.fmt_score(70.0) == "70" and tg.fmt_score(69.8) == "69.8", failures)

    fail = tg.format_failure("scan", "RuntimeError: <boom>")
    _check("сбой: шаг и экранированная ошибка", "Accumulation scan" in fail
           and "&lt;boom&gt;" in fail, failures)

    # Недельная сводка из ежедневного прогона: первый прогон недели (пн по умолчанию).
    from datetime import datetime as _dt
    weekly_due = tg.weekly_due
    mon = _dt(2026, 10, 5, 10, 0).timestamp()          # понедельник
    _check("сводка: никогда не было -> пора", weekly_due(None, mon), failures)
    _check("сводка: прошлый пн -> пора", weekly_due(_dt(2026, 9, 28, 10).timestamp(), mon),
           failures)
    _check("сводка: уже в этот пн -> нет",
           not weekly_due(_dt(2026, 10, 5, 9, 0).timestamp(), _dt(2026, 10, 7, 10).timestamp()),
           failures)
    _check("сводка: пн пропущен (ноутбук выкл.) -> во вторник",
           weekly_due(_dt(2026, 9, 28, 10).timestamp(), _dt(2026, 10, 6, 10).timestamp()), failures)
    _check("сводка: день недели настраивается (пт)",
           not weekly_due(_dt(2026, 10, 2, 10).timestamp(), _dt(2026, 10, 5, 10).timestamp(), 4)
           and weekly_due(_dt(2026, 10, 2, 10).timestamp(), _dt(2026, 10, 9, 10).timestamp(), 4),
           failures)


_FLAG_KEYS = ("mintable", "transfer_pausable", "proxy", "has_blacklist", "lp_unlocked",
              "wash_suspect", "rf_dex_only", "bybit_ticker_mismatch", "dd_in_bull_market")


def test_exit(cfg, failures: list[str]) -> None:
    print("Stage 8 — выходной контур (hodl-профиль):")
    pos = {"entry_price": 1.0, "base_low": 0.9, "qty": 1000.0, "entry_ts": 0.0}

    # 1) Инвалидация: пробой лоу базы -25% (пол = 0.9*0.75 = 0.675).
    #    Без recent_closes -> проверка по одному close (обратная совместимость).
    sigs = exit_stage.evaluate_exit(pos, 0.60, 1.0, None, set(), cfg)  # < 0.675
    _check("пробой базы-25% (одиночный) -> инвалидация",
           any(s["type"] == "invalidation" for s in sigs), failures)
    _check("инвалидация — единственный сигнал", len(sigs) == 1, failures)

    # прокол базы, но в пределах буфера (shakeout) -> тишина, hodl терпит
    sigs = exit_stage.evaluate_exit(pos, 0.80, 1.0, None, set(), cfg)  # > 0.675
    _check("shakeout в пределах буфера — сигналов нет", not sigs, failures)

    # Подтверждение N дней (config=2): однодневный пролив НЕ инвалидирует.
    whipsaw = [0.95, 0.60]   # вчера норма, сегодня пробой -> ждём подтверждения
    sigs = exit_stage.evaluate_exit(pos, 0.60, 1.0, None, set(), cfg, recent_closes=whipsaw)
    _check("1-дневный whipsaw НЕ инвалидирует (подтв. 2д)",
           not any(s["type"] == "invalidation" for s in sigs), failures)
    # Два закрытия подряд ниже пола -> инвалидация подтверждена.
    confirmed = [0.62, 0.58]
    sigs = exit_stage.evaluate_exit(pos, 0.58, 1.0, None, set(), cfg, recent_closes=confirmed)
    _check("2 закрытия ниже пола -> инвалидация подтверждена",
           any(s["type"] == "invalidation" for s in sigs), failures)

    # 2) Лестница: +100% -> первый уровень; повторно не алертит (идемпотентность).
    sigs = exit_stage.evaluate_exit(pos, 2.1, 2.1, None, set(), cfg)
    _check("+110% -> ladder_0", any(s["type"] == "ladder_0" for s in sigs), failures)
    sigs2 = exit_stage.evaluate_exit(pos, 2.1, 2.1, None, {"ladder_0"}, cfg)
    _check("ladder_0 уже в журнале -> не дублируется",
           not any(s["type"] == "ladder_0" for s in sigs2), failures)

    # +300% -> оба уровня сразу (если первый ещё не фиксировали).
    sigs = exit_stage.evaluate_exit(pos, 4.5, 4.5, None, set(), cfg)
    _check("+350% -> оба уровня лестницы",
           {"ladder_0", "ladder_1"} <= {s["type"] for s in sigs}, failures)

    # 3) Трейлинг взводится только после arm (+60%): без прибыли откат не выбивает.
    sigs = exit_stage.evaluate_exit(pos, 1.05, 1.5, None, set(), cfg)  # пик был +50%
    _check("пик +50% (< arm 60%) -> трейлинг молчит",
           not any(s["type"] == "trailing" for s in sigs), failures)
    sigs = exit_stage.evaluate_exit(pos, 1.3, 2.0, None, {"ladder_0"}, cfg)  # пик +100%, откат -35%
    _check("пик +100%, откат -35% -> трейлинг сработал",
           any(s["type"] == "trailing" for s in sigs), failures)

    # 4) Зона распределения: разгон над SMA + верх диапазона (информационный).
    ind = {"pct_above_sma": 45.0, "range_pos": 0.95, "vol_contraction": 1.4,
           "trend_recent_pct": 40.0, "n_days": 180}
    sigs = exit_stage.evaluate_exit(pos, 1.8, 1.8, ind, set(), cfg)
    _check("разгон -> peak_zone", any(s["type"] == "peak_zone" for s in sigs), failures)

    # Эйфория фандинга даёт peak_zone даже без ценового разгона.
    ind_f = {"pct_above_sma": 5.0, "range_pos": 0.5, "funding_rate": 0.001}
    sigs = exit_stage.evaluate_exit(pos, 1.4, 1.4, ind_f, set(), cfg)
    pz = next((s for s in sigs if s["type"] == "peak_zone"), None)
    _check("эйфория фандинга -> peak_zone", pz is not None, failures)
    _check("peak_zone: заметка про фандинг", pz and "фандинг" in pz["note"], failures)

    # 5) Перегрев рынка: алерт в плюсе; сужение трейла только у близнеца B (A/B paper).
    hot = {"hot_score": 0.5, "lit": ["mvrv_btc", "fng30", "breadth200", "fund30"]}
    cold = {"hot_score": 0.0, "lit": []}
    sigs = exit_stage.evaluate_exit(pos, 1.5, 1.5, None, set(), cfg, market=hot)
    mh = next((s for s in sigs if s["type"] == "market_hot"), None)
    _check("перегрев рынка + прибыль -> market_hot", mh is not None, failures)
    _check("market_hot: флаги в заметке", mh and "MVRV BTC" in mh["note"], failures)
    _check("холодный рынок -> market_hot молчит",
           not any(s["type"] == "market_hot" for s in
                   exit_stage.evaluate_exit(pos, 1.5, 1.5, None, set(), cfg, market=cold)), failures)
    _check("market_hot уже в журнале -> не дублируется",
           not any(s["type"] == "market_hot" for s in
                   exit_stage.evaluate_exit(pos, 1.5, 1.5, None, {"market_hot"}, cfg, market=hot)), failures)
    # пик +100% (взведён), откат −20%: A (трейл 30%) молчит, B (15% при перегреве) выходит.
    pos_b = {**pos, "variant": "B"}
    ta = exit_stage.evaluate_exit(pos, 1.6, 2.0, None, {"ladder_0"}, cfg, market=hot)
    tb = exit_stage.evaluate_exit(pos_b, 1.6, 2.0, None, {"ladder_0"}, cfg, market=hot)
    tbc = exit_stage.evaluate_exit(pos_b, 1.6, 2.0, None, {"ladder_0"}, cfg, market=cold)
    _check("A: откат −20% при перегреве — трейл 30% молчит",
           not any(s["type"] == "trailing" for s in ta), failures)
    _check("B: откат −20% при перегреве — сужённый трейл сработал",
           any(s["type"] == "trailing" and "сужен" in s["note"] for s in tb), failures)
    _check("B на холодном рынке ведёт себя как A",
           not any(s["type"] == "trailing" for s in tbc), failures)

    # 6) Близнец S (A/B ширины стопа): пол −50% (0.45) вместо −25% (0.675).
    pos_s = {**pos, "variant": "S"}
    mid = [0.62, 0.58]                 # ниже 0.675, выше 0.45
    deep = [0.44, 0.40]                # ниже обоих полов
    _check("S: 2 закрытия ниже −25%, но выше −50% — A выходит, S держит",
           any(s["type"] == "invalidation" for s in
               exit_stage.evaluate_exit(pos, 0.58, 1.0, None, set(), cfg, recent_closes=mid))
           and not exit_stage.evaluate_exit(pos_s, 0.58, 1.0, None, set(), cfg, recent_closes=mid),
           failures)
    ds = exit_stage.evaluate_exit(pos_s, 0.40, 1.0, None, set(), cfg, recent_closes=deep)
    _check("S: ниже −50% -> инвалидация с буфером −50% в заметке",
           len(ds) == 1 and ds[0]["type"] == "invalidation" and "−50%" in ds[0]["note"], failures)
    _check("S: лестница как у A (+110% -> ladder_0)",
           any(s["type"] == "ladder_0" for s in
               exit_stage.evaluate_exit(pos_s, 2.1, 2.1, None, set(), cfg)), failures)

    # P&L net-of-fees.
    pnl = exit_stage.position_pnl({"entry_price": 1.0, "qty": 1000.0}, 2.0)
    _check("P&L ~ +99.7% (комиссии учтены)", 99.0 < pnl["pnl_pct"] < 100.0, failures)


def test_positions_store(cfg, failures: list[str]) -> None:
    print("Stage 7 — хранилище позиций (in-memory SQLite):")
    ps = PositionStore(":memory:")
    pid = ps.add("ARB", 0.5, 1000, coin_id="arbitrum", base_low=0.45)
    _check("позиция создана", ps.find_open("ARB") is not None, failures)
    _check("поиск по id", ps.find_open(str(pid)) is not None, failures)

    ps.record_event(pid, "ladder_0", 1.0, "фиксация 25%")
    _check("событие в журнале", "ladder_0" in ps.event_types(pid), failures)

    ps.update_hwm(pid, 1.2)
    _check("hwm обновлён", ps.find_open("ARB")["hwm"] == 1.2, failures)

    # Частичная продажа (лестница/pos reduce): realized + остаток открыт.
    r1 = ps.sell(pid, 250, 1.0, "ladder_0")          # продать 250 из 1000 @ 1.0
    _check("частичная: realized > 0 (вход 0.5 -> 1.0)", r1 > 100, failures)
    _check("частичная: остаток открыт 750", abs(ps.get(pid)["qty"] - 750) < 1e-6, failures)
    _check("частичная: realized_usdt накоплен", ps.get(pid)["realized_usdt"] > 100, failures)

    ps.close(pid, 1.1, "тест")
    _check("закрыта -> не в открытых", ps.find_open("ARB") is None, failures)
    _check("manual_close в журнале", "manual_close" in ps.event_types(pid), failures)
    _check("realized суммируется (part+close)", ps.get(pid)["realized_usdt"] > 400, failures)

    # A/B близнецы: B не считается в лимите paper, пара находится, pos close берёт A.
    pa = ps.add("TWN", 1.0, 100, coin_id="twn", paper=True)
    pb = ps.add("TWN", 1.0, 100, coin_id="twn", paper=True, variant="B", twin_of=pa)
    _check("близнец B не в лимите paper (count=1)", ps.count_open_paper() == 1, failures)
    prs = ps.ab_pairs()
    _check("ab_pairs: пара A↔B", len(prs) == 1 and prs[0][0]["id"] == pa and prs[0][1]["id"] == pb,
           failures)
    _check("find_open по тикеру -> A раньше B", ps.find_open("TWN")["id"] == pa, failures)
    psn = ps.add("TWN", 1.0, 100, coin_id="twn", paper=True, variant="S", twin_of=pa)
    _check("близнец S: не в лимите paper, своя пара в ab_pairs('S')",
           ps.count_open_paper() == 1 and [(a["id"], b["id"]) for a, b in ps.ab_pairs("S")]
           == [(pa, psn)] and len(ps.ab_pairs()) == 1, failures)
    ps.close(pa, 1.0); ps.close(pb, 1.0)
    _check("открытый S не блокирует переоткрытие A (has_open_for только по A)",
           not ps.has_open_for("twn", "TWN"), failures)
    ps.close(psn, 1.0)

    # pos add --merge: ступени одной монеты -> одна позиция со средней ценой.
    mp = ps.add("MRG", 0.8, 50, coin_id="mrg", paper=True)       # paper не сливается
    pm = ps.add("MRG", 1.0, 100, coin_id="mrg", base_low=0.9)
    _check("merge: находит реальную, а не paper", ps.find_open_real("MRG")["id"] == pm, failures)
    m = ps.merge(pm, 0.8, 150)
    _check("merge: qty 250, средняя (100+120)/250 = 0.88, base_low не меняется",
           abs(m["qty"] - 250) < 1e-9 and abs(m["entry_price"] - 0.88) < 1e-12
           and m["base_low"] == 0.9 and abs(m["initial_qty"] - 250) < 1e-9, failures)
    _check("merge: событие в журнале", "merge" in ps.event_types(pm), failures)
    sig_avg = exit_stage.evaluate_exit(m, 1.32, 1.32, None, set(), cfg)      # +50% от 0.88
    sig_first = exit_stage.evaluate_exit({**m, "entry_price": 1.0}, 1.32, 1.32, None, set(), cfg)
    _check("merge: +50% считается от средней (1.32 = +50% от 0.88, но +32% от 1-й ступени)",
           any(s["type"] == "ladder_0" for s in sig_avg)
           and not any(s["type"] == "ladder_0" for s in sig_first), failures)
    r = ps.close(pm, 1.2)
    legs = 250 * 1.2 * (1 - 0.0015) - (100 * 1.0 + 150 * 0.8) * (1 + 0.0015)
    _check("merge: realized = сумма ступеней по отдельности (комиссии не задвоены)",
           abs(r - legs) < 1e-9, failures)
    _check("merge в закрытую позицию -> None", ps.merge(pm, 1.0, 10) is None, failures)
    ps.close(mp, 0.8)

    # Cooldown re-open: закрытая paper-монета не переоткрывается N дней.
    cp = ps.add("CD", 1.0, 100, coin_id="cd-coin", paper=True)
    ps.close(cp, 1.5, reason="paper_trailing_exec")
    _check("recent_closed_paper ловит недавнее закрытие",
           ps.recent_closed_paper("cd-coin", "CD", 30), failures)
    _check("recent_closed_paper: старше окна -> нет",
           not ps.recent_closed_paper("cd-coin", "CD", 0), failures)

    # Paper-режим: дедуп по монете, лимит открытых, исключение из риск-экспозиции.
    pp = ps.add("XYZ", 2.0, 50, coin_id="xyz-coin", paper=True)
    _check("paper-позиция создана с флагом",
           ps.find_open("XYZ")["is_paper"] == 1, failures)
    _check("has_open_for находит по coin_id", ps.has_open_for("xyz-coin"), failures)
    _check("has_open_for: чужой монеты нет", not ps.has_open_for("other-coin"), failures)
    _check("count_open_paper == 1", ps.count_open_paper() == 1, failures)
    ps.close_db()

    # Бюджет риска: капитал 5000, новая позиция 1000 (20% > лимита 10%).
    class _C:  # минимальный конфиг-стаб
        def get(self, path, default=None):
            return {"stage7_positions": {"capital_usdt": 5000,
                                         "max_position_pct_of_capital": 10,
                                         "worst_case_loss_pct": 60}}.get(path, default)
    warns = risk_check([], 1000.0, _C())
    _check("превышение лимита позиции -> предупреждение",
           any("лимита" in w for w in warns), failures)
    warns = risk_check([], 300.0, _C())
    _check("в рамках лимита -> тишина", not warns, failures)
    # Paper-позиции не входят в worst-case экспозицию.
    paper_pos = [{"entry_price": 100.0, "qty": 100.0, "is_paper": 1}]  # 10k virtual
    warns = risk_check(paper_pos, 300.0, _C())
    _check("paper не считается в экспозиции", not warns, failures)


def test_exit_alert_format(cfg, failures: list[str]) -> None:
    print("Stage 8 — карточки выхода и сводка дня:")
    from scanner.notify import telegram as tg
    day = 1790812800.0   # 2026-10-01 00:00 UTC — точка CoinGecko = закрытие 30.09
    pos = {"symbol": "ARB", "entry_price": 0.30, "qty": 1000.0, "initial_qty": 1000.0,
           "base_low": 0.25, "is_paper": 0, "status": "open"}
    lad = {"position": pos, "last_price": 0.46, "last_ts": day, "held_days": 120, "hwm": 0.47,
           "pnl": {"pnl_pct": 52.6, "pnl_usdt": 158.0},
           "signals": [{"type": "ladder_0", "urgency": "medium", "action": "ЗАФИКСИРОВАТЬ 33%",
                        "note": "достигнут уровень +50% (сейчас +53%)"}]}
    text, loud = tg.format_exit_card(lad, cfg)
    _check("выход: лестница — что и сколько продать",
           "ARB — зафиксировать 33%" in text and "Продать: 330 ARB" in text, failures)
    _check("выход: закрытие подписано днём закрытия (30.09)", "(30.09)" in text, failures)
    _check("выход: реальная позиция, medium -> со звуком", loud, failures)
    inv = {**lad, "signals": [{"type": "invalidation", "urgency": "high",
                               "action": "ВЫЙТИ ПОЛНОСТЬЮ", "note": "2 закрытия ниже стопа"}]}
    t2, l2 = tg.format_exit_card(inv, cfg)
    _check("выход: стоп — продать весь остаток", "выйти полностью" in t2
           and "весь остаток 1000 ARB" in t2 and l2, failures)
    info = {**lad, "signals": [{"type": "peak_zone", "urgency": "low",
                                "action": "ЗОНА РАСПРЕДЕЛЕНИЯ", "note": "разгон +35% над SMA"}]}
    _check("выход: информационный сигнал -> без звука", tg.format_exit_card(info, cfg)[1] is False,
           failures)
    # Paper выбита стопом в этом прогоне: P&L на момент сигнала, исполнено виртуально, тихо.
    closed = {"position": {"symbol": "STOP", "entry_price": 1.0, "qty": 0.0, "base_low": 0.9,
                           "is_paper": 1, "status": "closed"},
              "last_price": 0.6, "last_ts": day, "hwm": 1.1,
              "pnl": {"pnl_pct": 0.0, "pnl_usdt": 0.0},
              "pnl_at_signal": {"pnl_pct": -40.2, "pnl_usdt": -40.2},
              "realized_usdt": -40.2, "held_days": 20, "triggered": {"invalidation"},
              "executed": ["invalidation: выход, realized -40.20"],
              "signals": [{"type": "invalidation", "urgency": "high",
                           "action": "ВЫЙТИ ПОЛНОСТЬЮ", "note": "пробила лоу базы"}]}
    t3, l3 = tg.format_exit_card(closed, cfg)
    _check("paper: P&L на момент сигнала (−40.2%), не +0.0%",
           "−40.2%" in t3 and "+0.0%" not in t3, failures)
    _check("paper: исполнено виртуально, без «Продать» и без звука",
           "исполнено виртуально" in t3 and "Продать" not in t3 and l3 is False, failures)
    _check("нет сигналов -> пусто", tg.format_exit_card({"position": pos, "signals": []}, cfg)
           == ("", False), failures)

    s = tg.sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    _check("спарклайн: рост -> последний символ верхний", s.endswith("█"), failures)
    _check("спарклайн: 1 точка не падает", bool(tg.sparkline([5.0])), failures)

    # Сводка дня: суть в первой строке, рынок, монеты, позиции с ценами стопа и цели.
    gram = Candidate(source="t", track="A", symbol="GRAM", zone="ПРУЖИНА/ДНО", score=70.0)
    lunc = Candidate(source="t", track="A", symbol="LUNC", zone="ПРУЖИНА/ДНО", score=67.1)
    arb_row = {"position": pos, "last_price": 0.36, "last_ts": day, "hwm": 0.40,
               "pnl": {"pnl_pct": 19.6, "pnl_usdt": 58.7}, "held_days": 30,
               "triggered": set(), "spark_prices": [0.30, 0.31, 0.33, 0.35, 0.40, 0.38, 0.36]}
    gram_row = {"position": {"symbol": "GRAM", "entry_price": 1.5, "qty": 66.7, "base_low": 1.3155,
                             "is_paper": 1, "status": "open"},
                "last_price": 1.4996, "last_ts": day, "hwm": 1.5,
                "pnl": {"pnl_pct": -0.3, "pnl_usdt": -0.33}, "held_days": 0, "triggered": set()}
    stop_row = {"position": closed["position"], "pnl": {"pnl_pct": 0.0, "pnl_usdt": 0.0},
                "realized_usdt": -40.2}
    state = {"scan": {"ran": True, "ok": True, "elapsed_min": 23.0, "watchlist": 83},
             "watch_ok": True,
             "market": {"alt_dd": 0.43, "day": day, "hot": {"avail": 8, "lit": [], "near": []}},
             "new": [gram], "muted": [], "near": [lunc],
             "positions": [arb_row, gram_row, stop_row],
             "signals_today": [{"symbol": "ARB", "label": "фикс 33%", "is_paper": 0}],
             "unavailable": ["onchain"], "dev_github": "62 из 83"}
    br = tg.format_brief(state, cfg, now=day + 3600)
    head = br.split("\n")[0]
    _check("сводка: первая строка — у дна 1, позиций 2 (закрытая не в счёте), сигналов 1",
           "🟢 у дна: 1" in head and "позиций: 2" in head and "🔔 сигналов: 1" in head, failures)
    _check("сводка: монеты — карточка выше и ниже порога",
           "GRAM 70 — карточка выше" in br and "ниже порога 70: LUNC 67.1" in br, failures)
    _check("сводка: позиция со стопом и целью ценами",
           "💰 <b>ARB</b> +19.6%" in br and "стоп 0.1875" in br and "цель 0.4500" in br, failures)
    _check("сводка: спарклайн при ≥7 снапшотах, закрытая — итогом сделки",
           "<code>" in br and "STOP закрыта по сигналу · итог −$40.20" in br, failures)
    _check("сводка: подвал — длительность скана и недоступный on-chain",
           "скан 23 мин" in br and "on-chain недоступен" in br, failures)
    bad = tg.format_brief({**state, "scan": {"ran": True, "ok": False,
                                             "error": "скан упал — смотреть logs/"}}, cfg,
                          now=day + 3600)
    _check("сводка: упал скан -> ⚠ в первой строке, без вчерашних монет",
           "⚠ скан не завершился" in bad.split("\n")[0] and "нет свежих данных скана" in bad
           and "карточка выше" not in bad, failures)
    none = tg.format_brief({**state, "scan": {"ran": False, "ok": None}}, cfg, now=day + 3600)
    _check("сводка: скана не было -> так и сказано", "скана сегодня не было" in none, failures)
    _check("сводка: watch упал -> «позиции не обновлены»", "⚠ позиции не обновлены" in
           tg.format_brief({**state, "watch_ok": False}, cfg, now=day + 3600), failures)
    _check("сводка: пометка теста", tg.format_brief(state, cfg, now=day, test=True)
           .startswith("<b>🧪 ТЕСТ"), failures)
    old = tg.format_brief({**state, "positions": [arb_row]}, cfg, now=day + 5 * 86400)
    _check("сводка: застрявшая цена помечена датой", "⚠ цена на 30.09" in old, failures)

    # brief_state: из базы и watchlist (файловая SQLite во временной папке).
    import copy
    import json as _json
    import tempfile
    from pathlib import Path as _P
    from scanner.config import Config
    from scanner.db import Store
    from scanner.notify.deliver import brief_state
    with tempfile.TemporaryDirectory() as tmp:
        d = copy.deepcopy(cfg._d)
        d["output"] = {"db_path": str(_P(tmp) / "t.db"),
                       "watchlist_json": str(_P(tmp) / "wl.json")}
        c2 = Config(d)
        st = Store(d["output"]["db_path"])
        rid = st.new_run("x")
        st.finish_run(rid, 10, 5, 3, {"elapsed_sec": 600, "unavailable": ["onchain"],
                                      "dev_github": "2 из 3", "btc_dd": 0.3})
        st.record_alert("GRAM", 70.0)
        st.close()
        _P(d["output"]["watchlist_json"]).write_text(_json.dumps([
            {"symbol": "GRAM", "zone": "ПРУЖИНА/ДНО", "score": 70.0, "confidence": 0.9, "track": "A"},
            {"symbol": "LUNC", "zone": "ПРУЖИНА/ДНО", "score": 67.1, "confidence": 0.9, "track": "Q"},
            {"symbol": "MID", "zone": "СЕРЕДИНА", "score": 69.0, "confidence": 0.9, "track": "A"},
        ]), encoding="utf-8")
        ps = PositionStore(d["output"]["db_path"])
        pid = ps.add("GRAM", 1.5, 66.7, coin_id="the-open-network", base_low=1.3, paper=True)
        ps.add("GRAM", 1.5, 66.7, paper=True, variant="B", twin_of=pid)
        ps.snapshot(pid, 1.49, -0.8, 1.5)
        ps.record_event(pid, "ladder_0", 2.25, "тест")
        ps.close_db()
        bs = brief_state(c2)
    _check("brief_state: скан сегодня завершён, 10 мин, on-chain недоступен",
           bs["scan"]["ok"] is True and bs["scan"]["elapsed_min"] == 10
           and bs["unavailable"] == ["onchain"], failures)
    _check("brief_state: карточка сегодня -> new, ниже порога -> near (без СЕРЕДИНЫ)",
           [c.symbol for c in bs["new"]] == ["GRAM"] and [c.symbol for c in bs["near"]] == ["LUNC"],
           failures)
    _check("brief_state: позиции без близнецов, снапшот сегодня -> watch ok",
           len(bs["positions"]) == 1 and bs["watch_ok"] is True, failures)
    _check("brief_state: сигнал дня подписан («фикс 33%»)",
           bs["signals_today"] == [{"symbol": "GRAM", "label": "фикс 33%", "is_paper": 1}], failures)

    # Недельная сводка (обычная неделя): счётчик + напоминание + отсчёт до итога.
    wk = format_weekly({"week_no": 2, "milestone": False, "milestone_weeks": 4,
                        "opened": 3, "open_now": 5, "invalidations": 1,
                        "ladder_hits": 2, "trailings": 0,
                        "paper_pnl_usdt": 12.5, "real_open": 1,
                        "real_pnl_usdt": 40.0}, cfg)
    _check("недельная сводка: счётчик недели", "неделя 2" in wk, failures)
    _check("недельная сводка: напоминание + отсчёт", "До итоговой сводки: 2 нед" in wk, failures)
    _check("недельная сводка: цифры", "инвалидаций 1" in wk and "+12.50" in wk, failures)
    _check("идёт неделя -> без «paper не стартовал»", "не стартовал" not in wk, failures)
    w0 = format_weekly({"week_no": 0, "milestone": False, "milestone_weeks": 4, "opened": 0,
                        "open_now": 0, "invalidations": 0, "ladder_hits": 0, "trailings": 0,
                        "paper_pnl_usdt": 0.0, "real_open": 0}, cfg)
    _check("paper не стартовал -> так и сказано", "Paper ещё не стартовал" in w0, failures)
    _check("нет A/B пар -> строки сравнения нет", "🅰🅱" not in wk, failures)
    wab = format_weekly({"week_no": 2, "milestone": False, "milestone_weeks": 4,
                         "opened": 0, "open_now": 2, "invalidations": 0, "ladder_hits": 0,
                         "trailings": 0, "paper_pnl_usdt": 0.0, "real_open": 0,
                         "ab": {"pairs": 3, "a_usdt": 40.0, "b_usdt": 55.5,
                                "diverged": 1, "b_better": 1}}, cfg)
    _check("A/B: строка сравнения A vs B", "🅰🅱" in wab and "+40.00" in wab
           and "+55.50" in wab and "3 парах" in wab, failures)
    _check("нет пар S -> строки ширины стопа нет", "🅰🆂" not in wab, failures)
    was = format_weekly({"week_no": 3, "milestone": False, "milestone_weeks": 4,
                         "opened": 0, "open_now": 2, "invalidations": 1, "ladder_hits": 0,
                         "trailings": 0, "paper_pnl_usdt": 0.0, "real_open": 0,
                         "stop_pct": 25, "ab_stop_pct": 50,
                         "ab_stop": {"pairs": 2, "a_usdt": -26.0, "b_usdt": 4.5, "diverged": 1,
                                     "b_better": 1, "a_stopped": 1, "b_stopped": 0}}, cfg)
    _check("A/S: строка ширины стопа (−25% vs −50%, срабатывания)",
           "🅰🆂" in was and "A −25% -26.00" in was and "S −50% +4.50" in was
           and "стоп сработал A 1 / S 0" in was, failures)

    # Итоговая 4-недельная: кумулятив + call-to-decide про капитал.
    ms = format_weekly({"week_no": 4, "milestone": True, "milestone_weeks": 4,
                        "opened": 2, "open_now": 12, "invalidations": 1,
                        "ladder_hits": 0, "trailings": 0, "paper_pnl_usdt": 30.0,
                        "real_open": 0, "real_pnl_usdt": 0.0,
                        "cum_opened": 34, "cum_invalidations": 9,
                        "cum_ladder_hits": 3, "cum_trailings": 1}, cfg)
    _check("итоговая: заголовок ИТОГОВАЯ", "ИТОГОВАЯ за 4" in ms, failures)
    _check("итоговая: кумулятив пружин", "34" in ms, failures)
    _check("итоговая: call-to-decide про капитал", "capital_usdt" in ms, failures)


def test_benchmark(cfg, failures: list[str]) -> None:
    print("Против рынка — книга vs альты/BTC на тех же окнах (scanner/benchmark.py):")
    import copy
    import sqlite3
    import tempfile
    from pathlib import Path as _P
    from scanner import benchmark as bm
    from scanner.config import Config
    from scanner.db import Store
    from scanner.notify import telegram as tg
    D = bm.DAY
    d0 = 1783987200                        # 2026-07-14 00:00 UTC
    # альты = total×(1−BTC.D/100) − стейблы, BTC = total×BTC.D/100; дня 2 нет, у дня 4 нет стейблов
    rows = [{"day": d0 + i * D, "total_mcap": t, "btc_dominance": dom, "stables_usd": st}
            for i, t, dom, st in ((0, 1000, 50, 100), (1, 1100, 50, 100), (3, 1300, 40, 100),
                                  (4, 1500, 40, None))]
    mkt = bm.market_points(rows)
    _check("рынок: альты 400 → 450 → 680, BTC 500 → 550 → 520",
           mkt["alt"] == [400.0, 450.0, 680.0] and mkt["btc"] == [500.0, 550.0, 520.0], failures)
    _check("ближайший день ≤ даты: на день 2 — день 1",
           bm.market_at(mkt, d0 + 2 * D + 3600)[:2] == (d0 + D, 450.0), failures)
    _check("в течение дня — этот день, до начала ряда — None",
           bm.market_at(mkt, d0 + 5 * 3600)[0] == d0 and bm.market_at(mkt, d0 - 1) is None,
           failures)
    _check("день без стейблов пропускается (день 4 → день 3)",
           bm.market_at(mkt, d0 + 4 * D + 60)[0] == d0 + 3 * D, failures)

    # Книга: закрытая (realized −20 на $100, окно д0→д1) + открытая с частичной продажей
    # (realized +45, остаток 200 из 300 по 1.0, снапшот 1.5 на д3): нереализованное net-of-fees
    # (1.5×0.9985 − 1.0×1.0015)×200 = 99.25 → книга (−20 + 45 + 99.25) / 400 = +31.06%.
    closed = {"id": 1, "symbol": "AAA", "entry_price": 10.0, "qty": 0.0, "initial_qty": 10.0,
              "entry_ts": d0 + 3600, "status": "closed", "closed_ts": d0 + D + 7200,
              "realized_usdt": -20.0, "is_paper": 1}
    part = {"id": 2, "symbol": "BBB", "entry_price": 1.0, "qty": 200.0, "initial_qty": 300.0,
            "entry_ts": d0 + 3600, "status": "open", "closed_ts": None, "realized_usdt": 45.0,
            "is_paper": 1}
    snap = {2: {"ts": d0 + 3 * D + 6 * 3600, "price": 1.5}}
    res = bm.compare_book([closed, part], snap, mkt)
    _check("книга: Σ P&L / Σ стоимости net-of-fees = +31.06%",
           res["n"] == 2 and abs(res["book_pct"] - 31.0625) < 1e-9, failures)
    _check("рынок взвешен стоимостью: альты (100×12.5 + 300×70)/400 = +55.63%, BTC +5.5%",
           abs(res["alt_pct"] - 55.625) < 1e-9 and abs(res["btc_pct"] - 5.5) < 1e-9
           and abs(res["diff_pp"] + 24.5625) < 1e-9, failures)
    _check("окно: закрытая — до closed_ts, открытая — до последнего снапшота",
           [r["end"] for r in res["rows"]] == [closed["closed_ts"], snap[2]["ts"]], failures)
    r2 = bm.compare_book([closed, dict(part, id=3)], snap, mkt)
    _check("открытая без снапшота — вне итога (1 из 2)",
           r2["n"] == 1 and r2["positions"] == 2, failures)
    _check("рынок отстал от снапшота на 3 дня -> stale, на 2 — нет",
           bm.compare_book([part], {2: {"ts": d0 + 6 * D + 60, "price": 1.5}}, mkt)["stale"]
           and not bm.compare_book([part], {2: {"ts": d0 + 5 * D + 60, "price": 1.5}},
                                   mkt)["stale"], failures)
    _check("близнецы B/S — не основная книга; старая схема без variant — основная",
           not bm.is_main({"variant": "B", "twin_of": 1}) and not bm.is_main({"variant": "S"})
           and bm.is_main({"symbol": "OLD"}), failures)

    # weekly_books на файловой БД: близнецы исключены, real — отдельной строкой, архива нет —
    # молча пропущен; архив старой схемы читается только на чтение (без миграции).
    with tempfile.TemporaryDirectory() as tmp:
        d = copy.deepcopy(cfg._d)
        db = str(_P(tmp) / "t.db")
        d["output"] = {"db_path": db, "watchlist_json": str(_P(tmp) / "wl.json")}
        d["benchmark"] = {"enabled": True, "archive_db": str(_P(tmp) / "нет.db"),
                          "archive_label": "v1"}
        st = Store(db)
        st.upsert_market({r["day"]: {k: v for k, v in r.items() if k != "day"} for r in rows})
        st.close()
        ps = PositionStore(db)
        empty = bm.weekly_books(Config(d))
        a = ps.add("BBB", 1.0, 300, coin_id="bbb", paper=True, entry_ts=d0 + 3600)
        b = ps.add("BBB", 1.0, 300, paper=True, variant="B", twin_of=a, entry_ts=d0 + 3600)
        s = ps.add("BBB", 1.0, 300, paper=True, variant="S", twin_of=a, entry_ts=d0 + 3600)
        for pid, px in ((a, 1.6), (b, 3.0), (s, 3.0)):      # близнецы с другой ценой
            ps.conn.execute("INSERT INTO position_snapshots(position_id, ts, price, pnl_pct, hwm) "
                            "VALUES (?,?,?,?,?)", (pid, d0 + 3 * D + 3600, px, 0.0, px))
        ps.conn.commit()
        wb = bm.weekly_books(Config(d))
        r = ps.add("RRR", 2.0, 50, entry_ts=d0 + 3600)
        ps.conn.execute("INSERT INTO position_snapshots(position_id, ts, price, pnl_pct, hwm) "
                        "VALUES (?,?,?,?,?)", (r, d0 + D + 3600, 2.4, 0.0, 2.4))
        ps.conn.commit()
        ps.close_db()
        wr = bm.weekly_books(Config(d))
        old = _P(tmp) / "old.db"
        con = sqlite3.connect(old)
        con.executescript(
            "CREATE TABLE positions (id INTEGER PRIMARY KEY, symbol TEXT, entry_price REAL, "
            "qty REAL, initial_qty REAL, entry_ts REAL, status TEXT, closed_ts REAL, "
            "realized_usdt REAL, is_paper INTEGER);"
            "CREATE TABLE position_snapshots (position_id INTEGER, ts REAL, price REAL, "
            "pnl_pct REAL, hwm REAL);")
        con.execute("INSERT INTO positions VALUES (1, 'OLD', 10.0, 0, 10.0, ?, 'closed', ?, "
                    "20.0, 1)", (d0 + 3600, d0 + 3 * D))
        con.commit()
        con.close()
        before = old.read_bytes()
        d["benchmark"]["archive_db"] = str(old)
        wa = bm.weekly_books(Config(d))
        unchanged = old.read_bytes() == before
        d["benchmark"]["enabled"] = False
        off = bm.weekly_books(Config(d))
    _check("нет позиций -> строк нет", empty == [], failures)
    _check("книга A без близнецов B/S (их цена 3.0 не влияет): +59.61% на 1 поз.",
           [x["key"] for x in wb] == ["paper"] and wb[0]["n"] == wb[0]["positions"] == 1
           and abs(wb[0]["book_pct"] - 59.61) < 1e-9, failures)
    _check("архивной БД нет -> строка молча пропущена", "archive" not in [x["key"] for x in wb],
           failures)
    _check("real — отдельной строкой, только когда есть",
           [x["key"] for x in wr] == ["paper", "real"] and wr[1]["n"] == 1
           and abs(wr[1]["alt_pct"] - 12.5) < 1e-9, failures)
    arch = next((x for x in wa if x["key"] == "archive"), None)
    _check("архив старой схемы: книга по рынку текущей БД (+20% против альтов +70%)",
           arch is not None and arch["label"] == "v1" and arch["n"] == 1
           and abs(arch["book_pct"] - 20.0) < 1e-9 and abs(arch["alt_pct"] - 70.0) < 1e-9,
           failures)
    _check("архив открыт только на чтение: файл не изменён", unchanged, failures)
    _check("benchmark.enabled=false -> блока нет", off == [], failures)

    # Формат строк блока (как в недельной сводке).
    books = [{"emoji": "📝", "label": "книга A", "positions": 2, "n": 2, "book_pct": 1.2,
              "alt_pct": 0.8, "btc_pct": 0.5, "diff_pp": 0.4, "stale": False},
             {"emoji": "🗄", "label": "v1 (старый код, на 03.10)", "positions": 18, "n": 18,
              "book_pct": 30.73, "alt_pct": 36.68, "btc_pct": 27.97, "diff_pp": -5.95,
              "stale": True, "market_day": 1790812800}]
    blk = tg.benchmark_block(books)
    _check("блок: заголовок и подсказка про альты",
           blk[0] == "📊 <b>Против рынка</b> (те же даты входа и выхода):"
           and "обгоняет альты" in blk[-1], failures)
    _check("строка книги: «+1.2% · альты +0.8% · BTC +0.5% → +0.4 п.п. к альтам (2 поз.)»",
           "📝 книга A: +1.2% · альты +0.8% · BTC +0.5% → +0.4 п.п. к альтам (2 поз.)" in blk,
           failures)
    _check("строка архива: разница из показанных чисел (−6.0, не −5.9) + пометка рынка",
           "🗄 v1 (старый код, на 03.10): +30.7% · альты +36.7% · BTC +28.0% → −6.0 п.п. "
           "(18 поз.) · ⚠ рынок на 01.10" in blk, failures)
    part_b = tg.benchmark_block([dict(books[0], n=1, positions=2)])
    _check("часть позиций без данных -> «1 из 2 поз.»", "(1 из 2 поз.)" in part_b[1], failures)
    _check("нет позиций -> блока нет", tg.benchmark_block([]) == []
           and tg.benchmark_block([dict(books[0], positions=0, n=0)]) == [], failures)
    base = {"week_no": 2, "milestone": False, "milestone_weeks": 4, "opened": 0, "open_now": 2,
            "invalidations": 0, "ladder_hits": 0, "trailings": 0, "paper_pnl_usdt": 0.0,
            "real_open": 0}
    _check("недельная сводка: блок есть только с книгами",
           "Против рынка" in format_weekly({**base, "benchmark": books}, cfg)
           and "Против рынка" not in format_weekly(base, cfg), failures)


def test_github_levels(cfg, failures: list[str]) -> None:
    print("GitHub-активность, свечи Bybit, картинка уровней плана:")
    from scanner.sources import github
    from scanner.stages import liveness
    now = 1790812800.0   # 2026-10-01 00:00 UTC

    # GitHub: ссылки из CoinGecko, разбор commits.atom, сводка за 4 недели.
    det = {"links": {"repos_url": {"github": ["https://github.com/ton-blockchain/ton",
                                             "https://github.com/", "", "https://github.com/tonorg"]}}}
    _check("github: ссылки без пустых и «github.com/» без пути",
           github.repo_links(det) == ["https://github.com/ton-blockchain/ton",
                                      "https://github.com/tonorg"], failures)
    _check("github: repo-ссылка и org-ссылка",
           github.split_link("https://github.com/a/b.git/tree/x") == ("a", "b")
           and github.split_link("https://github.com/org") == ("org", ""), failures)
    entry = ("<entry><updated>{}</updated><author>\n<name>{}</name></author></entry>")
    atom = "<feed><updated>2026-10-01T00:00:00Z</updated>" + "".join(
        entry.format(t, a) for t, a in [("2026-09-30T10:00:00Z", "ann"), ("2026-09-20T10:00:00Z", "bob"),
                                         ("2026-09-10T10:00:00Z", "cid"), ("2026-08-01T10:00:00Z", "ann")]) + "</feed>"
    cm = github.parse_atom(atom)
    _check("github: 4 коммита из ленты (заголовок ленты не считается)", len(cm) == 4, failures)
    sm = github.summarize_commits(cm, now)
    _check("github: за 4 нед. 3 коммита от 3 авторов, последний 0 дн. назад",
           sm == {"commits_4w": 3, "authors_4w": 3, "last_commit_days": 0}, failures)
    _check("github: пустая лента -> None", github.summarize_commits([], now)["commits_4w"] is None,
           failures)

    # Живость по GitHub (developer_data CoinGecko больше нет).
    tick = {"tickers": [{"market": {"name": "Binance"}}] + [{"market": {"name": f"x{i}"}}
                                                            for i in range(10)]}
    c = Candidate(source="t", track="A", symbol="TON")
    liveness.assess_liveness(c, tick, cfg, {"commits_4w": 20, "authors_4w": 4,
                                            "last_commit_days": 0, "links": ["x"]})
    _check("живость: 20+ коммитов и 4 автора -> 9/10 (dev 3 + авторы 1 + биржи 5)",
           c.liveness_score == 9.0 and any("20+ коммитов" in n for n in c.liveness_notes), failures)
    c2 = Candidate(source="t", track="A", symbol="ORG")
    liveness.assess_liveness(c2, tick, cfg, {"commits_4w": None, "last_commit_days": 12,
                                             "links": ["x"]})
    _check("живость: org-ссылка, push 12 дн. назад -> «жив», 7/10",
           c2.liveness_score == 7.0 and any("последний коммит 12 дн." in n for n in c2.liveness_notes),
           failures)
    c3 = Candidate(source="t", track="A", symbol="OLD")
    liveness.assess_liveness(c3, tick, cfg, {"commits_4w": None, "last_commit_days": 400,
                                             "links": ["x"]})
    _check("живость: код не обновлялся 400 дн. -> предупреждение, только биржи (5)",
           c3.liveness_score == 5.0 and any("не обновлялся 400" in n for n in c3.liveness_notes),
           failures)
    c5 = Candidate(source="t", track="A", symbol="TON")
    liveness.assess_liveness(c5, tick, cfg, {"commits_4w": 0, "authors_4w": 0,
                                             "last_commit_days": 46, "links": ["x"]})
    _check("живость: 0 коммитов в основной ветке, последний 46 дн. -> «вялый», не «встал»",
           c5.liveness_score == 6.0 and not any("встал" in n for n in c5.liveness_notes), failures)
    c4 = Candidate(source="t", track="A", symbol="FAIL")
    liveness.assess_liveness(c4, tick, cfg, {"commits_4w": None, "last_commit_days": None,
                                             "links": ["x"]})
    _check("живость: ссылка есть, GitHub не ответил -> так и сказано",
           any("GitHub не ответил" in n for n in c4.liveness_notes), failures)

    # Скор: источник, недоступный для всех монет, не режет confidence.
    s1 = Candidate(source="t", track="A", symbol="S", zone="ПРУЖИНА/ДНО", rf_venue="Bybit spot",
                   volume_24h=5e7, mc_tvl=0.3, fdv_mc=1.2, liveness_score=8.0)
    sc, conf, br = compute_score(s1, cfg, {"onchain"})
    sc0, conf0, _ = compute_score(s1, cfg)
    _check("скор: on-chain недоступен -> confidence 1.0, балл тот же",
           conf == 1.0 and conf0 == 0.9 and sc == sc0 and br["onchain"]["source"] == "недоступен",
           failures)

    # Свечи Bybit: только закрытые, oldest→newest; сверка тикера по цене.
    from scanner.sources import bybit
    day_ms = 86_400_000
    now_ms = int(now * 1000) + 3 * 3600 * 1000
    raw = {"result": {"list": [[str(int(now * 1000)), "3", "4", "2", "3.5", "10", "35"],
                               [str(int(now * 1000) - day_ms), "2", "3", "1", "3", "10", "30"],
                               [str(int(now * 1000) - 2 * day_ms), "1", "2", "1", "2", "10", "20"]]}}
    ob = bybit.parse_daily_ohlcv(raw, now_ms)
    _check("bybit: незакрытая свеча отброшена, порядок oldest→newest",
           ob["c"] == [2.0, 3.0] and ob["h"] == [2.0, 3.0] and ob["qv"] == [20.0, 30.0], failures)
    _check("bybit: тикер совпал, цена та же -> та же монета",
           bybit.same_coin(1.50, 1.45) is True and bybit.same_coin(0.000586, 0.000300) is False
           and bybit.same_coin(None, 1.0) is None, failures)

    # watchlist.json -> кандидаты: цена и любые поля модели доезжают до Telegram.
    import json as _json
    import tempfile
    from pathlib import Path as _P
    from scanner.pipeline import load_watchlist
    with tempfile.TemporaryDirectory() as tmp:
        f = _P(tmp) / "wl.json"
        f.write_text(_json.dumps([{"symbol": "GRAM", "price_usd": 1.5, "zone": "ПРУЖИНА/ДНО",
                                   "score": 70.0, "spring": True, "unknown_field": 1,
                                   "indicators": {"base_len_days": 71}}]), encoding="utf-8")
        wl = load_watchlist(str(f))
    _check("watchlist: цена, зона, индикаторы, spring -> spring_prefilter",
           len(wl) == 1 and wl[0].price_usd == 1.5 and wl[0].indicators["base_len_days"] == 71
           and wl[0].spring_prefilter is True, failures)

    # Картинка: Pillow и шрифт есть — PNG; нет — None (сообщение уйдёт текстом).
    from scanner.notify import chart
    c_ = [10.0] * 100 + [8.0] * 250 + [9.0] * 50
    ohlcv = {"o": c_, "c": c_, "h": [x * 1.02 for x in c_], "l": [x * 0.98 for x in c_]}
    png = chart.render_levels(ohlcv, title="TEST/USDT", buys=[9.0, 8.5], stop=7.0,
                              targets=[(13.5, "+50%")])
    if chart.available():
        _check("картинка: PNG с уровнями", isinstance(png, bytes) and png[:4] == b"\x89PNG", failures)
    else:
        _check("картинка: Pillow нет -> None (текстом)", png is None, failures)
    _check("картинка: мало истории -> None", chart.render_levels({"c": [1.0, 2.0]}, title="x")
           is None, failures)


def test_ladder(cfg, failures: list[str]) -> None:
    print("Планировщик лестницы (run.py ladder):")
    from scanner.ladder import plan_ladder, round_step
    _check("round_step: 0.1+0.2 → 0.3 по шагу 0.1", round_step(0.1 + 0.2, 0.1) == 0.3, failures)
    _check("round_step вверх: 1.23401 → 1.235", round_step(1.23401, 0.001, up=True) == 1.235,
           failures)
    # пример из отчёта: цена 1.00, лоу базы 0.95 -> стоп 0.7125, ступени 1/0.916/0.832/0.748
    p = plan_ladder(1.0, 0.95, 100, tick=0.0001, qty_step=0.01)
    pxs = [b["price"] for b in p["buys"]]
    _check("4 ступени до пола: 1.000/0.916/0.832/0.748",
           p["ok"] and [round(x, 3) for x in pxs] == [1.0, 0.916, 0.832, 0.748], failures)
    _check("стоп = лоу базы −25% (0.7125)", abs(p["stop_px"] - 0.7125) < 1e-9, failures)
    _check("убыток на стопе ≈ 17.5% (+комиссии) против ≈ 29% разом",
           0.17 < p["worst"]["stop_loss_pct"] < 0.19
           and 0.28 < p["worst"]["lump_stop_loss_pct"] < 0.30, failures)
    _check("лимитки кратны шагу цены, ступени ≥ $10",
           all(abs(round(b["price"] / 0.0001) * 0.0001 - b["price"]) < 1e-12 for b in p["buys"])
           and all(b["usd"] >= 10 - 1e-9 for b in p["buys"]), failures)
    _check("prod: +50% и +150% от средней, остаток под трейл",
           [s["gain"] for s in p["sells"]] == [0.5, 1.5, None], failures)
    p = plan_ladder(1.0, 0.95, 30, qty_step=0.01)
    _check("$30 при минимуме $10 → 3 ступени с предупреждением",
           p["steps"] == 3 and any("ступеней 3" in w for w in p["warns"]), failures)
    p = plan_ladder(1.0, 0.95, 20, steps=2, qty_step=0.01, filled=1)
    _check("продажа < $5 сливается со следующей целью",
           any("слита" in w for w in p["warns"]) and p["sells"][0]["gain"] == 1.5, failures)
    p = plan_ladder(1.0, 0.95, 50, sell="paired", qty_step=0.001, tick=0.0001)
    _check("парная: каждая ступень продаётся на ≥ +40% от своей цены",
           len(p["sells"]) == 4 and all(s["gain"] >= 0.40 - 1e-9 for s in p["sells"]), failures)
    _check("цена ниже стопа → план не строится",
           not plan_ladder(0.70, 0.95, 50)["ok"], failures)
    _check("бюджет меньше минимума → план не строится",
           not plan_ladder(1.0, 0.95, 8)["ok"], failures)
    p = plan_ladder(1.30, 0.95, 50)
    _check("цена далеко над базой → предупреждение «не вход у дна»",
           any("не вход у дна" in w for w in p["warns"]), failures)


def test_bybit_sync(cfg, failures: list[str]) -> None:
    print("Bybit sync — подпись V5, исполнения -> реальные позиции, идемпотентность:")
    import json as _json
    import tempfile
    import urllib.parse as _up
    from pathlib import Path as _P
    from scanner import bybit, sync
    from scanner.config import Config

    # Эталон — функция gen_signature официального примера bybit-exchange/api-usage-examples
    # (V5_demo/api_demo/Encryption_HMAC.py; key = secret = "XXXXXXXXXX"), запущенная на строке
    # GET из docs/v5/guide: timestamp 1658384314791, recv_window 5000.
    q = "category=option&symbol=BTC-29JUL22-25000-C"
    _check("подпись: совпадает с официальным примером Bybit (HMAC-SHA256, hex)",
           bybit.sign("XXXXXXXXXX", "1658384314791", "XXXXXXXXXX", "5000", q)
           == "c00720f96c5934ca7057ac28ae65b823f83b8b67a8fe784e7795ca0fa3c148ec", failures)

    # Клиент: подписывается ровно та строка запроса, что ушла в URL; секрет не уходит в сеть.
    calls = []

    def fake(pages):
        def _f(url, headers, timeout):
            calls.append((url, headers))
            qs = dict(_up.parse_qsl(_up.urlsplit(url).query))
            body = pages(url, qs)
            return (body.pop("_status", 200), _json.dumps(body).encode())
        return _f

    ok_body = lambda result: {"retCode": 0, "retMsg": "OK", "result": result}  # noqa: E731
    cl = bybit.Client("KEY1", "SECRET1", fetch=fake(lambda u, qs: ok_body({"list": []})),
                      clock=lambda: 1700000000.123, pause=0)
    cl.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    url, h = calls[-1]
    sent_q = _up.urlsplit(url).query
    _check("клиент: X-BAPI-* заголовки, timestamp в мс, подпись над строкой из URL",
           h["X-BAPI-API-KEY"] == "KEY1" and h["X-BAPI-TIMESTAMP"] == "1700000000123"
           and h["X-BAPI-RECV-WINDOW"] == "5000" and h["X-BAPI-SIGN-TYPE"] == "2"
           and h["X-BAPI-SIGN"] == bybit.sign("SECRET1", "1700000000123", "KEY1", "5000", sent_q)
           and sent_q == "accountType=UNIFIED", failures)
    _check("клиент: секрет не попадает ни в URL, ни в заголовки",
           "SECRET1" not in url and all("SECRET1" not in v for v in h.values()), failures)
    bad = bybit.Client("KEY1", "SECRET1", pause=0, fetch=fake(
        lambda u, qs: {"retCode": 10004, "retMsg": "error sign!", "result": {}}))
    try:
        bad.get("/v5/execution/list", {"category": "spot"})
        err = None
    except bybit.BybitError as e:
        err = e
    _check("клиент: retCode 10004 -> BybitError с кодом и подсказкой, без секрета",
           err is not None and err.code == 10004 and "BYBIT_API_SECRET" in str(err)
           and "SECRET1" not in str(err), failures)
    unauth = bybit.Client("KEY1", "SECRET1", pause=0,
                          fetch=lambda url, headers, timeout: (401, b""))
    try:
        unauth.wallet_balance()
        err401 = None
    except bybit.BybitError as e:
        err401 = e
    _check("клиент: HTTP 401 с пустым телом (так отвечает wallet-balance) -> «ключ не принят»",
           err401 is not None and "HTTP 401" in str(err401) and "ключ не принят" in str(err401),
           failures)
    try:
        bybit.Client("", "")
        no_key = False
    except bybit.BybitError:
        no_key = True
    _check("клиент: без ключа — BybitError до любого запроса", no_key, failures)

    # Исполнения: окна по 7 дней (предел запроса), страницы по курсору, дубли execId отброшены.
    calls.clear()
    day_ms = 86400 * 1000

    def ex_pages(u, qs):
        s = int(qs["startTime"])
        if not qs.get("cursor"):
            return ok_body({"list": [{"execId": f"a{s}"}, {"execId": "dup"}],
                            "nextPageCursor": "p2"})
        return ok_body({"list": [{"execId": f"b{s}"}, {"execId": "dup"}], "nextPageCursor": ""})
    cl = bybit.Client("K", "S", fetch=fake(ex_pages), pause=0)
    got = cl.executions(0, 15 * day_ms)
    windows = sorted({int(dict(_up.parse_qsl(_up.urlsplit(u).query))["startTime"])
                      for u, _ in calls})
    spans = [int(dict(_up.parse_qsl(_up.urlsplit(u).query))["endTime"])
             - int(dict(_up.parse_qsl(_up.urlsplit(u).query))["startTime"]) for u, _ in calls]
    _check("исполнения: 15 дней -> 3 окна ≤ 7 дней, по 2 страницы, дубль execId один раз",
           windows == [0, 7 * day_ms, 14 * day_ms] and len(calls) == 6
           and max(spans) < 7 * day_ms and len(got) == 7
           and sum(1 for g in got if g["execId"] == "dup") == 1, failures)

    # Разбор фикстуры: формат /v5/execution/list (строки), спот, комиссия в базовой монете.
    t0 = 1759600000.0
    fx = lambda eid, side, px, qty, fee, cur, dt, sym="LINKUSDT", **kw: {  # noqa: E731
        "symbol": sym, "side": side, "execId": eid, "orderId": "o" + eid, "execType": "Trade",
        "execPrice": str(px), "execQty": str(qty), "execValue": str(px * qty),
        "execFee": str(fee), "feeCurrency": cur, "feeRate": "0.001", "isMaker": False,
        "execTime": str(int((t0 + dt) * 1000)), **kw}
    buy1 = fx("e1", "Buy", 20.0, 5.0, 0.005, "LINK", 0)        # комиссия в LINK
    buy2 = fx("e2", "Buy", 16.0, 2.5, 0.04, "USDT", 3600)      # комиссия в USDT (maker)
    sell1 = fx("e3", "Sell", 30.0, 3.0, 0.09, "USDT", 7200)
    sell2 = fx("e4", "Sell", 31.0, 4.495, 0.139345, "USDT", 10800)   # весь остаток
    p1 = sync.parse_fill(buy1)
    _check("разбор: покупка с комиссией в базовой монете -> на счёт пришло execQty − execFee",
           p1["symbol"] == "LINK" and p1["side"] == "buy" and abs(p1["base_delta"] - 4.995) < 1e-12
           and abs(p1["fee_usdt"] - 0.1) < 1e-12 and abs(p1["ts"] - t0) < 1e-6, failures)
    p3 = sync.parse_fill(sell1)
    _check("разбор: продажа с комиссией в USDT -> ставка = fee / value",
           abs(p3["base_delta"] + 3.0) < 1e-12 and abs(p3["fee_rate"] - 0.001) < 1e-12, failures)
    _check("разбор: не сделка (Funding), не пара к USDT, мусор -> None",
           sync.parse_fill({**buy1, "execType": "Funding"}) is None
           and sync.parse_fill({**buy1, "symbol": "LINKBTC"}) is None
           and sync.parse_fill({**buy1, "execQty": ""}) is None, failures)

    class FakeClient:
        def __init__(self, items, wallet):
            self.items, self.wallet, self.windows = items, wallet, []

        def executions(self, start_ms, end_ms):
            self.windows.append((start_ms, end_ms))
            return [it for it in self.items if start_ms <= int(it["execTime"]) <= end_ms]

        def wallet_balance(self):
            return self.wallet

    with tempfile.TemporaryDirectory() as tmp:
        d = _json.loads(_json.dumps(cfg._d))
        d["output"] = {"db_path": str(_P(tmp) / "t.db"), "watchlist_json": str(_P(tmp) / "w.json")}
        d["api_keys"]["bybit_key"] = d["api_keys"]["bybit_secret"] = ""
        c = Config(d)
        ps = PositionStore(d["output"]["db_path"])
        code, lines = sync.run_sync(c, ps, now=t0)
        _check("без ключа: код 0, «пропуск», в базе ничего",
               code == 0 and "пропуск" in lines[0] and not ps.all_positions()
               and not ps.system_flag(sync.FLAG), failures)

        paper = ps.add("LINK", 19.0, 10.0, paper=True)
        wallet = {"LINK": {"balance": 4.995 + 2.5 - 3.0, "usd": 140.0, "locked": 0.0},
                  "USDT": {"balance": 500.0, "usd": 500.0, "locked": 0.0},
                  "ARB": {"balance": 100.0, "usd": 40.0, "locked": 0.0},
                  "PEPE": {"balance": 1e5, "usd": 0.7, "locked": 0.0}}
        fc = FakeClient([buy1, buy2, sell1, fx("e9", "Sell", 1.0, 5.0, 0.005, "USDT", 50,
                                                 sym="OPUSDT")], wallet)
        look = lambda sym: {"coin_id": "chainlink"} if sym == "LINK" else {}  # noqa: E731
        code, lines = sync.run_sync(c, ps, client=fc, lookup=look, now=t0 + 8000)
        real = [p for p in ps.all_positions() if not p["is_paper"]]
        pos = real[0] if len(real) == 1 else {}
        avg = (20.0 * 4.995 + 16.0 * 2.5) / 7.495
        _check("sync: покупка + докупка -> одна реальная позиция, средняя по количеству",
               len(real) == 1 and pos["symbol"] == "LINK" and pos["coin_id"] == "chainlink"
               and pos["venue"] == "bybit" and abs(pos["entry_ts"] - t0) < 1e-6
               and abs(pos["initial_qty"] - 7.495) < 1e-9, failures)
        _check("sync: продажа 3 -> остаток 4.495, realized по фактической ставке 0.1%",
               abs(pos.get("qty", 0) - 4.495) < 1e-9
               and abs(pos["realized_usdt"] - (3 * 30 * 0.999 - 3 * avg * 1.001)) < 1e-9
               and abs(pos["entry_price"] - avg) < 1e-9, failures)
        _check("sync: paper-позиция той же монеты не тронута",
               ps.get(paper)["qty"] == 10.0 and ps.get(paper)["status"] == "open", failures)
        _check("sync: продажа без позиции записана, позиция не открыта",
               any("OP: продажа" in x and "позиции нет" in x for x in lines)
               and not any(p["symbol"] == "OP" for p in ps.all_positions())
               and len(ps.fills()) == 4, failures)
        _check("сверка: баланс LINK совпал -> молчим; ARB на счёте без позиции -> строка; "
               "USDT и пыль PEPE -> молчим",
               not any("LINK: на счёте" in x for x in lines)
               and any(x.startswith("ℹ ARB") for x in lines)
               and not any(x.startswith("ℹ USDT") or "PEPE" in x for x in lines), failures)
        _check("sync: первый раз — окно backfill_days назад, отметка курсора записана",
               fc.windows[0][0] == int((t0 + 8000 - d["bybit_sync"]["backfill_days"] * 86400)
                                       * 1000) and ps.system_flag(sync.FLAG), failures)

        # Повтор: те же исполнения снова в окне -> ничего не задваивается.
        before = (ps.get(pos["id"])["qty"], ps.get(pos["id"])["entry_price"],
                  ps.get(pos["id"])["realized_usdt"], len(ps.fills()))
        fc.items.append(sell2)
        fc.wallet = {"LINK": {"balance": 0.0, "usd": 0.0, "locked": 0.0}}
        last = ps.last_event_ts(0, sync.FLAG)
        code2, lines2 = sync.run_sync(c, ps, client=fc, lookup=look, now=t0 + 20000)
        after = ps.get(pos["id"])
        _check("повторный sync: старые 4 исполнения пропущены, новое применено один раз",
               code2 == 0 and any("новых 1, уже учтённых 4" in x for x in lines2)
               and len(ps.fills()) == before[3] + 1, failures)
        _check("повторный sync: окно с прошлого sync − overlap_days",
               fc.windows[-1][0] == int((last - d["bybit_sync"]["overlap_days"] * 86400) * 1000),
               failures)
        _check("продажа всего остатка -> позиция закрыта, realized накоплен",
               after["status"] == "closed" and after["qty"] == 0
               and after["realized_usdt"] > before[2], failures)
        code3, _ = sync.run_sync(c, ps, client=fc, lookup=look, now=t0 + 30000)
        _check("третий sync без новых исполнений: позиции и журнал не меняются",
               code3 == 0 and len(ps.fills()) == before[3] + 1
               and ps.get(pos["id"])["realized_usdt"] == after["realized_usdt"], failures)

        # Пыль: продали почти всё, остаток дешевле dust_usdt — закрывается.
        b = fx("d1", "Buy", 2.0, 10.0, 0.0, "USDT", 40000, sym="ARBUSDT")
        s_ = fx("d2", "Sell", 2.2, 9.7, 0.0, "USDT", 40100, sym="ARBUSDT")
        fc.items += [b, s_]
        fc.wallet = {}
        sync.run_sync(c, ps, client=fc, lookup=look, now=t0 + 41000)
        arb = next(p for p in ps.all_positions() if p["symbol"] == "ARB")
        _check("пыль: остаток 0.3 ARB (< 1 USDT) после продажи -> позиция закрыта",
               arb["status"] == "closed" and "bybit_dust" in ps.event_types(arb["id"])
               and not arb["coin_id"], failures)
        # Сверка: позиция больше, чем есть на счёте -> предупреждение.
        fc.items.append(fx("m1", "Buy", 1.0, 50.0, 0.0, "USDT", 42000, sym="OPUSDT"))
        fc.wallet = {"OP": {"balance": 20.0, "usd": 20.0, "locked": 0.0}}
        _, lines4 = sync.run_sync(c, ps, client=fc, lookup=look, now=t0 + 43000)
        _check("сверка: в позиции 50 OP, на счёте 20 -> «сверь вручную»",
               any(x.startswith("⚠ OP: на счёте 20") for x in lines4), failures)
        ps.close_db()

        # Сводка дня: код sync ≠ 0 -> пометка в первой строке; 0 или не запускали — молчим.
        from scanner.db import Store
        from scanner.notify import telegram as tg
        from scanner.notify.deliver import brief_state
        Store(d["output"]["db_path"]).close()
        s1 = brief_state(c, now=t0, sync_exit=1)
        s0 = brief_state(c, now=t0, sync_exit=0)
        sn = brief_state(c, now=t0)
    _check("brief_state: sync-exit 1 -> sync_fail, 0 и без кода -> нет",
           s1["sync_fail"] and not s0["sync_fail"] and not sn["sync_fail"], failures)
    st = {"scan": {"ran": True, "ok": True, "elapsed_min": 20.0, "watchlist": 80},
          "watch_ok": True, "market": {}, "new": [], "muted": [], "near": [], "positions": [],
          "signals_today": [], "unavailable": [], "dev_github": None}
    _check("сводка: sync упал -> «⚠ синхронизация с Bybit не прошла» в первой строке",
           "⚠ синхронизация с Bybit не прошла" in tg.format_brief(
               {**st, "sync_fail": True}, cfg, now=t0).split("\n")[0]
           and "Bybit" not in tg.format_brief(st, cfg, now=t0), failures)


class _FakeMarket:
    """Рынок для пробного исполнителя: те же методы, что executor.BybitMarket. Свечи отдаются
    только закрытые к self.now (как Bybit); hourly — все с start, фильтр по постановке — в коде."""

    def __init__(self, now: float):
        self.now = now
        self.inst: dict = {}          # пара -> instrument | None (нет на Bybit)
        self.price: dict = {}         # пара -> последняя цена
        self.days: dict = {}          # пара -> [(ts дня, close)]
        self.hours: dict = {}         # пара -> [(ts часа, low)]
        self.fail: set = set()        # пары, на которых daily бросает исключение

    def instrument(self, pair):
        return self.inst.get(pair)

    def last_price(self, pair):
        return self.price.get(pair)

    def daily(self, pair):
        if pair in self.fail:
            raise RuntimeError("биржа не ответила")
        rows = [(t, c) for t, c in self.days.get(pair, []) if t + 86400 <= self.now]
        return {"ts": [t for t, _ in rows], "c": [c for _, c in rows],
                "o": [c for _, c in rows], "h": [c for _, c in rows], "l": [c for _, c in rows]}

    def hourly(self, pair, start_ts):
        rows = [(t, lo) for t, lo in self.hours.get(pair, []) if t + 3600 <= self.now]
        return {"ts": [t for t, _ in rows], "l": [lo for _, lo in rows]}


def test_executor(cfg, failures: list[str]) -> None:
    print("Пробный исполнитель (dry-run): лестница, исполнения, выходы R/H, отказы, сводки:")
    import copy
    import json as _json
    import sqlite3
    import tempfile
    from pathlib import Path as _P
    from scanner import executor as ex
    from scanner.config import Config
    from scanner.db import Store
    from scanner.ladder import LIMIT_FEE, MARKET_FEE, round_step
    from scanner.notify import telegram as tg
    from scanner.notify.deliver import brief_state as deliver_brief
    from scanner.sources import bybit
    D, H = ex.DAY, ex.HOUR
    d0 = 1790812800                        # 2026-10-01 00:00 UTC
    now0 = d0 + 6 * H + 1800               # постановка в 06:30 UTC — не на границе часа
    inst = {"symbol": "", "status": "Trading", "st": False, "tick": 0.01, "qty_step": 0.1,
            "min_qty": 0.1, "min_amt": 5.0}
    # история: 40 дней по 1.0, за 10 дней до карточки — 0.95 → лоу базы 0.95, пол −25% 0.7125
    hist = [(d0 - k * D, 0.95 if k == 10 else 1.0) for k in range(40, 0, -1)]

    def make_cfg(tmp: str, **exe) -> Config:
        d = copy.deepcopy(cfg._d)
        d["output"] = {**d.get("output", {}), "db_path": str(_P(tmp) / "t.db"),
                       "watchlist_json": str(_P(tmp) / "wl.json")}
        d["executor"] = {**d.get("executor", {}), **exe}
        (_P(tmp) / "wl.json").write_text(_json.dumps([{"symbol": "AAA", "coin_id": "aaa-coin"}]),
                                         encoding="utf-8")
        Store(d["output"]["db_path"]).close()          # схема сканера: alert_log, market_daily
        return Config(d)

    def card(c: Config, sym: str, ts: float, score: float = 72.0) -> None:
        con = sqlite3.connect(c["output"]["db_path"])
        con.execute("INSERT INTO alert_log(symbol, ts, score) VALUES (?,?,?)", (sym, ts, score))
        con.commit()
        con.close()

    def q(c: Config, sql: str, args=()) -> list[dict]:
        con = sqlite3.connect(c["output"]["db_path"])
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, args).fetchall()]
        finally:
            con.close()

    def add_pair(m: _FakeMarket, pair: str, after: list, hours: list) -> None:
        m.inst[pair] = {**inst, "symbol": pair}
        m.price[pair] = 1.0
        m.days[pair] = hist + after
        m.hours[pair] = hours

    with tempfile.TemporaryDirectory() as tmp:
        c = make_cfg(tmp)
        m = _FakeMarket(now0)
        # AAA: рост 1.5 (+50%) → 2.6 (+150%) → 1.7 (откат 35% от пика — трейл)
        add_pair(m, "AAAUSDT", [(d0, 1.0), (d0 + D, 1.5), (d0 + 2 * D, 2.6), (d0 + 3 * D, 1.7)],
                 [(d0 + 6 * H, 0.90),     # началась ДО постановки — не в счёт, хотя ниже 0.93
                  (d0 + 7 * H, 0.93),     # касание: low == цене лимитки — не исполнена
                  (d0 + 8 * H, 0.929)])   # прошла сквозь — исполнена ступень 2
        # BBB: падение; лимитки исполнятся все, два закрытия ниже пола 0.7125 — стоп обеим книгам
        add_pair(m, "BBBUSDT", [(d0, 0.80), (d0 + D, 0.70), (d0 + 2 * D, 0.70)],
                 [(d0 + 7 * H, 0.92), (d0 + D + 5 * H, 0.69)])
        card(c, "AAA", now0 - 60)         # раньше BBB: позиции AAA получат id 1 (R) и 2 (H)
        card(c, "BBB", now0)
        code0, lines0 = ex.run_dry(c, m, now=now0, wallet_usdt=30.0)
        pos0 = q(c, "SELECT * FROM dry_positions ORDER BY id")
        ord0 = q(c, "SELECT * FROM dry_orders ORDER BY link_id")
        bs0 = deliver_brief(c, now=now0, exec_exit=0)
        code1, lines1 = ex.run_dry(c, m, now=now0)
        n_pos1 = len(q(c, "SELECT id FROM dry_positions"))
        n_ord1 = len(q(c, "SELECT link_id FROM dry_orders"))
        aaa_r = next(p for p in pos0 if p["symbol"] == "AAA" and p["book"] == "R")
        aaa_h = next(p for p in pos0 if p["symbol"] == "AAA" and p["book"] == "H")
        buys = [o for o in ord0 if o["position_id"] == aaa_r["id"]]
        buys.sort(key=lambda o: o["step"])

        # (а) размер и цены ступеней
        _check("лестница: 5 ступеней по ~$10, ступень 1 рынком, 2–5 лимитками",
               code0 == 0 and len(buys) == 5 and buys[0]["kind"] == "Market"
               and all(o["kind"] == "Limit" for o in buys[1:])
               and all(10 - 1e-9 <= o["usd"] < 10.2 for o in buys), failures)
        _check("цены лимиток вниз к tick 0.01: 0.93/0.87/0.81/0.74 (не 0.94/…/0.75)",
               [o["price"] for o in buys[1:]] == [0.93, 0.87, 0.81, 0.74], failures)
        _check("нижняя лимитка ≥ пола стопа 0.7125 и ≤ 1.05 × пола",
               0.7125 < buys[-1]["price"] <= round_step(0.95 * 0.75 * 1.05, 0.01), failures)
        _check("количества кратны qty_step 0.1",
               all(abs(o["qty"] / 0.1 - round(o["qty"] / 0.1)) < 1e-9 for o in buys), failures)
        _check("ступень 1 исполнена по цене запуска, 0.15% в монете",
               buys[0]["status"] == "filled" and abs(aaa_r["qty"] - 10.0 * (1 - MARKET_FEE)) < 1e-9
               and abs(aaa_r["spent_usdt"] - 10.0) < 1e-9
               and abs(buys[0]["fee_usdt"] - 10.0 * MARKET_FEE) < 1e-12, failures)
        _check("лоу базы 0.95, стоп 25%, минимум биржи и coin_id из watchlist",
               aaa_r["base_low"] == 0.95 and aaa_r["stop_pct"] == 25 and aaa_r["min_amt"] == 5.0
               and aaa_r["coin_id"] == "aaa-coin", failures)
        _check("лог: «поставил бы в книги R/H» и справка реального счёта «НЕ хватило бы»",
               any("AAA: поставил бы в книги R/H — 5 ступ." in x for x in lines0)
               and any("свободно 30.00 USDT" in x and "НЕ хватило бы" in x for x in lines0),
               failures)

        # (б) идемпотентность и orderLinkId
        _check("повтор run_dry в тот же день: позиций и ордеров не прибавилось",
               code1 == 0 and n_pos1 == len(pos0) == 4 and n_ord1 == len(ord0) == 20
               and any("уже обработана" in x for x in lines1), failures)
        _check("orderLinkId: dry-R1-B1 … dry-R1-B5, dry-H2-B1; все ≤ 36 символов",
               [o["link_id"] for o in buys] == [f"dry-R{aaa_r['id']}-B{n}" for n in range(1, 6)]
               and aaa_r["id"] == 1 and aaa_h["id"] == 2
               and any(o["link_id"] == "dry-H2-B1" for o in ord0)
               and all(len(o["link_id"]) <= 36 for o in ord0), failures)
        try:
            ex.link_id("R", 1, "X" * 40)
            long_ok = False
        except ValueError:
            long_ok = True
        _check("link_id длиннее 36 -> ValueError", long_ok, failures)
        _check("signal_tail: ladder_0 → S-L0, trailing → S-TR, invalidation → S-INV",
               ex.signal_tail("ladder_0") == "S-L0" and ex.signal_tail("trailing") == "S-TR"
               and ex.signal_tail("invalidation") == "S-INV", failures)

        # (з) сводка дня после постановки
        exl0 = tg.executor_brief_lines(bs0.get("executor"), c)
        _check("сводка дня: «🤖 Пробный исполнитель», поставил бы AAA и BBB, P&L книг",
               bool(exl0) and exl0[0].startswith("🤖 <b>Пробный исполнитель</b>")
               and any(x.startswith("поставил бы лестницу:") and "AAA" in x and "BBB" in x
                       for x in exl0)
               and any("📏 правила: 2 поз." in x and "✋ держать: 2 поз." in x for x in exl0),
               failures)

        # (в) исполнение лимиток по часовым свечам
        m.now = d0 + 8 * H
        ex.run_dry(c, m, now=m.now)
        st2a = q(c, "SELECT status FROM dry_orders WHERE link_id='dry-R1-B2'")[0]["status"]
        m.now = d0 + 9 * H
        _, lines_b = ex.run_dry(c, m, now=m.now)
        o2 = q(c, "SELECT * FROM dry_orders WHERE link_id='dry-R1-B2'")[0]
        o3 = q(c, "SELECT status FROM dry_orders WHERE link_id='dry-R1-B3'")[0]["status"]
        _check("лимитка 0.93: свеча до постановки (low 0.90) и касание (low 0.93) — не исполнена",
               st2a == "new", failures)
        _check("low 0.929 < 0.93 — исполнена по цене лимитки, комиссия 0.1%; ступень 3 ждёт",
               o2["status"] == "filled" and o2["price"] == 0.93 and o2["filled_ts"] == d0 + 9 * H
               and abs(o2["fee_usdt"] - o2["usd"] * LIMIT_FEE) < 1e-12 and o3 == "new"
               and any("R AAA: исполнилась бы ступень 2 по 0.93" in x for x in lines_b), failures)

        # (г) выходы: дни d0, d0+1 закрыты → R фиксирует 1/3 на +50%; BBB одно закрытие ниже пола
        m.now = d0 + 2 * D + H
        ex.run_dry(c, m, now=m.now)
        r_mid = q(c, "SELECT * FROM dry_positions WHERE id=1")[0]
        sells_mid = q(c, "SELECT * FROM dry_orders WHERE side='Sell' ORDER BY link_id")
        bbb_mid = q(c, "SELECT status, qty FROM dry_positions WHERE symbol='BBB'")
        m.now = d0 + 4 * D + H
        _, lines_d = ex.run_dry(c, m, now=m.now)
        pos = {(p["symbol"], p["book"]): p for p in q(c, "SELECT * FROM dry_positions")}
        sells = q(c, "SELECT * FROM dry_orders WHERE side='Sell' ORDER BY created_ts, link_id")
        _check("R: на +50% (закрытие 1.5) продана 1/3 купленного, ордер dry-R1-S-L0",
               [s["link_id"] for s in sells_mid] == ["dry-R1-S-L0"]
               and abs(sells_mid[0]["qty"] - 0.33 * r_mid["bought_qty"]) < 1e-9
               and sells_mid[0]["price"] == 1.5, failures)
        _check("BBB: одно закрытие ниже пола — ещё не стоп (нужно 2 подряд), лимитки исполнены",
               all(p["status"] == "open" for p in bbb_mid) and len(bbb_mid) == 2
               and len(q(c, "SELECT 1 FROM dry_orders o JOIN dry_positions p ON p.id=o.position_id"
                            " WHERE p.symbol='BBB' AND o.side='Buy' AND o.status='filled'")) == 10,
               failures)
        r_sig = [s["signal"] for s in sells if s["position_id"] == pos[("AAA", "R")]["id"]]
        _check("R AAA: 1/3 на +50%, 1/3 на +150%, остаток по трейлу — позиция закрыта",
               r_sig == ["ladder_0", "ladder_1", "trailing"]
               and pos[("AAA", "R")]["status"] == "closed" and pos[("AAA", "R")]["qty"] == 0
               and pos[("AAA", "R")]["reason"] == "трейл", failures)
        r_c = q(c, "SELECT status FROM dry_orders WHERE position_id=? AND side='Buy' ORDER BY "
                   "step", (pos[("AAA", "R")]["id"],))
        h_c = q(c, "SELECT status FROM dry_orders WHERE position_id=? AND side='Buy' ORDER BY "
                   "step", (pos[("AAA", "H")]["id"],))
        _check("R закрыта → лимитки 3–5 сняты; у H они живы",
               [o["status"] for o in r_c] == ["filled", "filled"] + ["cancelled"] * 3
               and [o["status"] for o in h_c] == ["filled", "filled"] + ["new"] * 3
               and any("R AAA: снял бы неисполненные лимитки (3)" in x for x in lines_d), failures)
        _check("H AAA «держать»: ни одной продажи на +50/+150 и откате, позиция открыта",
               not any(s["position_id"] == pos[("AAA", "H")]["id"] for s in sells)
               and pos[("AAA", "H")]["status"] == "open" and pos[("AAA", "H")]["last_price"] == 1.7,
               failures)
        bbb = [s for s in sells if s["pair"] == "BBBUSDT"]
        _check("BBB: два закрытия ниже лоу базы −25% — обе книги вышли полностью по стопу",
               sorted(s["book"] for s in bbb) == ["H", "R"]
               and all(s["signal"] == "invalidation" and s["price"] == 0.70 for s in bbb)
               and all(pos[("BBB", b)]["status"] == "closed" and pos[("BBB", b)]["qty"] == 0
                       and pos[("BBB", b)]["reason"] == "стоп" for b in "RH"), failures)

        # (ж) P&L с комиссиями — пересчёт руками
        bought = 10.0 * (1 - MARKET_FEE) + 10.8 * (1 - LIMIT_FEE)
        spent = 10.0 * 1.0 + 10.8 * 0.93
        q1 = q2 = 0.33 * bought
        proceeds = (q1 * 1.5 + q2 * 2.6 + (bought - q1 - q2) * 1.7) * (1 - MARKET_FEE)
        pr = pos[("AAA", "R")]
        ph = pos[("AAA", "H")]
        _check("P&L R AAA: Σ продаж × (1 − 0.15%) − (10 + 10.8 × 0.93)",
               abs(pr["spent_usdt"] - spent) < 1e-9 and abs(pr["bought_qty"] - bought) < 1e-9
               and abs(ex.pnl_usdt(pr) - (proceeds - spent)) < 1e-9, failures)
        _check("P&L H AAA: остаток × последнее закрытие 1.7 × (1 − 0.15%) − потрачено",
               abs(ex.pnl_usdt(ph) - (bought * 1.7 * (1 - MARKET_FEE) - spent)) < 1e-9, failures)
        fees = q(c, "SELECT kind, side, usd, fee_usdt FROM dry_orders WHERE status='filled'")
        _check("комиссии в журнале: рынок 0.15%, лимит 0.1%, продажа 0.15%",
               all(abs(f["fee_usdt"] - f["usd"] * (LIMIT_FEE if f["kind"] == "Limit"
                                                    else MARKET_FEE)) < 1e-12 for f in fees),
               failures)
        con_ = sqlite3.connect(c["output"]["db_path"])
        free_r = ex.free_usdt(con_, "R", 1500.0)
        con_.close()
        res_r = 0.0                                          # у R лимиток не осталось
        _check("свободный USDT книги R = 1500 + Σ(выручка − траты) − резерв лимиток",
               abs(free_r - (1500 + sum(p["proceeds_usdt"] - p["spent_usdt"]
                                        for (_, b), p in pos.items() if b == "R") - res_r))
               < 1e-9, failures)

        # (е) парность: монета в обеих книгах или ни в одной
        by_pair: dict = {}
        for p in q(c, "SELECT pair, book, card_day FROM dry_positions"):
            by_pair.setdefault((p["pair"], p["card_day"]), set()).add(p["book"])
        _check("парность: каждая (пара, день карточки) — в обеих книгах",
               all(v == {"R", "H"} for v in by_pair.values()), failures)

        # (з) сводка дня после выходов и недельный блок по той же БД
        bs = deliver_brief(c, now=m.now, exec_exit=1)
        exl = tg.executor_brief_lines(bs["executor"], c)
        _check("сводка дня: продажи за сутки «R AAA трейл», «H BBB стоп»; H — 1 поз.",
               bs["exec_fail"] and any("продажи:" in x and "R AAA трейл" in x and "H BBB стоп" in x
                                       for x in exl)
               and any("✋ держать: 1 поз." in x for x in exl), failures)
        st_ = Store(c["output"]["db_path"])
        st_.upsert_market(
            {d0 + k * D: {"total_mcap": 1000 + 100 * k, "btc_dominance": 50, "stables_usd": 100}
             for k in range(0, 6)})
        st_.close()
        wb = ex.weekly_books(c)
        wr = next((b for b in wb if b["key"] == "R"), {})
        exp_r = sum(ex.pnl_usdt(p) for (_, b), p in pos.items() if b == "R") / \
            sum(p["spent_usdt"] for (_, b), p in pos.items() if b == "R") * 100
        _check("недельный: книги R и H, R = Σ P&L / Σ потрачено на тех же окнах",
               [b["key"] for b in wb] == ["R", "H"] and wr.get("n") == 2
               and abs(wr.get("book_pct", 0) - exp_r) < 1e-9 and wr.get("alt_pct", 0) > 0,
               failures)

    # (д) отказы — каждый в своей БД, обеим книгам с причиной
    def reject_case(label: str, setup, want: str, **exe) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = make_cfg(tmp, **exe)
            m = _FakeMarket(now0)
            add_pair(m, "AAAUSDT", [], [])
            setup(c, m)
            card(c, "AAA", now0)
            code, lines = ex.run_dry(c, m, now=now0)
            ps = q(c, "SELECT book, status, reason FROM dry_positions WHERE pair='AAAUSDT'")
            no = q(c, "SELECT 1 FROM dry_orders WHERE pair='AAAUSDT'")
        _check(f"отказ: {label} -> rejected обеим книгам, ордеров нет",
               code == 0 and sorted(p["book"] for p in ps) == ["H", "R"]
               and all(p["status"] == "rejected" and want in p["reason"] for p in ps) and not no
               and any("AAA: ОТКАЗ" in x for x in lines), failures)

    def two_open(c, m):
        con = ex.connect(c["output"]["db_path"])
        for i, b in enumerate(("R", "H", "R", "H")):
            con.execute("INSERT INTO dry_positions(book, pair, symbol, card_day, status, "
                        "created_ts, qty, bought_qty, spent_usdt, last_price) VALUES "
                        "(?,?,?,?,?,?,?,?,?,?)", (b, f"X{i // 2}USDT", f"X{i // 2}", d0 - D,
                                                  "open", now0 - D, 10.0, 10.0, 10.0, 1.0))
        con.commit()
        con.close()

    def only_h_full(c, m):
        con = ex.connect(c["output"]["db_path"])
        con.execute("INSERT INTO dry_positions(book, pair, symbol, card_day, status, created_ts, "
                    "qty, bought_qty, spent_usdt, last_price) VALUES "
                    "('H','XUSDT','X',?,'open',?,10,10,10,1)", (d0 - D, now0 - D))
        con.commit()
        con.close()

    reject_case("лимит max_coins 2", two_open, "лимит 2 монет", max_coins=2)
    reject_case("нехватка USDT (capital 40)", lambda c, m: None, "нехватка USDT", capital_usdt=40)
    reject_case("парность: лимит монет только у H — отказ и R",
                only_h_full, "H: лимит 1 монет", max_coins=1)
    reject_case("пара не на Bybit", lambda c, m: m.inst.update({"AAAUSDT": None}),
                "нет на Bybit spot")
    reject_case("метка ST", lambda c, m: m.inst["AAAUSDT"].update(st=True), "метка ST")
    reject_case("статус не Trading", lambda c, m: m.inst["AAAUSDT"].update(status="PreLaunch"),
                "статус PreLaunch")
    with tempfile.TemporaryDirectory() as tmp:
        c = make_cfg(tmp)
        c._d["stage7_positions"] = {**c._d.get("stage7_positions", {}),
                                    "max_position_pct_of_capital": 2}
        m = _FakeMarket(now0)
        add_pair(m, "AAAUSDT", [], [])
        card(c, "AAA", now0)
        ex.run_dry(c, m, now=now0)
        ps = q(c, "SELECT status, reason FROM dry_positions")
    _check("отказ risk_check: позиция $50 > лимита 2% от $1500 -> rejected обеим",
           len(ps) == 2 and all(p["status"] == "rejected" and "> лимита 30" in p["reason"]
                                for p in ps), failures)

    # Сбой одной позиции не валит остальные, код 1; без карточек — строка «новых лестниц нет».
    with tempfile.TemporaryDirectory() as tmp:
        c = make_cfg(tmp)
        m = _FakeMarket(now0)
        add_pair(m, "AAAUSDT", [], [])
        add_pair(m, "BBBUSDT", [(d0, 1.0)], [])
        card(c, "AAA", now0)
        card(c, "BBB", now0)
        ex.run_dry(c, m, now=now0)
        m.fail.add("AAAUSDT")
        m.now = now0 + D
        code_f, lines_f = ex.run_dry(c, m, now=m.now)
        last_b = q(c, "SELECT last_day FROM dry_positions WHERE symbol='BBB'")
        m.fail.clear()
        empty_code, empty_lines = ex.run_dry(c, m, now=m.now)
        off = ex.run_dry(make_cfg(tmp, enabled=False), m, now=m.now)
    _check("сбой daily по AAA: код 1, «⚠ R AAA: RuntimeError», BBB обработана",
           code_f == 1 and any(x.startswith("⚠ R AAA: RuntimeError") for x in lines_f)
           and all(r["last_day"] == d0 for r in last_b), failures)
    _check("сегодня карточек нет -> «новых лестниц нет», код 0",
           empty_code == 0 and any("новых лестниц нет" in x for x in empty_lines), failures)
    _check("executor.enabled=false -> пропуск", off == (0, ["executor.enabled = false — пропуск"]),
           failures)
    _check("settings: книги из конфига R/H со стопом 25%, _-ключи отброшены",
           list(ex.settings(Config({"executor": {"books": {
               "_note": "x", **cfg["executor"]["books"]}}}))["books"]) == ["R", "H"]
           and all(b["stop_pct"] == 25 for b in ex.settings(cfg)["books"].values()), failures)

    # (з) недельный блок и пометка падения в сводке
    books = [{"emoji": "📏", "label": "правила", "positions": 2, "n": 2, "book_pct": 12.34,
              "alt_pct": 5.0, "basket_pct": 3.0},
             {"emoji": "✋", "label": "держать", "positions": 1, "n": 0}]
    blk = tg.executor_weekly_block(books)
    _check("недельный блок: «📏 правила: +12.3% · альты +5.0% · корзина +3.0% → +7.3 п.п.»",
           blk[0].startswith("🤖 <b>Пробный исполнитель</b>")
           and "📏 правила: +12.3% · альты +5.0% · корзина +3.0% → +7.3 п.п. к альтам (2 поз.)"
           in blk and "✋ держать: нет рынка на даты позиций (1 поз.)" in blk, failures)
    _check("недельный блок: позиций нет -> пусто; в format_weekly — только с книгами",
           tg.executor_weekly_block([]) == []
           and tg.executor_weekly_block([dict(books[0], positions=0)]) == [], failures)
    base = {"week_no": 2, "milestone": False, "milestone_weeks": 4, "opened": 0, "open_now": 2,
            "invalidations": 0, "ladder_hits": 0, "trailings": 0, "paper_pnl_usdt": 0.0,
            "real_open": 0}
    _check("format_weekly: блок исполнителя есть только с книгами",
           "Пробный исполнитель" in format_weekly({**base, "executor": books}, cfg)
           and "Пробный исполнитель" not in format_weekly(base, cfg), failures)
    _check("сводка дня: нет таблиц/позиций -> блока нет",
           tg.executor_brief_lines(None, cfg) == []
           and tg.executor_brief_lines({"books": {"R": {"open": 0, "closed": 0}}, "opened": [],
                                        "rejected": []}, cfg) == [], failures)
    st = {"scan": {"ran": True, "ok": True, "elapsed_min": 20.0, "watchlist": 80},
          "watch_ok": True, "market": {}, "new": [], "muted": [], "near": [], "positions": [],
          "signals_today": [], "unavailable": [], "dev_github": None}
    _check("сводка: исполнитель упал -> «⚠ пробный исполнитель упал» в первой строке",
           "⚠ пробный исполнитель упал" in tg.format_brief(
               {**st, "exec_fail": True}, cfg, now=now0).split("\n")[0]
           and "исполнитель" not in tg.format_brief(st, cfg, now=now0), failures)

    # (и) часовые свечи: незакрытая отброшена, порядок oldest→newest
    t = (d0 + 5 * H) * 1000
    raw = {"result": {"list": [[str(t), "1", "1", "0.5", "1", "1", "1"],
                               [str(t - 3_600_000), "1", "1", "0.6", "1", "1", "1"],
                               [str(t - 7_200_000), "1", "1", "0.7", "1", "1", "1"]]}}
    hk = bybit.parse_klines(raw, t + 1_800_000, 3_600_000)
    _check("parse_klines: свеча 05:00 ещё идёт — отброшена; 03:00, 04:00 по порядку",
           hk["ts"] == [d0 + 3 * H, d0 + 4 * H] and hk["l"] == [0.7, 0.6], failures)
    _check("parse_klines: ровно на закрытии свеча уже закрыта",
           bybit.parse_klines(raw, t + 3_600_000, 3_600_000)["ts"][-1] == d0 + 5 * H, failures)


def main() -> int:
    cfg = load_config()
    failures: list[str] = []
    print("=== SELFTEST (офлайн, без сети) ===\n")
    test_filters(cfg, failures)
    print()
    test_track_q(cfg, failures)
    print()
    test_quality_refresh(cfg, failures)
    print()
    test_backup(cfg, failures)
    print()
    test_bybit_sync(cfg, failures)
    print()
    test_antirug_evm(cfg, failures)
    print()
    test_honeypot_is(cfg, failures)
    print()
    test_fundamentals(cfg, failures)
    print()
    test_zone(cfg, failures)
    print()
    test_closed_daily(cfg, failures)
    print()
    test_retry_wait(cfg, failures)
    print()
    test_regime(cfg, failures)
    print()
    test_rf_gate(cfg, failures)
    print()
    test_liveness(cfg, failures)
    print()
    test_sources_parse(cfg, failures)
    print()
    test_entry_quality(cfg, failures)
    print()
    test_market_regime(cfg, failures)
    print()
    test_coin_context(cfg, failures)
    print()
    test_onchain(cfg, failures)
    print()
    test_score(cfg, failures)
    print()
    test_telegram_format(cfg, failures)
    print()
    test_exit(cfg, failures)
    print()
    test_positions_store(cfg, failures)
    print()
    test_exit_alert_format(cfg, failures)
    print()
    test_benchmark(cfg, failures)
    print()
    test_ladder(cfg, failures)
    print()
    test_executor(cfg, failures)
    print()
    test_github_levels(cfg, failures)
    print()
    if failures:
        print(f"РЕЗУЛЬТАТ: {_FAIL} — провалено {len(failures)}: {failures}")
        return 1
    print(f"РЕЗУЛЬТАТ: {_PASS} — все проверки пройдены")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
