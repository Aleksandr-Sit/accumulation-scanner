"""Telegram (Stage 6). Zero-dep: urllib POST к Bot API.

Формат «гибрид» (03.10.2026):
  • карточка — СО звуком, только когда есть что сделать: новая монета у дна (уровни
    лестницы, стоп, цели, картинка) или сигнал выхода по реальной позиции;
  • сводка дня — БЕЗ звука, последним сообщением прогона: рынок, монеты у дна, позиции,
    статус прогона. Пришла сводка — прогон дошёл до конца; нет её — смотреть logs/.
Paper-позиции и информационные сигналы приходят без звука: руками делать ничего не надо.
Первая строка каждого сообщения — суть события (её видно в уведомлении).
Токен/chat_id — из .env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID), не в коде.
"""
from __future__ import annotations

import html
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

_API = "https://api.telegram.org/bot"
CAPTION_MAX = 1024              # лимит подписи к фото (видимый текст после разбора тегов)
TEXT_MAX = 4096


# ---------------------------------------------------------------- транспорт

def _post(token: str, method: str, data: bytes, content_type: str) -> dict | None:
    req = urllib.request.Request(f"{_API}{token}/{method}", data=data,
                                 headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:      # 400 и т.п.: тело с description нужно вызывающему
        try:
            return json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            print(f"[telegram] {method} fail: {e}")
            return None
    except Exception as e:  # noqa: BLE001 — сеть: логируем и не падаем
        print(f"[telegram] {method} fail: {e}")
        return None


def keyboard(buttons: list[list[tuple[str, str]]] | None) -> str | None:
    """[[(текст, url), …], …] -> reply_markup (кнопки-ссылки, бот-сервер не нужен)."""
    rows = [[{"text": t, "url": u} for t, u in row if t and u] for row in (buttons or [])]
    rows = [r for r in rows if r]
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False) if rows else None


def build_payload(chat_id: str, text: str, silent: bool = False,
                  buttons: list[list[tuple[str, str]]] | None = None,
                  html_mode: bool = True) -> dict:
    p = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    if html_mode:
        p["parse_mode"] = "HTML"
    if silent:
        p["disable_notification"] = "true"
    kb = keyboard(buttons)
    if kb:
        p["reply_markup"] = kb
    return p


def strip_html(text: str) -> str:
    """Видимый текст сообщения (для лимитов длины и запасной отправки без разметки)."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))


def _parse_error(resp: dict | None) -> bool:
    return bool(resp and not resp.get("ok")
                and "parse" in (resp.get("description") or "").lower())


def send_message(token: str, chat_id: str, text: str, silent: bool = False,
                 buttons: list[list[tuple[str, str]]] | None = None) -> bool:
    if not token or not chat_id:
        print("[telegram] нет token/chat_id — пропуск отправки")
        return False
    form = "application/x-www-form-urlencoded"
    resp = _post(token, "sendMessage",
                 urllib.parse.urlencode(build_payload(chat_id, text, silent, buttons)).encode(),
                 form)
    if _parse_error(resp):
        # битая разметка не должна съедать сообщение: шлём тем же текстом без тегов
        print(f"[telegram] HTML не принят ({resp.get('description')}) — отправляю без разметки")
        resp = _post(token, "sendMessage", urllib.parse.urlencode(build_payload(
            chat_id, strip_html(text), silent, buttons, html_mode=False)).encode(), form)
    if resp and not resp.get("ok"):
        print(f"[telegram] sendMessage: {resp.get('description')}")
    return bool(resp and resp.get("ok"))


def multipart_body(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]],
                   boundary: str) -> bytes:
    """multipart/form-data для sendPhoto (stdlib не умеет)."""
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'
                     f"{v}\r\n".encode("utf-8"))
    for k, (fname, data, ctype) in files.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                     f'filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n'.encode("utf-8")
                     + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts)


def send_photo(token: str, chat_id: str, png: bytes, caption: str, silent: bool = False,
               buttons: list[list[tuple[str, str]]] | None = None) -> bool:
    """Картинка с подписью (≤1024 видимых символов). Не ушла — та же подпись текстом."""
    if not token or not chat_id:
        print("[telegram] нет token/chat_id — пропуск отправки")
        return False
    fields = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    if silent:
        fields["disable_notification"] = "true"
    kb = keyboard(buttons)
    if kb:
        fields["reply_markup"] = kb
    boundary = uuid.uuid4().hex
    resp = _post(token, "sendPhoto",
                 multipart_body(fields, {"photo": ("levels.png", png, "image/png")}, boundary),
                 f"multipart/form-data; boundary={boundary}")
    if resp and resp.get("ok"):
        return True
    print(f"[telegram] sendPhoto не прошёл ({(resp or {}).get('description')}) — шлю текстом")
    return send_message(token, chat_id, caption, silent, buttons)


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


# ---------------------------------------------------------------- числа, ссылки

def fmt_price(p) -> str:
    """4 значащие цифры: 84 579 · 1503 · 150.3 · 1.498 · 0.0005862."""
    if not isinstance(p, (int, float)) or p != p:
        return "—"
    a = abs(p)
    if a == 0:
        return "0"
    if a >= 10000:
        return f"{p:,.0f}".replace(",", " ")
    dec = max(0, 3 - int(math.floor(math.log10(a))))
    return f"{p:.{dec}f}"


def fmt_score(x) -> str:
    """70.0 -> «70», 69.8 -> «69.8»: округление до целого рисовало «70» ниже порога 70."""
    return f"{x:.1f}".rstrip("0").rstrip(".") if isinstance(x, (int, float)) else "?"


def fmt_usd(x: float) -> str:
    return f"{'−' if x < 0 else '+'}${abs(x):.2f}"


def _signed(x: float, digits: int = 1) -> str:
    """«+19.6%» / «−0.3%» из процентов (не доли)."""
    return f"{x:+.{digits}f}%".replace("-", "−")


def _close_day(ts: float) -> str:
    """Точка CoinGecko 00:00 UTC — закрытие ПРЕДЫДУЩЕГО дня: подпись этим днём
    (как у свечей Bybit, где метка — начало дня)."""
    return _day_label(ts - 86400)


def _pct(x: float, digits: int = 0) -> str:
    """Доля -> «+12%» / «−34%» (типографский минус)."""
    s = f"{x * 100:+.{digits}f}%"
    return s.replace("-", "−")


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _day_label(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d.%m")


_STALE_DAYS = 2   # дневные данные старше — помечаем датой, а не выдаём за текущие


def _is_stale(ts, now: float | None = None) -> bool:
    return (isinstance(ts, (int, float))
            and (now if now is not None else time.time()) - ts > _STALE_DAYS * 86400)


def coin_links(symbol: str, venue: str = "", coin_id: str = "", chain: str = "",
               address: str = "") -> list[tuple[str, str]]:
    """Куда смотреть: график (TradingView по паре Bybit или DEXScreener), биржа, CoinGecko."""
    sym = (symbol or "").upper()
    out = []
    if venue == "Bybit spot" and sym:
        out.append(("📈 График", f"https://www.tradingview.com/chart/?symbol=BYBIT:{sym}USDT"))
        out.append(("🛒 Bybit", f"https://www.bybit.com/en/trade/spot/{sym}/USDT"))
    elif chain and address:
        out.append(("📈 DEXScreener", f"https://dexscreener.com/{chain}/{address}"))
    if coin_id:
        out.append(("🦎 CoinGecko", f"https://www.coingecko.com/en/coins/{coin_id}"))
    return out


def link_line(links: list[tuple[str, str]]) -> str:
    return " · ".join(f'<a href="{html.escape(u, quote=True)}">{_esc(t.split(" ", 1)[-1])}</a>'
                      for t, u in links)


# ---------------------------------------------------------------- рынок

# (подпись, формат значения/порога) для флагов перегрева — «F&G 68/70».
_FLAG_FMT = {
    "alt_vs_sma200": ("альты к SMA200", lambda v: f"{v * 100:+.0f}".replace("-", "−"), "%"),
    "mvrv_btc": ("MVRV BTC", lambda v: f"{v:.2f}", ""),
    "mvrv_eth": ("MVRV ETH", lambda v: f"{v:.2f}", ""),
    "fng30": ("F&G", lambda v: f"{v:.0f}", ""),
    "breadth200": ("ширина", lambda v: f"{v:.0f}", "%"),
    "fund30": ("фандинг BTC", lambda v: f"{v:.3f}", "%"),
    "oi_rel365": ("плечо BTC ×", lambda v: f"{v:.2f}", ""),
    "altbtc_chg90": ("альты/BTC 90д", lambda v: f"{v * 100:+.0f}".replace("-", "−"), "%"),
}


def _flag_text(name: str, ctx: dict, cfg) -> str:
    label, f, suf = _FLAG_FMT.get(name, (name, lambda v: f"{v:g}", ""))
    spec = (cfg.get("market_regime.hot_flags", {}) or {}).get(name) or [">=", None]
    v, thr = ctx.get(name), spec[1]
    if isinstance(v, (int, float)) and isinstance(thr, (int, float)):
        return f"{label}{'' if label.endswith('×') else ' '}{f(v)}/{f(thr)}{suf}"
    return label.rstrip(" ×")


def market_state(ctx: dict, cfg) -> dict:
    """Фаза цикла по просадке альт-рынка + «температура» по флагам перегрева.
    Границы фазы — те же, что в множителе пружины (stage4b_quality.alt_dd_from/full)."""
    q = cfg.get("stage4b_quality", {}) or {}
    add = ctx.get("alt_dd")
    full, frm = q.get("alt_dd_full", 0.65), q.get("alt_dd_from", 0.35)
    if not isinstance(add, (int, float)):
        phase = "фаза неизвестна"
    elif add >= full:
        phase = "дно цикла"
    elif add <= frm:
        phase = "близко к хаям"
    else:
        phase = "середина цикла"
    hot = ctx.get("hot") or {}
    lit, near, avail = hot.get("lit") or [], hot.get("near") or [], hot.get("avail") or 0
    alert = cfg.get("stage8_exit.market_hot_alert", 0.375)
    if lit and avail and len(lit) / avail >= alert:
        temp, emoji = "перегрет", "🔴"
    elif lit:
        temp, emoji = "тёплый", "🟠"
    elif near:
        temp, emoji = "теплеет", "🟡"
    else:
        temp, emoji = "спокойный", "🟢"
    return {"phase": phase, "temp": temp, "emoji": emoji, "lit": lit, "near": near,
            "avail": avail, "full": full}


def market_block(ctx: dict, cfg, now: float | None = None) -> list[str]:
    """3 строки о рынке: вердикт, просадка альтов со шкалой, перегрев с порогами."""
    if not ctx:
        return ["🌍 <b>Рынок: нет данных</b> (market_daily пуст или не обновился)"]
    s = market_state(ctx, cfg)
    out = [f"🌍 <b>Рынок: {s['phase']}, {s['temp']}</b> {s['emoji']}"]
    parts = []
    add = ctx.get("alt_dd")
    if isinstance(add, (int, float)):
        parts.append(f"Альты −{add * 100:.0f}% от пика (дно цикла — от −{s['full'] * 100:.0f}%)")
    bdd = ctx.get("btc_dd")
    if isinstance(bdd, (int, float)):
        parts.append(f"BTC −{bdd * 100:.0f}%")
    if parts:
        out.append(" · ".join(parts))
    if s["avail"]:
        hl = f"Перегрев {len(s['lit'])} из {s['avail']}"
        if s["lit"]:
            hl += " · горят: " + ", ".join(_flag_text(k, ctx, cfg) for k in s["lit"])
        if s["near"]:
            hl += " · у порога: " + ", ".join(_flag_text(k, ctx, cfg) for k in s["near"])
        out.append(_esc(hl))
    day = ctx.get("day")
    if _is_stale(day, now):
        out.append(f"⚠ данные рынка на {_day_label(day)} — обновление не удалось")
    return out


# ---------------------------------------------------------------- отбор под алерт

def select_picks(watchlist: list, cfg) -> list:
    """Кандидаты, которые реально попадут в карточки (фильтры stage6_telegram + лимит).
    Им же пишется mute: монета, которой не было в сообщении, не должна глушиться."""
    t = cfg["stage6_telegram"]
    zones = set(t.get("only_zones") or [])
    min_score = t.get("min_score", 0)
    min_conf = t.get("min_confidence", 0.0)
    picks = [c for c in watchlist
             if c.score >= min_score and c.confidence >= min_conf
             and (not zones or c.zone in zones)]
    return picks[:t.get("max_alerts", 10)]


# ---------------------------------------------------------------- карточка монеты

_ZONE_EMOJI = {"ПРУЖИНА/ДНО": "🟢", "СЕРЕДИНА": "🟡", "ПИК": "🔴", "ПАДАЮЩИЙ_НОЖ": "🔻"}
_ZONE_WORD = {"ПРУЖИНА/ДНО": "у дна", "СЕРЕДИНА": "в середине диапазона", "ПИК": "у пика",
              "ПАДАЮЩИЙ_НОЖ": "падает"}

_FLAG_RISK = {
    "mintable": "в контракте есть выпуск новых токенов (mint)",
    "transfer_pausable": "переводы токена можно остановить",
    "proxy": "контракт-прокси — логику можно поменять",
    "has_blacklist": "в контракте есть чёрный список",
    "lp_unlocked": "пул ликвидности не заблокирован",
    "wash_suspect": "объём, возможно, накручен",
    "rf_dex_only": "только DEX — покупать своим кошельком",
    "bybit_ticker_mismatch": "тикер на Bybit — другая монета (цена не совпала)",
    "dd_in_bull_market": "BTC растёт, а монета у дна — часто отставание, не недооценка",
    "solana:limited_antirug": "Solana — проверка контракта беднее",
}


def why_lines(c) -> list[str]:
    """«Почему у дна» человеческими словами из индикаторов зоны."""
    ind = getattr(c, "indicators", None) or {}
    out = []
    dsl, base = ind.get("days_since_low"), ind.get("base_len_days")
    if isinstance(dsl, int) and isinstance(base, int):
        out.append(f"{dsl} дн. без нового минимума, база у дна {base} дн.")
    vc = ind.get("vol_contraction")
    if isinstance(vc, (int, float)):
        out.append(f"волатильность сжалась до {vc:.2f} от прежней")
    vt = ind.get("vol_trend")
    if isinstance(vt, (int, float)):
        if vt <= 0.3:
            out.append(f"объём ×{vt:.2f} к прежнему — почти мёртвая тишина")
        elif vt <= 0.8:
            out.append(f"объём ×{vt:.2f} к прежнему — продавцы выдыхаются")
        elif vt >= 1.2:
            out.append(f"объём ×{vt:.2f} — растёт у дна (капитуляция или раздача?)")
    tr, rs = ind.get("trend_recent_pct"), ind.get("rs_vs_btc_pct")
    if isinstance(tr, (int, float)):
        out.append(f"за 30 дн. {tr:+.0f}%" + (f", к BTC {rs:+.0f} п.п." if isinstance(rs, (int, float))
                                               else ""))
    fr = getattr(c, "funding_rate", None)
    if isinstance(fr, (int, float)) and fr <= -0.0003:
        out.append(f"фандинг {fr * 100:.3f}%/8ч — толпа в шортах (топливо отскока)")
    if getattr(c, "us_tag", "") == "etf":
        out.append("🇺🇸 спотовый ETF в США")
    pf = getattr(c, "p_f", None)
    if isinstance(pf, (int, float)) and pf <= 15:
        out.append(f"P/F {pf:g} — протокол зарабатывает")
    dev = getattr(c, "dev_commits_4w", None)
    last = getattr(c, "dev_last_commit_days", None)
    if isinstance(dev, int) and dev >= 3:
        out.append(f"GitHub: {'20+' if dev >= 20 else dev} коммитов за 4 нед.")
    elif isinstance(last, int) and last <= 30:
        out.append(f"GitHub: последний коммит {last} дн. назад")
    return out


def risk_lines(c, cfg) -> list[str]:
    q = cfg.get("stage4b_quality", {}) or {}
    out = []
    add = getattr(c, "alt_market_dd", None)
    lit = getattr(c, "market_hot_lit", None) or []
    if lit:
        from ..regime import FLAG_LABELS
        out.append("🔥 рынок перегрет (" + ", ".join(FLAG_LABELS.get(k, k) for k in lit)
                   + ") — пружины исторически слабее")
    if isinstance(add, (int, float)) and add < q.get("alt_dd_full", 0.65):
        out.append(f"рынок не на дне (альты −{add * 100:.0f}%) — сигнал слабее")
    dd = getattr(c, "drawdown_from_ath_pct", None)
    if isinstance(dd, (int, float)) and dd >= q.get("dd_extreme_pct", 94):
        out.append(f"просадка {dd:.0f}% — риск обнуления")
    g = getattr(c, "supply_growth", None)
    if isinstance(g, (int, float)) and g >= 0.05:
        out.append(f"{'⚠ ' if g >= 0.25 else ''}эмиссия {g * 100:+.0f}% за полгода")
    fm = getattr(c, "fdv_mc", None)
    if isinstance(fm, (int, float)) and fm >= 3:
        out.append(f"FDV/капа {fm:.1f}× — навес будущих разлоков")
    dl = getattr(c, "delist", "")
    if dl:
        out.append({"spot": "🚫 Bybit снимает спот с торгов", "perp": "Bybit снимает перп",
                    "st": "⚠ метка ST на Bybit (риск делистинга)"}.get(dl, dl))
    oi = getattr(c, "oi_mcap", None)
    if isinstance(oi, (int, float)) and oi >= 0.2:
        out.append(f"плечо на монете: OI {oi * 100:.0f}% капы")
    fr = getattr(c, "funding_rate", None)
    if isinstance(fr, (int, float)) and fr >= 0.0005:
        out.append(f"фандинг +{fr * 100:.3f}%/8ч — лонги переплачивают у дна")
    flags = getattr(c, "flags", None) or []
    for n in getattr(c, "liveness_notes", None) or []:
        if n.startswith("⚠") and not ("только DEX" in n and "rf_dex_only" in flags):
            out.append(n.lstrip("⚠ ").strip())
    for f in flags:
        if f in _FLAG_RISK:
            out.append(_FLAG_RISK[f])
    if getattr(c, "manual_review", False):
        out.append("нужна ручная проверка (совпадение по тикеру)")
    return out


def _ladder_table(plan: dict, P, cfg) -> list[str]:
    """Моноширинная таблица: ступени, стоп, цели (≤ 34 символов — влезает в телефон)."""
    rows = []
    w = max(len(P(b["price"])) for b in plan["buys"])
    for i, b in enumerate(plan["buys"]):
        head = "Купить" if i == 0 else ""
        note = "рынком" if b["type"] == "рынок" else _pct(b["from_price"])
        rows.append(f"{head:<6} {P(b['price']):>{w}}  {note:<7}${b['usd']:.0f}")
    confirm = int(cfg.get("stage8_exit.invalidation_confirm_days", 1) or 1)
    rows.append(f"{'Стоп':<6} {P(plan['stop_px']):>{w}}  "
                + (f"{confirm} закрытия ниже" if confirm > 1 else "закрытие ниже"))
    first = True
    for s in plan["sells"]:
        if s["price"] is None:
            e = cfg["stage8_exit"]
            rows.append(f"{'':<6} {'':>{w}}  остаток — трейл {e['trailing_from_hwm_pct']}%")
            continue
        frac = s["qty"] / plan["qty"] if plan.get("qty") else 0
        share = "⅓" if abs(frac - 1 / 3) < 0.04 else f"{frac * 100:.0f}%"
        rows.append(f"{'Цели' if first else '':<6} {P(s['price']):>{w}}  {s['label']} → {share}")
        first = False
    return rows


_SECTIONS = ("why", "risks")


def format_coin_card(c, plan: dict | None, cfg, *, price: float | None = None,
                     price_src: str = "", P=None, paper: dict | None = None,
                     with_links: bool = False, test: bool = False,
                     sections: tuple[str, ...] = _SECTIONS) -> str:
    """Карточка новой монеты у дна: суть → лестница/стоп/цели → свёрнутые «почему/риски»
    → paper-пометка. sections — какие свёрнутые блоки включать (подпись к фото ограничена
    1024 символами: лишнее снимается по одному, см. card_caption). Уровней графика (узлы
    объёма, VWAP, SMA200, хаи) нет намеренно: backtest/levels_study.py — не лучше placebo."""
    P = P or fmt_price
    sym = _esc(c.symbol or c.name or "?")
    venue = {"Bybit spot": "Bybit", "DEX only": "DEX"}.get(c.rf_venue, c.rf_venue or "")
    if c.zone:
        head = (f"{'🧪 ТЕСТ · ' if test else ''}{_ZONE_EMOJI.get(c.zone, '•')} "
                f"<b>{sym} {_ZONE_WORD.get(c.zone, c.zone)} — {fmt_score(c.score)}/100</b>")
    else:                       # run.py card по монете вне watchlist: зоны и балла нет
        head = f"{'🧪 ТЕСТ · ' if test else ''}📐 <b>{sym} — план лестницы</b>"
    head += f" · {venue}" if venue else ""
    lines = [head]
    sub = []
    if c.name and c.name.upper() != (c.symbol or "").upper():
        sub.append(_esc(c.name))
    px = price or getattr(c, "price_usd", None)
    if px:
        sub.append(f"цена {P(px)}" + (f" ({price_src})" if price_src else ""))
    if isinstance(c.drawdown_from_ath_pct, (int, float)):
        sub.append(f"от пика −{c.drawdown_from_ath_pct:.0f}%")
    if c.track == "Q":
        sub.append("без контракта · фильтр качества")
    if sub:
        lines.append(" · ".join(sub))
    if plan and plan.get("ok"):
        lines.append("<pre>" + _esc("\n".join(_ladder_table(plan, P, cfg))) + "</pre>")
        n = len(plan["buys"])
        lines.append(f"<i>Цели — от средней {P(plan['avg'])} (все {n} ступени); "
                     f"после части — от своей средней.</i>")
    elif plan and plan.get("error"):
        lines.append(f"⚠ Лестница не построена: {_esc(plan['error'])}")
    blocks = []
    why = why_lines(c) if "why" in sections else []
    if why:
        blocks.append("<b>Почему у дна</b>\n" + "\n".join(f"• {_esc(x)}" for x in why))
    risks = risk_lines(c, cfg) if "risks" in sections else []
    if risks:
        blocks.append("<b>Риски</b>\n" + "\n".join(f"• {_esc(x)}" for x in risks))
    if sections:
        for w in (plan or {}).get("warns") or []:
            blocks.append(f"⚠ {_esc(w)}")
    if blocks:
        lines.append("<blockquote expandable>" + "\n".join(blocks) + "</blockquote>")
    if paper:
        lines.append(f"📝 paper-позиция ${paper.get('stake', 100):.0f} по "
                     f"{P(paper['entry_price'])} — тест правил, без действий")
    if with_links:
        links = coin_links(c.symbol, c.rf_venue, c.coin_id, c.chain, c.address)
        if links:
            lines.append(link_line(links))
    lines.append("<i>Уровни — правила сканера, не рекомендация.</i>")
    return "\n".join(lines)


def card_caption(c, plan, cfg, **kw) -> str:
    """Подпись к картинке (≤1024 видимых символов): полная карточка; не влезает — без
    «Почему», в крайнем случае — без свёрнутого блока вовсе. Риски снимаются последними."""
    text = ""
    for sec in (_SECTIONS, ("risks",), ()):
        text = format_coin_card(c, plan, cfg, sections=sec, **kw)
        if len(strip_html(text)) <= CAPTION_MAX:
            return text
    return text


# ---------------------------------------------------------------- карточка выхода

_SIG_HEAD = {
    "invalidation": ("🚨", "выйти полностью"),
    "trailing": ("🚨", "зафиксировать остаток"),
    "market_hot": ("🔥", "рынок перегрет — рассмотреть фиксацию"),
    "peak_zone": ("ℹ️", "зона распределения — рассмотреть фиксацию"),
}
_URG = {"high": 3, "medium": 2, "low": 1}


def _sell_qty(s: dict, pos: dict, cfg) -> float | None:
    """Сколько продать по сигналу (как paper-исполнитель): весь остаток или долю лестницы."""
    if s["type"] in ("invalidation", "trailing"):
        return pos.get("qty")
    if s["type"].startswith("ladder_"):
        try:
            frac = cfg["stage8_exit"]["ladder"][int(s["type"].split("_")[1])][1]
        except (IndexError, ValueError, KeyError):
            return None
        return frac * (pos.get("initial_qty") or pos.get("qty") or 0)
    return None


def format_exit_card(row: dict, cfg, *, test: bool = False) -> tuple[str, bool]:
    """(текст, со_звуком). Звук — только реальная позиция и сигнал high/medium: paper
    исполняется виртуально, информационные сигналы ничего не требуют сделать."""
    p = row["position"]
    sigs = sorted(row.get("signals") or [], key=lambda s: -_URG.get(s.get("urgency"), 0))
    if not sigs:
        return "", False
    sym = _esc(p["symbol"])
    paper = bool(p.get("is_paper"))
    top = sigs[0]
    if top["type"].startswith("ladder_"):
        em, act = "🟠", top["action"].lower()           # «зафиксировать 33%»
    else:
        em, act = _SIG_HEAD.get(top["type"], ("•", top["action"].lower()))
    lines = [f"{'🧪 ТЕСТ · ' if test else ''}{em} <b>{'📝 ' if paper else ''}{sym} — "
             f"{_esc(act)}</b>"]
    pnl = row.get("pnl_at_signal") or row.get("pnl") or {}
    price = row.get("last_price")
    when = f" ({_close_day(row['last_ts'])})" if isinstance(row.get("last_ts"), (int, float)) else ""
    lines.append(f"Закрытие {fmt_price(price)}{when} · {_signed(pnl.get('pnl_pct', 0))} "
                 f"({fmt_usd(pnl.get('pnl_usdt', 0))}) · {row.get('held_days', '?')} дн.")
    for s in sigs:
        lines.append(f"• {_esc(s['note'])}")
        q = _sell_qty(s, p, cfg)
        if q and not paper:
            whole = s["type"] in ("invalidation", "trailing")
            lines.append(f"  Продать: {'весь остаток ' if whole else ''}{q:.6g} {sym}"
                         + (f" ≈ ${q * price:.2f}" if price else ""))
    for ex in row.get("executed") or []:
        lines.append(f"📝 paper: исполнено виртуально — {_esc(ex)} USDT")
    if not paper:
        lines.append("<i>Сигнал — для ручного решения, ордер не выставлен.</i>")
    loud = (not paper) and any(_URG.get(s.get("urgency"), 0) >= 2 for s in sigs)
    return "\n".join(lines), loud


# ---------------------------------------------------------------- сводка дня

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


def _position_lines(r: dict, cfg, now: float) -> list[str]:
    p, pnl = r["position"], r.get("pnl") or {}
    tag = "📝" if p.get("is_paper") else "💰"
    sym = _esc(p["symbol"])
    if p.get("status") == "closed":
        return [f"{tag} {sym} закрыта по сигналу · итог {fmt_usd(r.get('realized_usdt') or 0.0)}"]
    price = r.get("last_price")
    spark = r.get("spark_prices") or []
    sp = f"  <code>{sparkline(spark, 12)}</code>" if len(spark) >= 7 else ""
    l1 = f"{tag} <b>{sym}</b> {_signed(pnl.get('pnl_pct', 0))} · {r.get('held_days', '?')} дн.{sp}"
    e = cfg["stage8_exit"]
    bits = [f"{fmt_price(p['entry_price'])} → {fmt_price(price)}"]
    bl = p.get("base_low")
    inv = e["invalidation_below_base_low_pct"] / 100.0
    if isinstance(bl, (int, float)) and bl > 0 and price:
        stop = bl * (1 - inv)
        bits.append(f"стоп {fmt_price(stop)} ({_pct(stop / price - 1)})")
    else:
        bits.append("⚠ без стопа")
    trig = r.get("triggered") or set()
    for i, (level, _frac) in enumerate(e["ladder"]):
        if f"ladder_{i}" not in trig and price:
            tgt = p["entry_price"] * (1 + level)
            bits.append(f"цель {fmt_price(tgt)} ({_pct(tgt / price - 1)})")
            break
    if _is_stale(r.get("last_ts"), now):
        bits.append(f"⚠ цена на {_close_day(r['last_ts'])}")
    return [l1, "   " + " · ".join(bits)]


def format_brief(state: dict, cfg, *, now: float | None = None, test: bool = False) -> str:
    """Сводка дня — тихо, последним сообщением прогона. state собирает run.py brief
    (без сети): статус прогона, рынок, монеты у дна, позиции, сигналы дня."""
    now = now if now is not None else time.time()
    t = cfg.get("stage6_telegram", {}) or {}
    scan = state.get("scan") or {}
    new, muted, near = state.get("new") or [], state.get("muted") or [], state.get("near") or []
    rows = state.get("positions") or []
    sigs = state.get("signals_today") or []
    day = datetime.fromtimestamp(now).strftime("%d.%m")

    head = [f"{'🧪 ТЕСТ · ' if test else ''}☀️ {day}"]
    has_scan = bool(scan.get("ok"))
    if scan.get("ok") is False:
        head.append("⚠ скан не завершился")
    elif not scan.get("ran"):
        head.append("скана сегодня не было")
    if has_scan:
        head.append(f"🟢 у дна: {len(new)}" if new else "у дна новых нет")
    head.append(f"позиций: {len([r for r in rows if r['position'].get('status') != 'closed'])}")
    head.append(f"🔔 сигналов: {len(sigs)}" if sigs else "сигналов нет")
    if state.get("watch_ok") is False:
        head.append("⚠ позиции не обновлены")
    lines = ["<b>" + " · ".join(head) + "</b>", ""]
    lines += market_block(state.get("market") or {}, cfg, now)
    lines.append("")

    min_score = t.get("min_score", 0)
    lines.append("🎯 <b>Монеты у дна</b>")
    if has_scan:
        if new:
            lines += [f"{_ZONE_EMOJI.get(c.zone, '•')} {_esc(c.symbol)} {fmt_score(c.score)} — "
                      f"карточка выше" for c in new]
        if muted:
            lines.append("уже приходили: " + ", ".join(f"{_esc(c.symbol)} {fmt_score(c.score)}"
                                                       for c in muted))
        if near:
            lines.append(f"ниже порога {min_score}: " + " · ".join(
                f"{_esc(c.symbol)} {fmt_score(c.score)}" for c in near))
        if not (new or muted or near):
            lines.append("в зоне дна сейчас никого")
    else:
        lines.append("нет свежих данных скана")
    if scan.get("error"):
        lines.append(f"⚠ {_esc(scan['error'])}")
    lines.append("")

    open_rows = [r for r in rows if r.get("pnl")]
    if open_rows:
        ts = [r["last_ts"] for r in open_rows if isinstance(r.get("last_ts"), (int, float))]
        lines.append("💼 <b>Позиции</b>" + (f" · закрытие {_close_day(max(ts))}" if ts else ""))
        ordered = sorted(open_rows, key=lambda r: (r["position"].get("is_paper", 0),
                                                   -(r["pnl"] or {}).get("pnl_pct", 0)))
        cap = t.get("digest_max_positions", 20)
        for r in ordered[:cap]:
            lines += _position_lines(r, cfg, now)
        if len(ordered) > cap:
            lines.append(f"…и ещё {len(ordered) - cap}")
        real = sum((r["pnl"] or {}).get("pnl_usdt", 0) + (r.get("realized_usdt") or 0)
                   for r in open_rows if not r["position"].get("is_paper"))
        paper = sum((r["pnl"] or {}).get("pnl_usdt", 0) + (r.get("realized_usdt") or 0)
                    for r in open_rows if r["position"].get("is_paper"))
        lines.append(f"Итого: 💰 real {fmt_usd(real)} · 📝 paper {fmt_usd(paper)}")
        lines.append("")
    errs = [r for r in rows if not r.get("pnl") and r.get("error")]
    if errs:
        lines.append(f"⚠ без цены ({len(errs)}): "
                     + ", ".join(_esc(r["position"]["symbol"]) for r in errs[:10]))
        lines.append("")
    if sigs:
        lines.append("🔔 <b>Сигналы сегодня</b> — карточки выше")
        lines.append(", ".join(f"{'📝' if s.get('is_paper') else '💰'}{_esc(s['symbol'])} "
                               f"{_esc(s['label'])}" for s in sigs))
        lines.append("")

    foot = []
    if scan.get("elapsed_min") is not None:
        foot.append(f"скан {scan['elapsed_min']:.0f} мин")
    if scan.get("watchlist") is not None:
        foot.append(f"в наблюдении {scan['watchlist']}")
    if state.get("dev_github"):
        foot.append(f"GitHub {state['dev_github']}")
    if "onchain" in (state.get("unavailable") or []):
        foot.append("on-chain недоступен")
    if foot:
        lines.append("<i>" + _esc(" · ".join(foot)) + "</i>")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------- недельная сводка, сбой

def benchmark_block(books: list[dict]) -> list[str]:
    """«📊 Против рынка»: книга против альтов и BTC на тех же окнах (scanner/benchmark.py,
    weekly_books). Позиций нет ни в одной книге — пустой список (блок не выводится)."""
    books = [b for b in books or [] if b.get("positions")]
    if not books:
        return []
    out = ["📊 <b>Против рынка</b> (те же даты входа и выхода):"]
    first = True
    for b in books:
        head = f"{b.get('emoji', '•')} {_esc(b.get('label', ''))}:"
        if not b.get("n"):
            out.append(f"{head} нет цены или рынка на даты позиций ({b['positions']} поз.)")
            continue
        cnt = (f"{b['n']} поз." if b["n"] == b["positions"]
               else f"{b['n']} из {b['positions']} поз.")
        # разница — из показанных (округлённых) чисел: «+30.7 · альты +36.7 → −6.0», не −5.9
        shown = [float(f"{b[k]:.1f}") for k in ("book_pct", "alt_pct")]
        pp = f"{shown[0] - shown[1]:+.1f}".replace("-", "−")
        line = (f"{head} {_signed(b['book_pct'])} · альты {_signed(b['alt_pct'])} · "
                f"BTC {_signed(b['btc_pct'])} → {pp} п.п.{' к альтам' if first else ''} ({cnt})")
        if b.get("stale"):
            line += f" · ⚠ рынок на {_day_label(b['market_day'])}"
        out.append(line)
        first = False
    out.append("<i>Отбор полезен, только если обгоняет альты: рост рынка — не его заслуга.</i>")
    return out


def format_weekly(stats: dict, cfg) -> str:
    """Недельная сводка форвард-теста + счётчик недели, напоминание, 4-нед. итог."""
    wk = stats.get("week_no", 0)
    wtag = f" · неделя {wk}" if wk else ""
    lines = [f"<b>🧪 Paper-статистика за неделю</b>{wtag} — {datetime.now().date().isoformat()}", ""]
    if not wk:
        lines.append(f"Paper ещё не стартовал: не было пружин с баллом ≥ "
                     f"{cfg.get('stage7_positions.paper_min_score', 70)}.")
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
    bench = benchmark_block(stats.get("benchmark") or [])
    if bench:
        lines.append("")
        lines += bench

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


def format_failure(step: str, error: str) -> str:
    """Шаг ежедневного прогона упал исключением — сообщаем, а не молчим."""
    return (f"<b>⚠ Accumulation {html.escape(step)}</b> — {datetime.now().date().isoformat()} · "
            f"прогон упал\n<code>{html.escape(error[:500])}</code>\n"
            f"<i>Подробности — logs/daily_*.log.</i>")


def weekly_due(last_ts: float | None, now: float, weekday: int = 0) -> bool:
    """Пора ли недельной сводке: с начала текущей недели (последний `weekday` 00:00
    по локальному времени, 0 = понедельник) сводки ещё не было. Ноутбук был выключен
    в этот день — уйдёт в первый прогон после."""
    d = datetime.fromtimestamp(now)
    start = (d - timedelta(days=(d.weekday() - weekday) % 7)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return last_ts is None or last_ts < start.timestamp()
