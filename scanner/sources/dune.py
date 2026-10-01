"""On-chain накопление через Dune Analytics API (нужен DUNE_API_KEY в .env).

Модель как у фандинга: один Dune-запрос отдаёт таблицу метрик накопления по
токенам (нетто-поток на биржи, изменение числа холдеров), мы тянем её раз в
прогон и джойним к watchlist по symbol/адресу. Наполняет блок скора `onchain`,
который иначе всегда пуст (режет confidence).

Поток (self-contained urllib): execute query -> poll status -> results.
Ключа/query_id нет -> {} (нейтрально, не ломаемся). ЭВРИСТИКА: на free нельзя
провалидировать историей, вводим и проверяем на paper-статистике.

Что должен возвращать Dune-запрос (создаёт пользователь, даёт query_id):
таблицу со столбцами: symbol (UPPER) ИЛИ token_address; net_flow_usd_7d
(<0 = отток с бирж = накопление); holders_change_pct_7d (>0 = приток холдеров).
"""
from __future__ import annotations

import calendar
import http.client
import json
import time
import urllib.request

_BASE = "https://api.dune.com/api/v1"


def _req(url: str, key: str, method: str = "GET", timeout: int = 20):
    req = urllib.request.Request(url, method=method,
                                 headers={"X-Dune-API-Key": key,
                                          "User-Agent": "scanner-dune/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (OSError, http.client.HTTPException, ValueError) as e:  # URLError ⊂ OSError
        print(f"[dune] {method} {url.split('/api')[-1]} fail: {e}")
        return None


def result_age_hours(d: dict | None, now: float | None = None) -> float | None:
    """Возраст результата Dune в часах по execution_ended_at. None — поля нет/не разобрать."""
    s = (d or {}).get("execution_ended_at") or ""
    try:
        ended = calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    return ((now if now is not None else time.time()) - ended) / 3600.0


def fetch_query_rows(key: str, query_id: str | int, max_poll: int = 40,
                     use_cached: bool = True, max_age_hours: float = 20.0) -> list[dict]:
    """Строки результата Dune-запроса. use_cached=True -> последний кэш (0 кредитов),
    если он свежее max_age_hours; иначе execute+poll (~2 кредита).

    Кэш Dune сам не обновляется: без проверки возраста скан месяцами брал результат
    01.08.2026 и выдавал его за «поток за 7 дней». Не удалось обновить -> [] (блок
    onchain пуст), устаревшее не используем."""
    if not key or not query_id:
        return []
    # 1) дешёвый путь: последние кэшированные результаты, если свежие
    if use_cached:
        d = _req(f"{_BASE}/query/{query_id}/results?limit=1000", key)
        rows = (((d or {}).get("result") or {}).get("rows")) if d else None
        age = result_age_hours(d)
        if rows and age is not None and age <= max_age_hours:
            return rows
        if rows:
            shown = f"{age:.0f} ч" if age is not None else "возраст неизвестен"
            print(f"[dune] кэш запроса устарел ({shown}, порог {max_age_hours:g} ч) — перезапуск")
    # 2) выполнить и опросить
    ex = _req(f"{_BASE}/query/{query_id}/execute", key, method="POST")
    exec_id = (ex or {}).get("execution_id")
    if not exec_id:
        print("[dune] перезапуск не удался — on-chain блок в этом прогоне пуст")
        return []
    for _ in range(max_poll):
        st = _req(f"{_BASE}/execution/{exec_id}/status", key)
        state = (st or {}).get("state", "")
        if state == "QUERY_STATE_COMPLETED":
            res = _req(f"{_BASE}/execution/{exec_id}/results?limit=1000", key)
            return (((res or {}).get("result") or {}).get("rows")) or []
        if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
            print(f"[dune] execution {state}")
            return []
        time.sleep(3)
    print("[dune] poll timeout")
    return []


def build_onchain_map(rows: list[dict]) -> dict[str, dict]:
    """rows -> {ключ(UPPER symbol или lower адрес): {net_flow_usd_7d, holders_change_pct_7d}}.
    Чистая функция — тестируется офлайн на фикстуре."""
    out: dict[str, dict] = {}
    for r in rows:
        sym = (r.get("symbol") or "").upper()
        addr = (r.get("token_address") or "").lower()
        rec = {
            "net_flow_usd_7d": _num(r.get("net_flow_usd_7d")),
            "holders_change_pct_7d": _num(r.get("holders_change_pct_7d")),
        }
        if sym:
            out.setdefault(sym, rec)
        if addr:
            out.setdefault(addr, rec)
    return out


def _num(v):
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def fetch_onchain(cfg, http=None) -> dict[str, dict]:
    """Читает ключ+query_id из cfg, возвращает map накопления. {} если не настроено."""
    key = cfg.get("api_keys.dune", "")
    qid = cfg.get("onchain.dune_query_id", "")
    if not key or not qid:
        return {}
    max_age = cfg.get("onchain.max_age_hours", 20)
    return build_onchain_map(fetch_query_rows(key, qid, max_age_hours=max_age))
