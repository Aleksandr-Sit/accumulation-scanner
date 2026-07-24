"""Stage 3: фундамент из DeFiLlama (free /protocols).

Матчинг кандидата к протоколу по gecko_id (совпадает с coin_id CoinGecko).
Даёт: TVL, категория, mcap, и — ключевое — медиану MC/TVL по сектору для
сравнения «недо/переоценён относительно пиров».

ВНИМАНИЕ: /raises и /emissions у DeFiLlama теперь платные (HTTP 402).
Точные разблокировки и раунды инвесторов на free-тарифе недоступны — помечаются
«нет данных» в stages/fundamentals.py, не выдумываются.
"""
from __future__ import annotations

from ..http import HttpClient

_BASE = "https://api.llama.fi"


class DefiLlamaIndex:
    """Индекс протоколов по gecko_id/symbol.

    Медиану MC/TVL по сектору здесь НЕ считаем: поле mcap в бесплатном /protocols
    почти всегда null. Медиана строится из наших кандидатов (market_cap из CoinGecko
    + TVL отсюда) в stages/fundamentals.build_category_medians().
    """
    def __init__(self, protocols: list[dict]):
        self.by_gecko: dict[str, dict] = {}
        self.by_symbol: dict[str, dict] = {}
        for p in protocols:
            gid = p.get("gecko_id")
            if gid:
                # несколько протоколов на один gecko_id — берём с макс. TVL.
                # ВНИМАНИЕ: это под-модуль, не родительский агрегат (напр. Aave V1
                # вместо суммы всех версий) — TVL может быть занижен. См. README.
                prev = self.by_gecko.get(gid)
                if prev is None or (p.get("tvl") or 0) > (prev.get("tvl") or 0):
                    self.by_gecko[gid] = p
            sym = (p.get("symbol") or "").upper()
            if sym and sym != "-":
                # при коллизии тикеров берём с макс. TVL (как by_gecko), а не первый
                prev = self.by_symbol.get(sym)
                if prev is None or (p.get("tvl") or 0) > (prev.get("tvl") or 0):
                    self.by_symbol[sym] = p

    def find(self, coin_id: str, symbol: str) -> tuple[dict | None, str]:
        """Возвращает (протокол, тип_матча): 'gecko' надёжен, 'symbol' — слабее
        (риск однофамильца), 'none'. Мусорный TVL от неверного матча дополнительно
        отсекается порогом min_tvl в attach_tvl."""
        if coin_id and coin_id in self.by_gecko:
            return self.by_gecko[coin_id], "gecko"
        if symbol and symbol.upper() in self.by_symbol:
            return self.by_symbol[symbol.upper()], "symbol"
        return None, "none"


def fetch_index(http: HttpClient) -> DefiLlamaIndex:
    data = http.get_json(f"{_BASE}/protocols")
    protocols = data if isinstance(data, list) else []
    return DefiLlamaIndex(protocols)
