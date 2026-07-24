"""Реестр сетей. Расширяемый — добавление сети = одна строка.

Приоритет по значимости (пользователь: «все сети, по мере значимости»).
Поля:
  goplus       — chain_id для GoPlus token_security (EVM) или 'solana'
  honeypot     — chainID для honeypot.is (только EVM; None если не поддерживается)
  cg_platform  — ключ платформы в CoinGecko /coins/list?include_platform=true
  gt_network   — network id в GeckoTerminal API
"""
from __future__ import annotations

CHAINS: dict[str, dict] = {
    "ethereum": {"goplus": "1", "honeypot": 1, "cg_platform": "ethereum", "gt_network": "eth", "priority": 1},
    "solana": {"goplus": "solana", "honeypot": None, "cg_platform": "solana", "gt_network": "solana", "priority": 2},
    "bsc": {"goplus": "56", "honeypot": 56, "cg_platform": "binance-smart-chain", "gt_network": "bsc", "priority": 3},
    "base": {"goplus": "8453", "honeypot": 8453, "cg_platform": "base", "gt_network": "base", "priority": 4},
    "arbitrum": {"goplus": "42161", "honeypot": 42161, "cg_platform": "arbitrum-one", "gt_network": "arbitrum", "priority": 5},
    "polygon": {"goplus": "137", "honeypot": 137, "cg_platform": "polygon-pos", "gt_network": "polygon_pos", "priority": 6},
}

# Обратный маппинг CoinGecko-платформы -> наш ключ сети.
CG_PLATFORM_TO_CHAIN = {v["cg_platform"]: k for k, v in CHAINS.items()}


def is_evm(chain: str) -> bool:
    return CHAINS.get(chain, {}).get("honeypot") is not None


def goplus_id(chain: str) -> str | None:
    return CHAINS.get(chain, {}).get("goplus")
