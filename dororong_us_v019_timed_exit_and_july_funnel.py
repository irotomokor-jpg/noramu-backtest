#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dororong v0.19: execution-policy + July scarcity audit.

No strategy thresholds are changed. This script only:
1) compares causal execution policies for model TIME exits,
2) marks replay-boundary EOD_FINAL as CARRY (not a live exit),
3) measures the July signal funnel from raw DORO_AGG setups through BULL gate
   and portfolio risk acceptance.

Research/shadow only. NO ORDERS.
"""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf

TZ = "America/New_York"
OUT = Path("dororong_us_v019_audit_output")
SRC = Path("dororong_us_v018_replay_output")


def ts(x):
    t = pd.Timestamp(x)
    return t.tz_localize(TZ) if t.tzinfo is None else t.tz_convert(TZ)


def px(v):
    x = pd.to_numeric(v, errors="coerce")
    return float(x) if np.isfinite(x) else np.nan


def dl(ticker, start, end):
    for interval in ("1m", "2m", "5m"):
        try:
            z = yf.download(ticker, start=start.tz_convert("UTC").tz_localize(None),
                            end=end.tz_convert("UTC").tz_localize(None), interval=interval,
                            auto_adjust=False, progress=False, prepost=False, threads=False)
            if z is None or z.empty:
                continue
            if isinstance(z.columns, pd.MultiIndex): z.columns = z.columns.get_level_values(0)
            z = z.rename(columns=str.lower)
            idx = pd.DatetimeIndex(z.index)
            if idx.tz is None: idx = idx.tz_localize("UTC")
            z.index = idx.tz_convert(TZ)
            return interval, z
        except Exception:
            pass
    return "NONE", pd.DataFrame()


def timed_exit_audit():
    tr = pd.read_csv(SRC / "trades_5bps.csv")
    timed = tr[tr["exit_reason"].astype(str).str.lower().isin(["time", "eod_final"])].copy()
    rows = []
    for ticker, g in timed.groupby("ticker"):
        s = min(ts(x) for x in g.exit_time) - pd.Timedelta(hours=2)
        e = max(ts(x) for x in g.exit_time) + pd.Timedelta(days=3)
        interval, z = dl(str(ticker), s, e)
        for _, r in g.iterrows():
            start = ts(r.exit_time)
            reason = str(r.exit_reason).lower()
            # A 60m timestamp is the START of the model bar. Its decision can only
            # be known after the bar completes. Regular session is capped at 16:00.
            session_close = start.normalize() + pd.Timedelta(hours=16)
            bar_end = min(start + pd.Timedelta(hours=1), session_close)
            q = z[(z.index >= start) & (z.index < bar_end)] if not z.empty else pd.DataFrame()
            bar_close_proxy = px(q.iloc[-1].get("close")) if not q.empty else np.nan
            qnext = z[z.index >= bar_end] if not z.empty else pd.DataFrame()
            next_open = px(qnext.iloc[0].get("open")) if not qnext.empty else np.nan
            next_time = str(qnext.index[0]) if not qnext.empty else ""
            model_px = px(r.get("exit_price", r.get("exit_raw_price")))

            if reason == "time":
                recommended = "NEXT_LOWER_TF_OPEN_AFTER_COMPLETED_60M_BAR"
                recommended_px = next_open
                recommended_time = next_time
            else:
                # eod_final exists because the historical replay dataset ended.
                # A live engine must carry the position unless the strategy itself
                # has an EOD liquidation rule; v0.16 does not.
                recommended = "CARRY_POSITION_REPLAY_BOUNDARY_NOT_LIVE_EXIT"
                recommended_px = np.nan
                recommended_time = ""

            rows.append({
                "ticker": ticker, "setup_id": r.get("setup_id", ""), "model_exit_reason": reason,
                "intraday_interval": interval, "model_exit_bar_start": str(start),
                "model_exit_bar_end": str(bar_end), "model_exit_price": model_px,
                "completed_60m_bar_close_proxy": bar_close_proxy,
                "next_lower_tf_open_time": next_time, "next_lower_tf_open": next_open,
                "close_proxy_vs_model_bps": ((bar_close_proxy/model_px)-1)*10000 if np.isfinite(bar_close_proxy) and model_px else np.nan,
                "next_open_vs_model_bps": ((next_open/model_px)-1)*10000 if np.isfinite(next_open) and model_px else np.nan,
                "recommended_policy": recommended, "recommended_time": recommended_time,
                "recommended_price": recommended_px,
            })
    df = pd.DataFrame(rows)
    df.to_csv(OUT/"timed_exit_policy_audit.csv", index=False, encoding="utf-8-sig")
    time_rows = df[df.model_exit_reason == "time"] if not df.empty else df
    summary = {
        "timed_or_boundary_rows": int(len(df)),
        "true_time_exits": int((df.model_exit_reason == "time").sum()) if not df.empty else 0,
        "replay_boundary_eod_final": int((df.model_exit_reason == "eod_final").sum()) if not df.empty else 0,
        "time_rows_with_intraday": int((time_rows.intraday_interval != "NONE").sum()) if not time_rows.empty else 0,
        "max_abs_next_open_vs_model_bps": float(pd.to_numeric(time_rows.next_open_vs_model_bps, errors="coerce").abs().max()) if not time_rows.empty else np.nan,
        "recommended_time_exit": "NEXT_LOWER_TF_OPEN_AFTER_COMPLETED_60M_BAR",
        "recommended_eod_final": "CARRY_POSITION_REPLAY_BOUNDARY_NOT_LIVE_EXIT",
    }
    pd.DataFrame([summary]).to_csv(OUT/"timed_exit_policy_summary.csv", index=False, encoding="utf-8-sig")
    return summary


def july_funnel():
    gate = pd.read_csv(SRC/"bull_gate_audit.csv")
    gate["et"] = pd.to_datetime(gate["time"], utc=True, errors="coerce").dt.tz_convert(TZ)
    j = gate[(gate.et >= pd.Timestamp("2026-07-01", tz=TZ)) & (gate.et < pd.Timestamp("2026-08-01", tz=TZ))].copy()

    replay = pd.read_csv(SRC/"setup_replay_audit.csv")
    replay["et"] = pd.to_datetime(replay.get("entry_time"), utc=True, errors="coerce").dt.tz_convert(TZ)
    jr = replay[(replay.et >= pd.Timestamp("2026-07-01", tz=TZ)) & (replay.et < pd.Timestamp("2026-08-01", tz=TZ))].copy()

    trades = pd.read_csv(SRC/"trades_5bps.csv")
    trades["et"] = pd.to_datetime(trades.entry_time, utc=True, errors="coerce").dt.tz_convert(TZ)
    jt = trades[(trades.et >= pd.Timestamp("2026-07-01", tz=TZ)) & (trades.et < pd.Timestamp("2026-08-01", tz=TZ))].copy()

    rejects = pd.read_csv(SRC/"rejects_5bps.csv")
    rejects["et"] = pd.to_datetime(rejects.time, utc=True, errors="coerce").dt.tz_convert(TZ)
    jj = rejects[(rejects.et >= pd.Timestamp("2026-07-01", tz=TZ)) & (rejects.et < pd.Timestamp("2026-08-01", tz=TZ))].copy()

    raw = int(len(j)); bull = int(pd.to_numeric(j.kept, errors="coerce").fillna(0).astype(int).sum())
    replay_keep = int((jr.decision == "KEEP_REPLAY").sum()) if "decision" in jr else int(len(jr))
    accepted = int(len(jt)); risk_reject = int(len(jj))
    rows = [
        {"stage":"RAW_DORO_AGG_SETUP_IN_JULY","count":raw,"conversion_from_prior":1.0},
        {"stage":"BULL_GATE_PASS","count":bull,"conversion_from_prior":bull/raw if raw else np.nan},
        {"stage":"ENTRY_IN_REPLAY_WINDOW","count":replay_keep,"conversion_from_prior":replay_keep/bull if bull else np.nan},
        {"stage":"RISK_ACCEPTED_AND_TRADED","count":accepted,"conversion_from_prior":accepted/replay_keep if replay_keep else np.nan},
        {"stage":"PORTFOLIO_RISK_REJECT","count":risk_reject,"conversion_from_prior":risk_reject/replay_keep if replay_keep else np.nan},
    ]
    pd.DataFrame(rows).to_csv(OUT/"july_signal_funnel.csv", index=False, encoding="utf-8-sig")
    states = j.groupby("market_60m_state").size().reset_index(name="raw_setups") if not j.empty else pd.DataFrame(columns=["market_60m_state","raw_setups"])
    states.to_csv(OUT/"july_raw_setup_market_states.csv", index=False, encoding="utf-8-sig")
    return {"raw_july_setups":raw,"bull_pass":bull,"replay_entry_candidates":replay_keep,"trades":accepted,"risk_rejects":risk_reject}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    a = timed_exit_audit(); b = july_funnel()
    score = {"version":"DORORONG_V019_TIMED_EXIT_AND_JULY_SCARCITY_AUDIT",
             "purpose":"EXECUTION_POLICY_AND_SIGNAL_FUNNEL_DIAGNOSTIC_NOT_TUNING",
             "live_approval":False,"order_mode":"NO_ORDERS","timed_exit":a,"july_funnel":b}
    (OUT/"scorecard.json").write_text(json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(score, ensure_ascii=False, indent=2))

if __name__ == "__main__": main()
