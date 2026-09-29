"""Фандинг перпов Bybit — сигнал дна/вершины, ортогональный цене/объёму.

Один вызов /v5/market/tickers?category=linear отдаёт fundingRate по ВСЕМ ~680
USDT-перпам → строим map baseCoin -> ставка/8h. Дёшево (1 вызов на прогон).

Логика (feature_study-независимая, из механики рынка):
  • Экстремально ОТРИЦАТЕЛЬНЫЙ фандинг = шорты платят лонгам = толпа в шортах =
    капитуляция/сжатая пружина у ДНА (шорт-сквиз как топливо отскока).
  • Экстремально ПОЛОЖИТЕЛЬНЫЙ = лонги платят = эйфория/перегрев у ВЕРШИНЫ
    (распределение). Медиана рынка ~+0.005%/8h; хвосты ±0.05%+ уже значимы.

Не все монеты имеют перп → None (нейтрально, не штрафуем).
"""
from __future__ import annotations

from ..http import HttpClient

_URL = "https://api.bybit.com/v5/market/tickers"


def fetch_funding_map(http: HttpClient) -> dict[str, float]:
    """baseCoin(UPPER) -> fundingRate (доля/8h). Один вызов на все линейные перпы."""
    data = http.get_json(_URL, params={"category": "linear"})
    out: dict[str, float] = {}
    if not isinstance(data, dict):
        return out
    for it in (data.get("result", {}) or {}).get("list", []) or []:
        sym = it.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4].upper()
        fr = it.get("fundingRate")
        if fr in (None, ""):
            continue
        try:
            # первый перп по тикеру выигрывает (мультипликаторные 1000X — мимо)
            out.setdefault(base, float(fr))
        except (ValueError, TypeError):
            continue
    return out
