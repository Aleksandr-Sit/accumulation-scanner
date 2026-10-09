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
        _check("копия: scanner-YYYYMMDD-HHMM.db.gz и манифест, без временных файлов рядом",
               bk.NAME_RE.match(name) is not None and name.startswith("scanner-2026100")
               and sorted(p.name for p in out.iterdir()) == sorted([name, bk.MANIFEST]), failures)
        _check("восстановление: gunzip -> integrity ok, 2 позиции, размер как у базы",
               integ == "ok" and n_pos == 2 and restored.stat().st_size == info["db_size"],
               failures)
        _check("строка лога: backup ok, размер, сколько хранится по ярусам",
               bk.ok_line(info).startswith("backup ok: ") and "хранится 1 (последние 3 + "
               "недельные 8 + месячные 6)" in bk.ok_line(info), failures)

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
        thin = last["path"].name.replace(".db.gz", "-tg.db.gz")
        _check("неделя: первая отправка -> тонкая копия документом, без звука, отметка записана",
               r1[0] == 0 and len(sent) == 1 and sent[0]["silent"] is True
               and sent[0]["filename"] == thin
               and gzip.decompress(sent[0]["data"]).startswith(b"SQLite format 3\x00")
               and flags() == 1, failures)
        cap = sent[0]["caption"]
        _check("подпись: дата, размер, как восстановить (gunzip → scanner.db)",
               "Бэкап базы сканера" in cap and thin in cap and " КБ" in cap
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


def test_backup_guard(cfg, failures: list[str]) -> None:
    print("Бэкап — пустая база не вытесняет копии, ярусы хранения, тонкая копия, восстановление:")
    import argparse
    import contextlib
    import copy
    import gzip
    import html as _html
    import io
    import json as _json
    import os
    import re
    import shutil
    import sqlite3
    import subprocess
    import sys
    import tempfile
    from datetime import datetime as _dt, timedelta as _td
    from pathlib import Path as _P
    import run as _run
    from scanner import backup as bk
    from scanner import executor
    from scanner.config import Config
    from scanner.db import Store
    from scanner.notify import telegram as tg
    day = 86400
    # i-й день от пн 05.10.2026, 10:00 местного: имена и ярусы не зависят от пояса машины
    at = lambda i: (_dt(2026, 10, 5, 10) + _td(days=i)).timestamp()  # noqa: E731
    fname = lambda ts: time.strftime("scanner-%Y%m%d-%H%M.db.gz", time.localtime(ts))  # noqa: E731
    names = lambda out: {p.name for p in bk.list_backups(out)}  # noqa: E731
    manifest = lambda out: _json.loads((out / bk.MANIFEST).read_text("utf-8"))  # noqa: E731

    def build(fill) -> bytes:
        """Файл базы, собранный один раз (схема с fsync на каждую таблицу — медленно)."""
        with tempfile.TemporaryDirectory() as t:
            p = _P(t) / "x.db"
            Store(str(p)).close()
            PositionStore(str(p)).close_db()
            fill(str(p))
            return p.read_bytes()

    def fill_good(p):
        """Живая база: 3 прогона, позиция, событие, таблицы пробного исполнителя."""
        st = Store(p)
        for _ in range(3):
            st.finish_run(st.new_run("x"), 10, 5, 3, {})
        st.close()
        ps = PositionStore(p)
        ps.add("GRAM", 1.0, 100, coin_id="gram", paper=True)
        ps.set_system_flag("x")
        ps.close_db()
        executor.connect(p).close()

    good_file, empty_file = build(fill_good), build(lambda p: None)
    good_db = lambda db: db.write_bytes(good_file)  # noqa: E731
    empty_db = lambda db: db.write_bytes(empty_file)  # noqa: E731 — файл подменён пустой схемой

    def cands(con, run_id, n):
        con.executemany("INSERT INTO candidates(run_id, symbol, coin_id, chain, address, name) "
                        "VALUES (?,?,?,?,?,?)",
                        [(run_id, f"S{i}", f"c{i}", "ethereum", os.urandom(20).hex(),
                          os.urandom(40).hex()) for i in range(n)])

    def conf(db):
        d = copy.deepcopy(cfg._d)
        d["output"] = {"db_path": str(db), "watchlist_json": str(_P(db).parent / "w.json")}
        d["api_keys"] = {"telegram_token": "T", "telegram_chat_id": "C"}
        return Config(d)

    got = []                                         # отправленные документы (сеть подменена)

    def doc(token, chat, data, filename, caption, silent=False):
        got.append({"data": data, "filename": filename, "caption": caption})
        return True

    def no_msg(token, chat, text, silent=False):     # текст вместо файла — тоже без сети
        got.append("msg")
        return True

    # Пустая база: 3 целые копии, потом файл подменён пустой схемой -> 16 дней подряд копии
    # подозрительные, ни одна целая не удалена.
    with tempfile.TemporaryDirectory() as tmp:
        db, out = _P(tmp) / "scanner.db", _P(tmp) / "backups"
        good_db(db)
        good = [bk.make_backup(db, out, now=at(i)) for i in range(3)]
        good_bytes = [g["path"].read_bytes() for g in good]
        empty_db(db)
        sus = [bk.make_backup(db, out, now=at(i)) for i in range(3, 19)]
        why = sus[0]["suspect"] or ""
        _check("пустая база: 16 копий подряд подозрительные — runs 3 → 0, таблиц исполнителя "
               "нет, эталон — последняя целая",
               all(i["suspect"] for i in sus) and why.startswith("runs 3 → 0, positions 1 → 0")
               and "dry_orders 0 → нет таблицы" in why
               and {i["reference"] for i in sus} == {good[-1]["path"].name}, failures)
        _check("пустая база: ничего не удалено, 3 целые копии на месте байт в байт, рядом 16 новых",
               not any(i["removed"] for i in sus) and len(names(out)) == 19
               and [g["path"].read_bytes() for g in good] == good_bytes, failures)
        m = manifest(out)
        _check("манифест: целые — suspect false со строками, подозрительные — true с причиной",
               all(m[g["path"].name]["suspect"] is False for g in good)
               and m[good[0]["path"].name]["rows"]["runs"] == 3
               and all(m[i["path"].name]["suspect"] is True
                       and m[i["path"].name]["why"] == i["suspect"] for i in sus), failures)
        try:
            bk.make_backup(db, out, now=at(2) + 30)      # та же минута, что у последней целой
            same = ""
        except bk.BackupError as e:
            same = str(e)
        _check("подозрительная копия не затирает целую с тем же именем (та же минута)",
               "не затираю" in same and good[2]["path"].read_bytes() == good_bytes[2], failures)
        r = [bk.send_weekly(conf(db), sus[-1], notify=True, test=t, now=at(18), send_document=doc,
                            send_message=no_msg) for t in (False, True)]
        _check("подозрительная копия в Telegram не уходит (и с --test): код 4, ничего не ушло",
               [x[0] for x in r] == [bk.EXIT_SUSPECT] * 2 and not got
               and "подозрительная" in r[0][1], failures)

        # --accept-shrink: база уменьшена намеренно — копия становится эталоном, ротация идёт.
        acc = bk.make_backup(db, out, now=at(19), accept_shrink=True)
        _check("--accept-shrink: не подозрительная, уменьшение записано, ротация пошла: "
               "05–10.10 удалены, остались 14 последних",
               acc["suspect"] is None and (acc["accepted"] or "").startswith("runs 3 → 0")
               and sorted(acc["removed"]) == sorted(fname(at(i)) for i in range(6))
               and names(out) == {fname(at(i)) for i in range(6, 20)}
               and manifest(out)[acc["path"].name]["why"].startswith("принято --accept-shrink"),
               failures)
        nxt = bk.make_backup(db, out, now=at(20))
        _check("после --accept-shrink эталон — принятая копия: следующая не подозрительная, "
               "записей удалённых копий в манифесте нет",
               nxt["suspect"] is None and nxt["reference"] == acc["path"].name
               and set(manifest(out)) == names(out), failures)

    # run.py backup: подозрительная копия -> код 4 и строка «что делать»; --accept-shrink -> 0.
    with tempfile.TemporaryDirectory() as tmp:
        db, out = _P(tmp) / "scanner.db", _P(tmp) / "backups"
        good_db(db)
        bk.make_backup(db, out, now=time.time() - 2 * day)
        empty_db(db)
        d = copy.deepcopy(cfg._d)
        d["output"] = {"db_path": str(db), "watchlist_json": str(_P(tmp) / "w.json")}
        d["api_keys"] = {}
        cpath = _P(tmp) / "config.json"
        cpath.write_text(_json.dumps(d, ensure_ascii=False), encoding="utf-8")
        runs = []
        for accept in (False, True):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = _run.cmd_backup(argparse.Namespace(
                    config=str(cpath), dir=str(out), keep=None, accept_shrink=accept,
                    send_weekly=False, notify=False, test=False))
            runs.append((rc, buf.getvalue()))
    _check("run.py backup: пустая база -> код 4, «backup SUSPECT: runs 3 → 0 …» и подсказка "
           "--accept-shrink",
           runs[0][0] == bk.EXIT_SUSPECT == 4
           and runs[0][1].startswith("backup SUSPECT: runs 3 → 0")
           and "run.py backup --accept-shrink" in runs[0][1], failures)
    _check("run.py backup --accept-shrink -> код 0, «уменьшение принято»",
           runs[1][0] == 0 and "уменьшение принято" in runs[1][1], failures)

    # Ярусы: ежедневные копии 15.08.2025–08.10.2026 (420 дней, 10:00 местного).
    D = _dt
    every = [D(2025, 8, 15, 10) + _td(days=i) for i in range(420)]
    kept = bk.plan_retention([(f"{x:%Y-%m-%d}", x.timestamp()) for x in every], 14, 8, 6,
                             now=D(2026, 10, 8, 12).timestamp())
    want = (["2026-05-31", "2026-06-30", "2026-07-31", "2026-08-31"]             # месяцы
            + ["2026-08-23", "2026-08-30", "2026-09-06", "2026-09-13", "2026-09-20"]  # недели
            + [f"{D(2026, 9, 25) + _td(days=i):%Y-%m-%d}" for i in range(14)])     # последние
    _check("ярусы за 420 дней: 14 последних + воскресенья 8 недель + концы 6 месяцев = 23 копии",
           kept == set(want) and len(kept) == 23, failures)

    def plan(dts, daily, weekly, monthly, now=None):
        ent = [(f"{x:%Y-%m-%d %H:%M}", x.timestamp()) for x in dts]
        return sorted(bk.plan_retention(ent, daily, weekly, monthly,
                                        now=now or max(t for _, t in ent)))

    _check("ярус недель: пропуски (сервер был выключен) не съедают ярус, из недели — новейшая",
           plan([D(2026, 7, 20, 10), D(2026, 7, 22, 10), D(2026, 8, 26, 10), D(2026, 9, 30, 10),
                 D(2026, 10, 1, 10)], 1, 3, 0)
           == ["2026-07-22 10:00", "2026-08-26 10:00", "2026-10-01 10:00"], failures)
    _check("ярус недель: ISO-неделя через Новый год (31.12.2026 и 02.01.2027 — одна неделя)",
           plan([D(2026, 12, 31, 10), D(2027, 1, 2, 10), D(2027, 1, 4, 10)], 1, 3, 0)
           == ["2027-01-02 10:00", "2027-01-04 10:00"], failures)
    _check("ярус месяцев: граница по местному времени (30.09 23:30 — сентябрь, 01.10 00:30 — нет)",
           plan([D(2026, 9, 30, 23, 30), D(2026, 10, 1, 0, 30), D(2026, 10, 2, 10)], 1, 0, 2)
           == ["2026-09-30 23:30", "2026-10-02 10:00"], failures)
    _check("последние 14: 15-я уходит; ярусы 0 — только последние",
           plan([D(2026, 10, 1, 10) + _td(days=i) for i in range(15)], 14, 0, 0)
           == [f"{D(2026, 10, 2, 10) + _td(days=i):%Y-%m-%d %H:%M}" for i in range(14)], failures)
    _check("копия «из будущего» (часы были сбиты) остаётся и места в ярусе не занимает",
           plan([D(2026, 10, 1, 10), D(2026, 10, 2, 10), D(2026, 10, 9, 10)], 1, 0, 0,
                now=D(2026, 10, 3).timestamp()) == ["2026-10-02 10:00", "2026-10-09 10:00"],
           failures)

    # Тонкая копия для Telegram: кандидаты старше telegram_candidates_days убраны, прочее цело.
    with tempfile.TemporaryDirectory() as tmp:
        db, out = _P(tmp) / "scanner.db", _P(tmp) / "backups"
        now = at(0)
        st = Store(str(db))
        old = st.conn.execute("INSERT INTO runs(ts) VALUES (?)", (now - 45 * day,)).lastrowid
        new = st.conn.execute("INSERT INTO runs(ts) VALUES (?)", (now - 2 * day,)).lastrowid
        cands(st.conn, old, 400)
        cands(st.conn, new, 30)
        st.conn.executemany("INSERT INTO candidate_metrics(run_id, ts, coin_id) VALUES (?,?,?)",
                            [(old, now - 45 * day, "a")] * 50 + [(new, now - 2 * day, "b")] * 7)
        st.conn.commit()
        st.close()
        ps = PositionStore(str(db))
        ps.add("GRAM", 1.0, 100, coin_id="gram", paper=True)
        ps.close_db()
        info = bk.make_backup(db, out, now=now)
        c = conf(db)
        n0 = len(got)
        r = bk.send_weekly(c, info, notify=True, test=True, now=now, max_bytes=info["size"] - 1,
                           send_document=doc, send_message=no_msg)
        sent = (got[-1] if len(got) > n0 and isinstance(got[-1], dict)
                else {"data": b"", "filename": "", "caption": ""})
        by_run = metrics = other = integ = None
        if sent["data"]:                             # не ушло документом — проверки ниже FAIL
            thin = _P(tmp) / "thin.db"
            thin.write_bytes(gzip.decompress(sent["data"]))
            con = sqlite3.connect(thin)
            try:
                q = lambda sql: con.execute(sql).fetchall()  # noqa: E731
                by_run = dict(q("SELECT run_id, COUNT(*) FROM candidates GROUP BY run_id"))
                metrics = q("SELECT coin_id, COUNT(*) FROM candidate_metrics GROUP BY coin_id")
                other = (q("SELECT COUNT(*) FROM runs")[0][0],
                         q("SELECT COUNT(*) FROM positions")[0][0])
                integ = q("PRAGMA integrity_check")[0][0]
            finally:
                con.close()
        _check("тонкая копия: кандидаты и метрики старше 30 дн. убраны, свежие, прогоны и позиции "
               "целы, integrity ok",
               by_run == {new: 30} and metrics == [("b", 7)] and other == (2, 1) and integ == "ok",
               failures)
        _check("тонкая копия: scanner-…-tg.db.gz (мимо NAME_RE), лимит — по ней, а не по полной",
               r[0] == 0 and sent["filename"] == info["path"].name[:-6] + "-tg.db.gz"
               and not bk.NAME_RE.match(sent["filename"]) and len(sent["data"]) < info["size"] - 1,
               failures)
        _check("тонкая копия: временные файлы убраны — в каталоге только копия и манифест",
               sorted(p.name for p in out.iterdir()) == sorted([info["path"].name, bk.MANIFEST]),
               failures)
        t = bk.make_thin(info["path"], 30, now)
        try:
            _check("тонкая копия пишется во временное имя «.…tmp» (обрыв не оставит копию-двойник)",
                   t["path"].name.startswith(".") and t["path"].name.endswith(".tmp")
                   and t["path"].name != t["name"], failures)
        finally:
            t["path"].unlink(missing_ok=True)
        cap = sent["caption"]
        _check("подпись тонкой: «Тонкая копия», 30 дн., где полные копии и ярусы, ≤ 1024 видимых",
               "Тонкая копия" in cap and "за последние 30 дн." in cap
               and str(out) in _html.unescape(cap) and "последние 14, недельные за 8 нед., "
               "месячные за 6 мес." in cap and len(tg.strip_html(cap)) <= tg.CAPTION_MAX, failures)

        def boom(*a, **k):
            raise OSError("нет места")

        real, bk.make_thin = bk.make_thin, boom
        try:
            r2 = bk.send_weekly(c, info, notify=True, test=True, now=now, send_document=doc,
                                send_message=no_msg)
        finally:
            bk.make_thin = real
        _check("тонкая не вышла -> уходит полная (влезает), причина в логе, бэкап не падает",
               r2[0] == 0 and "тонкая копия не вышла (OSError: нет места)" in r2[1]
               and got[-1]["filename"] == info["path"].name
               and got[-1]["data"] == info["path"].read_bytes()
               and "Полная копия" in got[-1]["caption"], failures)

    # Манифеста нет (первый прогон после выкатки: на сервере уже лежат копии) или он битый —
    # эталон находится по самим копиям.
    with tempfile.TemporaryDirectory() as tmp:
        db, out = _P(tmp) / "scanner.db", _P(tmp) / "backups"
        good_db(db)
        first = bk.make_backup(db, out, now=at(0))
        (out / bk.MANIFEST).unlink()
        (out / fname(at(1))).write_bytes(gzip.compress(b"not a database " * 64))   # не база
        (out / fname(at(2))).write_bytes(b"not gzip at all")                       # не gzip
        boot = bk.make_backup(db, out, now=at(3))
        e = manifest(out).get(first["path"].name) or {}
        _check("манифеста нет: эталон — самая новая читаемая копия (битые пропущены), строки "
               "посчитаны по распакованной, временных файлов нет",
               boot["suspect"] is None and boot["reference"] == first["path"].name
               and e.get("rows", {}).get("runs") == 3 and e.get("bootstrap") is True
               and not [p for p in out.iterdir() if p.name.startswith(".")], failures)
        res = []
        for i, junk in enumerate(("{битый json", "[1, 2]", '{"scanner-20261001-1000.db.gz": 5}',
                                  '{"scanner-20261001-1000.db.gz": {"rows": {"runs": "3"}}}')):
            (out / bk.MANIFEST).write_text(junk, encoding="utf-8")
            info = bk.make_backup(db, out, now=at(4 + i))
            res.append(info["suspect"] is None and info["path"].name in manifest(out))
        _check("битый или чужой backups.json не ломает бэкап: копия есть, манифест пересобран",
               all(res), failures)
        (out / bk.MANIFEST).unlink()
        empty_db(db)
        sus = bk.make_backup(db, out, now=at(9))
        _check("манифеста нет, база пустая -> подозрительная по эталону из прежней копии",
               (sus["suspect"] or "").startswith("runs 3 → 0") and sus["reference"] == fname(at(7)),
               failures)

    # Рецепт восстановления: журнал прерванной записи удаляется ДО распаковки, иначе SQLite при
    # первом открытии «откатит» его поверх восстановленной копии.
    with tempfile.TemporaryDirectory() as tmp:
        proj, out = _P(tmp) / "proj", _P(tmp) / "backups"
        proj.mkdir()
        live = proj / "scanner.db"
        now = at(0)
        st = Store(str(live))
        cands(st.conn, st.conn.execute("INSERT INTO runs(ts) VALUES (?)",
                                       (now - day,)).lastrowid, 60)
        st.conn.execute("CREATE TABLE filler (x TEXT)")
        st.conn.executemany("INSERT INTO filler VALUES (?)", [("y" * 100,)] * 50)
        st.conn.commit()
        st.close()
        info = bk.make_backup(live, out, now=now)
        bk.send_weekly(conf(live), info, notify=True, test=True, now=now, send_document=doc,
                       send_message=no_msg)
        sent = got[-1]
        # После копии база ушла вперёд, потом запись упала посреди транзакции: страницы уже
        # пролились в файл, горячий журнал остался (os._exit — без отката).
        st = Store(str(live))
        cands(st.conn, st.conn.execute("INSERT INTO runs(ts) VALUES (?)", (now,)).lastrowid, 30)
        st.conn.commit()
        st.close()
        child = ("import os, sqlite3, sys\n"
                 "c = sqlite3.connect(sys.argv[1]); c.execute('PRAGMA cache_size=5')\n"
                 "c.execute('BEGIN'); c.execute('DELETE FROM candidates')\n"
                 "c.execute(\"UPDATE filler SET x='z'\"); os._exit(1)\n")
        subprocess.run([sys.executable, "-c", child, str(live)], timeout=60)
        journal = proj / "scanner.db-journal"
        hot = journal.is_file() and journal.stat().st_size > 0
        recipe = next((_html.unescape(b) for b in re.findall(r"<code>(.*?)</code>",
                                                             sent["caption"], re.S)
                       if "gunzip -c" in b), "")

        def restore(where, skip_rm=False):
            """Каталог проекта после сбоя + документ из Telegram; команды — строки подписи."""
            where.mkdir()
            for p in proj.iterdir():
                shutil.copy(p, where / p.name)
            (where / sent["filename"]).write_bytes(sent["data"])
            for cmd in (line.split() for line in recipe.splitlines()):
                if cmd[:2] == ["rm", "-f"] and not skip_rm:
                    for f in cmd[2:]:
                        (where / f).unlink(missing_ok=True)
                elif cmd[:2] == ["gunzip", "-c"] and cmd[3:4] == [">"]:
                    with gzip.open(where / cmd[2], "rb") as fi:
                        (where / cmd[4]).write_bytes(fi.read())
            try:
                con = sqlite3.connect(where / "scanner.db")
                try:
                    return (con.execute("PRAGMA integrity_check").fetchone()[0],
                            con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])
                finally:
                    con.close()
            except sqlite3.DatabaseError as e:
                return type(e).__name__, None

        fixed, broken = restore(_P(tmp) / "a"), restore(_P(tmp) / "b", skip_rm=True)
    _check("рецепт (подпись и docstring): журнал прерванной записи удаляется ДО распаковки",
           0 <= recipe.find("rm -f scanner.db-journal scanner.db-wal scanner.db-shm")
           < recipe.find("gunzip -c") and "rm -f scanner.db-journal" in bk.__doc__, failures)
    _check("восстановление по подписи при горячем журнале: integrity ok, все 60 кандидатов",
           hot and fixed == ("ok", 60), failures)
    _check("без удаления журнала SQLite откатывает его поверх копии -> база испорчена",
           hot and broken != ("ok", 60), failures)


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
    import copy
    from scanner.config import Config
    from scanner.stages.entry_quality import spring_quality
    from scanner.stages.score import _sub_zone
    # Шкала BTC-dd и нейтральность без данных — на источнике btc (прод — alt, ниже отдельно).
    d = copy.deepcopy(cfg._d)
    d["stage4b_quality"]["market_dd_source"] = "btc"
    cfg_alt, cfg = cfg, Config(d)

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

    # Источник alt (прод): данных альт-рынка нет/устарели -> множитель контекста по минимуму,
    # а не откат на BTC-dd в другой шкале (на 03.10 +29% и 6 пружин ≥70 вместо 1).
    lo = cfg_alt.get("stage4b_quality.btc_dd_min_mult", 0.6)
    ma, na = spring_quality(Candidate(source="t", track="A", symbol="N", zone="ПРУЖИНА/ДНО",
                                      market_dd=0.60), cfg_alt)
    _check("alt без данных рынка: множитель = минимум, BTC −60% не поднимает",
           abs(ma - lo) < 1e-9 and any("нет свежих данных рынка" in n for n in na), failures)


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
    # дно 2022: OI в монетах высокий (цена падает), остальное холодное — это не перегрев
    bottom = {"alt_vs_sma200": -0.4, "altbtc_chg90": -0.1, "fng30": 25, "fund30": 0.002,
              "mvrv_btc": 0.9, "mvrv_eth": 0.8, "breadth200": 10, "oi_rel365": 1.6}
    hb = regime.hot_flags(bottom, cfg)
    _check("OI BTC в монетах — не флаг перегрева: дно 2022 (OI ×1.6) — 0 из 7",
           "oi_rel365" not in (cfg.get("market_regime.hot_flags", {}) or {})
           and hb["n_lit"] == 0 and hb["avail"] == 7, failures)
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
    fb, nfb = sq(market_dd=0.60)
    _check("нет alt_dd -> множитель контекста по минимуму, не BTC-dd",
           fb == cfg.get("stage4b_quality.btc_dd_min_mult", 0.6)
           and any("нет свежих данных рынка" in n for n in nfb), failures)
    import copy
    from scanner.config import Config
    d_off = copy.deepcopy(cfg._d)
    d_off["market_regime"]["enabled"] = False
    fb_off, _ = spring_quality(Candidate(source="t", track="A", symbol="Q", zone="ПРУЖИНА/ДНО",
                                         market_dd=0.60), Config(d_off))
    _check("контекст рынка выключен в конфиге -> прежний откат на BTC-dd", fb_off > 1.1, failures)
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
    plan5 = plan_ladder(1.5, 1.317, 50, steps=5, min_order=10, tick=0.001, qty_step=0.01,
                        exch_min_amt=5, floor_pct=25, sell="prod",
                        prod_levels=cfg["stage8_exit"]["ladder"])
    card5 = tg.format_coin_card(g, plan5, cfg, price=1.5, price_src="Bybit", P=P)
    _check("карточка: «все 4 ступени», «все 5 ступеней»; 5 цен лестницы в <pre>",
           "(все 4 ступени)" in card and "(все 5 ступеней)" in card5
           and len(plan5["buys"]) == 5 and all(P(b["price"]) in card5 for b in plan5["buys"]),
           failures)
    _check("склонение: 1/21 ступень, 2–4/22 ступени, 5/11/12 ступеней",
           [tg._steps_word(n) for n in (1, 21, 2, 4, 22, 5, 11, 12)]
           == ["ступень"] * 2 + ["ступени"] * 3 + ["ступеней"] * 3, failures)
    t6, x6 = cfg["stage6_telegram"], cfg["executor"]
    _check("карточка = лестница пробного исполнителя: ступени, бюджет и минимум ступени совпадают",
           (t6["card_steps"], t6["card_budget_usdt"], t6["card_min_order_usdt"])
           == (x6["steps"], x6["budget_usdt"], x6["min_order_usdt"]), failures)
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


def test_watch_data(cfg, failures: list[str]) -> None:
    print("Блок A — watch по закрытиям с датой (Bybit/CoinGecko), доставка, данные скана:")
    import copy
    import os
    import sqlite3
    import tempfile
    from scanner import closes, pipeline, regime, sync
    from scanner.config import Config
    from scanner.notify import deliver, telegram as tg
    from scanner.sources import bybit as by, coingecko as cg, funding as fnd, market as mk
    D0 = 1790812800                       # 2026-10-01 00:00 UTC
    DAY = 86400

    def D(n):
        return D0 + n * DAY

    # --- источники закрытий (чистые функции) ---
    chart = {"ts": [D(1), D(2), D(3), D(3) + 6 * 3600], "prices": [1.0, 1.1, 1.2, 9.9],
             "volumes": [5, 6, 7, 8], "mcaps": [None] * 4}
    s = closes.from_coingecko(chart)
    _check("CoinGecko: живой тик отброшен, close_ts = точка 00:00",
           s["close_ts"] == [D(1), D(2), D(3)] and s["prices"] == [1.0, 1.1, 1.2], failures)
    b = closes.from_bybit({"ts": [D(1), D(2)], "c": [2.0, 2.1], "qv": [10, 11]})
    _check("Bybit: close_ts = старт свечи + 1 день", b["close_ts"] == [D(2), D(3)], failures)
    _check("подпись дня закрытия: close_ts 00:00 03.10 -> «02.10»",
           closes.close_label(D(2)) == "02.10", failures)
    _check("возраст замёрзшего графика (последняя точка 9 дней назад) = 9",
           closes.chart_age_days(chart, D(12) + 3600) == 9, failures)

    calls = []
    real_ohlcv, real_chart = by.fetch_daily_ohlcv, cg.fetch_market_chart

    def bybit_ok(http, pair, limit=400):
        calls.append(("bybit", pair))
        return {"ts": [D(i) for i in range(-40, 7)], "c": [1.0] * 47, "qv": [1.0] * 47,
                "o": [], "h": [], "l": [], "v": []}

    def cg_chart(http, coin_id, days, demo=""):
        calls.append(("cg", coin_id))
        pts = [D(i) for i in range(-40, 7)]           # 06.10 точки 07.10 00:00 ещё нет
        return {"ts": pts + [D(6) + 22000], "prices": [1.0] * 47 + [1.3],
                "volumes": [1.0] * 48, "mcaps": [None] * 48}
    try:
        by.fetch_daily_ohlcv, cg.fetch_market_chart = bybit_ok, cg_chart
        r1 = closes.load(None, {"symbol": "GRAM", "venue": "Bybit spot", "coin_id": "x"},
                         now=D(7) + 22000)
        _check("пара Bybit свежая -> Bybit, CoinGecko не спрашиваем",
               r1["src"] == "Bybit" and r1["lag_days"] == 0 and calls == [("bybit", "GRAMUSDT")],
               failures)
        calls.clear()
        by.fetch_daily_ohlcv = lambda http, pair, limit=400: {"ts": [], "c": [], "qv": []}
        r2 = closes.load(None, {"symbol": "GRAM", "venue": "bybit", "coin_id": "x"},
                         now=D(7) + 22000)
        _check("Bybit пуст -> CoinGecko, отставание 1 день и пометка «ещё не вышло»",
               r2["src"] == "CoinGecko" and r2["lag_days"] == 1
               and "ещё не вышло" in r2["note"] and "Bybit GRAMUSDT" in r2["note"], failures)
        r3 = closes.load(None, {"symbol": "DEXC", "venue": "DEX only", "coin_id": ""},
                         now=D(7) + 22000)
        _check("нет ни Bybit, ни coin_id -> пусто с причиной",
               not r3["prices"] and "coin_id" in r3["note"], failures)
    finally:
        by.fetch_daily_ohlcv, cg.fetch_market_chart = real_ohlcv, real_chart

    # Дневные свечи Bybit — без кэша (кэш до 00:00 отдал бы живую свечу закрытой).
    seen = {}

    class _H:
        def get_json(self, url, params=None, headers=None, use_cache=True, retries=5):
            seen["use_cache"] = use_cache
            return {"result": {"list": []}}
    by.fetch_daily_ohlcv(_H(), "GRAMUSDT", 10)
    c1 = seen.pop("use_cache")
    by.fetch_daily_closes(_H(), "BTCUSDT", 10)
    _check("свечи D Bybit (ohlcv и closes) — без кэша", c1 is False and seen["use_cache"] is False,
           failures)

    # Фандинг приводится к 8ч по fundingIntervalHour.
    fm = fnd.parse_funding({"result": {"list": [
        {"symbol": "MEMEUSDT", "fundingRate": "0.0003", "fundingIntervalHour": "4"},
        {"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingIntervalHour": "8"},
        {"symbol": "OLDUSDT", "fundingRate": "0.0002"}]}})
    _check("фандинг 4ч ×2 к 8ч, 8ч и без поля — как есть",
           abs(fm["MEME"] - 0.0006) < 1e-12 and abs(fm["BTC"] - 0.0001) < 1e-12
           and abs(fm["OLD"] - 0.0002) < 1e-12, failures)

    # --- run_watch на подменённых источниках ---
    e = cfg["stage8_exit"]
    with tempfile.TemporaryDirectory() as tmp:
        d = copy.deepcopy(cfg._d)
        d["output"] = {**d["output"], "db_path": os.path.join(tmp, "t.db"),
                       "watchlist_json": os.path.join(tmp, "wl.json")}
        d["market_regime"]["enabled"] = False
        d["stage6_telegram"]["charts"] = False
        d["api_keys"]["dune"] = ""
        c = Config(d)
        db = d["output"]["db_path"]
        series: dict[str, dict[int, float]] = {}       # coin_id -> {точка 00:00: цена}
        live_now = {"t": 0}

        def fake_chart(http, coin_id, days, demo=""):
            if coin_id == "boom":
                raise RuntimeError("битый ответ")
            pts = sorted((t, p) for t, p in series.get(coin_id, {}).items() if t <= live_now["t"])
            ts = [t for t, _ in pts] + [live_now["t"]]          # + живой тик
            pr = [p for _, p in pts] + [pts[-1][1] if pts else 1.0]
            return {"ts": ts, "prices": pr, "volumes": [1e6] * len(ts), "mcaps": [None] * len(ts)}

        real_fund, real_dune = pipeline.fetch_funding_map, pipeline.dune.fetch_onchain

        def watch(at):
            live_now["t"] = at
            return pipeline.run_watch(c, now=at, http=object())

        try:
            cg.fetch_market_chart = fake_chart
            pipeline.fetch_funding_map = lambda http: {}
            pipeline.dune.fetch_onchain = lambda cfg_: {}
            ps = PositionStore(db)
            # 1) Пропуск точки CoinGecko: стоп из закрытия, которое ни разу не было последним.
            pa = ps.add("TST", 1.0, 100, coin_id="tst", paper=True, base_low=1.0,
                        entry_ts=D(-60) + 7 * 3600)
            pb = ps.add("TST", 1.0, 100, coin_id="tst", paper=True, base_low=1.0,
                        entry_ts=D(-60) + 7 * 3600, variant="B", twin_of=pa)
            ps.close_db()
            series["tst"] = {D(i): 0.95 for i in range(-90, 6)}
            series["tst"].update({D(6): 0.70, D(8): 0.80})          # D(7)=0.72 придёт позже
            out1 = watch(D(6) + 22800)
            row1 = next(r for r in out1["rows"] if r["position"]["id"] == pa)
            _check("прогон 1: переоценка с входа, одно закрытие ниже пола — стопа нет",
                   not row1["signals"] and row1["new_closes"] > 30, failures)
            out2 = watch(D(7) + 22800)                    # точки D(7) ещё нет
            row2 = next(r for r in out2["rows"] if r["position"]["id"] == pa)
            _check("прогон 2: закрытия дня нет -> ничего не оценено, отставание 1 день",
                   row2["new_closes"] == 0 and row2["lag_days"] == 1 and not row2["signals"],
                   failures)
            st2 = deliver.brief_state(c, now=D(7) + 23400, watch_exit=0)
            txt2 = tg.strip_html(tg.format_brief(st2, c, now=D(7) + 23400))
            _check("сводка: позиция помечена «⚠ цена на <день последнего закрытия>»",
                   f"⚠ цена на {closes.close_label(D(6))}" in txt2, failures)
            series["tst"][D(7)] = 0.72
            out3 = watch(D(8) + 22800)
            ps = PositionStore(db)
            ev = [x for x in ps.events(pa) if x["type"] == "invalidation"]
            _check("прогон 3: стоп по закрытию D(7) 0.72 (2 подряд ниже пола 0.75), не по 0.80",
                   len(ev) == 1 and ev[0]["close_ts"] == D(7) and abs(ev[0]["price"] - 0.72) < 1e-9,
                   failures)
            _check("paper A исполнена по 0.72 и закрыта, D(8) после выхода не оценивался",
                   ps.get(pa)["status"] == "closed" and abs(ps.get(pa)["exit_price"] - 0.72) < 1e-9
                   and ps.get(pa)["last_close_ts"] == D(7), failures)
            _check("снапшоты — по одному на закрытие, с close_ts",
                   len({t for t, _ in ps.snapshot_prices(pa)}) == len(ps.snapshot_prices(pa))
                   and ps.last_snapshots()[pa]["close_ts"] == D(7), failures)
            _check("очередь: карточка только у A (близнец B — тихо)",
                   [x["position_id"] for x in ps.outbox_pending("exit")] == [pa]
                   and "invalidation" in ps.event_types(pb), failures)
            ps.close_db()
            # доставка: сбой -> остаётся в очереди, сводка предупреждает; успех -> снята
            sent_cards = []
            r_fail = deliver.flush_exit_outbox(c, None, send=lambda cfg_, card: False)
            st_f = deliver.brief_state(c, now=D(8) + 23400, watch_exit=3)
            txt_f = tg.strip_html(tg.format_brief(st_f, c, now=D(8) + 23400))
            r_ok = deliver.flush_exit_outbox(c, None,
                                             send=lambda cfg_, card: sent_cards.append(card) or True)
            ps = PositionStore(db)
            _check("Telegram не принял -> (0, 1), карточка в очереди, сводка: «не доставлено»",
                   r_fail == (0, 1) and "не доставлено карточек выхода: 1" in txt_f, failures)
            _check("повтор доставки -> ушла карточка закрытия D(7), очередь пуста",
                   r_ok == (1, 0) and ps.outbox_count("exit") == 0 and sent_cards
                   and f"({closes.close_label(D(7))})" in sent_cards[0]["text"], failures)

            # 2) День 0: покупка 06:00, закрытие 00:00 до покупки — не сравнивается.
            p0 = ps.add("CRSH", 0.60, 100, coin_id="crsh", paper=True,
                        entry_ts=D(8) + 6 * 3600)
            ps.close_db()
            series["crsh"] = {D(i): 1.0 for i in range(-40, 8)}
            series["crsh"][D(8)] = 0.60
            out4 = watch(D(8) + 22800)
            r0 = next(r for r in out4["rows"] if r["position"]["id"] == p0)
            ps = PositionStore(db)
            _check("день 0: закрытие до входа не оценивается (нет лестницы/стопа, нет снапшота)",
                   not r0["signals"] and r0["new_closes"] == 0 and p0 not in ps.last_snapshots(),
                   failures)
            st0 = deliver.brief_state(c, now=D(8) + 23400, watch_exit=0)
            row0 = next(r for r in st0["positions"] if r["position"]["id"] == p0)
            _check("сводка: новая позиция — цена входа и пометка, а не «⚠ без цены»",
                   row0.get("pnl") and "первое закрытие" in (row0.get("note") or ""), failures)

            # 3) Докупка вниз: старый максимум не взводит трейл на убыточной позиции.
            pm = ps.add("DCA", 1.0, 10, coin_id="dca", base_low=0.5, entry_ts=D(-30))
            ps.set_watch_state(pm, D(10), 1.5, None)        # пик 1.5 был до докупки, не взведён
            ps.merge(pm, 0.5, 30)                           # средняя 0.625
            ps.close_db()
            series["dca"] = {D(i): 1.0 for i in range(-60, 11)}
            series["dca"].update({D(11): 0.70, D(12): 1.05, D(13): 0.70})
            watch(D(11) + 22800)
            ps = PositionStore(db)
            _check("докупка: закрытие 0.70 при старом пике 1.5 — трейла нет, не взведён",
                   "trailing" not in ps.event_types(pm) and ps.get(pm)["trail_armed_ts"] is None,
                   failures)
            ps.close_db()
            watch(D(13) + 22800)
            ps = PositionStore(db)
            tr = [x for x in ps.events(pm) if x["type"] == "trailing"]
            arm_ok = ps.get(pm)["trail_armed_ts"] == D(12)
            _check("взвод фактом закрытия 1.05 ≥ +60% от средней, трейл −33% от него на D(13)",
                   arm_ok and len(tr) == 1 and tr[0]["close_ts"] == D(13), failures)
            # 4) Сбой одной позиции не роняет watch.
            ps.add("BAD", 1.0, 10, coin_id="boom", entry_ts=D(-30))
            pg = ps.add("GOOD", 1.0, 10, coin_id="good", base_low=0.9, entry_ts=D(-30))
            # снапшот старого формата (до close_ts) за тот же день, с ценой «не того» дня
            ps.snapshot(pg, 9.99, 0.0, 9.99, ts=D(14) + 22000)
            ps.close_db()
            series["good"] = {D(i): 1.0 for i in range(-60, 15)}
            out5 = watch(D(14) + 22800)
            good = next(r for r in out5["rows"] if r["position"]["id"] == pg)
            bad = next(r for r in out5["rows"] if r["position"]["symbol"] == "BAD")
            _check("сбой источника одной позиции: строка с ошибкой, остальные посчитаны",
                   good.get("pnl") and bad.get("error") and out5["summary"]["errors"] == 1,
                   failures)
            ps = PositionStore(db)
            sp = ps.snapshot_prices(pg)
            _check("миграция: старый снапшот дня заменён снапшотом закрытия (без двойников)",
                   9.99 not in [x for _, x in sp] and len({t for t, _ in sp}) == len(sp)
                   and dict(sp).get(D(14)) == 1.0, failures)
            ps.close_db()
            ps = PositionStore(db)
            n_ev, n_snap = len(ps.events(pg)), len(ps.snapshot_prices(pg))
            ps.close_db()
            out6 = watch(D(14) + 23000)                      # повторный прогон того же дня
            ps = PositionStore(db)
            _check("повтор прогона: ни событий, ни снапшотов не прибавилось",
                   len(ps.events(pg)) == n_ev and len(ps.snapshot_prices(pg)) == n_snap
                   and next(r for r in out6["rows"] if r["position"]["id"] == pg)["new_closes"] == 0,
                   failures)
            ps.close_db()
        finally:
            cg.fetch_market_chart = real_chart
            pipeline.fetch_funding_map, pipeline.dune.fetch_onchain = real_fund, real_dune

        # 5) sync: позиция и exchange_fills — одной транзакцией.
        ps = PositionStore(os.path.join(tmp, "s.db"))
        fill = {"exec_id": "E1", "venue": "bybit", "symbol": "LINK", "pair": "LINKUSDT",
                "side": "buy", "price": 10.0, "qty": 5.0, "value": 50.0, "fee_usdt": 0.05,
                "fee_rate": 0.001, "base_delta": 5.0, "ts": 1.76e9, "order_id": "o1"}
        orig = PositionStore.record_fill

        def boom(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")
        PositionStore.record_fill = boom
        try:
            sync.apply_fills(ps, [fill], lambda s_: {"coin_id": "chainlink"})
            raised = False
        except sqlite3.OperationalError:
            raised = True
        finally:
            PositionStore.record_fill = orig
        _check("sync: сбой записи исполнения -> позиция тоже откатилась",
               raised and ps.find_open_real("LINK") is None, failures)
        sync.apply_fills(ps, [fill], lambda s_: {"coin_id": "chainlink"})
        _check("sync: следующий прогон применяет покупку один раз (5, не 10)",
               ps.find_open_real("LINK")["qty"] == 5.0 and len(ps.fills()) == 1, failures)
        ps.close_db()

    # --- Telegram: повторы ---
    real_post, real_sleep = tg._post, tg._sleep
    script, waits = [], []
    tg._sleep = waits.append
    tg._post = lambda token, method, data, ctype, timeout=30: script.pop(0)
    try:
        script[:] = [None, {"ok": False, "error_code": 502, "description": "Bad Gateway"},
                     {"ok": True}]
        ok3 = tg.send_message("T", "C", "x")
        left3, w3 = len(script), list(waits)
        waits.clear()
        script[:] = [{"ok": False, "error_code": 400, "description": "chat not found"}, {"ok": True}]
        ok4 = tg.send_message("T", "C", "x")
        left4 = len(script)
        script[:] = [{"ok": False, "error_code": 429, "parameters": {"retry_after": 20}},
                     {"ok": True}]
        waits.clear()
        ok5 = tg.send_message("T", "C", "x")
    finally:
        tg._post, tg._sleep = real_post, real_sleep
    _check("Telegram: сеть/502 -> повтор, третья попытка дошла", ok3 and left3 == 0
           and w3 == [3.0, 10.0], failures)
    _check("Telegram: 400 (ошибка запроса) — без повтора", not ok4 and left4 == 1, failures)
    _check("Telegram: 429 -> ждём retry_after+1", ok5 and waits == [21.0], failures)

    # --- выходной контур: None и затёртый индекс перегрева ---
    pos = {"entry_price": 1.0, "base_low": 0.5, "qty": 10}
    try:
        sg = exit_stage.evaluate_exit(pos, 1.2, 1.2, {"funding_rate": 0.001}, set(), cfg)
        crash = False
    except TypeError:
        sg, crash = [], True
    _check("фандинг-эйфория без индикаторов зоны (<15 закрытий) — не падает",
           not crash and any(x["type"] == "peak_zone" and "фандинг" in x["note"] for x in sg),
           failures)
    sh = exit_stage.evaluate_exit(pos, 1.2, 1.2, {"pct_above_sma": 50, "range_pos": 0.95},
                                  set(), cfg, market={"hot_score": 0.5, "lit": ["fng30"]})
    mh = next((x for x in sh if x["type"] == "market_hot"), None)
    _check("перегрев рынка после зоны распределения: «индекс перегрева 50%», не 100/0%",
           mh is not None and "индекс перегрева 50%" in mh["note"], failures)

    # --- скан: тикер Bybit, стейблы, alt_dd, свежесть рынка ---
    _check("Bybit: цена совпала -> та же монета", pipeline.bybit_identity(
        "MEME", {"MEME"}, {"MEME": 0.0057}, 0.0056) == "", failures)
    _check("Bybit: цены разошлись -> bybit_ticker_mismatch", pipeline.bybit_identity(
        "MEME", {"MEME"}, {"MEME": 0.0057}, 0.9) == "bybit_ticker_mismatch", failures)
    _check("Bybit: цены CoinGecko нет (MEMETOON) -> bybit_unverified, не наследует Bybit",
           pipeline.bybit_identity("MEME", {"MEME"}, {"MEME": 0.0057}, None)
           == "bybit_unverified", failures)
    llama = {D(i): 311e9 for i in range(-5, 1)}
    cm = {D(i): 254e9 for i in range(-400, 1)}
    _check("стейблы: DefiLlama есть -> её значение", mk.merge_stables(llama, cm, D(-3))[D(0)]
           == 311e9, failures)
    _check("стейблы: DefiLlama не ответила -> не пишем (CoinMetrics не подменяет)",
           mk.merge_stables({}, cm, D(-3)) == {}, failures)
    m10 = mk.merge_stables(llama, cm, D(-10))           # окно с since − 1 день
    _check("стейблы: CoinMetrics — только дни до начала ряда DefiLlama",
           set(m10) == {D(i) for i in range(-11, 1)} and m10[D(-6)] == 254e9
           and m10[D(-5)] == 311e9, failures)
    rows = [{"day": D(i), "total_mcap": 3000.0, "btc_dominance": 50.0, "stables_usd": 100.0}
            for i in range(-99, 1)]
    _check("alt_dd: 100 дней истории < 365 -> нет (не «рынок у хаёв»)",
           "alt_dd" not in regime.market_context(rows, cfg), failures)
    ctx = {"day": D(0), "alt_dd": 0.5}
    _check("контекст рынка: 2 дня — свежий, 5 дней — не используется",
           regime.fresh_context(ctx, cfg, D(2) + 3600) == ctx
           and regime.fresh_context(ctx, cfg, D(5) + 3600) == {}, failures)


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
    # 5 ступеней + стоп у дна графика (LUNC 07.10): подписи упирались в строку «уровни — …»
    ys = chart.label_ys([600, 610, 625, 640, 655, 672], chart.LABEL_BOTTOM)
    _check("подписи уровней: не ближе 24 px, нижняя не ниже строки «уровни — правила сканера»",
           all(b - a >= chart.LABEL_GAP - 1e-9 for a, b in zip(ys, ys[1:]))
           and ys[-1] <= chart.LABEL_BOTTOM < chart.H - 30
           and chart.label_ys([100, 300], chart.LABEL_BOTTOM) == [100, 300]
           and chart.label_ys([], chart.LABEL_BOTTOM) == [], failures)
    if chart.available():
        long_png = chart.render_levels(
            ohlcv, title="LUNC/USDT", buys=[9.0, 8.8, 8.6, 8.4, 8.2], stop=7.9,
            targets=[(13.5, "+50%"), (22.5, "+150%")], fmt=lambda x: f"{x / 1e5:.8f}")
        _check("картинка: 5 ступеней и длинные цены целей — PNG строится",
               isinstance(long_png, bytes) and long_png[:4] == b"\x89PNG", failures)


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
    только закрытые к self.now (как Bybit). hourly — сплошной ряд часов с часа, в котором
    start (фильтр по постановке — в коде), low из self.hours, иначе 1e9 (ничего не исполняет);
    дыры — hour_gap. Сбои — как у реального рынка, ключом err, а не исключением (err_*);
    fail — daily бросает исключение (проверка «сбой позиции не валит остальные»)."""

    def __init__(self, now: float):
        self.now = now
        self.inst: dict = {}          # пара -> instrument | None (нет на Bybit)
        self.price: dict = {}         # пара -> последняя цена
        self.days: dict = {}          # пара -> [(ts дня, close)]
        self.hl: dict = {}            # пара -> {ts дня: (open, high)} (нет — open = high = close)
        self.hours: dict = {}         # пара -> [(ts часа, low)]
        self.fail: set = set()        # пары, на которых daily бросает исключение
        self.err_daily: set = set()   # daily -> пусто + err (сеть/retCode, как sources.bybit)
        self.err_hourly: set = set()  # hourly -> пусто + err
        self.err_inst: set = set()    # instrument -> {"err": …} (сеть ≠ «пары нет»)
        self.hour_gap: dict = {}      # пара -> {ts часа}: Bybit не отдал эти свечи
        self.ranks: dict = {}         # места по обороту (liquidity.ensure_ranks)
        self.ranks_fail = False       # vol_ranks бросает исключение
        self.ranks_calls = 0

    def vol_ranks(self, now):
        self.ranks_calls += 1
        if self.ranks_fail:
            raise RuntimeError("Bybit не ответил")
        return self.ranks

    def instrument(self, pair):
        if pair in self.err_inst:
            return {"err": "нет ответа (сеть/HTTP)"}
        return self.inst.get(pair)

    def last_price(self, pair):
        return self.price.get(pair)

    def daily(self, pair):
        if pair in self.fail:
            raise RuntimeError("биржа не ответила")
        if pair in self.err_daily:
            return {"ts": [], "o": [], "h": [], "l": [], "c": [],
                    "err": "retCode 10016: Service unavailable"}
        rows = [(t, c) for t, c in self.days.get(pair, []) if t + 86400 <= self.now]
        oh = self.hl.get(pair, {})
        return {"ts": [t for t, _ in rows], "c": [c for _, c in rows],
                "o": [oh.get(t, (c, c))[0] for t, c in rows],
                "h": [oh.get(t, (c, c))[1] for t, c in rows], "l": [c for _, c in rows]}

    def hourly(self, pair, start_ts):
        if pair in self.err_hourly:
            return {"ts": [], "l": [], "err": "нет ответа (сеть/HTTP)"}
        lows = dict(self.hours.get(pair, []))
        gap = self.hour_gap.get(pair, set())
        out = {"ts": [], "l": []}
        t = int(start_ts // 3600) * 3600
        while t + 3600 <= self.now:
            if t not in gap:
                out["ts"].append(t)
                out["l"].append(lows.get(t, 1e9))
            t += 3600
        return out


def _wl_add(c, sym: str) -> None:
    """Монета карточки — в watchlist.json фикстуры (исполнитель сверяет тикер fail-closed);
    уже есть или файл намеренно битый — не трогаем."""
    import json as _json
    from pathlib import Path as _P
    p = _P(c["output"]["watchlist_json"])
    try:
        rows = _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not any((r.get("symbol") or "").upper() == sym.upper() for r in rows):
        rows.append({"symbol": sym, "coin_id": f"{sym.lower()}-coin", "rf_venue": "Bybit spot"})
        p.write_text(_json.dumps(rows), encoding="utf-8")


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
    # холодный рынок: 0 флагов из 8, день данных свежий для всех шагов теста
    cold = {"day": d0 + 4 * D, "hot": {"lit": [], "near": [], "avail": 8, "n_lit": 0}}

    def run(c: Config, m, **kw):
        return ex.run_dry(c, m, mctx=cold, **kw)

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
        _wl_add(c, sym)
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
        code0, lines0 = run(c, m, now=now0, wallet_usdt=30.0)
        pos0 = q(c, "SELECT * FROM dry_positions ORDER BY id")
        ord0 = q(c, "SELECT * FROM dry_orders ORDER BY link_id")
        bs0 = deliver_brief(c, now=now0, exec_exit=0)
        code1, lines1 = run(c, m, now=now0)
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
        run(c, m, now=m.now)
        st2a = q(c, "SELECT status FROM dry_orders WHERE link_id='dry-R1-B2'")[0]["status"]
        m.now = d0 + 9 * H
        _, lines_b = run(c, m, now=m.now)
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
        _, lines_mid = run(c, m, now=m.now)
        r_mid = q(c, "SELECT * FROM dry_positions WHERE id=1")[0]
        sells_mid = q(c, "SELECT * FROM dry_orders WHERE side='Sell' ORDER BY link_id")
        bbb_mid = q(c, "SELECT status, qty FROM dry_positions WHERE symbol='BBB'")
        m.now = d0 + 4 * D + H
        _, lines_d = run(c, m, now=m.now)
        pos = {(p["symbol"], p["book"]): p for p in q(c, "SELECT * FROM dry_positions")}
        sells = q(c, "SELECT * FROM dry_orders WHERE side='Sell' ORDER BY created_ts, link_id")
        _check("R: на +50% (high 1.5 ≥ цели) продана 1/3 купленного вниз к qty_step, лимиткой "
               "dry-R1-S-L0 по max(цель, open)",
               [s["link_id"] for s in sells_mid] == ["dry-R1-S-L0"]
               and sells_mid[0]["qty"] == round_step(0.33 * r_mid["bought_qty"], 0.1)
               and sells_mid[0]["kind"] == "Limit" and sells_mid[0]["price"] == 1.5, failures)
        _check("BBB: одно закрытие ниже пола — ещё не стоп (нужно 2 подряд), лимитки исполнены",
               all(p["status"] == "open" for p in bbb_mid) and len(bbb_mid) == 2
               and len(q(c, "SELECT 1 FROM dry_orders o JOIN dry_positions p ON p.id=o.position_id"
                            " WHERE p.symbol='BBB' AND o.side='Buy' AND o.status='filled'")) == 10,
               failures)
        r_sig = [s["signal"] for s in sells if s["position_id"] == pos[("AAA", "R")]["id"]]
        _check("R AAA: 1/3 на +50%, 1/3 на +150%, остаток по трейлу — закрыта, хвост < qty_step",
               r_sig == ["ladder_0", "ladder_1", "trailing"]
               and pos[("AAA", "R")]["status"] == "closed" and pos[("AAA", "R")]["qty"] < 0.1
               and pos[("AAA", "R")]["reason"] == "трейл", failures)
        r_c = q(c, "SELECT status, note FROM dry_orders WHERE position_id=? AND side='Buy' "
                   "ORDER BY step", (pos[("AAA", "R")]["id"],))
        h_c = q(c, "SELECT status FROM dry_orders WHERE position_id=? AND side='Buy' ORDER BY "
                   "step", (pos[("AAA", "H")]["id"],))
        _check("R: первая продажа +50% сняла лимитки 3–5 («начали продавать»); у H они живы",
               [o["status"] for o in r_c] == ["filled", "filled"] + ["cancelled"] * 3
               and all(o["note"] == "начали продавать" for o in r_c[2:])
               and [o["status"] for o in h_c] == ["filled", "filled"] + ["new"] * 3
               and any("R AAA: снял бы лимитки покупки — начали продавать (3)" in x
                       for x in lines_mid), failures)
        _check("H AAA «держать»: ни одной продажи на +50/+150 и откате, позиция открыта",
               not any(s["position_id"] == pos[("AAA", "H")]["id"] for s in sells)
               and pos[("AAA", "H")]["status"] == "open" and pos[("AAA", "H")]["last_price"] == 1.7,
               failures)
        bbb = [s for s in sells if s["pair"] == "BBBUSDT"]
        _check("BBB: два закрытия ниже лоу базы −25% — обе книги вышли полностью по стопу",
               sorted(s["book"] for s in bbb) == ["H", "R"]
               and all(s["signal"] == "invalidation" and s["price"] == 0.70 for s in bbb)
               and all(pos[("BBB", b)]["status"] == "closed" and pos[("BBB", b)]["qty"] < 0.1
                       and pos[("BBB", b)]["reason"] == "стоп" for b in "RH"), failures)

        # (ж) P&L с комиссиями — пересчёт руками: тейки — лимитками 0.1%, трейл — рынком
        # 0.15%, количества вниз к 0.1; хвост меньше шага остаётся и оценивается по закрытию
        bought = 10.0 * (1 - MARKET_FEE) + 10.8 * (1 - LIMIT_FEE)
        spent = 10.0 * 1.0 + 10.8 * 0.93
        q1 = q2 = round_step(0.33 * bought, 0.1)
        q3 = round_step(bought - q1 - q2, 0.1)
        tail = bought - q1 - q2 - q3
        proceeds = ((q1 * 1.5 + q2 * 2.6) * (1 - LIMIT_FEE) + q3 * 1.7 * (1 - MARKET_FEE)
                    + tail * 1.7 * (1 - MARKET_FEE))
        pr = pos[("AAA", "R")]
        ph = pos[("AAA", "H")]
        _check("P&L R AAA: тейки × (1 − 0.1%) + трейл × (1 − 0.15%) − (10 + 10.8 × 0.93)",
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

        # (е) места хватило обеим книгам — монеты в обеих (книги независимы: test_executor_fixes)
        by_pair: dict = {}
        for p in q(c, "SELECT pair, book, card_day FROM dry_positions"):
            by_pair.setdefault((p["pair"], p["card_day"]), set()).add(p["book"])
        _check("места хватило: каждая (пара, день карточки) — в обеих книгах",
               all(v == {"R", "H"} for v in by_pair.values()), failures)

        # (з) сводка дня после выходов и недельный блок по той же БД
        bs = deliver_brief(c, now=m.now, exec_exit=1)
        exl = tg.executor_brief_lines(bs["executor"], c)
        _check("сводка дня: продажи за сутки «R AAA трейл», «H BBB стоп»; H — 1 поз.",
               bs["exec_fail"] and any("продажи:" in x and "R AAA трейл" in x and "H BBB стоп" in x
                                       for x in exl)
               and any("✋ держать: 1 поз." in x for x in exl), failures)
        _check("сводка: код «нет данных Bybit» (EXIT_NODATA) — не «исполнитель упал»",
               not deliver_brief(c, now=m.now, exec_exit=ex.EXIT_NODATA)["exec_fail"],
               failures)
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
            code, lines = run(c, m, now=now0)
            ps = q(c, "SELECT book, status, reason FROM dry_positions WHERE pair='AAAUSDT'")
            no = q(c, "SELECT 1 FROM dry_orders WHERE pair='AAAUSDT'")
        _check(f"отказ: {label} -> rejected обеим книгам, ордеров нет",
               code == 0 and sorted(p["book"] for p in ps) == ["H", "R"]
               and all(p["status"] == "rejected" and want in p["reason"] for p in ps) and not no
               and any("AAA: ОТКАЗ" in x for x in lines), failures)

    def two_open(c, m):
        for i in range(2):
            add_pair(m, f"X{i}USDT", [], [])
        con = ex.connect(c["output"]["db_path"])
        for i, b in enumerate(("R", "H", "R", "H")):
            con.execute("INSERT INTO dry_positions(book, pair, symbol, card_day, status, "
                        "created_ts, qty, bought_qty, spent_usdt, last_price) VALUES "
                        "(?,?,?,?,?,?,?,?,?,?)", (b, f"X{i // 2}USDT", f"X{i // 2}", d0 - D,
                                                  "open", now0 - D, 10.0, 10.0, 10.0, 1.0))
        con.commit()
        con.close()

    def only_h_full(c, m):
        add_pair(m, "XUSDT", [], [])
        con = ex.connect(c["output"]["db_path"])
        con.execute("INSERT INTO dry_positions(book, pair, symbol, card_day, status, created_ts, "
                    "qty, bought_qty, spent_usdt, last_price) VALUES "
                    "('H','XUSDT','X',?,'open',?,10,10,10,1)", (d0 - D, now0 - D))
        con.commit()
        con.close()

    reject_case("лимит max_coins 2", two_open, "лимит 2 монет", max_coins=2)
    reject_case("нехватка USDT (capital 40)", lambda c, m: None, "нехватка USDT", capital_usdt=40)
    with tempfile.TemporaryDirectory() as tmp:          # книги независимы (как в бэктесте)
        c = make_cfg(tmp, max_coins=1)
        m = _FakeMarket(now0)
        add_pair(m, "AAAUSDT", [], [])
        only_h_full(c, m)
        card(c, "AAA", now0)
        code_i, lines_i = run(c, m, now=now0)
        ps_i = {p["book"]: p for p in q(c, "SELECT book, status, reason FROM dry_positions "
                                           "WHERE pair='AAAUSDT'")}
    _check("книги независимы: лимит монет только у H — отказ H, R ставит лестницу",
           code_i == 0 and ps_i["H"]["status"] == "rejected"
           and ps_i["H"]["reason"] == "H: лимит 1 монет" and ps_i["R"]["status"] == "open"
           and "AAA: ОТКАЗ — H: лимит 1 монет" in lines_i
           and any(x.startswith("AAA: поставил бы в книги R — 5 ступ.") for x in lines_i),
           failures)
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
        run(c, m, now=now0)
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
        run(c, m, now=now0)
        m.fail.add("AAAUSDT")
        m.now = now0 + D
        code_f, lines_f = run(c, m, now=m.now)
        last_b = q(c, "SELECT last_day FROM dry_positions WHERE symbol='BBB'")
        m.fail.clear()
        m.now += H                                  # карточки старше 24 ч — окно их не видит
        empty_code, empty_lines = run(c, m, now=m.now)
        off = run(make_cfg(tmp, enabled=False), m, now=m.now)
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


def test_liquidity(cfg, failures: list[str]) -> None:
    print("Место по обороту (scanner/liquidity.py): вселенная, средний оборот, места, кэш в БД:")
    import sqlite3
    import tempfile
    from pathlib import Path as _P
    from scanner import liquidity as L
    d0 = 1790812800                        # 2026-10-01 00:00 UTC
    now = d0 + 6 * 3600
    inst = {"result": {"list": [
        {"symbol": "AAAUSDT", "baseCoin": "AAA", "quoteCoin": "USDT", "status": "Trading",
         "symbolType": ""},
        {"symbol": "AAAUSDC", "baseCoin": "AAA", "quoteCoin": "USDC", "status": "Trading"},
        {"symbol": "USDCUSDT", "baseCoin": "USDC", "quoteCoin": "USDT", "status": "Trading"},
        {"symbol": "BTC3LUSDT", "baseCoin": "BTC3L", "quoteCoin": "USDT", "status": "Trading"},
        {"symbol": "AAPLXUSDT", "baseCoin": "AAPLX", "quoteCoin": "USDT", "status": "Trading",
         "symbolType": "xstocks"},
        {"symbol": "NYMUSDT", "baseCoin": "NYM", "quoteCoin": "USDT", "status": "Trading",
         "symbolType": "adventure"},
        {"symbol": "OLDUSDT", "baseCoin": "OLD", "quoteCoin": "USDT", "status": "Closed"}]}}
    _check("вселенная: USDT + Trading, без стейблов, плечевых и xstocks; adventure — в счёт",
           L.universe(inst) == [("AAAUSDT", "AAA"), ("NYMUSDT", "NYM")], failures)

    def kl(n_closed: int, val, live: bool = True) -> dict:
        rows = [[str(d0 * 1000), "1", "1", "1", "1", "1", "999999"]] if live else []
        rows += [[str((d0 - k * 86400) * 1000), "1", "1", "1", "1", "1", str(val(k))]
                 for k in range(1, n_closed + 1)]           # newest-first, как отдаёт Bybit
        return {"retCode": 0, "result": {"list": rows}}
    _check("средний оборот: живая свеча отброшена, 30 последних закрытых, поле [6]",
           L.avg_turnover(kl(35, lambda k: 100.0 if k <= 30 else 1e9), now) == 100.0, failures)
    _check("средний оборот: 20 закрытых — среднее по ним; 19 — нет данных (свежий листинг)",
           L.avg_turnover(kl(20, lambda k: k), now) == 10.5
           and L.avg_turnover(kl(19, lambda k: 1.0), now) is None, failures)
    t = L.rank_table({"A": 300.0, "B": 100.0, "C": 200.0, "D": 100.0})
    _check("места: 1 — самый большой оборот, равные — по тикеру; доля = место / число пар",
           [t[s]["rank"] for s in "ACBD"] == [1, 2, 3, 4] and t["A"]["share"] == 0.25
           and t["D"]["share"] == 1.0 and t["B"]["n"] == 4, failures)

    class FakeHttp:
        def __init__(self, inst_, klines, fail=()):
            self.inst, self.klines, self.fail, self.calls = inst_, klines, set(fail), 0

        def get_json(self, url, params=None, use_cache=True, **kw):
            self.calls += 1
            if "instruments-info" in url:
                return self.inst
            return None if params["symbol"] in self.fail else self.klines.get(params["symbol"])

    class Boom:
        def get_json(self, *a, **k):
            raise OSError("сеть упала")

    def count(db: str) -> int:
        con = sqlite3.connect(db)
        try:
            return con.execute("SELECT COUNT(*) FROM vol_ranks").fetchone()[0]
        finally:
            con.close()
    many = {"result": {"list": [{"symbol": f"C{i:03d}USDT", "baseCoin": f"C{i:03d}",
                                 "quoteCoin": "USDT", "status": "Trading"} for i in range(1, 121)]}}
    kls = {f"C{i:03d}USDT": kl(30, lambda k, i=i: float(i)) for i in range(1, 121)}
    with tempfile.TemporaryDirectory() as tmp:
        db = str(_P(tmp) / "t.db")
        h = FakeHttp(many, kls)
        r1 = L.ensure_ranks(db, h, now)
        calls1 = h.calls
        r2 = L.ensure_ranks(db, h, now + 3600)
        r3 = L.load_ranks(db, now + 86400)
        n1 = count(db)
        dbf = str(_P(tmp) / "f.db")
        rf = L.ensure_ranks(dbf, FakeHttp(many, kls, {f"C{i:03d}USDT" for i in range(1, 14)}), now)
        nf = count(dbf)
        rb = L.ensure_ranks(str(_P(tmp) / "b.db"), Boom(), now)
    _check("подсчёт: 120 пар, C120 — 1-е место, C001 — 120-е; 121 запрос; в БД 120 строк",
           r1["ok"] and r1["n"] == 120 and L.lookup(r1, "c120")["rank"] == 1
           and L.lookup(r1, "C001")["rank"] == 120 and calls1 == 121 and n1 == 120, failures)
    _check("тот же UTC-день — из БД без сети, те же места и доли",
           r2["ok"] and h.calls == calls1 and r2["by_sym"]["C001"]["rank"] == 120
           and abs(r2["by_sym"]["C060"]["share"] - r1["by_sym"]["C060"]["share"]) < 1e-12,
           failures)
    _check("следующий UTC-день — данных ещё нет (пересчитает первый, кому нужно)",
           not r3["ok"] and r3["by_sym"] == {} and r3["note"] == "нет данных оборота", failures)
    _check("не ответили 13 из 120 пар (> 10%) — сбой: ok False, в БД пусто",
           not rf["ok"] and "не ответили 13" in rf["note"] and nf == 0, failures)
    _check("сеть бросила исключение — не падает: ok False, причина в note",
           not rb["ok"] and "OSError" in rb["note"], failures)
    _check("lookup: нет монеты или нет данных — None",
           L.lookup(r1, "ZZZ") is None and L.lookup({}, "C001") is None
           and L.lookup(None, "C001") is None, failures)


def test_executor_filters(cfg, failures: list[str]) -> None:
    print("Отсев перед покупкой: нижняя четверть, квота, перегрев, теневая книга, метки:")
    import copy
    import json as _json
    import sqlite3
    import tempfile
    from pathlib import Path as _P
    from scanner import executor as ex
    from scanner.config import Config
    from scanner.db import Store
    from scanner.ladder import plan_ladder
    from scanner.notify import telegram as tg
    from scanner.notify.deliver import brief_state as deliver_brief
    D, H = ex.DAY, ex.HOUR
    d0 = 1790812800                        # 2026-10-01 00:00 UTC
    now0 = d0 + 6 * H + 1800
    inst = {"symbol": "", "status": "Trading", "st": False, "tick": 0.01, "qty_step": 0.1,
            "min_qty": 0.1, "min_amt": 5.0}
    hist = [(d0 - k * D, 0.95 if k == 10 else 1.0) for k in range(40, 0, -1)]
    cold = {"day": d0 + 4 * D, "hot": {"lit": [], "near": ["fng30"], "avail": 8}}
    hot = {"day": d0, "hot": {"lit": ["mvrv_eth"], "near": [], "avail": 8}}
    s0 = ex.settings(cfg)

    def mk(tmp: str, wl: list | None = None, **exe) -> Config:
        d = copy.deepcopy(cfg._d)
        d["output"] = {**d.get("output", {}), "db_path": str(_P(tmp) / "t.db"),
                       "watchlist_json": str(_P(tmp) / "wl.json")}
        d["executor"] = {**d.get("executor", {}), **exe}
        (_P(tmp) / "wl.json").write_text(_json.dumps(wl or []), encoding="utf-8")
        Store(d["output"]["db_path"]).close()
        return Config(d)

    def rk(places: dict, n: int) -> dict:
        return {"ok": True, "day": ex.utc_day(now0), "n": n, "note": "",
                "by_sym": {s: {"rank": r, "n": n, "share": r / n, "usd": 1e6 / r}
                           for s, r in places.items()}}

    def pair(m, sym: str, after=(), hours=()) -> None:
        p = f"{sym}USDT"
        m.inst[p] = {**inst, "symbol": p}
        m.price[p] = 1.0
        m.days[p] = hist + list(after)
        m.hours[p] = list(hours)

    def card(c: Config, sym: str, ts: float) -> None:
        _wl_add(c, sym)
        con = sqlite3.connect(c["output"]["db_path"])
        con.execute("INSERT INTO alert_log(symbol, ts, score) VALUES (?,?,72)", (sym, ts))
        con.commit()
        con.close()

    def q(c: Config, sql: str, args=()) -> list[dict]:
        con = sqlite3.connect(c["output"]["db_path"])
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, args).fetchall()]
        finally:
            con.close()

    def seed(c: Config, sym: str, share, shadow: int = 0, why: str = "", m=None) -> None:
        """Открытая позиция в обе книги с меткой доли оборота на входе (без ордеров); m —
        пара торгуется на рынке фикстуры (иначе исполнитель закрыл бы её как делистинг)."""
        if m is not None:
            pair(m, sym)
        con = ex.connect(c["output"]["db_path"])
        for b in ("R", "H"):
            con.execute("INSERT INTO dry_positions(book, pair, symbol, card_day, status, "
                        "created_ts, qty, bought_qty, spent_usdt, last_price, shadow, shadow_why, "
                        "vol_share) VALUES (?,?,?,?,'open',?,10,10,10,1,?,?,?)",
                        (b, f"{sym}USDT", sym, d0 - D, now0 - D, shadow, why, share))
        con.commit()
        con.close()

    def money(c: Config, book: str) -> dict:
        con = ex.connect(c["output"]["db_path"])
        try:
            return {"free": ex.free_usdt(con, book, 1500.0), "res": ex.reserved_usdt(con, book),
                    "main": ex.book_summary(con, book),
                    "shadow": ex.book_summary(con, book, shadow=True)}
        finally:
            con.close()

    # --- чистые функции: перегрев и места
    g = {k: ex.market_gate(cfg, s0, v, now0) for k, v in {
        "cold": cold, "hot": hot, "none": {}, "stale": {**cold, "day": d0 - 4 * D},
        "edge": {**cold, "day": d0 - 3 * D},
        "few": {"day": d0, "hot": {"lit": [], "near": [], "avail": 3}}}.items()}
    _check("перегрев: 0 флагов («близко» не в счёт) — cold; 1 флаг — hot, «горят: MVRV ETH»",
           g["cold"]["status"] == "cold" and g["hot"]["status"] == "hot"
           and g["hot"]["note"] == "перегрев 1/8, горят: MVRV ETH", failures)
    _check("перегрев: данных нет, день данных старше 3 дн., флагов с данными < 4 — nodata",
           g["none"]["status"] == g["stale"]["status"] == g["few"]["status"] == "nodata"
           and "старше 3 дн." in g["stale"]["note"] and g["none"]["note"] == "данных рынка нет",
           failures)
    _check("перегрев: данным 3 дня — ещё верим (cold)", g["edge"]["status"] == "cold", failures)
    r8 = rk({"Q6": 6, "Q7": 7, "M3": 3, "M4": 4, "TOP": 1}, 8)

    def eg(sym: str, heat=None, s=None) -> dict:
        return ex.entry_gates(s or s0, sym, r8, heat or g["cold"])
    _check("нижняя четверть: доля > 0.75 строго (7/8 — да, 6/8 — нет)",
           eg("Q7")["why"] == ["bottom"] and eg("Q6")["why"] == [], failures)
    _check("вне топ-39%: 4/8 — да, 3/8 — нет (квота проверяется позже)",
           eg("M4")["illiquid"] and not eg("M3")["illiquid"] and eg("M4")["why"] == [], failures)
    _check("нет данных оборота — не отсекается, метки оборота пустые",
           eg("NONE")["why"] == [] and not eg("NONE")["illiquid"]
           and eg("NONE")["labels"]["vol_rank"] is None, failures)
    _check("перегрев → hot, нет данных → nodata; hot_block false — не отсекает",
           eg("TOP", g["hot"])["why"] == ["hot"] and eg("TOP", g["none"])["why"] == ["nodata"]
           and eg("TOP", g["hot"], {**s0, "hot_block": False})["why"] == [], failures)
    _check("метки: место, пары, доля, оборот, флаги, день данных; без данных рынка hot_n None",
           eg("Q7", g["hot"])["labels"] == {"vol_rank": 7, "vol_pairs": 8, "vol_share": 7 / 8,
                                             "vol_usd": 1e6 / 7, "hot_n": 1,
                                             "hot_flags": "mvrv_eth", "hot_day": d0}
           and eg("TOP", g["none"])["labels"]["hot_n"] is None, failures)

    # --- (1) нижняя четверть -> тень; основная книга не тронута
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "LOW")
        m.ranks = rk({"LOW": 7, "TOP": 1}, 8)
        card(c, "LOW", now0)
        code1, lines1 = ex.run_dry(c, m, now=now0, mctx=cold)
        ps1 = q(c, "SELECT * FROM dry_positions ORDER BY id")
        os1 = q(c, "SELECT * FROM dry_orders ORDER BY link_id")
        mo1 = money(c, "R")
        bs1 = deliver_brief(c, now=now0, exec_exit=0)
    _check("нижняя четверть (7/8): обе книги — в тень, shadow_why bottom, позиции открыты",
           code1 == 0 and sorted(p["book"] for p in ps1) == ["H", "R"]
           and all(p["shadow"] == 1 and p["status"] == "open" and p["shadow_why"] == "bottom"
                   for p in ps1), failures)
    _check("тень: та же лестница (5 ступеней), ордера shd-…, ступень 1 исполнена рынком",
           len(os1) == 10 and all(o["link_id"].startswith("shd-") for o in os1)
           and sorted(o["link_id"] for o in os1 if o["status"] == "filled")
           == [f"shd-H{ps1[1]['id']}-B1", f"shd-R{ps1[0]['id']}-B1"], failures)
    _check("метки входа в тени: место 7 из 8, доля 0.875, оборот, 0 флагов, день данных",
           all(p["vol_rank"] == 7 and p["vol_pairs"] == 8 and abs(p["vol_share"] - 0.875) < 1e-12
               and p["vol_usd"] > 0 and p["hot_n"] == 0 and p["hot_flags"] == ""
               and p["hot_day"] == cold["day"] for p in ps1), failures)
    _check("тень мест и денег не занимает: основная — 0 позиций, свободно 1500, резерв 0",
           mo1["main"]["open"] == 0 and abs(mo1["free"] - 1500.0) < 1e-9 and mo1["res"] == 0
           and mo1["shadow"]["open"] == 1 and mo1["shadow"]["spent"] > 0, failures)
    _check("P&L в день входа (закрытий ещё нет) — по средней цене: −$0.015 комиссии, не −$10",
           abs(mo1["shadow"]["pnl"] + 10.0 * ex.MARKET_FEE) < 1e-9, failures)
    _check("лог: «LOW: НЕ покупаю — нижняя четверть по обороту (7-е место из 8) → в тень R/H»",
           any(x.startswith("LOW: НЕ покупаю — нижняя четверть по обороту (7-е место из 8) → "
                            "в тень R/H: 5 ступ.") for x in lines1)
           and any(x.startswith("оборот: 8 пар Bybit, нижняя четверть — место > 6")
                   for x in lines1)
           and any(x.startswith("тень (мест и денег не занимает): R открыто 1") for x in lines1)
           and m.ranks_calls == 1, failures)
    exl1 = tg.executor_brief_lines(bs1["executor"], c)
    _check("сводка дня: «не купил бы, ушло в тень: LOW — нижняя четверть…», строка тени",
           any(x == "не купил бы, ушло в тень: LOW — нижняя четверть по обороту (7-е место из 8)"
               for x in exl1)
           and any(x.startswith("👥 тень: 📏 1 поз.") for x in exl1)
           and not any(x.startswith("поставил бы") for x in exl1), failures)

    # --- нет данных оборота: не отсекать, метки пустые, в логе причина
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "LOW")
        m.ranks_fail = True
        card(c, "LOW", now0)
        code2, lines2 = ex.run_dry(c, m, now=now0, mctx=cold)
        ps2 = q(c, "SELECT * FROM dry_positions")
    _check("нет данных оборота (сбой Bybit): покупка в основную, метки оборота пустые, код 0",
           code2 == 0 and len(ps2) == 2 and all(p["shadow"] == 0 and p["status"] == "open"
                                                and p["vol_rank"] is None and p["hot_n"] == 0
                                                for p in ps2), failures)
    _check("лог: «нет данных оборота: RuntimeError … — фильтры по обороту не отсекают», метки",
           any(x.startswith("нет данных оборота: RuntimeError: Bybit не ответил — фильтры по "
                            "обороту не отсекают") for x in lines2)
           and any(x == "  метки: нет данных оборота, рынок перегрев 0/8" for x in lines2),
           failures)

    # --- (2) квота: переполнение, порядок по месту, метка на день входа
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        m = _FakeMarket(now0)
        for sym in ("ILA", "ILB", "LIQ"):
            pair(m, sym)
        for i in range(4):
            seed(c, f"OLD{i}", 0.5, m=m)               # 4 места вне топа по метке входа
        seed(c, "WAS", 0.10, m=m)                    # на входе — в топе, сегодня — вне его
        m.ranks = rk({"ILA": 45, "ILB": 42, "LIQ": 10, "WAS": 60, "OLD0": 50}, 100)
        card(c, "ILA", now0 - 120)                   # раньше всех по времени, но место хуже
        card(c, "ILB", now0 - 60)
        card(c, "LIQ", now0)
        code3, lines3 = ex.run_dry(c, m, now=now0, mctx=cold)
        ps3 = {(p["symbol"], p["book"]): p for p in q(c, "SELECT * FROM dry_positions")}
    _check("квота: 4 из 5 мест вне топа — ILB (42-е) взяла 5-е место, ILA (45-е) — в тень quota",
           code3 == 0 and all(ps3[("ILB", b)]["shadow"] == 0 and ps3[("ILB", b)]["status"] == "open"
                              for b in "RH")
           and all(ps3[("ILA", b)]["shadow"] == 1 and ps3[("ILA", b)]["shadow_why"] == "quota"
                   for b in "RH"), failures)
    _check("квота: ликвидная LIQ (10-е) — в основную, квоту не занимает",
           all(ps3[("LIQ", b)]["shadow"] == 0 and ps3[("LIQ", b)]["status"] == "open"
               for b in "RH"), failures)
    _check("порядок дня — по месту: LIQ, ILB, ILA (ILA пришла первой, но место хуже)",
           ps3[("LIQ", "R")]["id"] < ps3[("ILB", "R")]["id"] < ps3[("ILA", "R")]["id"], failures)
    _check("квота считает метку входа: WAS (0.10 на входе, сегодня 60-е) — не в квоте",
           ps3[("ILB", "R")]["shadow"] == 0, failures)
    _check("лог: «ILA: НЕ покупаю — квота неликвида (45-е место из 100): вне топ-39% уже 5 из 5»",
           any(x.startswith("ILA: НЕ покупаю — квота неликвида (45-е место из 100): вне топ-39% "
                            "уже 5 из 5 мест → в тень R/H") for x in lines3), failures)

    # --- (4) перегрев: новые — в тень; открытые позиции, лимитки и выходы работают
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "AAA", hours=[(d0 + 8 * H, 0.929)])
        pair(m, "BBB")
        m.ranks = rk({"AAA": 1, "BBB": 2}, 100)
        card(c, "AAA", now0)
        ex.run_dry(c, m, now=now0, mctx=cold)
        m.now = d0 + D + 7 * H
        card(c, "BBB", m.now)
        code4, lines4 = ex.run_dry(c, m, now=m.now, mctx=hot)
        ps4 = {(p["symbol"], p["book"]): p for p in q(c, "SELECT * FROM dry_positions")}
        aaa_b2 = q(c, "SELECT status FROM dry_orders WHERE link_id='dry-R1-B2'")[0]["status"]
        aaa_new = q(c, "SELECT COUNT(*) n FROM dry_orders WHERE pair='AAAUSDT' AND status='new'")
    _check("перегрев: BBB — в тень hot, метки 1 флаг mvrv_eth",
           code4 == 0 and all(ps4[("BBB", b)]["shadow"] == 1 and ps4[("BBB", b)]["shadow_why"] == "hot"
                              and ps4[("BBB", b)]["hot_n"] == 1
                              and ps4[("BBB", b)]["hot_flags"] == "mvrv_eth" for b in "RH"),
           failures)
    _check("перегрев: открытая AAA не тронута — лимитка 0.93 исполнилась, 3 лимитки ждут",
           aaa_b2 == "filled" and aaa_new[0]["n"] == 6 and ps4[("AAA", "R")]["status"] == "open"
           and ps4[("AAA", "R")]["shadow"] == 0, failures)
    _check("лог: «рынок: перегрев 1/8, горят: MVRV ETH — новых лестниц нет…», BBB «перегрев рынка»",
           "рынок: перегрев 1/8, горят: MVRV ETH — новых лестниц нет, карточки уходят в тень"
           in lines4
           and any(x.startswith("BBB: НЕ покупаю — перегрев рынка (MVRV ETH) → в тень")
                   for x in lines4), failures)

    def one(label: str, ctx, want_shadow: int, want_why: str, **exe) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = mk(tmp, **exe)
            m = _FakeMarket(now0)
            pair(m, "AAA")
            card(c, "AAA", now0)
            kw = {} if ctx is None else {"mctx": ctx}
            code, lines = ex.run_dry(c, m, now=now0, **kw)
            ps = q(c, "SELECT shadow, shadow_why, status FROM dry_positions")
        _check(label, code == 0 and len(ps) == 2
               and all(p["status"] == "open" and p["shadow"] == want_shadow
                       and p["shadow_why"] == want_why for p in ps), failures)
    one("нет данных рынка ({}) — в тень nodata", {}, 1, "nodata")
    one("данные рынка старше 3 дн. — в тень nodata", {**cold, "day": d0 - 4 * D}, 1, "nodata")
    one("mctx не передан, market_daily пуст — в тень nodata", None, 1, "nodata")
    one("данным рынка 3 дня, флагов 0 — покупка в основную", {**cold, "day": d0 - 3 * D}, 0, "")
    one("hot_block false: перегрев не отсекает — покупка в основную", hot, 0, "",
        hot_block=False)
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp, shadow=False)
        m = _FakeMarket(now0)
        pair(m, "AAA")
        card(c, "AAA", now0)
        ex.run_dry(c, m, now=now0, mctx=hot)
        ps_off = q(c, "SELECT shadow, status, reason FROM dry_positions")
    _check("теневая книга выключена: отсеянное — отказ с причиной фильтра, тени нет",
           len(ps_off) == 2 and all(p["shadow"] == 0 and p["status"] == "rejected"
                                    and p["reason"] == "перегрев рынка (MVRV ETH)"
                                    for p in ps_off), failures)

    # --- обычные отказы в тень не идут, даже при перегреве
    def plain(label: str, setup, want: str, ctx=hot, wl=None, **exe) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = mk(tmp, wl=wl, **exe)
            m = _FakeMarket(now0)
            pair(m, "AAA")
            setup(c, m)
            card(c, "AAA", now0)
            ex.run_dry(c, m, now=now0, mctx=ctx)
            ps = q(c, "SELECT shadow, status, reason FROM dry_positions WHERE symbol='AAA'")
            no = q(c, "SELECT 1 FROM dry_orders WHERE pair='AAAUSDT'")
        _check(label, len(ps) == 2 and not no
               and all(p["shadow"] == 0 and p["status"] == "rejected" and want in p["reason"]
                       for p in ps), failures)
    plain("перегрев + нет на Bybit — обычный отказ, не в тень",
          lambda c, m: m.inst.update({"AAAUSDT": None}), "нет на Bybit spot")
    plain("перегрев + метка ST — обычный отказ, не в тень",
          lambda c, m: m.inst["AAAUSDT"].update(st=True), "метка ST")
    plain("тикер на Bybit — другая монета (флаг сканера): отказ, не в тень",
          lambda c, m: None, "на Bybit под тикером AAA другая монета",
          wl=[{"symbol": "AAA", "coin_id": "aaa", "flags": ["bybit_ticker_mismatch"]}])
    plain("холодный рынок + лимит монет — обычный отказ, не в тень",
          lambda c, m: seed(c, "X", 0.1, m=m), "лимит 1 монет", ctx=cold, max_coins=1)
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp, max_coins=1)
        m = _FakeMarket(now0)
        pair(m, "AAA")
        seed(c, "X", 0.1, m=m)
        card(c, "AAA", now0)
        ex.run_dry(c, m, now=now0, mctx=hot)
        ps_full = q(c, "SELECT shadow, status FROM dry_positions WHERE symbol='AAA'")
    _check("перегрев + полная книга — в тень (перегрев — свойство рынка, проверяется до лимитов)",
           len(ps_full) == 2 and all(p["shadow"] == 1 and p["status"] == "open"
                                     for p in ps_full), failures)

    # --- тень: лимит, не занимает мест/денег, повтор, независимость от основной
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp, shadow_max_coins=1, max_coins=1)
        m = _FakeMarket(now0)
        for sym in ("LOW", "LOW2", "LIQ"):
            pair(m, sym)
        m.ranks = rk({"LOW": 7, "LOW2": 8, "LIQ": 1}, 8)
        card(c, "LOW", now0)
        card(c, "LOW2", now0)
        _, lines_a = ex.run_dry(c, m, now=now0, mctx=cold)
        bs_a = deliver_brief(c, now=now0, exec_exit=0)
        m.now = now0 + D                              # день 2: ликвидная при max_coins 1
        card(c, "LIQ", m.now)
        ex.run_dry(c, m, now=m.now, mctx=cold)
        mo_b = money(c, "R")
        liq_res = sum(o["usd"] for o in q(
            c, "SELECT o.usd FROM dry_orders o JOIN dry_positions p ON p.id=o.position_id "
               "WHERE p.symbol='LIQ' AND o.book='R' AND o.status='new'"))
        liq_spent = q(c, "SELECT spent_usdt FROM dry_positions WHERE symbol='LIQ' AND "
                         "book='R'")[0]["spent_usdt"]
        m.now = now0 + 2 * D                          # день 3: LOW снова в нижней четверти
        card(c, "LOW", m.now)
        _, lines_c = ex.run_dry(c, m, now=m.now, mctx=cold)
        n_low = len(q(c, "SELECT id FROM dry_positions WHERE symbol='LOW'"))
        m.now = now0 + 3 * D                          # день 4: LOW поднялась в топ
        c._d["executor"]["max_coins"] = 2
        m.ranks = rk({"LOW": 1, "LOW2": 8, "LIQ": 2}, 8)
        card(c, "LOW", m.now)
        ex.run_dry(c, m, now=m.now, mctx=cold)
        low = q(c, "SELECT shadow, status FROM dry_positions WHERE symbol='LOW' ORDER BY id")
    _check("тень заполнена (1 монета): LOW2 — отказ «теневая книга заполнена», shadow=1",
           any(x.startswith("LOW2: ОТКАЗ — нижняя четверть по обороту (8-е место из 8); "
                            "теневая книга заполнена (1 монет)") for x in lines_a), failures)
    _check("сводка дня: отказ LOW2 с причиной, LOW — в тени",
           ("LOW2", "нижняя четверть по обороту (8-е место из 8); теневая книга заполнена "
                    "(1 монет)") in bs_a["executor"]["rejected"]
           and bs_a["executor"]["shadowed"] == [("LOW", "нижняя четверть по обороту "
                                                        "(7-е место из 8)")], failures)
    _check("тень не занимает мест: при max_coins 1 и монете в тени LIQ куплена в основную",
           mo_b["main"]["open"] == 1 and mo_b["shadow"]["open"] == 1, failures)
    _check("деньги основной — без тени: резерв = лимитки LIQ, свободно = 1500 − LIQ − резерв",
           abs(mo_b["res"] - liq_res) < 1e-9
           and abs(mo_b["free"] - (1500.0 - liq_spent - liq_res)) < 1e-9, failures)
    _check("повторная карточка LOW (всё ещё внизу) — «в тени уже с …», новых позиций нет",
           any(x.startswith("LOW: НЕ покупаю — нижняя четверть") and "в тени уже с" in x
               for x in lines_c) and n_low == 2, failures)
    _check("LOW поднялась в топ — покупка в основную, теневая позиция живёт отдельно",
           [(p["shadow"], p["status"]) for p in low] == [(1, "open"), (1, "open"), (0, "open"),
                                                         (0, "open")], failures)

    # --- тень по тем же правилам: R фиксирует 1/3 на +50%, H держит; продажи — не в сводке
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "LOW", after=[(d0, 1.0), (d0 + D, 1.6)])
        m.ranks = rk({"LOW": 7}, 8)
        card(c, "LOW", now0)
        ex.run_dry(c, m, now=now0, mctx=cold)
        m.now = d0 + 2 * D + H
        _, lines_e = ex.run_dry(c, m, now=m.now, mctx=cold)
        sells = q(c, "SELECT link_id, book, signal FROM dry_orders WHERE side='Sell'")
        pe = {p["book"]: p for p in q(c, "SELECT * FROM dry_positions")}
        bs_e = deliver_brief(c, now=m.now, exec_exit=0)
        st_ = Store(c["output"]["db_path"])
        st_.upsert_market({d0 + k * D: {"total_mcap": 1000 + 100 * k, "btc_dominance": 50,
                                        "stables_usd": 100} for k in range(0, 4)})
        st_.close()
        ws = ex.weekly_shadow(c)
        wm = ex.weekly_books(c)
    _check("тень R: 1/3 на +50% — ордер shd-R…-S-L0; тень H держит; позиции парные",
           sells == [{"link_id": f"shd-R{pe['R']['id']}-S-L0", "book": "R", "signal": "ladder_0"}]
           and pe["R"]["shadow"] == pe["H"]["shadow"] == 1 and pe["H"]["status"] == "open"
           and any(x.startswith("тень R LOW: продал бы") for x in lines_e), failures)
    _check("сводка дня: продажи и исполнения тени в «за сутки» основной не попадают",
           bs_e["executor"]["sells"] == [] and bs_e["executor"]["fills"] == 0, failures)
    wr = next((b for b in ws if b["key"] == "R"), {})
    _check("недельная тень: R и H, разбивка по причинам bottom, P&L = Σ позиций; основных нет",
           [b["key"] for b in ws] == ["R", "H"] and wm == []
           and wr.get("by_why", {}).get("bottom", {}).get("n") == 1
           and abs(wr.get("pnl_usd", 0) - ex.pnl_usdt(pe["R"])) < 1e-9 and wr.get("n") == 1,
           failures)
    wblk = tg.executor_weekly_block(wm, ws)
    _check("недельный блок: «👥 Тень», строка книги и «нижняя четверть 1 поз.»",
           any(x.startswith("👥 <b>Тень</b>") for x in wblk)
           and any(x.startswith("   нижняя четверть 1 поз. +") for x in wblk), failures)

    # --- миграция БД ec02fc7 и сводка по немигрированной БД (только чтение)
    old = ("CREATE TABLE dry_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, book TEXT NOT NULL, "
           "pair TEXT NOT NULL, symbol TEXT NOT NULL, coin_id TEXT DEFAULT '', card_day INTEGER "
           "NOT NULL, status TEXT NOT NULL, reason TEXT DEFAULT '', created_ts REAL NOT NULL, "
           "base_low REAL, stop_pct REAL, min_amt REAL DEFAULT 5, qty REAL DEFAULT 0, bought_qty "
           "REAL DEFAULT 0, spent_usdt REAL DEFAULT 0, proceeds_usdt REAL DEFAULT 0, hwm REAL, "
           "last_price REAL, last_day INTEGER, fills_until REAL DEFAULT 0, first_fill_ts REAL, "
           "closed_ts REAL, UNIQUE(book, pair, card_day));"
           "CREATE TABLE dry_orders (link_id TEXT PRIMARY KEY, position_id INTEGER NOT NULL, "
           "book TEXT NOT NULL, pair TEXT NOT NULL, side TEXT NOT NULL, kind TEXT NOT NULL, "
           "step INTEGER, signal TEXT DEFAULT '', price REAL, qty REAL, usd REAL, status TEXT "
           "NOT NULL, created_ts REAL NOT NULL, filled_ts REAL, fee_usdt REAL DEFAULT 0, note "
           "TEXT DEFAULT '');")
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        con = sqlite3.connect(c["output"]["db_path"])
        con.executescript(old)
        con.execute("INSERT INTO dry_positions(book, pair, symbol, card_day, status, created_ts, "
                    "qty, bought_qty, spent_usdt, last_price) VALUES "
                    "('R','XUSDT','X',?,'open',?,10,10,10,1)", (d0, now0))
        con.commit()
        con.close()
        bs_old = ex.brief_state(c, now=now0)
        wk_old = ex.weekly_shadow(c)
        con = ex.connect(c["output"]["db_path"])
        cols = {r[1] for r in con.execute("PRAGMA table_info(dry_positions)")}
        row = dict(con.execute("SELECT * FROM dry_positions").fetchone())
        con.close()
    _check("сводка по БД до миграции: позиция основная, тени нет, не падает",
           bs_old["books"]["R"]["open"] == 1 and bs_old["shadow"]["R"]["open"] == 0
           and bs_old["shadowed"] == [] and wk_old == [], failures)
    _check("миграция: ADD COLUMN shadow…hot_day; старая позиция — основная, меток нет",
           {n for n, _ in ex._MIGRATIONS} <= cols and row["shadow"] == 0
           and row["shadow_why"] == "" and row["vol_rank"] is None and row["hot_n"] is None,
           failures)
    _check("миграция блока C: qty_step, tick, trail_armed_ts — NULL (без округления, трейл не "
           "взведён), data_err пусто; сводка по старой БД — без «нет данных»",
           {"qty_step", "tick", "trail_armed_ts", "data_err"} <= cols
           and row["qty_step"] is None and row["tick"] is None and row["trail_armed_ts"] is None
           and row["data_err"] == "" and bs_old["nodata"] == [], failures)

    # --- строки карточки (scan --notify) и сводок
    from scanner.models import Candidate

    def cn(sym: str, ctx, c_=cfg, venue: str = "Bybit spot", flags=None) -> list[str]:
        cand = Candidate(source="t", track="A", symbol=sym, zone="ПРУЖИНА/ДНО", score=72.0,
                         confidence=0.9, rf_venue=venue, flags=list(flags or []))
        return ex.card_notes(c_, cand, rk({"LOW": 7, "TOP": 1}, 8), ctx, now0)
    off_cfg = Config({**cfg._d, "executor": {**cfg._d["executor"], "enabled": False}})
    _check("карточка: «⚠ нижняя четверть по обороту — исполнитель не покупает (7-е место из 8 "
           "пар Bybit)»",
           cn("LOW", cold) == ["⚠ нижняя четверть по обороту — исполнитель не покупает "
                               "(7-е место из 8 пар Bybit)"], failures)
    _check("карточка: «🔥 перегрев — исполнитель пропускает (перегрев 1/8, горят: MVRV ETH)»",
           cn("TOP", hot) == ["🔥 перегрев — исполнитель пропускает (перегрев 1/8, горят: "
                              "MVRV ETH)"], failures)
    _check("карточка: нет данных рынка — «⚠ нет свежих данных рынка — исполнитель пропускает»",
           cn("TOP", {}) == ["⚠ нет свежих данных рынка — исполнитель пропускает (данных рынка "
                             "нет)"], failures)
    _check("карточка: обе причины — две строки; ликвидная в холодном рынке — ничего",
           len(cn("LOW", hot)) == 2 and cn("TOP", cold) == [], failures)
    _check("карточка: DEX, тёзка под тикером, исполнитель выключен — строк нет",
           cn("LOW", hot, venue="DEX only") == []
           and cn("LOW", hot, flags=["bybit_ticker_mismatch"]) == []
           and cn("LOW", hot, c_=off_cfg) == [], failures)
    plan = plan_ladder(1.0, 0.95, 50, steps=5, min_order=10, tick=0.01, qty_step=0.1,
                       exch_min_amt=5, floor_pct=25, sell="prod",
                       prod_levels=cfg["stage8_exit"]["ladder"])
    notes = cn("LOW", {**hot, "hot": {"lit": ["fng30"], "avail": 8}})
    noisy = Candidate(source="t", track="A", symbol="LOW", zone="ПРУЖИНА/ДНО", score=72.0,
                      confidence=0.9, rf_venue="Bybit spot", flags=list(_FLAG_KEYS),
                      liveness_notes=[f"⚠ заметка {i} " + "x" * 60 for i in range(6)])
    cap = tg.card_caption(noisy, plan, cfg, price=1.0, P=lambda x: f"{x:.2f}", exec_notes=notes)
    txt = tg.format_coin_card(noisy, plan, cfg, price=1.0, exec_notes=notes)
    _check("карточка: строки исполнителя в тексте (экранированы: F&amp;G) и в подписи ≤ 1024",
           "горят: F&amp;G" in txt and all(n in tg.strip_html(cap) for n in notes)
           and len(tg.strip_html(cap)) <= tg.CAPTION_MAX and len(notes) == 2, failures)
    shb = [{"emoji": "📏", "label": "правила", "positions": 3, "n": 3, "book_pct": -4.0,
            "alt_pct": 2.0, "pnl_usd": -1.2,
            "by_why": {"hot": {"n": 2, "cost": 100.0, "pnl": 4.0},
                       "bottom": {"n": 1, "cost": 50.0, "pnl": -10.0}}}]
    mb = [{"emoji": "📏", "label": "правила", "positions": 2, "n": 2, "book_pct": 12.34,
           "alt_pct": 5.0, "basket_pct": 3.0, "pnl_usd": 1.23}]
    blk = tg.executor_weekly_block(mb, shb)
    _check("недельный блок: основная с $, тень «−4.0% · альты +2.0% → −6.0 п.п.», по причинам",
           "📏 правила: +12.3% · альты +5.0% · корзина +3.0% → +7.3 п.п. к альтам (2 поз., "
           "+$1.23)" in blk
           and "📏 правила: −4.0% · альты +2.0% → −6.0 п.п. к альтам (3 поз., −$1.20)" in blk
           and "   перегрев 2 поз. +4.0% · нижняя четверть 1 поз. −20.0%" in blk
           and any("Тень хуже основной книги" in x for x in blk)
           and not any("считается в обеих" in x for x in blk), failures)
    shb2 = [dict(shb[0], positions=2)]                # 2 позиции, причин 3 — одна в обеих
    _check("недельный блок: причины пересекаются — «Монета с двумя причинами считается в обеих»",
           any("Монета с двумя причинами считается в обеих" in x
               for x in tg.executor_weekly_block([], shb2)), failures)
    _check("недельный блок: только тень — блок есть; format_weekly показывает «Тень»",
           tg.executor_weekly_block([], shb)[0].startswith("🤖")
           and "Тень" in format_weekly({"week_no": 2, "milestone": False, "milestone_weeks": 4,
                                        "opened": 0, "open_now": 0, "invalidations": 0,
                                        "ladder_hits": 0, "trailings": 0,
                                        "paper_pnl_usdt": 0.0, "real_open": 0,
                                        "executor": [], "executor_shadow": shb}, cfg), failures)
    exb = tg.executor_brief_lines(
        {"books": {"R": {"open": 0, "closed": 0, "pnl": 0.0, "label": "правила", "emoji": "📏"}},
         "shadow": {"R": {"open": 0, "closed": 0, "pnl": 0.0, "emoji": "📏"}},
         "opened": [], "rejected": [], "shadowed": [("HYPER", "нижняя четверть по обороту "
                                                              "(302-е место из 370)")]}, cfg)
    _check("сводка дня: только отсеянное сегодня — блок есть, причина понятна",
           exb[0].startswith("🤖") and "не купил бы, ушло в тень: HYPER — нижняя четверть по "
                                      "обороту (302-е место из 370)" in exb, failures)


def test_executor_fixes(cfg, failures: list[str]) -> None:
    print("Исполнитель, блок C: сбои данных и делистинг, кулдаун, 15 монет на книгу, продажи, "
          "защёлка трейла, тикер fail-closed, идемпотентность, окно карточек, тейк по high:")
    import copy
    import json as _json
    import sqlite3
    import tempfile
    import time as _time
    from pathlib import Path as _P
    from scanner import executor as ex
    from scanner.config import Config
    from scanner.db import Store
    from scanner.ladder import LIMIT_FEE, MARKET_FEE, round_step
    from scanner.notify import telegram as tg
    from scanner.sources import bybit
    D, H = ex.DAY, ex.HOUR
    d0 = 1790812800                        # 2026-10-01 00:00 UTC
    now0 = d0 + 6 * H + 1800
    inst = {"symbol": "", "status": "Trading", "st": False, "tick": 0.01, "qty_step": 0.1,
            "min_qty": 0.1, "min_amt": 5.0}
    hist = [(d0 - k * D, 0.95 if k == 10 else 1.0) for k in range(40, 0, -1)]
    cold = {"day": d0 + 4 * D, "hot": {"lit": [], "near": [], "avail": 8}}

    def mk(tmp: str, wl: list | None = None, **exe) -> Config:
        d = copy.deepcopy(cfg._d)
        d["output"] = {**d.get("output", {}), "db_path": str(_P(tmp) / "t.db"),
                       "watchlist_json": str(_P(tmp) / "wl.json")}
        d["executor"] = {**d.get("executor", {}), **exe}
        (_P(tmp) / "wl.json").write_text(_json.dumps(wl or []), encoding="utf-8")
        Store(d["output"]["db_path"]).close()
        return Config(d)

    def card(c: Config, sym: str, ts: float, add: bool = True) -> None:
        if add:
            _wl_add(c, sym)
        con = sqlite3.connect(c["output"]["db_path"])
        con.execute("INSERT INTO alert_log(symbol, ts, score) VALUES (?,?,72)", (sym, ts))
        con.commit()
        con.close()

    def q(c: Config, sql: str, args=()) -> list[dict]:
        con = sqlite3.connect(c["output"]["db_path"])
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, args).fetchall()]
        finally:
            con.close()

    def pair(m, sym: str, after=(), hours=(), price: float = 1.0, days=None) -> None:
        p = f"{sym}USDT"
        m.inst[p] = {**inst, "symbol": p}
        m.price[p] = price
        m.days[p] = list(days) if days is not None else hist + list(after)
        m.hours[p] = list(hours)

    def run(c: Config, m, now: float | None = None):
        return ex.run_dry(c, m, now=m.now if now is None else now, mctx=cold)

    def seed(c: Config, sym: str, book: str = "R", **cols) -> None:
        row = {"book": book, "pair": f"{sym}USDT", "symbol": sym, "card_day": d0 - D,
               "status": "open", "created_ts": now0 - D, "qty": 10.0, "bought_qty": 10.0,
               "spent_usdt": 10.0, "min_amt": 5.0, **cols}
        con = ex.connect(c["output"]["db_path"])
        con.execute(f"INSERT INTO dry_positions({', '.join(row)}) VALUES "
                    f"({', '.join('?' * len(row))})", tuple(row.values()))
        con.commit()
        con.close()

    # --- 1. сбой ответа Bybit ≠ «свечей нет» (sources/bybit: ключ err)
    class _HJ:
        def __init__(self, *answers):
            self.answers = list(answers)

        def get_json(self, url, params=None, headers=None, use_cache=True, retries=5):
            return self.answers.pop(0) if self.answers else None
    t_old = 1_600_000_000                   # 2020 — свеча давно закрыта
    ok = {"retCode": 0, "result": {"list": [[str(t_old * 1000), "1", "1.2", "0.9", "1.1", "5",
                                             "5"]]}}
    nsup = {"retCode": 10001, "retMsg": "Not supported symbols", "result": {}}
    r_ok = bybit.fetch_daily_ohlcv(_HJ(ok), "AAAUSDT")
    r_none = bybit.fetch_daily_ohlcv(_HJ(None), "AAAUSDT")
    r_rc = bybit.fetch_daily_ohlcv(_HJ(nsup), "AAAUSDT")
    _check("Bybit D: ответ есть — без err; сеть (None) и retCode≠0 — пусто + err с причиной",
           "err" not in r_ok and r_ok["c"] == [1.1] and r_none["c"] == []
           and "сеть" in r_none["err"] and r_rc["err"] == "retCode 10001: Not supported symbols",
           failures)
    page = {"retCode": 0, "result": {"list": [
        [str((t_old - i * 3600) * 1000), "1", "1", "0.5", "1", "1", "1"] for i in range(1000)]}}
    k_part = bybit.fetch_klines(_HJ(page, None), "AAAUSDT", "60", (t_old - 1500 * 3600) * 1000,
                                now_ms=(t_old + 7200) * 1000)
    k_none = bybit.fetch_klines(_HJ(None), "AAAUSDT", "60", 0)
    _check("Bybit часовые: вторая страница не пришла — 1000 свечей + err; первая — пусто + err",
           len(k_part["ts"]) == 1000 and "сеть" in k_part["err"]
           and k_none["ts"] == [] and "сеть" in k_none["err"], failures)
    good = {"retCode": 0, "result": {"list": [{"symbol": "AAAUSDT", "status": "Trading",
                                               "lotSizeFilter": {"basePrecision": "0.01"},
                                               "priceFilter": {"tickSize": "0.001"}}]}}
    _check("instrument: with_err — сеть → {err}, «Not supported symbols» → None (пары нет); "
           "без with_err (карточка, pos add) — None как раньше",
           bybit.fetch_instrument(_HJ(None), "X", with_err=True) == {"err": "нет ответа "
                                                                            "(сеть/HTTP)"}
           and bybit.fetch_instrument(_HJ(nsup), "X", with_err=True) is None
           and bybit.fetch_instrument(_HJ({"retCode": 0, "result": {"list": []}}), "X",
                                      with_err=True) is None   # так Bybit отвечает 09.10
           and bybit.fetch_instrument(_HJ(None), "X") is None
           and bybit.fetch_instrument(_HJ(good), "X", with_err=True)["qty_step"] == 0.01,
           failures)

    # --- 1. позиция при сбое дневных/часовых свечей не трогается; потом догоняет
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        m = _FakeMarket(now0)
        # AAA: часовой low 0.69 проходит все лимитки, два закрытия ниже пола — стоп
        pair(m, "AAA", [(d0, 1.0), (d0 + D, 0.70), (d0 + 2 * D, 0.70)], [(d0 + D + 5 * H, 0.69)])
        pair(m, "BBB", [(d0, 1.0), (d0 + D, 1.1), (d0 + 2 * D, 1.1)])
        card(c, "AAA", now0)
        card(c, "BBB", now0)
        run(c, m)

        def snap() -> tuple:
            ps = [(p["qty"], p["spent_usdt"], p["last_day"], p["fills_until"], p["status"])
                  for p in q(c, "SELECT * FROM dry_positions WHERE symbol='AAA' ORDER BY id")]
            return ps, q(c, "SELECT link_id, status FROM dry_orders WHERE pair='AAAUSDT' "
                            "ORDER BY link_id")
        before = snap()
        m.now = d0 + 3 * D + 6 * H
        m.err_daily.add("AAAUSDT")
        code1, lines1 = run(c, m)
        after1 = snap()
        err1 = [p["data_err"] for p in q(c, "SELECT data_err FROM dry_positions WHERE "
                                            "symbol='AAA'")]
        bbb1 = [p["last_day"] for p in q(c, "SELECT last_day FROM dry_positions WHERE "
                                            "symbol='BBB'")]
        bs1 = ex.brief_state(c, now=m.now)
        m.err_daily.clear()
        m.err_hourly.add("AAAUSDT")
        m.now += H
        code2, lines2 = run(c, m)
        after2 = snap()
        m.err_hourly.clear()
        m.now += H
        code3, lines3 = run(c, m)
        p3 = q(c, "SELECT * FROM dry_positions WHERE symbol='AAA'")
        bs3 = ex.brief_state(c, now=m.now)
    w1 = "⚠ нет данных Bybit: AAAUSDT (дневные свечи: retCode 10016: Service unavailable)"
    _check("сбой дневных свечей: код 4, одна строка «⚠ нет данных Bybit: AAAUSDT (…)» на пару",
           code1 == ex.EXIT_NODATA and lines1.count(w1) == 1, failures)
    _check("сбой дневных: позиция и ордера не тронуты (ни исполнений, ни стопа, ни отмен), "
           "причина в data_err; BBB обработана",
           after1 == before and all(e.startswith("дневные свечи: retCode 10016") for e in err1)
           and bbb1 == [d0 + 2 * D] * 2, failures)
    exl1 = tg.executor_brief_lines(bs1, c)
    _check("сводка дня: «⚠ нет данных Bybit: AAAUSDT (дневные свечи: …) — позиция ждёт данных»",
           bs1["nodata"] == [("AAAUSDT", "дневные свечи: retCode 10016: Service unavailable")]
           and any(x.startswith("⚠ нет данных Bybit: AAAUSDT (дневные свечи") for x in exl1),
           failures)
    _check("сбой часовых при стоящих лимитках: код 4, лимитки не сняты и не исполнены, стопа нет",
           code2 == ex.EXIT_NODATA and "⚠ нет данных Bybit: AAAUSDT (часовые свечи: нет ответа (сеть/HTTP))"
           in lines2 and after2 == before, failures)
    _check("данные вернулись: все 5 ступеней исполнены, затем стоп; data_err стёрт, в сводке пусто",
           code3 == 0 and all(p["status"] == "closed" and p["reason"] == "стоп"
                              and abs(p["spent_usdt"] - 50.157) < 1e-6 and p["data_err"] == ""
                              for p in p3) and bs3["nodata"] == [], failures)

    with tempfile.TemporaryDirectory() as tmp:          # дыра в часовых свечах
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "AAA", hours=[(d0 + 8 * H, 0.929)])
        card(c, "AAA", now0)
        run(c, m)
        m.hour_gap["AAAUSDT"] = {d0 + 7 * H}
        m.now = d0 + 9 * H
        code_g, lines_g = run(c, m)
        b2_g = q(c, "SELECT status FROM dry_orders WHERE link_id='dry-R1-B2'")[0]["status"]
        m.hour_gap.clear()
        code_g2, _ = run(c, m)
        b2_g2 = q(c, "SELECT status FROM dry_orders WHERE link_id='dry-R1-B2'")[0]["status"]
    _check("дыра: первой часовой свечи после постановки нет — «нет данных», лимитка ждёт; "
           "свеча пришла — исполнена",
           code_g == ex.EXIT_NODATA and "⚠ нет данных Bybit: AAAUSDT (часовые свечи: нет свечи 01.10 07:00 "
                           "UTC)" in lines_g
           and b2_g == "new" and code_g2 == 0 and b2_g2 == "filled", failures)
    with tempfile.TemporaryDirectory() as tmp:          # лимиток нет — часовые не нужны
        c = mk(tmp, buy_valid_days=0)
        m = _FakeMarket(now0)
        pair(m, "AAA")
        card(c, "AAA", now0)
        run(c, m)
        m.err_hourly.add("AAAUSDT")
        m.now = d0 + 9 * H
        code_e, lines_e = run(c, m)
    _check("часовые не нужны (срок лимиток вышел) — их сбой не мешает: код 0, лимитки по сроку",
           code_e == 0 and "R AAA: снял бы лимитки по сроку (4)" in lines_e, failures)

    # --- 1. делистинг: свечей нет и пары нет / не Trading — выход по последней цене
    def gone(setup) -> tuple:
        with tempfile.TemporaryDirectory() as tmp:
            c = mk(tmp, max_coins=1)
            m = _FakeMarket(now0)
            pair(m, "AAA", [(d0, 0.9)])
            pair(m, "BBB")
            card(c, "AAA", now0)
            run(c, m)
            m.now = d0 + D + 6 * H
            run(c, m)
            setup(m)
            m.now = d0 + 2 * D + 6 * H
            card(c, "BBB", m.now - 60)
            code, lines = run(c, m)
            ps = q(c, "SELECT * FROM dry_positions WHERE symbol='AAA' ORDER BY id")
            sells = q(c, "SELECT * FROM dry_orders WHERE pair='AAAUSDT' AND side='Sell'")
            buys = q(c, "SELECT status, note FROM dry_orders WHERE pair='AAAUSDT' AND side='Buy' "
                        "AND kind='Limit'")
            bbb = q(c, "SELECT book, status FROM dry_positions WHERE symbol='BBB'")
        return code, lines, ps, sells, buys, bbb

    def delisted(m) -> None:
        m.err_daily.add("AAAUSDT")
        m.inst["AAAUSDT"] = None
    code_d, lines_d, ps_d, sells_d, buys_d, bbb_d = gone(delisted)
    _check("делистинг (свечей нет, пары нет): обе книги закрыты «делистинг» по последнему "
           "закрытию 0.9, лимитки сняты, код 0",
           code_d == 0 and all(p["status"] == "closed" and p["reason"] == "делистинг"
                               for p in ps_d) and len(sells_d) == 2
           and all(s["signal"] == "delist" and s["price"] == 0.9 and s["status"] == "filled"
                   for s in sells_d)
           and all(abs(p["proceeds_usdt"] - 10.0 * (1 - MARKET_FEE) * 0.9 * (1 - MARKET_FEE))
                   < 1e-9 for p in ps_d)
           and buys_d and all(b["status"] == "cancelled" and b["note"] == "делистинг"
                              for b in buys_d)
           and any(x.startswith("R AAA: делистинг (пары нет на Bybit) — закрыл бы остаток по "
                                "последней цене 0.9") for x in lines_d), failures)
    _check("делистинг освобождает место: при max_coins 1 BBB встала в обе книги",
           sorted((b["book"], b["status"]) for b in bbb_d) == [("H", "open"), ("R", "open")],
           failures)

    def closed_status(m) -> None:
        m.days["AAAUSDT"] = []
        m.inst["AAAUSDT"]["status"] = "Closed"
    code_cl, _, ps_cl, _, _, _ = gone(closed_status)

    def net_down(m) -> None:
        m.err_daily.add("AAAUSDT")
        m.err_inst.add("AAAUSDT")
    code_n, lines_n, ps_n, sells_n, _, _ = gone(net_down)

    def stale(m) -> None:
        m.days["AAAUSDT"] = hist                    # свечи стоят с 30.09, пара торгуется
    code_s, lines_s, ps_s, _, _, _ = gone(stale)
    _check("делистинг: свечей нет и статус Closed — тоже выход «делистинг»",
           code_cl == 0 and all(p["reason"] == "делистинг" for p in ps_cl), failures)
    _check("сбой instruments-info (сеть) ≠ «пары нет»: позиция открыта, код 4, причина в строке",
           code_n == ex.EXIT_NODATA and all(p["status"] == "open" for p in ps_n) and not sells_n
           and any(x.startswith("⚠ нет данных Bybit: AAAUSDT (дневные свечи: retCode 10016: "
                                "Service unavailable; инструмент: нет ответа") for x in lines_n),
           failures)
    _check("пара торгуется, а дневные свечи стоят 2+ дня — «нет данных», не делистинг",
           code_s == ex.EXIT_NODATA and all(p["status"] == "open" for p in ps_s)
           and "⚠ нет данных Bybit: AAAUSDT (дневные свечи стоят с 30.09)" in lines_s, failures)

    with tempfile.TemporaryDirectory() as tmp:          # вход: instruments-info не ответил
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "AAA")
        m.err_inst.add("AAAUSDT")
        card(c, "AAA", now0)
        code_i, lines_i = run(c, m)
        n_i = len(q(c, "SELECT id FROM dry_positions"))
        m.err_inst.clear()
        m.now += H
        code_i2, _ = run(c, m)
        ps_i2 = q(c, "SELECT status FROM dry_positions")
    _check("вход: сбой instruments-info — не «нет на Bybit»: строк нет, код 4; следующий прогон "
           "ставит лестницу",
           code_i == ex.EXIT_NODATA and n_i == 0
           and "⚠ нет данных Bybit: AAAUSDT (инструмент: нет ответа (сеть/HTTP))" in lines_i
           and code_i2 == 0 and [p["status"] for p in ps_i2] == ["open", "open"], failures)

    # --- 2. кулдаун после входа (бэктест: эпизоды монеты ≥ 90 дн. друг от друга)
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "AAA", [(d0, 1.0), (d0 + D, 0.70), (d0 + 2 * D, 0.69)]
             + [(d0 + k * D, 0.68) for k in range(3, 100)], [(d0 + D + 5 * H, 0.69)])
        card(c, "AAA", now0)
        run(c, m)
        m.now = d0 + 3 * D + 6 * H
        run(c, m)                                   # стоп обеим книгам
        m.now = d0 + 4 * D + 6 * H
        m.price["AAAUSDT"] = 0.68
        card(c, "AAA", m.now - 60)
        code_c, lines_c = run(c, m)
        rows_c = q(c, "SELECT book, status, reason FROM dry_positions WHERE card_day=?",
                   (d0 + 4 * D,))
        m.now = now0 + 90 * D + H
        card(c, "AAA", m.now - 60)
        _, lines_c2 = ex.run_dry(c, m, now=m.now, mctx={**cold, "day": ex.utc_day(m.now)})
    until = _time.strftime("%d.%m", _time.gmtime(now0 + 90 * D))
    _check("кулдаун: карточка через 3 дня после стопа — отказ обеим «кулдаун до 30.12»",
           until == "30.12" and code_c == 0 and len(rows_c) == 2
           and all(r["status"] == "rejected" and r["reason"] == "кулдаун до 30.12"
                   for r in rows_c)
           and any(x.startswith("AAA: ОТКАЗ R — кулдаун до 30.12") for x in lines_c), failures)
    _check("кулдаун истёк (90 дн.) — новая лестница в обе книги",
           any(x.startswith("AAA: поставил бы в книги R/H") for x in lines_c2), failures)
    with tempfile.TemporaryDirectory() as tmp:          # кулдаун — по книге
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "AAA")
        seed(c, "AAA", "R", status="closed", created_ts=now0 - 10 * D, card_day=d0 - 10 * D)
        card(c, "AAA", now0)
        _, lines_k = run(c, m)
        ps_k = {p["book"]: p for p in q(c, "SELECT * FROM dry_positions WHERE card_day=?",
                                        (d0,))}
    _check("кулдаун по книге: R входила 10 дн. назад — отказ R, H ставит лестницу",
           ps_k["R"]["status"] == "rejected" and ps_k["R"]["reason"].startswith("кулдаун до")
           and ps_k["H"]["status"] == "open"
           and any(x.startswith("AAA: поставил бы в книги H —") for x in lines_k), failures)
    with tempfile.TemporaryDirectory() as tmp:          # кулдаун тени
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "LOW")
        m.ranks = {"ok": True, "day": ex.utc_day(now0), "n": 8, "note": "",
                   "by_sym": {"LOW": {"rank": 7, "n": 8, "share": 7 / 8, "usd": 1.0}}}
        for b in "RH":
            seed(c, "LOW", b, status="closed", shadow=1, created_ts=now0 - 10 * D,
                 card_day=d0 - 10 * D)
        card(c, "LOW", now0)
        _, lines_sh = run(c, m)
        ps_sh = q(c, "SELECT shadow, status, reason FROM dry_positions WHERE card_day=?", (d0,))
    _check("кулдаун тени: монета была в тени 10 дн. назад — отказ «в тени кулдаун до»",
           len(ps_sh) == 2 and all(p["shadow"] == 1 and p["status"] == "rejected"
                                   and "в тени кулдаун до" in p["reason"] for p in ps_sh),
           failures)

    # --- 3. 15 лестниц на книгу при $1500, 16-я — лимит монет; риск по вложенному
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        m = _FakeMarket(now0)
        for i in range(1, 17):
            pair(m, f"C{i:02d}")
            card(c, f"C{i:02d}", now0 - 100 + i)
        code15, _ = run(c, m)
        ps15 = q(c, "SELECT symbol, book, status, reason FROM dry_positions ORDER BY id")
        con = ex.connect(c["output"]["db_path"])
        expo = sum(x["qty"] for x in ex.book_exposure(con, "R", 50.0))
        con.close()
    opened = {b: [p["symbol"] for p in ps15 if p["book"] == b and p["status"] == "open"]
              for b in "RH"}
    rej16 = [p for p in ps15 if p["symbol"] == "C16"]
    _check("15 лестниц по $50.16 при capital 1500 проходят в обе книги (risk_check по вложенному, "
           "≤ $50 на позицию)",
           code15 == 0 and all(opened[b] == [f"C{i:02d}" for i in range(1, 16)] for b in "RH")
           and abs(expo - 750.0) < 1e-9, failures)
    _check("16-я монета — отказ «лимит 15 монет» обеим книгам",
           len(rej16) == 2 and all(p["status"] == "rejected" and "лимит 15 монет" in p["reason"]
                                   for p in rej16), failures)
    with tempfile.TemporaryDirectory() as tmp:          # рост H не съедает места
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "AAA")
        for i in range(9):
            pair(m, f"X{i}", days=[(d0 - k * D, 1.8) for k in range(40, 0, -1)])
            seed(c, f"X{i}", "H", qty=50.0, bought_qty=50.0, spent_usdt=50.0, last_price=1.8,
                 created_ts=now0 - 30 * D, card_day=d0 - 30 * D)
            seed(c, f"X{i}", "R", status="closed", qty=0.0, bought_qty=50.0, spent_usdt=50.0,
                 proceeds_usdt=75.0, created_ts=now0 - 30 * D, card_day=d0 - 30 * D)
        card(c, "AAA", now0)
        code_j, lines_j = run(c, m)
        ps_j = q(c, "SELECT book, status FROM dry_positions WHERE symbol='AAA'")
        # продано полкупленного, цена выросла: вложено в остаток $25, по рынку $40
        seed(c, "PART", "R", qty=25.0, bought_qty=50.0, spent_usdt=50.0, last_price=1.6)
        con = ex.connect(c["output"]["db_path"])
        pid = con.execute("SELECT id FROM dry_positions WHERE symbol='PART'").fetchone()[0]
        expo_part = next(x["qty"] for x in ex.book_exposure(con, "R", 50.0) if x["id"] == pid)
        con.close()
    _check("9 монет H на +80% (по рынку $810) не блокируют входы: экспозиция по вложенному $450",
           code_j == 0 and sorted((p["book"], p["status"]) for p in ps_j)
           == [("H", "open"), ("R", "open")], failures)
    _check("экспозиция позиции — вложено в остаток (50 × 25/50 = $25), не рынок ($40)",
           abs(expo_part - 25.0) < 1e-9, failures)

    # --- 4. продажи: qty_step, минимум биржи, пыль
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "AAA", [(d0, 1.0), (d0 + D, 1.51)])
        card(c, "AAA", now0)
        run(c, m)
        m.now = d0 + 2 * D + 6 * H
        run(c, m)
        s_b = q(c, "SELECT * FROM dry_orders WHERE side='Sell' AND book='R'")
        p_b = q(c, "SELECT * FROM dry_positions WHERE book='R'")[0]
    _check("тейк +50% при одной ступени: 1/3 = $4.83 < минимума $5 — продан весь остаток 9.9 "
           "(вниз к шагу 0.1), позиция закрыта, хвост < шага",
           len(s_b) == 1 and s_b[0]["qty"] == 9.9 and s_b[0]["usd"] >= 5.0
           and p_b["status"] == "closed" and p_b["reason"] == "фикс +50%" and p_b["qty"] < 0.1,
           failures)
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "DST", days=hist[:-1] + [(d0 - D, 0.5), (d0, 0.5)])
        seed(c, "DST", "R", qty=3.0, bought_qty=3.0, spent_usdt=3.0, base_low=1.0, stop_pct=25,
             created_ts=d0 - 3 * D, card_day=d0 - 3 * D)
        m.now = d0 + D + 6 * H
        code_u, lines_u = run(c, m)
        o_u = q(c, "SELECT * FROM dry_orders WHERE pair='DSTUSDT'")
        p_u = q(c, "SELECT * FROM dry_positions WHERE symbol='DST'")[0]
        m.now += D
        code_u2, _ = run(c, m)
        n_u2 = len(q(c, "SELECT 1 FROM dry_orders WHERE pair='DSTUSDT'"))
        bs_u = ex.brief_state(c, now=d0 + D + 7 * H)
    _check("стоп, а весь остаток $1.50 < минимума: ордер dust «пыль, не продать», позиция "
           "закрыта без выручки, код 0",
           code_u == 0 and len(o_u) == 1 and o_u[0]["status"] == "dust"
           and o_u[0]["signal"] == "invalidation" and "пыль, не продать" in o_u[0]["note"]
           and p_u["status"] == "closed" and p_u["reason"] == "стоп; пыль, не продать"
           and p_u["proceeds_usdt"] == 0 and p_u["qty"] == 3.0, failures)
    _check("пыль: повтор не зацикливается (ордеров не прибавилось), в «продажи» сводки не идёт; "
           "старой позиции шаги взяты из instruments-info",
           code_u2 == 0 and n_u2 == 1 and bs_u["sells"] == []
           and p_u["qty_step"] == 0.1 and p_u["tick"] == 0.01, failures)

    # --- 5. защёлка трейла: докупки вниз не взводят его старым максимумом; hwm — с взвода
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "AAA", [(d0, 1.0), (d0 + D, 1.49), (d0 + 2 * D, 0.85), (d0 + 3 * D, 1.44),
                        (d0 + 4 * D, 1.03)], [(d0 + 2 * D + 10 * H, 0.80)])
        card(c, "AAA", now0)
        run(c, m)
        m.now = d0 + 3 * D + 6 * H
        run(c, m)
        r3 = q(c, "SELECT * FROM dry_positions WHERE book='R'")[0]
        s3 = q(c, "SELECT signal FROM dry_orders WHERE side='Sell'")
        m.now = d0 + 5 * D + 6 * H
        run(c, m)
        r5 = q(c, "SELECT * FROM dry_positions WHERE book='R'")[0]
        s5 = [o["signal"] for o in q(c, "SELECT signal FROM dry_orders WHERE side='Sell' AND "
                                        "book='R' ORDER BY created_ts")]
    _check("пик 1.49 до докупок (+49%), средняя после них ~0.90, закрытие 0.85: трейл не взведён "
           "и не продаёт",
           not s3 and r3["status"] == "open" and r3["trail_armed_ts"] is None
           and r3["spent_usdt"] / r3["bought_qty"] < 0.91 and r3["hwm"] == 1.49, failures)
    _check("закрытие 1.44 ≥ +60% от средней — взвод, hwm сброшен на 1.44; 1.03 (−28% от 1.44, "
           "−31% от старого 1.49) — трейла нет",
           r5["trail_armed_ts"] == d0 + 4 * D and r5["hwm"] == 1.44 and s5 == ["ladder_0"]
           and r5["status"] == "open", failures)

    # --- 6. чужая монета под тикером — fail-closed
    def tk(wl=None, raw: str | None = None, missing: bool = False, bybit_px: float = 1.0):
        with tempfile.TemporaryDirectory() as tmp:
            c = mk(tmp, wl=wl)
            p = _P(c["output"]["watchlist_json"])
            if raw is not None:
                p.write_text(raw, encoding="utf-8")
            if missing:
                p.unlink()
            m = _FakeMarket(now0)
            pair(m, "AAA", price=bybit_px)
            card(c, "AAA", now0, add=False)
            code, lines = run(c, m)
            ps = q(c, "SELECT book, status, reason FROM dry_positions")
            n_o = len(q(c, "SELECT 1 FROM dry_orders"))
        return code, lines, ps, n_o

    def rejected(res, want: str) -> bool:
        code, lines, ps, n_o = res
        return (code == 0 and n_o == 0 and sorted(p["book"] for p in ps) == ["H", "R"]
                and all(p["status"] == "rejected" and p["reason"] == want for p in ps)
                and f"AAA: ОТКАЗ — {want}" in lines)
    a_ok = {"symbol": "AAA", "coin_id": "aaa", "rf_venue": "Bybit spot"}
    _check("тикер: watchlist.json нет — отказ «тикер не сверен: watchlist не прочитан»",
           rejected(tk(missing=True), "тикер не сверен: watchlist не прочитан"), failures)
    _check("тикер: watchlist обрезан при записи (флаг mismatch потерян) — отказ, а не покупка",
           rejected(tk(raw='[{"symbol": "AAA", "coin_id": "a", "flags": ["bybit_ticker_mi'),
                    "тикер не сверен: watchlist не прочитан"), failures)
    _check("тикер: монеты нет в watchlist — отказ «монеты нет в watchlist»",
           rejected(tk(wl=[{"symbol": "BBB"}]), "тикер не сверен: монеты нет в watchlist"),
           failures)
    _check("тикер: две монеты AAA в watchlist — отказ «тикер неоднозначен»",
           rejected(tk(wl=[a_ok, {**a_ok, "coin_id": "aaa-2"}]),
                    "тикер неоднозначен: в watchlist 2 монеты AAA"), failures)
    _check("тикер: bybit_unverified (цену не с чем сравнить) — отказ",
           rejected(tk(wl=[{**a_ok, "flags": ["bybit_unverified"]}]),
                    "тикер не сверен: цену AAA на Bybit не с чем сравнить (bybit_unverified)"),
           failures)
    _check("тикер: площадка DEX only — отказ «не Bybit spot»",
           rejected(tk(wl=[{**a_ok, "rf_venue": "DEX only"}]),
                    "площадка «DEX only» — не Bybit spot"), failures)
    _check("цена Bybit 1.0 против цены скана 10 — отказ «цена Bybit не совпадает с карточкой»",
           rejected(tk(wl=[{**a_ok, "price_usd": 10.0}]),
                    "цена Bybit 1 не совпадает с карточкой (10) — похоже, другая монета"),
           failures)
    ok_res = tk(wl=[{**a_ok, "price_usd": 1.1}])
    _check("цена в пределах 25% (1.0 против 1.1), площадка Bybit spot — лестница в обе книги",
           ok_res[0] == 0 and [p["status"] for p in ok_res[2]] == ["open", "open"], failures)

    # --- 8. идемпотентность: исполнение по rowcount, одна транзакция на пару
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "AAA")
        card(c, "AAA", now0)
        run(c, m)
        con = ex.connect(c["output"]["db_path"])
        o = dict(con.execute("SELECT * FROM dry_orders WHERE link_id='dry-R1-B2'").fetchone())
        f1 = ex._fill_buy(con, dict(o), 0.93, LIMIT_FEE, now0 + H)
        f2 = ex._fill_buy(con, dict(o), 0.93, LIMIT_FEE, now0 + H)   # второй процесс, старый снимок
        con.commit()
        con.close()
        p8 = q(c, "SELECT * FROM dry_positions WHERE id=1")[0]
    _check("_fill_buy: ордер уже исполнен (rowcount 0) — позиция не пополняется второй раз",
           f1 is True and f2 is False
           and abs(p8["spent_usdt"] - (10.0 + o["qty"] * 0.93)) < 1e-9, failures)
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "AAA")
        card(c, "AAA", now0)
        orig = ex._insert_position

        def flaky(con, book, card_, pair_, now_, status, *a, **k):
            if book == "H" and status == "open":
                raise sqlite3.OperationalError("database is locked")
            return orig(con, book, card_, pair_, now_, status, *a, **k)
        ex._insert_position = flaky
        try:
            code_t, lines_t = run(c, m)
        finally:
            ex._insert_position = orig
        n_t = len(q(c, "SELECT 1 FROM dry_positions"))
        m.now += H
        run(c, m)
        ps_t = q(c, "SELECT book, status FROM dry_positions ORDER BY book")
    _check("сбой книги H при постановке — откат всей пары (R не осталась одна), код 1; повтор "
           "ставит обе",
           code_t == 1 and n_t == 0 and "⚠ AAA: OperationalError: database is locked" in lines_t
           and [(p["book"], p["status"]) for p in ps_t] == [("H", "open"), ("R", "open")],
           failures)

    # --- 9. окно карточек — последние 24 ч, не местная полночь; сводка — UTC-день
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        m = _FakeMarket(d0 + 300)
        pair(m, "AAA")
        card(c, "AAA", d0 - 300)                    # 23:55 UTC накануне, скан через полночь
        _, lines_w = run(c, m)
        card(c, "AAA", d0 + 5 * H)
        con = ex.connect(c["output"]["db_path"])
        cards_2 = ex.todays_cards(c, con, d0 + 6 * H)
        cards_late = ex.todays_cards(c, con, d0 + 5 * H + D + 60)
        con.close()
        seed(c, "OLD", "R", created_ts=d0 - 7 * H, card_day=d0 - D)
        seed(c, "NEW", "R", created_ts=d0 + H, card_day=d0)
        bs_w = ex.brief_state(c, now=d0 + 6 * H)
    _check("карточка в 23:55, прогон в 00:05 — лестница стоит (окно 24 ч)",
           any(x.startswith("AAA: поставил бы в книги R/H") for x in lines_w), failures)
    _check("окно: карточки AAA двух UTC-дней — две записи; старше 24 ч — не видны",
           [x["ts"] for x in cards_2] == [d0 - 300, d0 + 5 * H] and cards_late == [], failures)
    _check("сводка дня: «поставил бы» — с начала UTC-дня (вчерашняя 17:00 UTC не попадает)",
           "NEW" in bs_w["opened"] and "OLD" not in bs_w["opened"], failures)

    # --- а/б. тейк лимиткой по HIGH дня; первая продажа снимает лимитки покупки
    with tempfile.TemporaryDirectory() as tmp:
        c = mk(tmp)
        m = _FakeMarket(now0)
        pair(m, "AAA", [(d0, 1.0), (d0 + D, 1.3), (d0 + 2 * D, 2.6)], [(d0 + 8 * H, 0.80)])
        m.hl["AAAUSDT"] = {d0 + D: (1.2, 1.6), d0 + 2 * D: (2.7, 2.8)}
        card(c, "AAA", now0)
        run(c, m)
        m.now = d0 + 3 * D + 6 * H
        _, lines_tp = run(c, m)
        pr = q(c, "SELECT * FROM dry_positions WHERE book='R'")[0]
        s_tp = q(c, "SELECT * FROM dry_orders WHERE side='Sell' AND book='R' ORDER BY created_ts")
        b5 = q(c, "SELECT status, note FROM dry_orders WHERE link_id='dry-R1-B5'")[0]
    avg = pr["spent_usdt"] / pr["bought_qty"]
    t0_ = round_step(avg * 1.5, 0.01, up=True)
    _check("тейк +50%: закрытие 1.3 ниже цели, high 1.6 выше — продажа лимиткой по цели (вверх к "
           "tick), 0.1%",
           [s["signal"] for s in s_tp] == ["ladder_0", "ladder_1"] and t0_ != avg * 1.5
           and s_tp[0]["price"] == t0_ and s_tp[0]["kind"] == "Limit"
           and abs(s_tp[0]["fee_usdt"] - s_tp[0]["usd"] * LIMIT_FEE) < 1e-12
           and any(x.startswith(f"R AAA: продал бы 14.7 лимиткой по {t0_:.6g} (high 1.6")
                   for x in lines_tp), failures)
    _check("тейк +150%: гэп — open 2.7 выше цели — продажа по open",
           s_tp[1]["price"] == 2.7 and s_tp[1]["kind"] == "Limit", failures)
    _check("первая продажа сняла лимитку 0.74 («начали продавать»)",
           b5 == {"status": "cancelled", "note": "начали продавать"}, failures)


def test_ops_guard(cfg, failures: list[str]) -> None:
    print("Защита и эксплуатация (блок D): стоп-кран, ключ Bybit, длинная сводка, report, "
          "сторож прогона, ключи в config.json, daily_run.sh:")
    import copy
    import errno
    import io
    import json as _json
    import os
    import re
    import sqlite3
    import sys
    import tempfile
    import types
    import urllib.parse
    from pathlib import Path as _P
    import run as run_cli
    from scanner import bybit, control, executor as ex, keycheck, runguard
    from scanner.config import Config, load_config as _load
    from scanner.db import Store
    from scanner.notify import deliver, telegram as tg

    root = _P(__file__).resolve().parent.parent
    d0 = 1790812800                                  # 2026-10-01 00:00 UTC
    now = d0 + 6 * 3600 + 1800
    env_saved = {k: os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                                                "TELEGRAM_OWNER_ID", "DUNE_API_KEY")}
    real_post, real_sleep, real_out = tg._post, tg._sleep, sys.stdout
    tg._sleep = lambda s: None
    os.environ.update(TELEGRAM_BOT_TOKEN="T", TELEGRAM_CHAT_ID="42")
    os.environ.pop("TELEGRAM_OWNER_ID", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            T = _P(tmp)
            base = T / "data"
            db = str(T / "t.db")
            d = copy.deepcopy(cfg._d)
            d["output"] = {**d.get("output", {}), "db_path": db,
                           "watchlist_json": str(T / "wl.json")}
            d["api_keys"] = {**d.get("api_keys", {}), "telegram_token": "T",
                             "telegram_chat_id": "42", "telegram_owner_id": ""}
            c = Config(d)
            cpath = T / "cfg.json"
            cpath.write_text(_json.dumps(d, ensure_ascii=False), encoding="utf-8")
            Store(db).close()
            PositionStore(db).close_db()
            ex.connect(db).close()

            # (а) /status: старая база без finished_ts, близнецы B/S, вложено = spent_usdt
            old = str(T / "old.db")
            con = sqlite3.connect(old)
            con.executescript(
                "CREATE TABLE runs (id INTEGER PRIMARY KEY, ts REAL, n_ingested INT, "
                "n_stage1 INT, n_watchlist INT, cfg_version TEXT);"
                "CREATE TABLE positions (id INTEGER PRIMARY KEY, symbol TEXT, status TEXT, "
                "is_paper INT, variant TEXT);"
                "CREATE TABLE dry_positions (id INTEGER PRIMARY KEY, book TEXT, status TEXT, "
                "shadow INT, spent_usdt REAL);")
            con.execute("INSERT INTO runs VALUES (1, ?, 900, 100, 83, 'x')", (now - 3 * 3600,))
            con.executemany("INSERT INTO positions(symbol, status, is_paper, variant) "
                            "VALUES (?,?,?,?)", [("GRAM", "open", 1, "A"), ("GRAM", "open", 1, "B"),
                                                 ("GRAM", "open", 1, "S"), ("LUNC", "open", 0, None)])
            con.executemany("INSERT INTO dry_positions(book, status, shadow, spent_usdt) "
                            "VALUES (?,?,?,?)", [("R", "open", 0, 30.0), ("R", "open", 0, 20.0),
                                                 ("H", "open", 0, 50.0), ("R", "open", 1, 99.0),
                                                 ("R", "closed", 0, 10.0)])
            con.commit()
            con.close()
            st_old = control.status_text(old, base, now)
            st_new = control.status_text(db, base, now)
            _check("/status: база без finished_ts — «завершён 3 ч назад, в наблюдении 83»",
                   "завершён 3 ч назад, в наблюдении 83" in st_old, failures)
            _check("/status: близнецы B/S не считаются (paper 1, реальных 1)",
                   "реальных 1 · 📝 paper 1" in st_old, failures)
            _check("/status: книги по spent_usdt без тени и закрытых (R $50 из 2, H $50)",
                   "пробная книга R: открыто 2, вложено $50.00" in st_old
                   and "пробная книга H: открыто 1, вложено $50.00" in st_old, failures)
            _check("/status: на схеме из кода база читается (колонки совпадают)",
                   "не читается" not in st_new and "не читается" not in st_old, failures)

            # (б) команды: только владелец; в группе — только его from.id
            ups = [{"update_id": 10, "message": {"chat": {"id": 42, "type": "private"},
                                                 "from": {"id": 42},
                                                 "text": "/stop@Scannerb_bot падает биржа"}},
                   {"update_id": 11, "message": {"chat": {"id": 42}, "from": {"id": 999,
                                                 "username": "mallory"}, "text": "/status"}},
                   {"update_id": 12, "message": {"chat": {"id": 7}, "from": {"id": 42},
                                                 "text": "/stop"}},
                   {"update_id": 13, "edited_message": {"chat": {"id": 42}, "from": {"id": 42},
                                                        "text": "просто текст"}}]
            own, foreign, last = control.parse_updates(ups, "42")
            _check("команды: /stop@бот с причиной — от владельца; чужой from.id и чужой чат — "
                   "не команды",
                   own == [{"cmd": "/stop", "arg": "падает биржа", "update_id": 10}]
                   and {(f["chat_id"], f["from_id"]) for f in foreign} == {("42", "999"),
                                                                           ("7", "42")}
                   and last == 13, failures)
            grp = [{"update_id": 1, "message": {"chat": {"id": -100123, "type": "group"},
                                                "from": {"id": 999}, "text": "/stop"}},
                   {"update_id": 2, "message": {"chat": {"id": -100123, "type": "group"},
                                                "from": {"id": 42}, "text": "/status"}}]
            g_own, g_foreign, _ = control.parse_updates(grp, "-100123", "42")
            g_def, _, _ = control.parse_updates(grp, "-100123")
            _check("команды в группе: участник 999 не владелец, владелец 42 — да; без "
                   "TELEGRAM_OWNER_ID в группе не исполняется ничего",
                   [x["cmd"] for x in g_own] == ["/status"] and len(g_foreign) == 1
                   and g_def == [], failures)

            sent, offsets = [], []
            batches = [ups, [{"update_id": 14, "message": {"chat": {"id": 42},
                                                           "from": {"id": 999}, "text": "/stop"}},
                             {"update_id": 15, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                                           "text": "/resume"}},
                             {"update_id": 16, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                                           "text": "/status"}}]]

            def fetch(off):
                offsets.append(off)
                return batches.pop(0) if batches else []
            rc1, _ = control.poll(c, base=base, now=now, fetch=fetch,
                                  send=lambda t: sent.append(t) or True)
            h1 = control.halted(base)
            n1 = len(sent)
            rc2, _ = control.poll(c, base=base, now=now + 300, fetch=fetch,
                                  send=lambda t: sent.append(t) or True)
            _check("/stop владельца ставит стоп-кран с причиной, ответ «⛔»; чужим — одно "
                   "предупреждение на отправителя",
                   rc1 == 0 and h1 and h1["reason"] == "падает биржа" and h1["by"] == "telegram"
                   and n1 == 3 and "⛔" in sent[0]
                   and sum("чужой" in s for s in sent[:n1]) == 2, failures)
            _check("повторный чужой — без нового предупреждения; /resume из Telegram — отказ, "
                   "стоп-кран на месте; /status — «остановлен»; offset = последний + 1",
                   rc2 == 0 and len(sent) == n1 + 2 and "только на сервере" in sent[n1]
                   and "остановлен" in sent[n1 + 1] and control.halted(base) is not None
                   and offsets == [None, 14]
                   and _json.loads((base / control.STATE_NAME).read_text(
                       encoding="utf-8"))["offset"] == 17, failures)
            _check("опрос команд: свежий — тихо; молчит больше часа — предупреждение",
                   control.poll_stale(base, now + 600) == ""
                   and "/stop не сработает" in control.poll_stale(base, now + 2 * 3600), failures)
            (base / control.HALT_NAME).write_text("{битый", encoding="utf-8")
            bad = control.halted(base)
            _check("стоп-кран: нечитаемый файл = стоп", bool(bad) and "не читается" in
                   bad["reason"], failures)
            control.clear_halt(base)
            _check("стоп-кран снят — halted() None", control.halted(base) is None, failures)

            # (в) исполнитель: при стоп-кране ничего не делает, после снятия — работает
            m = _FakeMarket(now)
            m.inst["AAAUSDT"] = {"symbol": "AAAUSDT", "status": "Trading", "st": False,
                                 "tick": 0.01, "qty_step": 0.1, "min_qty": 0.1, "min_amt": 5.0}
            m.price["AAAUSDT"] = 1.0
            m.days["AAAUSDT"] = [(d0 - k * 86400, 1.0) for k in range(40, 0, -1)]
            cold = {"day": d0, "hot": {"lit": [], "near": [], "avail": 8, "n_lit": 0}}
            (T / "wl.json").write_text(_json.dumps([{"symbol": "AAA", "coin_id": "aaa-coin"}]),
                                       encoding="utf-8")
            con = sqlite3.connect(db)
            con.execute("INSERT INTO alert_log(symbol, ts, score) VALUES ('AAA', ?, 72)",
                        (now - 60,))
            con.commit()
            con.close()

            def n_dry() -> int:
                k = sqlite3.connect(db)
                try:
                    return k.execute("SELECT COUNT(*) FROM dry_positions").fetchone()[0]
                finally:
                    k.close()
            control.set_halt("тест", "selftest", base, now)
            code_h, lines_h = ex.run_dry(c, m, now=now, mctx=cold, halt_base=base)
            n_h = n_dry()
            control.clear_halt(base)
            code_r, lines_r = ex.run_dry(c, m, now=now, mctx=cold, halt_base=base)
            _check("исполнитель при стоп-кране: код 0, «⛔ … resume», лестниц нет; снят — "
                   "лестницы есть",
                   code_h == 0 and len(lines_h) == 1 and "⛔" in lines_h[0]
                   and "resume" in lines_h[0] and n_h == 0 and code_r == 0 and n_dry() > 0,
                   failures)

            # (г) ключ Bybit: правила sync и trade
            ok_info = {"readOnly": 1, "permissions": {
                "ContractTrade": ["Order", "Position"], "Spot": ["SpotTrade"],
                "Options": ["OptionsTrade"], "Derivatives": ["DerivativesTrade"], "Wallet": []},
                "ips": ["151.244.251.34"], "deadlineDay": -2, "expiredAt": "1970-01-01T00:00:00Z"}
            r_ok = bybit.check_key(ok_info, "sync")
            r_rw = bybit.check_key({**ok_info, "readOnly": 0}, "sync")
            r_wd = bybit.check_key({**ok_info, "permissions": {
                "Wallet": ["AccountTransfer", "Withdraw"]}}, "sync")
            r_ip = bybit.check_key({**ok_info, "ips": ["*"], "deadlineDay": 10,
                                    "expiredAt": "2026-10-11T00:00:00Z"}, "sync")
            r_30 = bybit.check_key({**ok_info, "ips": [], "deadlineDay": 30}, "sync")
            _check("ключ sync: Read-Only с IP и без вывода — ok, «бессрочный» (deadlineDay −2)",
                   r_ok["level"] == "ok" and r_ok["issues"] == []
                   and "только чтение" in r_ok["facts"] and "бессрочный" in r_ok["facts"],
                   failures)
            _check("ключ sync: торговля или право вывода — danger",
                   r_rw["level"] == "danger" and r_wd["level"] == "danger"
                   and any("ВЫВОДА" in x for x in r_wd["issues"]), failures)
            _check("ключ sync: без IP ('*' или пусто) — warn; срок ≤ 14 дн. — warn с датой, "
                   "30 дн. — без срока",
                   r_ip["level"] == "warn" and any("IP" in x for x in r_ip["issues"])
                   and any("через 10 дн. (2026-10-11)" in x for x in r_ip["issues"])
                   and r_30["level"] == "warn" and not any("истекает" in x
                                                          for x in r_30["issues"]), failures)
            r_star = bybit.check_key({**ok_info, "ips": ["*"]}, "sync")
            _check("ключ: ips ['*'] — не привязка: «без IP» в фактах, warn и без срока",
                   r_star["level"] == "warn" and "без IP" in r_star["facts"]
                   and any("IP" in x for x in r_star["issues"]), failures)
            tr = {"readOnly": 0, "permissions": {"Spot": ["SpotTrade"]}, "ips": ["1.2.3.4"],
                  "deadlineDay": -1}
            _check("ключ trade (демо): только Spot с IP — ok; лишние права, без IP, вывод — "
                   "danger; только чтение — warn",
                   bybit.check_key(tr, "trade")["level"] == "ok"
                   and bybit.check_key({**tr, "permissions": {"Spot": ["SpotTrade"],
                                                              "ContractTrade": ["Order"]}},
                                       "trade")["level"] == "danger"
                   and bybit.check_key({**tr, "ips": []}, "trade")["level"] == "danger"
                   and bybit.check_key({**tr, "permissions": {"Spot": ["SpotTrade"],
                                                              "Wallet": ["Withdraw"]}},
                                       "trade")["level"] == "danger"
                   and bybit.check_key({**tr, "readOnly": 1}, "trade")["level"] == "warn",
                   failures)

            class _Cli:
                def __init__(self, info=None, err=None):
                    self.info, self.err = info, err

                def api_key_info(self):
                    if self.err:
                        raise self.err
                    return self.info
            kc_ok = keycheck.run(c, client=_Cli(ok_info), base=base, now=now)
            _check("самопроверка: итог записан, читается в течение суток, вчерашний — нет",
                   kc_ok["level"] == "ok" and keycheck.load(base, now + 3600)["level"] == "ok"
                   and keycheck.load(base, now + 40 * 3600) is None
                   and keycheck.line(kc_ok).startswith("ключ Bybit: только чтение"), failures)
            kc_err = keycheck.run(c, client=_Cli(err=bybit.BybitError("10003", 10003)),
                                  base=base, now=now)
            c_nokey = Config({**d, "api_keys": {**d["api_keys"], "bybit_key": "",
                                                "bybit_secret": ""}})
            _check("самопроверка: Bybit не ответил — «⚠ … не проверен»; ключа нет — None и "
                   "старый итог удалён",
                   kc_err["level"] == "error"
                   and keycheck.line(kc_err).startswith("⚠ ключ Bybit: не проверен")
                   and keycheck.run(c_nokey, base=base, now=now) is None
                   and keycheck.load(base, now) is None, failures)

            # (д) сторож прогона: оборвался — одно сообщение; дошёл до конца — молчим
            sp = base / runguard.STATE
            sp.write_text(f"start {now - 600:.0f} logs/daily_x.log\nstep quality {now - 600:.0f}"
                          f"\nstep scan {now - 590:.0f}\nstep sca", encoding="utf-8")
            sent = []
            ra1 = runguard.alert(c, base=base, now=now, why="сигнал TERM",
                                 send=lambda t: sent.append(t) or True)
            ra2 = runguard.alert(c, base=base, now=now, send=lambda t: sent.append(t) or True)
            _check("прогон убит на scan: одно сообщение с шагом и логом, повтор — тишина",
                   ra1[0] == 0 and ra2[0] == 0 and len(sent) == 1 and "<b>scan</b>" in sent[0]
                   and "daily_x.log" in sent[0] and "сигнал TERM" in sent[0]
                   and "alerted" in sp.read_text(encoding="utf-8"), failures)
            sp.write_text(f"start {now:.0f} l\nstep brief {now:.0f}\nend {now:.0f} 0\n",
                          encoding="utf-8")
            ra3 = runguard.alert(c, base=base, now=now, send=lambda t: sent.append(t) or True)
            sp.write_text(f"start {now:.0f} l\nstep watch {now:.0f}\n", encoding="utf-8")
            ra4 = runguard.alert(c, base=base, now=now, send=lambda t: False)
            ra5 = runguard.alert(c, base=base, now=now, send=lambda t: sent.append(t) or True)
            _check("сторож: дошёл до «end» — молчит; не отправилось — код 1 и повтор потом",
                   ra3[0] == 0 and len(sent) == 2 and ra4[0] == 1 and ra5[0] == 0
                   and "<b>watch</b>" in sent[1], failures)
            (base / runguard.PREV).write_text(f"start {now:.0f} l\nstep scan {now:.0f}\n"
                                              f"signal TERM {now:.0f}\n", encoding="utf-8")
            prev_cut = runguard.prev_line(base)
            (base / runguard.PREV).write_text(f"start {now:.0f} l\nend {now:.0f} 0\n",
                                              encoding="utf-8")
            _check("сводка: прошлый прогон оборвался — строка с шагом и сигналом; дошёл — пусто",
                   "оборвался на шаге scan, сигнал TERM" in prev_cut
                   and runguard.prev_line(base) == "", failures)

            # (е) длинная сводка: части ≤ лимита, теги закрыты, текст не потерян
            long = ("<b>☀️ шапка</b>\n\n" + "\n".join(
                f"• <b>COIN{i:02d}</b> <i>{'x' * 70}</i> &lt;тег&gt; 🟢" for i in range(60))
                + "\n<b>блок\nв две строки</b>\n\n" + "y" * 5000)
            parts = tg.split_html(long)
            bal = all(p.count(f"<{t}>") == p.count(f"</{t}>") for p in parts for t in ("b", "i"))

            def flat(s: str) -> str:
                return re.sub(r"\s+", "", tg.strip_html(s))
            _check("split_html: каждая часть ≤ лимита, теги закрыты, текст целиком",
                   len(parts) >= 3 and all(tg.vis_len(p) <= tg.SPLIT_AT for p in parts) and bal
                   and "".join(flat(p) for p in parts) == flat(long)
                   and tg.split_html("<b>коротко</b>") == ["<b>коротко</b>"], failures)
            straddle = tg.split_html("<b>" + "\n".join("z" * 90 for _ in range(12)) + "</b>",
                                     limit=400)
            _check("split_html: тег через границу — закрыт в конце части, открыт в следующей",
                   len(straddle) >= 3
                   and all(p.startswith("<b>") and p.endswith("</b>")
                           and p.count("<b>") == p.count("</b>") == 1 for p in straddle),
                   failures)
            posts = []
            tg._post = lambda token, method, data, ctype, timeout=30: (
                posts.append(urllib.parse.parse_qs(data.decode())) or {"ok": True})
            ok_long = tg.send_message("T", "42", long, silent=True,
                                      buttons=[[("Bybit", "https://bybit.com")]])
            _check("send_message: длинное — частями «(часть i/n)», каждая ≤ 4096, кнопки у "
                   "последней",
                   ok_long and len(posts) == len(parts)
                   and all(tg.vis_len(p["text"][0]) <= tg.TEXT_MAX for p in posts)
                   and "(часть 1/" in posts[0]["text"][0]
                   and "reply_markup" in posts[-1] and "reply_markup" not in posts[0], failures)

            # сводка дня в масштабе 10 карточек + 20 позиций (> 4096) уходит целиком
            from scanner.models import Candidate
            day = d0

            def pos_row(i, paper):
                return {"position": {"symbol": f"COIN{i:02d}", "entry_price": 0.012345,
                                     "qty": 1000.0, "base_low": 0.0101, "is_paper": int(paper),
                                     "status": "open"},
                        "last_price": 0.011111, "last_ts": day - 86400, "hwm": 0.013,
                        "pnl": {"pnl_pct": -10.0, "pnl_usdt": -1.23}, "realized_usdt": 0.0,
                        "held_days": 40, "triggered": set(),
                        "spark_prices": [0.01 + j * 1e-4 for j in range(30)]}
            def pick(sym, score):
                return Candidate(source="t", track="A", symbol=sym, zone="ПРУЖИНА/ДНО",
                                 score=score, confidence=0.9)
            big = {"scan": {"ran": True, "ok": True, "elapsed_min": 25.0, "watchlist": 83},
                   "watch_ok": True, "market": {},
                   "muted": [pick(f"MUTE{i:02d}", 72) for i in range(5)],
                   "near": [pick(f"NEAR{i:02d}", 66) for i in range(3)],
                   "new": [pick(f"PICK{i:02d}", 75) for i in range(10)],
                   "positions": [pos_row(i, i >= 20) for i in range(40)],
                   "signals_today": [{"symbol": f"COIN{i:02d}", "label": "фикс 33%",
                                      "is_paper": 0} for i in range(12)],
                   "unavailable": ["onchain"], "dev_github": "62 из 83",
                   "executor": {"books": {"R": {"open": 12, "closed": 3, "pnl": -4.2,
                                                "label": "правила", "emoji": "📏"}},
                                "shadow": {}, "opened": [f"PICK{i:02d}" for i in range(4)],
                                "shadowed": [(f"PICK{i:02d}", "нижняя четверть по обороту "
                                              "(412-е место из 520); перегрев рынка")
                                             for i in range(4, 10)],
                                "rejected": [(f"REJ{i}", "нет на Bybit spot")
                                             for i in range(6)], "fills": 3, "sells": []}}
            brief = tg.format_brief(big, cfg, now=day + 6 * 3600)
            posts.clear()
            ok_brief = tg.send_message("T", "42", brief, silent=True)
            _check(f"сводка дня {tg.vis_len(brief)} симв. (> 4096) уходит частями, все ≤ 4096",
                   tg.vis_len(brief) > tg.TEXT_MAX and ok_brief and len(posts) >= 2
                   and all(tg.vis_len(p["text"][0]) <= tg.TEXT_MAX for p in posts), failures)

            # (ж) диск полон: print в лог бросает ENOSPC — сводка всё равно уходит
            calls = []
            tg._post = lambda token, method, *a, **k: calls.append(method) or {"ok": True}

            class _Full:
                def write(self, s):
                    raise OSError(errno.ENOSPC, "No space left on device")

                def flush(self):
                    pass
            sys.stdout = _Full()
            try:
                rc_b = run_cli.cmd_brief(types.SimpleNamespace(
                    config=str(cpath), notify=True, test=False, scan_exit=0, watch_exit=0,
                    backup_exit=0, sync_exit=0, exec_exit=0, report_exit=0, quality_exit=0))
            except OSError as e:
                rc_b = e
            finally:
                sys.stdout = real_out
            _check("ENOSPC в логе не роняет сводку: код 0, sendMessage был", rc_b == 0
                   and "sendMessage" in calls, failures)

            # (з) report: флаги недели — только после доставки
            ps = PositionStore(db)
            ps.add("GRAM", 1.5, 66.7, paper=True, entry_ts=time.time() - 30 * 86400)
            ps.close_db()

            def report(resp, **kw):
                tg._post = lambda *a, **k: resp
                a = types.SimpleNamespace(config=str(cpath), notify=False, if_due=False,
                                          milestone_weeks=4, monthly=False)
                for k, v in kw.items():
                    setattr(a, k, v)
                sys.stdout = io.StringIO()           # текст сводки в лог не нужен
                try:
                    rc = run_cli.cmd_report(a)
                finally:
                    sys.stdout = real_out
                p = PositionStore(db)
                try:
                    return rc, p.system_flag("weekly_report"), p.system_flag("milestone_4w")
                finally:
                    p.close_db()
            fail_resp = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
            r_view = report({"ok": True})
            r_due = report({"ok": True}, if_due=True)
            r_fail = report(fail_resp, if_due=True, notify=True)
            r_sent = report({"ok": True}, if_due=True, notify=True)
            _check("report: просмотр и --if-due без --notify флагов не ставят; сбой отправки — "
                   "код 1 без флагов; доставлено — флаг недели",
                   r_view == (0, False, False) and r_due == (0, False, False)
                   and r_fail[:2] == (1, False) and r_sent[:2] == (0, True), failures)

            # (и) ключи в config.json не принимаются
            leak = copy.deepcopy(_json.loads((root / "config.json").read_text(encoding="utf-8")))
            leak["api_keys"]["dune"] = "SECRET-123"
            lp = T / "leak.json"
            lp.write_text(_json.dumps(leak, ensure_ascii=False), encoding="utf-8")
            # пустое, а не pop: иначе _load_dotenv вернёт ключ из боевого .env (на сервере он есть)
            dune_env = os.environ.get("DUNE_API_KEY")
            os.environ["DUNE_API_KEY"] = ""
            sys.stderr, real_err = io.StringIO(), sys.stderr
            try:
                cl = _load(str(lp))
                shipped = _load()
            finally:
                sys.stderr = real_err
                if dune_env is None:
                    os.environ.pop("DUNE_API_KEY", None)
                else:
                    os.environ["DUNE_API_KEY"] = dune_env
            _check("config.json: ключ в файле не используется, предупреждение; в репозитории "
                   "ключей нет",
                   cl.get("api_keys.dune") == "" and "dune" in (cl.get("_config_warnings")
                                                                 or [""])[0]
                   and not shipped.get("_config_warnings"), failures)

            # (к) сводка дня: строки защиты под шапкой
            st0 = {"scan": {"ran": True, "ok": True, "elapsed_min": 20.0, "watchlist": 80},
                   "watch_ok": True, "market": {}, "new": [], "muted": [], "near": [],
                   "positions": [], "signals_today": [], "unavailable": [], "dev_github": None}
            gd = {"halt": {"reason": "тест", "ts": now, "by": "telegram"},
                  "key": {"level": "danger", "issues": ["у ключа есть право ВЫВОДА"],
                          "facts": []},
                  "prev_run": "⚠ прошлый прогон (01.10 08:00) оборвался на шаге scan",
                  "poll_stale": "", "timeouts": ["scan"], "report_fail": True,
                  "config_warnings": ["config.json: ключи"], "disk_low": 500 * 1024 ** 2}
            txt = tg.format_brief({**st0, "guard": gd, "backup": "suspect"}, cfg, now=now)
            ln = txt.split("\n")
            plain = tg.format_brief(st0, cfg, now=now)
            okk = tg.format_brief({**st0, "guard": {"key": {"level": "ok", "facts": []}}}, cfg,
                                  now=now)
            _check("сводка: таймаут, сбой report и подозрительный бэкап — в шапке; стоп-кран — "
                   "второй строкой; ключ с выводом — жирно; диск, прошлый прогон, config",
                   "⏱ убит по таймауту: scan" in ln[0] and "недельная сводка не ушла" in ln[0]
                   and "строк меньше, чем в прошлой" in ln[0] and "⛔ исполнитель остановлен" in ln[1]
                   and "resume" in ln[1] and "<b>⛔ ключ Bybit" in txt
                   and "оборвался на шаге scan" in txt and "на диске свободно" in txt
                   and "config.json: ключи" in txt, failures)
            _check("сводка: без проблем — строк защиты нет; ключ ок — «ключ Bybit ✓» в подвале",
                   "⛔" not in plain and "⏱" not in plain and "ключ Bybit" not in plain
                   and "ключ Bybit ✓" in okk.split("\n")[-1], failures)
            control.set_halt("из brief_state", "selftest", base, now)
            bs = deliver.brief_state(c, now=now, data_dir=base, scan_exit=124, backup_exit=137,
                                     report_exit=1)
            control.clear_halt(base)
            gb = bs["guard"]
            _check("brief_state: стоп-кран, таймауты (124/137), сбой report — из data/ и кодов",
                   gb["halt"]["reason"] == "из brief_state" and gb["timeouts"] == ["scan", "backup"]
                   and gb["report_fail"] and not gb["quality_fail"], failures)
    finally:
        tg._post, tg._sleep, sys.stdout = real_post, real_sleep, real_out
        for k, v in env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # (л) daily_run.sh и юниты: замок, таймаут у каждого шага, сторож, «end» после сводки
    sh = (root / "scripts" / "daily_run.sh").read_text(encoding="utf-8")
    unit = (root / "scripts" / "systemd" / "accumulation-scanner.service").read_text(
        encoding="utf-8")
    steps = re.findall(r"^run_step (\w+) (\d+) ", sh, re.M)
    hours = re.search(r"^TimeoutStartSec=(\d+)h", unit, re.M)
    _check("daily_run.sh: 8 шагов, у каждого таймаут; сумма (+60 с KILL) < TimeoutStartSec",
           {n for n, _ in steps} == {"quality", "scan", "sync", "watch", "execute", "report",
                                     "backup", "brief"}
           and "timeout --kill-after=60 \"$limit\" python3" in sh and hours
           and sum(int(s) + 60 for _, s in steps) < int(hours.group(1)) * 3600, failures)
    _check("daily_run.sh: flock до записи состояния, trap TERM → run.py killed, «end» после "
           "сводки; юнит: OnFailure=scanner-alert",
           0 < sh.find("flock -n 9") < sh.find("printf 'start")
           and "trap 'on_signal TERM' TERM" in sh and "run.py killed" in sh
           and sh.find("printf 'end") > sh.find("run_step brief")
           and "--report-exit" in sh and "--quality-exit" in sh
           and "OnFailure=scanner-alert.service" in unit
           and (root / "scripts" / "systemd" / "scanner-alert.service").exists(), failures)
    ctl_t = (root / "scripts" / "systemd" / "scanner-control.timer").read_text(encoding="utf-8")
    ctl_s = (root / "scripts" / "systemd" / "scanner-control.service").read_text(encoding="utf-8")
    _check("таймер команд: раз в 5 минут, run.py control",
           "OnCalendar=*:0/5" in ctl_t and "run.py control" in ctl_s, failures)
    sec = (root / "scripts" / "vps_security_check.sh").read_text(encoding="utf-8")
    writes = re.findall(r"\b(systemctl (?:restart|reload|stop|start|enable|disable)|ufw "
                        r"(?:allow|deny|delete|enable|disable)|sed -i|rm -|chmod|chown|useradd|"
                        r"usermod|(?<!/)passwd |tee )|>\s*/etc", sec)    # /etc/passwd читаем
    _check("vps_security_check.sh — только чтение (нет команд, меняющих систему)",
           writes == [], failures)


def test_monthly(cfg, failures: list[str]) -> None:
    print("Месячная сводка и бенчмарк по денежным потокам (scanner/monthly.py, benchmark.flow_pnl):")
    import copy
    import io
    import sqlite3
    import sys
    import tempfile
    import types
    from datetime import datetime as _d
    from pathlib import Path as _P
    import run as run_cli
    from scanner import benchmark as bm
    from scanner import executor as ex
    from scanner import monthly
    from scanner.config import Config
    from scanner.db import Store
    from scanner.notify import telegram as tg
    from scanner.positions import PositionStore
    D, H, f = bm.DAY, 3600, bm.FEE
    d0 = 1783987200                        # 2026-07-14 00:00 UTC

    # (а) потоки: $10 по 100, $10 по 50, продажа половины по 80, остаток по 100
    lv = {d0: 100.0, d0 + D: 50.0, d0 + 2 * D: 80.0, d0 + 3 * D: 100.0}
    level = lambda ts: lv.get(bm.day_of(ts))  # noqa: E731
    flows = [{"ts": d0 + H, "side": "Buy", "usd": 10.0, "frac": 0.0},
             {"ts": d0 + D + H, "side": "Buy", "usd": 10.0, "frac": 0.0},
             {"ts": d0 + 2 * D + H, "side": "Sell", "usd": 0.0, "frac": 0.5}]
    u = 10 * (1 - f) / 100 + 10 * (1 - f) / 50
    exp = u * 0.5 * 80 * (1 - f) + u * 0.5 * 100 * (1 - f) - 20
    _check("потоки: та же сумма в те же дни, продажа той же долей, комиссия на вход и выход",
           abs(bm.flow_pnl(flows, level, d0 + 3 * D) - exp) < 1e-9, failures)
    sold_all = flows[:1] + [{"ts": d0 + D + H, "side": "Sell", "usd": 0.0, "frac": 1.0}]
    _check("потоки: всё продано — уровень на конец окна не нужен; нет уровня на день потока — None",
           abs(bm.flow_pnl(sold_all, level, d0 + 9 * D)
               - (10 * (1 - f) / 100 * 50 * (1 - f) - 10)) < 1e-9
           and bm.flow_pnl(flows, level, d0 + 9 * D) is None
           and bm.flow_pnl([{**flows[0], "ts": d0 - D}], level, d0 + D) is None, failures)

    # (б) ошибка executor 882: вся сумма в рынок с первого исполнения. Альты 400 → 600 после
    # первой ступени, $10 до роста и $40 после: по потокам рынок дал ~+9.7%, а не +50%.
    mkt = bm.market_points([{"day": d0 + i * D, "total_mcap": t, "btc_dominance": 50,
                             "stables_usd": 100} for i, t in ((0, 1000), (1, 1400), (2, 1400))])
    lad = [{"ts": d0 + H, "side": "Buy", "usd": 10.0, "frac": 0.0},
           {"ts": d0 + D + H, "side": "Buy", "usd": 40.0, "frac": 0.0}]
    o = {"id": 1, "symbol": "AAA", "start": d0 + H, "end": d0 + 2 * D, "cost": 50.0, "pnl": 0.0}
    old = bm.compare_outcomes([o], mkt)
    new = bm.compare_outcomes([{**o, "flows": lad}], mkt)
    exp_alt = (10 * (1 - f) / 400 * 600 * (1 - f) + 40 * (1 - f) ** 2 - 50) / 50 * 100
    _check("лестница: без потоков альты +50% (всё с первой ступени), по потокам ~+9.7%, BTC так же",
           abs(old["alt_pct"] - 50) < 1e-9 and abs(new["alt_pct"] - exp_alt) < 1e-9
           and abs(new["btc_pct"] - (10 * (1 - f) / 500 * 700 * (1 - f) + 40 * (1 - f) ** 2 - 50)
                   / 50 * 100) < 1e-9
           and abs(new["alt_usd"] - exp_alt / 2) < 1e-9 and new["n"] == 1, failures)

    # (в) корзина по потокам: монеты A 100→150→200, B 100→50→100; уровень 1 → 1 → 1.5
    t0, t1, t2 = d0, d0 + D, d0 + 2 * D
    uni = {"runs": [(t0, 1, 5), (t1, 2, 5), (t2, 3, 5)], "wl_runs": [(t0, 1, 5)],
           "watch": {1: {"a": ("A", 100.0), "b": ("B", 100.0)}},
           "caps": {"a": ([t0, t1, t2], [100.0, 150.0, 200.0]),
                    "b": ([t0, t1, t2], [100.0, 50.0, 100.0])}}
    _check("корзина: уровень на входе 1.0, затем 1.0 и 1.5",
           bm.basket_level(uni, t0, t0) == 1.0 and bm.basket_level(uni, t0, t1) == 1.0
           and bm.basket_level(uni, t0, t2) == 1.5, failures)
    row = {"start": t0, "end": t2, "cost": 20.0,
           "flows": [{"ts": t0, "side": "Buy", "usd": 10.0, "frac": 0.0},
                     {"ts": t1, "side": "Buy", "usd": 10.0, "frac": 0.0}]}
    bk = bm.basket_for(uni, [row])
    _check("корзина по потокам: обе ступени ×1.5 с комиссиями; без потоков — окно целиком",
           abs(bk["basket_pct"] - (20 * (1 - f) * 1.5 * (1 - f) - 20) / 20 * 100) < 1e-9
           and abs(bk["basket_usd"] - (20 * (1 - f) * 1.5 * (1 - f) - 20)) < 1e-9
           and abs(bm.basket_for(uni, [{k: v for k, v in row.items() if k != "flows"}])
                   ["basket_pct"] - 50) < 1e-9, failures)

    # (г) исполнения из БД, активность месяца, книги по потокам
    with tempfile.TemporaryDirectory() as tmp:
        d = copy.deepcopy(cfg._d)
        d["output"] = {**d.get("output", {}), "db_path": str(_P(tmp) / "t.db")}
        d["api_keys"] = {**d.get("api_keys", {}), "telegram_token": "T", "telegram_chat_id": "1"}
        c = Config(d)
        db = d["output"]["db_path"]
        st = Store(db)
        st.upsert_market({d0 + k * D: {"total_mcap": 1000 + 200 * k, "btc_dominance": 50,
                                       "stables_usd": 100} for k in range(0, 40)})
        st.close()
        con = ex.connect(db)

        def pos(book, pair, status, created, shadow=0, **kw):
            cols = {"book": book, "pair": pair, "symbol": pair[:-4], "card_day": bm.day_of(created),
                    "status": status, "created_ts": created, "shadow": shadow, **kw}
            cur = con.execute(f"INSERT INTO dry_positions({','.join(cols)}) VALUES "
                              f"({','.join('?' * len(cols))})", list(cols.values()))
            return cur.lastrowid

        def order(pid, book, side, status, qty, usd, fee, ts, kind="Market"):
            con.execute("INSERT INTO dry_orders(link_id, position_id, book, pair, side, kind, "
                        "status, qty, usd, fee_usdt, created_ts, filled_ts) VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f"x{pid}-{side}-{ts}-{status}", pid, book, "AAAUSDT", side, kind,
                         status, qty, usd, fee, ts, ts if status == "filled" else None))
        m0 = d0 + 20 * D                   # «месяц» = [m0, m0 + 10 дн.)
        pr = pos("R", "AAAUSDT", "open", m0 + H, qty=20.0, bought_qty=29.965, spent_usdt=20.0,
                 proceeds_usdt=11.0, last_price=1.2, last_day=m0 + 3 * D, first_fill_ts=m0 + H)
        order(pr, "R", "Buy", "filled", 10.0, 10.0, 0.015, m0 + H)
        order(pr, "R", "Buy", "cancelled", 10.0, 0.0, 0.0, m0 + 2 * H, kind="Limit")
        order(pr, "R", "Buy", "filled", 20.0, 10.0, 0.01, m0 + D, kind="Limit")
        order(pr, "R", "Sell", "filled", 9.965, 11.0, 0.0165, m0 + 2 * D)
        order(pr, "R", "Sell", "dust", 0.5, 0.0, 0.0, m0 + 3 * D)
        ph = pos("H", "AAAUSDT", "closed", m0 + H, qty=0.0, bought_qty=9.985, spent_usdt=10.0,
                 proceeds_usdt=8.0, last_price=0.8, last_day=m0 + 2 * D, first_fill_ts=m0 + H,
                 closed_ts=m0 + 2 * D + H)
        order(ph, "H", "Buy", "filled", 10.0, 10.0, 0.015, m0 + H)
        order(ph, "H", "Sell", "filled", 9.985, 8.0, 0.012, m0 + 2 * D + H)
        pos("R", "BBBUSDT", "open", m0 + 4 * D, shadow=1, shadow_why="bottom")
        pos("R", "CCCUSDT", "rejected", m0 + 5 * D, reason="нет на Bybit spot")
        pos("H", "CCCUSDT", "rejected", m0 + 5 * D, reason="нет на Bybit spot")
        pos("R", "OLDUSDT", "closed", d0 + H, spent_usdt=10.0, first_fill_ts=d0 + H,
            closed_ts=d0 + 2 * D)                   # до месяца: в активность не входит
        con.commit()
        fl = ex.fill_flows(con, pr)
        con.close()
        held = 10 * (1 - 0.0015) + 20 * (1 - 0.001)
        _check("fill_flows: 2 покупки и продажа; доля = qty / монет на счёте (за вычетом "
               "комиссии в монете); отменённая и пыль не в счёт",
               [x["side"] for x in fl] == ["Buy", "Buy", "Sell"]
               and [x["usd"] for x in fl[:2]] == [10.0, 10.0]
               and abs(fl[2]["frac"] - 9.965 / held) < 1e-12, failures)
        act = ex.period_activity(c, m0, m0 + 10 * D)
        _check("активность месяца: R новых 1 (тень отдельно), H закрыто 1, продаж R 1 / H 1, "
               "отказов 1 пара, старт — первое исполнение",
               act["books"]["R"] == {"opened": 1, "shadow_opened": 1, "closed": 0, "sells": 1}
               and act["books"]["H"] == {"opened": 1, "shadow_opened": 0, "closed": 1, "sells": 1}
               and act["rejected"] == 1 and act["first_ts"] == d0 + H, failures)
        wb = {b["key"]: b for b in ex.weekly_books(c)}
        rr = next(r for r in wb["R"]["rows"] if r["id"] == pr)
        exp_r = bm.flow_pnl(rr["flows"], bm._level(bm.load_market(db), 1), rr["end"])
        _check("недельный/месячный блок: книга R против альтов по потокам её исполнений",
               len(rr["flows"]) == 3 and abs(rr["alt_usd"] - exp_r) < 1e-9, failures)

        # (д) когда слать: 1-го числа или позже, если в этом месяце ещё не было
        ts = lambda *a: _d(*a).timestamp()  # noqa: E731
        _check("месячная: первая — только в первые дни месяца; потом — раз в месяц, догоняет",
               monthly.due(None, ts(2026, 11, 1, 8)) and monthly.due(None, ts(2026, 11, 3, 8))
               and not monthly.due(None, ts(2026, 10, 10, 8))
               and monthly.due(ts(2026, 10, 1, 8), ts(2026, 11, 20, 8))
               and not monthly.due(ts(2026, 11, 1, 8, 5), ts(2026, 11, 20, 8)), failures)
        p0, p1, lab = monthly.prev_month(ts(2026, 1, 5, 8))
        _check("месяц сводки — прошлый: январь → «декабрь 2025»",
               lab == "декабрь 2025" and p0 == ts(2025, 12, 1) and p1 == ts(2026, 1, 1), failures)

        # (е) текст
        empty = tg.format_monthly({"month": "сентябрь 2026", "books": [], "shadow": [],
                                   "activity": None, "errors": ["books: KeyError: 'x'"]})
        full = tg.format_monthly({"month": "октябрь 2026", "books": list(wb.values()),
                                  "shadow": ex.weekly_shadow(c), "activity": act, "errors": []})
        _check("текст: нет позиций — так и пишем, сбой блока — строкой ⚠",
               "Месячная сводка</b> · сентябрь 2026" in empty
               and "позиций ещё не было" in empty and "⚠ блок не посчитан: books" in empty,
               failures)
        _check("текст: R/H с $ против альтов, BTC и п.п.; активность месяца; пометка о потоках",
               "📏 правила:" in full and "BTC" in full and "п.п. к альтам" in full
               and "$" in full and "правила — новых 1, закрыто 0, продаж 1" in full
               and "в тень 1" in full and "отказано парам: 1" in full
               and "те же дни" in full and len(full) < 4096, failures)

        # (ж) доставка: флаг monthly_report — только после отправки; --if-due — раз в месяц
        real_load, real_out = run_cli.load_config, sys.stdout
        run_cli.load_config = lambda p=None: c

        def report(resp, **kw):
            tg._post = lambda *a, **k: resp
            a = types.SimpleNamespace(config=None, notify=True, if_due=False, monthly=True,
                                      milestone_weeks=4)
            for k, v in kw.items():
                setattr(a, k, v)
            sys.stdout = io.StringIO()
            try:
                rc = run_cli.cmd_report(a)
            finally:
                sys.stdout = real_out
            p = PositionStore(db)
            try:
                return rc, p.last_event_ts(0, "monthly_report")
            finally:
                p.close_db()
        try:
            r_fail = report({"ok": False, "error_code": 400, "description": "Bad Request"})
            r_ok = report({"ok": True, "result": {"message_id": 1}})
            r_view = report({"ok": True}, notify=False)
            calls: list = []
            tg._post = lambda *a, **k: calls.append(a) or {"ok": True}
            sys.stdout, ps_ = io.StringIO(), PositionStore(db)
            try:
                rc_due = run_cli._report_monthly(
                    types.SimpleNamespace(notify=True, if_due=True, monthly=False), c,
                    ps_, time.time())
            finally:
                sys.stdout = real_out
                ps_.close_db()
            real_due = monthly.due
            monthly.due = lambda *a, **k: True   # «1-е число»: ежедневный report шлёт и её
            try:
                r_daily = report({"ok": True, "result": {"message_id": 2}}, if_due=True,
                                 monthly=False)
            finally:
                monthly.due = real_due
        finally:
            run_cli.load_config = real_load
        cn = sqlite3.connect(db)
        n_monthly = cn.execute("SELECT COUNT(*) FROM position_events WHERE position_id=0 AND "
                               "type='monthly_report'").fetchone()[0]
        cn.close()
        _check("ежедневный report --if-due в день месячной шлёт и её (флаг обновился)",
               r_daily[0] == 0 and n_monthly == 2, failures)
        _check("месячная: сбой отправки — код 1 без флага; доставлено — флаг; просмотр — без "
               "флага; --if-due после отправки в этом месяце — не шлёт",
               r_fail == (1, None) and r_ok[0] == 0 and r_ok[1] is not None
               and r_view == (0, r_ok[1]) and rc_due == 0 and calls == [], failures)


def test_source_health(cfg, failures: list[str]) -> None:
    print("Здоровье источников (scanner/health.py, счётчики HttpClient, source_health):")
    import copy
    import io
    import json as _json
    import sqlite3
    import sys
    import tempfile
    import types
    import urllib.error
    from pathlib import Path as _P
    import run as run_cli
    from scanner import closes, health
    from scanner import http as hmod
    from scanner.config import Config
    from scanner.notify import telegram as tg
    D = 86400

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def err(code):
        return urllib.error.HTTPError("http://x", code, "x", {}, None)

    with tempfile.TemporaryDirectory() as tmp:
        script: list = []

        def fake_open(req, timeout=None):
            x = script.pop(0)
            if isinstance(x, Exception):
                raise x
            return _Resp(_json.dumps(x).encode())
        real_open, real_sleep = hmod.urllib.request.urlopen, hmod.time.sleep
        hmod.urllib.request.urlopen, hmod.time.sleep = fake_open, lambda s: None
        try:
            h = hmod.HttpClient(str(_P(tmp) / "cache"), 3600, 5, {})
            script[:] = [err(429), {"retCode": 10006, "result": {}}]
            r1 = h.get_json("https://api.bybit.com/v5/market/kline", use_cache=False)
            script[:] = [err(404)]
            r2 = h.get_json("https://api.github.com/repos/x/y", retries=3)
            script[:] = [urllib.error.URLError("down")] * 3
            r3 = h.get_json("https://api.llama.fi/x", retries=3)
            script[:] = [{"a": 1}]
            h.get_json("https://stablecoins.llama.fi/s")
            h.get_json("https://stablecoins.llama.fi/s")      # из кэша
        finally:
            hmod.urllib.request.urlopen, hmod.time.sleep = real_open, real_sleep
        by = h.stats
        _check("http: 429 → повтор и ответ с retCode 10006: запрос 1, ok 1, коды 429 и ret10006, "
               "ожидание учтено",
               r1 == {"retCode": 10006, "result": {}}
               and by["api.bybit.com"]["req"] == 1 and by["api.bybit.com"]["ok"] == 1
               and by["api.bybit.com"]["codes"] == {"429": 1, "ret10006": 1}
               and by["api.bybit.com"]["wait_s"] >= 60, failures)
        _check("http: 404 — сбой без повтора; сеть 3 попытки — один сбой, net ×3; кэш отдельно",
               r2 is None and by["api.github.com"]["fail"] == 1
               and by["api.github.com"]["codes"] == {"404": 1}
               and r3 is None and by["api.llama.fi"]["fail"] == 1
               and by["api.llama.fi"]["codes"] == {"net": 3}
               and by["stablecoins.llama.fi"]["req"] == 1
               and by["stablecoins.llama.fi"]["cache"] == 1, failures)

        # отставание закрытий: CoinGecko без точки дня → lag 1 отмечен у клиента
        now = 1783987200 + 10 * D + 6 * 3600
        chart = {"prices": [[(1783987200 + k * D) * 1000, 1.0 + k] for k in range(10)],
                 "total_volumes": [[(1783987200 + k * D) * 1000, 5.0] for k in range(10)],
                 "market_caps": []}
        hc = hmod.HttpClient(str(_P(tmp) / "cache2"), 3600, 5, {})
        hc.get_json = lambda *a, **k: chart
        got = closes.load(hc, {"symbol": "XYO", "coin_id": "xyo"}, now=now)
        _check("closes.load: опоздание закрытия CoinGecko отмечено у клиента (lag 1)",
               got["lag_days"] == 1 and hc.lags == {"CoinGecko": [1]}, failures)

        # запись, повторная запись не задваивает, чтение
        db = str(_P(tmp) / "t.db")
        t0 = 1783987200
        w1 = types.SimpleNamespace(stats={"api.coingecko.com": {
            "req": 100, "ok": 98, "fail": 2, "cache": 5, "codes": {"429": 3}, "wait_s": 180.0}},
            lags={"CoinGecko": [0, 1, 2, 0]})
        w2 = types.SimpleNamespace(stats={"api.bybit.com": {
            "req": 50, "ok": 50, "fail": 0, "cache": 0, "codes": {}, "wait_s": 0.0},
            "pro-api.coingecko.com": {"req": 10, "ok": 10, "fail": 0, "cache": 0, "codes": {},
                                      "wait_s": 0.0}}, lags={"Bybit": [0, 0]})
        n1 = health.record(db, "scan", [w1, w2], now=t0 + 3600)
        n_again = health.record(db, "scan", [w1, w2], now=t0 + 3700)
        health.record(db, "scan", [types.SimpleNamespace(stats={}, lags={"CoinGecko": [0, 0]})],
                      now=t0 + D + 3600)                 # прогон без опозданий
        health.record(db, "watch", [types.SimpleNamespace(
            stats={"api.coingecko.com": {"req": 10, "ok": 10, "fail": 0, "cache": 0,
                                         "codes": {}, "wait_s": 0.0}},
            lags={"CoinGecko": [0]})], now=t0 + 8 * D)
        rows = health.load(db, t0, t0 + 14 * D)
        _check("record: строки http по хосту и lag по источнику; повтор того же процесса — 0 "
               "строк; load читает, codes — словарь",
               n1 == 5 and n_again == 0 and len(rows) == 8
               and any(r["kind"] == "lag" and r["source"] == "CoinGecko" and r["req"] == 4
                       and r["fail"] == 2 and r["lag_max"] == 2 for r in rows)
               and any(r["codes"] == {"429": 3} for r in rows), failures)
        sqlite3.connect(str(_P(tmp) / "empty.db")).close()
        _check("load: нет файла или таблицы — None; таблица есть, строк нет — []",
               health.load(str(_P(tmp) / "none.db"), 0, 1) is None
               and health.load(str(_P(tmp) / "empty.db"), 0, 1) is None
               and health.load(db, 0, 1) == [], failures)

        s = health.summarize(rows, t0, t0 + 14 * D, 7)
        cg = next(x for x in s["http"] if x["source"] == "CoinGecko")
        lag_scan = next(g for g in s["lag"] if g["source"] == "CoinGecko" and g["step"] == "scan")
        _check("summarize: хосты CoinGecko сведены, сбоивший — первым; тренд по неделям; "
               "опоздание по шагу",
               s["http"][0]["source"] == "CoinGecko" and cg["req"] == 120 and cg["fail"] == 2
               and [b["fail"] for b in cg["buckets"]] == [2, 0]
               and [b["req"] for b in cg["buckets"]] == [110, 10] and s["buckets"] == 2
               and lag_scan["late"] == 2 and lag_scan["n"] == 6 and lag_scan["late_runs"] == 1
               and lag_scan["runs"] == 2, failures)
        lines = tg.source_health_lines(s, "за месяц")
        txt = "\n".join(lines)
        _check("текст: сбоивший источник с кодами, ожиданием и неделями; остальные «без сбоев»; "
               "опоздание CoinGecko; Bybit без опозданий не показан",
               "CoinGecko: сбоев 2 из 120 (2%) · 429 ×3 · ждали 3 мин · нед.: 2/110 · 0/10" in txt
               and "без сбоев: Bybit 50 запр." in txt
               and "CoinGecko (графики скана): закрытие дня не вышло к прогону у 2 из 6 (33%), "
                   "в 1 из 2 прогонов, макс. 2 дн. · нед.: 33% · —" in txt
               and "Bybit (" not in txt and tg.source_health_lines(None, "x") == []
               and "без сбоев" not in " ".join(tg.source_health_lines(
                   {"http": [{"source": "F&G", "req": 0, "ok": 0, "fail": 0, "cache": 3,
                              "codes": {}, "wait_s": 0.0, "buckets": []}], "lag": [],
                    "buckets": 0}, "x"))
               and "🩺 <b>Источники</b>" in tg.format_monthly(
                   {"month": "x", "books": [], "shadow": [], "activity": None, "errors": [],
                    "health": s})
               and tg.source_health_lines(health.summarize([], 0, 1, None), "x") == [],
               failures)

        # хук run.py: шаг пишет счётчики своих клиентов; selftest — нет
        d = copy.deepcopy(cfg._d)
        d["output"] = {**d.get("output", {}), "db_path": str(_P(tmp) / "hook.db")}
        c = Config(d)
        real_load, real_clients, real_out = run_cli.load_config, hmod.CLIENTS, sys.stdout
        run_cli.load_config = lambda p=None: c
        try:
            hmod.CLIENTS = type(real_clients)()      # пустой контейнер того же типа

            def step():                    # клиент шага — локальный, как в cmd_*
                hx = hmod.HttpClient(str(_P(tmp) / "cache3"), 3600, 5, {})
                hx.stats = {"api.bybit.com": {"req": 3, "ok": 2, "fail": 1, "cache": 0,
                                              "codes": {"503": 5}, "wait_s": 30.0}}
            step()
            sys.stdout = io.StringIO()
            run_cli._record_health(types.SimpleNamespace(cmd="selftest", config=None))
            run_cli._record_health(types.SimpleNamespace(cmd="execute", config=None))
        finally:
            run_cli.load_config, hmod.CLIENTS, sys.stdout = real_load, real_clients, real_out
        cn = sqlite3.connect(str(_P(tmp) / "hook.db"))
        try:
            got = cn.execute("SELECT step, source, req, fail, codes FROM source_health").fetchall()
        except sqlite3.OperationalError:          # таблицы нет — шаг ничего не записал
            got = None
        finally:
            cn.close()
        _check("run.py: после шага счётчики в source_health (шаг execute); selftest не пишет",
               got == [("execute", "api.bybit.com", 3, 1, '{"503": 5}')], failures)


def test_checklist(cfg, failures: list[str]) -> None:
    print("Чек-лист решений (docs/DECISIONS.md, scanner/checklist.py):")
    import tempfile
    from datetime import datetime as _d
    from pathlib import Path as _P
    from scanner import checklist as ck
    from scanner.notify import telegram as tg
    doc = """# Решения

| ID | Что | Сейчас | Когда | Как проверить |
|----|-----|--------|-------|---------------|
| L1 | Прогон <b>10.10</b> в `source_health` **важно** | не видели | 2026-10-10 | лог |
| P1 | Порог `70` | 70 | после 20 закрытых | отчёт |
| ✅ L2 | Сделано давно | — | 2026-09-01 | — |
| ~~L3~~ | Зачёркнуто | — | 2026-09-02 | — |
| Q1 | Квартальный перезамер | — | 2026-11-20, затем раз в квартал | скрипт |
| Q2 | Через год | — | 2027-10-01 | — |

| Порог | Значение |
|-------|----------|
| 2026-10-05 | без столбцов ID/Что/Когда — не читается |
"""
    items = ck.parse(doc)
    by = {i["id"]: i for i in items}
    _check("parse: строки таблицы с ID/Что/Когда, ✅ и ~~…~~ — закрыты, без даты — due None, "
           "чужие таблицы пропущены",
           sorted(by) == ["L1", "L2", "L3", "P1", "Q1", "Q2"]
           and by["L2"]["done"] and by["L3"]["done"] and not by["L1"]["done"]
           and by["P1"]["due"] is None
           and by["Q1"]["due"] == _d(2026, 11, 20).timestamp(), failures)
    now = _d(2026, 11, 1, 8).timestamp()
    late, soon = ck.due_items(items, now)
    _check("due_items: просрочено L1, в ближайший месяц Q1; закрытые, без даты и далёкие — нет",
           [i["id"] for i in late] == ["L1"] and [i["id"] for i in soon] == ["Q1"], failures)
    with tempfile.TemporaryDirectory() as tmp:
        p = _P(tmp) / "DECISIONS.md"
        p.write_text(doc, encoding="utf-8")
        lines = ck.reminder(now, p)
        none = ck.reminder(_d(2026, 9, 1).timestamp(), _P(tmp) / "nope.md")
        q = _P(tmp) / "q.md"
        q.write_text("| ID | Что | Когда |\n|--|--|--|\n| A | x | 2027-01-01 |\n",
                     encoding="utf-8")
        quiet = ck.reminder(_d(2026, 9, 1).timestamp(), q)
    txt = "\n".join(lines)
    _check("reminder: просроченное и ближайшее с датой, HTML экранирован, без Markdown; "
           "нет файла — ⚠; нечего напоминать — пусто",
           "⏰ просрочено 10.10 · L1: Прогон &lt;b&gt;10.10&lt;/b&gt; в source_health важно" in txt
           and "🗓 в этом месяце 20.11 · Q1" in txt and "P1" not in txt
           and none and none[0].startswith("⚠") and quiet == [], failures)
    _check("месячная сводка: блок чек-листа выводится",
           "📋 <b>Чек-лист решений</b>" in tg.format_monthly(
               {"month": "x", "books": [], "shadow": [], "activity": None, "errors": [],
                "checklist": lines}), failures)
    import copy
    from scanner import monthly
    from scanner.config import Config
    with tempfile.TemporaryDirectory() as tmp:
        d = copy.deepcopy(cfg._d)
        d["output"] = {**d.get("output", {}), "db_path": str(_P(tmp) / "none.db")}
        st = monthly.collect(Config(d), _d(2026, 11, 1, 8).timestamp())
    _check("monthly.collect: напоминание чек-листа из docs/DECISIONS.md на 01.11 есть",
           (st["checklist"] or [""])[0].startswith("📋") and not st["errors"], failures)
    real = ck.parse(ck.DOC.read_text(encoding="utf-8"))
    _check("docs/DECISIONS.md: читается, есть открытые строки со сроком, ID уникальны",
           any(not i["done"] and i["due"] for i in real)
           and len({i["id"] for i in real}) == len(real), failures)


def main() -> int:
    cfg = load_config()
    failures: list[str] = []
    print("=== SELFTEST (офлайн, без сети) ===\n")
    # Стоп-кран, состояние прогона, итог проверки ключа — во временном каталоге: боевые
    # data/HALT и data/bybit_key.json на сервере не должны влиять на тесты (и наоборот).
    import tempfile
    from pathlib import Path as _P
    from scanner import control
    data_tmp = tempfile.TemporaryDirectory(prefix="selftest-data-")
    control.DATA = _P(data_tmp.name)
    test_filters(cfg, failures)
    print()
    test_track_q(cfg, failures)
    print()
    test_quality_refresh(cfg, failures)
    print()
    test_backup(cfg, failures)
    print()
    test_backup_guard(cfg, failures)
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
    test_watch_data(cfg, failures)
    print()
    test_benchmark(cfg, failures)
    print()
    test_ladder(cfg, failures)
    print()
    test_executor(cfg, failures)
    print()
    test_liquidity(cfg, failures)
    print()
    test_executor_filters(cfg, failures)
    print()
    test_executor_fixes(cfg, failures)
    print()
    test_github_levels(cfg, failures)
    print()
    test_ops_guard(cfg, failures)
    print()
    test_monthly(cfg, failures)
    print()
    test_source_health(cfg, failures)
    print()
    test_checklist(cfg, failures)
    print()
    data_tmp.cleanup()
    if failures:
        print(f"РЕЗУЛЬТАТ: {_FAIL} — провалено {len(failures)}: {failures}")
        return 1
    print(f"РЕЗУЛЬТАТ: {_PASS} — все проверки пройдены")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
