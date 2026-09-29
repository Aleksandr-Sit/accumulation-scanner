"""Stage 4b — множитель качества пружины из дата-находок (feature_study 17.07.2026).

Не все пружины равны. На 945 эпизодах Binance 2017-2026 (survivor-upper-bound,
опора на P(+100%), не на выбросный mean) выделились предикторы:

  • Просадка рынка (стресс) — САМЫЙ сильный; с 29.09.2026 по умолчанию просадка
    альт-рынка без стейблов (market_regime_study), BTC — фолбэк. Просадка BTC: пружина при BTC у ATH → P=22%,
    при BTC −55%+ → P=74%. Пружина на альте, пока рынок на хаях = относительная
    слабость (труп); пружина, когда весь рынок на дне = mean-reversion.
  • Длина базы у дна: база ≤20д → 44%, база 50-100д → 67%. Долго стоял = живой.
  • Объём dry-up — ПОЛОСА, не «чем ниже тем лучше»: 0.5-0.8 → 57%, мёртвая тишина
    ≤0.3 → 44%, растущий объём >1.2 → 39%.
  • Просадка от ATH: сладкая зона 80-94% → 53%, >94% (около нуля) → 42% (риск
    обнуления).

Возвращает множитель ~[0.4, 1.25] к под-баллу зоны для пружин + пояснения.
Чистая функция — тестируется офлайн. Недостающие данные не штрафуют (нейтрально).
"""
from __future__ import annotations

from ..config import Config


def _lerp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if x1 == x0:
        return y0
    t = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
    return y0 + t * (y1 - y0)


def spring_quality(candidate, cfg: Config) -> tuple[float, list[str]]:
    """Множитель качества пружины (1.0 = нейтрально) + заметки. Только для зоны дна."""
    q = cfg.get("stage4b_quality", {}) or {}
    mult = 1.0
    notes: list[str] = []

    # 1) Стресс рынка — сильнейший фактор. Источник (market_dd_source):
    #    alt — просадка альт-рынка без стейблов (устойчивее в обоих циклах), нет данных ->
    #    откат на BTC; btc — прежнее поведение; mix — среднее двух множителей.
    lo_m, hi_m = q.get("btc_dd_min_mult", 0.6), q.get("btc_dd_max_mult", 1.15)
    mdd = getattr(candidate, "market_dd", None)
    add = getattr(candidate, "alt_market_dd", None)
    m_btc = (_lerp(mdd, 0.0, q.get("btc_dd_full", 0.5), lo_m, hi_m)
             if isinstance(mdd, (int, float)) else None)
    m_alt = (_lerp(add, q.get("alt_dd_from", 0.35), q.get("alt_dd_full", 0.65), lo_m, hi_m)
             if isinstance(add, (int, float)) else None)
    src = q.get("market_dd_source", "btc")
    use_alt = src in ("alt", "mix") and m_alt is not None
    if src == "mix" and m_btc is not None and m_alt is not None:
        m = (m_btc + m_alt) / 2
    else:
        m = m_alt if use_alt else m_btc
    if m is not None:
        mult *= m
        if use_alt:
            if add >= q.get("alt_dd_full", 0.65):
                notes.append(f"🎯 альт-рынок на дне (−{add*100:.0f}% от ATH) — сильный контекст для пружины")
            elif add <= q.get("alt_dd_from", 0.35):
                notes.append(f"⚠ альт-рынок у хаёв (−{add*100:.0f}%) — пружина = слабость к рынку (низкая P)")
        elif mdd >= 0.55:
            notes.append(f"🎯 рынок на дне (BTC −{mdd*100:.0f}%) — сильный контекст для пружины")
        elif mdd <= 0.15:
            notes.append(f"⚠ BTC близко к ATH (−{mdd*100:.0f}%) — пружина = слабость к рынку (низкая P)")

    # 1b) Перегрев рынка на входе (8 флагов): холодный рынок — P(+100) 59–77%,
    #     хотя бы один флаг — 27–52%. «Тёплый» = горит, но ниже hot_threshold.
    hs = getattr(candidate, "market_hot_score", None)
    if isinstance(hs, (int, float)) and hs > 0:
        lit = getattr(candidate, "market_hot_lit", None) or []
        from ..regime import FLAG_LABELS
        names = ", ".join(FLAG_LABELS.get(k, k) for k in lit)
        if hs >= q.get("hot_threshold", 0.25):
            mult *= q.get("hot_penalty", 0.75)
            notes.append(f"🔥 рынок перегрет ({names}) — пружины исторически слабее")
        else:
            mult *= q.get("hot_warm_penalty", 0.85)
            notes.append(f"⚠ рынок теплеет ({names})")

    # 2) Длина базы у дна.
    ind = getattr(candidate, "indicators", None) or {}
    base_len = ind.get("base_len_days")
    if isinstance(base_len, (int, float)):
        if base_len >= q.get("base_len_strong", 50):
            mult *= q.get("base_len_bonus", 1.1)
            notes.append(f"длинная база {base_len:.0f}д — накопление (P выше)")
        elif base_len <= q.get("base_len_weak", 20):
            mult *= q.get("base_len_penalty", 0.92)
            notes.append(f"короткая база {base_len:.0f}д — свежее дно (P ниже)")

    # 3) Объём dry-up полосой.
    vt = ind.get("vol_trend")
    if isinstance(vt, (int, float)):
        lo_b, hi_b = q.get("dryup_band", [0.5, 0.8])
        if lo_b <= vt <= hi_b:
            mult *= q.get("dryup_bonus", 1.05)
            notes.append(f"объём в полосе накопления (×{vt:.2f})")
        elif vt <= q.get("dryup_dead", 0.3):
            mult *= q.get("dryup_penalty", 0.9)
            notes.append(f"⚠ мёртвая тишина по объёму (×{vt:.2f}) — нет интереса")
        elif vt >= q.get("dryup_rising", 1.2):
            mult *= q.get("dryup_penalty", 0.9)
            notes.append(f"⚠ объём растёт у дна (×{vt:.2f}) — не накопление")

    # 4) Экстремальная просадка (риск обнуления).
    dd = getattr(candidate, "drawdown_from_ath_pct", None)
    if isinstance(dd, (int, float)) and dd >= q.get("dd_extreme_pct", 94):
        mult *= q.get("dd_extreme_penalty", 0.85)
        notes.append(f"⚠ просадка {dd:.0f}% (около нуля) — риск обнуления")

    # 5) Фандинг перпа (ортогонален цене): экстремально отрицательный у дна =
    #    толпа в шортах = топливо отскока (шорт-сквиз). Положительный на пружине
    #    подозрителен (эйфория без роста).
    fr = getattr(candidate, "funding_rate", None)
    if isinstance(fr, (int, float)):
        if fr <= q.get("funding_capitulation", -0.0003):
            mult *= q.get("funding_bonus", 1.08)
            notes.append(f"фандинг {fr*100:.3f}%/8h — шорты в толпе (капитуляция, топливо отскока)")
        elif fr >= q.get("funding_euphoria", 0.0005):
            mult *= q.get("funding_penalty", 0.92)
            notes.append(f"⚠ фандинг +{fr*100:.3f}%/8h — эйфория лонгов на пружине (подозрительно)")

    mult = max(q.get("mult_floor", 0.4), min(q.get("mult_cap", 1.25), mult))
    return round(mult, 3), notes
