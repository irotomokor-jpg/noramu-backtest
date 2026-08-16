#!/usr/bin/env python3
from pathlib import Path

p = Path("fast_rebound_v004_koru_regime_diagnostic.py")
if not p.exists():
    raise SystemExit(f"MISSING={p}")

s = p.read_text(encoding="utf-8")
old = '''def attach_asof(tr: pd.DataFrame, ctx: pd.DataFrame) -> pd.DataFrame:\n    a = tr.sort_values("signal_dt").copy()\n    b = ctx.sort_values("ts").copy()\n    out = pd.merge_asof(a, b, left_on="signal_dt", right_on="ts", direction="backward", tolerance=pd.Timedelta(minutes=1))\n    return out.drop(columns=["ts"], errors="ignore")\n'''
new = '''def attach_asof(tr: pd.DataFrame, ctx: pd.DataFrame) -> pd.DataFrame:\n    a = tr.sort_values("signal_dt").copy()\n    # trade_date is metadata duplicated in every context frame.  Keeping it on\n    # the right side of repeated merge_asof calls creates trade_date_x/y, then\n    # a later merge tries to create the same suffixes again and pandas raises.\n    # Context feature names are already symbol-prefixed, so only ts + features\n    # are needed for the asof join.\n    right_cols = [c for c in ctx.columns if c != "trade_date"]\n    b = ctx[right_cols].sort_values("ts").copy()\n    overlap = [c for c in b.columns if c != "ts" and c in a.columns]\n    if overlap:\n        raise SystemExit("UNEXPECTED_CONTEXT_COLUMN_OVERLAP=" + ",".join(overlap))\n    out = pd.merge_asof(\n        a,\n        b,\n        left_on="signal_dt",\n        right_on="ts",\n        direction="backward",\n        tolerance=pd.Timedelta(minutes=1),\n    )\n    return out.drop(columns=["ts"], errors="ignore")\n'''

if old not in s:
    if 'right_cols = [c for c in ctx.columns if c != "trade_date"]' in s:
        print("FAST_REBOUND_V004_FIX1=ALREADY_APPLIED")
        raise SystemExit(0)
    raise SystemExit("PATCH_TARGET_NOT_FOUND")

p.write_text(s.replace(old, new, 1), encoding="utf-8")
print("FAST_REBOUND_V004_FIX1=PASS")
print("FIX=drop_duplicate_trade_date_metadata_before_repeated_merge_asof")
print("SAFETY=fail_on_unexpected_context_column_overlap")
