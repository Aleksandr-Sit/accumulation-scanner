"""Самопроверка ключа Bybit при каждом прогоне: права, привязка к IP, срок жизни.

`run.py sync` перед синхронизацией спрашивает у Bybit про свой же ключ (GET /v5/user/query-api,
scanner/bybit.check_key) и пишет итог в data/bybit_key.json; сводка дня показывает строку:
всё в порядке — коротко в подвале, предупреждение — «⚠» в шапке, опасность (право вывода,
ключ sync умеет торговать) — «⛔» в шапке и отдельное сообщение со звуком (`sync --notify`).
Проверка не блокирует sync: синхронизация только читает, а опасность — в самом ключе на
сервере, её снимает владелец на bybit.com.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import control

NAME = "bybit_key.json"
MAX_AGE_SEC = 36 * 3600          # старше — проверка не из сегодняшнего прогона, в сводку не идёт


def _path(base: Path | None) -> Path:
    return (base or control.DATA) / NAME


def run(cfg, *, client=None, base: Path | None = None, now: float | None = None) -> dict | None:
    """Проверить ключ sync -> итог (и записать его). None — ключа нет (шаг sync пропускается,
    проверять нечего; старый итог удаляется, чтобы сводка не показала его)."""
    from . import bybit
    now = now if now is not None else time.time()
    key, secret = cfg.get("api_keys.bybit_key", ""), cfg.get("api_keys.bybit_secret", "")
    if client is None and not (key and secret):
        _path(base).unlink(missing_ok=True)
        return None
    try:
        if client is None:
            client = bybit.Client(key, secret, cfg.get("bybit_sync.base_url", bybit.BASE_URL),
                                  recv_window=cfg.get("bybit_sync.recv_window_ms", 5000))
        res = bybit.check_key(client.api_key_info(), "sync",
                              cfg.get("bybit_sync.key_warn_days", 14))
    except (bybit.BybitError, OSError) as e:
        res = {"role": "sync", "level": "error", "issues": [f"не проверен: {e}"], "facts": []}
    res["ts"] = now
    p = _path(base)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:                     # диск: итог всё равно уходит в лог шага
        res.setdefault("issues", []).append(f"итог не записан: {e}")
    return res


def load(base: Path | None = None, now: float | None = None) -> dict | None:
    """Итог сегодняшней проверки или None (не было, устарел, не читается)."""
    now = now if now is not None else time.time()
    try:
        res = json.loads(_path(base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(res, dict) or now - (res.get("ts") or 0) > MAX_AGE_SEC:
        return None
    return res


def line(res: dict | None) -> str:
    """Строка для лога/сводки: «ключ Bybit: только чтение · вывода нет · …» или проблемы."""
    if not res:
        return ""
    lv = res.get("level")
    if lv == "ok":
        return "ключ Bybit: " + " · ".join(res.get("facts") or [])
    mark = {"danger": "⛔", "warn": "⚠", "error": "⚠"}.get(lv, "⚠")
    return f"{mark} ключ Bybit: " + "; ".join(res.get("issues") or ["?"])
