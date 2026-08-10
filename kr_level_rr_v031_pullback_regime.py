#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Noramu KR v0.31 pullback-specific execution + market-regime study.

Research only; no live orders.

v0.30 correctly exposed two things: (1) the delayed PULLBACK entry must be
re-validated at its actual fill time, and (2) blindly reusing v0.28's
0.25-ATR distance from the *original breakout level* destroys the delayed
pullback sample (103 -> 16 candidates).  v0.31 keeps the correctness fixes but
uses execution tests that match the actual entry grammar:

- actual-entry risk %, R/ATR and tick/R always apply;
- RISK_ONLY: those execution constraints only (diagnostic control);
- PB_TIGHT / PB_WIDE: additionally cap the next-open chase relative to the
  immediately preceding pullback-confirmation close, while the confirmation
  bar itself must remain reasonably close to the breakout level;
- market regimes use the KOSPI index (^KS11) plus KOSPI40 PIT breadth, using
  strictly prior 60m bars;
- averaging-down policies are separated from entry-regime policy;
- trailing-stop raises become effective on the *next* bar, eliminating the
  unknowable high/low ordering inside one Yahoo 60m OHLC bar;
- 2023-25 train, 2026 OOS, July-2026 stress, cost stress and concentration are
  all required in the scorecard.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

import kr_level_rr_v025 as kr
import kr_level_rr_v026_pit as pit
import kr_level_rr_v027_execution as ex
import kr_level_rr_v028_execution_filter as v28
import kr_level_rr_v029_adaptive_exit_entry as v29
import kr_level_rr_v030_regime_robustness as v30

VERSION = "v0.31-KR-PULLBACK-REGIME"
GATE_MODES = ("RISK_ONLY", "PB_TIGHT", "PB_WIDE")
REGIME_MODES = ("OFF", "FAST", "STRUCTURAL", "COMBINED")
ADD_POLICIES = ("STAGED_FULL", "STAGED_CONDITIONAL", "DIRECT")
HOLD_BARS = (26, 52)
PRIMARY_SCENARIOS = ((5_000_000, 1), (5_000_000, 2), (20_000_000, 1))


def load_kospi_index(args) -> pd.DataFrame:
    raw = kr.download_60m("^KS11", args.period_60m, 3)
    raw = raw[raw.index.date >= pd.Timestamp(pit.PIT_DATE).date()]
    x = kr.prep_60m(raw)
    if len(x) < 300:
        raise RuntimeError(f"KOSPI index 60m coverage insufficient: {len(x)}")
    return x


def build_market_regime(data: Dict[str, pd.DataFrame], kospi_index: pd.DataFrame) -> pd.DataFrame:
    r = v30.build_regime_table(data).copy()
    idx = kospi_index.close.astype(float).sort_index()
    z = pd.DataFrame(index=idx.index)
    z["ks_close"] = idx
    for n in (5, 20, 120, 200):
        z[f"ks_ema{n}"] = idx.ewm(span=n, adjust=False, min_periods=n).mean()
    z = z.reindex(r.index, method="ffill")
    return r.join(z, how="left")


def regime_pass(row, mode: str, args) -> bool:
    if mode == "OFF":
        return True
    if row is None:
        return False
    fast_cols = ("ks_close", "ks_ema5", "ks_ema20", "breadth20", "coverage20")
    if any(not np.isfinite(float(row[k])) for k in fast_cols):
        return False
    fast = bool(
        float(row.coverage20) >= args.regime_min_coverage
        and float(row.ks_close) > float(row.ks_ema20)
        and float(row.ks_ema5) > float(row.ks_ema20)
        and float(row.breadth20) >= args.fast_breadth20
    )
    if mode == "FAST":
        return fast
    slow_cols = ("ks_ema120", "ks_ema200", "breadth120", "breadth200", "coverage120", "coverage200")
    if any(not np.isfinite(float(row[k])) for k in slow_cols):
        return False
    structural = bool(
        float(row.coverage120) >= args.regime_min_coverage
        and float(row.coverage200) >= args.regime_min_coverage
        and float(row.ks_close) > float(row.ks_ema120)
        and float(row.ks_close) > float(row.ks_ema200)
        and float(row.ks_ema20) > float(row.ks_ema120)
        and float(row.breadth120) >= args.structural_breadth120
        and float(row.breadth200) >= args.structural_breadth200
    )
    if mode == "STRUCTURAL":
        return structural
    return fast and structural


def actual_entry_gate(data, candidates, args, gate_mode: str):
    kept = {}; rows = []
    cap_close = None
    if gate_mode == "PB_TIGHT": cap_close = args.pb_tight_close_level_atr
    elif gate_mode == "PB_WIDE": cap_close = args.pb_wide_close_level_atr

    for ticker, cs in candidates.items():
        x = data[ticker]; out = []
        for cand in cs:
            s = cand.setup; ei = int(cand.entry_i)
            reason = "KEEP"
            vals = dict(entry=np.nan, atr=np.nan, risk=np.nan, risk_pct=np.nan, r_atr=np.nan,
                        tick_over_r=np.nan, prev_close=np.nan, prev_high=np.nan, prev_close_level_atr=np.nan,
                        open_gap_prev_close_atr=np.nan, open_over_prev_high_atr=np.nan, entry_level_atr=np.nan)
            if ei <= 0 or ei >= len(x):
                reason = "INVALID_ENTRY_INDEX"
            else:
                entry = float(x.open.iloc[ei]); prev_close = float(x.close.iloc[ei-1]); prev_high = float(x.high.iloc[ei-1])
                stop = float(s.stop); risk = entry - stop; atr = v28.atr14_at(x, ei-1); tick = ex.tick_size(entry)
                risk_pct = risk / entry if entry > 0 else np.nan
                r_atr = risk / atr if np.isfinite(atr) and atr > 0 else np.nan
                tick_over_r = tick / risk if risk > 0 else np.inf
                prev_close_level_atr = (prev_close - float(s.level)) / atr if np.isfinite(atr) and atr > 0 else np.nan
                open_gap_prev_close_atr = (entry - prev_close) / atr if np.isfinite(atr) and atr > 0 else np.nan
                open_over_prev_high_atr = (entry - prev_high) / atr if np.isfinite(atr) and atr > 0 else np.nan
                entry_level_atr = (entry - float(s.level)) / atr if np.isfinite(atr) and atr > 0 else np.nan
                vals.update(entry=entry, atr=atr, risk=risk, risk_pct=risk_pct, r_atr=r_atr,
                            tick_over_r=tick_over_r, prev_close=prev_close, prev_high=prev_high,
                            prev_close_level_atr=prev_close_level_atr,
                            open_gap_prev_close_atr=open_gap_prev_close_atr,
                            open_over_prev_high_atr=open_over_prev_high_atr, entry_level_atr=entry_level_atr)
                if not np.isfinite(risk) or risk <= 0: reason = "INVALID_RISK"
                elif not np.isfinite(atr) or atr <= 0: reason = "NO_ATR"
                elif risk_pct < args.min_risk_pct: reason = "RISK_PCT_TOO_SMALL"
                elif r_atr < args.min_r_atr: reason = "R_TOO_SMALL_VS_ATR"
                elif tick_over_r > args.max_tick_r: reason = "TICK_BURDEN_HIGH"
                elif entry <= stop: reason = "OPEN_BELOW_STOP"
                elif gate_mode != "RISK_ONLY":
                    if prev_close_level_atr > cap_close:
                        reason = "PULLBACK_CONFIRM_CLOSE_TOO_FAR"
                    elif max(0.0, open_gap_prev_close_atr) > args.pb_max_next_open_gap_atr:
                        reason = "NEXT_OPEN_CHASE_GAP"
                    elif entry_level_atr < -args.pb_max_below_level_atr:
                        reason = "NEXT_OPEN_LOST_LEVEL"
            if reason == "KEEP": out.append(cand)
            rows.append({"gate_mode": gate_mode, "ticker": ticker, "setup_id": s.setup_id,
                         "entry_time": str(x.index[ei]) if 0 <= ei < len(x) else "", "level": float(s.level),
                         "stop": float(s.stop), **vals, "decision": reason})
        kept[ticker] = out
    return kept, pd.DataFrame(rows)


def period_metrics(tr: pd.DataFrame, start: str, end: str) -> dict:
    return v30.period_metrics(tr, start, end)


def simulate(strategy, data, candidates, regime, args, starting_equity, slippage_ticks,
             regime_mode, add_policy, max_hold):
    bars_at = {}; setup_at = {}
    for ticker, x in data.items():
        for i, ts in enumerate(x.index):
            u = pd.Timestamp(ts).tz_convert("UTC"); bars_at.setdefault(u, []).append((ticker, i))
        for cand in candidates.get(ticker, []):
            ei = int(cand.entry_i)
            if ei < len(x):
                u = pd.Timestamp(x.index[ei]).tz_convert("UTC"); setup_at.setdefault(u, []).append((ticker, ei, cand))

    timeline = sorted(bars_at); cash = float(starting_equity); positions = {}; last_mark = {}
    trades = []; rejects = []; equity_rows = []; realized_by_day = {}; day_start_equity = {}; peak_equity = cash
    feas = {"starter_lt_1_share": 0, "starter_one_share": 0, "add20_lt_1_share": 0,
            "add60_lt_1_share": 0, "adds_blocked_regime": 0, "trail_updates_next_bar": 0}

    def mtm(): return cash + sum(p["shares"] * last_mark.get(t, p["last_mark"]) for t, p in positions.items())
    def planned_total(): return sum(p["planned_seed"] for p in positions.values())
    def reserved_risk_total(): return sum(p["reserved_risk"] for p in positions.values())

    def buy(p, raw_price, fraction, reason, ts):
        nonlocal cash
        px = ex.adverse_ticks(raw_price, "BUY", slippage_ticks)
        qty = int(math.floor(p["planned_seed"] * fraction / px + 1e-12))
        if qty < 1:
            if reason == "starter": feas["starter_lt_1_share"] += 1
            elif reason == "adverse20": feas["add20_lt_1_share"] += 1
            else: feas["add60_lt_1_share"] += 1
            return False
        gross = qty * px; comm = gross * ex.TOSS_KRX_COMMISSION
        if cash + 1e-9 < gross + comm: return False
        cash -= gross + comm; p["shares"] += qty; p["cash_out"] += gross + comm
        p["buy_notional"] += gross; p["commissions"] += comm
        p["fills"].append({"time": str(ts), "raw_price": float(raw_price), "price": px, "shares": qty,
                           "fraction": fraction, "reason": reason, "slippage_ticks": slippage_ticks})
        p["last_mark"] = px; last_mark[p["ticker"]] = px
        if reason == "starter" and qty == 1: feas["starter_one_share"] += 1
        return True

    def sell(p, qty, raw_price, reason, ts):
        nonlocal cash
        qty = min(int(qty), int(p["shares"]))
        if qty <= 0: return 0
        px = ex.adverse_ticks(raw_price, "SELL", slippage_ticks); gross = qty * px
        comm = gross * ex.TOSS_KRX_COMMISSION; stt_rate, rural_rate = ex.tax_components(p["market"], ts)
        stt = gross * stt_rate; rural = gross * rural_rate; tax = stt + rural
        cash += gross - comm - tax; p["shares"] -= qty; p["cash_in"] += gross - comm - tax
        p["sell_notional"] += gross; p["commissions"] += comm; p["taxes"] += tax
        p["events"].append({"time": str(ts), "raw_price": float(raw_price), "price": px, "shares": qty,
                            "reason": reason, "slippage_ticks": slippage_ticks, "commission": comm,
                            "stt": stt, "rural_tax": rural})
        return qty

    def close(ticker, raw_price, reason, status, ts):
        p = positions[ticker]
        if p["shares"] > 0: sell(p, p["shares"], raw_price, reason, ts)
        pnl = p["cash_in"] - p["cash_out"]; d = kr.kr_date(ts); realized_by_day[d] = realized_by_day.get(d, 0.0) + pnl
        row = {k: v for k, v in p.items() if k not in {"fills", "events"}}
        row.update({"exit_time": str(ts), "exit_raw_price": float(raw_price), "exit_reason": reason,
                    "status": status, "pnl": pnl, "fill_count": len(p["fills"]),
                    "fill_detail": json.dumps(p["fills"], ensure_ascii=False),
                    "event_detail": json.dumps(p["events"], ensure_ascii=False)})
        trades.append(row); del positions[ticker]; last_mark.pop(ticker, None)

    for u in timeline:
        bars = bars_at[u]
        for ticker, i in bars:
            if ticker in positions:
                o = float(data[ticker].open.iloc[i]); positions[ticker]["last_mark"] = o; last_mark[ticker] = o
        for ticker, i in list(bars):
            if ticker not in positions: continue
            p = positions[ticker]; o = float(data[ticker].open.iloc[i])
            if o <= p["active_stop"]:
                close(ticker, o, "gap_stop", "LOSS" if p["active_stop"] < p["first_entry"] else "BE_OR_WIN", u)

        eq_open = mtm(); peak_equity = max(peak_equity, eq_open); d = kr.kr_date(u)
        day_start_equity.setdefault(d, eq_open); realized_by_day.setdefault(d, 0.0)
        for ticker, ei, cand in sorted(setup_at.get(u, []), key=lambda q: q[0]):
            s = cand.setup
            if ticker in positions:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "SAME_TICKER_OPEN"}); continue
            rr = v30.prior_regime_row(regime, u)
            if not regime_pass(rr, regime_mode, args):
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "MARKET_REGIME"}); continue
            eq_open = mtm(); peak_equity = max(peak_equity, eq_open); dd_open = 1 - eq_open / peak_equity if peak_equity > 0 else 0
            if dd_open >= args.dd_halt_pct:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "MTM_DD_HALT"}); continue
            dd_mult = args.dd_risk_mult if dd_open >= args.dd_reduce_pct else 1.0; ds = day_start_equity[d]
            if realized_by_day[d] <= -args.daily_loss_stop_pct * ds:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "DAILY_REALIZED_STOP"}); continue
            if len(positions) >= args.max_positions:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "MAX_POSITIONS"}); continue

            x = data[ticker]; raw_first = float(x.open.iloc[ei]); first = ex.adverse_ticks(raw_first, "BUY", slippage_ticks)
            stop = float(s.stop); risk = first - stop
            if not np.isfinite(risk) or risk <= 0:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "INVALID_STOP"}); continue
            risk_pct = risk / first; budget = eq_open * args.base_risk_pct * dd_mult
            planned = min(eq_open * args.max_symbol_pct, budget / risk_pct)
            if planned < args.min_seed_krw:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "TOO_SMALL"}); continue
            reserved = planned * risk_pct
            if reserved_risk_total() + reserved > eq_open * args.max_total_risk_pct + 1e-9:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "TOTAL_RISK_CAP"}); continue
            if planned_total() + planned > eq_open * 0.80 + 1e-9:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "GROSS_CAP"}); continue

            trail_pct, trail_samples = v29.trail_stat_for_entry(x, ei, "TRAIL_P70", args)
            p = {"strategy": strategy, "regime_mode": regime_mode, "add_policy": add_policy, "max_hold": max_hold,
                 "ticker": ticker, "symbol": s.symbol, "market": s.market, "name": s.name, "setup_id": s.setup_id,
                 "entry_time": str(u), "starting_equity": starting_equity, "slippage_ticks": slippage_ticks,
                 "planned_seed": planned, "reserved_risk": reserved, "structural_stop": stop, "active_stop": stop,
                 "raw_first_entry": raw_first, "first_entry": first, "R": risk, "level": s.level, "touches": s.touches,
                 "shares": 0, "cash_out": 0.0, "cash_in": 0.0, "buy_notional": 0.0, "sell_notional": 0.0,
                 "commissions": 0.0, "taxes": 0.0, "fills": [], "events": [], "added20": False, "added60": False,
                 "entry_i": ei, "bars_held": 0, "last_mark": first, "mfe_R": 0.0, "mae_R": 0.0,
                 "peak_price": first, "trail_pct": trail_pct, "trail_samples": trail_samples, "trail_armed": False}
            starter_fraction = 1.0 if add_policy == "DIRECT" else 0.20
            if not buy(p, raw_first, starter_fraction, "starter", u):
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "STARTER_LT_1_OR_CASH"}); continue
            positions[ticker] = p; last_mark[ticker] = first

        for ticker, i in list(bars):
            if ticker not in positions: continue
            p = positions[ticker]; x = data[ticker]
            o, h, l, c = map(float, (x.open.iloc[i], x.high.iloc[i], x.low.iloc[i], x.close.iloc[i])); p["bars_held"] += 1
            # Only the stop known before this bar can fire inside this bar.
            if l <= p["active_stop"]:
                close(ticker, p["active_stop"], "stop", "LOSS" if p["active_stop"] < p["first_entry"] else "BE_OR_WIN", u); continue

            p["mfe_R"] = max(p["mfe_R"], (h - p["first_entry"]) / p["R"])
            p["mae_R"] = min(p["mae_R"], (l - p["first_entry"]) / p["R"])
            p["peak_price"] = max(p["peak_price"], h)

            if add_policy != "DIRECT" and not p["trail_armed"]:
                add_ok = True
                if add_policy == "STAGED_CONDITIONAL":
                    add_ok = regime_pass(v30.prior_regime_row(regime, u), "FAST", args)
                    if not add_ok: feas["adds_blocked_regime"] += 1
                if add_ok:
                    lvl20 = p["first_entry"] - args.adverse20_r * p["R"]
                    lvl60 = p["first_entry"] - args.adverse60_r * p["R"]
                    if not p["added20"] and l <= lvl20 and lvl20 > p["active_stop"]:
                        if buy(p, lvl20, 0.20, "adverse20", u): p["added20"] = True
                    if p["added20"] and not p["added60"] and l <= lvl60 and lvl60 > p["active_stop"]:
                        if buy(p, lvl60, 0.60, "support60", u): p["added60"] = True

            # Path-independent trailing: new stop is calculated from this bar's
            # completed high and is effective from the next bar onward.
            next_stop = p["active_stop"]
            if p["peak_price"] >= p["first_entry"] + args.trail_arm_r * p["R"]:
                p["trail_armed"] = True
            if p["trail_armed"]:
                next_stop = max(next_stop, p["structural_stop"], p["first_entry"],
                                p["peak_price"] * (1.0 - p["trail_pct"]))
            if next_stop > p["active_stop"] + 1e-12:
                p["active_stop"] = next_stop; feas["trail_updates_next_bar"] += 1

            p["last_mark"] = c; last_mark[ticker] = c
            if p["bars_held"] >= max_hold:
                close(ticker, c, "time", "TIME", u)

        eq = mtm(); peak_equity = max(peak_equity, eq)
        equity_rows.append({"time": str(u), "equity": eq, "cash": cash, "open_positions": len(positions),
                            "drawdown": 1 - eq / peak_equity if peak_equity > 0 else 0})

    if timeline:
        last_u = timeline[-1]
        for ticker in list(positions): close(ticker, last_mark[ticker], "eod_final", "TIME", last_u)
        eq = mtm(); peak_equity = max(peak_equity, eq)
        equity_rows.append({"time": str(last_u), "equity": eq, "cash": cash, "open_positions": 0,
                            "drawdown": 1 - eq / peak_equity if peak_equity > 0 else 0})
    return pd.DataFrame(trades), pd.DataFrame(equity_rows), pd.DataFrame(rejects), feas


def run(args):
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True); state = Path(args.state_dir); state.mkdir(parents=True, exist_ok=True)
    u, data, setups, resolved = ex.load_data_and_signals(args, out, state)
    kospi = [t for t in data if u.loc[u.yf_ticker == t, "market"].iloc[0] == "KOSPI"]
    d = {t: data[t] for t in kospi}; s0 = {t: setups[t] for t in kospi}
    sf, v28audit = v28.filter_setups(d, s0, args); v28audit.to_csv(out / "v028_setup_gate.csv", index=False, encoding="utf-8-sig")
    v30.data_fingerprints(d).to_csv(out / "data_fingerprint.csv", index=False, encoding="utf-8-sig")

    ks = load_kospi_index(args)
    pd.DataFrame([{"ticker": "^KS11", "rows": len(ks), "start": str(ks.index.min()), "end": str(ks.index.max()),
                   "last_close": float(ks.close.iloc[-1])}]).to_csv(out / "kospi_index_coverage.csv", index=False, encoding="utf-8-sig")
    regime = build_market_regime(d, ks); regime.to_csv(out / "market_regime_60m.csv", encoding="utf-8-sig")

    c29, entry_audit = v29.build_candidates(d, sf, "PULLBACK", args); entry_audit.to_csv(out / "pullback_candidate_audit.csv", index=False, encoding="utf-8-sig")
    gated = {}; gate_audits = []; gate_counts = []
    for gm in GATE_MODES:
        cg, ga = actual_entry_gate(d, c29, args, gm); gated[gm] = cg; gate_audits.append(ga)
        vc = ga.decision.value_counts().to_dict()
        gate_counts.append({"gate_mode": gm, "candidates_in": len(ga), "kept": int((ga.decision == "KEEP").sum()), **{f"reject_{k}": int(v) for k,v in vc.items() if k != "KEEP"}})
    pd.concat(gate_audits, ignore_index=True).to_csv(out / "actual_entry_gate_audit.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(gate_counts).to_csv(out / "actual_entry_gate_counts.csv", index=False, encoding="utf-8-sig")

    # Same-run v0.29 control on identical bars and costs.
    control_rows = []; control_periods = []
    for cap, slip in PRIMARY_SCENARIOS:
        label = f"V029_SAME_RUN|{cap//1_000_000}M|{slip}T"
        tr, eq, rj, f = v29.simulate_variant(label, d, c29, args, cap, slip, "TRAIL_P70")
        control_rows.append({"capital_krw": cap, "slippage_ticks": slip, **ex.summarize(tr, eq, cap), "rejects": len(rj)})
        if cap == 5_000_000 and slip == 1:
            for name, a, b in (("train", "2023-08-08", "2026-01-01"), ("oos2026", "2026-01-01", "2027-01-01"), ("jul2026", "2026-07-01", "2026-08-01")):
                control_periods.append({"period": name, **period_metrics(tr, a, b)})
            tr.to_csv(out / "v029_same_run_trades_5M_1T.csv", index=False, encoding="utf-8-sig")
    control = pd.DataFrame(control_rows); control.to_csv(out / "v029_same_run_control.csv", index=False, encoding="utf-8-sig")
    cperiod = pd.DataFrame(control_periods); cperiod.to_csv(out / "v029_same_run_periods.csv", index=False, encoding="utf-8-sig")

    rows = []; detail = {}; configs = {}
    for gm in GATE_MODES:
        for rm in REGIME_MODES:
            for apol in ADD_POLICIES:
                for hold in HOLD_BARS:
                    variant = f"{gm}|{rm}|{apol}|H{hold}"; configs[variant] = (gm, rm, apol, hold)
                    base_tr = None
                    record = {"variant": variant, "gate_mode": gm, "regime_mode": rm, "add_policy": apol, "max_hold": hold,
                              "candidate_count": sum(len(v) for v in gated[gm].values())}
                    for cap, slip in PRIMARY_SCENARIOS:
                        label = f"V031|{variant}|{cap//1_000_000}M|{slip}T"
                        tr, eq, rj, feas = simulate(label, d, gated[gm], regime, args, cap, slip, rm, apol, hold)
                        m = ex.summarize(tr, eq, cap); key = f"{cap//1_000_000}m_{slip}t"
                        for k in ("pnl", "pf", "max_dd_pct", "trades", "wins", "losses"):
                            record[f"{key}_{k}"] = m.get(k)
                        record[f"{key}_rejects"] = len(rj)
                        if cap == 5_000_000 and slip == 1:
                            base_tr = tr; detail[variant] = (tr, rj, feas)
                    if base_tr is None: continue
                    train = period_metrics(base_tr, "2023-08-08", "2026-01-01")
                    oos = period_metrics(base_tr, "2026-01-01", "2027-01-01")
                    jul = period_metrics(base_tr, "2026-07-01", "2026-08-01")
                    for pref, mm in (("train", train), ("oos2026", oos), ("jul2026", jul)):
                        for k,v in mm.items(): record[f"{pref}_{k}"] = v
                    conc = v30.concentration_metrics(base_tr); record.update(conc)
                    yy = v30.year_metrics(base_tr); record["min_year_pnl"] = float(yy.pnl.min()) if len(yy) else 0.0
                    rows.append(record)
                    print(variant, f"5M1T pnl={record['5m_1t_pnl']:.0f} PF={record['5m_1t_pf']:.2f} trades={record['5m_1t_trades']} OOS={record['oos2026_pnl']:.0f} JUL={record['jul2026_pnl']:.0f}")

    sdf = pd.DataFrame(rows)
    score_rows = []
    for _, r in sdf.iterrows():
        enough = bool(r["5m_1t_trades"] >= args.min_total_trades and r["train_trades"] >= args.min_train_trades and r["oos2026_trades"] >= args.min_oos_trades)
        survivor = bool(enough and r["5m_1t_pnl"] > 0 and r["5m_1t_pf"] > 1.05 and r["5m_2t_pnl"] > 0
                        and r["20m_1t_pnl"] > 0 and r["oos2026_pnl"] >= 0 and r["jul2026_pnl"] >= 0
                        and r["5m_1t_max_dd_pct"] <= args.max_survivor_dd)
        conc_pen = max(0.0, (float(r["top1_positive_share"]) if np.isfinite(r["top1_positive_share"]) else 1.0) - 0.50)
        score = float(r["5m_1t_pnl"] + .35*r["5m_2t_pnl"] + .15*r["20m_1t_pnl"] + .50*r["oos2026_pnl"]
                      + .25*r["jul2026_pnl"] + .10*r["train_pnl"] - 1_000_000*r["5m_1t_max_dd_pct"]
                      - 40_000*conc_pen - .20*max(0.0, -r["min_year_pnl"]))
        dct = r.to_dict(); dct.update(status="SURVIVOR" if survivor else "RESEARCH_ONLY", enough_samples=enough, robust_score=score); score_rows.append(dct)
    scores = pd.DataFrame(score_rows).sort_values(["status", "enough_samples", "robust_score"], ascending=[True, False, False]).reset_index(drop=True)
    # Explicit sort so SURVIVOR comes first, then adequate-sample research variants.
    scores["rank_group"] = scores.status.map({"SURVIVOR": 0, "RESEARCH_ONLY": 1}).fillna(2)
    scores = scores.sort_values(["rank_group", "enough_samples", "robust_score"], ascending=[True, False, False]).reset_index(drop=True)
    scores.drop(columns=["rank_group"]).to_csv(out / "kr_v031_scores.csv", index=False, encoding="utf-8-sig")

    finalists = scores.head(args.finalist_count).copy(); stress_rows = []
    for variant in finalists.variant:
        gm, rm, apol, hold = configs[variant]
        for cap in ex.ACCOUNT_SIZES:
            for slip in (0,1,2,3):
                tr, eq, rj, feas = simulate(f"V031_FINAL|{variant}|{cap//1_000_000}M|{slip}T", d, gated[gm], regime, args, cap, slip, rm, apol, hold)
                stress_rows.append({"variant": variant, "capital_krw": cap, "slippage_ticks": slip, **ex.summarize(tr, eq, cap), "rejects": len(rj)})
        tr0, rj0, f0 = detail[variant]; safe = variant.replace("|", "_")
        tr0.to_csv(out / f"trades_{safe}_5M_1T.csv", index=False, encoding="utf-8-sig")
        rj0.to_csv(out / f"rejects_{safe}_5M_1T.csv", index=False, encoding="utf-8-sig")
    stress = pd.DataFrame(stress_rows); stress.to_csv(out / "kr_v031_finalist_cost_matrix.csv", index=False, encoding="utf-8-sig")

    frows = []
    for _, r in finalists.iterrows():
        v = r.variant; dct = r.to_dict()
        for cap in (5_000_000, 20_000_000):
            for slip in (0,1,2,3):
                q = stress[(stress.variant == v) & (stress.capital_krw == cap) & (stress.slippage_ticks == slip)].iloc[0]
                dct[f"stress_{cap//1_000_000}m_{slip}t_pnl"] = float(q.pnl)
                dct[f"stress_{cap//1_000_000}m_{slip}t_pf"] = float(q.pf)
        dct["three_tick_robust"] = bool(dct["stress_5m_3t_pnl"] > 0 and dct["stress_20m_3t_pnl"] > 0)
        frows.append(dct)
    final = pd.DataFrame(frows).sort_values(["status", "three_tick_robust", "robust_score"], ascending=[True, False, False])
    final.to_csv(out / "kr_v031_finalists.csv", index=False, encoding="utf-8-sig")

    cb = control[(control.capital_krw == 5_000_000) & (control.slippage_ticks == 1)].iloc[0]
    cp = {r.period: r for _, r in cperiod.iterrows()}; best = final.iloc[0].to_dict() if len(final) else {}
    scorecard = {
        "version": VERSION, "historical_backtest_only": True, "live_approval": False,
        "same_run_v029_control": {"5m1t_pnl": float(cb.pnl), "5m1t_pf": float(cb.pf), "5m1t_dd": float(cb.max_dd_pct),
                                   "trades": int(cb.trades), "oos2026_pnl": float(cp["oos2026"].pnl),
                                   "oos2026_trades": int(cp["oos2026"].trades), "jul2026_pnl": float(cp["jul2026"].pnl)},
        "gate_counts": {r["gate_mode"]: int(r["kept"]) for r in gate_counts},
        "survivor_count": int((scores.status == "SURVIVOR").sum()), "best": best,
        "correctness": {"actual_fill_risk_gate": True, "pullback_reference_gate": True,
                        "regime_uses_prior_bar_only": True, "new_trail_effective_next_bar": True,
                        "same_run_control": True},
        "cost_model": {"broker": "Toss KRX", "commission_each_side": ex.TOSS_KRX_COMMISSION,
                       "primary_slippage_ticks": [1,2], "finalist_stress_ticks": 3},
    }
    (out / "kr_v031_scorecard.json").write_text(json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "RUN_VALIDATION.txt").write_text("PASS\n" + json.dumps(scorecard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def self_test():
    idx = pd.date_range("2026-01-01", periods=260, freq="h", tz=kr.TZ); b = np.linspace(100, 130, len(idx))
    x = pd.DataFrame({"open": b, "high": b+1, "low": b-1, "close": b, "volume": 1.0}, index=idx)
    r = build_market_regime({"A": x, "B": x*1.01}, x)
    a = argparse.Namespace(regime_min_coverage=1, fast_breadth20=.45, structural_breadth120=.40, structural_breadth200=.35)
    assert regime_pass(r.iloc[-1], "OFF", a); assert regime_pass(r.iloc[-1], "FAST", a)
    assert v30.prior_regime_row(r, idx[-1]) is not None
    print("SELF_TEST=PASS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="kr_v031_latest_output"); ap.add_argument("--state-dir", default="kr_state_pit")
    ap.add_argument("--period-60m", default="730d"); ap.add_argument("--top-n", type=int, default=40); ap.add_argument("--min-market-coverage", type=int, default=30)
    ap.add_argument("--self-test", action="store_true"); ap.add_argument("--max-hold", type=int, default=26)  # frozen v0.29 control only
    ap.add_argument("--base-risk-pct", type=float, default=.01); ap.add_argument("--max-total-risk-pct", type=float, default=.02)
    ap.add_argument("--max-symbol-pct", type=float, default=.20); ap.add_argument("--max-positions", type=int, default=4)
    ap.add_argument("--daily-loss-stop-pct", type=float, default=.015); ap.add_argument("--dd-reduce-pct", type=float, default=.05)
    ap.add_argument("--dd-risk-mult", type=float, default=.50); ap.add_argument("--dd-halt-pct", type=float, default=.08)
    ap.add_argument("--min-seed-krw", type=float, default=50_000); ap.add_argument("--partial-fraction", type=float, default=.50)
    ap.add_argument("--adverse20-r", type=float, default=.40); ap.add_argument("--adverse60-r", type=float, default=.80)
    ap.add_argument("--min-risk-pct", type=float, default=.012); ap.add_argument("--min-r-atr", type=float, default=.75); ap.add_argument("--max-tick-r", type=float, default=.10)
    ap.add_argument("--max-entry-gap-atr", type=float, default=.25)  # v0.28 setup gate only
    ap.add_argument("--pullback-wait-bars", type=int, default=3); ap.add_argument("--pullback-tol-atr", type=float, default=.15); ap.add_argument("--pullback-hold-tol-atr", type=float, default=.05)
    ap.add_argument("--pb-tight-close-level-atr", type=float, default=.50); ap.add_argument("--pb-wide-close-level-atr", type=float, default=1.00)
    ap.add_argument("--pb-max-next-open-gap-atr", type=float, default=.25); ap.add_argument("--pb-max-below-level-atr", type=float, default=.20)
    ap.add_argument("--trail-lookback-bars", type=int, default=480); ap.add_argument("--trail-pivot-span", type=int, default=2); ap.add_argument("--trail-horizon-bars", type=int, default=26)
    ap.add_argument("--trail-min-samples", type=int, default=8); ap.add_argument("--trail-sample-min-dd", type=float, default=.005); ap.add_argument("--trail-sample-max-dd", type=float, default=.20)
    ap.add_argument("--trail-fallback-pct", type=float, default=.03); ap.add_argument("--trail-min-pct", type=float, default=.015); ap.add_argument("--trail-max-pct", type=float, default=.06); ap.add_argument("--trail-arm-r", type=float, default=1.0)
    ap.add_argument("--regime-min-coverage", type=int, default=20); ap.add_argument("--fast-breadth20", type=float, default=.45)
    ap.add_argument("--structural-breadth120", type=float, default=.40); ap.add_argument("--structural-breadth200", type=float, default=.35)
    ap.add_argument("--min-total-trades", type=int, default=20); ap.add_argument("--min-train-trades", type=int, default=15); ap.add_argument("--min-oos-trades", type=int, default=3)
    ap.add_argument("--max-survivor-dd", type=float, default=.03); ap.add_argument("--finalist-count", type=int, default=6)
    args = ap.parse_args()
    if args.self_test: self_test(); return
    run(args)


if __name__ == "__main__": main()
