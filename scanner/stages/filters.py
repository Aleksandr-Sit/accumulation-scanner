"""Stage 1 — дешёвые фильтры (только поля Stage 0, без доп. сетевых вызовов).

Чистая функция: список кандидатов -> (прошедшие, отклонённые). Тестируется офлайн.
Отсекает нетоварные/мусор и помечает предфильтр «сжатой пружины».
"""
from __future__ import annotations

from ..config import Config
from ..models import Candidate

# Символы стейблов (усечённый список; расширяемо). Исключаем — не для откупа.
_STABLES = {
    "USDT", "USDC", "DAI", "TUSD", "USDE", "FDUSD", "USDD", "FRAX", "LUSD",
    "PYUSD", "GUSD", "USDP", "SUSD", "USDS", "CRVUSD", "GHO", "USD0", "BUSD",
}
# Подстроки в имени для wrapped / liquid-staking деривативов.
_WRAP_LST_NAME = ("wrapped", "staked ", "liquid staking", "restaked", "bridged")
_WRAP_LST_SYM = ("WBTC", "WETH", "WBNB", "STETH", "WSTETH", "RETH", "CBETH", "WEETH",
                 "JITOSOL", "MSOL", "JUPSOL", "BSC-USD")


def _is_stable(c: Candidate) -> bool:
    return c.symbol in _STABLES


def _is_wrapped_lst(c: Candidate) -> bool:
    if c.symbol in _WRAP_LST_SYM:
        return True
    name = (c.name or "").lower()
    return any(s in name for s in _WRAP_LST_NAME)


def apply_filters(candidates: list[Candidate], cfg: Config) -> tuple[list[Candidate], list[Candidate]]:
    f = cfg["stage1_filters"]
    min_vol = f["min_volume_24h_usd"]
    min_liq = f["min_liquidity_usd"]
    max_age = f["track_b_max_age_days"]
    spring_dd = f["spring_min_drawdown_from_ath_pct"]

    passed: list[Candidate] = []
    rejected: list[Candidate] = []

    for c in candidates:
        c.stage = "1"

        if cfg.get("stage1_filters.exclude_stablecoins", True) and _is_stable(c):
            c.reject("stablecoin")
            rejected.append(c); continue
        if cfg.get("stage1_filters.exclude_wrapped_lst", True) and _is_wrapped_lst(c):
            c.reject("wrapped/LST derivative")
            rejected.append(c); continue

        # Ликвидность/объём — нужно уметь выйти. Проверяем по тому, что есть.
        if c.volume_24h is not None and c.volume_24h < min_vol:
            c.reject(f"volume<{min_vol}")
            rejected.append(c); continue
        if c.liquidity_usd is not None and c.liquidity_usd < min_liq:
            c.reject(f"liquidity<{min_liq}")
            rejected.append(c); continue
        # Track B без объёма И без ликвидности (DEXScreener-профили) — торгуемость
        # непроверяема, вслепую в воронку не пускаем (экономим лимит GoPlus).
        if c.track == "B" and c.volume_24h is None and c.liquidity_usd is None:
            c.manual_review = True
            c.flags.append("no_tradability_data")
            c.reject("track B: no volume/liquidity data")
            rejected.append(c); continue

        # Track B: возраст свежести (если известен).
        if c.track == "B" and c.age_days is not None and c.age_days > max_age:
            c.reject(f"age>{max_age}d (not fresh)")
            rejected.append(c); continue

        # Анти-раг требует контракт+сеть. Без адреса кандидат непроверяем на легитимность.
        if not c.address or not c.chain:
            c.reject("no contract/chain (anti-rug impossible)")
            rejected.append(c); continue

        # Предфильтр «пружины»: сильная просадка от ATH (Track A).
        if c.drawdown_from_ath_pct is not None and c.drawdown_from_ath_pct >= spring_dd:
            c.spring_prefilter = True

        if f.get("spring_hard_filter") and c.track == "A" and not c.spring_prefilter:
            c.reject(f"drawdown<{spring_dd}% (not a spring)")
            rejected.append(c); continue

        passed.append(c)

    return passed, rejected
