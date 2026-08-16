#!/usr/bin/env bash
set -u
cd "$HOME/noramu-backtest"
PIDFILE="fast_rebound_v003_koru/run.pid"
LOG="fast_rebound_v003_koru/run.log"
PID=""
if [ -f "$PIDFILE" ]; then PID="$(cat "$PIDFILE" 2>/dev/null || true)"; fi
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  echo "FAST_REBOUND_V003_KORU_PROCESS=RUNNING"
  echo "PID=$PID"
else
  echo "FAST_REBOUND_V003_KORU_PROCESS=NOT_RUNNING"
  if [ -n "$PID" ]; then echo "LAST_PID=$PID"; fi
fi
if [ -f "fast_rebound_v003_koru/FINAL_RANKING.csv" ]; then
  echo "===== FINAL RANKING TOP 20 ====="
  .venv/bin/python - <<'PY'
import pandas as pd
p='fast_rebound_v003_koru/FINAL_RANKING.csv'
d=pd.read_csv(p)
cols=[c for c in ['config','dev_trades','dev_profit_factor','dev_expectancy_bps','dev_stress_profit_factor','dev_stress_expectancy_bps','holdout_trades','holdout_profit_factor','holdout_expectancy_bps','holdout_stress_profit_factor','holdout_stress_expectancy_bps','positive_years','min_year_pf','holdout_pass','fold_stable','final_research_pass'] if c in d.columns]
print(d[cols].head(20).to_string(index=False))
PY
fi
if [ -f "fast_rebound_v003_koru/walkforward_selected.csv" ]; then
  echo "===== WALK FORWARD SELECTED ====="
  cat fast_rebound_v003_koru/walkforward_selected.csv
fi
if [ -f "fast_rebound_v003_koru/stop_overshoot.csv" ]; then
  echo "===== STOP OVERSHOOT TOP 20 ====="
  head -n 21 fast_rebound_v003_koru/stop_overshoot.csv
fi
echo "===== LOG TAIL ====="
tail -n 120 "$LOG" 2>/dev/null || true
