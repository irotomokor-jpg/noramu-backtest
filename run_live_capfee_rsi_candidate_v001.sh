#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/noramu-backtest"
BEFORE="$(sha256sum toss_us_live_open_v001.py | awk '{print $1}')"
.venv/bin/python -m py_compile build_live_capfee_rsi_candidate_v001.py
.venv/bin/python build_live_capfee_rsi_candidate_v001.py
AFTER="$(sha256sum toss_us_live_open_v001.py | awk '{print $1}')"
if [ "$BEFORE" != "$AFTER" ]; then
  echo "ACTIVE_ENGINE_HASH_CHANGED=FAIL"
  exit 20
fi
echo "ACTIVE_ENGINE_HASH_UNCHANGED=PASS"
.venv/bin/python - <<'PY'
import importlib.util
from decimal import Decimal
from pathlib import Path
p=Path('toss_us_live_open_v002_capfee.py')
spec=importlib.util.spec_from_file_location('cand',p)
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print('===== FEE SAFE SIZING AUDIT =====')
total=Decimal('0')
for x in ('120','40','20','20'):
    y=m.fee_safe_order_amount(Decimal(x))
    total += y
    print(f'budget={x} safe_order_amount={y}')
print(f'all_four_safe_order_sum={total}')
print(f'<=200={total <= Decimal("200")}')
PY
echo "===== CANDIDATE AUDIT JSON ====="
cat live/US_FROZEN_V1/capfee_rsi_candidate_audit.json
echo
echo "LIVE_CAPFEE_RSI_CANDIDATE_AUDIT=PASS"
