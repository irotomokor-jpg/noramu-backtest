#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
TRADES = ROOT / "fast_rebound_v003_koru" / "koru_trades_costed.csv"
BASE_SRC = ROOT / "fast_rebound_v001_research.py"
OUT = ROOT / "fast_rebound_v004_koru_regime"
NY = "America/New_York"
START = pd.Timestamp("2022-01-01")
END = pd.Timestamp("2026-08-14")
STD = "ACCOUNT_PLUS_2BPS_SLIP"
STRESS = "ACCOUNT_PLUS_5BPS_SLIP"
TARGET_CONFIGS = [
    "K_CLOSE_STRONG__S04_T06_M10",
    "K_CLOSE_STRONG__S08_T12_M20",
    "K_VOLUME_HIGH__S08_T12_M20",
]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"IMPORT_FAIL={path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def pf_exp(g: pd.DataFrame) -> dict:
    r = pd.to_numeric(g.net_return, errors="coerce").dropna()
    if r.empty:
        return {"trades": 0, "win_rate": np.nan, "expectancy_bps": np.nan, "profit_factor": np.nan}
    w = r[r > 0]
    l = r[r < 0]
    gp = float(w.sum()) if len(w) else 0.0
    gl = float(-l.sum()) if len(l) else 0.0
    return {
        "trades": int(len(r)),
        "win_rate": float((r > 0).mean()),
        "expectancy_bps": float(r.mean() * 10000.0),
        "profit_factor": float(gp / gl) if gl > 0 else np.inf,
    }


def load_context(base, symbol: str) -> pd.DataFrame:
    base.REQUESTED_START = START
    base.REQUESTED_END = END
    raw = base.load_symbol(symbol)
    raw = raw[(raw.ts.dt.date >= START.date()) & (raw.ts.dt.date <= END.date())].copy()
    parts = []
    for td, g in raw.groupby("trade_date", sort=True):
        x = base.source_features(g.copy()).sort_values("ts").reset_index(drop=True)
        if x.empty:
            continue
        open_px = float(x.open.iloc[0])
        x["session_ret"] = x.close / open_px - 1.0
        x["run_high"] = x.high.cummax()
        x["dd_from_high"] = x.close / x.run_high - 1.0
        x["vwap_slope10"] = x.vwap / x.vwap.shift(10) - 1.0
        x["lower_low"] = (x.low < x.low.shift(1)).astype(float)
        x["lower_low_count10"] = x.lower_low.rolling(10, min_periods=1).sum()
        x["red"] = (x.close < x.close.shift(1)).astype(float)
        x["red_count10"] = x.red.rolling(10, min_periods=1).sum()
        parts.append(x[["ts", "trade_date", "session_ret", "dd_from_high", "vwap_slope10", "lower_low_count10", "red_count10", "rv20", "vwap_z"]].copy())
    if not parts:
        raise SystemExit(f"NO_CONTEXT={symbol}")
    z = pd.concat(parts, ignore_index=True).sort_values("ts").drop_duplicates("ts", keep="last")
    ren = {c: f"{symbol.lower()}_{c}" for c in z.columns if c not in ["ts", "trade_date"]}
    return z.rename(columns=ren).reset_index(drop=True)


def attach_asof(tr: pd.DataFrame, ctx: pd.DataFrame) -> pd.DataFrame:
    a = tr.sort_values("signal_dt").copy()
    b = ctx.sort_values("ts").copy()
    out = pd.merge_asof(a, b, left_on="signal_dt", right_on="ts", direction="backward", tolerance=pd.Timedelta(minutes=1))
    return out.drop(columns=["ts"], errors="ignore")


def period_bucket(dt: pd.Series) -> pd.Series:
    y = dt.dt.year.astype(str)
    h = np.where(dt.dt.month <= 6, "H1", "H2")
    return pd.Series(y + h, index=dt.index)


def add_bins(x: pd.DataFrame) -> pd.DataFrame:
    z = x.copy()
    z["ewy_session_bin"] = pd.cut(z.ewy_session_ret, [-np.inf, -0.012, -0.006, 0.0, np.inf], labels=["LE_-1.2", "-1.2_-0.6", "-0.6_0", "GT_0"])
    z["ewy_dd_bin"] = pd.cut(z.ewy_dd_from_high, [-np.inf, -0.015, -0.008, 0.0], labels=["LE_-1.5", "-1.5_-0.8", "GT_-0.8"], include_lowest=True)
    z["ewy_vwap_slope_bin"] = pd.cut(z.ewy_vwap_slope10, [-np.inf, -0.0015, -0.0005, 0.0, np.inf], labels=["LT_-15bp", "-15_-5bp", "-5_0bp", "GE_0"])
    z["ewy_lowerlow_bin"] = pd.cut(z.ewy_lower_low_count10, [-np.inf, 3, 6, np.inf], labels=["0_3", "4_6", "7_10"])
    z["spy_session_bin"] = pd.cut(z.spy_session_ret, [-np.inf, -0.0075, 0.0, np.inf], labels=["LE_-0.75", "-0.75_0", "GT_0"])
    z["qqq_session_bin"] = pd.cut(z.qqq_session_ret, [-np.inf, -0.010, 0.0, np.inf], labels=["LE_-1.0", "-1.0_0", "GT_0"])
    m = z.signal_dt.dt.hour * 60 + z.signal_dt.dt.minute
    z["time_bin"] = np.select([m < 10*60+30, m < 12*60], ["09:40-10:29", "10:30-11:59"], default="12:00-14:30")
    return z


def grouped(z: pd.DataFrame, key: str) -> pd.DataFrame:
    rows = []
    for (cfg, val), g in z.groupby(["config", key], dropna=False, sort=True):
        rows.append({"config": cfg, key: str(val), **pf_exp(g)})
    return pd.DataFrame(rows)


def guard_masks(z: pd.DataFrame) -> dict[str, pd.Series]:
    broad = (z.spy_session_ret <= -0.0075) | (z.qqq_session_ret <= -0.0100)
    ewy_break = (z.ewy_session_ret <= -0.0120) & (z.ewy_vwap_slope10 < -0.0010)
    persistence = (z.ewy_lower_low_count10 >= 7) & (z.ewy_vwap_slope10 < 0.0)
    deep_dd = (z.ewy_dd_from_high <= -0.018) & (z.ewy_vwap_slope10 < -0.0005)
    return {
        "G0_NONE": pd.Series(True, index=z.index),
        "G1_NO_BROAD_SELLOFF": ~broad,
        "G2_NO_EWY_TREND_BREAK": ~ewy_break,
        "G3_NO_PERSISTENT_LOWERLOW": ~persistence,
        "G4_NO_DEEP_DD": ~deep_dd,
        "G5_COMBINED": ~(broad | ewy_break | persistence),
    }


def guard_table(z: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for cfg in TARGET_CONFIGS:
        base_cfg = z[z.config == cfg].copy()
        if base_cfg.empty:
            continue
        for guard, mask in guard_masks(base_cfg).items():
            keep = base_cfg[mask].copy()
            for period, gp in keep.groupby("period", sort=True):
                rows.append({"config": cfg, "guard": guard, "period": period, **pf_exp(gp)})
            rows.append({"config": cfg, "guard": guard, "period": "ALL", **pf_exp(keep)})
    return pd.DataFrame(rows)


def guard_summary(gt: pd.DataFrame) -> pd.DataFrame:
    allr = gt[gt.period == "ALL"].copy()
    half = gt[gt.period != "ALL"].copy()
    stats = half.groupby(["config", "guard"]).agg(
        positive_halves=("expectancy_bps", lambda s: int((s > 0).sum())),
        min_half_pf=("profit_factor", "min"),
        median_half_pf=("profit_factor", "median"),
        mean_half_exp_bps=("expectancy_bps", "mean"),
        halves=("period", "nunique"),
    ).reset_index()
    out = allr.merge(stats, on=["config", "guard"], how="left")
    out = out.rename(columns={"trades": "all_trades", "win_rate": "all_win_rate", "expectancy_bps": "all_expectancy_bps", "profit_factor": "all_profit_factor"})
    return out.sort_values(["positive_halves", "min_half_pf", "all_expectancy_bps"], ascending=[False, False, False]).reset_index(drop=True)


def main():
    if not TRADES.exists():
        raise SystemExit(f"MISSING_TRADES={TRADES}")
    if not BASE_SRC.exists():
        raise SystemExit(f"MISSING_BASE={BASE_SRC}")
    OUT.mkdir(parents=True, exist_ok=True)
    base = load_module("fast_rebound_v001_for_v004", BASE_SRC)

    tr = pd.read_csv(TRADES)
    tr = tr[tr.config.isin(TARGET_CONFIGS) & tr.cost_scenario.isin([STD, STRESS])].copy()
    tr["signal_dt"] = pd.to_datetime(tr.signal_ts, utc=True).dt.tz_convert(NY)
    tr["period"] = period_bucket(tr.signal_dt)
    print("FAST_REBOUND_V004_KORU_REGIME_DIAGNOSTIC", flush=True)
    print("PURPOSE=EXPLAIN_WHY_KORU_EDGE_FAILS_IN_SOME_HALF_YEAR_FOLDS", flush=True)
    print("MODE=DIAGNOSTIC_NOT_STRATEGY_SELECTION", flush=True)
    print("CONFIGS=" + ",".join(TARGET_CONFIGS), flush=True)
    print("ORDER_WRITES=OFF", flush=True)

    for sym in ["EWY", "SPY", "QQQ"]:
        print(f"LOAD_CONTEXT={sym}", flush=True)
        ctx = load_context(base, sym)
        tr = attach_asof(tr, ctx)
        print(f"CONTEXT_DONE={sym} rows={len(ctx)}", flush=True)

    tr = add_bins(tr)
    tr.to_csv(OUT / "trades_with_regime_context.csv", index=False)

    std = tr[tr.cost_scenario == STD].copy()
    stress = tr[tr.cost_scenario == STRESS].copy()

    baseline_rows = []
    for cost_name, zz in [(STD, std), (STRESS, stress)]:
        for (cfg, period), g in zz.groupby(["config", "period"], sort=True):
            baseline_rows.append({"cost": cost_name, "config": cfg, "period": period, **pf_exp(g)})
        for cfg, g in zz.groupby("config", sort=True):
            baseline_rows.append({"cost": cost_name, "config": cfg, "period": "ALL", **pf_exp(g)})
    baseline = pd.DataFrame(baseline_rows)
    baseline.to_csv(OUT / "baseline_by_halfyear.csv", index=False)

    features = ["ewy_session_bin", "ewy_dd_bin", "ewy_vwap_slope_bin", "ewy_lowerlow_bin", "spy_session_bin", "qqq_session_bin", "time_bin"]
    diag = []
    for f in features:
        g = grouped(std, f)
        g.insert(0, "feature", f)
        diag.append(g)
    diagnostics = pd.concat(diag, ignore_index=True)
    diagnostics.to_csv(OUT / "feature_bucket_diagnostics_standard.csv", index=False)

    gt_std = guard_table(std)
    gt_std.insert(0, "cost", STD)
    gt_stress = guard_table(stress)
    gt_stress.insert(0, "cost", STRESS)
    guards = pd.concat([gt_std, gt_stress], ignore_index=True)
    guards.to_csv(OUT / "guard_results_by_halfyear.csv", index=False)

    gs_std = guard_summary(gt_std.drop(columns=["cost"], errors="ignore"))
    gs_stress = guard_summary(gt_stress.drop(columns=["cost"], errors="ignore"))
    gs_std = gs_std.rename(columns={c: "std_" + c for c in gs_std.columns if c not in ["config", "guard"]})
    gs_stress = gs_stress.rename(columns={c: "stress_" + c for c in gs_stress.columns if c not in ["config", "guard"]})
    gs = gs_std.merge(gs_stress, on=["config", "guard"], how="outer")
    gs["exploratory_guard_candidate"] = (
        (gs.std_all_profit_factor > 1.05)
        & (gs.std_all_expectancy_bps > 0)
        & (gs.std_positive_halves >= 7)
        & (gs.std_min_half_pf >= 0.70)
        & (gs.stress_all_expectancy_bps > -1.0)
    )
    gs = gs.sort_values(["exploratory_guard_candidate", "std_positive_halves", "std_min_half_pf", "std_all_expectancy_bps"], ascending=[False, False, False, False]).reset_index(drop=True)
    gs.to_csv(OUT / "GUARD_SUMMARY.csv", index=False)

    print("===== BASELINE HALF-YEAR STANDARD COST =====", flush=True)
    print(baseline[baseline.cost == STD].to_string(index=False), flush=True)
    print("===== REGIME GUARD SUMMARY =====", flush=True)
    cols = ["config", "guard", "std_all_trades", "std_all_profit_factor", "std_all_expectancy_bps", "std_positive_halves", "std_min_half_pf", "std_mean_half_exp_bps", "stress_all_profit_factor", "stress_all_expectancy_bps", "exploratory_guard_candidate"]
    print(gs[cols].head(30).to_string(index=False), flush=True)
    print("===== FEATURE BUCKET DIAGNOSTIC TOP/BOTTOM =====", flush=True)
    d = diagnostics[(diagnostics.trades >= 25) & np.isfinite(diagnostics.expectancy_bps)].copy()
    print("TOP", flush=True)
    print(d.sort_values(["expectancy_bps", "trades"], ascending=[False, False]).head(20).to_string(index=False), flush=True)
    print("BOTTOM", flush=True)
    print(d.sort_values(["expectancy_bps", "trades"], ascending=[True, False]).head(20).to_string(index=False), flush=True)
    print("V004_INTERPRETATION=exploratory_regime_diagnostic_only", flush=True)
    print("NEXT=freeze_a_guard_only_if_it_improves_bad_halves_without_destroying_holdout_behavior", flush=True)
    print("ORDER_WRITES=OFF", flush=True)
    print(f"OUTPUT={OUT}", flush=True)


if __name__ == "__main__":
    main()
