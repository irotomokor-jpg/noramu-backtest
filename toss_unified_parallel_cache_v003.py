#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Two-worker, globally rate-limited Toss adjusted 1m cache v0.03.

Research only / NO_ORDERS.

Design goals
------------
- Resume/reuse the exact v0.02 SQLite + cache_state database.
- Use at most two stock workers to overlap HTTP latency.
- Share one thread-safe RateGate across all workers so aggregate chart requests
  remain globally paced instead of each worker independently consuming a limit.
- Give each worker its own SQLite connection; WAL + busy_timeout serializes the
  short page commits safely.
- Never touch account, asset, holding, buying-power, or order endpoints.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
from pathlib import Path
import threading

import pandas as pd

from toss_replay_source_v001 import RateGate, TossReplayClient, TossReplayError
from toss_sqlite_cache_v001 import db_connect
from toss_unified_adjusted_cache_v001 import load_manifest, _terminalize_stock_404
from toss_unified_adjusted_cache_v002 import _cache_one

MODE = "TOSS_UNIFIED_PARALLEL_CACHE_V003_READ_ONLY_NO_ORDERS"
LIVE_APPROVAL = False
MAX_WORKERS = 2
DEFAULT_GLOBAL_CHART_GAP_SECONDS = 0.24


def build_plan(manifest: str, sleeves: str, chunk_size: int,
               start_chunk: int, end_chunk: int, workers: int,
               global_gap: float) -> tuple[pd.DataFrame, dict]:
    wanted = [x.strip() for x in sleeves.split(",") if x.strip()]
    allz = load_manifest(manifest, wanted or None).reset_index(drop=True)
    n = len(allz)
    chunks = int(math.ceil(n / int(chunk_size))) if n else 0
    s = max(0, int(start_chunk))
    e = chunks if int(end_chunk) < 0 else min(chunks, int(end_chunk))
    if e < s:
        raise ValueError("end_chunk must be >= start_chunk, or -1")
    if not 1 <= int(workers) <= MAX_WORKERS:
        raise ValueError(f"workers must be between 1 and {MAX_WORKERS}")
    if float(global_gap) < 0.20:
        raise ValueError("global chart gap below 0.20s is not allowed")
    lo = s * int(chunk_size)
    hi = min(n, e * int(chunk_size))
    z = allz.iloc[lo:hi].copy().reset_index(drop=True)
    if not z.empty:
        z["global_index"] = range(lo, hi)
    plan = {
        "mode": MODE,
        "live_approval": False,
        "manifest": manifest,
        "sleeves": wanted,
        "all_symbols": int(n),
        "chunk_size": int(chunk_size),
        "all_chunks": int(chunks),
        "start_chunk": int(s),
        "end_chunk_exclusive": int(e),
        "selected_symbols": int(len(z)),
        "workers": int(workers),
        "global_chart_gap_seconds": float(global_gap),
        "theoretical_chart_tps_ceiling": 1.0 / float(global_gap),
        "cache_reuse_policy": "V002_COMPLETED_CACHE_STATE_CONTIGUOUS_CHAIN_ONLY",
        "sqlite_policy": "WAL_PER_WORKER_CONNECTION_BUSY_TIMEOUT_30S",
    }
    return z, plan


def _symbol_name(row) -> str:
    try:
        v = getattr(row, "name")
        return "" if pd.isna(v) else str(v)
    except Exception:
        return ""


def _worker(row, a, gate: RateGate, done_counter: list[int], counter_lock: threading.Lock,
            selected_total: int) -> dict:
    market = str(row.market)
    sym = str(row.symbol).zfill(6) if market == "KR" else str(row.symbol).upper()
    sleeves = str(row.sleeves)
    global_index = int(row.global_index)
    name = _symbol_name(row)
    print(
        f"PAR_V003 START global={global_index+1}/{a._all_symbols} selected={global_index-a._lo+1}/{selected_total} "
        f"{market} {sym} {name}",
        flush=True,
    )
    con = db_connect(Path(a.db))
    con.execute("PRAGMA busy_timeout=30000")
    client = TossReplayClient(gate=gate)
    try:
        st = _cache_one(
            con, client, kind="stock", symbol=sym, adjusted=True,
            start=a.start, end=a.end, max_pages=a.max_pages,
            progress_every=a.progress_every,
        )
        result = {
            "global_index": global_index,
            "market": market,
            "symbol": sym,
            "name": name,
            "sleeves": sleeves,
            "done": int(st.get("done", 0)),
            "pages": int(st.get("pages", 0)),
            "stored_rows": int(st.get("stored_rows", 0)),
            "stop_reason": str(st.get("stop_reason") or ""),
            "cache_reuse": int(st.get("cache_reuse", 0)),
            "reused_state_count": int(st.get("reused_state_count", 0)),
            "effective_start": st.get("effective_start", a.start),
            "error": "",
        }
    except TossReplayError as e:
        if int(getattr(e, "status", 0) or 0) != 404:
            raise
        st = _terminalize_stock_404(con, symbol=sym, start=a.start, end=a.end, exc=e)
        reason = str(st.get("stop_reason") or "STOCK_404")
        print(f"PAR_V003 SKIP_404 {market} {sym} reason={reason}", flush=True)
        result = {
            "global_index": global_index,
            "market": market,
            "symbol": sym,
            "name": name,
            "sleeves": sleeves,
            "done": 1,
            "pages": int(st.get("pages", 0) or 0),
            "stored_rows": int(st.get("stored_rows", 0) or 0),
            "stop_reason": reason,
            "cache_reuse": 0,
            "reused_state_count": 0,
            "effective_start": a.start,
            "error": str(e),
        }
    finally:
        con.close()
    with counter_lock:
        done_counter[0] += 1
        finished = done_counter[0]
    print(
        f"PAR_V003 DONE {finished}/{selected_total} global={global_index+1}/{a._all_symbols} "
        f"{sym} reason={result['stop_reason']} pages={result['pages']} reuse={result['cache_reuse']}",
        flush=True,
    )
    return result


def _cache_indicators(a, gate: RateGate) -> list[dict]:
    if not a.include_indicators:
        return []
    con = db_connect(Path(a.db))
    con.execute("PRAGMA busy_timeout=30000")
    client = TossReplayClient(gate=gate)
    out = []
    try:
        for ind in ("KOSPI", "KOSDAQ"):
            print(f"PAR_V003 INDICATOR {ind}", flush=True)
            st = _cache_one(
                con, client, kind="indicator", symbol=ind, adjusted=False,
                start=a.start, end=a.end, max_pages=a.max_pages,
                progress_every=a.progress_every,
            )
            out.append({
                "global_index": -1,
                "market": "KR",
                "symbol": ind,
                "name": ind,
                "sleeves": "REGIME_INDICATOR",
                "done": int(st.get("done", 0)),
                "pages": int(st.get("pages", 0)),
                "stored_rows": int(st.get("stored_rows", 0)),
                "stop_reason": str(st.get("stop_reason") or ""),
                "cache_reuse": int(st.get("cache_reuse", 0)),
                "reused_state_count": int(st.get("reused_state_count", 0)),
                "effective_start": st.get("effective_start", a.start),
                "error": "",
            })
    finally:
        con.close()
    return out


def run(a) -> dict:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    z, plan = build_plan(
        a.manifest, a.sleeves, a.chunk_size, a.start_chunk, a.end_chunk,
        a.workers, a.global_chart_gap_seconds,
    )
    print("=== UNIFIED_PARALLEL_CACHE_V003_PLAN ===", flush=True)
    print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
    if not a.execute:
        print("PLAN_ONLY=1 (pass --execute on the fixed-IP Toss host)", flush=True)
        return plan
    if z.empty:
        raise RuntimeError("no symbols selected")

    # One shared process-local gate is the core safety property of v0.03.
    gate = RateGate()
    gate._gap["MARKET_DATA_CHART"] = float(a.global_chart_gap_seconds)
    gate._gap["MARKET_INDICATOR_CHART"] = float(a.global_chart_gap_seconds)

    a._all_symbols = int(plan["all_symbols"])
    a._lo = int(plan["start_chunk"]) * int(plan["chunk_size"])
    done_counter = [0]
    counter_lock = threading.Lock()
    results = []

    with ThreadPoolExecutor(max_workers=int(a.workers), thread_name_prefix="toss-v003") as ex:
        futures = [
            ex.submit(_worker, row, a, gate, done_counter, counter_lock, len(z))
            for row in z.itertuples(index=False)
        ]
        for fut in as_completed(futures):
            results.append(fut.result())

    results.extend(_cache_indicators(a, gate))
    results.sort(key=lambda x: (int(x.get("global_index", -1)) < 0, int(x.get("global_index", -1)), str(x.get("symbol", ""))))

    con = db_connect(Path(a.db))
    candle_rows = int(con.execute("SELECT COUNT(*) FROM candles").fetchone()[0])
    con.close()
    stock_results = [x for x in results if x.get("sleeves") != "REGIME_INDICATOR"]
    state = {
        **plan,
        "execute": True,
        "stock_datasets": int(len(stock_results)),
        "stock_done": int(sum(int(x.get("done", 0)) for x in stock_results)),
        "dataset_404_count": int(sum("404" in str(x.get("stop_reason", "")) for x in stock_results)),
        "cache_reuse_count": int(sum(int(x.get("cache_reuse", 0)) for x in stock_results)),
        "api_pages_this_run": int(sum(int(x.get("pages", 0)) for x in results)),
        "stored_rows_this_run": int(sum(int(x.get("stored_rows", 0)) for x in results)),
        "sqlite_candle_rows": candle_rows,
        "status": "PASS" if len(stock_results) == len(z) and all(int(x.get("done", 0)) for x in stock_results) else "INCOMPLETE",
    }
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(results).to_csv(out / "parallel_datasets.csv", index=False, encoding="utf-8-sig")
    (out / "parallel_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("=== UNIFIED_PARALLEL_CACHE_V003_STATE ===", flush=True)
    print(json.dumps(state, ensure_ascii=False, indent=2, default=str), flush=True)
    return state


def self_test() -> None:
    import tempfile
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "m.csv"
        pd.DataFrame([
            {"symbol": f"{i:06d}", "market": "KR", "sleeves": "KR_KOSDAQ", "name": f"N{i}"}
            for i in range(41)
        ]).to_csv(p, index=False)
        z, q = build_plan(str(p), "KR_KOSDAQ", 20, 1, 3, 2, 0.24)
        assert q["all_symbols"] == 41 and q["all_chunks"] == 3
        assert q["selected_symbols"] == 21 and list(z.global_index) == list(range(20, 41))
        assert q["workers"] == 2 and q["theoretical_chart_tps_ceiling"] < 5.0
        try:
            build_plan(str(p), "KR_KOSDAQ", 20, 0, -1, 3, 0.24)
            raise AssertionError("workers>2 must fail")
        except ValueError:
            pass
        try:
            build_plan(str(p), "KR_KOSDAQ", 20, 0, -1, 2, 0.19)
            raise AssertionError("gap<0.20 must fail")
        except ValueError:
            pass
    print("TOSS_UNIFIED_PARALLEL_CACHE_V003_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="unified_pit_membership_v001/kr_union_manifest.csv")
    ap.add_argument("--sleeves", default="KR_KOSDAQ")
    ap.add_argument("--db", default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--start", default="2025-09-01T00:00:00+00:00")
    ap.add_argument("--end", default="2026-08-12T00:00:00+00:00")
    ap.add_argument("--chunk-size", type=int, default=20)
    ap.add_argument("--start-chunk", type=int, default=0,
                    help="0-based first chunk to process")
    ap.add_argument("--end-chunk", type=int, default=-1,
                    help="0-based exclusive end chunk; -1 means through the end")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--global-chart-gap-seconds", type=float, default=DEFAULT_GLOBAL_CHART_GAP_SECONDS)
    ap.add_argument("--max-pages", type=int, default=100000)
    ap.add_argument("--progress-every", type=int, default=50)
    ap.add_argument("--include-indicators", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--outdir", default="toss_unified_parallel_cache_v003")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test()
        return
    run(a)


if __name__ == "__main__":
    main()
