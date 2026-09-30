"""Планировщик лестницы: ступени покупки до пола стопа, цели продажи, худший случай.

Чистые функции без сети (данные биржи передаются параметрами) — проверяются selftest.
Схема входа — «4 ступени до пола» из backtest/ladder_dca_study.py: первая ступень рынком,
остальные — лимитки ровно до 1.05 × уровня стопа; стоп — как в проде (stage8_exit):
N закрытий ниже лоу 30-дневной базы минус буфер. Продажа:
  prod   — цели stage8_exit.ladder от средней (+50% → 1/3, +150% → 1/3), остаток — трейл;
  even   — +30/60/100/200% по 1/4;
  paired — каждая ступень продаётся целиком на +40% от своей цены.
Округление: цена покупки — вниз к tickSize, продажи — вверх; количество — вниз к
basePrecision; ступень меньше минимума поднимается до минимума (вверх по шагу).
Ордер продажи меньше минимума биржи сливается со следующим (последний — остаток).
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

FLOOR_MARGIN = 1.05          # нижняя ступень чуть выше уровня стопа (как в бэктесте)
MARKET_FEE = 0.0015          # рыночный ордер: 0.1% комиссия + ~0.05% спред
LIMIT_FEE = 0.0010           # лимитка: 0.1% (Bybit spot, базовый уровень)

SELL_MODES = {
    "even": [(0.30, 0.25), (0.60, 0.25), (1.00, 0.25), (2.00, 0.25)],
    "paired": 0.40,
}


def round_step(x: float, step: float, up: bool = False) -> float:
    """Округление к шагу биржи через Decimal (без хвостов 0.30000000000000004)."""
    if step <= 0:
        return x
    d, s = Decimal(str(x)), Decimal(str(step))
    n = (d / s).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return float(n * s)


def _decimals(step: float) -> int:
    exp = Decimal(str(step)).normalize().as_tuple().exponent
    return max(0, -int(exp))


def fmt_step(x: float, step: float) -> str:
    return f"{x:.{_decimals(step)}f}"


def plan_ladder(price: float, base_low: float, budget: float, *, steps: int = 4,
                min_order: float = 10.0, tick: float = 0.0, qty_step: float = 0.0,
                exch_min_amt: float = 5.0, exch_min_qty: float = 0.0,
                floor_pct: float = 25.0, stop_pct: float | None = None,
                sell: str = "prod", prod_levels: list | None = None,
                filled: int | None = None, worst_case_pct: float = 60.0) -> dict:
    """План лестницы. Возвращает {ok, buys, stop, avg, worst, sells, warns} или {ok: False,
    error}. floor_pct — буфер под лоу базы для НИЖНЕЙ ступени (прод-инвалидация);
    stop_pct — уровень стопа (по умолчанию = floor_pct; шире — вариант «стоп −50%»)."""
    warns: list[str] = []
    stop_pct = floor_pct if stop_pct is None else stop_pct
    floor = base_low * (1 - floor_pct / 100.0)
    stop_px = base_low * (1 - stop_pct / 100.0)
    if price <= 0 or base_low <= 0 or budget <= 0:
        return {"ok": False, "error": "цена, лоу базы и бюджет должны быть > 0"}
    if price <= stop_px:
        return {"ok": False, "error": f"цена {price:g} уже ниже уровня стопа {stop_px:g} — "
                                      f"тезис «дно» сломан, лестницу не строю"}
    need = max(min_order, exch_min_amt)
    max_steps = int(budget // need)
    if max_steps < 1:
        return {"ok": False, "error": f"бюджет ${budget:g} меньше минимального ордера ${need:g}"}
    if steps > max_steps:
        warns.append(f"бюджет ${budget:g} / минимум ${need:g} → ступеней {max_steps} вместо {steps}")
        steps = max_steps
    if price / base_low > 1.20:
        warns.append(f"цена на {price / base_low - 1:.0%} выше лоу базы — это не вход у дна: "
                     f"лестница растянута, стоп далеко (в бэктесте вход — по сигналу «пружина»)")
    bottom = floor * FLOOR_MARGIN
    if steps > 1 and price <= bottom * 1.02:
        warns.append("цена уже у пола стопа — лестница вырождается в одну покупку")
        steps = 1

    # --- покупки
    per = budget / steps
    prices = [price]
    if steps > 1:
        gap = (price - bottom) / (steps - 1)
        prices += [price - k * gap for k in range(1, steps)]
    buys = []
    for i, px in enumerate(prices):
        market = i == 0
        px = px if market else round_step(px, tick)
        qty = round_step(per / px, qty_step)
        if qty * px < need or qty < exch_min_qty:
            qty = max(round_step(need / px, qty_step, up=True),
                      round_step(exch_min_qty, qty_step, up=True))
        buys.append({"n": i + 1, "type": "рынок" if market else "лимит", "price": px,
                     "qty": qty, "usd": qty * px, "from_price": px / price - 1})
    spent = sum(b["usd"] for b in buys)
    if spent > budget * 1.001:
        warns.append(f"после округления к минимумам вложится ${spent:.2f} > бюджета ${budget:g}")

    k = len(buys) if filled is None else max(1, min(filled, len(buys)))
    got = buys[:k]
    qty_k = sum(b["qty"] * (1 - (MARKET_FEE if b["type"] == "рынок" else LIMIT_FEE))
                for b in got)
    usd_k = sum(b["usd"] for b in got)
    avg = usd_k / qty_k if qty_k else 0.0

    # --- худший случай: исполнены все ступени, выход по уровню стопа (закрытие)
    qty_all = sum(b["qty"] * (1 - (MARKET_FEE if b["type"] == "рынок" else LIMIT_FEE))
                  for b in buys)
    stop_value = qty_all * stop_px * (1 - MARKET_FEE)
    worst = {"stop_loss_usd": spent - stop_value,
             "stop_loss_pct": (1 - stop_value / spent) if spent else 0.0,
             "gap_loss_usd": spent * worst_case_pct / 100.0,
             "lump_stop_loss_pct": 1 - stop_px * (1 - MARKET_FEE) / (price * (1 + MARKET_FEE))}

    # --- продажи (от исполненных k ступеней)
    sells = plan_sells(got, qty_k, avg, sell=sell, prod_levels=prod_levels, tick=tick,
                       qty_step=qty_step, exch_min_amt=exch_min_amt, warns=warns)
    return {"ok": True, "steps": steps, "buys": buys, "filled": k, "spent": spent,
            "avg": avg, "qty": qty_k, "floor": floor, "bottom": bottom, "stop_px": stop_px,
            "stop_pct": stop_pct, "worst": worst, "sells": sells, "warns": warns}


def plan_sells(got: list[dict], qty: float, avg: float, *, sell: str,
               prod_levels: list | None, tick: float, qty_step: float,
               exch_min_amt: float, warns: list[str]) -> list[dict]:
    if sell == "paired":
        tp = SELL_MODES["paired"]
        out = []
        for b in got:
            fee = MARKET_FEE if b["type"] == "рынок" else LIMIT_FEE
            q = round_step(b["qty"] * (1 - fee), qty_step)
            px = round_step(b["price"] * (1 + tp), tick, up=True)
            out.append({"label": f"ступень {b['n']} +{tp:.0%}", "price": px, "qty": q,
                        "usd": q * px, "gain": px / b["price"] - 1})
        return out
    levels = (prod_levels or [(0.5, 0.33), (1.5, 0.33)]) if sell == "prod" else SELL_MODES["even"]
    raw = []
    left = qty
    for i, (g, frac) in enumerate(levels):
        last = sell == "even" and i == len(levels) - 1
        q = left if last else round_step(qty * frac, qty_step)
        q = min(q, left)
        raw.append({"gain": g, "qty": q})
        left -= q
    # ордер меньше минимума биржи — сливаем со следующим (последний — с остатком/трейлом)
    out: list[dict] = []
    carry = 0.0
    for i, r in enumerate(raw):
        px = round_step(avg * (1 + r["gain"]), tick, up=True)
        q = r["qty"] + carry
        if q * px < exch_min_amt and i < len(raw) - 1:
            carry = q
            warns.append(f"продажа на +{r['gain']:.0%} меньше ${exch_min_amt:g} — слита со следующей")
            continue
        carry = 0.0
        q = round_step(q, qty_step)
        if q * px < exch_min_amt:
            warns.append(f"продажа на +{r['gain']:.0%} меньше ${exch_min_amt:g} даже после слияния — "
                         f"продавать там весь остаток")
        out.append({"label": f"+{r['gain']:.0%}", "price": px, "qty": q, "usd": q * px,
                    "gain": r["gain"]})
    rest = round_step(left, qty_step)
    if sell == "prod" and rest > 0:
        out.append({"label": "остаток — трейл", "price": None, "qty": rest, "usd": None,
                    "gain": None})
        if rest * avg * 1.6 < exch_min_amt:       # трейл взводится после +60%
            warns.append("остаток под трейл меньше минимума биржи — продать вместе с последней целью")
    return out
