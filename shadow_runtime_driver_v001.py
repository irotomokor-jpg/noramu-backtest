#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Provider-neutral event driver for the v0.02 shadow engine.

Input is JSONL. A future Toss/Yahoo/other data adapter only has to emit these
records; the trading/risk/position logic remains unchanged.

Supported records:
- CANDIDATES: one causal signal batch available at event_time
- BAR: one completed/executable lower-timeframe bar
- FORCE_EXIT: explicit strategy exit intent executed at the provided bar open
- DAY_ROLLOVER: reset daily risk accounting while preserving positions
- ETF_CLOSE: completed daily close updates next-session hysteresis state

NO LIVE ORDERS. This driver cannot send broker orders.
"""
from __future__ import annotations

from pathlib import Path
import argparse, json

from trading_engine_v002_shadow import (
    ShadowTradingEngine, ProgramCandidate, Bar, EtfHysteresisState,
    RuntimeRiskLimits, ORDER_MODE, LIVE_APPROVAL,
)


def load_jsonl(path: str | Path):
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            yield n, json.loads(line)
        except Exception as e:
            raise ValueError(f"invalid JSONL line {n}: {e}") from e


def program_candidate(d: dict) -> ProgramCandidate:
    return ProgramCandidate(**d)


def bar_from(d: dict) -> Bar:
    return Bar(**d)


def run_stream(input_path: str | Path, state_path: str | Path, audit_path: str | Path,
               starting_equity: float, limits: RuntimeRiskLimits) -> dict:
    assert LIVE_APPROVAL is False and ORDER_MODE == "SHADOW_ONLY_NO_ORDERS"
    engine = ShadowTradingEngine(starting_equity, state_path, audit_path, limits)
    etf_states: dict[str, EtfHysteresisState] = {}
    etf_events = []
    processed = 0

    for line_no, rec in load_jsonl(input_path):
        typ = str(rec.get("type", "")).upper()
        processed += 1
        if typ == "CANDIDATES":
            event_time = rec["event_time"]
            engine.submit_candidates([program_candidate(x) for x in rec.get("candidates", [])], event_time)
        elif typ == "BAR":
            engine.on_bar(bar_from(rec["bar"]))
        elif typ == "FORCE_EXIT":
            engine.force_exit_at_open(rec["ticker"], bar_from(rec["bar"]), rec.get("reason", "EXTERNAL_EXIT_INTENT"))
        elif typ == "DAY_ROLLOVER":
            engine.rollover_day()
        elif typ == "ETF_CLOSE":
            key = rec["key"]
            if key not in etf_states:
                cfg = rec["config"]
                etf_states[key] = EtfHysteresisState(
                    lever=cfg["lever"], base=cfg["base"], state=cfg["state"],
                    band=float(cfg["band"]), ma_days=int(cfg.get("ma_days", 200)),
                )
            e = etf_states[key].on_completed_close(rec["date"], rec["signal_close"], rec["ma"])
            etf_events.append({"key": key, **e})
        else:
            raise ValueError(f"unsupported event type at line {line_no}: {typ!r}")

    summary = {
        "processed_records": processed,
        "starting_equity": starting_equity,
        "ending_equity": engine.equity(),
        "cash": engine.state.cash,
        "open_positions": sorted(engine.state.positions),
        "pending_orders": sorted(engine.state.pending_orders),
        "reserved_risk": engine.reserved_risk(),
        "etf_states": {k: v.state for k, v in etf_states.items()},
        "etf_events": etf_events,
        "order_mode": ORDER_MODE,
        "live_approval": LIVE_APPROVAL,
    }
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--audit", required=True)
    ap.add_argument("--starting-equity", type=float, required=True)
    ap.add_argument("--summary", default="")
    a = ap.parse_args()
    result = run_stream(a.input, a.state, a.audit, a.starting_equity, RuntimeRiskLimits())
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if a.summary:
        Path(a.summary).parent.mkdir(parents=True, exist_ok=True)
        Path(a.summary).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
