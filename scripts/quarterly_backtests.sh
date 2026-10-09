#!/usr/bin/env bash
# Квартальный перезапуск ключевых исследований на свежей истории (docs/DECISIONS.md, Q2).
#
# Что делает (по порядку, без остановки на упавшем шаге):
#   1. докачивает архив Binance с делистнутыми парами (backtest/binance_archive.py ->
#      .cache/binance_1d_full) и свечи живых монет для market_regime_study (.cache/binance_1d);
#   2. клонирует репозиторий в рабочий каталог и копирует туда базу (sqlite backup, источник
#      открывается только на чтение) и кэши — боевая scanner.db и рабочая копия кода не
#      меняются, исследования пишут *_results.json только в клон;
#   3. по очереди: ladder_dca_study, junk_filter_study, portfolio_vs_market,
#      market_regime_study, hold_stop_study, score_quintiles — лог каждого в logs/;
#   4. сравнивает новые *_results.json с теми, что лежат в репозитории
#      (backtest/compare_results.py) -> summary.txt.
# Принять новые итоги: скопировать *_results.json из <рабочий>/repo/backtest в backtest/,
# дописать итог в docs/ (LADDER_REPORT.md и др.) и закоммитить — руками, после чтения summary.
#
# Где запускать: ноутбук (Git Bash) или VPS — нужны python3 и сеть до data.binance.vision и
# api.binance.com (с VPS проверено 09.10.2026). Архива на VPS изначально нет: первый прогон
# скачает его целиком (~40 МБ, сотни тысяч мелких запросов — долго); быстрее один раз
# скопировать .cache/binance_1d_full с ноутбука. Счёт ~15–25 мин CPU, один процесс.
# На VPS — вне окна таймера сканера (06:00 UTC), не трогает его замок и базу.
#
# Запуск из корня репозитория:
#   bash scripts/quarterly_backtests.sh                 # база — ./scanner.db
#   bash scripts/quarterly_backtests.sh --db /путь/копия.db --no-update
#   WORK=/tmp/q bash scripts/quarterly_backtests.sh     # другой рабочий каталог
# Ключи: --db PATH — база с market_daily и candidates (на ноутбуке ./scanner.db устаревает —
#   взять свежую копию с VPS); --quintiles-db PATH — база прогонов для score_quintiles (по
#   умолчанию --db; итог 10.2026 считан по архиву v1 /opt/scanner-old-2026-10-03/scanner.db —
#   с другой базой сравнение с ним показывает смену выборки, а не балла); --no-update — без
#   сети (кэши как есть); --only "a b" — только эти исследования (имена без .py).
# Код берётся из закоммиченного HEAD (git clone), незакоммиченные правки в прогон не идут.
# Код: 0 — все шаги прошли, 1 — хотя бы один упал (см. summary.txt и logs/).
set -u
src="$(cd "$(dirname "$0")/.." && pwd)"
db="$src/scanner.db"
qdb=""
update=1
only=""
while [ $# -gt 0 ]; do
    case "$1" in
        --db) db="$2"; shift 2 ;;
        --quintiles-db) qdb="$2"; shift 2 ;;
        --no-update) update=0; shift ;;
        --only) only="$2"; shift 2 ;;
        -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
        *) echo "неизвестный ключ: $1"; exit 2 ;;
    esac
done
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1
py="${PY:-}"
if [ -z "$py" ]; then
    for c in python3 python; do
        if "$c" -c "import sys; sys.exit(sys.version_info < (3, 10))" 2>/dev/null; then
            py="$c"; break
        fi
    done
fi
[ -n "$py" ] || { echo "нужен python ≥ 3.10"; exit 2; }
[ -f "$db" ] || { echo "нет базы $db (--db PATH)"; exit 2; }

stamp="$(date -u +%Y-%m-%d_%H%M)"
work="${WORK:-$src/.cache/quarterly/$stamp}"
mkdir -p "$work/logs"
summary="$work/summary.txt"
log() { echo "$*" | tee -a "$summary"; }
log "Квартальный перезапуск бэктестов $stamp UTC"
log "код: $(git -C "$src" rev-parse --short HEAD) ($(git -C "$src" log -1 --format=%cs)); база: $db"
log "рабочий каталог: $work"

# ---- 1. данные
if [ "$update" = 1 ]; then
    log "--- 1. докачка архива Binance (binance_archive.py)"
    ( cd "$src" && "$py" backtest/binance_archive.py ) > "$work/logs/binance_archive.log" 2>&1 \
        && log "  ок: $(tail -1 "$work/logs/binance_archive.log")" \
        || log "  ⚠ не прошла (код $?) — считаю на том, что есть; logs/binance_archive.log"
    log "--- 1б. свечи живых монет для market_regime_study (.cache/binance_1d)"
    ( cd "$src" && "$py" - ) > "$work/logs/binance_1d.log" 2>&1 <<'EOF'
import sys
sys.path.insert(0, "backtest")
import zone_edges_study as Z
have = sorted(p.stem for p in Z.CACHE.glob("*.json")) if Z.CACHE.exists() else []
syms = sorted(set(have) | {"BTCUSDT"}) if have else ["BTCUSDT"] + Z.top_symbols(Z.TOP_N)
ok = sum(1 for s in syms if len(Z.history(s)[1]) > 0)
print(f"монет {len(syms)}, со свечами {ok}")
EOF
    rc=$?
    [ $rc = 0 ] && log "  ок: $(tail -1 "$work/logs/binance_1d.log")" \
        || log "  ⚠ не прошло (код $rc) — logs/binance_1d.log"
fi
idx="$src/.cache/binance_1d_full/_index.json"
if [ -f "$idx" ]; then
    log "  архив Binance: $("$py" -c "import json,time,sys; d=json.load(open(sys.argv[1])); print(len(d), 'пар, последний день', time.strftime('%Y-%m-%d', time.gmtime(max(v['last'] for v in d.values()))))" "$idx")"
else
    log "  ⚠ архива .cache/binance_1d_full нет — ladder/junk/portfolio/hold_stop упадут"
fi

# ---- 2. клон, база, кэши
log "--- 2. клон репозитория и копия базы"
rm -rf "$work/repo"
git -c core.longpaths=true clone -q "$src" "$work/repo" || { log "  ⚠ git clone не прошёл"; exit 1; }
mkdir -p "$work/repo/.cache"
for c in binance_1d_full binance_1d; do
    [ -d "$src/.cache/$c" ] && cp -r "$src/.cache/$c" "$work/repo/.cache/"
done
"$py" - "$db" "$work/repo/scanner.db" <<'EOF' || { log "  ⚠ копия базы не снята"; exit 1; }
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
EOF
log "  клон $(git -C "$work/repo" rev-parse --short HEAD), база скопирована ($(du -h "$work/repo/scanner.db" | cut -f1))"

# ---- 3. исследования
studies="${only:-ladder_dca_study junk_filter_study portfolio_vs_market market_regime_study hold_stop_study score_quintiles}"
fail=0
log "--- 3. исследования: $studies"
for s in $studies; do
    args=""
    out="backtest/${s%_study}_results.json"
    case "$s" in
        ladder_dca_study) out="backtest/ladder_dca_results.json" ;;
        junk_filter_study) out="backtest/junk_filter_results.json" ;;
        portfolio_vs_market) out="backtest/portfolio_vs_market_results.json" ;;
        market_regime_study) out="backtest/market_regime_results.json" ;;
        hold_stop_study) out="backtest/hold_stop_results.json" ;;
        score_quintiles) out="backtest/score_quintiles_results.json"
                         args="--db ${qdb:-scanner.db} --out $out" ;;
    esac
    t0=$(date +%s)
    # shellcheck disable=SC2086
    ( cd "$work/repo" && "$py" "backtest/$s.py" $args ) > "$work/logs/$s.log" 2>&1
    rc=$?
    dt=$(( $(date +%s) - t0 ))
    if [ $rc = 0 ]; then
        log "  ✓ $s — ${dt} с"
    else
        fail=1
        log "  ✗ $s — код $rc за ${dt} с; хвост лога:"
        tail -5 "$work/logs/$s.log" | sed 's/^/      /' | tee -a "$summary"
    fi
    if [ -f "$src/$out" ] && [ -f "$work/repo/$out" ]; then
        "$py" "$src/backtest/compare_results.py" "$src/$out" "$work/repo/$out" --top 12 \
            2>&1 | sed 's/^/    /' | tee -a "$summary"
    fi
done
log "--- итог: $([ $fail = 0 ] && echo 'все шаги прошли' || echo 'есть упавшие шаги')"
log "Принять: cp $work/repo/backtest/*_results.json $src/backtest/ ; итог — в docs/; новая строка Q в docs/DECISIONS.md (+3 мес.)"
exit $fail
