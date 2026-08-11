#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from toss_replay_source_v001 import TossReplayClient, TossReplayError


@dataclass
class FakeResponse:
    status_code: int
    payload: Any
    headers: dict[str, str] | None = None
    def json(self): return self.payload
    def __post_init__(self): self.headers = self.headers or {}


class FakeSession:
    def __init__(self):
        self.posts=[]; self.gets=[]; self.stock_pages=0; self.ind_pages=0
    def post(self,url,headers=None,data=None,timeout=None):
        self.posts.append((url,headers,data,timeout))
        return FakeResponse(200,{"result":"TOKEN_STRING"})
    def get(self,url,headers=None,params=None,timeout=None):
        self.gets.append((url,headers,dict(params or {}),timeout))
        if "/market-indicators/" in url:
            self.ind_pages += 1
            if self.ind_pages == 1:
                return FakeResponse(200,{"result":{"candles":[
                    {"timestamp":"2026-01-03T09:00:00+09:00","openPrice":"1","highPrice":"2","lowPrice":"1","closePrice":"2","volume":"10"},
                    {"timestamp":"2026-01-02T09:00:00+09:00","openPrice":"1","highPrice":"2","lowPrice":"1","closePrice":"2","volume":"10"}],
                    "nextBefore":"2026-01-01T09:00:00+09:00"}})
            return FakeResponse(200,{"result":{"candles":[
                {"timestamp":"2025-12-31T09:00:00+09:00","openPrice":"1","highPrice":"2","lowPrice":"1","closePrice":"2","volume":"10"}],
                "nextBefore":None}})
        self.stock_pages += 1
        if self.stock_pages == 1:
            assert params.get("adjusted") == "false"
            return FakeResponse(200,{"result":{"candles":[
                {"timestamp":"2026-01-03T09:00:00+09:00","openPrice":"100","highPrice":"105","lowPrice":"99","closePrice":"104","volume":"100"},
                {"timestamp":"2026-01-02T09:00:00+09:00","openPrice":"90","highPrice":"101","lowPrice":"89","closePrice":"100","volume":"90"}],
                "nextBefore":"2026-01-01T09:00:00+09:00"}})
        return FakeResponse(200,{"result":{"candles":[
            {"timestamp":"2025-12-31T09:00:00+09:00","openPrice":"80","highPrice":"91","lowPrice":"79","closePrice":"90","volume":"80"}],
            "nextBefore":None}})


def main():
    fs=FakeSession(); c=TossReplayClient("ID","SECRET",session=fs)
    r=c.probe_depth(kind="stock",symbol="005930",interval="1m",target="2026-01-01T00:00:00+09:00",adjusted=False,max_pages=10)
    assert r.target_reached and r.pages==2 and r.earliest.startswith("2025-12-31")
    assert fs.posts and fs.posts[0][2]["client_id"]=="ID"
    assert all("X-Tossinvest-Account" not in (g[1] or {}) for g in fs.gets)

    # Fresh fake session so its independent indicator pagination starts at page 1.
    fs2=FakeSession(); c2=TossReplayClient("ID","SECRET",session=fs2)
    ir=c2.probe_depth(kind="indicator",symbol="KOSDAQ",interval="1m",target="2026-01-01T00:00:00+09:00",max_pages=10)
    assert ir.target_reached and ir.adjusted is None and ir.pages==2
    assert any("/api/v1/market-indicators/KOSDAQ/candles" in g[0] for g in fs2.gets)

    fs3=FakeSession(); c3=TossReplayClient("ID","SECRET",session=fs3)
    rows=c3.download_range(kind="stock",symbol="005930",interval="1m",start="2026-01-02T00:00:00+09:00",end="2026-01-03T23:59:00+09:00",adjusted=False)
    assert len(rows)==2 and rows[0]["timestamp"].startswith("2026-01-02") and rows[1]["timestamp"].startswith("2026-01-03")

    try:
        TossReplayClient._json(FakeResponse(403,{"error":"access_denied","message":"IP address not allowed"}))
        raise AssertionError("403 should raise")
    except TossReplayError as e:
        assert e.status==403 and e.code=="access_denied" and "IP address not allowed" in str(e)

    print("TOSS_REPLAY_SOURCE_VALIDATION=PASS")


if __name__=="__main__": main()
