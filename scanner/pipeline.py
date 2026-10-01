"""Оркестрация Stage 0 -> 1 -> 2. Выдаёт watchlist (DB + JSON)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .chains import is_evm
from .config import Config
from .db import Store
from .http import HttpClient
from .models import Candidate
from .positions import PositionStore
from .sources import coingecko, newtokens
from .sources.goplus import fetch_goplus, fetch_honeypot_is
from .sources import defillama
from .sources.bybit import fetch_spot_basecoins, fetch_daily_closes
from .sources.funding import fetch_funding_map
from . import regime
from .stages import (antirug, entry_quality, exit as exit_stage, fundamentals,
                    liveness, onchain, zone, score)
from .sources import dune
from .sources import coin_extras
from .stages import coin_context
from .stages.access import rf_gate
from .stages.filters import apply_filters


def _make_http(cfg: Config) -> HttpClient:
    h = cfg["http"]
    limits = dict(h["rate_limits_per_min"])
    # Лимит CoinGecko в конфиге — под demo-ключ; без ключа анонимный тариф 429-ит раньше.
    if not cfg.get("api_keys.coingecko_demo", "") and "api.coingecko.com" in limits:
        limits["api.coingecko.com"] = min(limits["api.coingecko.com"],
                                          h.get("coingecko_no_key_per_min", 10))
    return HttpClient(h["cache_dir"], h["cache_ttl_seconds"], h["timeout_seconds"], limits)


def stage0_ingest(cfg: Config, http: HttpClient, track: str) -> list[Candidate]:
    demo = cfg.get("api_keys.coingecko_demo", "")
    chains = cfg["universe"]["chains_priority"]
    cands: list[Candidate] = []
    if track in ("A", "all"):
        cands += coingecko.build_track_a(
            http, cfg["universe"]["track_a_pages"],
            cfg["universe"]["track_a_per_page"], demo)
    if track in ("B", "all") and cfg["universe"].get("track_b_enabled", True):
        cands += newtokens.build_track_b(http, chains)
    return cands


def stage2_antirug(cfg: Config, http: HttpClient, candidates: list[Candidate]) -> tuple[list[Candidate], list[Candidate]]:
    key = cfg.get("api_keys.goplus", "")
    use_hp = cfg.get("stage2_antirug.use_honeypot_is", True)
    watchlist: list[Candidate] = []
    rejected: list[Candidate] = []

    for c in candidates:
        c.stage = "2"
        if c.track == "Q":
            # Нативная монета без контракта: проверять нечего, её «анти-раг» — гейт
            # качества на Stage 1 (quality_screen). Идёт дальше на зону и скор.
            c.stage = "watchlist"
            watchlist.append(c)
            continue
        sec = fetch_goplus(http, c.chain, c.address, key)
        if not sec:
            # Нет данных безопасности — не пропускаем вслепую, отправляем на ручную проверку.
            c.manual_review = True
            c.flags.append("no_security_data")
            c.reject("no_antirug_data")
            rejected.append(c)
            continue
        c.security = sec
        c.holder_count = antirug.holder_count(sec)
        c.lp_locked_pct = antirug.lp_locked_pct(sec)

        if is_evm(c.chain):
            passed, reasons, flags = antirug.evaluate_goplus_evm(sec, cfg)
            # LP-lock значим только для свежих DEX-листингов (Track B). У Track A
            # блю-чипов с ликвидностью на десятках CEX разблокированный DEX-пул —
            # шум, а не риск (иначе LINK/BNB ложно получают lp_unlocked).
            if c.track != "B":
                flags = [f for f in flags if f != "lp_unlocked"]
            c.flags += flags
            if passed and use_hp:
                hp = fetch_honeypot_is(http, c.chain, c.address)
                if hp:
                    hp_ok, hp_reasons = antirug.evaluate_honeypot_is(hp)
                    if not hp_ok:
                        passed = False
                        reasons += hp_reasons
        else:
            passed, reasons, flags = antirug.evaluate_solana(sec, cfg)
            c.flags += flags

        if passed:
            c.stage = "watchlist"
            watchlist.append(c)
        else:
            for r in reasons:
                c.reject_reasons.append(r)
            c.stage = "rejected"
            rejected.append(c)

    return watchlist, rejected


def _cfg_version(cfg: Config) -> str:
    """Короткий хэш ключевых порогов/весов — чтобы score между прогонами был
    сопоставим только внутри одной версии конфига (веса менялись несколько раз)."""
    import hashlib
    import json as _json
    key = {"w": cfg.get("stage5_score.weights"),
           "exit": cfg.get("stage8_exit"),
           "zone": cfg.get("stage4_zone"),
           "live": cfg.get("stage3b_liveness")}
    blob = _json.dumps(key, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:8]


def run_scan(cfg: Config, track: str = "all", limit: int | None = None) -> dict:
    http = _make_http(cfg)
    store = Store(cfg["output"]["db_path"])
    run_id = store.new_run(_cfg_version(cfg))

    t0 = time.time()
    # Контекст рынка альтов (market_daily): до ингеста, чтобы бэкфилл при первом
    # запуске не делил rate-limit с CoinGecko. Сбой источника -> {} (скан идёт дальше).
    from .sources import market
    mctx = market.load_context(cfg, http, store)
    mhot = mctx.get("hot") or {}

    ingested = stage0_ingest(cfg, http, track)
    if limit:
        ingested = ingested[:limit]

    # Трек Q: срез фильтра качества (монеты без контракта вместо анти-рага).
    qual = {"ok": False, "by_sym": {}, "note": "выключен"}
    if cfg.get("track_q.enabled", False):
        from .quality import load_quality
        qual = load_quality(cfg.get("track_q.source"), cfg.get("track_q.max_age_days", 30))
        if not qual["ok"]:
            print(f"[scan] трек Q пуст: {qual['note']}")

    passed1, rejected1 = apply_filters(ingested, cfg, qual["by_sym"])
    watchlist, rejected2 = stage2_antirug(cfg, http, passed1)

    # Stage 3 — фундамент/оценка по выжившим анти-раг.
    # Медианы MC/TVL по сектору строим из всего Track A ингеста (больше пиров),
    # затем оцениваем только watchlist.
    n_fund = 0
    if cfg.get("stage3_fundamentals.enabled", True) and watchlist:
        index = defillama.fetch_index(http)
        min_tvl = cfg.get("stage3_fundamentals.min_tvl_usd", 0)
        for c in ingested:
            fundamentals.attach_tvl(c, index, min_tvl)
        medians = fundamentals.build_category_medians(ingested)
        for c in watchlist:
            fundamentals.assess(c, medians, cfg)
            c.stage = "watchlist"
            n_fund += 1

    # Stage 4 — зона (дно/пик) из истории цены+объёма, режим BTC, гейт доступа РФ.
    n_zone = 0
    btc_regime: dict = {"regime": "?"}
    if cfg.get("stage4_zone.enabled", True) and watchlist:
        demo = cfg.get("api_keys.coingecko_demo", "")
        days = cfg["stage4_zone"]["price_history_days"]
        recent = cfg["stage4_zone"]["recent_days"]
        sma = cfg["stage4_zone"]["sma_days"]
        bybit_spot = fetch_spot_basecoins(http)
        funding = fetch_funding_map(http)     # 1 вызов на все перпы (сигнал дна/вершины)
        onchain_map = dune.fetch_onchain(cfg)  # {} если Dune-ключ/query не заданы
        extras = coin_extras.load(cfg, http)   # выручка/OI/делистинги/Coinbase: 4 вызова
        hard = cfg.get("stage4_zone.rf_hard_gate", False)

        # Режим BTC из Bybit klines (1000 дней) — надёжнее CoinGecko (без 429),
        # окно шире = drawdown от ATH цикла. Fallback на CoinGecko при пустом ответе.
        btc_prices = fetch_daily_closes(http, "BTCUSDT", 1000)
        if not btc_prices:
            btc_prices = coingecko.fetch_market_chart(http, "bitcoin", 365, demo)["prices"]
        btc_regime = regime.classify_regime(btc_prices, recent, sma)

        live_on = cfg.get("stage3b_liveness.enabled", True)
        kept: list[Candidate] = []
        for c in watchlist:
            chart = coingecko.fetch_market_chart(http, c.coin_id, days, demo)
            c.indicators = zone.compute_indicators(
                chart["prices"], recent, sma, chart["volumes"]) or {}
            # Stage 3b — живость: dev-активность + широта листингов + wash-ratio.
            if live_on:
                detail = coingecko.fetch_coin_detail(http, c.coin_id, demo)
                liveness.assess_liveness(c, detail, cfg)
            c.zone, c.zone_signals = zone.classify_zone(
                c.indicators or None, c.drawdown_from_ath_pct, cfg)
            if extras:
                coin_context.annotate(c, extras, cfg, chart["prices"], chart.get("mcaps"))

            # Относительная сила к BTC + разметка контекста режима.
            rs = regime.rs_vs_btc(c.indicators.get("trend_recent_pct"),
                                  btc_regime.get("trend_recent_pct"))
            if rs is not None:
                c.indicators["rs_vs_btc_pct"] = rs
            c.market_dd = btc_regime.get("drawdown")
            c.alt_market_dd = mctx.get("alt_dd")
            c.market_hot_score = mhot.get("score")
            c.market_hot_lit = list(mhot.get("lit") or [])
            c.funding_rate = funding.get(c.symbol.upper())
            # On-chain накопление (Dune): по адресу, затем по symbol; наполняет блок onchain.
            oc = onchain.match_onchain(onchain_map, c.symbol, c.address)
            if oc:
                c.net_flow_usd_7d = oc.get("net_flow_usd_7d")
                c.holders_change_pct_7d = oc.get("holders_change_pct_7d")
                c.onchain_score, oc_notes = onchain.assess_onchain(
                    c.net_flow_usd_7d, c.holders_change_pct_7d, cfg, c.volume_24h)
                c.zone_signals += oc_notes
            # Множитель качества пружины (feature_study): BTC-dd, база, объём, dd-cap, фандинг.
            if c.zone == "ПРУЖИНА/ДНО":
                c.spring_quality, q_notes = entry_quality.spring_quality(c, cfg)
                c.zone_signals += q_notes
            if btc_regime["regime"] == "BULL" and c.zone == "ПРУЖИНА/ДНО":
                c.flags.append("dd_in_bull_market")

            c.rf_access, c.rf_venue = rf_gate(c.symbol, c.chain, c.address, bybit_spot)
            if c.rf_venue == "DEX only":
                c.flags.append("rf_dex_only")
            n_zone += 1
            if hard and not c.rf_access:
                c.reject("rf_access:FAIL")
            else:
                kept.append(c)
        watchlist = kept

    # Stage 5 — композитный скор 0–100 + confidence.
    if cfg.get("stage5_score.enabled", True):
        for c in watchlist:
            c.score, c.confidence, c.score_breakdown = score.compute_score(c, cfg)

    # Ранжирование: по баллу, затем по доверию, затем глубже просадка.
    watchlist.sort(key=lambda c: (-c.score, -c.confidence,
                                  -(c.drawdown_from_ath_pct or 0)))

    store.save_candidates(run_id, ingested)
    # Метрики — не только watchlist, но и все «пружины» (даже отсеянные позже): иначе
    # ряды трендов сами обрезаны условием успеха (survivorship внутри воронки, аудит).
    metric_targets = {id(c): c for c in watchlist}
    for c in ingested:
        if c.spring_prefilter and c.coin_id:
            metric_targets.setdefault(id(c), c)
    store.save_metrics(run_id, list(metric_targets.values()))
    store.finish_run(run_id, len(ingested), len(passed1), len(watchlist))
    store.close()

    # Paper-режим: авто-открытие виртуальных позиций по топ-пружинам — фоновый
    # сбор статистики выходного контура без действий пользователя. Реальные
    # покупки вносятся руками (pos add) и в риск-бюджете учитываются только они.
    n_paper = 0
    if cfg.get("stage7_positions.paper_auto", False):
        q_by_sym = qual["by_sym"]
        if not q_by_sym and cfg.get("stage7_positions.paper_ab_stop", False):
            # трек Q выключен, но A/B стопа тоже опирается на срез качества
            from .quality import load_quality
            q_by_sym = load_quality(cfg.get("track_q.source"),
                                    cfg.get("track_q.max_age_days", 30))["by_sym"]
        n_paper = _open_paper_positions(cfg, watchlist, q_by_sym)

    _write_watchlist(cfg, watchlist)

    summary = {
        "run_id": run_id,
        "elapsed_sec": round(time.time() - t0, 1),
        "ingested": len(ingested),
        "after_filters": len(passed1),
        "rejected_filters": len(rejected1),
        "watchlist": len(watchlist),
        "track_q": (f"{sum(1 for c in watchlist if c.track == 'Q')} без контракта "
                    f"(срез {qual.get('date') or '—'})" if qual["ok"] else qual["note"]),
        "rejected_antirug": len(rejected2),
        "manual_review": sum(1 for c in rejected2 if c.manual_review),
        "stage3_enriched": n_fund,
        "stage4_zoned": n_zone,
        "btc_regime": f"{btc_regime['regime']} ({btc_regime.get('trend_recent_pct')}% за период)",
        "market": regime.context_line(mctx, btc_regime.get("drawdown")) or "нет данных",
        "market_ctx": {**mctx, "btc_dd": btc_regime.get("drawdown")} if mctx else {},
        "paper_opened": n_paper,
        "top_score": round(max((c.score for c in watchlist), default=0.0), 1),
    }
    return summary


def _open_paper_positions(cfg: Config, watchlist: list[Candidate],
                          quality: dict[str, dict] | None = None) -> int:
    """Открывает paper-позиции по пружинам, прошедшим пороги. Возвращает число новых.
    quality — срез фильтра качества {SYM: row}: монетам из него открывается близнец S
    (стоп −50%) для A/B ширины стопа."""
    from .quality import in_quality
    p = cfg["stage7_positions"]
    ab_stop = p.get("paper_ab_stop", False)
    q_ratio = cfg.get("track_q.max_mcap_ratio", 3.0)
    min_score = p.get("paper_min_score", 70)
    min_conf = p.get("paper_min_confidence", 0.6)
    stake = p.get("paper_stake_usdt", 100)
    max_open = p.get("paper_max_open", 15)
    reopen_cd = p.get("paper_reopen_cooldown_days", 30)
    ab = p.get("paper_ab", False)

    pstore = PositionStore(cfg["output"]["db_path"])
    opened = 0
    for c in watchlist:
        if c.zone != "ПРУЖИНА/ДНО" or c.score < min_score or c.confidence < min_conf:
            continue
        if not c.coin_id or not c.price_usd or c.price_usd <= 0:
            continue  # без coin_id watch не достанет цену
        if pstore.has_open_for(c.coin_id, c.symbol):
            continue
        # cooldown: не переоткрывать монету, paper-позицию по которой недавно закрыли
        # (иначе один и тот же актив крутится в тесте, раздувая статистику).
        if pstore.recent_closed_paper(c.coin_id, c.symbol, reopen_cd):
            continue
        if pstore.count_open_paper() >= max_open:
            break
        pid = pstore.add(c.symbol, c.price_usd, stake / c.price_usd,
                         coin_id=c.coin_id, chain=c.chain, address=c.address,
                         venue=c.rf_venue, paper=True,
                         notes=f"auto-paper score={c.score} conf={c.confidence} "
                               f"dd={c.drawdown_from_ath_pct}")
        pstore.record_event(pid, "paper_open", c.price_usd,
                            "; ".join(c.zone_signals[:3]))
        if ab:
            # Близнец B: тот же вход, но трейл сужается при перегреве рынка (A/B выхода).
            pstore.add(c.symbol, c.price_usd, stake / c.price_usd,
                       coin_id=c.coin_id, chain=c.chain, address=c.address,
                       venue=c.rf_venue, paper=True, variant="B", twin_of=pid,
                       notes=f"ab-twin of #{pid}")
        if ab_stop and in_quality(c, quality or {}, q_ratio):
            # Близнец S: тот же вход и выходы, но инвалидация −50% (A/B ширины стопа,
            # ladder_dca_study) — только для монет фильтра качества.
            pstore.add(c.symbol, c.price_usd, stake / c.price_usd,
                       coin_id=c.coin_id, chain=c.chain, address=c.address,
                       venue=c.rf_venue, paper=True, variant="S", twin_of=pid,
                       notes=f"stop-twin of #{pid} (−{p.get('paper_ab_stop_pct', 50)}%)")
        opened += 1
    pstore.close_db()
    return opened


def run_watch(cfg: Config) -> dict:
    """Stage 7-8: обход открытых позиций -> exit-сигналы. Дёшево: 1 вызов/позицию.

    Возвращает {"rows": [{position, pnl, held_days, signals}], "summary": {...}}.
    Идемпотентность алертов — по журналу position_events.
    """
    http = _make_http(cfg)
    pstore = PositionStore(cfg["output"]["db_path"])
    demo = cfg.get("api_keys.coingecko_demo", "")
    z = cfg["stage4_zone"]
    now = time.time()

    e = cfg["stage8_exit"]
    ladder = e["ladder"]
    peak_cooldown = e.get("peak_zone_cooldown_days", 30) * 86400
    hot_cooldown = e.get("market_hot_cooldown_days", 30) * 86400
    open_pos = pstore.open_positions()
    funding = fetch_funding_map(http) if open_pos else {}
    market_in: dict | None = None
    mctx: dict = {}
    if open_pos:
        from .sources import market
        mstore = Store(cfg["output"]["db_path"])
        mctx = market.load_context(cfg, http, mstore)
        mstore.close()
        hot = mctx.get("hot") or {}
        market_in = {"hot_score": hot.get("score"), "lit": hot.get("lit") or []}
    onchain_map = dune.fetch_onchain(cfg) if open_pos else {}  # cached-результаты, дёшево

    rows: list[dict] = []
    for pos in open_pos:
        if not pos["coin_id"]:
            rows.append({"position": pos, "pnl": None, "held_days": None,
                         "signals": [], "error": "нет coin_id — цену не достать (задай при pos add)"})
            continue
        # Только ЗАКРЫТЫЕ дневные свечи: последняя точка market_chart — live-тик,
        # он ломал бы подтверждение инвалидации «2 закрытия» и дёргал HWM/зону.
        chart = coingecko.closed_daily(coingecko.fetch_market_chart(http, pos["coin_id"], 365, demo))
        prices, ts = chart["prices"], chart["ts"]
        if not prices:
            rows.append({"position": pos, "pnl": None, "held_days": None,
                         "signals": [], "error": "нет истории цены (429/новый coin_id?)"})
            continue
        last_close = prices[-1]

        # HWM: максимум с момента входа в пределах окна + накопленный между прогонами.
        since_entry = [p for t, p in zip(ts, prices) if t >= pos["entry_ts"]]
        hwm = max([pos["hwm"] or pos["entry_price"], last_close] + since_entry)
        if hwm != pos["hwm"]:
            pstore.update_hwm(pos["id"], hwm)

        # base_low мог не проставиться при add (429) — дожимаем из истории до входа.
        if not pos.get("base_low"):
            before_entry = [p for t, p in zip(ts, prices) if t <= pos["entry_ts"]]
            bl = exit_stage.compute_base_low(before_entry)
            if bl:
                pstore.set_base_low(pos["id"], bl)
                pos["base_low"] = bl

        indicators = zone.compute_indicators(prices, z["recent_days"], z["sma_days"],
                                             chart["volumes"]) or {}
        indicators["funding_rate"] = funding.get(pos["symbol"].upper())
        triggered = pstore.event_types(pos["id"])
        # peak_zone — переармируется после cooldown (окно распределения может
        # повториться на горизонте 1–2 года), в отличие от once-ever у остальных.
        for etype, cd in (("peak_zone", peak_cooldown), ("market_hot", hot_cooldown)):
            if etype in triggered:
                lastp = pstore.last_event_ts(pos["id"], etype)
                if lastp and (now - lastp) > cd:
                    triggered = triggered - {etype}

        signals = exit_stage.evaluate_exit(pos, last_close, hwm, indicators,
                                           triggered, cfg, recent_closes=prices,
                                           market=market_in)
        executed: list[str] = []
        for s in signals:
            pstore.record_event(pos["id"], s["type"], last_close, s["note"])
            # Paper-executor: виртуально ИСПОЛНЯЕМ сигналы, чтобы журнал мерил
            # стратегию-с-выходами, а не buy&hold. Реальные позиции — только алерт.
            if pos.get("is_paper"):
                if s["type"] in ("invalidation", "trailing"):
                    r = pstore.sell(pos["id"], pstore.get(pos["id"])["qty"], last_close,
                                    f"paper_{s['type']}_exec")
                    executed.append(f"{s['type']}: выход, realized {r:+.2f}")
                elif s["type"].startswith("ladder_"):
                    idx = int(s["type"].split("_")[1])
                    frac = ladder[idx][1] if idx < len(ladder) else 0.0
                    r = pstore.sell(pos["id"], frac * pos["initial_qty"], last_close,
                                    f"paper_{s['type']}_exec")
                    executed.append(f"{s['type']}: фикс {frac*100:.0f}%, realized {r:+.2f}")

        cur = pstore.get(pos["id"]) or pos          # состояние после исполнения
        pnl = exit_stage.position_pnl(cur, last_close)
        pstore.snapshot(pos["id"], last_close, pnl["pnl_pct"], hwm)

        oc = onchain.match_onchain(onchain_map, pos["symbol"], pos.get("address"))
        rows.append({
            "position": cur, "last_price": last_close, "hwm": hwm,
            "pnl": pnl,
            "realized_usdt": round(cur.get("realized_usdt") or 0.0, 2),
            "held_days": exit_stage.held_days(pos, now),
            "signals": signals, "executed": executed,
            "spark_prices": since_entry if since_entry else prices[-30:],
            "triggered": triggered,
            "net_flow_usd_7d": (oc or {}).get("net_flow_usd_7d"),
        })

    pstore.close_db()
    ok_rows = [r for r in rows if r.get("pnl")]
    summary = exit_stage.summarize_watch(ok_rows)
    summary["market"] = regime.context_line(mctx) if mctx else ""
    return {"rows": rows, "summary": summary}


def _write_watchlist(cfg: Config, watchlist: list[Candidate]) -> None:
    path = Path(cfg["output"]["watchlist_json"])
    rows = []
    for c in watchlist:
        rows.append({
            "symbol": c.symbol, "name": c.name, "chain": c.chain,
            "address": c.address, "track": c.track, "coin_id": c.coin_id,
            "score": c.score, "confidence": c.confidence,
            "score_breakdown": c.score_breakdown,
            "market_cap": c.market_cap, "fdv": c.fdv,
            "volume_24h": c.volume_24h, "liquidity_usd": c.liquidity_usd,
            "liveness_score": c.liveness_score, "liveness_notes": c.liveness_notes,
            "dev_commits_4w": c.dev_commits_4w, "n_exchanges": c.n_exchanges,
            "on_cex": c.on_cex, "holder_count": c.holder_count,
            "lp_locked_pct": c.lp_locked_pct, "wash_ratio": c.wash_ratio,
            "drawdown_from_ath_pct": c.drawdown_from_ath_pct,
            "spring": c.spring_prefilter,
            "zone": c.zone, "zone_signals": c.zone_signals,
            "indicators": c.indicators,
            "market_dd": c.market_dd, "spring_quality": c.spring_quality,
            "alt_market_dd": c.alt_market_dd, "market_hot_score": c.market_hot_score,
            "market_hot_lit": c.market_hot_lit,
            "funding_rate": c.funding_rate,
            "supply_growth": c.supply_growth, "revenue_30d": c.revenue_30d,
            "p_f": c.p_f, "oi_mcap": c.oi_mcap, "delist": c.delist, "us_tag": c.us_tag,
            "onchain_score": c.onchain_score,
            "net_flow_usd_7d": c.net_flow_usd_7d,
            "holders_change_pct_7d": c.holders_change_pct_7d,
            "rf_venue": c.rf_venue, "rf_access": c.rf_access,
            "category": c.category, "tvl": c.tvl,
            "mc_tvl": c.mc_tvl, "fdv_mc": c.fdv_mc,
            "val_notes": c.val_notes,
            "flags": c.flags, "manual_review": c.manual_review,
        })
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
