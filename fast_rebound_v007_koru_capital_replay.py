#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "fast_rebound_v004_koru_regime" / "trades_with_regime_context.csv"
RULE = ROOT / "fast_rebound_koru_v1_frozen.json"
OUT = ROOT / "fast_rebound_v007_koru_capital"
CONFIG = "K_CLOSE_STRONG__S04_T06_M10"
STD = "ACCOUNT_PLUS_2BPS_SLIP"
STRESS = "ACCOUNT_PLUS_5BPS_SLIP"
CAPITALS = [1000.0, 1500.0, 2000.0]
FRACTIONS = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
PORTFOLIO_SHARED_CAP_FRACTION = 0.30


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_rule() -> tuple[str, dict]:
    if not RULE.exists():
        raise SystemExit(f"MISSING_RULE={RULE}")
    r = json.loads(RULE.read_text(encoding="utf-8"))
    expected = {
        "version": "FAST_REBOUND_KORU_V1",
        "regime_guard": "NONE",
        "order_writes_enabled": False,
    }
    for k, v in expected.items():
        if r.get(k) != v:
            raise SystemExit(f"RULE_MISMATCH {k} expected={v} actual={r.get(k)}")
    e = r.get("entry", {})
    x = r.get("exit", {})
    f = r.get("frequency", {})
    frozen = {
        "rsi2_max": 10.0,
        "shock_z_max": -1.4,
        "vwap_z_max": -1.05,
        "min_abs_5m": 0.0025,
        "vol_peak3_min": 1.6,
        "close_pos_min": 0.68,
        "rv_rel_min": 0.85,
        "stop_pct": 0.004,
        "take_profit_pct": 0.006,
        "max_hold_minutes": 10,
        "cooldown_minutes": 8,
        "max_trades_per_day": 3,
    }
    actual = {
        "rsi2_max": float(e.get("rsi2_max")),
        "shock_z_max": float(e.get("shock_z_max")),
        "vwap_z_max": float(e.get("vwap_z_max")),
        "min_abs_5m": float(e.get("min_abs_5m")),
        "vol_peak3_min": float(e.get("vol_peak3_min")),
        "close_pos_min": float(e.get("close_pos_min")),
        "rv_rel_min": float(e.get("rv_rel_min")),
        "stop_pct": float(x.get("stop_pct")),
        "take_profit_pct": float(x.get("take_profit_pct")),
        "max_hold_minutes": int(x.get("max_hold_minutes")),
        "cooldown_minutes": int(f.get("cooldown_minutes")),
        "max_trades_per_day": int(f.get("max_trades_per_day")),
    }
    if actual != frozen:
        raise SystemExit(f"FROZEN_RULE_CHANGED actual={actual}")
    return sha256_file(RULE), r


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return np.nan
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min())


def longest_negative_streak(values: list[float]) -> int:
    best = 0
    cur = 0
    for v in values:
        if v < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def replay_one(g: pd.DataFrame, initial_capital: float, fraction: float) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    z = g.copy().sort_values(["entry_dt", "exit_dt"]).reset_index(drop=True)
    equity = float(initial_capital)
    fixed_notional = float(initial_capital) * float(fraction)
    rows = []
    for i, r in z.iterrows():
        nr = float(r.net_return)
        notional = min(fixed_notional, max(0.0, equity))
        pnl = notional * nr
        before = equity
        equity += pnl
        rows.append({
            "seq": i + 1,
            "trade_date": str(r.trade_date),
            "entry_ts": str(r.entry_ts),
            "exit_ts": str(r.exit_ts),
            "exit_reason": str(r.exit_reason),
            "net_return": nr,
            "equity_before": before,
            "notional_usd": notional,
            "pnl_usd": pnl,
            "equity_after": equity,
        })
    tr = pd.DataFrame(rows)
    if tr.empty:
        raise SystemExit("NO_REPLAY_ROWS")
    tr["equity_peak"] = tr.equity_after.cummax()
    tr["drawdown"] = tr.equity_after / tr.equity_peak - 1.0
    daily = tr.groupby("trade_date", sort=True).agg(
        trades=("seq", "count"),
        pnl_usd=("pnl_usd", "sum"),
        start_equity=("equity_before", "first"),
        end_equity=("equity_after", "last"),
    ).reset_index()
    daily["daily_return_on_initial"] = daily.pnl_usd / initial_capital
    first_date = pd.Timestamp(tr.trade_date.iloc[0])
    last_date = pd.Timestamp(tr.trade_date.iloc[-1])
    years = max((last_date - first_date).days / 365.2425, 1.0 / 365.2425)
    total_return = equity / initial_capital - 1.0
    cagr = (equity / initial_capital) ** (1.0 / years) - 1.0 if equity > 0 else -1.0
    rets = tr.net_return.astype(float)
    wins = rets[rets > 0]
    losses = rets[rets < 0]
    gp = float(wins.sum()) if len(wins) else 0.0
    gl = float(-losses.sum()) if len(losses) else 0.0
    summary = {
        "initial_capital_usd": initial_capital,
        "fraction": fraction,
        "fixed_notional_usd": fixed_notional,
        "trades": int(len(tr)),
        "active_days": int(len(daily)),
        "ending_equity_usd": float(equity),
        "pnl_usd": float(equity - initial_capital),
        "total_return_pct": float(total_return * 100.0),
        "cagr_pct": float(cagr * 100.0),
        "max_drawdown_pct": float(max_drawdown(pd.concat([pd.Series([initial_capital]), tr.equity_after], ignore_index=True)) * 100.0),
        "win_rate": float((rets > 0).mean()),
        "trade_profit_factor": gp / gl if gl > 0 else np.inf,
        "avg_trade_net_bps": float(rets.mean() * 10000.0),
        "worst_trade_pnl_usd": float(tr.pnl_usd.min()),
        "worst_trade_pct_initial": float(tr.pnl_usd.min() / initial_capital * 100.0),
        "best_trade_pnl_usd": float(tr.pnl_usd.max()),
        "worst_day_pnl_usd": float(daily.pnl_usd.min()),
        "worst_day_pct_initial": float(daily.daily_return_on_initial.min() * 100.0),
        "best_day_pnl_usd": float(daily.pnl_usd.max()),
        "max_consecutive_losing_trades": int(longest_negative_streak(tr.pnl_usd.tolist())),
        "max_consecutive_losing_days": int(longest_negative_streak(daily.pnl_usd.tolist())),
        "max_trades_day": int(daily.trades.max()),
        "avg_trades_active_day": float(daily.trades.mean()),
    }
    return summary, tr, daily


def main() -> None:
    rule_hash, rule = validate_rule()
    if not SRC.exists():
        raise SystemExit(f"MISSING_SOURCE={SRC}")
    OUT.mkdir(parents=True, exist_ok=True)

    z = pd.read_csv(SRC)
    z = z[(z.config == CONFIG) & z.cost_scenario.isin([STD, STRESS])].copy()
    if z.empty:
        raise SystemExit("NO_V007_INPUT_TRADES")
    z["entry_dt"] = pd.to_datetime(z.entry_ts, errors="coerce", utc=True)
    z["exit_dt"] = pd.to_datetime(z.exit_ts, errors="coerce", utc=True)
    z = z.dropna(subset=["entry_dt", "exit_dt", "net_return"]).copy()
    z["trade_date"] = z.entry_dt.dt.tz_convert("America/New_York").dt.date.astype(str)

    print("FAST_REBOUND_V007_KORU_CAPITAL_REPLAY", flush=True)
    print("PURPOSE=CAPITAL_SIZING_AND_ACCOUNT_RISK_WITH_FROZEN_STRATEGY", flush=True)
    print(f"RULE_SHA256={rule_hash}", flush=True)
    print(f"CONFIG={CONFIG}", flush=True)
    print("RULE=NO_GUARD_STOP04_TP06_MAX10_COOLDOWN8_MAX3DAY", flush=True)
    print("CAPITALS=1000,1500,2000", flush=True)
    print("FRACTIONS=10,15,20,25,30,40,50pct", flush=True)
    print("SIZING=FIXED_NOTIONAL_ANCHORED_TO_INITIAL_CAPITAL", flush=True)
    print("STANDARD_COST=ACCOUNT_PLUS_2BPS_SLIP", flush=True)
    print("STRESS_COST=ACCOUNT_PLUS_5BPS_SLIP", flush=True)
    print("ORDER_WRITES=OFF", flush=True)

    all_rows = []
    selected_trade_paths = []
    selected_daily_paths = []
    for cost in [STD, STRESS]:
        g = z[z.cost_scenario == cost].copy()
        for cap in CAPITALS:
            for frac in FRACTIONS:
                s, tr, daily = replay_one(g, cap, frac)
                s["cost"] = cost
                all_rows.append(s)
                if abs(frac - PORTFOLIO_SHARED_CAP_FRACTION) < 1e-12:
                    tr["cost"] = cost
                    tr["initial_capital_usd"] = cap
                    tr["fraction"] = frac
                    daily["cost"] = cost
                    daily["initial_capital_usd"] = cap
                    daily["fraction"] = frac
                    selected_trade_paths.append(tr)
                    selected_daily_paths.append(daily)

    replay = pd.DataFrame(all_rows)
    replay.to_csv(OUT / "capital_replay_all.csv", index=False)
    if selected_trade_paths:
        pd.concat(selected_trade_paths, ignore_index=True).to_csv(OUT / "selected_30pct_trade_paths.csv", index=False)
    if selected_daily_paths:
        pd.concat(selected_daily_paths, ignore_index=True).to_csv(OUT / "selected_30pct_daily_paths.csv", index=False)

    # Standalone risk screen. This does NOT model exact Frozen/RSI occupancy; it only tells us
    # how much KORU can tolerate by itself. Shared portfolio sizing is capped at 30% until an
    # exact combined occupancy replay is performed.
    std = replay[replay.cost == STD].copy()
    stress = replay[replay.cost == STRESS].copy()
    joined = std.merge(
        stress,
        on=["initial_capital_usd", "fraction", "fixed_notional_usd", "trades", "active_days"],
        suffixes=("_std", "_stress"),
    )
    joined["standalone_risk_pass"] = (
        (joined.total_return_pct_std > 0)
        & (joined.total_return_pct_stress > 0)
        & (joined.max_drawdown_pct_std >= -8.0)
        & (joined.max_drawdown_pct_stress >= -10.0)
        & (joined.worst_day_pct_initial_std >= -2.0)
        & (joined.worst_day_pct_initial_stress >= -2.5)
        & (joined.worst_trade_pct_initial_std >= -1.0)
    )
    joined.to_csv(OUT / "sizing_screen.csv", index=False)

    rec_rows = []
    for cap in CAPITALS:
        q = joined[(joined.initial_capital_usd == cap) & joined.standalone_risk_pass].copy()
        standalone = float(q.fraction.max()) if len(q) else np.nan
        shared = min(standalone, PORTFOLIO_SHARED_CAP_FRACTION) if np.isfinite(standalone) else np.nan
        rec_rows.append({
            "capital_usd": cap,
            "standalone_max_fraction": standalone,
            "standalone_max_notional_usd": cap * standalone if np.isfinite(standalone) else np.nan,
            "shared_portfolio_candidate_fraction": shared,
            "shared_portfolio_candidate_notional_usd": cap * shared if np.isfinite(shared) else np.nan,
            "note": "shared cap is provisional until exact Frozen+RSI+KORU occupancy replay",
        })
    rec = pd.DataFrame(rec_rows)
    rec.to_csv(OUT / "sizing_recommendation.csv", index=False)

    # Strategy-level diagnostics independent of capital size.
    std_trades = z[z.cost_scenario == STD].sort_values("entry_dt").copy()
    stop = std_trades[std_trades.exit_reason.astype(str).str.startswith("STOP")].copy()
    strategy = {
        "rule_sha256": rule_hash,
        "config": CONFIG,
        "trades": int(len(std_trades)),
        "active_days": int(std_trades.trade_date.nunique()),
        "win_rate": float((std_trades.net_return > 0).mean()),
        "avg_net_bps_standard": float(std_trades.net_return.mean() * 10000.0),
        "profit_factor_standard": float(std_trades.loc[std_trades.net_return > 0, "net_return"].sum() / -std_trades.loc[std_trades.net_return < 0, "net_return"].sum()),
        "max_trades_day": int(std_trades.groupby("trade_date").size().max()),
        "avg_trades_active_day": float(std_trades.groupby("trade_date").size().mean()),
        "stop_trades": int(len(stop)),
        "avg_stop_gross_pct": float(pd.to_numeric(stop.gross_return, errors="coerce").mean() * 100.0) if len(stop) else np.nan,
        "worst_stop_gross_pct": float(pd.to_numeric(stop.gross_return, errors="coerce").min() * 100.0) if len(stop) else np.nan,
    }
    (OUT / "strategy_summary.json").write_text(json.dumps(strategy, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("===== V007 STRATEGY RESULT SUMMARY =====", flush=True)
    print(f"TRADES={strategy['trades']}", flush=True)
    print(f"ACTIVE_DAYS={strategy['active_days']}", flush=True)
    print(f"WIN_RATE={strategy['win_rate']:.4f}", flush=True)
    print(f"STANDARD_PF={strategy['profit_factor_standard']:.4f}", flush=True)
    print(f"STANDARD_EXPECTANCY_BPS={strategy['avg_net_bps_standard']:.4f}", flush=True)
    print(f"MAX_TRADES_DAY={strategy['max_trades_day']}", flush=True)
    print(f"AVG_TRADES_ACTIVE_DAY={strategy['avg_trades_active_day']:.4f}", flush=True)
    print(f"STOP_TRADES={strategy['stop_trades']}", flush=True)
    print(f"AVG_STOP_GROSS_PCT={strategy['avg_stop_gross_pct']:.4f}", flush=True)
    print(f"WORST_STOP_GROSS_PCT={strategy['worst_stop_gross_pct']:.4f}", flush=True)

    print("===== CAPITAL REPLAY 30PCT SHARED-SLEEVE CANDIDATE =====", flush=True)
    cols = [
        "cost", "initial_capital_usd", "fixed_notional_usd", "trades", "ending_equity_usd",
        "pnl_usd", "total_return_pct", "cagr_pct", "max_drawdown_pct", "worst_trade_pnl_usd",
        "worst_trade_pct_initial", "worst_day_pnl_usd", "worst_day_pct_initial",
        "max_consecutive_losing_trades", "max_consecutive_losing_days",
    ]
    print(replay[replay.fraction == PORTFOLIO_SHARED_CAP_FRACTION][cols].to_string(index=False), flush=True)

    print("===== SIZING RECOMMENDATION =====", flush=True)
    print(rec.to_string(index=False), flush=True)
    print("SIZING_NOTE=30pct shared-portfolio cap is provisional, not an optimized threshold", flush=True)
    print("LIMITATION=V007_KORU_STANDALONE_DOES_NOT_YET_REPLAY_EXACT_FROZEN_RSI_TIME_OVERLAPS", flush=True)
    print("NEXT=use_selected_sizing_for_exact_Frozen_RSI_KORU_occupancy_replay_before_live", flush=True)
    print("ORDER_WRITES=OFF", flush=True)
    print(f"OUTPUT={OUT}", flush=True)


if __name__ == "__main__":
    main()
