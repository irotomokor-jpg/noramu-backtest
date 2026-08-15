#!/usr/bin/env python3
from pathlib import Path

p = Path("fast_rebound_v001_research.py")
s = p.read_text(encoding="utf-8")

old1 = '''                    used_signal_until = int(np.searchsorted(sx.ts.to_numpy(), np.datetime64(exit_ts.tz_convert("UTC").tz_localize(None).to_datetime64()), side="right") - 1)\n'''
new1 = '''                    used_signal_until = int(sx["ts"].searchsorted(exit_ts, side="right") - 1)\n'''
if old1 in s:
    s = s.replace(old1, new1, 1)

old2 = '''            qqq_daily_range = sig.groupby("trade_date").apply(lambda g: float(g.high.max() / g.low.min() - 1.0), include_groups=False).rename("qqq_range").reset_index()\n            qqq_daily_range["trade_date"] = qqq_daily_range.trade_date.astype(str)\n'''
new2 = '''            qqq_daily_range = sig.groupby("trade_date", as_index=False).agg(day_high=("high", "max"), day_low=("low", "min"))\n            qqq_daily_range["qqq_range"] = qqq_daily_range.day_high / qqq_daily_range.day_low - 1.0\n            qqq_daily_range = qqq_daily_range[["trade_date", "qqq_range"]].copy()\n            qqq_daily_range["trade_date"] = qqq_daily_range.trade_date.astype(str)\n'''
if old2 in s:
    s = s.replace(old2, new2, 1)

compile(s, str(p), "exec")
p.write_text(s, encoding="utf-8")
print("FAST_REBOUND_V001_FIX1=PASS")
print("FIX=timezone_searchsorted_and_pandas_groupby_compat")
