#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extended-session feature policy for KR/US research.

Research only / NO_ORDERS. Frozen regular-session signal generation is unchanged.
Extended-session data is treated as an auxiliary information layer until a
separate replay proves that a feature improves robustness out of sample.
"""
from __future__ import annotations

import pandas as pd

MODE = "EXTENDED_SESSION_FEATURE_RESEARCH_NO_ORDERS"
LIVE_APPROVAL = False


def classify_session(ts: pd.Timestamp, market: str) -> str:
    if ts.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    if market == "US":
        t = ts.tz_convert("America/New_York")
        m = t.hour * 60 + t.minute
        if m < 4*60: return "US_OVERNIGHT_00_04"
        if m < 9*60+30: return "US_PRE_04_0930"
        if m < 16*60: return "US_REGULAR_0930_1600"
        if m < 20*60: return "US_AFTER_1600_2000"
        return "US_OVERNIGHT_20_24"
    if market == "KR":
        t = ts.tz_convert("Asia/Seoul")
        m = t.hour * 60 + t.minute
        if m < 9*60: return "KR_BEFORE_0900"
        if m < 15*60+30: return "KR_REGULAR_0900_1530"
        return "KR_AFTER_1530"
    raise ValueError(market)


def session_features(df: pd.DataFrame, market: str) -> dict:
    """Compute descriptive extended-session features without changing core signals."""
    if df.empty:
        return {}
    z = df.copy().sort_index()
    if z.index.tz is None:
        raise ValueError("timezone-aware index required")
    z["session"] = [classify_session(ts, market) for ts in z.index]
    regular_name = "US_REGULAR_0930_1600" if market == "US" else "KR_REGULAR_0900_1530"
    reg = z[z.session == regular_name]
    ext = z[z.session != regular_name]
    out = {
        "market": market,
        "regular_bars": int(len(reg)),
        "extended_bars": int(len(ext)),
        "extended_volume": float(pd.to_numeric(ext.get("volume", 0), errors="coerce").fillna(0).sum()),
    }
    for name, g in z.groupby("session"):
        if len(g):
            op = float(g.open.iloc[0]); cl = float(g.close.iloc[-1])
            hi = float(g.high.max()); lo = float(g.low.min())
            out[f"{name}_return"] = cl/op - 1.0 if op else 0.0
            out[f"{name}_range"] = hi/lo - 1.0 if lo else 0.0
            out[f"{name}_volume"] = float(pd.to_numeric(g.get("volume", 0), errors="coerce").fillna(0).sum())
    return out


def policy() -> dict:
    return {
        "mode": MODE,
        "live_approval": False,
        "core_signal_session": {"KR":"09:00-15:30 KST", "US":"09:30-16:00 ET"},
        "extended_data_role": "AUXILIARY_FEATURE_ONLY",
        "do_not_mix_into_frozen_60m": True,
        "candidate_features": [
            "pre_or_before_return", "pre_or_before_range", "pre_or_before_volume",
            "prior_after_return", "prior_after_range", "prior_after_volume",
            "overnight_return_us", "overnight_volume_us",
            "gap_to_regular_open", "extended_vs_regular_volume_ratio",
        ],
        "promotion_rule": "only after separate causal replay improves PF/MDD/cost robustness without lookahead",
    }


def self_test() -> None:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    assert classify_session(pd.Timestamp("2026-08-10 08:00", tz="America/New_York"), "US") == "US_PRE_04_0930"
    assert classify_session(pd.Timestamp("2026-08-10 17:00", tz="America/New_York"), "US") == "US_AFTER_1600_2000"
    assert classify_session(pd.Timestamp("2026-08-10 16:00", tz="Asia/Seoul"), "KR") == "KR_AFTER_1530"
    p = policy(); assert p["do_not_mix_into_frozen_60m"] is True
    print("EXTENDED_SESSION_FEATURE_POLICY_V001_SELF_TEST=PASS")

if __name__ == "__main__":
    self_test()
