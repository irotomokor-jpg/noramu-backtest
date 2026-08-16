#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
NY = "America/New_York"
V5_SRC = ROOT / "portfolio_200_exact_full_replay_v005.py"
RSI_VALIDATED = ROOT / "final_live_runtime_1m_replay_v002_recovered" / "validated_window_runtime_trades.csv"
RSI_AUDIT = ROOT / "final_live_runtime_1m_replay_v002_recovered" / "corrected_runtime_parity_audit.json"
RSI_SUMMARY = ROOT / "final_live_runtime_1m_replay_v002_recovered" / "scaled_portfolio_summary_both_windows.csv"
FAST_SRC = ROOT / "fast_rebound_v004_koru_regime" / "trades_with_regime_context.csv"
RULE = ROOT / "fast_rebound_koru_v1_frozen.json"
OUT = ROOT / "fast_rebound_v008_combined_occupancy"

FAST_CONFIG = "K_CLOSE_STRONG__S04_T06_M10"
CAPITALS = [1000.0, 1500.0, 2000.0]
RSI_CAP_RATIO = 0.40
FAST_CAP_RATIO = 0.30
MIN_ORDER_USD = 1.0

MODES = [
    ("STANDARD_PREEMPT_0BPS", "ACCOUNT_PLUS_2BPS_SLIP", 0.0, 0.0002),
    ("STANDARD_PREEMPT_50BPS", "ACCOUNT_PLUS_2BPS_SLIP", 50.0, 0.0002),
    ("STRESS_PREEMPT_0BPS", "ACCOUNT_PLUS_5BPS_SLIP", 0.0, 0.0005),
    ("STRESS_PREEMPT_50BPS", "ACCOUNT_PLUS_5BPS_SLIP", 50.0, 0.0005),
]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"IMPORT_FAIL={path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def parse_ny(x):
    y = pd.to_datetime(x, errors="coerce", utc=True)
    if isinstance(y, pd.Series):
        return y.dt.tz_convert(NY)
    return y.tz_convert(NY)


def mdd_info(starting: float, ends: list[float], dates: list[pd.Timestamp]):
    vals = [float(starting)] + [float(x) for x in ends]
    dts = [pd.Timestamp(dates[0]) - pd.Timedelta(days=1)] + list(dates)
    peak = vals[0]
    peak_date = dts[0]
    worst = 0.0
    wp = peak_date
    wt = peak_date
    for v, d in zip(vals, dts):
        if v > peak:
            peak = v
            peak_date = d
        dd = v / peak - 1.0 if peak > 0 else -1.0
        if dd < worst:
            worst = dd
            wp = peak_date
            wt = d
    return worst, str(pd.Timestamp(wp).date()), str(pd.Timestamp(wt).date())


def validate_inputs():
    req = [V5_SRC, RSI_VALIDATED, RSI_AUDIT, RSI_SUMMARY, FAST_SRC, RULE]
    for p in req:
        if not p.exists():
            raise SystemExit(f"MISSING_INPUT={p}")
    audit = json.loads(RSI_AUDIT.read_text(encoding="utf-8"))
    if not bool(audit.get("pass")):
        raise SystemExit("RSI_VALIDATED_PARITY_NOT_PASS")
    if int(audit.get("validated_window_runtime_count", 0)) != 42:
        raise SystemExit("RSI_VALIDATED_COUNT_NOT_42")
    rule = json.loads(RULE.read_text(encoding="utf-8"))
    if rule.get("version") != "FAST_REBOUND_KORU_V1" or rule.get("regime_guard") != "NONE":
        raise SystemExit("FAST_FROZEN_RULE_MISMATCH")
    x = rule.get("exit", {})
    if float(x.get("stop_pct")) != 0.004 or float(x.get("take_profit_pct")) != 0.006 or int(x.get("max_hold_minutes")) != 10:
        raise SystemExit("FAST_EXIT_RULE_CHANGED")
    return audit, rule


def prepare_rsi(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    z = pd.read_csv(RSI_VALIDATED)
    z["entry_ny"] = parse_ny(z.entry_ny)
    z["exit_ny"] = parse_ny(z.exit_ny)
    z["trade_date"] = pd.to_datetime(z.trade_date, errors="coerce").dt.tz_localize(None).dt.normalize()
    z["entry_px"] = pd.to_numeric(z.entry_px, errors="raise")
    z["net_return"] = pd.to_numeric(z.net_return, errors="raise")
    z = z[(z.trade_date >= start) & (z.trade_date <= end)].copy().sort_values(["entry_ny", "exec_symbol"]).reset_index(drop=True)
    z["rsi_id"] = np.arange(len(z), dtype=int)
    if len(z) != 42:
        raise SystemExit(f"RSI_WINDOW_COUNT_CHANGED={len(z)}")
    return z


def prepare_fast(cost: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    z = pd.read_csv(FAST_SRC)
    z = z[(z.config == FAST_CONFIG) & (z.cost_scenario == cost)].copy()
    z["entry_ny"] = parse_ny(z.entry_ts)
    z["exit_ny"] = parse_ny(z.exit_ts)
    z["trade_date"] = z.entry_ny.dt.tz_localize(None).dt.normalize()
    z["entry_px"] = pd.to_numeric(z.entry_px, errors="raise")
    z["net_return"] = pd.to_numeric(z.net_return, errors="raise")
    z = z[(z.trade_date >= start) & (z.trade_date <= end)].copy().sort_values(["entry_ny", "exit_ny"]).reset_index(drop=True)
    z["fast_id"] = np.arange(len(z), dtype=int)
    return z


def build_frozen(v5, start: pd.Timestamp, end: pd.Timestamp):
    fs = pd.read_csv(v5.FROZEN_STRATEGY)
    fs["trade_date"] = pd.to_datetime(fs.trade_date, utc=True).dt.tz_convert(None).dt.normalize()
    fs = fs.sort_values("trade_date").drop_duplicates("trade_date", keep="last").reset_index(drop=True)
    weights = v5.build_start_weights(fs)
    fs_by_day = {r.trade_date: r for _, r in fs.iterrows()}

    fp = pd.read_csv(v5.FROZEN_PORT)
    fp["trade_date"] = pd.to_datetime(fp.trade_date, utc=True).dt.tz_convert(None).dt.normalize()
    fp = fp[fp.portfolio == v5.PORTFOLIO_NAME].copy().sort_values("trade_date").reset_index(drop=True)
    fp["frozen_daily_return"] = pd.to_numeric(fp.portfolio_wealth, errors="raise").pct_change()
    cal_start = max(start, fp.trade_date.iloc[1], min(weights.keys()))
    cal_end = min(end, fp.trade_date.max())
    cal = fp[(fp.trade_date >= cal_start) & (fp.trade_date <= cal_end)][["trade_date", "frozen_daily_return"]].copy().reset_index(drop=True)
    if cal.empty or cal.frozen_daily_return.isna().any():
        raise SystemExit("BAD_FROZEN_CALENDAR")
    intervals = v5.select_strict_intervals(pd.read_csv(v5.STRICT_TRADES))
    return fs_by_day, weights, cal, intervals


def entry_exit_return(entry_px: float, exit_px: float, cf: float, slip_side: float, extra_exit_bps: float) -> float:
    buy = float(entry_px) * (1.0 + float(slip_side))
    sell = float(exit_px) * (1.0 - float(slip_side)) * (1.0 - float(extra_exit_bps) / 10000.0)
    return (sell * (1.0 - cf)) / (buy * (1.0 + cf)) - 1.0


def run_scenario(v5, cal, fs_by_day, weights, intervals, rsi: pd.DataFrame, fast: pd.DataFrame,
                 capital: float, include_fast: bool, fast_cost: str, fast_slip: float,
                 preempt_extra_bps: float, cf: float):
    equity = float(capital)
    rsi_cap = capital * RSI_CAP_RATIO
    fast_cap = capital * FAST_CAP_RATIO
    raw_cache = {}
    rsi_by_day = {d: g.copy() for d, g in rsi.groupby("trade_date")}
    fast_by_day = {d: g.copy() for d, g in fast.groupby("trade_date")}

    daily_rows = []
    fill_rows = []
    preempt_rows = []
    rsi_accepted = rsi_rejected = 0
    fast_accepted = fast_rejected = 0
    fast_candidates = int(len(fast)) if include_fast else 0
    preempt_events = 0
    preempt_rsi_usd = 0.0
    preempt_fast_usd = 0.0
    rsi_pnl_total = 0.0
    fast_pnl_total = 0.0
    frozen_pnl_total = 0.0
    max_gross = 0.0
    max_rsi_occ = 0.0
    max_fast_occ = 0.0
    same_symbol_rsi_fast = 0
    exact_entry_ties = 0

    for _, crow in cal.iterrows():
        d = crow.trade_date
        equity_start = equity
        deployable = min(capital, equity_start)
        reserve = max(0.0, equity_start - deployable)
        if deployable <= 0:
            raise SystemExit(f"NONPOSITIVE_DEPLOYABLE date={d}")
        w = weights.get(d)
        fsrow = fs_by_day.get(d)
        if w is None or fsrow is None:
            raise SystemExit(f"FROZEN_INPUT_MISSING date={d}")
        budgets = {s: deployable * w[s] for s in v5.SYMS}
        open_ts = pd.Timestamp(f"{d.date()} 09:30:00", tz=NY)
        frozen_active = {
            "TQQQ": bool(int(fsrow.TQQQ_position)),
            "UPRO": bool(int(fsrow.UPRO_position)),
            "SOXL": v5.strict_active(intervals, "SOXL", open_ts),
            "KORU": v5.strict_active(intervals, "KORU", open_ts),
        }

        events = v5.strict_day_events(intervals, d)
        rg = rsi_by_day.get(d, pd.DataFrame())
        for _, tr in rg.iterrows():
            events.append({"ts": tr.entry_ny, "kind": "RSI_ENTRY", "symbol": tr.exec_symbol, "id": int(tr.rsi_id)})
            events.append({"ts": tr.exit_ny, "kind": "RSI_EXIT", "symbol": tr.exec_symbol, "id": int(tr.rsi_id)})
        fg = fast_by_day.get(d, pd.DataFrame()) if include_fast else pd.DataFrame()
        for _, tr in fg.iterrows():
            events.append({"ts": tr.entry_ny, "kind": "FAST_ENTRY", "symbol": "KORU", "id": int(tr.fast_id)})
            events.append({"ts": tr.exit_ny, "kind": "FAST_EXIT", "symbol": "KORU", "id": int(tr.fast_id)})

        if len(rg) and len(fg):
            exact_entry_ties += len(set(rg.entry_ny) & set(fg.entry_ny))

        rank = {"FROZEN_EXIT": 0, "RSI_EXIT": 1, "FAST_EXIT": 2, "FROZEN_ENTRY": 3, "RSI_ENTRY": 4, "FAST_ENTRY": 5}
        events = sorted(events, key=lambda x: (x["ts"], rank[x["kind"]], x.get("symbol", "")))
        active_rsi = {}
        active_fast = {}
        day_rsi_pnl = 0.0
        day_fast_pnl = 0.0
        day_max_gross = 0.0

        def frozen_occ():
            return sum(budgets[s] for s in v5.SYMS if frozen_active[s])

        def rsi_occ():
            return sum(p["notional"] for p in active_rsi.values())

        def fast_occ():
            return sum(p["notional"] for p in active_fast.values())

        for ev in events:
            ts = pd.Timestamp(ev["ts"])
            kind = ev["kind"]
            sym = ev["symbol"]

            if kind == "FROZEN_EXIT":
                frozen_active[sym] = False

            elif kind == "RSI_EXIT":
                pos = active_rsi.pop(ev["id"], None)
                if pos is not None and pos["notional"] > 0:
                    pnl = pos["notional"] * pos["original_net_return"]
                    day_rsi_pnl += pnl

            elif kind == "FAST_EXIT":
                pos = active_fast.pop(ev["id"], None)
                if pos is not None and pos["notional"] > 0:
                    pnl = pos["notional"] * pos["original_net_return"]
                    day_fast_pnl += pnl

            elif kind == "FROZEN_ENTRY":
                frozen_active[sym] = True
                excess = max(0.0, frozen_occ() + rsi_occ() + fast_occ() - deployable)
                if excess > 1e-9:
                    preempt_events += 1
                    pool = []
                    for rid, p in active_rsi.items():
                        pool.append(("RSI", rid, p))
                    for fid, p in active_fast.items():
                        pool.append(("FAST", fid, p))
                    pool.sort(key=lambda x: (-x[2]["notional"], x[0], x[1]))
                    remain = excess
                    for strategy, pid, pos in pool:
                        if remain <= 1e-9:
                            break
                        release = min(remain, pos["notional"])
                        if release <= 0:
                            continue
                        raw_open = v5.raw_open_at(pos["symbol"], ts, raw_cache)
                        slip = 0.0 if strategy == "RSI" else fast_slip
                        pret = entry_exit_return(pos["entry_px"], raw_open, cf, slip, preempt_extra_bps)
                        pnl = release * pret
                        if strategy == "RSI":
                            day_rsi_pnl += pnl
                            preempt_rsi_usd += release
                        else:
                            day_fast_pnl += pnl
                            preempt_fast_usd += release
                        pos["notional"] -= release
                        remain -= release
                        preempt_rows.append({
                            "capital_usd": capital, "fast_cost": fast_cost, "preempt_extra_bps": preempt_extra_bps,
                            "trade_date": str(d.date()), "ts": ts.isoformat(), "frozen_entry_symbol": sym,
                            "strategy": strategy, "position_id": pid, "symbol": pos["symbol"],
                            "release_usd": release, "raw_open": raw_open, "preempt_return": pret, "preempt_pnl_usd": pnl,
                        })
                    if remain > 1e-7:
                        raise SystemExit(f"PREEMPT_INSUFFICIENT date={d} ts={ts} remaining={remain}")

            elif kind == "RSI_ENTRY":
                available = max(0.0, deployable - frozen_occ() - rsi_occ() - fast_occ())
                notional = min(rsi_cap, available)
                tr = rg[rg.rsi_id == ev["id"]].iloc[0]
                if notional < MIN_ORDER_USD:
                    rsi_rejected += 1
                    status = "REJECT_NO_IDLE_CAPACITY"
                    notional = 0.0
                else:
                    rsi_accepted += 1
                    status = "FILLED"
                    if any(p["symbol"] == str(tr.exec_symbol) and p["notional"] > 0 for p in active_fast.values()):
                        same_symbol_rsi_fast += 1
                    active_rsi[int(tr.rsi_id)] = {
                        "symbol": str(tr.exec_symbol), "notional": float(notional), "entry_px": float(tr.entry_px),
                        "original_net_return": float(tr.net_return),
                    }
                fill_rows.append({"capital_usd": capital, "fast_cost": fast_cost, "preempt_extra_bps": preempt_extra_bps,
                                  "strategy": "RSI", "trade_date": str(d.date()), "id": int(tr.rsi_id),
                                  "symbol": str(tr.exec_symbol), "entry_ts": tr.entry_ny.isoformat(),
                                  "status": status, "notional_usd": notional, "available_usd": available})

            elif kind == "FAST_ENTRY":
                available = max(0.0, deployable - frozen_occ() - rsi_occ() - fast_occ())
                notional = min(fast_cap, available)
                tr = fg[fg.fast_id == ev["id"]].iloc[0]
                if notional < MIN_ORDER_USD:
                    fast_rejected += 1
                    status = "REJECT_NO_IDLE_CAPACITY"
                    notional = 0.0
                else:
                    fast_accepted += 1
                    status = "FILLED"
                    if any(p["symbol"] == "KORU" and p["notional"] > 0 for p in active_rsi.values()):
                        same_symbol_rsi_fast += 1
                    active_fast[int(tr.fast_id)] = {
                        "symbol": "KORU", "notional": float(notional), "entry_px": float(tr.entry_px),
                        "original_net_return": float(tr.net_return),
                    }
                fill_rows.append({"capital_usd": capital, "fast_cost": fast_cost, "preempt_extra_bps": preempt_extra_bps,
                                  "strategy": "FAST", "trade_date": str(d.date()), "id": int(tr.fast_id),
                                  "symbol": "KORU", "entry_ts": tr.entry_ny.isoformat(),
                                  "status": status, "notional_usd": notional, "available_usd": available})

            gross = frozen_occ() + rsi_occ() + fast_occ()
            day_max_gross = max(day_max_gross, gross)
            max_gross = max(max_gross, gross)
            max_rsi_occ = max(max_rsi_occ, rsi_occ())
            max_fast_occ = max(max_fast_occ, fast_occ())
            if gross > deployable + 1e-7:
                raise SystemExit(f"HARD_CAP_FAIL date={d} ts={ts} gross={gross} deployable={deployable}")

        if active_rsi or active_fast:
            raise SystemExit(f"NONFROZEN_OPEN_AT_DAY_END date={d} rsi={list(active_rsi)} fast={list(active_fast)}")

        frozen_pnl = deployable * float(crow.frozen_daily_return)
        equity = equity_start + frozen_pnl + day_rsi_pnl + day_fast_pnl
        frozen_pnl_total += frozen_pnl
        rsi_pnl_total += day_rsi_pnl
        fast_pnl_total += day_fast_pnl
        daily_rows.append({
            "capital_usd": capital, "fast_cost": fast_cost, "preempt_extra_bps": preempt_extra_bps,
            "include_fast": include_fast, "trade_date": d, "equity_start": equity_start,
            "deployable_usd": deployable, "reserve_profit_usd": reserve, "frozen_pnl_usd": frozen_pnl,
            "rsi_pnl_usd": day_rsi_pnl, "fast_pnl_usd": day_fast_pnl,
            "max_gross_deployed_usd": day_max_gross, "equity_end": equity,
        })

    daily = pd.DataFrame(daily_rows)
    dd, peak_date, trough_date = mdd_info(capital, daily.equity_end.tolist(), daily.trade_date.tolist())
    days = max((daily.trade_date.iloc[-1] - daily.trade_date.iloc[0]).days + 1, 1)
    years = days / 365.25
    total_ret = equity / capital - 1.0
    cagr = (equity / capital) ** (1.0 / years) - 1.0 if equity > 0 else np.nan
    eq = pd.Series([capital] + daily.equity_end.astype(float).tolist())
    dr = eq.pct_change().dropna()
    sharpe = float(dr.mean() / dr.std(ddof=0) * np.sqrt(252.0)) if len(dr) and dr.std(ddof=0) > 0 else np.nan
    summary = {
        "capital_usd": capital, "include_fast": include_fast, "fast_cost": fast_cost,
        "preempt_extra_bps": preempt_extra_bps, "start_date": str(daily.trade_date.iloc[0].date()),
        "end_date": str(daily.trade_date.iloc[-1].date()), "sessions": int(len(daily)),
        "ending_usd": float(equity), "net_profit_usd": float(equity - capital),
        "return_pct": float(total_ret * 100.0), "cagr_pct": float(cagr * 100.0),
        "mdd_pct": float(dd * 100.0), "mdd_peak_date": peak_date, "mdd_trough_date": trough_date,
        "sharpe0": sharpe, "frozen_pnl_usd": frozen_pnl_total, "rsi_pnl_usd": rsi_pnl_total,
        "fast_pnl_usd": fast_pnl_total, "rsi_accepted": rsi_accepted, "rsi_rejected": rsi_rejected,
        "fast_candidates": fast_candidates, "fast_accepted": fast_accepted, "fast_rejected": fast_rejected,
        "fast_capture_rate": (fast_accepted / fast_candidates) if fast_candidates else np.nan,
        "preempt_events": preempt_events, "preempt_rsi_release_usd": preempt_rsi_usd,
        "preempt_fast_release_usd": preempt_fast_usd, "max_rsi_occupancy_usd": max_rsi_occ,
        "max_fast_occupancy_usd": max_fast_occ, "max_gross_deployed_usd": max_gross,
        "same_symbol_rsi_fast_events": same_symbol_rsi_fast, "exact_rsi_fast_entry_ties": exact_entry_ties,
    }
    return summary, daily, pd.DataFrame(fill_rows), pd.DataFrame(preempt_rows)


def expected_baseline_map() -> dict:
    z = pd.read_csv(RSI_SUMMARY)
    z = z[z.window == "VERIFIED_42_WINDOW"].copy()
    out = {}
    for _, r in z.iterrows():
        out[(float(r.capital_usd), str(r.scenario))] = float(r.ending_usd)
    return out


def main():
    audit, rule = validate_inputs()
    v5 = load_module("v008_v5", V5_SRC)
    cf = float(v5.commission_fraction())
    start = pd.Timestamp(str(audit["original_long_analysis_start"]))
    end = pd.Timestamp(str(audit["common_raw_end"]))
    rsi = prepare_rsi(start, end)
    fs_by_day, weights, cal, intervals = build_frozen(v5, start, end)
    start = cal.trade_date.iloc[0]
    end = cal.trade_date.iloc[-1]
    rsi = rsi[(rsi.trade_date >= start) & (rsi.trade_date <= end)].copy()
    if len(rsi) != 42:
        raise SystemExit(f"RSI_CALENDAR_COUNT_CHANGED={len(rsi)}")

    OUT.mkdir(parents=True, exist_ok=True)
    expected = expected_baseline_map()
    summaries = []
    dailies = []
    fills_all = []
    preempts_all = []
    parity_rows = []

    print("FAST_REBOUND_V008_COMBINED_OCCUPANCY_REPLAY", flush=True)
    print(f"WINDOW={start.date()}..{end.date()}", flush=True)
    print("PRIORITY=FROZEN_ABSOLUTE; RSI_AND_FAST_PEER_IDLE_CAPACITY_FIRST_COME", flush=True)
    print("PREEMPT=FROZEN_ENTRY_RELEASES_LARGEST_NONFROZEN_POSITION_FIRST", flush=True)
    print("RSI_SINGLE_TRADE_CAP=40pct_INITIAL_CAPITAL", flush=True)
    print("FAST_SINGLE_TRADE_CAP=30pct_INITIAL_CAPITAL", flush=True)
    print("HARD_TOTAL_PRINCIPAL_CAP=100pct_INITIAL_CAPITAL", flush=True)
    print("CAPITALS=1000,1500,2000", flush=True)
    print("ORDER_WRITES=OFF", flush=True)

    for mode, fast_cost, preempt_bps, fast_slip in MODES:
        fast = prepare_fast(fast_cost, start, end)
        print(f"MODE_START={mode} FAST_CANDIDATES={len(fast)}", flush=True)
        for cap in CAPITALS:
            base, db, fb, pb = run_scenario(v5, cal, fs_by_day, weights, intervals, rsi, fast, cap, False, fast_cost, fast_slip, preempt_bps, cf)
            comb, dc, fc, pc = run_scenario(v5, cal, fs_by_day, weights, intervals, rsi, fast, cap, True, fast_cost, fast_slip, preempt_bps, cf)
            base["mode"] = mode; base["scenario"] = "FROZEN_RSI_BASELINE"
            comb["mode"] = mode; comb["scenario"] = "FROZEN_RSI_FAST30"
            summaries += [base, comb]
            for x, label in [(db, "FROZEN_RSI_BASELINE"), (dc, "FROZEN_RSI_FAST30")]:
                x["mode"] = mode; x["scenario"] = label; dailies.append(x)
            if len(fc):
                fc["mode"] = mode; fc["scenario"] = "FROZEN_RSI_FAST30"; fills_all.append(fc)
            if len(pc):
                pc["mode"] = mode; pc["scenario"] = "FROZEN_RSI_FAST30"; preempts_all.append(pc)

            if fast_cost == "ACCOUNT_PLUS_2BPS_SLIP":
                expected_scenario = f"CAP{int(cap)}_RSI40_PREEMPT_{int(preempt_bps)}BPS"
                exp = expected.get((cap, expected_scenario))
                diff = np.nan if exp is None else float(base["ending_usd"] - exp)
                passed = bool(exp is not None and abs(diff) <= 1e-6)
                parity_rows.append({"capital_usd": cap, "mode": mode, "expected_scenario": expected_scenario,
                                    "expected_ending_usd": exp, "replayed_ending_usd": base["ending_usd"],
                                    "difference_usd": diff, "baseline_parity_pass": passed})

    summary = pd.DataFrame(summaries)
    daily = pd.concat(dailies, ignore_index=True)
    fills = pd.concat(fills_all, ignore_index=True) if fills_all else pd.DataFrame()
    preempts = pd.concat(preempts_all, ignore_index=True) if preempts_all else pd.DataFrame()
    parity = pd.DataFrame(parity_rows)

    base = summary[summary.scenario == "FROZEN_RSI_BASELINE"].copy()
    comb = summary[summary.scenario == "FROZEN_RSI_FAST30"].copy()
    cmp = comb.merge(base[["capital_usd", "mode", "ending_usd", "return_pct", "mdd_pct", "sharpe0"]],
                     on=["capital_usd", "mode"], suffixes=("_combined", "_baseline"))
    cmp["fast_incremental_ending_usd"] = cmp.ending_usd_combined - cmp.ending_usd_baseline
    cmp["fast_incremental_return_pct_points"] = cmp.return_pct_combined - cmp.return_pct_baseline
    cmp["mdd_change_pct_points"] = cmp.mdd_pct_combined - cmp.mdd_pct_baseline
    cmp["sharpe_change"] = cmp.sharpe0_combined - cmp.sharpe0_baseline
    cmp["value_add"] = cmp.fast_incremental_ending_usd > 0

    hard_cap_pass = bool((summary.max_gross_deployed_usd <= summary.capital_usd + 1e-7).all())
    baseline_parity_pass = bool(len(parity) == 6 and parity.baseline_parity_pass.all())
    std0 = cmp[cmp.mode == "STANDARD_PREEMPT_0BPS"]
    std50 = cmp[cmp.mode == "STANDARD_PREEMPT_50BPS"]
    stress50 = cmp[cmp.mode == "STRESS_PREEMPT_50BPS"]
    standard_value_add = bool(len(std0) == 3 and (std0.fast_incremental_ending_usd > 0).all())
    preempt50_value_add = bool(len(std50) == 3 and (std50.fast_incremental_ending_usd > 0).all())
    stress50_value_add = bool(len(stress50) == 3 and (stress50.fast_incremental_ending_usd > 0).all())
    occupancy_pass = bool(hard_cap_pass and baseline_parity_pass)
    fast30_portfolio_candidate = bool(occupancy_pass and standard_value_add and preempt50_value_add)

    summary.to_csv(OUT / "portfolio_summary.csv", index=False)
    daily.to_csv(OUT / "daily_equity.csv", index=False)
    fills.to_csv(OUT / "nonfrozen_fills.csv", index=False)
    preempts.to_csv(OUT / "preempt_events.csv", index=False)
    parity.to_csv(OUT / "baseline_parity.csv", index=False)
    cmp.to_csv(OUT / "combined_vs_baseline.csv", index=False)

    audit_out = {
        "version": "FAST_REBOUND_V008_COMBINED_OCCUPANCY_REPLAY",
        "window": f"{start.date()}..{end.date()}",
        "rsi_trades": int(len(rsi)),
        "fast_rule": FAST_CONFIG,
        "fast_cap_ratio": FAST_CAP_RATIO,
        "rsi_cap_ratio": RSI_CAP_RATIO,
        "hard_cap_pass": hard_cap_pass,
        "baseline_parity_pass": baseline_parity_pass,
        "standard_value_add_all_capitals": standard_value_add,
        "preempt50_value_add_all_capitals": preempt50_value_add,
        "stress50_value_add_all_capitals": stress50_value_add,
        "occupancy_engine_pass": occupancy_pass,
        "fast30_portfolio_candidate": fast30_portfolio_candidate,
        "order_writes": False,
        "live_approval": False,
    }
    (OUT / "FINAL_AUDIT.json").write_text(json.dumps(audit_out, indent=2) + "\n", encoding="utf-8")

    show = ["capital_usd", "mode", "scenario", "ending_usd", "return_pct", "cagr_pct", "mdd_pct", "sharpe0",
            "rsi_pnl_usd", "fast_pnl_usd", "rsi_accepted", "rsi_rejected", "fast_candidates", "fast_accepted",
            "fast_rejected", "fast_capture_rate", "preempt_events", "preempt_rsi_release_usd", "preempt_fast_release_usd",
            "max_rsi_occupancy_usd", "max_fast_occupancy_usd", "max_gross_deployed_usd"]
    print("===== BASELINE PARITY =====", flush=True)
    print(parity.to_string(index=False), flush=True)
    print("===== PORTFOLIO SUMMARY =====", flush=True)
    print(summary[show].to_string(index=False), flush=True)
    print("===== COMBINED VS BASELINE =====", flush=True)
    cols = ["capital_usd", "mode", "ending_usd_baseline", "ending_usd_combined", "fast_incremental_ending_usd",
            "fast_incremental_return_pct_points", "mdd_pct_baseline", "mdd_pct_combined", "mdd_change_pct_points",
            "sharpe0_baseline", "sharpe0_combined", "sharpe_change", "fast_candidates", "fast_accepted", "fast_rejected",
            "fast_capture_rate", "value_add"]
    print(cmp[cols].to_string(index=False), flush=True)
    print("===== V008 RESULT SUMMARY =====", flush=True)
    print(f"BASELINE_PARITY_PASS={baseline_parity_pass}", flush=True)
    print(f"HARD_CAP_PASS={hard_cap_pass}", flush=True)
    print(f"STANDARD_VALUE_ADD_ALL_CAPITALS={standard_value_add}", flush=True)
    print(f"PREEMPT50_VALUE_ADD_ALL_CAPITALS={preempt50_value_add}", flush=True)
    print(f"STRESS50_VALUE_ADD_ALL_CAPITALS={stress50_value_add}", flush=True)
    print(f"OCCUPANCY_ENGINE_PASS={occupancy_pass}", flush=True)
    print(f"FAST30_PORTFOLIO_CANDIDATE={fast30_portfolio_candidate}", flush=True)
    print("LIVE_APPROVAL=False", flush=True)
    print("ORDER_WRITES=OFF", flush=True)
    print("NOTE=Frozen remains absolute priority; RSI and FAST only use idle capacity and do not preempt each other.", flush=True)
    print(f"OUTPUT={OUT}", flush=True)


if __name__ == "__main__":
    main()
