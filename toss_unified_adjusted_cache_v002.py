#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Coverage-aware broad Toss adjusted 1m cache.

Research only / NO_ORDERS.

v0.02 keeps the v0.01 404 isolation behavior and adds verified cache reuse.
It never assumes that rows existing in SQLite imply full historical coverage.
Instead it chains completed cache_state intervals for the same dataset and only
skips API history that has already been proven complete by a prior cache run.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

import pandas as pd

from toss_replay_source_v001 import TossReplayClient, TossReplayError
from toss_sqlite_cache_v001 import db_connect, cache_range
from toss_unified_adjusted_cache_v001 import (
    load_manifest,
    select_chunk,
    estimate,
    _terminalize_stock_404,
)

MODE = "TOSS_UNIFIED_ADJUSTED_CACHE_V002_READ_ONLY_NO_ORDERS"
LIVE_APPROVAL = False
REUSABLE_TERMINAL_REASONS = {"START_REACHED", "EMPTY_PAGE", "NO_NEXT_BEFORE"}


def _utc_ts(value: str | pd.Timestamp) -> pd.Timestamp:
    t = pd.Timestamp(value)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def completed_coverage_chain(con: sqlite3.Connection, *, kind: str, symbol: str,
                             adjusted: bool, start: str, end: str) -> dict:
    """Return the contiguous verified completed cache coverage from requested start.

    Only cache_state rows with terminal reasons that prove the requested interval
    was fully paged are reusable. 404-terminalized datasets are deliberately not
    reusable as complete coverage.
    """
    s = _utc_ts(start)
    e = _utc_ts(end)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        """
        SELECT dataset_key,start_ts,end_ts,oldest_ts,newest_ts,done,stop_reason,pages,stored_rows
        FROM cache_state
        WHERE kind=? AND symbol=? AND adjusted=? AND done=1
        ORDER BY start_ts,end_ts
        """,
        (kind, symbol, int(bool(adjusted))),
    )]
    con.row_factory = None

    intervals = []
    for r in rows:
        reason = str(r.get("stop_reason") or "").split(":", 1)[0]
        if reason not in REUSABLE_TERMINAL_REASONS:
            continue
        try:
            a = _utc_ts(r["start_ts"])
            b = _utc_ts(r["end_ts"])
        except Exception:
            continue
        if b <= a:
            continue
        intervals.append((a, b, r))

    covered_until = s
    used = []
    changed = True
    while changed:
        changed = False
        best = None
        for a, b, r in intervals:
            if a <= covered_until and b > covered_until:
                if best is None or b > best[1]:
                    best = (a, b, r)
        if best is not None:
            covered_until = min(best[1], e)
            used.append(best[2])
            changed = True
            if covered_until >= e:
                break

    return {
        "covered_from": s.isoformat(),
        "covered_until": covered_until.isoformat(),
        "covered_fully": bool(covered_until >= e),
        "reused_state_count": len(used),
        "reused_state_keys": [str(x.get("dataset_key")) for x in used],
    }


def _cache_one(con, client, *, kind: str, symbol: str, adjusted: bool,
               start: str, end: str, max_pages: int, progress_every: int) -> dict:
    cov = completed_coverage_chain(
        con, kind=kind, symbol=symbol, adjusted=adjusted, start=start, end=end
    )
    if cov["covered_fully"]:
        print(
            f"CACHE_REUSE_FULL {kind} {symbol} adj={int(bool(adjusted))} "
            f"states={cov['reused_state_count']} until={cov['covered_until']}",
            flush=True,
        )
        return {
            "done": 1,
            "pages": 0,
            "stored_rows": 0,
            "stop_reason": "COVERED_BY_COMPLETED_CACHE_STATE",
            "cache_reuse": 1,
            **cov,
        }

    effective_start = cov["covered_until"] if cov["reused_state_count"] else start
    if cov["reused_state_count"]:
        print(
            f"CACHE_REUSE_PREFIX {kind} {symbol} adj={int(bool(adjusted))} "
            f"states={cov['reused_state_count']} resume_from={effective_start}",
            flush=True,
        )
    st = cache_range(
        con, client, kind=kind, symbol=symbol, adjusted=adjusted,
        start=effective_start, end=end, max_pages=max_pages,
        progress_every=progress_every,
    )
    out = dict(st)
    out["cache_reuse"] = int(cov["reused_state_count"] > 0)
    out.update(cov)
    out["effective_start"] = effective_start
    return out


def run(a) -> dict:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    sleeves = [x.strip() for x in a.sleeves.split(",") if x.strip()] if a.sleeves else None
    allz = load_manifest(a.manifest, sleeves)
    z = select_chunk(allz, a.chunk_index, a.chunk_size)
    plan = {
        "mode": MODE,
        "live_approval": False,
        "manifest": a.manifest,
        "selected_total_symbols": int(len(allz)),
        "chunk_index": int(a.chunk_index),
        "chunk_size": int(a.chunk_size),
        "chunk_symbols": int(len(z)),
        "symbols": z[["market", "symbol", "sleeves"]].to_dict(orient="records"),
        "start": a.start,
        "end": a.end,
        "estimate": estimate(z, a.start, a.end, a.chart_gap_seconds),
        "cache_reuse_policy": "COMPLETED_CACHE_STATE_CONTIGUOUS_CHAIN_ONLY",
    }
    print("=== UNIFIED_CACHE_V002_PLAN ===")
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not a.execute:
        print("PLAN_ONLY=1 (pass --execute on the fixed-IP Toss host to download)")
        return plan
    if z.empty:
        print("EMPTY_CHUNK=1")
        return plan

    con = db_connect(Path(a.db))
    client = TossReplayClient()
    client.gate._gap["MARKET_DATA_CHART"] = float(a.chart_gap_seconds)
    client.gate._gap["MARKET_INDICATOR_CHART"] = float(a.chart_gap_seconds)
    results = []

    for i, r in z.reset_index(drop=True).iterrows():
        sym = str(r.symbol).zfill(6) if r.market == "KR" else str(r.symbol).upper()
        print(f"\nUNIFIED_ADJ_V002 {i+1}/{len(z)} {r.market} {sym} {r.sleeves}", flush=True)
        try:
            st = _cache_one(
                con, client, kind="stock", symbol=sym, adjusted=True,
                start=a.start, end=a.end, max_pages=a.max_pages,
                progress_every=a.progress_every,
            )
            results.append({
                "market": r.market,
                "symbol": sym,
                "sleeves": r.sleeves,
                "done": int(st.get("done", 0)),
                "pages": int(st.get("pages", 0)),
                "stored_rows": int(st.get("stored_rows", 0)),
                "stop_reason": st.get("stop_reason"),
                "cache_reuse": int(st.get("cache_reuse", 0)),
                "reused_state_count": int(st.get("reused_state_count", 0)),
                "effective_start": st.get("effective_start", a.start),
                "error": "",
            })
        except TossReplayError as e:
            if int(getattr(e, "status", 0) or 0) != 404:
                raise
            st = _terminalize_stock_404(con, symbol=sym, start=a.start, end=a.end, exc=e)
            reason = str(st.get("stop_reason") or "STOCK_404")
            print(f"SKIP_DATASET_404 market={r.market} symbol={sym} reason={reason} error={e}", flush=True)
            results.append({
                "market": r.market,
                "symbol": sym,
                "sleeves": r.sleeves,
                "done": 1,
                "pages": int(st.get("pages", 0) or 0),
                "stored_rows": int(st.get("stored_rows", 0) or 0),
                "stop_reason": reason,
                "cache_reuse": 0,
                "reused_state_count": 0,
                "effective_start": a.start,
                "error": str(e),
            })

    if a.include_indicators:
        markets = set(z.market.astype(str))
        if "KR" in markets:
            for ind in ("KOSPI", "KOSDAQ"):
                print(f"\nUNIFIED_INDICATOR_V002 {ind}", flush=True)
                st = _cache_one(
                    con, client, kind="indicator", symbol=ind, adjusted=False,
                    start=a.start, end=a.end, max_pages=a.max_pages,
                    progress_every=a.progress_every,
                )
                results.append({
                    "market": "KR",
                    "symbol": ind,
                    "sleeves": "REGIME_INDICATOR",
                    "done": int(st.get("done", 0)),
                    "pages": int(st.get("pages", 0)),
                    "stored_rows": int(st.get("stored_rows", 0)),
                    "stop_reason": st.get("stop_reason"),
                    "cache_reuse": int(st.get("cache_reuse", 0)),
                    "reused_state_count": int(st.get("reused_state_count", 0)),
                    "effective_start": st.get("effective_start", a.start),
                    "error": "",
                })

    candle_rows = int(con.execute("SELECT COUNT(*) FROM candles").fetchone()[0])
    con.close()
    state = {
        **plan,
        "execute": True,
        "datasets": results,
        "done": int(sum(x["done"] for x in results)),
        "dataset_count": len(results),
        "sqlite_candle_rows": candle_rows,
        "dataset_404_count": int(sum("404" in str(x.get("stop_reason", "")) for x in results)),
        "cache_reuse_count": int(sum(int(x.get("cache_reuse", 0)) for x in results)),
        "api_pages_this_run": int(sum(int(x.get("pages", 0)) for x in results)),
        "stored_rows_this_run": int(sum(int(x.get("stored_rows", 0)) for x in results)),
    }
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"chunk_{a.chunk_index:03d}_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    pd.DataFrame(results).to_csv(
        out / f"chunk_{a.chunk_index:03d}_datasets.csv", index=False, encoding="utf-8-sig"
    )
    print("=== UNIFIED_CACHE_V002_STATE ===")
    print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
    return state


def self_test() -> None:
    import tempfile
    from datetime import datetime, timezone

    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "m.csv"
        pd.DataFrame([
            {"symbol": "005930", "market": "KR", "sleeves": "KR_KOSPI"},
            {"symbol": "000660", "market": "KR", "sleeves": "KR_KOSPI"},
        ]).to_csv(p, index=False)
        z = load_manifest(p, ["KR_KOSPI"])
        assert len(z) == 2

        con = db_connect(Path(td) / "x.sqlite")
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            ("a", "stock", "005930", 1, "2025-09-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", None, 1, 1, 1, "2025-08-31T23:59:00+00:00", "2025-12-31T23:59:00+00:00", 1, "START_REACHED", now),
            ("b", "stock", "005930", 1, "2026-01-01T00:00:00+00:00", "2026-08-12T00:00:00+00:00", None, 1, 1, 1, "2025-12-31T23:59:00+00:00", "2026-08-11T23:59:00+00:00", 1, "START_REACHED", now),
            ("c", "stock", "000660", 1, "2025-09-01T00:00:00+00:00", "2026-08-12T00:00:00+00:00", None, 0, 0, 0, None, None, 1, "STOCK_NOT_FOUND_404:stock-not-found", now),
        ]
        con.executemany(
            "INSERT INTO cache_state(dataset_key,kind,symbol,adjusted,start_ts,end_ts,next_before,pages,api_rows,stored_rows,oldest_ts,newest_ts,done,stop_reason,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        con.commit()
        cov = completed_coverage_chain(
            con, kind="stock", symbol="005930", adjusted=True,
            start="2025-09-01T00:00:00+00:00", end="2026-08-12T00:00:00+00:00"
        )
        assert cov["covered_fully"] and cov["reused_state_count"] == 2
        bad = completed_coverage_chain(
            con, kind="stock", symbol="000660", adjusted=True,
            start="2025-09-01T00:00:00+00:00", end="2026-08-12T00:00:00+00:00"
        )
        assert not bad["covered_fully"] and bad["reused_state_count"] == 0
        con.close()
    print("TOSS_UNIFIED_ADJUSTED_CACHE_V002_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="unified_pit_membership_v001/kr_union_manifest.csv")
    ap.add_argument("--sleeves", default="KR_KOSPI,KR_KOSDAQ")
    ap.add_argument("--db", default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--start", default="2025-09-01T00:00:00+00:00")
    ap.add_argument("--end", default="2026-08-12T00:00:00+00:00")
    ap.add_argument("--chunk-index", type=int, default=0)
    ap.add_argument("--chunk-size", type=int, default=20)
    ap.add_argument("--chart-gap-seconds", type=float, default=.40)
    ap.add_argument("--max-pages", type=int, default=100000)
    ap.add_argument("--progress-every", type=int, default=50)
    ap.add_argument("--include-indicators", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--outdir", default="toss_unified_adjusted_cache_v002")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test()
        return
    run(a)


if __name__ == "__main__":
    main()
