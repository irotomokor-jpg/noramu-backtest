from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from sor_exit_069500_v001 import load_data, net_long_return, stop_fill, target_fill
import sor_entry_v004_breakout as v4
import sor_v010_shared_portfolio as v10
from sor_v008_broad_universe import UNIVERSE


STRATEGY = "SOR_E1_BE"
CONFIG = "P8_R8"
MAX_POSITIONS = 8
MAX_OPEN_RISK = 0.08
MINUTE_LOOKBACK_DAYS = 28
MINUTE_CHUNK_DAYS = 6
NY_TZ = "America/New_York"
OUTDIR = Path("sor_v012_1m_strict_replay_output")


def _flatten_single_ticker(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        # yfinance can return (Price, Ticker) columns even for one ticker.
        if df.columns.nlevels == 2:
            lvl0 = set(df.columns.get_level_values(0))
            if {"Open", "High", "Low", "Close"}.issubset(lvl0):
                df = df.copy()
                df.columns = df.columns.get_level_values(0)
            else:
                df = df.copy()
                df.columns = df.columns.get_level_values(-1)
    return df


def fetch_1m_rth(ticker: str, start_date: pd.Timestamp, end_date: pd.Timestamp) -> tuple[pd.DataFrame, list[dict]]:
    frames: list[pd.DataFrame] = []
    diagnostics: list[dict] = []

    cur = start_date.normalize()
    end = end_date.normalize() + pd.Timedelta(days=1)
    while cur < end:
        nxt = min(cur + pd.Timedelta(days=MINUTE_CHUNK_DAYS), end)
        try:
            x = yf.download(
                ticker,
                start=cur.strftime("%Y-%m-%d"),
                end=nxt.strftime("%Y-%m-%d"),
                interval="1m",
                auto_adjust=True,
                prepost=False,
                repair=False,
                progress=False,
                threads=False,
                timeout=20,
                multi_level_index=False,
            )
            x = _flatten_single_ticker(x)
            if x is not None and not x.empty:
                idx = pd.DatetimeIndex(x.index)
                if idx.tz is None:
                    idx = idx.tz_localize(NY_TZ)
                else:
                    idx = idx.tz_convert(NY_TZ)
                x = x.copy()
                x.index = idx
                needed = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in x.columns]
                x = x[needed]
                x = x.between_time("09:30", "15:59")
                frames.append(x)
                diagnostics.append(
                    {
                        "ticker": ticker,
                        "chunk_start": cur.date(),
                        "chunk_end_exclusive": nxt.date(),
                        "rows": len(x),
                        "status": "ok" if len(x) else "empty_after_rth_filter",
                    }
                )
            else:
                diagnostics.append(
                    {
                        "ticker": ticker,
                        "chunk_start": cur.date(),
                        "chunk_end_exclusive": nxt.date(),
                        "rows": 0,
                        "status": "empty",
                    }
                )
        except Exception as exc:
            diagnostics.append(
                {
                    "ticker": ticker,
                    "chunk_start": cur.date(),
                    "chunk_end_exclusive": nxt.date(),
                    "rows": 0,
                    "status": f"error:{exc!r}",
                }
            )
        cur = nxt

    if not frames:
        return pd.DataFrame(), diagnostics

    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out, diagnostics


def _daily_by_date(setup: pd.DataFrame) -> dict:
    return {pd.Timestamp(idx).date(): row for idx, row in setup.iterrows()}


def strict_replay_e1(trade: pd.Series, setup: pd.DataFrame, minute: pd.DataFrame) -> dict:
    ticker = str(trade["ticker"])
    daily_entry = float(trade["entry_price"])
    initial_stop = float(trade["initial_stop"])
    entry_date = pd.Timestamp(trade["entry_time"]).date()
    daily_exit_date = pd.Timestamp(trade["exit_time"]).date()

    out = {
        "ticker": ticker,
        "daily_entry_time": trade["entry_time"],
        "daily_exit_time": trade["exit_time"],
        "daily_entry_price": daily_entry,
        "initial_stop": initial_stop,
        "daily_return_pct": float(trade["return_pct"]),
        "daily_r_multiple": float(trade["r_multiple"]),
        "daily_tp1_hit": bool(trade["tp1_hit"]),
        "daily_exit_reason": str(trade["exit_reason"]),
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

    daily_map = _daily_by_date(setup)
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
                    # A 1m OHLC bar cannot prove tick order. Conservative long-side rule:
                    # assume the stop occurred first and flag the bar as ambiguous.
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
                        # TP and the new BE level are both inside one minute. We cannot
                        # prove the sub-minute order, so conservatively realize BE on the rest.
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


def summarize(details: pd.DataFrame, selected_count: int) -> pd.DataFrame:
    complete = details[details["audit_status"] == "complete"].copy()
    if complete.empty:
        return pd.DataFrame(
            [
                {
                    "strategy": STRATEGY,
                    "config": CONFIG,
                    "selected_recent_trades": selected_count,
                    "complete_audits": 0,
                    "coverage_pct": 0.0,
                }
            ]
        )

    return pd.DataFrame(
        [
            {
                "strategy": STRATEGY,
                "config": CONFIG,
                "selected_recent_trades": selected_count,
                "complete_audits": len(complete),
                "coverage_pct": 100.0 * len(complete) / selected_count if selected_count else np.nan,
                "tickers_audited": int(complete["ticker"].nunique()),
                "daily_mean_return_pct": float(complete["daily_return_pct"].mean()),
                "minute_mean_return_pct": float(complete["minute_return_pct"].mean()),
                "mean_return_delta_pctpt": float(complete["return_delta_vs_daily_pctpt"].mean()),
                "median_return_delta_pctpt": float(complete["return_delta_vs_daily_pctpt"].median()),
                "minute_worse_than_daily_pct": 100.0 * float((complete["return_delta_vs_daily_pctpt"] < 0).mean()),
                "sign_flip_count": int(((complete["daily_return_pct"] > 0) != (complete["minute_return_pct"] > 0)).sum()),
                "exit_date_match_pct": 100.0 * float(complete["exit_date_match"].mean()),
                "tp1_match_pct": 100.0 * float(complete["tp1_match"].mean()),
                "ambiguous_stop_vs_tp_bars": int(complete["ambiguous_stop_vs_tp_count"].sum()),
                "ambiguous_tp_then_be_bars": int(complete["ambiguous_tp_then_be_count"].sum()),
                "mean_entry_slippage_bps": float(complete["entry_slippage_vs_daily_open_bps"].mean()),
                "median_entry_slippage_bps": float(complete["entry_slippage_vs_daily_open_bps"].median()),
                "max_abs_return_delta_pctpt": float(complete["return_delta_vs_daily_pctpt"].abs().max()),
            }
        ]
    )


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    now_ny = pd.Timestamp.now(tz=NY_TZ)
    minute_start = (now_ny - pd.Timedelta(days=MINUTE_LOOKBACK_DAYS)).normalize()
    minute_end = now_ny.normalize()

    print("SOR V012 - RECENT 1-MINUTE STRICT EXECUTION REPLAY")
    print(f"Frozen portfolio candidate: {CONFIG} + {STRATEGY}")
    print(f"Minute audit window: {minute_start.date()} -> {minute_end.date()} | RTH only")
    print("Daily setup/portfolio selection is frozen; this audit only challenges execution ordering.")
    print("Ambiguous same-minute stop/TP bars use a conservative stop-first convention.")
    print()

    raw_data: dict[str, pd.DataFrame] = {}
    failures: list[dict] = []
    for ticker in UNIVERSE:
        try:
            raw_data[ticker] = load_data(None, ticker, v10.DOWNLOAD_START, None)
        except Exception as exc:
            failures.append({"stage": "daily_download", "ticker": ticker, "error": repr(exc)})

    if not raw_data:
        raise RuntimeError("All daily downloads failed")

    opportunities, _, build_failures = v10.build_opportunities(raw_data)
    failures.extend(build_failures)
    accepted, _, _ = v10.portfolio_sim(
        opportunities,
        "2023_NOW",
        STRATEGY,
        CONFIG,
        MAX_POSITIONS,
        MAX_OPEN_RISK,
    )
    if accepted.empty:
        raise RuntimeError("No frozen V010 accepted trades found")

    accepted["entry_time"] = pd.to_datetime(accepted["entry_time"])
    recent = accepted[accepted["entry_time"].dt.date >= minute_start.date()].copy()
    recent = recent.sort_values("entry_time").reset_index(drop=True)
    recent.to_csv(OUTDIR / "selected_recent_v010_trades.csv", index=False, encoding="utf-8-sig")

    if recent.empty:
        print("No V010 P8_R8 + E1 entries fall inside the available recent 1m window.")
        pd.DataFrame([{"strategy": STRATEGY, "config": CONFIG, "selected_recent_trades": 0, "complete_audits": 0}]).to_csv(
            OUTDIR / "replay_summary.csv", index=False, encoding="utf-8-sig"
        )
        return

    minute_cache: dict[str, pd.DataFrame] = {}
    download_diag: list[dict] = []
    for ticker in sorted(recent["ticker"].astype(str).unique()):
        print(f"Downloading 1m RTH {ticker} ...")
        m, diag = fetch_1m_rth(ticker, minute_start, minute_end)
        minute_cache[ticker] = m
        download_diag.extend(diag)

    details = []
    setup_cache: dict[str, pd.DataFrame] = {}
    original_threshold = v4.ATR_RATIO_MAX
    v4.ATR_RATIO_MAX = v10.ATR_RATIO_MAX
    try:
        for _, trade in recent.iterrows():
            ticker = str(trade["ticker"])
            if ticker not in setup_cache:
                setup_cache[ticker] = v4.add_sor_setup(raw_data[ticker])
            details.append(strict_replay_e1(trade, setup_cache[ticker], minute_cache.get(ticker, pd.DataFrame())))
    finally:
        v4.ATR_RATIO_MAX = original_threshold

    detail_df = pd.DataFrame(details)
    summary = summarize(detail_df, len(recent))
    status = detail_df.groupby("audit_status", as_index=False).size().rename(columns={"size": "trades"})

    detail_df.to_csv(OUTDIR / "replay_details.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUTDIR / "replay_summary.csv", index=False, encoding="utf-8-sig")
    status.to_csv(OUTDIR / "replay_status_counts.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(download_diag).to_csv(OUTDIR / "minute_download_diagnostics.csv", index=False, encoding="utf-8-sig")
    if failures:
        pd.DataFrame(failures).to_csv(OUTDIR / "failures.csv", index=False, encoding="utf-8-sig")

    print()
    with pd.option_context("display.max_columns", None, "display.width", 240):
        print("REPLAY SUMMARY")
        print(summary.to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
        print()
        print("STATUS COUNTS")
        print(status.to_string(index=False))

    print()
    print(f"Saved under: {OUTDIR}")
    print("NOTE: V012 is a recent-window RTH execution-order audit. It freezes V010 portfolio selection;")
    print("if minute exits materially differ, the next replay must rebuild shared-account occupancy from minute exits.")


if __name__ == "__main__":
    main()
