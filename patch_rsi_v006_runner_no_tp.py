#!/usr/bin/env python3
from pathlib import Path

src = Path('rsi_v005_exit_study_fix1.py')
out = Path('rsi_v006_runner_no_tp.py')
s = src.read_text(encoding='utf-8')

s = s.replace('OUT = Path("rsi_pullback_v005_exit_study_fix1")','OUT = Path("rsi_pullback_v006_runner_no_tp")',1)
s = s.replace('VARIANTS = ["CURRENT", "TIME90_WEAK", "VWAP60_WEAK"]','VARIANTS = ["CURRENT", "RUNNER_NO_TP"]',1)

old = '''        if float(r.high) / entry_px - 1.0 >= HARD_TP:\n            z = next_open(x, i, "HARD_TP")\n            return z[0], z[1], z[2], locked\n'''
new = '''        if variant != "RUNNER_NO_TP" and float(r.high) / entry_px - 1.0 >= HARD_TP:\n            z = next_open(x, i, "HARD_TP")\n            return z[0], z[1], z[2], locked\n'''
if old not in s:
    raise SystemExit('PATCH_MISS:hard_tp')
s = s.replace(old, new, 1)

# Add a focused comparison for the trades that CURRENT exits via HARD_TP.
needle = '''    cutoff_compare.to_csv(OUT / "current_cutoff_subset.csv", index=False)\n    delta.to_csv(OUT / "current_cutoff_delta.csv", index=False)\n\n    report = [\n'''
insert = '''    cutoff_compare.to_csv(OUT / "current_cutoff_subset.csv", index=False)\n    delta.to_csv(OUT / "current_cutoff_delta.csv", index=False)\n\n    current_hardtp_ids = set(current.loc[current.exit_reason == "HARD_TP", "trade_id"].astype(int))\n    hh = d[d.trade_id.isin(current_hardtp_ids)].copy()\n    hardtp_compare = grouped(hh, ["variant"]) if len(hh) else pd.DataFrame()\n    if len(hh):\n        cur_map_h = current.set_index("trade_id").net_return\n        hh["delta_vs_current"] = hh.apply(lambda r: float(r.net_return) - float(cur_map_h.loc[int(r.trade_id)]), axis=1)\n        hardtp_delta = hh.groupby("variant").agg(\n            trades=("trade_id", "size"),\n            avg_delta_vs_current=("delta_vs_current", "mean"),\n            improved_share=("delta_vs_current", lambda z: float((z > 0).mean())),\n            worsened_share=("delta_vs_current", lambda z: float((z < 0).mean())),\n            best_delta=("delta_vs_current", "max"),\n            worst_delta=("delta_vs_current", "min"),\n        ).reset_index()\n    else:\n        hardtp_delta = pd.DataFrame()\n    hardtp_compare.to_csv(OUT / "current_hardtp_subset.csv", index=False)\n    hardtp_delta.to_csv(OUT / "current_hardtp_delta.csv", index=False)\n\n    report = [\n'''
if needle not in s:
    raise SystemExit('PATCH_MISS:report_insert')
s = s.replace(needle, insert, 1)

s = s.replace('"RSI_PULLBACK_V005_EXIT_STUDY_FIX1",','"RSI_PULLBACK_V006_RUNNER_NO_TP",',1)
s = s.replace('        "time90=after_90m + never_locked + exec_close<=entry -> next_1m_open",\n        "vwap60=after_60m + never_locked + exec_close<=entry + signal_5m_close<vwap -> next_1m_open",\n', '        "runner_no_tp=LOCK_1.5_TRAIL_0.7_NO_HARD_TP_CUTOFF_14:55",\n',1)

needle2 = '''        "===== DELTA ON CURRENT-CUTOFF TRADES =====",\n        delta.to_string(index=False) if len(delta) else "NO_DELTA",\n    ]\n'''
repl2 = '''        "===== DELTA ON CURRENT-CUTOFF TRADES =====",\n        delta.to_string(index=False) if len(delta) else "NO_DELTA",\n        "",\n        "===== ORIGINAL CURRENT-HARD-TP TRADES ONLY =====",\n        hardtp_compare.to_string(index=False) if len(hardtp_compare) else "NO_CURRENT_HARDTP_TRADES",\n        "",\n        "===== DELTA ON CURRENT-HARD-TP TRADES =====",\n        hardtp_delta.to_string(index=False) if len(hardtp_delta) else "NO_HARDTP_DELTA",\n    ]\n'''
if needle2 not in s:
    raise SystemExit('PATCH_MISS:report_tail')
s = s.replace(needle2, repl2, 1)

compile(s, str(out), 'exec')
out.write_text(s, encoding='utf-8')
print(f'WROTE={out} bytes={len(s.encode("utf-8"))}')
print('V006_RUNNER_PATCH=PASS')
