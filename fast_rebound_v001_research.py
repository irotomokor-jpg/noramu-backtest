#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DB = ROOT / "toss_replay_cache" / "toss_1m.sqlite"
COMMISSION = ROOT / "live" / "US_FROZEN_V1" / "commission_status.json"
OUT = ROOT / "fast_rebound_v001"
NY = "America/New_York"

PAIRS = [("QQQ", "TQQQ"), ("SPY", "UPRO"), ("SOXX", "SOXL"), ("EWY", "KORU")]
REQUESTED_START = pd.Timestamp("2024-01-01")
REQUESTED_END = pd.Timestamp("2026-08-14")
ENTRY_START_MIN = 9 * 60 + 40
ENTRY_END_MIN = 14 * 60 + 30
FORCE_EXIT_MIN = 14 * 60 + 55
COOLDOWN_MIN = 8
MAX_TRADES_PER_PAIR_DAY = 3

MIN_ABS_5M = {
    "SPY": 0.0015,
    "QQQ": 0.0020,
    "SOXX": 0.0035,
    "EWY": 0.0025,
}

ENTRY_VARIANTS = {
    "WAVE_FAST": {
        "rsi2_max": 18.0,
        "shock_z_max": -1.10,
        "vwap_z_max": -0.80,
        "reclaim": "CLOSE_UP",
    },
    "WAVE_BASE": {
        "rsi2_max": 12.0,
        "shock_z_max": -1.30,
        "vwap_z_max": -1.00,
        "reclaim": "HIGHER_LOW_CLOSE_UP",
    },
    "WAVE_STRICT": {
        "rsi2_max": 8.0,
        "shock_z_max": -1.50,
        "vwap_z_max": -1.20,
        "reclaim": "PREV_HIGH_RECLAIM",
    },
}

EXIT_PROFILES = {
    "S06_T08_M15": (0.006, 0.008, 15),
    "S08_T10_M20": (0.008, 0.010, 20),
    "S08_T12_M30": (0.008, 0.012, 30),
    "S10_T12_M30": (0.010, 0.012, 30),
    "S10_T15_M45": (0.010, 0.015, 45),
    "S12_T15_M45": (0.012, 0.015, 45),
}


@dataclass
class CostScenario:
    name: str
    fee_side: float
    slip_side: float


def parse_ts(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce", utc=True).dt.tz_convert(NY)


def minute_of_day(ts: pd.Series) -> pd.Series:
    return ts.dt.hour * 60 + ts.dt.minute


def rsi_wilder(close: pd.Series, n: int = 2) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d.clip(upper=0.0))
    au = up.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = au / ad.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(ad > 0.0, 100.0)
    out = out.where(au > 0.0, 0.0)
    return out


def observed_commission() -> tuple[float, dict]:
    if not COMMISSION.exists():
        return 1e-5, {"source": "fallback", "commissionFraction": 1e-5}
    try:
        j = json.loads(COMMISSION.read_text(encoding="utf-8"))
        frac = float(j.get("commissionFraction", 0.0) or 0.0)
        if frac < 0:
            frac = 0.0
        j = {"source": str(COMMISSION), **j}
        return frac, j
    except Exception as e:
        return 1e-5, {"source": "fallback_after_parse_error", "error": str(e), "commissionFraction": 1e-5}


def cost_scenarios() -> list[CostScenario]:
    obs, _ = observed_commission()
    return [
        CostScenario("ACCOUNT_FEE_ONLY", obs, 0.0),
        CostScenario("ACCOUNT_PLUS_2BPS_SLIP", obs, 0.0002),
        CostScenario("FEE5BPS_PLUS_2BPS_SLIP", 0.0005, 0.0002),
        CostScenario("FEE10BPS_PLUS_2BPS_SLIP", 0.0010, 0.0002),
        CostScenario("STRESS_FEE10BPS_PLUS_5BPS_SLIP", 0.0010, 0.0005),
    ]


def load_symbol(symbol: str) -> pd.DataFrame:
    if not DB.exists():
        raise SystemExit(f"DB_NOT_FOUND={DB}")
    start = (REQUESTED_START - pd.Timedelta(days=3)).date().isoformat()
    end = (REQUESTED_END + pd.Timedelta(days=2)).date().isoformat()
    with sqlite3.connect(DB) as con:
        d = pd.read_sql_query(
            "SELECT timestamp,open,high,low,close,volume FROM candles "
            "WHERE symbol=? AND timestamp>=? AND timestamp<? ORDER BY timestamp",
            con,
            params=(symbol, start, end),
        )
    if d.empty:
        raise SystemExit(f"NO_DATA={symbol}")
    d["ts"] = parse_ts(d["timestamp"])
    for c in ["open", "high", "low", "close", "volume"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["ts", "open", "high", "low", "close"]).copy()
    d = d.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)
    mins = minute_of_day(d.ts)
    d = d[(mins >= 9 * 60 + 30) & (mins < 16 * 60)].copy()
    d["trade_date"] = d.ts.dt.date
    return d.reset_index(drop=True)


def source_features(day: pd.DataFrame) -> pd.DataFrame:
    x = day.copy().sort_values("ts").reset_index(drop=True)
    x["ret1"] = x.close.pct_change()
    x["ret5"] = x.close.pct_change(5)
    x["rv20"] = x.ret1.rolling(20, min_periods=10).std(ddof=0)
    x["shock_z"] = x.ret5 / (x.rv20 * math.sqrt(5.0) + 1e-12)
    tp = (x.high + x.low + x.close) / 3.0
    vol = x.volume.fillna(0.0).clip(lower=0.0)
    cumv = vol.cumsum()
    x["vwap"] = (tp * vol).cumsum() / cumv.replace(0.0, np.nan)
    x["vwap_dev"] = x.close / x.vwap - 1.0
    mu = x.vwap_dev.rolling(30, min_periods=10).mean()
    sd = x.vwap_dev.rolling(30, min_periods=10).std(ddof=0)
    x["vwap_z"] = (x.vwap_dev - mu) / (sd + 1e-12)
    x["rsi2"] = rsi_wilder(x.close, 2)
    medv = x.volume.shift(1).rolling(20, min_periods=8).median()
    x["volume_ratio"] = x.volume / medv.replace(0.0, np.nan)
    x["minute"] = minute_of_day(x.ts)
    return x


def signal_mask(x: pd.DataFrame, sigsym: str, spec: dict) -> pd.Series:
    prev = x.shift(1)
    oversold = (
        (prev.rsi2 <= float(spec["rsi2_max"]))
        & (prev.shock_z <= float(spec["shock_z_max"]))
        & (prev.vwap_z <= float(spec["vwap_z_max"]))
        & (prev.ret5 <= -float(MIN_ABS_5M[sigsym]))
    )
    kind = spec["reclaim"]
    if kind == "CLOSE_UP":
        reclaim = x.close > prev.close
    elif kind == "HIGHER_LOW_CLOSE_UP":
        reclaim = (x.close > prev.close) & (x.low >= prev.low)
    elif kind == "PREV_HIGH_RECLAIM":
        reclaim = (x.close > prev.high) & (x.low >= prev.low)
    else:
        raise ValueError(kind)
    time_ok = (x.minute >= ENTRY_START_MIN) & (x.minute <= ENTRY_END_MIN)
    return (oversold & reclaim & time_ok).fillna(False)


def next_exec_index(exe: pd.DataFrame, after_ts: pd.Timestamp) -> int | None:
    arr = exe.ts.to_numpy()
    target = np.datetime64(after_ts.tz_convert("UTC").tz_localize(None).to_datetime64())
    arr_utc = exe.ts.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
    i = int(np.searchsorted(arr_utc, target, side="left"))
    return None if i >= len(exe) else i


def simulate_trade(exe: pd.DataFrame, entry_i: int, stop_pct: float, tp_pct: float, max_hold: int) -> dict | None:
    if entry_i >= len(exe):
        return None
    er = exe.iloc[entry_i]
    entry_ts = pd.Timestamp(er.ts)
    entry_px = float(er.open)
    if not np.isfinite(entry_px) or entry_px <= 0:
        return None
    stop_level = entry_px * (1.0 - stop_pct)
    tp_level = entry_px * (1.0 + tp_pct)
    cutoff = pd.Timestamp(f"{entry_ts.date()} 14:55", tz=NY)
    time_exit_ts = entry_ts + pd.Timedelta(minutes=max_hold)

    # If a deterministic time/cutoff boundary arrives before any completed-bar trigger,
    # exit at the first raw open at or after that boundary.
    for j in range(entry_i, len(exe)):
        r = exe.iloc[j]
        ts = pd.Timestamp(r.ts)
        if ts >= cutoff:
            return {
                "exit_i": j,
                "exit_ts": ts,
                "exit_px": float(r.open),
                "exit_reason": "CUTOFF",
                "hold_min": max(0.0, (ts - entry_ts).total_seconds() / 60.0),
            }
        if ts >= time_exit_ts:
            return {
                "exit_i": j,
                "exit_ts": ts,
                "exit_px": float(r.open),
                "exit_reason": "TIME",
                "hold_min": max(0.0, (ts - entry_ts).total_seconds() / 60.0),
            }

        # This bar is only actionable after completion; execute on next raw minute open.
        stop_hit = float(r.low) <= stop_level
        tp_hit = float(r.high) >= tp_level
        if stop_hit or tp_hit:
            reason = "STOP" if stop_hit else "TP"  # conservative if both occur in one bar
            if j + 1 < len(exe):
                rr = exe.iloc[j + 1]
                nts = pd.Timestamp(rr.ts)
                if nts.date() == entry_ts.date() and nts <= cutoff:
                    return {
                        "exit_i": j + 1,
                        "exit_ts": nts,
                        "exit_px": float(rr.open),
                        "exit_reason": reason,
                        "hold_min": max(0.0, (nts - entry_ts).total_seconds() / 60.0),
                    }
            return {
                "exit_i": j,
                "exit_ts": ts,
                "exit_px": float(r.close),
                "exit_reason": reason + "_NO_NEXT_OPEN",
                "hold_min": max(0.0, (ts - entry_ts).total_seconds() / 60.0),
            }
    return None


def generate_pair_trades(sigsym: str, exesym: str, sig: pd.DataFrame, exe: pd.DataFrame) -> pd.DataFrame:
    sig_days = {d: g.copy().reset_index(drop=True) for d, g in sig.groupby("trade_date", sort=True)}
    exe_days = {d: g.copy().reset_index(drop=True) for d, g in exe.groupby("trade_date", sort=True)}
    rows = []
    common = sorted(set(sig_days) & set(exe_days))
    print(f"PAIR_START {sigsym}->{exesym} days={len(common)}", flush=True)

    for di, td in enumerate(common, 1):
        if di == 1 or di % 100 == 0 or di == len(common):
            print(f"PAIR_PROGRESS {sigsym}->{exesym} {di}/{len(common)} date={td}", flush=True)
        sx = source_features(sig_days[td])
        ex = exe_days[td].sort_values("ts").reset_index(drop=True)
        if len(sx) < 20 or len(ex) < 20:
            continue
        day_range = float(sx.high.max() / sx.low.min() - 1.0) if float(sx.low.min()) > 0 else np.nan

        masks = {name: signal_mask(sx, sigsym, spec) for name, spec in ENTRY_VARIANTS.items()}
        signal_indices = {name: list(np.flatnonzero(mask.to_numpy())) for name, mask in masks.items()}

        for entry_name, idxs in signal_indices.items():
            if not idxs:
                continue
            for exit_name, (stop_pct, tp_pct, max_hold) in EXIT_PROFILES.items():
                last_exit_ts = None
                trades_today = 0
                used_signal_until = -1
                for si in idxs:
                    if trades_today >= MAX_TRADES_PER_PAIR_DAY:
                        break
                    sr = sx.iloc[si]
                    signal_ts = pd.Timestamp(sr.ts)
                    if si <= used_signal_until:
                        continue
                    if last_exit_ts is not None and signal_ts < last_exit_ts + pd.Timedelta(minutes=COOLDOWN_MIN):
                        continue
                    entry_after = signal_ts + pd.Timedelta(minutes=1)
                    ei = next_exec_index(ex, entry_after)
                    if ei is None:
                        continue
                    if pd.Timestamp(ex.iloc[ei].ts).date() != td:
                        continue
                    out = simulate_trade(ex, ei, stop_pct, tp_pct, max_hold)
                    if out is None:
                        continue
                    entry_ts = pd.Timestamp(ex.iloc[ei].ts)
                    entry_px = float(ex.iloc[ei].open)
                    exit_ts = pd.Timestamp(out["exit_ts"])
                    exit_px = float(out["exit_px"])
                    if exit_ts < entry_ts:
                        raise SystemExit("CAUSAL_EXIT_BEFORE_ENTRY")
                    gross = exit_px / entry_px - 1.0
                    cfg = f"{entry_name}__{exit_name}"
                    rows.append({
                        "config": cfg,
                        "entry_variant": entry_name,
                        "exit_profile": exit_name,
                        "signal_symbol": sigsym,
                        "exec_symbol": exesym,
                        "trade_date": str(td),
                        "signal_ts": signal_ts.isoformat(),
                        "entry_ts": entry_ts.isoformat(),
                        "entry_px": entry_px,
                        "exit_ts": exit_ts.isoformat(),
                        "exit_px": exit_px,
                        "exit_reason": out["exit_reason"],
                        "hold_min": float(out["hold_min"]),
                        "gross_return": gross,
                        "source_day_range_pct": day_range * 100.0,
                        "rsi2_prev": float(sx.iloc[si - 1].rsi2) if si > 0 else np.nan,
                        "shock_z_prev": float(sx.iloc[si - 1].shock_z) if si > 0 else np.nan,
                        "vwap_z_prev": float(sx.iloc[si - 1].vwap_z) if si > 0 else np.nan,
                        "source_ret5_prev_pct": float(sx.iloc[si - 1].ret5) * 100.0 if si > 0 else np.nan,
                    })
                    trades_today += 1
                    last_exit_ts = exit_ts
                    # Do not reuse signals that occurred while the position was open.
                    used_signal_until = int(np.searchsorted(sx.ts.to_numpy(), np.datetime64(exit_ts.tz_convert("UTC").tz_localize(None).to_datetime64()), side="right") - 1)
    return pd.DataFrame(rows)


def apply_costs(tr: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for c in cost_scenarios():
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


def maxdd_trade_sequence(r: pd.Series) -> float:
    if len(r) == 0:
        return np.nan
    eq = (1.0 + r.astype(float)).cumprod()
    dd = eq / eq.cummax() - 1.0
    return float(dd.min())


def metrics(g: pd.DataFrame, all_days: int) -> dict:
    r = g.net_return.astype(float)
    wins = r[r > 0]
    losses = r[r < 0]
    gp = float(wins.sum()) if len(wins) else 0.0
    gl = float(-losses.sum()) if len(losses) else 0.0
    pf = gp / gl if gl > 0 else np.inf
    counts = g.groupby("trade_date").size()
    active_days = int(counts.size)
    q = g.source_day_range_pct.quantile([0.33, 0.66]).to_dict() if len(g) else {0.33: np.nan, 0.66: np.nan}
    high_cut = float(q.get(0.66, np.nan))
    high_share = float((g.source_day_range_pct >= high_cut).mean()) if len(g) and np.isfinite(high_cut) else np.nan
    reasons = g.exit_reason.astype(str)
    return {
        "trades": int(len(g)),
        "win_rate": float((r > 0).mean()) if len(r) else np.nan,
        "avg_net_return": float(r.mean()) if len(r) else np.nan,
        "median_net_return": float(r.median()) if len(r) else np.nan,
        "avg_win": float(wins.mean()) if len(wins) else np.nan,
        "avg_loss": float(losses.mean()) if len(losses) else np.nan,
        "profit_factor": pf,
        "expectancy_bps": float(r.mean() * 10000.0) if len(r) else np.nan,
        "trade_seq_maxdd_pct": maxdd_trade_sequence(r) * 100.0,
        "avg_hold_min": float(g.hold_min.mean()) if len(g) else np.nan,
        "median_hold_min": float(g.hold_min.median()) if len(g) else np.nan,
        "active_days": active_days,
        "zero_trade_day_pct": float((all_days - active_days) / all_days * 100.0) if all_days else np.nan,
        "avg_trades_active_day": float(counts.mean()) if active_days else 0.0,
        "p95_trades_day": float(counts.quantile(0.95)) if active_days else 0.0,
        "max_trades_day": int(counts.max()) if active_days else 0,
        "days_5_to_8_trades": int(((counts >= 5) & (counts <= 8)).sum()) if active_days else 0,
        "days_ge_5_trades": int((counts >= 5).sum()) if active_days else 0,
        "stop_share": float(reasons.str.startswith("STOP").mean()) if len(g) else np.nan,
        "tp_share": float(reasons.str.startswith("TP").mean()) if len(g) else np.nan,
        "time_share": float((reasons == "TIME").mean()) if len(g) else np.nan,
        "cutoff_share": float((reasons == "CUTOFF").mean()) if len(g) else np.nan,
        "high_source_range_trade_share": high_share,
        "avg_cost_drag_bps": float(g.cost_drag_bps.mean()) if len(g) else np.nan,
    }


def summarize(net: pd.DataFrame, calendar_days: list[str]) -> pd.DataFrame:
    rows = []
    nday = len(calendar_days)
    for (cfg, cost), g in net.groupby(["config", "cost_scenario"], sort=True):
        row = {"config": cfg, "cost_scenario": cost}
        row.update(metrics(g.sort_values(["entry_ts", "exec_symbol"]), nday))
        row["entry_variant"] = str(g.entry_variant.iloc[0])
        row["exit_profile"] = str(g.exit_profile.iloc[0])
        rows.append(row)
    return pd.DataFrame(rows)


def system_daily_counts(net: pd.DataFrame, config: str, cost: str, calendar_days: list[str]) -> pd.DataFrame:
    z = net[(net.config == config) & (net.cost_scenario == cost)].copy()
    counts = z.groupby("trade_date").size().rename("trades").reset_index()
    base = pd.DataFrame({"trade_date": calendar_days})
    out = base.merge(counts, how="left", on="trade_date").fillna({"trades": 0})
    out["trades"] = out.trades.astype(int)
    return out


def group_metrics(g: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    rows = []
    for k, x in g.groupby(keys, dropna=False, sort=True):
        vals = k if isinstance(k, tuple) else (k,)
        r = x.net_return.astype(float)
        wins = r[r > 0]
        losses = r[r < 0]
        gp = float(wins.sum()) if len(wins) else 0.0
        gl = float(-losses.sum()) if len(losses) else 0.0
        row = dict(zip(keys, vals))
        row.update({
            "trades": int(len(x)),
            "win_rate": float((r > 0).mean()) if len(r) else np.nan,
            "avg_net_return": float(r.mean()) if len(r) else np.nan,
            "expectancy_bps": float(r.mean() * 10000.0) if len(r) else np.nan,
            "profit_factor": gp / gl if gl > 0 else np.inf,
            "avg_hold_min": float(x.hold_min.mean()) if len(x) else np.nan,
        })
        rows.append(row)
    return pd.DataFrame(rows)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    obs_fee, fee_meta = observed_commission()
    print("FAST_REBOUND_V001_RESEARCH", flush=True)
    print(f"PERIOD={REQUESTED_START.date()}..{REQUESTED_END.date()}", flush=True)
    print("MODE=REGULAR_SESSION_WAVE_REBOUND", flush=True)
    print("SIGNAL_SOURCE=QQQ,SPY,SOXX,EWY", flush=True)
    print("EXECUTION=TQQQ,UPRO,SOXL,KORU", flush=True)
    print(f"ACCOUNT_COMMISSION_FRACTION={obs_fee:.12g}", flush=True)
    print(f"ACCOUNT_COMMISSION_META={json.dumps(fee_meta, default=str)}", flush=True)
    print("COST_SENSITIVITY=ACCOUNT,FEE5BPS,FEE10BPS,SLIPPAGE_STRESS", flush=True)
    print(f"COOLDOWN_MIN={COOLDOWN_MIN}", flush=True)
    print(f"MAX_TRADES_PER_PAIR_DAY={MAX_TRADES_PER_PAIR_DAY}", flush=True)
    print("HARD_STOP=YES", flush=True)
    print("ORDER_WRITES=OFF", flush=True)

    pair_frames = []
    all_calendar_days = set()
    qqq_daily_range = None

    for sigsym, exesym in PAIRS:
        print(f"LOAD_PAIR {sigsym}->{exesym}", flush=True)
        sig = load_symbol(sigsym)
        exe = load_symbol(exesym)
        sig = sig[(sig.ts.dt.date >= REQUESTED_START.date()) & (sig.ts.dt.date <= REQUESTED_END.date())].copy()
        exe = exe[(exe.ts.dt.date >= REQUESTED_START.date()) & (exe.ts.dt.date <= REQUESTED_END.date())].copy()
        all_calendar_days.update(str(d) for d in sorted(set(sig.trade_date) & set(exe.trade_date)))
        if sigsym == "QQQ":
            qqq_daily_range = sig.groupby("trade_date").apply(lambda g: float(g.high.max() / g.low.min() - 1.0), include_groups=False).rename("qqq_range").reset_index()
            qqq_daily_range["trade_date"] = qqq_daily_range.trade_date.astype(str)
        tr = generate_pair_trades(sigsym, exesym, sig, exe)
        print(f"PAIR_DONE {sigsym}->{exesym} gross_trade_rows={len(tr)}", flush=True)
        if len(tr):
            pair_frames.append(tr)
        del sig, exe

    if not pair_frames:
        raise SystemExit("NO_FAST_REBOUND_TRADES")
    gross = pd.concat(pair_frames, ignore_index=True)
    gross = gross.sort_values(["config", "entry_ts", "exec_symbol"]).reset_index(drop=True)
    gross.to_csv(OUT / "trades_gross.csv", index=False)

    net = apply_costs(gross)
    net.to_csv(OUT / "trades_costed.csv", index=False)
    calendar_days = sorted(all_calendar_days)
    summary = summarize(net, calendar_days)
    summary.to_csv(OUT / "summary_all.csv", index=False)

    standard_name = "FEE10BPS_PLUS_2BPS_SLIP"
    stress_name = "STRESS_FEE10BPS_PLUS_5BPS_SLIP"
    std = summary[summary.cost_scenario == standard_name].copy()
    stress = summary[summary.cost_scenario == stress_name][["config", "profit_factor", "expectancy_bps"]].copy()
    stress = stress.rename(columns={"profit_factor": "stress_pf", "expectancy_bps": "stress_expectancy_bps"})
    rank = std.merge(stress, on="config", how="left")
    rank["robust_pass"] = (
        (rank.trades >= 100)
        & (rank.profit_factor >= 1.20)
        & (rank.expectancy_bps > 0)
        & (rank.stress_pf >= 1.05)
        & (rank.stress_expectancy_bps > 0)
    )
    rank["rank_score"] = (
        rank.expectancy_bps.clip(lower=-1000, upper=1000)
        * np.sqrt(rank.trades.clip(lower=1))
        * np.minimum(rank.profit_factor.clip(lower=0, upper=3), 3.0)
    )
    rank = rank.sort_values(["robust_pass", "rank_score", "trades"], ascending=[False, False, False]).reset_index(drop=True)
    rank.to_csv(OUT / "ranked_candidates.csv", index=False)

    best_cfg = str(rank.iloc[0].config)
    print(f"TOP_CONFIG_STANDARD_COST={best_cfg}", flush=True)
    top = net[net.config == best_cfg].copy()
    top_std = top[top.cost_scenario == standard_name].copy()
    top_std["year"] = pd.to_datetime(top_std.trade_date).dt.year

    by_symbol = group_metrics(top_std, ["exec_symbol"])
    by_year = group_metrics(top_std, ["year"])
    by_symbol.to_csv(OUT / "top_by_symbol.csv", index=False)
    by_year.to_csv(OUT / "top_by_year.csv", index=False)

    if qqq_daily_range is not None and len(qqq_daily_range):
        q = qqq_daily_range.qqq_range.quantile([0.33, 0.66]).to_dict()
        qqq_daily_range["market_vol_bucket"] = np.select(
            [qqq_daily_range.qqq_range <= q[0.33], qqq_daily_range.qqq_range <= q[0.66]],
            ["LOW", "MID"],
            default="HIGH",
        )
        qqq_daily_range.to_csv(OUT / "qqq_daily_vol_bucket.csv", index=False)
        topv = top_std.merge(qqq_daily_range[["trade_date", "market_vol_bucket", "qqq_range"]], on="trade_date", how="left")
        by_vol = group_metrics(topv.dropna(subset=["market_vol_bucket"]), ["market_vol_bucket"])
        by_vol.to_csv(OUT / "top_by_market_vol_bucket.csv", index=False)

    daily = system_daily_counts(net, best_cfg, standard_name, calendar_days)
    if qqq_daily_range is not None:
        daily = daily.merge(qqq_daily_range, on="trade_date", how="left")
    daily.to_csv(OUT / "top_daily_trade_counts.csv", index=False)

    cost_view = summary[summary.config == best_cfg].copy().sort_values("cost_scenario")
    cost_view.to_csv(OUT / "top_cost_sensitivity.csv", index=False)

    report = []
    report.append("FAST_REBOUND_V001_RESEARCH")
    report.append(f"period={REQUESTED_START.date()}..{REQUESTED_END.date()}")
    report.append("goal=multiple short rebound waves on volatile days; quiet days may have zero trades")
    report.append("entry=1m oversold shock on source ETF, then causal reclaim; execution=next leveraged ETF 1m open")
    report.append("exit=hard stop / fixed TP / time stop / 14:55 ET cutoff; trigger on completed 1m bar then next raw open")
    report.append(f"cooldown_min={COOLDOWN_MIN}; max_trades_per_pair_day={MAX_TRADES_PER_PAIR_DAY}")
    report.append("capital_gains_tax=IGNORED")
    report.append(f"account_commission_fraction_snapshot={obs_fee:.12g}")
    report.append("ranking_cost=10bps fee per side + 2bps slippage per side")
    report.append("stress_cost=10bps fee per side + 5bps slippage per side")
    report.append("order_writes=OFF")
    report.append("")
    report.append("===== TOP 15 STANDARD-COST CANDIDATES =====")
    show_cols = [
        "config", "trades", "win_rate", "profit_factor", "expectancy_bps", "avg_win", "avg_loss",
        "avg_hold_min", "active_days", "zero_trade_day_pct", "avg_trades_active_day", "p95_trades_day",
        "max_trades_day", "days_5_to_8_trades", "days_ge_5_trades", "stress_pf", "stress_expectancy_bps", "robust_pass"
    ]
    report.append(rank[show_cols].head(15).to_string(index=False))
    report.append("")
    report.append(f"===== TOP CONFIG COST SENSITIVITY: {best_cfg} =====")
    report.append(cost_view[["cost_scenario", "trades", "win_rate", "profit_factor", "expectancy_bps", "avg_cost_drag_bps", "trade_seq_maxdd_pct"]].to_string(index=False))
    report.append("")
    report.append("===== TOP CONFIG BY SYMBOL / STANDARD COST =====")
    report.append(by_symbol.to_string(index=False))
    report.append("")
    report.append("===== TOP CONFIG BY YEAR / STANDARD COST =====")
    report.append(by_year.to_string(index=False))
    report.append("")
    report.append("NOTE=V001 is a coarse benchmark, not a frozen strategy. V002 should refine only broad stable regions, not the single best point.")
    text = "\n".join(report) + "\n"
    (OUT / "REPORT.txt").write_text(text, encoding="utf-8")
    print(text, end="", flush=True)
    print(f"OUTPUT={OUT}", flush=True)
    print("FAST_REBOUND_V001=PASS", flush=True)


if __name__ == "__main__":
    main()
