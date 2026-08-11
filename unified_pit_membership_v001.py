#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build/validate point-in-time membership manifests for the unified research universe.

Research only / NO_ORDERS. No broker account/order endpoints are used.

KR historical replay:
- monthly KOSPI top-N and KOSDAQ top-N snapshots
- source row is the latest marcap close known on or before each effective date
- preferred shares, SPACs and REITs are excluded

US historical replay:
- requires an externally supplied PIT constituent snapshot CSV
- current-membership lists are never silently accepted as historical PIT data

The output schema is shared across KR/US so downstream Toss collection can dedupe
symbols while retaining sleeve membership metadata.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

MODE = "UNIFIED_PIT_MEMBERSHIP_RESEARCH_NO_ORDERS"
LIVE_APPROVAL = False
MARCAP_URL = "https://raw.githubusercontent.com/FinanceData/marcap/master/data/marcap-{year}.parquet"
DEFAULT_START = "2025-09-01"
DEFAULT_END = "2026-08-01"
ALLOWED_KR = {"KOSPI": "KR_KOSPI", "KOSDAQ": "KR_KOSDAQ"}
ALLOWED_US = {"US_SP500", "US_NDX"}


def month_starts(start: str, end: str) -> list[pd.Timestamp]:
    a = pd.Timestamp(start).normalize().replace(day=1)
    b = pd.Timestamp(end).normalize().replace(day=1)
    if b < a:
        raise ValueError("end before start")
    return list(pd.date_range(a, b, freq="MS"))


def load_marcap_years(years: Iterable[int]) -> pd.DataFrame:
    parts = []
    for year in sorted(set(int(y) for y in years)):
        df = pd.read_parquet(MARCAP_URL.format(year=year))
        dates = pd.to_datetime(df["Date"], errors="coerce") if "Date" in df.columns else pd.to_datetime(df.index, errors="coerce")
        z = df.copy()
        z["_date"] = dates
        z["_source_year"] = year
        parts.append(z.dropna(subset=["_date"]))
    if not parts:
        raise RuntimeError("no marcap years loaded")
    return pd.concat(parts, ignore_index=True)


def _clean_kr_snapshot(z: pd.DataFrame, market: str, top_n: int) -> pd.DataFrame:
    required = {"Code", "Name", "Market", "Marcap"}
    miss = required - set(z.columns)
    if miss:
        raise RuntimeError(f"marcap schema missing {sorted(miss)}")
    q = z.copy()
    q["market_norm"] = q.Market.astype(str).str.upper().str.strip()
    q = q[q.market_norm == market].copy()
    q["symbol"] = q.Code.astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    q["name"] = q.Name.astype(str).str.strip()
    q["marcap"] = pd.to_numeric(q.Marcap, errors="coerce")
    bad = (
        q.name.str.contains("스팩", na=False)
        | q.name.str.contains("리츠", na=False)
        | q.name.str.contains("우선주", na=False)
        | q.name.str.contains(r"(?:\d+)?우(?:B|C)?$", regex=True, na=False)
    )
    q = q[~bad].dropna(subset=["marcap"])
    q = q[q.marcap > 0].sort_values(["marcap", "symbol"], ascending=[False, True]).head(int(top_n)).copy()
    if len(q) < int(top_n):
        raise RuntimeError(f"only {len(q)} eligible {market} names; requested {top_n}")
    q["rank"] = np.arange(1, len(q) + 1)
    return q[["symbol", "name", "marcap", "rank"]]


def kr_snapshot_asof(marcap: pd.DataFrame, effective: pd.Timestamp, market: str, top_n: int) -> pd.DataFrame:
    effective = pd.Timestamp(effective).normalize()
    eligible = marcap.loc[marcap._date.dt.normalize() <= effective, "_date"]
    if eligible.empty:
        raise RuntimeError(f"no marcap data known by {effective.date()}")
    source_date = pd.Timestamp(eligible.max()).normalize()
    day = marcap[marcap._date.dt.normalize() == source_date].copy()
    q = _clean_kr_snapshot(day, market, top_n)
    q["market"] = "KR"
    q["sleeve"] = ALLOWED_KR[market]
    q["effective_date"] = effective
    q["source_date"] = source_date
    q["membership_source"] = "FinanceData_marcap"
    q["point_in_time"] = True
    return q


def build_kr_monthly(start: str, end: str, top_n: int = 100, marcap: pd.DataFrame | None = None) -> pd.DataFrame:
    effs = month_starts(start, end)
    years = {d.year for d in effs} | {(effs[0] - pd.Timedelta(days=7)).year}
    m = marcap if marcap is not None else load_marcap_years(years)
    parts = []
    for effective in effs:
        for market in ("KOSPI", "KOSDAQ"):
            parts.append(kr_snapshot_asof(m, effective, market, top_n))
    out = pd.concat(parts, ignore_index=True)
    validate_pit(out, expected_snapshot_size=top_n)
    return add_valid_until(out)


def add_valid_until(df: pd.DataFrame) -> pd.DataFrame:
    z = df.copy()
    z["effective_date"] = pd.to_datetime(z.effective_date)
    next_map = {}
    for sleeve, g in z[["sleeve", "effective_date"]].drop_duplicates().groupby("sleeve"):
        dates = sorted(pd.to_datetime(g.effective_date).unique())
        for i, d in enumerate(dates):
            next_map[(sleeve, pd.Timestamp(d))] = pd.Timestamp(dates[i + 1]) if i + 1 < len(dates) else pd.NaT
    z["valid_until"] = [next_map[(r.sleeve, pd.Timestamp(r.effective_date))] for r in z.itertuples()]
    return z


def validate_pit(df: pd.DataFrame, expected_snapshot_size: int | None = None) -> None:
    required = {"symbol", "market", "sleeve", "effective_date", "source_date", "membership_source", "point_in_time"}
    miss = required - set(df.columns)
    if miss:
        raise ValueError(f"PIT manifest missing {sorted(miss)}")
    z = df.copy()
    z["effective_date"] = pd.to_datetime(z.effective_date, errors="coerce")
    z["source_date"] = pd.to_datetime(z.source_date, errors="coerce")
    if z[["effective_date", "source_date"]].isna().any().any():
        raise ValueError("invalid PIT dates")
    if (z.source_date > z.effective_date).any():
        raise ValueError("future source_date leakage")
    truth = z.point_in_time.astype(str).str.lower().isin({"true", "1"})
    if not bool(truth.all()):
        raise ValueError("historical PIT manifest contains point_in_time=false")
    if z.duplicated(["symbol", "sleeve", "effective_date"]).any():
        raise ValueError("duplicate PIT membership row")
    if expected_snapshot_size is not None:
        n = z.groupby(["sleeve", "effective_date"]).size()
        if len(n) and not bool((n == int(expected_snapshot_size)).all()):
            raise ValueError(f"snapshot size mismatch: {n[n != int(expected_snapshot_size)].to_dict()}")


def load_us_pit_csv(path: str | Path) -> pd.DataFrame:
    z = pd.read_csv(path, dtype={"symbol": str})
    required = {"symbol", "sleeve", "effective_date", "source_date", "membership_source", "point_in_time"}
    miss = required - set(z.columns)
    if miss:
        raise ValueError(f"US PIT CSV missing {sorted(miss)}")
    z["market"] = "US"
    z["symbol"] = z.symbol.astype(str).str.upper().str.strip()
    if not set(z.sleeve.astype(str)).issubset(ALLOWED_US):
        raise ValueError(f"US PIT CSV has unsupported sleeves: {sorted(set(z.sleeve.astype(str)) - ALLOWED_US)}")
    validate_pit(z)
    return add_valid_until(z)


def load_current_forward_csv(path: str | Path) -> pd.DataFrame:
    z = pd.read_csv(path, dtype={"symbol": str})
    required = {"symbol", "sleeve"}
    miss = required - set(z.columns)
    if miss:
        raise ValueError(f"current membership CSV missing {sorted(miss)}")
    z["symbol"] = z.symbol.astype(str).str.upper().str.strip()
    z["market"] = np.where(z.sleeve.astype(str).str.startswith("US_"), "US", "KR")
    z["point_in_time"] = False
    z["membership_source"] = z.get("membership_source", "CURRENT_FORWARD_ONLY")
    return z


def historical_guard(df: pd.DataFrame) -> None:
    if df.empty:
        raise ValueError("historical membership is empty")
    truth = df.point_in_time.astype(str).str.lower().isin({"true", "1"}) if "point_in_time" in df else pd.Series(False, index=df.index)
    if not bool(truth.all()):
        raise ValueError("CURRENT_MEMBERSHIP_BLOCKED_FOR_HISTORICAL_REPLAY")
    validate_pit(df)


def union_manifest(df: pd.DataFrame) -> pd.DataFrame:
    z = df.copy()
    z["effective_date"] = pd.to_datetime(z.effective_date)
    rows = []
    for (symbol, market), g in z.groupby(["symbol", "market"], sort=True):
        rows.append({
            "symbol": symbol,
            "market": market,
            "sleeves": "|".join(sorted(set(g.sleeve.astype(str)))),
            "first_effective_date": g.effective_date.min(),
            "last_effective_date": g.effective_date.max(),
            "snapshot_rows": int(len(g)),
            "name": str(g.name.dropna().iloc[-1]) if "name" in g and g.name.notna().any() else "",
        })
    return pd.DataFrame(rows)


def turnover(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    z = df.copy(); z["effective_date"] = pd.to_datetime(z.effective_date)
    for sleeve, g in z.groupby("sleeve"):
        dates = sorted(g.effective_date.unique())
        for i in range(1, len(dates)):
            a = set(g[g.effective_date == dates[i-1]].symbol.astype(str))
            b = set(g[g.effective_date == dates[i]].symbol.astype(str))
            rows.append({"sleeve": sleeve, "from": pd.Timestamp(dates[i-1]), "to": pd.Timestamp(dates[i]),
                         "kept": len(a & b), "entered": len(b-a), "exited": len(a-b),
                         "turnover_fraction": len(b-a)/len(b) if b else np.nan})
    return pd.DataFrame(rows)


def self_test() -> None:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    dates = pd.to_datetime(["2025-08-29"] * 6 + ["2025-09-01"] * 6)
    fake = pd.DataFrame({
        "Date": dates,
        "Code": ["000001","000002","000003","100001","100002","100003"] * 2,
        "Name": ["A","B","C","D","E","F"] * 2,
        "Market": ["KOSPI","KOSPI","KOSPI","KOSDAQ","KOSDAQ","KOSDAQ"] * 2,
        "Marcap": [30,20,10,30,20,10, 300,200,100,300,200,100],
    })
    fake["_date"] = pd.to_datetime(fake.Date)
    s = build_kr_monthly("2025-09-01", "2025-09-01", top_n=2, marcap=fake)
    assert len(s) == 4 and s.source_date.max() <= s.effective_date.min()
    validate_pit(s, expected_snapshot_size=2)
    u = union_manifest(s); assert len(u) == 4
    cur = pd.DataFrame({"symbol":["AAPL"],"sleeve":["US_SP500"],"point_in_time":[False]})
    try:
        historical_guard(cur)
        raise AssertionError("current membership should be blocked")
    except ValueError as e:
        assert "BLOCKED" in str(e)
    print("UNIFIED_PIT_MEMBERSHIP_V001_SELF_TEST=PASS")


def run(args) -> dict:
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    kr = build_kr_monthly(args.start, args.end, args.top_n)
    kr.to_csv(out / "kr_pit_snapshots.csv", index=False, encoding="utf-8-sig")
    ku = union_manifest(kr); ku.to_csv(out / "kr_union_manifest.csv", index=False, encoding="utf-8-sig")
    kt = turnover(kr); kt.to_csv(out / "kr_snapshot_turnover.csv", index=False, encoding="utf-8-sig")
    status = {
        "mode": MODE, "live_approval": False,
        "kr": {"status":"READY", "snapshot_rows":int(len(kr)), "snapshots":int(kr.effective_date.nunique()),
               "unique_symbols":int(ku.symbol.nunique()), "top_n_per_sleeve":int(args.top_n),
               "start":args.start, "end":args.end},
        "us": {"status":"BLOCKED_US_HISTORICAL_PIT_INPUT_REQUIRED", "accepted_sleeves":sorted(ALLOWED_US)},
    }
    if args.us_pit_csv:
        us = load_us_pit_csv(args.us_pit_csv); historical_guard(us)
        us.to_csv(out / "us_pit_snapshots.csv", index=False, encoding="utf-8-sig")
        uu = union_manifest(us); uu.to_csv(out / "us_union_manifest.csv", index=False, encoding="utf-8-sig")
        turnover(us).to_csv(out / "us_snapshot_turnover.csv", index=False, encoding="utf-8-sig")
        status["us"] = {"status":"READY", "snapshot_rows":int(len(us)), "snapshots":int(us.effective_date.nunique()),
                        "unique_symbols":int(uu.symbol.nunique()), "sleeves":sorted(set(us.sleeve.astype(str)))}
    (out / "membership_state.json").write_text(json.dumps(status, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("=== UNIFIED_PIT_MEMBERSHIP_STATE ===")
    print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
    return status


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--top-n", type=int, default=100)
    ap.add_argument("--us-pit-csv", default="")
    ap.add_argument("--outdir", default="unified_pit_membership_v001")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test: self_test(); return
    run(a)


if __name__ == "__main__":
    main()
