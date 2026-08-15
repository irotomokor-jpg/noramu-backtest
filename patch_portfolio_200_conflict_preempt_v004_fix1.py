#!/usr/bin/env python3
from pathlib import Path

SRC = Path('portfolio_200_conflict_preempt_replay_v004.py')
DST = Path('portfolio_200_conflict_preempt_replay_v004_fix1.py')
s = SRC.read_text(encoding='utf-8')

s = s.replace('OUT = Path("portfolio_200_conflict_preempt_replay_v004")', 'OUT = Path("portfolio_200_conflict_preempt_replay_v004_fix1")', 1)
s = s.replace('"PORTFOLIO_200_CONFLICT_PREEMPT_REPLAY_V004",', '"PORTFOLIO_200_CONFLICT_PREEMPT_REPLAY_V004_FIX1",', 1)

old = '''    m = mz.iloc[0]\n    signal_bar_end = m.bar_end_ny\n    if not (signal_bar_end < conflict_ts):\n        raise SystemExit(f"CAUSAL_AUDIT_FAIL signal={signal_bar_end} exec={conflict_ts}")\n\n    raw = read_day(rsi_symbol, pd.Timestamp(conflict_ts).date())\n'''
new = '''    m = mz.iloc[0]\n    signal_bar_end = m.bar_end_ny\n    if conflict_ts < signal_bar_end:\n        raise SystemExit(f"CAUSAL_AUDIT_FAIL_EXEC_BEFORE_BAR_END signal={signal_bar_end} exec={conflict_ts}")\n\n    # STRICT boundary audit.  The execution map uses right-edge 5m labels:\n    # a bar labeled 10:25 is formed only from source 1m bars with timestamps < 10:25,\n    # while the leveraged ETF next-open execution may itself be timestamped 10:25.\n    # Equality is causal only if the source data used by the completed signal bar is strictly earlier.\n    signal_source = {"SOXL": "SOXX", "KORU": "EWY"}.get(frozen_symbol)\n    if signal_source is None:\n        raise SystemExit(f"NO_SIGNAL_SOURCE_FOR_BOUNDARY_AUDIT symbol={frozen_symbol}")\n    sigraw = read_day(signal_source, pd.Timestamp(conflict_ts).date())\n    window_start = signal_bar_end - pd.Timedelta(minutes=5)\n    used = sigraw[(sigraw.ts >= window_start) & (sigraw.ts < signal_bar_end)].copy()\n    if used.empty:\n        raise SystemExit(f"SIGNAL_SOURCE_WINDOW_EMPTY source={signal_source} start={window_start} end={signal_bar_end}")\n    last_signal_source_ts = used.ts.max()\n    if not (last_signal_source_ts < conflict_ts):\n        raise SystemExit(f"CAUSAL_SOURCE_AUDIT_FAIL last_source={last_signal_source_ts} exec={conflict_ts}")\n    same_boundary_next_open = bool(signal_bar_end == conflict_ts)\n\n    raw = read_day(rsi_symbol, pd.Timestamp(conflict_ts).date())\n'''
if old not in s:
    raise SystemExit('PATCH_MISS causal block')
s = s.replace(old, new, 1)

old_fields = '''        "frozen_signal_bar_end": signal_bar_end.isoformat(),\n        "frozen_entry_ts": conflict_ts.isoformat(),\n'''
new_fields = '''        "frozen_signal_bar_end": signal_bar_end.isoformat(),\n        "last_signal_source_ts": last_signal_source_ts.isoformat(),\n        "same_boundary_next_open": int(same_boundary_next_open),\n        "frozen_entry_ts": conflict_ts.isoformat(),\n'''
if old_fields not in s:
    raise SystemExit('PATCH_MISS output fields')
s = s.replace(old_fields, new_fields, 1)

old_exec = '''        "execution=Frozen signal completed before frozen next_exec; RSI preempt executes at the same raw next 1m OPEN before Frozen buy in serial order engine",\n'''
new_exec = '''        "execution=Frozen completed 5m signal uses only source 1m timestamps strictly before the next-open boundary; RSI preempt executes first at that raw boundary OPEN, then Frozen buy in serial-order backtest convention",\n'''
if old_exec not in s:
    raise SystemExit('PATCH_MISS execution report')
s = s.replace(old_exec, new_exec, 1)

old_conf = '''        f"frozen={r.frozen_entry_symbol} signal_bar_end={r.frozen_signal_bar_end} entry_ts={r.frozen_entry_ts}",\n'''
new_conf = '''        f"frozen={r.frozen_entry_symbol} signal_bar_end={r.frozen_signal_bar_end} entry_ts={r.frozen_entry_ts}",\n        f"last_signal_source_ts={r.last_signal_source_ts} same_boundary_next_open={bool(r.same_boundary_next_open)}",\n'''
if old_conf not in s:
    raise SystemExit('PATCH_MISS conflict report')
s = s.replace(old_conf, new_conf, 1)

old_audit = '''        f"signal_before_execution={signal_bar_end < conflict_ts}",\n        f"preempt_at_frozen_exec_open={preempt_ts == conflict_ts}",\n'''
new_audit = '''        f"bar_end_le_execution={signal_bar_end <= conflict_ts}",\n        f"signal_source_strictly_before_execution={last_signal_source_ts < conflict_ts}",\n        f"same_boundary_next_open={same_boundary_next_open}",\n        f"preempt_at_frozen_exec_open={preempt_ts == conflict_ts}",\n'''
if old_audit not in s:
    raise SystemExit('PATCH_MISS audit report')
s = s.replace(old_audit, new_audit, 1)

compile(s, str(DST), 'exec')
DST.write_text(s, encoding='utf-8')
print(f'WROTE={DST} bytes={len(s.encode("utf-8"))}')
print('V004_FIX1_PATCH=PASS')
