#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch wrapper for coverage-aware unified Toss adjusted 1m cache v0.02.

Research only / NO_ORDERS.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from types import SimpleNamespace

from toss_unified_adjusted_cache_v001 import load_manifest
from toss_unified_adjusted_cache_v002 import run as run_chunk

MODE = "TOSS_UNIFIED_CACHE_BATCH_V002_READ_ONLY_NO_ORDERS"
LIVE_APPROVAL = False


def batch_plan(manifest: str, sleeves: str, chunk_size: int) -> dict:
    wanted = [x.strip() for x in sleeves.split(",") if x.strip()]
    z = load_manifest(manifest, wanted or None)
    chunks = int(math.ceil(len(z) / int(chunk_size))) if len(z) else 0
    return {
        "mode": MODE,
        "live_approval": False,
        "manifest": manifest,
        "sleeves": wanted,
        "symbols": int(len(z)),
        "chunk_size": int(chunk_size),
        "chunks": chunks,
        "cache_reuse_policy": "COMPLETED_CACHE_STATE_CONTIGUOUS_CHAIN_ONLY",
    }


def run(a) -> dict:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    p = batch_plan(a.manifest, a.sleeves, a.chunk_size)
    print("=== UNIFIED_BATCH_V002_PLAN ===", flush=True)
    print(json.dumps(p, ensure_ascii=False, indent=2), flush=True)
    if not a.execute:
        print("PLAN_ONLY=1 (pass --execute on the fixed-IP Toss host)", flush=True)
        return p
    if p["chunks"] <= 0:
        raise RuntimeError("no chunks selected")

    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    states = []
    total_404 = 0
    total_reuse = 0
    total_api_pages = 0
    total_stored = 0
    for i in range(p["chunks"]):
        print(f"\n===== BATCH_V002 CHUNK {i+1}/{p['chunks']} index={i} =====", flush=True)
        ns = SimpleNamespace(
            manifest=a.manifest,
            sleeves=a.sleeves,
            db=a.db,
            start=a.start,
            end=a.end,
            chunk_index=i,
            chunk_size=a.chunk_size,
            chart_gap_seconds=a.chart_gap_seconds,
            max_pages=a.max_pages,
            progress_every=a.progress_every,
            include_indicators=bool(a.include_indicators and i == 0),
            execute=True,
            outdir=a.chunk_outdir,
        )
        st = run_chunk(ns)
        s = {
            "chunk_index": i,
            "chunk_symbols": int(st.get("chunk_symbols", 0)),
            "dataset_count": int(st.get("dataset_count", 0)),
            "done": int(st.get("done", 0)),
            "dataset_404_count": int(st.get("dataset_404_count", 0)),
            "cache_reuse_count": int(st.get("cache_reuse_count", 0)),
            "api_pages_this_run": int(st.get("api_pages_this_run", 0)),
            "stored_rows_this_run": int(st.get("stored_rows_this_run", 0)),
            "sqlite_candle_rows": int(st.get("sqlite_candle_rows", 0)),
        }
        states.append(s)
        total_404 += s["dataset_404_count"]
        total_reuse += s["cache_reuse_count"]
        total_api_pages += s["api_pages_this_run"]
        total_stored += s["stored_rows_this_run"]
        partial = {
            **p,
            "execute": True,
            "completed_chunks": i + 1,
            "states": states,
            "dataset_404_count": total_404,
            "cache_reuse_count": total_reuse,
            "api_pages_this_run": total_api_pages,
            "stored_rows_this_run": total_stored,
        }
        (out / "batch_state.json").write_text(
            json.dumps(partial, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    final = {
        **p,
        "execute": True,
        "completed_chunks": len(states),
        "states": states,
        "dataset_404_count": total_404,
        "cache_reuse_count": total_reuse,
        "api_pages_this_run": total_api_pages,
        "stored_rows_this_run": total_stored,
        "status": "PASS" if len(states) == p["chunks"] else "INCOMPLETE",
    }
    (out / "batch_state.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n=== UNIFIED_BATCH_V002_STATE ===", flush=True)
    print(json.dumps(final, ensure_ascii=False, indent=2), flush=True)
    return final


def self_test() -> None:
    import tempfile
    import pandas as pd

    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "m.csv"
        pd.DataFrame([
            {"symbol": f"{i:06d}", "market": "KR", "sleeves": "KR_KOSDAQ"}
            for i in range(41)
        ]).to_csv(p, index=False)
        q = batch_plan(str(p), "KR_KOSDAQ", 20)
        assert q["symbols"] == 41 and q["chunks"] == 3
    print("TOSS_UNIFIED_CACHE_BATCH_V002_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="unified_pit_membership_v001/kr_union_manifest.csv")
    ap.add_argument("--sleeves", default="KR_KOSDAQ")
    ap.add_argument("--db", default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--start", default="2025-09-01T00:00:00+00:00")
    ap.add_argument("--end", default="2026-08-12T00:00:00+00:00")
    ap.add_argument("--chunk-size", type=int, default=20)
    ap.add_argument("--chart-gap-seconds", type=float, default=.40)
    ap.add_argument("--max-pages", type=int, default=100000)
    ap.add_argument("--progress-every", type=int, default=50)
    ap.add_argument("--include-indicators", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--outdir", default="toss_unified_cache_batch_v002")
    ap.add_argument("--chunk-outdir", default="toss_unified_adjusted_cache_v002")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test()
        return
    run(a)


if __name__ == "__main__":
    main()
