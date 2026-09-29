"""Track A: вселенная established-токенов из CoinGecko (free/demo API).

/coins/markets       — цена, MC, FDV, объём, ATH, % от ATH (пагинация).
/coins/list?include_platform=true — маппинг coin_id -> {платформа: адрес} (кэш на день).

Free demo: ~30 вызовов/мин, ключ (опц.) через header x-cg-demo-api-key поднимает лимит.
"""
from __future__ import annotations

from ..chains import CG_PLATFORM_TO_CHAIN
from ..http import HttpClient
from ..models import Candidate

_BASE = "https://api.coingecko.com/api/v3"


def _headers(demo_key: str) -> dict[str, str]:
    return {"x-cg-demo-api-key": demo_key} if demo_key else {}


def fetch_platform_map(http: HttpClient, demo_key: str = "") -> dict[str, dict[str, str]]:
    """coin_id -> {platform: address}. Кэшируется (список меняется редко)."""
    data = http.get_json(
        f"{_BASE}/coins/list", params={"include_platform": "true"},
        headers=_headers(demo_key),
    )
    out: dict[str, dict[str, str]] = {}
    if isinstance(data, list):
        for coin in data:
            out[coin.get("id", "")] = coin.get("platforms", {}) or {}
    return out


def _pick_chain(platforms: dict[str, str]) -> tuple[str, str]:
    """Выбирает поддерживаемую сеть с непустым адресом (по приоритету реестра)."""
    best: tuple[int, str, str] | None = None
    for cg_platform, addr in platforms.items():
        chain = CG_PLATFORM_TO_CHAIN.get(cg_platform)
        if chain and addr:
            from ..chains import CHAINS
            prio = CHAINS[chain]["priority"]
            if best is None or prio < best[0]:
                best = (prio, chain, addr)
    if best:
        return best[1], best[2]
    return "", ""


def fetch_markets(http: HttpClient, pages: int, per_page: int,
                  demo_key: str = "") -> list[dict]:
    rows: list[dict] = []
    for page in range(1, pages + 1):
        data = http.get_json(
            f"{_BASE}/coins/markets",
            params={
                "vs_currency": "usd", "order": "market_cap_desc",
                "per_page": per_page, "page": page,
                "price_change_percentage": "24h",
            },
            headers=_headers(demo_key),
        )
        if not isinstance(data, list) or not data:
            break
        rows.extend(data)
    return rows


def fetch_market_chart(http: HttpClient, coin_id: str, days: int,
                       demo_key: str = "") -> dict:
    """Дневной ряд за `days` дней (oldest→newest): {ts, prices, volumes}.

    ts — unix-секунды. volumes — total_volumes из того же ответа (бесплатный
    сигнал накопления/распределения, 0 дополнительных вызовов API). mcaps —
    market_caps оттуда же: предложение = mcap / price (прокси разлоков/эмиссии).
    """
    empty = {"ts": [], "prices": [], "volumes": [], "mcaps": []}
    if not coin_id:
        return empty
    data = http.get_json(
        f"{_BASE}/coins/{coin_id}/market_chart",
        params={"vs_currency": "usd", "days": days, "interval": "daily"},
        headers=_headers(demo_key),
    )
    if not isinstance(data, dict):
        return empty
    ts, prices, volumes, mcaps = [], [], [], []
    raw_v = {int(v[0]) : v[1] for v in data.get("total_volumes", [])
             if isinstance(v, list) and len(v) == 2}
    raw_m = {int(v[0]): v[1] for v in data.get("market_caps", [])
             if isinstance(v, list) and len(v) == 2}
    for p in data.get("prices", []):
        if isinstance(p, list) and len(p) == 2 and p[1] is not None:
            ts.append(int(p[0]) / 1000.0)
            prices.append(p[1])
            volumes.append(raw_v.get(int(p[0])))
            mcaps.append(raw_m.get(int(p[0])))
    return {"ts": ts, "prices": prices, "volumes": volumes, "mcaps": mcaps}


def fetch_global(http: HttpClient, demo_key: str = "") -> dict:
    """/global — доминация BTC и суммарный капитал (текущий срез, истории нет на free).

    Возвращает {btc_dominance_pct, total_mcap_usd, total2_mcap_usd}. TOTAL2 = альты
    (total − BTC). Высокая BTC.D = альты капитулировали (контекст для откупа у дна).
    Информационный контекст, НЕ скоринговый (на free нельзя провалидировать историей).
    """
    data = http.get_json(f"{_BASE}/global", headers=_headers(demo_key))
    d = (data or {}).get("data") if isinstance(data, dict) else None
    if not isinstance(d, dict):
        return {}
    mcp = d.get("market_cap_percentage") or {}
    total = (d.get("total_market_cap") or {}).get("usd")
    btc_d = mcp.get("btc")
    total2 = None
    if isinstance(total, (int, float)) and isinstance(btc_d, (int, float)):
        total2 = total * (1 - btc_d / 100.0)
    return {"btc_dominance_pct": round(btc_d, 1) if isinstance(btc_d, (int, float)) else None,
            "total_mcap_usd": total, "total2_mcap_usd": total2}


def closed_daily(chart: dict) -> dict:
    """Отбрасывает последнюю НЕЗАКРЫТУЮ (внутридневную) точку market_chart.

    Закрытые дневные точки CoinGecko выровнены на 00:00 UTC (ts % 86400 == 0);
    финальная точка — live-цена на момент запроса (подтверждено замером: 18:37 UTC).
    Для exit-логики (инвалидация «2 закрытия», HWM, зона) нужны ТОЛЬКО закрытия,
    иначе внутридневной тик засчитывается как «закрытие» и ломает подтверждение.
    """
    ts = chart.get("ts") or []
    if ts and int(round(ts[-1])) % 86400 > 60:
        return {k: v[:-1] for k, v in chart.items() if isinstance(v, list)}
    return chart


def fetch_price_history(http: HttpClient, coin_id: str, days: int,
                        demo_key: str = "") -> list[float]:
    """Дневные цены за `days` дней (oldest→newest) для детектора зоны (Stage 4)."""
    return fetch_market_chart(http, coin_id, days, demo_key)["prices"]


def fetch_coin_detail(http: HttpClient, coin_id: str, demo_key: str = "") -> dict:
    """`/coins/{id}` с developer_data + tickers (для Stage 3b liveness). Free.

    Возвращает {} при отсутствии данных. Тяжёлые блоки отключены параметрами,
    оставлены developer_data (GitHub-активность) и tickers (широта листингов).
    """
    if not coin_id:
        return {}
    data = http.get_json(
        f"{_BASE}/coins/{coin_id}",
        params={"localization": "false", "tickers": "true", "market_data": "false",
                "community_data": "false", "developer_data": "true", "sparkline": "false"},
        headers=_headers(demo_key),
    )
    return data if isinstance(data, dict) else {}


def build_track_a(http: HttpClient, pages: int, per_page: int,
                  demo_key: str = "") -> list[Candidate]:
    platform_map = fetch_platform_map(http, demo_key)
    markets = fetch_markets(http, pages, per_page, demo_key)
    return [m for m in (market_to_candidate(r, platform_map) for r in markets) if m]


def market_to_candidate(r: dict, platform_map: dict[str, dict[str, str]]) -> Candidate | None:
    coin_id = r.get("id", "")
    chain, address = _pick_chain(platform_map.get(coin_id, {}))

    ath_change = r.get("ath_change_percentage")   # отрицательное = насколько ниже ATH
    drawdown = abs(ath_change) if isinstance(ath_change, (int, float)) and ath_change < 0 else (
        0.0 if isinstance(ath_change, (int, float)) else None)

    return Candidate(
        source="coingecko", track="A",
        symbol=(r.get("symbol") or "").upper(), name=r.get("name") or "",
        coin_id=coin_id, chain=chain, address=address,
        price_usd=r.get("current_price"),
        market_cap=r.get("market_cap"),
        fdv=r.get("fully_diluted_valuation"),
        volume_24h=r.get("total_volume"),
        ath=r.get("ath"),
        drawdown_from_ath_pct=drawdown,
    )
