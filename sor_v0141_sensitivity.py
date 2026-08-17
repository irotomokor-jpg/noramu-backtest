from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from sor_exit_069500_v001 import net_long_return
import sor_entry_v004_breakout as v4
import sor_v014_full_shared_account_1m as v14

OUTDIR = v14.OUTDIR


def as_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "y"}
    if pd.isna(v):
        return False
    return bool(v)


def stop_loss_return_pct(row: pd.Series) -> float:
    entry = float(row["entry_price"])
    stop = float(row["initial_stop"])
    return 100.0 * net_long_return(entry, [(1.0, stop)], v4.COST_BPS)


def scenario(plan: pd.DataFrame, replay: pd.DataFrame, fallback_ids: set[str], mode: str) -> dict:
    p = plan.copy()
    if mode == "ZERO_FALLBACK":
        p.loc[p["candidate_id"].astype(str).isin(fallback_ids), "return_pct"] = 0.0
    elif mode == "STOP_FALLBACK":
        mask = p["candidate_id"].astype(str).isin(fallback_ids)
        p.loc[mask, "return_pct"] = p.loc[mask].apply(stop_loss_return_pct, axis=1)
    elif mode != "HYBRID_AS_RUN":
        raise ValueError(mode)

    accepted, summary, _ = v14._portfolio_sim_effective(p, replay)
    accepted_ids = set(accepted["candidate_id"].astype(str)) if not accepted.empty else set()
    return {
        "scenario": mode,
        "accepted_trades": int(len(accepted)),
        "portfolio_total_return_pct": float(summary["portfolio_total_return_pct"]),
        "closed_event_max_drawdown_pct": float(summary["closed_event_max_drawdown_pct"]),
        "return_over_mdd": float(summary["return_over_mdd"]),
        "daily_fallback_accepted_trades": int(summary["daily_fallback_accepted_trades"]),
        "portfolio_strictness": str(summary["portfolio_strictness"]),
        "accepted_ids": accepted_ids,
    }


def main() -> None:
    plan, _ = v14.load_plan(OUTDIR)
    replay = pd.read_csv(OUTDIR / "candidate_replay.csv")
    accepted = pd.read_csv(OUTDIR / "portfolio_accepted.csv")
    daily = pd.read_csv(OUTDIR / "daily_baseline_accepted.csv")

    fallback = accepted[accepted["execution_source"].astype(str) != "minute"].copy()
    fallback_ids = set(fallback["candidate_id"].astype(str))

    comp = replay[replay["audit_status"].astype(str) == "complete"].copy()
    if not comp.empty:
        comp["sign_flip"] = (comp["daily_return_pct"].astype(float) > 0) != (comp["minute_return_pct"].astype(float) > 0)
        comp["exit_mismatch"] = ~comp["exit_date_match"].map(as_bool)
        comp["tp1_mismatch"] = ~comp["tp1_match"].map(as_bool)
        anomalies = comp[comp[["sign_flip", "exit_mismatch", "tp1_mismatch"]].any(axis=1)].copy()
    else:
        anomalies = pd.DataFrame()

    accepted_ids = set(accepted["candidate_id"].astype(str))
    daily_ids = set(daily["candidate_id"].astype(str)) if "candidate_id" in daily.columns else {
        v14.candidate_id(r) for _, r in daily.iterrows()
    }

    if not anomalies.empty:
        anomalies["accepted_minute_portfolio"] = anomalies["candidate_id"].astype(str).isin(accepted_ids)
        anomalies["accepted_daily_portfolio"] = anomalies["candidate_id"].astype(str).isin(daily_ids)
        anomalies["abs_return_delta_pctpt"] = anomalies["return_delta_vs_daily_pctpt"].astype(float).abs()
        anomalies = anomalies.sort_values(
            ["accepted_minute_portfolio", "abs_return_delta_pctpt"], ascending=[False, False]
        )
        anomalies.to_csv(OUTDIR / "v0141_accepted_anomaly_review.csv", index=False, encoding="utf-8-sig")

    scenario_rows = []
    scenario_ids: dict[str, set[str]] = {}
    for mode in ["HYBRID_AS_RUN", "ZERO_FALLBACK", "STOP_FALLBACK"]:
        s = scenario(plan, replay, fallback_ids, mode)
        scenario_ids[mode] = s.pop("accepted_ids")
        scenario_rows.append(s)

    scenarios = pd.DataFrame(scenario_rows)
    scenarios.to_csv(OUTDIR / "v0141_fallback_sensitivity.csv", index=False, encoding="utf-8-sig")

    hybrid_ids = scenario_ids["HYBRID_AS_RUN"]
    selection_rows = []
    for mode in ["ZERO_FALLBACK", "STOP_FALLBACK"]:
        ids = scenario_ids[mode]
        for cid in sorted(hybrid_ids - ids):
            selection_rows.append({"scenario": mode, "side": "HYBRID_ONLY", "candidate_id": cid})
        for cid in sorted(ids - hybrid_ids):
            selection_rows.append({"scenario": mode, "side": "SCENARIO_ONLY", "candidate_id": cid})
    pd.DataFrame(selection_rows).to_csv(
        OUTDIR / "v0141_sensitivity_selection_changes.csv", index=False, encoding="utf-8-sig"
    )

    fallback_cols = [c for c in [
        "candidate_id", "ticker", "entry_time", "exit_time", "return_pct", "risk_pct",
        "portfolio_pnl", "notional", "execution_source", "audit_status"
    ] if c in fallback.columns]
    fallback[fallback_cols].to_csv(
        OUTDIR / "v0141_fallback_accepted_detail.csv", index=False, encoding="utf-8-sig"
    )

    accepted_anomalies = anomalies[anomalies["accepted_minute_portfolio"]].copy() if not anomalies.empty else pd.DataFrame()
    sign_flip_accepted = int(accepted_anomalies["sign_flip"].sum()) if not accepted_anomalies.empty else 0
    tp1_mismatch_accepted = int(accepted_anomalies["tp1_mismatch"].sum()) if not accepted_anomalies.empty else 0
    exit_mismatch_accepted = int(accepted_anomalies["exit_mismatch"].sum()) if not accepted_anomalies.empty else 0

    hybrid = scenarios[scenarios["scenario"] == "HYBRID_AS_RUN"].iloc[0]
    zero = scenarios[scenarios["scenario"] == "ZERO_FALLBACK"].iloc[0]
    stop = scenarios[scenarios["scenario"] == "STOP_FALLBACK"].iloc[0]

    result = {
        "fallback_accepted_trades": int(len(fallback)),
        "fallback_tickers": sorted(fallback["ticker"].astype(str).unique().tolist()) if len(fallback) else [],
        "candidate_anomalies": int(len(anomalies)),
        "accepted_candidate_anomalies": int(len(accepted_anomalies)),
        "accepted_sign_flips": sign_flip_accepted,
        "accepted_exit_date_mismatches": exit_mismatch_accepted,
        "accepted_tp1_mismatches": tp1_mismatch_accepted,
        "hybrid_return_pct": float(hybrid["portfolio_total_return_pct"]),
        "zero_fallback_return_pct": float(zero["portfolio_total_return_pct"]),
        "stop_fallback_return_pct": float(stop["portfolio_total_return_pct"]),
        "hybrid_mdd_pct": float(hybrid["closed_event_max_drawdown_pct"]),
        "zero_fallback_mdd_pct": float(zero["closed_event_max_drawdown_pct"]),
        "stop_fallback_mdd_pct": float(stop["closed_event_max_drawdown_pct"]),
        "zero_fallback_selection_changes": int(len(hybrid_ids ^ scenario_ids["ZERO_FALLBACK"])),
        "stop_fallback_selection_changes": int(len(hybrid_ids ^ scenario_ids["STOP_FALLBACK"])),
        "interpretation": (
            "ROBUST_TO_FALLBACK_STRESS" if float(stop["portfolio_total_return_pct"]) > 0
            else "FALLBACK_STRESS_REQUIRES_REVIEW"
        ),
    }
    (OUTDIR / "v0141_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    print("\n=== FALLBACK SENSITIVITY ===")
    print(scenarios.to_string(index=False))
    if not accepted_anomalies.empty:
        show_cols = [c for c in [
            "candidate_id", "ticker", "daily_return_pct", "minute_return_pct",
            "return_delta_vs_daily_pctpt", "sign_flip", "exit_mismatch", "tp1_mismatch"
        ] if c in accepted_anomalies.columns]
        print("\n=== ACCEPTED ANOMALIES ===")
        print(accepted_anomalies[show_cols].to_string(index=False))


if __name__ == "__main__":
    main()
