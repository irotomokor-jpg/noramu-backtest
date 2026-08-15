#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

NY = "America/New_York"
DB = Path("toss_replay_cache/toss_1m.sqlite")
TRADES = Path("rsi_pullback_v004_long/trades_all.csv")
COMMISSION = Path("live/US_FROZEN_V1/commission_status.json")
OUT = Path("rsi_pullback_v005_exit_study")
VARIANTS = ["CURRENT", "TIME90_WEAK", "VWAP60_WEAK"]
LOCK = 0.015
TRAIL = 0.007
HARD_TP = 0.040
CUTOFF = "14:55"


def parse_ts(s):
    x = pd.to_datetime(s, errors="coerce", utc=True)
    if isinstance(x, pd.Series):
        return x.dt.tz_convert(NY)
    return x.tz_convert(NY)


def read_day(symbol: str, day: str) -> pd.DataFrame:
    start = pd.Timestamp(day)
    end = start + pd.Timedelta(days=1)
    con = sqlite3.connect(DB)
    q = "SELECT timestamp, open, high, low, close, volume FROM candles WHERE symbol=? AND timestamp>=? AND timestamp<? ORDER BY timestamp"
    d = pd.read_sql_query(q, con, params=[symbol, str(start.date()), str(end.date())])
    con.close()
    if d.empty:
        return d
    d["ts"] = parse_ts(d["timestamp"])
    for c in ["open", "high", "low", "close", "volume"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    mins = d.ts.dt.hour * 60 + d.ts.dt.minute
    d = d[(mins >= 570) & (mins < 960)].dropna(subset=["ts", "open", "high", "low", "close"])
    return d.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)


def bars5(sig: pd.DataFrame) -> pd.DataFrame:
    if sig.empty:
        return sig
    x = sig.set_index("ts")
    b = x.resample("5min", origin="start_day", offset="30min", label="right", closed="left").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum")
    ).dropna(subset=["open", "close"]).reset_index()
    typ = (b.high + b.low + b.close) / 3.0
    pv = typ * b.volume
    b["vwap"] = pv.cumsum() / b.volume.cumsum().replace(0, np.nan)
    return b


def next_open(x: pd.DataFrame, i: int, reason: str):
    if i + 1 < len(x):
        z = x.iloc[i + 1]
        return z.ts, float(z.open), reason
    z = x.iloc[i]
    return z.ts, float(z.close), reason + "_CLOSE"


def latest_sigbar(sig5: pd.DataFrame, ts: pd.Timestamp):
    if sig5.empty:
        return None
    z = sig5[sig5.ts <= ts]
    if z.empty:
        return None
    return z.iloc[-1]


def replay_exit(exe: pd.DataFrame, sig5: pd.DataFrame, entry_ts: pd.Timestamp, entry_px: float, variant: str):
    x = exe[exe.ts >= entry_ts].copy().reset_index(drop=True)
    if x.empty:
        return None
    cutoff = pd.Timestamp(f"{entry_ts.date()} {CUTOFF}", tz=NY)
    peak = float(entry_px)
    locked = False
    entry_time = entry_ts

    for i, r in x.iterrows():
        ts = r.ts
        if ts >= cutoff:
            return ts, float(r.open), "FRACTIONAL_CUTOFF_EXIT", locked

        # Same frozen STRICT_1M_CAUSAL exit ordering used by V004.
        if locked:
            trail_level = peak * (1.0 - TRAIL)
            if float(r.low) <= trail_level:
                z = next_open(x, i, "PROFIT_TRAIL")
                return z[0], z[1], z[2], locked

        if float(r.high) / entry_px - 1.0 >= HARD_TP:
            z = next_open(x, i, "HARD_TP")
            return z[0], z[1], z[2], locked

        peak = max(peak, float(r.high))
        if peak / entry_px - 1.0 >= LOCK:
            locked = True

        elapsed = (ts - entry_time).total_seconds() / 60.0

        if variant == "TIME90_WEAK" and elapsed >= 90.0 and (not locked) and float(r.close) <= entry_px:
            z = next_open(x, i, "TIME90_WEAK_EXIT")
            return z[0], z[1], z[2], locked

        if variant == "VWAP60_WEAK" and elapsed >= 60.0 and (not locked) and float(r.close) <= entry_px:
            sb = latest_sigbar(sig5, ts)
            if sb is not None and pd.notna(sb.vwap) and float(sb.close) < float(sb.vwap):
                z = next_open(x, i, "VWAP60_WEAK_EXIT")
                return z[0], z[1], z[2], locked

    r = x.iloc[-1]
    return r.ts, float(r.close), "SESSION_END", locked


def mae_mfe(exe: pd.DataFrame, entry_ts: pd.Timestamp, exit_ts: pd.Timestamp, entry_px: float):
    x = exe[(exe.ts >= entry_ts) & (exe.ts < exit_ts)]
    if x.empty:
        return np.nan, np.nan
    return float(x.low.min() / entry_px - 1.0), float(x.high.max() / entry_px - 1.0)


def summarize(g: pd.DataFrame) -> dict:
    r = g.net_return.astype(float)
    eq = (1.0 + r).cumprod()
    dd = eq / eq.cummax() - 1.0
    return {
        "trades": len(g),
        "win_rate": float((r > 0).mean()),
        "avg_return": float(r.mean()),
        "median_return": float(r.median()),
        "trade_seq_compounded_return": float(eq.iloc[-1] - 1.0),
        "max_drawdown_trade_seq": float(dd.min()),
        "worst_trade": float(r.min()),
        "best_trade": float(r.max()),
        "avg_mae": float(g.mae.mean()),
        "worst_mae": float(g.mae.min()),
        "avg_mfe": float(g.mfe.mean()),
        "avg_hold_min": float(g.hold_min.mean()),
        "cutoff_share": float((g.exit_reason == "FRACTIONAL_CUTOFF_EXIT").mean()),
    }


def grouped(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    rows = []
    for k, g in df.groupby(keys, dropna=False, sort=True):
        vals = k if isinstance(k, tuple) else (k,)
        row = dict(zip(keys, vals))
        row.update(summarize(g.sort_values(["trade_date", "entry_ts"])))
        rows.append(row)
    return pd.DataFrame(rows)


def main():
    if not DB.exists():
        raise SystemExit(f"DB_NOT_FOUND={DB}")
    if not TRADES.exists():
        raise SystemExit(f"TRADES_NOT_FOUND={TRADES}")
    cf = 0.0
    if COMMISSION.exists():
        j = json.loads(COMMISSION.read_text())
        cf = float(j.get("commissionFraction", 0) or 0)

    base = pd.read_csv(TRADES)
    base = base[base.variant == "DYN_2BAR"].copy()
    base["entry_ts_et"] = pd.to_datetime(base.entry_ts, utc=True).dt.tz_convert(NY)
    base = base.sort_values(["trade_date", "exec_symbol", "entry_ts_et"]).reset_index(drop=True)
    base["trade_id"] = np.arange(len(base))
    print(f"FROZEN_ENTRY_TRADES={len(base)} commission_fraction={cf}", flush=True)

    cache = {}
    rows = []
    for n, t in base.iterrows():
        day = str(t.trade_date)[:10]
        k1 = (t.exec_symbol, day)
        k2 = (t.signal_symbol, day)
        if k1 not in cache:
            cache[k1] = read_day(t.exec_symbol, day)
        if k2 not in cache:
            cache[k2] = read_day(t.signal_symbol, day)
        exe = cache[k1]
        sig = cache[k2]
        sig5 = bars5(sig)
        ets = t.entry_ts_et
        epx = float(t.entry_px)
        if exe.empty or sig.empty:
            print(f"SKIP missing {t.signal_symbol}->{t.exec_symbol} {day}", flush=True)
            continue
        for variant in VARIANTS:
            z = replay_exit(exe, sig5, ets, epx, variant)
            if z is None:
                continue
            xts, xpx, reason, locked = z
            net = (float(xpx) * (1.0 - cf)) / (epx * (1.0 + cf)) - 1.0
            mae, mfe = mae_mfe(exe, ets, xts, epx)
            rows.append({
                "trade_id": int(t.trade_id),
                "trade_date": day,
                "signal_symbol": t.signal_symbol,
                "exec_symbol": t.exec_symbol,
                "variant": variant,
                "entry_ts": ets.isoformat(),
                "entry_px": epx,
                "exit_ts": xts.isoformat(),
                "exit_px": float(xpx),
                "exit_reason": reason,
                "net_return": net,
                "mae": mae,
                "mfe": mfe,
                "hold_min": (xts - ets).total_seconds() / 60.0,
                "profit_lock_ever": bool(locked),
                "frozen_current_net": float(t.net_return),
                "frozen_current_reason": t.exit_reason,
            })
        if (n + 1) % 10 == 0 or n + 1 == len(base):
            print(f"REPLAY {n+1}/{len(base)}", flush=True)

    d = pd.DataFrame(rows)
    if d.empty:
        raise SystemExit("NO_EXIT_RESULTS")
    OUT.mkdir(parents=True, exist_ok=True)
    d.to_csv(OUT / "exit_trades.csv", index=False)

    current = d[d.variant == "CURRENT"].copy()
    audit = current.net_return.to_numpy() - current.frozen_current_net.to_numpy()
    audit_max = float(np.max(np.abs(audit))) if len(audit) else np.nan

    overall = grouped(d, ["variant"])
    by_symbol = grouped(d, ["variant", "exec_symbol"])
    d["year"] = pd.to_datetime(d.trade_date).dt.year
    by_year = grouped(d, ["variant", "year"])
    overall.to_csv(OUT / "summary_overall.csv", index=False)
    by_symbol.to_csv(OUT / "summary_by_symbol.csv", index=False)
    by_year.to_csv(OUT / "summary_by_year.csv", index=False)

    exits = d.groupby(["variant", "exit_reason"]).size().rename("trades").reset_index()
    exits["share"] = exits.trades / exits.groupby("variant").trades.transform("sum")
    exits.to_csv(OUT / "exit_reasons.csv", index=False)

    current_cutoff_ids = set(current.loc[current.exit_reason == "FRACTIONAL_CUTOFF_EXIT", "trade_id"].astype(int))
    cc = d[d.trade_id.isin(current_cutoff_ids)].copy()
    cutoff_compare = grouped(cc, ["variant"]) if len(cc) else pd.DataFrame()
    if len(cc):
        cur_map = current.set_index("trade_id").net_return
        cc["delta_vs_current"] = cc.apply(lambda r: float(r.net_return) - float(cur_map.loc[int(r.trade_id)]), axis=1)
        delta = cc.groupby("variant").agg(
            trades=("trade_id", "size"),
            avg_delta_vs_current=("delta_vs_current", "mean"),
            improved_share=("delta_vs_current", lambda s: float((s > 0).mean())),
            worsened_share=("delta_vs_current", lambda s: float((s < 0).mean())),
        ).reset_index()
    else:
        delta = pd.DataFrame()
    cutoff_compare.to_csv(OUT / "current_cutoff_subset.csv", index=False)
    delta.to_csv(OUT / "current_cutoff_delta.csv", index=False)

    report = [
        "RSI_PULLBACK_V005_EXIT_STUDY",
        "entry=V004_DYN_2BAR_FROZEN",
        "exit_current=LOCK_1.5_TRAIL_0.7_HARDTP_4.0_CUTOFF_14:55",
        "time90=after_90m + never_locked + exec_close<=entry -> next_1m_open",
        "vwap60=after_60m + never_locked + exec_close<=entry + signal_5m_close<vwap -> next_1m_open",
        f"commission_fraction={cf}",
        "capital_gains_tax=IGNORED",
        f"CURRENT_AUDIT_MAX_ABS_DIFF={audit_max:.12g}",
        "NOTE=trade_seq_compounded_return is a comparison metric, not the $200 portfolio return.",
        "",
        "===== OVERALL =====",
        overall.to_string(index=False),
        "",
        "===== BY SYMBOL =====",
        by_symbol.to_string(index=False),
        "",
        "===== BY YEAR =====",
        by_year.to_string(index=False),
        "",
        "===== EXIT REASONS =====",
        exits.to_string(index=False),
        "",
        "===== ORIGINAL CURRENT-CUTOFF TRADES ONLY =====",
        cutoff_compare.to_string(index=False) if len(cutoff_compare) else "NO_CURRENT_CUTOFF_TRADES",
        "",
        "===== DELTA ON CURRENT-CUTOFF TRADES =====",
        delta.to_string(index=False) if len(delta) else "NO_DELTA",
    ]
    (OUT / "EXIT_REPORT.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n" + "\n".join(report), flush=True)
    print(f"\nOUTPUT={OUT}", flush=True)


if __name__ == "__main__":
    main()
