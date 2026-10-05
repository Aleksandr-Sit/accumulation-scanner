"""Сборка и отправка сообщений «гибрида»: карточки (сеть: Bybit/CoinGecko) и сводка дня
(без сети — из scanner.db и watchlist.json). Тексты — в telegram.py, картинка — chart.py.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from . import chart, telegram
from ..ladder import fmt_step, plan_ladder
from ..sources import bybit, coingecko

_SIGNAL_LABEL = {"invalidation": "стоп", "trailing": "трейл", "peak_zone": "зона распределения",
                 "market_hot": "перегрев рынка"}


def _creds(cfg) -> tuple[str, str]:
    return cfg.get("api_keys.telegram_token", ""), cfg.get("api_keys.telegram_chat_id", "")


def day_start(now: float | None = None) -> float:
    """Начало сегодняшнего дня по локальному времени (граница «сегодня» для сводки)."""
    d = datetime.fromtimestamp(now if now is not None else time.time())
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _cg_series(http, coin_id: str, days: int, demo: str) -> dict:
    """Закрытия CoinGecko как «свечи» без хаёв/лоёв (h = l = c): для DEX-монет."""
    ch = coingecko.closed_daily(coingecko.fetch_market_chart(http, coin_id, days, demo))
    c = ch.get("prices") or []
    return {"ts": [int(t) for t in ch.get("ts") or []], "c": c, "h": c, "l": c,
            "qv": [v or 0.0 for v in ch.get("volumes") or []]}


def market_data(cfg, http, symbol: str, venue: str, coin_id: str, ref_price: float | None,
                days: int = 400) -> dict:
    """Цена, правила пары и свечи для уровней. Bybit — если монета там и цена сходится с
    CoinGecko (одинаковый тикер ≠ та же монета), иначе закрытия CoinGecko."""
    out = {"price": None, "src": "", "inst": None, "ohlcv": {}, "bybit": False}
    if venue == "Bybit spot" and symbol:
        pair = f"{symbol.upper()}USDT"
        inst = bybit.fetch_instrument(http, pair)
        last = bybit.fetch_last_price(http, pair) if inst else None
        if inst and last and bybit.same_coin(last, ref_price) is not False:
            out.update(price=last, src="Bybit", inst=inst, bybit=True,
                       ohlcv=bybit.fetch_daily_ohlcv(http, pair, days))
    if not (out["ohlcv"].get("c")) and coin_id:
        out["ohlcv"] = _cg_series(http, coin_id, min(days, 365),
                                  cfg.get("api_keys.coingecko_demo", ""))
    if out["price"] is None:
        c = out["ohlcv"].get("c") or []
        out["price"] = ref_price or (c[-1] if c else None)
        out["src"] = "CoinGecko" if ref_price or c else ""
    return out


def coin_card(cfg, http, c, *, paper: dict | None = None, test: bool = False) -> dict:
    """Карточка новой монеты у дна: {caption, text, png, buttons, plan}."""
    t = cfg.get("stage6_telegram", {}) or {}
    e = cfg["stage8_exit"]
    md = market_data(cfg, http, c.symbol, c.rf_venue, c.coin_id, getattr(c, "price_usd", None))
    inst, ohlcv, price = md["inst"] or {}, md["ohlcv"], md["price"]
    tick = inst.get("tick") or 0.0
    P = (lambda x: fmt_step(x, tick)) if tick else telegram.fmt_price
    closes = ohlcv.get("c") or []
    plan = None
    if price and len(closes) >= 30:
        plan = plan_ladder(price, min(closes[-30:]), t.get("card_budget_usdt", 40),
                           steps=t.get("card_steps", 4), min_order=t.get("card_min_order_usdt", 10),
                           tick=tick, qty_step=inst.get("qty_step") or 0.0,
                           exch_min_amt=inst.get("min_amt") or 0.0,
                           exch_min_qty=inst.get("min_qty") or 0.0,
                           floor_pct=e["invalidation_below_base_low_pct"], sell="prod",
                           prod_levels=e["ladder"],
                           worst_case_pct=cfg.get("stage7_positions.worst_case_loss_pct", 60))
    kw = dict(price=price, price_src=md["src"], P=P, paper=paper, test=test)
    links = telegram.coin_links(c.symbol, c.rf_venue if md["bybit"] or c.rf_venue != "Bybit spot"
                                else "", c.coin_id, c.chain, c.address)
    png = None
    if t.get("charts", True) and plan and plan.get("ok"):
        sym = (c.symbol or "").upper()
        dd = (f" · от пика −{c.drawdown_from_ath_pct:.0f}%"
              if isinstance(c.drawdown_from_ath_pct, (int, float)) else "")
        last_day = (datetime.fromtimestamp(ohlcv["ts"][-1], timezone.utc).strftime("%d.%m")
                    if ohlcv.get("ts") else "")
        png = chart.render_levels(
            ohlcv, title=(f"{sym}/USDT · Bybit · 1D · 180 дней" if md["bybit"]
                          else f"{sym} · CoinGecko · закрытия · 180 дней"),
            subtitle=f"закрытие {last_day}: {P(closes[-1])}{dd} · балл {telegram.fmt_score(c.score)}",
            buys=[b["price"] for b in plan["buys"]], stop=plan["stop_px"],
            targets=[(s["price"], s["label"]) for s in plan["sells"] if s["price"]], fmt=P)
    return {"caption": telegram.card_caption(c, plan, cfg, **kw),
            "text": telegram.format_coin_card(c, plan, cfg, **kw),
            "png": png, "buttons": [links] if links else None, "plan": plan}


def send_card(cfg, card: dict, silent: bool = False) -> bool:
    token, chat = _creds(cfg)
    if card.get("png"):
        return telegram.send_photo(token, chat, card["png"], card["caption"], silent,
                                   card.get("buttons"))
    return telegram.send_message(token, chat, card["text"], silent, card.get("buttons"))


def exit_card(cfg, http, row: dict, *, test: bool = False) -> dict:
    """Карточка сигнала выхода: {text, png, buttons, loud}. Картинка — вход, стоп, цели."""
    text, loud = telegram.format_exit_card(row, cfg, test=test)
    p = row["position"]
    links = telegram.coin_links(p["symbol"], p.get("venue", ""), p.get("coin_id", ""),
                                p.get("chain", ""), p.get("address", ""))
    png = None
    if text and cfg.get("stage6_telegram.charts", True) and chart.available():
        md = market_data(cfg, http, p["symbol"], p.get("venue", ""), p.get("coin_id", ""),
                         row.get("last_price"))
        e = cfg["stage8_exit"]
        inv = e["invalidation_below_base_low_pct"]
        if p.get("variant") == "S":
            inv = cfg.get("stage7_positions.paper_ab_stop_pct", inv)
        bl = p.get("base_low")
        trig = row.get("triggered") or set()
        tg = [(p["entry_price"] * (1 + lv), f"+{lv * 100:.0f}%")
              for i, (lv, _f) in enumerate(e["ladder"]) if f"ladder_{i}" not in trig]
        extra = []
        hwm = row.get("hwm") or 0
        if hwm and hwm / p["entry_price"] - 1 >= e["trailing_arm_after_gain_pct"] / 100:
            tr = hwm * (1 - e["trailing_from_hwm_pct"] / 100)
            extra.append((tr, f"трейл · {telegram.fmt_price(tr)}", "trail"))
        png = chart.render_levels(md["ohlcv"], title=f"{p['symbol']} · позиция "
                                  f"{'paper' if p.get('is_paper') else 'real'}",
                                  subtitle=f"вход {telegram.fmt_price(p['entry_price'])} · "
                                           f"{row.get('held_days', '?')} дн.",
                                  stop=bl * (1 - inv / 100) if bl else None, targets=tg,
                                  entry=p["entry_price"], extra=extra)
    return {"text": text, "png": png, "buttons": [links] if links else None, "loud": loud}


def send_exit_card(cfg, card: dict) -> bool:
    token, chat = _creds(cfg)
    silent = not card.get("loud")
    if card.get("png") and len(telegram.strip_html(card["text"])) <= telegram.CAPTION_MAX:
        return telegram.send_photo(token, chat, card["png"], card["text"], silent,
                                   card.get("buttons"))
    return telegram.send_message(token, chat, card["text"], silent, card.get("buttons"))


# ---------------------------------------------------------------- сводка дня

def brief_state(cfg, *, now: float | None = None, scan_exit: int | None = None,
                watch_exit: int | None = None, backup_exit: int | None = None,
                sync_exit: int | None = None) -> dict:
    """Всё для сводки дня — из scanner.db, watchlist.json и среза трека Q, без сети."""
    from .. import regime
    from ..backup import EXIT_SEND_FAILED
    from ..db import Store
    from ..pipeline import load_watchlist
    from ..positions import PositionStore
    from ..quality import fresh_slice
    from ..stages.exit import held_days, position_pnl

    now = now if now is not None else time.time()
    t0 = day_start(now)
    t = cfg.get("stage6_telegram", {}) or {}
    db = cfg["output"]["db_path"]
    store = Store(db)
    run = store.last_run()
    ran_today = bool(run and run["ts"] >= t0)
    # n_watchlist пишет только finish_run — завершённость видна и у прогонов до finished_ts
    finished = bool(ran_today and (run.get("finished_ts") or run.get("n_watchlist") is not None))
    summ = (run or {}).get("summary") or {}
    scan = {"ran": ran_today,
            "ok": (scan_exit == 0 and finished) if scan_exit is not None else
                  (finished if ran_today else None),
            "elapsed_min": summ["elapsed_sec"] / 60 if finished and summ.get("elapsed_sec") else None,
            "watchlist": run.get("n_watchlist") if finished else None}
    if scan_exit is not None and not finished:
        scan["error"] = "скан упал или не дошёл до конца — смотреть logs/"
    elif scan_exit is None and not ran_today:
        scan["error"] = "сегодня скана не было"

    # watchlist только от сегодняшнего завершённого скана: вчерашний выдал бы старые монеты
    # за сегодняшние
    wl = load_watchlist(cfg["output"]["watchlist_json"]) if finished else []
    ctx = regime.market_context(store.market_rows(), cfg)
    if ctx:
        ctx["btc_dd"] = summ.get("btc_dd") or next((c.market_dd for c in wl
                                                    if c.market_dd is not None), None)
    sent = store.alerts_since(t0)
    recent = store.recent_alerts(t.get("alert_mute_days", 3))
    store.close()

    picks = telegram.select_picks(wl, cfg)
    new = [c for c in picks if c.symbol in sent]
    muted = [c for c in picks if c.symbol not in sent and c.symbol in recent]
    zones = set(t.get("only_zones") or [])
    shown = {c.symbol for c in new + muted}
    near = [c for c in wl if (not zones or c.zone in zones) and c.symbol not in shown
            and c.score < t.get("min_score", 0)][:t.get("near_threshold_n", 3)]

    ps = PositionStore(db)
    snaps = ps.last_snapshots()
    rows = []
    for p in ps.open_positions() + ps.closed_since(t0):
        if (p.get("variant") or "A") != "A":
            continue                                  # близнецы A/B — только в report
        s = snaps.get(p["id"])
        if p["status"] == "closed":
            rows.append({"position": p, "pnl": {"pnl_pct": 0.0, "pnl_usdt": 0.0},
                         "realized_usdt": p.get("realized_usdt") or 0.0})
            continue
        if not s:
            rows.append({"position": p, "pnl": None, "error": "нет цены (watch ещё не считал)"})
            continue
        hist =[px for ts_, px in ps.snapshot_prices(p["id"]) if ts_ >= p["entry_ts"] - 86400]
        rows.append({"position": p, "last_price": s["price"],
                     "last_ts": (s["ts"] // 86400) * 86400,   # последнее закрытие 00:00 UTC
                     "hwm": s.get("hwm"), "pnl": position_pnl(p, s["price"]),
                     "realized_usdt": p.get("realized_usdt") or 0.0,
                     "held_days": held_days(p, now), "triggered": ps.event_types(p["id"]),
                     "spark_prices": hist, "snap_ts": s["ts"]})
    sigs = []
    for ev in ps.events_since(t0):
        if ev["position_id"] == 0 or (ev.get("variant") or "A") != "A":
            continue
        typ = ev["type"]
        if typ.startswith("ladder_"):
            i = int(typ.split("_")[1])
            lad = cfg["stage8_exit"]["ladder"]
            lab = f"фикс {lad[i][1] * 100:.0f}%" if i < len(lad) else typ
        elif typ in _SIGNAL_LABEL:
            lab = _SIGNAL_LABEL[typ]
        else:
            continue
        sigs.append({"symbol": ev["symbol"], "label": lab, "is_paper": ev.get("is_paper")})
    ps.close_db()

    open_rows = [r for r in rows if r["position"]["status"] == "open"]
    if watch_exit is not None:
        watch_ok = watch_exit == 0
    else:
        watch_ok = (all((r.get("snap_ts") or 0) >= t0 for r in open_rows) if open_rows else None)
    # бэкап: код шага из daily_run (нет кода — шаг не запускали, молчим)
    backup = None
    if backup_exit:
        backup = "send_fail" if backup_exit == EXIT_SEND_FAILED else "fail"
    track_q = None
    if cfg.get("track_q.enabled", False):
        s = fresh_slice(cfg, now)
        track_q = {"ok": s["ok"], "date": s["date"], "age_days": s["age_days"]}
    return {"scan": scan, "watch_ok": watch_ok, "market": ctx, "new": new, "muted": muted,
            "near": near, "positions": rows, "signals_today": sigs,
            "unavailable": summ.get("unavailable") or [], "dev_github": summ.get("dev_github"),
            "backup": backup, "track_q": track_q,
            # sync с Bybit: код шага из daily_run; ключа нет — шаг выходит с 0, пометки нет
            "sync_fail": bool(sync_exit)}
