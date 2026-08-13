#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Causal priority replay using extended-session information.

Research only / NO_ORDERS.  The frozen Noramu signal grammar, sizing, stops,
trailing logic, holding period and execution model are unchanged.  This module
changes only the deterministic ordering of candidates that share the exact same
entry minute.

Two development-sample features are used as a *ranking* hypothesis, never as a
hard entry filter:
- gap_prior_after_close_to_open
- prior_after_vs_regular_volume

To avoid outcome leakage, each feature component is the empirical percentile of
the current value versus candidate values observed strictly before that entry
time.  Win/loss labels and future feature values are not used by the scorer.
The first few candidates use a neutral percentile until enough history exists.

The underlying strict engine is reused unchanged.  A small pandas DataFrame
subclass overrides only the engine's frozen same-minute ``symbol,setup_id``
sort, inserting ``priority_score DESC`` before those original tie breakers.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

import toss_noramu_strict_execution_v001 as strict

MODE = "NORAMU_KR_EXTENDED_PRIORITY_REPLAY_RESEARCH_NO_ORDERS"
LIVE_APPROVAL = False
FEATURES = (
    "gap_prior_after_close_to_open",
    "prior_after_vs_regular_volume",
)


class PriorityFrame(pd.DataFrame):
    """Preserve frozen ordering unless strict engine requests its candidate sort."""

    @property
    def _constructor(self):
        return PriorityFrame

    def sort_values(self, by, *args, **kwargs):  # type: ignore[override]
        keys = [by] if isinstance(by, str) else list(by)
        if keys == ["symbol", "setup_id"] and "priority_score" in self.columns:
            # Stable tie-breakers are exactly the frozen order after priority.
            kw = dict(kwargs)
            kw.pop("ascending", None)
            kw.setdefault("kind", "mergesort")
            return super().sort_values(
                ["priority_score", "symbol", "setup_id"],
                ascending=[False, True, True],
                *args,
                **kw,
            )
        return super().sort_values(by, *args, **kwargs)


def _percentile(hist: pd.Series, value: Any, neutral: float = 0.5) -> float:
    h = pd.to_numeric(hist, errors="coerce").dropna().astype(float)
    try:
        v = float(value)
    except Exception:
        return neutral
    if not np.isfinite(v) or h.empty:
        return neutral
    # Mid-rank empirical percentile handles ties deterministically.
    lt = float((h < v).sum())
    eq = float((h == v).sum())
    return (lt + 0.5 * eq) / float(len(h))


def causal_priority_scores(features: pd.DataFrame, min_history: int = 4) -> PriorityFrame:
    """Score candidates using only feature observations strictly before entry."""
    z = features.copy()
    need = {"entry_time", "symbol", "setup_id", *FEATURES}
    miss = need - set(z.columns)
    if miss:
        raise ValueError(f"feature file missing columns: {sorted(miss)}")
    z["entry_ts"] = pd.to_datetime(z.entry_time, utc=True, errors="coerce")
    if z.entry_ts.isna().any():
        raise ValueError("invalid entry_time in feature file")
    z["symbol"] = z.symbol.astype(str).str.zfill(6)
    z = z.sort_values(["entry_ts", "symbol", "setup_id"], kind="mergesort").reset_index(drop=True)

    rows = []
    for ts, g in z.groupby("entry_ts", sort=True):
        prior = z[z.entry_ts < ts]
        for _, r in g.iterrows():
            d = r.to_dict()
            total = 0.0
            used = 0
            for f in FEATURES:
                hist = pd.to_numeric(prior[f], errors="coerce").dropna()
                if len(hist) < int(min_history):
                    pct = 0.5
                    status = "NEUTRAL_INSUFFICIENT_HISTORY"
                else:
                    pct = _percentile(hist, r[f])
                    status = "HISTORICAL_PERCENTILE"
                d[f"priority_{f}_pct"] = float(pct)
                d[f"priority_{f}_history_n"] = int(len(hist))
                d[f"priority_{f}_status"] = status
                total += float(pct)
                used += 1
            d["priority_score"] = float(total / max(1, used))
            d["priority_history_cutoff"] = str(ts)
            d["priority_outcome_used"] = False
            rows.append(d)
    out = PriorityFrame(rows)
    # Score generation itself must not inspect outcome/PnL, even if those columns
    # happen to be present in the audit CSV.
    assert bool((out.priority_outcome_used == False).all())  # noqa:E712
    return out


def _baseline_summary(replay_dir: Path) -> dict[str, Any]:
    p = replay_dir / "strict_summary.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _ids(df: pd.DataFrame) -> set[str]:
    if df.empty or "setup_id" not in df.columns:
        return set()
    return set(df.setup_id.astype(str))


def run(a) -> dict[str, Any]:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    replay_dir = Path(a.replay_dir)
    feature_path = Path(a.features)
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)

    feat = pd.read_csv(feature_path, dtype={"symbol": str})
    cand = causal_priority_scores(feat, min_history=a.min_history)
    cand.to_csv(out / "priority_candidates.csv", index=False, encoding="utf-8-sig")

    # Raw windows already exist for every fast-pass candidate from the strict
    # Toss pipeline.  No API call and no credential is needed here.
    wins = strict.candidate_windows(cand, days=a.window_days)
    con = sqlite3.connect(a.db)
    timeline = strict.load_timeline(con, wins)
    con.close()
    if timeline.empty and len(cand):
        raise RuntimeError("candidate timeline empty; existing raw-window cache is missing")

    ex = strict.load_ex_module()
    args = strict.frozen_args()
    baseline = _baseline_summary(replay_dir)
    comparisons = []
    all_summary = {}

    scenarios = ((5_000_000, 1), (5_000_000, 3), (20_000_000, 1), (20_000_000, 3))
    for cap, slip in scenarios:
        key = f"{cap//1_000_000}m_{slip}t"
        print(f"PRIORITY_REPLAY {key}", flush=True)
        res = strict.simulate(
            timeline,
            cand,
            starting_equity=float(cap),
            slippage_ticks=int(slip),
            ex=ex,
            args=args,
        )
        tr = res.trades.copy()
        if len(tr):
            score_cols = ["setup_id", "priority_score"] + [f"priority_{f}_pct" for f in FEATURES]
            tr = tr.merge(pd.DataFrame(cand)[score_cols], on="setup_id", how="left")
        tr.to_csv(out / f"priority_trades_{key}.csv", index=False, encoding="utf-8-sig")
        res.rejects.to_csv(out / f"priority_rejects_{key}.csv", index=False, encoding="utf-8-sig")
        res.equity.to_csv(out / f"priority_equity_{key}.csv", index=False, encoding="utf-8-sig")
        all_summary[key] = res.summary

        b = baseline.get("results", {}).get(key, {}) if baseline else {}
        base_tr_path = replay_dir / f"strict_trades_{key}.csv"
        base_tr = pd.read_csv(base_tr_path, dtype={"symbol": str}) if base_tr_path.exists() else pd.DataFrame()
        pids = _ids(tr); bids = _ids(base_tr)
        comparisons.append({
            "scenario": key,
            "baseline_pnl": b.get("realized_pnl"),
            "priority_pnl": res.summary.get("realized_pnl"),
            "pnl_delta": (float(res.summary.get("realized_pnl", 0.0)) - float(b.get("realized_pnl", 0.0))) if b else np.nan,
            "baseline_return": b.get("return_pct"),
            "priority_return": res.summary.get("return_pct"),
            "baseline_pf": b.get("pf"),
            "priority_pf": res.summary.get("pf"),
            "baseline_max_dd": b.get("max_dd_pct"),
            "priority_max_dd": res.summary.get("max_dd_pct"),
            "baseline_trades": b.get("closed_trades"),
            "priority_trades": res.summary.get("closed_trades"),
            "added_setup_ids": "|".join(sorted(pids - bids)),
            "removed_setup_ids": "|".join(sorted(bids - pids)),
            "same_selection": bool(pids == bids),
        })

    comp = pd.DataFrame(comparisons)
    comp.to_csv(out / "priority_vs_baseline.csv", index=False, encoding="utf-8-sig")
    state = {
        "mode": MODE,
        "live_approval": False,
        "core_frozen_signal_changed": False,
        "hard_filter_added": False,
        "ordering_only": True,
        "features": list(FEATURES),
        "score_policy": "mean empirical percentile vs candidate features strictly before same entry timestamp; no outcomes/PnL",
        "min_history": int(a.min_history),
        "candidate_rows": int(len(cand)),
        "timeline_rows": int(len(timeline)),
        "results": all_summary,
        "outputs": {
            "candidates": str(out / "priority_candidates.csv"),
            "comparison": str(out / "priority_vs_baseline.csv"),
        },
    }
    (out / "priority_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("=== EXTENDED_PRIORITY_REPLAY_STATE ===")
    print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
    print("\n=== PRIORITY_VS_BASELINE ===")
    print(comp.to_string(index=False))
    return state


def self_test() -> None:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    rows = []
    for i in range(6):
        rows.append({
            "entry_time": f"2026-01-{i+2:02d}T10:00:00+09:00",
            "symbol": f"{i+1:06d}",
            "setup_id": f"S{i}",
            "gap_prior_after_close_to_open": float(i),
            "prior_after_vs_regular_volume": float(i * 10),
            "outcome": "WIN" if i % 2 else "LOSS",
            "trade_pnl": 999999 if i == 5 else -999999,
        })
    z = causal_priority_scores(pd.DataFrame(rows), min_history=4)
    assert len(z) == 6
    assert abs(float(z.iloc[0].priority_score) - 0.5) < 1e-12
    # Fifth candidate has four strictly earlier observations and is above all of them.
    assert float(z.iloc[4].priority_score) == 1.0
    assert bool((z.priority_outcome_used == False).all())  # noqa:E712

    # Verify the only overridden ordering behavior.
    q = PriorityFrame([
        {"symbol":"000001","setup_id":"A","priority_score":0.1},
        {"symbol":"000002","setup_id":"B","priority_score":0.9},
    ])
    ordered = q.sort_values(["symbol","setup_id"])
    assert list(ordered.symbol) == ["000002","000001"]
    ordinary = q.sort_values(["setup_id"])
    assert list(ordinary.setup_id) == ["A","B"]
    print("NORAMU_EXTENDED_PRIORITY_REPLAY_V001_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--replay-dir", default="toss_noramu_full_replay_v001")
    ap.add_argument("--features", default="toss_noramu_extended_feature_audit_v001/extended_candidate_features.csv")
    ap.add_argument("--outdir", default="toss_noramu_extended_priority_replay_v001")
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--min-history", type=int, default=4)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test(); return
    run(a)


if __name__ == "__main__":
    main()
