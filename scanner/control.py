"""Управление исполнителем: стоп-кран и команды владельца из Telegram.

Стоп-кран — файл data/HALT (JSON: reason, ts, by). Пока он лежит, исполнитель не ставит
новых лестниц и не исполняет ордера (в пробном режиме — виртуальные), сводка дня начинается
с «⛔». Скан, позиции и сводки работают как обычно — данные не теряются.

Снять стоп-кран можно только с сервера: `run.py resume` (по SSH). Telegram умеет только
останавливать: угнанный аккаунт Telegram может остановить исполнителя, но не запустить его.
Файл, который не читается, считается стоп-краном: сомнение — в пользу остановки.

Команды владельца (`run.py control --poll`, таймер раз в 5 минут), только из чата
TELEGRAM_CHAT_ID:
  /stop [причина] — стоп-кран;
  /status         — стоп-кран, последний прогон, позиции, книги исполнителя;
  /help           — список команд.
Сообщения из чужих чатов не исполняются; владельцу уходит одно предупреждение на каждый
такой чат.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HALT_NAME = "HALT"
STATE_NAME = "control_state.json"

HELP = ("Команды:\n"
        "/stop [причина] — остановить исполнитель (новых лестниц и исполнений нет)\n"
        "/status — состояние: стоп-кран, последний прогон, позиции\n"
        "Снять остановку можно только на сервере: python3 run.py resume")


# ---------------------------------------------------------------- стоп-кран

def halt_path(base: Path | None = None) -> Path:
    return (base or DATA) / HALT_NAME


def halted(base: Path | None = None) -> dict | None:
    """None — исполнителю можно работать; иначе {reason, ts, by}. Нечитаемый файл — стоп."""
    p = halt_path(base)
    if not p.exists():
        return None
    try:
        info = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(info, dict):
            raise ValueError("не объект")
    except (OSError, ValueError) as e:
        return {"reason": f"файл {HALT_NAME} не читается ({type(e).__name__}) — считаю стоп-краном",
                "ts": p.stat().st_mtime if p.exists() else time.time(), "by": "?"}
    info.setdefault("reason", "без причины")
    info.setdefault("ts", p.stat().st_mtime)
    info.setdefault("by", "?")
    return info


def set_halt(reason: str, by: str, base: Path | None = None, now: float | None = None) -> dict:
    """Ставит стоп-кран (повторный вызов причину не затирает — первая остановка главнее)."""
    cur = halted(base)
    if cur:
        return cur
    p = halt_path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    info = {"reason": (reason or "без причины").strip()[:300], "ts": now or time.time(), "by": by}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return info


def clear_halt(base: Path | None = None) -> dict | None:
    """Снимает стоп-кран; возвращает то, что было снято (None — его не было)."""
    cur = halted(base)
    p = halt_path(base)
    if p.exists():
        p.unlink()
    return cur


def halt_line(info: dict | None) -> str:
    if not info:
        return ""
    when = time.strftime("%d.%m %H:%M", time.localtime(info.get("ts") or time.time()))
    return f"⛔ исполнитель остановлен с {when} ({info.get('by')}): {info.get('reason')}"


# ---------------------------------------------------------------- команды из Telegram

def _state(base: Path | None) -> dict:
    p = (base or DATA) / STATE_NAME
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(st: dict, base: Path | None) -> None:
    p = (base or DATA) / STATE_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def parse_updates(updates: list[dict], owner_chat: str) -> tuple[list[dict], list[dict], int | None]:
    """Разбор getUpdates: (команды владельца, сообщения из чужих чатов, последний update_id).
    Команда — {cmd, arg, update_id}; чужое — {chat_id, name, update_id}."""
    own, foreign, last = [], [], None
    for u in updates:
        uid = u.get("update_id")
        if isinstance(uid, int):
            last = uid if last is None else max(last, uid)
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        if chat.get("id") is None:
            continue
        if str(chat["id"]) != str(owner_chat):
            foreign.append({"chat_id": str(chat["id"]), "update_id": uid,
                            "name": chat.get("username") or chat.get("first_name") or ""})
            continue
        if not text.startswith("/"):
            continue
        head, _, arg = text.partition(" ")
        cmd = head.split("@", 1)[0].lower()      # /stop@Scannerb_bot -> /stop
        own.append({"cmd": cmd, "arg": arg.strip(), "update_id": uid})
    return own, foreign, last


def status_text(db_path: str, base: Path | None = None, now: float | None = None) -> str:
    """Коротко для /status: стоп-кран, последний прогон, позиции, книги исполнителя."""
    now = now or time.time()
    h = halted(base)
    lines = [halt_line(h) if h else "✅ исполнитель работает (стоп-крана нет)"]
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            r = con.execute("SELECT id, ts, finished_ts, n_watchlist FROM runs "
                            "ORDER BY id DESC LIMIT 1").fetchone()
            if r:
                ago = (now - (r[2] or r[1])) / 3600
                state = "завершён" if r[2] else "не завершён"
                lines.append(f"последний скан #{r[0]}: {state} {ago:.0f} ч назад, "
                             f"в наблюдении {r[3]}")
            p = con.execute("SELECT SUM(is_paper = 0), SUM(is_paper = 1) FROM positions "
                            "WHERE status = 'open'").fetchone()
            lines.append(f"позиции: 💰 реальных {p[0] or 0} · 📝 paper {p[1] or 0}")
            tabs = {t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "dry_positions" in tabs:
                for book, n, inv in con.execute(
                        "SELECT book, COUNT(*), SUM(invested) FROM dry_positions "
                        "WHERE status = 'open' AND COALESCE(shadow, 0) = 0 GROUP BY book"):
                    lines.append(f"книга {book}: открыто {n}, вложено ${inv or 0:.2f}")
        finally:
            con.close()
    except sqlite3.Error as e:
        lines.append(f"⚠ база не читается: {type(e).__name__}: {e}")
    return "\n".join(lines)


def poll(cfg, *, base: Path | None = None, now: float | None = None, fetch=None,
         send=None) -> tuple[int, list[str]]:
    """Один проход по входящим командам. fetch(offset) -> list[update] | None, send(text) — для
    тестов; по умолчанию Telegram Bot API. Код 0 — ок, 1 — Telegram не ответил."""
    from scanner.notify import telegram
    token = cfg.get("api_keys.telegram_token", "")
    owner = str(cfg.get("api_keys.telegram_chat_id", ""))
    if not token or not owner:
        return 0, ["нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID — команды не читаются"]
    st = _state(base)
    offset = st.get("offset")
    if fetch is None:
        def fetch(off):
            q = {"timeout": 0, "allowed_updates": json.dumps(["message", "edited_message"])}
            if off is not None:
                q["offset"] = off
            resp = telegram._post(token, "getUpdates", urllib.parse.urlencode(q).encode(),
                                  "application/x-www-form-urlencoded", timeout=20)
            return resp.get("result") if resp and resp.get("ok") else None
    if send is None:
        def send(text):
            return telegram.send_message(token, owner, text)
    updates = fetch(offset)
    if updates is None:
        return 1, ["Telegram getUpdates не ответил"]
    own, foreign, last = parse_updates(updates, owner)
    log = []
    db_path = cfg.get("output.db_path", "scanner.db")
    for c in own:
        if c["cmd"] == "/stop":
            info = set_halt(c["arg"] or "команда /stop из Telegram", "telegram", base, now)
            send(halt_line(info) + "\nСнять: python3 run.py resume (на сервере)")
            log.append(f"/stop → {info['reason']}")
        elif c["cmd"] == "/status":
            send(status_text(db_path, base, now))
            log.append("/status")
        elif c["cmd"] in ("/start", "/help"):
            send(HELP)
            log.append(c["cmd"])
        elif c["cmd"] in ("/resume", "/go", "/start_trading"):
            send("Снять остановку из Telegram нельзя — только на сервере: python3 run.py resume")
            log.append(f"{c['cmd']} → отказ (только с сервера)")
        else:
            send(f"Не знаю команду {c['cmd']}.\n{HELP}")
            log.append(f"{c['cmd']} → неизвестна")
    warned = set(st.get("warned_chats") or [])
    for f in foreign:
        if f["chat_id"] in warned:
            continue
        warned.add(f["chat_id"])
        send(f"⚠ Боту пишет чужой чат {f['chat_id']} {('@' + f['name']) if f['name'] else ''}"
             f" — команды из него не исполняются.")
        log.append(f"чужой чат {f['chat_id']} — предупредил")
    if last is not None:
        st["offset"] = last + 1                  # подтверждаем: Telegram их больше не отдаст
    st["warned_chats"] = sorted(warned)
    st["last_poll"] = now or time.time()
    _save_state(st, base)
    return 0, log or ["новых команд нет"]
