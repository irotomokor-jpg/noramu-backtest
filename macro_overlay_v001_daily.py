#!/usr/bin/env python3
from __future__ import annotations

import io
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

TRADES = Path("rsi_pullback_v004_long/trades_all.csv")
OUT = Path("macro_overlay_v001_daily")
SERIES = {
    "dgs5": "DGS5",
    "dgs10": "DGS10",
    "wti": "DCOILWTICO",
    "vix": "VIXCLS",
}


def fred_series(series_id: str, name: str) -> pd.DataFrame:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "noramu-backtest/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    d = pd.read_csv(io.BytesIO(raw))
    if d.shape[1] < 2:
        raise RuntimeError(f"BAD_FRED_CSV {series_id}")
    d = d.iloc[:, :2].copy()
    d.columns = ["date", name]
    d["date"] = pd.to_datetime(d.date, errors="coerce")
    d[name] = pd.to_numeric(d[name], errors="coerce")
    d = d.dropna(subset=["date", name]).sort_values("date").drop_duplicates("date", keep="last")
    return d.reset_index(drop=True)


def asof_attach(tr: pd.DataFrame, feat: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    left = tr.sort_values("trade_date_dt").copy()
    right = feat.sort_values("date").copy()
    out = pd.merge_asof(
        left,
        right[["date"] + cols],
        left_on="trade_date_dt",
        right_on="date",
        direction="backward",
        allow_exact_matches=False,
    )
    out = out.drop(columns=["date"])
    return out.sort_values("trade_id").reset_index(drop=True)


def bucket_summary(df: pd.DataFrame, metric: str) -> pd.DataFrame:
    z = df.dropna(subset=[metric]).copy()
    if len(z) < 8 or z[metric].nunique() < 4:
        return pd.DataFrame()
    try:
        z["bucket"] = pd.qcut(z[metric], 4, labels=["Q1_LOW", "Q2", "Q3", "Q4_HIGH"], duplicates="drop")
    except Exception:
        return pd.DataFrame()
    rows = []
    for b, g in z.groupby("bucket", observed=True, sort=True):
        r = g.net_return.astype(float)
        rows.append({
            "metric": metric,
            "bucket": str(b),
            "trades": len(g),
            "metric_min": float(g[metric].min()),
            "metric_max": float(g[metric].max()),
            "win_rate": float((r > 0).mean()),
            "avg_return": float(r.mean()),
            "median_return": float(r.median()),
            "worst_trade": float(r.min()),
            "avg_mae": float(g.mae.mean()),
            "worst_mae": float(g.mae.min()),
        })
    return pd.DataFrame(rows)


def main():
    if not TRADES.exists():
        raise SystemExit(f"TRADES_NOT_FOUND={TRADES}")

    t = pd.read_csv(TRADES)
    t = t[t.variant == "DYN_2BAR"].copy()
    t = t.sort_values(["trade_date", "exec_symbol", "entry_ts"]).reset_index(drop=True)
    t["trade_id"] = np.arange(len(t))
    t["trade_date_dt"] = pd.to_datetime(t.trade_date, errors="coerce").dt.normalize()
    if len(t) != 42:
        print(f"WARN_EXPECTED_42 got={len(t)}", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    print("===== DOWNLOAD FRED DAILY MACRO =====", flush=True)
    d5 = fred_series(SERIES["dgs5"], "dgs5")
    d10 = fred_series(SERIES["dgs10"], "dgs10")
    wti = fred_series(SERIES["wti"], "wti")
    vix = fred_series(SERIES["vix"], "vix")

    d5["dgs5_chg_1d_bp"] = d5.dgs5.diff() * 100.0
    d5["dgs5_chg_5obs_bp"] = d5.dgs5.diff(5) * 100.0
    d10["dgs10_chg_1d_bp"] = d10.dgs10.diff() * 100.0
    d10["dgs10_chg_5obs_bp"] = d10.dgs10.diff(5) * 100.0
    wti["wti_ret_1d"] = wti.wti.pct_change()
    wti["wti_ret_5obs"] = wti.wti.pct_change(5)
    vix["vix_ret_1d"] = vix.vix.pct_change()
    vix["vix_ret_5obs"] = vix.vix.pct_change(5)

    yc = pd.merge(d5[["date", "dgs5"]], d10[["date", "dgs10"]], on="date", how="inner")
    yc["curve_10y5y_bp"] = (yc.dgs10 - yc.dgs5) * 100.0
    yc["curve_chg_1d_bp"] = yc.curve_10y5y_bp.diff()

    for name, d in [("DGS5", d5), ("DGS10", d10), ("WTI", wti), ("VIX", vix), ("CURVE", yc)]:
        print(f"{name} rows={len(d)} min={d.date.min().date()} max={d.date.max().date()}", flush=True)

    z = t.copy()
    z = asof_attach(z, d5, ["dgs5", "dgs5_chg_1d_bp", "dgs5_chg_5obs_bp"])
    z = asof_attach(z, d10, ["dgs10", "dgs10_chg_1d_bp", "dgs10_chg_5obs_bp"])
    z = asof_attach(z, yc, ["curve_10y5y_bp", "curve_chg_1d_bp"])
    z = asof_attach(z, wti, ["wti", "wti_ret_1d", "wti_ret_5obs"])
    z = asof_attach(z, vix, ["vix", "vix_ret_1d", "vix_ret_5obs"])

    z["winner"] = z.net_return.astype(float) > 0
    z["deep_mae_2pct"] = z.mae.astype(float) <= -0.02
    z.to_csv(OUT / "rsi_dyn2bar_macro_tagged.csv", index=False)

    metrics = [
        "dgs5", "dgs5_chg_1d_bp", "dgs5_chg_5obs_bp",
        "dgs10", "dgs10_chg_1d_bp", "dgs10_chg_5obs_bp",
        "curve_10y5y_bp", "curve_chg_1d_bp",
        "wti", "wti_ret_1d", "wti_ret_5obs",
        "vix", "vix_ret_1d", "vix_ret_5obs",
    ]

    rows = []
    for m in metrics:
        q = z[[m, "net_return", "mae"]].dropna()
        if len(q) < 5:
            continue
        rows.append({
            "metric": m,
            "n": len(q),
            "spearman_vs_return": float(q[m].rank().corr(q.net_return.rank())),
            "spearman_vs_mae": float(q[m].rank().corr(q.mae.rank())),
            "winner_mean": float(z.loc[z.winner, m].mean()),
            "loser_mean": float(z.loc[~z.winner, m].mean()),
            "deep_mae_mean": float(z.loc[z.deep_mae_2pct, m].mean()) if z.deep_mae_2pct.any() else np.nan,
            "nondeep_mae_mean": float(z.loc[~z.deep_mae_2pct, m].mean()),
        })
    diagnostic = pd.DataFrame(rows)
    diagnostic.to_csv(OUT / "macro_metric_diagnostic.csv", index=False)

    buckets = []
    for m in metrics:
        b = bucket_summary(z, m)
        if len(b):
            buckets.append(b)
    bucket_df = pd.concat(buckets, ignore_index=True) if buckets else pd.DataFrame()
    bucket_df.to_csv(OUT / "macro_quartile_summary.csv", index=False)

    worst = z.nsmallest(10, "net_return")[[
        "trade_date", "signal_symbol", "exec_symbol", "net_return", "mae", "mfe",
        "dgs5", "dgs5_chg_1d_bp", "dgs10", "dgs10_chg_1d_bp",
        "curve_10y5y_bp", "wti", "wti_ret_1d", "vix", "vix_ret_1d"
    ]]
    worst.to_csv(OUT / "worst10_macro_context.csv", index=False)

    report = [
        "MACRO_OVERLAY_V001_DAILY_DIAGNOSTIC",
        "strategy=RSI_PULLBACK_V1_DYN_2BAR_CURRENT_EXIT",
        f"trades={len(z)}",
        "lookahead_guard=STRICTLY_PREVIOUS_MACRO_OBSERVATION_ONLY",
        "sources=FRED:DGS5,DGS10,DCOILWTICO,VIXCLS",
        "purpose=DIAGNOSTIC_ONLY_NO_FILTER_THRESHOLD_SELECTED",
        "",
        "===== METRIC DIAGNOSTIC =====",
        diagnostic.to_string(index=False),
        "",
        "===== WORST 10 TRADE MACRO CONTEXT =====",
        worst.to_string(index=False),
        "",
        "===== QUARTILE SUMMARY =====",
        bucket_df.to_string(index=False) if len(bucket_df) else "NO_BUCKETS",
    ]
    (OUT / "MACRO_REPORT.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n" + "\n".join(report), flush=True)
    print(f"\nOUTPUT={OUT}", flush=True)


if __name__ == "__main__":
    main()
