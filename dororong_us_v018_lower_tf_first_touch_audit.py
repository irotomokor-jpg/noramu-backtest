#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lower-timeframe first-touch audit for Dororong v0.18 replay trades.

Does not change frozen signals or reported backtest PnL. The stored 60m
`exit_time` is the START of the model bar in which the exit condition was
observed, so condition exits are scanned through that entire 60m bar using the
best available 1m/2m/5m data. Timed/EOD exits are not assigned a fabricated
intrabar execution price; they are flagged for the future runtime policy.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf

TZ = "America/New_York"
CONDITION_REASONS = {"stop", "target2"}


def _ts(x):
    t = pd.Timestamp(x)
    return t.tz_localize(TZ) if t.tzinfo is None else t.tz_convert(TZ)


def _download(ticker: str, start: pd.Timestamp, end: pd.Timestamp):
    for interval in ("1m", "2m", "5m"):
        try:
            z = yf.download(ticker,
                start=start.tz_convert("UTC").tz_localize(None),
                end=end.tz_convert("UTC").tz_localize(None),
                interval=interval, auto_adjust=False, progress=False,
                prepost=False, threads=False)
            if z is None or z.empty:
                continue
            if isinstance(z.columns, pd.MultiIndex):
                z.columns = z.columns.get_level_values(0)
            z = z.rename(columns=str.lower)
            idx = pd.DatetimeIndex(z.index)
            if idx.tz is None:
                idx = idx.tz_localize("UTC")
            z.index = idx.tz_convert(TZ)
            return interval, z
        except Exception:
            pass
    return "NONE", pd.DataFrame()


def _px(v):
    x = pd.to_numeric(v, errors="coerce")
    return float(x) if np.isfinite(x) else np.nan


def _exit_bar_end(t: pd.Timestamp) -> pd.Timestamp:
    end = t + pd.Timedelta(minutes=60)
    close = t.normalize() + pd.Timedelta(hours=16)
    return min(end, close)


def audit_trade(r: pd.Series, interval: str, z: pd.DataFrame) -> dict:
    entry_t = _ts(r["entry_time"])
    model_exit_t = _ts(r["exit_time"])
    model_reason = str(r.get("exit_reason", ""))
    scan_end = _exit_bar_end(model_exit_t)
    entry = _px(r.get("first_entry"))
    stop = _px(r.get("structural_stop"))
    t1 = _px(r.get("target1")); t2 = _px(r.get("target2"))

    q = z[(z.index >= entry_t) & (z.index < scan_end)] if not z.empty else pd.DataFrame()
    t1_taken = False; pending_be = False; t1_time = None
    first_exit_t = None; first_exit_px = np.nan; first_exit_reason = ""
    ambiguous = ""

    for ts, b in q.iterrows():
        o, h, l = _px(b.get("open")), _px(b.get("high")), _px(b.get("low"))
        if pending_be:
            stop = entry
            pending_be = False
        stop_hit = np.isfinite(stop) and np.isfinite(l) and l <= stop
        if not t1_taken:
            t1_hit = np.isfinite(t1) and np.isfinite(h) and h >= t1
            if stop_hit and t1_hit:
                first_exit_t, first_exit_px = ts, (o if np.isfinite(o) and o < stop else stop)
                first_exit_reason = "STOP_FIRST_AMBIGUOUS_T1"; ambiguous = "STOP_AND_T1_SAME_BAR"; break
            if stop_hit:
                first_exit_t, first_exit_px, first_exit_reason = ts, (o if np.isfinite(o) and o < stop else stop), "STRUCTURAL_STOP"
                break
            if t1_hit:
                t1_taken = True; t1_time = ts; pending_be = True
                if np.isfinite(t2) and np.isfinite(h) and h >= t2:
                    ambiguous = "T1_AND_T2_SAME_BAR_DEFER_T2"
                continue
        else:
            t2_hit = np.isfinite(t2) and np.isfinite(h) and h >= t2
            if stop_hit and t2_hit:
                first_exit_t, first_exit_px = ts, (o if np.isfinite(o) and o < stop else stop)
                first_exit_reason = "STOP_FIRST_AMBIGUOUS_T2"; ambiguous = "STOP_AND_T2_SAME_BAR"; break
            if stop_hit:
                first_exit_t, first_exit_px, first_exit_reason = ts, (o if np.isfinite(o) and o < stop else stop), "BE_STOP_AFTER_T1"
                break
            if t2_hit:
                first_exit_t, first_exit_px, first_exit_reason = ts, t2, "TARGET2"
                break

    if first_exit_t is None:
        if model_reason in CONDITION_REASONS:
            first_exit_reason = "CONDITION_NOT_RESOLVED_IN_LOWER_TF_BAR"
        elif model_reason in {"time", "eod_final"}:
            first_exit_reason = "TIMED_EXIT_POLICY_REQUIRED"
        else:
            first_exit_reason = "NO_FIRST_TOUCH_EXIT"

    model_px = _px(r.get("exit_price", r.get("exit_raw_price")))
    price_diff_bps = ((first_exit_px / model_px - 1.0) * 10000.0
                      if np.isfinite(first_exit_px) and np.isfinite(model_px) and model_px else np.nan)
    time_from_model_bar_start = ((first_exit_t - model_exit_t).total_seconds()/60.0
                                 if first_exit_t is not None else np.nan)

    return {
        "ticker": r.get("ticker", ""), "setup_id": r.get("setup_id", ""),
        "intraday_interval": interval, "entry_time": str(entry_t), "entry_price": entry,
        "structural_stop": _px(r.get("structural_stop")), "target1": t1, "target2": t2,
        "target1_first_touch_time": str(t1_time) if t1_time is not None else "",
        "lower_tf_exit_time": str(first_exit_t) if first_exit_t is not None else "",
        "lower_tf_exit_price": first_exit_px, "lower_tf_exit_reason": first_exit_reason,
        "model_exit_bar_start": str(model_exit_t), "model_exit_bar_end": str(scan_end),
        "model_exit_price": model_px, "model_exit_reason": model_reason,
        "exit_price_diff_bps": price_diff_bps,
        "minutes_after_model_exit_bar_start": time_from_model_bar_start,
        "ambiguity": ambiguous,
    }


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--outdir", default="dororong_us_v018_replay_output")
    a = ap.parse_args(); out = Path(a.outdir); tr = pd.read_csv(out / "trades_5bps.csv")
    rows=[]
    for ticker,g in tr.groupby("ticker"):
        s=min(_ts(x) for x in g["entry_time"])-pd.Timedelta(days=1)
        e=max(_exit_bar_end(_ts(x)) for x in g["exit_time"])+pd.Timedelta(days=1)
        interval,z=_download(str(ticker),s,e)
        for _,r in g.iterrows(): rows.append(audit_trade(r,interval,z))
    df=pd.DataFrame(rows); df.to_csv(out/"lower_tf_first_touch_audit.csv",index=False,encoding="utf-8-sig")
    cond=df[df["model_exit_reason"].isin(CONDITION_REASONS)] if not df.empty else pd.DataFrame()
    summary=pd.DataFrame([{
        "trades":len(df),
        "with_intraday_data":int((df["intraday_interval"]!="NONE").sum()) if not df.empty else 0,
        "condition_exit_trades":len(cond),
        "condition_exits_resolved":int(cond["lower_tf_exit_time"].fillna("").ne("").sum()) if not cond.empty else 0,
        "timed_exit_policy_required":int((df["lower_tf_exit_reason"]=="TIMED_EXIT_POLICY_REQUIRED").sum()) if not df.empty else 0,
        "ambiguous_bars":int((df["ambiguity"].fillna("")!="").sum()) if not df.empty else 0,
        "max_abs_resolved_exit_price_diff_bps":float(pd.to_numeric(cond["exit_price_diff_bps"],errors="coerce").abs().max()) if not cond.empty else np.nan,
    }])
    summary.to_csv(out/"lower_tf_first_touch_summary.csv",index=False,encoding="utf-8-sig")
    print(summary.to_string(index=False))

if __name__=="__main__": main()
