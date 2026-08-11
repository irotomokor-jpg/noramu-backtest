# Leveraged ETF Sleeve v0.01 — TQQQ / SOXL Parallel Track

Research only. No orders. Existing frozen ETF v0.03 parameters are unchanged.

## Frozen strategies retained

- TQQQ: self signal, 200-day MA, hysteresis band ±3%, base asset QQQ.
- SOXL: self signal, 200-day MA, hysteresis band ±8%, base asset SOXX.
- TECL remains excluded.

The unified KR/US stock scanner does **not** absorb these ETFs. They remain a separate leveraged-ETF sleeve.

## Why separate the ETF sleeve

Leveraged ETFs have daily leverage reset, larger gap risk and strong factor overlap with US growth/semiconductor stocks. Treating them as ordinary stock candidates would hide concentration and correlation risk.

## Causal execution revalidation

The frozen signal rule is unchanged, but an additional execution audit is required:

1. Build the signal only from a **completed US daily close**.
2. If desired asset changes at that completed close, queue the switch.
3. Execute the switch at the **next available regular-session 1-minute open**.
4. Apply 5 / 10 / 20 / 30 bps per-side cost stress.
5. Never execute at the same close that created the signal.
6. Persist positions across replay boundaries; no fake final liquidation.

This is a stricter execution test than same-close switching and is not a parameter change.

## Combined-portfolio diagnostics before any overlay cap is chosen

Report, but do not tune yet:

- TQQQ and SOXL simultaneous LEVER-state days.
- Correlation of daily sleeve returns.
- Worst joint 1d / 5d / 20d drawdowns.
- Overlap with US stock sleeves (especially technology and semiconductors).
- Share of total portfolio risk coming from leveraged ETFs.
- Cost sensitivity under next-open execution.
- Gap from signal close to next-session open.

## Candidate improvements to consider only after the diagnostics

1. **Leveraged-ETF sleeve cap** — cap total ETF allocation or risk contribution.
2. **Correlation guard** — reduce combined TQQQ+SOXL exposure when both are in LEVER state and highly correlated.
3. **US factor overlap guard** — avoid simultaneously maxing SOXL plus multiple semiconductor stock positions.
4. **Volatility-aware allocation** — scale ETF sleeve exposure by realized volatility, without changing the MA200 signal itself.
5. **FX-aware combined reporting** — keep KRW and USD sleeves separate first; only later combine with explicit KRW/USD mark-to-market.

No guard threshold is selected in v0.01. These are diagnostics and candidate improvements only.

## Portfolio architecture

- KR stock sleeve: Noramu-family broad scanner, market-local regimes.
- US stock sleeve: Doro/Noramu research sleeves, market-local regimes.
- Leveraged ETF sleeve: frozen TQQQ/SOXL MA200 rules.
- Global portfolio layer: diagnostics first; risk caps only after evidence review.

NO_ORDERS / LIVE_APPROVAL=False.
