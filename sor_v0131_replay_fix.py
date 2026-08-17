from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

import sor_v013_2024_1m_audit as v13
from sor_us_rth_calendar import RTH_OPEN_MINUTE, is_early_close, session_end_minute
from toss_sqlite_cache_v001 import db_connect

MODE = "SOR_V0131_REPLAY_COVERAGE_FIX_NO_ORDERS"
OUTDIR = v13.OUTDIR
DB_PATH = v13.DB_PATH
NY_TZ = v13.NY_TZ

# Toss/vendor minute conventions can omit a handful of otherwise uneventful
# minutes. Keep the audit strict enough to detect broken sessions.
MIN_REGULAR_BARS = 375
MIN_EARLY_BARS = 195


def robust_load_1m(
    con: sqlite3.Connection,
    ticker: str,
    adjusted: bool,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Load by symbol/basis, convert to New-York time, then keep true core RTH.

    Early-close dates are capped at 13:00 ET. This is important because Toss can
    return sparse post-close/extended-hours prints between 13:00 and 16:00 on
    those dates; those bars must not enter the RTH replay baseline.
    """
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
    q = q.loc[good].copy()
    idx = idx[good]
    q.index = pd.DatetimeIndex(idx).tz_convert(NY_TZ)
    q.columns = [c.capitalize() for c in q.columns]
    q = q[~q.index.duplicated(keep="last")].sort_index()

    s = pd.Timestamp(start_date).date()
    e = pd.Timestamp(end_date).date()
    local_dates = np.array(q.index.date)
    date_mask = np.array([(d >= s and d <= e) for d in local_dates], dtype=bool)
    q = q.loc[date_mask]
    if q.empty:
        return q

    mins = np.asarray(q.index.hour * 60 + q.index.minute)
    day_strings = q.index.strftime("%Y-%m-%d")
    end_mins = np.array([session_end_minute(ds) for ds in day_strings], dtype=int)
    core_mask = (mins >= RTH_OPEN_MINUTE) & (mins < end_mins)
    return q.loc[core_mask]


def source_coverage(df: pd.DataFrame, expected_dates: list[str]) -> tuple[list[dict], bool, int]:
    rows: list[dict] = []
    all_ok = True
    good_days = 0
    date_arr = np.array(df.index.date) if len(df) else np.array([])

    for ds in expected_dates:
        d = pd.Timestamp(ds).date()
        g = df[date_arr == d] if len(df) else df
        n = len(g)
        early_day = is_early_close(ds)
        if n:
            first = g.index[0]
            last = g.index[-1]
            fm = first.hour * 60 + first.minute
            lm = last.hour * 60 + last.minute
            if early_day:
                ok = n >= MIN_EARLY_BARS and fm <= 575 and lm >= 775
            else:
                ok = n >= MIN_REGULAR_BARS and fm <= 575 and lm >= 955
        else:
            first = last = pd.NaT
            ok = False
        if ok:
            good_days += 1
        all_ok = all_ok and ok
        rows.append({
            "date": ds,
            "rth_bars": int(n),
            "first": str(first) if n else "",
            "last": str(last) if n else "",
            "coverage_ok": bool(ok),
            "early_close": bool(early_day),
        })
    return rows, all_ok, good_days


def best_source_and_coverage(
    con: sqlite3.Connection,
    ticker: str,
    start_date: str,
    end_date: str,
    expected_dates: list[str],
) -> tuple[pd.DataFrame, list[dict], bool]:
    adj = robust_load_1m(con, ticker, True, start_date, end_date)
    raw = robust_load_1m(con, ticker, False, start_date, end_date)

    adj_rows, adj_all, adj_good = source_coverage(adj, expected_dates)
    raw_rows, raw_all, raw_good = source_coverage(raw, expected_dates)

    if adj_all:
        chosen_name, chosen, chosen_rows, chosen_all = "adjusted", adj, adj_rows, True
    elif raw_all:
        chosen_name, chosen, chosen_rows, chosen_all = "raw_fallback", raw, raw_rows, True
    elif adj_good >= raw_good and len(adj) > 0:
        chosen_name, chosen, chosen_rows, chosen_all = "adjusted_partial", adj, adj_rows, False
    else:
        chosen_name, chosen, chosen_rows, chosen_all = "raw_partial", raw, raw_rows, False

    by_date_adj = {r["date"]: r for r in adj_rows}
    by_date_raw = {r["date"]: r for r in raw_rows}
    out_rows = []
    for r in chosen_rows:
        ds = r["date"]
        a = by_date_adj.get(ds, {})
        b = by_date_raw.get(ds, {})
        x = dict(r)
        x.update({
            "ticker": ticker,
            "source": chosen_name,
            "adjusted_rth_bars": int(a.get("rth_bars", 0)),
            "raw_rth_bars": int(b.get("rth_bars", 0)),
            "adjusted_coverage_ok": bool(a.get("coverage_ok", False)),
            "raw_coverage_ok": bool(b.get("coverage_ok", False)),
        })
        out_rows.append(x)
    return chosen, out_rows, chosen_all


def diagnose(outdir: Path, db_path: Path) -> dict:
    plan, _ = v13.load_plan(outdir)
    con = db_connect(db_path)
    try:
        db_rows = pd.read_sql_query(
            "SELECT adjusted, COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols, "
            "MIN(timestamp) AS earliest, MAX(timestamp) AS latest "
            "FROM candles WHERE kind='stock' GROUP BY adjusted ORDER BY adjusted",
            con,
        )
        diag_rows = []
        trade_ok_adj = trade_ok_raw = trade_ok_best = 0
        source_counts: dict[str, int] = {}

        for _, trade in plan.iterrows():
            ticker = str(trade["ticker"])
            start_date = pd.Timestamp(trade["entry_time"]).strftime("%Y-%m-%d")
            end_date = str(trade["hard_end_date"])
            expected = [x for x in str(trade["expected_dates"]).split("|") if x]
            adj = robust_load_1m(con, ticker, True, start_date, end_date)
            raw = robust_load_1m(con, ticker, False, start_date, end_date)
            _, aok, agood = source_coverage(adj, expected)
            _, rok, rgood = source_coverage(raw, expected)
            _, cr, bok = best_source_and_coverage(con, ticker, start_date, end_date, expected)
            source = cr[0]["source"] if cr else "none"
            source_counts[source] = source_counts.get(source, 0) + 1
            trade_ok_adj += int(aok)
            trade_ok_raw += int(rok)
            trade_ok_best += int(bok)
            diag_rows.append({
                "trade_id": trade["trade_id"],
                "ticker": ticker,
                "expected_days": len(expected),
                "adjusted_rows": len(adj),
                "raw_rows": len(raw),
                "adjusted_good_days": agood,
                "raw_good_days": rgood,
                "adjusted_all_days_ok": aok,
                "raw_all_days_ok": rok,
                "chosen_source": source,
                "best_all_days_ok": bok,
            })
    finally:
        con.close()

    ddf = pd.DataFrame(diag_rows)
    ddf.to_csv(outdir / "v0131_coverage_diagnose.csv", index=False, encoding="utf-8-sig")
    summary = {
        "mode": MODE,
        "selected_trades": int(len(plan)),
        "db_basis": db_rows.to_dict(orient="records"),
        "adjusted_full_coverage_trades": int(trade_ok_adj),
        "raw_full_coverage_trades": int(trade_ok_raw),
        "best_source_full_coverage_trades": int(trade_ok_best),
        "best_source_coverage_pct": 100.0 * trade_ok_best / len(plan) if len(plan) else 0.0,
        "source_counts": source_counts,
        "diagnosis": (
            "NO_1M_ROWS_RECOLLECT" if int(db_rows["rows"].sum()) == 0
            else "REPLAY_READY" if trade_ok_best > 0
            else "DATA_PRESENT_BUT_SESSION_COVERAGE_INCOMPLETE"
        ),
    }
    (outdir / "v0131_diagnose.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return summary


def replay_fixed(outdir: Path, db_path: Path) -> None:
    # The original replay normalizes the selected minute source to the frozen
    # daily entry-price basis. Replace only its source/coverage loader.
    original = v13.paired_and_coverage
    v13.paired_and_coverage = best_source_and_coverage
    try:
        v13.replay(outdir, db_path)
    finally:
        v13.paired_and_coverage = original

    p = outdir / "replay_summary.csv"
    if p.exists():
        s = pd.read_csv(p)
        if len(s) and int(s.loc[0, "complete_audits"]) == 0:
            s.loc[0, "next_step"] = "CHECK_COLLECTION_OR_COVERAGE_NOT_STRATEGY_RESULT"
            s.to_csv(p, index=False, encoding="utf-8-sig")
            state = s.iloc[0].to_dict()
            (outdir / "replay_state.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
            print("\n=== V013.1 CORRECTED STATUS ===")
            print(json.dumps(state, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["diagnose", "replay", "diagnose-replay"], default="diagnose")
    ap.add_argument("--outdir", default=str(OUTDIR))
    ap.add_argument("--db", default=str(DB_PATH))
    a = ap.parse_args()
    outdir = Path(a.outdir)
    db = Path(a.db)
    if a.mode in {"diagnose", "diagnose-replay"}:
        diagnose(outdir, db)
    if a.mode in {"replay", "diagnose-replay"}:
        replay_fixed(outdir, db)


if __name__ == "__main__":
    main()
