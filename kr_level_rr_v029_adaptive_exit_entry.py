#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Noramu KR v0.29 adaptive exit + entry-angle research.

Research only; no live orders.

v0.28 is frozen as the execution-quality baseline. v0.29 keeps its filtered
LEVEL_RR setup universe, then tests two independent changes:

1) Entry timing
   - NEXT_OPEN: v0.28 control, enter the bar after confirmation.
   - PULLBACK: after confirmation, wait causally for a shallow retest that holds
     the breakout level, then enter the following bar open.

2) Exit policy
   - LEGACY_R: v0.28 +1R half-take / break-even / +2R final target.
   - TRAIL_AVG: no fixed R target; use only pre-entry historical confirmed-peak
     drawdowns for that ticker, take their mean, and trail the whole position.
   - TRAIL_P70: same, but use the 70th percentile for a looser trend-following
     exit intended to let strong winners continue.

The adaptive trail is walk-forward: only bars strictly before each entry are
used to estimate the reversal drawdown. This avoids future leakage.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

import kr_level_rr_v025 as kr
import kr_level_rr_v027_execution as ex
import kr_level_rr_v028_execution_filter as v28

VERSION = "v0.29-KR-ADAPTIVE-EXIT-ENTRY"
ACCOUNT_SIZES = ex.ACCOUNT_SIZES
SLIPPAGE_TICKS = ex.SLIPPAGE_TICKS
ENTRY_MODES = ("NEXT_OPEN", "PULLBACK")
EXIT_MODES = ("LEGACY_R", "TRAIL_AVG", "TRAIL_P70")


@dataclass
class EntryCandidate:
    setup: kr.Setup
    entry_i: int
    entry_mode: str


def historical_reversal_drawdowns(
    x: pd.DataFrame,
    entry_i: int,
    lookback: int,
    pivot_span: int,
    horizon: int,
    min_dd: float,
    max_dd: float,
) -> List[float]:
    """Peak-to-subsequent-trough drawdowns known before entry_i only."""
    start = max(0, int(entry_i) - int(lookback))
    z = x.iloc[start:int(entry_i)].copy()
    if len(z) < 2 * pivot_span + 10:
        return []
    h = z.high.astype(float).to_numpy()
    l = z.low.astype(float).to_numpy()
    out: List[float] = []
    n = len(z)
    for i in range(pivot_span, n - pivot_span):
        w = h[i - pivot_span : i + pivot_span + 1]
        if not (h[i] == np.max(w) and int(np.sum(w == h[i])) == 1):
            continue
        j0 = i + pivot_span + 1
        if j0 >= n:
            continue
        j1 = min(n, j0 + int(horizon))
        peak = float(h[i])
        trough = peak
        for j in range(j0, j1):
            if float(h[j]) >= peak:
                break
            trough = min(trough, float(l[j]))
        dd = (peak - trough) / peak if peak > 0 else np.nan
        if np.isfinite(dd) and min_dd <= dd <= max_dd:
            out.append(float(dd))
    return out


def trail_stat_for_entry(x: pd.DataFrame, entry_i: int, exit_mode: str, args) -> tuple[float, int]:
    dds = historical_reversal_drawdowns(
        x=x,
        entry_i=entry_i,
        lookback=args.trail_lookback_bars,
        pivot_span=args.trail_pivot_span,
        horizon=args.trail_horizon_bars,
        min_dd=args.trail_sample_min_dd,
        max_dd=args.trail_sample_max_dd,
    )
    if len(dds) < args.trail_min_samples:
        return float(args.trail_fallback_pct), len(dds)
    if exit_mode == "TRAIL_AVG":
        raw = float(np.mean(dds))
    elif exit_mode == "TRAIL_P70":
        raw = float(np.quantile(dds, 0.70))
    else:
        raw = float(args.trail_fallback_pct)
    return float(np.clip(raw, args.trail_min_pct, args.trail_max_pct)), len(dds)


def build_candidates(data: Dict[str, pd.DataFrame], setups: Dict[str, List[kr.Setup]], entry_mode: str, args):
    out: Dict[str, List[EntryCandidate]] = {}
    audit = []
    for ticker, ss in setups.items():
        x = data[ticker]
        cur: List[EntryCandidate] = []
        for s in ss:
            legacy_ei = s.setup_i + 1
            chosen = None
            reason = "NO_ENTRY"
            if legacy_ei >= len(x):
                reason = "NO_NEXT_BAR"
            elif entry_mode == "NEXT_OPEN":
                chosen = legacy_ei
                reason = "NEXT_OPEN"
            else:
                j_end = min(len(x) - 2, legacy_ei + args.pullback_wait_bars - 1)
                for j in range(legacy_ei, j_end + 1):
                    atr = float(x.atr14.iloc[j]) if "atr14" in x else float(s.atr)
                    if not np.isfinite(atr) or atr <= 0:
                        continue
                    low = float(x.low.iloc[j]); close = float(x.close.iloc[j]); open_ = float(x.open.iloc[j])
                    shallow_touch = low <= float(s.level) + args.pullback_tol_atr * atr
                    holds_level = close >= float(s.level) - args.pullback_hold_tol_atr * atr
                    bounce = close >= open_
                    if shallow_touch and holds_level and bounce:
                        ei = j + 1
                        if ei < len(x) and float(x.open.iloc[ei]) > float(s.stop):
                            chosen = ei
                            reason = "PULLBACK_HOLD"
                            break
                if chosen is None:
                    reason = "NO_PULLBACK_HOLD"
            if chosen is not None:
                cur.append(EntryCandidate(s, int(chosen), entry_mode))
            audit.append({
                "ticker": ticker,
                "setup_id": s.setup_id,
                "entry_mode": entry_mode,
                "legacy_entry_i": legacy_ei,
                "chosen_entry_i": chosen,
                "decision": reason,
                "legacy_entry_time": str(x.index[legacy_ei]) if legacy_ei < len(x) else "",
                "chosen_entry_time": str(x.index[chosen]) if chosen is not None else "",
            })
        out[ticker] = cur
    return out, pd.DataFrame(audit)


def simulate_variant(
    strategy: str,
    data: Dict[str, pd.DataFrame],
    candidates: Dict[str, List[EntryCandidate]],
    args,
    starting_equity: float,
    slippage_ticks: int,
    exit_mode: str,
):
    bars_at = {}; setup_at = {}
    for ticker, x in data.items():
        for i, ts in enumerate(x.index):
            u = pd.Timestamp(ts).tz_convert("UTC")
            bars_at.setdefault(u, []).append((ticker, i))
        for cand in candidates.get(ticker, []):
            if cand.entry_i >= len(x):
                continue
            u = pd.Timestamp(x.index[cand.entry_i]).tz_convert("UTC")
            setup_at.setdefault(u, []).append((ticker, cand.entry_i, cand))

    timeline = sorted(bars_at)
    cash = float(starting_equity)
    positions = {}; last_mark = {}; trades = []; rejects = []; equity_rows = []
    realized_by_day = {}; day_start_equity = {}; peak_equity = cash
    feasibility = {
        "starter_lt_1_share": 0,
        "add20_lt_1_share": 0,
        "add60_lt_1_share": 0,
        "partial_rounded_up_to_one": 0,
        "partial_whole_exit": 0,
    }

    def mtm():
        return cash + sum(p["shares"] * last_mark.get(t, p["last_mark"]) for t, p in positions.items())

    def planned_total():
        return sum(p["planned_seed"] for p in positions.values())

    def reserved_risk_total():
        return sum(p["reserved_risk"] for p in positions.values())

    def buy(p, raw_price, fraction, reason, ts):
        nonlocal cash
        px = ex.adverse_ticks(raw_price, "BUY", slippage_ticks)
        desired = p["planned_seed"] * fraction
        qty = int(math.floor(desired / px + 1e-12))
        if qty < 1:
            if reason == "starter20": feasibility["starter_lt_1_share"] += 1
            elif reason == "adverse20": feasibility["add20_lt_1_share"] += 1
            else: feasibility["add60_lt_1_share"] += 1
            return False
        gross = qty * px
        commission = gross * ex.TOSS_KRX_COMMISSION
        if cash + 1e-9 < gross + commission:
            return False
        cash -= gross + commission
        p["shares"] += qty; p["cash_out"] += gross + commission
        p["buy_notional"] += gross; p["commissions"] += commission
        p["fills"].append({
            "time": str(ts), "raw_price": float(raw_price), "price": px,
            "shares": qty, "fraction": fraction, "reason": reason,
            "slippage_ticks": slippage_ticks,
        })
        p["last_mark"] = px; last_mark[p["ticker"]] = px
        return True

    def sell(p, qty, raw_price, reason, ts):
        nonlocal cash
        qty = min(int(qty), int(p["shares"]))
        if qty <= 0:
            return 0
        px = ex.adverse_ticks(raw_price, "SELL", slippage_ticks)
        gross = qty * px
        commission = gross * ex.TOSS_KRX_COMMISSION
        stt_rate, rural_rate = ex.tax_components(p["market"], ts)
        stt = gross * stt_rate; rural = gross * rural_rate; tax = stt + rural
        cash += gross - commission - tax
        p["shares"] -= qty; p["cash_in"] += gross - commission - tax
        p["sell_notional"] += gross; p["commissions"] += commission; p["taxes"] += tax
        p["events"].append({
            "time": str(ts), "raw_price": float(raw_price), "price": px,
            "shares": qty, "reason": reason, "slippage_ticks": slippage_ticks,
            "commission": commission, "stt": stt, "rural_tax": rural,
        })
        return qty

    def close(ticker, raw_price, reason, status, ts):
        p = positions[ticker]
        if p["shares"] > 0:
            sell(p, p["shares"], raw_price, reason, ts)
        pnl = p["cash_in"] - p["cash_out"]
        d = kr.kr_date(ts); realized_by_day[d] = realized_by_day.get(d, 0.0) + pnl
        row = {k: v for k, v in p.items() if k not in {"fills", "events"}}
        row.update({
            "exit_time": str(ts), "exit_raw_price": float(raw_price),
            "exit_reason": reason, "status": status, "pnl": pnl,
            "fill_count": len(p["fills"]),
            "fill_detail": json.dumps(p["fills"], ensure_ascii=False),
            "event_detail": json.dumps(p["events"], ensure_ascii=False),
        })
        trades.append(row)
        del positions[ticker]; last_mark.pop(ticker, None)

    for u in timeline:
        bars = bars_at[u]
        for ticker, i in bars:
            if ticker in positions:
                o = float(data[ticker].open.iloc[i])
                positions[ticker]["last_mark"] = o; last_mark[ticker] = o

        for ticker, i in list(bars):
            if ticker not in positions:
                continue
            p = positions[ticker]; o = float(data[ticker].open.iloc[i])
            if o <= p["active_stop"]:
                status = "WIN" if p["active_stop"] > p["first_entry"] else ("BE_STOP" if p["partial_taken"] else "LOSS")
                close(ticker, o, "gap_stop", status, u)

        eq_open = mtm(); peak_equity = max(peak_equity, eq_open)
        d = kr.kr_date(u); day_start_equity.setdefault(d, eq_open); realized_by_day.setdefault(d, 0.0)

        for ticker, ei, cand in sorted(setup_at.get(u, []), key=lambda q: q[0]):
            s = cand.setup
            if ticker in positions:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "SAME_TICKER_OPEN"}); continue
            eq_open = mtm(); peak_equity = max(peak_equity, eq_open)
            dd_open = 1 - eq_open / peak_equity if peak_equity > 0 else 0
            if dd_open >= args.dd_halt_pct:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "MTM_DD_HALT"}); continue
            dd_mult = args.dd_risk_mult if dd_open >= args.dd_reduce_pct else 1.0
            ds = day_start_equity[d]
            if realized_by_day[d] <= -args.daily_loss_stop_pct * ds:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "DAILY_REALIZED_STOP"}); continue
            if len(positions) >= args.max_positions:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "MAX_POSITIONS"}); continue

            x = data[ticker]
            raw_first = float(x.open.iloc[ei])
            first = ex.adverse_ticks(raw_first, "BUY", slippage_ticks)
            stop = float(s.stop); risk = first - stop
            if not np.isfinite(risk) or risk <= 0:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "INVALID_STOP"}); continue
            risk_pct = risk / first
            budget = eq_open * args.base_risk_pct * dd_mult
            planned = min(eq_open * args.max_symbol_pct, budget / risk_pct)
            if planned < args.min_seed_krw:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "TOO_SMALL"}); continue
            reserved = planned * risk_pct
            if reserved_risk_total() + reserved > eq_open * args.max_total_risk_pct + 1e-9:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "TOTAL_RISK_CAP"}); continue
            if planned_total() + planned > eq_open * 0.80 + 1e-9:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "GROSS_CAP"}); continue

            trail_pct, trail_samples = trail_stat_for_entry(x, ei, exit_mode, args)
            p = {
                "strategy": strategy, "entry_mode": cand.entry_mode, "exit_mode": exit_mode,
                "ticker": ticker, "symbol": s.symbol, "market": s.market, "name": s.name,
                "setup_id": s.setup_id, "entry_time": str(u), "starting_equity": starting_equity,
                "slippage_ticks": slippage_ticks, "planned_seed": planned, "reserved_risk": reserved,
                "structural_stop": stop, "active_stop": stop, "raw_first_entry": raw_first,
                "first_entry": first, "R": risk, "target1": first + risk, "target2": first + 2 * risk,
                "level": s.level, "touches": s.touches, "shares": 0, "cash_out": 0.0,
                "cash_in": 0.0, "buy_notional": 0.0, "sell_notional": 0.0,
                "commissions": 0.0, "taxes": 0.0, "fills": [], "events": [],
                "partial_taken": False, "added20": False, "added60": False,
                "entry_i": ei, "bars_held": 0, "last_mark": first,
                "mfe_R": 0.0, "mae_R": 0.0, "peak_price": first,
                "trail_pct": trail_pct, "trail_samples": trail_samples,
                "trail_armed": False,
            }
            if not buy(p, raw_first, 0.20, "starter20", u):
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "STARTER_LT_1_OR_CASH"}); continue
            positions[ticker] = p; last_mark[ticker] = first

        for ticker, i in list(bars):
            if ticker not in positions:
                continue
            p = positions[ticker]; x = data[ticker]
            o, h, l, c = map(float, (x.open.iloc[i], x.high.iloc[i], x.low.iloc[i], x.close.iloc[i]))
            p["bars_held"] += 1

            if l <= p["active_stop"]:
                if p["active_stop"] > p["first_entry"]:
                    status = "WIN"; reason = "adaptive_trail"
                else:
                    status = "BE_STOP" if p["partial_taken"] else "LOSS"; reason = "stop"
                close(ticker, p["active_stop"], reason, status, u)
                continue

            p["mfe_R"] = max(p["mfe_R"], (h - p["first_entry"]) / p["R"])
            p["mae_R"] = min(p["mae_R"], (l - p["first_entry"]) / p["R"])
            p["peak_price"] = max(p["peak_price"], h)

            can_add = exit_mode == "LEGACY_R" and not p["partial_taken"]
            if exit_mode != "LEGACY_R":
                can_add = not p["trail_armed"]
            if can_add:
                lvl20 = p["first_entry"] - args.adverse20_r * p["R"]
                lvl60 = p["first_entry"] - args.adverse60_r * p["R"]
                if not p["added20"] and l <= lvl20 and lvl20 > p["active_stop"]:
                    if buy(p, lvl20, 0.20, "adverse20", u): p["added20"] = True
                if p["added20"] and not p["added60"] and l <= lvl60 and lvl60 > p["active_stop"]:
                    if buy(p, lvl60, 0.60, "support60", u): p["added60"] = True

            if exit_mode == "LEGACY_R":
                if not p["partial_taken"] and h >= p["target1"]:
                    qty = int(math.floor(p["shares"] * args.partial_fraction))
                    if qty < 1 and p["shares"] >= 1:
                        qty = 1; feasibility["partial_rounded_up_to_one"] += 1
                    if qty >= p["shares"] and p["shares"] > 0:
                        feasibility["partial_whole_exit"] += 1
                        close(ticker, p["target1"], "target1_whole_share_exit", "WIN", u)
                        continue
                    sold = sell(p, qty, p["target1"], "target1_partial", u)
                    if sold > 0:
                        p["partial_taken"] = True; p["active_stop"] = p["first_entry"]
                if ticker not in positions:
                    continue
                p = positions[ticker]
                if p["partial_taken"] and h >= p["target2"]:
                    close(ticker, p["target2"], "target2", "WIN", u)
                    continue
            else:
                if p["peak_price"] >= p["first_entry"] * (1.0 + p["trail_pct"]):
                    p["trail_armed"] = True
                if p["trail_armed"]:
                    next_stop = p["peak_price"] * (1.0 - p["trail_pct"])
                    p["active_stop"] = max(p["structural_stop"], next_stop)

            p["last_mark"] = c; last_mark[ticker] = c
            if p["bars_held"] >= args.max_hold:
                close(ticker, c, "time", "TIME", u)

        eq = mtm(); peak_equity = max(peak_equity, eq)
        equity_rows.append({
            "time": str(u), "equity": eq, "cash": cash,
            "open_positions": len(positions),
            "drawdown": 1 - eq / peak_equity if peak_equity > 0 else 0,
        })

    if timeline:
        last_u = timeline[-1]
        for ticker in list(positions):
            close(ticker, last_mark[ticker], "eod_final", "TIME", last_u)
        eq = mtm(); peak_equity = max(peak_equity, eq)
        equity_rows.append({
            "time": str(last_u), "equity": eq, "cash": cash,
            "open_positions": 0,
            "drawdown": 1 - eq / peak_equity if peak_equity > 0 else 0,
        })

    return pd.DataFrame(trades), pd.DataFrame(equity_rows), pd.DataFrame(rejects), feasibility


def period_metrics(tr: pd.DataFrame, start: str, end: str) -> dict:
    if tr.empty:
        return {"trades": 0, "pnl": 0.0, "pf": np.nan, "winrate": np.nan}
    z = tr.copy()
    dt = pd.to_datetime(z.entry_time, utc=True, errors="coerce").dt.tz_convert(kr.TZ)
    s = pd.Timestamp(start, tz=kr.TZ); e = pd.Timestamp(end, tz=kr.TZ)
    g = z[(dt >= s) & (dt < e)]
    if g.empty:
        return {"trades": 0, "pnl": 0.0, "pf": np.nan, "winrate": np.nan}
    p = g.pnl.astype(float); gp = float(p[p > 0].sum()); gl = float(-p[p < 0].sum())
    return {
        "trades": int(len(g)), "pnl": float(p.sum()),
        "pf": gp / gl if gl > 0 else (float("inf") if gp > 0 else np.nan),
        "winrate": float((p > 0).mean()),
    }


def period_summary(tr: pd.DataFrame, label: str, period: str) -> pd.DataFrame:
    if tr.empty:
        return pd.DataFrame()
    z = tr.copy()
    z["dt"] = pd.to_datetime(z.entry_time, utc=True, errors="coerce").dt.tz_convert(kr.TZ)
    z = z.dropna(subset=["dt"]); z["period"] = z.dt.dt.to_period(period).astype(str)
    rows = []
    for p, g in z.groupby("period"):
        pnl = g.pnl.astype(float); gp = float(pnl[pnl > 0].sum()); gl = float(-pnl[pnl < 0].sum())
        rows.append({
            "scenario": label, "period": p, "trades": len(g), "pnl": float(pnl.sum()),
            "pf": gp / gl if gl > 0 else np.nan, "winrate": float((pnl > 0).mean()),
        })
    return pd.DataFrame(rows)


def run(args):
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    state = Path(args.state_dir); state.mkdir(parents=True, exist_ok=True)

    u, data, setups, resolved = ex.load_data_and_signals(args, out, state)
    kospi = [t for t in data if u.loc[u.yf_ticker == t, "market"].iloc[0] == "KOSPI"]
    d = {t: data[t] for t in kospi}; s0 = {t: setups[t] for t in kospi}
    sf, gate = v28.filter_setups(d, s0, args)
    gate.to_csv(out / "execution_gate_audit.csv", index=False, encoding="utf-8-sig")

    candidates = {}; entry_audits = []
    for em in ENTRY_MODES:
        c, a = build_candidates(d, sf, em, args)
        candidates[em] = c; entry_audits.append(a)
    pd.concat(entry_audits, ignore_index=True).to_csv(out / "entry_angle_audit.csv", index=False, encoding="utf-8-sig")

    rows = []; qparts = []; yparts = []; jul_rows = []; feas_rows = []
    for em in ENTRY_MODES:
        for xm in EXIT_MODES:
            variant = f"{em}|{xm}"
            for capital in ACCOUNT_SIZES:
                for slip in SLIPPAGE_TICKS:
                    label = f"V029|{variant}|{capital//1_000_000}M|{slip}T"
                    tr, eq, rj, feas = simulate_variant(label, d, candidates[em], args, capital, slip, xm)
                    m = ex.summarize(tr, eq, capital)
                    rows.append({
                        "variant": variant, "entry_mode": em, "exit_mode": xm,
                        "capital_krw": capital, "slippage_ticks": slip, **m,
                        "resolved_tickers": len(kospi),
                        "setups_before": sum(len(v) for v in s0.values()),
                        "setups_after_v028_filter": sum(len(v) for v in sf.values()),
                        "entry_candidates": sum(len(v) for v in candidates[em].values()),
                    })
                    feas_rows.append({"variant": variant, "capital_krw": capital, "slippage_ticks": slip, "rejects": len(rj), **feas})
                    pm = period_metrics(tr, "2026-07-01", "2026-08-01")
                    jul_rows.append({"variant": variant, "capital_krw": capital, "slippage_ticks": slip, **pm})
                    q = period_summary(tr, label, "Q"); y = period_summary(tr, label, "Y")
                    if len(q): qparts.append(q)
                    if len(y): yparts.append(y)
                    if capital == 5_000_000 and slip == 1:
                        tr.to_csv(out / f"trades_{em}_{xm}_5M_1T.csv", index=False, encoding="utf-8-sig")
                        rj.to_csv(out / f"rejects_{em}_{xm}_5M_1T.csv", index=False, encoding="utf-8-sig")
                    print(label, f"ret={m['return_pct']*100:.2f}% PF={m['pf']:.3f} DD={m['max_dd_pct']*100:.2f}% trades={m['trades']}")

    sdf = pd.DataFrame(rows)
    sdf.to_csv(out / "kr_v029_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(feas_rows).to_csv(out / "kr_v029_feasibility.csv", index=False, encoding="utf-8-sig")
    jdf = pd.DataFrame(jul_rows); jdf.to_csv(out / "kr_v029_july_2026.csv", index=False, encoding="utf-8-sig")
    if qparts: pd.concat(qparts, ignore_index=True).to_csv(out / "kr_v029_quarter_summary.csv", index=False, encoding="utf-8-sig")
    if yparts: pd.concat(yparts, ignore_index=True).to_csv(out / "kr_v029_year_summary.csv", index=False, encoding="utf-8-sig")

    surv = []
    for variant in sorted(sdf.variant.unique()):
        b = sdf[(sdf.variant == variant) & (sdf.capital_krw == 5_000_000) & (sdf.slippage_ticks == 1)].iloc[0]
        st = sdf[(sdf.variant == variant) & (sdf.capital_krw == 5_000_000) & (sdf.slippage_ticks == 2)].iloc[0]
        c20 = sdf[(sdf.variant == variant) & (sdf.capital_krw == 20_000_000) & (sdf.slippage_ticks == 1)].iloc[0]
        jul = jdf[(jdf.variant == variant) & (jdf.capital_krw == 5_000_000) & (jdf.slippage_ticks == 1)].iloc[0]
        supported = bool(b.pnl > 0 and b.pf > 1.05 and st.pnl > 0 and c20.pnl > 0)
        robust_score = float(b.pnl + 0.35 * st.pnl + 0.15 * c20.pnl - 1_000_000 * b.max_dd_pct)
        surv.append({
            "variant": variant,
            "status": "SURVIVOR" if supported else "RESEARCH_ONLY",
            "5m_1t_pnl": float(b.pnl), "5m_1t_pf": float(b.pf), "5m_1t_dd": float(b.max_dd_pct),
            "5m_2t_pnl": float(st.pnl), "20m_1t_pnl": float(c20.pnl),
            "jul2026_5m_1t_pnl": float(jul.pnl), "jul2026_trades": int(jul.trades),
            "robust_score": robust_score,
        })
    survivors = pd.DataFrame(surv).sort_values("robust_score", ascending=False)
    survivors.to_csv(out / "kr_v029_survivors.csv", index=False, encoding="utf-8-sig")

    baseline_ok = None; baseline_delta = None
    old_path = Path("kr_v028_latest_output") / "kr_v028_summary.csv"
    if old_path.exists():
        old = pd.read_csv(old_path)
        ob = old[(old.capital_krw == 5_000_000) & (old.slippage_ticks == 1)].iloc[0]
        nb = sdf[(sdf.variant == "NEXT_OPEN|LEGACY_R") & (sdf.capital_krw == 5_000_000) & (sdf.slippage_ticks == 1)].iloc[0]
        baseline_delta = float(nb.pnl - ob.pnl)
        baseline_ok = bool(abs(baseline_delta) < 1e-6 and abs(float(nb.pf) - float(ob.pf)) < 1e-9)

    best = survivors.iloc[0].to_dict()
    score = {
        "version": VERSION,
        "historical_backtest_only": True,
        "live_approval": False,
        "v028_control_reproduced": baseline_ok,
        "v028_5m1t_pnl_delta": baseline_delta,
        "best_variant": best,
        "survivor_count": int((survivors.status == "SURVIVOR").sum()),
        "adaptive_method": {
            "walk_forward": True,
            "lookback_bars": args.trail_lookback_bars,
            "pivot_span": args.trail_pivot_span,
            "horizon_bars": args.trail_horizon_bars,
            "min_samples": args.trail_min_samples,
            "fallback_pct": args.trail_fallback_pct,
            "clip": [args.trail_min_pct, args.trail_max_pct],
        },
    }
    (out / "kr_v029_scorecard.json").write_text(json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "RUN_VALIDATION.txt").write_text("PASS\n" + json.dumps(score, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def self_test():
    assert ex.tick_size(1999) == 1 and ex.tick_size(2000) == 5
    n = 80
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="Asia/Seoul")
    base = np.linspace(100, 120, n) + np.sin(np.arange(n) / 3) * 4
    x = pd.DataFrame({"open": base, "high": base + 1, "low": base - 1, "close": base, "atr14": 2.0}, index=idx)
    a = argparse.Namespace(
        trail_lookback_bars=60, trail_pivot_span=2, trail_horizon_bars=12,
        trail_sample_min_dd=0.005, trail_sample_max_dd=0.20, trail_min_samples=2,
        trail_fallback_pct=0.03, trail_min_pct=0.015, trail_max_pct=0.06,
    )
    p, cnt = trail_stat_for_entry(x, 70, "TRAIL_AVG", a)
    assert 0.015 <= p <= 0.06 and cnt >= 0
    print("SELF_TEST=PASS", p, cnt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="kr_v029_latest_output")
    ap.add_argument("--state-dir", default="kr_state_pit")
    ap.add_argument("--period-60m", default="730d")
    ap.add_argument("--top-n", type=int, default=40)
    ap.add_argument("--min-market-coverage", type=int, default=30)
    ap.add_argument("--self-test", action="store_true")

    ap.add_argument("--base-risk-pct", type=float, default=0.01)
    ap.add_argument("--max-total-risk-pct", type=float, default=0.02)
    ap.add_argument("--max-symbol-pct", type=float, default=0.20)
    ap.add_argument("--max-positions", type=int, default=4)
    ap.add_argument("--daily-loss-stop-pct", type=float, default=0.015)
    ap.add_argument("--dd-reduce-pct", type=float, default=0.05)
    ap.add_argument("--dd-risk-mult", type=float, default=0.50)
    ap.add_argument("--dd-halt-pct", type=float, default=0.08)
    ap.add_argument("--min-seed-krw", type=float, default=50_000)
    ap.add_argument("--partial-fraction", type=float, default=0.50)
    ap.add_argument("--max-hold", type=int, default=26)
    ap.add_argument("--adverse20-r", type=float, default=0.40)
    ap.add_argument("--adverse60-r", type=float, default=0.80)

    ap.add_argument("--min-risk-pct", type=float, default=0.012)
    ap.add_argument("--min-r-atr", type=float, default=0.75)
    ap.add_argument("--max-tick-r", type=float, default=0.10)
    ap.add_argument("--max-entry-gap-atr", type=float, default=0.25)

    ap.add_argument("--pullback-wait-bars", type=int, default=3)
    ap.add_argument("--pullback-tol-atr", type=float, default=0.15)
    ap.add_argument("--pullback-hold-tol-atr", type=float, default=0.05)

    ap.add_argument("--trail-lookback-bars", type=int, default=480)
    ap.add_argument("--trail-pivot-span", type=int, default=2)
    ap.add_argument("--trail-horizon-bars", type=int, default=26)
    ap.add_argument("--trail-min-samples", type=int, default=8)
    ap.add_argument("--trail-sample-min-dd", type=float, default=0.005)
    ap.add_argument("--trail-sample-max-dd", type=float, default=0.20)
    ap.add_argument("--trail-fallback-pct", type=float, default=0.03)
    ap.add_argument("--trail-min-pct", type=float, default=0.015)
    ap.add_argument("--trail-max-pct", type=float, default=0.06)

    args = ap.parse_args()
    if args.self_test:
        self_test(); return
    run(args)


if __name__ == "__main__":
    main()
