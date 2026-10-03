#!/usr/bin/env bash
# Ежедневный прогон сканера на VPS: scan -> watch -> report (раз в неделю) -> brief, Telegram, лог в logs/.
# Linux-двойник daily_run.ps1. Запускается таймером systemd (scripts/systemd/accumulation-scanner.timer).
# Ручной запуск: bash scripts/daily_run.sh [--no-notify]
set -u
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

export PYTHONIOENCODING=utf-8 PYTHONUTF8=1
notify=(--notify)
[ "${1:-}" = "--no-notify" ] && notify=()

mkdir -p logs
log="logs/daily_$(date +%F_%H%M).log"

run_step() {  # имя, аргументы run.py; код выхода — в $rc
    local name="$1"; shift
    echo "=== $name $(date '+%F %T') ===" >> "$log"
    python3 -u run.py "$@" >> "$log" 2>&1
    rc=$?
    printf '=== %s exit %s ===\n\n' "$name" "$rc" >> "$log"
}

# watch идёт и при сбое scan: открытые позиции надо проверять независимо от воронки.
run_step scan scan "${notify[@]}"; scan_rc=$rc
run_step watch watch "${notify[@]}"; watch_rc=$rc
# Недельная сводка: report сам решает, пора ли (--if-due, первый прогон недели).
run_step report report --if-due "${notify[@]}"; report_rc=$rc
# Сводка дня — тихо и последней; пришла сводка = прогон дошёл до конца.
run_step brief brief --scan-exit "$scan_rc" --watch-exit "$watch_rc" "${notify[@]}"; brief_rc=$rc

# Логи старше 30 дней не нужны.
find logs -name 'daily_*.log' -mtime +30 -delete

[ "$scan_rc" -eq 0 ] && [ "$watch_rc" -eq 0 ] && [ "$report_rc" -eq 0 ] && [ "$brief_rc" -eq 0 ]
