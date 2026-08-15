#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
BASE_SRC = ROOT / "fast_rebound_v001_research.py"
OUT = ROOT / "fast_rebound_v002"
NY = "America/New_York"

REQUESTED_START = pd.Timestamp("2024-01-01")
REQUESTED_END = pd.Timestamp("2026-08-14")
ENTRY_START_MIN = 9 * 60 + 40
ENTRY_END_MIN = 14 * 60 + 30
COOLDOWN_MIN = 8
MAX_TRADES_PER_PAIR_DAY = 3

MIN_ABS_5M = {
    "SPY": 0.0015,
    "QQQ": 0.0020,
    "SOXX": 0.0035,
    "EWY": 0.0025,
}

ENTRY_VARIANTS = {
    "CAP_BASE": {"rsi2_max": 14.0, "shock_z_max": -1.20, "vwap_z_max": -0.90, "vol_peak_min": 1.35, "close_pos_min": 0.55, "rv_rel_min": 0.75, "confirm": "HIGHER_LOW_UP"},
    "CAP_STRICT": {"rsi2_max": 10.0, "shock_z_max": -1.40, "vwap_z_max": -1.05, "vol_peak_min": 1.60, "close_pos_min": 0.60, "rv_rel_min": 0.85, "confirm": "PREV_HIGH_RECLAIM"},
    "FAILED_BREAK": {"rsi2_max": 14.0, "shock_z_max": -1.25, "vwap_z_max": -0.90, "vol_peak_min": 1.40, "close_pos_min": 0.62, "rv_rel_min": 0.75, "confirm": "FAILED_BREAK"},
    "DECEL_RECLAIM": {"rsi2_max": 16.0, "shock_z_max": -1.15, "vwap_z_max": -0.85, "vol_peak_min": 1.25, "close_pos_min": 0.58, "rv_rel_min": 0.80, "confirm": "DECEL_RECLAIM"},
}

EXIT_PROFILES = {
    "S04_T06_M10": (0.004, 0.006, 10),
    "S05_T07_M10": (0.005, 0.007, 10),
    "S06_T08_M12": (0.006, 0.008, 12),
    "S06_T08_M15": (0.006, 0.008, 15),
    "S07_T10_M15": (0.007, 0.010, 15),
    "S08_T12_M20": (0.008, 0.012, 20),
}

@dataclass
class CostScenario:
    name: str
    fee_side: float
    slip_side: float


def load_base():
    if not BASE_SRC.exists():
        raise SystemExit(f"MISSING_BASE={BASE_SRC}")
    spec = importlib.util.spec_from_file_location("fast_rebound_v001_base", BASE_SRC)
    if spec is None or spec.loader is None:
        raise SystemExit("BASE_IMPORT_SPEC_FAIL")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def minute_of_day(ts: pd.Series) -> pd.Series:
    return ts.dt.hour * 60 + ts.dt.minute


def cost_scenarios(base) -> list[CostScenario]:
    obs, _ = base.observed_commission()
    return [
        CostScenario("ACCOUNT_FEE_ONLY", obs, 0.0),
        CostScenario("ACCOUNT_PLUS_1BPS_SLIP", obs, 0.0001),
        CostScenario("ACCOUNT_PLUS_2BPS_SLIP", obs, 0.0002),
        CostScenario("ACCOUNT_PLUS_5BPS_SLIP", obs, 0.0005),
        CostScenario("FEE5BPS_PLUS_5BPS_SLIP", 0.0005, 0.0005),
        CostScenario("EXTREME_FEE10BPS_PLUS_5BPS_SLIP", 0.0010, 0.0005),
    ]


def source_features(base, day: pd.DataFrame) -> pd.DataFrame:
    x = base.source_features(day).copy().sort_values("ts").reset_index(drop=True)
    span = (x.high - x.low).replace(0.0, np.nan)
    x["close_pos"] = ((x.close - x.low) / span).clip(0.0, 1.0).fillna(0.5)
    x["ret3"] = x.close.pct_change(3)
    x["ret10"] = x.close.pct_change(10)
    x["ret15"] = x.close.pct_change(15)
    x["vol_peak3"] = x.volume_ratio.rolling(3, min_periods=1).max()
    x["rv20_med60"] = x.rv20.shift(1).rolling(60, min_periods=20).median()
    x["rv_rel"] = x.rv20 / x.rv20_med60.replace(0.0, np.nan)
    x["vwap_z_delta"] = x.vwap_z.diff()
    x["ret1_delta"] = x.ret1.diff()
    x["minute"] = minute_of_day(x.ts)
    return x


def signal_mask(x: pd.DataFrame, sigsym: str, spec: dict) -> pd.Series:
    p1 = x.shift(1)
    p2 = x.shift(2)
    rsi_min3 = x.rsi2.shift(1).rolling(3, min_periods=1).min()
    shock_min3 = x.shock_z.shift(1).rolling(3, min_periods=1).min()
    vwap_min3 = x.vwap_z.shift(1).rolling(3, min_periods=1).min()
    ret5_min3 = x.ret5.shift(1).rolling(3, min_periods=1).min()
    vol_peak3 = x.volume_ratio.shift(1).rolling(3, min_periods=1).max()
    shock_seen = (
        (rsi_min3 <= float(spec["rsi2_max"]))
        & (shock_min3 <= float(spec["shock_z_max"]))
        & (vwap_min3 <= float(spec["vwap_z_max"]))
        & (ret5_min3 <= -float(MIN_ABS_5M[sigsym]))
        & (vol_peak3 >= float(spec["vol_peak_min"]))
    )
    close_up = x.close > p1.close
    higher_low = x.low >= p1.low
    vwap_improve = x.vwap_z > p1.vwap_z
    close_quality = x.close_pos >= float(spec["close_pos_min"])
    rv_ok = x.rv_rel.fillna(1.0) >= float(spec["rv_rel_min"])
    decel = (x.ret1 > p1.ret1) & ((p1.ret1 < 0) | (p2.ret1 < 0))
    failed_break = (p1.low <= p2.low) & (x.low > p1.low) & (x.close > p1.close)
    kind = str(spec["confirm"])
    if kind == "HIGHER_LOW_UP":
        confirm = higher_low & close_up & vwap_improve
    elif kind == "PREV_HIGH_RECLAIM":
        confirm = (x.close > p1.high) & higher_low & vwap_improve
    elif kind == "FAILED_BREAK":
        confirm = failed_break & close_up & vwap_improve
    elif kind == "DECEL_RECLAIM":
        confirm = decel & higher_low & close_up & vwap_improve
    else:
        raise ValueError(kind)
    time_ok = (x.minute >= ENTRY_START_MIN) & (x.minute <= ENTRY_END_MIN)
    return (shock_seen & confirm & close_quality & rv_ok & time_ok).fillna(False)


def generate_pair_trades(base, sigsym: str, exesym: str, sig: pd.DataFrame, exe: pd.DataFrame) -> pd.DataFrame:
    sig_days = {d: g.copy().reset_index(drop=True) for d, g in sig.groupby("trade_date", sort=True)}
    exe_days = {d: g.copy().reset_index(drop=True) for d, g in exe.groupby("trade_date", sort=True)}
    rows = []
    common = sorted(set(sig_days) & set(exe_days))
    print(f"PAIR_START {sigsym}->{exesym} days={len(common)}", flush=True)
    for di, td in enumerate(common, 1):
        if di == 1 or di % 100 == 0 or di == len(common):
            print(f"PAIR_PROGRESS {sigsym}->{exesym} {di}/{len(common)} date={td}", flush=True)
        sx = source_features(base, sig_days[td])
        ex = exe_days[td].sort_values("ts").reset_index(drop=True)
        if len(sx) < 25 or len(ex) < 20:
            continue
        day_range = float(sx.high.max() / sx.low.min() - 1.0) if float(sx.low.min()) > 0 else np.nan
        masks = {name: signal_mask(sx, sigsym, spec) for name, spec in ENTRY_VARIANTS.items()}
        sx_utc = sx.ts.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
        for entry_name, mask in masks.items():
            idxs = list(np.flatnonzero(mask.to_numpy()))
            if not idxs:
                continue
            for exit_name, (stop_pct, tp_pct, max_hold) in EXIT_PROFILES.items():
                last_exit_ts = None
                trades_today = 0
                used_signal_until = -1
                for si in idxs:
                    if trades_today >= MAX_TRADES_PER_PAIR_DAY:
                        break
                    if si <= used_signal_until:
                        continue
                    sr = sx.iloc[si]
                    signal_ts = pd.Timestamp(sr.ts)
                    if last_exit_ts is not None and signal_ts < last_exit_ts + pd.Timedelta(minutes=COOLDOWN_MIN):
                        continue
                    entry_after = signal_ts + pd.Timedelta(minutes=1)
                    ei = base.next_exec_index(ex, entry_after)
                    if ei is None or pd.Timestamp(ex.iloc[ei].ts).date() != td:
                        continue
                    out = base.simulate_trade(ex, ei, stop_pct, tp_pct, max_hold)
                    if out is None:
                        continue
                    entry_ts = pd.Timestamp(ex.iloc[ei].ts)
                    entry_px = float(ex.iloc[ei].open)
                    exit_ts = pd.Timestamp(out["exit_ts"])
                    exit_px = float(out["exit_px"])
                    if exit_ts < entry_ts:
                        raise SystemExit("CAUSAL_EXIT_BEFORE_ENTRY")
                    p1 = sx.iloc[max(0, si - 1)]
                    rows.append({
                        "config": f"{entry_name}__{exit_name}", "entry_variant": entry_name, "exit_profile": exit_name,
                        "signal_symbol": sigsym, "exec_symbol": exesym, "trade_date": str(td),
                        "signal_ts": signal_ts.isoformat(), "entry_ts": entry_ts.isoformat(), "entry_px": entry_px,
                        "exit_ts": exit_ts.isoformat(), "exit_px": exit_px, "exit_reason": str(out["exit_reason"]),
                        "hold_min": float(out["hold_min"]), "gross_return": exit_px / entry_px - 1.0,
                        "source_day_range_pct": day_range * 100.0,
                        "rsi2_prev": float(p1.rsi2) if np.isfinite(p1.rsi2) else np.nan,
                        "shock_z_prev": float(p1.shock_z) if np.isfinite(p1.shock_z) else np.nan,
                        "vwap_z_prev": float(p1.vwap_z) if np.isfinite(p1.vwap_z) else np.nan,
                        "volume_ratio_prev": float(p1.volume_ratio) if np.isfinite(p1.volume_ratio) else np.nan,
                        "vol_peak3_prev": float(p1.vol_peak3) if np.isfinite(p1.vol_peak3) else np.nan,
                        "rv_rel_signal": float(sr.rv_rel) if np.isfinite(sr.rv_rel) else np.nan,
                        "close_pos_signal": float(sr.close_pos),
                        "ret15_signal_pct": float(sr.ret15) * 100.0 if np.isfinite(sr.ret15) else np.nan,
                        "vwap_z_delta_signal": float(sr.vwap_z_delta) if np.isfinite(sr.vwap_z_delta) else np.nan,
                    })
                    trades_today += 1
                    last_exit_ts = exit_ts
                    target = np.datetime64(exit_ts.tz_convert("UTC").tz_localize(None).to_datetime64())
                    used_signal_until = int(np.searchsorted(sx_utc, target, side="right") - 1)
    return pd.DataFrame(rows)


def apply_costs(base, tr: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for c in cost_scenarios(base):
        x = tr.copy()
        buy = x.entry_px.astype(float) * (1.0 + c.slip_side)
        sell = x.exit_px.astype(float) * (1.0 - c.slip_side)
        x["cost_scenario"] = c.name
        x["fee_side"] = c.fee_side
        x["slip_side"] = c.slip_side
        x["net_return"] = (sell * (1.0 - c.fee_side)) / (buy * (1.0 + c.fee_side)) - 1.0
        x["cost_drag_bps"] = (x.gross_return - x.net_return) * 10000.0
        parts.append(x)
    return pd.concat(parts, ignore_index=True)


def summarize(base, net: pd.DataFrame, calendar_days: list[str]) -> pd.DataFrame:
    rows = []
    for (cfg, cost), g in net.groupby(["config", "cost_scenario"], sort=True):
        row = {"config": cfg, "cost_scenario": cost}
        row.update(base.metrics(g.sort_values(["entry_ts", "exec_symbol"]), len(calendar_days)))
        row["entry_variant"] = str(g.entry_variant.iloc[0])
        row["exit_profile"] = str(g.exit_profile.iloc[0])
        rows.append(row)
    return pd.DataFrame(rows)


def grouped(g: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    rows = []
    for k, x in g.groupby(keys, dropna=False, sort=True):
        vals = k if isinstance(k, tuple) else (k,)
        r = x.net_return.astype(float)
        wins = r[r > 0]
        losses = r[r < 0]
        gp = float(wins.sum()) if len(wins) else 0.0
        gl = float(-losses.sum()) if len(losses) else 0.0
        row = dict(zip(keys, vals))
        row.update({"trades": int(len(x)), "win_rate": float((r > 0).mean()) if len(r) else np.nan, "expectancy_bps": float(r.mean() * 10000.0) if len(r) else np.nan, "profit_factor": gp / gl if gl > 0 else np.inf, "avg_hold_min": float(x.hold_min.mean()) if len(x) else np.nan})
        rows.append(row)
    return pd.DataFrame(rows)


def simple_summary(net: pd.DataFrame, cost_name: str) -> pd.DataFrame:
    return grouped(net[net.cost_scenario == cost_name], ["config"])


def main():
    base = load_base()
    OUT.mkdir(parents=True, exist_ok=True)
    obs_fee, fee_meta = base.observed_commission()
    print("FAST_REBOUND_V002_RESEARCH", flush=True)
    print(f"PERIOD={REQUESTED_START.date()}..{REQUESTED_END.date()}", flush=True)
    print("GOAL=FILTER_FALSE_WAVES_USING_CAPITULATION_EXHAUSTION", flush=True)
    print(f"ACCOUNT_COMMISSION_FRACTION={obs_fee:.12g}", flush=True)
    print(f"ACCOUNT_COMMISSION_META={json.dumps(fee_meta, default=str)}", flush=True)
    print("RANKING_COST=ACCOUNT_PLUS_2BPS_SLIP", flush=True)
    print("STRESS_COST=ACCOUNT_PLUS_5BPS_SLIP", flush=True)
    print("EXTREME_COST=FEE10BPS_PLUS_5BPS_SLIP_DIAGNOSTIC_ONLY", flush=True)
    print(f"COOLDOWN_MIN={COOLDOWN_MIN}", flush=True)
    print(f"MAX_TRADES_PER_PAIR_DAY={MAX_TRADES_PER_PAIR_DAY}", flush=True)
    print("HARD_STOP=YES_NEXT_1M_OPEN_AFTER_COMPLETED_TRIGGER", flush=True)
    print("ORDER_WRITES=OFF", flush=True)

    pair_frames = []
    all_calendar_days = set()
    for sigsym, exesym in base.PAIRS:
        print(f"LOAD_PAIR {sigsym}->{exesym}", flush=True)
        sig = base.load_symbol(sigsym)
        exe = base.load_symbol(exesym)
        sig = sig[(sig.ts.dt.date >= REQUESTED_START.date()) & (sig.ts.dt.date <= REQUESTED_END.date())].copy()
        exe = exe[(exe.ts.dt.date >= REQUESTED_START.date()) & (exe.ts.dt.date <= REQUESTED_END.date())].copy()
        all_calendar_days.update(str(d) for d in sorted(set(sig.trade_date) & set(exe.trade_date)))
        tr = generate_pair_trades(base, sigsym, exesym, sig, exe)
        print(f"PAIR_DONE {sigsym}->{exesym} gross_trade_rows={len(tr)}", flush=True)
        if len(tr):
            pair_frames.append(tr)
        del sig, exe
    if not pair_frames:
        raise SystemExit("NO_V002_TRADES")

    gross = pd.concat(pair_frames, ignore_index=True).sort_values(["config", "entry_ts", "exec_symbol"]).reset_index(drop=True)
    gross.to_csv(OUT / "trades_gross.csv", index=False)
    net = apply_costs(base, gross)
    net.to_csv(OUT / "trades_costed.csv", index=False)
    calendar_days = sorted(all_calendar_days)
    summary = summarize(base, net, calendar_days)
    summary.to_csv(OUT / "summary_all.csv", index=False)

    standard_name = "ACCOUNT_PLUS_2BPS_SLIP"
    stress_name = "ACCOUNT_PLUS_5BPS_SLIP"
    rank = summary[summary.cost_scenario == standard_name].copy()
    stress = simple_summary(net, stress_name).rename(columns={"profit_factor": "stress_pf", "expectancy_bps": "stress_expectancy_bps"})
    rank = rank.merge(stress[["config", "stress_pf", "stress_expectancy_bps"]], on="config", how="left")

    yr = grouped(net[net.cost_scenario == standard_name].assign(year=lambda x: pd.to_datetime(x.trade_date).dt.year), ["config", "year"])
    yr_stats = yr.groupby("config").agg(positive_years=("expectancy_bps", lambda s: int((s > 0).sum())), min_year_pf=("profit_factor", "min"), min_year_expectancy_bps=("expectancy_bps", "min")).reset_index()
    rank = rank.merge(yr_stats, on="config", how="left")
    rank["robust_pass"] = (rank.trades >= 250) & (rank.profit_factor >= 1.12) & (rank.expectancy_bps >= 1.5) & (rank.stress_pf >= 1.0) & (rank.stress_expectancy_bps > 0) & (rank.positive_years >= 2) & (rank.min_year_pf >= 0.85)
    rank["rank_score"] = rank.expectancy_bps.clip(-1000, 1000) * np.sqrt(rank.trades.clip(lower=1)) * np.minimum(rank.profit_factor.clip(0, 3), 3) * (1.0 + 0.15 * rank.positive_years.fillna(0))
    rank = rank.sort_values(["robust_pass", "rank_score", "trades"], ascending=[False, False, False]).reset_index(drop=True)
    rank.to_csv(OUT / "ranked_candidates.csv", index=False)
    yr.to_csv(OUT / "by_year_standard_cost.csv", index=False)

    pair_std = grouped(net[net.cost_scenario == standard_name], ["exec_symbol", "config"])
    pair_stress = grouped(net[net.cost_scenario == stress_name], ["exec_symbol", "config"])[["exec_symbol", "config", "profit_factor", "expectancy_bps"]].rename(columns={"profit_factor": "stress_pf", "expectancy_bps": "stress_expectancy_bps"})
    by_pair = pair_std.merge(pair_stress, on=["exec_symbol", "config"], how="left")
    by_pair["pair_candidate"] = (by_pair.trades >= 60) & (by_pair.profit_factor >= 1.10) & (by_pair.expectancy_bps > 1.0) & (by_pair.stress_pf >= 0.95)
    by_pair = by_pair.sort_values(["exec_symbol", "pair_candidate", "expectancy_bps", "trades"], ascending=[True, False, False, False]).reset_index(drop=True)
    by_pair.to_csv(OUT / "ranked_by_symbol.csv", index=False)

    top_cfg = str(rank.iloc[0].config)
    top = net[(net.config == top_cfg) & (net.cost_scenario == standard_name)].copy()
    if len(top) and top.rv_rel_signal.notna().sum() >= 10:
        q = top.rv_rel_signal.quantile([0.33, 0.66]).to_dict()
        top["local_vol_bucket"] = np.select([top.rv_rel_signal <= q.get(0.33, np.nan), top.rv_rel_signal <= q.get(0.66, np.nan)], ["LOW", "MID"], default="HIGH")
        grouped(top, ["local_vol_bucket"]).to_csv(OUT / "top_by_local_vol_bucket.csv", index=False)

    report = [
        "FAST_REBOUND_V002_RESEARCH",
        f"period={REQUESTED_START.date()}..{REQUESTED_END.date()}",
        "purpose=keep multi-wave behavior but reject weak/non-exhausted dips",
        f"account_commission_fraction_snapshot={obs_fee}",
        "ranking_cost=actual account fee + 2bps slippage per side",
        "stress_cost=actual account fee + 5bps slippage per side",
        "extreme 10bps fee stress=diagnostic only",
        "hard_stop_semantics=completed 1m trigger then next raw 1m open",
        "order_writes=OFF",
        "",
        "===== TOP 20 SYSTEM CANDIDATES =====",
    ]
    show = ["config", "trades", "win_rate", "profit_factor", "expectancy_bps", "avg_win", "avg_loss", "avg_hold_min", "active_days", "avg_trades_active_day", "p95_trades_day", "max_trades_day", "days_5_to_8_trades", "stress_pf", "stress_expectancy_bps", "positive_years", "min_year_pf", "robust_pass"]
    report.append(rank[show].head(20).to_string(index=False))
    report += ["", "===== TOP 5 PER EXEC SYMBOL ====="]
    for sym in sorted(by_pair.exec_symbol.unique()):
        report += [f"--- {sym} ---", by_pair[by_pair.exec_symbol == sym].head(5).to_string(index=False)]
    report += ["", f"TOP_CONFIG={top_cfg}", f"ROBUST_PASS_COUNT={int(rank.robust_pass.sum())}", "NOTE=research only; any positive candidate still needs walk-forward/OOS and capital replay before LIVE."]
    (OUT / "REPORT.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n" + "\n".join(report), flush=True)
    print(f"OUTPUT={OUT}", flush=True)
    print("FAST_REBOUND_V002=PASS", flush=True)


if __name__ == "__main__":
    main()
