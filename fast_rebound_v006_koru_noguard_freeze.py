#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "fast_rebound_v004_koru_regime" / "trades_with_regime_context.csv"
OUT = ROOT / "fast_rebound_v006_koru_noguard"
CONFIG = "K_CLOSE_STRONG__S04_T06_M10"
STD = "ACCOUNT_PLUS_2BPS_SLIP"
STRESS = "ACCOUNT_PLUS_5BPS_SLIP"
BOOTSTRAPS = 5000
SEED = 20260816


def pf_exp(g: pd.DataFrame) -> dict:
    r = pd.to_numeric(g.net_return, errors="coerce").dropna()
    if r.empty:
        return {"trades": 0, "win_rate": np.nan, "expectancy_bps": np.nan, "profit_factor": np.nan, "avg_return": np.nan}
    w = r[r > 0]
    l = r[r < 0]
    gp = float(w.sum()) if len(w) else 0.0
    gl = float(-l.sum()) if len(l) else 0.0
    return {
        "trades": int(len(r)),
        "win_rate": float((r > 0).mean()),
        "expectancy_bps": float(r.mean() * 10000.0),
        "profit_factor": float(gp / gl) if gl > 0 else np.inf,
        "avg_return": float(r.mean()),
    }


def add_periods(z: pd.DataFrame) -> pd.DataFrame:
    x = z.copy()
    dt = pd.to_datetime(x.signal_dt, errors="coerce", utc=True).dt.tz_convert("America/New_York")
    x["year"] = dt.dt.year
    x["half"] = np.where(dt.dt.month <= 6, "H1", "H2")
    x["halfyear"] = x.year.astype("Int64").astype(str) + x.half
    x["sample_group"] = np.where(x.year <= 2023, "BACKCAST_2022_2023", "DEV_2024_2026")
    x["trade_date"] = dt.dt.date.astype(str)
    return x


def cluster_bootstrap_by_day(z: pd.DataFrame, seed: int, n_boot: int = BOOTSTRAPS) -> dict:
    x = z.copy()
    x["r"] = pd.to_numeric(x.net_return, errors="coerce")
    x = x.dropna(subset=["r", "trade_date"])
    groups = [g.r.to_numpy(dtype=float) for _, g in x.groupby("trade_date", sort=True) if len(g)]
    if len(groups) < 20:
        return {"bootstrap_days": len(groups), "trade_exp_ci_low_bps": np.nan, "trade_exp_ci_high_bps": np.nan, "daily_mean_ci_low_bps": np.nan, "daily_mean_ci_high_bps": np.nan, "prob_trade_exp_gt0": np.nan}
    rng = np.random.default_rng(seed)
    n = len(groups)
    trade_means = np.empty(n_boot, dtype=float)
    daily_means = np.empty(n_boot, dtype=float)
    daily_returns = np.array([np.prod(1.0 + a) - 1.0 for a in groups], dtype=float)
    for i in range(n_boot):
        idx = rng.integers(0, n, size=n)
        sampled = [groups[j] for j in idx]
        trade_means[i] = np.concatenate(sampled).mean() * 10000.0
        daily_means[i] = daily_returns[idx].mean() * 10000.0
    return {
        "bootstrap_days": n,
        "trade_exp_ci_low_bps": float(np.quantile(trade_means, 0.025)),
        "trade_exp_ci_high_bps": float(np.quantile(trade_means, 0.975)),
        "daily_mean_ci_low_bps": float(np.quantile(daily_means, 0.025)),
        "daily_mean_ci_high_bps": float(np.quantile(daily_means, 0.975)),
        "prob_trade_exp_gt0": float((trade_means > 0).mean()),
    }


def g2_keep(z: pd.DataFrame) -> pd.Series:
    trend_break = (pd.to_numeric(z.ewy_session_ret, errors="coerce") <= -0.0120) & (pd.to_numeric(z.ewy_vwap_slope10, errors="coerce") < -0.0010)
    return (~trend_break).fillna(False)


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"MISSING_SOURCE={SRC}")
    OUT.mkdir(parents=True, exist_ok=True)

    z = pd.read_csv(SRC)
    z = z[(z.config == CONFIG) & z.cost_scenario.isin([STD, STRESS])].copy()
    if z.empty:
        raise SystemExit("NO_V006_INPUT_TRADES")
    z = add_periods(z)

    print("FAST_REBOUND_V006_KORU_NOGUARD_FREEZE")
    print("PURPOSE=FREEZE_SIMPLE_BASELINE_IF_ROBUST_WITHOUT_HISTORICALLY_DISCOVERED_G2")
    print(f"CONFIG={CONFIG}")
    print("RULE=NO_REGIME_GUARD")
    print("STOP=0.4pct TP=0.6pct MAX_HOLD=10min")
    print(f"STANDARD_COST={STD}")
    print(f"STRESS_COST={STRESS}")
    print(f"CLUSTER_BOOTSTRAP_BY_DAY={BOOTSTRAPS}")
    print("ORDER_WRITES=OFF")

    rows = []
    half_rows = []
    boot_rows = []
    for cost in [STD, STRESS]:
        base = z[z.cost_scenario == cost].copy()
        scopes = [
            ("ALL", base),
            ("BACKCAST_2022_2023", base[base["sample_group"] == "BACKCAST_2022_2023"]),
            ("DEV_2024_2026", base[base["sample_group"] == "DEV_2024_2026"]),
        ]
        for scope, sub in scopes:
            rows.append({"cost": cost, "scope": scope, **pf_exp(sub)})
        for hy, sub in base.groupby("halfyear", sort=True):
            half_rows.append({"cost": cost, "halfyear": hy, **pf_exp(sub)})
        boot_rows.append({"cost": cost, **cluster_bootstrap_by_day(base, SEED + (0 if cost == STD else 1000))})

    summary = pd.DataFrame(rows)
    half = pd.DataFrame(half_rows)
    boot = pd.DataFrame(boot_rows)

    # G2 is diagnostic only in V006: quantify whether it actually removed bad trades.
    std_all = z[z.cost_scenario == STD].copy()
    reject = std_all[~g2_keep(std_all)].copy()
    keep = std_all[g2_keep(std_all)].copy()
    g2_diag = pd.DataFrame([
        {"bucket": "NO_GUARD_ALL", **pf_exp(std_all)},
        {"bucket": "G2_KEEP", **pf_exp(keep)},
        {"bucket": "G2_REJECT", **pf_exp(reject)},
    ])

    summary.to_csv(OUT / "summary_by_sample.csv", index=False)
    half.to_csv(OUT / "halfyear_no_guard.csv", index=False)
    boot.to_csv(OUT / "cluster_bootstrap_by_day.csv", index=False)
    g2_diag.to_csv(OUT / "g2_value_add_diagnostic.csv", index=False)

    s_all = summary[(summary.cost == STD) & (summary.scope == "ALL")].iloc[0]
    x_all = summary[(summary.cost == STRESS) & (summary.scope == "ALL")].iloc[0]
    s_ho = summary[(summary.cost == STD) & (summary.scope == "BACKCAST_2022_2023")].iloc[0]
    x_ho = summary[(summary.cost == STRESS) & (summary.scope == "BACKCAST_2022_2023")].iloc[0]
    hs = half[half.cost == STD].copy()
    positive_halves = int((hs.expectancy_bps > 0).sum())
    min_half_pf = float(hs.profit_factor.min()) if len(hs) else np.nan
    bstd = boot[boot.cost == STD].iloc[0]
    bstress = boot[boot.cost == STRESS].iloc[0]
    rejected = g2_diag[g2_diag.bucket == "G2_REJECT"].iloc[0]

    # Historical shadow candidate: deliberately less strict than LIVE approval.
    # +5bps bootstrap lower CI may remain below zero; that is reported as execution-risk uncertainty, not hidden.
    historical_shadow_candidate = bool(
        (s_all.trades >= 400)
        and (s_all.profit_factor >= 1.30)
        and (s_all.expectancy_bps >= 6.0)
        and (x_all.profit_factor >= 1.05)
        and (x_all.expectancy_bps > 0)
        and (s_ho.trades >= 120)
        and (s_ho.profit_factor >= 1.20)
        and (s_ho.expectancy_bps > 4.0)
        and (x_ho.profit_factor >= 1.00)
        and (x_ho.expectancy_bps > 0)
        and (positive_halves >= 8)
        and (min_half_pf >= 0.60)
        and np.isfinite(bstd.trade_exp_ci_low_bps)
        and (bstd.trade_exp_ci_low_bps > 0)
    )

    g2_adds_value = bool(np.isfinite(rejected.expectancy_bps) and rejected.expectancy_bps < 0)
    execution_stress_confirmed = bool(np.isfinite(bstress.trade_exp_ci_low_bps) and bstress.trade_exp_ci_low_bps > 0)

    final = pd.DataFrame([{
        "config": CONFIG,
        "std_all_trades": int(s_all.trades),
        "std_all_pf": float(s_all.profit_factor),
        "std_all_expectancy_bps": float(s_all.expectancy_bps),
        "stress_all_pf": float(x_all.profit_factor),
        "stress_all_expectancy_bps": float(x_all.expectancy_bps),
        "holdout_std_trades": int(s_ho.trades),
        "holdout_std_pf": float(s_ho.profit_factor),
        "holdout_std_expectancy_bps": float(s_ho.expectancy_bps),
        "holdout_stress_pf": float(x_ho.profit_factor),
        "holdout_stress_expectancy_bps": float(x_ho.expectancy_bps),
        "positive_halves": positive_halves,
        "halves": int(hs.halfyear.nunique()),
        "min_half_pf": min_half_pf,
        "bootstrap_std_ci_low_bps": float(bstd.trade_exp_ci_low_bps),
        "bootstrap_std_ci_high_bps": float(bstd.trade_exp_ci_high_bps),
        "bootstrap_std_prob_gt0": float(bstd.prob_trade_exp_gt0),
        "bootstrap_stress_ci_low_bps": float(bstress.trade_exp_ci_low_bps),
        "bootstrap_stress_ci_high_bps": float(bstress.trade_exp_ci_high_bps),
        "bootstrap_stress_prob_gt0": float(bstress.prob_trade_exp_gt0),
        "g2_rejected_trades": int(rejected.trades),
        "g2_rejected_expectancy_bps": float(rejected.expectancy_bps),
        "g2_rejected_pf": float(rejected.profit_factor),
        "g2_adds_value": g2_adds_value,
        "execution_stress_confirmed": execution_stress_confirmed,
        "historical_shadow_candidate": historical_shadow_candidate,
    }])
    final.to_csv(OUT / "FINAL_NOGUARD_FREEZE_VALIDATION.csv", index=False)

    print("===== FINAL NO-GUARD VALIDATION =====")
    print(final.to_string(index=False))
    print("===== STANDARD HALF-YEAR =====")
    print(hs.to_string(index=False))
    print("===== G2 VALUE-ADD DIAGNOSTIC =====")
    print(g2_diag.to_string(index=False))
    print("===== DAY-CLUSTER BOOTSTRAP =====")
    print(boot.to_string(index=False))
    print(f"HISTORICAL_SHADOW_CANDIDATE={historical_shadow_candidate}")
    print(f"G2_ADDS_VALUE={g2_adds_value}")
    print(f"EXECUTION_STRESS_CONFIRMED={execution_stress_confirmed}")
    print("NEXT=if historical_shadow_candidate true, freeze NO_GUARD rule and start forward shadow; do not tune historical thresholds again")
    print("ORDER_WRITES=OFF")
    print(f"OUTPUT={OUT}")


if __name__ == "__main__":
    main()
