#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Toss Securities market-data adapter for the shadow runtime.

READ-ONLY MARKET DATA ONLY.
- OAuth2 client-credentials token
- prices / trades / 1m and 1d candles / market calendar
- 1m -> 5m / 60m local aggregation
- normalized BAR events for shadow_runtime_driver_v001.py

This file deliberately contains no account, asset, or trading-write integration.
Credentials are read only from environment variables:
  TOSS_CLIENT_ID
  TOSS_CLIENT_SECRET
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import argparse
import json
import os
import threading
import time
from typing import Any, Iterable, Optional

import pandas as pd
import requests

BASE_URL = "https://openapi.tossinvest.com"
TOKEN_PATH = "/oauth2/token"
READ_ONLY_MODE = "MARKET_DATA_ONLY_NO_ORDERS"
LIVE_APPROVAL = False


class TossAPIError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, request_id: str = ""):
        super().__init__(f"Toss API {status} {code}: {message} request_id={request_id}")
        self.status = status
        self.code = code
        self.request_id = request_id


class RateGate:
    """Simple process-local minimum-interval gate, below published TPS ceilings."""
    def __init__(self):
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        # Conservative spacing vs official maxima: auth 5/s, data 10/s, chart 5/s, market-info 3/s.
        self._interval = {"AUTH": 0.22, "MARKET_DATA": 0.11, "MARKET_DATA_CHART": 0.22, "MARKET_INFO": 0.36}

    def wait(self, group: str) -> None:
        gap = self._interval.get(group, 0.25)
        with self._lock:
            now = time.monotonic()
            prev = self._last.get(group, 0.0)
            delay = prev + gap - now
            if delay > 0:
                time.sleep(delay)
            self._last[group] = time.monotonic()


@dataclass(frozen=True)
class NormalizedBar:
    symbol: str
    timestamp: str
    interval: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    currency: str = ""
    source: str = "TOSS"

    def runtime_event(self) -> dict[str, Any]:
        return {
            "type": "BAR",
            "ticker": self.symbol,
            "time": self.timestamp,
            "interval": self.interval,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "source": self.source,
        }


class TossMarketDataClient:
    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        *,
        session: Optional[requests.Session] = None,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        rate_gate: Optional[RateGate] = None,
    ):
        self.client_id = client_id or os.getenv("TOSS_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("TOSS_CLIENT_SECRET", "")
        if not self.client_id or not self.client_secret:
            raise ValueError("TOSS_CLIENT_ID and TOSS_CLIENT_SECRET are required")
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.rate_gate = rate_gate or RateGate()
        self._access_token = ""
        self._token_expiry = datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _payload(response: requests.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except Exception as exc:
            raise TossAPIError(response.status_code, "invalid-json", str(exc), response.headers.get("X-Request-Id", ""))
        if response.status_code >= 400:
            err = body.get("error", {}) if isinstance(body, dict) else {}
            raise TossAPIError(
                response.status_code,
                str(err.get("code", "http-error")),
                str(err.get("message", body)),
                str(err.get("requestId", response.headers.get("X-Request-Id", ""))),
            )
        if not isinstance(body, dict):
            raise TossAPIError(response.status_code, "invalid-envelope", "response is not an object")
        return body

    def _token_from_body(self, body: dict[str, Any]) -> tuple[str, int]:
        src = body.get("result", body)
        token = src.get("access_token") or src.get("accessToken")
        if not token:
            raise TossAPIError(200, "token-missing", "OAuth response has no access token")
        expires = src.get("expires_in") or src.get("expiresIn") or 300
        try:
            expires_i = max(30, int(expires))
        except Exception:
            expires_i = 300
        return str(token), expires_i

    def access_token(self, *, force: bool = False) -> str:
        now = datetime.now(timezone.utc)
        if not force and self._access_token and now + timedelta(seconds=30) < self._token_expiry:
            return self._access_token
        self.rate_gate.wait("AUTH")
        r = self.session.post(
            self.base_url + TOKEN_PATH,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=self.timeout,
        )
        body = self._payload(r)
        token, expires = self._token_from_body(body)
        self._access_token = token
        self._token_expiry = now + timedelta(seconds=expires)
        return token

    def _get(self, path: str, params: dict[str, Any], group: str) -> dict[str, Any]:
        self.rate_gate.wait(group)
        headers = {"Authorization": f"Bearer {self.access_token()}"}
        r = self.session.get(self.base_url + path, headers=headers, params=params, timeout=self.timeout)
        if r.status_code == 401:
            # One controlled refresh for expired/revoked cached token.
            headers["Authorization"] = f"Bearer {self.access_token(force=True)}"
            self.rate_gate.wait(group)
            r = self.session.get(self.base_url + path, headers=headers, params=params, timeout=self.timeout)
        return self._payload(r)

    def prices(self, symbols: Iterable[str]) -> list[dict[str, Any]]:
        syms = [str(x).strip() for x in symbols if str(x).strip()]
        if not syms:
            return []
        if len(syms) > 200:
            raise ValueError("prices supports at most 200 symbols per call")
        body = self._get("/api/v1/prices", {"symbols": ",".join(syms)}, "MARKET_DATA")
        rows = body.get("result", [])
        return rows if isinstance(rows, list) else []

    def trades(self, symbol: str, count: int = 50) -> list[dict[str, Any]]:
        count = max(1, min(50, int(count)))
        body = self._get("/api/v1/trades", {"symbol": symbol, "count": count}, "MARKET_DATA")
        rows = body.get("result", [])
        return rows if isinstance(rows, list) else []

    def candles_page(
        self,
        symbol: str,
        interval: str = "1m",
        count: int = 200,
        before: Optional[str] = None,
        adjusted: bool = True,
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
        if interval not in {"1m", "1d"}:
            raise ValueError("Toss candle interval must be 1m or 1d")
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "count": max(1, min(200, int(count))),
            "adjusted": str(bool(adjusted)).lower(),
        }
        if before:
            params["before"] = before
        body = self._get("/api/v1/candles", params, "MARKET_DATA_CHART")
        result = body.get("result", {})
        if not isinstance(result, dict):
            return [], None
        rows = result.get("candles", [])
        return (rows if isinstance(rows, list) else []), result.get("nextBefore")

    def candles(
        self,
        symbol: str,
        interval: str = "1m",
        *,
        max_bars: int = 1000,
        before: Optional[str] = None,
        adjusted: bool = True,
    ) -> list[dict[str, Any]]:
        """Backward-page candles, returned oldest->newest and deduplicated by timestamp."""
        want = max(1, int(max_bars))
        cursor = before
        by_ts: dict[str, dict[str, Any]] = {}
        seen_cursor: set[str] = set()
        while len(by_ts) < want:
            rows, nxt = self.candles_page(symbol, interval, min(200, want - len(by_ts)), cursor, adjusted)
            for row in rows:
                ts = str(row.get("timestamp", ""))
                if ts:
                    by_ts[ts] = row
            if not rows or not nxt or nxt in seen_cursor:
                break
            seen_cursor.add(str(nxt))
            cursor = str(nxt)
        return [by_ts[k] for k in sorted(by_ts)][-want:]

    def market_calendar(self, market: str) -> Any:
        m = market.upper()
        if m not in {"KR", "US"}:
            raise ValueError("market must be KR or US")
        return self._get(f"/api/v1/market-calendar/{m}", {}, "MARKET_INFO").get("result")


def normalize_candles(symbol: str, rows: Iterable[dict[str, Any]], interval: str) -> list[NormalizedBar]:
    out: list[NormalizedBar] = []
    for r in rows:
        out.append(NormalizedBar(
            symbol=symbol,
            timestamp=str(r["timestamp"]),
            interval=interval,
            open=float(r["openPrice"]),
            high=float(r["highPrice"]),
            low=float(r["lowPrice"]),
            close=float(r["closePrice"]),
            volume=float(r.get("volume", 0) or 0),
            currency=str(r.get("currency", "")),
        ))
    return sorted(out, key=lambda b: pd.Timestamp(b.timestamp))


def aggregate_bars(bars: Iterable[NormalizedBar], rule: str, label: str) -> list[NormalizedBar]:
    bars = list(bars)
    if not bars:
        return []
    symbol = bars[0].symbol
    currency = bars[0].currency
    df = pd.DataFrame([{
        "timestamp": pd.Timestamp(b.timestamp),
        "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume,
    } for b in bars]).set_index("timestamp").sort_index()
    # No cross-day bins: resample each local calendar date independently.
    chunks = []
    for _, day in df.groupby(df.index.date):
        z = day.resample(rule, origin="start_day", label="left", closed="left").agg(
            {"open":"first", "high":"max", "low":"min", "close":"last", "volume":"sum"}
        ).dropna(subset=["open", "close"])
        chunks.append(z)
    agg = pd.concat(chunks).sort_index() if chunks else pd.DataFrame()
    return [NormalizedBar(symbol, ts.isoformat(), label, float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume), currency)
            for ts, r in agg.iterrows()]


def one_minute_to_runtime_events(symbol: str, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b.runtime_event() for b in normalize_candles(symbol, rows, "1m")]


def self_test() -> None:
    assert READ_ONLY_MODE == "MARKET_DATA_ONLY_NO_ORDERS" and LIVE_APPROVAL is False
    rows = [
        {"timestamp":"2026-08-11T09:00:00+09:00","openPrice":"100","highPrice":"102","lowPrice":"99","closePrice":"101","volume":"10","currency":"KRW"},
        {"timestamp":"2026-08-11T09:01:00+09:00","openPrice":"101","highPrice":"103","lowPrice":"100","closePrice":"102","volume":"20","currency":"KRW"},
        {"timestamp":"2026-08-11T09:05:00+09:00","openPrice":"102","highPrice":"104","lowPrice":"101","closePrice":"103","volume":"30","currency":"KRW"},
    ]
    b = normalize_candles("005930", rows, "1m")
    a = aggregate_bars(b, "5min", "5m")
    assert len(a) == 2
    assert a[0].open == 100 and a[0].high == 103 and a[0].low == 99 and a[0].close == 102 and a[0].volume == 30
    ev = one_minute_to_runtime_events("005930", rows)
    assert ev[0]["type"] == "BAR" and ev[0]["interval"] == "1m"
    print("SELF_TEST=PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--smoke-symbols", default="005930,AAPL,TQQQ,SOXL")
    ap.add_argument("--bars", type=int, default=20)
    args = ap.parse_args()
    if args.self_test:
        self_test(); return
    c = TossMarketDataClient()
    syms = [x.strip() for x in args.smoke_symbols.split(",") if x.strip()]
    prices = c.prices(syms)
    result: dict[str, Any] = {"mode": READ_ONLY_MODE, "price_symbols": [x.get("symbol") for x in prices], "candles": {}}
    for s in syms:
        rows = c.candles(s, "1m", max_bars=args.bars)
        result["candles"][s] = {"bars": len(rows), "last": rows[-1].get("timestamp") if rows else None}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
