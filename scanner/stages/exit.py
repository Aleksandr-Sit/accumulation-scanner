"""Stage 8 — выходной контур (сигналы на продажу held-позиций). Чистые функции.

Профиль стратегии — hodl/накопление, не свинг: купил у дна -> держим год-два ->
распределение в бычьей фазе. Отсюда правила:

  1. ИНВАЛИДАЦИЯ (всегда активна): цена пробила лоу базы входа − буфер.
     Тезис «дно» сломан — держать труп нельзя даже при холде. Выход полностью.
  2. ЛЕСТНИЦА (циклические уровни): частичная фиксация на кратных росте
     (дефолт +100% -> 25%, +300% -> 25%). Уровни из config, [оценка] до бэктеста
     полного цикла (эмпирика ladder_study покрыла только свинг-горизонт).
  3. ТРЕЙЛИНГ (взводимый): активируется ТОЛЬКО после arm_after_gain (+60%),
     чтобы не выбивало на шуме, пока тезис жив и прибыли нет. После взвода —
     просадка от HWM больше порога = сигнал фиксации остатка.
  4. ЗОНА РАСПРЕДЕЛЕНИЯ (информационный): бычий разгон — цена высоко над SMA,
     верх диапазона, (опц.) расширение объёма. Не приказ, а «пора смотреть».
  5. ПЕРЕГРЕВ РЫНКА (информационный): индекс перегрева альт-рынка ≥ market_hot_alert.
     Опционально сужает трейлинг (market_hot_tighten или paper-близнец variant=B):
     market_regime_study — медиана лучше, но режет хвост маний, поэтому A/B на paper.
  Близнец variant=S (paper, монеты фильтра качества) — инвалидация шире (−50% вместо
  −25%, ladder_dca_study: главный рычаг — ширина стопа); остальное как у A.

Сигнал — алерт для ручного решения, НЕ ордер. Идемпотентность — по журналу
position_events (типы уже сработавших событий передаются в triggered).
"""
from __future__ import annotations

from ..config import Config


def evaluate_exit(position: dict, last_price: float, hwm: float,
                  indicators: dict | None, triggered: set[str],
                  cfg: Config, recent_closes: list[float] | None = None,
                  market: dict | None = None, armed: bool | None = None) -> list[dict]:
    """Возвращает список новых сигналов [{type, action, note, urgency}].

    position: dict c entry_price, base_low (может быть None).
    triggered: типы событий, уже записанных в журнал (не дублируем алерты).
    recent_closes: хвост дневных закрытий (для подтверждения инвалидации N дней).
        None -> проверка по одному last_price (обратная совместимость).
    market: {"hot_score": 0..1 | None, "lit": [флаги]} — перегрев рынка (None = нет данных).
    armed: трейл взведён фактом закрытия ≥ +arm от средней (защёлка watch). None — по hwm
        (прежнее правило: после докупки вниз старый максимум от новой средней взводил трейл
        на убыточной позиции).
    """
    e = cfg["stage8_exit"]
    entry = position["entry_price"]
    base_low = position.get("base_low")
    signals: list[dict] = []
    if not isinstance(entry, (int, float)) or entry <= 0 or last_price <= 0:
        return signals

    gain = last_price / entry - 1
    hwm_gain = hwm / entry - 1 if hwm > 0 else 0.0
    hot = (market or {}).get("hot_score")
    hot_on = isinstance(hot, (int, float)) and hot >= e.get("market_hot_alert", 1.01)
    tighten = hot_on and (e.get("market_hot_tighten", False) or position.get("variant") == "B")

    # 1) Инвалидация тезиса — важнее всего, дальше можно не смотреть.
    #    Подтверждение N закрытий подряд ниже пола отсекает однодневный shakeout
    #    (59-70% пружин прокалывают базу перед разворотом — калибровка 16.07.2026).
    #    Близнец S (A/B ширины стопа на paper) — пол ниже: stage7_positions.paper_ab_stop_pct.
    inv_pct = e["invalidation_below_base_low_pct"]
    if position.get("variant") == "S":
        inv_pct = cfg.get("stage7_positions.paper_ab_stop_pct", inv_pct)
    if isinstance(base_low, (int, float)) and base_low > 0:
        floor = base_low * (1 - inv_pct / 100.0)
        confirm = int(e.get("invalidation_confirm_days", 1))
        if recent_closes and len(recent_closes) >= confirm and confirm >= 1:
            tail = recent_closes[-confirm:]
            breached = all(p < floor for p in tail)
        else:
            breached = last_price < floor   # нет истории -> проверка по одному close
        if breached and "invalidation" not in triggered:
            conf_note = f", {confirm} закрытия подряд" if confirm > 1 else ""
            signals.append({
                "type": "invalidation", "urgency": "high",
                "action": "ВЫЙТИ ПОЛНОСТЬЮ",
                "note": (f"цена {last_price:.6g} пробила лоу базы {base_low:.6g} "
                         f"(−{inv_pct:g}% буфер{conf_note}) — тезис «дно» сломан"),
            })
            return signals  # инвалидация исключает остальные сигналы

    # 2) Лестница циклической фиксации.
    for i, (level, frac) in enumerate(e["ladder"]):
        etype = f"ladder_{i}"
        if etype in triggered:
            continue
        if gain >= level:
            signals.append({
                "type": etype, "urgency": "medium",
                "action": f"ЗАФИКСИРОВАТЬ {frac * 100:.0f}%",
                "note": f"достигнут уровень +{level * 100:.0f}% (сейчас {gain * 100:+.0f}%)",
            })

    # 3) Взводимый трейлинг: только после существенной прибыли.
    if armed is None:
        armed = hwm_gain >= e["trailing_arm_after_gain_pct"] / 100.0
    trail_pct = e["trailing_from_hwm_pct"]
    if tighten:
        trail_pct = min(trail_pct, e.get("market_hot_trailing_pct", trail_pct))
    if armed and hwm > 0 and "trailing" not in triggered:
        dd_from_hwm = 1 - last_price / hwm
        if dd_from_hwm >= trail_pct / 100.0:
            why = f", трейл сужен до {trail_pct:g}% — рынок перегрет" if tighten else ""
            signals.append({
                "type": "trailing", "urgency": "high",
                "action": "ЗАФИКСИРОВАТЬ ОСТАТОК",
                "note": (f"откат −{dd_from_hwm * 100:.0f}% от максимума {hwm:.6g} "
                         f"(пик был {hwm_gain * 100:+.0f}% от входа{why})"),
            })

    # 4) Зона распределения — информационный (повторяется не чаще раза, см. журнал).
    if indicators and "peak_zone" not in triggered:
        above = indicators.get("pct_above_sma")
        rangep = indicators.get("range_pos")
        vol_exp = indicators.get("vol_trend")  # >1 = расширение объёма (None = нет данных)
        funding = indicators.get("funding_rate")  # эйфория лонгов = перегрев у вершины
        euphoria = (isinstance(funding, (int, float))
                    and funding >= e.get("funding_euphoria", 0.0005))
        # не `hot`: имя занято индексом перегрева рынка (п.5 писал бы «перегрев 0%»)
        overheated = (isinstance(above, (int, float)) and above >= e["peak_min_above_sma_pct"]
                      and isinstance(rangep, (int, float)) and rangep >= e["peak_min_range_pos"])
        if (overheated or euphoria) and gain > 0:
            parts = []
            if overheated:      # только фандинг (меньше 15 закрытий): above/rangep — None
                parts.append(f"разгон: +{above:.0f}% над SMA, верх диапазона ({rangep:.2f})")
            if isinstance(vol_exp, (int, float)) and vol_exp >= e.get("vol_expansion_ratio", 1.6):
                parts.append(f"объём ×{vol_exp:.1f} к базе")
            if euphoria:
                parts.append(f"фандинг +{funding*100:.3f}%/8h (эйфория лонгов — распределение)")
            note = ", ".join(parts)
            signals.append({
                "type": "peak_zone", "urgency": "low",
                "action": "ЗОНА РАСПРЕДЕЛЕНИЯ — рассмотреть фиксацию",
                "note": note,
            })

    # 5) Перегрев рынка альтов — информационный, переармируется после cooldown.
    if hot_on and gain > 0 and "market_hot" not in triggered:
        from ..regime import FLAG_LABELS
        lit = ", ".join(FLAG_LABELS.get(k, k) for k in (market or {}).get("lit") or [])
        signals.append({
            "type": "market_hot", "urgency": "medium",
            "action": "РЫНОК ПЕРЕГРЕТ — рассмотреть фиксацию",
            "note": (f"индекс перегрева {hot*100:.0f}% ({lit}); исторически после такого "
                     f"рынок альтов чаще отдаёт, чем растёт"
                     + (f" · трейл сужен до {trail_pct:g}%" if tighten else "")),
        })

    return signals


def compute_base_low(prices: list[float], lookback: int = 30) -> float | None:
    """Лоу базы входа: минимум последних lookback дневных цен (на момент входа)."""
    if not prices:
        return None
    window = prices[-lookback:]
    return min(window) if window else None


def position_pnl(position: dict, last_price: float,
                 fee_side: float = 0.0015) -> dict:
    """P&L позиции net-of-fees (Bybit spot ~0.1% + спред, за сторону)."""
    entry, qty = position["entry_price"], position["qty"]
    cost = entry * qty * (1 + fee_side)
    value = last_price * qty * (1 - fee_side)
    return {
        "value_usdt": round(value, 2),
        "pnl_usdt": round(value - cost, 2),
        "pnl_pct": round((value / cost - 1) * 100, 1) if cost > 0 else 0.0,
    }


def held_days(position: dict, now_ts: float) -> int:
    return int((now_ts - position["entry_ts"]) / 86400)


def summarize_watch(rows: list[dict]) -> dict:
    """Сводка по прогону watch для консоли/журнала."""
    n_sig = sum(len(r["signals"]) for r in rows)
    worst = min((r["pnl"]["pnl_pct"] for r in rows), default=0.0)
    best = max((r["pnl"]["pnl_pct"] for r in rows), default=0.0)
    total = round(sum(r["pnl"]["pnl_usdt"] for r in rows), 2)
    return {"positions": len(rows), "signals": n_sig,
            "pnl_total_usdt": total, "best_pct": best, "worst_pct": worst}
