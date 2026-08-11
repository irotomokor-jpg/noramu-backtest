#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fixed-IP Toss market-data daemon for shadow trading research.

MARKET DATA ONLY. NO ACCOUNT OR ORDER ENDPOINTS.
- Polls 1-minute candles from Toss REST API.
- Deduplicates bars across restarts.
- Persists normalized BAR events to JSONL.
- Writes a health/status JSON file.
- Keeps all credentials in environment variables only.

This daemon intentionally does NOT place, modify, cancel, or inspect orders.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, time as dtime
import json
import os
from pathlib import Path
import signal
import sys
import time
from typing import Iterable
from zoneinfo import ZoneInfo

from toss_market_data_adapter_v002 import (
    TossMarketDataClient,
    TossAPIError,
    normalize_candles,
    READ_ONLY_MODE,
    LIVE_APPROVAL,
)

DAEMON_MODE = "FIXED_IP_SHADOW_FEED_NO_ORDERS"
KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")

DEFAULT_US = [
    "NVDA","AAPL","MSFT","AMZN","GOOGL","AVGO","META","TSLA","MU","NFLX","COST","PLTR","AMD",
    "CSCO","TMUS","INTU","AMAT","QCOM","ISRG","JPM","LLY","WMT","V","MA","XOM","JNJ","ORCL",
    "TQQQ","QQQ","SOXL","SOXX",
]
DEFAULT_KR = ["005930","035420"]


def _csv_env(name: str, default: Iterable[str]) -> list[str]:
    raw = os.getenv(name, "").strip()
    vals = [x.strip().upper() for x in raw.split(",") if x.strip()] if raw else list(default)
    out, seen = [], set()
    for x in vals:
        if x not in seen:
            seen.add(x); out.append(x)
    return out


def _atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path, fallback: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback.copy()


def _market_open(market: str, now_utc: datetime) -> bool:
    if market == "KR":
        z = now_utc.astimezone(KST)
        if z.weekday() >= 5:
            return False
        # Regular KRX window + small warmup/close buffer. Market calendar validation is separate.
        return dtime(8, 50) <= z.time() <= dtime(15, 40)
    z = now_utc.astimezone(ET)
    if z.weekday() >= 5:
        return False
    return dtime(9, 20) <= z.time() <= dtime(16, 10)


def _append_events(path: Path, events: list[dict]) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n")


class ShadowFeedDaemon:
    def __init__(self, root: Path, poll_seconds: int = 60, force_markets: str = ""):
        if READ_ONLY_MODE != "MARKET_DATA_ONLY_NO_ORDERS" or LIVE_APPROVAL is not False:
            raise RuntimeError("read-only safety contract violated")
        self.root = root
        self.poll_seconds = max(10, int(poll_seconds))
        self.kr_symbols = _csv_env("KR_WATCHLIST", DEFAULT_KR)
        self.us_symbols = _csv_env("US_WATCHLIST", DEFAULT_US)
        self.force_markets = {x.strip().upper() for x in force_markets.split(",") if x.strip()}
        self.client = TossMarketDataClient()
        self.state_path = root / "state.json"
        self.status_path = root / "status.json"
        self.state = _load_json(self.state_path, {"last_ts": {}, "cycles": 0})
        self.stop = False
        self.last_errors: dict[str, str] = {}

    def request_stop(self, *_):
        self.stop = True

    def _poll_symbol(self, market: str, symbol: str) -> list[dict]:
        rows = self.client.candles(symbol, "1m", max_bars=3)
        bars = normalize_candles(symbol, rows, "1m")
        key = f"{market}:{symbol}"
        last = str(self.state.get("last_ts", {}).get(key, ""))
        fresh = [b for b in bars if b.timestamp > last]
        if fresh:
            self.state.setdefault("last_ts", {})[key] = fresh[-1].timestamp
        out = []
        for b in fresh:
            ev = b.runtime_event()
            ev["market"] = market
            ev["volume"] = b.volume
            ev["currency"] = b.currency
            ev["ingested_at"] = datetime.now().astimezone().isoformat()
            ev["mode"] = DAEMON_MODE
            out.append(ev)
        return out

    def cycle(self) -> dict:
        now_utc = datetime.now(ZoneInfo("UTC"))
        markets: list[tuple[str, list[str]]] = []
        for market, symbols in (("KR", self.kr_symbols), ("US", self.us_symbols)):
            if market in self.force_markets or _market_open(market, now_utc):
                markets.append((market, symbols))

        events: list[dict] = []
        errors: dict[str, str] = {}
        for market, symbols in markets:
            for symbol in symbols:
                try:
                    events.extend(self._poll_symbol(market, symbol))
                except TossAPIError as exc:
                    errors[f"{market}:{symbol}"] = f"TossAPIError status={exc.status} code={exc.code} request_id={exc.request_id}"
                except Exception as exc:
                    errors[f"{market}:{symbol}"] = f"{type(exc).__name__}: {exc}"

        local_date = datetime.now(KST).date().isoformat()
        _append_events(self.root / "feed" / f"{local_date}.jsonl", events)
        self.state["cycles"] = int(self.state.get("cycles", 0)) + 1
        self.state["updated_at"] = datetime.now().astimezone().isoformat()
        _atomic_json(self.state_path, self.state)

        self.last_errors = errors
        status = {
            "mode": DAEMON_MODE,
            "read_only": True,
            "live_approval": False,
            "updated_at": self.state["updated_at"],
            "cycles": self.state["cycles"],
            "active_markets": [m for m, _ in markets],
            "kr_watchlist_count": len(self.kr_symbols),
            "us_watchlist_count": len(self.us_symbols),
            "new_events": len(events),
            "error_count": len(errors),
            "errors": errors,
        }
        _atomic_json(self.status_path, status)
        return status

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        while not self.stop:
            started = time.monotonic()
            status = self.cycle()
            print(json.dumps({k: status[k] for k in ("updated_at","active_markets","new_events","error_count")}, ensure_ascii=False), flush=True)
            elapsed = time.monotonic() - started
            sleep_for = max(1.0, self.poll_seconds - elapsed)
            deadline = time.monotonic() + sleep_for
            while not self.stop and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))


def self_test() -> None:
    assert DAEMON_MODE == "FIXED_IP_SHADOW_FEED_NO_ORDERS"
    assert LIVE_APPROVAL is False
    assert _market_open("KR", datetime(2026, 8, 11, 1, 0, tzinfo=ZoneInfo("UTC"))) is True
    assert _market_open("US", datetime(2026, 8, 11, 14, 0, tzinfo=ZoneInfo("UTC"))) is True
    assert _market_open("US", datetime(2026, 8, 9, 14, 0, tzinfo=ZoneInfo("UTC"))) is False
    print("TOSS_SHADOW_DAEMON_SELF_TEST=PASS")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.getenv("SHADOW_DATA_DIR", "/var/lib/noramu-shadow"))
    ap.add_argument("--poll-seconds", type=int, default=int(os.getenv("POLL_SECONDS", "60")))
    ap.add_argument("--force-markets", default="")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test(); return 0
    d = ShadowFeedDaemon(Path(args.root), args.poll_seconds, args.force_markets)
    if args.once:
        print(json.dumps(d.cycle(), ensure_ascii=False, indent=2)); return 0
    d.run(); return 0


if __name__ == "__main__":
    sys.exit(main())
