#!/usr/bin/env bash
# Обновление сканера на VPS: fetch -> только fast-forward -> selftest нового кода во временной
# копии -> git merge --ff-only -> selftest на месте -> провал: откат на прежний коммит.
# Запуск на сервере: bash /opt/scanner/scripts/deploy.sh   (код 0 — обновлено или нечего)
# Не посреди прогона: тот же замок, что у daily_run.sh (data/daily_run.lock), ждём до 10 минут.
# Во временной копии нет .env и scanner.db — selftest идёт без ключей и без боевой базы.
set -u
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1
branch="${DEPLOY_BRANCH:-main}"

# Репозиторий отдан пользователю scanner (docs/VPS_SECURITY.md, шаг 4) — git от его имени,
# иначе git откажется («dubious ownership») или оставит файлы root в рабочей копии.
owner="$(stat -c %U "$root")"
if [ "$(id -u)" = 0 ] && [ "$owner" != root ]; then
    exec runuser -u "$owner" -- bash "$0" "$@"
fi

mkdir -p data
exec 9>data/daily_run.lock
flock -w 600 9 || { echo "deploy: идёт прогон сканера — повтори позже"; exit 1; }

old="$(git rev-parse HEAD)"
git fetch -q origin "$branch" || { echo "deploy: git fetch не прошёл"; exit 1; }
new="$(git rev-parse "origin/$branch")"
if [ "$old" = "$new" ]; then
    echo "deploy: уже на $(git rev-parse --short HEAD) — обновлять нечего"
    exit 0
fi
if ! git merge-base --is-ancestor "$old" "$new"; then
    echo "deploy: origin/$branch не продолжает текущий коммит (история переписана?) — стоп"
    exit 1
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "deploy: на сервере изменены файлы из git — стоп, сначала разобраться:"
    git status --short --untracked-files=no
    exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
echo "deploy: selftest $(git rev-parse --short "$new") во временной копии…"
git clone -q --no-checkout "$root" "$tmp/sc" && git -C "$tmp/sc" checkout -q "$new" \
    || { echo "deploy: временная копия не создалась"; exit 1; }
if ! (cd "$tmp/sc" && timeout 1200 python3 run.py selftest > "$tmp/selftest-new.log" 2>&1); then
    tail -25 "$tmp/selftest-new.log"
    echo "deploy: selftest нового кода не прошёл — рабочая копия не тронута"
    exit 1
fi

git merge -q --ff-only "$new" || { echo "deploy: fast-forward не прошёл"; exit 1; }
if ! timeout 1200 python3 run.py selftest > "$tmp/selftest-here.log" 2>&1; then
    tail -25 "$tmp/selftest-here.log"
    git reset -q --hard "$old"
    echo "deploy: selftest на месте не прошёл — откатил на $(git rev-parse --short "$old")"
    exit 1
fi
echo "deploy: $(git rev-parse --short "$old") -> $(git rev-parse --short HEAD) — selftest " \
     "$(grep -c 'PASS' "$tmp/selftest-here.log") PASS"
git log --oneline "$old..$new"

if ! git diff --quiet "$old" "$new" -- scripts/systemd; then
    if [ "$(id -u)" = 0 ]; then
        bash scripts/install_units.sh
    else
        echo "deploy: изменились юниты systemd — от root: bash $root/scripts/install_units.sh"
    fi
fi
