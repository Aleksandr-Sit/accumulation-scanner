"""Сторож ежедневного прогона: дошёл ли он до конца или был убит посреди шагов.

scripts/daily_run.sh ведёт data/daily_run.state:
  start <epoch> <лог>        — в начале (прошлый файл сначала уходит в daily_run.prev)
  step <имя> <epoch>         — перед каждым шагом
  end <epoch> <код>          — после сводки дня
  signal <имя> <epoch>       — trap поймал TERM/INT/HUP
  alerted <epoch>            — `run.py killed` уже сообщил
Строки «end» нет — прогон оборвался на последнем «step»: таймаут systemd, OOM, перезагрузка,
ручная остановка. Сообщают трое, каждый раз не больше одного сообщения: trap в daily_run.sh
(TERM — таймаут или остановка), OnFailure= юнита (scanner-alert.service: SIGKILL, OOM) и
сводка следующего прогона по daily_run.prev (перезагрузка, когда не успел никто).
"""
from __future__ import annotations

import time
from pathlib import Path

from . import control

STATE = "daily_run.state"
PREV = "daily_run.prev"


def read(path: Path) -> dict | None:
    """-> {"start", "log", "steps": [(имя, ts)], "end": (ts, код) | None, "signal", "alerted"}
    или None (файла нет / пуст / нет строки start)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    st: dict = {"start": None, "log": "", "steps": [], "end": None, "signal": None,
                "alerted": False}
    for raw in text.splitlines():
        f = raw.split()
        if not f:
            continue
        try:
            if f[0] == "start":
                st["start"] = float(f[1])
                st["log"] = " ".join(f[2:])
            elif f[0] == "step":
                st["steps"].append((f[1], float(f[2])))
            elif f[0] == "end":
                st["end"] = (float(f[1]), int(f[2]) if len(f) > 2 else 0)
            elif f[0] == "signal":
                st["signal"] = f[1]
            elif f[0] == "alerted":
                st["alerted"] = True
        except (IndexError, ValueError):
            continue                          # обрезанная строка (диск, kill посреди записи)
    return st if st["start"] is not None else None


def _hm(ts: float) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts))


def killed_text(st: dict, now: float, why: str = "") -> str:
    step, ts = st["steps"][-1] if st["steps"] else ("до первого шага", st["start"])
    mins = (now - ts) / 60
    return (f"⚠ <b>Прогон сканера убит</b> — начат {_hm(st['start'])}, оборвался на шаге "
            f"<b>{step}</b> (шёл {mins:.0f} мин)" + (f": {why}" if why else "")
            + ". Сводки дня не будет; позиции и карточки, которые не успели, — в следующем "
              f"прогоне.\nЛог: <code>{st['log'] or 'logs/'}</code>")


def alert(cfg, *, base: Path | None = None, now: float | None = None, why: str = "",
          send=None) -> tuple[int, str]:
    """`run.py killed`: прогон не дошёл до «end» и о нём ещё не сообщали — сообщение со
    звуком и строка «alerted». Код 0 всегда (нечего сообщать — тоже 0), 1 — не отправилось."""
    from .notify import telegram
    now = now if now is not None else time.time()
    path = (base or control.DATA) / STATE
    st = read(path)
    if not st:
        return 0, "состояния прогона нет — сообщать не о чем"
    if st["end"]:
        return 0, "прогон дошёл до конца — сводка дня уже всё сказала"
    if st["alerted"]:
        return 0, "о прерванном прогоне уже сообщено"
    text = killed_text(st, now, why)
    if send is None:
        def send(t):
            return telegram.send_message(cfg.get("api_keys.telegram_token", ""),
                                         cfg.get("api_keys.telegram_chat_id", ""), t)
    ok = send(text)
    if ok:
        try:
            # файл, оборванный посреди строки (kill, диск), — отметка с новой строки, иначе
            # она прилипнет к обрывку и не прочитается: сообщение уйдёт ещё раз
            tail = path.read_bytes()[-1:]
            with open(path, "a", encoding="utf-8") as f:
                f.write(("" if tail in (b"", b"\n") else "\n") + f"alerted {now:.0f}\n")
        except OSError:
            pass                                    # повтор лучше, чем тишина
    return (0 if ok else 1), telegram.strip_html(text) + ("" if ok else " — НЕ отправлено")


def prev_line(base: Path | None = None) -> str:
    """Для сводки дня: прошлый прогон не дошёл до конца — пусто, если дошёл или его не было."""
    st = read((base or control.DATA) / PREV)
    if not st or st["end"]:
        return ""
    step = st["steps"][-1][0] if st["steps"] else "до первого шага"
    sig = f", сигнал {st['signal']}" if st["signal"] else ""
    return f"⚠ прошлый прогон ({_hm(st['start'])}) оборвался на шаге {step}{sig}"
