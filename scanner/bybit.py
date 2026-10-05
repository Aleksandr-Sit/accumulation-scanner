"""Bybit V5: подписанные GET-запросы только на чтение (шаг 1 автоматизации — синхронизация).

Подпись — как в официальной доке (docs/v5/guide, «Create A Request») и примере
bybit-exchange/api-usage-examples V5_demo/api_demo/Encryption_HMAC.py:
  строка = timestamp(мс) + api_key + recv_window + queryString  (GET; у POST — тело JSON)
  X-BAPI-SIGN = HMAC-SHA256(secret, строка) в нижнем hex.
Заголовки: X-BAPI-API-KEY, X-BAPI-TIMESTAMP, X-BAPI-SIGN, X-BAPI-SIGN-TYPE=2 (HMAC),
X-BAPI-RECV-WINDOW. Timestamp должен лежать в [server_time − recv_window; server_time + 1000) —
часы сервера под NTP. queryString подписывается ровно в том виде, в каком уходит в URL.

Здесь только чтение: /v5/execution/list (исполнения, окно ≤ 7 дней за запрос, история 2 года,
страницы — nextPageCursor) и /v5/account/wallet-balance (accountType=UNIFIED). Ордеров нет:
ключ создаётся с правами Read-Only. Кэша нет — приватные ответы на диск не пишутся.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

BASE_URL = "https://api.bybit.com"
DEMO_URL = "https://api-demo.bybit.com"
WEEK_MS = 7 * 86400 * 1000
MAX_HISTORY_MS = 730 * 86400 * 1000      # /v5/execution/list хранит 2 года

# Частые коды ошибок — подсказка в лог (сверено с docs/v5/error 05.10.2026).
_EXPIRED = "срок ключа истёк (ключ без привязки к IP живёт 90 дней) — создай новый"
HINTS = {
    10002: "время запроса вне recv_window — проверь NTP на сервере (timedatectl)",
    10003: "ключ не принят — опечатка в BYBIT_API_KEY или ключ от другого домена "
           "(демо-ключ работает только с api-demo.bybit.com)",
    10004: "подпись не сошлась — проверь BYBIT_API_SECRET (без пробелов и кавычек)",
    10005: "у ключа нет прав на этот запрос — нужны права Read-Only",
    10006: "лимит запросов — повтор в следующий прогон",
    10010: "IP не в списке ключа — запрос не с того сервера, что привязан к ключу",
    -2015: _EXPIRED,
    33004: _EXPIRED,
}


class BybitError(RuntimeError):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


def sign(secret: str, ts_ms: str, api_key: str, recv_window: str, payload: str) -> str:
    """HMAC-SHA256 подписи V5 в нижнем hex (payload — queryString для GET)."""
    return hmac.new(secret.encode("utf-8"), (ts_ms + api_key + recv_window + payload)
                    .encode("utf-8"), hashlib.sha256).hexdigest()


def _urllib_fetch(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""


class Client:
    """fetch(url, headers, timeout) -> (http-код, тело) подменяется в selftest (без сети);
    clock() -> секунды — тоже."""

    def __init__(self, api_key: str, api_secret: str, base_url: str = BASE_URL,
                 recv_window: int = 5000, timeout: float = 20.0,
                 fetch: Callable[..., tuple[int, bytes]] | None = None,
                 clock: Callable[[], float] = time.time, pause: float = 0.2):
        if not api_key or not api_secret:
            raise BybitError("нет ключа: BYBIT_API_KEY и BYBIT_API_SECRET в .env")
        self.api_key, self._secret = api_key, api_secret
        self.base_url = base_url.rstrip("/")
        self.recv_window = str(int(recv_window))
        self.timeout = timeout
        self._fetch = fetch or _urllib_fetch
        self._clock = clock
        self._pause = pause                 # между страницами: лимиты V5 щедрые, но не дёргаем

    def headers(self, query: str, ts_ms: str) -> dict[str, str]:
        return {"X-BAPI-API-KEY": self.api_key, "X-BAPI-TIMESTAMP": ts_ms,
                "X-BAPI-SIGN": sign(self._secret, ts_ms, self.api_key, self.recv_window, query),
                "X-BAPI-SIGN-TYPE": "2", "X-BAPI-RECV-WINDOW": self.recv_window,
                "User-Agent": "accumulation-scanner/sync"}

    def get(self, path: str, params: dict[str, Any]) -> dict:
        """Подписанный GET -> result. retCode ≠ 0, не-JSON или HTTP-ошибка — BybitError
        (секрет и подпись в текст ошибки не попадают)."""
        query = urllib.parse.urlencode([(k, str(v)) for k, v in params.items()
                                        if v is not None and v != ""])
        ts_ms = str(int(self._clock() * 1000))
        status, body = self._fetch(f"{self.base_url}{path}?{query}", self.headers(query, ts_ms),
                                   self.timeout)
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise BybitError(f"{path}: HTTP {status}, ответ не JSON") from None
        code = data.get("retCode")
        if status != 200 or code != 0:
            hint = HINTS.get(code) if isinstance(code, int) else None
            raise BybitError(f"{path}: HTTP {status}, retCode {code} {data.get('retMsg', '')!r}"
                             + (f" — {hint}" if hint else ""), code)
        return data.get("result") or {}

    def wallet_balance(self) -> dict[str, dict[str, float]]:
        """Ненулевые монеты единого счёта -> {COIN: {"balance", "usd", "locked"}}."""
        res = self.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        out: dict[str, dict[str, float]] = {}
        for acc in res.get("list") or []:
            for c in acc.get("coin") or []:
                out[str(c.get("coin", "")).upper()] = {
                    "balance": _num(c.get("walletBalance")), "usd": _num(c.get("usdValue")),
                    "locked": _num(c.get("locked"))}
        return out

    def executions(self, start_ms: int, end_ms: int, category: str = "spot",
                   limit: int = 100) -> list[dict]:
        """Все исполнения category за [start_ms, end_ms]: окна по 7 дней (предел запроса),
        внутри окна — страницы по nextPageCursor. Дубли по execId отбрасываются."""
        start_ms = max(int(start_ms), int(end_ms) - MAX_HISTORY_MS)
        seen: set[str] = set()
        out: list[dict] = []
        s = start_ms
        while s <= end_ms:
            e = min(s + WEEK_MS - 1, int(end_ms))
            cursor = ""
            for _ in range(1000):           # предохранитель от бесконечного курсора
                res = self.get("/v5/execution/list", {
                    "category": category, "startTime": s, "endTime": e, "limit": limit,
                    "cursor": cursor})
                for it in res.get("list") or []:
                    eid = str(it.get("execId", ""))
                    if eid and eid not in seen:
                        seen.add(eid)
                        out.append(it)
                cursor = res.get("nextPageCursor") or ""
                if not cursor or not res.get("list"):
                    break
                if self._pause:
                    time.sleep(self._pause)
            s = e + 1
        return out


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0
