"""Монетный контекст (free, по 1 вызову на прогон на источник; проверено 29.09.2026).

  • DeFiLlama /overview/fees?dataType=dailyRevenue — выручка протоколов за 30д.
    У дочерних протоколов (Uniswap V3, Aave V3…) нет gecko_id -> связываем через
    defillamaId с /protocols (символ, gecko_id) и суммируем по parentProtocol.
  • Bybit /v5/market/tickers linear — open interest перпа по монете (USD).
  • Bybit /v5/announcements (type=delistings) — заголовки делистингов; тикеры есть
    только в части заголовков («Delisting of L3,VIC», «Delisting of ICXUSDT Perpetual
    Contract»); статьи с этой машины недоступны. Плюс stTag=1 в spot instruments-info
    (пометка Bybit «под наблюдением»).
  • Coinbase Exchange /products — листинг на регулируемой бирже США (прокси «US-тега»).
Разметка — чистые функции в stages/coin_context.py; в скор НЕ входит (не провалидировано).
"""
from __future__ import annotations

import re
import time

from ..http import HttpClient

_LLAMA = "https://api.llama.fi"
_BYBIT = "https://api.bybit.com"
_COINBASE = "https://api.exchange.coinbase.com/products"


def fetch_revenue_30d(http: HttpClient) -> dict[str, dict[str, float]]:
    """{"by_gecko": {gecko_id: $}, "by_symbol": {SYMBOL: $}} — выручка протокола за 30д,
    агрегированная по родительскому протоколу (все версии/продукты)."""
    ov = http.get_json(f"{_LLAMA}/overview/fees",
                       params={"excludeTotalDataChart": "true",
                               "excludeTotalDataChartBreakdown": "true",
                               "dataType": "dailyRevenue"})
    prots = http.get_json(f"{_LLAMA}/protocols")
    if not isinstance(ov, dict) or not isinstance(prots, list):
        return {"by_gecko": {}, "by_symbol": {}}
    meta = {str(p.get("id")): p for p in prots if p.get("id") is not None}
    groups: dict[str, float] = {}
    gsym: dict[str, dict[str, int]] = {}
    ggecko: dict[str, str] = {}
    for r in ov.get("protocols", []) or []:
        rev = r.get("total30d")
        if not isinstance(rev, (int, float)) or rev <= 0:
            continue
        m = meta.get(str(r.get("defillamaId")), {})
        key = r.get("parentProtocol") or m.get("parentProtocol") or f"id#{r.get('defillamaId')}"
        groups[key] = groups.get(key, 0.0) + float(rev)
        sym = (m.get("symbol") or "").upper()
        if sym and sym != "-":
            gsym.setdefault(key, {})
            gsym[key][sym] = gsym[key].get(sym, 0) + 1
        if m.get("gecko_id"):
            ggecko[key] = m["gecko_id"]
    by_symbol: dict[str, float] = {}
    by_gecko: dict[str, float] = {}
    for key, rev in groups.items():
        if key in gsym:
            sym = max(gsym[key].items(), key=lambda kv: kv[1])[0]
            by_symbol[sym] = max(by_symbol.get(sym, 0.0), rev)   # коллизия тикеров -> больший
        if key in ggecko:
            by_gecko[ggecko[key]] = rev
        elif key.startswith("parent#"):
            by_gecko.setdefault(key[len("parent#"):], rev)       # parent#aave -> aave
    return {"by_gecko": by_gecko, "by_symbol": by_symbol}


def fetch_perp_oi(http: HttpClient) -> dict[str, float]:
    """baseCoin -> open interest перпа Bybit в USD (тот же вызов, что фандинг: кэш)."""
    d = http.get_json(f"{_BYBIT}/v5/market/tickers", params={"category": "linear"})
    out: dict[str, float] = {}
    for it in ((d or {}).get("result") or {}).get("list") or [] if isinstance(d, dict) else []:
        sym = it.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            out.setdefault(sym[:-4].upper(), float(it.get("openInterestValue") or 0))
        except (ValueError, TypeError):
            continue
    return out


_DELIST_OF = re.compile(r"Delisting of ([A-Z0-9 ,]+?)(?:\s+(?:Perpetual|on|and)\b|$)")


def parse_delist_title(title: str) -> tuple[list[str], bool]:
    """Тикеры из заголовка Bybit и признак «только перп». Нет тикеров -> ([], …)."""
    m = _DELIST_OF.search(title or "")
    if not m:
        return [], False
    perp_only = "Perpetual" in title
    out = []
    for tok in m.group(1).split(","):
        tok = tok.strip().upper()
        if not tok or " " in tok:
            continue
        if tok.endswith("USDT"):
            tok = tok[:-4]
        out.append(tok)
    return out, perp_only


def fetch_delistings(http: HttpClient, days: int = 90) -> dict[str, str]:
    """{TICKER: 'spot' | 'perp' | 'st'} — делистинг спота/перпа за days или метка ST."""
    out: dict[str, str] = {}
    inst = http.get_json(f"{_BYBIT}/v5/market/instruments-info",
                         params={"category": "spot", "limit": 1000})
    for it in ((inst or {}).get("result") or {}).get("list") or [] if isinstance(inst, dict) else []:
        if it.get("stTag") == "1" and it.get("baseCoin"):
            out[it["baseCoin"].upper()] = "st"
    d = http.get_json(f"{_BYBIT}/v5/announcements/index",
                      params={"locale": "en-US", "type": "delistings", "limit": 100})
    cutoff = (time.time() - days * 86400) * 1000
    for r in ((d or {}).get("result") or {}).get("list") or [] if isinstance(d, dict) else []:
        if (r.get("publishTime") or 0) < cutoff:
            continue
        toks, perp_only = parse_delist_title(r.get("title", ""))
        for t in toks:
            kind = "perp" if perp_only else "spot"
            if out.get(t) != "spot":          # spot — самый жёсткий, не понижаем
                out[t] = kind
    return out


def fetch_coinbase_bases(http: HttpClient) -> set[str]:
    """Базовые активы, торгующиеся на Coinbase Exchange (status=online)."""
    d = http.get_json(_COINBASE, headers={"User-Agent": "Mozilla/5.0"})
    return {p["base_currency"].upper() for p in d if isinstance(p, dict)
            and p.get("status") == "online" and p.get("base_currency")} if isinstance(d, list) else set()


def load(cfg, http: HttpClient) -> dict:
    """Все монетные справочники одним вызовом; сбой источника -> пустой справочник."""
    if not cfg.get("coin_context.enabled", True):
        return {}
    out: dict = {}
    for name, fn in (("revenue", lambda: fetch_revenue_30d(http)),
                     ("oi", lambda: fetch_perp_oi(http)),
                     ("delist", lambda: fetch_delistings(http, cfg.get("coin_context.delist_days", 90))),
                     ("coinbase", lambda: fetch_coinbase_bases(http))):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001 — контекст не должен ронять скан
            print(f"[coin_context] {name}: {e}")
            out[name] = {}
    return out
