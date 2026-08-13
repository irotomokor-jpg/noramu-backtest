#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Empirically probe whether Toss historical 1m candles contain extended sessions.

Research/read-only only. NO_ORDERS. The public docs expose US day/pre/regular/
after calendar sessions and KR KRX/NXT calendar sessions, but the candle docs do
not explicitly promise which sessions are present in historical 1m candles.
This probe inspects actual candle timestamps from the Toss API.
"""
from __future__ import annotations

import argparse
import json
from datetime import time as dtime
from pathlib import Path

import pandas as pd

from toss_replay_source_v001 import TossReplayClient

MODE = "TOSS_EXTENDED_SESSION_PROBE_READ_ONLY_NO_ORDERS"
LIVE_APPROVAL = False


def _minutes(t: dtime) -> int:
    return t.hour * 60 + t.minute


def classify_us(ts: pd.Timestamp) -> str:
    """ET buckets. 'DAY_OVERNIGHT' is intentionally broad because venue naming can vary."""
    m = ts.hour * 60 + ts.minute
    if m < 4 * 60:
        return "DAY_OVERNIGHT_00_04_ET"
    if m < 9 * 60 + 30:
        return "PRE_04_0930_ET"
    if m < 16 * 60:
        return "REGULAR_0930_1600_ET"
    if m < 20 * 60:
        return "AFTER_1600_2000_ET"
    return "DAY_OVERNIGHT_20_24_ET"


def classify_kr(ts: pd.Timestamp) -> str:
    m = ts.hour * 60 + ts.minute
    if m < 9 * 60:
        return "BEFORE_KRX_REGULAR"
    if m < 15 * 60 + 30:
        return "KRX_REGULAR_0900_1530"
    return "AFTER_KRX_REGULAR"


def get_day(client: TossReplayClient, symbol: str, market: str, day: str) -> pd.DataFrame:
    d = pd.Timestamp(day)
    if market == "US":
        tz = "America/New_York"
        start = d.tz_localize(tz)
        end = start + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    elif market == "KR":
        tz = "Asia/Seoul"
        start = d.tz_localize(tz)
        end = start + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    else:
        raise ValueError(market)
    rows = client.download_range(
        kind="stock", symbol=symbol, interval="1m",
        start=start.isoformat(), end=end.isoformat(), adjusted=False, max_pages=20,
    )
    if not rows:
        return pd.DataFrame(columns=["timestamp","open","high","low","close","volume","session"])
    z = pd.DataFrame(rows)
    z["timestamp"] = pd.to_datetime(z["timestamp"], utc=True, errors="coerce").dt.tz_convert(tz)
    z = z.dropna(subset=["timestamp"]).sort_values("timestamp")
    z = z[z.timestamp.dt.date == d.date()].copy()
    z["session"] = z.timestamp.map(classify_us if market == "US" else classify_kr)
    return z


def summarize(symbol: str, market: str, day: str, z: pd.DataFrame) -> dict:
    groups = []
    for session, g in z.groupby("session", sort=False):
        groups.append({
            "session": session,
            "bars": int(len(g)),
            "first": str(g.timestamp.min()),
            "last": str(g.timestamp.max()),
            "volume_sum": float(pd.to_numeric(g.get("volume", 0), errors="coerce").fillna(0).sum()),
        })
    outside = 0
    if market == "US":
        outside = int((z.session != "REGULAR_0930_1600_ET").sum())
    else:
        outside = int((z.session != "KRX_REGULAR_0900_1530").sum())
    return {
        "symbol": symbol, "market": market, "date": day,
        "bars_total": int(len(z)),
        "first": str(z.timestamp.min()) if len(z) else None,
        "last": str(z.timestamp.max()) if len(z) else None,
        "outside_regular_bars": outside,
        "extended_present": bool(outside > 0),
        "sessions": groups,
    }


def self_test() -> None:
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    assert classify_us(pd.Timestamp("2026-08-10 08:00", tz="America/New_York")) == "PRE_04_0930_ET"
    assert classify_us(pd.Timestamp("2026-08-10 17:00", tz="America/New_York")) == "AFTER_1600_2000_ET"
    assert classify_kr(pd.Timestamp("2026-08-10 16:00", tz="Asia/Seoul")) == "AFTER_KRX_REGULAR"
    print("TOSS_EXTENDED_SESSION_PROBE_V001_SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-08-10")
    ap.add_argument("--out", default="toss_extended_session_probe_v001/session_probe.json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test(); return
    assert MODE.endswith("NO_ORDERS") and LIVE_APPROVAL is False
    c = TossReplayClient()
    specs = [
        ("AAPL", "US"), ("TQQQ", "US"), ("SOXL", "US"),
        ("005930", "KR"), ("035420", "KR"),
    ]
    results = []
    for symbol, market in specs:
        print(f"PROBE {market} {symbol} {a.date}", flush=True)
        z = get_day(c, symbol, market, a.date)
        s = summarize(symbol, market, a.date, z)
        results.append(s)
        print(json.dumps(s, ensure_ascii=False, indent=2), flush=True)
    final = {
        "mode": MODE,
        "live_approval": False,
        "interpretation": "extended_present is empirical candle evidence, not an orderability claim",
        "results": results,
    }
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=== EXTENDED_SESSION_PROBE_SUMMARY ===")
    print(json.dumps(final, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
