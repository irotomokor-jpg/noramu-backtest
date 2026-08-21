# US v0.25 validation scope

Frozen candidate: C-S3 + QQQ MRS v2 from `noramu_us_v07_legacy_engine.py`.

This branch does not retune signal thresholds. It validates the existing candidate under:
- merged US large-cap universe sizes (top 20/30/40 from each existing index resolver),
- $5,000 and $20,000 accounts,
- 5/10/20/30 bps per-side friction assumptions,
- exact top-1/top-3 contributor exclusion and re-simulation,
- calendar year/quarter summaries and 2026 July stress.

Known limitation: the current exact 60m yfinance feed cannot reach 2022 as of 2026. No daily proxy is substituted. Historical point-in-time membership remains the next required validation if this static-universe test survives.

Research only. No live order path exists.
