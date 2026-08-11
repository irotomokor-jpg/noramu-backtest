#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KR v0.38 July signal-frequency diagnostic from frozen v0.37 replay outputs.

No thresholds are changed. This only measures how many final eligible candidates
were produced by month and how July candidates were filtered by market regime
and portfolio risk.
"""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pandas as pd

SRC = Path("kr_v037_replay_output")
OUT = Path("kr_v038_july_funnel_output")
TZ = "Asia/Seoul"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cand = pd.read_csv(SRC/"candidate_replay_audit.csv")
    cand["dt"] = pd.to_datetime(cand.entry_time, utc=True, errors="coerce").dt.tz_convert(TZ)
    cand = cand[cand.dt.notna()].copy()
    cand["month"] = cand.dt.dt.to_period("M").astype(str)
    cand["year"] = cand.dt.dt.year

    monthly = cand.groupby("month").size().reset_index(name="final_eligible_candidates")
    monthly.to_csv(OUT/"monthly_final_candidate_counts.csv", index=False, encoding="utf-8-sig")
    m26 = monthly[monthly.month.str.startswith("2026-")].copy()
    m26.to_csv(OUT/"monthly_final_candidate_counts_2026.csv", index=False, encoding="utf-8-sig")

    july = cand[(cand.dt >= pd.Timestamp("2026-07-01", tz=TZ)) & (cand.dt < pd.Timestamp("2026-08-01", tz=TZ))]
    rej = pd.read_csv(SRC/"rejects_5m_1t.csv")
    rej["dt"] = pd.to_datetime(rej.time, utc=True, errors="coerce").dt.tz_convert(TZ)
    jr = rej[(rej.dt >= pd.Timestamp("2026-07-01", tz=TZ)) & (rej.dt < pd.Timestamp("2026-08-01", tz=TZ))]
    tr = pd.read_csv(SRC/"trades_5m_1t.csv")
    tr["dt"] = pd.to_datetime(tr.entry_time, utc=True, errors="coerce").dt.tz_convert(TZ)
    jt = tr[(tr.dt >= pd.Timestamp("2026-07-01", tz=TZ)) & (tr.dt < pd.Timestamp("2026-08-01", tz=TZ))]

    final_candidates = int(len(july))
    market_reject = int((jr.reason.astype(str) == "MARKET_REGIME").sum()) if not jr.empty else 0
    risk_reject = int((jr.reason.astype(str) == "TOTAL_RISK_CAP").sum()) if not jr.empty else 0
    traded = int(len(jt))
    after_market = max(0, final_candidates - market_reject)
    funnel = pd.DataFrame([
        {"stage":"FINAL_ELIGIBLE_CANDIDATE_BEFORE_MARKET_REGIME","count":final_candidates,"conversion_from_prior":1.0},
        {"stage":"MARKET_REGIME_PASS","count":after_market,"conversion_from_prior":after_market/final_candidates if final_candidates else np.nan},
        {"stage":"PORTFOLIO_RISK_ACCEPTED_AND_TRADED","count":traded,"conversion_from_prior":traded/after_market if after_market else np.nan},
        {"stage":"MARKET_REGIME_REJECT","count":market_reject,"conversion_from_prior":market_reject/final_candidates if final_candidates else np.nan},
        {"stage":"TOTAL_RISK_CAP_REJECT","count":risk_reject,"conversion_from_prior":risk_reject/final_candidates if final_candidates else np.nan},
    ])
    funnel.to_csv(OUT/"july_signal_funnel.csv", index=False, encoding="utf-8-sig")
    jr.groupby("reason").size().reset_index(name="count").to_csv(OUT/"july_reject_reasons.csv", index=False, encoding="utf-8-sig")

    # Compare July with other 2026 months at the same FINAL-candidate stage.
    counts = dict(zip(m26.month, m26.final_eligible_candidates))
    jul_count = int(counts.get("2026-07", 0))
    prior = [int(v) for k,v in counts.items() if k < "2026-07"]
    score = {
        "version":"KR_V038_JULY_SIGNAL_FUNNEL_AUDIT",
        "purpose":"SIGNAL_SCARCITY_DIAGNOSTIC_NOT_TUNING",
        "live_approval":False,"order_mode":"NO_ORDERS",
        "july_final_candidates":jul_count,
        "july_market_regime_rejects":market_reject,
        "july_trades":traded,
        "prior_2026_month_median_final_candidates":float(np.median(prior)) if prior else np.nan,
        "prior_2026_month_mean_final_candidates":float(np.mean(prior)) if prior else np.nan,
        "interpretation":"Do not loosen thresholds from this seen-history diagnostic. Use counts to decide whether a separate higher-frequency sleeve is needed."
    }
    (OUT/"scorecard.json").write_text(json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(score, ensure_ascii=False, indent=2))

if __name__ == "__main__": main()
