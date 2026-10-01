"""Офлайн-проверка логики воронки на фикстурах (без сети).

Покрывает чистые функции: Stage 1 фильтры и Stage 2 интерпретацию анти-рага.
Запуск: python run.py selftest  (или python -m tests.selftest)
"""
from __future__ import annotations

from scanner.config import load_config
from scanner.models import Candidate
from scanner.positions import PositionStore, risk_check
from scanner.stages.filters import apply_filters
from scanner.stages import antirug
from scanner.stages import exit as exit_stage
from scanner.stages.fundamentals import assess_valuation
from scanner.stages import zone
from scanner.stages.access import rf_gate
from scanner.stages.score import compute_score
from scanner.notify.telegram import (format_alert, format_digest, format_exit_alert,
                                     format_weekly, sparkline)

_PASS = "\033[92mPASS\033[0m"
_FAIL = "\033[91mFAIL\033[0m"


def _check(name: str, cond: bool, failures: list[str]) -> None:
    print(f"  [{_PASS if cond else _FAIL}] {name}")
    if not cond:
        failures.append(name)


def test_filters(cfg, failures: list[str]) -> None:
    print("Stage 1 — фильтры:")
    cands = [
        # прошёл: живой альт, глубокая просадка -> пружина
        Candidate(source="coingecko", track="A", symbol="ARB", name="Arbitrum",
                  chain="arbitrum", address="0xabc", volume_24h=5_000_000,
                  drawdown_from_ath_pct=85.0),
        # стейбл -> reject
        Candidate(source="coingecko", track="A", symbol="USDT", name="Tether",
                  chain="ethereum", address="0xdef", volume_24h=9e9,
                  drawdown_from_ath_pct=1.0),
        # wrapped -> reject
        Candidate(source="coingecko", track="A", symbol="WBTC", name="Wrapped Bitcoin",
                  chain="ethereum", address="0x111", volume_24h=1e8),
        # тонкий объём -> reject
        Candidate(source="coingecko", track="A", symbol="DUST", name="Dust",
                  chain="ethereum", address="0x222", volume_24h=100.0,
                  drawdown_from_ath_pct=90.0),
        # нет контракта -> reject (анти-раг невозможен)
        Candidate(source="coingecko", track="A", symbol="NOADDR", name="NoAddr",
                  chain="", address="", volume_24h=1e6, drawdown_from_ath_pct=80.0),
        # Track B: слишком старый -> reject
        Candidate(source="geckoterminal", track="B", symbol="OLD", name="Old pool",
                  chain="base", address="0x333", liquidity_usd=500_000,
                  volume_24h=1e6, age_days=400.0),
        # Track B: ни объёма, ни ликвидности (DEXScreener-профиль) -> manual_review
        Candidate(source="dexscreener", track="B", symbol="BLIND", name="",
                  chain="solana", address="mint111"),
    ]
    passed, rejected = apply_filters(cands, cfg)
    psyms = {c.symbol for c in passed}

    _check("ARB прошёл", "ARB" in psyms, failures)
    _check("ARB помечен как пружина", any(c.symbol == "ARB" and c.spring_prefilter for c in passed), failures)
    _check("USDT отклонён (стейбл)", "USDT" not in psyms, failures)
    _check("WBTC отклонён (wrapped)", "WBTC" not in psyms, failures)
    _check("DUST отклонён (объём)", "DUST" not in psyms, failures)
    _check("NOADDR отклонён (нет контракта)", "NOADDR" not in psyms, failures)
    _check("OLD отклонён (возраст)", "OLD" not in psyms, failures)
    blind = next(c for c in rejected if c.symbol == "BLIND")
    _check("BLIND (без vol/liq) отклонён + manual_review",
           blind.manual_review and "no_tradability_data" in blind.flags, failures)
    _check("итог: ровно 1 прошёл", len(passed) == 1, failures)


def test_track_q(cfg, failures: list[str]) -> None:
    print("Трек Q — монеты без контракта через гейт качества:")
    import json as _json
    import os
    import tempfile
    import time as _t
    from scanner.pipeline import stage2_antirug
    from scanner.quality import load_quality
    qual = {"BTC": {"sym": "BTC", "mcap": 1.6e12, "fails": []},
            "FAKE": {"sym": "FAKE", "mcap": 5e9, "fails": []}}
    mk = lambda sym, mcap, **kw: Candidate(source="coingecko", track="A", symbol=sym,  # noqa: E731
                                           coin_id=sym.lower(), volume_24h=1e9,
                                           market_cap=mcap, **kw)
    cands = [mk("BTC", 1.65e12, drawdown_from_ath_pct=33.0),
             mk("BTC", 1.65e12),                        # тёзка/дубль — только первое вхождение
             mk("XRP", 1.5e11),                         # без контракта и не в срезе
             mk("FAKE", 4e7),                           # тикер совпал, капа в 125 раз меньше
             mk("ARB", 1e9, chain="arbitrum", address="0xabc")]
    passed, rejected = apply_filters(cands, cfg, qual)
    q = [c for c in passed if c.track == "Q"]
    _check("BTC без контракта из среза -> трек Q", len(q) == 1 and q[0].symbol == "BTC"
           and "no_contract_quality_gate" in q[0].flags, failures)
    _check("дубль тикера, XRP вне среза, тёзка по капе -> отклонены",
           sorted(c.symbol for c in rejected) == ["BTC", "FAKE", "XRP"], failures)
    _check("монета с контрактом — обычный Track A", any(c.symbol == "ARB" and c.track == "A"
                                                          for c in passed), failures)
    _check("без среза поведение прежнее (BTC отклонён)",
           not any(c.track == "Q" for c in apply_filters([mk("BTC", 1.65e12)], cfg)[0]), failures)
    wl, rj = stage2_antirug(cfg, None, q)               # сети нет: Q анти-раг не вызывает
    _check("Stage 2: трек Q идёт в watchlist без запроса анти-рага", len(wl) == 1 and not rj,
           failures)
    wl[0].zone, wl[0].rf_venue = "ПРУЖИНА/ДНО", "Bybit spot"
    s, conf, brk = compute_score(wl[0], cfg)
    _check("скор Q считается (safety 10 — гейт вместо контракта)",
           brk["safety"]["sub"] == 10.0 and s > 0, failures)

    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    day = _t.strftime("%Y-%m-%d", _t.localtime())
    _json.dump({"date": day, "rows": [{"sym": "BTC", "mcap": 1, "fails": []},
                                      {"sym": "DOGE", "mcap": 1, "fails": ["мем"]}]}, tmp)
    tmp.close()
    fresh = load_quality(tmp.name, 30)
    stale = load_quality(tmp.name, 30, now=_t.time() + 60 * 86400)
    os.unlink(tmp.name)
    _check("срез: берутся только прошедшие (BTC, не DOGE)",
           fresh["ok"] and list(fresh["by_sym"]) == ["BTC"], failures)
    _check("срез старше max_age_days -> пусто + подсказка обновить",
           not stale["ok"] and not stale["by_sym"] and "обнови" in stale["note"], failures)
    _check("нет файла -> пусто, без падения", not load_quality("nope/none.json")["ok"], failures)

    # Авто-paper: близнец S (стоп −50%) — только монетам фильтра качества.
    from scanner.pipeline import _open_paper_positions
    spring = lambda sym, mcap, track="A": Candidate(  # noqa: E731
        source="t", track=track, symbol=sym, coin_id=sym.lower(), price_usd=2.0,
        market_cap=mcap, zone="ПРУЖИНА/ДНО", score=80.0, confidence=0.9)
    wl = [spring("BTC", 1.6e12, "Q"), spring("LINK", 9e9), spring("JUNK", 5e8),
          spring("FAKE", 4e7)]                              # FAKE: тикер в срезе, капа нет
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()
    saved = cfg["output"]["db_path"]
    cfg["output"]["db_path"] = tmp_db.name
    try:
        n = _open_paper_positions(cfg, wl, {**qual, "LINK": {"sym": "LINK", "mcap": 1e10}})
        ps = PositionStore(tmp_db.name)
        rows = ps.all_positions()
        ps.close_db()
    finally:
        cfg["output"]["db_path"] = saved
        os.unlink(tmp_db.name)
    by = {}
    for r in rows:
        by.setdefault(r["variant"], []).append(r["symbol"])
    _check("paper: 4 позиции A и 4 близнеца B", n == 4 and len(by.get("A", [])) == 4
           and len(by.get("B", [])) == 4, failures)
    _check("paper: близнецы S — только BTC (трек Q) и LINK (срез + капа)",
           sorted(by.get("S", [])) == ["BTC", "LINK"], failures)


def test_antirug_evm(cfg, failures: list[str]) -> None:
    print("Stage 2 — анти-раг (EVM):")

    clean = {"is_honeypot": "0", "buy_tax": "0.03", "sell_tax": "0.03",
             "is_open_source": "1", "cannot_sell_all": "0",
             "holders": [{"percent": "0.05"}, {"percent": "0.04"}]}
    ok, reasons, _ = antirug.evaluate_goplus_evm(clean, cfg)
    _check("чистый токен проходит", ok and not reasons, failures)

    honey = {"is_honeypot": "1", "buy_tax": "0", "sell_tax": "0", "is_open_source": "1"}
    ok, reasons, _ = antirug.evaluate_goplus_evm(honey, cfg)
    _check("honeypot отклонён", not ok and "honeypot" in reasons, failures)

    taxed = {"is_honeypot": "0", "buy_tax": "0.40", "sell_tax": "0.50", "is_open_source": "1"}
    ok, reasons, _ = antirug.evaluate_goplus_evm(taxed, cfg)
    _check("грабительский налог отклонён", not ok and any("sell_tax" in r for r in reasons), failures)

    concentrated = {"is_honeypot": "0", "buy_tax": "0", "sell_tax": "0", "is_open_source": "1",
                    "holders": [{"percent": "0.85"}]}
    ok, reasons, _ = antirug.evaluate_goplus_evm(concentrated, cfg)
    _check("концентрация холдеров отклонена", not ok and any("top10" in r for r in reasons), failures)

    unverified = {"is_honeypot": "0", "buy_tax": "0", "sell_tax": "0", "is_open_source": "0"}
    ok, reasons, _ = antirug.evaluate_goplus_evm(unverified, cfg)
    _check("неверифицированный контракт отклонён", not ok and "not_open_source" in reasons, failures)

    mintable = {"is_honeypot": "0", "buy_tax": "0", "sell_tax": "0",
                "is_open_source": "1", "is_mintable": "1"}
    ok, reasons, flags = antirug.evaluate_goplus_evm(mintable, cfg)
    _check("mintable проходит но флагается (дефолт)", ok and "mintable" in flags, failures)


def test_honeypot_is(cfg, failures: list[str]) -> None:
    print("Stage 2 — honeypot.is (вторичное подтверждение):")
    ok, _ = antirug.evaluate_honeypot_is({"honeypotResult": {"isHoneypot": False}})
    _check("не-ханипот проходит", ok, failures)
    ok, reasons = antirug.evaluate_honeypot_is({"honeypotResult": {"isHoneypot": True}})
    _check("ханипот отклонён", not ok and reasons, failures)


def test_fundamentals(cfg, failures: list[str]) -> None:
    print("Stage 3 — фундамент/оценка:")

    # Недооценён к сектору: MC/TVL сильно ниже медианы.
    m, notes, flags = assess_valuation(
        market_cap=100e6, fdv=120e6, tvl=400e6,  # MC/TVL=0.25
        category="Dexes", category_median_mc_tvl=1.0, peer_count=50, cfg=cfg)
    _check("MC/TVL посчитан", m["mc_tvl"] == 0.25, failures)
    _check("недооценён к сектору флаг", "undervalued_vs_sector" in flags, failures)
    _check("умеренная дилюция (FDV/MC=1.2)", "high_dilution" not in flags, failures)

    # Переоценён + высокая дилюция.
    m, notes, flags = assess_valuation(
        market_cap=100e6, fdv=500e6, tvl=20e6,   # MC/TVL=5.0, FDV/MC=5
        category="Dexes", category_median_mc_tvl=1.0, peer_count=50, cfg=cfg)
    _check("переоценён к сектору флаг", "overvalued_vs_sector" in flags, failures)
    _check("высокая дилюция флаг (FDV/MC=5)", "high_dilution" in flags, failures)

    # Нет TVL (не DeFi) — честный «нет данных», без падения.
    m, notes, flags = assess_valuation(
        market_cap=100e6, fdv=100e6, tvl=None,
        category="Unknown", category_median_mc_tvl=None, peer_count=0, cfg=cfg)
    _check("нет TVL -> mc_tvl None", m["mc_tvl"] is None, failures)
    _check("есть пометка про unlocks pro-only", any("разблокировки" in n for n in notes), failures)

    # Мало пиров -> без вывода недо/пере.
    m, notes, flags = assess_valuation(
        market_cap=100e6, fdv=100e6, tvl=50e6,
        category="Niche", category_median_mc_tvl=0.5, peer_count=2, cfg=cfg)
    _check("мало пиров -> нет флага оценки", not ({"undervalued_vs_sector", "overvalued_vs_sector"} & set(flags)), failures)


def test_zone(cfg, failures: list[str]) -> None:
    print("Stage 4 — детектор зоны:")
    z = cfg["stage4_zone"]
    rec, sma = z["recent_days"], z["sma_days"]

    # Пружина: рваный обвал 100→20 (высокая vol), затем 30 дней ровно у дна (сжатие).
    spring = [100 - 80 * i / 149 + (3 if i % 2 else -3) for i in range(150)] + [20.0] * 30
    ind = zone.compute_indicators(spring, rec, sma)
    zl, _ = zone.classify_zone(ind, 85.0, cfg)
    _check("глубокое дно + сжатие -> ПРУЖИНА/ДНО", zl == "ПРУЖИНА/ДНО", failures)

    # Пик: рост с ускорением в конце, у максимума, около ATH.
    peak = [20 + 30 * i / 149 for i in range(150)] + [50 + 50 * i / 29 for i in range(30)]
    ind = zone.compute_indicators(peak, rec, sma)
    zl, _ = zone.classify_zone(ind, 3.0, cfg)
    _check("около ATH + над SMA + верх диапазона -> ПИК", zl == "ПИК", failures)

    # Падающий нож: плоско, затем резкий обвал -30% за последние 30 дней.
    knife = [100.0] * 150 + [100 - 30 * i / 29 for i in range(30)]
    ind = zone.compute_indicators(knife, rec, sma)
    zl, _ = zone.classify_zone(ind, 40.0, cfg)
    _check("активный обвал -> ПАДАЮЩИЙ_НОЖ", zl == "ПАДАЮЩИЙ_НОЖ", failures)

    # Нет истории -> '?'
    zl, _ = zone.classify_zone(None, 80.0, cfg)
    _check("нет истории -> ?", zl == "?", failures)

    # Гейт days_since_low: конфигурируемый, ПО УМОЛЧАНИЮ ВЫКЛЮЧЕН (0) — walk-forward
    # (detector_study) показал, что фильтр «лоу давно не обновлялся» ухудшает форвард.
    # Проверяем механику гейта с явным порогом, затем дефолт.
    bleed = ([100 - 60 * i / 149 + (4 if i % 2 else -4) for i in range(150)]  # рваный спад до ~40
             + [40 - 4 * i / 29 for i in range(30)])                          # тихое сползание к 36
    ind = zone.compute_indicators(bleed, rec, sma)
    _check("истечение: лоу обновлён недавно (days_since_low<14)",
           ind["days_since_low"] < 14, failures)
    saved = cfg["stage4_zone"].get("spring_min_days_since_low", 0)
    cfg["stage4_zone"]["spring_min_days_since_low"] = 14      # включаем гейт явно
    zl, _ = zone.classify_zone(ind, 85.0, cfg)
    _check("гейт=14: свежий лоу блокирует пружину", zl != "ПРУЖИНА/ДНО", failures)
    cfg["stage4_zone"]["spring_min_days_since_low"] = saved   # дефолт (выключен)
    zl, _ = zone.classify_zone(ind, 85.0, cfg)
    _check("гейт выключен (дефолт по данным) -> пружина допускается",
           zl == "ПРУЖИНА/ДНО", failures)

    # Пружина с усыханием объёма: сигнал накопления попадает в описание.
    spring2 = [100 - 80 * i / 149 + (3 if i % 2 else -3) for i in range(150)] + [20.0] * 30
    vols = [1e6] * 150 + [3e5] * 30   # объём усох втрое у дна
    ind = zone.compute_indicators(spring2, rec, sma, vols)
    _check("vol_trend посчитан и <0.6", ind["vol_trend"] is not None and ind["vol_trend"] < 0.6, failures)
    zl, sigs = zone.classify_zone(ind, 85.0, cfg)
    _check("пружина + dry-up -> сигнал накопления",
           zl == "ПРУЖИНА/ДНО" and any("усох" in s for s in sigs), failures)

    # Без volumes индикаторы работают как раньше (vol_trend=None, зона не ломается).
    ind = zone.compute_indicators(spring2, rec, sma)
    _check("без объёмов vol_trend=None, зона работает",
           ind["vol_trend"] is None and zone.classify_zone(ind, 85.0, cfg)[0] == "ПРУЖИНА/ДНО",
           failures)


def test_closed_daily(cfg, failures: list[str]) -> None:
    print("closed_daily — отбрасывание live-тика:")
    from scanner.sources.coingecko import closed_daily
    day = 86400
    # два закрытия 00:00 UTC + live-тик 18:37 -> должна отвалиться последняя точка
    live = {"ts": [10 * day, 11 * day, 11 * day + 67020],
            "prices": [100.0, 101.0, 95.0], "volumes": [1, 2, 3]}
    out = closed_daily(live)
    _check("live-тик отброшен (осталось 2 закрытия)", out["prices"] == [100.0, 101.0], failures)
    # все точки выровнены на полночь -> ничего не трогаем
    clean = {"ts": [10 * day, 11 * day], "prices": [100.0, 101.0], "volumes": [1, 2]}
    _check("чистые закрытия не трогаем", closed_daily(clean)["prices"] == [100.0, 101.0], failures)


def test_retry_wait(cfg, failures: list[str]) -> None:
    print("http.retry_wait — пауза перед повтором:")
    import tempfile
    from scanner.http import retry_wait
    _check("429 без Retry-After ждёт окно 60 с", retry_wait(429, None, 2.0) == 60.0, failures)
    _check("429 с Retry-After: 30 -> 31 с", retry_wait(429, "30", 2.0) == 31.0, failures)
    _check("429 Retry-After сверху ограничен 90 с", retry_wait(429, "600", 2.0) == 90.0, failures)
    _check("429 Retry-After в виде даты -> окно",
           retry_wait(429, "Wed, 30 Sep 2026 09:00:00 GMT", 2.0) == 60.0, failures)
    _check("5xx — обычный backoff", retry_wait(503, None, 4.0) == 4.0, failures)

    # Лимит CoinGecko: конфиг под demo-ключ, без ключа — coingecko_no_key_per_min.
    import copy
    from scanner.config import Config
    from scanner.pipeline import _make_http
    data = copy.deepcopy(cfg._d)
    data["http"]["cache_dir"] = tempfile.mkdtemp()
    data["http"]["rate_limits_per_min"]["api.coingecko.com"] = 25
    data["http"]["coingecko_no_key_per_min"] = 10
    data.setdefault("api_keys", {})["coingecko_demo"] = "CG-x"
    with_key = _make_http(Config(data))._min_interval["api.coingecko.com"]
    data["api_keys"]["coingecko_demo"] = ""
    no_key = _make_http(Config(data))._min_interval["api.coingecko.com"]
    _check("с demo-ключом CG 25/мин (2.4 с)", abs(with_key - 2.4) < 1e-9, failures)
    _check("без ключа CG 10/мин (6 с)", abs(no_key - 6.0) < 1e-9, failures)


def test_regime(cfg, failures: list[str]) -> None:
    print("Режим BTC (контекст, не блокер):")
    from scanner.regime import classify_regime, rs_vs_btc

    bull = [50000 + 200 * i for i in range(120)]          # устойчивый рост
    r = classify_regime(bull)
    _check("рост + выше SMA -> BULL", r["regime"] == "BULL", failures)

    bear = [100000 - 400 * i for i in range(120)]         # устойчивое снижение
    r = classify_regime(bear)
    _check("снижение + ниже SMA -> BEAR", r["regime"] == "BEAR", failures)

    r = classify_regime([50000.0] * 10)
    _check("мало истории -> ?", r["regime"] == "?", failures)

    _check("RS: монета -5% при BTC -20% -> +15 п.п.", rs_vs_btc(-5.0, -20.0) == 15.0, failures)
    _check("RS: нет данных -> None", rs_vs_btc(None, -20.0) is None, failures)


def test_rf_gate(cfg, failures: list[str]) -> None:
    print("Stage 4 — гейт доступа РФ:")
    bybit = {"BTC", "ETH", "ARB"}
    ok, venue = rf_gate("ARB", "arbitrum", "0xabc", bybit)
    _check("на Bybit -> Bybit spot", ok and venue == "Bybit spot", failures)
    ok, venue = rf_gate("XYZ", "ethereum", "0xdef", bybit)
    _check("не на Bybit, но on-chain -> DEX only", ok and venue == "DEX only", failures)
    ok, venue = rf_gate("NOPE", "", "", bybit)
    _check("нет венью -> FAIL", (not ok) and venue == "нет", failures)


def test_liveness(cfg, failures: list[str]) -> None:
    print("Stage 3b — живость (dev/listings/wash) + LP-lock:")
    from scanner.stages import liveness, antirug

    # Живой проект: коммиты + широкий листинг + CEX.
    detail = {"developer_data": {"commit_count_4_weeks": 40, "pull_request_contributors": 5,
                                 "stars": 800},
              "tickers": [{"market": {"name": "Binance"}}, {"market": {"name": "Bybit"}},
                          {"market": {"name": "OKX"}}] + [{"market": {"name": f"dex{i}"}}
                                                          for i in range(8)]}
    c = Candidate(source="t", track="A", symbol="LIVE", volume_24h=1e6, liquidity_usd=5e5)
    liveness.assess_liveness(c, detail, cfg)
    _check("живой: dev_commits проставлен", c.dev_commits_4w == 40, failures)
    _check("живой: on_cex True", c.on_cex is True, failures)
    _check("живой: liveness_score высокий (>7)", c.liveness_score is not None and c.liveness_score > 7, failures)

    # Труп: 0 коммитов, только DEX.
    dead = {"developer_data": {"commit_count_4_weeks": 0},
            "tickers": [{"market": {"name": "someswap"}}]}
    c2 = Candidate(source="t", track="B", symbol="DEAD", volume_24h=1e5, liquidity_usd=2e5)
    liveness.assess_liveness(c2, dead, cfg)
    _check("труп: liveness низкий (<4)", c2.liveness_score is not None and c2.liveness_score < 4, failures)
    _check("труп: заметка про 0 коммитов", any("встал" in n for n in c2.liveness_notes), failures)

    # Нет detail -> liveness None (режет confidence, не врёт).
    c3 = Candidate(source="t", track="A", symbol="NODATA")
    liveness.assess_liveness(c3, {}, cfg)
    _check("нет detail -> liveness_score None", c3.liveness_score is None, failures)

    # Wash-trading: объём ≫ ликвидности.
    c4 = Candidate(source="t", track="B", symbol="WASH", volume_24h=1e8, liquidity_usd=1e5)
    liveness.assess_liveness(c4, {}, cfg)
    _check("wash: ratio посчитан (1000)", c4.wash_ratio == 1000.0, failures)
    _check("wash: флаг wash_suspect", "wash_suspect" in c4.flags, failures)

    # LP-lock интерпретация.
    locked = {"lp_holders": [{"is_locked": "1", "percent": "0.95"},
                             {"is_locked": "0", "percent": "0.05"}]}
    _check("LP заблокирован 95%", antirug.lp_locked_pct(locked) == 95.0, failures)
    burned = {"lp_holders": [{"address": "0x000000000000000000000000000000000000dead",
                             "percent": "1.0"}]}
    _check("LP сожжён (dead-адрес) = 100%", antirug.lp_locked_pct(burned) == 100.0, failures)
    unlocked = {"lp_holders": [{"is_locked": "0", "percent": "0.9"}]}
    ok, reasons, flags = antirug.evaluate_goplus_evm(
        {"is_honeypot": "0", "buy_tax": "0", "sell_tax": "0", "is_open_source": "1",
         **unlocked}, cfg)
    _check("разблокированный LP -> флаг lp_unlocked", "lp_unlocked" in flags, failures)
    _check("нет lp_holders -> None (не пенализируем)", antirug.lp_locked_pct({}) is None, failures)


def test_onchain(cfg, failures: list[str]) -> None:
    print("Stage 4c — on-chain накопление (Dune):")
    from scanner.stages.onchain import assess_onchain
    from scanner.sources.dune import build_onchain_map

    # Накопление: сильный отток (−20% нед.оборота при vol=1M) + рост холдеров.
    s, notes = assess_onchain(-1_400_000, 8.0, cfg, volume_24h=1_000_000)
    _check("отток+рост холдеров -> балл высокий (>8)", s is not None and s > 8, failures)
    _check("заметка про отток + долю оборота", any("отток" in n and "оборота" in n for n in notes), failures)
    # Распределение: сильный приток (+20% оборота) + исход холдеров -> низкий.
    s2, _ = assess_onchain(1_400_000, -8.0, cfg, volume_24h=1_000_000)
    _check("приток+исход -> балл низкий (<3)", s2 is not None and s2 < 3, failures)
    # Нормировка: тот же $ поток, но огромный объём -> сигнал слабый (крупнокап
    # не штрафуется абсолютом). LINK-кейс: $302M при vol $400M/д = ~11% оборота.
    s_big, _ = assess_onchain(1_400_000, None, cfg, volume_24h=100_000_000)
    _check("нормировка: малая доля оборота -> балл ~нейтрален (>4)", s_big > 4, failures)
    # Нет объёма -> fallback на абсолютный порог.
    s_abs, _ = assess_onchain(-3_000_000, None, cfg, volume_24h=None)
    _check("без объёма -> абсолютный порог работает (>6)", s_abs > 6, failures)
    # Санити-кап: аномальный поток (BNB-кейс +108% оборота) -> игнор -> None без холдеров.
    s_anom, n_anom = assess_onchain(5_130_000_000, None, cfg, volume_24h=680_000_000)
    _check("аномальный поток (>50% оборота) -> None (не врём)", s_anom is None, failures)
    _check("аномалия помечена в заметке", any("аномален" in n for n in n_anom), failures)
    # но если при аномальном потоке есть холдеры -> оцениваем по холдерам.
    s_anh, _ = assess_onchain(5_130_000_000, 7.0, cfg, volume_24h=680_000_000)
    _check("аномальный поток + холдеры -> балл по холдерам (>5)", s_anh is not None and s_anh > 5, failures)
    # Нет данных -> None (режет confidence, не врёт).
    s3, _ = assess_onchain(None, None, cfg)
    _check("нет данных Dune -> None", s3 is None, failures)

    # build_onchain_map: индексирует по symbol и адресу.
    rows = [{"symbol": "arb", "token_address": "0xABC", "net_flow_usd_7d": -5e5,
             "holders_change_pct_7d": 3.0}]
    m = build_onchain_map(rows)
    _check("map по symbol (UPPER)", "ARB" in m and m["ARB"]["net_flow_usd_7d"] == -5e5, failures)
    _check("map по адресу (lower)", "0xabc" in m, failures)

    # match_onchain: адрес приоритетнее тикера (однофамильцы из других сетей).
    from scanner.stages.onchain import flow_ratio, match_onchain
    m2 = {"ARB": {"net_flow_usd_7d": 1.0}, "0xabc": {"net_flow_usd_7d": 2.0}}
    _check("match: по адресу раньше тикера",
           match_onchain(m2, "ARB", "0xABC")["net_flow_usd_7d"] == 2.0, failures)
    _check("match: fallback по тикеру", match_onchain(m2, "arb", "")["net_flow_usd_7d"] == 1.0, failures)
    _check("match: пустая карта -> None", match_onchain({}, "ARB", "0xabc") is None, failures)

    # flow_ratio = net_flow / (vol*7); нет объёма -> None.
    _check("flow_ratio: -7M при vol 10M/д = -10%", abs(flow_ratio(-7e6, 1e7) + 0.10) < 1e-9, failures)
    _check("flow_ratio: vol=0 -> None", flow_ratio(-7e6, 0) is None, failures)
    _check("flow_ratio: нет потока -> None", flow_ratio(None, 1e7) is None, failures)

    # Без DUNE_API_KEY -> {} без сетевых вызовов (блок onchain остаётся None).
    import copy
    from scanner.config import Config
    from scanner.sources.dune import fetch_onchain
    nokey = copy.deepcopy(cfg._d)
    nokey.setdefault("api_keys", {})["dune"] = ""
    _check("нет Dune-ключа -> {} (нейтрально)", fetch_onchain(Config(nokey)) == {}, failures)

    # Блок onchain в скоре теперь читает onchain_score (не всегда None).
    from scanner.stages.score import compute_score
    c = Candidate(source="t", track="A", symbol="OC", zone="ПРУЖИНА/ДНО",
                  rf_venue="Bybit spot", volume_24h=5e7, mc_tvl=0.3, fdv_mc=1.2,
                  liveness_score=8.0)
    c.onchain_score = 9.0
    _, conf, brk = compute_score(c, cfg)
    _check("onchain-блок наполнен -> confidence 1.0", conf == 1.0, failures)
    _check("onchain sub в breakdown = 9.0", brk["onchain"]["sub"] == 9.0, failures)


def test_sources_parse(cfg, failures: list[str]) -> None:
    print("Парсинг источников (фандинг Bybit, klines, SQLite):")
    from scanner.sources.funding import parse_funding
    from scanner.sources.bybit import parse_daily_closes

    data = {"result": {"list": [
        {"symbol": "ARBUSDT", "fundingRate": "-0.0004"},
        {"symbol": "1000PEPEUSDT", "fundingRate": "0.0006"},
        {"symbol": "BTCPERP", "fundingRate": "0.0001"},       # не USDT -> мимо
        {"symbol": "ETHUSDT", "fundingRate": ""},             # пустая ставка -> мимо
        {"symbol": "1000BONKUSDT", "fundingRate": "0.0002"},
        {"symbol": "BONKUSDT", "fundingRate": "0.0003"},      # прямой листинг приоритетнее
    ]}}
    fm = parse_funding(data)
    _check("фандинг: строка -> float", fm.get("ARB") == -0.0004, failures)
    _check("фандинг: 1000PEPE -> PEPE", fm.get("PEPE") == 0.0006, failures)
    _check("фандинг: прямой BONK приоритетнее 1000BONK", fm.get("BONK") == 0.0003, failures)
    _check("фандинг: не-USDT и пустые пропущены", "BTC" not in fm and "ETH" not in fm, failures)
    _check("фандинг: мусорный ответ -> {}", parse_funding(None) == {} and parse_funding({}) == {}, failures)

    day = 86_400_000
    now = 12 * day + 3_600_000                     # 12-й день, 01:00 UTC — свеча дня 12 не закрыта
    kl = {"result": {"list": [[str(12 * day), "0", "0", "0", "99"],     # newest-first, live
                              [str(11 * day), "0", "0", "0", "101"],
                              [str(10 * day), "0", "0", "0", "100"]]}}
    _check("klines: oldest->newest, live-свеча отброшена",
           parse_daily_closes(kl, now) == [100.0, 101.0], failures)
    _check("klines: пустой ответ -> []", parse_daily_closes(None, now) == [], failures)

    # SQLite: поля Stage 4b/4c пишутся в candidates.
    from scanner.db import Store
    st = Store(":memory:")
    c = Candidate(source="t", track="A", symbol="Q", coin_id="q", chain="ethereum",
                  address="0x1", market_dd=0.42, spring_quality=1.1, funding_rate=-0.0004,
                  onchain_score=7.5, net_flow_usd_7d=-2e6, holders_change_pct_7d=3.0)
    rid = st.new_run("t")
    st.save_candidates(rid, [c])
    row = st.conn.execute("SELECT market_dd, spring_quality, funding_rate, onchain_score,"
                          " net_flow_usd_7d, holders_change_pct_7d FROM candidates").fetchone()
    st.close()
    _check("SQLite: поля Stage 4b/4c сохранены", row == (0.42, 1.1, -0.0004, 7.5, -2e6, 3.0), failures)


def test_entry_quality(cfg, failures: list[str]) -> None:
    print("Stage 4b — качество пружины (feature_study):")
    from scanner.stages.entry_quality import spring_quality
    from scanner.stages.score import _sub_zone

    # Сильная: рынок на дне (BTC −60%), длинная база, объём в полосе.
    good = Candidate(source="t", track="A", symbol="G", zone="ПРУЖИНА/ДНО",
                     drawdown_from_ath_pct=85.0, market_dd=0.60,
                     indicators={"base_len_days": 60, "vol_trend": 0.65})
    mg, ng = spring_quality(good, cfg)
    good.spring_quality = mg
    _check("сильная пружина: множитель > 1.05", mg > 1.05, failures)
    _check("сильная: заметка про рынок на дне", any("рынок на дне" in n for n in ng), failures)
    _check("сильная: sub_zone поднят выше базовых 9", _sub_zone(good) > 9.0, failures)

    # Слабая: BTC у ATH, короткая база, мёртвый объём, обнуление.
    bad = Candidate(source="t", track="A", symbol="B", zone="ПРУЖИНА/ДНО",
                    drawdown_from_ath_pct=96.0, market_dd=0.05,
                    indicators={"base_len_days": 12, "vol_trend": 0.2})
    mb, nb = spring_quality(bad, cfg)
    bad.spring_quality = mb
    _check("слабая пружина: множитель < 0.8", mb < 0.8, failures)
    _check("слабая: sub_zone занижен (< 8)", _sub_zone(bad) < 8.0, failures)
    _check("сильная ранжируется выше слабой по зоне", _sub_zone(good) > _sub_zone(bad), failures)

    # Нет market_dd/indicators -> нейтрально (множитель ~1, не штрафуем вслепую).
    neutral = Candidate(source="t", track="A", symbol="N", zone="ПРУЖИНА/ДНО")
    mn, _ = spring_quality(neutral, cfg)
    _check("нет данных -> множитель 1.0 (нейтрально)", mn == 1.0, failures)

    # Фандинг: капитуляция шортов (отрицательный) = бонус.
    fc = Candidate(source="t", track="A", symbol="F", zone="ПРУЖИНА/ДНО",
                   funding_rate=-0.001)
    mf, nf = spring_quality(fc, cfg)
    _check("отрицательный фандинг -> бонус (>1)", mf > 1.0, failures)
    _check("фандинг: заметка про капитуляцию", any("капитуляц" in n for n in nf), failures)
    # эйфория лонгов на пружине = штраф
    fe = Candidate(source="t", track="A", symbol="FE", zone="ПРУЖИНА/ДНО",
                   funding_rate=0.001)
    me, _ = spring_quality(fe, cfg)
    _check("положительный фандинг на пружине -> штраф (<1)", me < 1.0, failures)


def test_market_regime(cfg, failures: list[str]) -> None:
    print("Контекст рынка альтов (market_daily -> признаки -> перегрев):")
    from scanner import regime
    from scanner.stages.entry_quality import spring_quality
    D = regime.DAY
    # 400 дней: альт-рынок (total − BTC − стейблы) растёт до пика и падает на 40%.
    rows = []
    for i in range(400):
        alt = 1000 + 5 * i if i < 300 else 2500 * (1 - 0.004 * (i - 299))
        btc = 1000.0
        rows.append({"day": i * D, "total_mcap": alt + btc + 200, "btc_dominance": btc / (alt + btc + 200) * 100,
                     "stables_usd": 200, "fng": 40, "mvrv_btc": 1.5, "mvrv_eth": 0.9,
                     "breadth200": 80, "funding_btc": 0.00005})
    S = regime.market_series(rows)
    _check("alt_ex = total − BTC − стейблы", abs(S["alt_ex"][0] - 1000) < 1e-6, failures)
    ctx = regime.market_context(rows, cfg)
    _check("alt_dd на последний день ≈ 0.40", abs(ctx.get("alt_dd", 0) - 0.40) < 0.01, failures)
    hot = ctx["hot"]
    _check("холодный рынок: 0 горящих флагов", hot["n_lit"] == 0 and hot["score"] == 0, failures)
    _check("ширина 80% попала в «близко»", "breadth200" in hot["near"], failures)
    f = {"alt_vs_sma200": 0.5, "mvrv_btc": 2.5, "fng30": 50, "breadth200": 60}
    h = regime.hot_flags(f, cfg)
    _check("перегрев: 2 из 4 доступных = 0.5", h["score"] == 0.5 and h["n_lit"] == 2, failures)
    _check("мало доступных флагов -> score None", regime.hot_flags({"fng30": 80}, cfg)["score"] is None, failures)
    line = regime.context_line({**ctx, "btc_dd": 0.33})
    _check("строка контекста: альты, BTC, перегрев, F&G",
           "альты −40%" in line and "BTC −33%" in line and "перегрев 0/" in line
           and "близко: ширина рынка" in line and "F&G 40" in line, failures)
    _check("пустой контекст -> пустая строка", regime.context_line({}) == "", failures)

    # spring_quality: альт-рынок на дне > у хаёв; BTC — фолбэк при отсутствии alt.
    def sq(**kw):
        return spring_quality(Candidate(source="t", track="A", symbol="Q", zone="ПРУЖИНА/ДНО", **kw), cfg)
    bottom, nb = sq(alt_market_dd=0.70, market_dd=0.10)
    top, nt = sq(alt_market_dd=0.20, market_dd=0.60)
    _check("источник alt: альты −70% > альты −20% (BTC игнорируется)", bottom > 1.1 > 0.7 > top, failures)
    _check("заметка про альт-рынок на дне", any("альт-рынок на дне" in n for n in nb), failures)
    fb, _ = sq(market_dd=0.60)
    _check("нет alt_dd -> фолбэк на BTC-dd", fb > 1.1, failures)
    cold, _ = sq(alt_market_dd=0.5)
    warm, nw = sq(alt_market_dd=0.5, market_hot_score=0.125, market_hot_lit=["fng30"])
    hotm, nh = sq(alt_market_dd=0.5, market_hot_score=0.375,
                  market_hot_lit=["fng30", "mvrv_btc", "breadth200"])
    _check("перегрев режет множитель: холодный > тёплый ≥ горячий", cold > warm >= hotm, failures)
    _check("заметки: теплеет / перегрет", any("теплеет" in n for n in nw)
           and any("перегрет" in n and "MVRV BTC" in n for n in nh), failures)


def test_coin_context(cfg, failures: list[str]) -> None:
    print("Stage 4d — монетный контекст (информационный):")
    from scanner.stages.coin_context import annotate, supply_growth
    from scanner.sources.coin_extras import parse_delist_title
    from scanner.notify.telegram import coin_line
    _check("предложение: mcap/price 100→130 = +30%",
           supply_growth([1.0, 2.0], [100.0, 260.0]) == 0.3, failures)
    _check("предложение: мало точек -> None", supply_growth([1.0], [100.0]) is None, failures)
    _check("делистинг: список тикеров", parse_delist_title("Delisting of L3,VIC") == (["L3", "VIC"], False),
           failures)
    _check("делистинг: только перп", parse_delist_title("Delisting of ICXUSDT Perpetual Contract")
           == (["ICX"], True), failures)
    _check("делистинг: без тикеров в заголовке",
           parse_delist_title("Bybit to Delist 4 Token(s) on Sep 24, 2026")[0] == [], failures)
    extras = {"revenue": {"by_gecko": {}, "by_symbol": {"UNI": 10e6}},
              "oi": {"UNI": 30e6, "ZZZ": 50e6}, "delist": {"ZZZ": "spot"},
              "coinbase": {"UNI", "ZZZ"}}
    u = Candidate(source="t", track="A", symbol="UNI", coin_id="uniswap", market_cap=1.2e9)
    annotate(u, extras, cfg, [1.0, 1.0], [1.0e9, 1.1e9])
    _check("P/F = капа / (выручка30д·12) = 10", u.p_f == 10.0, failures)
    _check("P/F ≤ pf_cheap -> заметка «дёшево»", any("дёшево" in n for n in u.zone_signals), failures)
    _check("эмиссия +10% без флага, US-тег coinbase",
           u.supply_growth == 0.1 and "supply_inflation" not in u.flags and u.us_tag == "coinbase", failures)
    z = Candidate(source="t", track="A", symbol="ZZZ", market_cap=100e6)
    annotate(z, extras, cfg, [1.0, 1.0], [100e6, 140e6])
    _check("делистинг спота -> флаг + заметка", z.delist == "spot" and "delist_spot" in z.flags, failures)
    _check("OI 50% капы -> high_leverage", "high_leverage" in z.flags, failures)
    _check("эмиссия +40% -> supply_inflation", "supply_inflation" in z.flags, failures)
    line = coin_line(z)
    _check("строка монеты: делистинг, эмиссия, OI", "делистинг" in line and "эмиссия +40%" in line
           and "OI 50%" in line, failures)
    sol = Candidate(source="t", track="A", symbol="SOL")
    annotate(sol, {"coinbase": {"SOL"}}, cfg)
    _check("SOL в списке ETF -> us_tag etf", sol.us_tag == "etf", failures)
    from scanner.stages.score import compute_score
    a, b = Candidate(source="t", track="A", symbol="A1", zone="ПРУЖИНА/ДНО"),         Candidate(source="t", track="A", symbol="A1", zone="ПРУЖИНА/ДНО")
    b.flags += ["supply_inflation", "high_leverage", "delist_spot"]
    _check("монетные флаги не меняют скор (информационно)",
           compute_score(a, cfg)[0] == compute_score(b, cfg)[0], failures)


def test_score(cfg, failures: list[str]) -> None:
    print("Stage 5 — композитный скор:")

    strong = Candidate(source="t", track="A", symbol="GOOD", zone="ПРУЖИНА/ДНО",
                       rf_venue="Bybit spot", volume_24h=5e7, mc_tvl=0.3, fdv_mc=1.2,
                       flags=["undervalued_vs_sector"], liveness_score=8.0)
    s, conf, _ = compute_score(strong, cfg)
    _check("сильный кандидат: высокий балл (>70)", s > 70, failures)
    _check("confidence 0.90 (есть liveness, нет on-chain)", conf == 0.90, failures)

    # Тот же без liveness-данных -> confidence ниже (liveness режет доверие).
    no_live = Candidate(source="t", track="A", symbol="NOLIVE", zone="ПРУЖИНА/ДНО",
                        rf_venue="Bybit spot", volume_24h=5e7, mc_tvl=0.3, fdv_mc=1.2,
                        flags=["undervalued_vs_sector"])
    _, conf2, _ = compute_score(no_live, cfg)
    _check("без liveness confidence ниже (0.74)", conf2 == 0.74, failures)

    weak = Candidate(source="t", track="A", symbol="BAD", zone="ПАДАЮЩИЙ_НОЖ",
                     rf_venue="DEX only", volume_24h=2e5, mc_tvl=8.0, fdv_mc=4.0,
                     flags=["overvalued_vs_sector", "mintable"])
    sw, _, _ = compute_score(weak, cfg)
    _check("слабый кандидат: низкий балл (<40)", sw < 40, failures)
    _check("сильный ранжируется выше слабого", s > sw, failures)

    # Нет зоны и оценки -> только safety+tradability -> confidence ниже.
    thin = Candidate(source="t", track="B", symbol="NEW", zone="?", rf_venue="DEX only")
    _, ct, _ = compute_score(thin, cfg)
    _check("тонкий кандидат: confidence < 0.85", ct < 0.85, failures)


def test_telegram_format(cfg, failures: list[str]) -> None:
    print("Stage 6 — формат Telegram-алерта:")
    cands = [
        Candidate(source="t", track="A", symbol="ICP", zone="ПРУЖИНА/ДНО",
                  rf_venue="Bybit spot", score=84.7, confidence=0.85,
                  drawdown_from_ath_pct=90.0),
        Candidate(source="t", track="A", symbol="KITE", zone="ПАДАЮЩИЙ_НОЖ",
                  rf_venue="DEX only", score=44.0, confidence=0.85),
        Candidate(source="t", track="A", symbol="MID", zone="СЕРЕДИНА",
                  rf_venue="Bybit spot", score=75.0, confidence=0.85),
    ]
    msg = format_alert(cands, cfg)
    _check("алерт не пустой", bool(msg), failures)
    _check("включает ICP (зона дна, score>70)", "ICP" in (msg or ""), failures)
    _check("исключает падающий нож KITE", "KITE" not in (msg or ""), failures)
    _check("исключает СЕРЕДИНУ (only_zones=дно)", "MID" not in (msg or ""), failures)

    # Никто не проходит порог -> None.
    low = [Candidate(source="t", track="A", symbol="X", zone="ПРУЖИНА/ДНО", score=10.0)]
    _check("нет проходящих -> None", format_alert(low, cfg) is None, failures)

    # Высокий балл, но низкий confidence (данных мало) -> гейт режет.
    thin = [Candidate(source="t", track="B", symbol="THIN", zone="ПРУЖИНА/ДНО",
                      score=90.0, confidence=0.3)]
    _check("низкий confidence отсечён гейтом", format_alert(thin, cfg) is None, failures)

    # Рыночный контекст в шапке (BTC.D + просадка BTC).
    from scanner.notify.telegram import format_market_ctx
    mc = format_market_ctx({"market_dd": 0.49, "btc_dominance_pct": 58.0,
                            "total2_mcap_usd": 1.2e12})
    _check("контекст: BTC drawdown + зона (0.49=средняя)", "BTC −49%" in mc and "средняя зона" in mc, failures)
    _check("контекст: BTC.D + альты", "BTC.D 58%" in mc and "альты $1200B" in mc, failures)
    msg = format_alert(cands, cfg, {"market_dd": 0.49, "btc_dominance_pct": 58.0})
    _check("шапка контекста в алерте", "🌍" in (msg or ""), failures)
    mc2 = format_market_ctx({"alt_dd": 0.42, "btc_dd": 0.33, "fng": 45, "btc_d": 58.4,
                             "hot": {"n_lit": 0, "avail": 8, "lit": [], "near": ["fng30"]}})
    _check("новый контекст: альты · BTC · перегрев 0/8 (близко) · BTC.D",
           mc2 == "альты −42% · BTC −33% · перегрев 0/8 (близко: F&G) · F&G 45 · BTC.D 58%", failures)


def test_exit(cfg, failures: list[str]) -> None:
    print("Stage 8 — выходной контур (hodl-профиль):")
    pos = {"entry_price": 1.0, "base_low": 0.9, "qty": 1000.0, "entry_ts": 0.0}

    # 1) Инвалидация: пробой лоу базы -25% (пол = 0.9*0.75 = 0.675).
    #    Без recent_closes -> проверка по одному close (обратная совместимость).
    sigs = exit_stage.evaluate_exit(pos, 0.60, 1.0, None, set(), cfg)  # < 0.675
    _check("пробой базы-25% (одиночный) -> инвалидация",
           any(s["type"] == "invalidation" for s in sigs), failures)
    _check("инвалидация — единственный сигнал", len(sigs) == 1, failures)

    # прокол базы, но в пределах буфера (shakeout) -> тишина, hodl терпит
    sigs = exit_stage.evaluate_exit(pos, 0.80, 1.0, None, set(), cfg)  # > 0.675
    _check("shakeout в пределах буфера — сигналов нет", not sigs, failures)

    # Подтверждение N дней (config=2): однодневный пролив НЕ инвалидирует.
    whipsaw = [0.95, 0.60]   # вчера норма, сегодня пробой -> ждём подтверждения
    sigs = exit_stage.evaluate_exit(pos, 0.60, 1.0, None, set(), cfg, recent_closes=whipsaw)
    _check("1-дневный whipsaw НЕ инвалидирует (подтв. 2д)",
           not any(s["type"] == "invalidation" for s in sigs), failures)
    # Два закрытия подряд ниже пола -> инвалидация подтверждена.
    confirmed = [0.62, 0.58]
    sigs = exit_stage.evaluate_exit(pos, 0.58, 1.0, None, set(), cfg, recent_closes=confirmed)
    _check("2 закрытия ниже пола -> инвалидация подтверждена",
           any(s["type"] == "invalidation" for s in sigs), failures)

    # 2) Лестница: +100% -> первый уровень; повторно не алертит (идемпотентность).
    sigs = exit_stage.evaluate_exit(pos, 2.1, 2.1, None, set(), cfg)
    _check("+110% -> ladder_0", any(s["type"] == "ladder_0" for s in sigs), failures)
    sigs2 = exit_stage.evaluate_exit(pos, 2.1, 2.1, None, {"ladder_0"}, cfg)
    _check("ladder_0 уже в журнале -> не дублируется",
           not any(s["type"] == "ladder_0" for s in sigs2), failures)

    # +300% -> оба уровня сразу (если первый ещё не фиксировали).
    sigs = exit_stage.evaluate_exit(pos, 4.5, 4.5, None, set(), cfg)
    _check("+350% -> оба уровня лестницы",
           {"ladder_0", "ladder_1"} <= {s["type"] for s in sigs}, failures)

    # 3) Трейлинг взводится только после arm (+60%): без прибыли откат не выбивает.
    sigs = exit_stage.evaluate_exit(pos, 1.05, 1.5, None, set(), cfg)  # пик был +50%
    _check("пик +50% (< arm 60%) -> трейлинг молчит",
           not any(s["type"] == "trailing" for s in sigs), failures)
    sigs = exit_stage.evaluate_exit(pos, 1.3, 2.0, None, {"ladder_0"}, cfg)  # пик +100%, откат -35%
    _check("пик +100%, откат -35% -> трейлинг сработал",
           any(s["type"] == "trailing" for s in sigs), failures)

    # 4) Зона распределения: разгон над SMA + верх диапазона (информационный).
    ind = {"pct_above_sma": 45.0, "range_pos": 0.95, "vol_contraction": 1.4,
           "trend_recent_pct": 40.0, "n_days": 180}
    sigs = exit_stage.evaluate_exit(pos, 1.8, 1.8, ind, set(), cfg)
    _check("разгон -> peak_zone", any(s["type"] == "peak_zone" for s in sigs), failures)

    # Эйфория фандинга даёт peak_zone даже без ценового разгона.
    ind_f = {"pct_above_sma": 5.0, "range_pos": 0.5, "funding_rate": 0.001}
    sigs = exit_stage.evaluate_exit(pos, 1.4, 1.4, ind_f, set(), cfg)
    pz = next((s for s in sigs if s["type"] == "peak_zone"), None)
    _check("эйфория фандинга -> peak_zone", pz is not None, failures)
    _check("peak_zone: заметка про фандинг", pz and "фандинг" in pz["note"], failures)

    # 5) Перегрев рынка: алерт в плюсе; сужение трейла только у близнеца B (A/B paper).
    hot = {"hot_score": 0.5, "lit": ["mvrv_btc", "fng30", "breadth200", "fund30"]}
    cold = {"hot_score": 0.0, "lit": []}
    sigs = exit_stage.evaluate_exit(pos, 1.5, 1.5, None, set(), cfg, market=hot)
    mh = next((s for s in sigs if s["type"] == "market_hot"), None)
    _check("перегрев рынка + прибыль -> market_hot", mh is not None, failures)
    _check("market_hot: флаги в заметке", mh and "MVRV BTC" in mh["note"], failures)
    _check("холодный рынок -> market_hot молчит",
           not any(s["type"] == "market_hot" for s in
                   exit_stage.evaluate_exit(pos, 1.5, 1.5, None, set(), cfg, market=cold)), failures)
    _check("market_hot уже в журнале -> не дублируется",
           not any(s["type"] == "market_hot" for s in
                   exit_stage.evaluate_exit(pos, 1.5, 1.5, None, {"market_hot"}, cfg, market=hot)), failures)
    # пик +100% (взведён), откат −20%: A (трейл 30%) молчит, B (15% при перегреве) выходит.
    pos_b = {**pos, "variant": "B"}
    ta = exit_stage.evaluate_exit(pos, 1.6, 2.0, None, {"ladder_0"}, cfg, market=hot)
    tb = exit_stage.evaluate_exit(pos_b, 1.6, 2.0, None, {"ladder_0"}, cfg, market=hot)
    tbc = exit_stage.evaluate_exit(pos_b, 1.6, 2.0, None, {"ladder_0"}, cfg, market=cold)
    _check("A: откат −20% при перегреве — трейл 30% молчит",
           not any(s["type"] == "trailing" for s in ta), failures)
    _check("B: откат −20% при перегреве — сужённый трейл сработал",
           any(s["type"] == "trailing" and "сужен" in s["note"] for s in tb), failures)
    _check("B на холодном рынке ведёт себя как A",
           not any(s["type"] == "trailing" for s in tbc), failures)

    # 6) Близнец S (A/B ширины стопа): пол −50% (0.45) вместо −25% (0.675).
    pos_s = {**pos, "variant": "S"}
    mid = [0.62, 0.58]                 # ниже 0.675, выше 0.45
    deep = [0.44, 0.40]                # ниже обоих полов
    _check("S: 2 закрытия ниже −25%, но выше −50% — A выходит, S держит",
           any(s["type"] == "invalidation" for s in
               exit_stage.evaluate_exit(pos, 0.58, 1.0, None, set(), cfg, recent_closes=mid))
           and not exit_stage.evaluate_exit(pos_s, 0.58, 1.0, None, set(), cfg, recent_closes=mid),
           failures)
    ds = exit_stage.evaluate_exit(pos_s, 0.40, 1.0, None, set(), cfg, recent_closes=deep)
    _check("S: ниже −50% -> инвалидация с буфером −50% в заметке",
           len(ds) == 1 and ds[0]["type"] == "invalidation" and "−50%" in ds[0]["note"], failures)
    _check("S: лестница как у A (+110% -> ladder_0)",
           any(s["type"] == "ladder_0" for s in
               exit_stage.evaluate_exit(pos_s, 2.1, 2.1, None, set(), cfg)), failures)

    # P&L net-of-fees.
    pnl = exit_stage.position_pnl({"entry_price": 1.0, "qty": 1000.0}, 2.0)
    _check("P&L ~ +99.7% (комиссии учтены)", 99.0 < pnl["pnl_pct"] < 100.0, failures)


def test_positions_store(cfg, failures: list[str]) -> None:
    print("Stage 7 — хранилище позиций (in-memory SQLite):")
    ps = PositionStore(":memory:")
    pid = ps.add("ARB", 0.5, 1000, coin_id="arbitrum", base_low=0.45)
    _check("позиция создана", ps.find_open("ARB") is not None, failures)
    _check("поиск по id", ps.find_open(str(pid)) is not None, failures)

    ps.record_event(pid, "ladder_0", 1.0, "фиксация 25%")
    _check("событие в журнале", "ladder_0" in ps.event_types(pid), failures)

    ps.update_hwm(pid, 1.2)
    _check("hwm обновлён", ps.find_open("ARB")["hwm"] == 1.2, failures)

    # Частичная продажа (лестница/pos reduce): realized + остаток открыт.
    r1 = ps.sell(pid, 250, 1.0, "ladder_0")          # продать 250 из 1000 @ 1.0
    _check("частичная: realized > 0 (вход 0.5 -> 1.0)", r1 > 100, failures)
    _check("частичная: остаток открыт 750", abs(ps.get(pid)["qty"] - 750) < 1e-6, failures)
    _check("частичная: realized_usdt накоплен", ps.get(pid)["realized_usdt"] > 100, failures)

    ps.close(pid, 1.1, "тест")
    _check("закрыта -> не в открытых", ps.find_open("ARB") is None, failures)
    _check("manual_close в журнале", "manual_close" in ps.event_types(pid), failures)
    _check("realized суммируется (part+close)", ps.get(pid)["realized_usdt"] > 400, failures)

    # A/B близнецы: B не считается в лимите paper, пара находится, pos close берёт A.
    pa = ps.add("TWN", 1.0, 100, coin_id="twn", paper=True)
    pb = ps.add("TWN", 1.0, 100, coin_id="twn", paper=True, variant="B", twin_of=pa)
    _check("близнец B не в лимите paper (count=1)", ps.count_open_paper() == 1, failures)
    prs = ps.ab_pairs()
    _check("ab_pairs: пара A↔B", len(prs) == 1 and prs[0][0]["id"] == pa and prs[0][1]["id"] == pb,
           failures)
    _check("find_open по тикеру -> A раньше B", ps.find_open("TWN")["id"] == pa, failures)
    psn = ps.add("TWN", 1.0, 100, coin_id="twn", paper=True, variant="S", twin_of=pa)
    _check("близнец S: не в лимите paper, своя пара в ab_pairs('S')",
           ps.count_open_paper() == 1 and [(a["id"], b["id"]) for a, b in ps.ab_pairs("S")]
           == [(pa, psn)] and len(ps.ab_pairs()) == 1, failures)
    ps.close(pa, 1.0); ps.close(pb, 1.0)
    _check("открытый S не блокирует переоткрытие A (has_open_for только по A)",
           not ps.has_open_for("twn", "TWN"), failures)
    ps.close(psn, 1.0)

    # pos add --merge: ступени одной монеты -> одна позиция со средней ценой.
    mp = ps.add("MRG", 0.8, 50, coin_id="mrg", paper=True)       # paper не сливается
    pm = ps.add("MRG", 1.0, 100, coin_id="mrg", base_low=0.9)
    _check("merge: находит реальную, а не paper", ps.find_open_real("MRG")["id"] == pm, failures)
    m = ps.merge(pm, 0.8, 150)
    _check("merge: qty 250, средняя (100+120)/250 = 0.88, base_low не меняется",
           abs(m["qty"] - 250) < 1e-9 and abs(m["entry_price"] - 0.88) < 1e-12
           and m["base_low"] == 0.9 and abs(m["initial_qty"] - 250) < 1e-9, failures)
    _check("merge: событие в журнале", "merge" in ps.event_types(pm), failures)
    sig_avg = exit_stage.evaluate_exit(m, 1.32, 1.32, None, set(), cfg)      # +50% от 0.88
    sig_first = exit_stage.evaluate_exit({**m, "entry_price": 1.0}, 1.32, 1.32, None, set(), cfg)
    _check("merge: +50% считается от средней (1.32 = +50% от 0.88, но +32% от 1-й ступени)",
           any(s["type"] == "ladder_0" for s in sig_avg)
           and not any(s["type"] == "ladder_0" for s in sig_first), failures)
    r = ps.close(pm, 1.2)
    legs = 250 * 1.2 * (1 - 0.0015) - (100 * 1.0 + 150 * 0.8) * (1 + 0.0015)
    _check("merge: realized = сумма ступеней по отдельности (комиссии не задвоены)",
           abs(r - legs) < 1e-9, failures)
    _check("merge в закрытую позицию -> None", ps.merge(pm, 1.0, 10) is None, failures)
    ps.close(mp, 0.8)

    # Cooldown re-open: закрытая paper-монета не переоткрывается N дней.
    cp = ps.add("CD", 1.0, 100, coin_id="cd-coin", paper=True)
    ps.close(cp, 1.5, reason="paper_trailing_exec")
    _check("recent_closed_paper ловит недавнее закрытие",
           ps.recent_closed_paper("cd-coin", "CD", 30), failures)
    _check("recent_closed_paper: старше окна -> нет",
           not ps.recent_closed_paper("cd-coin", "CD", 0), failures)

    # Paper-режим: дедуп по монете, лимит открытых, исключение из риск-экспозиции.
    pp = ps.add("XYZ", 2.0, 50, coin_id="xyz-coin", paper=True)
    _check("paper-позиция создана с флагом",
           ps.find_open("XYZ")["is_paper"] == 1, failures)
    _check("has_open_for находит по coin_id", ps.has_open_for("xyz-coin"), failures)
    _check("has_open_for: чужой монеты нет", not ps.has_open_for("other-coin"), failures)
    _check("count_open_paper == 1", ps.count_open_paper() == 1, failures)
    ps.close_db()

    # Бюджет риска: капитал 5000, новая позиция 1000 (20% > лимита 10%).
    class _C:  # минимальный конфиг-стаб
        def get(self, path, default=None):
            return {"stage7_positions": {"capital_usdt": 5000,
                                         "max_position_pct_of_capital": 10,
                                         "worst_case_loss_pct": 60}}.get(path, default)
    warns = risk_check([], 1000.0, _C())
    _check("превышение лимита позиции -> предупреждение",
           any("лимита" in w for w in warns), failures)
    warns = risk_check([], 300.0, _C())
    _check("в рамках лимита -> тишина", not warns, failures)
    # Paper-позиции не входят в worst-case экспозицию.
    paper_pos = [{"entry_price": 100.0, "qty": 100.0, "is_paper": 1}]  # 10k virtual
    warns = risk_check(paper_pos, 300.0, _C())
    _check("paper не считается в экспозиции", not warns, failures)


def test_exit_alert_format(cfg, failures: list[str]) -> None:
    print("Stage 8 — формат exit-алерта:")
    rows = [{
        "position": {"symbol": "ARB"}, "held_days": 120,
        "pnl": {"pnl_pct": 105.0, "pnl_usdt": 525.0, "value_usdt": 1025.0},
        "signals": [{"type": "ladder_0", "urgency": "medium",
                     "action": "ЗАФИКСИРОВАТЬ 25%", "note": "достигнут уровень +100%"}],
    }]
    msg = format_exit_alert(rows, cfg)
    _check("алерт сформирован", bool(msg and "ARB" in msg), failures)
    _check("действие в алерте", "ЗАФИКСИРОВАТЬ 25%" in (msg or ""), failures)
    _check("нет сигналов -> None",
           format_exit_alert([{"position": {"symbol": "X"}, "signals": []}], cfg) is None,
           failures)

    # Спарклайн: растущий ряд заканчивается верхним блоком, короткий не падает.
    s = sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    _check("спарклайн: рост -> последний символ верхний", s.endswith("█"), failures)
    _check("спарклайн: 1 точка не падает", bool(sparkline([5.0])), failures)

    # Дайджест: обе позиции (real+paper), суммы, ближайший уровень лестницы.
    drows = [
        {"position": {"symbol": "ARB", "entry_price": 0.30, "qty": 1000.0,
                      "base_low": 0.25, "is_paper": 0},
         "last_price": 0.36, "hwm": 0.40,
         "pnl": {"pnl_pct": 19.6, "pnl_usdt": 58.7, "value_usdt": 358.7},
         "held_days": 30, "signals": [], "triggered": set(),
         "spark_prices": [0.30, 0.32, 0.35, 0.40, 0.36]},
        {"position": {"symbol": "GRAM", "entry_price": 1.61, "qty": 62.0,
                      "base_low": 1.5, "is_paper": 1},
         "last_price": 1.60, "hwm": 1.61,
         "pnl": {"pnl_pct": -0.9, "pnl_usdt": -0.9, "value_usdt": 99.1},
         "held_days": 1, "signals": [], "triggered": set(),
         "spark_prices": [1.61, 1.60]},
    ]
    dig = format_digest(drows, cfg)
    _check("дайджест: обе позиции", "ARB" in (dig or "") and "GRAM" in (dig or ""), failures)
    _check("дайджест: маркировка 💰/📝", "💰" in (dig or "") and "📝" in (dig or ""), failures)
    _check("дайджест: ближайший уровень лестницы", "+50%" in (dig or ""), failures)
    _check("дайджест: суммы real/paper", "Σ" in (dig or ""), failures)
    _check("дайджест: пусто -> None", format_digest([], cfg) is None, failures)

    # Недельная сводка (обычная неделя): счётчик + напоминание + отсчёт до итога.
    wk = format_weekly({"week_no": 2, "milestone": False, "milestone_weeks": 4,
                        "opened": 3, "open_now": 5, "invalidations": 1,
                        "ladder_hits": 2, "trailings": 0,
                        "paper_pnl_usdt": 12.5, "real_open": 1,
                        "real_pnl_usdt": 40.0}, cfg)
    _check("недельная сводка: счётчик недели", "неделя 2" in wk, failures)
    _check("недельная сводка: напоминание + отсчёт", "До итоговой сводки: 2 нед" in wk, failures)
    _check("недельная сводка: цифры", "инвалидаций 1" in wk and "+12.50" in wk, failures)
    _check("нет A/B пар -> строки сравнения нет", "🅰🅱" not in wk, failures)
    wab = format_weekly({"week_no": 2, "milestone": False, "milestone_weeks": 4,
                         "opened": 0, "open_now": 2, "invalidations": 0, "ladder_hits": 0,
                         "trailings": 0, "paper_pnl_usdt": 0.0, "real_open": 0,
                         "ab": {"pairs": 3, "a_usdt": 40.0, "b_usdt": 55.5,
                                "diverged": 1, "b_better": 1}}, cfg)
    _check("A/B: строка сравнения A vs B", "🅰🅱" in wab and "+40.00" in wab
           and "+55.50" in wab and "3 парах" in wab, failures)
    _check("нет пар S -> строки ширины стопа нет", "🅰🆂" not in wab, failures)
    was = format_weekly({"week_no": 3, "milestone": False, "milestone_weeks": 4,
                         "opened": 0, "open_now": 2, "invalidations": 1, "ladder_hits": 0,
                         "trailings": 0, "paper_pnl_usdt": 0.0, "real_open": 0,
                         "stop_pct": 25, "ab_stop_pct": 50,
                         "ab_stop": {"pairs": 2, "a_usdt": -26.0, "b_usdt": 4.5, "diverged": 1,
                                     "b_better": 1, "a_stopped": 1, "b_stopped": 0}}, cfg)
    _check("A/S: строка ширины стопа (−25% vs −50%, срабатывания)",
           "🅰🆂" in was and "A −25% -26.00" in was and "S −50% +4.50" in was
           and "стоп сработал A 1 / S 0" in was, failures)

    # Итоговая 4-недельная: кумулятив + call-to-decide про капитал.
    ms = format_weekly({"week_no": 4, "milestone": True, "milestone_weeks": 4,
                        "opened": 2, "open_now": 12, "invalidations": 1,
                        "ladder_hits": 0, "trailings": 0, "paper_pnl_usdt": 30.0,
                        "real_open": 0, "real_pnl_usdt": 0.0,
                        "cum_opened": 34, "cum_invalidations": 9,
                        "cum_ladder_hits": 3, "cum_trailings": 1}, cfg)
    _check("итоговая: заголовок ИТОГОВАЯ", "ИТОГОВАЯ за 4" in ms, failures)
    _check("итоговая: кумулятив пружин", "34" in ms, failures)
    _check("итоговая: call-to-decide про капитал", "capital_usdt" in ms, failures)


def test_ladder(cfg, failures: list[str]) -> None:
    print("Планировщик лестницы (run.py ladder):")
    from scanner.ladder import plan_ladder, round_step
    _check("round_step: 0.1+0.2 → 0.3 по шагу 0.1", round_step(0.1 + 0.2, 0.1) == 0.3, failures)
    _check("round_step вверх: 1.23401 → 1.235", round_step(1.23401, 0.001, up=True) == 1.235,
           failures)
    # пример из отчёта: цена 1.00, лоу базы 0.95 -> стоп 0.7125, ступени 1/0.916/0.832/0.748
    p = plan_ladder(1.0, 0.95, 100, tick=0.0001, qty_step=0.01)
    pxs = [b["price"] for b in p["buys"]]
    _check("4 ступени до пола: 1.000/0.916/0.832/0.748",
           p["ok"] and [round(x, 3) for x in pxs] == [1.0, 0.916, 0.832, 0.748], failures)
    _check("стоп = лоу базы −25% (0.7125)", abs(p["stop_px"] - 0.7125) < 1e-9, failures)
    _check("убыток на стопе ≈ 17.5% (+комиссии) против ≈ 29% разом",
           0.17 < p["worst"]["stop_loss_pct"] < 0.19
           and 0.28 < p["worst"]["lump_stop_loss_pct"] < 0.30, failures)
    _check("лимитки кратны шагу цены, ступени ≥ $10",
           all(abs(round(b["price"] / 0.0001) * 0.0001 - b["price"]) < 1e-12 for b in p["buys"])
           and all(b["usd"] >= 10 - 1e-9 for b in p["buys"]), failures)
    _check("prod: +50% и +150% от средней, остаток под трейл",
           [s["gain"] for s in p["sells"]] == [0.5, 1.5, None], failures)
    p = plan_ladder(1.0, 0.95, 30, qty_step=0.01)
    _check("$30 при минимуме $10 → 3 ступени с предупреждением",
           p["steps"] == 3 and any("ступеней 3" in w for w in p["warns"]), failures)
    p = plan_ladder(1.0, 0.95, 20, steps=2, qty_step=0.01, filled=1)
    _check("продажа < $5 сливается со следующей целью",
           any("слита" in w for w in p["warns"]) and p["sells"][0]["gain"] == 1.5, failures)
    p = plan_ladder(1.0, 0.95, 50, sell="paired", qty_step=0.001, tick=0.0001)
    _check("парная: каждая ступень продаётся на ≥ +40% от своей цены",
           len(p["sells"]) == 4 and all(s["gain"] >= 0.40 - 1e-9 for s in p["sells"]), failures)
    _check("цена ниже стопа → план не строится",
           not plan_ladder(0.70, 0.95, 50)["ok"], failures)
    _check("бюджет меньше минимума → план не строится",
           not plan_ladder(1.0, 0.95, 8)["ok"], failures)
    p = plan_ladder(1.30, 0.95, 50)
    _check("цена далеко над базой → предупреждение «не вход у дна»",
           any("не вход у дна" in w for w in p["warns"]), failures)


def main() -> int:
    cfg = load_config()
    failures: list[str] = []
    print("=== SELFTEST (офлайн, без сети) ===\n")
    test_filters(cfg, failures)
    print()
    test_track_q(cfg, failures)
    print()
    test_antirug_evm(cfg, failures)
    print()
    test_honeypot_is(cfg, failures)
    print()
    test_fundamentals(cfg, failures)
    print()
    test_zone(cfg, failures)
    print()
    test_closed_daily(cfg, failures)
    print()
    test_retry_wait(cfg, failures)
    print()
    test_regime(cfg, failures)
    print()
    test_rf_gate(cfg, failures)
    print()
    test_liveness(cfg, failures)
    print()
    test_sources_parse(cfg, failures)
    print()
    test_entry_quality(cfg, failures)
    print()
    test_market_regime(cfg, failures)
    print()
    test_coin_context(cfg, failures)
    print()
    test_onchain(cfg, failures)
    print()
    test_score(cfg, failures)
    print()
    test_telegram_format(cfg, failures)
    print()
    test_exit(cfg, failures)
    print()
    test_positions_store(cfg, failures)
    print()
    test_exit_alert_format(cfg, failures)
    print()
    test_ladder(cfg, failures)
    print()
    if failures:
        print(f"РЕЗУЛЬТАТ: {_FAIL} — провалено {len(failures)}: {failures}")
        return 1
    print(f"РЕЗУЛЬТАТ: {_PASS} — все проверки пройдены")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
