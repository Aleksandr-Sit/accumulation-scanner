"""Чек-лист решений (docs/DECISIONS.md) — напоминание в месячной сводке.

Документ пишется для человека: таблицы Markdown, в каждой есть столбцы «ID», «Что» и
«Когда». Машина читает только их: первая дата ГГГГ-ММ-ДД в «Когда» — срок пересмотра или
проверки; строка, у которой в ID стоит ✅ (или ID зачёркнут ~~…~~), закрыта. Строка без
даты («после 20 закрытых позиций») в напоминание не попадает — её смотрят при пересмотре.

Месячная сводка показывает просроченные и те, срок которых наступит в ближайший месяц:
пересмотр порогов не должен зависеть от того, вспомнит ли кто-то о нём сам.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "DECISIONS.md"
DATE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
HORIZON_DAYS = 31
MAX_LINES = 8


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse(text: str) -> list[dict]:
    """Строки таблиц с ID/Что/Когда: {id, what, when, due (timestamp полуночи или None),
    done}. Таблицы без этих столбцов пропускаются."""
    items: list[dict] = []
    cols: dict[str, int] | None = None
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            cols = None                                   # таблица кончилась
            continue
        cells = _cells(line)
        if all(set(c) <= set("-: ") for c in cells):      # разделитель |---|
            continue
        if cols is None:
            names = {c.strip("* ").lower(): i for i, c in enumerate(cells)}
            if {"id", "что", "когда"} <= set(names):
                cols = {k: names[k] for k in ("id", "что", "когда")}
            else:
                cols = {}
            continue
        if not cols or len(cells) <= max(cols.values()):
            continue
        rid, what, when = (cells[cols[k]] for k in ("id", "что", "когда"))
        m = DATE.search(when)
        due = None
        if m:
            try:
                due = datetime.strptime(m.group(1), "%Y-%m-%d").timestamp()
            except ValueError:
                due = None
        items.append({"id": rid.replace("~", "").replace("✅", "").strip(), "what": what,
                      "when": when, "due": due,
                      "done": "✅" in rid or rid.startswith("~~")})
    return items


def due_items(items: list[dict], now: float,
              horizon_days: int = HORIZON_DAYS) -> tuple[list[dict], list[dict]]:
    """(просроченные, в ближайшие horizon_days дней) среди открытых с датой, по сроку."""
    open_ = sorted((i for i in items if not i["done"] and i["due"] is not None),
                   key=lambda i: i["due"])
    late = [i for i in open_ if i["due"] < now]
    soon = [i for i in open_ if now <= i["due"] < now + horizon_days * 86400]
    return late, soon


def _plain(s: str, n: int = 90) -> str:
    """Текст ячейки без разметки Markdown, коротко."""
    s = re.sub(r"`|\*\*", "", s)                 # код и жирный; _ в именах таблиц оставить
    return s if len(s) <= n else s[:n - 1] + "…"


def reminder(now: float, path: Path | None = None) -> list[str]:
    """Строки «📋 Чек-лист решений» для месячной сводки (HTML Telegram, текст экранирован).
    Нечего напоминать — пусто; нет файла — строка-предупреждение."""
    import html
    p = path or DOC
    try:
        items = parse(p.read_text(encoding="utf-8"))
    except OSError:
        return [f"⚠ чек-лист решений не прочитан: {html.escape(p.name)}"]
    late, soon = due_items(items, now)
    if not (late or soon):
        return []
    out = ["📋 <b>Чек-лист решений</b> (docs/DECISIONS.md):"]
    for mark, group in (("⏰ просрочено", late), ("🗓 в этом месяце", soon)):
        for i in group:
            if len(out) > MAX_LINES:
                break
            day = datetime.fromtimestamp(i["due"]).strftime("%d.%m")
            out.append(f"{mark} {day} · {html.escape(i['id'])}: {html.escape(_plain(i['what']))}")
    rest = len(late) + len(soon) - (len(out) - 1)
    if rest > 0:
        out.append(f"… и ещё {rest}")
    out.append("<i>Сделано — поставь ✅ в ID строки; срок сдвигается правкой даты.</i>")
    return out
