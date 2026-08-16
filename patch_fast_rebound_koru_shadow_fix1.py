#!/usr/bin/env python3
from pathlib import Path

P = Path(__file__).resolve().parent / "fast_rebound_koru_v1_shadow_runtime.py"
if not P.exists():
    raise SystemExit(f"MISSING={P}")
s = P.read_text(encoding="utf-8")
old = '''    if now >= cutoff:\n        rr = next_raw_open(raw, cutoff)\n        if rr is not None and et_ts(rr.ts) <= now:\n            return close_position(ledger, pos, rr, "CUTOFF", fee_side)\n\n    if now >= time_exit:\n        rr = next_raw_open(raw, time_exit)\n        if rr is not None and et_ts(rr.ts) <= now:\n            return close_position(ledger, pos, rr, "TIME", fee_side)\n\n    last_processed = et_ts(pos["last_processed_exec_bar_ts"]) if pos.get("last_processed_exec_bar_ts") else entry_ts - pd.Timedelta(minutes=1)\n    for _, r in done[done.ts > last_processed].iterrows():\n        ts = et_ts(r.ts)\n        stop_hit = float(r.low) <= stop_level\n        tp_hit = float(r.high) >= tp_level\n        if stop_hit or tp_hit:\n            reason = "STOP" if stop_hit else "TP"\n            after = ts + pd.Timedelta(minutes=1)\n            rr = next_raw_open(raw, after)\n            if rr is not None and et_ts(rr.ts) <= now:\n                return close_position(ledger, pos, rr, reason, fee_side)\n            pos["pending_exit_reason"] = reason\n            pos["pending_exit_after_ts"] = after.isoformat()\n            pos["last_processed_exec_bar_ts"] = ts.isoformat()\n            emit("EXIT_ARMED", reason=reason, execute_after_ts=after.isoformat(), entry_ts=pos["entry_ts"])\n            return {"action": "EXIT_ARMED", "reason": reason, "execute_after_ts": after.isoformat()}\n        pos["last_processed_exec_bar_ts"] = ts.isoformat()\n    return None\n'''
new = '''    last_processed = et_ts(pos["last_processed_exec_bar_ts"]) if pos.get("last_processed_exec_bar_ts") else entry_ts - pd.Timedelta(minutes=1)\n    for _, r in raw[raw.ts > last_processed].iterrows():\n        ts = et_ts(r.ts)\n        if ts > now:\n            break\n        if ts >= cutoff:\n            return close_position(ledger, pos, r, "CUTOFF", fee_side)\n        if ts >= time_exit:\n            return close_position(ledger, pos, r, "TIME", fee_side)\n        if ts + pd.Timedelta(minutes=1) > now:\n            break\n        stop_hit = float(r.low) <= stop_level\n        tp_hit = float(r.high) >= tp_level\n        if stop_hit or tp_hit:\n            reason = "STOP" if stop_hit else "TP"\n            after = ts + pd.Timedelta(minutes=1)\n            rr = next_raw_open(raw, after)\n            if rr is not None and et_ts(rr.ts) <= now:\n                return close_position(ledger, pos, rr, reason, fee_side)\n            pos["pending_exit_reason"] = reason\n            pos["pending_exit_after_ts"] = after.isoformat()\n            pos["last_processed_exec_bar_ts"] = ts.isoformat()\n            emit("EXIT_ARMED", reason=reason, execute_after_ts=after.isoformat(), entry_ts=pos["entry_ts"])\n            return {"action": "EXIT_ARMED", "reason": reason, "execute_after_ts": after.isoformat()}\n        pos["last_processed_exec_bar_ts"] = ts.isoformat()\n    return None\n'''
if old not in s:
    if new in s:
        print("FAST_REBOUND_KORU_SHADOW_FIX1=ALREADY_APPLIED")
    else:
        raise SystemExit("PATCH_TARGET_NOT_FOUND")
else:
    P.write_text(s.replace(old, new, 1), encoding="utf-8")
    print("FAST_REBOUND_KORU_SHADOW_FIX1=PASS")
print("FIX=preserve_historical_exit_order_stop_tp_before_later_time_boundary")
print("ORDER_WRITES=OFF")
