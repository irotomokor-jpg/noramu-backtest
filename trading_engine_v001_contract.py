#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trading engine v0.01 execution-contract skeleton.

NO LIVE ORDERS. This module contains only portable event/state/risk/execution
contracts distilled from replay audits. Strategy-specific signal generation
stays in independent adapters.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from hashlib import sha256
from typing import Optional
import json


LIVE_APPROVAL = False
ORDER_MODE = "NO_ORDERS"


class PositionState(str, Enum):
    WATCH = "WATCH"
    READY = "READY"
    ORDER_PENDING = "ORDER_PENDING"
    OPEN = "OPEN"
    TRAIL_ARMED = "TRAIL_ARMED"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


class EventType(str, Enum):
    SIGNAL = "SIGNAL"
    REJECT = "REJECT"
    ORDER_INTENT = "ORDER_INTENT"
    FILL = "FILL"
    STOP_UPDATE = "STOP_UPDATE"
    TRAIL_ARMED = "TRAIL_ARMED"
    EXIT_INTENT = "EXIT_INTENT"
    CLOSED = "CLOSED"
    STATE_RESTORE = "STATE_RESTORE"


@dataclass(frozen=True)
class SignalCandidate:
    strategy_id: str
    setup_id: str
    ticker: str
    signal_time: str
    next_executable_time: str
    raw_reference_price: float
    structural_stop: float
    reserved_risk: float
    internal_sort_key: str = ""


@dataclass
class Position:
    strategy_id: str
    setup_id: str
    ticker: str
    state: PositionState = PositionState.WATCH
    quantity: int = 0
    average_price: float = 0.0
    active_stop: Optional[float] = None
    peak_price: Optional[float] = None
    trail_pct: Optional[float] = None
    reserved_risk: float = 0.0


@dataclass(frozen=True)
class EngineEvent:
    event_id: str
    strategy_id: str
    setup_id: str
    ticker: str
    event_time: str
    event_type: EventType
    reason: str
    queue_rank: Optional[int] = None
    bar_interval: str = ""
    bar_time: str = ""
    raw_price: Optional[float] = None
    execution_price: Optional[float] = None
    quantity: Optional[int] = None
    fee: float = 0.0
    tax: float = 0.0
    position_state_before: Optional[PositionState] = None
    position_state_after: Optional[PositionState] = None
    account_equity: Optional[float] = None
    reserved_risk: Optional[float] = None
    data_fidelity: str = ""
    source_hash: str = ""
    idempotency_key: str = ""

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, ensure_ascii=False, sort_keys=True, default=str)


def stable_idempotency_key(*parts) -> str:
    raw = "|".join(str(x) for x in parts)
    return sha256(raw.encode("utf-8")).hexdigest()


def deterministic_candidate_order(candidates: list[SignalCandidate]) -> list[SignalCandidate]:
    """Frozen v0.01 queue contract: stable ascending internal key/ticker/setup."""
    return sorted(candidates, key=lambda c: (c.internal_sort_key or c.ticker, c.ticker, c.setup_id))


@dataclass
class RiskSnapshot:
    equity: float
    open_positions: int
    reserved_risk: float
    realized_today: float
    day_start_equity: float
    drawdown: float
    open_tickers: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class RiskLimits:
    max_positions: int = 4
    max_total_risk_pct: float = 0.02
    daily_loss_stop_pct: float = 0.015
    dd_halt_pct: float = 0.08


def risk_decision(candidate: SignalCandidate, snap: RiskSnapshot, limits: RiskLimits) -> tuple[bool, str]:
    """Portable pre-order hard gates. Strategy-specific sizing can add more gates."""
    if candidate.ticker in snap.open_tickers:
        return False, "SAME_TICKER_OPEN"
    if snap.drawdown >= limits.dd_halt_pct:
        return False, "MTM_DD_HALT"
    if snap.realized_today <= -limits.daily_loss_stop_pct * snap.day_start_equity:
        return False, "DAILY_REALIZED_STOP"
    if snap.open_positions >= limits.max_positions:
        return False, "MAX_POSITIONS"
    if snap.reserved_risk + candidate.reserved_risk > snap.equity * limits.max_total_risk_pct + 1e-9:
        return False, "TOTAL_RISK_CAP"
    return True, "ACCEPT"


def resolve_gap_stop(open_price: float, active_stop: float, side: str = "LONG") -> bool:
    if side != "LONG":
        raise NotImplementedError("v0.01 contract currently validates long replay fixtures")
    return float(open_price) <= float(active_stop)


def conservative_intrabar_resolution(low: float, high: float, stop: Optional[float], target: Optional[float]) -> str:
    """Resolve only what the available bar proves; ambiguity favors adverse outcome."""
    stop_hit = stop is not None and float(low) <= float(stop)
    target_hit = target is not None and float(high) >= float(target)
    if stop_hit and target_hit:
        return "STOP_FIRST_AMBIGUOUS_INTRABAR"
    if stop_hit:
        return "STOP"
    if target_hit:
        return "TARGET"
    return "NONE"


def etf_next_session_asset(previous_state: str, signal_close: float, ma: float, band: float) -> str:
    """Persistent hysteresis state; result applies to the following session."""
    state = previous_state
    upper = float(ma) * (1.0 + float(band))
    lower = float(ma) * (1.0 - float(band))
    if state == "BASE" and float(signal_close) > upper:
        return "LEVER"
    if state == "LEVER" and float(signal_close) < lower:
        return "BASE"
    return state


def self_test():
    assert LIVE_APPROVAL is False and ORDER_MODE == "NO_ORDERS"
    c1 = SignalCandidate("KR","A","AAA","t","t+1",100,90,60,"002")
    c2 = SignalCandidate("KR","B","BBB","t","t+1",100,90,50,"001")
    assert [c.setup_id for c in deterministic_candidate_order([c1,c2])] == ["B","A"]
    s = RiskSnapshot(5000,0,50,0,5000,0,set())
    ok, reason = risk_decision(c1,s,RiskLimits(max_total_risk_pct=.02))
    assert not ok and reason == "TOTAL_RISK_CAP"
    assert conservative_intrabar_resolution(89,111,90,110) == "STOP_FIRST_AMBIGUOUS_INTRABAR"
    assert etf_next_session_asset("BASE",104,100,.03) == "LEVER"
    assert etf_next_session_asset("LEVER",96,100,.03) == "BASE"
    print("SELF_TEST=PASS")


if __name__ == "__main__":
    self_test()
