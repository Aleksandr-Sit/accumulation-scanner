"""Бэкап scanner.db — единственного носителя форвард-теста (задним числом его не пересчитать).

`run.py backup`: консистентная копия через sqlite3 backup API (не копирование файла — оно
может застать запись посреди транзакции) во временный файл → PRAGMA integrity_check == ok →
gzip → <dir>/scanner-YYYYMMDD-HHMM.db.gz → ротация (хранятся keep самых новых). Источник
открывается только на чтение (mode=ro). Любой сбой — исключение: код выхода ≠ 0, сводка дня
покажет «⚠ бэкап не сделан». Битая база в копию не попадает и ротацию не запускает — старые
целые копии остаются.

Копия вне сервера: раз в неделю свежий .gz уходит в Telegram документом без звука
(--send-weekly --notify). День и отметка — как у недельной сводки: weekly_report_weekday,
position_events с position_id=0, type backup_sent (пишется только после доставки).
Восстановление: gunzip -c scanner-YYYYMMDD-HHMM.db.gz > scanner.db.
"""
from __future__ import annotations

import gzip
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

from .notify.telegram import fmt_size

PROJ = Path(__file__).resolve().parent.parent
DEFAULT_DIR = PROJ / "backups"
NAME_RE = re.compile(r"^scanner-\d{8}-\d{4}\.db\.gz$")
TG_MAX_BYTES = 45 * 1024 * 1024     # лимит Bot API на файл — 50 МБ; берём с запасом
FLAG = "backup_sent"                 # отметка недельной отправки (position_events, id=0)
EXIT_SEND_FAILED = 3                 # копия сделана, но в Telegram не ушла (сводка различает;
                                     # 1 — сбой/исключение, 2 — ошибка аргументов argparse)


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


def make_backup(db_path, out_dir=DEFAULT_DIR, keep: int = 14, now: float | None = None) -> dict:
    """-> {"path", "size", "db_size", "ts", "kept", "keep", "removed"}. Сбой — исключение
    (BackupError / sqlite3.Error / OSError); временные файлы убираются в любом случае."""
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
        finally:
            chk.close()
        if res != ["ok"]:
            raise BackupError("integrity_check: " + "; ".join(map(str, res[:3])))
        db_size = raw.stat().st_size
        with open(raw, "rb") as fi, open(part, "wb") as fo:
            # имя внутри .gz — без «.gz»: gunzip -N не затрёт рабочую scanner.db
            with gzip.GzipFile(filename=name[:-3], mode="wb", fileobj=fo) as gz:
                shutil.copyfileobj(fi, gz, 1024 * 1024)
            fo.flush()
            os.fsync(fo.fileno())
        os.replace(part, final)
    finally:
        for p in tmp_files:
            p.unlink(missing_ok=True)
    removed = []
    for old in list_backups(out)[keep:]:
        old.unlink()
        removed.append(old.name)
    return {"path": final, "size": final.stat().st_size, "db_size": db_size, "ts": ts,
            "kept": len(list_backups(out)), "keep": keep, "removed": removed}


def ok_line(info: dict) -> str:
    """«backup ok: файл · размер · сколько хранится» — строка лога прогона."""
    line = (f"backup ok: {info['path']} · {fmt_size(info['size'])} "
            f"(база {fmt_size(info['db_size'])}) · хранится {info['kept']} из {info['keep']}")
    if info["removed"]:
        line += f" · удалено старых: {len(info['removed'])}"
    return line


def send_weekly(cfg, info: dict, *, notify: bool, test: bool = False, now: float | None = None,
                max_bytes: int = TG_MAX_BYTES, send_document=None,
                send_message=None) -> tuple[int, str]:
    """Недельная копия в Telegram документом без звука -> (код выхода, строка лога).
    Пора — как недельной сводке (telegram.weekly_due от отметки backup_sent); отметка — только
    после доставки, иначе повтор в следующий прогон. test: подпись «🧪 ТЕСТ», отметка не
    проверяется и не пишется. Больше max_bytes — файл не шлём, а предупреждаем текстом.
    send_document/send_message подменяются в selftest (без сети)."""
    from .notify import telegram
    from .positions import PositionStore
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
        if info["size"] > max_bytes:
            what = f"предупреждение: {path.name} {fmt_size(info['size'])} больше лимита"
            ok = (send_message or telegram.send_message)(
                token, chat, telegram.format_backup_too_big(info, max_bytes, test=test),
                silent=True)
        else:
            what = f"{path.name} документом, {fmt_size(info['size'])}"
            ok = (send_document or telegram.send_document)(
                token, chat, path.read_bytes(), path.name,
                telegram.format_backup_caption(info, test=test), silent=True)
        if not ok:
            return EXIT_SEND_FAILED, f"в Telegram не ушло ({what}) — повтор в следующий прогон"
        if not test:
            ps.set_system_flag(FLAG, path.name)
        return 0, f"в Telegram ушло без звука: {what}"
    finally:
        ps.close_db()
