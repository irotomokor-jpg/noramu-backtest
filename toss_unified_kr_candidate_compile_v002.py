#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lifecycle-aware wrapper for unified KR PIT candidate compilation.

Research only / NO_ORDERS.

v0.02 intentionally preserves the frozen signal grammar and v0.01 candidate
logic. It adds an explicit audit for known historical-universe members that are
unavailable from the Toss candle API because the security is no longer listed.
Such symbols are never silently treated as ordinary universe exclusions.

This does NOT fill missing prices from another vendor. The gap remains visible
in the output and downstream results must be described as having a known
lifecycle source gap until an approved historical source is added.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

import pandas as pd

import toss_unified_kr_candidate_compile_v001 as v1

MODE = "TOSS_UNIFIED_KR_PIT_CANDIDATE_COMPILE_V002_NO_ORDERS"
LIVE_APPROVAL = False

# Confirmed lifecycle exception from the fixed-IP Toss collection/audit.
# The PIT rows themselves remain untouched.
KNOWN_LIFECYCLE_GAPS = {
    "335890": {
        "name": "비올",
        "sleeve": "KR_KOSDAQ",
        "reason": "KNOWN_MISSING_DELISTED_TOSS_404",
        "first_effective": "2025-09-01",
        "last_effective": "2025-12-01",
        "performance_window_overlap": False,
        "warmup_overlap": True,
    }
}


def lifecycle_gap_audit(snapshots: str | Path, db: str | Path,
                        replay_start: str, replay_end: str) -> pd.DataFrame:
    snap = v1.load_snapshots(snapshots)
    rs = pd.Timestamp(replay_start)
    re = pd.Timestamp(replay_end)
    if rs.tzinfo is not None:
        rs = rs.tz_convert(v1.TZ).tz_localize(None)
    if re.tzinfo is not None:
        re = re.tz_convert(v1.TZ).tz_localize(None)
    rs = rs.normalize(); re = re.normalize()

    con = sqlite3.connect(str(db))
    rows = []
    try:
        for sym, meta in KNOWN_LIFECYCLE_GAPS.items():
            g = snap[(snap.symbol.astype(str).str.zfill(6) == sym) & (snap.sleeve == meta["sleeve"])].copy()
            candle_rows = int(con.execute(
                "SELECT COUNT(*) FROM candles WHERE kind='stock' AND symbol=? AND adjusted=1",
                (sym,),
            ).fetchone()[0])
            effs = sorted(pd.to_datetime(g.effective_date).dt.normalize().unique()) if len(g) else []
            effective_dates = "|".join(pd.Timestamp(x).strftime("%Y-%m-%d") for x in effs)
            perf_overlap = bool(any(rs <= pd.Timestamp(x) < re for x in effs))
            warmup_overlap = bool(any(pd.Timestamp(x) < rs for x in effs))
            rows.append({
                "symbol": sym,
                "name": meta["name"],
                "sleeve": meta["sleeve"],
                "reason": meta["reason"],
                "snapshot_rows": int(len(g)),
                "effective_dates": effective_dates,
                "adjusted_candle_rows": candle_rows,
                "performance_window_overlap": perf_overlap,
                "warmup_overlap": warmup_overlap,
                "audit_status": "KNOWN_LIFECYCLE_GAP" if len(g) and candle_rows == 0 else "REVIEW_REQUIRED",
            })
    finally:
        con.close()
    return pd.DataFrame(rows)


def run(a) -> dict:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    # v0.01 remains the frozen candidate implementation.
    base = v1.run(a)
    out = Path(a.outdir)
    gaps = lifecycle_gap_audit(a.snapshots, a.db, a.replay_start, a.replay_end)
    gaps.to_csv(out / "known_lifecycle_gaps.csv", index=False, encoding="utf-8-sig")

    known = gaps[gaps.audit_status == "KNOWN_LIFECYCLE_GAP"] if len(gaps) else gaps
    perf = known[known.performance_window_overlap == True] if len(known) else known
    warm = known[known.warmup_overlap == True] if len(known) else known
    state = {
        **base,
        "mode": MODE,
        "known_lifecycle_gap_count": int(len(known)),
        "known_lifecycle_gap_symbols": known.symbol.astype(str).tolist() if len(known) else [],
        "performance_window_lifecycle_gap_count": int(len(perf)),
        "warmup_lifecycle_gap_count": int(len(warm)),
        "lifecycle_gap_policy": "PIT_MEMBERSHIP_PRESERVED_MISSING_TOSS_HISTORY_EXPLICIT_NOT_IMPUTED",
        "status": "PASS_WITH_KNOWN_WARMUP_LIFECYCLE_GAP" if len(known) and not len(perf) else ("REVIEW_PERFORMANCE_WINDOW_GAP" if len(perf) else "PASS"),
        "causal_final": False,
        "causal_final_blocker": "TRUNCATION_EQUIVALENCE_AUDIT_PENDING",
    }
    (out / "candidate_compile_state_v002.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("=== UNIFIED_KR_CANDIDATE_COMPILE_V002_STATE ===")
    print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
    return state


def self_test() -> None:
    import tempfile
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        snap = td / "snap.csv"
        pd.DataFrame([
            {"symbol":"335890","name":"비올","sleeve":"KR_KOSDAQ","effective_date":"2025-09-01","source_date":"2025-09-01","point_in_time":True},
            {"symbol":"335890","name":"비올","sleeve":"KR_KOSDAQ","effective_date":"2025-10-01","source_date":"2025-10-01","point_in_time":True},
            {"symbol":"335890","name":"비올","sleeve":"KR_KOSDAQ","effective_date":"2025-11-01","source_date":"2025-10-31","point_in_time":True},
            {"symbol":"335890","name":"비올","sleeve":"KR_KOSDAQ","effective_date":"2025-12-01","source_date":"2025-12-01","point_in_time":True},
        ]).to_csv(snap, index=False)
        db = td / "x.sqlite"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE candles(kind TEXT,symbol TEXT,adjusted INTEGER,timestamp TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL)")
        con.commit(); con.close()
        q = lifecycle_gap_audit(snap, db, "2026-01-01T00:00:00+09:00", "2026-08-11T00:00:00+09:00")
        assert len(q) == 1
        r = q.iloc[0]
        assert r.audit_status == "KNOWN_LIFECYCLE_GAP"
        assert int(r.snapshot_rows) == 4 and int(r.adjusted_candle_rows) == 0
        assert bool(r.warmup_overlap) is True and bool(r.performance_window_overlap) is False
    print("TOSS_UNIFIED_KR_CANDIDATE_COMPILE_V002_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--snapshots", default="unified_pit_membership_v001/kr_pit_snapshots.csv")
    ap.add_argument("--outdir", default="toss_unified_kr_candidate_compile_v002")
    ap.add_argument("--replay-start", default="2026-01-01T00:00:00+09:00")
    ap.add_argument("--replay-end", default="2026-08-11T00:00:00+09:00")
    ap.add_argument("--min-snapshot-coverage", type=float, default=.90)
    ap.add_argument("--regime-min-coverage", type=int, default=70)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test(); return
    run(a)


if __name__ == "__main__":
    main()
