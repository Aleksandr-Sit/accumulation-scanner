#!/usr/bin/env bash
# Ежедневный прогон сканера на VPS: quality (срез трека Q, если пора) -> scan -> sync (позиции
# из Bybit) -> watch -> report (раз в неделю) -> backup -> brief, Telegram, лог в logs/.
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

# Срез трека Q: старше refresh_after_days — пересчёт (~6 мин сети) в data/. Сбой не блокирует
# scan: трек живёт на прежнем срезе до max_age_days, сводка дня предупредит.
run_step quality quality --refresh-if-due; quality_rc=$rc
# watch идёт и при сбое scan: открытые позиции надо проверять независимо от воронки.
run_step scan scan "${notify[@]}"; scan_rc=$rc
# Реальные позиции из исполнений Bybit (ключ Read-Only в .env; нет ключа — пропуск, код 0) —
# до watch, чтобы сегодняшние покупки сразу получили цену и сигналы. Сбой не блокирует watch.
run_step sync sync; sync_rc=$rc
run_step watch watch "${notify[@]}"; watch_rc=$rc
# Недельная сводка: report сам решает, пора ли (--if-due, первый прогон недели).
run_step report report --if-due "${notify[@]}"; report_rc=$rc
# Бэкап базы — после всех записей дня; раз в неделю копия уходит в Telegram (без звука).
backup_dir=()
[ -d /opt/backups ] && backup_dir=(--dir /opt/backups/scanner)
run_step backup backup "${backup_dir[@]}" --send-weekly "${notify[@]}"; backup_rc=$rc
# Сводка дня — тихо и последней; пришла сводка = прогон дошёл до конца.
run_step brief brief --scan-exit "$scan_rc" --watch-exit "$watch_rc" --backup-exit "$backup_rc" \
    --sync-exit "$sync_rc" "${notify[@]}"; brief_rc=$rc

# Логи старше 30 дней не нужны.
find logs -name 'daily_*.log' -mtime +30 -delete

[ "$quality_rc" -eq 0 ] && [ "$scan_rc" -eq 0 ] && [ "$sync_rc" -eq 0 ] && [ "$watch_rc" -eq 0 ] \
    && [ "$report_rc" -eq 0 ] && [ "$backup_rc" -eq 0 ] && [ "$brief_rc" -eq 0 ]
