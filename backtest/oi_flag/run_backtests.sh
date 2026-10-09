#!/usr/bin/env bash
# Прогон прод-бэктестов в копии варианта: ./run_backtests.sh v0|v1|v2  (логи в logs/)
set -u
B="$(cd "$(dirname "$0")" && pwd)"
v="$1"; cd "$B/code_$v" || exit 1
mkdir -p "$B/logs"
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1
for s in market_regime_study junk_filter_study portfolio_vs_market hot_exit_study; do
  echo "== $v $s $(date +%T)"
  python "backtest/$s.py" > "$B/logs/${v}_$s.log" 2>&1 || echo "FAIL $v $s"
done
echo "== $v done $(date +%T)"
