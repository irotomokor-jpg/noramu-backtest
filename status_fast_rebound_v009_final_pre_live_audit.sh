#!/usr/bin/env bash
set -u
cd "$HOME/noramu-backtest"
OUT="fast_rebound_v009_final_pre_live_audit"
if [ -f "$OUT/FINAL_PRE_LIVE_AUDIT.txt" ]; then
  cat "$OUT/FINAL_PRE_LIVE_AUDIT.txt"
else
  echo "V009_AUDIT_OUTPUT_NOT_FOUND"
fi
if [ -f "$OUT/FINAL_PRE_LIVE_AUDIT.json" ]; then
  echo "===== JSON SUMMARY ====="
  .venv/bin/python - <<'PY'
import json
from pathlib import Path
p=Path('fast_rebound_v009_final_pre_live_audit/FINAL_PRE_LIVE_AUDIT.json')
j=json.loads(p.read_text())
for k in ['pre_live_contract_pass','integrated_writer_candidate_exists','live_ready','live_approval','order_writes_changed','current_live_cap_usd','projected_rsi_cap_usd','projected_fast_cap_usd','checks_passed','checks_total','checks_failed','next']:
    print(f"{k.upper()}={j.get(k)}")
PY
fi
