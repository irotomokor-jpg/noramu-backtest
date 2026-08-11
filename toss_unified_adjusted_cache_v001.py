#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Chunked/resumable Toss adjusted 1m cache for unified PIT manifests.

Research only / NO_ORDERS. This calls only read-only candle/index endpoints via
TossReplayClient and reuses the durable SQLite cache from toss_sqlite_cache_v001.

Broad historical 1m collection is intentionally chunked because the Toss candle
endpoint returns the full historical minute stream (including extended-session
bars observed empirically) and has no regular-session-only selector.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd

from toss_replay_source_v001 import TossReplayClient
from toss_sqlite_cache_v001 import db_connect, cache_range

MODE = "TOSS_UNIFIED_ADJUSTED_CACHE_READ_ONLY_NO_ORDERS"
LIVE_APPROVAL = False
EMPIRICAL_BARS_PER_TRADING_DAY = {"KR": 720, "US": 1440}
PAGE_SIZE = 200


def load_manifest(path: str | Path, sleeves: list[str] | None = None) -> pd.DataFrame:
    z = pd.read_csv(path, dtype={"symbol": str})
    required = {"symbol", "market"}
    miss = required - set(z.columns)
    if miss:
        raise ValueError(f"manifest missing {sorted(miss)}")
    z["symbol"] = z.symbol.astype(str).str.strip()
    z["market"] = z.market.astype(str).str.upper().str.strip()
    if "sleeves" not in z.columns:
        if "sleeve" not in z.columns:
            raise ValueError("manifest requires sleeves or sleeve")
        z["sleeves"] = z.sleeve.astype(str)
    if sleeves:
        wanted = set(sleeves)
        z = z[z.sleeves.astype(str).map(lambda x: bool(set(x.split("|")) & wanted))].copy()
    z = z.drop_duplicates(["market", "symbol"]).sort_values(["market", "symbol"]).reset_index(drop=True)
    if z.empty:
        raise ValueError("manifest selection is empty")
    return z


def select_chunk(z: pd.DataFrame, chunk_index: int, chunk_size: int) -> pd.DataFrame:
    if chunk_index < 0 or chunk_size <= 0:
        raise ValueError("chunk-index >=0 and chunk-size >0 required")
    a = chunk_index * chunk_size
    return z.iloc[a:a + chunk_size].copy()


def estimate(z: pd.DataFrame, start: str, end: str, gap_seconds: float) -> dict:
    a = pd.Timestamp(start); b = pd.Timestamp(end)
    if b <= a:
        raise ValueError("end must be after start")
    # Business-day count is deliberately an upper-ish planning approximation;
    # exchange holidays reduce actual pages while sparse minutes can reduce them further.
    days = max(1, int(len(pd.bdate_range(a.normalize().tz_localize(None), b.normalize().tz_localize(None)))))
    rows = []
    for market, g in z.groupby("market"):
        bpd = EMPIRICAL_BARS_PER_TRADING_DAY.get(str(market), 1440)
        bars = int(len(g) * days * bpd)
        pages = int(math.ceil(bars / PAGE_SIZE))
        rows.append({"market":str(market),"symbols":int(len(g)),"business_days":days,
                     "empirical_bars_per_trading_day":bpd,"estimated_bars":bars,
                     "estimated_pages":pages,"estimated_serial_hours":pages*gap_seconds/3600.0})
    return {"rows":rows,"estimated_pages":sum(x["estimated_pages"] for x in rows),
            "estimated_serial_hours":sum(x["estimated_serial_hours"] for x in rows),
            "chart_gap_seconds":gap_seconds}


def run(a) -> dict:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    sleeves = [x.strip() for x in a.sleeves.split(",") if x.strip()] if a.sleeves else None
    allz = load_manifest(a.manifest, sleeves)
    z = select_chunk(allz, a.chunk_index, a.chunk_size)
    plan = {
        "mode": MODE, "live_approval": False, "manifest": a.manifest,
        "selected_total_symbols": int(len(allz)), "chunk_index": int(a.chunk_index),
        "chunk_size": int(a.chunk_size), "chunk_symbols": int(len(z)),
        "symbols": z[["market","symbol","sleeves"]].to_dict(orient="records"),
        "start": a.start, "end": a.end,
        "estimate": estimate(z, a.start, a.end, a.chart_gap_seconds),
    }
    print("=== UNIFIED_CACHE_PLAN ===")
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
        print(f"\nUNIFIED_ADJ {i+1}/{len(z)} {r.market} {sym} {r.sleeves}", flush=True)
        st = cache_range(con, client, kind="stock", symbol=sym, adjusted=True,
                         start=a.start, end=a.end, max_pages=a.max_pages, progress_every=a.progress_every)
        results.append({"market":r.market,"symbol":sym,"sleeves":r.sleeves,
                        "done":int(st.get("done",0)),"pages":int(st.get("pages",0)),
                        "stored_rows":int(st.get("stored_rows",0)),"stop_reason":st.get("stop_reason")})

    if a.include_indicators:
        markets = set(z.market.astype(str))
        if "KR" in markets:
            for ind in ("KOSPI", "KOSDAQ"):
                print(f"\nUNIFIED_INDICATOR {ind}", flush=True)
                st = cache_range(con, client, kind="indicator", symbol=ind, adjusted=False,
                                 start=a.start, end=a.end, max_pages=a.max_pages, progress_every=a.progress_every)
                results.append({"market":"KR","symbol":ind,"sleeves":"REGIME_INDICATOR","done":int(st.get("done",0)),
                                "pages":int(st.get("pages",0)),"stored_rows":int(st.get("stored_rows",0)),
                                "stop_reason":st.get("stop_reason")})
    candle_rows = int(con.execute("SELECT COUNT(*) FROM candles").fetchone()[0])
    con.close()
    state = {**plan, "execute": True, "datasets":results, "done":int(sum(x["done"] for x in results)),
             "dataset_count":len(results),"sqlite_candle_rows":candle_rows}
    out = Path(a.outdir); out.mkdir(parents=True, exist_ok=True)
    (out / f"chunk_{a.chunk_index:03d}_state.json").write_text(json.dumps(state,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    pd.DataFrame(results).to_csv(out / f"chunk_{a.chunk_index:03d}_datasets.csv", index=False, encoding="utf-8-sig")
    print("=== UNIFIED_CACHE_STATE ===")
    print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
    return state


def self_test() -> None:
    import tempfile
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)/"m.csv"
        pd.DataFrame([
            {"symbol":"005930","market":"KR","sleeves":"KR_KOSPI"},
            {"symbol":"AAPL","market":"US","sleeves":"US_NDX|US_SP500"},
            {"symbol":"MSFT","market":"US","sleeves":"US_SP500"},
        ]).to_csv(p,index=False)
        z=load_manifest(p,["US_NDX"]); assert list(z.symbol)==["AAPL"]
        z2=load_manifest(p); c=select_chunk(z2,0,2); assert len(c)==2
        e=estimate(z2,"2026-01-01T00:00:00+00:00","2026-01-10T00:00:00+00:00",.4)
        assert e["estimated_pages"]>0
    print("TOSS_UNIFIED_ADJUSTED_CACHE_V001_SELF_TEST=PASS")


def main() -> None:
    ap=argparse.ArgumentParser()
    ap.add_argument("--manifest",default="unified_pit_membership_v001/kr_union_manifest.csv")
    ap.add_argument("--sleeves",default="KR_KOSPI,KR_KOSDAQ")
    ap.add_argument("--db",default="toss_replay_cache/toss_1m.sqlite")
    ap.add_argument("--start",default="2025-09-01T00:00:00+00:00")
    ap.add_argument("--end",default="2026-08-12T00:00:00+00:00")
    ap.add_argument("--chunk-index",type=int,default=0)
    ap.add_argument("--chunk-size",type=int,default=20)
    ap.add_argument("--chart-gap-seconds",type=float,default=.40)
    ap.add_argument("--max-pages",type=int,default=100000)
    ap.add_argument("--progress-every",type=int,default=50)
    ap.add_argument("--include-indicators",action="store_true")
    ap.add_argument("--execute",action="store_true")
    ap.add_argument("--outdir",default="toss_unified_adjusted_cache_v001")
    ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()
    if a.self_test:self_test();return
    run(a)


if __name__=="__main__":main()
