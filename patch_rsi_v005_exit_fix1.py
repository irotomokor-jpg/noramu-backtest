#!/usr/bin/env python3
from pathlib import Path

src = Path('rsi_v005_exit_study.py')
out = Path('rsi_v005_exit_study_fix1.py')
s = src.read_text(encoding='utf-8')

s = s.replace('OUT = Path("rsi_pullback_v005_exit_study")','OUT = Path("rsi_pullback_v005_exit_study_fix1")',1)

start = s.index('def read_day(symbol: str, day: str) -> pd.DataFrame:')
end = s.index('\n\ndef bars5', start)
new_read = r'''def read_day(symbol: str, day: str) -> pd.DataFrame:
    # DB timestamps are stored with an offset that can cross the US trade date at KST midnight.
    # Read a wider raw window, then select the exact America/New_York trade date after parsing.
    target = pd.Timestamp(day).date()
    anchor = pd.Timestamp(day)
    q_start = (anchor - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    q_end = (anchor + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    con = sqlite3.connect(DB)
    q = "SELECT timestamp, open, high, low, close, volume FROM candles WHERE symbol=? AND timestamp>=? AND timestamp<? ORDER BY timestamp"
    d = pd.read_sql_query(q, con, params=[symbol, q_start, q_end])
    con.close()
    if d.empty:
        return d
    d["ts"] = parse_ts(d["timestamp"])
    for c in ["open", "high", "low", "close", "volume"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d[d.ts.dt.date == target].copy()
    mins = d.ts.dt.hour * 60 + d.ts.dt.minute
    d = d[(mins >= 570) & (mins < 960)].dropna(subset=["ts", "open", "high", "low", "close"])
    return d.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)'''
s = s[:start] + new_read + s[end:]

needle = '''    audit = current.net_return.to_numpy() - current.frozen_current_net.to_numpy()\n    audit_max = float(np.max(np.abs(audit))) if len(audit) else np.nan\n'''
replacement = '''    audit = current.net_return.to_numpy() - current.frozen_current_net.to_numpy()\n    audit_max = float(np.max(np.abs(audit))) if len(audit) else np.nan\n    expected = len(base)\n    replayed = len(current)\n    reason_mismatch = int((current.exit_reason.astype(str).to_numpy() != current.frozen_current_reason.astype(str).to_numpy()).sum()) if replayed == expected else -1\n    print(f"AUDIT expected={expected} replayed={replayed} max_abs_diff={audit_max:.12g} reason_mismatch={reason_mismatch}", flush=True)\n    if replayed != expected:\n        raise SystemExit(f"AUDIT_FAIL_COUNT expected={expected} replayed={replayed}")\n    if (not np.isfinite(audit_max)) or audit_max > 1e-10:\n        raise SystemExit(f"AUDIT_FAIL_RETURN max_abs_diff={audit_max}")\n    if reason_mismatch != 0:\n        raise SystemExit(f"AUDIT_FAIL_REASON mismatches={reason_mismatch}")\n'''
if needle not in s:
    raise SystemExit('PATCH_MISS:audit')
s = s.replace(needle, replacement, 1)

s = s.replace('RSI_PULLBACK_V005_EXIT_STUDY','RSI_PULLBACK_V005_EXIT_STUDY_FIX1')
compile(s, str(out), 'exec')
out.write_text(s, encoding='utf-8')
print(f'WROTE={out} bytes={len(s.encode("utf-8"))}')
print('V005_FIX1_PATCH=PASS')
