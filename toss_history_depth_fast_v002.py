#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rate-safe fast Toss 1m retention probe.

Uses direct `before=` queries and a date binary search instead of paging every
200 bars from the present. Read-only market data only; NO_ORDERS.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import random
import time

from toss_replay_source_v001 import TossReplayClient, TossReplayError, MODE, LIVE_APPROVAL, default_probe_suite

TARGET = datetime.fromisoformat("2026-01-01T00:00:00+09:00")
LOWER = datetime.fromisoformat("2025-01-01T00:00:00+09:00")
UPPER = datetime.now(timezone.utc).astimezone(TARGET.tzinfo)


def safe_page(c: TossReplayClient, spec: dict, before: datetime, attempts: int = 6):
    before_s = before.isoformat()
    for n in range(attempts):
        try:
            if spec["kind"] == "stock":
                rows, nxt = c.stock_candles_page(
                    spec["symbol"], "1m", count=2, before=before_s,
                    adjusted=bool(spec.get("adjusted", False)),
                )
            else:
                rows, nxt = c.indicator_candles_page(spec["symbol"], "1m", count=2, before=before_s)
            return rows, nxt
        except TossReplayError as e:
            if e.status != 429 or n == attempts - 1:
                raise
            # Conservative exponential backoff + jitter. Official docs advise
            # Retry-After/backoff for 429; this wrapper stays safely below TPS.
            delay = min(12.0, 1.0 * (2 ** n)) + random.uniform(0.05, 0.35)
            print(f"RATE_LIMIT {spec['symbol']} retry={n+1} sleep={delay:.2f}s")
            time.sleep(delay)
    return [], None


def newest_at_or_before(c: TossReplayClient, spec: dict, when: datetime):
    rows, _ = safe_page(c, spec, when)
    if not rows:
        return None
    stamps = [r.get("timestamp") for r in rows if r.get("timestamp")]
    if not stamps:
        return None
    return max(stamps, key=datetime.fromisoformat)


def probe_one(c: TossReplayClient, spec: dict):
    # Direct target check: if this is non-empty, Toss has 1m history at/before target.
    at_target = newest_at_or_before(c, spec, TARGET)
    target_reached = at_target is not None

    # Binary-search the approximate earliest date that returns any 1m candle.
    lo = LOWER
    hi = UPPER
    # If even upper is empty, report unavailable.
    latest = newest_at_or_before(c, spec, hi)
    if latest is None:
        return {
            "label": spec["label"], "kind": spec["kind"], "symbol": spec["symbol"],
            "target": TARGET.isoformat(), "target_reached": False,
            "target_sample": None, "approx_earliest_available": None,
            "latest_sample": None, "status": "NO_1M_DATA",
        }

    # If lower already returns data, the true earliest may be before LOWER.
    lower_sample = newest_at_or_before(c, spec, lo)
    if lower_sample is not None:
        earliest = f"<= {lo.date().isoformat()}"
    else:
        # Monotone by retention/listing floor: before the floor => empty; after => data.
        while (hi - lo) > timedelta(days=1):
            mid = lo + (hi - lo) / 2
            sample = newest_at_or_before(c, spec, mid)
            if sample is None:
                lo = mid
            else:
                hi = mid
        first = newest_at_or_before(c, spec, hi)
        earliest = first or hi.isoformat()

    return {
        "label": spec["label"], "kind": spec["kind"], "symbol": spec["symbol"],
        "target": TARGET.isoformat(), "target_reached": target_reached,
        "target_sample": at_target, "approx_earliest_available": earliest,
        "latest_sample": latest, "status": "PASS" if target_reached else "TARGET_NOT_REACHED",
    }


def main():
    assert MODE == "TOSS_REPLAY_READ_ONLY_NO_ORDERS" and LIVE_APPROVAL is False
    c = TossReplayClient()
    # 0.55 sec ~= 1.8 TPS, comfortably under the published chart ceiling and
    # tolerant of dynamically reduced limits.
    c.gate._gap["MARKET_DATA_CHART"] = 0.55
    c.gate._gap["MARKET_INDICATOR_CHART"] = 0.55

    out = []
    for spec in default_probe_suite():
        print(f"PROBE {spec['label']} {spec['symbol']}")
        row = probe_one(c, spec)
        out.append(row)
        print(json.dumps(row, ensure_ascii=False))
        time.sleep(0.6)

    result = {"mode": MODE, "live_approval": False, "method": "DIRECT_BEFORE_BINARY_SEARCH_V002", "probes": out}
    with open("toss_history_depth_fast_v002.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("\n=== FINAL ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
