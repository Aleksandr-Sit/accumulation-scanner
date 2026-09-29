"""Загрузка config.json + переопределение секретов из окружения."""
from __future__ import annotations

import json
import os
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


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    _load_dotenv()
    p = Path(path) if path else _DEFAULT_PATH
    data = json.loads(p.read_text(encoding="utf-8"))

    # Секреты из окружения приоритетнее файла.
    keys = data.setdefault("api_keys", {})
    if os.getenv("COINGECKO_DEMO_KEY"):
        keys["coingecko_demo"] = os.environ["COINGECKO_DEMO_KEY"]
    if os.getenv("GOPLUS_KEY"):
        keys["goplus"] = os.environ["GOPLUS_KEY"]
    if os.getenv("DUNE_API_KEY"):
        keys["dune"] = os.environ["DUNE_API_KEY"]
    keys["telegram_token"] = os.getenv("TELEGRAM_BOT_TOKEN", "")
    keys["telegram_chat_id"] = os.getenv("TELEGRAM_CHAT_ID", "")

    return Config(data)
