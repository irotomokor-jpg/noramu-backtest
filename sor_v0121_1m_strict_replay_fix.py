from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

import sor_v012_1m_strict_replay as v12
from sor_exit_069500_v001 import net_long_return, stop_fill, target_fill
import sor_entry_v004_breakout as v4


OUTDIR = Path("sor_v0121_1m_strict_replay_fix_output")


def strict_replay_e1_fixed(trade: pd.Series, setup: pd.DataFrame, minute: pd.DataFrame) -> dict:
    ticker = str(trade["ticker"])
    daily_entry = float(trade["entry_price"])
    initial_stop = float(trade["initial_stop"])
    entry_date = pd.Timestamp(trade["entry_time"]).date()
    daily_exit_date = pd.Timestamp(trade["exit_time"]).date()
    daily_exit_reason = str(trade["exit_reason"])

    out = {
        "ticker": ticker,
        "daily_entry_time": trade["entry_time"],
        "daily_exit_time": trade["exit_time"],
        "daily_entry_price": daily_entry,
        "initial_stop": initial_stop,
        "daily_return_pct": float(trade["return_pct"]),
        "daily_r_multiple": float(trade["r_multiple"]),
        "daily_tp1_hit": bool(trade["tp1_hit"]),
        "daily_exit_reason": daily_exit_reason,
        "audit_status": "",
        "minute_entry_time": pd.NaT,
        "minute_exit_time": pd.NaT,
        "minute_entry_price": np.nan,
        "minute_exit_price_weighted": np.nan,
        "minute_return_pct": np.nan,
        "minute_r_multiple": np.nan,
        "minute_tp1_hit": False,
        "minute_exit_reason": "",
        "entry_slippage_vs_daily_open_bps": np.nan,
        "return_delta_vs_daily_pctpt": np.nan,
        "exit_date_match": False,
        "tp1_match": False,
        "ambiguous_stop_vs_tp_count": 0,
        "ambiguous_tp_then_be_count": 0,
    }

    if minute.empty:
        out["audit_status"] = "no_minute_data_ticker"
        return out

    entry_bars = minute[np.array([ts.date() == entry_date for ts in minute.index])]
    if entry_bars.empty:
        out["audit_status"] = "no_minute_data_entry_date"
        return out

    first = entry_bars.iloc[0]
    entry_time = entry_bars.index[0]
    entry = float(first["Open"])
    if not np.isfinite(entry) or entry <= initial_stop:
        out["audit_status"] = "invalid_strict_entry_vs_stop"
        return out

    risk = entry - initial_stop
    target = entry + v4.RR_TARGET * risk
    active_stop = initial_stop
    tp1_hit = False
    first_exit_px: float | None = None
    final_exit_px: float | None = None
    final_exit_time = None
    reason = None
    pending_trend_exit = False

    out["minute_entry_time"] = entry_time
    out["minute_entry_price"] = entry
    out["entry_slippage_vs_daily_open_bps"] = 10000.0 * (entry / daily_entry - 1.0)

    daily_map = v12._daily_by_date(setup)
    m = minute[minute.index >= entry_time].copy()
    if m.empty:
        out["audit_status"] = "no_minutes_after_entry"
        return out

    for d, daybars in m.groupby(m.index.date, sort=True):
        if pending_trend_exit:
            o = float(daybars.iloc[0]["Open"])
            final_exit_px = o
            final_exit_time = daybars.index[0]
            reason = "trend_off_next_open_1m"
            break

        for ts, bar in daybars.iterrows():
            o = float(bar["Open"])
            h = float(bar["High"])
            l = float(bar["Low"])

            if not tp1_hit:
                stop_touch = l <= active_stop
                target_touch = h >= target

                if stop_touch and target_touch:
                    out["ambiguous_stop_vs_tp_count"] += 1
                    final_exit_px = stop_fill(o, active_stop)
                    final_exit_time = ts
                    reason = "ambiguous_1m_stop_first"
                    break

                if stop_touch:
                    final_exit_px = stop_fill(o, active_stop)
                    final_exit_time = ts
                    reason = "initial_stop_1m"
                    break

                if target_touch:
                    tp1_hit = True
                    first_exit_px = target_fill(o, target)
                    active_stop = entry
                    if l <= active_stop:
                        out["ambiguous_tp_then_be_count"] += 1
                        final_exit_px = active_stop
                        final_exit_time = ts
                        reason = "ambiguous_1m_tp_then_be"
                        break
            else:
                if l <= active_stop:
                    final_exit_px = stop_fill(o, active_stop)
                    final_exit_time = ts
                    reason = "BE_stop_1m"
                    break

        if final_exit_time is not None:
            break

        # V012 bug fix: daily simulations can legitimately end with a mark at the
        # truncated backtest boundary. If that daily trade's reason is end_of_data
        # (or trend_off_end), reproduce that convention with the last available RTH
        # 1m close on the same daily exit date instead of labeling the audit incomplete.
        if d == daily_exit_date and daily_exit_reason in {"end_of_data", "trend_off_end"}:
            final_exit_px = float(daybars.iloc[-1]["Close"])
            final_exit_time = daybars.index[-1]
            reason = f"{daily_exit_reason}_last_1m_close"
            break

        drow = daily_map.get(d)
        if drow is not None and not bool(drow["trend"]):
            pending_trend_exit = True

    if final_exit_time is None:
        out["audit_status"] = "incomplete_1m_window"
        out["minute_tp1_hit"] = tp1_hit
        return out

    if tp1_hit:
        assert first_exit_px is not None
        exits = [(v4.PARTIAL, float(first_exit_px)), (1.0 - v4.PARTIAL, float(final_exit_px))]
        weighted = v4.PARTIAL * float(first_exit_px) + (1.0 - v4.PARTIAL) * float(final_exit_px)
        gross_r = v4.PARTIAL * ((float(first_exit_px) - entry) / risk) + (1.0 - v4.PARTIAL) * ((float(final_exit_px) - entry) / risk)
    else:
        exits = [(1.0, float(final_exit_px))]
        weighted = float(final_exit_px)
        gross_r = (float(final_exit_px) - entry) / risk

    ret = net_long_return(entry, exits, v4.COST_BPS) * 100.0
    out.update(
        {
            "audit_status": "complete",
            "minute_exit_time": final_exit_time,
            "minute_exit_price_weighted": weighted,
            "minute_return_pct": ret,
            "minute_r_multiple": gross_r,
            "minute_tp1_hit": tp1_hit,
            "minute_exit_reason": reason,
            "return_delta_vs_daily_pctpt": ret - float(trade["return_pct"]),
            "exit_date_match": final_exit_time.date() == daily_exit_date,
            "tp1_match": tp1_hit == bool(trade["tp1_hit"]),
        }
    )
    return out


def main() -> None:
    v12.OUTDIR = OUTDIR
    v12.strict_replay_e1 = strict_replay_e1_fixed
    print("SOR V012.1 - 1M STRICT REPLAY END-OF-DATA FIX")
    print("Fix: mark daily end_of_data/trend_off_end trades at the last RTH 1m close on the same exit date.")
    print()
    v12.main()


if __name__ == "__main__":
    main()
