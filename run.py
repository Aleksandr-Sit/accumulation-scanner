#!/usr/bin/env python3
"""CLI автосканера: воронка входа + контур позиций (hodl-профиль).

  python run.py scan --track all            # полный прогон воронки (сеть)
  python run.py pos add ARB --price 0.5 --qty 1000    # записать покупку
  python run.py pos add ARB --price 0.45 --qty 500 --merge   # ступень -> средняя цена
  python run.py pos list                    # открытые позиции
  python run.py pos close ARB --price 1.2   # закрыть (P&L в журнал)
  python run.py watch [--notify]            # ре-скан позиций -> exit-сигналы
  python run.py brief [--notify]            # сводка дня (без сети) — последним шагом прогона
  python run.py card GRAM [--notify]        # карточка монеты: лестница, стоп, цели, картинка
  python run.py ladder LINK --budget 50      # план лестницы под Bybit spot (без ордеров)
  python run.py market [--backfill]         # история рынка альтов + индекс перегрева
  python run.py quality [--refresh-if-due]  # срез трека Q: дата, возраст; обновить, если пора
  python run.py backup [--send-weekly --notify]   # бэкап scanner.db (+ раз в неделю в Telegram)
  python run.py sync [--notify]             # реальные позиции из исполнений Bybit (Read-Only)
  python run.py execute --dry-run           # пробный исполнитель: «поставил бы», без ордеров
  python run.py halt [причина]              # стоп-кран исполнителя (снять — только resume)
  python run.py resume                      # снять стоп-кран (только на сервере, по SSH)
  python run.py control [--status]          # команды /stop /status из Telegram (таймер 5 мин)
  python run.py killed [--why ...]          # «⚠ прогон убит», если прогон не дошёл до конца
  python run.py selftest                    # офлайн-проверка логики на фикстурах
"""
from __future__ import annotations

import argparse
import html
import sqlite3
import sys

from scanner import closes
from scanner.config import load_config
from scanner.pipeline import run_scan, run_watch


def cmd_scan(args) -> int:
    import time as _t
    cfg = load_config(args.config)
    if args.pages is not None:
        cfg["universe"]["track_a_pages"] = args.pages
    print(f"[scan] track={args.track} limit={args.limit} pages={cfg['universe']['track_a_pages']} — старт")
    t_start = _t.time()
    try:
        summary = run_scan(cfg, track=args.track, limit=args.limit)
    except Exception as e:
        if args.notify:
            _notify_failure(cfg, "scan", e)
        raise
    print("\n=== ИТОГ ПРОГОНА ===")
    for k, v in summary.items():
        if not isinstance(v, dict):
            print(f"  {k:22}: {v}")
    print(f"\nWatchlist → {cfg['output']['watchlist_json']}")
    print(f"База      → {cfg['output']['db_path']}")

    if args.notify:
        # Гибрид: здесь — только карточки новых монет у дна (со звуком). Итог дня, рынок
        # и «прогон прошёл» — тихая сводка `run.py brief` последним шагом daily_run.
        from scanner.db import Store
        from scanner.notify import deliver, telegram
        from scanner.pipeline import _make_http, load_watchlist
        cands = load_watchlist(cfg["output"]["watchlist_json"])
        st = Store(cfg["output"]["db_path"])
        # mute: не повторять карточку по монете N дней, если балл не вырос заметно
        recent = st.recent_alerts(cfg.get("stage6_telegram.alert_mute_days", 3))
        fresh = [c for c in cands
                 if c.symbol not in recent or c.score > recent[c.symbol] + 5]
        if len(fresh) < len(cands):
            print(f"[telegram] mute: {len(cands) - len(fresh)} повторных кандидатов пропущено")
        picks = telegram.select_picks(fresh, cfg)
        papers = _papers_opened_since(cfg, t_start)
        http = _make_http(cfg)
        ranks = _card_ranks(cfg, http) if picks else {}
        for c in picks:
            notes = _exec_notes(cfg, c, ranks, summary.get("market_ctx") or {})
            card = deliver.coin_card(cfg, http, c, paper=papers.get(c.symbol.upper()),
                                     exec_notes=notes)
            ok = deliver.send_card(cfg, card)
            print(f"[telegram] карточка {c.symbol}: {'ok' if ok else 'fail'}"
                  f"{' (с картинкой)' if card.get('png') else ''}")
            if ok:
                # mute только тем, кто реально пришёл карточкой (не СЕРЕДИНА, не за лимитом)
                st.record_alert(c.symbol, c.score)
        if not picks:
            print("[telegram] новых монет у дна нет — карточек нет (итог дня — run.py brief)")
        st.close()
    return 0


def _card_ranks(cfg, http, network: bool = True) -> dict:
    """Места по 30-дн. обороту Bybit на сегодня для строк исполнителя в карточке
    (scanner/liquidity.py): из БД, а если их ещё нет — подсчёт (network) и запись, тогда
    execute возьмёт те же. Сбой — {}: карточка уходит без строки о нижней четверти."""
    try:
        from scanner import liquidity
        db = cfg["output"]["db_path"]
        r = liquidity.ensure_ranks(db, http) if network else liquidity.load_ranks(db)
    except Exception as e:  # noqa: BLE001 — оборот не должен ронять скан
        print(f"[liquidity] места по обороту не получены: {type(e).__name__}: {e}")
        return {}
    print(f"[liquidity] {r['n']} пар Bybit на сегодня" if r.get("ok") else f"[liquidity] {r['note']}")
    return r


def _exec_notes(cfg, c, ranks: dict, mctx: dict) -> list[str]:
    """Строки карточки «что сделает пробный исполнитель» (executor.card_notes); сбой — без них."""
    try:
        from scanner import executor
        return executor.card_notes(cfg, c, ranks, mctx)
    except Exception as e:  # noqa: BLE001
        print(f"[telegram] строки исполнителя для {c.symbol} пропущены: {type(e).__name__}: {e}")
        return []


def _papers_opened_since(cfg, ts: float) -> dict[str, dict]:
    """{SYMBOL: {entry_price, stake}} — paper-позиции A, открытые этим прогоном (пометка в карточке)."""
    from scanner.positions import PositionStore
    ps = PositionStore(cfg["output"]["db_path"])
    out = {p["symbol"].upper(): {"entry_price": p["entry_price"],
                                 "stake": p["entry_price"] * (p.get("initial_qty") or p["qty"])}
           for p in ps.open_positions()
           if p.get("is_paper") and (p.get("variant") or "A") == "A" and p["entry_ts"] >= ts}
    ps.close_db()
    return out


def _say(*parts) -> None:
    """print, который не роняет шаг: лог прогона — это stdout в logs/, и при полном диске
    (ENOSPC) print бросает OSError раньше, чем сводка дня уйдёт в Telegram."""
    try:
        print(*parts)
    except OSError:
        pass


class _SafeStream:
    """stdout/stderr, запись в которые не бросает OSError (полный диск под logs/)."""

    def __init__(self, stream):
        self._s = stream

    def write(self, s):
        try:
            return self._s.write(s)
        except OSError:
            return len(s)

    def flush(self):
        try:
            self._s.flush()
        except OSError:
            pass

    def __getattr__(self, name):
        return getattr(self._s, name)


def _notify_failure(cfg, step: str, e: Exception) -> None:
    """Шаг прогона упал исключением — шлём короткое сообщение (ошибки отправки глотаем,
    чтобы не заслонить исходное исключение)."""
    try:
        from scanner.notify import telegram
        telegram.send_message(cfg.get("api_keys.telegram_token", ""),
                              cfg.get("api_keys.telegram_chat_id", ""),
                              telegram.format_failure(step, f"{type(e).__name__}: {e}"))
    except Exception as e2:  # noqa: BLE001
        print(f"[telegram] сообщение о сбое не отправлено: {e2}")


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
        prices_map = {s["position_id"]: s["price"]
                      for s in pstore.last_snapshots().values()}
        cur = pstore.find_open_real(args.symbol) if args.merge else None
        if cur:
            # Ступень лестницы в существующую позицию: одна позиция со средней ценой.
            others = [p for p in pstore.open_positions() if p["id"] != cur["id"]]
            held = prices_map.get(cur["id"], cur["entry_price"]) * cur["qty"]
            for w in risk_check(others, held + args.price * args.qty, cfg, prices_map):
                print(f"⚠ РИСК: {w}")
            if args.base_low is not None and cur["base_low"] and \
                    abs(args.base_low - cur["base_low"]) > 1e-12:
                print(f"⚠ --base-low {args.base_low:g} проигнорирован: у позиции остаётся "
                      f"{cur['base_low']:.6g} (стоп считается от базы первой ступени)")
            if pstore.event_types(cur["id"]) & {"ladder_0", "ladder_1", "trailing"}:
                print("⚠ по позиции уже были сигналы лестницы/трейла — они не переармируются "
                      "от новой средней")
            m = pstore.merge(cur["id"], args.price, args.qty)
            fee = 0.0015                       # как в position_pnl / sell
            bl = f"{m['base_low']:.6g}" if m["base_low"] else "—"
            print(f"Позиция #{m['id']} {m['symbol']}: +{args.qty:g} @ {args.price:g} → "
                  f"qty={m['qty']:g}, средняя {m['entry_price']:.6g} "
                  f"(с комиссией {m['entry_price'] * (1 + fee):.6g}), base_low={bl}")
            e = cfg["stage8_exit"]
            tgt = " / ".join(f"+{g * 100:.0f}% ≈ {m['entry_price'] * (1 + g):.6g}"
                             for g, _ in e["ladder"])
            print(f"  цели watch от средней: {tgt}; трейл после "
                  f"+{e['trailing_arm_after_gain_pct']}%")
            pstore.close_db(); return 0
        if args.merge:
            print(f"Открытой реальной позиции {args.symbol.upper()} нет — открываю новую")
        info = _lookup_coin(cfg, args.symbol)
        coin_id = args.coin_id or info.get("coin_id", "")
        if not coin_id:
            print(f"⚠ coin_id для {args.symbol} не найден в watchlist/БД — "
                  f"watch не сможет достать цену. Задай: --coin-id <id CoinGecko>")
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
            v = p.get("variant") or "A"
            tag = ("📝paper" + (f"·{v}" if v != "A" else "  ")
                   if p.get("is_paper") else "💰real   ")
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


def _quality_note(cfg, sym: str) -> str:
    """Строка о монете из свежего среза фильтра качества (scanner/quality.fresh_slice)."""
    from scanner.quality import fresh_slice
    s = fresh_slice(cfg)
    if not s["ok"]:
        return f"фильтр качества: среза нет ({s['note']}; run.py quality --refresh-if-due)"
    row = next((r for r in s["rows"] if (r.get("sym") or "").upper() == sym), None)
    day = s["date"]
    if row is None:
        return f"фильтр качества ({day}): нет в топ-500 — вне проверенной зоны"
    if not row["fails"]:
        return f"фильтр качества ({day}): ✅ прошла"
    return f"фильтр качества ({day}): ⚠ отсеяна — " + "; ".join(row["fails"])


def cmd_ladder(args) -> int:
    """План лестницы покупок/продаж под Bybit spot. Ничего не выставляет — только считает."""
    import os
    cfg = load_config(args.config)
    from scanner.ladder import fmt_step, plan_ladder
    from scanner.pipeline import _make_http
    from scanner.sources import bybit
    from scanner.stages.exit import compute_base_low

    base = args.symbol.upper().removesuffix("USDT")
    pair = f"{base}USDT"
    http = _make_http(cfg)
    inst = bybit.fetch_instrument(http, pair)
    if not inst:
        print(f"{pair} нет на Bybit spot (или API недоступен) — план не строю")
        return 1
    price = args.price or bybit.fetch_last_price(http, pair)
    base_low = args.base_low
    if base_low is None:
        closes = bybit.fetch_daily_closes(http, pair, 40)   # только закрытые свечи
        base_low = compute_base_low(closes, 30)
    if not price or not base_low:
        print("Нет цены или истории закрытий — задай --price и --base-low вручную")
        return 1
    e = cfg["stage8_exit"]
    floor_pct = e["invalidation_below_base_low_pct"]
    plan = plan_ladder(price, base_low, args.budget, steps=args.steps, min_order=args.min_order,
                       tick=inst["tick"], qty_step=inst["qty_step"],
                       exch_min_amt=inst["min_amt"], exch_min_qty=inst["min_qty"],
                       floor_pct=floor_pct, stop_pct=args.stop_pct, sell=args.sell,
                       prod_levels=e["ladder"], filled=args.filled,
                       worst_case_pct=cfg.get("stage7_positions.worst_case_loss_pct", 60))
    if not plan["ok"]:
        print(f"⚠ {plan['error']}")
        return 1
    tick, qs = inst["tick"], inst["qty_step"]
    P = lambda x: fmt_step(x, tick)  # noqa: E731
    Q = lambda x: fmt_step(x, qs)  # noqa: E731

    print(f"=== ЛЕСТНИЦА {pair} (Bybit spot) — расчёт, не рекомендация ===")
    if inst["st"] or inst["status"] != "Trading":
        print(f"⚠ Bybit: метка ST (риск делистинга) или статус {inst['status']}")
    print(f"  {_quality_note(cfg, base)}")
    print(f"  цена {P(price)} · лоу базы 30д {P(base_low)} · шаг цены {tick:g} · "
          f"шаг кол-ва {qs:g} · мин. ордер биржи ${inst['min_amt']:g}, ваш ${args.min_order:g}")
    confirm = e.get("invalidation_confirm_days", 1)
    print(f"  стоп: {confirm} закрытия ниже {P(plan['stop_px'])} "
          f"(лоу базы −{plan['stop_pct']:g}%, {plan['stop_px'] / price - 1:+.1%} от цены)"
          + ("" if args.stop_pct is None else f"; ступени — до пола прод-стопа −{floor_pct:g}%"))

    print(f"\nПокупка — бюджет ${args.budget:g}, ступеней {plan['steps']}:")
    print(f"  {'#':>2} {'тип':6} {'цена':>12} {'кол-во':>12} {'$':>8} {'от цены':>8}")
    for b in plan["buys"]:
        px = f"~{P(b['price'])}" if b["type"] == "рынок" else P(b["price"])
        print(f"  {b['n']:>2} {b['type']:6} {px:>12} {Q(b['qty']):>12} {b['usd']:>8.2f} "
              f"{b['from_price']:>+8.1%}")
    w = plan["worst"]
    print("  ступень 1 — рыночный ордер на сумму в USDT, остальные — лимитки GTC")
    print(f"\nХудший случай (все ступени, выход по стопу): −${w['stop_loss_usd']:.2f} "
          f"({-w['stop_loss_pct']:.1%} вложенного; разом было бы {-w['lump_stop_loss_pct']:.1%})")
    print(f"  гэп/делистинг — стоп не гарантирован: risk_check закладывает "
          f"−${w['gap_loss_usd']:.2f} ({cfg.get('stage7_positions.worst_case_loss_pct', 60)}%)")

    k = plan["filled"]
    what = "все ступени" if k == len(plan["buys"]) else f"первые {k} ступ."
    print(f"\nПродажа ({args.sell}) — если исполнены {what}: вложено "
          f"${sum(b['usd'] for b in plan['buys'][:k]):.2f}, средняя {P(plan['avg'])}, "
          f"кол-во {Q(plan['qty'])}:")
    for s in plan["sells"]:
        if s["price"] is None:
            print(f"  {s['label']:18} {Q(s['qty']):>12}  трейл {e['trailing_from_hwm_pct']}% "
                  f"от максимума после +{e['trailing_arm_after_gain_pct']}% — ведёт `watch`")
        else:
            print(f"  {s['label']:18} {Q(s['qty']):>12}  по {P(s['price']):>12}  ≈ ${s['usd']:.2f}")
    if args.sell != "paired":
        print("  после частичного исполнения пересчитай цели: --filled N")

    for msg in plan["warns"]:
        print(f"⚠ {msg}")

    # бюджет риска проекта (как в pos add): лимит позиции и worst-case просадка портфеля
    from scanner.positions import PositionStore, risk_check
    cap = args.capital if args.capital is not None else cfg.get("stage7_positions.capital_usdt", 0)
    if cap:
        cfg["stage7_positions"]["capital_usdt"] = cap
        opens = []
        if os.path.exists(cfg["output"]["db_path"]):
            ps = PositionStore(cfg["output"]["db_path"])
            opens = ps.open_positions()
            ps.close_db()
        warns = risk_check(opens, plan["spent"], cfg)
        print(f"\nБюджет риска (капитал ${cap:g}): " + ("в лимитах ✅" if not warns else "превышен"))
        for msg in warns:
            print(f"  ⚠ {msg}")
    else:
        print("\nБюджет риска не проверен: задай --capital (или stage7_positions.capital_usdt)")

    print(f"\nПосле каждой исполненной ступени (первая откроет позицию, следующие усреднят её):"
          f"\n  py -3 run.py pos add {base} --price <факт> --qty <факт> "
          f"--base-low {P(base_low)} --merge")
    print("  цели продажи и трейл `watch` считает от средней всей позиции")
    return 0


def cmd_watch(args) -> int:
    """Код: 0 — всё посчитано и доставлено; 2 — у части позиций нет цены/сбой расчёта;
    3 — карточка выхода не доставлена (осталась в очереди — повтор в следующем прогоне)."""
    cfg = load_config(args.config)
    try:
        out = run_watch(cfg)
    except Exception as e:
        if args.notify:
            _notify_failure(cfg, "watch", e)
        raise
    rows, summary = out["rows"], out["summary"]
    code = 2 if summary.get("errors") else 0
    if not rows:
        print("Открытых позиций нет — нечего отслеживать (run.py pos add ...).")
    print(f"=== WATCH: {summary['positions']} позиций, "
          f"{summary['signals']} новых сигналов ===")
    if summary.get("market"):
        print(f"  рынок: {summary['market']}")
    print()
    for r in rows:
        p = r["position"]
        if r.get("error"):
            print(f"  {p['symbol']:<8} ⚠ {r['error']}")
            continue
        pnl = r["pnl"]
        tag = "📝" if p.get("is_paper") else "💰"
        if (p.get("variant") or "A") != "A":
            tag += p["variant"]
        rz = r.get("realized_usdt") or 0.0
        rzs = f", realized {rz:+.2f}" if abs(rz) > 1e-9 else ""
        day = f" ({closes.close_label(r['last_ts'])}, {r.get('src')})" if r.get("last_ts") else ""
        print(f"  {tag} {p['symbol']:<8} {pnl['pnl_pct']:+7.1f}%  "
              f"({pnl['pnl_usdt']:+.2f} USDT{rzs}, {r['held_days']}д, "
              f"закрытие {r['last_price']:.6g}{day}, hwm {r['hwm']:.6g}, "
              f"новых закрытий {r.get('new_closes', 0)})")
        if r.get("note"):
            print(f"           ⚠ {r['note']}")
        for s in r["signals"]:
            when = f" ({closes.close_label(s['close_ts'])})" if s.get("close_ts") else ""
            print(f"           🔔 [{s['type']}]{when} {s['action']} — {s['note']}")
        for ex in r.get("executed", []):
            print(f"           ⚙ [paper] {ex}")
    print(f"\n  Итого нереализованный P&L: {summary['pnl_total_usdt']:+.2f} USDT")

    if args.notify:
        # Гибрид: по карточке на позицию и закрытие с новым сигналом. Звук — только реальная
        # позиция и сигнал high/medium; paper и информационные — тихо. Позиции целиком — в
        # сводке дня (run.py brief). Близнецы B/S — тихий эксперимент, в очередь не попадают.
        # Очередь notify_outbox: карточка, не дошедшая в прошлый раз, уходит сейчас.
        # Без --notify очередь не трогается — ручной просмотр не съедает уведомление.
        from scanner.notify import deliver
        from scanner.pipeline import _make_http
        sent, failed = deliver.flush_exit_outbox(cfg, _make_http(cfg))
        if not (sent or failed):
            print("[telegram] новых сигналов нет — карточек нет (позиции — в run.py brief)")
        if failed:
            print(f"[telegram] не доставлено карточек: {failed} — остались в очереди")
            code = 3
    return code


def cmd_brief(args) -> int:
    """Сводка дня — тихо, последним шагом ежедневного прогона. Без сети: scanner.db +
    watchlist.json. Пришла сводка — прогон дошёл до конца."""
    cfg = load_config(args.config)
    from scanner.notify import deliver, telegram
    try:
        state = deliver.brief_state(cfg, scan_exit=args.scan_exit, watch_exit=args.watch_exit,
                                    backup_exit=args.backup_exit, sync_exit=args.sync_exit,
                                    exec_exit=args.exec_exit, report_exit=args.report_exit,
                                    quality_exit=args.quality_exit)
        text = telegram.format_brief(state, cfg, test=args.test)
    except Exception as e:
        if args.notify:
            _notify_failure(cfg, "brief", e)
        raise
    _say(telegram.strip_html(text))
    if not args.notify:
        return 0
    ok = telegram.send_message(cfg.get("api_keys.telegram_token", ""),
                               cfg.get("api_keys.telegram_chat_id", ""), text, silent=True)
    _say(f"[telegram] сводка дня: {'ok' if ok else 'fail'}")
    return 0 if ok else 1


def cmd_card(args) -> int:
    """Карточка монеты (лестница, стоп, цели, уровни, картинка) — как придёт в Telegram.
    Без --notify печатает текст и сохраняет картинку в logs/card_SYMBOL.png."""
    from pathlib import Path
    cfg = load_config(args.config)
    from scanner.models import Candidate
    from scanner.notify import deliver, telegram
    from scanner.pipeline import _make_http, load_watchlist
    sym = args.symbol.upper().removesuffix("USDT")
    c = next((x for x in load_watchlist(cfg["output"]["watchlist_json"])
              if (x.symbol or "").upper() == sym), None)
    if c is None:
        info = _lookup_coin(cfg, sym)
        c = Candidate(source="manual", track="", symbol=sym, coin_id=info.get("coin_id", ""),
                      chain=info.get("chain", ""), address=info.get("address", ""),
                      rf_venue=info.get("venue") or "Bybit spot")
        print(f"⚠ {sym} нет в последнем watchlist — карточка без балла и зоны")
    if args.budget:
        cfg["stage6_telegram"]["card_budget_usdt"] = args.budget
    from scanner.positions import PositionStore
    ps = PositionStore(cfg["output"]["db_path"])
    pp = next((p for p in ps.open_positions() if p["symbol"] == sym and p.get("is_paper")
               and (p.get("variant") or "A") == "A"), None)
    ps.close_db()
    paper = ({"entry_price": pp["entry_price"],
              "stake": pp["entry_price"] * (pp.get("initial_qty") or pp["qty"])} if pp else None)
    # строки исполнителя — по уже посчитанным за сегодня местам и рынку из БД (без 390 запросов)
    from scanner import regime
    from scanner.db import Store
    st = Store(cfg["output"]["db_path"])
    mctx = regime.market_context(st.market_rows(), cfg)
    st.close()
    http = _make_http(cfg)
    notes = _exec_notes(cfg, c, _card_ranks(cfg, http, network=False), mctx)
    card = deliver.coin_card(cfg, http, c, paper=paper, test=args.test, exec_notes=notes)
    print(telegram.strip_html(card["caption"] if card.get("png") else card["text"]))
    if card.get("png"):
        out = Path(__file__).resolve().parent / "logs" / f"card_{sym}.png"
        out.parent.mkdir(exist_ok=True)
        out.write_bytes(card["png"])
        print(f"\nкартинка → {out}")
    if args.notify:
        ok = deliver.send_card(cfg, card)
        print(f"[telegram] карточка: {'ok' if ok else 'fail'}")
        return 0 if ok else 1
    return 0


def cmd_report(args) -> int:
    """Недельная сводка форвард-теста. Без сети — из снапшотов/журнала."""
    import time as _t
    cfg = load_config(args.config)
    from scanner.positions import PositionStore
    from scanner.notify import telegram
    pstore = PositionStore(cfg["output"]["db_path"])

    now = _t.time()
    if args.if_due:
        # Ежедневный прогон зовёт report каждый день, сводка уходит раз в неделю.
        last = pstore.last_event_ts(0, "weekly_report")
        if not telegram.weekly_due(last, now, cfg.get("stage6_telegram.weekly_report_weekday", 0)):
            print(f"[report] сводка этой недели уже была "
                  f"({_t.strftime('%d.%m %H:%M', _t.localtime(last))}) — пропуск")
            pstore.close_db()
            return 0
    week_ago = now - 7 * 86400
    # Близнецы B/S (A/B выхода и стопа) не входят в основные счётчики — только в блоки
    # сравнения.
    def _main(x):
        return (x.get("variant") or "A") == "A"
    events = [e for e in pstore.events_since(week_ago) if _main(e)]
    opens = [p for p in pstore.open_positions() if _main(p)]
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
    all_pos = [p for p in pstore.all_positions() if _main(p)]
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
    all_events = ([e for e in pstore.events_since(0) if _main(e)]
                  if milestone else events)

    # A/B выхода: полный P&L пар A↔B net-of-fees (realized + unrealized по последнему
    # снапшоту, как при продаже) — иначе закрытый B платил бы комиссию, а открытый A нет.
    from scanner.stages.exit import position_pnl

    def _total(p):
        s = snaps.get(p["id"])
        unreal = (position_pnl(p, s["price"])["pnl_usdt"]
                  if s and p["status"] == "open" and p["qty"] > 0 else 0.0)
        return (p.get("realized_usdt") or 0.0) + unreal
    def _ab(variant):
        pairs = pstore.ab_pairs(variant)
        diffs = [_total(b) - _total(a) for a, b in pairs]
        return {"pairs": len(pairs),
                "a_usdt": round(sum(_total(a) for a, _ in pairs), 2),
                "b_usdt": round(sum(_total(b) for _, b in pairs), 2),
                "diverged": sum(1 for d in diffs if abs(d) > 0.01),
                "b_better": sum(1 for d in diffs if d > 0.01),
                # сколько раз стоп сработал у каждой стороны (для S — главный вопрос)
                "a_stopped": sum(1 for a, _ in pairs
                                 if "invalidation" in pstore.event_types(a["id"])),
                "b_stopped": sum(1 for _, b in pairs
                                 if "invalidation" in pstore.event_types(b["id"])),
                "b_open": sum(1 for _, b in pairs if b["status"] == "open")}
    ab = _ab("B")
    ab_stop = _ab("S")

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
        "ab": ab,
        "ab_stop": ab_stop,
        "ab_stop_pct": cfg.get("stage7_positions.paper_ab_stop_pct", 50),
        "stop_pct": cfg.get("stage8_exit.invalidation_below_base_low_pct", 25),
    }
    # Против рынка (scanner/benchmark.py): книга на тех же окнах, что альты и BTC, — иначе
    # рост рынка выглядит успехом отбора. Сбой сравнения не должен съесть сводку.
    try:
        from scanner import benchmark
        stats["benchmark"] = benchmark.weekly_books(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[report] сравнение с рынком пропущено: {type(e).__name__}: {e}")
    # Пробный исполнитель (scanner/executor.py): книги R/H против альтов и контрольной корзины,
    # теневые книги (отсеянное фильтрами) — против альтов и по причинам.
    try:
        from scanner import executor
        stats["executor"] = executor.weekly_books(cfg)
        stats["executor_shadow"] = executor.weekly_shadow(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[report] блок пробного исполнителя пропущен: {type(e).__name__}: {e}")
    text = telegram.format_weekly(stats, cfg)
    print(text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    delivered = False
    if args.notify:
        delivered = telegram.send_message(cfg.get("api_keys.telegram_token", ""),
                                          cfg.get("api_keys.telegram_chat_id", ""), text,
                                          silent=True)
        print(f"[telegram] недельная сводка: {'ok' if delivered else 'fail'}")
    # Флаги — только после доставки в Telegram: ручной просмотр (без --notify), прогон
    # --no-notify и сбой отправки не должны «съесть» сводку недели и итоговую 4-недельную.
    if delivered:
        if milestone:
            pstore.set_system_flag("milestone_4w", f"week={week_no}")
        pstore.set_system_flag("weekly_report", f"week={week_no}")
    pstore.close_db()
    return 1 if args.notify and not delivered else 0


def cmd_market(args) -> int:
    """Обновить market_daily (или полный бэкфилл) и показать контекст рынка."""
    import time as _t
    cfg = load_config(args.config)
    from scanner.db import Store
    from scanner.pipeline import _make_http
    from scanner.sources import market
    from scanner import regime
    store = Store(cfg["output"]["db_path"])
    t0 = _t.time()
    info = market.update_market_daily(cfg, _make_http(cfg), store,
                                      force=True, backfill=args.backfill)
    st = store.market_stats()
    ctx = regime.market_context(store.market_rows(), cfg)
    store.close()
    print(f"[market] {info['mode']}: записано дней {info['updated']} за {_t.time()-t0:.0f} с; "
          f"в таблице {st['days_alt']} дней с альт-рынком")
    if not ctx:
        print("Контекст не посчитан — нет данных по альт-рынку.")
        return 1
    day = _t.strftime("%Y-%m-%d", _t.gmtime(ctx["day"]))
    print(f"\n=== РЫНОК на {day} ===\n  {regime.context_line(ctx)}\n")
    hot = ctx["hot"]
    flags = cfg.get("market_regime.hot_flags", {}) or {}
    for name, spec in flags.items():
        v = ctx.get(name)
        mark = "🔥" if name in hot["lit"] else ("·~" if name in hot["near"] else " ·")
        vs = f"{v:.3f}" if isinstance(v, (int, float)) else "нет данных"
        print(f"  {mark} {regime.FLAG_LABELS.get(name, name):<18} {vs:>10}  "
              f"(порог {spec[0]} {spec[1]}, близко {spec[2] if len(spec) > 2 else '—'})")
    return 0


def cmd_quality(args) -> int:
    """Срез фильтра качества (трек Q): какой файл свежий, дата, возраст, число монет.
    --refresh-if-due (ежедневный прогон): срезу ≥ track_q.refresh_after_days или его нет —
    backtest/quality_screen.py --out <live_source> подпроцессом (~6 мин сети, таймаут 30 мин).
    Без флага код 1 — трек Q сейчас пуст (среза нет или старше max_age_days)."""
    cfg = load_config(args.config)
    from scanner import quality
    s = quality.fresh_slice(cfg)
    max_age = cfg.get("track_q.max_age_days", 30)
    refresh = cfg.get("track_q.refresh_after_days", 25)
    label = lambda x: quality.SRC_LABEL.get(x["src"], x["src"])  # noqa: E731
    alive = False
    if s["ok"]:
        n_ok = len(quality.passed(s["rows"]))
        print(f"Срез трека Q: {s['date']}, возраст {s['age_days']:.1f} дн. · "
              f"{quality.rel(s['path'])} ({label(s)}) · прошли гейт {n_ok} из {len(s['rows'])}")
        for t in s["tried"]:                  # второй файл: почему не он
            if t["path"] != s["path"]:
                print(f"  {label(t)}: " + (f"срез от {t['date']}" if t["ok"] else t["note"]))
        day = lambda n: quality.day_after(s["date"], n)  # noqa: E731
        alive = max_age is None or s["age_days"] <= max_age
        if alive:
            tail = ("" if max_age is None else
                    f", трек выключится {day(max_age)} (max_age_days {max_age:g})")
            print(f"  автообновление — с {day(refresh)} (refresh_after_days {refresh:g}){tail}")
        else:
            print(f"⚠ трек Q выключен: срез старше {max_age:g} дн.")
    else:
        print(f"⚠ Среза трека Q нет: {s['note']}")
    if not args.refresh_if_due:
        return 0 if alive else 1
    code, msg = quality.refresh_if_due(cfg)
    print(f"[quality] {msg}")
    return code


def cmd_backup(args) -> int:
    """Бэкап scanner.db: sqlite backup API → integrity_check → gzip → ротация. --send-weekly
    (+ --notify): раз в неделю свежий .gz в Telegram документом без звука. Код ≠ 0 при любом
    сбое; 3 — копия сделана, но в Telegram не ушла (сводка дня различает)."""
    cfg = load_config(args.config)
    from scanner import backup
    try:
        info = backup.make_backup(cfg["output"]["db_path"], args.dir or backup.DEFAULT_DIR,
                                  keep=args.keep)
    except Exception as e:  # noqa: BLE001 — любой сбой: код 1, сводка дня покажет
        print(f"backup FAIL: {type(e).__name__}: {e}")
        return 1
    print(backup.ok_line(info))
    if not (args.send_weekly or args.test):
        return 0
    try:
        code, msg = backup.send_weekly(cfg, info, notify=args.notify, test=args.test)
    except Exception as e:  # noqa: BLE001 — копия уже есть: «не ушла», а не «не сделана»
        code, msg = backup.EXIT_SEND_FAILED, f"недельная отправка упала: {type(e).__name__}: {e}"
    print(f"[backup] {msg}")
    return code


def cmd_sync(args) -> int:
    """Реальные позиции из спот-исполнений Bybit (ключ Read-Only в .env). Нет ключа — тихий
    пропуск, код 0; сбой API — код 1 (сводка дня: «⚠ синхронизация с Bybit не прошла»).
    Сначала самопроверка ключа (scanner/keycheck.py): итог — в сводку дня; опасность (право
    вывода, ключ умеет торговать) — с --notify ещё и отдельным сообщением со звуком."""
    cfg = load_config(args.config)
    from scanner import bybit, keycheck, sync
    from scanner.positions import PositionStore
    kc = keycheck.run(cfg)
    if kc:
        print(f"[sync] {keycheck.line(kc)}")
        if kc["level"] == "danger" and getattr(args, "notify", False):
            from scanner.notify import telegram
            telegram.send_message(cfg.get("api_keys.telegram_token", ""),
                                  cfg.get("api_keys.telegram_chat_id", ""),
                                  "<b>" + html.escape(keycheck.line(kc)) + "</b>")
    ps = PositionStore(cfg["output"]["db_path"])
    try:
        code, lines = sync.run_sync(cfg, ps, lookup=lambda sym: _lookup_coin(cfg, sym))
    except (bybit.BybitError, OSError, sqlite3.Error) as e:   # сеть/ключ/база — текст в лог;
        code, lines = 1, [f"FAIL: {type(e).__name__}: {e}"]    # исполнение не записано целиком
    finally:
        ps.close_db()
    for line in lines:
        print(f"[sync] {line}")
    return code


def cmd_execute(args) -> int:
    """Пробный исполнитель (scanner/executor.py): лестницы по сегодняшним карточкам в книги
    R «правила» и H «держать», исполнения по свечам Bybit, журнал «поставил бы». Ордеров нет.
    Код 1 — сбой (сводка дня: «⚠ пробный исполнитель упал»), 2 — запуск без --dry-run."""
    if not args.dry_run:
        print("реальный режим не реализован (шаги 3–4 плана) — запускай с --dry-run")
        return 2
    cfg = load_config(args.config)
    from scanner import bybit, executor
    from scanner.pipeline import _make_http
    wallet, note = None, ""
    key, secret = cfg.get("api_keys.bybit_key", ""), cfg.get("api_keys.bybit_secret", "")
    if key and secret:              # справка: хватило бы реального USDT (ключ Read-Only)
        try:
            c = bybit.Client(key, secret, cfg.get("bybit_sync.base_url", bybit.BASE_URL),
                             recv_window=cfg.get("bybit_sync.recv_window_ms", 5000))
            u = c.wallet_balance().get("USDT") or {}
            wallet = max(0.0, u.get("balance", 0.0) - u.get("locked", 0.0))
        except (bybit.BybitError, OSError) as e:
            note = f"реальный баланс не прочитан: {e}"
    else:
        note = "реальный баланс: ключа Bybit нет — справки нет"
    http = _make_http(cfg)
    db = cfg["output"]["db_path"]
    # Перегрев (фильтр 4) — тот же контекст, что видел скан: market_daily, обновление не чаще
    # update_min_interval_hours. Сбой — None: run_dry возьмёт рынок из БД.
    mctx = None
    try:
        from scanner.db import Store
        from scanner.sources import market
        st = Store(db)
        try:
            mctx = market.load_context(cfg, http, st)
        finally:
            st.close()
    except Exception as e:  # noqa: BLE001
        print(f"[execute] контекст рынка не обновлён ({type(e).__name__}: {e}) — беру из БД")
    try:
        code, lines = executor.run_dry(cfg, executor.BybitMarket(http, db),
                                       wallet_usdt=wallet, wallet_note=note, mctx=mctx)
    except Exception as e:  # noqa: BLE001 — код 1 и текст в лог, сводка дня покажет
        code, lines = 1, [f"FAIL: {type(e).__name__}: {e}"]
    for line in lines:
        print(f"[execute] {line}")
    return code


def _tell_owner(cfg, text: str) -> None:
    """Сообщение владельцу о стоп-кране (без исключений: стоп важнее доставки)."""
    try:
        from scanner.notify import telegram
        telegram.send_message(cfg.get("api_keys.telegram_token", ""),
                              cfg.get("api_keys.telegram_chat_id", ""), text)
    except Exception as e:  # noqa: BLE001
        _say(f"[telegram] не отправлено: {type(e).__name__}: {e}")


def cmd_halt(args) -> int:
    """Стоп-кран с сервера: исполнитель не ставит лестниц и не исполняет ордеров до resume."""
    from scanner import control
    cfg = load_config(args.config)
    info = control.set_halt(" ".join(args.reason) or "остановлен на сервере", "сервер")
    line = control.halt_line(info)
    print(line + "\nСнять: python3 run.py resume")
    if not args.quiet:
        _tell_owner(cfg, html.escape(line) + "\nСнять — только на сервере: python3 run.py resume")
    return 0


def cmd_resume(args) -> int:
    """Снять стоп-кран. Только с сервера (по SSH): из Telegram нельзя. Владельцу — сообщение,
    чтобы снятие чужими руками не прошло тихо."""
    from scanner import control
    cfg = load_config(args.config)
    was = control.clear_halt()
    if not was:
        print("стоп-крана не было — исполнитель и так работает")
        return 0
    print(f"стоп-кран снят (был: {control.halt_line(was)})")
    if not args.quiet:
        _tell_owner(cfg, "▶️ Стоп-кран снят на сервере — исполнитель снова работает со "
                         "следующего прогона.\n<i>Был: "
                         + html.escape(control.halt_line(was)) + "</i>")
    return 0


def cmd_control(args) -> int:
    """Опрос команд владельца в Telegram (/stop, /status, /help) — таймер раз в 5 минут.
    --status: то же, что /status, в консоль (без Telegram)."""
    from scanner import control
    cfg = load_config(args.config)
    if args.status:
        print(control.status_text(cfg["output"]["db_path"]))
        return 0
    code, log = control.poll(cfg)
    for line in log:
        if line != "новых команд нет":         # журнал таймера — только события
            print(f"[control] {line}")
    return code


def cmd_killed(args) -> int:
    """«⚠ прогон убит»: зовут trap daily_run.sh и scanner-alert.service (OnFailure=). Сообщает,
    только если прогон не дошёл до сводки дня и о нём ещё не сообщали."""
    from scanner import runguard
    cfg = load_config(args.config)
    code, msg = runguard.alert(cfg, why=args.why)
    _say(f"[killed] {msg}")
    return code


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
    pp.add_argument("--merge", action="store_true",
                    help="add: докупка в открытую реальную позицию монеты (средняя цена, "
                         "base_low не меняется); позиции нет — откроется новая")
    pp.add_argument("--notes", default="")
    pp.add_argument("--all", action="store_true", help="list: включая закрытые")
    pp.add_argument("--config", default=None)
    pp.set_defaults(func=cmd_pos)

    pl = sub.add_parser("ladder", help="план лестницы покупок/продаж под Bybit spot (без ордеров)")
    pl.add_argument("symbol", help="тикер: LINK или LINKUSDT")
    pl.add_argument("--budget", type=float, required=True, help="бюджет на монету, USDT")
    pl.add_argument("--steps", type=int, default=4, help="ступеней покупки (1 рынком + лимитки)")
    pl.add_argument("--min-order", type=float, default=10.0, help="минимум на ступень, USDT")
    pl.add_argument("--sell", choices=["prod", "even", "paired"], default="prod",
                    help="prod: +50/+150 по 1/3 + трейл; even: +30/60/100/200; paired: +40%% на ступень")
    pl.add_argument("--stop-pct", type=float, default=None,
                    help="стоп: %% ниже лоу базы (по умолчанию как в проде, 25)")
    pl.add_argument("--filled", type=int, default=None,
                    help="цели продажи для первых N исполненных ступеней")
    pl.add_argument("--price", type=float, default=None, help="цена вместо последней с Bybit")
    pl.add_argument("--base-low", type=float, default=None,
                    help="лоу базы вместо минимума 30 закрытий Bybit")
    pl.add_argument("--capital", type=float, default=None, help="капитал для проверки risk_check")
    pl.add_argument("--config", default=None)
    pl.set_defaults(func=cmd_ladder)

    pw = sub.add_parser("watch", help="ре-скан открытых позиций -> exit-сигналы")
    pw.add_argument("--notify", action="store_true", help="отправить exit-алерт в Telegram")
    pw.add_argument("--config", default=None)
    pw.set_defaults(func=cmd_watch)

    pr = sub.add_parser("report", help="недельная сводка форвард-теста (без сети)")
    pr.add_argument("--notify", action="store_true", help="отправить в Telegram")
    pr.add_argument("--milestone-weeks", type=int, default=4,
                    help="на какой неделе выдать итоговую сводку (одноразово)")
    pr.add_argument("--if-due", action="store_true",
                    help="только если сводки этой недели ещё не было "
                         "(stage6_telegram.weekly_report_weekday) — для ежедневного прогона")
    pr.add_argument("--config", default=None)
    pr.set_defaults(func=cmd_report)

    pb = sub.add_parser("brief", help="сводка дня в Telegram (без сети, последним шагом прогона)")
    pb.add_argument("--notify", action="store_true", help="отправить в Telegram (без звука)")
    pb.add_argument("--test", action="store_true", help="пометить сообщение «🧪 ТЕСТ»")
    pb.add_argument("--scan-exit", type=int, default=None,
                    help="код выхода scan из daily_run (иначе — по журналу прогонов)")
    pb.add_argument("--watch-exit", type=int, default=None, help="код выхода watch из daily_run")
    pb.add_argument("--backup-exit", type=int, default=None,
                    help="код выхода backup из daily_run: ≠ 0 — «⚠ бэкап не сделан» в строке "
                         "статуса (3 — копия есть, но в Telegram не ушла)")
    pb.add_argument("--sync-exit", type=int, default=None,
                    help="код выхода sync из daily_run: ≠ 0 — «⚠ синхронизация с Bybit не "
                         "прошла» в строке статуса")
    pb.add_argument("--exec-exit", type=int, default=None,
                    help="код выхода execute из daily_run: ≠ 0 — «⚠ пробный исполнитель упал»")
    pb.add_argument("--report-exit", type=int, default=None,
                    help="код выхода report из daily_run: ≠ 0 — «⚠ недельная сводка не ушла»")
    pb.add_argument("--quality-exit", type=int, default=None,
                    help="код выхода quality из daily_run: ≠ 0 — «⚠ срез трека Q не обновился»")
    pb.add_argument("--config", default=None)
    pb.set_defaults(func=cmd_brief)

    pk = sub.add_parser("card", help="карточка монеты: лестница, стоп, цели, картинка уровней")
    pk.add_argument("symbol", help="тикер: GRAM или GRAMUSDT")
    pk.add_argument("--notify", action="store_true", help="отправить в Telegram")
    pk.add_argument("--test", action="store_true", help="пометить сообщение «🧪 ТЕСТ»")
    pk.add_argument("--budget", type=float, default=None,
                    help="бюджет лестницы, USDT (по умолчанию stage6_telegram.card_budget_usdt)")
    pk.add_argument("--config", default=None)
    pk.set_defaults(func=cmd_card)

    pm = sub.add_parser("market", help="обновить историю рынка и показать контекст/перегрев")
    pm.add_argument("--backfill", action="store_true", help="полный бэкфилл с backfill_start (~3 мин)")
    pm.add_argument("--config", default=None)
    pm.set_defaults(func=cmd_market)

    pq = sub.add_parser("quality", help="срез трека Q: дата, возраст, источник, число монет")
    pq.add_argument("--refresh-if-due", action="store_true",
                    help="срезу ≥ track_q.refresh_after_days (или его нет) — пересчитать "
                         "track_q.live_source (backtest/quality_screen.py, ~6 мин сети)")
    pq.add_argument("--config", default=None)
    pq.set_defaults(func=cmd_quality)

    def _keep(v: str) -> int:
        n = int(v)
        if n < 1:
            raise argparse.ArgumentTypeError("хранить нужно хотя бы одну копию")
        return n

    pbk = sub.add_parser("backup", help="бэкап scanner.db: копия, integrity_check, gzip, ротация")
    pbk.add_argument("--dir", default=None, help="каталог копий (по умолчанию backups/ в корне)")
    pbk.add_argument("--keep", type=_keep, default=14, help="сколько последних копий хранить")
    pbk.add_argument("--send-weekly", action="store_true",
                     help="раз в неделю (weekly_report_weekday) свежий .gz в Telegram без звука")
    pbk.add_argument("--notify", action="store_true",
                     help="разрешить отправку в Telegram (без него --send-weekly только пишет, "
                          "что пора)")
    pbk.add_argument("--test", action="store_true",
                     help="отправить сейчас с пометкой «🧪 ТЕСТ» (недельная отметка не "
                          "проверяется и не пишется); с --notify")
    pbk.add_argument("--config", default=None)
    pbk.set_defaults(func=cmd_backup)

    psy = sub.add_parser("sync", help="реальные позиции из спот-исполнений Bybit (ключ "
                                      "Read-Only в .env; нет ключа — пропуск)")
    psy.add_argument("--notify", action="store_true",
                     help="опасный ключ (право вывода, торговля) — сообщение со звуком")
    psy.add_argument("--config", default=None)
    psy.set_defaults(func=cmd_sync)

    pex = sub.add_parser("execute", help="пробный исполнитель: лестницы по сегодняшним "
                                         "карточкам в книги R/H, без ордеров (--dry-run)")
    pex.add_argument("--dry-run", action="store_true",
                     help="обязателен: реального режима пока нет (шаги 3–4)")
    pex.add_argument("--config", default=None)
    pex.set_defaults(func=cmd_execute)

    ph = sub.add_parser("halt", help="стоп-кран: исполнитель не ставит лестниц и не "
                                     "исполняет ордеров (снять — run.py resume)")
    ph.add_argument("reason", nargs="*", help="причина (попадёт в сводку дня)")
    ph.add_argument("--quiet", action="store_true", help="не сообщать в Telegram")
    ph.add_argument("--config", default=None)
    ph.set_defaults(func=cmd_halt)

    pre = sub.add_parser("resume", help="снять стоп-кран (только здесь, на сервере)")
    pre.add_argument("--quiet", action="store_true", help="не сообщать в Telegram")
    pre.add_argument("--config", default=None)
    pre.set_defaults(func=cmd_resume)

    pco = sub.add_parser("control", help="команды владельца из Telegram: /stop, /status "
                                         "(таймер scanner-control раз в 5 минут)")
    pco.add_argument("--status", action="store_true",
                     help="показать состояние в консоли, без Telegram")
    pco.add_argument("--config", default=None)
    pco.set_defaults(func=cmd_control)

    pki = sub.add_parser("killed", help="«⚠ прогон убит» в Telegram, если прогон не дошёл "
                                        "до сводки (trap daily_run.sh, OnFailure=)")
    pki.add_argument("--why", default="", help="причина: сигнал, systemd")
    pki.add_argument("--config", default=None)
    pki.set_defaults(func=cmd_killed)

    pt = sub.add_parser("selftest", help="офлайн-проверка на фикстурах")
    pt.set_defaults(func=cmd_selftest)

    pc = sub.add_parser("chat-id", help="показать chat_id из истории бота")
    pc.add_argument("--config", default=None)
    pc.set_defaults(func=cmd_chatid)

    pg = sub.add_parser("telegram-test", help="отправить тестовый алерт")
    pg.add_argument("--config", default=None)
    pg.set_defaults(func=cmd_tgtest)

    args = p.parse_args()
    # лог прогона — stdout в logs/: полный диск не должен ронять шаг посреди работы
    sys.stdout, sys.stderr = _SafeStream(sys.stdout), _SafeStream(sys.stderr)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
