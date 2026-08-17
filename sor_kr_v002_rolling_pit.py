#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SOR KR v0.02 rolling-PIT annual robustness test. Research only."""
from __future__ import annotations

import argparse
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

import sor_entry_v004_breakout as v4
import sor_v010_shared_portfolio as v10

VERSION = "SOR-KR-v0.02-ROLLING-PIT"
YEARS = list(range(2019, 2027))
ATR_RATIO_MAX = 0.90
STRATEGY = "SOR_E1_BE"
CONFIG = "P8_R8"
COSTS = [5.0, 20.0]
MAX_POSITIONS = 8
MAX_OPEN_RISK = 0.08
MARCAP_TMPL = "https://raw.githubusercontent.com/FinanceData/marcap/master/data/marcap-{year}.parquet"


def snapshot_for_year(year: int, top_n: int) -> pd.DataFrame:
    src_year = year - 1
    df = pd.read_parquet(MARCAP_TMPL.format(year=src_year))
    if "Date" in df.columns:
        dates = pd.to_datetime(df["Date"], errors="coerce")
    else:
        dates = pd.to_datetime(df.index, errors="coerce")
    valid = dates.dropna()
    if valid.empty:
        raise RuntimeError(f"No dates in marcap-{src_year}")
    snap_date = valid.max().normalize()
    z = df.loc[dates.dt.normalize() == snap_date].copy() if isinstance(dates, pd.Series) else df.loc[dates.normalize() == snap_date].copy()
    if z.empty:
        raise RuntimeError(f"No snapshot rows on {snap_date.date()} in marcap-{src_year}")
    req = {"Code", "Name", "Market", "Marcap"}
    miss = req - set(z.columns)
    if miss:
        raise RuntimeError(f"schema missing {sorted(miss)}")
    z["symbol"] = z["Code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    z["name"] = z["Name"].astype(str)
    z["marcap"] = pd.to_numeric(z["Marcap"], errors="coerce")
    z["market_norm"] = z["Market"].astype(str).str.upper()
    rows = []
    for market, suffix in [("KOSPI", ".KS"), ("KOSDAQ", ".KQ")]:
        m = z[z.market_norm == market].copy()
        bad = (
            m["name"].str.contains("스팩", na=False)
            | m["name"].str.contains("리츠", na=False)
            | m["name"].str.endswith("우", na=False)
            | m["name"].str.contains("우B", na=False)
        )
        m = m[~bad].sort_values("marcap", ascending=False).head(top_n)
        for _, r in m.iterrows():
            rows.append({
                "trade_year": year,
                "snapshot_date": str(snap_date.date()),
                "market": market,
                "symbol": r["symbol"],
                "name": r["name"],
                "yf_ticker": r["symbol"] + suffix,
                "marcap": float(r["marcap"]),
            })
    u = pd.DataFrame(rows)
    if len(u) != 2 * top_n:
        raise RuntimeError(f"year {year}: expected {2*top_n}, got {len(u)}")
    return u


def normalize_yf(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    if isinstance(x.columns, pd.MultiIndex):
        x.columns = x.columns.get_level_values(0)
    need = ["Open", "High", "Low", "Close", "Volume"]
    if not set(need).issubset(x.columns):
        return pd.DataFrame()
    x = x[need].copy()
    x.index = pd.to_datetime(x.index, errors="coerce")
    if getattr(x.index, "tz", None) is not None:
        x.index = x.index.tz_convert(None)
    x = x[~x.index.isna() & ~x.index.duplicated(keep="last")].sort_index()
    for c in need:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    return x.dropna(subset=["Open", "High", "Low", "Close"])


def download_one(ticker: str, start: str, end: str, retries: int = 3) -> pd.DataFrame:
    import yfinance as yf
    errs = []
    for k in range(retries):
        try:
            raw = yf.download(ticker, start=start, end=end, interval="1d", auto_adjust=False,
                              actions=False, progress=False, threads=False)
            x = normalize_yf(raw)
            if len(x) >= 80:
                return x
            errs.append(f"rows={len(x)}")
        except Exception as e:
            errs.append(repr(e))
        time.sleep(1.0 * (k + 1))
    raise RuntimeError("; ".join(errs))


def period_bounds(year: int, end_last: str) -> tuple[str, pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(f"{year}-01-01")
    end = pd.Timestamp(f"{year}-12-31") if year < 2026 else pd.Timestamp(end_last)
    return f"Y{year}", start, end


def build_year_opps(raw_data, uy: pd.DataFrame, year: int, cost: float) -> pd.DataFrame:
    period, start, end = period_bounds(year, "2026-08-17")
    rows = []
    old_atr, old_cost = v4.ATR_RATIO_MAX, v4.COST_BPS
    v4.ATR_RATIO_MAX, v4.COST_BPS = ATR_RATIO_MAX, cost
    try:
        for _, m in uy.iterrows():
            t = str(m.yf_ticker)
            raw = raw_data.get(t)
            if raw is None or raw.empty:
                continue
            prefix = raw.loc[raw.index <= end].copy()
            if len(prefix) < 250:
                continue
            df = v4.add_sor_setup(prefix)
            df.loc[df.index < start, "entry_signal"] = False
            cand, _ = v4.build_candidates(df)
            for c in cand:
                r = v4.simulate_candidate(df, c, STRATEGY)
                r.update({
                    "period": period, "trade_year": year, "market": m.market,
                    "symbol": m.symbol, "name": m["name"], "ticker": t,
                    "strategy": STRATEGY, "cost_bps_side": cost,
                    "priority_breakout_vol": float(c["breakout_vol_ratio"]),
                    "priority_atr_ratio": float(c["atr_ratio_setup"]),
                    "priority_vol_ratio": float(c["vol_ratio_setup"]),
                })
                rows.append(r)
    finally:
        v4.ATR_RATIO_MAX, v4.COST_BPS = old_atr, old_cost
    return pd.DataFrame(rows)


def stats(accepted: pd.DataFrame) -> dict:
    if accepted.empty:
        return {"trades": 0}
    r = pd.to_numeric(accepted.return_pct, errors="coerce").dropna()
    w, l = r[r > 0], r[r < 0]
    return {
        "trades": len(r), "wins": len(w), "losses": len(l),
        "win_rate_pct": 100 * len(w) / len(r) if len(r) else np.nan,
        "avg_trade_pct": r.mean(), "median_trade_pct": r.median(),
        "avg_win_pct": w.mean() if len(w) else np.nan,
        "avg_loss_pct": l.mean() if len(l) else np.nan,
        "payoff": w.mean() / abs(l.mean()) if len(w) and len(l) and l.mean() != 0 else np.nan,
        "pf": w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else np.inf,
        "best_trade_pct": r.max(), "worst_trade_pct": r.min(),
    }


def run(args):
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    print(f"{VERSION}: no parameter tuning, annual rolling PIT universes")

    universes = []
    for year in YEARS:
        print(f"Build PIT universe for trading year {year} from prior year-end snapshot")
        universes.append(snapshot_for_year(year, args.top_n))
    u = pd.concat(universes, ignore_index=True)
    u.to_csv(out / "rolling_pit_universe.csv", index=False, encoding="utf-8-sig")

    unique_tickers = sorted(u.yf_ticker.unique())
    print(f"Unique Yahoo tickers to download: {len(unique_tickers)}")
    raw_data = {}; failures = []
    for i, t in enumerate(unique_tickers, 1):
        try:
            print(f" {i:>3}/{len(unique_tickers)} {t}")
            raw_data[t] = download_one(t, args.download_start, args.end_exclusive)
        except Exception as e:
            failures.append({"ticker": t, "error": repr(e)})
    pd.DataFrame(failures).to_csv(out / "download_failures.csv", index=False, encoding="utf-8-sig")

    coverage_rows = []
    for year in YEARS:
        uy = u[u.trade_year == year]
        for market in ["KOSPI", "KOSDAQ"]:
            m = uy[uy.market == market]
            ok = int(m.yf_ticker.isin(raw_data).sum())
            coverage_rows.append({"trade_year": year, "market": market, "target": len(m), "resolved": ok, "coverage_pct": 100*ok/len(m)})
    coverage = pd.DataFrame(coverage_rows)
    coverage.to_csv(out / "coverage_by_year_market.csv", index=False, encoding="utf-8-sig")

    annual_rows = []; accepted_rows = []; opp_rows = []
    for cost in COSTS:
        for year in YEARS:
            uy = u[u.trade_year == year].copy()
            period = f"Y{year}"
            opp = build_year_opps(raw_data, uy, year, cost)
            if not opp.empty:
                opp_rows.append(opp)
            for uname, markets in [("KOSPI40_ROLLING", ["KOSPI"]), ("KOSDAQ40_ROLLING", ["KOSDAQ"]), ("KR80_ROLLING", ["KOSPI", "KOSDAQ"])]:
                x = opp[opp.market.isin(markets)].copy() if not opp.empty else pd.DataFrame()
                if x.empty:
                    annual_rows.append({"universe": uname, "trade_year": year, "period": period, "cost_bps_side": cost,
                                        "portfolio_return_pct": np.nan, "closed_mdd_pct": np.nan, "accepted": 0, "opportunities": 0})
                    continue
                a, s, rej = v10.portfolio_sim(x, period, STRATEGY, CONFIG, MAX_POSITIONS, MAX_OPEN_RISK)
                row = {
                    "universe": uname, "trade_year": year, "period": period, "cost_bps_side": cost,
                    "portfolio_return_pct": s.get("portfolio_total_return_pct", np.nan),
                    "closed_mdd_pct": s.get("closed_event_max_drawdown_pct", np.nan),
                    "accepted": s.get("accepted_trades", 0), "opportunities": s.get("opportunities", 0),
                }
                row.update(stats(a))
                annual_rows.append(row)
                if cost == 5.0 and not a.empty:
                    a = a.copy(); a["universe"] = uname; a["trade_year"] = year; accepted_rows.append(a)
                print(f"{cost:>4.0f}bps {year} {uname:<17} ret={row['portfolio_return_pct']:+7.2f}% MDD={row['closed_mdd_pct']:6.2f}% trades={row.get('trades',0)}")

    annual = pd.DataFrame(annual_rows)
    annual.to_csv(out / "annual_score.csv", index=False, encoding="utf-8-sig")
    if accepted_rows:
        pd.concat(accepted_rows, ignore_index=True).to_csv(out / "accepted_base5bps.csv", index=False, encoding="utf-8-sig")
    if opp_rows:
        pd.concat(opp_rows, ignore_index=True).to_csv(out / "opportunities_all_costs.csv", index=False, encoding="utf-8-sig")

    overall_rows = []
    for uname in ["KOSPI40_ROLLING", "KOSDAQ40_ROLLING", "KR80_ROLLING"]:
        for cost in COSTS:
            g = annual[(annual.universe == uname) & (annual.cost_bps_side == cost)].dropna(subset=["portfolio_return_pct"])
            rets = g.portfolio_return_pct.astype(float)
            comp = (np.prod(1.0 + rets.to_numpy()/100.0) - 1.0) * 100.0 if len(rets) else np.nan
            overall_rows.append({
                "universe": uname, "cost_bps_side": cost, "years": len(g),
                "positive_years": int((rets > 0).sum()),
                "positive_year_pct": 100 * (rets > 0).mean() if len(rets) else np.nan,
                "median_annual_return_pct": rets.median() if len(rets) else np.nan,
                "mean_annual_return_pct": rets.mean() if len(rets) else np.nan,
                "worst_annual_return_pct": rets.min() if len(rets) else np.nan,
                "best_annual_return_pct": rets.max() if len(rets) else np.nan,
                "compounded_return_pct": comp,
                "median_closed_mdd_pct": g.closed_mdd_pct.median() if len(g) else np.nan,
                "worst_closed_mdd_pct": g.closed_mdd_pct.max() if len(g) else np.nan,
                "total_trades": int(g.trades.fillna(0).sum()) if "trades" in g else 0,
            })
    overall = pd.DataFrame(overall_rows)
    overall.to_csv(out / "overall_score.csv", index=False, encoding="utf-8-sig")

    verdict_rows = []
    for uname in ["KOSPI40_ROLLING", "KOSDAQ40_ROLLING", "KR80_ROLLING"]:
        b = overall[(overall.universe == uname) & (overall.cost_bps_side == 5.0)].iloc[0]
        c = overall[(overall.universe == uname) & (overall.cost_bps_side == 20.0)].iloc[0]
        supported = bool(b.years >= 6 and b.positive_year_pct >= 62.5 and b.median_annual_return_pct > 0 and b.compounded_return_pct > 0 and c.compounded_return_pct > 0)
        mixed = bool(b.compounded_return_pct > 0 and b.positive_year_pct >= 50.0)
        verdict_rows.append({
            "universe": uname,
            "base_positive_years": int(b.positive_years), "base_years": int(b.years),
            "base_median_annual_return_pct": float(b.median_annual_return_pct),
            "base_compounded_return_pct": float(b.compounded_return_pct),
            "cost20_compounded_return_pct": float(c.compounded_return_pct),
            "verdict": "ROLLING_PIT_SUPPORTED" if supported else ("ROLLING_PIT_MIXED" if mixed else "ROLLING_PIT_UNSUPPORTED"),
        })
    verdict = pd.DataFrame(verdict_rows)
    verdict.to_csv(out / "verdict.csv", index=False, encoding="utf-8-sig")

    (out / "RUN_VALIDATION.txt").write_text(
        "PASS\n" + f"years={YEARS[0]}-{YEARS[-1]}\n" + f"unique_tickers={len(unique_tickers)}\n" +
        "PASS means the rolling-PIT research pipeline completed; no live approval.\n",
        encoding="utf-8"
    )
    print("\nVERDICT")
    print(verdict.to_string(index=False))
    print("\nOVERALL")
    print(overall.to_string(index=False, float_format=lambda x: f"{x:,.3f}"))
    print("RUN_VALIDATION=PASS")


def self_test():
    assert YEARS == list(range(2019, 2027))
    assert ATR_RATIO_MAX == 0.90 and STRATEGY == "SOR_E1_BE" and CONFIG == "P8_R8"
    assert v4.BREAKOUT_LOOKBACK == 20 and v4.RR_TARGET == 2.0 and v4.PARTIAL == 0.50
    print("SOR_KR_V002_SELF_TEST=PASS")


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="sor_kr_v002_rolling_pit_output")
    ap.add_argument("--top-n", type=int, default=40)
    ap.add_argument("--download-start", default="2017-01-01")
    ap.add_argument("--end-exclusive", default="2026-08-18")
    ap.add_argument("--self-test", action="store_true")
    return ap.parse_args()


if __name__ == "__main__":
    a = parse_args()
    self_test() if a.self_test else run(a)
