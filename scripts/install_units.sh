#!/usr/bin/env bash
# Установка и обновление юнитов systemd сканера (от root): scripts/systemd/*.service и *.timer
# -> /etc/systemd/system, daemon-reload, таймеры прогона и команд включены. Повторный запуск
# безопасен. Drop-in песочницы (*.service.d/, docs/VPS_SECURITY.md) этот скрипт не трогает.
set -eu
src="$(cd "$(dirname "$0")" && pwd)/systemd"
[ "$(id -u)" = 0 ] || { echo "install_units: нужен root"; exit 1; }
for f in "$src"/*.service "$src"/*.timer; do
    dst="/etc/systemd/system/$(basename "$f")"
    if ! cmp -s "$f" "$dst"; then
        install -m 644 "$f" "$dst"
        echo "install_units: обновлён $dst"
    fi
done
systemctl daemon-reload
systemctl enable --now accumulation-scanner.timer scanner-control.timer
systemctl list-timers --all --no-pager | grep -E 'accumulation-scanner|scanner-control' || true
