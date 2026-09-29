"""Модель кандидата, проходящего воронку."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Candidate:
    # --- идентификация ---
    source: str                      # 'coingecko' | 'geckoterminal' | 'dexscreener'
    track: str                       # 'A' (спящие живые) | 'B' (свежие листинги)
    symbol: str
    name: str = ""
    coin_id: str = ""                # id CoinGecko, если есть
    chain: str = ""                  # ключ из chains.CHAINS
    address: str = ""                # адрес контракта (обязателен для анти-рага)

    # --- рыночные поля (Stage 0) ---
    price_usd: float | None = None
    market_cap: float | None = None
    fdv: float | None = None
    volume_24h: float | None = None
    liquidity_usd: float | None = None
    ath: float | None = None
    drawdown_from_ath_pct: float | None = None   # 0..100, сколько упал от ATH
    age_days: float | None = None

    # --- фундамент / tokenomics (Stage 3) ---
    tvl: float | None = None
    mc_tvl: float | None = None            # market_cap / TVL (ниже = дешевле к залоку)
    fdv_mc: float | None = None            # FDV / market_cap (выше = навес дилюции)
    category: str = ""                     # сектор из DeFiLlama
    val_notes: list[str] = field(default_factory=list)   # findings с тегами источника

    # --- живость / listings / LP (Stage 3b) ---
    dev_commits_4w: int | None = None      # коммиты за 4 недели (0 = проект встал)
    dev_contributors: int | None = None
    dev_stars: int | None = None
    n_exchanges: int | None = None         # число рынков (CoinGecko tickers)
    on_cex: bool = False                   # есть ли CEX-листинг (не только DEX)
    holder_count: int | None = None        # число холдеров (GoPlus)
    lp_locked_pct: float | None = None     # % LP заблокирован/сожжён (None = нет данных)
    wash_ratio: float | None = None        # volume_24h / liquidity (аномально высокое = накрутка)
    liveness_score: float | None = None    # под-балл 0–10 (None = нет данных)
    liveness_notes: list[str] = field(default_factory=list)

    # --- зона и доступ (Stage 4) ---
    zone: str = ""                         # ПРУЖИНА/ДНО | СЕРЕДИНА | ПИК | ПАДАЮЩИЙ_НОЖ | ?
    zone_signals: list[str] = field(default_factory=list)
    indicators: dict = field(default_factory=dict)       # vol-contraction, trend, range-pos…
    market_dd: float | None = None         # просадка BTC от ATH (0..1) — контекст рынка
    alt_market_dd: float | None = None     # просадка альт-рынка без стейблов от ATH (0..1)
    market_hot_score: float | None = None  # индекс перегрева рынка: доля горящих флагов (0..1)
    market_hot_lit: list[str] = field(default_factory=list)  # какие флаги перегрева горят
    spring_quality: float | None = None    # множитель качества пружины (feature_study)
    funding_rate: float | None = None      # фандинг перпа Bybit (доля/8h): <0 капитуляция, >0 эйфория

    # --- монетный контекст (Stage 4d, информационный — в скор не входит) ---
    supply_growth: float | None = None     # рост предложения за окно истории (mcap/price)
    revenue_30d: float | None = None       # выручка протокола за 30д, $ (DeFiLlama)
    p_f: float | None = None               # капа / годовая выручка
    oi_mcap: float | None = None           # OI перпа Bybit / капа (плечо на монете)
    delist: str = ""                       # spot | perp | st — риск делистинга Bybit
    us_tag: str = ""                       # etf | coinbase — регулируемый доступ из США

    # --- on-chain накопление (Dune, опц.) ---
    net_flow_usd_7d: float | None = None    # нетто-поток на биржи 7д (<0 = отток = накопление)
    holders_change_pct_7d: float | None = None  # изменение числа холдеров 7д (%)
    onchain_score: float | None = None      # под-балл 0–10 (None = нет данных Dune)
    rf_venue: str = ""                     # Bybit spot | DEX only | нет
    rf_access: bool = True                 # доступен ли гражданину РФ легально

    # --- композитный скор (Stage 5) ---
    score: float = 0.0                     # 0–100, приоритизация (НЕ сигнал входа)
    confidence: float = 0.0                # 0–1, доля блоков с данными
    score_breakdown: dict = field(default_factory=dict)

    # --- аннотации воронки ---
    spring_prefilter: bool = False   # прошёл предфильтр «сжатой пружины» (drawdown велик)
    security: dict[str, Any] = field(default_factory=dict)   # сырой ответ анти-рага
    flags: list[str] = field(default_factory=list)           # предупреждения (mintable, proxy…)
    reject_reasons: list[str] = field(default_factory=list)  # почему отклонён
    manual_review: bool = False
    stage: str = "0"                 # докуда дошёл: '0'|'1'|'2'|'watchlist'|'rejected'

    def reject(self, reason: str) -> None:
        self.reject_reasons.append(reason)
        self.stage = "rejected"

    def to_row(self) -> dict[str, Any]:
        return asdict(self)
