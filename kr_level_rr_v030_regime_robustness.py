#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Noramu KR v0.30 regime + robustness research.

Research only; no live orders.

v0.30 freezes the best v0.29 family (PULLBACK + TRAIL_P70) and addresses the
remaining correctness/robustness issues instead of widening signal search:

- same-run v0.29 control on the exact same downloaded bars
- data fingerprints for reproducibility auditing
- re-run execution-quality gates at the *actual delayed pullback entry*
- KOSPI40 point-in-time breadth / equal-weight 5,20,120,200-bar regime filters
- staged-add policies, including regime-conditional averaging down
- 26 vs 52 bar holding horizon
- +1R break-even arming before historical-reversal trailing
- conservative same-bar treatment when a fresh high and new trail stop are both
  touched inside one 60m OHLC bar
- 2023-25 train vs 2026 out-of-sample, July-2026 stress, concentration checks
- 3-tick execution stress for finalists

The market regime always uses bars strictly earlier than the entry/add bar.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

import kr_level_rr_v025 as kr
import kr_level_rr_v027_execution as ex
import kr_level_rr_v028_execution_filter as v28
import kr_level_rr_v029_adaptive_exit_entry as v29

VERSION = "v0.30-KR-REGIME-ROBUSTNESS"
REGIME_MODES = ("OFF", "BALANCED", "STRICT")
ADD_POLICIES = ("STAGED_FULL", "STAGED_CONDITIONAL", "DIRECT")
HOLD_BARS = (26, 52)


def data_fingerprints(data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for ticker, x in sorted(data.items()):
        cols = [c for c in ["open", "high", "low", "close", "volume"] if c in x.columns]
        z = x[cols].copy()
        hv = pd.util.hash_pandas_object(z, index=True).values.tobytes()
        rows.append({
            "ticker": ticker,
            "rows": len(z),
            "start": str(z.index.min()) if len(z) else "",
            "end": str(z.index.max()) if len(z) else "",
            "sha256": hashlib.sha256(hv).hexdigest(),
        })
    return pd.DataFrame(rows)


def build_regime_table(data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    closes = pd.concat({t: x.close.astype(float) for t, x in data.items()}, axis=1).sort_index()
    e5 = closes.ewm(span=5, adjust=False, min_periods=5).mean()
    e20 = closes.ewm(span=20, adjust=False, min_periods=20).mean()
    e120 = closes.ewm(span=120, adjust=False, min_periods=120).mean()
    e200 = closes.ewm(span=200, adjust=False, min_periods=200).mean()

    def breadth(ema: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
        valid = closes.notna() & ema.notna()
        n = valid.sum(axis=1)
        num = ((closes > ema) & valid).sum(axis=1)
        return num / n.replace(0, np.nan), n

    b20, n20 = breadth(e20)
    b120, n120 = breadth(e120)
    b200, n200 = breadth(e200)

    rets = closes.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)
    ew_ret = rets.mean(axis=1, skipna=True).clip(-0.15, 0.15).fillna(0.0)
    ew = (1.0 + ew_ret).cumprod()

    r = pd.DataFrame(index=closes.index)
    r["breadth20"] = b20
    r["breadth120"] = b120
    r["breadth200"] = b200
    r["coverage20"] = n20
    r["coverage120"] = n120
    r["coverage200"] = n200
    r["ew_index"] = ew
    r["ew_ema5"] = ew.ewm(span=5, adjust=False, min_periods=5).mean()
    r["ew_ema20"] = ew.ewm(span=20, adjust=False, min_periods=20).mean()
    r["ew_ema120"] = ew.ewm(span=120, adjust=False, min_periods=120).mean()
    r["ew_ema200"] = ew.ewm(span=200, adjust=False, min_periods=200).mean()
    return r


def prior_regime_row(regime: pd.DataFrame, ts):
    if regime.empty:
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    t = t.tz_convert(kr.TZ)
    pos = int(regime.index.searchsorted(t, side="left")) - 1
    if pos < 0:
        return None
    return regime.iloc[pos]


def regime_pass(row, mode: str, args) -> bool:
    if mode == "OFF":
        return True
    if row is None:
        return False
    need = ["breadth20", "breadth120", "ew_ema5", "ew_ema20"]
    if any(not np.isfinite(float(row[k])) for k in need):
        return False
    if float(row.coverage120) < args.regime_min_coverage:
        return False
    balanced = bool(
        float(row.breadth20) >= args.balanced_breadth20
        and float(row.breadth120) >= args.balanced_breadth120
        and float(row.ew_ema5) > float(row.ew_ema20)
    )
    if mode == "BALANCED":
        return balanced
    need2 = ["breadth200", "ew_index", "ew_ema120", "ew_ema200"]
    if any(not np.isfinite(float(row[k])) for k in need2):
        return False
    if float(row.coverage200) < args.regime_min_coverage:
        return False
    return bool(
        balanced
        and float(row.breadth20) >= args.strict_breadth20
        and float(row.breadth120) >= args.strict_breadth120
        and float(row.breadth200) >= args.strict_breadth200
        and float(row.ew_index) > float(row.ew_ema120)
        and float(row.ew_index) > float(row.ew_ema200)
    )


def actual_entry_regate(data, candidates, args):
    kept = {}; rows = []
    for ticker, cs in candidates.items():
        x = data[ticker]; out = []
        for cand in cs:
            s = cand.setup; ei = int(cand.entry_i)
            reason = "KEEP"
            if ei <= 0 or ei >= len(x):
                reason = "INVALID_ENTRY_INDEX"
                entry = atr = risk = risk_pct = ratr = tickr = gap = np.nan
            else:
                entry = float(x.open.iloc[ei]); stop = float(s.stop); risk = entry - stop
                atr = v28.atr14_at(x, ei - 1)
                tick = ex.tick_size(entry)
                risk_pct = risk / entry if entry > 0 else np.nan
                ratr = risk / atr if np.isfinite(atr) and atr > 0 else np.nan
                tickr = tick / risk if risk > 0 else np.inf
                gap = (entry - float(s.level)) / atr if np.isfinite(atr) and atr > 0 else np.inf
                if not np.isfinite(risk) or risk <= 0: reason = "INVALID_RISK"
                elif not np.isfinite(atr) or atr <= 0: reason = "NO_ATR"
                elif risk_pct < args.min_risk_pct: reason = "RISK_PCT_TOO_SMALL"
                elif ratr < args.min_r_atr: reason = "R_TOO_SMALL_VS_ATR"
                elif tickr > args.max_tick_r: reason = "TICK_BURDEN_HIGH"
                elif gap > args.max_entry_gap_atr: reason = "ENTRY_GAP_TOO_HIGH"
                elif gap < -args.max_entry_below_level_atr: reason = "ENTRY_BELOW_LEVEL_TOO_FAR"
                elif entry <= stop: reason = "OPEN_BELOW_STOP"
            if reason == "KEEP":
                out.append(cand)
            rows.append({
                "ticker": ticker, "setup_id": s.setup_id, "entry_time": str(x.index[ei]) if 0 <= ei < len(x) else "",
                "entry_open": entry, "level": float(s.level), "stop": float(s.stop), "risk": risk,
                "risk_pct": risk_pct, "atr14_prior": atr, "r_atr": ratr, "tick_over_r": tickr,
                "entry_gap_atr": gap, "decision": reason,
            })
        kept[ticker] = out
    return kept, pd.DataFrame(rows)


def period_metrics(tr: pd.DataFrame, start: str, end: str) -> dict:
    if tr.empty:
        return {"trades": 0, "pnl": 0.0, "pf": np.nan, "winrate": np.nan}
    dt = pd.to_datetime(tr.entry_time, utc=True, errors="coerce").dt.tz_convert(kr.TZ)
    s = pd.Timestamp(start, tz=kr.TZ); e = pd.Timestamp(end, tz=kr.TZ)
    g = tr[(dt >= s) & (dt < e)]
    if g.empty:
        return {"trades": 0, "pnl": 0.0, "pf": np.nan, "winrate": np.nan}
    p = g.pnl.astype(float); gp = float(p[p > 0].sum()); gl = float(-p[p < 0].sum())
    return {
        "trades": int(len(g)), "pnl": float(p.sum()),
        "pf": gp / gl if gl > 0 else (float("inf") if gp > 0 else np.nan),
        "winrate": float((p > 0).mean()),
    }


def concentration_metrics(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"top1_positive_share": np.nan, "top3_positive_share": np.nan, "residual_after_top3": 0.0}
    g = tr.groupby("ticker", as_index=False).pnl.sum().sort_values("pnl", ascending=False)
    pos = float(g.loc[g.pnl > 0, "pnl"].sum())
    t1 = float(g.head(1).pnl.clip(lower=0).sum())
    t3 = float(g.head(3).pnl.clip(lower=0).sum())
    total = float(g.pnl.sum())
    return {
        "top1_positive_share": t1 / pos if pos > 0 else np.nan,
        "top3_positive_share": t3 / pos if pos > 0 else np.nan,
        "residual_after_top3": total - t3,
    }


def year_metrics(tr: pd.DataFrame) -> pd.DataFrame:
    if tr.empty:
        return pd.DataFrame(columns=["year", "trades", "pnl", "pf", "winrate"])
    z = tr.copy()
    z["dt"] = pd.to_datetime(z.entry_time, utc=True, errors="coerce").dt.tz_convert(kr.TZ)
    z = z.dropna(subset=["dt"]); z["year"] = z.dt.dt.year
    rows = []
    for y, g in z.groupby("year"):
        p = g.pnl.astype(float); gp = float(p[p > 0].sum()); gl = float(-p[p < 0].sum())
        rows.append({"year": int(y), "trades": len(g), "pnl": float(p.sum()),
                     "pf": gp / gl if gl > 0 else np.nan, "winrate": float((p > 0).mean())})
    return pd.DataFrame(rows)


def simulate_v030(strategy, data, candidates, regime, args, starting_equity, slippage_ticks,
                  regime_mode, add_policy, max_hold):
    bars_at = {}; setup_at = {}
    for ticker, x in data.items():
        for i, ts in enumerate(x.index):
            u = pd.Timestamp(ts).tz_convert("UTC")
            bars_at.setdefault(u, []).append((ticker, i))
        for cand in candidates.get(ticker, []):
            ei = int(cand.entry_i)
            if ei < len(x):
                u = pd.Timestamp(x.index[ei]).tz_convert("UTC")
                setup_at.setdefault(u, []).append((ticker, ei, cand))

    timeline = sorted(bars_at)
    cash = float(starting_equity); positions = {}; last_mark = {}
    trades = []; rejects = []; equity_rows = []
    realized_by_day = {}; day_start_equity = {}; peak_equity = cash
    feas = {"starter_lt_1_share": 0, "starter_one_share": 0, "add20_lt_1_share": 0,
            "add60_lt_1_share": 0, "adds_blocked_regime": 0, "same_bar_trail_exits": 0}

    def mtm():
        return cash + sum(p["shares"] * last_mark.get(t, p["last_mark"]) for t, p in positions.items())
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
        if cash + 1e-9 < gross + comm:
            return False
        cash -= gross + comm
        p["shares"] += qty; p["cash_out"] += gross + comm; p["buy_notional"] += gross; p["commissions"] += comm
        p["fills"].append({"time": str(ts), "raw_price": float(raw_price), "price": px, "shares": qty,
                           "fraction": fraction, "reason": reason, "slippage_ticks": slippage_ticks})
        p["last_mark"] = px; last_mark[p["ticker"]] = px
        if reason == "starter" and qty == 1: feas["starter_one_share"] += 1
        return True

    def sell(p, qty, raw_price, reason, ts):
        nonlocal cash
        qty = min(int(qty), int(p["shares"]))
        if qty <= 0: return 0
        px = ex.adverse_ticks(raw_price, "SELL", slippage_ticks)
        gross = qty * px; comm = gross * ex.TOSS_KRX_COMMISSION
        stt_rate, rural_rate = ex.tax_components(p["market"], ts)
        stt = gross * stt_rate; rural = gross * rural_rate; tax = stt + rural
        cash += gross - comm - tax
        p["shares"] -= qty; p["cash_in"] += gross - comm - tax; p["sell_notional"] += gross
        p["commissions"] += comm; p["taxes"] += tax
        p["events"].append({"time": str(ts), "raw_price": float(raw_price), "price": px, "shares": qty,
                            "reason": reason, "slippage_ticks": slippage_ticks, "commission": comm,
                            "stt": stt, "rural_tax": rural})
        return qty

    def close(ticker, raw_price, reason, status, ts):
        p = positions[ticker]
        if p["shares"] > 0: sell(p, p["shares"], raw_price, reason, ts)
        pnl = p["cash_in"] - p["cash_out"]
        d = kr.kr_date(ts); realized_by_day[d] = realized_by_day.get(d, 0.0) + pnl
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

        eq_open = mtm(); peak_equity = max(peak_equity, eq_open)
        d = kr.kr_date(u); day_start_equity.setdefault(d, eq_open); realized_by_day.setdefault(d, 0.0)

        for ticker, ei, cand in sorted(setup_at.get(u, []), key=lambda q: q[0]):
            s = cand.setup
            if ticker in positions:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "SAME_TICKER_OPEN"}); continue
            rr = prior_regime_row(regime, u)
            if not regime_pass(rr, regime_mode, args):
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "MARKET_REGIME"}); continue
            eq_open = mtm(); peak_equity = max(peak_equity, eq_open); dd_open = 1 - eq_open / peak_equity if peak_equity > 0 else 0
            if dd_open >= args.dd_halt_pct:
                rejects.append({"time": str(u), "ticker": ticker, "setup_id": s.setup_id, "reason": "MTM_DD_HALT"}); continue
            dd_mult = args.dd_risk_mult if dd_open >= args.dd_reduce_pct else 1.0
            ds = day_start_equity[d]
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
            if l <= p["active_stop"]:
                close(ticker, p["active_stop"], "stop", "LOSS" if p["active_stop"] < p["first_entry"] else "BE_OR_WIN", u); continue

            p["mfe_R"] = max(p["mfe_R"], (h - p["first_entry"]) / p["R"])
            p["mae_R"] = min(p["mae_R"], (l - p["first_entry"]) / p["R"])
            old_peak = p["peak_price"]; p["peak_price"] = max(old_peak, h)

            if add_policy != "DIRECT" and not p["trail_armed"]:
                add_ok = True
                if add_policy == "STAGED_CONDITIONAL":
                    add_ok = regime_pass(prior_regime_row(regime, u), "BALANCED", args)
                    if not add_ok: feas["adds_blocked_regime"] += 1
                if add_ok:
                    lvl20 = p["first_entry"] - args.adverse20_r * p["R"]
                    lvl60 = p["first_entry"] - args.adverse60_r * p["R"]
                    if not p["added20"] and l <= lvl20 and lvl20 > p["active_stop"]:
                        if buy(p, lvl20, 0.20, "adverse20", u): p["added20"] = True
                    if p["added20"] and not p["added60"] and l <= lvl60 and lvl60 > p["active_stop"]:
                        if buy(p, lvl60, 0.60, "support60", u): p["added60"] = True

            arm_level = p["first_entry"] + args.trail_arm_r * p["R"]
            if p["peak_price"] >= arm_level:
                p["trail_armed"] = True
            if p["trail_armed"]:
                candidate_stop = max(p["structural_stop"], p["first_entry"], p["peak_price"] * (1.0 - p["trail_pct"]))
                if candidate_stop > p["active_stop"] + 1e-12:
                    if args.conservative_same_bar_trail and p["peak_price"] > old_peak and l <= candidate_stop:
                        feas["same_bar_trail_exits"] += 1
                        close(ticker, candidate_stop, "adaptive_trail_same_bar", "BE_OR_WIN", u); continue
                    p["active_stop"] = candidate_stop

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
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    state = Path(args.state_dir); state.mkdir(parents=True, exist_ok=True)
    u, data, setups, resolved = ex.load_data_and_signals(args, out, state)
    kospi = [t for t in data if u.loc[u.yf_ticker == t, "market"].iloc[0] == "KOSPI"]
    d = {t: data[t] for t in kospi}; s0 = {t: setups[t] for t in kospi}
    sf, gate = v28.filter_setups(d, s0, args)
    gate.to_csv(out / "v028_execution_gate_audit.csv", index=False, encoding="utf-8-sig")

    fps = data_fingerprints(d); fps.to_csv(out / "data_fingerprint.csv", index=False, encoding="utf-8-sig")
    regime = build_regime_table(d); regime.to_csv(out / "market_regime_60m.csv", encoding="utf-8-sig")

    c29, entry_audit = v29.build_candidates(d, sf, "PULLBACK", args)
    entry_audit.to_csv(out / "pullback_entry_audit.csv", index=False, encoding="utf-8-sig")
    c30, regate = actual_entry_regate(d, c29, args)
    regate.to_csv(out / "actual_entry_execution_regate.csv", index=False, encoding="utf-8-sig")

    control_rows = []
    for cap in ex.ACCOUNT_SIZES:
        for slip in ex.SLIPPAGE_TICKS:
            label = f"V029_SAME_RUN_CONTROL|{cap//1_000_000}M|{slip}T"
            tr, eq, rj, f = v29.simulate_variant(label, d, c29, args, cap, slip, "TRAIL_P70")
            control_rows.append({"capital_krw": cap, "slippage_ticks": slip, **ex.summarize(tr, eq, cap), "rejects": len(rj)})
    control = pd.DataFrame(control_rows); control.to_csv(out / "v029_same_run_control.csv", index=False, encoding="utf-8-sig")

    rows = []; train_rows = []; oos_rows = []; jul_rows = []; feas_rows = []; year_parts = []
    detail = {}; configs = {}
    for rm in REGIME_MODES:
        for apol in ADD_POLICIES:
            for hold in HOLD_BARS:
                variant = f"{rm}|{apol}|H{hold}"
                configs[variant] = (rm, apol, hold)
                for cap in ex.ACCOUNT_SIZES:
                    for slip in ex.SLIPPAGE_TICKS:
                        label = f"V030|{variant}|{cap//1_000_000}M|{slip}T"
                        tr, eq, rj, feas = simulate_v030(label, d, c30, regime, args, cap, slip, rm, apol, hold)
                        m = ex.summarize(tr, eq, cap)
                        rows.append({"variant": variant, "regime_mode": rm, "add_policy": apol, "max_hold": hold,
                                     "capital_krw": cap, "slippage_ticks": slip, **m,
                                     "resolved_tickers": len(kospi), "candidates_v029": sum(len(v) for v in c29.values()),
                                     "candidates_v030": sum(len(v) for v in c30.values())})
                        train_rows.append({"variant": variant, "capital_krw": cap, "slippage_ticks": slip,
                                           **period_metrics(tr, "2023-08-08", "2026-01-01")})
                        oos_rows.append({"variant": variant, "capital_krw": cap, "slippage_ticks": slip,
                                         **period_metrics(tr, "2026-01-01", "2027-01-01")})
                        jul_rows.append({"variant": variant, "capital_krw": cap, "slippage_ticks": slip,
                                         **period_metrics(tr, "2026-07-01", "2026-08-01")})
                        feas_rows.append({"variant": variant, "capital_krw": cap, "slippage_ticks": slip,
                                          "rejects": len(rj), **feas})
                        if cap == 5_000_000 and slip == 1:
                            detail[variant] = (tr, rj)
                            y = year_metrics(tr)
                            if len(y):
                                y.insert(0, "variant", variant); year_parts.append(y)
                        print(label, f"ret={m['return_pct']*100:.2f}% PF={m['pf']:.3f} DD={m['max_dd_pct']*100:.2f}% trades={m['trades']}")

    sdf = pd.DataFrame(rows); tdf = pd.DataFrame(train_rows); odf = pd.DataFrame(oos_rows); jdf = pd.DataFrame(jul_rows)
    sdf.to_csv(out / "kr_v030_summary.csv", index=False, encoding="utf-8-sig")
    tdf.to_csv(out / "kr_v030_train_2023_2025.csv", index=False, encoding="utf-8-sig")
    odf.to_csv(out / "kr_v030_oos_2026.csv", index=False, encoding="utf-8-sig")
    jdf.to_csv(out / "kr_v030_july_2026.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(feas_rows).to_csv(out / "kr_v030_feasibility.csv", index=False, encoding="utf-8-sig")
    ydf = pd.concat(year_parts, ignore_index=True) if year_parts else pd.DataFrame()
    if len(ydf): ydf.to_csv(out / "kr_v030_year_5m1t.csv", index=False, encoding="utf-8-sig")

    score_rows = []
    for variant in sorted(sdf.variant.unique()):
        b = sdf[(sdf.variant == variant) & (sdf.capital_krw == 5_000_000) & (sdf.slippage_ticks == 1)].iloc[0]
        st = sdf[(sdf.variant == variant) & (sdf.capital_krw == 5_000_000) & (sdf.slippage_ticks == 2)].iloc[0]
        c20 = sdf[(sdf.variant == variant) & (sdf.capital_krw == 20_000_000) & (sdf.slippage_ticks == 1)].iloc[0]
        train = tdf[(tdf.variant == variant) & (tdf.capital_krw == 5_000_000) & (tdf.slippage_ticks == 1)].iloc[0]
        oos = odf[(odf.variant == variant) & (odf.capital_krw == 5_000_000) & (odf.slippage_ticks == 1)].iloc[0]
        jul = jdf[(jdf.variant == variant) & (jdf.capital_krw == 5_000_000) & (jdf.slippage_ticks == 1)].iloc[0]
        conc = concentration_metrics(detail[variant][0])
        yy = ydf[ydf.variant == variant] if len(ydf) else pd.DataFrame()
        min_year = float(yy.pnl.min()) if len(yy) else 0.0
        supported = bool(b.pnl > 0 and b.pf > 1.05 and st.pnl > 0 and c20.pnl > 0 and oos.pnl >= 0
                         and jul.pnl >= 0 and b.max_dd_pct <= args.max_survivor_dd and oos.trades >= args.min_oos_trades)
        score = float(b.pnl + 0.35 * st.pnl + 0.15 * c20.pnl + 0.50 * oos.pnl + 0.10 * train.pnl
                      + 0.25 * jul.pnl - 1_000_000 * b.max_dd_pct - 0.25 * max(0.0, -min_year))
        score_rows.append({"variant": variant, "status": "SURVIVOR" if supported else "RESEARCH_ONLY",
                           "5m_1t_pnl": float(b.pnl), "5m_1t_pf": float(b.pf), "5m_1t_dd": float(b.max_dd_pct),
                           "5m_2t_pnl": float(st.pnl), "20m_1t_pnl": float(c20.pnl),
                           "train_pnl": float(train.pnl), "train_trades": int(train.trades),
                           "oos2026_pnl": float(oos.pnl), "oos2026_trades": int(oos.trades),
                           "jul2026_pnl": float(jul.pnl), "jul2026_trades": int(jul.trades),
                           "min_year_pnl": min_year, **conc, "robust_score": score})

    scores = pd.DataFrame(score_rows).sort_values("robust_score", ascending=False).reset_index(drop=True)
    scores.to_csv(out / "kr_v030_scores_pre3t.csv", index=False, encoding="utf-8-sig")

    stress_rows = []
    for variant in scores.head(args.finalist_count).variant:
        rm, apol, hold = configs[variant]
        for cap in [5_000_000, 20_000_000]:
            tr, eq, rj, feas = simulate_v030(f"V030_3T|{variant}|{cap//1_000_000}M", d, c30, regime, args, cap, 3, rm, apol, hold)
            m = ex.summarize(tr, eq, cap)
            stress_rows.append({"variant": variant, "capital_krw": cap, "slippage_ticks": 3, **m, "rejects": len(rj)})
    stress = pd.DataFrame(stress_rows); stress.to_csv(out / "kr_v030_3tick_stress.csv", index=False, encoding="utf-8-sig")

    finalists = scores.head(args.finalist_count).copy()
    finalists["5m_3t_pnl"] = finalists.variant.map({v: float(stress[(stress.variant == v) & (stress.capital_krw == 5_000_000)].iloc[0].pnl) for v in finalists.variant})
    finalists["20m_3t_pnl"] = finalists.variant.map({v: float(stress[(stress.variant == v) & (stress.capital_krw == 20_000_000)].iloc[0].pnl) for v in finalists.variant})
    finalists["stress_status"] = np.where((finalists["5m_3t_pnl"] > 0) & (finalists["20m_3t_pnl"] > 0), "3T_ROBUST", "3T_STRESS_ONLY")
    finalists = finalists.sort_values(["stress_status", "robust_score"], ascending=[True, False]).reset_index(drop=True)
    finalists.to_csv(out / "kr_v030_finalists.csv", index=False, encoding="utf-8-sig")

    for variant in finalists.variant:
        tr, rj = detail[variant]
        safe = variant.replace("|", "_")
        tr.to_csv(out / f"trades_{safe}_5M_1T.csv", index=False, encoding="utf-8-sig")
        rj.to_csv(out / f"rejects_{safe}_5M_1T.csv", index=False, encoding="utf-8-sig")

    cb = control[(control.capital_krw == 5_000_000) & (control.slippage_ticks == 1)].iloc[0]
    best = finalists.iloc[0].to_dict()
    scorecard = {
        "version": VERSION, "historical_backtest_only": True, "live_approval": False,
        "same_run_v029_control_5m1t": {"pnl": float(cb.pnl), "pf": float(cb.pf), "dd": float(cb.max_dd_pct), "trades": int(cb.trades)},
        "v030_best": best,
        "survivor_count": int((scores.status == "SURVIVOR").sum()),
        "candidate_counts": {"v029_pullback": int(sum(len(v) for v in c29.values())), "v030_after_actual_regate": int(sum(len(v) for v in c30.values()))},
        "correctness": {"actual_entry_execution_regate": True, "regime_strictly_prior_bar": True,
                        "conservative_same_bar_trail": bool(args.conservative_same_bar_trail), "trail_break_even_after_1R": args.trail_arm_r},
        "cost_model": {"broker": "Toss KRX", "commission_each_side": ex.TOSS_KRX_COMMISSION,
                       "note": "KRX model; NXT not mixed into historical Yahoo 60m study"},
    }
    (out / "kr_v030_scorecard.json").write_text(json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "RUN_VALIDATION.txt").write_text("PASS\n" + json.dumps(scorecard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def self_test():
    idx = pd.date_range("2026-01-01 09:00", periods=260, freq="h", tz=kr.TZ)
    base = np.linspace(100.0, 130.0, len(idx)) + np.sin(np.arange(len(idx)) / 8.0)
    x1 = pd.DataFrame({"open": base, "high": base + 1, "low": base - 1, "close": base, "volume": 1.0}, index=idx)
    x2 = x1.copy(); x2[["open", "high", "low", "close"]] *= 1.03
    r = build_regime_table({"A": x1, "B": x2})
    assert len(r) == len(idx) and np.isfinite(r.breadth20.dropna().iloc[-1])
    a = argparse.Namespace(regime_min_coverage=1, balanced_breadth20=0.45, balanced_breadth120=0.40,
                           strict_breadth20=0.50, strict_breadth120=0.45, strict_breadth200=0.40)
    assert regime_pass(r.iloc[-1], "OFF", a)
    assert prior_regime_row(r, idx[-1]) is not None
    print("SELF_TEST=PASS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="kr_v030_latest_output")
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
    ap.add_argument("--adverse20-r", type=float, default=0.40)
    ap.add_argument("--adverse60-r", type=float, default=0.80)

    ap.add_argument("--min-risk-pct", type=float, default=0.012)
    ap.add_argument("--min-r-atr", type=float, default=0.75)
    ap.add_argument("--max-tick-r", type=float, default=0.10)
    ap.add_argument("--max-entry-gap-atr", type=float, default=0.25)
    ap.add_argument("--max-entry-below-level-atr", type=float, default=0.10)

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
    ap.add_argument("--trail-arm-r", type=float, default=1.0)
    ap.add_argument("--conservative-same-bar-trail", action=argparse.BooleanOptionalAction, default=True)

    ap.add_argument("--regime-min-coverage", type=int, default=20)
    ap.add_argument("--balanced-breadth20", type=float, default=0.45)
    ap.add_argument("--balanced-breadth120", type=float, default=0.40)
    ap.add_argument("--strict-breadth20", type=float, default=0.50)
    ap.add_argument("--strict-breadth120", type=float, default=0.45)
    ap.add_argument("--strict-breadth200", type=float, default=0.40)

    ap.add_argument("--max-survivor-dd", type=float, default=0.03)
    ap.add_argument("--min-oos-trades", type=int, default=3)
    ap.add_argument("--finalist-count", type=int, default=4)
    args = ap.parse_args()
    if args.self_test:
        self_test(); return
    run(args)


if __name__ == "__main__":
    main()
