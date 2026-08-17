from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

import sor_v0133_rth_day_collector as v133
from toss_sqlite_cache_v001 import db_connect

MODE = "SOR_V0133_FAST_STATUS_NO_ORDERS"


def fast_status(outdir: Path, db_path: Path) -> dict:
    required = v133.required_days(outdir).copy()
    required["ticker"] = required["ticker"].astype(str)
    required["date"] = required["date"].astype(str)
    needed = set(map(tuple, required[["ticker", "date"]].itertuples(index=False, name=None)))

    con = db_connect(db_path)
    stats: dict[tuple[str, str], dict] = {}
    try:
        tickers = sorted(required["ticker"].unique())
        total_tickers = len(tickers)
        for i, ticker in enumerate(tickers, 1):
            q = pd.read_sql_query(
                "SELECT timestamp FROM candles "
                "WHERE kind='stock' AND symbol=? AND adjusted=1 ORDER BY timestamp",
                con,
                params=(ticker,),
            )
            if not q.empty:
                idx = pd.to_datetime(q["timestamp"], utc=True, errors="coerce")
                idx = pd.DatetimeIndex(idx[idx.notna()]).tz_convert(v133.NY_TZ)
                mins = idx.hour * 60 + idx.minute
                idx = idx[(mins >= 570) & (mins < 960)]

                if len(idx):
                    tmp = pd.DataFrame({"ts": idx})
                    tmp["date"] = tmp["ts"].dt.strftime("%Y-%m-%d")
                    for day, g in tmp.groupby("date", sort=False):
                        key = (ticker, str(day))
                        if key not in needed:
                            continue
                        times = pd.DatetimeIndex(g["ts"])
                        n = len(times)
                        first = times[0]
                        last = times[-1]
                        fm = first.hour * 60 + first.minute
                        lm = last.hour * 60 + last.minute
                        regular = n >= v133.MIN_REGULAR_BARS and fm <= 575 and lm >= 955
                        early = n >= v133.MIN_EARLY_BARS and fm <= 575 and 770 <= lm <= 790
                        stats[key] = {
                            "rows": int(n),
                            "ok": bool(regular or early),
                            "first": str(first),
                            "last": str(last),
                        }
            if i % 10 == 0 or i == total_tickers:
                print(f"FAST_STATUS {i}/{total_tickers} tickers", flush=True)
    finally:
        con.close()

    rows = []
    for ticker, day in required[["ticker", "date"]].itertuples(index=False, name=None):
        s = stats.get((ticker, day), {"rows": 0, "ok": False, "first": "", "last": ""})
        rows.append({"ticker": ticker, "date": day, **s})

    z = pd.DataFrame(rows)
    incomplete = z[~z["ok"]].copy()
    incomplete.to_csv(outdir / "v0133_incomplete_days.csv", index=False, encoding="utf-8-sig")

    summary = {
        "mode": MODE,
        "required_ticker_days": int(len(z)),
        "complete_days": int(z["ok"].sum()) if len(z) else 0,
        "incomplete_days": int((~z["ok"]).sum()) if len(z) else 0,
        "coverage_pct": 100.0 * float(z["ok"].mean()) if len(z) else 0.0,
        "median_rows": float(z["rows"].median()) if len(z) else 0.0,
        "min_rows": int(z["rows"].min()) if len(z) else 0,
        "max_rows": int(z["rows"].max()) if len(z) else 0,
        "zero_row_days": int((z["rows"] == 0).sum()) if len(z) else 0,
        "partial_nonzero_days": int(((z["rows"] > 0) & (~z["ok"])).sum()) if len(z) else 0,
        "incomplete_file": str(outdir / "v0133_incomplete_days.csv"),
    }
    (outdir / "v0133_fast_status.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=str(v133.OUTDIR))
    ap.add_argument("--db", default=str(v133.DB_PATH))
    a = ap.parse_args()
    fast_status(Path(a.outdir), Path(a.db))


if __name__ == "__main__":
    main()
