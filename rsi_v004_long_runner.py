#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path("toss_replay_cache/toss_1m.sqlite")
ENGINE = Path("us_rsi_pullback_v004_dynamic_release.py")
OUT = Path("rsi_pullback_v004_long")
SYMBOLS = ["QQQ","TQQQ","SPY","UPRO","SOXX","SOXL","EWY","KORU"]
KEEP = ["BASE_OPEN","DYN_2BAR","DYN_2BAR_PCLOSE"]


def coverage():
    con = sqlite3.connect(DB)
    q = "SELECT symbol, MIN(timestamp) AS min_ts, MAX(timestamp) AS max_ts, COUNT(*) AS rows FROM candles WHERE symbol IN ({}) GROUP BY symbol".format(
        ",".join("?" for _ in SYMBOLS)
    )
    d = pd.read_sql_query(q, con, params=SYMBOLS)
    con.close()
    if set(d.symbol) != set(SYMBOLS):
        missing = sorted(set(SYMBOLS) - set(d.symbol))
        raise SystemExit(f"MISSING_SYMBOLS={missing}")
    d["min_ts"] = pd.to_datetime(d.min_ts, utc=True, errors="coerce")
    d["max_ts"] = pd.to_datetime(d.max_ts, utc=True, errors="coerce")
    return d.sort_values("symbol")


def summarize(g: pd.DataFrame) -> dict:
    r = g.net_return.astype(float)
    eq = (1 + r).cumprod()
    dd = eq / eq.cummax() - 1
    mfe = g.mfe.astype(float)
    mae = g.mae.astype(float)
    return {
        "trades": len(g),
        "win_rate": float((r > 0).mean()),
        "avg_return": float(r.mean()),
        "median_return": float(r.median()),
        "trade_seq_compounded_return": float(eq.iloc[-1] - 1),
        "max_drawdown_trade_seq": float(dd.min()),
        "worst_trade": float(r.min()),
        "best_trade": float(r.max()),
        "avg_mae": float(mae.mean()),
        "worst_mae": float(mae.min()),
        "avg_mfe": float(mfe.mean()),
        "mae_le_minus_2pct": float((mae <= -0.02).mean()),
        "mae_le_minus_3pct": float((mae <= -0.03).mean()),
        "mae_le_minus_5pct": float((mae <= -0.05).mean()),
        "avg_mfe_minus_net": float((mfe - r).mean()),
    }


def grouped_summary(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    rows = []
    for k, g in df.groupby(keys, dropna=False, sort=True):
        vals = k if isinstance(k, tuple) else (k,)
        row = dict(zip(keys, vals))
        row.update(summarize(g.sort_values(["trade_date","entry_ts"])))
        rows.append(row)
    return pd.DataFrame(rows)


def main():
    if not DB.exists():
        raise SystemExit(f"DB_NOT_FOUND={DB}")
    if not ENGINE.exists():
        raise SystemExit(f"ENGINE_NOT_FOUND={ENGINE}")

    try:
        os.nice(15)
    except Exception:
        pass

    cov = coverage()
    print("===== DATA COVERAGE =====", flush=True)
    print(cov.to_string(index=False), flush=True)

    common_min = cov.min_ts.max().tz_convert("America/New_York").normalize()
    common_max = cov.max_ts.min().tz_convert("America/New_York").normalize()
    # Reserve roughly 200+ trading sessions for EMA200 and slope warm-up.
    analysis_start = (common_min + pd.Timedelta(days=320)).normalize()
    analysis_end_exclusive = (common_max + pd.Timedelta(days=1)).normalize()
    if analysis_start >= analysis_end_exclusive:
        raise SystemExit(f"INSUFFICIENT_COMMON_HISTORY common_min={common_min.date()} common_max={common_max.date()}")

    print(f"COMMON_RAW={common_min.date()}..{common_max.date()}", flush=True)
    print(f"ANALYSIS={analysis_start.date()}..{common_max.date()}", flush=True)

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "chunks").mkdir(parents=True)
    cov.to_csv(OUT / "data_coverage.csv", index=False)

    frames = []
    y = analysis_start.year
    while y <= common_max.year:
        s = max(analysis_start, pd.Timestamp(f"{y}-01-01", tz="America/New_York"))
        e = min(analysis_end_exclusive, pd.Timestamp(f"{y+1}-01-01", tz="America/New_York"))
        if s < e:
            chunk = OUT / "chunks" / f"{s.date()}_{(e-pd.Timedelta(days=1)).date()}"
            print(f"\n===== CHUNK {s.date()}..{(e-pd.Timedelta(days=1)).date()} =====", flush=True)
            cmd = [sys.executable, "-u", str(ENGINE), "--start", str(s.date()), "--end", str(e.date()), "--out", str(chunk)]
            subprocess.run(cmd, check=True)
            p = chunk / "trades.csv"
            if p.exists():
                d = pd.read_csv(p)
                if len(d):
                    d = d[d.variant.isin(KEEP)].copy()
                    d["chunk_start"] = str(s.date())
                    if len(d):
                        frames.append(d)
        y += 1

    if not frames:
        raise SystemExit("NO_TRADES_IN_LONG_RUN")

    tr = pd.concat(frames, ignore_index=True)
    tr = tr.drop_duplicates(subset=["exec_symbol","variant","trade_date","entry_ts"], keep="last")
    tr["trade_date"] = pd.to_datetime(tr.trade_date)
    tr["year"] = tr.trade_date.dt.year
    tr["entry_ts_et"] = pd.to_datetime(tr.entry_ts, utc=True).dt.tz_convert("America/New_York")
    mins = tr.entry_ts_et.dt.hour * 60 + tr.entry_ts_et.dt.minute
    tr["entry_bucket"] = np.select(
        [mins < 600, mins < 720],
        ["09:30-09:59", "10:00-11:59"],
        default="12:00+",
    )
    tr.to_csv(OUT / "trades_all.csv", index=False)

    overall = grouped_summary(tr, ["variant"])
    by_symbol = grouped_summary(tr, ["variant","exec_symbol"])
    by_year = grouped_summary(tr, ["variant","year"])
    overall.to_csv(OUT / "summary_variant.csv", index=False)
    by_symbol.to_csv(OUT / "summary_by_symbol.csv", index=False)
    by_year.to_csv(OUT / "summary_by_year.csv", index=False)

    exit_reason = tr.groupby(["variant","exit_reason"]).size().rename("trades").reset_index()
    exit_reason["share"] = exit_reason.trades / exit_reason.groupby("variant").trades.transform("sum")
    exit_reason.to_csv(OUT / "exit_reason.csv", index=False)

    entry_bucket = tr.groupby(["variant","entry_bucket"]).size().rename("trades").reset_index()
    entry_bucket["share"] = entry_bucket.trades / entry_bucket.groupby("variant").trades.transform("sum")
    entry_bucket.to_csv(OUT / "entry_time_bucket.csv", index=False)

    opp = tr.groupby("variant").size().rename("trades").reset_index()
    base_n = int(opp.loc[opp.variant == "BASE_OPEN", "trades"].iloc[0]) if (opp.variant == "BASE_OPEN").any() else 0
    opp["retention_vs_base"] = opp.trades / base_n if base_n else np.nan
    opp.to_csv(OUT / "opportunity_retention.csv", index=False)

    july = tr[(tr.trade_date >= pd.Timestamp("2026-07-01")) & (tr.trade_date < pd.Timestamp("2026-08-01"))]
    july_summary = grouped_summary(july, ["variant"]) if len(july) else pd.DataFrame()
    july_summary.to_csv(OUT / "summary_2026_07.csv", index=False)

    report = [
        "RSI_PULLBACK_V004_LONG",
        f"common_raw={common_min.date()}..{common_max.date()}",
        f"analysis={analysis_start.date()}..{common_max.date()}",
        "variants=BASE_OPEN,DYN_2BAR,DYN_2BAR_PCLOSE",
        "exit_engine=STRICT_1M_CAUSAL_V003_FIXED",
        "capital_gains_tax=IGNORED",
        "NOTE=trade_seq_compounded_return is NOT a $200 portfolio return; it compounds sequential trades only.",
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
        "===== OPPORTUNITY RETENTION =====",
        opp.to_string(index=False),
        "",
        "===== EXIT REASONS =====",
        exit_reason.to_string(index=False),
        "",
        "===== ENTRY TIME =====",
        entry_bucket.to_string(index=False),
    ]
    if len(july_summary):
        report += ["", "===== JULY 2026 CHECK =====", july_summary.to_string(index=False)]
    (OUT / "LONG_REPORT.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n" + "\n".join(report), flush=True)
    print(f"\nOUTPUT={OUT}", flush=True)


if __name__ == "__main__":
    main()
