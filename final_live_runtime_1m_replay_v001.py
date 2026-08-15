#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
NY = "America/New_York"
DB = ROOT / "toss_replay_cache" / "toss_1m.sqlite"
PARITY_SRC = ROOT / "rsi_live_shadow_parity_v001.py"
RUNTIME_SRC = ROOT / "rsi_live_shadow_runtime_v001.py"
V005_SRC = ROOT / "portfolio_200_exact_full_replay_v005.py"
TRADES = ROOT / "rsi_pullback_v004_long" / "trades_all.csv"
FROZEN_STRATEGY = ROOT / "forward/US_FROZEN_V1/runtime/strategies/PORTFOLIO_US_V010/strategy_daily.csv"
FROZEN_PORT = ROOT / "forward/US_FROZEN_V1/runtime/strategies/PORTFOLIO_US_V010/portfolio_daily.csv"
STRICT_TRADES = ROOT / "forward/US_FROZEN_V1/runtime/strategies/STRICT_EXEC_US_V007/trades.csv"
COMMISSION = ROOT / "live/US_FROZEN_V1/commission_status.json"
OUT = ROOT / "final_live_runtime_1m_replay_v001"

PAIRS = [("QQQ", "TQQQ"), ("SPY", "UPRO"), ("SOXX", "SOXL"), ("EWY", "KORU")]
SYMS = ["TQQQ", "SOXL", "KORU", "UPRO"]
INITIAL_W = {"TQQQ": 0.60, "SOXL": 0.20, "KORU": 0.10, "UPRO": 0.10}
CAPITAL_SCENARIOS = [1000.0, 1500.0, 2000.0]
RSI_TRADE_CAP_RATIO = 0.40
PREEMPT_SLIP_BPS = [0.0, 50.0]
REQUESTED_START = pd.Timestamp("2024-01-01")
PORTFOLIO_NAME = "P3_TQQQ60_SOXL20_KORU10_UPRO10"
PREMARKET_START_MIN = 4 * 60
REGULAR_OPEN_MIN = 9 * 60 + 30
REGULAR_CLOSE_MIN = 16 * 60


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"IMPORT_SPEC_FAIL={path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return mod


def parse_ts(x):
    t = pd.to_datetime(x, errors="coerce", utc=True)
    if isinstance(t, pd.Series):
        return t.dt.tz_convert(NY)
    return t.tz_convert(NY)


def commission_fraction() -> float:
    if not COMMISSION.exists():
        return 0.0
    j = json.loads(COMMISSION.read_text(encoding="utf-8"))
    return float(j.get("commissionFraction", 0) or 0)


def load_all_symbol(mod, symbol: str, end_date) -> pd.DataFrame:
    storage_end = str(pd.Timestamp(end_date).date() + pd.Timedelta(days=2))
    # Ask for a broad warmup window. If the DB starts later, read_symbol simply returns available rows.
    d = mod.read_symbol(DB, symbol, "2022-01-01", storage_end)
    if d.empty:
        raise SystemExit(f"NO_DATA={symbol}")
    d = d.copy().sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)
    return d


def setup_map(mod, sig_all: pd.DataFrame, calendar_dates: list) -> dict:
    daily = mod.daily_features(sig_all).copy().sort_values("date").reset_index(drop=True)
    didx = list(daily.date)
    out = {}
    for td in calendar_dates:
        pos = np.searchsorted(didx, td) - 1
        out[td] = None if pos < 0 else daily.iloc[pos]
    return out


def raw_by_date(d: pd.DataFrame) -> dict:
    z = d.copy()
    z["date"] = z.ts.dt.date
    return {k: g.drop(columns=["date"]).copy().reset_index(drop=True) for k, g in z.groupby("date", sort=False)}


def regular_by_date(mod, d: pd.DataFrame) -> dict:
    z = mod.regular(d).copy()
    z["date"] = z.ts.dt.date
    return {k: g.drop(columns=["date"]).copy().reset_index(drop=True) for k, g in z.groupby("date", sort=False)}


def build_pair_cache(mod, end_date, calendar_dates):
    symbols = sorted({s for p in PAIRS for s in p})
    all_data = {}
    print("DATA_LOAD_START symbols=8", flush=True)
    for i, s in enumerate(symbols, 1):
        all_data[s] = load_all_symbol(mod, s, end_date)
        print(f"DATA_LOAD {i}/8 symbol={s} rows={len(all_data[s])} first={all_data[s].ts.min()} last={all_data[s].ts.max()}", flush=True)

    cache = {}
    for sigsym, exesym in PAIRS:
        sig_all = all_data[sigsym]
        exe_all = all_data[exesym]
        cache[(sigsym, exesym)] = {
            "setup": setup_map(mod, sig_all, calendar_dates),
            "sig_reg": regular_by_date(mod, sig_all),
            "exe_reg": regular_by_date(mod, exe_all),
        }
    return all_data, cache


def pair_day(mod, cache, sigsym: str, exesym: str, td):
    c = cache[(sigsym, exesym)]
    setup = c["setup"].get(td)
    sigday = c["sig_reg"].get(td)
    exeday = c["exe_reg"].get(td)
    if setup is None or sigday is None or exeday is None or sigday.empty or exeday.empty:
        return None
    bars = mod.bars5(sigday)
    if bars.empty:
        return None
    gap, brk = mod.first5_dynamic(bars, setup)
    score = float(setup.knife_weighted_static) + 2.0 * float(gap) + 1.0 * float(brk)
    return setup, sigday, exeday, bars, score


def discover_entry(par, mod, pack, td):
    setup, sigday, exeday, bars, score = pack
    if not bool(setup.arm_base):
        return None, 0
    pre_asof = pd.Timestamp(f"{td} 09:29:59", tz=NY)
    pre = par.live_entry(mod, setup, sigday, exeday, bars, score, pre_asof)
    pre_violation = int(pre is not None)

    # The strategy signal changes only when a 5-minute bar completes. Scan each causal
    # completion boundary, while execution and exits use the raw 1-minute ETF tape.
    for t in pd.date_range(pd.Timestamp(f"{td} 09:35", tz=NY), pd.Timestamp(f"{td} 15:00", tz=NY), freq="5min"):
        got = par.live_entry(mod, setup, sigday, exeday, bars, score, t)
        if got is not None:
            return got, pre_violation
    return None, pre_violation


def new_replay_position(entry_ts, entry_px: float):
    principal = 1.0
    qty = principal / float(entry_px)
    return {
        "qty": str(qty),
        "principal_usd": str(principal),
        "entry_price": str(float(entry_px)),
        "entry_ts": pd.Timestamp(entry_ts).isoformat(),
        "peak_price": str(float(entry_px)),
        "profit_locked": False,
        "last_processed_1m_ts": None,
        "pending_exit_reason": None,
        "pending_exit_after_ts": None,
        "last_trade_date": str(pd.Timestamp(entry_ts).date()),
        "last_exit_ts": None,
        "last_exit_price": None,
        "last_exit_reason": None,
        "simulated_realized_pnl_usd": "0",
    }


def replay_exit_minute_by_minute(runtime, exeday: pd.DataFrame, entry_ts, entry_px: float):
    p = new_replay_position(entry_ts, entry_px)
    x = exeday[exeday.ts >= pd.Timestamp(entry_ts)].copy().sort_values("ts").reset_index(drop=True)
    if x.empty:
        return None, 0
    calls = 0
    for _, r in x.iterrows():
        # A minute bar becomes completed one minute after its timestamp. JSON round-trip
        # every step emulates ledger persistence/restart semantics between watcher runs.
        asof = pd.Timestamp(r.ts) + pd.Timedelta(minutes=1)
        p = json.loads(json.dumps(p))
        out = runtime.simulate_exit(p, exeday, asof)
        calls += 1
        if out and out.get("action") == "SHADOW_EXIT":
            return out, calls
    return None, calls


def expected_trades():
    tr = pd.read_csv(TRADES)
    tr = tr[tr.variant == "DYN_2BAR"].copy()
    tr["entry_ny"] = parse_ts(tr.entry_ts)
    tr["exit_ny"] = parse_ts(tr.exit_ts)
    tr["entry_px"] = pd.to_numeric(tr.entry_px, errors="raise")
    tr["exit_px"] = pd.to_numeric(tr.exit_px, errors="raise")
    tr["net_return"] = pd.to_numeric(tr.net_return, errors="raise")
    return tr.sort_values(["entry_ny", "exec_symbol"]).reset_index(drop=True)


def trade_key(r):
    return (str(r.signal_symbol), str(r.exec_symbol), pd.Timestamp(r.entry_ny).isoformat())


def discover_runtime_trades(par, runtime, mod, cache, calendar_dates, cf: float):
    rows = []
    premarket_entry_violations = 0
    lookahead_violations = 0
    minute_exit_calls = 0
    sessions = len(calendar_dates)
    for i, td in enumerate(calendar_dates, 1):
        if i == 1 or i % 20 == 0 or i == sessions:
            print(f"REPLAY_PROGRESS session={i}/{sessions} date={td} trades_found={len(rows)}", flush=True)
        for sigsym, exesym in PAIRS:
            pack = pair_day(mod, cache, sigsym, exesym, td)
            if pack is None:
                continue
            got, pre_bad = discover_entry(par, mod, pack, td)
            premarket_entry_violations += pre_bad
            if got is None:
                continue
            signal_ts = pd.Timestamp(got["signal_ts"])
            entry_ts = pd.Timestamp(got["entry_ts"])
            if signal_ts > entry_ts:
                lookahead_violations += 1
            ex = None
            calls = 0
            exeday = pack[2]
            ex, calls = replay_exit_minute_by_minute(runtime, exeday, entry_ts, float(got["entry_px"]))
            minute_exit_calls += calls
            if ex is None:
                raise SystemExit(f"RUNTIME_EXIT_NOT_FOUND date={td} pair={sigsym}->{exesym} entry={entry_ts}")
            exit_ts = pd.Timestamp(ex["exit_ts"])
            entry_px = float(got["entry_px"])
            exit_px = float(ex["exit_px"])
            net_return = (exit_px * (1.0 - cf)) / (entry_px * (1.0 + cf)) - 1.0
            rows.append({
                "signal_symbol": sigsym,
                "exec_symbol": exesym,
                "trade_date": pd.Timestamp(td),
                "signal_ts": signal_ts,
                "entry_ny": entry_ts,
                "entry_px": entry_px,
                "exit_ny": exit_ts,
                "exit_px": exit_px,
                "exit_reason": str(ex["reason"]),
                "net_return": net_return,
                "knife_score": float(got["score"]),
                "runtime_minute_calls": calls,
            })
    got = pd.DataFrame(rows)
    if not got.empty:
        got = got.sort_values(["entry_ny", "exec_symbol"]).reset_index(drop=True)
        got["rsi_id"] = np.arange(len(got), dtype=int)
    return got, premarket_entry_violations, lookahead_violations, minute_exit_calls


def parity_full_audit(got: pd.DataFrame, exp: pd.DataFrame):
    exp_keys = {trade_key(r): r for _, r in exp.iterrows()}
    got_keys = {trade_key(r): r for _, r in got.iterrows()}
    missed = sorted(set(exp_keys) - set(got_keys))
    extra = sorted(set(got_keys) - set(exp_keys))
    exit_mismatch = []
    for k in sorted(set(exp_keys) & set(got_keys)):
        a = exp_keys[k]
        b = got_keys[k]
        ok = (
            pd.Timestamp(a.exit_ny) == pd.Timestamp(b.exit_ny)
            and str(a.exit_reason) == str(b.exit_reason)
            and abs(float(a.exit_px) - float(b.exit_px)) < 1e-8
            and abs(float(a.entry_px) - float(b.entry_px)) < 1e-8
        )
        if not ok:
            exit_mismatch.append({
                "key": k,
                "expected_exit": pd.Timestamp(a.exit_ny).isoformat(),
                "got_exit": pd.Timestamp(b.exit_ny).isoformat(),
                "expected_reason": str(a.exit_reason),
                "got_reason": str(b.exit_reason),
                "expected_entry_px": float(a.entry_px),
                "got_entry_px": float(b.entry_px),
                "expected_exit_px": float(a.exit_px),
                "got_exit_px": float(b.exit_px),
            })
    return {
        "expected_count": int(len(exp)),
        "runtime_count": int(len(got)),
        "missed_count": len(missed),
        "extra_false_positive_count": len(extra),
        "exit_mismatch_count": len(exit_mismatch),
        "missed": missed,
        "extra": extra,
        "exit_mismatches": exit_mismatch,
        "pass": bool(len(exp) == 42 and len(got) == 42 and not missed and not extra and not exit_mismatch),
    }


def premarket_audit(all_data: dict, start_date, end_date):
    rows = []
    summary = []
    for s, d0 in sorted(all_data.items()):
        d = d0.copy()
        d = d[(d.ts.dt.date >= start_date) & (d.ts.dt.date <= end_date)].copy()
        mins = d.ts.dt.hour * 60 + d.ts.dt.minute
        p = d[(mins >= PREMARKET_START_MIN) & (mins < REGULAR_OPEN_MIN)].copy()
        if not p.empty:
            p["trade_date"] = p.ts.dt.date
            for td, g in p.groupby("trade_date"):
                rows.append({
                    "symbol": s,
                    "trade_date": str(td),
                    "minutes_present": int(g.ts.nunique()),
                    "first_ts": g.ts.min().isoformat(),
                    "last_ts": g.ts.max().isoformat(),
                    "high": float(pd.to_numeric(g.high, errors="coerce").max()),
                    "low": float(pd.to_numeric(g.low, errors="coerce").min()),
                    "volume": float(pd.to_numeric(g.volume, errors="coerce").fillna(0).sum()),
                })
        summary.append({
            "symbol": s,
            "premarket_rows": int(len(p)),
            "premarket_days": int(p.ts.dt.date.nunique()) if len(p) else 0,
            "first_premarket_ts": None if p.empty else p.ts.min().isoformat(),
            "last_premarket_ts": None if p.empty else p.ts.max().isoformat(),
        })
    return pd.DataFrame(summary), pd.DataFrame(rows)


def build_frozen_inputs(v5, end_date):
    fs = pd.read_csv(FROZEN_STRATEGY)
    fs["trade_date"] = pd.to_datetime(fs.trade_date, utc=True).dt.tz_convert(None).dt.normalize()
    fs = fs.sort_values("trade_date").drop_duplicates("trade_date", keep="last").reset_index(drop=True)
    weights = v5.build_start_weights(fs)
    fs_by_day = {r.trade_date: r for _, r in fs.iterrows()}

    fp = pd.read_csv(FROZEN_PORT)
    fp["trade_date"] = pd.to_datetime(fp.trade_date, utc=True).dt.tz_convert(None).dt.normalize()
    fp = fp[fp.portfolio == PORTFOLIO_NAME].copy().sort_values("trade_date").reset_index(drop=True)
    fp["frozen_daily_return"] = pd.to_numeric(fp.portfolio_wealth, errors="raise").pct_change()
    start = max(REQUESTED_START, fp.trade_date.iloc[1], min(weights.keys()))
    end = min(pd.Timestamp(end_date), fp.trade_date.max())
    cal = fp[(fp.trade_date >= start) & (fp.trade_date <= end)][["trade_date", "frozen_daily_return"]].copy().reset_index(drop=True)
    if cal.empty or cal.frozen_daily_return.isna().any():
        raise SystemExit("BAD_FROZEN_CALENDAR")
    intervals = v5.select_strict_intervals(pd.read_csv(STRICT_TRADES))
    return fs_by_day, weights, cal, intervals


def run_scaled_portfolios(v5, runtime_trades: pd.DataFrame, cf: float, end_date):
    fs_by_day, weights, cal, intervals = build_frozen_inputs(v5, end_date)
    rt = runtime_trades.copy()
    rt["trade_date"] = pd.to_datetime(rt.trade_date).dt.tz_localize(None).dt.normalize()
    rt_by_day = {d: g.copy() for d, g in rt.groupby("trade_date")}

    summaries = []
    fills_all = []
    preempts_all = []
    daily_all = []
    cap_audits = []

    for capital in CAPITAL_SCENARIOS:
        trade_cap = capital * RSI_TRADE_CAP_RATIO
        v5.STARTING_USD = float(capital)
        v5.HARD_CAP_USD = float(capital)
        v5.RSI_TRADE_CAP_USD = float(trade_cap)
        v5.MIN_ORDER_USD = 1.0

        s0, d0, f0, p0 = v5.run_scenario(cal, fs_by_day, weights, intervals, rt_by_day, cf, None)
        s0["capital_usd"] = capital
        s0["trade_cap_usd"] = trade_cap
        s0["scenario"] = f"CAP{int(capital)}_FROZEN_ONLY"
        d0["scenario"] = s0["scenario"]
        summaries.append(s0); daily_all.append(d0)

        for slip in PREEMPT_SLIP_BPS:
            s, d, f, p = v5.run_scenario(cal, fs_by_day, weights, intervals, rt_by_day, cf, slip)
            label = f"CAP{int(capital)}_RSI40_PREEMPT_{int(slip)}BPS"
            s["capital_usd"] = capital
            s["trade_cap_usd"] = trade_cap
            s["scenario"] = label
            d["scenario"] = label
            if len(f):
                f["scenario"] = label
                f["capital_usd"] = capital
                f["trade_cap_usd"] = trade_cap
                fills_all.append(f)
            if len(p):
                p["scenario"] = label
                p["capital_usd"] = capital
                p["trade_cap_usd"] = trade_cap
                preempts_all.append(p)
            summaries.append(s); daily_all.append(d)

            max_single = 0.0
            if len(f):
                z = f[f.status == "FILLED"]
                if len(z):
                    max_single = float(pd.to_numeric(z.notional_usd, errors="coerce").max())
            cap_audits.append({
                "scenario": label,
                "capital_usd": capital,
                "trade_cap_usd": trade_cap,
                "max_single_trade_notional_usd": max_single,
                "max_aggregate_rsi_occupancy_usd": float(s["max_rsi_notional_usd"]),
                "max_gross_deployed_usd": float(s["max_gross_deployed_usd"]),
                "single_trade_cap_pass": bool(max_single <= trade_cap + 1e-7),
                "hard_cap_pass": bool(float(s["max_gross_deployed_usd"]) <= capital + 1e-7),
            })

    summary = pd.DataFrame(summaries)
    daily = pd.concat(daily_all, ignore_index=True)
    fills = pd.concat(fills_all, ignore_index=True) if fills_all else pd.DataFrame()
    preempts = pd.concat(preempts_all, ignore_index=True) if preempts_all else pd.DataFrame()
    cap_audit = pd.DataFrame(cap_audits)
    return summary, daily, fills, preempts, cap_audit, cal


def main():
    required = [DB, PARITY_SRC, RUNTIME_SRC, V005_SRC, TRADES, FROZEN_STRATEGY, FROZEN_PORT, STRICT_TRADES]
    for p in required:
        if not p.exists():
            raise SystemExit(f"MISSING_INPUT={p}")
    OUT.mkdir(parents=True, exist_ok=True)

    par = load_module("final_replay_parity", PARITY_SRC)
    runtime = load_module("final_replay_runtime", RUNTIME_SRC)
    v5 = load_module("final_replay_v005", V005_SRC)
    mod = par.ensure_engine()
    cf = commission_fraction()
    latest = par.latest_common_date()

    # Use the Frozen calendar from 2024 onward as the all-session replay calendar.
    fp0 = pd.read_csv(FROZEN_PORT)
    fp0["trade_date"] = pd.to_datetime(fp0.trade_date, utc=True).dt.tz_convert(None).dt.normalize()
    fp0 = fp0[fp0.portfolio == PORTFOLIO_NAME].copy().sort_values("trade_date")
    cdates = [x.date() for x in fp0[(fp0.trade_date >= REQUESTED_START) & (fp0.trade_date <= pd.Timestamp(latest))].trade_date]
    if not cdates:
        raise SystemExit("NO_REPLAY_CALENDAR")

    print("FINAL_LIVE_RUNTIME_1M_REPLAY_V001", flush=True)
    print(f"REQUESTED_PERIOD=2024-01-01..{latest}", flush=True)
    print(f"CALENDAR_SESSIONS={len(cdates)}", flush=True)
    print("CAPITAL_SCENARIOS=1000,1500,2000", flush=True)
    print("RSI_TRADE_CAP_RATIO=0.40", flush=True)
    print("PREMARKET_MODE=OBSERVE_ONLY_04:00_TO_09:30_ET", flush=True)
    print("ORDER_WRITES=OFF", flush=True)

    all_data, cache = build_pair_cache(mod, latest, cdates)
    pre_sum, pre_daily = premarket_audit(all_data, cdates[0], cdates[-1])
    pre_sum.to_csv(OUT / "premarket_coverage_summary.csv", index=False)
    pre_daily.to_csv(OUT / "premarket_daily.csv", index=False)

    exp = expected_trades()
    got, pre_bad, look_bad, minute_calls = discover_runtime_trades(par, runtime, mod, cache, cdates, cf)
    got.to_csv(OUT / "runtime_discovered_trades.csv", index=False)
    parity = parity_full_audit(got, exp)
    parity["premarket_entry_violations"] = int(pre_bad)
    parity["lookahead_ordering_violations"] = int(look_bad)
    parity["runtime_exit_minute_calls"] = int(minute_calls)
    parity["all_session_pass"] = bool(parity["pass"] and pre_bad == 0 and look_bad == 0)
    (OUT / "runtime_parity_audit.json").write_text(json.dumps(parity, indent=2, default=str) + "\n", encoding="utf-8")

    print("===== ALL-SESSION RUNTIME PARITY =====", flush=True)
    print(f"EXPECTED_TRADES={parity['expected_count']}", flush=True)
    print(f"RUNTIME_TRADES={parity['runtime_count']}", flush=True)
    print(f"MISSED_TRADES={parity['missed_count']}", flush=True)
    print(f"EXTRA_FALSE_POSITIVES={parity['extra_false_positive_count']}", flush=True)
    print(f"EXIT_MISMATCHES={parity['exit_mismatch_count']}", flush=True)
    print(f"PREMARKET_ENTRY_VIOLATIONS={pre_bad}", flush=True)
    print(f"LOOKAHEAD_ORDERING_VIOLATIONS={look_bad}", flush=True)
    print(f"RUNTIME_EXIT_MINUTE_CALLS={minute_calls}", flush=True)
    print(f"ALL_SESSION_RUNTIME_PARITY={'PASS' if parity['all_session_pass'] else 'FAIL'}", flush=True)
    if not parity["all_session_pass"]:
        raise SystemExit(30)

    summary, daily, fills, preempts, cap_audit, cal = run_scaled_portfolios(v5, got, cf, latest)
    summary.to_csv(OUT / "scaled_portfolio_summary.csv", index=False)
    daily.to_csv(OUT / "scaled_daily_equity.csv", index=False)
    fills.to_csv(OUT / "scaled_rsi_fills.csv", index=False)
    preempts.to_csv(OUT / "scaled_preempt_events.csv", index=False)
    cap_audit.to_csv(OUT / "scaled_cap_audit.csv", index=False)

    cap_pass = bool(len(cap_audit) and cap_audit.single_trade_cap_pass.all() and cap_audit.hard_cap_pass.all())
    print("===== SCALED CAPITAL AUDIT =====", flush=True)
    print(cap_audit.to_string(index=False), flush=True)
    print(f"SCALED_CAP_AUDIT={'PASS' if cap_pass else 'FAIL'}", flush=True)
    if not cap_pass:
        raise SystemExit(31)

    show_cols = ["scenario", "capital_usd", "trade_cap_usd", "ending_usd", "return_pct", "cagr_pct", "mdd_pct", "sharpe0", "rsi_pnl_usd", "rsi_accepted", "rsi_rejected", "preempt_events", "max_rsi_notional_usd", "max_gross_deployed_usd"]
    print("===== SCALED PORTFOLIO SUMMARY =====", flush=True)
    print(summary[show_cols].to_string(index=False), flush=True)
    print("===== PREMARKET COVERAGE =====", flush=True)
    print(pre_sum.to_string(index=False), flush=True)

    report = {
        "version": "FINAL_LIVE_RUNTIME_1M_REPLAY_V001",
        "requested_period_start": "2024-01-01",
        "replay_end": str(latest),
        "calendar_sessions": len(cdates),
        "runtime_trade_count": int(len(got)),
        "expected_trade_count": int(len(exp)),
        "runtime_parity_pass": bool(parity["all_session_pass"]),
        "premarket_mode": "OBSERVE_ONLY_NO_ENTRY",
        "premarket_window_et": "04:00-09:30",
        "capital_scenarios_usd": CAPITAL_SCENARIOS,
        "rsi_trade_cap_ratio": RSI_TRADE_CAP_RATIO,
        "rsi_trade_caps_usd": {str(int(x)): x * RSI_TRADE_CAP_RATIO for x in CAPITAL_SCENARIOS},
        "commission_fraction": cf,
        "capital_gains_tax": "IGNORED",
        "order_writes": False,
        "live_files_modified": False,
        "scaled_cap_audit_pass": cap_pass,
        "final_pass": bool(parity["all_session_pass"] and cap_pass),
    }
    (OUT / "FINAL_AUDIT.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("===== FINAL =====", flush=True)
    print(f"FINAL_RUNTIME_REPLAY={'PASS' if report['final_pass'] else 'FAIL'}", flush=True)
    print("ORDER_WRITES=OFF", flush=True)
    print("LIVE_WATCHER_UNCHANGED=true", flush=True)
    print(f"OUTPUT={OUT}", flush=True)


if __name__ == "__main__":
    main()
