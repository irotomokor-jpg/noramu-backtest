#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from toss_market_data_adapter_v001 import TossMarketDataClient, normalize_candles, aggregate_bars, READ_ONLY_MODE


@dataclass
class FakeResponse:
    status_code: int
    body: dict[str, Any]
    headers: dict[str, str] | None = None
    def json(self):
        return self.body


class FakeSession:
    def __init__(self):
        self.posts = []
        self.gets = []
        self.candle_calls = 0

    def post(self, url, headers=None, data=None, timeout=None):
        self.posts.append((url, headers, data, timeout))
        return FakeResponse(200, {"access_token":"TOKEN_X","expires_in":3600}, {})

    def get(self, url, headers=None, params=None, timeout=None):
        self.gets.append((url, headers, params, timeout))
        assert headers and headers.get("Authorization") == "Bearer TOKEN_X"
        if url.endswith("/api/v1/prices"):
            return FakeResponse(200, {"result":[
                {"symbol":"005930","timestamp":"2026-08-11T10:00:00+09:00","lastPrice":"71000","currency":"KRW"},
                {"symbol":"AAPL","timestamp":"2026-08-10T20:00:00-04:00","lastPrice":"220.10","currency":"USD"},
            ]}, {})
        if url.endswith("/api/v1/candles"):
            self.candle_calls += 1
            if self.candle_calls == 1:
                return FakeResponse(200, {"result":{"candles":[
                    {"timestamp":"2026-08-11T09:02:00+09:00","openPrice":"102","highPrice":"104","lowPrice":"101","closePrice":"103","volume":"30","currency":"KRW"},
                    {"timestamp":"2026-08-11T09:01:00+09:00","openPrice":"101","highPrice":"103","lowPrice":"100","closePrice":"102","volume":"20","currency":"KRW"},
                ],"nextBefore":"2026-08-11T09:00:00+09:00"}}, {})
            return FakeResponse(200, {"result":{"candles":[
                {"timestamp":"2026-08-11T09:00:00+09:00","openPrice":"100","highPrice":"102","lowPrice":"99","closePrice":"101","volume":"10","currency":"KRW"}
            ],"nextBefore":None}}, {})
        if "/api/v1/market-calendar/" in url:
            return FakeResponse(200, {"result":{"market":"KR","open":True}}, {})
        raise AssertionError(url)


def main():
    fake = FakeSession()
    c = TossMarketDataClient("id","secret",session=fake)
    assert c.access_token() == "TOKEN_X"
    assert c.access_token() == "TOKEN_X" and len(fake.posts) == 1

    px = c.prices(["005930","AAPL"])
    assert [r["symbol"] for r in px] == ["005930","AAPL"]

    rows = c.candles("005930","1m",max_bars=3)
    assert [r["timestamp"] for r in rows] == [
        "2026-08-11T09:00:00+09:00","2026-08-11T09:01:00+09:00","2026-08-11T09:02:00+09:00"
    ]
    bars = normalize_candles("005930", rows, "1m")
    five = aggregate_bars(bars,"5min","5m")
    assert len(five) == 1
    assert five[0].open == 100 and five[0].high == 104 and five[0].low == 99 and five[0].close == 103 and five[0].volume == 60
    cal = c.market_calendar("KR")
    assert cal["market"] == "KR"

    # The adapter must never attach account-level headers.
    for _, headers, _, _ in fake.gets:
        assert "X-Tossinvest-Account" not in (headers or {})
    assert READ_ONLY_MODE == "MARKET_DATA_ONLY_NO_ORDERS"
    print("TOSS_MARKET_DATA_VALIDATION=PASS")


if __name__ == "__main__":
    main()
