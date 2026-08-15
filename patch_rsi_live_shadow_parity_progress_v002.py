#!/usr/bin/env python3
from pathlib import Path

p = Path("rsi_live_shadow_parity_v001.py")
s = p.read_text(encoding="utf-8")

old = '''    rows = []\n    cache = {}\n    for _, t in tr.iterrows():\n'''
new = '''    rows = []\n    cache = {}\n    total_expected = len(tr)\n    print(f"PARITY_START total={total_expected}", flush=True)\n    for idx, (_, t) in enumerate(tr.iterrows(), start=1):\n        print(f"PARITY_PROGRESS {idx}/{total_expected} date={t.trade_date} pair={t.signal_symbol}->{t.exec_symbol}", flush=True)\n'''
if old not in s:
    raise SystemExit("PATCH_MISS:parity_loop")
s = s.replace(old, new, 1)

old2 = '''    historical_parity(mod)\n    shadow_snapshot(mod)\n    print("RSI_LIVE_SHADOW_CANDIDATE_V001=PASS")\n'''
new2 = '''    print("ENGINE_IMPORT=PASS", flush=True)\n    historical_parity(mod)\n    print("HISTORICAL_PARITY_DONE", flush=True)\n    shadow_snapshot(mod)\n    print("SHADOW_SNAPSHOT_DONE", flush=True)\n    print("RSI_LIVE_SHADOW_CANDIDATE_V001=PASS", flush=True)\n'''
if old2 not in s:
    raise SystemExit("PATCH_MISS:main_progress")
s = s.replace(old2, new2, 1)

compile(s, str(p), "exec")
p.write_text(s, encoding="utf-8")
print("RSI_LIVE_SHADOW_PROGRESS_V002=PASS")
print("PROGRESS=PER_TRADE_1_OF_42")
