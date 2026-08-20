#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Noramu KR v0.35 frozen forward/shadow validator.

No live orders. No parameter tuning.

The observed candidate is frozen from the historical research sequence:
PB_WIDE | FAST | DIRECT | H26 | TRAIL_P70.

Forward observations start 2026-08-11 KST. Every run rebuilds the strategy from
source data, allows only entries whose actual entry timestamp is on/after the
forward start, and records both 1-tick and 3-tick shadow results. Historical
bars before the forward start are used only as lookback for signals/indicators.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import kr_level_rr_v025 as kr
import kr_level_rr_v028_execution_filter as v28
import kr_level_rr_v029_adaptive_exit_entry as v29
import kr_level_rr_v030_regime_robustness as v30
import kr_level_rr_v031_pullback_regime as v31
import kr_level_rr_v032_portfolio_walkforward as v32
import kr_level_rr_v033_dynamic_pit_universe as v33
import kr_level_rr_v0331_dynamic_pit_hotfix as v331  # patches annual marcap loader
import kr_level_rr_v034_dynamic_regime_final as v34

VERSION = "v0.35-KR-FROZEN-FORWARD-SHADOW"
FORWARD_START = pd.Timestamp("2026-08-11 00:00:00", tz=kr.TZ)
FROZEN_CONFIG = "PB_WIDE|FAST|DIRECT|H26|TRAIL_P70"


def filter_forward_candidates(data, candidates):
    out = {}
    audit = []
    for t, cs in candidates.items():
        keep = []
        x = data[t]
        for c in cs:
            ei = int(c.entry_i)
            ts = pd.Timestamp(x.index[ei]) if 0 <= ei < len(x) else pd.NaT
            if pd.notna(ts):
                if ts.tzinfo is None:
                    ts = ts.tz_localize(kr.TZ)
                else:
                    ts = ts.tz_convert(kr.TZ)
            decision = "KEEP_FORWARD" if pd.notna(ts) and ts >= FORWARD_START else "PRE_FORWARD"
            if decision == "KEEP_FORWARD":
                keep.append(c)
            audit.append({"ticker": t, "setup_id": c.setup.setup_id,
                          "entry_time": str(ts) if pd.notna(ts) else "", "decision": decision})
        out[t] = keep
    return out, pd.DataFrame(audit)


def summarize_forward(tr, eq, cap):
    m = v32.summarize_sim(tr, eq, cap)
    if tr is None or tr.empty:
        return {**m, "first_trade": None, "last_trade": None}
    dt = pd.to_datetime(tr.entry_time, utc=True, errors="coerce")
    return {**m, "first_trade": str(dt.min()), "last_trade": str(dt.max())}


def append_history(out: Path, row: dict):
    p = out / "forward_daily_history.csv"
    new = pd.DataFrame([row])
    if p.exists():
        old = pd.read_csv(p)
        if "asof_kst" in old.columns:
            old = old[old.asof_kst.astype(str) != str(row["asof_kst"])]
        new = pd.concat([old, new], ignore_index=True)
    new.to_csv(p, index=False, encoding="utf-8-sig")


def run(args):
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)

    # Correct annual point-in-time top-40 universe. The imported hotfix replaces
    # v33.load_marcap with annual 2023/2024/2025 data loading.
    snapshots = v33.build_snapshots(args.top_n)
    snapshots.to_csv(out / "forward_dynamic_pit_snapshots.csv", index=False, encoding="utf-8-sig")
    meta = v33.union_metadata(snapshots)
    data, setups = v33.download_union(meta, args, out)
    cov = v33.snapshot_coverage(snapshots, data)
    cov.to_csv(out / "forward_snapshot_coverage.csv", index=False, encoding="utf-8-sig")
    if cov.available.min() < args.min_snapshot_coverage:
        raise RuntimeError(f"forward snapshot coverage too low: {cov.to_dict(orient='records')}")
    v30.data_fingerprints(data).to_csv(out / "forward_data_fingerprint.csv", index=False, encoding="utf-8-sig")

    sf, _ = v28.filter_setups(data, setups, args)
    c29, _ = v29.build_candidates(data, sf, "PULLBACK", args)
    cg, _ = v31.actual_entry_gate(data, c29, args, "PB_WIDE")
    dyn_c, membership = v33.filter_dynamic_membership(data, cg, snapshots)
    membership.to_csv(out / "forward_membership_audit.csv", index=False, encoding="utf-8-sig")
    fw_c, fw_audit = filter_forward_candidates(data, dyn_c)
    fw_audit.to_csv(out / "forward_candidate_audit.csv", index=False, encoding="utf-8-sig")

    ks = v31.load_kospi_index(args)
    regime = v34.build_dynamic_full_regime(data, snapshots, ks)

    results = {}
    for cap, slip in ((5_000_000,1),(5_000_000,3),(20_000_000,1),(20_000_000,3)):
        tr, eq, rj, _ = v32.run_sim(data, fw_c, regime, args, cap, slip, "ASC")
        # run_sim is frozen FAST/DIRECT/H26 from v0.32.
        key = f"{cap//1_000_000}m_{slip}t"
        tr.to_csv(out / f"forward_trades_{key}.csv", index=False, encoding="utf-8-sig")
        results[key] = {**summarize_forward(tr, eq, cap), "rejects": int(len(rj))}

    now = pd.Timestamp.now(tz=kr.TZ)
    forward_candidate_count = int(sum(len(v) for v in fw_c.values()))
    score = {
        "version": VERSION,
        "mode": "SHADOW_ONLY_NO_ORDERS",
        "live_approval": False,
        "parameters_frozen": True,
        "frozen_config": FROZEN_CONFIG,
        "forward_start_kst": str(FORWARD_START),
        "asof_kst": str(now),
        "clean_forward_observation": bool(now >= FORWARD_START),
        "forward_candidate_count": forward_candidate_count,
        "results": results,
        "promotion_rule": {
            "minimum_closed_trades_before_review": args.min_forward_trades,
            "minimum_observation_days_before_review": args.min_forward_days,
            "note": "Do not tune parameters from these observations during the freeze window. Review only after both minimums are met."
        }
    }
    (out / "kr_v035_forward_scorecard.json").write_text(json.dumps(score, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    base = results["5m_1t"]
    history_row = {
        "asof_kst": now.strftime("%Y-%m-%d"),
        "run_time_kst": str(now),
        "forward_start_kst": str(FORWARD_START),
        "candidate_count": forward_candidate_count,
        "closed_trades_5m1t": int(base.get("trades",0)),
        "pnl_5m1t": float(base.get("pnl",0.0)),
        "pf_5m1t": float(base.get("pf",np.nan)) if np.isfinite(base.get("pf",np.nan)) else np.nan,
        "dd_5m1t": float(base.get("max_dd_pct",0.0)),
        "pnl_5m3t": float(results["5m_3t"].get("pnl",0.0)),
        "pnl_20m1t": float(results["20m_1t"].get("pnl",0.0)),
        "pnl_20m3t": float(results["20m_3t"].get("pnl",0.0)),
    }
    append_history(out, history_row)
    (out / "RUN_VALIDATION.txt").write_text("PASS\n" + json.dumps(score, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(score, ensure_ascii=False, indent=2, default=str))


def self_test():
    idx = pd.date_range("2026-08-10", periods=3, freq="D", tz=kr.TZ)
    assert idx[1] >= FORWARD_START
    assert FROZEN_CONFIG.startswith("PB_WIDE|FAST|DIRECT|H26")
    print("SELF_TEST=PASS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="kr_v035_forward_output")
    ap.add_argument("--period-60m", default="730d")
    ap.add_argument("--top-n", type=int, default=40)
    ap.add_argument("--min-snapshot-coverage", type=int, default=35)
    ap.add_argument("--min-forward-trades", type=int, default=20)
    ap.add_argument("--min-forward-days", type=int, default=90)
    ap.add_argument("--self-test", action="store_true")

    ap.add_argument("--base-risk-pct", type=float, default=.01); ap.add_argument("--max-total-risk-pct", type=float, default=.02)
    ap.add_argument("--max-symbol-pct", type=float, default=.20); ap.add_argument("--max-positions", type=int, default=4)
    ap.add_argument("--daily-loss-stop-pct", type=float, default=.015); ap.add_argument("--dd-reduce-pct", type=float, default=.05)
    ap.add_argument("--dd-risk-mult", type=float, default=.50); ap.add_argument("--dd-halt-pct", type=float, default=.08)
    ap.add_argument("--min-seed-krw", type=float, default=50_000); ap.add_argument("--adverse20-r", type=float, default=.40); ap.add_argument("--adverse60-r", type=float, default=.80)
    ap.add_argument("--min-risk-pct", type=float, default=.012); ap.add_argument("--min-r-atr", type=float, default=.75); ap.add_argument("--max-tick-r", type=float, default=.10)
    ap.add_argument("--max-entry-gap-atr", type=float, default=.25); ap.add_argument("--pullback-wait-bars", type=int, default=3)
    ap.add_argument("--pullback-tol-atr", type=float, default=.15); ap.add_argument("--pullback-hold-tol-atr", type=float, default=.05)
    ap.add_argument("--pb-tight-close-level-atr", type=float, default=.50); ap.add_argument("--pb-wide-close-level-atr", type=float, default=1.00)
    ap.add_argument("--pb-max-next-open-gap-atr", type=float, default=.25); ap.add_argument("--pb-max-below-level-atr", type=float, default=.20)
    ap.add_argument("--trail-lookback-bars", type=int, default=480); ap.add_argument("--trail-pivot-span", type=int, default=2); ap.add_argument("--trail-horizon-bars", type=int, default=26)
    ap.add_argument("--trail-min-samples", type=int, default=8); ap.add_argument("--trail-sample-min-dd", type=float, default=.005); ap.add_argument("--trail-sample-max-dd", type=float, default=.20)
    ap.add_argument("--trail-fallback-pct", type=float, default=.03); ap.add_argument("--trail-min-pct", type=float, default=.015); ap.add_argument("--trail-max-pct", type=float, default=.06)
    ap.add_argument("--trail-arm-r", type=float, default=1.0); ap.add_argument("--regime-min-coverage", type=int, default=20); ap.add_argument("--fast-breadth20", type=float, default=.45)
    ap.add_argument("--structural-breadth120", type=float, default=.40); ap.add_argument("--structural-breadth200", type=float, default=.35)
    ap.add_argument("--max-hold", type=int, default=26); ap.add_argument("--partial-fraction", type=float, default=.50); ap.add_argument("--min-market-coverage", type=int, default=30)
    args = ap.parse_args()
    if args.self_test: self_test(); return
    run(args)

if __name__ == "__main__": main()
