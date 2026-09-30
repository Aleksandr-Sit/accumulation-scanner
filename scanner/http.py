"""HTTP-клиент на stdlib: rate-limit по хосту, дисковый кэш, ретраи с backoff.

Держит бесплатные тарифы в рамках лимитов и экономит вызовы между запусками.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_RETRY_CODES = (429, 500, 502, 503, 504)
_WAIT_429_MIN = 60.0   # окно лимита free-тарифов — минута; backoff 2→16 с его не переживает
_WAIT_429_MAX = 90.0


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
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        if use_cache:
            cached = self._read_cache(url)
            if cached is not None:
                return cached

        host = urllib.parse.urlparse(url).netloc
        req = urllib.request.Request(url, headers=headers or {})
        req.add_header("User-Agent", "accumulation-scanner/0.1")
        req.add_header("Accept", "application/json")

        backoff = 2.0
        for attempt in range(retries):
            self._throttle(host)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if use_cache:
                    self._write_cache(url, data)
                return data
            except urllib.error.HTTPError as e:
                if e.code in _RETRY_CODES and attempt < retries - 1:
                    time.sleep(retry_wait(e.code, e.headers.get("Retry-After")
                                          if e.headers else None, backoff))
                    backoff *= 2
                    continue
                print(f"[http] {e.code} {url}")
                return None
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                if attempt < retries - 1:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                print(f"[http] fail {url}: {e}")
                return None
        return None
