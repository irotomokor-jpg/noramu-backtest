#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lower-timeframe first-touch audit for Dororong v0.18 replay trades.

This does NOT change the frozen signal model or its reported PnL. It scans the
best available 1m/2m/5m bars chronologically after each model entry to test the
execution path: structural stop, target1 -> break-even stop, target2, then
model time/EOD fallback. Same-bar stop/target ambiguity is resolved stop-first.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf

TZ = "America/New_York"


def _ts(x):
    t = pd.Timestamp(x)
    return t.tz_localize(TZ) if t.tzinfo is None else t.tz_convert(TZ)


def _download(ticker: str, start: pd.Timestamp, end: pd.Timestamp):
    for interval in ("1m", "2m", "5m"):
        try:
            z = yf.download(
                ticker,
                start=start.tz_convert("UTC").tz_localize(None),
                end=end.tz_convert("UTC").tz_localize(None),
                interval=interval,
                auto_adjust=False,
                progress=False,
                prepost=False,
                threads=False,
            )
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


def audit_trade(r: pd.Series, interval: str, z: pd.DataFrame) -> dict:
    entry_t = _ts(r["entry_time"])
    model_exit_t = _ts(r["exit_time"])
    entry = _px(r.get("first_entry"))
    stop = _px(r.get("structural_stop"))
    t1 = _px(r.get("target1"))
    t2 = _px(r.get("target2"))

    q = z[(z.index >= entry_t) & (z.index <= model_exit_t)] if not z.empty else pd.DataFrame()
    t1_taken = False
    pending_be = False
    t1_time = None
    first_exit_t = None
    first_exit_px = np.nan
    first_exit_reason = "NO_INTRADAY_DATA" if q.empty else "MODEL_EXIT_FALLBACK"
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
                first_exit_t = ts
                first_exit_px = o if np.isfinite(o) and o < stop else stop
                first_exit_reason = "STOP_FIRST_AMBIGUOUS_T1"
                ambiguous = "STOP_AND_T1_SAME_BAR"
                break
            if stop_hit:
                first_exit_t = ts
                first_exit_px = o if np.isfinite(o) and o < stop else stop
                first_exit_reason = "STRUCTURAL_STOP"
                break
            if t1_hit:
                t1_taken = True
                t1_time = ts
                pending_be = True  # BE stop becomes active on the next lower-TF bar.
                if np.isfinite(t2) and np.isfinite(h) and h >= t2:
                    ambiguous = "T1_AND_T2_SAME_BAR_DEFER_T2"
                continue
        else:
            t2_hit = np.isfinite(t2) and np.isfinite(h) and h >= t2
            if stop_hit and t2_hit:
                first_exit_t = ts
                first_exit_px = o if np.isfinite(o) and o < stop else stop
                first_exit_reason = "STOP_FIRST_AMBIGUOUS_T2"
                ambiguous = "STOP_AND_T2_SAME_BAR"
                break
            if stop_hit:
                first_exit_t = ts
                first_exit_px = o if np.isfinite(o) and o < stop else stop
                first_exit_reason = "BE_STOP_AFTER_T1"
                break
            if t2_hit:
                first_exit_t = ts
                first_exit_px = t2
                first_exit_reason = "TARGET2"
                break

    if first_exit_t is None and not q.empty:
        q2 = z[z.index >= model_exit_t]
        if not q2.empty:
            first_exit_t = q2.index[0]
            o = _px(q2.iloc[0].get("open"))
            first_exit_px = o
            first_exit_reason = "MODEL_TIME_OR_EOD_OPEN"

    model_px = _px(r.get("exit_price", r.get("exit_raw_price")))
    price_diff_bps = (first_exit_px / model_px - 1.0) * 10000.0 if np.isfinite(first_exit_px) and np.isfinite(model_px) and model_px else np.nan
    time_diff_min = (first_exit_t - model_exit_t).total_seconds() / 60.0 if first_exit_t is not None else np.nan

    return {
        "ticker": r.get("ticker", ""),
        "setup_id": r.get("setup_id", ""),
        "intraday_interval": interval,
        "entry_time": str(entry_t),
        "entry_price": entry,
        "structural_stop": _px(r.get("structural_stop")),
        "target1": t1,
        "target2": t2,
        "target1_first_touch_time": str(t1_time) if t1_time is not None else "",
        "lower_tf_exit_time": str(first_exit_t) if first_exit_t is not None else "",
        "lower_tf_exit_price": first_exit_px,
        "lower_tf_exit_reason": first_exit_reason,
        "model_exit_time": str(model_exit_t),
        "model_exit_price": model_px,
        "model_exit_reason": r.get("exit_reason", ""),
        "exit_price_diff_bps": price_diff_bps,
        "exit_time_diff_minutes": time_diff_min,
        "ambiguity": ambiguous,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="dororong_us_v018_replay_output")
    a = ap.parse_args()
    out = Path(a.outdir)
    tr = pd.read_csv(out / "trades_5bps.csv")
    rows = []
    for ticker, g in tr.groupby("ticker"):
        s = min(_ts(x) for x in g["entry_time"]) - pd.Timedelta(days=1)
        e = max(_ts(x) for x in g["exit_time"]) + pd.Timedelta(days=1)
        interval, z = _download(str(ticker), s, e)
        for _, r in g.iterrows():
            rows.append(audit_trade(r, interval, z))
    df = pd.DataFrame(rows)
    df.to_csv(out / "lower_tf_first_touch_audit.csv", index=False, encoding="utf-8-sig")
    summary = pd.DataFrame([{
        "trades": len(df),
        "with_intraday_data": int((df["intraday_interval"] != "NONE").sum()) if not df.empty else 0,
        "same_exit_reason": int((df["lower_tf_exit_reason"] == df["model_exit_reason"]).sum()) if not df.empty else 0,
        "earlier_than_model_exit": int((pd.to_numeric(df["exit_time_diff_minutes"], errors="coerce") < 0).sum()) if not df.empty else 0,
        "ambiguous_bars": int((df["ambiguity"].fillna("") != "").sum()) if not df.empty else 0,
        "max_abs_exit_price_diff_bps": float(pd.to_numeric(df["exit_price_diff_bps"], errors="coerce").abs().max()) if not df.empty else np.nan,
    }])
    summary.to_csv(out / "lower_tf_first_touch_summary.csv", index=False, encoding="utf-8-sig")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
