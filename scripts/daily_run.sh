#!/usr/bin/env bash
# Ежедневный прогон сканера на VPS: quality (срез трека Q, если пора) -> scan -> sync (позиции
# из Bybit) -> watch -> execute (пробный исполнитель) -> demo (демо-счёт Bybit) -> report (раз в неделю) -> backup -> brief, Telegram, лог в logs/.
# Linux-двойник daily_run.ps1. Запускается таймером systemd (scripts/systemd/accumulation-scanner.timer).
# Ручной запуск: bash scripts/daily_run.sh [--no-notify]
#
# Защита прогона:
# - один прогон за раз: flock на data/daily_run.lock (второй параллельный задвоил бы карточки и
#   paper) — занят, значит идёт другой прогон или деплой: пропуск с кодом 0;
# - у каждого шага свой таймаут (timeout: TERM, через 60 с KILL; код 124/137): зависший шаг
#   убит, следующие всё равно идут — сводка дня покажет «⏱ таймаут»;
# - состояние в data/daily_run.state (scanner/runguard.py): прогон убит посреди шагов (таймаут
#   юнита, остановка, OOM) — «⚠ прогон убит» в Telegram: trap здесь, OnFailure= юнита
#   (scanner-alert.service), а после перезагрузки — строка в сводке следующего прогона.
set -u
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

export PYTHONIOENCODING=utf-8 PYTHONUTF8=1
notify=(--notify)
[ "${1:-}" = "--no-notify" ] && notify=()

mkdir -p logs data
exec 9>data/daily_run.lock
if ! flock -n 9; then
    echo "daily_run: уже идёт другой прогон или деплой (data/daily_run.lock) — пропуск"
    exit 0
fi

log="logs/daily_$(date +%F_%H%M).log"
state=data/daily_run.state
[ -f "$state" ] && mv -f "$state" data/daily_run.prev
printf 'start %s %s\n' "$(date +%s)" "$log" > "$state"

on_signal() {  # TERM/INT/HUP: таймаут юнита, systemctl stop, перезагрузка, Ctrl-C
    trap - TERM INT HUP
    printf 'signal %s %s\n' "$1" "$(date +%s)" >> "$state"
    echo "=== прогон убит сигналом $1 $(date '+%F %T') ===" >> "$log"
    timeout 60 python3 -u run.py killed --why "сигнал $1" >> "$log" 2>&1
    exit 143
}
trap 'on_signal TERM' TERM
trap 'on_signal INT' INT
trap 'on_signal HUP' HUP

run_step() {  # имя, таймаут в секундах, аргументы run.py; код выхода — в $rc
    local name="$1" limit="$2"; shift 2
    printf 'step %s %s\n' "$name" "$(date +%s)" >> "$state"
    echo "=== $name $(date '+%F %T') ===" >> "$log"
    timeout --kill-after=60 "$limit" python3 -u run.py "$@" >> "$log" 2>&1
    rc=$?
    case "$rc" in
        124|137) echo "=== $name: убит — таймаут $limit с (137 — или нехватка памяти) ===" >> "$log" ;;
    esac
    printf '=== %s exit %s ===\n\n' "$name" "$rc" >> "$log"
}

# Таймауты — с запасом в 3–5 раз к обычной длительности (скан ~20 мин, остальное — секунды,
# пересчёт трека Q ~6 мин, отправка копии базы — до пары минут). Сумма 3 ч 30 мин < 4 ч
# TimeoutStartSec юнита: при зависании убивает таймаут шага, а не systemd весь прогон.

# Срез трека Q: старше refresh_after_days — пересчёт (~6 мин сети) в data/. Сбой не блокирует
# scan: трек живёт на прежнем срезе до max_age_days, сводка дня предупредит.
run_step quality 1800 quality --refresh-if-due; quality_rc=$rc
# watch идёт и при сбое scan: открытые позиции надо проверять независимо от воронки.
run_step scan 5400 scan "${notify[@]}"; scan_rc=$rc
# Реальные позиции из исполнений Bybit (ключ Read-Only в .env; нет ключа — пропуск, код 0) —
# до watch, чтобы сегодняшние покупки сразу получили цену и сигналы. Сбой не блокирует watch.
# Перед синхронизацией — самопроверка ключа (права, IP, срок): опасность — сообщение со звуком.
run_step sync 600 sync "${notify[@]}"; sync_rc=$rc
run_step watch 900 watch "${notify[@]}"; watch_rc=$rc
# Пробный исполнитель: лестницы по сегодняшним карточкам в книги R/H, без ордеров. Сбой не
# блокирует остальные шаги — код уходит в сводку дня. Стоп-кран (data/HALT) — шаг ничего не делает.
run_step execute 900 execute --dry-run; exec_rc=$rc
# Демо-счёт Bybit (блок F): те же ордера на учебные деньги (api-demo.bybit.com) — после execute,
# решения берёт у него. Нет ключа демо — пропуск, код 0. Стоп-кран — шаг ничего не делает.
run_step demo 900 demo; demo_rc=$rc
# Недельная сводка: report сам решает, пора ли (--if-due, первый прогон недели).
run_step report 600 report --if-due "${notify[@]}"; report_rc=$rc
# Бэкап базы — после всех записей дня; раз в неделю копия уходит в Telegram (без звука).
backup_dir=()
[ -d /opt/backups ] && backup_dir=(--dir /opt/backups/scanner)
run_step backup 1200 backup "${backup_dir[@]}" --send-weekly "${notify[@]}"; backup_rc=$rc
# Сводка дня — тихо и последней; пришла сводка = прогон дошёл до конца.
run_step brief 300 brief --scan-exit "$scan_rc" --watch-exit "$watch_rc" --backup-exit "$backup_rc" \
    --sync-exit "$sync_rc" --exec-exit "$exec_rc" --demo-exit "$demo_rc" \
    --report-exit "$report_rc" \
    --quality-exit "$quality_rc" "${notify[@]}"; brief_rc=$rc

# Логи старше 30 дней не нужны.
find logs -name 'daily_*.log' -mtime +30 -delete

printf 'end %s %s\n' "$(date +%s)" "$brief_rc" >> "$state"
[ "$quality_rc" -eq 0 ] && [ "$scan_rc" -eq 0 ] && [ "$sync_rc" -eq 0 ] && [ "$watch_rc" -eq 0 ] \
    && [ "$exec_rc" -eq 0 ] && [ "$demo_rc" -eq 0 ] && [ "$report_rc" -eq 0 ] && [ "$backup_rc" -eq 0 ] \
    && [ "$brief_rc" -eq 0 ]
