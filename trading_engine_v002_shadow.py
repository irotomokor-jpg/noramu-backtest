#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trading Engine v0.02 - persistent shadow runtime.

SAFETY
------
NO LIVE ORDERS. No broker write adapter exists in this module.
All fills are virtual and every state transition is auditable.

The runtime implements the replay-derived contract:
- deterministic simultaneous-candidate queue
- pre-order risk reservation / rejection
- next executable bar entry (never same signal bar)
- adverse virtual execution costs
- gap-stop at executable open
- chronological lower-timeframe first-touch exit semantics
- conservative stop-first resolution if one bar proves both stop and target
- trailing-stop updates become effective on the NEXT bar
- persistent state + idempotency keys for restart safety
- ETF hysteresis state based only on completed closes
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Optional, Iterable
import argparse
import json
import math

import trading_engine_v001_contract as c1

LIVE_APPROVAL = False
ORDER_MODE = "SHADOW_ONLY_NO_ORDERS"
VERSION = "v0.02-SHADOW-RUNTIME"


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def stable_key(*parts) -> str:
    return sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Bar:
    ticker: str
    time: str
    interval: str
    open: float
    high: float
    low: float
    close: float
    fidelity: str = ""


@dataclass(frozen=True)
class ProgramCandidate:
    strategy_id: str
    setup_id: str
    ticker: str
    signal_time: str
    next_executable_time: str
    structural_stop: float
    reserved_risk: float
    planned_notional: float
    internal_sort_key: str = ""
    trail_pct: Optional[float] = None
    trail_arm_pct: Optional[float] = None
    target_price: Optional[float] = None
    max_hold_bars: Optional[int] = None
    tick_size: float = 0.0
    slippage_ticks: int = 0
    commission_bps: float = 0.0
    sell_tax_bps: float = 0.0
    execution_cost_bps_side: float = 0.0

    def contract_candidate(self) -> c1.SignalCandidate:
        return c1.SignalCandidate(
            strategy_id=self.strategy_id,
            setup_id=self.setup_id,
            ticker=self.ticker,
            signal_time=self.signal_time,
            next_executable_time=self.next_executable_time,
            raw_reference_price=0.0,
            structural_stop=self.structural_stop,
            reserved_risk=self.reserved_risk,
            internal_sort_key=self.internal_sort_key,
        )


@dataclass
class PendingOrder:
    candidate: ProgramCandidate
    created_time: str
    idempotency_key: str


@dataclass
class ShadowPosition:
    strategy_id: str
    setup_id: str
    ticker: str
    quantity: int
    entry_raw_price: float
    entry_execution_price: float
    entry_time: str
    active_stop: float
    structural_stop: float
    reserved_risk: float
    cash_out: float
    commission_bps: float = 0.0
    sell_tax_bps: float = 0.0
    execution_cost_bps_side: float = 0.0
    tick_size: float = 0.0
    slippage_ticks: int = 0
    trail_pct: Optional[float] = None
    trail_arm_pct: Optional[float] = None
    target_price: Optional[float] = None
    max_hold_bars: Optional[int] = None
    bars_held: int = 0
    peak_price: float = 0.0
    trail_armed: bool = False
    pending_stop_next_bar: Optional[float] = None


@dataclass
class AccountState:
    starting_equity: float
    cash: float
    peak_equity: float
    day_start_equity: float
    realized_today: float = 0.0
    positions: dict[str, ShadowPosition] = field(default_factory=dict)
    pending_orders: dict[str, PendingOrder] = field(default_factory=dict)
    seen_idempotency_keys: set[str] = field(default_factory=set)
    last_marks: dict[str, float] = field(default_factory=dict)


class JsonStateStore:
    """Atomic-enough small-state persistence for shadow mode only."""
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, s: AccountState) -> None:
        d = asdict(s)
        d["seen_idempotency_keys"] = sorted(s.seen_idempotency_keys)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def load(self, starting_equity: float) -> AccountState:
        if not self.path.exists():
            return AccountState(starting_equity, starting_equity, starting_equity, starting_equity)
        d = json.loads(self.path.read_text(encoding="utf-8"))
        d["positions"] = {k: ShadowPosition(**v) for k, v in d.get("positions", {}).items()}
        d["pending_orders"] = {
            k: PendingOrder(candidate=ProgramCandidate(**v["candidate"]), created_time=v["created_time"], idempotency_key=v["idempotency_key"])
            for k, v in d.get("pending_orders", {}).items()
        }
        d["seen_idempotency_keys"] = set(d.get("seen_idempotency_keys", []))
        return AccountState(**d)


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: dict) -> None:
        event = {"engine_version": VERSION, "order_mode": ORDER_MODE, **event}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True, default=str) + "\n")


@dataclass(frozen=True)
class RuntimeRiskLimits:
    max_positions: int = 4
    max_total_risk_pct: float = 0.02
    daily_loss_stop_pct: float = 0.015
    dd_halt_pct: float = 0.08


class ShadowTradingEngine:
    def __init__(self, starting_equity: float, state_path: str | Path, audit_path: str | Path,
                 limits: RuntimeRiskLimits = RuntimeRiskLimits()):
        assert LIVE_APPROVAL is False and ORDER_MODE == "SHADOW_ONLY_NO_ORDERS"
        self.store = JsonStateStore(state_path)
        self.audit = AuditLog(audit_path)
        self.state = self.store.load(starting_equity)
        self.limits = limits
        if self.store.path.exists():
            self.audit.emit({"event":"STATE_RESTORE","time":"","positions":sorted(self.state.positions),
                             "pending":sorted(self.state.pending_orders),"cash":self.state.cash})

    def equity(self) -> float:
        return self.state.cash + sum(
            p.quantity * self.state.last_marks.get(t, p.entry_execution_price)
            for t, p in self.state.positions.items()
        )

    def reserved_risk(self) -> float:
        return sum(p.reserved_risk for p in self.state.positions.values()) + sum(
            o.candidate.reserved_risk for o in self.state.pending_orders.values()
        )

    def _snapshot(self) -> c1.RiskSnapshot:
        eq = self.equity()
        self.state.peak_equity = max(self.state.peak_equity, eq)
        dd = 1.0 - eq / self.state.peak_equity if self.state.peak_equity > 0 else 0.0
        return c1.RiskSnapshot(
            equity=eq,
            open_positions=len(self.state.positions) + len(self.state.pending_orders),
            reserved_risk=self.reserved_risk(),
            realized_today=self.state.realized_today,
            day_start_equity=self.state.day_start_equity,
            drawdown=dd,
            open_tickers=set(self.state.positions) | {o.candidate.ticker for o in self.state.pending_orders.values()},
        )

    def submit_candidates(self, candidates: Iterable[ProgramCandidate], event_time: str) -> None:
        ordered = sorted(candidates, key=lambda x: (x.internal_sort_key or x.ticker, x.ticker, x.setup_id))
        for rank, cand in enumerate(ordered, 1):
            idem = stable_key("SIGNAL", cand.strategy_id, cand.setup_id, cand.ticker, cand.signal_time)
            if idem in self.state.seen_idempotency_keys:
                self.audit.emit({"event":"DUPLICATE_IGNORED","time":event_time,"ticker":cand.ticker,
                                 "setup_id":cand.setup_id,"queue_rank":rank,"idempotency_key":idem})
                continue
            self.state.seen_idempotency_keys.add(idem)
            self.audit.emit({"event":"SIGNAL","time":event_time,"ticker":cand.ticker,"setup_id":cand.setup_id,
                             "strategy_id":cand.strategy_id,"queue_rank":rank,"reserved_risk":cand.reserved_risk,
                             "next_executable_time":cand.next_executable_time,"idempotency_key":idem})
            snap = self._snapshot()
            ok, reason = c1.risk_decision(cand.contract_candidate(), snap, c1.RiskLimits(
                max_positions=self.limits.max_positions,
                max_total_risk_pct=self.limits.max_total_risk_pct,
                daily_loss_stop_pct=self.limits.daily_loss_stop_pct,
                dd_halt_pct=self.limits.dd_halt_pct,
            ))
            if not ok:
                self.audit.emit({"event":"REJECT","time":event_time,"ticker":cand.ticker,"setup_id":cand.setup_id,
                                 "strategy_id":cand.strategy_id,"queue_rank":rank,"reason":reason,
                                 "reserved_risk_before":snap.reserved_risk,"equity":snap.equity})
                continue
            key = stable_key("ORDER", cand.strategy_id, cand.setup_id, cand.ticker, cand.next_executable_time)
            self.state.pending_orders[key] = PendingOrder(cand, event_time, key)
            self.audit.emit({"event":"ORDER_INTENT","time":event_time,"ticker":cand.ticker,"setup_id":cand.setup_id,
                             "strategy_id":cand.strategy_id,"queue_rank":rank,"reason":"RISK_ACCEPT",
                             "idempotency_key":key,"planned_notional":cand.planned_notional})
        self.store.save(self.state)

    @staticmethod
    def _adverse_price(raw: float, side: str, tick_size: float, ticks: int) -> float:
        if tick_size <= 0 or ticks <= 0:
            return float(raw)
        return float(raw) + (tick_size * ticks if side == "BUY" else -tick_size * ticks)

    @staticmethod
    def _fee(notional: float, commission_bps: float, execution_cost_bps_side: float) -> float:
        return float(notional) * (float(commission_bps) + float(execution_cost_bps_side)) / 10_000.0

    def _fill_pending_for_bar(self, bar: Bar) -> None:
        for key, order in list(self.state.pending_orders.items()):
            cand = order.candidate
            if cand.ticker != bar.ticker or _dt(bar.time) < _dt(cand.next_executable_time):
                continue
            px = self._adverse_price(bar.open, "BUY", cand.tick_size, cand.slippage_ticks)
            qty = int(math.floor(cand.planned_notional / px + 1e-12))
            fee = self._fee(qty * px, cand.commission_bps, cand.execution_cost_bps_side)
            if qty < 1 or self.state.cash + 1e-9 < qty * px + fee:
                self.audit.emit({"event":"REJECT","time":bar.time,"ticker":cand.ticker,"setup_id":cand.setup_id,
                                 "strategy_id":cand.strategy_id,"reason":"VIRTUAL_FILL_INFEASIBLE","quantity":qty})
                del self.state.pending_orders[key]
                continue
            self.state.cash -= qty * px + fee
            p = ShadowPosition(
                strategy_id=cand.strategy_id, setup_id=cand.setup_id, ticker=cand.ticker, quantity=qty,
                entry_raw_price=float(bar.open), entry_execution_price=px, entry_time=bar.time,
                active_stop=float(cand.structural_stop), structural_stop=float(cand.structural_stop),
                reserved_risk=float(cand.reserved_risk), cash_out=qty * px + fee,
                commission_bps=cand.commission_bps, sell_tax_bps=cand.sell_tax_bps,
                execution_cost_bps_side=cand.execution_cost_bps_side,
                tick_size=cand.tick_size, slippage_ticks=cand.slippage_ticks,
                trail_pct=cand.trail_pct, trail_arm_pct=cand.trail_arm_pct,
                target_price=cand.target_price, max_hold_bars=cand.max_hold_bars,
                peak_price=px,
            )
            self.state.positions[cand.ticker] = p
            self.state.last_marks[cand.ticker] = px
            del self.state.pending_orders[key]
            self.audit.emit({"event":"FILL","time":bar.time,"ticker":cand.ticker,"setup_id":cand.setup_id,
                             "strategy_id":cand.strategy_id,"side":"BUY","raw_price":bar.open,
                             "execution_price":px,"quantity":qty,"fee":fee,"bar_interval":bar.interval,
                             "data_fidelity":bar.fidelity})

    def _close(self, ticker: str, raw_price: float, time: str, reason: str, bar: Bar) -> None:
        p = self.state.positions[ticker]
        px = self._adverse_price(raw_price, "SELL", p.tick_size, p.slippage_ticks)
        gross = p.quantity * px
        fee = self._fee(gross, p.commission_bps, p.execution_cost_bps_side)
        tax = gross * p.sell_tax_bps / 10_000.0
        cash_in = gross - fee - tax
        self.state.cash += cash_in
        pnl = cash_in - p.cash_out
        self.state.realized_today += pnl
        self.audit.emit({"event":"CLOSED","time":time,"ticker":ticker,"setup_id":p.setup_id,
                         "strategy_id":p.strategy_id,"side":"SELL","reason":reason,
                         "raw_price":raw_price,"execution_price":px,"quantity":p.quantity,
                         "fee":fee,"tax":tax,"pnl":pnl,"bar_interval":bar.interval,
                         "data_fidelity":bar.fidelity})
        del self.state.positions[ticker]
        self.state.last_marks.pop(ticker, None)

    def on_bar(self, bar: Bar) -> None:
        # 1) Existing positions see the executable open first (gap semantics).
        if bar.ticker in self.state.positions:
            p = self.state.positions[bar.ticker]
            if p.pending_stop_next_bar is not None:
                p.active_stop = max(p.structural_stop, float(p.pending_stop_next_bar))
                p.pending_stop_next_bar = None
                self.audit.emit({"event":"STOP_UPDATE","time":bar.time,"ticker":bar.ticker,
                                 "setup_id":p.setup_id,"active_stop":p.active_stop,"reason":"EFFECTIVE_NEXT_BAR"})
            if c1.resolve_gap_stop(bar.open, p.active_stop):
                self._close(bar.ticker, bar.open, bar.time, "GAP_STOP", bar)

        # 2) Pending entry can fill only on/after next_executable_time.
        self._fill_pending_for_bar(bar)

        # 3) Intrabar first-touch handling on the lowest available runtime bar.
        if bar.ticker in self.state.positions:
            p = self.state.positions[bar.ticker]
            p.bars_held += 1
            outcome = c1.conservative_intrabar_resolution(bar.low, bar.high, p.active_stop, p.target_price)
            if outcome == "STOP_FIRST_AMBIGUOUS_INTRABAR":
                self.audit.emit({"event":"AMBIGUOUS_INTRABAR","time":bar.time,"ticker":bar.ticker,
                                 "setup_id":p.setup_id,"stop":p.active_stop,"target":p.target_price,
                                 "bar_low":bar.low,"bar_high":bar.high})
                self._close(bar.ticker, p.active_stop, bar.time, outcome, bar)
            elif outcome == "STOP":
                self._close(bar.ticker, p.active_stop, bar.time, "STOP_FIRST_TOUCH", bar)
            elif outcome == "TARGET":
                self._close(bar.ticker, p.target_price, bar.time, "TARGET_FIRST_TOUCH", bar)

        # 4) Trail updates use this completed bar but are effective next bar only.
        if bar.ticker in self.state.positions:
            p = self.state.positions[bar.ticker]
            p.peak_price = max(p.peak_price, float(bar.high))
            arm = p.trail_arm_pct if p.trail_arm_pct is not None else p.trail_pct
            if p.trail_pct is not None and arm is not None:
                if p.peak_price >= p.entry_execution_price * (1.0 + float(arm)):
                    if not p.trail_armed:
                        p.trail_armed = True
                        self.audit.emit({"event":"TRAIL_ARMED","time":bar.time,"ticker":bar.ticker,
                                         "setup_id":p.setup_id,"peak":p.peak_price,"trail_pct":p.trail_pct})
                    p.pending_stop_next_bar = max(p.structural_stop, p.peak_price * (1.0 - p.trail_pct))
            if p.max_hold_bars is not None and p.bars_held >= p.max_hold_bars:
                self._close(bar.ticker, bar.close, bar.time, "MAX_HOLD_CLOSE", bar)

        if bar.ticker in self.state.positions:
            self.state.last_marks[bar.ticker] = float(bar.close)
        self.state.peak_equity = max(self.state.peak_equity, self.equity())
        self.store.save(self.state)

    def force_exit_at_open(self, ticker: str, bar: Bar, reason: str = "EXTERNAL_EXIT_INTENT") -> None:
        if ticker in self.state.positions:
            self._close(ticker, bar.open, bar.time, reason, bar)
            self.store.save(self.state)

    def rollover_day(self) -> None:
        self.state.day_start_equity = self.equity()
        self.state.realized_today = 0.0
        self.store.save(self.state)


@dataclass
class EtfHysteresisState:
    lever: str
    base: str
    state: str
    band: float
    ma_days: int = 200

    def on_completed_close(self, date: str, signal_close: float, ma: float) -> dict:
        before = self.state
        self.state = c1.etf_next_session_asset(before, signal_close, ma, self.band)
        return {
            "date": date,
            "held_state_before_close": before,
            "next_session_state": self.state,
            "event": "SWITCH" if before != self.state else "HOLD",
            "lever": self.lever,
            "base": self.base,
            "signal_close": float(signal_close),
            "ma": float(ma),
            "band": float(self.band),
        }


def self_test() -> None:
    assert LIVE_APPROVAL is False and ORDER_MODE == "SHADOW_ONLY_NO_ORDERS"
    assert EtfHysteresisState("TQQQ","QQQ","BASE",.03).on_completed_close("d",104,100)["next_session_state"] == "LEVER"
    print("SELF_TEST=PASS")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test()
    else:
        print(json.dumps({"version":VERSION,"order_mode":ORDER_MODE,"live_approval":LIVE_APPROVAL}, ensure_ascii=False))
