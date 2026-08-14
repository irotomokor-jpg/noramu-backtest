#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "===== V003 BUILD ====="
.venv/bin/python patch_rsi_pullback_v003_weighted.py
.venv/bin/python -m py_compile us_rsi_pullback_v003_weighted.py
echo "V003_SYNTAX=PASS"

echo
echo "===== UPDATE COMMISSION ====="
set -a
source "$HOME/.config/noramu/toss.env"
set +a
.venv/bin/python toss_fee_v001.py

echo
echo "===== LIVE WATCHERS BEFORE ====="
./job_status.sh US_LIVE_OPEN_WATCHER
./job_status.sh US_LIVE_INTRADAY_DATA
./job_status.sh US_LIVE_INTRADAY_SIGNAL

echo
echo "===== V003 JULY 2026 1M REPLAY ====="
rm -rf rsi_pullback_v003_weighted_202607
nice -n 15 .venv/bin/python -u us_rsi_pullback_v003_weighted.py --start 2026-07-01 --end 2026-08-01 --out rsi_pullback_v003_weighted_202607 | tee rsi_pullback_v003_weighted_202607.log

echo
echo "===== V003 TRADE DETAIL ====="
.venv/bin/python -c "import pandas as pd; d=pd.read_csv('rsi_pullback_v003_weighted_202607/trades.csv'); cols=['exec_symbol','variant','trade_date','entry_ts','entry_reason','exit_reason','net_return','mae','mfe','knife_score','knife_weighted_static','band_walk','bb_lower_fall','bandwidth_exp','lower_low3','lower_close3','gap_down','early_break_prior_low']; print(d[cols].sort_values(['trade_date','exec_symbol','variant']).to_string(index=False) if len(d) else 'NO_TRADES')"

echo
echo "===== LIVE WATCHERS AFTER ====="
./job_status.sh US_LIVE_OPEN_WATCHER
./job_status.sh US_LIVE_INTRADAY_DATA
./job_status.sh US_LIVE_INTRADAY_SIGNAL
