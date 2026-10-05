"""Срез фильтра качества монет (backtest/quality_screen.py → JSON с полем date).

Общая точка чтения среза для воронки, выхода и CLI:
  • трек Q — монеты без контракта (BTC, ETH, SOL, XRP, ADA…), которые Stage 1 иначе
    отбрасывает как «анти-раг невозможен»: вместо проверки контракта — жёсткий гейт
    качества (капа, оборот и его ранг, топ-биржи, возраст ≥ 1 года, FDV/MC, Bybit без ST,
    не мем);
  • A/B ширины стопа на paper — близнецы со стопом −50% только для монет из фильтра;
  • пометка фильтра в `run.py ladder`, статус в `run.py quality` и в сводке дня.
Срезов два (config track_q): source — закоммиченный ручной, live_source — автообновляемый
(data/, вне git: `run.py quality --refresh-if-due` в ежедневном прогоне, когда срезу
≥ refresh_after_days). Резолвер fresh_slice берёт свежий по полю date — читать срез только
через него. Старше max_age_days — трек Q пуст; сводка дня предупреждает заранее.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
DEFAULT_PATH = "backtest/quality_screen_results.json"
SCREEN = PROJ / "backtest" / "quality_screen.py"
REFRESH_TIMEOUT_SEC = 30 * 60        # срез ~6 мин сети; дольше получаса — завис
SRC_LABEL = {"live_source": "авто", "source": "закоммиченный"}


def _abs(p) -> Path:
    path = Path(p)
    return path if path.is_absolute() else PROJ / path


def rel(p) -> str:
    """Путь от корня проекта (для логов), чужой — как есть."""
    try:
        return Path(p).resolve().relative_to(PROJ).as_posix()
    except ValueError:
        return str(p)


def slice_paths(cfg) -> list[tuple[str, Path]]:
    """[("live_source", путь), ("source", путь)] из track_q; относительные — от корня проекта."""
    out = []
    for key, default in (("live_source", ""), ("source", DEFAULT_PATH)):
        v = cfg.get(f"track_q.{key}") or default
        if v:
            out.append((key, _abs(v)))
    return out


def live_path(cfg) -> Path | None:
    v = cfg.get("track_q.live_source")
    return _abs(v) if v else None


def day_after(date: str, days: float) -> str:
    """«DD.MM» через days дней после даты среза — календарём (сдвиг DST не переносит дату)."""
    return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=days)).strftime("%d.%m")


def read_slice(path, now: float | None = None) -> dict:
    """Один файл среза -> {"ok", "path", "date", "day_ts", "age_days", "rows", "note"}.
    ok — файл прочитан и дата разобрана; возраст — от локальной полуночи даты среза."""
    p = _abs(path)
    out = {"ok": False, "path": p, "date": "", "day_ts": None, "age_days": None, "rows": [],
           "note": ""}
    if not p.exists():
        out["note"] = f"нет файла {rel(p)}"
        return out
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        out["note"] = f"срез повреждён ({rel(p)})"
        return out
    if not isinstance(d, dict):
        out["note"] = f"срез повреждён ({rel(p)})"
        return out
    out["date"] = d.get("date") or ""
    try:
        out["day_ts"] = time.mktime(time.strptime(out["date"], "%Y-%m-%d"))
    except (TypeError, ValueError):
        out["note"] = f"срез без даты ({rel(p)})"
        return out
    out["age_days"] = ((now if now is not None else time.time()) - out["day_ts"]) / 86400
    out["rows"] = d.get("rows") or []
    out["ok"] = True
    return out


def fresh_slice(cfg, now: float | None = None) -> dict:
    """РЕЗОЛВЕР — единственный путь к срезу: из [track_q.live_source, track_q.source] берёт
    свежий по полю date (при равных — live). -> read_slice + {"src", "tried"}; читаемого
    нет — ok False, note с причинами по каждому файлу."""
    tried = []
    for key, p in slice_paths(cfg):
        s = read_slice(p, now)
        s["src"] = key
        tried.append(s)
    good = [s for s in tried if s["ok"]]
    if not good:
        return {"ok": False, "path": None, "src": "", "date": "", "day_ts": None,
                "age_days": None, "rows": [], "tried": tried,
                "note": "; ".join(s["note"] for s in tried) or "срез не задан (track_q)"}
    best = max(good, key=lambda s: s["day_ts"])
    return {**best, "tried": tried}


def passed(rows: list[dict]) -> dict[str, dict]:
    """{SYM: строка} прошедших гейт (пустой fails). Из дублей тикера срез уже взял старший
    по капе."""
    return {r["sym"].upper(): r for r in rows if r.get("sym") and not r.get("fails")}


def load_quality(cfg, now: float | None = None) -> dict:
    """Срез для воронки и A/B стопа: свежий (fresh_slice), не старше track_q.max_age_days.
    -> {"ok", "date", "age_days", "by_sym": {SYM: row}, "note", "src", "path"}. Нет среза /
    протух / битый — пустой by_sym (трек Q просто не наполняется)."""
    s = fresh_slice(cfg, now)
    max_age = cfg.get("track_q.max_age_days", 30)
    out = {"ok": False, "date": s["date"], "age_days": s["age_days"], "by_sym": {},
           "note": s["note"], "src": s["src"], "path": s["path"]}
    if not s["ok"]:
        out["note"] = f"{s['note']} — обнови: run.py quality --refresh-if-due"
        return out
    if max_age is not None and s["age_days"] > max_age:
        out["note"] = (f"срез от {s['date']} старше {max_age:g} дн — обнови: "
                       f"run.py quality --refresh-if-due")
        return out
    out["by_sym"] = passed(s["rows"])
    out["ok"] = True
    return out


def write_slice(path, data: dict) -> Path:
    """Атомарная запись среза: временный файл рядом → fsync → os.replace. Сбой посреди
    записи не оставляет битый JSON — читатели видят прежний срез целиком."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return p


# ---------------------------------------------------------------- автообновление

def refresh_due(s: dict, cfg) -> bool:
    """Пора ли обновлять: свежего среза нет или ему ≥ track_q.refresh_after_days."""
    return not s["ok"] or s["age_days"] >= cfg.get("track_q.refresh_after_days", 25)


def refresh(cfg, *, now: float | None = None, runner=subprocess.run,
            timeout: float = REFRESH_TIMEOUT_SEC) -> tuple[bool, str]:
    """backtest/quality_screen.py --out <track_q.live_source> подпроцессом (вывод идёт в лог
    прогона). Успех — код 0 и live-срез сегодняшний. runner подменяется в selftest."""
    out = live_path(cfg)
    if out is None:
        return False, "track_q.live_source не задан — обновлять некуда"
    cmd = [sys.executable, "-u", str(SCREEN), "--out", str(out)]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    sys.stdout.flush()
    try:
        r = runner(cmd, cwd=str(PROJ), env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"quality_screen.py не уложился в {timeout / 60:g} мин — прерван"
    except OSError as e:
        return False, f"quality_screen.py не запустился: {e}"
    if r.returncode != 0:
        return False, (f"quality_screen.py завершился с кодом {r.returncode} — "
                       f"срез не обновлён, работает прежний")
    s = read_slice(out, now)
    if not s["ok"] or s["age_days"] >= 1:
        return False, f"{rel(out)} не обновился ({s['note'] or 'срез от ' + s['date']})"
    return True, f"срез обновлён: {rel(out)} от {s['date']}, прошли гейт {len(passed(s['rows']))}"


def refresh_if_due(cfg, *, now: float | None = None, runner=subprocess.run,
                   timeout: float = REFRESH_TIMEOUT_SEC) -> tuple[int, str]:
    """`run.py quality --refresh-if-due`: (код выхода, строка лога). Не пора — 0 без сети."""
    s = fresh_slice(cfg, now)
    if not refresh_due(s, cfg):
        n = cfg.get("track_q.refresh_after_days", 25)
        return 0, (f"обновлять рано: срезу {s['age_days']:.1f} дн. < {n:g} — "
                   f"автообновление с {day_after(s['date'], n)}")
    ok, msg = refresh(cfg, now=now, runner=runner, timeout=timeout)
    return (0 if ok else 1), msg


# ---------------------------------------------------------------- сопоставление монет

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
