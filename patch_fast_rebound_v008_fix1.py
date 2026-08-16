#!/usr/bin/env python3
from pathlib import Path
import py_compile

P = Path("fast_rebound_v008_combined_occupancy_replay.py")
if not P.exists():
    raise SystemExit(f"MISSING={P}")

s = P.read_text(encoding="utf-8")
old = '''    std0 = cmp[cmp.mode == "STANDARD_PREEMPT_0BPS"]
    std50 = cmp[cmp.mode == "STANDARD_PREEMPT_50BPS"]
    stress50 = cmp[cmp.mode == "STRESS_PREEMPT_50BPS"]
'''
new = '''    if "mode" not in cmp.columns:
        raise SystemExit("CMP_MODE_COLUMN_MISSING")
    std0 = cmp[cmp["mode"] == "STANDARD_PREEMPT_0BPS"]
    std50 = cmp[cmp["mode"] == "STANDARD_PREEMPT_50BPS"]
    stress50 = cmp[cmp["mode"] == "STRESS_PREEMPT_50BPS"]
'''
if old not in s:
    if 'cmp["mode"] == "STANDARD_PREEMPT_0BPS"' in s:
        print("FAST_REBOUND_V008_FIX1=ALREADY_APPLIED")
    else:
        raise SystemExit("PATCH_TARGET_NOT_FOUND")
else:
    P.write_text(s.replace(old, new), encoding="utf-8")
    print("FAST_REBOUND_V008_FIX1=PASS")

py_compile.compile(str(P), doraise=True)
print("FIX=use_bracket_access_for_dataframe_mode_column")
print("SAFETY=assert_mode_column_exists_before_filters")
print("COMPILE=PASS")
