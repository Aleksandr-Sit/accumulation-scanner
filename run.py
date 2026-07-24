#!/usr/bin/env python3
"""CLI автосканера: воронка входа + контур позиций (hodl-профиль).

  python run.py scan --track all            # полный прогон воронки (сеть)
  python run.py pos add ARB --price 0.5 --qty 1000    # записать покупку
  python run.py pos list                    # открытые позиции
  python run.py pos close ARB --price 1.2   # закрыть (P&L в журнал)
  python run.py watch [--notify]            # ре-скан позиций -> exit-сигналы
  python run.py selftest                    # офлайн-проверка логики на фикстурах
"""
from __future__ import annotations

import argparse
import sys

from scanner.config import load_config
from scanner.pipeline import run_scan, run_watch


def cmd_scan(args) -> int:
    cfg = load_config(args.config)
    if args.pages is not None:
        cfg["universe"]["track_a_pages"] = args.pages
    print(f"[scan] track={args.track} limit={args.limit} pages={cfg['universe']['track_a_pages']} — старт")
    summary = run_scan(cfg, track=args.track, limit=args.limit)
    print("\n=== ИТОГ ПРОГОНА ===")
    for k, v in summary.items():
        print(f"  {k:22}: {v}")
    print(f"\nWatchlist → {cfg['output']['watchlist_json']}")
    print(f"База      → {cfg['output']['db_path']}")

    if args.notify:
        import json as _json
        from scanner.notify import telegram
        from scanner.models import Candidate
        rows = _json.loads(open(cfg["output"]["watchlist_json"], encoding="utf-8").read())
        cands = [Candidate(source="wl", track=r.get("track", ""), symbol=r.get("symbol", ""),
                           name=r.get("name", ""), score=r.get("score", 0.0),
                           confidence=r.get("confidence", 0.0), zone=r.get("zone", ""),
                           rf_venue=r.get("rf_venue", ""), category=r.get("category", ""),
                           drawdown_from_ath_pct=r.get("drawdown_from_ath_pct"),
                           manual_review=r.get("manual_review", False),
                           flags=r.get("flags", []),
                           liveness_score=r.get("liveness_score"),
                           dev_commits_4w=r.get("dev_commits_4w")) for r in rows]
        # mute: не повторять алерт по монете N дней, если балл не вырос заметно
        from scanner.db import Store
        st = Store(cfg["output"]["db_path"])
        mute_days = cfg.get("stage6_telegram.alert_mute_days", 3)
        recent = st.recent_alerts(mute_days)
        fresh = [c for c in cands
                 if c.symbol not in recent or c.score > recent[c.symbol] + 5]
        muted = len(cands) - len(fresh)
        if muted:
            print(f"[telegram] mute: {muted} повторных кандидатов пропущено")
        if telegram.notify(fresh, cfg):
            min_s = cfg.get("stage6_telegram.min_score", 70)
            for c in fresh:
                if c.score >= min_s:
                    st.record_alert(c.symbol, c.score)
        st.close()
    return 0


def cmd_chatid(args) -> int:
    cfg = load_config(args.config)
    from scanner.notify import telegram
    token = cfg.get("api_keys.telegram_token", "")
    if not token:
        print("Нет TELEGRAM_BOT_TOKEN в .env")
        return 1
    chats = telegram.get_chat_ids(token)
    if not chats:
        print("Никто не писал боту. Открой бота в Telegram, нажми Start, повтори команду.")
        return 1
    for ch in chats:
        print(f"chat_id={ch.get('id')}  {ch.get('type')}  {ch.get('first_name','')} @{ch.get('username','')}")
    print("\nВпиши нужный chat_id в .env → TELEGRAM_CHAT_ID=")
    return 0


def cmd_tgtest(args) -> int:
    cfg = load_config(args.config)
    from scanner.notify import telegram
    ok = telegram.send_message(cfg.get("api_keys.telegram_token", ""),
                               cfg.get("api_keys.telegram_chat_id", ""),
                               "✅ <b>Scanner</b>: тестовый алерт. Связь есть.")
    print("Отправлено." if ok else "Не отправлено (проверь token/chat_id).")
    return 0 if ok else 1


def _lookup_coin(cfg, symbol: str) -> dict:
    """Ищет coin_id/chain/address по тикеру в последнем watchlist.json, затем в БД."""
    import json as _json
    import sqlite3
    from pathlib import Path
    sym = symbol.upper()
    wl = Path(cfg["output"]["watchlist_json"])
    if wl.exists():
        try:
            for r in _json.loads(wl.read_text(encoding="utf-8")):
                if (r.get("symbol") or "").upper() == sym:
                    return {"chain": r.get("chain", ""), "address": r.get("address", ""),
                            "venue": r.get("rf_venue", ""), "coin_id": r.get("coin_id", "")}
        except ValueError:
            pass
    try:
        conn = sqlite3.connect(cfg["output"]["db_path"])
        row = conn.execute(
            "SELECT coin_id, chain, address, rf_venue FROM candidates "
            "WHERE symbol=? AND coin_id != '' ORDER BY run_id DESC LIMIT 1", (sym,)).fetchone()
        conn.close()
        if row:
            return {"coin_id": row[0], "chain": row[1], "address": row[2],
                    "venue": row[3] or ""}
    except sqlite3.Error:
        pass
    return {}


def cmd_pos(args) -> int:
    cfg = load_config(args.config)
    from scanner.positions import PositionStore, risk_check
    pstore = PositionStore(cfg["output"]["db_path"])

    if args.action == "add":
        if args.price is None or args.qty is None:
            print("Нужны --price и --qty"); return 1
        info = _lookup_coin(cfg, args.symbol)
        coin_id = args.coin_id or info.get("coin_id", "")
        if not coin_id:
            print(f"⚠ coin_id для {args.symbol} не найден в watchlist/БД — "
                  f"watch не сможет достать цену. Задай: --coin-id <id CoinGecko>")
        prices_map = {s["position_id"]: s["price"]
                      for s in pstore.last_snapshots().values()}
        warns = risk_check(pstore.open_positions(), args.price * args.qty, cfg, prices_map)
        for w in warns:
            print(f"⚠ РИСК: {w}")
        base_low = args.base_low
        if base_low is None and coin_id:
            # лоу базы входа — минимум последних 30 ЗАКРЫТЫХ дневных цен (для инвалидации)
            from scanner.pipeline import _make_http
            from scanner.sources.coingecko import fetch_market_chart, closed_daily
            from scanner.stages.exit import compute_base_low
            chart = closed_daily(fetch_market_chart(_make_http(cfg), coin_id, 60,
                                 cfg.get("api_keys.coingecko_demo", "")))
            base_low = compute_base_low(chart["prices"])
            if base_low is None:
                print("⚠ история цены недоступна (429?) — base_low проставится при watch")
        pid = pstore.add(args.symbol, args.price, args.qty, coin_id=coin_id,
                         chain=info.get("chain", ""), address=info.get("address", ""),
                         venue=info.get("venue", ""), base_low=base_low,
                         notes=args.notes or "")
        bl = f"{base_low:.6g}" if base_low else "—"
        print(f"Позиция #{pid}: {args.symbol.upper()} qty={args.qty} @ {args.price} "
              f"(base_low={bl}, coin_id={coin_id or '—'})")
        pstore.close_db(); return 0

    if args.action == "list":
        rows = pstore.open_positions() if not args.all else pstore.all_positions()
        if not rows:
            print("Позиций нет."); pstore.close_db(); return 0
        for p in rows:
            import time as _t
            days = int((_t.time() - p["entry_ts"]) / 86400)
            bl = f"{p['base_low']:.6g}" if p["base_low"] else "—"
            tag = "📝paper" if p.get("is_paper") else "💰real "
            rz = p.get("realized_usdt") or 0.0
            rzs = f" realized={rz:+.2f}" if abs(rz) > 1e-9 else ""
            print(f"#{p['id']:<3} {p['symbol']:<8} {tag} {p['status']:<7} qty={p['qty']:<12g} "
                  f"@ {p['entry_price']:<10g} {days}д  base_low={bl} hwm={p['hwm']:.6g}{rzs}")
        pstore.close_db(); return 0

    if args.action == "close":
        if args.price is None:
            print("Нужен --price (цена фактической продажи)"); return 1
        pos = pstore.find_open(args.symbol)
        if not pos:
            print(f"Открытая позиция {args.symbol} не найдена"); pstore.close_db(); return 1
        realized = pstore.close(pos["id"], args.price, args.notes or "")
        print(f"Закрыто #{pos['id']} {pos['symbol']}: realized {realized:+.2f} USDT "
              f"(с учётом комиссий)")
        pstore.close_db(); return 0

    if args.action == "reduce":
        if args.price is None or args.qty is None:
            print("Нужны --qty (сколько продать) и --price"); return 1
        pos = pstore.find_open(args.symbol)
        if not pos:
            print(f"Открытая позиция {args.symbol} не найдена"); pstore.close_db(); return 1
        if args.qty > pos["qty"]:
            print(f"⚠ продаётся {args.qty} > остатка {pos['qty']:g} — будет закрыта полностью")
        realized = pstore.sell(pos["id"], args.qty, args.price, "manual_reduce",
                               args.notes or "")
        left = pstore.get(pos["id"])
        st = "закрыта" if left["status"] == "closed" else f"остаток {left['qty']:g}"
        print(f"Частичная фиксация #{pos['id']} {pos['symbol']}: realized {realized:+.2f} USDT, {st}")
        pstore.close_db(); return 0

    print(f"Неизвестное действие: {args.action}")
    return 1


def cmd_watch(args) -> int:
    cfg = load_config(args.config)
    out = run_watch(cfg)
    rows, summary = out["rows"], out["summary"]
    if not rows:
        print("Открытых позиций нет — нечего отслеживать (run.py pos add ...).")
        return 0
    print(f"=== WATCH: {summary['positions']} позиций, "
          f"{summary['signals']} новых сигналов ===\n")
    for r in rows:
        p = r["position"]
        if r.get("error"):
            print(f"  {p['symbol']:<8} ⚠ {r['error']}")
            continue
        pnl = r["pnl"]
        tag = "📝" if p.get("is_paper") else "💰"
        rz = r.get("realized_usdt") or 0.0
        rzs = f", realized {rz:+.2f}" if abs(rz) > 1e-9 else ""
        print(f"  {tag} {p['symbol']:<8} {pnl['pnl_pct']:+7.1f}%  "
              f"({pnl['pnl_usdt']:+.2f} USDT{rzs}, {r['held_days']}д, "
              f"закрытие {r['last_price']:.6g}, hwm {r['hwm']:.6g})")
        for s in r["signals"]:
            print(f"           🔔 [{s['type']}] {s['action']} — {s['note']}")
        for ex in r.get("executed", []):
            print(f"           ⚙ [paper] {ex}")
    print(f"\n  Итого нереализованный P&L: {summary['pnl_total_usdt']:+.2f} USDT")

    if args.notify:
        from scanner.notify import telegram
        token = cfg.get("api_keys.telegram_token", "")
        chat = cfg.get("api_keys.telegram_chat_id", "")
        text = telegram.format_exit_alert(rows, cfg)
        if text:
            ok = telegram.send_message(token, chat, text)
            print(f"[telegram] exit-алерт: {'ok' if ok else 'fail'}")
        else:
            print("[telegram] новых сигналов нет — алерт не отправлен")
        if cfg.get("stage6_telegram.daily_digest", True):
            dig = telegram.format_digest(rows, cfg)
            if dig:
                ok = telegram.send_message(token, chat, dig)
                print(f"[telegram] дайджест: {'ok' if ok else 'fail'}")
    return 0


def cmd_report(args) -> int:
    """Недельная сводка форвард-теста. Без сети — из снапшотов/журнала."""
    import time as _t
    cfg = load_config(args.config)
    from scanner.positions import PositionStore
    from scanner.notify import telegram
    pstore = PositionStore(cfg["output"]["db_path"])

    now = _t.time()
    week_ago = now - 7 * 86400
    events = pstore.events_since(week_ago)
    opens = pstore.open_positions()
    snaps = pstore.last_snapshots()

    def _pnl(pos_list):
        total = 0.0
        for p in pos_list:
            s = snaps.get(p["id"])
            if s and p["entry_price"] > 0:
                total += (s["price"] - p["entry_price"]) * p["qty"]
        return round(total, 2)

    paper_open = [p for p in opens if p.get("is_paper")]
    real_open = [p for p in opens if not p.get("is_paper")]

    # Realized по закрытым paper-позициям (paper-executor фиксирует по сигналам).
    all_pos = pstore.all_positions()
    paper_realized = round(sum(p.get("realized_usdt") or 0.0
                               for p in all_pos if p.get("is_paper")), 2)
    real_realized = round(sum(p.get("realized_usdt") or 0.0
                              for p in all_pos if not p.get("is_paper")), 2)
    paper_closed = sum(1 for p in all_pos
                       if p.get("is_paper") and p["status"] == "closed")

    # Неделя наблюдения от первой paper-позиции + одноразовый 4-недельный милстоун.
    first_ts = pstore.first_paper_ts()
    week_no = int((now - first_ts) // (7 * 86400)) + 1 if first_ts else 0
    milestone = (week_no >= args.milestone_weeks
                 and not pstore.system_flag("milestone_4w"))
    all_events = pstore.events_since(0) if milestone else events

    stats = {
        "week_no": week_no,
        "milestone": milestone,
        "milestone_weeks": args.milestone_weeks,
        "opened": sum(1 for e in events if e["type"] == "paper_open"),
        "open_now": len(paper_open),
        "invalidations": sum(1 for e in events if e["type"] == "invalidation"),
        "ladder_hits": sum(1 for e in events if e["type"].startswith("ladder_")),
        "trailings": sum(1 for e in events if e["type"] == "trailing"),
        "paper_pnl_usdt": _pnl(paper_open),
        "paper_realized_usdt": paper_realized,
        "paper_closed": paper_closed,
        "real_open": len(real_open),
        "real_pnl_usdt": _pnl(real_open),
        "real_realized_usdt": real_realized,
        # кумулятив за всё наблюдение (для милстоуна)
        "cum_opened": sum(1 for e in all_events if e["type"] == "paper_open"),
        "cum_invalidations": sum(1 for e in all_events if e["type"] == "invalidation"),
        "cum_ladder_hits": sum(1 for e in all_events if e["type"].startswith("ladder_")),
        "cum_trailings": sum(1 for e in all_events if e["type"] == "trailing"),
    }
    if milestone:
        pstore.set_system_flag("milestone_4w", f"week={week_no}")
    pstore.close_db()

    text = telegram.format_weekly(stats, cfg)
    print(text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    if args.notify:
        ok = telegram.send_message(cfg.get("api_keys.telegram_token", ""),
                                   cfg.get("api_keys.telegram_chat_id", ""), text)
        print(f"[telegram] недельная сводка: {'ok' if ok else 'fail'}")
    return 0


def cmd_selftest(args) -> int:
    from tests.selftest import main as selftest_main
    return selftest_main()


def main() -> int:
    p = argparse.ArgumentParser(description="Accumulation scanner (MVP Stage 0-2)")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("scan", help="прогон воронки")
    ps.add_argument("--track", choices=["A", "B", "all"], default="all")
    ps.add_argument("--limit", type=int, default=None)
    ps.add_argument("--pages", type=int, default=None,
                    help="override track_a_pages (250 монет/страница)")
    ps.add_argument("--notify", action="store_true", help="отправить алерт в Telegram")
    ps.add_argument("--config", default=None)
    ps.set_defaults(func=cmd_scan)

    pp = sub.add_parser("pos", help="позиции: add | list | close | reduce")
    pp.add_argument("action", choices=["add", "list", "close", "reduce"])
    pp.add_argument("symbol", nargs="?", default="")
    pp.add_argument("--price", type=float, default=None)
    pp.add_argument("--qty", type=float, default=None)
    pp.add_argument("--coin-id", default="", help="id CoinGecko (если не найден автоматически)")
    pp.add_argument("--base-low", type=float, default=None,
                    help="лоу базы входа для инвалидации (иначе — из истории цены)")
    pp.add_argument("--notes", default="")
    pp.add_argument("--all", action="store_true", help="list: включая закрытые")
    pp.add_argument("--config", default=None)
    pp.set_defaults(func=cmd_pos)

    pw = sub.add_parser("watch", help="ре-скан открытых позиций -> exit-сигналы")
    pw.add_argument("--notify", action="store_true", help="отправить exit-алерт в Telegram")
    pw.add_argument("--config", default=None)
    pw.set_defaults(func=cmd_watch)

    pr = sub.add_parser("report", help="недельная сводка форвард-теста (без сети)")
    pr.add_argument("--notify", action="store_true", help="отправить в Telegram")
    pr.add_argument("--milestone-weeks", type=int, default=4,
                    help="на какой неделе выдать итоговую сводку (одноразово)")
    pr.add_argument("--config", default=None)
    pr.set_defaults(func=cmd_report)

    pt = sub.add_parser("selftest", help="офлайн-проверка на фикстурах")
    pt.set_defaults(func=cmd_selftest)

    pc = sub.add_parser("chat-id", help="показать chat_id из истории бота")
    pc.add_argument("--config", default=None)
    pc.set_defaults(func=cmd_chatid)

    pg = sub.add_parser("telegram-test", help="отправить тестовый алерт")
    pg.add_argument("--config", default=None)
    pg.set_defaults(func=cmd_tgtest)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
