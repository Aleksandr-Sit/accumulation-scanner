"""Загрузка config.json + переопределение секретов из окружения."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.json"
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_dotenv() -> None:
    """Простейший загрузчик .env (zero-dep): KEY=VALUE, без экспорта поверх уже заданных."""
    if not _ENV_PATH.exists():
        return
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key and key not in os.environ:
            os.environ[key] = val


class Config:
    def __init__(self, data: dict[str, Any]):
        self._d = data

    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def get(self, path: str, default: Any = None) -> Any:
        """Достаёт вложенное значение по 'a.b.c'."""
        cur: Any = self._d
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


# api_keys.<имя> <- переменная окружения (.env). Других источников ключей нет.
ENV_KEYS = {"coingecko_demo": "COINGECKO_DEMO_KEY", "goplus": "GOPLUS_KEY",
            "dune": "DUNE_API_KEY", "telegram_token": "TELEGRAM_BOT_TOKEN",
            "telegram_chat_id": "TELEGRAM_CHAT_ID", "telegram_owner_id": "TELEGRAM_OWNER_ID",
            "bybit_key": "BYBIT_API_KEY", "bybit_secret": "BYBIT_API_SECRET"}


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    _load_dotenv()
    p = Path(path) if path else _DEFAULT_PATH
    data = json.loads(p.read_text(encoding="utf-8"))

    # Ключи — только из окружения (.env, права 600): репозиторий публичный, ключ, вписанный
    # в config.json, уйдёт в git с первым же коммитом. Непустое значение из файла не
    # используется — предупреждение в лог и в сводку дня (_config_warnings).
    keys = data.setdefault("api_keys", {})
    leaked = sorted(k for k, v in keys.items()
                    if not k.startswith("_") and isinstance(v, str) and v.strip())
    if leaked:
        warn = (f"config.json: в api_keys есть ключи ({', '.join(leaked)}) — не использую; "
                f"ключи только в .env, а из config.json и истории git их убрать")
        data["_config_warnings"] = [warn]
        print(f"[config] ⚠ {warn}", file=sys.stderr)
    for name, env in ENV_KEYS.items():
        keys[name] = os.getenv(env, "")

    return Config(data)
