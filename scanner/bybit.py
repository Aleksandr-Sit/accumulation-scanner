"""Bybit V5: подписанные GET-запросы только на чтение (шаг 1 автоматизации — синхронизация).

Подпись — как в официальной доке (docs/v5/guide, «Create A Request») и примере
bybit-exchange/api-usage-examples V5_demo/api_demo/Encryption_HMAC.py:
  строка = timestamp(мс) + api_key + recv_window + queryString  (GET; у POST — тело JSON)
  X-BAPI-SIGN = HMAC-SHA256(secret, строка) в нижнем hex.
Заголовки: X-BAPI-API-KEY, X-BAPI-TIMESTAMP, X-BAPI-SIGN, X-BAPI-SIGN-TYPE=2 (HMAC),
X-BAPI-RECV-WINDOW. Timestamp должен лежать в [server_time − recv_window; server_time + 1000) —
часы сервера под NTP. queryString подписывается ровно в том виде, в каком уходит в URL.

Client — только чтение: /v5/execution/list (исполнения, окно ≤ 7 дней за запрос, история
2 года, страницы — nextPageCursor) и /v5/account/wallet-balance (accountType=UNIFIED); ключ sync
создаётся с правами Read-Only. TradeClient — ордера демо-счёта (блок F, scanner/demo.py): только
api-demo.bybit.com, только спот без займа. Кэша нет — приватные ответы на диск не пишутся.
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
            if hint is None and status == 401:   # wallet-balance с чужим ключом: 401, тело пустое
                hint = HINTS[10003]
            raise BybitError(f"{path}: HTTP {status}, retCode {code} {data.get('retMsg', '')!r}"
                             + (f" — {hint}" if hint else ""), code)
        return data.get("result") or {}

    def api_key_info(self) -> dict:
        """GET /v5/user/query-api — права, IP и срок жизни этого же ключа (доступно любому
        ключу, в том числе Read-Only)."""
        return self.get("/v5/user/query-api", {})

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


def _urllib_post(url: str, headers: dict[str, str], body: bytes,
                 timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""


# Коды ответа на ордер (сверено живыми запросами к api-demo 10.10.2026)
DUPLICATE = 170141          # «Duplicate clientOrderId» — ордер с этим orderLinkId уже есть
NOT_EXISTS = 170213         # «Order does not exist» — снимать нечего (исполнен, снят, пропал)
LOW_VALUE = 170140          # «Order value exceeded lower limit» — меньше minOrderAmt


class TradeClient(Client):
    """Ключ демо-счёта (блок F): ордера ТОЛЬКО на api-demo.bybit.com и ТОЛЬКО спот без займа
    (category=spot и isLeverage=0 зашиты здесь, снаружи их не передать). Живые деньги — шаг 4
    плана (DECISIONS U3), здесь не включаются: другой домен — отказ в конструкторе.
    post(url, headers, body, timeout) -> (http-код, тело) подменяется в selftest.
    Подпись POST — та же HMAC, payload — тело JSON ровно в том виде, в каком уходит."""

    def __init__(self, api_key: str, api_secret: str, base_url: str = DEMO_URL, *,
                 post: Callable[..., tuple[int, bytes]] | None = None, **kw):
        if base_url.rstrip("/") != DEMO_URL:
            raise BybitError(f"торговый клиент работает только с демо-счётом ({DEMO_URL}); "
                             f"живые деньги не включены (DECISIONS U3)")
        if not api_key or not api_secret:
            raise BybitError("нет ключа демо-счёта: BYBIT_DEMO_API_KEY и BYBIT_DEMO_API_SECRET "
                             "в .env")
        super().__init__(api_key, api_secret, base_url, **kw)
        self._post = post or _urllib_post

    def post(self, path: str, body: dict[str, Any]) -> dict:
        """Подписанный POST -> {"retCode", "retMsg", "result"} как есть: коды ордеров
        (DUPLICATE, NOT_EXISTS, …) разбирает вызывающий. HTTP-ошибка или не JSON — BybitError."""
        payload = json.dumps(body, separators=(",", ":"))
        ts_ms = str(int(self._clock() * 1000))
        h = {**self.headers(payload, ts_ms), "Content-Type": "application/json"}
        status, raw = self._post(f"{self.base_url}{path}", h, payload.encode("utf-8"),
                                 self.timeout)
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise BybitError(f"{path}: HTTP {status}, ответ не JSON") from None
        code = data.get("retCode")
        if status != 200 or not isinstance(code, int):
            hint = HINTS.get(code) if isinstance(code, int) else None
            raise BybitError(f"{path}: HTTP {status}, retCode {code} {data.get('retMsg', '')!r}"
                             + (f" — {hint}" if hint else ""), code)
        if code in HINTS:                     # ключ, подпись, IP, время — не про этот ордер
            raise BybitError(f"{path}: retCode {code} {data.get('retMsg', '')!r} — "
                             f"{HINTS[code]}", code)
        return {"retCode": code, "retMsg": data.get("retMsg", ""),
                "result": data.get("result") or {}}

    def create_order(self, symbol: str, side: str, order_type: str, qty: str, link: str,
                     price: str | None = None, market_unit: str | None = None) -> dict:
        """Спот-ордер без займа. Лимитка — GTC; рыночная покупка — qty в USDT по умолчанию
        (market_unit «quoteCoin»), продажа — в монете."""
        if side not in ("Buy", "Sell") or order_type not in ("Market", "Limit"):
            raise BybitError(f"ордер {side}/{order_type} не поддерживается")
        body: dict[str, Any] = {"category": "spot", "symbol": symbol, "side": side,
                                "orderType": order_type, "qty": qty, "orderLinkId": link,
                                "isLeverage": 0}
        if order_type == "Limit":
            if not price:
                raise BybitError("лимитка без цены")
            body.update(price=price, timeInForce="GTC")
        elif market_unit:
            body["marketUnit"] = market_unit
        return self.post("/v5/order/create", body)

    def cancel_order(self, symbol: str, link: str) -> dict:
        return self.post("/v5/order/cancel", {"category": "spot", "symbol": symbol,
                                              "orderLinkId": link})

    def order(self, link: str) -> dict | None:
        """Ордер по orderLinkId в любом статусе: /v5/order/realtime (по orderLinkId отдаёт и
        закрытые), не нашёлся — /v5/order/history (7 дней). None — на бирже такого нет."""
        for path in ("/v5/order/realtime", "/v5/order/history"):
            lst = self.get(path, {"category": "spot", "orderLinkId": link}).get("list") or []
            for o in lst:
                if o.get("orderLinkId") == link:
                    return o
        return None

    def last_price(self, symbol: str) -> float | None:
        lst = self.get("/v5/market/tickers", {"category": "spot", "symbol": symbol}).get("list")
        px = _num((lst or [{}])[0].get("lastPrice"))
        return px if px > 0 else None


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def check_key(info: dict, role: str = "sync", warn_days: int = 14, demo: bool = False) -> dict:
    """Ответ /v5/user/query-api -> {"level": ok|warn|danger, "issues": [...], "facts": [...]}.
    role "sync" — ключ синхронизации: только Read-Only (readOnly=1). role "trade" — торговый
    ключ: торговля, но только Spot. Обоим нельзя право вывода (Withdraw) и
    нужна привязка к IP: ключ без IP живёт 90 дней, deadlineDay — сколько осталось (у ключа
    с IP — −1/−2, срока нет). Сверено с docs/v5/user/apikey-info 08.10.2026: у Read-Only ключа
    permissions всё равно перечисляют категории (ContractTrade, Spot…) — для sync решает
    readOnly. demo — ключ демо-счёта: Bybit выдаёт «Единый торговый аккаунт» только целиком
    (спот + контракты + опционы, 10.10.2026), поэтому лишние права — пометка в facts, а не
    опасность; торговый клиент и так ставит только спот без займа (TradeClient)."""
    perms = {k: [str(x) for x in v] for k, v in (info.get("permissions") or {}).items() if v}
    ro = info.get("readOnly") == 1
    withdraw = "Withdraw" in perms.get("Wallet", [])
    ips = [str(x).strip() for x in info.get("ips") or [] if str(x).strip()]
    bound = bool(ips) and "*" not in ips
    dd = info.get("deadlineDay")
    days_left = dd if isinstance(dd, int) and dd >= 0 else None
    issues: list[tuple[str, str]] = []
    if withdraw:
        issues.append(("danger", "у ключа есть право ВЫВОДА средств — удали его на bybit.com "
                                 "и создай новый без Withdraw"))
    if role == "sync":
        if not ro:
            issues.append(("danger", "ключ может торговать (не Read-Only) — для sync нужен "
                                     "ключ только на чтение"))
    else:
        if ro:
            issues.append(("warn", "ключ только на чтение — ордера им не поставить"))
        extra = sorted(k for k in perms if k != "Spot")
        if extra and not demo:
            issues.append(("danger", "лишние права: " + ", ".join(extra) + " — нужен только Spot"))
        if "SpotTrade" not in perms.get("Spot", []):
            issues.append(("warn", "нет права SpotTrade"))
    if not bound:
        issues.append(("warn" if role == "sync" else "danger",
                       "ключ не привязан к IP сервера — укради его кто-то, им можно "
                       "пользоваться откуда угодно, и он истечёт через 90 дней"))
    if days_left is not None and days_left <= warn_days:
        issues.append(("warn", f"ключ истекает через {days_left} дн. "
                               f"({str(info.get('expiredAt') or '')[:10]}) — создай новый с "
                               f"привязкой к IP"))
    level = ("danger" if any(lv == "danger" for lv, _ in issues)
             else "warn" if issues else "ok")
    facts = ["только чтение" if ro else "торговля",
             "вывода нет" if not withdraw else "ЕСТЬ ВЫВОД",
             "IP привязан" if bound else "без IP",
             "бессрочный" if days_left is None else f"ещё {days_left} дн."]
    if role != "sync" and demo:
        other = sorted(k for k in perms if k not in ("Spot", "Wallet"))
        if other:
            facts.append("демо: Bybit дал ещё " + ", ".join(other) + " — ордера только спот")
    return {"role": role, "level": level, "issues": [t for _, t in issues], "facts": facts}
