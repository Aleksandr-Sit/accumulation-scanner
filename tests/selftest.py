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
    _check("дайджест: ближайший уровень лестницы", "+100%" in (dig or ""), failures)
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


def main() -> int:
    cfg = load_config()
    failures: list[str] = []
    print("=== SELFTEST (офлайн, без сети) ===\n")
    test_filters(cfg, failures)
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
    test_regime(cfg, failures)
    print()
    test_rf_gate(cfg, failures)
    print()
    test_liveness(cfg, failures)
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
    if failures:
        print(f"РЕЗУЛЬТАТ: {_FAIL} — провалено {len(failures)}: {failures}")
        return 1
    print(f"РЕЗУЛЬТАТ: {_PASS} — все проверки пройдены")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
