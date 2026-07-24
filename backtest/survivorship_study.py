"""Оценка survivorship bias для вероятностей пружины. Zero-dep, бесплатно.

Проблема: history_calibration.py считает P(+100%|пружина) на Binance-выживших —
монеты, сформировавшие пружину и УМЕРШИЕ (делистинг/→0), невидимы, поэтому P
завышена. Многолетний OHLC мёртвых монет бесплатно больше недоступен
(CryptoCompare под ключом, Coinpaprika/CoinCap историю режут).

Обход: не форвард-цены мёртвых, а SURVIVAL-RATE когорты, которая массово
формировала пружины. Монеты из топа пика цикла 2021 → почти все ушли в 70%+
просадку в медведе 2022 (= прекондиция пружины). Доля этой когорты, дожившая до
сегодня, оценивает, какая часть пружин вообще ВИДНА survivor-only исследованию.

Логика haircut: пул на пике = N монет, все станут пружинами. Доживает X·N.
Мёртвые (1−X)·N имеют ~0 «успехов» (+100% и удержался — они умерли). Survivor-
исследование видит только X·N и завышает: P_surv = успехи/(X·N).
Истинная P по всему пулу ≈ P_surv · X.  →  haircut-множитель = survival_rate.

Источники (free): CoinMarketCap historical snapshots через web.archive.org
(point-in-time топ-200 на воскресенья) + текущий ранг/цена CoinGecko.

Ограничения (честно): survival по текущему рангу — один из порогов; часть
мёртвых пампила +100% перед смертью (haircut слегка консервативен); когорта
«топ пика» ⊇ «пружины», но не тождественна; CoinGecko сам почти не удаляет
монеты, полностью исчезнувшие невидимы и здесь.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request

_CMC_SUNDAYS = ["20210509", "20211107", "20220102"]  # пики/склон цикла 2021
_CG = "https://api.coingecko.com/api/v3"
# P(+100%) по циклам из history_calibration (survivor-only, ВЕРХНЯЯ граница):
_P100_SURV = {"2021": 0.68, "2022": 0.52, "2023-24": 0.63, "средн.": 0.61}


def _get(url: str, headers: dict | None = None, retries: int = 5):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "surv/0.1"})
    backoff = 2.0
    for _ in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception:
            time.sleep(backoff); backoff *= 1.6
    return None


def fetch_cmc_snapshot(yyyymmdd: str) -> list[dict]:
    """Топ-200 монет на дату (symbol, name, slug, rank, price) из архива CMC."""
    url = (f"https://web.archive.org/web/{yyyymmdd}000000/"
           f"https://coinmarketcap.com/historical/{yyyymmdd}/")
    html = _get(url)
    if not html:
        return []
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))

    def find_list(o):
        if isinstance(o, list) and o and isinstance(o[0], dict) \
                and "symbol" in o[0] and "cmc_rank" in o[0]:
            return o
        if isinstance(o, dict):
            for v in o.values():
                r = find_list(v)
                if r:
                    return r
        if isinstance(o, list):
            for v in o:
                r = find_list(v)
                if r:
                    return r
        return None

    lst = find_list(data) or []
    out = []
    for c in lst:
        q = (c.get("quote") or {}).get("USD") or {}
        out.append({"symbol": (c.get("symbol") or "").upper(),
                    "name": c.get("name") or "", "slug": c.get("slug") or "",
                    "rank": c.get("cmc_rank"), "price": q.get("price")})
    return out


def _cg_headers() -> dict:
    import os
    k = os.getenv("COINGECKO_DEMO_KEY", "")
    h = {"User-Agent": "surv/0.1"}
    if k:
        h["x-cg-demo-api-key"] = k
    return h


def fetch_cg_current(pages: int = 10) -> dict[str, dict]:
    """symbol -> {rank, price, id} для текущего топ-(pages*250) CoinGecko."""
    out: dict[str, dict] = {}
    for p in range(1, pages + 1):
        raw = _get(f"{_CG}/coins/markets?vs_currency=usd&order=market_cap_desc"
                   f"&per_page=250&page={p}", _cg_headers())
        if not raw:
            continue
        try:
            rows = json.loads(raw)
        except ValueError:
            continue
        if not rows:
            break
        for r in rows:
            sym = (r.get("symbol") or "").upper()
            # первый (высший ранг) выигрывает при коллизии тикеров
            if sym and sym not in out:
                out[sym] = {"rank": r.get("market_cap_rank"),
                            "price": r.get("current_price"), "id": r.get("id")}
        time.sleep(2.5)
    return out


def main() -> int:
    print("=== survivorship-оценка пружинных вероятностей ===\n")

    # 1) Когорта пика цикла 2021 (union топов по symbol; пиковая цена = max).
    cohort: dict[str, dict] = {}
    for d in _CMC_SUNDAYS:
        snap = fetch_cmc_snapshot(d)
        print(f"CMC {d}: {len(snap)} монет")
        for c in snap:
            s = c["symbol"]
            if not s or c["price"] is None:
                continue
            prev = cohort.get(s)
            if prev is None or (c["price"] or 0) > (prev["peak_price"] or 0):
                cohort[s] = {"name": c["name"], "peak_price": c["price"],
                             "best_rank": min(c["rank"] or 999,
                                              prev["best_rank"] if prev else 999)}
        time.sleep(1)

    # стейблы/обёртки вон — они не «пружины» и искажают survival вверх
    _EXCL = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "UST", "USDP", "USDN", "HUSD",
             "WBTC", "WETH", "STETH", "STptETH", "FRAX", "USDD", "GUSD"}
    cohort = {s: v for s, v in cohort.items() if s not in _EXCL}
    print(f"\nКогорта пика цикла 2021 (без стейблов): {len(cohort)} монет")

    # 2) Текущее состояние CoinGecko.
    cur = fetch_cg_current(10)
    print(f"Текущий индекс CoinGecko: {len(cur)} тикеров (топ-2500)\n")

    # 3) Survival при разных порогах «жива».
    alive_ranks = {1000: 0, 2000: 0, 99999: 0}  # порог ранга «ещё жива»
    dead_gone = 0          # выпала из топ-2500 вовсе
    recovered = 0          # текущая цена >= пика 2021 (полностью восстановилась)
    down90 = 0             # −90%+ от пика (ходячий труп)
    matched = 0
    for s, v in cohort.items():
        c = cur.get(s)
        if not c or c["rank"] is None:
            dead_gone += 1
            continue
        matched += 1
        for thr in alive_ranks:
            if c["rank"] <= thr:
                alive_ranks[thr] += 1
        if v["peak_price"] and c["price"]:
            r = c["price"] / v["peak_price"] - 1
            if r >= 0.0:
                recovered += 1
            if r <= -0.90:
                down90 += 1

    N = len(cohort)
    print(f"{'порог «жива»':28} {'дожило':>8} {'survival':>9}  {'haircut P(+100)':>26}")
    for thr, cnt in sorted(alive_ranks.items()):
        surv = cnt / N
        label = f"в топ-{thr} CoinGecko" if thr < 99999 else "в топ-2500 CoinGecko"
        hp = _P100_SURV["средн."] * surv
        print(f"{label:28} {cnt:>8} {surv:>8.0%}   "
              f"{_P100_SURV['средн.']:.0%} → {hp:.0%}")
    print(f"{'полностью выпала (>2500)':28} {dead_gone:>8} {dead_gone/N:>8.0%}")
    print(f"\nИз когорты (N={N}): восстановились ≥ пика 2021: {recovered} "
          f"({recovered/N:.0%}); −90%+ от пика (труп): {down90} ({down90/N:.0%})")

    # 4) Итоговый haircut по циклам (survival = топ-2000 как «ещё торгуема/жива»).
    surv2000 = alive_ranks[2000] / N
    print(f"\n=== ИТОГ: haircut = ×{surv2000:.2f} (survival до топ-2000) ===")
    print(f"{'цикл':10} {'P_surv(+100)':>13} {'→ истинная P':>14}")
    for cyc, p in _P100_SURV.items():
        print(f"{cyc:10} {p:>12.0%} {p*surv2000:>13.0%}")

    result = {"cohort_n": N, "survival": {str(k): v/N for k, v in alive_ranks.items()},
              "dead_gone_pct": dead_gone/N, "recovered_pct": recovered/N,
              "down90_pct": down90/N, "haircut_x": surv2000,
              "p100_true": {c: p*surv2000 for c, p in _P100_SURV.items()}}
    with open("survivorship_results.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("\nsaved -> survivorship_results.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
