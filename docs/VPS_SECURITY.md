# Защита VPS сканера — что сделать владельцу сервера

Проверка (только чтение, ничего не меняет): `bash /opt/scanner/scripts/vps_security_check.sh`.
Ниже — исправления по её предупреждениям. Делает их **владелец сервера руками**: скрипты сканера
системных и сетевых настроек не меняют. Состояние на 08.10.2026: 8 предупреждений.

Перед каждым шагом про SSH держи открытой текущую сессию и проверяй вход **во втором окне**
(`ssh vps-trader true`) — ошибка в настройке не должна запереть тебя снаружи.

## 1. Вход по паролю и X11 — выключить

За сутки ~1700 неудачных попыток подобрать пароль. Сейчас вход по паролю разрешает файл
`/etc/ssh/sshd_config.d/50-cloud-init.conf`. sshd берёт **первое** значение, а файлы читает
по алфавиту, поэтому файл `00-…` перекрывает `50-cloud-init.conf`:

```bash
cat > /etc/ssh/sshd_config.d/00-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
X11Forwarding no
EOF
sshd -t && systemctl reload ssh
sshd -T | grep -E '^(passwordauthentication|x11forwarding|kbdinteractiveauthentication) '
```

Ожидается `no` у всех трёх. Ключи (ноутбук и второй сервер) продолжают работать.

## 2. Лишнее правило ufw: порт 8080

Правило `8080/tcp ALLOW` с комментарием «Watchdog health-check» оставлено торговым ботом
(crypto-trading-lab), но на 8080 сейчас никто не слушает. Если внешний мониторинг на этот порт
не ходит:

```bash
ufw status numbered          # найти номера строк 8080 (v4 и v6)
ufw delete allow 8080/tcp    # удаляет обе (v4 и v6)
ufw status
```

## 3. Второй ключ root: root@538614.senko.network

В `/root/.ssh/authorized_keys` два ключа: ноутбук (`sanny@LAPTOP-…`) и
`root@538614.senko.network`. Второй ключ за 30 дней входил 91 раз, всегда с IP
31.77.160.135. Если это твой второй сервер (Senko) — оставь, но ограничь вход этим IP:
допиши в начало его строки `from="31.77.160.135" ` (через пробел перед `ssh-ed25519 …`).
Если ключ не твой — удали строку и смени ключи доступа на всех серверах.

## 4. Отдельный пользователь `scanner` и песочница systemd

Сейчас сканер работает от root, а его код приходит `git pull` из публичного репозитория:
кто получит запись в репозиторий, получит весь сервер (и `.env` торгового бота). Пользователь
без прав и песочница ограничивают сканер каталогом `/opt/scanner`. Сделать в паузе между
прогонами (не в 10:00 по Самаре):

```bash
useradd --system --home-dir /opt/scanner --shell /usr/sbin/nologin scanner
chown -R scanner:scanner /opt/scanner /opt/backups/scanner
chmod 600 /opt/scanner/.env
for u in accumulation-scanner scanner-control scanner-alert; do
    mkdir -p /etc/systemd/system/$u.service.d
    cp /opt/scanner/scripts/systemd/sandbox/sandbox.conf /etc/systemd/system/$u.service.d/
done
systemctl daemon-reload
```

Проверка (без отправки в Telegram):

```bash
systemd-run --wait --pipe -p User=scanner -p ProtectSystem=strict \
    -p ReadWritePaths=/opt/scanner -p WorkingDirectory=/opt/scanner \
    python3 run.py brief                    # сводка собирается, пишет в консоль
systemctl start scanner-control.service && journalctl -u scanner-control -n 5 --no-pager
systemd-analyze security accumulation-scanner.service | tail -1
bash /opt/scanner/scripts/vps_security_check.sh | grep -E 'scanner|WARN'
```

Обновления после этого: `bash /opt/scanner/scripts/deploy.sh` можно запускать и от root —
скрипт сам переключится на пользователя `scanner`. Юниты (если поменялись) ставит root:
`bash /opt/scanner/scripts/install_units.sh`.

Откат: `rm -r /etc/systemd/system/{accumulation-scanner,scanner-control,scanner-alert}.service.d`,
`systemctl daemon-reload`, `chown -R root:root /opt/scanner /opt/backups/scanner`.

## 5. Перезагрузка после обновлений

`/var/run/reboot-required` есть — ядро обновлено. Перезагрузить в спокойное время
(`reboot`), лучше не рядом с 10:00 по Самаре. Сканер переживает перезагрузку: таймер с
`Persistent=true` догонит пропущенный прогон, а оборванный прогон сводка следующего покажет
строкой «⚠ прошлый прогон оборвался». Торговый бот в docker поднимется сам, если у
контейнеров `restart: unless-stopped` или `always`; это лучше проверить до перезагрузки.

## Что уже защищено кодом сканера

- Стоп-кран: `/stop` из Telegram (только из твоего чата и только от тебя) или
  `python3 run.py halt причина` на сервере. Снять можно только на сервере:
  `python3 run.py resume`, и тебе придёт сообщение. Угнанный Telegram может остановить
  исполнителя, но не запустить его.
- Самопроверка ключа Bybit перед каждым sync: только чтение, без права вывода, IP привязан,
  срок. Опасный ключ — сообщение со звуком.
- Ключи только в `.env` (права 600). Ключ, вписанный в `config.json`, не используется, а
  сводка дня об этом предупреждает.
- Прогон: замок (два прогона не идут одновременно), таймаут на каждый шаг, «⚠ прогон убит»,
  если прогон оборвался.
