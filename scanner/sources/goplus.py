"""Stage 2 fetch: GoPlus Security API + honeypot.is (анти-раг). Free, ключ опционален.

EVM:    GET /api/v1/token_security/{chain_id}?contract_addresses=0x..
Solana: GET /api/v1/solana/token_security?contract_addresses=..

Возвращает сырой словарь безопасности по адресу; интерпретация — в stages/antirug.py
(разделение fetch/evaluate: evaluate тестируется офлайн на фикстурах).
"""
from __future__ import annotations

from ..chains import CHAINS, goplus_id, is_evm
from ..http import HttpClient

_GP = "https://api.gopluslabs.io/api/v1"
_HP = "https://api.honeypot.is/v2"


def fetch_goplus(http: HttpClient, chain: str, address: str, key: str = "") -> dict | None:
    gid = goplus_id(chain)
    if gid is None:
        return None
    headers = {"Authorization": key} if key else {}
    addr = address.lower()
    if gid == "solana":
        data = http.get_json(f"{_GP}/solana/token_security",
                             params={"contract_addresses": addr}, headers=headers)
    else:
        data = http.get_json(f"{_GP}/token_security/{gid}",
                             params={"contract_addresses": addr}, headers=headers)
    if not isinstance(data, dict):
        return None
    result = data.get("result") or {}   # API может вернуть {"result": null}
    # ключ результата может быть в другом регистре
    for k, v in result.items():
        if k.lower() == addr:
            return v
    # solana отдаёт по mint как есть
    return next(iter(result.values()), None) if result else None


def fetch_honeypot_is(http: HttpClient, chain: str, address: str) -> dict | None:
    if not is_evm(chain):
        return None
    chain_id = CHAINS[chain]["honeypot"]
    return http.get_json(f"{_HP}/IsHoneypot",
                        params={"address": address, "chainID": chain_id})
