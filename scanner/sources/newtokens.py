"""Track B: свежие листинги из GeckoTerminal + DEXScreener (free, on-chain нативно).

GeckoTerminal /networks/{net}/new_pools — новые пулы (адрес токена, ликвидность, объём).
DEXScreener /token-profiles/latest — недавно добавленные профили токенов.

Оба возвращают контракт+сеть нативно → сразу пригодны для анти-рага (Stage 2).
"""
from __future__ import annotations

import calendar
import time

from ..chains import CHAINS
from ..http import HttpClient
from ..models import Candidate

_GT = "https://api.geckoterminal.com/api/v2"
_DS = "https://api.dexscreener.com"

# gt_network -> наш ключ сети
_GT_TO_CHAIN = {v["gt_network"]: k for k, v in CHAINS.items()}
_DS_TO_CHAIN = {  # dexscreener chainId -> наш ключ
    "ethereum": "ethereum", "solana": "solana", "bsc": "bsc",
    "base": "base", "arbitrum": "arbitrum", "polygon": "polygon",
}


def fetch_geckoterminal_new(http: HttpClient, chains: list[str]) -> list[Candidate]:
    out: list[Candidate] = []
    for chain in chains:
        net = CHAINS.get(chain, {}).get("gt_network")
        if not net:
            continue
        data = http.get_json(f"{_GT}/networks/{net}/new_pools",
                             headers={"Accept": "application/json;version=20230302"})
        pools = (data or {}).get("data", []) if isinstance(data, dict) else []
        for pool in pools:
            attrs = pool.get("attributes", {})
            rel = pool.get("relationships", {})
            base = rel.get("base_token", {}).get("data", {})
            # id вида "eth_0xabc..." — адрес после первого '_'
            token_id = base.get("id", "")
            address = token_id.split("_", 1)[1] if "_" in token_id else ""
            if not address:
                continue
            created = attrs.get("pool_created_at")
            age = _age_days(created)
            out.append(Candidate(
                source="geckoterminal", track="B",
                symbol="", name=attrs.get("name", ""),
                chain=chain, address=address,
                liquidity_usd=_f(attrs.get("reserve_in_usd")),
                volume_24h=_f((attrs.get("volume_usd") or {}).get("h24")),
                price_usd=_f(attrs.get("base_token_price_usd")),
                age_days=age,
            ))
    return out


def fetch_dexscreener_profiles(http: HttpClient, chains: list[str]) -> list[Candidate]:
    data = http.get_json(f"{_DS}/token-profiles/latest/v1")
    items = data if isinstance(data, list) else []
    out: list[Candidate] = []
    for it in items:
        chain = _DS_TO_CHAIN.get(it.get("chainId", ""))
        address = it.get("tokenAddress", "")
        if not chain or chain not in chains or not address:
            continue
        out.append(Candidate(
            source="dexscreener", track="B",
            symbol="", name="", chain=chain, address=address,
        ))
    return out


def build_track_b(http: HttpClient, chains: list[str]) -> list[Candidate]:
    cands = fetch_geckoterminal_new(http, chains) + fetch_dexscreener_profiles(http, chains)
    # дедуп по (chain, address)
    seen: set[tuple[str, str]] = set()
    uniq: list[Candidate] = []
    for c in cands:
        key = (c.chain, c.address.lower())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _age_days(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        # формат '2024-01-01T00:00:00Z'; timegm трактует как UTC (mktime — как локаль)
        t = time.strptime(iso_ts.replace("Z", "").split(".")[0], "%Y-%m-%dT%H:%M:%S")
        return (time.time() - calendar.timegm(t)) / 86400.0
    except (ValueError, TypeError):
        return None
