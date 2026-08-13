#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit unified PIT manifest coverage against the local Toss SQLite cache.

Research only / NO_ORDERS. No network or broker account/order endpoint is used.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

import pandas as pd

from toss_sqlite_cache_v001 import db_connect
from toss_unified_adjusted_cache_v001 import load_manifest
from toss_unified_adjusted_cache_v002 import completed_coverage_chain

MODE = "TOSS_UNIFIED_COVERAGE_AUDIT_NO_ORDERS"
LIVE_APPROVAL = False


def _latest_404(con: sqlite3.Connection, symbol: str) -> str:
    row = con.execute(
        """
        SELECT stop_reason FROM cache_state
        WHERE kind='stock' AND symbol=? AND adjusted=1 AND stop_reason LIKE '%404%'
        ORDER BY updated_at DESC LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    return str(row[0]) if row else ""


def audit(manifest: str, sleeves: str, db: str, start: str, end: str) -> tuple[pd.DataFrame, dict]:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    wanted = [x.strip() for x in sleeves.split(",") if x.strip()]
    z = load_manifest(manifest, wanted or None)
    con = db_connect(Path(db))
    rows = []
    for r in z.itertuples():
        sym = str(r.symbol).zfill(6) if str(r.market).upper() == "KR" else str(r.symbol).upper()
        q = con.execute(
            "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM candles WHERE kind='stock' AND symbol=? AND adjusted=1",
            (sym,),
        ).fetchone()
        n, oldest, newest = int(q[0]), q[1], q[2]
        cov = completed_coverage_chain(
            con, kind="stock", symbol=sym, adjusted=True, start=start, end=end
        )
        reason404 = _latest_404(con, sym)
        if cov["covered_fully"]:
            status = "COMPLETE_CACHE_STATE"
        elif reason404:
            status = "KNOWN_404"
        elif n > 0:
            status = "PARTIAL_OR_UNVERIFIED_DATA"
        else:
            status = "NO_DATA"
        rows.append({
            "market": str(r.market),
            "symbol": sym,
            "sleeves": str(r.sleeves),
            "name": str(getattr(r, "name", "") or ""),
            "row_count": n,
            "oldest_ts": oldest,
            "newest_ts": newest,
            "coverage_status": status,
            "covered_fully": int(cov["covered_fully"]),
            "reused_state_count": int(cov["reused_state_count"]),
            "latest_404_reason": reason404,
        })
    con.close()
    df = pd.DataFrame(rows)
    counts = df.coverage_status.value_counts().to_dict()
    state = {
        "mode": MODE,
        "live_approval": False,
        "manifest": manifest,
        "sleeves": wanted,
        "start": start,
        "end": end,
        "symbols": int(len(df)),
        "complete_cache_state": int(counts.get("COMPLETE_CACHE_STATE", 0)),
        "known_404": int(counts.get("KNOWN_404", 0)),
        "partial_or_unverified": int(counts.get("PARTIAL_OR_UNVERIFIED_DATA", 0)),
        "no_data": int(counts.get("NO_DATA", 0)),
        "total_candle_rows_selected": int(df.row_count.sum()),
        "status": "PASS" if int(counts.get("PARTIAL_OR_UNVERIFIED_DATA", 0)) == 0 and int(counts.get("NO_DATA", 0)) == 0 else "REVIEW_REQUIRED",
    }
    return df, state


def self_test() -> None:
    import tempfile
    from datetime import datetime, timezone

    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    with tempfile.TemporaryDirectory() as td:
        m = Path(td) / "m.csv"
        db = Path(td) / "x.sqlite"
        pd.DataFrame([
            {"symbol": "005930", "market": "KR", "sleeves": "KR_KOSPI", "name": "A"},
            {"symbol": "010620", "market": "KR", "sleeves": "KR_KOSPI", "name": "B"},
        ]).to_csv(m, index=False)
        con = db_connect(db)
        con.execute(
            "INSERT INTO candles(kind,symbol,adjusted,timestamp,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?,?,?)",
            ("stock", "005930", 1, "2026-01-02T09:00:00+09:00", 1, 1, 1, 1, 1),
        )
        now = datetime.now(timezone.utc).isoformat()
        con.execute(
            "INSERT INTO cache_state(dataset_key,kind,symbol,adjusted,start_ts,end_ts,next_before,pages,api_rows,stored_rows,oldest_ts,newest_ts,done,stop_reason,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("a", "stock", "005930", 1, "2025-09-01T00:00:00+00:00", "2026-08-12T00:00:00+00:00", None, 1, 1, 1, "2025-08-31T23:59:00+00:00", "2026-08-11T23:59:00+00:00", 1, "START_REACHED", now),
        )
        con.execute(
            "INSERT INTO cache_state(dataset_key,kind,symbol,adjusted,start_ts,end_ts,next_before,pages,api_rows,stored_rows,oldest_ts,newest_ts,done,stop_reason,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("b", "stock", "010620", 1, "2025-09-01T00:00:00+00:00", "2026-08-12T00:00:00+00:00", None, 0, 0, 0, None, None, 1, "STOCK_NOT_FOUND_404:stock-not-found", now),
        )
        con.commit(); con.close()
        df, st = audit(str(m), "KR_KOSPI", str(db), "2025-09-01T00:00:00+00:00", "2026-08-12T00:00:00+00:00")
        assert st["symbols"] == 2 and st["complete_cache_state"] == 1 and st["known_404"] == 1
        assert set(df.coverage_status) == {"COMPLETE_CACHE_STATE", "KNOWN_404"}
    print("TOSS_UNIFIED_COVERAGE_AUDIT_V001_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="unified_pit_membership_v001/kr_union_manifest.csv")
    ap.add_argument("--sleeves", default="KR_KOSPI")
    ap.add_argument("--db", default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--start", default="2025-09-01T00:00:00+00:00")
    ap.add_argument("--end", default="2026-08-12T00:00:00+00:00")
    ap.add_argument("--outdir", default="toss_unified_coverage_audit_v001")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test(); return
    df, state = audit(a.manifest, a.sleeves, a.db, a.start, a.end)
    out = Path(a.outdir); out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / "coverage_audit.csv", index=False, encoding="utf-8-sig")
    (out / "coverage_audit_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=== UNIFIED_COVERAGE_AUDIT ===")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    problems = df[df.coverage_status != "COMPLETE_CACHE_STATE"][["symbol", "name", "coverage_status", "row_count", "latest_404_reason"]]
    if not problems.empty:
        print("=== COVERAGE_EXCEPTIONS ===")
        print(problems.to_string(index=False))


if __name__ == "__main__":
    main()
