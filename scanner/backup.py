"""Бэкап scanner.db — единственного носителя форвард-теста (задним числом его не пересчитать).

`run.py backup`: консистентная копия через sqlite3 backup API (не копирование файла — оно
может застать запись посреди транзакции) во временный файл → PRAGMA integrity_check == ok →
сверка строк с эталоном → gzip → <dir>/scanner-YYYYMMDD-HHMM.db.gz → ротация. Источник
открывается только на чтение (mode=ro). Любой сбой — исключение: код выхода ≠ 0, сводка дня
покажет «⚠ бэкап не сделан». Битая база в копию не попадает и ротацию не запускает — старые
целые копии остаются.

Пустая база не вытесняет хорошие копии. В каждой копии считаются строки таблиц-журналов, из
которых код не удаляет (GUARD_TABLES), — в манифест <dir>/backups.json. Эталон — самая новая
неподозрительная копия из манифеста (записей нет — самая новая читаемая копия в каталоге).
Строк меньше, чем в эталоне, или таблицы нет — копия подозрительная: пишется, но ничего не
удаляется и в Telegram не уходит, код выхода 4 (EXIT_SUSPECT). База уменьшена намеренно —
`run.py backup --accept-shrink`: копия становится эталоном, ротация идёт дальше.

Ротация ярусами (config backup): keep_daily самых новых + самая новая копия каждой из последних
keep_weekly ISO-недель и keep_monthly календарных месяцев, где копии есть. Чужие файлы в
каталоге не трогаются.

Копия вне сервера: раз в неделю в Telegram документом без звука (--send-weekly --notify) уходит
тонкая копия scanner-YYYYMMDD-HHMM-tg.db.gz — candidates и candidate_metrics только за последние
telegram_candidates_days дней (полная копия растёт ~0.5 МБ/день и упёрлась бы в лимит Bot API).
День и отметка — как у недельной сводки: weekly_report_weekday, position_events с
position_id=0, type backup_sent (пишется только после доставки).

Восстановление — в каталоге проекта, таймер остановлен. Журнал прерванной записи удалить ДО
распаковки: иначе SQLite при первом открытии «откатит» его поверх восстановленной копии и
испортит её.
    systemctl stop accumulation-scanner.timer
    rm -f scanner.db-journal scanner.db-wal scanner.db-shm
    gunzip -c scanner-YYYYMMDD-HHMM.db.gz > scanner.db
    systemctl start accumulation-scanner.timer
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import sqlite3
import time
import zlib
from datetime import datetime
from pathlib import Path

from .notify.telegram import fmt_size

PROJ = Path(__file__).resolve().parent.parent
DEFAULT_DIR = PROJ / "backups"
NAME_RE = re.compile(r"^scanner-\d{8}-\d{4}\.db\.gz$")
MANIFEST = "backups.json"            # строки таблиц-журналов по копиям, пометка «подозрительная»
TG_MAX_BYTES = 45 * 1024 * 1024     # лимит Bot API на файл — 50 МБ; берём с запасом
FLAG = "backup_sent"                 # отметка недельной отправки (position_events, id=0)
EXIT_SEND_FAILED = 3                 # копия сделана, но в Telegram не ушла (сводка различает;
                                     # 1 — сбой/исключение, 2 — ошибка аргументов argparse)
EXIT_SUSPECT = 4                     # копия сделана, но строк меньше, чем в эталоне: старые
                                     # копии не удаляются, в Telegram она не уходит
# Таблицы-журналы: код из них не удаляет (единственный DELETE — position_snapshots), строк
# меньше бывает только у пустой или подменённой базы. candidates и candidate_metrics (объём
# базы) не сверяются: их прореживание не должно замораживать ротацию.
GUARD_TABLES = ("runs", "alert_log", "positions", "position_events", "market_daily",
                "dry_positions", "dry_orders", "exchange_fills")


class BackupError(RuntimeError):
    pass


def list_backups(out_dir) -> list[Path]:
    """Бэкапы в каталоге, новые первыми (по имени: в нём дата и время). Ротация видит только
    scanner-YYYYMMDD-HHMM.db.gz — чужие файлы в каталоге не трогаются."""
    d = Path(out_dir)
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir() if p.is_file() and NAME_RE.match(p.name)),
                  key=lambda p: p.name, reverse=True)


def _name_ts(name: str) -> float | None:
    """Время копии из имени (местное, до минуты); в имени не дата — None (файл не удаляется)."""
    try:
        return time.mktime(time.strptime(name[8:21], "%Y%m%d-%H%M"))
    except (ValueError, OverflowError):
        return None


def _gzip(src: Path, dst: Path, inner: str) -> None:
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        # имя внутри .gz — без «.gz»: gunzip -N не затрёт рабочую scanner.db
        with gzip.GzipFile(filename=inner, mode="wb", fileobj=fo) as gz:
            shutil.copyfileobj(fi, gz, 1024 * 1024)
        fo.flush()
        os.fsync(fo.fileno())


def _gunzip(src: Path, dst: Path) -> None:
    with gzip.open(src, "rb") as fi, open(dst, "wb") as fo:
        shutil.copyfileobj(fi, fo, 1024 * 1024)


# ---------------------------------------------------------------- сверка с эталоном

def count_rows(con: sqlite3.Connection) -> dict[str, int]:
    """Строки таблиц-журналов (GUARD_TABLES); таблицы, которой нет в базе, нет и в ответе."""
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in GUARD_TABLES if t in have}


def shrink(ref: dict, rows: dict) -> str:
    """Что уменьшилось против эталона: «runs 21 → 0, dry_orders 4 → нет таблицы»; пусто —
    ничего. Сверяются только GUARD_TABLES, которые есть в эталоне."""
    out = []
    for t in GUARD_TABLES:
        if t not in ref:
            continue
        if t not in rows:
            out.append(f"{t} {ref[t]} → нет таблицы")
        elif rows[t] < ref[t]:
            out.append(f"{t} {ref[t]} → {rows[t]}")
    return ", ".join(out)


def load_manifest(out_dir) -> dict:
    """Манифест копий {имя: {"ts", "db_size", "rows", "suspect", "why"}}. Нет файла, битый JSON,
    чужие записи — пусто/пропуск: бэкап не ломается, эталон найдётся по самим копиям."""
    try:
        data = json.loads((Path(out_dir) / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: e for k, e in data.items()
            if NAME_RE.match(k) and isinstance(e, dict) and isinstance(e.get("rows"), dict)
            and all(type(n) is int for n in e["rows"].values())}


def save_manifest(out_dir, manifest: dict) -> None:
    """Атомарно: временный файл рядом → fsync → os.replace (сбой не оставит битый JSON)."""
    p = Path(out_dir) / MANIFEST
    tmp = p.with_name(f".{MANIFEST}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def _gz_rows(path: Path) -> tuple[dict, int] | None:
    """(строки таблиц-журналов, размер базы) копии .gz — распаковкой во временный файл рядом;
    не распаковывается или не база — None."""
    tmp = path.with_name(f".{path.name[:-3]}.ref.tmp")
    try:
        _gunzip(path, tmp)
        con = sqlite3.connect(f"{tmp.resolve().as_uri()}?mode=ro", uri=True)
        try:
            return count_rows(con), tmp.stat().st_size
        finally:
            con.close()
    except (OSError, EOFError, zlib.error, sqlite3.Error):
        return None
    finally:
        tmp.unlink(missing_ok=True)


def reference(out_dir, manifest: dict) -> str | None:
    """Эталон для сверки: самая новая НЕподозрительная копия из манифеста, чей файл на месте.
    Таких нет (первый прогон после выкатки, манифест потерян) — самая новая читаемая копия без
    записи: строки считаются по распакованной копии, запись добавляется в manifest.
    Подозрительная копия эталоном не становится."""
    files = list_backups(out_dir)
    for p in files:
        e = manifest.get(p.name)
        if e and not e.get("suspect"):
            return p.name
    for p in files:
        if p.name in manifest:
            continue
        got = _gz_rows(p)
        if got is not None:
            manifest[p.name] = {"ts": _name_ts(p.name) or p.stat().st_mtime, "db_size": got[1],
                                "rows": got[0], "suspect": False, "why": "", "bootstrap": True}
            return p.name
    return None


# ---------------------------------------------------------------- ротация

def plan_retention(entries, daily: int = 14, weekly: int = 8, monthly: int = 6,
                   now: float | None = None) -> set[str]:
    """Какие копии оставить (entries — [(имя, ts)]): `daily` самых новых + самая новая копия
    каждой из последних `weekly` ISO-недель, где копии есть, + самая новая каждого из последних
    `monthly` календарных месяцев (местное время). Пропуск (сервер был выключен) ярус не
    съедает: берутся недели и месяцы, где копии есть. Копии новее now (часы были сбиты)
    остаются и мест в ярусах не занимают."""
    now = time.time() if now is None else now
    keep = {n for n, t in entries if t > now}
    past = sorted(((t, n) for n, t in entries if t <= now), reverse=True)
    keep.update(n for _, n in past[:daily])
    for size, period in ((weekly, lambda d: d.isocalendar()[:2]),
                         (monthly, lambda d: (d.year, d.month))):
        newest: dict = {}
        for t, n in past:                # новые первыми: первая копия периода — его новейшая
            k = period(datetime.fromtimestamp(t))
            if k not in newest:
                if len(newest) >= size:
                    break
                newest[k] = n
        keep.update(newest.values())
    return keep


def make_backup(db_path, out_dir=DEFAULT_DIR, keep: int = 14, now: float | None = None, *,
                keep_weekly: int = 8, keep_monthly: int = 6,
                accept_shrink: bool = False) -> dict:
    """-> {"path", "size", "db_size", "ts", "rows", "reference", "suspect", "accepted", "kept",
    "keep", "keep_weekly", "keep_monthly", "removed"}. suspect — что уменьшилось против эталона
    (None — ничего): такая копия пишется, но ничего не удаляется. accept_shrink — база уменьшена
    намеренно: копия не подозрительная и становится эталоном (accepted — что уменьшилось).
    Сбой — исключение (BackupError / sqlite3.Error / OSError); временные файлы убираются
    в любом случае."""
    if keep < 1:
        raise BackupError(f"keep={keep}: хранить нужно хотя бы одну копию")
    src_path = Path(db_path)
    if not src_path.is_file():
        raise BackupError(f"нет базы {src_path}")
    ts = now if now is not None else time.time()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    name = time.strftime("scanner-%Y%m%d-%H%M.db.gz", time.localtime(ts))
    final = out / name
    raw = out / f".{name[:-3]}.tmp"          # несжатая копия
    part = out / f".{name}.tmp"              # .gz до переименования
    tmp_files = (raw, raw.with_name(raw.name + "-journal"), part)
    for p in tmp_files:                      # хвосты прогона, убитого посреди записи
        p.unlink(missing_ok=True)
    manifest = load_manifest(out)
    try:
        src = sqlite3.connect(f"{src_path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            dst = sqlite3.connect(raw)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        chk = sqlite3.connect(raw)
        try:
            res = [r[0] for r in chk.execute("PRAGMA integrity_check")]
            rows = count_rows(chk) if res == ["ok"] else {}
        finally:
            chk.close()
        if res != ["ok"]:
            raise BackupError("integrity_check: " + "; ".join(map(str, res[:3])))
        ref = reference(out, manifest)
        why = shrink(manifest[ref]["rows"], rows) if ref else ""
        suspect = bool(why) and not accept_shrink
        if suspect and final.exists() and not manifest.get(name, {}).get("suspect"):
            raise BackupError(f"{name} уже есть, а новая копия подозрительная ({why}) — "
                              f"не затираю")
        db_size = raw.stat().st_size
        _gzip(raw, part, name[:-3])
        os.replace(part, final)
    finally:
        for p in tmp_files:
            p.unlink(missing_ok=True)
    manifest[name] = {"ts": ts, "db_size": db_size, "rows": rows, "suspect": suspect,
                      "why": why if suspect else (f"принято --accept-shrink: {why}" if why else "")}
    removed = []
    if not suspect:                          # подозрительная копия — ротация заморожена
        dated = [(p.name, t) for p in list_backups(out) if (t := _name_ts(p.name)) is not None]
        keep_set = plan_retention(dated, keep, keep_weekly, keep_monthly, now=ts)
        for n, _ in dated:
            if n not in keep_set:
                (out / n).unlink(missing_ok=True)
                removed.append(n)
    left = {p.name for p in list_backups(out)}
    save_manifest(out, {n: e for n, e in manifest.items() if n in left})
    return {"path": final, "size": final.stat().st_size, "db_size": db_size, "ts": ts,
            "rows": rows, "reference": ref, "suspect": why if suspect else None,
            "accepted": why if why and not suspect else None, "kept": len(left), "keep": keep,
            "keep_weekly": keep_weekly, "keep_monthly": keep_monthly, "removed": removed}


def ok_line(info: dict) -> str:
    """«backup ok: файл · размер · сколько хранится» — строка лога прогона."""
    line = (f"backup ok: {info['path']} · {fmt_size(info['size'])} "
            f"(база {fmt_size(info['db_size'])}) · хранится {info['kept']} (последние "
            f"{info['keep']} + недельные {info['keep_weekly']} + месячные {info['keep_monthly']})")
    if info["removed"]:
        line += f" · удалено старых: {len(info['removed'])}"
    if info.get("accepted"):
        line += f" · уменьшение принято (--accept-shrink): {info['accepted']}"
    return line


def suspect_line(info: dict) -> str:
    """Строка лога подозрительной копии: что уменьшилось, что делать."""
    return (f"backup SUSPECT: {info['suspect']} (эталон {info['reference']}) — копия "
            f"{info['path']} записана, старые копии не удаляю, в Telegram не отправляю; если "
            f"база уменьшена намеренно — run.py backup --accept-shrink")


# ---------------------------------------------------------------- копия вне сервера

def make_thin(full, days: float, now: float | None = None) -> dict:
    """Тонкая копия для Telegram из полной .gz: candidates только прогонов (runs.ts) за
    последние days дней, candidate_metrics — тоже по ts; VACUUM, integrity_check == ok, gzip.
    -> {"path" — временный .gz рядом с полной (удаляет вызывающий), "name" —
    scanner-YYYYMMDD-HHMM-tg.db.gz, "size", "db_size", "dropped" — убрано кандидатов}.
    Временные имена начинаются с точки и под NAME_RE не попадают — ротация их не видит."""
    full = Path(full)
    cutoff = (time.time() if now is None else now) - days * 86400
    name = full.name[:-len(".db.gz")] + "-tg.db.gz"
    raw = full.with_name(f".{name[:-3]}.tmp")
    part = full.with_name(f".{name}.tmp")
    tails = (raw, raw.with_name(raw.name + "-journal"))
    for p in (*tails, part):
        p.unlink(missing_ok=True)
    try:
        _gunzip(full, raw)
        con = sqlite3.connect(raw)
        try:
            have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            dropped = 0
            if {"candidates", "runs"} <= have:
                dropped = con.execute("DELETE FROM candidates WHERE run_id NOT IN "
                                      "(SELECT id FROM runs WHERE ts >= ?)", (cutoff,)).rowcount
            if "candidate_metrics" in have and "ts" in {
                    r[1] for r in con.execute("PRAGMA table_info(candidate_metrics)")}:
                con.execute("DELETE FROM candidate_metrics WHERE ts < ?", (cutoff,))
            con.commit()
            con.execute("VACUUM")
            res = [r[0] for r in con.execute("PRAGMA integrity_check")]
        finally:
            con.close()
        if res != ["ok"]:
            raise BackupError("тонкая копия: integrity_check: " + "; ".join(map(str, res[:3])))
        db_size = raw.stat().st_size
        _gzip(raw, part, name[:-3])
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        for p in tails:
            p.unlink(missing_ok=True)
    return {"path": part, "name": name, "size": part.stat().st_size, "db_size": db_size,
            "dropped": dropped}


def send_weekly(cfg, info: dict, *, notify: bool, test: bool = False, now: float | None = None,
                max_bytes: int = TG_MAX_BYTES, send_document=None,
                send_message=None) -> tuple[int, str]:
    """Недельная копия в Telegram документом без звука -> (код выхода, строка лога).
    Подозрительная копия не уходит никогда (EXIT_SUSPECT): вне сервера остаётся прежняя целая.
    Пора — как недельной сводке (telegram.weekly_due от отметки backup_sent); отметка — только
    после доставки, иначе повтор в следующий прогон. test: подпись «🧪 ТЕСТ», отметка не
    проверяется и не пишется. Уходит тонкая копия (make_thin, backup.telegram_candidates_days);
    не вышла — полная. Больше max_bytes — файл не шлём, а предупреждаем текстом.
    send_document/send_message подменяются в selftest (без сети)."""
    from .notify import telegram
    from .positions import PositionStore
    if info.get("suspect"):
        return EXIT_SUSPECT, (f"копия подозрительная ({info['suspect']}) — в Telegram не "
                              f"отправляю: там остаётся прежняя целая")
    now = now if now is not None else time.time()
    ps = PositionStore(cfg["output"]["db_path"])
    try:
        if not test:
            last = ps.last_event_ts(0, FLAG)
            if not telegram.weekly_due(last, now,
                                       cfg.get("stage6_telegram.weekly_report_weekday", 0)):
                when = time.strftime("%d.%m %H:%M", time.localtime(last))
                return 0, f"копия этой недели уже в Telegram ({when}) — пропуск"
        if not notify:
            return 0, "пора отправить недельную копию в Telegram — без --notify не отправляю"
        token = cfg.get("api_keys.telegram_token", "")
        chat = cfg.get("api_keys.telegram_chat_id", "")
        path = Path(info["path"])
        days = cfg.get("backup.telegram_candidates_days", 30)
        note = ""
        try:
            thin = make_thin(path, days, now)
        except Exception as e:  # noqa: BLE001 — копия вне сервера важнее её размера
            thin = None
            note = f"тонкая копия не вышла ({type(e).__name__}: {e}) — вместо неё полная; "
        try:
            if thin:
                doc = {**info, "name": thin["name"], "size": thin["size"],
                       "db_size": thin["db_size"], "thin_days": days}
            else:
                doc = {**info, "name": path.name, "thin_days": None}
            if doc["size"] > max_bytes:
                what = f"предупреждение: {doc['name']} {fmt_size(doc['size'])} больше лимита"
                ok = (send_message or telegram.send_message)(
                    token, chat, telegram.format_backup_too_big(doc, max_bytes, test=test),
                    silent=True)
            else:
                what = f"{doc['name']} документом, {fmt_size(doc['size'])}" + (
                    f" (тонкая: кандидаты за {days:g} дн.)" if thin else "")
                ok = (send_document or telegram.send_document)(
                    token, chat, (thin["path"] if thin else path).read_bytes(), doc["name"],
                    telegram.format_backup_caption(doc, test=test), silent=True)
        finally:
            if thin:
                thin["path"].unlink(missing_ok=True)
        if not ok:
            return EXIT_SEND_FAILED, (f"{note}в Telegram не ушло ({what}) — повтор в следующий "
                                      f"прогон")
        if not test:
            ps.set_system_flag(FLAG, doc["name"])
        return 0, f"{note}в Telegram ушло без звука: {what}"
    finally:
        ps.close_db()
