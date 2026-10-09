"""Сравнение двух *_results.json одного исследования: что изменилось после перезапуска.

Числа сравниваются по путям в JSON (variants.разом | прод….med_rob и т.п.): сколько ключей
совпало, сколько ушло и появилось, и самые большие сдвиги — по абсолютной разнице, со
знаком и относительным изменением. Для квартального перезапуска (scripts/quarterly_backtests.sh):
прошлый итог из репозитория против нового из рабочей копии.

Запуск:  python backtest/compare_results.py old.json new.json [--top 15] [--min-rel 0.05]
Код 0 — сравнение выполнено (даже если всё изменилось), 2 — файл не прочитан.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def flatten(x, path: str = "", out: dict | None = None) -> dict[str, float]:
    """Числовые листья JSON -> {путь: число}; bool и строки не сравниваются."""
    out = {} if out is None else out
    if isinstance(x, dict):
        for k, v in x.items():
            flatten(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(x, list):
        for i, v in enumerate(x):
            flatten(v, f"{path}[{i}]", out)
    elif isinstance(x, (int, float)) and not isinstance(x, bool):
        out[path] = float(x)
    return out


def compare(old: dict, new: dict, min_rel: float = 0.05) -> dict:
    a, b = flatten(old), flatten(new)
    common = sorted(set(a) & set(b))
    moved = []
    for k in common:
        d = b[k] - a[k]
        if d == 0:
            continue
        rel = d / abs(a[k]) if a[k] else float("inf")
        if abs(rel) >= min_rel:
            moved.append({"key": k, "old": a[k], "new": b[k], "diff": d, "rel": rel})
    moved.sort(key=lambda m: -abs(m["diff"]))
    return {"common": len(common), "same": sum(1 for k in common if a[k] == b[k]),
            "moved": moved, "gone": sorted(set(a) - set(b)), "added": sorted(set(b) - set(a))}


def fmt(x: float) -> str:
    return f"{x:.4g}" if abs(x) < 10 else f"{x:.1f}"


def report(name: str, c: dict, top: int) -> str:
    lines = [f"{name}: ключей {c['common']} общих, без изменений {c['same']}, сдвинулось ≥ порога "
             f"{len(c['moved'])}, ушло {len(c['gone'])}, появилось {len(c['added'])}"]
    for m in c["moved"][:top]:
        rel = "новое ≠ 0" if m["rel"] == float("inf") else f"{m['rel'] * 100:+.0f}%"
        lines.append(f"  {m['key'][:90]:90} {fmt(m['old']):>10} → {fmt(m['new']):>10} ({rel})")
    for title, keys in (("ушло", c["gone"]), ("появилось", c["added"])):
        if keys:
            roots = sorted({k.split(".")[0].split("[")[0] for k in keys})
            lines.append(f"  {title}: {len(keys)} ключей в разделах {', '.join(roots[:10])}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Сравнение двух *_results.json")
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--min-rel", type=float, default=0.05,
                    help="показывать сдвиги не меньше этой доли от старого значения")
    a = ap.parse_args()
    try:
        old = json.loads(Path(a.old).read_text(encoding="utf-8"))
        new = json.loads(Path(a.new).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"{Path(a.new).name}: не прочитан ({e})")
        return 2
    print(report(Path(a.new).name, compare(old, new, a.min_rel), a.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
