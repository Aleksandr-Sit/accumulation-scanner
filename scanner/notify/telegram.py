"""Telegram-алерты (Stage 6). Zero-dep: urllib POST к Bot API.

Отправляет топ кандидатов зоны дна с высоким баллом. Токен/chat_id — из .env
(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID), не в коде.
"""
from __future__ import annotations

import html
import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

_API = "https://api.telegram.org/bot"


def _post(token: str, method: str, payload: dict) -> dict | None:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(f"{_API}{token}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — сеть/HTTP, любую ошибку логируем и не падаем
        print(f"[telegram] {method} fail: {e}")
        return None


def get_chat_ids(token: str) -> list[dict]:
    """Достаёт chat_id из истории бота (getUpdates). Пусто = никто не писал боту."""
    try:
        with urllib.request.urlopen(f"{_API}{token}/getUpdates", timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[telegram] getUpdates fail: {e}")
        return []
    seen: dict[int, dict] = {}
    for u in d.get("result", []):
        msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
        ch = msg.get("chat") or {}
        if ch.get("id") is not None and ch["id"] not in seen:
            seen[ch["id"]] = ch
    return list(seen.values())


def send_message(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        print("[telegram] нет token/chat_id — пропуск отправки")
        return False
    resp = _post(token, "sendMessage", {
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    })
    return bool(resp and resp.get("ok"))


_ZONE_EMOJI = {"ПРУЖИНА/ДНО": "🟢", "СЕРЕДИНА": "🟡", "ПИК": "🔴", "ПАДАЮЩИЙ_НОЖ": "🔻"}


def _market_zone_label(mdd: float) -> str:
    """Ярлык зоны рынка по бакетам feature_study: >0.55 дно(74%), 0.30-0.55
    средняя(40%), <0.15 хаи(22%)."""
    if mdd >= 0.55:
        return "рынок на дне"
    if mdd <= 0.15:
        return "BTC у хаёв"
    return "средняя зона"


_STALE_DAYS = 2   # дневные данные старше — помечаем датой, а не выдаём за текущие


def _day_label(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d.%m")


def _is_stale(ts, now: float | None = None) -> bool:
    return (isinstance(ts, (int, float))
            and (now if now is not None else time.time()) - ts > _STALE_DAYS * 86400)


def format_market_ctx(ctx: dict, now: float | None = None) -> str:
    """Строка рыночного контекста для шапки алерта.

    Новый формат (summary["market_ctx"] из market_daily): «альты −42% · BTC −33% ·
    перегрев 0/8 (близко: …) · F&G · BTC.D». Старый (только /global + market_dd) —
    фолбэк, если таблица рынка пуста. Обновление market_daily не удалось и последний
    день старый — помечаем «⚠ рынок на DD.MM».
    """
    from ..regime import context_line
    if "alt_dd" in ctx or "hot" in ctx:
        line = context_line(ctx)
        day = ctx.get("day")
        if line and _is_stale(day, now):
            line += f" · ⚠ рынок на {_day_label(day)}"
        return line
    parts = []
    mdd = ctx.get("market_dd")
    if isinstance(mdd, (int, float)):
        lbl = _market_zone_label(mdd)
        parts.append(f"BTC −{mdd*100:.0f}% ({lbl})")
    bd = ctx.get("btc_dominance_pct")
    if isinstance(bd, (int, float)):
        parts.append(f"BTC.D {bd:.0f}%")
    t2 = ctx.get("total2_mcap_usd")
    if isinstance(t2, (int, float)):
        parts.append(f"альты ${t2/1e9:.0f}B")
    return " · ".join(parts)


def coin_line(c) -> str:
    """Монетный контекст одной строкой (информационный): делистинг, US, P/F, эмиссия, плечо."""
    out = []
    dl = getattr(c, "delist", "")
    if dl:
        out.append({"spot": "🚫делистинг Bybit", "perp": "⚠перп снимают",
                    "st": "⚠ST на Bybit"}.get(dl, dl))
    us = getattr(c, "us_tag", "")
    if us == "etf":
        out.append("🇺🇸ETF")
    elif us == "coinbase":
        out.append("Coinbase")
    pf = getattr(c, "p_f", None)
    if isinstance(pf, (int, float)):
        out.append(f"P/F {pf:g}")
    g = getattr(c, "supply_growth", None)
    if isinstance(g, (int, float)) and abs(g) >= 0.05:
        out.append(f"{'⚠' if g >= 0.25 else ''}эмиссия {g*100:+.0f}%")
    oi = getattr(c, "oi_mcap", None)
    if isinstance(oi, (int, float)) and oi >= 0.2:
        out.append(f"⚠OI {oi*100:.0f}% капы")
    return " · ".join(out)


def select_picks(watchlist: list, cfg) -> list:
    """Кандидаты, которые реально попадут в алерт (фильтры stage6_telegram + лимит).
    Им же пишется mute: монета, которой не было в сообщении, не должна глушиться."""
    t = cfg["stage6_telegram"]
    zones = set(t.get("only_zones") or [])
    min_score = t.get("min_score", 0)
    min_conf = t.get("min_confidence", 0.0)
    picks = [c for c in watchlist
             if c.score >= min_score and c.confidence >= min_conf
             and (not zones or c.zone in zones)]
    return picks[:t.get("max_alerts", 10)]


def format_alert(watchlist: list, cfg, market_ctx: dict | None = None) -> str | None:
    """Формирует HTML-сообщение из топ кандидатов по фильтрам stage6_telegram."""
    min_score = cfg["stage6_telegram"].get("min_score", 0)
    picks = select_picks(watchlist, cfg)
    if not picks:
        return None

    lines = [f"<b>🛰 Accumulation scan</b> — {date.today().isoformat()}"]
    if market_ctx:
        mc = format_market_ctx(market_ctx)
        if mc:
            lines.append(f"🌍 {mc}")
    lines += [f"Кандидаты зоны дна (score ≥ {min_score}):", ""]
    for i, c in enumerate(picks, 1):
        emoji = _ZONE_EMOJI.get(c.zone, "•")
        sym = html.escape(c.symbol or c.name or "?")
        val = next((f for f in c.flags if "valued" in f), c.category or "")
        dd = f"DD {c.drawdown_from_ath_pct:.0f}%" if c.drawdown_from_ath_pct else ""
        mr = " ⚠️проверить" if c.manual_review else ""
        q = "без контракта · гейт качества" if c.track == "Q" else ""
        parts = [p for p in (c.zone, c.rf_venue, dd, val, q) if p]
        # живость: dev-активность + LP + флаги риска
        live = []
        if c.liveness_score is not None:
            live.append(f"жив {c.liveness_score:.0f}/10")
        if c.dev_commits_4w is not None:
            live.append(f"{c.dev_commits_4w} коммитов/4нед")
        if "lp_unlocked" in c.flags:
            live.append("⚠LP разблок")
        if "wash_suspect" in c.flags:
            live.append("⚠объём накручен")
        nf = getattr(c, "net_flow_usd_7d", None)
        if isinstance(nf, (int, float)) and abs(nf) >= 1e5:
            vol = c.volume_24h or 0
            ratio = f" ({nf/(vol*7)*100:+.0f}% оборота)" if vol > 0 else ""
            live.append(f"{'отток' if nf < 0 else 'приток'} бирж ${abs(nf)/1e6:.1f}M/7д{ratio}")
        lines.append(f"{i}. {emoji} <b>{sym}</b>  <b>{c.score}</b> (conf {c.confidence})")
        lines.append(f"    {' · '.join(parts)}{mr}")
        if live:
            lines.append(f"    {' · '.join(live)}")
        coin = coin_line(c)
        if coin:
            lines.append(f"    {coin}")
        # качество пружины + контекст рынка (feature_study)
        q = getattr(c, "spring_quality", None)
        mdd = getattr(c, "market_dd", None)
        if q is not None or isinstance(mdd, (int, float)):
            ctx = []
            if q is not None:
                ctx.append(f"качество ×{q:.2f}")
            add = getattr(c, "alt_market_dd", None)
            if isinstance(add, (int, float)):
                ctx.append(f"альты −{add*100:.0f}%")
            if isinstance(mdd, (int, float)):
                ctx.append(f"BTC −{mdd*100:.0f}% ({_market_zone_label(mdd)})")
            hs = getattr(c, "market_hot_score", None)
            if isinstance(hs, (int, float)) and hs > 0:
                ctx.append(f"🔥 перегрев {len(getattr(c, 'market_hot_lit', []) or [])} фл.")
            fr = getattr(c, "funding_rate", None)
            if isinstance(fr, (int, float)) and abs(fr) >= 0.0003:
                ctx.append(f"фандинг {fr*100:+.3f}%/8h")
            lines.append(f"    {' · '.join(ctx)}")
    lines.append("")
    lines.append("<i>Балл — приоритизация, не сигнал входа. on-chain/сентимент не учтены (free).</i>")
    return "\n".join(lines)


_URGENCY_EMOJI = {"high": "🚨", "medium": "🟠", "low": "ℹ️"}
_SPARK = "▁▂▃▄▅▆▇█"


def sparkline(prices: list[float], width: int = 16) -> str:
    """Мини-график из блочных символов (zero-dep альтернатива PNG).

    Даунсэмплинг до width точек; нормировка min-max по переданному окну.
    """
    pts = [p for p in prices if isinstance(p, (int, float))]
    if len(pts) < 2:
        return "·" * 2
    if len(pts) > width:
        step = len(pts) / width
        pts = [pts[int(i * step)] for i in range(width - 1)] + [pts[-1]]
    lo, hi = min(pts), max(pts)
    if hi <= lo:
        return _SPARK[3] * len(pts)
    return "".join(_SPARK[min(7, int((p - lo) / (hi - lo) * 7.999))] for p in pts)


def _dist_pct(target: float, price: float) -> str:
    return f"{(target / price - 1) * 100:+.0f}%"


def _close_label(rows: list[dict]) -> list[str]:
    """Строка «цены — закрытие 00:00 UTC DD.MM»: watch считает по последнему ЗАКРЫТОМУ
    дневному закрытию CoinGecko, а не по текущей цене — иначе цифры путают с биржей."""
    ts = [r["last_ts"] for r in rows if isinstance(r.get("last_ts"), (int, float))]
    if not ts:
        return []
    return [f"<i>цены — дневное закрытие {_day_label(max(ts))} 00:00 UTC, не текущие</i>"]


def format_digest(rows: list[dict], cfg) -> str | None:
    """Ежедневный дайджест ВСЕХ позиций (не только с сигналами): вход → текущая,
    P&L, спарклайн с момента входа, расстояния до инвалидации и уровней лестницы.
    Пагинация: не больше digest_max_positions строк (лимит Telegram 4096)."""
    ok = [r for r in rows if r.get("pnl")]
    errs = [r for r in rows if not r.get("pnl") and r.get("error")]
    if not ok and not errs:
        return None
    t = cfg.get("stage6_telegram", {}) or {}
    cap = t.get("digest_max_positions", 20)
    e = cfg["stage8_exit"]
    inv_pct = e["invalidation_below_base_low_pct"] / 100.0
    lines = [f"<b>📊 Позиции</b> — {date.today().isoformat()}"]
    lines += _close_label(ok)
    lines.append("")
    total_real = total_paper = 0.0
    ordered = sorted(ok, key=lambda x: (x["position"].get("is_paper", 0),
                                        -x["pnl"]["pnl_pct"]))
    for r in ordered[:cap]:
        p, pnl = r["position"], r["pnl"]
        tag = "📝" if p.get("is_paper") else "💰"
        sym = html.escape(p["symbol"])
        if p.get("status") == "closed":
            # paper-позиция закрыта исполнением сигнала в этом прогоне: P&L остатка
            # (qty=0) был бы «+0.0%» — показываем итог сделки.
            lines.append(f"{tag} <b>{sym}</b>  закрыта по сигналу · realized "
                         f"{r.get('realized_usdt') or 0.0:+.2f} USDT")
            continue
        spark = sparkline(r.get("spark_prices") or [])
        lines.append(f"{tag} <b>{sym}</b>  {pnl['pnl_pct']:+.1f}% "
                     f"({r.get('held_days', '?')}д)  <code>{spark}</code>")
        price, hwm = r["last_price"], r["hwm"]
        dd_hwm = (1 - price / hwm) * 100 if hwm > 0 else 0.0
        old = (f" (⚠ закрытие {_day_label(r['last_ts'])}, свежих цен нет)"
               if _is_stale(r.get("last_ts")) else "")
        lines.append(f"   {p['entry_price']:.6g} → {price:.6g}{old} · "
                     f"hwm {hwm:.6g} (−{dd_hwm:.0f}%)")
        dists = []
        bl = p.get("base_low")
        if isinstance(bl, (int, float)) and bl > 0:
            dists.append(f"инвалидация {_dist_pct(bl * (1 - inv_pct), price)}")
        else:
            dists.append("⚠ без стопа (base_low не задан)")
        for i, (level, frac) in enumerate(e["ladder"]):
            if f"ladder_{i}" not in (r.get("triggered") or set()):
                dists.append(f"фикс {frac*100:.0f}% на +{level*100:.0f}%: "
                             f"{_dist_pct(p['entry_price'] * (1 + level), price)}")
                break  # показываем только ближайший недостигнутый уровень
        if dists:
            lines.append(f"   {' · '.join(dists)}")
        nf = r.get("net_flow_usd_7d")
        if isinstance(nf, (int, float)) and abs(nf) >= 5e5:
            arrow = "📥 приток на биржи" if nf > 0 else "📤 отток с бирж"
            tail = " — возможно распределение" if nf > 0 else ""
            lines.append(f"   {arrow} ${abs(nf)/1e6:.1f}M/7д{tail}")
    # суммы считаем по ВСЕМ позициям (не только показанным), включая realized
    for r in ok:
        realized = r.get("realized_usdt") or 0.0
        total = r["pnl"]["pnl_usdt"] + realized
        if r["position"].get("is_paper"):
            total_paper += total
        else:
            total_real += total
    if len(ok) > cap:
        lines.append(f"   …и ещё {len(ok) - cap} позиций (показаны топ-{cap})")
    if errs:
        esym = ", ".join(html.escape(r["position"]["symbol"]) for r in errs[:10])
        lines.append(f"⚠ без данных цены ({len(errs)}): {esym}")
    lines.append("")
    lines.append(f"Σ 💰 real: {total_real:+.2f} · 📝 paper: {total_paper:+.2f} USDT "
                 f"(unrealized+realized)")
    lines.append("<i>P&L net-of-fees. Спарклайн — с момента входа.</i>")
    return "\n".join(lines)


def format_weekly(stats: dict, cfg) -> str:
    """Недельная сводка форвард-теста + счётчик недели, напоминание, 4-нед. итог."""
    wk = stats.get("week_no", 0)
    wtag = f" · неделя {wk}" if wk else ""
    lines = [f"<b>🧪 Paper-статистика за неделю</b>{wtag} — {date.today().isoformat()}", ""]
    lines.append(f"Открыто новых: {stats['opened']} · сейчас открыто: {stats['open_now']} "
                 f"· закрыто по сигналам: {stats.get('paper_closed', 0)}")
    lines.append(f"Сигналы: инвалидаций {stats['invalidations']}, "
                 f"уровней лестницы {stats['ladder_hits']}, трейлингов {stats['trailings']}")
    lines.append(f"📝 paper P&L: unrealized {stats['paper_pnl_usdt']:+.2f} · "
                 f"realized {stats.get('paper_realized_usdt', 0.0):+.2f} USDT")
    ab = stats.get("ab") or {}
    if ab.get("pairs"):
        lines.append(f"🅰🅱 выход при перегреве: A (обычный трейл) {ab['a_usdt']:+.2f} · "
                     f"B (сужение) {ab['b_usdt']:+.2f} USDT на {ab['pairs']} парах; "
                     f"разошлись {ab.get('diverged', 0)}, B лучше в {ab.get('b_better', 0)}")
    abs_ = stats.get("ab_stop") or {}
    if abs_.get("pairs"):
        lines.append(f"🅰🆂 ширина стопа (монеты фильтра качества): "
                     f"A −{stats.get('stop_pct', 25):g}% {abs_['a_usdt']:+.2f} · "
                     f"S −{stats.get('ab_stop_pct', 50):g}% {abs_['b_usdt']:+.2f} USDT "
                     f"на {abs_['pairs']} парах; стоп сработал A {abs_.get('a_stopped', 0)} / "
                     f"S {abs_.get('b_stopped', 0)}, S лучше в {abs_.get('b_better', 0)} из "
                     f"{abs_.get('diverged', 0)} разошедшихся")
    if stats.get("real_open") or abs(stats.get("real_realized_usdt", 0.0)) > 1e-9:
        lines.append(f"💰 real: открыто {stats['real_open']}, unrealized "
                     f"{stats['real_pnl_usdt']:+.2f} · realized "
                     f"{stats.get('real_realized_usdt', 0.0):+.2f} USDT")

    # Одноразовая итоговая сводка на N-й неделе — с кумулятивом и call-to-decide.
    if stats.get("milestone"):
        mw = stats.get("milestone_weeks", 4)
        lines.append("")
        lines.append(f"<b>🏁 ИТОГОВАЯ за {mw} недели наблюдения</b>")
        lines.append(f"Всего открыто пружин: {stats.get('cum_opened', 0)}")
        lines.append(f"Инвалидаций: {stats.get('cum_invalidations', 0)} · "
                     f"достигло уровней лестницы: {stats.get('cum_ladder_hits', 0)} · "
                     f"трейлингов: {stats.get('cum_trailings', 0)}")
        lines.append("")
        lines.append("<i>📌 Пора решать по реальному капиталу: пересмотри журнал "
                     "(pos list --all), выстави capital_usdt в config, если запускаешь "
                     "живые деньги. Напомню: истинная P(+100%) ≈45%, профиль лотерейный "
                     "— размер позиции критичен.</i>")
    else:
        lines.append("")
        left = max(0, stats.get("milestone_weeks", 4) - wk) if wk else None
        remind = "📌 Просмотри статистику."
        if left:
            remind += f" До итоговой сводки: {left} нед."
        lines.append(f"<i>{remind} Это форвард-тест стратегии на живом рынке.</i>")
    return "\n".join(lines)


def format_exit_alert(rows: list[dict], cfg) -> str | None:
    """HTML-сообщение по НОВЫМ exit-сигналам прогона watch. Нет сигналов -> None."""
    hot = [r for r in rows if r.get("signals")]
    if not hot:
        return None
    lines = [f"<b>📤 Exit watch</b> — {date.today().isoformat()}"]
    lines += _close_label(hot)
    lines.append("")
    for r in hot:
        p = r["position"]
        # P&L на момент сигнала: после paper-исполнения остаток может быть 0 шт.,
        # и пост-фактум P&L показал бы «+0.0%» у позиции, выбитой стопом.
        pnl = r.get("pnl_at_signal") or r.get("pnl") or {}
        sym = html.escape(p["symbol"])
        tag = "📝 " if p.get("is_paper") else "💰 "
        lines.append(f"{tag}<b>{sym}</b>  {pnl.get('pnl_pct', 0):+.1f}% "
                     f"({pnl.get('pnl_usdt', 0):+.2f} USDT, {r.get('held_days', '?')}д)")
        for s in r["signals"]:
            em = _URGENCY_EMOJI.get(s.get("urgency", "low"), "•")
            lines.append(f"  {em} <b>{html.escape(s['action'])}</b>")
            lines.append(f"     {html.escape(s['note'])}")
        for ex in r.get("executed") or []:
            lines.append(f"  ⚙ paper: {html.escape(ex)} USDT")
        lines.append("")
    lines.append("<i>Сигнал — алерт для ручного решения, не ордер. Издержки учтены в P&L.</i>")
    return "\n".join(lines)


def notify(watchlist: list, cfg, market_ctx: dict | None = None) -> bool:
    token = cfg.get("api_keys.telegram_token", "")
    chat_id = cfg.get("api_keys.telegram_chat_id", "")
    text = format_alert(watchlist, cfg, market_ctx)
    if not text:
        print("[telegram] нет кандидатов под критерии алерта")
        return False
    ok = send_message(token, chat_id, text)
    print(f"[telegram] отправка: {'ok' if ok else 'fail'}")
    return ok
