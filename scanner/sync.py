"""Синхронизация РЕАЛЬНЫХ позиций с Bybit по исполнениям (шаг 1 автоматизации).

Зачем: ручной `pos add` после переезда на VPS возможен только через ssh — за всё время
реальных позиций в базе 0. `run.py sync` читает спот-исполнения ключом Read-Only и сам ведёт
реальные позиции; дальше watch, brief и report работают с ними как с внесёнными руками.

Правила:
  • идемпотентно по execId: каждое исполнение пишется в exchange_fills, повтор пропускается —
    окна запроса перекрываются (overlap_days) без задвоения;
  • покупка → открытая реальная позиция монеты (merge: средняя по количеству) или новая;
  • продажа → sell() той же позиции с фактической ставкой комиссии исполнения;
  • количество — то, что реально пришло на счёт: комиссия покупки на споте часто берётся в
    базовой монете (docs/v5/enum «Spot Fee Currency Instruction»), тогда qty = execQty − execFee.
    P&L проекта считает вход как entry×(1+fee) — это и есть комиссия покупки, второй раз её
    не вшиваем (см. PositionStore.merge);
  • после продажи остаток дешевле dust_usdt (продать его на бирже нельзя — меньше лота)
    закрывается той же ценой без комиссии;
  • берутся только пары к USDT; paper-позиции не трогаются;
  • сверка с балансом счёта: расхождение позиции и walletBalance и монеты на счёте без позиции —
    строками в лог (куплены до окна синхронизации, переведены, Earn).
Без ключа (BYBIT_API_KEY / BYBIT_API_SECRET в .env) шаг тихо пропускается, код 0.
Сбой API — код 1, сводка дня покажет «⚠ синхронизация с Bybit не прошла».
"""
from __future__ import annotations

import time
from typing import Any, Callable

from . import bybit

FLAG = "bybit_sync"          # отметка успешного sync (position_events, position_id=0): курсор
VENUE = "bybit"
QUOTE = "USDT"
STABLES = {"USDT", "USDC", "USDE", "DAI", "FDUSD", "PYUSD", "USD1", "TUSD"}


def parse_fill(it: dict, quote: str = QUOTE) -> dict[str, Any] | None:
    """Исполнение /v5/execution/list -> fill или None (не сделка / не пара к quote / мусор).
    fill: exec_id, venue, symbol (базовая монета), pair, side buy|sell, price, qty (execQty),
    fee_usdt, fee_rate, base_delta (изменение базовой монеты на счёте), ts (сек), order_id."""
    if str(it.get("execType") or "Trade") != "Trade":
        return None
    pair = str(it.get("symbol", "")).upper()
    if not pair.endswith(quote) or len(pair) <= len(quote):
        return None
    base = pair[:-len(quote)]
    side = str(it.get("side", "")).lower()
    price, qty = bybit._num(it.get("execPrice")), bybit._num(it.get("execQty"))
    eid = str(it.get("execId") or "")
    if side not in ("buy", "sell") or price <= 0 or qty <= 0 or not eid:
        return None
    value = bybit._num(it.get("execValue")) or price * qty
    fee = bybit._num(it.get("execFee"))
    fee_cur = str(it.get("feeCurrency") or "").upper()
    if fee_cur == base:
        fee_usdt, fee_base = fee * price, fee
    elif fee_cur in (quote, ""):
        fee_usdt, fee_base = fee, 0.0
    else:                                       # комиссия в третьей монете — по ставке
        fee_usdt, fee_base = bybit._num(it.get("feeRate")) * value, 0.0
    delta = (qty - fee_base) if side == "buy" else -(qty + fee_base)
    return {"exec_id": eid, "venue": VENUE, "symbol": base, "pair": pair, "side": side,
            "price": price, "qty": qty, "value": value, "fee_usdt": fee_usdt,
            "fee_rate": fee_usdt / value if value > 0 else 0.0, "base_delta": delta,
            "ts": bybit._num(it.get("execTime")) / 1000.0, "order_id": str(it.get("orderId", ""))}


def apply_fills(ps, fills: list[dict], lookup: Callable[[str], dict], *,
                dust_usdt: float = 1.0) -> dict[str, Any]:
    """Применить исполнения к реальным позициям по времени. Уже записанные (exec_id в
    exchange_fills) пропускаются. lookup(тикер) -> {coin_id, chain, address, venue} для новой
    позиции (watchlist/БД, как у pos add). -> {"new", "skipped", "opened", "merged", "sold",
    "closed", "unmatched", "no_coin_id", "lines"}."""
    out: dict[str, Any] = {"new": 0, "skipped": 0, "opened": [], "merged": 0, "sold": 0,
                           "closed": [], "unmatched": [], "no_coin_id": [], "lines": []}
    for f in sorted(fills, key=lambda x: (x["ts"], x["exec_id"])):
        if ps.has_fill(f["exec_id"]):
            out["skipped"] += 1
            continue
        # Позиция и запись исполнения — одной транзакцией: сбой между ними (диск, база
        # занята) иначе оставлял докупку без exchange_fills, и следующий sync применял её
        # второй раз (кол-во 10 вместо 5).
        with ps.atomic():
            _apply_fill(ps, f, lookup, dust_usdt, out)
    return out


def _apply_fill(ps, f: dict, lookup: Callable[[str], dict], dust_usdt: float,
                out: dict[str, Any]) -> None:
    """Одно исполнение -> позиция + exchange_fills (внутри ps.atomic())."""
    out["new"] += 1
    sym, px = f["symbol"], f["price"]
    pos = ps.find_open_real(sym)
    when = time.strftime("%d.%m %H:%M", time.localtime(f["ts"]))
    if f["side"] == "buy":
        qty = f["base_delta"]
        if pos:
            m = ps.merge(pos["id"], px, qty)
            pid, note = pos["id"], "merge"
            out["merged"] += 1
            out["lines"].append(f"{when} {sym}: докупка {qty:.6g} @ {px:.6g} → позиция "
                                f"#{pid}, средняя {m['entry_price']:.6g}, кол-во "
                                f"{m['qty']:.6g}")
        else:
            info = lookup(sym) or {}
            coin_id = info.get("coin_id", "")
            pid = ps.add(sym, px, qty, coin_id=coin_id, chain=info.get("chain", ""),
                         address=info.get("address", ""), venue=VENUE, entry_ts=f["ts"],
                         notes="bybit sync")
            ps.record_event(pid, "bybit_open", px, f"исполнение {f['exec_id']}: "
                            f"{qty:.6g} @ {px:.6g}, комиссия {f['fee_usdt']:.4f} USDT")
            note = "open"
            out["opened"].append(sym)
            if not coin_id:
                out["no_coin_id"].append(sym)
            out["lines"].append(f"{when} {sym}: покупка {qty:.6g} @ {px:.6g} → новая "
                                f"позиция #{pid}" + ("" if coin_id else
                                                     " (⚠ coin_id не найден — watch не "
                                                     "достанет цену)"))
    else:
        sold = -f["base_delta"]
        if not pos:
            ps.record_fill(f, None, "sell без позиции")
            out["unmatched"].append(sym)
            out["lines"].append(f"{when} {sym}: продажа {sold:.6g} @ {px:.6g} — открытой "
                                f"реальной позиции нет (куплено до окна синхронизации?)")
            return
        pid, note = pos["id"], "sell"
        if sold > pos["qty"] * (1 + 1e-9):
            out["lines"].append(f"⚠ {sym}: продано {sold:.6g} > {pos['qty']:.6g} в позиции "
                                f"#{pid} — лишнее куплено до окна синхронизации")
        realized = ps.sell(pid, sold, px, "bybit_sell",
                           f"исполнение {f['exec_id']}: {min(sold, pos['qty']):.6g} @ "
                           f"{px:.6g}, комиссия {f['fee_usdt']:.4f} USDT",
                           fee=f["fee_rate"])
        out["sold"] += 1
        left = ps.get(pid)
        if left["status"] == "open" and left["qty"] * px < dust_usdt:
            realized += ps.sell(pid, left["qty"], px, "bybit_dust",
                                f"остаток {left['qty']:.6g} < {dust_usdt:g} USDT — "
                                f"закрыт без комиссии", fee=0.0)
            left = ps.get(pid)
        closed = left["status"] == "closed"
        if closed:
            out["closed"].append(sym)
        rest = "закрыта" if closed else f"остаток {left['qty']:.6g}"
        out["lines"].append(f"{when} {sym}: продажа {sold:.6g} @ {px:.6g} → позиция #{pid} "
                            f"{rest}, realized {realized:+.2f} USDT")
    ps.record_fill(f, pid, note)


def reconcile(ps, wallet: dict[str, dict[str, float]], *, dust_usdt: float = 1.0,
              tolerance: float = 0.01) -> list[str]:
    """Сверка открытых реальных позиций Bybit (venue = bybit) с балансом счёта -> строки
    предупреждений. Позиции других площадок, внесённые руками, не сверяются."""
    lines = []
    held: set[str] = set()
    for p in ps.open_positions():
        if p.get("is_paper") or (p.get("venue") or "").lower() != VENUE:
            continue
        sym = p["symbol"].upper()
        held.add(sym)
        bal = wallet.get(sym, {}).get("balance", 0.0)
        if abs(bal - p["qty"]) > max(tolerance * max(bal, p["qty"]), 1e-12) and \
                abs(bal - p["qty"]) * p["entry_price"] >= dust_usdt:
            lines.append(f"⚠ {sym}: на счёте {bal:.6g}, в позиции #{p['id']} {p['qty']:.6g} — "
                         f"куплено до окна синхронизации, переведено или в Earn; сверь вручную")
    for coin, c in sorted(wallet.items()):
        if coin in held or coin in STABLES or c.get("usd", 0.0) < max(dust_usdt, 5.0):
            continue
        lines.append(f"ℹ {coin}: на счёте {c['balance']:.6g} (≈{c['usd']:.0f} USD) без "
                     f"позиции в базе — куплено до окна синхронизации")
    return lines


def run_sync(cfg, ps, *, client: bybit.Client | None = None, lookup=None,
             now: float | None = None) -> tuple[int, list[str]]:
    """Шаг ежедневного прогона -> (код выхода, строки лога). Ключа нет — (0, пропуск).
    Окно: с прошлого успешного sync − overlap_days (первый раз — backfill_days) до now."""
    s = cfg.get("bybit_sync", {}) or {}
    key = cfg.get("api_keys.bybit_key", "")
    secret = cfg.get("api_keys.bybit_secret", "")
    if client is None and not (key and secret):
        return 0, ["ключа Bybit нет (BYBIT_API_KEY/BYBIT_API_SECRET в .env) — пропуск"]
    if not s.get("enabled", True):
        return 0, ["bybit_sync.enabled = false — пропуск"]
    now = now if now is not None else time.time()
    client = client or bybit.Client(key, secret, s.get("base_url", bybit.BASE_URL),
                                    recv_window=s.get("recv_window_ms", 5000))
    dust = float(s.get("dust_usdt", 1.0))
    last = ps.last_event_ts(0, FLAG)
    start = (last - s.get("overlap_days", 2) * 86400) if last else \
        now - s.get("backfill_days", 30) * 86400
    lines = [f"окно исполнений: {time.strftime('%d.%m.%y %H:%M', time.localtime(start))} — "
             f"{time.strftime('%d.%m.%y %H:%M', time.localtime(now))}"
             + ("" if last else " (первый sync)")]
    raw = client.executions(int(start * 1000), int(now * 1000))
    fills = [f for f in (parse_fill(it, s.get("quote", QUOTE)) for it in raw) if f]
    res = apply_fills(ps, fills, lookup or (lambda _sym: {}), dust_usdt=dust)
    lines.append(f"исполнений: {len(raw)} (пар к {s.get('quote', QUOTE)}: {len(fills)}), новых "
                 f"{res['new']}, уже учтённых {res['skipped']}; открыто {len(res['opened'])}, "
                 f"докупок {res['merged']}, продаж {res['sold']}, закрыто {len(res['closed'])}")
    lines += res["lines"]
    lines += reconcile(ps, client.wallet_balance(), dust_usdt=dust)
    # ts отметки = конец окна (курсор следующего sync), а не время записи
    ps.set_system_flag(FLAG, f"new={res['new']} skipped={res['skipped']}", ts=now)
    return 0, lines
