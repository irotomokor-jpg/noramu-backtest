from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

OUTDIR = Path("sor_v014_full_shared_account_1m_output")


def cid(row: pd.Series) -> str:
    return f"{str(row['ticker'])}|{pd.Timestamp(row['signal_time']).date()}|{pd.Timestamp(row['entry_time']).date()}"


def main() -> None:
    plan = pd.read_csv(OUTDIR / "candidate_plan.csv")
    replay = pd.read_csv(OUTDIR / "candidate_replay.csv")
    accepted = pd.read_csv(OUTDIR / "portfolio_accepted.csv")
    daily = pd.read_csv(OUTDIR / "daily_baseline_accepted.csv")
    missing = pd.read_csv(OUTDIR / "missing_ticker_days.csv", dtype={"ticker": str, "date": str})

    if "candidate_id" not in daily.columns:
        daily["candidate_id"] = daily.apply(cid, axis=1)

    missing_set = set(map(tuple, missing[["ticker", "date"]].astype(str).itertuples(index=False, name=None)))

    pcols = [c for c in ["candidate_id", "ticker", "signal_time", "entry_time", "exit_time", "return_pct", "expected_dates"] if c in plan.columns]
    rcols = [c for c in [
        "candidate_id", "audit_status", "minute_source", "minute_exit_time", "minute_return_pct",
        "minute_exit_reason", "exit_date_match", "tp1_match", "return_delta_vs_daily_pctpt"
    ] if c in replay.columns]

    fallback = accepted[accepted["execution_source"].astype(str) != "minute"].copy()
    fallback = fallback.merge(plan[pcols], on="candidate_id", how="left", suffixes=("", "_plan"))
    fallback = fallback.merge(replay[rcols], on="candidate_id", how="left", suffixes=("", "_replay"))

    def missing_dates_for(row: pd.Series) -> str:
        ticker = str(row.get("ticker", row.get("ticker_plan", "")))
        dates = [x for x in str(row.get("expected_dates", "")).split("|") if x]
        return "|".join(ds for ds in dates if (ticker, ds) in missing_set)

    if not fallback.empty:
        fallback["missing_dates"] = fallback.apply(missing_dates_for, axis=1)
        fallback["missing_day_count"] = fallback["missing_dates"].map(lambda x: len([z for z in str(x).split("|") if z]))
        keep = [c for c in [
            "candidate_id", "ticker", "signal_time", "entry_time", "exit_time", "return_pct",
            "execution_source", "audit_status", "missing_day_count", "missing_dates"
        ] if c in fallback.columns]
        fallback = fallback[keep]
    fallback.to_csv(OUTDIR / "review_fallback_accepted.csv", index=False, encoding="utf-8-sig")

    daily_ids = set(daily["candidate_id"].astype(str))
    minute_ids = set(accepted["candidate_id"].astype(str))

    changed_rows = []
    for label, ids in [("DAILY_ONLY", daily_ids - minute_ids), ("MINUTE_ONLY", minute_ids - daily_ids)]:
        src = daily if label == "DAILY_ONLY" else accepted
        for _, r in src[src["candidate_id"].astype(str).isin(ids)].iterrows():
            changed_rows.append({
                "side": label,
                "candidate_id": r["candidate_id"],
                "ticker": r.get("ticker", ""),
                "entry_time": r.get("entry_time", ""),
                "daily_return_pct": r.get("return_pct", float("nan")),
                "effective_return_pct": r.get("effective_return_pct", float("nan")),
                "execution_source": r.get("execution_source", "daily" if label == "DAILY_ONLY" else ""),
            })
    changed = pd.DataFrame(changed_rows)
    changed.to_csv(OUTDIR / "review_selection_changes.csv", index=False, encoding="utf-8-sig")

    comp = replay[replay["audit_status"].astype(str) == "complete"].copy()
    if not comp.empty:
        daily_ret = pd.to_numeric(comp.get("daily_return_pct"), errors="coerce")
        minute_ret = pd.to_numeric(comp.get("minute_return_pct"), errors="coerce")
        sign_flip = (daily_ret.gt(0) != minute_ret.gt(0))
        exit_bad = ~comp.get("exit_date_match", pd.Series(False, index=comp.index)).astype(bool)
        tp_bad = ~comp.get("tp1_match", pd.Series(False, index=comp.index)).astype(bool)
        anomalous = comp[sign_flip | exit_bad | tp_bad].copy()
    else:
        anomalous = comp
    anomalous.to_csv(OUTDIR / "review_candidate_anomalies.csv", index=False, encoding="utf-8-sig")

    summary = {
        "fallback_accepted_trades": int(len(fallback)),
        "fallback_tickers": sorted(fallback["ticker"].astype(str).unique().tolist()) if len(fallback) and "ticker" in fallback.columns else [],
        "fallback_missing_ticker_days": int(fallback["missing_day_count"].sum()) if len(fallback) and "missing_day_count" in fallback.columns else 0,
        "daily_only_accepted": int(len(daily_ids - minute_ids)),
        "minute_only_accepted": int(len(minute_ids - daily_ids)),
        "candidate_anomalies": int(len(anomalous)),
        "remaining_missing_ticker_days": int(len(missing)),
    }
    (OUTDIR / "review_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not fallback.empty:
        print("\n=== FALLBACK ACCEPTED ===")
        print(fallback.to_string(index=False))
    if not changed.empty:
        print("\n=== SELECTION CHANGES ===")
        print(changed.to_string(index=False))
    if not anomalous.empty:
        cols = [c for c in ["candidate_id", "ticker", "daily_return_pct", "minute_return_pct", "return_delta_vs_daily_pctpt", "exit_date_match", "tp1_match"] if c in anomalous.columns]
        print("\n=== CANDIDATE ANOMALIES ===")
        print(anomalous[cols].to_string(index=False))


if __name__ == "__main__":
    main()
