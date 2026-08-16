#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "fast_rebound_v004_koru_regime" / "trades_with_regime_context.csv"
OUT = ROOT / "fast_rebound_v005_koru_guard"
STD = "ACCOUNT_PLUS_2BPS_SLIP"
STRESS = "ACCOUNT_PLUS_5BPS_SLIP"
CANDIDATES = [
    "K_CLOSE_STRONG__S04_T06_M10",
    "K_CLOSE_STRONG__S08_T12_M20",
]
BOOTSTRAPS = 2000
SEED = 20260816


def pf_exp(g: pd.DataFrame) -> dict:
    r = pd.to_numeric(g.net_return, errors="coerce").dropna()
    if r.empty:
        return {
            "trades": 0,
            "win_rate": np.nan,
            "expectancy_bps": np.nan,
            "profit_factor": np.nan,
            "avg_return": np.nan,
        }
    wins = r[r > 0]
    losses = r[r < 0]
    gp = float(wins.sum()) if len(wins) else 0.0
    gl = float(-losses.sum()) if len(losses) else 0.0
    return {
        "trades": int(len(r)),
        "win_rate": float((r > 0).mean()),
        "expectancy_bps": float(r.mean() * 10000.0),
        "profit_factor": float(gp / gl) if gl > 0 else np.inf,
        "avg_return": float(r.mean()),
    }


def g2_keep(z: pd.DataFrame) -> pd.Series:
    # Frozen from V004. Do not tune in V005.
    trend_break = (pd.to_numeric(z.ewy_session_ret, errors="coerce") <= -0.0120) & (
        pd.to_numeric(z.ewy_vwap_slope10, errors="coerce") < -0.0010
    )
    return (~trend_break).fillna(False)


def add_periods(z: pd.DataFrame) -> pd.DataFrame:
    x = z.copy()
    dt = pd.to_datetime(x.signal_dt, errors="coerce", utc=True).dt.tz_convert("America/New_York")
    x["year"] = dt.dt.year
    x["half"] = np.where(dt.dt.month <= 6, "H1", "H2")
    x["halfyear"] = x.year.astype("Int64").astype(str) + x.half
    x["sample"] = np.where(x.year <= 2023, "BACKCAST_2022_2023", "DEV_2024_2026")
    x["trade_date"] = dt.dt.date.astype(str)
    return x


def summarize_scope(z: pd.DataFrame, label: str) -> dict:
    m = pf_exp(z)
    return {"scope": label, **m}


def cluster_bootstrap_by_day(z: pd.DataFrame, seed: int, n_boot: int = BOOTSTRAPS) -> dict:
    x = z.copy()
    x["r"] = pd.to_numeric(x.net_return, errors="coerce")
    x = x.dropna(subset=["r", "trade_date"])
    groups = [g.r.to_numpy(dtype=float) for _, g in x.groupby("trade_date", sort=True) if len(g)]
    if len(groups) < 20:
        return {
            "bootstrap_days": len(groups),
            "trade_exp_ci_low_bps": np.nan,
            "trade_exp_ci_high_bps": np.nan,
            "daily_mean_ci_low_bps": np.nan,
            "daily_mean_ci_high_bps": np.nan,
            "prob_trade_exp_gt0": np.nan,
        }
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


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"MISSING_SOURCE={SRC}")
    OUT.mkdir(parents=True, exist_ok=True)

    z = pd.read_csv(SRC)
    z = z[z.config.isin(CANDIDATES) & z.cost_scenario.isin([STD, STRESS])].copy()
    if z.empty:
        raise SystemExit("NO_V005_INPUT_TRADES")
    z = add_periods(z)
    z["guard"] = np.where(g2_keep(z), "G2_KEEP", "G2_REJECT")
    z.to_csv(OUT / "v005_labeled_trades.csv", index=False)

    print("FAST_REBOUND_V005_KORU_GUARD_FREEZE")
    print("PURPOSE=FIX_G2_AND_TEST_ROBUSTNESS_WITHOUT_NEW_PARAMETER_SEARCH")
    print("G2=reject_if_EWY_session_return_le_-1.2pct_AND_EWY_vwap_slope10_lt_-10bp")
    print("CONFIGS=" + ",".join(CANDIDATES))
    print(f"STANDARD_COST={STD}")
    print(f"STRESS_COST={STRESS}")
    print(f"CLUSTER_BOOTSTRAP_BY_DAY={BOOTSTRAPS}")
    print("ORDER_WRITES=OFF")

    rows = []
    half_rows = []
    removed_rows = []
    boot_rows = []

    for cfg in CANDIDATES:
        for cost in [STD, STRESS]:
            base = z[(z.config == cfg) & (z.cost_scenario == cost)].copy()
            keep = base[base.guard == "G2_KEEP"].copy()
            reject = base[base.guard == "G2_REJECT"].copy()

            for label, sub in [
                ("ALL", keep),
                ("BACKCAST_2022_2023", keep[keep.sample == "BACKCAST_2022_2023"]),
                ("DEV_2024_2026", keep[keep.sample == "DEV_2024_2026"]),
            ]:
                rows.append({"config": cfg, "cost": cost, **summarize_scope(sub, label)})

            for hy, sub in keep.groupby("halfyear", sort=True):
                half_rows.append({"config": cfg, "cost": cost, "halfyear": hy, **pf_exp(sub)})

            removed_rows.append({
                "config": cfg,
                "cost": cost,
                "baseline_trades": int(len(base)),
                "kept_trades": int(len(keep)),
                "rejected_trades": int(len(reject)),
                "rejected_share": float(len(reject) / len(base)) if len(base) else np.nan,
                **{f"rejected_{k}": v for k, v in pf_exp(reject).items()},
            })

            b = cluster_bootstrap_by_day(keep, SEED + (0 if cost == STD else 1000) + CANDIDATES.index(cfg))
            boot_rows.append({"config": cfg, "cost": cost, **b})

    summary = pd.DataFrame(rows)
    half = pd.DataFrame(half_rows)
    removed = pd.DataFrame(removed_rows)
    boot = pd.DataFrame(boot_rows)

    summary.to_csv(OUT / "summary_by_sample.csv", index=False)
    half.to_csv(OUT / "halfyear_fixed_guard.csv", index=False)
    removed.to_csv(OUT / "rejected_trade_diagnostics.csv", index=False)
    boot.to_csv(OUT / "cluster_bootstrap_by_day.csv", index=False)

    final_rows = []
    for cfg in CANDIDATES:
        s_all = summary[(summary.config == cfg) & (summary.cost == STD) & (summary.scope == "ALL")].iloc[0]
        x_all = summary[(summary.config == cfg) & (summary.cost == STRESS) & (summary.scope == "ALL")].iloc[0]
        s_ho = summary[(summary.config == cfg) & (summary.cost == STD) & (summary.scope == "BACKCAST_2022_2023")].iloc[0]
        x_ho = summary[(summary.config == cfg) & (summary.cost == STRESS) & (summary.scope == "BACKCAST_2022_2023")].iloc[0]
        h = half[(half.config == cfg) & (half.cost == STD)].copy()
        bx = boot[(boot.config == cfg) & (boot.cost == STD)].iloc[0]
        rem = removed[(removed.config == cfg) & (removed.cost == STD)].iloc[0]

        positive_halves = int((h.expectancy_bps > 0).sum())
        halves = int(h.halfyear.nunique())
        min_half_pf = float(h.profit_factor.min()) if len(h) else np.nan
        rejected_bad = bool(
            (rem.rejected_trades >= 15)
            and np.isfinite(rem.rejected_expectancy_bps)
            and (rem.rejected_expectancy_bps < 0)
        )

        freeze_candidate = bool(
            (s_all.trades >= 350)
            and (s_all.profit_factor >= 1.20)
            and (s_all.expectancy_bps >= 5.0)
            and (x_all.profit_factor >= 1.05)
            and (x_all.expectancy_bps > 0)
            and (s_ho.trades >= 100)
            and (s_ho.profit_factor >= 1.10)
            and (s_ho.expectancy_bps > 2.0)
            and (x_ho.profit_factor >= 0.95)
            and (x_ho.expectancy_bps > -1.0)
            and (positive_halves >= 8)
            and (min_half_pf >= 0.75)
            and np.isfinite(bx.trade_exp_ci_low_bps)
            and (bx.trade_exp_ci_low_bps > 0)
            and rejected_bad
        )

        final_rows.append({
            "config": cfg,
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
            "halves": halves,
            "min_half_pf": min_half_pf,
            "bootstrap_trade_exp_ci_low_bps": float(bx.trade_exp_ci_low_bps),
            "bootstrap_trade_exp_ci_high_bps": float(bx.trade_exp_ci_high_bps),
            "bootstrap_prob_exp_gt0": float(bx.prob_trade_exp_gt0),
            "rejected_trades": int(rem.rejected_trades),
            "rejected_expectancy_bps": float(rem.rejected_expectancy_bps),
            "rejected_pf": float(rem.rejected_profit_factor),
            "freeze_candidate": freeze_candidate,
        })

    final = pd.DataFrame(final_rows).sort_values(
        ["freeze_candidate", "std_all_expectancy_bps"], ascending=[False, False]
    ).reset_index(drop=True)
    final.to_csv(OUT / "FINAL_FIXED_GUARD_VALIDATION.csv", index=False)

    print("===== FINAL FIXED G2 VALIDATION =====")
    print(final.to_string(index=False))
    print("===== STANDARD HALF-YEAR =====")
    print(half[half.cost == STD].to_string(index=False))
    print("===== REJECTED TRADE DIAGNOSTIC =====")
    print(removed[removed.cost == STD].to_string(index=False))
    print("===== DAY-CLUSTER BOOTSTRAP =====")
    print(boot.to_string(index=False))
    print(f"FREEZE_CANDIDATE_COUNT={int(final.freeze_candidate.sum())}")
    print("NOTE=G2 was discovered using historical data in V004; V005 is a frozen historical robustness check, not pristine future OOS.")
    print("NEXT=if_one_candidate_survives_freeze_it_and_start_forward_shadow; do_not_tune_G2_again")
    print("ORDER_WRITES=OFF")
    print(f"OUTPUT={OUT}")


if __name__ == "__main__":
    main()
