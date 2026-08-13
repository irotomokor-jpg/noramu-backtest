#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sparse-timeline hotfix for unified KR strict 1m execution.

Research only / NO_ORDERS.

The v001 engine correctly rejects missing raw prices when an adjusted row exists,
but it incorrectly required every open symbol to have a row at every timestamp in
the *global* multi-symbol timeline. Individual symbols can legitimately omit a
minute while other symbols trade. In that case the position must carry forward
until that symbol's next available 1m bar.

This wrapper patches only that Phase-A invariant. It still fails closed when the
symbol's cached candidate window has actually been exhausted while the position
remains open.
"""
from __future__ import annotations

import argparse
import inspect
import textwrap

import numpy as np
import pandas as pd

import toss_unified_kr_strict_execution_v001 as v1

MODE = "TOSS_UNIFIED_KR_STRICT_1M_EXECUTION_V002_NO_ORDERS"
LIVE_APPROVAL = False

_OLD_LOOP = '''    for ts,g in timeline.groupby("timestamp_utc",sort=True):\n'''
_NEW_LOOP = '''    symbol_last_ts = {\n        str(sym).zfill(6): pd.to_datetime(g["timestamp_utc"], utc=True).max()\n        for sym, g in timeline.groupby("symbol", sort=False)\n    }\n\n    for ts,g in timeline.groupby("timestamp_utc",sort=True):\n'''

_OLD_GAP = '''            if sym not in rows:\n                raise RuntimeError(f"RAW_WINDOW_GAP open position {sym} at {ts}")\n'''
_NEW_GAP = '''            if sym not in rows:\n                # A global timestamp may exist only because another symbol traded.\n                # Carry this position until its own next available 1m bar.  If this\n                # timestamp is already beyond the symbol's cached window, fail\n                # closed rather than silently marking forever with a stale price.\n                last_ts = symbol_last_ts.get(sym)\n                if last_ts is None or ts > last_ts:\n                    raise RuntimeError(f"RAW_WINDOW_EXHAUSTED open position {sym} at {ts} last={last_ts}")\n                continue\n'''


def _build_sparse_safe_simulate():
    src = textwrap.dedent(inspect.getsource(v1.simulate))
    if src.count(_OLD_LOOP) != 1:
        raise RuntimeError("v001 loop patch anchor changed")
    if src.count(_OLD_GAP) != 1:
        raise RuntimeError("v001 gap patch anchor changed")
    src = src.replace(_OLD_LOOP, _NEW_LOOP, 1).replace(_OLD_GAP, _NEW_GAP, 1)
    ns = {}
    exec(compile(src, "<unified_strict_v002_sparse_patch>", "exec"), v1.__dict__, ns)
    return ns["simulate"]


simulate = _build_sparse_safe_simulate()


def run(a):
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    old = v1.simulate
    try:
        v1.simulate = simulate
        out = v1.run(a)
    finally:
        v1.simulate = old
    out["mode"] = MODE
    out["sparse_symbol_minute_policy"] = "CARRY_TO_NEXT_OWN_1M_BAR_FAIL_IF_WINDOW_EXHAUSTED"
    return out


class _Ex:
    TOSS_KRX_COMMISSION = 0.0
    @staticmethod
    def adverse_ticks(px, side, ticks): return float(px)
    @staticmethod
    def tax_components(market, ts): return (0.0, 0.0)


def _args():
    a = v1.base.frozen_args()
    a.max_hold = 26
    return a


def self_test() -> None:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False

    # Symbol A enters at 10:00. At 10:01 only symbol B has a bar. A returns at
    # 10:02 and hits its stop. v001 raised RAW_WINDOW_GAP at 10:01; v002 must
    # carry A unchanged until its own next bar and then execute causally.
    t0 = pd.Timestamp("2026-01-02 10:00", tz="Asia/Seoul").tz_convert("UTC")
    t1 = t0 + pd.Timedelta(minutes=1)
    t2 = t0 + pd.Timedelta(minutes=2)
    tl = pd.DataFrame([
        {"timestamp_utc":t0,"symbol":"123456","a_open":100.0,"a_high":101.0,"a_low":99.0,"a_close":100.0,
         "r_open":100.0,"r_high":101.0,"r_low":99.0,"r_close":100.0,"scale":1.0},
        {"timestamp_utc":t1,"symbol":"654321","a_open":50.0,"a_high":50.0,"a_low":50.0,"a_close":50.0,
         "r_open":50.0,"r_high":50.0,"r_low":50.0,"r_close":50.0,"scale":1.0},
        {"timestamp_utc":t2,"symbol":"123456","a_open":100.0,"a_high":100.0,"a_low":94.0,"a_close":95.0,
         "r_open":100.0,"r_high":100.0,"r_low":94.0,"r_close":95.0,"scale":1.0},
    ])
    cand = pd.DataFrame([{
        "sleeve":"KR_KOSDAQ","exchange":"KOSDAQ","ticker":"123456.KQ","symbol":"123456","name":"X",
        "setup_id":"S","entry_time":t0.isoformat(),"adjusted_stop":95.0,"trail_pct":.05,
        "trail_samples":10,"fast_regime_pass":True,
    }])
    res = simulate(tl, cand, starting_equity=5_000_000, slippage_ticks=0, ex=_Ex, args=_args())
    assert len(res.trades) == 1
    assert str(res.trades.iloc[0].exit_reason) == "stop_1m"
    assert pd.Timestamp(res.trades.iloc[0].exit_time) == t2

    # Existing v001 market-aware tax self-test must still pass under patched simulate.
    old = v1.simulate
    try:
        v1.simulate = simulate
        v1.self_test()
    finally:
        v1.simulate = old
    print("TOSS_UNIFIED_KR_STRICT_EXECUTION_V002_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--candidates", default="toss_unified_kr_candidate_compile_v002/unified_kr_candidates_2026.csv")
    ap.add_argument("--outdir", default="toss_unified_kr_strict_execution_v002")
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test(); return
    run(a)


if __name__ == "__main__":
    main()
