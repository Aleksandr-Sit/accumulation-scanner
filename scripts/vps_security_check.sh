#!/usr/bin/env bash
# Проверка защиты VPS — ТОЛЬКО ЧТЕНИЕ: ничего не меняет, ничего не перезапускает.
# Запуск на сервере от root: bash /opt/scanner/scripts/vps_security_check.sh
# Что исправлять и как — docs/VPS_SECURITY.md (меняет настройки только владелец сервера).
# Итог: [OK] — в порядке, [WARN] — стоит исправить, [INFO] — к сведению. Код выхода 0 всегда.
set -u
export LC_ALL=C

warn=0
ok()   { printf '[OK]   %s\n' "$*"; }
bad()  { printf '[WARN] %s\n' "$*"; warn=$((warn + 1)); }
info() { printf '[INFO] %s\n' "$*"; }
head_() { printf '\n== %s\n' "$*"; }

scanner_dir="${SCANNER_DIR:-/opt/scanner}"
units="accumulation-scanner.service scanner-control.service"

head_ "SSH (действующие значения sshd -T)"
if command -v sshd >/dev/null 2>&1 && sshd_cfg="$(sshd -T 2>/dev/null)"; then
    val() { printf '%s\n' "$sshd_cfg" | awk -v k="$1" '$1 == k {print $2; exit}'; }
    [ "$(val passwordauthentication)" = "no" ] && ok "вход по паролю выключен" \
        || bad "вход по паролю ВКЛЮЧЁН (PasswordAuthentication $(val passwordauthentication)) — подбор паролей идёт постоянно"
    [ "$(val kbdinteractiveauthentication)" = "no" ] && ok "keyboard-interactive выключен" \
        || bad "KbdInteractiveAuthentication $(val kbdinteractiveauthentication) — второй путь входа по паролю"
    [ "$(val x11forwarding)" = "no" ] && ok "X11Forwarding выключен" \
        || bad "X11Forwarding $(val x11forwarding) — серверу без экрана не нужен"
    case "$(val permitrootlogin)" in
        no) ok "root по SSH не входит" ;;
        prohibit-password|without-password) info "root входит только по ключу (PermitRootLogin $(val permitrootlogin))" ;;
        *) bad "root может входить по паролю (PermitRootLogin $(val permitrootlogin))" ;;
    esac
    info "порт SSH: $(val port)"
else
    bad "sshd -T не выполнился — запусти скрипт от root"
fi
for f in /etc/ssh/sshd_config.d/*.conf; do
    [ -e "$f" ] || continue
    info "файл $f: $(grep -Ei '^[[:space:]]*(PasswordAuthentication|X11Forwarding|PermitRootLogin|KbdInteractiveAuthentication)' "$f" | tr '\n' ';')"
done

head_ "Ключи SSH (authorized_keys)"
for home in /root /home/*; do
    ak="$home/.ssh/authorized_keys"
    [ -f "$ak" ] || continue
    n="$(grep -cEv '^[[:space:]]*(#|$)' "$ak")"
    info "$ak: ключей $n"
    while IFS= read -r line; do
        case "$line" in ''|'#'*) continue ;; esac
        fp="$(printf '%s\n' "$line" | ssh-keygen -lf - 2>/dev/null | awk '{print $2}')"
        comment="$(printf '%s\n' "$line" | awk '{for (i = 1; i <= NF; i++) if ($i ~ /^(ssh-|ecdsa-|sk-)/) {c = ""; for (j = i + 2; j <= NF; j++) c = c " " $j; print c; exit}}')"
        acc="$(journalctl -u ssh -u sshd --since '-30 days' --no-pager 2>/dev/null | grep -F "Accepted publickey" | grep -F "${fp:-нет}")"
        used="$(printf '%s' "$acc" | grep -c .)"
        from="$(printf '%s\n' "$acc" | awk '{for (i = 1; i <= NF; i++) if ($i == "from") print $(i + 1)}' | sort | uniq -c | sort -rn | head -3 | awk '{printf "%s×%s ", $2, $1}')"
        info "  ${fp:-?} ${comment:- (без комментария)} — входов за 30 дн.: $used ${from:+(с IP: $from)}"
    done < "$ak"
    [ "$n" -gt 1 ] && bad "$ak: ключей больше одного — проверь, что каждый твой (лишний удалить)"
done
ips="$(journalctl -u ssh -u sshd --since '-7 days' --no-pager 2>/dev/null | grep -F 'Accepted ' | awk '{for (i = 1; i <= NF; i++) if ($i == "from") print $(i + 1)}' | sort | uniq -c | sort -rn | head -5 | awk '{printf "%s×%s ", $2, $1}')"
info "успешные входы за 7 дн. (IP×раз): ${ips:-нет записей}"
fails="$(journalctl -u ssh -u sshd --since '-24 hours' --no-pager 2>/dev/null | grep -cE 'Failed password|Invalid user')"
info "неудачных попыток входа за сутки: $fails"

head_ "Файрвол (ufw) и открытые порты"
if command -v ufw >/dev/null 2>&1; then
    ufw status 2>/dev/null | grep -q '^Status: active' && ok "ufw включён" || bad "ufw выключен"
    listen="$(ss -tlnH 2>/dev/null | awk '{print $4}' | sed 's/.*://' | sort -u)"
    for port in $(ufw status 2>/dev/null | awk '/ALLOW/ {print $1}' | grep -oE '^[0-9]+' | sort -u); do
        note="$(ufw status 2>/dev/null | grep -E "^$port(/tcp)? " | grep -o '#.*' | head -1)"
        if printf '%s\n' "$listen" | grep -qx "$port"; then
            info "порт $port открыт, слушается ${note}"
        else
            bad "порт $port открыт в ufw, но на нём никто не слушает ${note} — правило лишнее"
        fi
    done
else
    bad "ufw не установлен"
fi
info "слушают снаружи (не 127.0.0.1): $(ss -tlnH 2>/dev/null | awk '{print $4}' | grep -vE '^(127\.|\[::1\]|::1)' | tr '\n' ' ')"
if command -v fail2ban-client >/dev/null 2>&1; then
    st="$(fail2ban-client status sshd 2>/dev/null)"
    [ -n "$st" ] && ok "fail2ban sshd: забанено сейчас $(printf '%s\n' "$st" | awk -F: '/Currently banned/ {gsub(/[ \t]/, "", $2); print $2}')" \
        || bad "fail2ban не охраняет sshd"
else
    bad "fail2ban не установлен"
fi

head_ "Обновления и время"
up="$(apt list --upgradable 2>/dev/null | grep -c upgradable)"
sec="$(apt list --upgradable 2>/dev/null | grep -c -- '-security')"
[ "$sec" -eq 0 ] && ok "обновлений безопасности не ждёт (всего обновлений $up)" \
    || bad "ждут установки обновлений: $up, из них безопасности $sec"
[ -f /var/run/reboot-required ] && bad "нужна перезагрузка после обновлений (/var/run/reboot-required)"
[ "$(systemctl is-enabled unattended-upgrades 2>/dev/null)" = "enabled" ] && ok "unattended-upgrades включён" \
    || bad "unattended-upgrades выключен"
[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "yes" ] && ok "время синхронизировано (NTP) — нужно для подписи запросов Bybit" \
    || bad "время не синхронизировано — Bybit отклонит подписанные запросы"

head_ "Сканер: пользователь и песочница systemd"
for u in $units; do
    systemctl cat "$u" >/dev/null 2>&1 || { info "$u не установлен"; continue; }
    user="$(systemctl show "$u" -p User --value)"
    [ -n "$user" ] && [ "$user" != "root" ] && ok "$u работает от $user" \
        || bad "$u работает от root — код из публичного репозитория получает весь сервер"
    [ "$(systemctl show "$u" -p NoNewPrivileges --value)" = "yes" ] && ok "$u: NoNewPrivileges" \
        || bad "$u: нет NoNewPrivileges=yes"
    ps_="$(systemctl show "$u" -p ProtectSystem --value)"
    case "$ps_" in strict|full) ok "$u: ProtectSystem=$ps_" ;; *) bad "$u: ProtectSystem=${ps_:-no}" ;; esac
done
if [ -f "$scanner_dir/.env" ]; then
    mode="$(stat -c '%a %U' "$scanner_dir/.env")"
    case "$mode" in 600\ *|400\ *) ok ".env: права $mode" ;; *) bad ".env: права $mode — должно быть 600" ;; esac
fi
ww="$(find "$scanner_dir" -xdev -perm -o+w ! -type l 2>/dev/null | head -5 | tr '\n' ' ')"
[ -z "$ww" ] && ok "в $scanner_dir нет файлов, которые может менять любой пользователь" \
    || bad "файлы с правом записи для всех: $ww"
if [ -d "$scanner_dir/.git" ]; then
    dirty="$(git -c safe.directory="$scanner_dir" -C "$scanner_dir" status --porcelain --untracked-files=no 2>/dev/null | head -5 | tr '\n' ' ')"
    [ -z "$dirty" ] && ok "код сканера совпадает с коммитом $(git -c safe.directory="$scanner_dir" -C "$scanner_dir" rev-parse --short HEAD 2>/dev/null) (правок на сервере нет)" \
        || bad "в $scanner_dir изменены файлы из git: $dirty — кто их правил?"
    info "источник обновлений: $(git -c safe.directory="$scanner_dir" -C "$scanner_dir" remote get-url origin 2>/dev/null)"
fi

head_ "Учётные записи"
info "пользователи с оболочкой входа: $(awk -F: '$7 !~ /(nologin|false|sync|halt|shutdown)$/ {printf "%s ", $1}' /etc/passwd)"
info "в группе sudo: $(getent group sudo | cut -d: -f4)"
id scanner >/dev/null 2>&1 && ok "пользователь scanner есть" || info "пользователя scanner нет (см. docs/VPS_SECURITY.md, шаг 4)"

printf '\nИтого предупреждений: %s. Что делать — docs/VPS_SECURITY.md.\n' "$warn"
exit 0
