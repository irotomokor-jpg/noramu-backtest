from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

import sor_v013_2024_1m_audit as v13
from toss_replay_source_v001 import TossReplayClient
from toss_sqlite_cache_v001 import db_connect, normalized_tuple, safe_page

MODE = "SOR_V0133_RTH_DAY_COLLECTOR_NO_ORDERS"
LIVE_APPROVAL = False
NY_TZ = "America/New_York"
OUTDIR = v13.OUTDIR
DB_PATH = v13.DB_PATH
MIN_REGULAR_BARS = 375
MIN_EARLY_BARS = 195


def _local_day_rows(con: sqlite3.Connection, ticker: str, day: str, adjusted: bool = True) -> pd.DataFrame:
    q = pd.read_sql_query(
        "SELECT timestamp,open,high,low,close,volume FROM candles "
        "WHERE kind='stock' AND symbol=? AND adjusted=? ORDER BY timestamp",
        con,
        params=(ticker, int(adjusted)),
    )
    if q.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    idx = pd.to_datetime(q.pop("timestamp"), utc=True, errors="coerce")
    good = idx.notna()
    q = q.loc[good].copy(); idx = idx[good]
    q.index = pd.DatetimeIndex(idx).tz_convert(NY_TZ)
    q.columns = [c.capitalize() for c in q.columns]
    target = pd.Timestamp(day).date()
    q = q[np.array([x.date() == target for x in q.index])]
    mins = q.index.hour * 60 + q.index.minute
    q = q[(mins >= 570) & (mins < 960)]
    return q[~q.index.duplicated(keep="last")].sort_index()


def coverage_ok(df: pd.DataFrame) -> bool:
    if df.empty:
        return False
    n = len(df)
    first = df.index[0]
    last = df.index[-1]
    fm = first.hour * 60 + first.minute
    lm = last.hour * 60 + last.minute
    regular = n >= MIN_REGULAR_BARS and fm <= 575 and lm >= 955
    early = n >= MIN_EARLY_BARS and fm <= 575 and 770 <= lm <= 790
    return bool(regular or early)


def required_days(outdir: Path) -> pd.DataFrame:
    plan, _ = v13.load_plan(outdir)
    pairs: set[tuple[str, str]] = set()
    for _, r in plan.iterrows():
        ticker = str(r["ticker"])
        for ds in str(r["expected_dates"]).split("|"):
            ds = ds.strip()
            if ds:
                pairs.add((ticker, ds))
    return pd.DataFrame(sorted(pairs), columns=["ticker", "date"])


def _day_end_iso(day: str) -> str:
    # 15:59:59 ET deliberately excludes after-hours candles while remaining
    # after the final ordinary RTH 1-minute bar on standard sessions.
    return (pd.Timestamp(day + " 15:59:59").tz_localize(NY_TZ)).isoformat()


def _manual_before(oldest: str) -> str:
    dt = pd.Timestamp(oldest)
    if dt.tzinfo is None:
        dt = dt.tz_localize("UTC")
    return (dt - pd.Timedelta(seconds=1)).isoformat()


def collect_one_day(
    con: sqlite3.Connection,
    client: TossReplayClient,
    ticker: str,
    day: str,
    *,
    max_pages: int = 6,
) -> dict[str, Any]:
    before = _day_end_iso(day)
    target_date = pd.Timestamp(day).date()
    pages = 0
    api_rows = 0
    inserted = 0
    manual_steps = 0
    stop_reason = "MAX_PAGES"
    seen: set[str] = set()

    for _ in range(max_pages):
        pages += 1
        rows, nxt = safe_page(
            client, kind="stock", symbol=ticker, adjusted=True, before=before
        )
        api_rows += len(rows)
        if not rows:
            stop_reason = "EMPTY_PAGE"
            break

        tuples = []
        stamps: list[str] = []
        local_times: list[pd.Timestamp] = []
        for row in rows:
            tup = normalized_tuple("stock", ticker, True, row)
            if tup is None:
                continue
            stamps.append(tup[3])
            ts = pd.Timestamp(tup[3])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            local = ts.tz_convert(NY_TZ)
            local_times.append(local)
            if local.date() == target_date and 570 <= local.hour * 60 + local.minute < 960:
                tuples.append(tup)

        if tuples:
            before_changes = con.total_changes
            con.executemany(
                "INSERT OR IGNORE INTO candles(kind,symbol,adjusted,timestamp,open,high,low,close,volume) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                tuples,
            )
            inserted += con.total_changes - before_changes
            con.commit()

        if not stamps or not local_times:
            stop_reason = "NO_VALID_TIMESTAMPS"
            break

        oldest_i = int(np.argmin([x.value for x in local_times]))
        oldest_local = local_times[oldest_i]
        oldest_stamp = stamps[oldest_i]

        # Once the fetched page has reached the RTH open (or earlier), the day
        # has been fully traversed for our replay purpose. We do not need to
        # paginate across the previous trading day.
        if oldest_local.date() < target_date or (
            oldest_local.date() == target_date
            and oldest_local.hour * 60 + oldest_local.minute <= 570
        ):
            stop_reason = "RTH_OPEN_REACHED"
            break

        if nxt:
            new_before = str(nxt)
        else:
            # Observed Toss US behaviour can omit nextBefore at a session/page
            # boundary even when earlier same-day candles are still queryable.
            # before is inclusive, so use one second before the oldest candle.
            new_before = _manual_before(oldest_stamp)
            manual_steps += 1

        if new_before == before or new_before in seen:
            stop_reason = "CURSOR_REPEAT"
            break
        seen.add(new_before)
        before = new_before

    df = _local_day_rows(con, ticker, day, adjusted=True)
    ok = coverage_ok(df)
    if ok:
        stop_reason = "COMPLETE_RTH"
    return {
        "ticker": ticker,
        "date": day,
        "pages": pages,
        "api_rows": api_rows,
        "inserted_rows": int(inserted),
        "rth_rows_after": int(len(df)),
        "coverage_ok": bool(ok),
        "first": str(df.index[0]) if len(df) else "",
        "last": str(df.index[-1]) if len(df) else "",
        "manual_before_steps": int(manual_steps),
        "stop_reason": stop_reason,
    }


def probe(outdir: Path, db_path: Path, ticker: str, day: str, chart_gap: float) -> dict[str, Any]:
    con = db_connect(db_path)
    client = TossReplayClient()
    client.gate._gap["MARKET_DATA_CHART"] = max(0.23, chart_gap)
    try:
        before_df = _local_day_rows(con, ticker, day, adjusted=True)
        result = collect_one_day(con, client, ticker, day)
        result["rth_rows_before"] = int(len(before_df))
    finally:
        con.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return result


def collect_all(outdir: Path, db_path: Path, chart_gap: float) -> pd.DataFrame:
    days = required_days(outdir)
    con = db_connect(db_path)
    client = TossReplayClient()
    client.gate._gap["MARKET_DATA_CHART"] = max(0.23, chart_gap)
    results: list[dict[str, Any]] = []
    already = 0
    try:
        total = len(days)
        for i, r in days.iterrows():
            ticker = str(r["ticker"]); day = str(r["date"])
            current = _local_day_rows(con, ticker, day, adjusted=True)
            if coverage_ok(current):
                already += 1
                result = {
                    "ticker": ticker, "date": day, "pages": 0, "api_rows": 0,
                    "inserted_rows": 0, "rth_rows_after": int(len(current)),
                    "coverage_ok": True,
                    "first": str(current.index[0]), "last": str(current.index[-1]),
                    "manual_before_steps": 0, "stop_reason": "ALREADY_COMPLETE",
                }
            else:
                result = collect_one_day(con, client, ticker, day)
            results.append(result)
            if (i + 1) % 25 == 0 or not result["coverage_ok"] or i + 1 == total:
                complete = sum(int(x["coverage_ok"]) for x in results)
                print(
                    f"RTH_DAY {i+1}/{total} {ticker} {day} rows={result['rth_rows_after']} "
                    f"ok={int(result['coverage_ok'])} pages={result['pages']} "
                    f"complete_so_far={complete}",
                    flush=True,
                )
    finally:
        con.close()

    df = pd.DataFrame(results)
    df.to_csv(outdir / "v0133_rth_day_collection.csv", index=False, encoding="utf-8-sig")
    summary = {
        "mode": MODE,
        "required_ticker_days": int(len(df)),
        "coverage_complete_days": int(df["coverage_ok"].sum()) if len(df) else 0,
        "coverage_pct": 100.0 * float(df["coverage_ok"].mean()) if len(df) else 0.0,
        "already_complete_days": int((df["stop_reason"] == "ALREADY_COMPLETE").sum()) if len(df) else 0,
        "api_pages": int(df["pages"].sum()) if len(df) else 0,
        "api_rows": int(df["api_rows"].sum()) if len(df) else 0,
        "inserted_rows": int(df["inserted_rows"].sum()) if len(df) else 0,
        "manual_before_steps": int(df["manual_before_steps"].sum()) if len(df) else 0,
        "incomplete_days": int((~df["coverage_ok"]).sum()) if len(df) else 0,
        "stop_reasons": df["stop_reason"].value_counts().to_dict() if len(df) else {},
    }
    (outdir / "v0133_rth_day_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return df


def status(outdir: Path, db_path: Path) -> dict[str, Any]:
    days = required_days(outdir)
    con = db_connect(db_path)
    rows = []
    try:
        for _, r in days.iterrows():
            df = _local_day_rows(con, str(r["ticker"]), str(r["date"]), adjusted=True)
            rows.append({"ticker": r["ticker"], "date": r["date"], "rows": len(df), "ok": coverage_ok(df)})
    finally:
        con.close()
    z = pd.DataFrame(rows)
    summary = {
        "required_ticker_days": int(len(z)),
        "complete_days": int(z["ok"].sum()) if len(z) else 0,
        "coverage_pct": 100.0 * float(z["ok"].mean()) if len(z) else 0.0,
        "median_rows": float(z["rows"].median()) if len(z) else 0.0,
        "min_rows": int(z["rows"].min()) if len(z) else 0,
        "max_rows": int(z["rows"].max()) if len(z) else 0,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["probe", "collect", "status"], default="probe")
    ap.add_argument("--outdir", default=str(OUTDIR))
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--ticker", default="AAPL")
    ap.add_argument("--date", default="2025-12-16")
    ap.add_argument("--chart-gap-seconds", type=float, default=0.40)
    a = ap.parse_args()
    outdir = Path(a.outdir); db = Path(a.db)
    if a.mode == "probe":
        probe(outdir, db, a.ticker, a.date, a.chart_gap_seconds)
    elif a.mode == "collect":
        collect_all(outdir, db, a.chart_gap_seconds)
    else:
        status(outdir, db)


if __name__ == "__main__":
    main()
