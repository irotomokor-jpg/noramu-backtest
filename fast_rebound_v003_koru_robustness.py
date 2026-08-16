#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
V002_SRC = ROOT / "fast_rebound_v002_research.py"
OUT = ROOT / "fast_rebound_v003_koru"
NY = "America/New_York"

LOAD_START = pd.Timestamp("2022-01-01")
LOAD_END = pd.Timestamp("2026-08-14")
HOLDOUT_START = pd.Timestamp("2022-01-03")
HOLDOUT_END = pd.Timestamp("2023-12-29")
DEV_START = pd.Timestamp("2024-01-01")
DEV_END = pd.Timestamp("2026-08-14")
STANDARD_COST = "ACCOUNT_PLUS_2BPS_SLIP"
STRESS_COST = "ACCOUNT_PLUS_5BPS_SLIP"

# Orthogonal perturbations around V002 CAP_STRICT. Confirm logic remains PREV_HIGH_RECLAIM.
ENTRY_VARIANTS = {
    "K_LOOSE": {"rsi2_max": 12.0, "shock_z_max": -1.30, "vwap_z_max": -0.95, "vol_peak_min": 1.40, "close_pos_min": 0.55, "rv_rel_min": 0.75, "confirm": "PREV_HIGH_RECLAIM"},
    "K_CENTER": {"rsi2_max": 10.0, "shock_z_max": -1.40, "vwap_z_max": -1.05, "vol_peak_min": 1.60, "close_pos_min": 0.60, "rv_rel_min": 0.85, "confirm": "PREV_HIGH_RECLAIM"},
    "K_RSI_TIGHT": {"rsi2_max": 8.0, "shock_z_max": -1.40, "vwap_z_max": -1.05, "vol_peak_min": 1.60, "close_pos_min": 0.60, "rv_rel_min": 0.85, "confirm": "PREV_HIGH_RECLAIM"},
    "K_RSI_LOOSE": {"rsi2_max": 12.0, "shock_z_max": -1.40, "vwap_z_max": -1.05, "vol_peak_min": 1.60, "close_pos_min": 0.60, "rv_rel_min": 0.85, "confirm": "PREV_HIGH_RECLAIM"},
    "K_SHOCK_DEEP": {"rsi2_max": 10.0, "shock_z_max": -1.55, "vwap_z_max": -1.15, "vol_peak_min": 1.60, "close_pos_min": 0.60, "rv_rel_min": 0.85, "confirm": "PREV_HIGH_RECLAIM"},
    "K_VOLUME_HIGH": {"rsi2_max": 10.0, "shock_z_max": -1.40, "vwap_z_max": -1.05, "vol_peak_min": 1.80, "close_pos_min": 0.60, "rv_rel_min": 0.85, "confirm": "PREV_HIGH_RECLAIM"},
    "K_CLOSE_STRONG": {"rsi2_max": 10.0, "shock_z_max": -1.40, "vwap_z_max": -1.05, "vol_peak_min": 1.60, "close_pos_min": 0.68, "rv_rel_min": 0.85, "confirm": "PREV_HIGH_RECLAIM"},
    "K_RV_HIGH": {"rsi2_max": 10.0, "shock_z_max": -1.40, "vwap_z_max": -1.05, "vol_peak_min": 1.60, "close_pos_min": 0.60, "rv_rel_min": 0.95, "confirm": "PREV_HIGH_RECLAIM"},
    "K_DEEP_QUALITY": {"rsi2_max": 8.0, "shock_z_max": -1.50, "vwap_z_max": -1.15, "vol_peak_min": 1.70, "close_pos_min": 0.65, "rv_rel_min": 0.90, "confirm": "PREV_HIGH_RECLAIM"},
}

EXIT_PROFILES = {
    "S04_T06_M08": (0.004, 0.006, 8),
    "S04_T06_M10": (0.004, 0.006, 10),
    "S05_T07_M10": (0.005, 0.007, 10),
    "S06_T08_M12": (0.006, 0.008, 12),
    "S06_T08_M15": (0.006, 0.008, 15),
    "S07_T10_M15": (0.007, 0.010, 15),
    "S08_T12_M20": (0.008, 0.012, 20),
}

FOLDS = [
    ("F1", "2024-01-01", "2024-06-30", "2024-07-01", "2024-12-31"),
    ("F2", "2024-01-01", "2024-12-31", "2025-01-01", "2025-06-30"),
    ("F3", "2024-01-01", "2025-06-30", "2025-07-01", "2025-12-31"),
    ("F4", "2024-01-01", "2025-12-31", "2026-01-01", "2026-08-14"),
]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"IMPORT_FAIL={path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def pf_and_exp(g: pd.DataFrame) -> tuple[int, float, float, float, float]:
    if g.empty:
        return 0, np.nan, np.nan, np.nan, np.nan
    r = pd.to_numeric(g.net_return, errors="coerce").dropna()
    if r.empty:
        return 0, np.nan, np.nan, np.nan, np.nan
    wins = r[r > 0]
    losses = r[r < 0]
    gp = float(wins.sum()) if len(wins) else 0.0
    gl = float(-losses.sum()) if len(losses) else 0.0
    pf = gp / gl if gl > 0 else np.inf
    return int(len(r)), float((r > 0).mean()), float(r.mean() * 10000.0), float(pf), float(r.mean())


def metrics_row(g: pd.DataFrame, prefix: str = "") -> dict:
    n, wr, exp, pf, avg = pf_and_exp(g)
    return {
        f"{prefix}trades": n,
        f"{prefix}win_rate": wr,
        f"{prefix}expectancy_bps": exp,
        f"{prefix}profit_factor": pf,
        f"{prefix}avg_net_return": avg,
        f"{prefix}avg_hold_min": float(pd.to_numeric(g.hold_min, errors="coerce").mean()) if n else np.nan,
    }


def slice_dates(df: pd.DataFrame, start: str | pd.Timestamp, end: str | pd.Timestamp) -> pd.DataFrame:
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    d = pd.to_datetime(df.trade_date, errors="coerce")
    return df[(d >= s) & (d <= e)].copy()


def summarize_configs(net: pd.DataFrame, start, end, cost: str, prefix: str = "") -> pd.DataFrame:
    z = slice_dates(net[net.cost_scenario == cost], start, end)
    rows = []
    for cfg, g in z.groupby("config", sort=True):
        row = {"config": cfg}
        row.update(metrics_row(g, prefix))
        rows.append(row)
    return pd.DataFrame(rows)


def build_dev_holdout_table(net: pd.DataFrame) -> pd.DataFrame:
    dev = summarize_configs(net, DEV_START, DEV_END, STANDARD_COST, "dev_")
    dev_s = summarize_configs(net, DEV_START, DEV_END, STRESS_COST, "dev_stress_")
    ho = summarize_configs(net, HOLDOUT_START, HOLDOUT_END, STANDARD_COST, "holdout_")
    ho_s = summarize_configs(net, HOLDOUT_START, HOLDOUT_END, STRESS_COST, "holdout_stress_")
    out = dev.merge(dev_s, on="config", how="outer").merge(ho, on="config", how="outer").merge(ho_s, on="config", how="outer")

    std = net[net.cost_scenario == STANDARD_COST].copy()
    std["year"] = pd.to_datetime(std.trade_date).dt.year
    yr = []
    for (cfg, year), g in std.groupby(["config", "year"], sort=True):
        m = metrics_row(g)
        yr.append({"config": cfg, "year": int(year), **m})
    ydf = pd.DataFrame(yr)
    if len(ydf):
        ystats = ydf.groupby("config").agg(
            positive_years=("expectancy_bps", lambda s: int((s > 0).sum())),
            min_year_pf=("profit_factor", "min"),
            min_year_expectancy_bps=("expectancy_bps", "min"),
            years_observed=("year", "nunique"),
        ).reset_index()
        out = out.merge(ystats, on="config", how="left")
    else:
        ystats = pd.DataFrame()

    out["holdout_pass"] = (
        (out.dev_trades >= 250)
        & (out.dev_profit_factor >= 1.15)
        & (out.dev_expectancy_bps >= 4.0)
        & (out.dev_stress_profit_factor >= 1.00)
        & (out.dev_stress_expectancy_bps > 0)
        & (out.holdout_trades >= 120)
        & (out.holdout_profit_factor >= 1.05)
        & (out.holdout_expectancy_bps > 0)
        & (out.holdout_stress_profit_factor >= 1.00)
        & (out.holdout_stress_expectancy_bps > 0)
        & (out.positive_years >= 4)
        & (out.min_year_pf >= 0.85)
    )
    out["robust_score"] = (
        out.dev_expectancy_bps.fillna(-999)
        + out.holdout_expectancy_bps.fillna(-999)
        + 0.5 * out.dev_stress_expectancy_bps.fillna(-999)
        + 0.5 * out.holdout_stress_expectancy_bps.fillna(-999)
    ) * np.sqrt(out.dev_trades.fillna(0).clip(lower=1) + out.holdout_trades.fillna(0).clip(lower=1))
    return out.sort_values(["holdout_pass", "robust_score", "dev_trades"], ascending=[False, False, False]).reset_index(drop=True), ydf


def walkforward(net: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    std = net[net.cost_scenario == STANDARD_COST].copy()
    stress = net[net.cost_scenario == STRESS_COST].copy()
    selected_rows = []
    fixed_rows = []

    configs = sorted(std.config.unique())
    for fold, tr_s, tr_e, te_s, te_e in FOLDS:
        train_rows = []
        for cfg in configs:
            g = slice_dates(std[std.config == cfg], tr_s, tr_e)
            gs = slice_dates(stress[stress.config == cfg], tr_s, tr_e)
            m = metrics_row(g, "train_")
            ms = metrics_row(gs, "train_stress_")
            score = -np.inf
            if m["train_trades"] >= 40 and np.isfinite(m["train_expectancy_bps"]) and np.isfinite(m["train_profit_factor"]):
                penalty = 1.0 if (ms["train_stress_expectancy_bps"] > -2.0 and ms["train_stress_profit_factor"] >= 0.85) else 0.6
                score = m["train_expectancy_bps"] * np.sqrt(m["train_trades"]) * min(m["train_profit_factor"], 2.0) * penalty
            train_rows.append({"config": cfg, "score": score, **m, **ms})
        trank = pd.DataFrame(train_rows).sort_values(["score", "train_trades"], ascending=[False, False]).reset_index(drop=True)
        chosen = str(trank.iloc[0].config)
        test = slice_dates(std[std.config == chosen], te_s, te_e)
        test_s = slice_dates(stress[stress.config == chosen], te_s, te_e)
        selected_rows.append({
            "fold": fold, "train_start": tr_s, "train_end": tr_e, "test_start": te_s, "test_end": te_e,
            "selected_config": chosen, "train_score": float(trank.iloc[0].score),
            **metrics_row(test, "test_"), **metrics_row(test_s, "test_stress_"),
        })

        for cfg in configs:
            g = slice_dates(std[std.config == cfg], te_s, te_e)
            gs = slice_dates(stress[stress.config == cfg], te_s, te_e)
            fixed_rows.append({"fold": fold, "config": cfg, **metrics_row(g, "test_"), **metrics_row(gs, "test_stress_")})

    selected = pd.DataFrame(selected_rows)
    fixed = pd.DataFrame(fixed_rows)
    if len(fixed):
        stability = fixed.groupby("config").agg(
            folds=("fold", "nunique"),
            positive_test_folds=("test_expectancy_bps", lambda s: int((s > 0).sum())),
            positive_stress_folds=("test_stress_expectancy_bps", lambda s: int((s > 0).sum())),
            min_test_pf=("test_profit_factor", "min"),
            median_test_pf=("test_profit_factor", "median"),
            mean_test_expectancy_bps=("test_expectancy_bps", "mean"),
            mean_stress_expectancy_bps=("test_stress_expectancy_bps", "mean"),
        ).reset_index()
        stability["fold_stable"] = (
            (stability.positive_test_folds >= 3)
            & (stability.positive_stress_folds >= 2)
            & (stability.min_test_pf >= 0.85)
            & (stability.mean_test_expectancy_bps > 0)
        )
        stability = stability.sort_values(["fold_stable", "positive_test_folds", "mean_test_expectancy_bps"], ascending=[False, False, False]).reset_index(drop=True)
    else:
        stability = pd.DataFrame()
    return selected, fixed, stability


def diagnostics(net: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    z = net[net.cost_scenario == STANDARD_COST].copy()
    z["year"] = pd.to_datetime(z.trade_date).dt.year
    hour = pd.to_datetime(z.entry_ts, utc=True).dt.tz_convert(NY)
    mins = hour.dt.hour * 60 + hour.dt.minute
    z["time_bucket"] = np.select(
        [mins < 10 * 60 + 30, mins < 12 * 60],
        ["09:40-10:29", "10:30-11:59"],
        default="12:00-14:30",
    )

    by_time = []
    by_year = []
    overs = []
    stop_map = {k: v[0] for k, v in EXIT_PROFILES.items()}
    for (cfg, bucket), g in z.groupby(["config", "time_bucket"], sort=True):
        by_time.append({"config": cfg, "time_bucket": bucket, **metrics_row(g)})
    for (cfg, year), g in z.groupby(["config", "year"], sort=True):
        by_year.append({"config": cfg, "year": int(year), **metrics_row(g)})
    for cfg, g in z.groupby("config", sort=True):
        ep = str(g.exit_profile.iloc[0])
        stop = float(stop_map[ep])
        sg = g[g.exit_reason.astype(str).str.startswith("STOP")].copy()
        if len(sg):
            gross = pd.to_numeric(sg.gross_return, errors="coerce")
            overshoot_bps = (-gross - stop) * 10000.0
            overs.append({
                "config": cfg, "stop_pct": stop, "stop_trades": int(len(sg)),
                "avg_stop_gross_return": float(gross.mean()),
                "avg_stop_overshoot_bps": float(overshoot_bps.mean()),
                "p95_stop_overshoot_bps": float(overshoot_bps.quantile(0.95)),
                "worst_stop_gross_return": float(gross.min()),
            })
    return pd.DataFrame(by_time), pd.DataFrame(by_year), pd.DataFrame(overs)


def main():
    if not V002_SRC.exists():
        raise SystemExit(f"MISSING_V002={V002_SRC}")
    OUT.mkdir(parents=True, exist_ok=True)

    v2 = load_module("fast_rebound_v002_for_v003", V002_SRC)
    base = v2.load_base()
    base.REQUESTED_START = LOAD_START
    base.REQUESTED_END = LOAD_END
    v2.ENTRY_VARIANTS = ENTRY_VARIANTS
    v2.EXIT_PROFILES = EXIT_PROFILES
    v2.COOLDOWN_MIN = 8
    v2.MAX_TRADES_PER_PAIR_DAY = 3

    obs_fee, fee_meta = base.observed_commission()
    print("FAST_REBOUND_V003_KORU_ROBUSTNESS", flush=True)
    print(f"LOAD_PERIOD={LOAD_START.date()}..{LOAD_END.date()}", flush=True)
    print(f"BACKCAST_HOLDOUT={HOLDOUT_START.date()}..{HOLDOUT_END.date()}", flush=True)
    print(f"DEVELOPMENT_WINDOW={DEV_START.date()}..{DEV_END.date()}", flush=True)
    print("PAIR=EWY->KORU_ONLY", flush=True)
    print("PURPOSE=VERIFY_KORU_EDGE_NOT_SYSTEM_AVERAGE", flush=True)
    print(f"ACCOUNT_COMMISSION_FRACTION={obs_fee:.12g}", flush=True)
    print(f"ACCOUNT_COMMISSION_META={json.dumps(fee_meta, default=str)}", flush=True)
    print("STANDARD_COST=ACCOUNT_PLUS_2BPS_SLIP", flush=True)
    print("STRESS_COST=ACCOUNT_PLUS_5BPS_SLIP", flush=True)
    print("ENTRY_NEIGHBORHOOD=9_CAP_STRICT_PERTURBATIONS", flush=True)
    print("EXIT_NEIGHBORHOOD=7_SHORT_REBOUND_PROFILES", flush=True)
    print("ORDER_WRITES=OFF", flush=True)

    sig = base.load_symbol("EWY")
    exe = base.load_symbol("KORU")
    sig = sig[(sig.ts.dt.date >= LOAD_START.date()) & (sig.ts.dt.date <= LOAD_END.date())].copy()
    exe = exe[(exe.ts.dt.date >= LOAD_START.date()) & (exe.ts.dt.date <= LOAD_END.date())].copy()
    print(f"EWY_ROWS={len(sig)} KORU_ROWS={len(exe)}", flush=True)

    gross = v2.generate_pair_trades(base, "EWY", "KORU", sig, exe)
    if gross.empty:
        raise SystemExit("NO_KORU_TRADES")
    gross = gross.sort_values(["config", "entry_ts"]).reset_index(drop=True)
    gross.to_csv(OUT / "koru_trades_gross.csv", index=False)
    net = v2.apply_costs(base, gross)
    net.to_csv(OUT / "koru_trades_costed.csv", index=False)

    table, ydf = build_dev_holdout_table(net)
    table.to_csv(OUT / "dev_vs_backcast_holdout.csv", index=False)
    ydf.to_csv(OUT / "by_year_all_configs.csv", index=False)

    selected, fixed, stability = walkforward(net)
    selected.to_csv(OUT / "walkforward_selected.csv", index=False)
    fixed.to_csv(OUT / "walkforward_all_configs.csv", index=False)
    stability.to_csv(OUT / "walkforward_stability.csv", index=False)

    by_time, by_year, overs = diagnostics(net)
    by_time.to_csv(OUT / "by_time_bucket.csv", index=False)
    by_year.to_csv(OUT / "by_year_standard_cost.csv", index=False)
    overs.to_csv(OUT / "stop_overshoot.csv", index=False)

    merged = table.merge(stability[["config", "fold_stable", "positive_test_folds", "positive_stress_folds", "min_test_pf", "mean_test_expectancy_bps", "mean_stress_expectancy_bps"]], on="config", how="left")
    merged["final_research_pass"] = merged.holdout_pass & merged.fold_stable.fillna(False)
    merged = merged.sort_values(["final_research_pass", "holdout_pass", "robust_score"], ascending=[False, False, False]).reset_index(drop=True)
    merged.to_csv(OUT / "FINAL_RANKING.csv", index=False)

    pass_count = int(merged.final_research_pass.sum())
    holdout_count = int(merged.holdout_pass.sum())
    fold_count = int(stability.fold_stable.sum()) if len(stability) else 0

    print("===== TOP 20 DEV VS BACKCAST HOLDOUT =====", flush=True)
    cols = ["config", "dev_trades", "dev_profit_factor", "dev_expectancy_bps", "dev_stress_profit_factor", "dev_stress_expectancy_bps", "holdout_trades", "holdout_profit_factor", "holdout_expectancy_bps", "holdout_stress_profit_factor", "holdout_stress_expectancy_bps", "positive_years", "min_year_pf", "holdout_pass"]
    print(table[cols].head(20).to_string(index=False), flush=True)

    print("===== WALK-FORWARD SELECTED CONFIGS =====", flush=True)
    print(selected.to_string(index=False), flush=True)

    if len(stability):
        print("===== TOP WALK-FORWARD STABILITY =====", flush=True)
        print(stability.head(20).to_string(index=False), flush=True)

    print("===== STOP OVERSHOOT TOP DEV CANDIDATES =====", flush=True)
    top_cfgs = set(table.head(10).config)
    if len(overs):
        print(overs[overs.config.isin(top_cfgs)].to_string(index=False), flush=True)

    print(f"HOLDOUT_PASS_COUNT={holdout_count}", flush=True)
    print(f"FOLD_STABLE_COUNT={fold_count}", flush=True)
    print(f"FINAL_RESEARCH_PASS_COUNT={pass_count}", flush=True)
    print("NOTE=2022-2023 is an independent backcast holdout relative to FAST V001/V002 development, but it is earlier in time, not a future OOS sample.", flush=True)
    print("NOTE=2024-2026 walk-forward is temporal robustness, not pristine OOS because V002 was designed after observing the full 2024-2026 window.", flush=True)
    print("ORDER_WRITES=OFF", flush=True)
    print(f"OUTPUT={OUT}", flush=True)


if __name__ == "__main__":
    main()
