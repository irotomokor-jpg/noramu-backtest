from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

import pandas as pd

import sor_v013_2024_1m_audit as v13
from toss_sqlite_cache_v001 import db_connect
from toss_replay_source_v001 import TossReplayClient

MODE = "SOR_V0132_CACHE_STATE_DIAGNOSE_NO_ORDERS"


def summarize_cache(db_path: Path) -> dict:
    con = db_connect(db_path)
    try:
        states = pd.read_sql_query(
            "SELECT dataset_key,kind,symbol,adjusted,start_ts,end_ts,next_before,pages,api_rows,stored_rows,oldest_ts,newest_ts,done,stop_reason,updated_at "
            "FROM cache_state WHERE kind='stock' ORDER BY symbol,adjusted,start_ts",
            con,
        )
        candles = pd.read_sql_query(
            "SELECT adjusted,COUNT(*) rows,COUNT(DISTINCT symbol) symbols,MIN(timestamp) earliest,MAX(timestamp) latest "
            "FROM candles WHERE kind='stock' GROUP BY adjusted ORDER BY adjusted",
            con,
        )
    finally:
        con.close()

    if states.empty:
        out = {"mode": MODE, "cache_state_rows": 0, "diagnosis": "NO_CACHE_STATE"}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return out

    stop = (
        states.groupby(["adjusted", "stop_reason"], dropna=False)
        .agg(datasets=("dataset_key", "count"), pages_sum=("pages", "sum"), api_rows_sum=("api_rows", "sum"), stored_rows_sum=("stored_rows", "sum"))
        .reset_index()
    )
    page_dist = (
        states.groupby("adjusted")
        .agg(
            datasets=("dataset_key", "count"),
            pages_min=("pages", "min"),
            pages_median=("pages", "median"),
            pages_mean=("pages", "mean"),
            pages_max=("pages", "max"),
            api_rows_sum=("api_rows", "sum"),
            stored_rows_sum=("stored_rows", "sum"),
            done_pct=("done", lambda x: 100.0 * float(pd.Series(x).mean())),
        )
        .reset_index()
    )

    samples = []
    for reason, g in states.groupby(states["stop_reason"].fillna("NULL")):
        z = g.head(3)
        for _, r in z.iterrows():
            samples.append({
                "stop_reason": reason,
                "symbol": r["symbol"],
                "adjusted": int(r["adjusted"]),
                "pages": int(r["pages"]),
                "api_rows": int(r["api_rows"]),
                "stored_rows": int(r["stored_rows"]),
                "start_ts": r["start_ts"],
                "end_ts": r["end_ts"],
                "oldest_ts": r["oldest_ts"],
                "newest_ts": r["newest_ts"],
                "next_before": r["next_before"],
            })

    dominant = stop.sort_values("datasets", ascending=False).iloc[0]["stop_reason"] if len(stop) else None
    if dominant == "NO_NEXT_BEFORE":
        diagnosis = "API_RETURNED_NO_NEXT_BEFORE_OR_SOURCE_NOT_PAGINATING"
    elif dominant == "START_REACHED":
        diagnosis = "TIME_RANGE_COMPARISON_STOPPED_TOO_EARLY"
    elif dominant == "CURSOR_REPEAT":
        diagnosis = "NEXT_BEFORE_DID_NOT_ADVANCE"
    elif dominant == "EMPTY_PAGE":
        diagnosis = "API_EMPTY_PAGE_OR_RETENTION_LIMIT"
    else:
        diagnosis = "MIXED_OR_UNKNOWN_STOP_REASON"

    out = {
        "mode": MODE,
        "cache_state_rows": int(len(states)),
        "candles": candles.to_dict(orient="records"),
        "page_distribution": page_dist.to_dict(orient="records"),
        "stop_reasons": stop.to_dict(orient="records"),
        "samples": samples,
        "dominant_stop_reason": dominant,
        "diagnosis": diagnosis,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return out


def live_probe(db_path: Path, ticker: str | None = None) -> dict:
    plan, win = v13.load_plan(v13.OUTDIR)
    if ticker is None:
        ticker = str(win.iloc[0]["ticker"])
    w = win[win["ticker"].astype(str) == ticker].iloc[0]
    client = TossReplayClient()
    client.gate._gap["MARKET_DATA_CHART"] = 0.40

    rows1, next1 = client.stock_candles_page(ticker, "1m", count=200, before=str(w["end_iso"]), adjusted=True)
    rows2, next2 = ([], None)
    if next1:
        rows2, next2 = client.stock_candles_page(ticker, "1m", count=200, before=str(next1), adjusted=True)

    def stamps(rows):
        xs = [str(r.get("timestamp")) for r in rows if r.get("timestamp")]
        return {"count": len(xs), "newest": max(xs) if xs else None, "oldest": min(xs) if xs else None}

    out = {
        "mode": MODE,
        "ticker": ticker,
        "requested_end": str(w["end_iso"]),
        "page1": stamps(rows1),
        "nextBefore1": next1,
        "page2": stamps(rows2),
        "nextBefore2": next2,
        "next_before_advances": bool(next1 and next2 and str(next2) != str(next1)),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["state", "probe"], default="state")
    ap.add_argument("--db", default=str(v13.DB_PATH))
    ap.add_argument("--ticker", default=None)
    a = ap.parse_args()
    if a.mode == "state":
        summarize_cache(Path(a.db))
    else:
        live_probe(Path(a.db), a.ticker)


if __name__ == "__main__":
    main()
