#!/usr/bin/env python3
from pathlib import Path
import py_compile

p = Path("fast_rebound_v005_koru_guard_freeze.py")
if not p.exists():
    raise SystemExit(f"MISSING={p}")

s = p.read_text(encoding="utf-8")
old1 = 'keep[keep.sample == "BACKCAST_2022_2023"]'
old2 = 'keep[keep.sample == "DEV_2024_2026"]'
new1 = 'keep[keep["sample"] == "BACKCAST_2022_2023"]'
new2 = 'keep[keep["sample"] == "DEV_2024_2026"]'

if old1 not in s or old2 not in s:
    if new1 in s and new2 in s:
        print("FAST_REBOUND_V005_FIX1=ALREADY_APPLIED")
    else:
        raise SystemExit("EXPECTED_SAMPLE_ACCESS_PATTERN_NOT_FOUND")
else:
    s = s.replace(old1, new1).replace(old2, new2)
    marker = 'z = add_periods(z)\n'
    guard = 'z = add_periods(z)\n    if "sample" not in z.columns:\n        raise SystemExit("MISSING_SAMPLE_COLUMN_AFTER_ADD_PERIODS")\n'
    if marker in s and 'MISSING_SAMPLE_COLUMN_AFTER_ADD_PERIODS' not in s:
        s = s.replace(marker, guard, 1)
    p.write_text(s, encoding="utf-8")
    print("FAST_REBOUND_V005_FIX1=PASS")

py_compile.compile(str(p), doraise=True)
print("FIX=use_bracket_access_for_dataframe_sample_column")
print("SAFETY=assert_sample_column_exists_before_scope_filters")
print("COMPILE=PASS")
