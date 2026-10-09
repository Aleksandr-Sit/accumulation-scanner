"""HTTP-клиент на stdlib: rate-limit по хосту, дисковый кэш, ретраи с backoff.

Держит бесплатные тарифы в рамках лимитов и экономит вызовы между запусками.
Считает по хостам запросы, сбои, коды ответов и ожидание повторов (stats), а closes.load
отмечает отставание дневных закрытий (note_lag) — run.py пишет это в source_health
(scanner/health.py) после каждого шага: тренд здоровья источников для сводок.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import weakref
from pathlib import Path
from typing import Any

_RETRY_CODES = (429, 500, 502, 503, 504)
_WAIT_429_MIN = 60.0   # окно лимита free-тарифов — минута; backoff 2→16 с его не переживает
_WAIT_429_MAX = 90.0
CLIENTS: "weakref.WeakSet[HttpClient]" = weakref.WeakSet()   # все клиенты процесса (health)


def retry_wait(code: int, retry_after: str | None, backoff: float) -> float:
    """Пауза перед повтором, сек. 429: Retry-After (в секундах), иначе ждём окно
    лимита целиком. 5xx — обычный экспоненциальный backoff."""
    if code != 429:
        return backoff
    try:
        ra = float(retry_after) if retry_after else None
    except ValueError:
        ra = None   # HTTP-date вместо секунд — редкость, берём окно
    if ra is not None and ra >= 0:
        return min(ra + 1.0, _WAIT_429_MAX)
    return min(max(backoff, _WAIT_429_MIN), _WAIT_429_MAX)


class HttpClient:
    _PRUNE_AGE = 7 * 86400   # кэш старше недели бесполезен (TTL максимум часы)

    def __init__(self, cache_dir: str, cache_ttl: int, timeout: int,
                 rate_limits_per_min: dict[str, int]):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_ttl = cache_ttl
        self._prune_cache()
        self.timeout = timeout
        # min интервал между вызовами хоста, сек
        self._min_interval = {h: 60.0 / max(1, n) for h, n in rate_limits_per_min.items()}
        self._last_call: dict[str, float] = {}
        self.stats: dict[str, dict] = {}       # хост -> счётчики (_st)
        self.lags: dict[str, list[int]] = {}   # источник закрытий -> lag_days позиций
        CLIENTS.add(self)

    def _st(self, host: str) -> dict:
        """Счётчики хоста: req — запросов в сеть (логических, с повторами — один), ok,
        fail — так и не ответил, cache — из кэша, codes — {код: попыток}: HTTP-коды, «net» —
        сеть/таймаут, «retN» — ответ 200 с retCode N ≠ 0 (Bybit), wait_s — пауз перед повтором."""
        return self.stats.setdefault(host, {"req": 0, "ok": 0, "fail": 0, "cache": 0,
                                            "codes": {}, "wait_s": 0.0})

    def note_lag(self, src: str, lag: int | None) -> None:
        """Отставание дневного закрытия позиции от ожидаемого (closes.load), дней."""
        if lag is not None:
            self.lags.setdefault(src, []).append(int(lag))

    def _prune_cache(self) -> None:
        """Удаляет файлы кэша старше _PRUNE_AGE — иначе .cache растёт бесконечно."""
        cutoff = time.time() - self._PRUNE_AGE
        try:
            for p in self.cache_dir.iterdir():
                if p.suffix == ".json" and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
        except OSError:
            pass

    # --- rate limiting ---
    def _throttle(self, host: str) -> None:
        interval = self._min_interval.get(host, 0.0)
        if interval <= 0:
            return
        last = self._last_call.get(host, 0.0)
        wait = interval - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        self._last_call[host] = time.monotonic()

    # --- cache ---
    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{h}.json"

    def _read_cache(self, url: str) -> Any | None:
        p = self._cache_path(url)
        if not p.exists():
            return None
        if time.time() - p.stat().st_mtime > self.cache_ttl:
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def _write_cache(self, url: str, data: Any) -> None:
        try:
            self._cache_path(url).write_text(json.dumps(data), encoding="utf-8")
        except (OSError, TypeError):
            pass

    # --- request ---
    def get_json(self, url: str, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None,
                 use_cache: bool = True, retries: int = 5) -> Any | None:
        return self._get(url, params, headers, use_cache, retries, "application/json",
                         lambda raw: json.loads(raw.decode("utf-8")))

    def get_text(self, url: str, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None,
                 use_cache: bool = True, retries: int = 3) -> str | None:
        """Как get_json, но тело — текст (Atom/XML GitHub). В кэше лежит JSON-строкой."""
        return self._get(url, params, headers, use_cache, retries, "*/*",
                         lambda raw: raw.decode("utf-8", errors="replace"))

    def _get(self, url: str, params: dict[str, Any] | None, headers: dict[str, str] | None,
             use_cache: bool, retries: int, accept: str, decode) -> Any | None:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        host = urllib.parse.urlparse(url).netloc
        st = self._st(host)
        if use_cache:
            cached = self._read_cache(url)
            if cached is not None:
                st["cache"] += 1
                return cached
        st["req"] += 1
        codes = st["codes"]
        req = urllib.request.Request(url, headers=headers or {})
        req.add_header("User-Agent", "accumulation-scanner/0.1")
        req.add_header("Accept", accept)

        backoff = 2.0
        for attempt in range(retries):
            self._throttle(host)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = decode(resp.read())
                st["ok"] += 1
                rc = data.get("retCode") if isinstance(data, dict) else None
                if isinstance(rc, int) and rc != 0:
                    codes[f"ret{rc}"] = codes.get(f"ret{rc}", 0) + 1
                if use_cache:
                    self._write_cache(url, data)
                return data
            except urllib.error.HTTPError as e:
                codes[str(e.code)] = codes.get(str(e.code), 0) + 1
                if e.code in _RETRY_CODES and attempt < retries - 1:
                    w = retry_wait(e.code, e.headers.get("Retry-After") if e.headers else None,
                                   backoff)
                    st["wait_s"] += w
                    time.sleep(w)
                    backoff *= 2
                    continue
                print(f"[http] {e.code} {url}")
                st["fail"] += 1
                return None
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                codes["net"] = codes.get("net", 0) + 1
                if attempt < retries - 1:
                    st["wait_s"] += backoff
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                print(f"[http] fail {url}: {e}")
                st["fail"] += 1
                return None
        st["fail"] += 1
        return None
