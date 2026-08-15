#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT1 = ROOT / "final_live_runtime_1m_replay_v001"
OUT2 = ROOT / "final_live_runtime_1m_replay_v002_recovered"
DISCOVERED = OUT1 / "runtime_discovered_trades.csv"


def load_final():
    import importlib.util
    import sys

    path = ROOT / "final_live_runtime_1m_replay_v001.py"
    spec = importlib.util.spec_from_file_location("final_replay_v001_recovery", path)
    if spec is None or spec.loader is None:
        raise SystemExit("IMPORT_FINAL_REPLAY_FAIL")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def parse_trade_frame(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    for c in ["entry_ny", "exit_ny", "signal_ts"]:
        if c in x.columns:
            x[c] = pd.to_datetime(x[c], utc=True).dt.tz_convert("America/New_York")
    if "trade_date" in x.columns:
        x["trade_date"] = pd.to_datetime(x["trade_date"], errors="coerce").dt.tz_localize(None).dt.normalize()
    for c in ["entry_px", "exit_px", "net_return", "knife_score"]:
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    return x


def original_long_window(final):
    symbols = sorted({s for p in final.PAIRS for s in p})
    with sqlite3.connect(final.DB) as con:
        q = "SELECT symbol, MIN(timestamp) AS min_ts, MAX(timestamp) AS max_ts FROM candles WHERE symbol IN ({}) GROUP BY symbol".format(
            ",".join("?" for _ in symbols)
        )
        cov = pd.read_sql_query(q, con, params=symbols)
    if set(cov.symbol) != set(symbols):
        raise SystemExit("COVERAGE_SYMBOL_MISMATCH")
    cov["min_ts"] = pd.to_datetime(cov.min_ts, utc=True, errors="raise")
    cov["max_ts"] = pd.to_datetime(cov.max_ts, utc=True, errors="raise")
    common_min = cov.min_ts.max().tz_convert(final.NY).normalize()
    common_max = cov.max_ts.min().tz_convert(final.NY).normalize()
    analysis_start = (common_min + pd.Timedelta(days=320)).normalize()
    return cov.sort_values("symbol"), common_min, common_max, analysis_start


def run_window(final, v5, runtime_trades: pd.DataFrame, start_date, latest, label: str):
    final.REQUESTED_START = pd.Timestamp(start_date).tz_localize(None).normalize()
    summary, daily, fills, preempts, cap_audit, cal = final.run_scaled_portfolios(v5, runtime_trades, final.commission_fraction(), latest)
    for df in [summary, daily, fills, preempts, cap_audit]:
        if len(df):
            df.insert(0, "window", label)
    return summary, daily, fills, preempts, cap_audit, cal


def main():
    if not DISCOVERED.exists():
        raise SystemExit(f"DISCOVERED_TRADES_NOT_FOUND={DISCOVERED}")

    final = load_final()
    v5 = final.load_module("final_replay_v005_recovery", final.V005_SRC)
    OUT2.mkdir(parents=True, exist_ok=True)

    got_all = parse_trade_frame(pd.read_csv(DISCOVERED))
    exp = final.expected_trades()
    cov, common_min, common_max, analysis_start = original_long_window(final)

    # The original V004 long run intentionally starts only after a 320-calendar-day
    # common-history warmup. Therefore trades discovered before that date are not
    # false positives versus the 42-trade reference; they are pre-analysis discoveries.
    got_valid = got_all[got_all.entry_ny >= analysis_start].copy().reset_index(drop=True)
    got_pre = got_all[got_all.entry_ny < analysis_start].copy().reset_index(drop=True)

    parity = final.parity_full_audit(got_valid, exp)
    parity["common_raw_start"] = str(common_min.date())
    parity["common_raw_end"] = str(common_max.date())
    parity["original_long_analysis_start"] = str(analysis_start.date())
    parity["full_2024_runtime_count"] = int(len(got_all))
    parity["preanalysis_discovered_count"] = int(len(got_pre))
    parity["validated_window_runtime_count"] = int(len(got_valid))
    parity["interpretation"] = "PRE_ANALYSIS_DISCOVERIES_ARE_NOT_FALSE_POSITIVES_AGAINST_42_TRADE_REFERENCE"
    (OUT2 / "corrected_runtime_parity_audit.json").write_text(json.dumps(parity, indent=2, default=str) + "\n", encoding="utf-8")

    cov.to_csv(OUT2 / "data_coverage.csv", index=False)
    got_pre.to_csv(OUT2 / "preanalysis_discovered_trades.csv", index=False)
    got_valid.to_csv(OUT2 / "validated_window_runtime_trades.csv", index=False)

    pre_by_pair = (
        got_pre.groupby(["signal_symbol", "exec_symbol"], dropna=False)
        .size().rename("trades").reset_index()
        if len(got_pre) else pd.DataFrame(columns=["signal_symbol", "exec_symbol", "trades"])
    )
    pre_by_pair.to_csv(OUT2 / "preanalysis_by_pair.csv", index=False)

    print("FINAL_REPLAY_V002_RECOVERY", flush=True)
    print(f"COMMON_RAW={common_min.date()}..{common_max.date()}", flush=True)
    print(f"ORIGINAL_LONG_ANALYSIS_START={analysis_start.date()}", flush=True)
    print(f"FULL_2024_RUNTIME_TRADES={len(got_all)}", flush=True)
    print(f"PREANALYSIS_DISCOVERED_TRADES={len(got_pre)}", flush=True)
    print(f"VALIDATED_WINDOW_RUNTIME_TRADES={len(got_valid)}", flush=True)
    print(f"EXPECTED_REFERENCE_TRADES={len(exp)}", flush=True)
    print(f"VALIDATED_MISSED={parity['missed_count']}", flush=True)
    print(f"VALIDATED_EXTRAS={parity['extra_false_positive_count']}", flush=True)
    print(f"VALIDATED_EXIT_MISMATCHES={parity['exit_mismatch_count']}", flush=True)
    print(f"CORRECTED_RUNTIME_PARITY={'PASS' if parity['pass'] else 'FAIL'}", flush=True)
    if len(pre_by_pair):
        print("===== PRE-ANALYSIS DISCOVERIES BY PAIR =====", flush=True)
        print(pre_by_pair.to_string(index=False), flush=True)
    if not parity["pass"]:
        raise SystemExit(40)

    # Run both windows. VERIFIED is apples-to-apples with the 42-trade research window.
    # EXTENDED_2024 keeps the 16 pre-analysis discoveries as exploratory only.
    verified = run_window(final, v5, got_valid, analysis_start, common_max.date(), "VERIFIED_42_WINDOW")
    extended = run_window(final, v5, got_all, pd.Timestamp("2024-01-01"), common_max.date(), "EXTENDED_2024_EXPLORATORY")

    summaries = pd.concat([verified[0], extended[0]], ignore_index=True)
    dailies = pd.concat([verified[1], extended[1]], ignore_index=True)
    fills = pd.concat([x for x in [verified[2], extended[2]] if len(x)], ignore_index=True)
    preempts = pd.concat([x for x in [verified[3], extended[3]] if len(x)], ignore_index=True) if any(len(x) for x in [verified[3], extended[3]]) else pd.DataFrame()
    caps = pd.concat([verified[4], extended[4]], ignore_index=True)

    summaries.to_csv(OUT2 / "scaled_portfolio_summary_both_windows.csv", index=False)
    dailies.to_csv(OUT2 / "scaled_daily_equity_both_windows.csv", index=False)
    fills.to_csv(OUT2 / "scaled_rsi_fills_both_windows.csv", index=False)
    preempts.to_csv(OUT2 / "scaled_preempt_events_both_windows.csv", index=False)
    caps.to_csv(OUT2 / "scaled_cap_audit_both_windows.csv", index=False)

    cap_pass_verified = bool(
        len(caps[caps.window == "VERIFIED_42_WINDOW"])
        and caps[caps.window == "VERIFIED_42_WINDOW"].single_trade_cap_pass.all()
        and caps[caps.window == "VERIFIED_42_WINDOW"].hard_cap_pass.all()
    )
    cap_pass_extended = bool(
        len(caps[caps.window == "EXTENDED_2024_EXPLORATORY"])
        and caps[caps.window == "EXTENDED_2024_EXPLORATORY"].single_trade_cap_pass.all()
        and caps[caps.window == "EXTENDED_2024_EXPLORATORY"].hard_cap_pass.all()
    )

    audit = {
        "version": "FINAL_LIVE_RUNTIME_1M_REPLAY_V002_RECOVERED",
        "common_raw": f"{common_min.date()}..{common_max.date()}",
        "original_long_analysis_start": str(analysis_start.date()),
        "full_2024_runtime_trades": int(len(got_all)),
        "preanalysis_discovered_trades": int(len(got_pre)),
        "validated_window_runtime_trades": int(len(got_valid)),
        "reference_trades": int(len(exp)),
        "corrected_runtime_parity_pass": bool(parity["pass"]),
        "verified_cap_audit_pass": cap_pass_verified,
        "extended_cap_audit_pass": cap_pass_extended,
        "extended_2024_status": "EXPLORATORY_PREANALYSIS_WARMUP_NOT_PART_OF_ORIGINAL_42_REFERENCE",
        "order_writes": False,
        "live_runtime_core_pass": bool(parity["pass"] and cap_pass_verified),
    }
    (OUT2 / "FINAL_AUDIT.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")

    show = ["window", "scenario", "capital_usd", "trade_cap_usd", "ending_usd", "return_pct", "cagr_pct", "mdd_pct", "sharpe0", "rsi_pnl_usd", "rsi_accepted", "rsi_rejected", "preempt_events", "max_rsi_notional_usd", "max_gross_deployed_usd"]
    print("===== CAPITAL AUDIT =====", flush=True)
    print(caps.to_string(index=False), flush=True)
    print("===== PORTFOLIO SUMMARY =====", flush=True)
    print(summaries[show].to_string(index=False), flush=True)
    print(f"VERIFIED_CAP_AUDIT={'PASS' if cap_pass_verified else 'FAIL'}", flush=True)
    print(f"EXTENDED_CAP_AUDIT={'PASS' if cap_pass_extended else 'FAIL'}", flush=True)
    print(f"LIVE_RUNTIME_CORE={'PASS' if audit['live_runtime_core_pass'] else 'FAIL'}", flush=True)
    print("EXTENDED_2024=EXPLORATORY", flush=True)
    print("ORDER_WRITES=OFF", flush=True)
    print(f"OUTPUT={OUT2}", flush=True)


if __name__ == "__main__":
    main()
