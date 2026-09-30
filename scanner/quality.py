"""Срез фильтра качества монет (backtest/quality_screen.py → quality_screen_results.json).

Общая точка чтения среза для воронки и выхода:
  • трек Q — монеты без контракта (BTC, ETH, SOL, XRP, ADA…), которые Stage 1 иначе
    отбрасывает как «анти-раг невозможен»: вместо проверки контракта — жёсткий гейт
    качества (капа, оборот и его ранг, топ-биржи, возраст ≥ 1 года, FDV/MC, Bybit без ST,
    не мем);
  • A/B ширины стопа на paper — близнецы со стопом −50% только для монет из фильтра.
Срез делается руками (~6 мин сети) и стареет: max_age_days ограничивает, сколько дней
ему верить. Нет файла / протух / битый — пустой набор (трек Q просто не наполняется).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
DEFAULT_PATH = "backtest/quality_screen_results.json"


def load_quality(path: str | None = None, max_age_days: float | None = None,
                 now: float | None = None) -> dict:
    """{"ok", "date", "age_days", "by_sym": {SYM: row}, "note"} — только прошедшие гейт
    (пустой fails). Из дублей тикера срез уже взял старший по капе."""
    p = Path(path or DEFAULT_PATH)
    if not p.is_absolute():
        p = PROJ / p
    out = {"ok": False, "date": "", "age_days": None, "by_sym": {}, "note": ""}
    if not p.exists():
        out["note"] = f"среза нет ({p.name}): py -3 -u backtest/quality_screen.py"
        return out
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        out["note"] = f"срез повреждён ({p.name})"
        return out
    out["date"] = d.get("date", "")
    try:
        day_ts = time.mktime(time.strptime(out["date"], "%Y-%m-%d"))
        out["age_days"] = ((now or time.time()) - day_ts) / 86400
    except (TypeError, ValueError):
        out["age_days"] = None
    if max_age_days is not None and (out["age_days"] is None or out["age_days"] > max_age_days):
        out["note"] = (f"срез от {out['date'] or '?'} старше {max_age_days:g} дн — "
                       f"обнови: py -3 -u backtest/quality_screen.py")
        return out
    out["by_sym"] = {r["sym"].upper(): r for r in d.get("rows", [])
                     if r.get("sym") and not r.get("fails")}
    out["ok"] = True
    return out


def in_quality(c, by_sym: dict[str, dict], max_ratio: float = 3.0) -> bool:
    """Кандидат воронки из фильтра качества: трек Q — по построению; остальные — тикер
    в срезе и капа сходится (тёзки не проходят)."""
    if getattr(c, "track", "") == "Q":
        return True
    row = by_sym.get((c.symbol or "").upper())
    return bool(row) and mcap_matches(c.market_cap, row, max_ratio)


def mcap_matches(cg_mcap: float | None, row: dict, max_ratio: float = 3.0) -> bool:
    """Та же ли это монета: тикер совпал, капа CoinGecko и CMC в пределах max_ratio раз.
    Защита от тёзок (мелкая монета без контракта с тикером крупной)."""
    cmc = row.get("mcap")
    if not isinstance(cg_mcap, (int, float)) or not isinstance(cmc, (int, float)):
        return False
    if cg_mcap <= 0 or cmc <= 0:
        return False
    r = cg_mcap / cmc
    return 1 / max_ratio <= r <= max_ratio
