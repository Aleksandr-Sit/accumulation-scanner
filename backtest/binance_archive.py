"""Дневные OHLCV всех USDT-пар Binance, ВКЛЮЧАЯ ДЕЛИСТНУТЫЕ (data.binance.vision).

Зачем: /api/v3/klines отдаёт только живые сегодня пары, поэтому прежние исследования
(feature_study, market_regime_study) — survivor-only, все P в них верхняя граница.
Публичный архив Binance хранит месячные zip-файлы и по делистнутым символам
(WAVES, XEM, OMG, ...) — умершие монеты становятся видимы. Для лестницы
(усреднение вниз) это главный риск, поэтому без них считать нельзя.

Источник (без ключа, проверено 29.09.2026):
  листинг  https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix=...
  файлы    https://data.binance.vision/data/spot/monthly/klines/{SYM}/1d/{SYM}-1d-YYYY-MM.zip
Текущий неполный месяц в архиве отсутствует — для живых пар дотягивается из /api/v3/klines.
С 2025-01 метки времени в архиве в микросекундах (раньше — миллисекунды), приводим к секундам.

Кэш: .cache/binance_1d_full/{SYM}.json = {ts, o, h, l, c, qv} (qv — оборот в USDT)
     .cache/binance_1d_full/_index.json = {SYM: {first, last, n, trading}}
Повторный запуск докачивает только новое.  Запуск:  python backtest/binance_archive.py
"""
from __future__ import annotations

import io
import json
import re
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".cache" / "binance_1d_full"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
DATA = "https://data.binance.vision"
API = "https://api.binance.com"
UA = {"User-Agent": "ladder-research/0.1"}

# Не монеты для откупа: стейблы, фиат, золото, обёртки (базовый актив пары).
NON_ALTS = {
    "USDC", "BUSD", "TUSD", "USDP", "PAX", "DAI", "FDUSD", "UST", "USTC", "SUSD",
    "USDS", "USDSB", "BFUSD", "USD1", "RLUSD", "XUSD", "USDE", "PYUSD", "EURC", "U",
    "EUR", "GBP", "AUD", "AEUR", "EURI", "BRL", "TRY", "RUB", "UAH", "NGN", "ZAR",
    "BKRW", "IDRT", "BIDR", "BVND", "PAXG", "XAUT", "WBTC", "WBETH", "BETH", "WETH",
}


def _get(url: str, timeout: int = 30, tries: int = 4) -> bytes | None:
    for a in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                        timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(1.5 * (a + 1))
        except Exception:
            time.sleep(1.5 * (a + 1))
    return None


def _s3_list(prefix: str, delimiter: bool) -> tuple[list[str], list[str]]:
    """(подкаталоги, ключи) S3-листинга с пагинацией по marker."""
    dirs, keys, marker = [], [], ""
    while True:
        url = f"{S3}?prefix={prefix}" + ("&delimiter=/" if delimiter else "") + \
              (f"&marker={marker}" if marker else "")
        raw = _get(url)
        if raw is None:
            break
        txt = raw.decode()
        if delimiter:   # первый <Prefix> — эхо запроса, не подкаталог
            dirs += [p for p in re.findall(r"<Prefix>([^<]+)</Prefix>", txt) if p != prefix]
        keys += re.findall(r"<Key>([^<]+)</Key>", txt)
        m = re.search(r"<NextMarker>([^<]+)</NextMarker>", txt)
        if "<IsTruncated>true" not in txt:
            break
        marker = m.group(1) if m else (keys[-1] if keys else "")
        if not marker:
            break
    return dirs, keys


def is_leveraged(base: str, bases: set[str]) -> bool:
    """BTCUP/BTCDOWN/ETHBULL/BEAR... — но не JUP/SYRUP (настоящие монеты)."""
    if base in ("BULL", "BEAR"):
        return True
    for suf in ("DOWN", "UP", "BULL", "BEAR"):
        if base.endswith(suf) and base[:-len(suf)] in bases:
            return True
    return False


def usdt_symbols() -> list[str]:
    dirs, _ = _s3_list("data/spot/monthly/klines/", delimiter=True)
    syms = [d.rstrip("/").split("/")[-1] for d in dirs]
    bases = {s[:-4] for s in syms if s.endswith("USDT")}
    out = []
    for s in syms:
        if not s.endswith("USDT"):
            continue
        b = s[:-4]
        if b in NON_ALTS or is_leveraged(b, bases):
            continue
        out.append(s)
    return sorted(set(out))


def _norm_ts(v: float) -> int:
    v = float(v)
    if v > 1e14:
        return int(v // 1_000_000)       # микросекунды (архив с 2025-01)
    if v > 1e11:
        return int(v // 1000)            # миллисекунды
    return int(v)


def _parse_csv(text: str, rows: dict[int, tuple]) -> None:
    for line in text.splitlines():
        p = line.split(",")
        if len(p) < 8 or not p[0][:1].isdigit():
            continue                      # заголовок (бывает в новых файлах)
        try:
            ts = _norm_ts(p[0]) // 86400 * 86400
            rows[ts] = (float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[7]))
        except ValueError:
            continue


def fetch_symbol(sym: str, trading: bool) -> dict | None:
    """Живая пара — вся история из API (3–4 запроса); делистнутая — из месячных zip."""
    path = OUT / f"{sym}.json"
    rows: dict[int, tuple] = {}
    if path.exists():
        d = json.loads(path.read_text())
        for i, t in enumerate(d["ts"]):
            rows[t] = (d["o"][i], d["h"][i], d["l"][i], d["c"][i], d["qv"][i])
    if not trading and not rows:
        _, keys = _s3_list(f"data/spot/monthly/klines/{sym}/1d/", delimiter=False)
        for k in sorted(k for k in keys if k.endswith(".zip")):
            raw = _get(f"{DATA}/{k}")
            if not raw:
                continue
            try:
                z = zipfile.ZipFile(io.BytesIO(raw))
                _parse_csv(z.read(z.namelist()[0]).decode(), rows)
            except (zipfile.BadZipFile, IndexError, UnicodeDecodeError):
                continue
    if trading:                               # с листинга (или с конца кэша) до вчера
        start = (max(rows) + 86400) * 1000 if rows else 1483228800000
        while True:
            raw = _get(f"{API}/api/v3/klines?symbol={sym}&interval=1d&startTime={start}&limit=1000")
            data = json.loads(raw) if raw else []
            if not isinstance(data, list) or not data:
                break
            now_day = int(time.time()) // 86400 * 86400
            for k in data:
                ts = int(k[0]) // 1000 // 86400 * 86400
                if ts >= now_day:              # незакрытая свеча
                    continue
                rows[ts] = (float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[7]))
            if len(data) < 1000:
                break
            start = int(data[-1][0]) + 86400000
    if not rows:
        return None
    ts = sorted(rows)
    out = {"ts": ts,
           "o": [rows[t][0] for t in ts], "h": [rows[t][1] for t in ts],
           "l": [rows[t][2] for t in ts], "c": [rows[t][3] for t in ts],
           "qv": [round(rows[t][4], 2) for t in ts]}
    path.write_text(json.dumps(out, separators=(",", ":")))
    return {"first": ts[0], "last": ts[-1], "n": len(ts), "trading": trading}


def trading_now() -> set[str]:
    raw = _get(f"{API}/api/v3/exchangeInfo?permissions=SPOT", timeout=60)
    if not raw:
        return set()
    info = json.loads(raw)
    return {s["symbol"] for s in info.get("symbols", []) if s.get("status") == "TRADING"}


def load(min_days: int = 0) -> dict[str, dict]:
    """{SYM: {ts, o, h, l, c, qv, trading}} из кэша (без сети) — для исследований."""
    idx = json.loads((OUT / "_index.json").read_text())
    out = {}
    for sym, meta in idx.items():
        p = OUT / f"{sym}.json"
        if not p.exists() or meta["n"] < min_days:
            continue
        d = json.loads(p.read_text())
        d["trading"] = meta["trading"]
        out[sym] = d
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from feature_study import _tagged_non_alts   # токенизированные акции/золото (теги Binance)
    tagged = _tagged_non_alts()
    syms = [s for s in usdt_symbols() if s[:-4] not in tagged]
    live = trading_now()
    if not live:
        print("⚠ exchangeInfo недоступен — текущий месяц не дотянется, статус trading=False")
    print(f"USDT-пар в архиве (без стейблов/плечевых): {len(syms)}; торгуются сейчас: "
          f"{sum(1 for s in syms if s in live)}")
    index: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=24) as ex:
        futs = {ex.submit(fetch_symbol, s, s in live): s for s in syms}
        for i, f in enumerate(as_completed(futs), 1):
            s = futs[f]
            try:
                meta = f.result()
            except Exception as e:           # один символ не должен ронять прогон
                print(f"  {s}: ошибка {e}")
                continue
            if meta:
                index[s] = meta
            if i % 100 == 0:
                print(f"  ...{i}/{len(syms)} ({time.time() - t0:.0f} с)")
    (OUT / "_index.json").write_text(json.dumps(index, indent=0, sort_keys=True))
    n_dead = sum(1 for m in index.values() if not m["trading"])
    print(f"Готово: {len(index)} символов, из них делистнуто/не торгуется {n_dead}; "
          f"{time.time() - t0:.0f} с → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
