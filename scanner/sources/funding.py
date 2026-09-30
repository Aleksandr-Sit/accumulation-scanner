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

import re

from ..http import HttpClient

_URL = "https://api.bybit.com/v5/market/tickers"
_MULT_PREFIX = re.compile(r"^(10+)([A-Z].*)$")   # 1000PEPE, 10000SATS, 1000000MOG


def parse_funding(data) -> dict[str, float]:
    """Ответ /v5/market/tickers -> baseCoin(UPPER) -> fundingRate (доля/8h).

    Чистая функция (офлайн-тест). Мультипликаторные контракты (1000PEPEUSDT)
    мапятся и на базовый тикер (PEPE): ставка фандинга от номинала не зависит.
    Прямой листинг тикера приоритетнее мультипликаторного.
    """
    out: dict[str, float] = {}
    mult: dict[str, float] = {}
    if not isinstance(data, dict):
        return out
    for it in (data.get("result", {}) or {}).get("list", []) or []:
        sym = (it.get("symbol") or "").upper()
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        fr = it.get("fundingRate")
        if fr in (None, ""):
            continue
        try:
            rate = float(fr)
        except (ValueError, TypeError):
            continue
        out.setdefault(base, rate)
        m = _MULT_PREFIX.match(base)
        if m:
            mult.setdefault(m.group(2), rate)
    for base, rate in mult.items():
        out.setdefault(base, rate)
    return out


def fetch_funding_map(http: HttpClient) -> dict[str, float]:
    """baseCoin(UPPER) -> fundingRate (доля/8h). Один вызов на все линейные перпы."""
    return parse_funding(http.get_json(_URL, params={"category": "linear"}))
