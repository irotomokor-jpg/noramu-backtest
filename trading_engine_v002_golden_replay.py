#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Golden replay checks for Trading Engine v0.02.

These fixtures come from the Aug 03-10 replay audits. They are execution/state
contracts, not OOS performance assertions and not strategy-tuning inputs.
"""
from __future__ import annotations

from pathlib import Path
import json
import math
import shutil
import tempfile

from trading_engine_v002_shadow import (
    ShadowTradingEngine, ProgramCandidate, Bar, EtfHysteresisState,
    RuntimeRiskLimits, ORDER_MODE, LIVE_APPROVAL,
)


def read_events(path: Path):
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def kr_fixture(root: Path):
    state = root / "kr_state.json"
    audit = root / "kr_audit.jsonl"
    eng = ShadowTradingEngine(5_000_000.0, state, audit)

    # Replay-derived queue: NAVER accepted first, remaining simultaneous candidates
    # blocked by the total-risk reservation once the first intent exists.
    candidates = [
        ProgramCandidate(
            "KR_V035", "KRLEVEL_RR|035420|5T|4321|4322|4324", "035420.KS",
            "2026-08-03T09:00:00+09:00", "2026-08-03T10:00:00+09:00",
            structural_stop=193606.05072219548, reserved_risk=50_000.0,
            planned_notional=772736.9863309297, internal_sort_key="029",
            trail_pct=.06, trail_arm_pct=.06, max_hold_bars=26,
            tick_size=500.0, slippage_ticks=1, commission_bps=1.5, sell_tax_bps=20.0,
        ),
        ProgramCandidate("KR_V035","KRLEVEL_RR|042660|4T","042660.KS","2026-08-03T09:00:00+09:00","2026-08-03T10:00:00+09:00",
                         100000, 51000, 600000, "031"),
        ProgramCandidate("KR_V035","KRLEVEL_RR|086790|4T","086790.KS","2026-08-03T09:00:00+09:00","2026-08-03T10:00:00+09:00",
                         100000, 51000, 600000, "038"),
        ProgramCandidate("KR_V035","KRLEVEL_RR|0126Z0|2T","0126Z0.KS","2026-08-03T09:00:00+09:00","2026-08-03T11:00:00+09:00",
                         100000, 51000, 600000, "018"),
    ]
    eng.submit_candidates(candidates, "2026-08-03T09:00:00+09:00")

    # Actual available 2m replay open at the model entry timestamp was 206,500.
    eng.on_bar(Bar("035420.KS","2026-08-03T10:00:00+09:00","2m",206500,206500,205000,205000,"2m"))
    assert "035420.KS" in eng.state.positions
    p = eng.state.positions["035420.KS"]
    assert p.quantity == 3 and p.entry_execution_price == 207000.0

    # Restart recovery must preserve the open position and idempotency state.
    eng = ShadowTradingEngine(5_000_000.0, state, audit)
    assert "035420.KS" in eng.state.positions

    # Peak 234,000 with 6% trail => 219,960 becomes effective on next bar.
    eng.on_bar(Bar("035420.KS","2026-08-06T15:00:00+09:00","2m",225000,234000,224000,230000,"2m"))
    assert math.isclose(eng.state.positions["035420.KS"].pending_stop_next_bar, 219960.0, rel_tol=0, abs_tol=1e-9)

    # Replay 2m open at 2026-08-07 09:00 was 219,500: gap below trailing stop.
    eng.on_bar(Bar("035420.KS","2026-08-07T09:00:00+09:00","2m",219500,222000,214000,214500,"2m"))
    assert "035420.KS" not in eng.state.positions

    ev = read_events(audit)
    rejects = [x for x in ev if x.get("event") == "REJECT" and x.get("reason") == "TOTAL_RISK_CAP"]
    assert len(rejects) == 3
    fills = [x for x in ev if x.get("event") == "FILL" and x.get("ticker") == "035420.KS"]
    assert len(fills) == 1 and fills[0]["raw_price"] == 206500 and fills[0]["execution_price"] == 207000
    closes = [x for x in ev if x.get("event") == "CLOSED" and x.get("ticker") == "035420.KS"]
    assert len(closes) == 1 and closes[0]["reason"] == "GAP_STOP"
    assert closes[0]["raw_price"] == 219500 and closes[0]["execution_price"] == 219000
    assert math.isclose(closes[0]["pnl"], 34494.3, rel_tol=0, abs_tol=1e-6)
    return {"accepted":"NAVER","risk_rejects":3,"pnl":closes[0]["pnl"]}


def dororong_fixture(root: Path):
    state = root / "doro_state.json"
    audit = root / "doro_audit.jsonl"
    eng = ShadowTradingEngine(5_000.0, state, audit)

    # Four accepted intents plus an LLY candidate that exceeds 2% reserved risk.
    cs = [
        ProgramCandidate("DORO_V016","DAGG|V|5034","V","2026-08-03T13:30:00-04:00","2026-08-03T14:30:00-04:00",350,20,900,"01",execution_cost_bps_side=10),
        ProgramCandidate("DORO_V016","DAGG|WMT|5044","WMT","2026-08-05T08:30:00-04:00","2026-08-05T09:30:00-04:00",105,20,900,"02",execution_cost_bps_side=10),
        ProgramCandidate("DORO_V016","DAGG|XOM|5060","XOM","2026-08-07T11:30:00-04:00","2026-08-07T12:30:00-04:00",145,20,900,"03",execution_cost_bps_side=10),
        ProgramCandidate("DORO_V016","DAGG|INTU|5066","INTU","2026-08-10T09:30:00-04:00","2026-08-10T10:30:00-04:00",310,20,900,"04",execution_cost_bps_side=10),
        ProgramCandidate("DORO_V016","DAGG|LLY|5050","LLY","2026-08-06T08:30:00-04:00","2026-08-06T09:30:00-04:00",600,30,900,"05",execution_cost_bps_side=10),
    ]
    eng.submit_candidates(cs, "2026-08-03T13:30:00-04:00")

    entry_bars = [
        Bar("V","2026-08-03T14:30:00-04:00","2m",366.1000061035156,366.16,366.07,366.07,"2m"),
        Bar("WMT","2026-08-05T09:30:00-04:00","2m",112.59500122070312,112.95,111.49,111.68,"2m"),
        Bar("XOM","2026-08-07T12:30:00-04:00","2m",153.4499969482422,153.53,153.40,153.44,"2m"),
        Bar("INTU","2026-08-10T10:30:00-04:00","2m",328.8800048828125,329.58,328.88,329.35,"2m"),
    ]
    for b in entry_bars:
        eng.on_bar(b)

    assert set(eng.state.positions) == {"V","WMT","XOM","INTU"}
    ev = read_events(audit)
    raw = {x["ticker"]:x["raw_price"] for x in ev if x.get("event") == "FILL"}
    assert raw == {"V":366.1000061035156,"WMT":112.59500122070312,"XOM":153.4499969482422,"INTU":328.8800048828125}
    assert len([x for x in ev if x.get("event") == "REJECT" and x.get("ticker") == "LLY" and x.get("reason") == "TOTAL_RISK_CAP"]) == 1

    # Runtime exits use the lower-timeframe executable bar, not old compressed-60m exit values.
    exits = [
        Bar("V","2026-08-07T11:30:00-04:00","2m",364.4100036621094,364.4324,364.13,364.18,"2m"),
        Bar("XOM","2026-08-10T12:30:00-04:00","2m",158.1699981689453,158.22,158.07,158.21,"2m"),
        Bar("WMT","2026-08-10T13:30:00-04:00","2m",111.73500061035156,111.74,111.675,111.695,"2m"),
        Bar("INTU","2026-08-10T15:30:00-04:00","2m",332.42999267578125,333.10,332.43,333.09,"2m"),
    ]
    for b in exits:
        eng.force_exit_at_open(b.ticker, b, "REPLAY_EXIT_INTENT")
    assert not eng.state.positions
    return {"fills":4,"risk_rejects":1,"entry_prices_match_2m":True}


def etf_fixture():
    rows = {
        "TQQQ":[
            ("2026-08-03",67.95999908447266,59.203109683990476),
            ("2026-08-04",74.81999969482422,59.32062900543213),
            ("2026-08-05",72.83999633789062,59.43110748291016),
            ("2026-08-06",72.02999877929688,59.53271266937256),
            ("2026-08-07",74.47000122070312,59.63682149887085),
            ("2026-08-10",73.80000305175781,59.73787868499756),
        ],
        "SOXL":[
            ("2026-08-03",116.70999908447266,98.93404994010925),
            ("2026-08-04",139.89999389648438,99.4336499118805),
            ("2026-08-05",132.07000732421875,99.89114995002747),
            ("2026-08-06",132.3300018310547,100.3513499546051),
            ("2026-08-07",140.25,100.84139994621277),
            ("2026-08-10",130.0,101.2838499546051),
        ],
    }
    states = {
        "TQQQ":EtfHysteresisState("TQQQ","QQQ","LEVER",.03),
        "SOXL":EtfHysteresisState("SOXL","SOXX","LEVER",.08),
    }
    out = {}
    for ticker, rr in rows.items():
        events = [states[ticker].on_completed_close(d,c,m) for d,c,m in rr]
        assert all(x["event"] == "HOLD" and x["next_session_state"] == "LEVER" for x in events)
        out[ticker] = {"days":len(events),"switches":0,"final":"LEVER"}
    return out


def ambiguity_and_idempotency_fixture(root: Path):
    state = root / "misc_state.json"; audit = root / "misc_audit.jsonl"
    eng = ShadowTradingEngine(10_000, state, audit, RuntimeRiskLimits(max_positions=4,max_total_risk_pct=.5))
    cand = ProgramCandidate("TEST","AMB1","XYZ","2026-01-01T09:00:00+00:00","2026-01-01T10:00:00+00:00",
                            structural_stop=90,reserved_risk=50,planned_notional=1000,target_price=110)
    eng.submit_candidates([cand],"2026-01-01T09:00:00+00:00")
    eng.submit_candidates([cand],"2026-01-01T09:00:01+00:00")
    eng.on_bar(Bar("XYZ","2026-01-01T10:00:00+00:00","1m",100,111,89,100,"1m"))
    ev=read_events(audit)
    assert len([x for x in ev if x.get("event") == "DUPLICATE_IGNORED"]) == 1
    assert len([x for x in ev if x.get("event") == "AMBIGUOUS_INTRABAR"]) == 1
    close=[x for x in ev if x.get("event") == "CLOSED"][-1]
    assert close["reason"] == "STOP_FIRST_AMBIGUOUS_INTRABAR"
    return {"duplicate_ignored":True,"ambiguous_stop_first":True}


def main():
    assert ORDER_MODE == "SHADOW_ONLY_NO_ORDERS" and LIVE_APPROVAL is False
    root = Path(tempfile.mkdtemp(prefix="engine_v002_"))
    try:
        result = {
            "kr":kr_fixture(root),
            "dororong":dororong_fixture(root),
            "etf":etf_fixture(),
            "safety":ambiguity_and_idempotency_fixture(root),
            "order_mode":ORDER_MODE,
            "live_approval":LIVE_APPROVAL,
        }
        print("GOLDEN_REPLAY=PASS")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
