#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Walk-forward robustness audit for Noramu KR extended-session features.

Research only / NO_ORDERS.  This consumes the already-causal candidate feature
file produced by toss_noramu_extended_feature_audit_v001.py.  It never changes
frozen Noramu parameters and never promotes a filter automatically.

For each feature, executed trades are ordered by entry time.  Starting after a
minimum training history, a single threshold/direction is selected using ONLY
prior executed trades and then applied to the next trade.  This provides a tiny
sequential out-of-sample sanity check against the highly optimistic all-sample
threshold sweep.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

MODE = "NORAMU_KR_EXTENDED_FEATURE_WALKFORWARD_RESEARCH_NO_ORDERS"
LIVE_APPROVAL = False
FEATURES = [
    "prior_after_return", "prior_after_range", "prior_after_vs_regular_volume",
    "same_day_before_return", "same_day_before_range", "same_day_before_vs_prev_regular_volume",
    "gap_prev_regular_close_to_open", "gap_prior_after_close_to_open",
    "gap_same_day_before_close_to_open", "prior_after_close_vs_prev_regular_close",
    "same_day_before_high_vs_prev_regular_close", "same_day_before_low_vs_prev_regular_close",
]


def _pf(pnl: pd.Series) -> float:
    x = pd.to_numeric(pnl, errors="coerce").dropna()
    gp = float(x[x > 0].sum()); gl = float(-x[x < 0].sum())
    return gp / gl if gl > 0 else (np.inf if gp > 0 else np.nan)


def candidate_rules(train: pd.DataFrame, feature: str) -> list[dict]:
    vals = pd.to_numeric(train[feature], errors="coerce")
    u = sorted(set(vals.dropna().astype(float)))
    rules = []
    if len(u) < 2:
        return rules
    # Keep at least half of the training sample.  This prevents the selector from
    # learning a threshold that merely cherry-picks one or two old winners.
    min_keep = max(3, int(np.ceil(len(train) * 0.50)))
    for th in [(u[i] + u[i+1]) / 2.0 for i in range(len(u)-1)]:
        for direction in ("GE", "LE"):
            keep = vals >= th if direction == "GE" else vals <= th
            g = train[keep.fillna(False)]
            if len(g) < min_keep:
                continue
            pnl = pd.to_numeric(g.trade_pnl, errors="coerce").dropna()
            if pnl.empty:
                continue
            # Score prioritizes PnL, then PF, then larger kept sample.
            rules.append({
                "feature": feature, "direction": direction, "threshold": float(th),
                "train_rows": int(len(train)), "train_kept": int(len(g)),
                "train_pnl": float(pnl.sum()), "train_pf": float(_pf(pnl)),
                "train_win_rate": float((g.outcome == "WIN").mean()),
            })
    rules.sort(key=lambda r: (r["train_pnl"], r["train_pf"] if np.isfinite(r["train_pf"]) else 1e99, r["train_kept"]), reverse=True)
    return rules


def walkforward(df: pd.DataFrame, feature: str, min_train: int = 4) -> pd.DataFrame:
    z = df[df.outcome.isin(["WIN", "LOSS"])].copy()
    z["entry_dt"] = pd.to_datetime(z.entry_time, utc=True, errors="coerce")
    z = z.dropna(subset=["entry_dt"]).sort_values(["entry_dt", "symbol", "setup_id"]).reset_index(drop=True)
    rows = []
    for i in range(min_train, len(z)):
        train = z.iloc[:i].copy(); test = z.iloc[i]
        rules = candidate_rules(train, feature)
        if not rules:
            continue
        r = rules[0]; v = pd.to_numeric(pd.Series([test.get(feature)]), errors="coerce").iloc[0]
        keep = False
        if np.isfinite(v):
            keep = bool(v >= r["threshold"] if r["direction"] == "GE" else v <= r["threshold"])
        rows.append({
            **r,
            "test_entry_time": str(test.entry_time), "test_symbol": str(test.symbol).zfill(6),
            "test_name": test.get("name", ""), "test_value": float(v) if np.isfinite(v) else np.nan,
            "test_keep": keep, "test_outcome": test.outcome, "test_pnl": float(test.trade_pnl),
            "kept_test_pnl": float(test.trade_pnl) if keep else 0.0,
            "saved_loss": bool((not keep) and test.outcome == "LOSS"),
            "missed_win": bool((not keep) and test.outcome == "WIN"),
        })
    return pd.DataFrame(rows)


def summarize(wf: pd.DataFrame, baseline: pd.DataFrame, feature: str, min_train: int) -> dict:
    if wf.empty:
        return {"feature": feature, "test_rows": 0}
    # Test baseline is exactly the sequential test rows represented in wf.
    baseline_pnl = float(wf.test_pnl.sum())
    kept = wf[wf.test_keep]
    filtered_pnl = float(wf.kept_test_pnl.sum())
    directions = wf.direction.value_counts(normalize=True)
    return {
        "feature": feature,
        "min_train": int(min_train),
        "test_rows": int(len(wf)),
        "kept_test_rows": int(wf.test_keep.sum()),
        "baseline_test_pnl": baseline_pnl,
        "filtered_test_pnl": filtered_pnl,
        "pnl_delta": filtered_pnl - baseline_pnl,
        "saved_losses": int(wf.saved_loss.sum()),
        "missed_wins": int(wf.missed_win.sum()),
        "kept_wins": int(((wf.test_keep) & (wf.test_outcome == "WIN")).sum()),
        "kept_losses": int(((wf.test_keep) & (wf.test_outcome == "LOSS")).sum()),
        "direction_stability": float(directions.iloc[0]) if len(directions) else np.nan,
        "dominant_direction": str(directions.index[0]) if len(directions) else None,
        "warning": "TINY_SEQUENTIAL_OOS_SAMPLE_RESEARCH_ONLY",
    }


def run(a) -> dict:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    src = Path(a.features)
    z = pd.read_csv(src, dtype={"symbol": str})
    ex = z[z.outcome.isin(["WIN", "LOSS"])].copy()
    if len(ex) < a.min_train + 1:
        raise RuntimeError(f"insufficient executed trades: {len(ex)}")
    out = Path(a.outdir); out.mkdir(parents=True, exist_ok=True)
    summaries = []; allwf = []
    for f in FEATURES:
        if f not in ex.columns:
            continue
        wf = walkforward(ex, f, a.min_train)
        if len(wf):
            wf.insert(0, "feature_name", f); allwf.append(wf)
        summaries.append(summarize(wf, ex, f, a.min_train))
    wfall = pd.concat(allwf, ignore_index=True) if allwf else pd.DataFrame()
    s = pd.DataFrame(summaries)
    if len(s):
        s = s.sort_values(["pnl_delta", "direction_stability", "kept_test_rows"], ascending=[False, False, False])
    wfall.to_csv(out / "walkforward_detail.csv", index=False, encoding="utf-8-sig")
    s.to_csv(out / "walkforward_summary.csv", index=False, encoding="utf-8-sig")
    state = {
        "mode": MODE, "live_approval": False, "executed_rows": int(len(ex)),
        "min_train": int(a.min_train), "test_steps_per_feature_max": int(max(0, len(ex)-a.min_train)),
        "automatic_filter_promotion": False,
        "decision_rule": "only treat as promising if sequential pnl_delta is positive, direction is stable, and missed_wins are not excessive; then re-test on expanded universe",
        "outputs": {"summary": str(out / "walkforward_summary.csv"), "detail": str(out / "walkforward_detail.csv")},
    }
    (out / "walkforward_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=== EXTENDED_FEATURE_WALKFORWARD_STATE ==="); print(json.dumps(state, ensure_ascii=False, indent=2))
    print("\n=== WALKFORWARD_SUMMARY ===")
    print(s.to_string(index=False))
    return state


def self_test() -> None:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    rows=[]
    for i in range(9):
        rows.append({"entry_time": f"2026-01-{i+1:02d}T10:00:00+09:00", "symbol": str(i), "setup_id": str(i),
                     "name":"X", "outcome":"WIN" if i%2==0 else "LOSS", "trade_pnl": 10.0 if i%2==0 else -5.0,
                     **{f: float(i) for f in FEATURES}})
    z=pd.DataFrame(rows); wf=walkforward(z,"prior_after_range",4)
    assert len(wf)==5
    assert all(pd.to_datetime(wf.test_entry_time, utc=True).values > pd.to_datetime(z.entry_time.iloc[3], utc=True).to_datetime64())
    print("NORAMU_EXTENDED_FEATURE_WALKFORWARD_V001_SELF_TEST=PASS")


def main() -> None:
    ap=argparse.ArgumentParser()
    ap.add_argument("--features", default="toss_noramu_extended_feature_audit_v001/extended_candidate_features.csv")
    ap.add_argument("--outdir", default="toss_noramu_extended_feature_walkforward_v001")
    ap.add_argument("--min-train", type=int, default=4)
    ap.add_argument("--self-test", action="store_true")
    a=ap.parse_args(); self_test() if a.self_test else run(a)

if __name__ == "__main__": main()
