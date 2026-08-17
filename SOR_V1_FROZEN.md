# SOR V1.0 FROZEN — Live Trading Specification

Status: **FROZEN / LIVE-DEPLOYABLE**

This document freezes the strategy and portfolio rules that passed the current research cycle. Do not retune these values on the same sample.

## Strategy

- Strategy: `SOR_E1_BE`
- Universe: current `UNIVERSE` in `sor_v008_broad_universe.py`
- Trend: `Close > EMA20 > EMA120 > EMA200` and EMA120 rising
- Contraction: prior-day `ATR5 / ATR20 < 0.90` and prior-day `VOL5 / VOL50 < 1.0`
- Breakout: close above prior 20-day high and breakout-day volume above VOL50
- Entry: next US regular-session open
- Entry gap filter: reject upside gap greater than `0.5 * ATR20`
- Initial stop: most recent causal 2-left / 2-right confirmed pivot low; fallback prior 20-day low
- TP1: at `+2R`, sell 50%
- After TP1: remaining stop moves to breakeven
- Trend off: detected after daily close; final exit at next US regular-session open

## Shared-account portfolio

- Config: `P8_R8`
- Maximum positions: 8
- Target planned risk per accepted trade: 1% of managed equity
- Maximum planned open risk: 8%
- Maximum gross exposure: 100%
- Same-day priority:
  1. breakout volume ratio descending
  2. ATR contraction ratio ascending
  3. volume contraction ratio ascending
- Conservative capacity convention: original entry notional and original planned risk remain occupied until the final exit, including after TP1/BE.
- Existing holdings not opened by the bot are blocked from new SOR entries and are never sold by the bot.

## Validation snapshot

### V013 accepted-trade 1-minute audit

- Complete: 141 / 147 (95.92%)
- Daily mean trade return: +1.7368%
- 1-minute mean trade return: +1.6851%
- Mean delta: -0.0517 percentage points
- Median delta: -0.0103 percentage points
- Sign flips: 1
- Exit-date match: 98.58%
- TP1 match: 100%
- Ambiguous stop/TP bars: 0

### V014 full shared-account 1-minute reconstruction

- Analysis start: 2024-05-06
- Candidate opportunities: 323
- Complete candidate replays: 308 (95.36%)
- Daily accepted: 132
- Minute-effective accepted: 132
- Accepted overlap: 130 / 132
- Daily portfolio return: +17.2920%
- Minute-effective portfolio return: +15.5035%
- Return delta: -1.7885 percentage points
- Daily closed-event MDD: 5.3766%
- Minute-effective closed-event MDD: 5.3213%
- Daily-close MTM MDD: about 8.33% in both baselines

### V014.1 missing-data stress

Six accepted trades lacked complete Toss minute data: BKNG, FDX, IBM, KMB, PNC, WMT.

- Hybrid result: +15.5035%, MDD 5.3213%
- Missing trades forced to 0%: +15.9662%, MDD 5.3213%
- Missing trades forced to initial-stop losses: +8.9607%, MDD 8.9263%
- Selection changes under zero fallback: 0
- Selection changes under stop fallback: 0
- Interpretation: `ROBUST_TO_FALLBACK_STRESS`

Current research conclusion: **ROBUST PASS**.

## Live implementation

Live executor: `sor_toss_live_v100.py`

The historical replay source remains read-only and contains no order endpoints.

Real orders require both:

1. local `sor_live.local.json` has `liveEnabled=true`, and
2. the process is started with CLI flag `--live`.

Only an API account reported as `BROKERAGE` is accepted by setup.

The live executor uses a 3-minute entry window only as failure tolerance; it is intended to be running before the US regular-session open and submit immediately after open.

### Broker protection

- Every actual filled entry is written to local state before protection is created.
- Every bot-managed residual is protected with a Toss broker-side `SINGLE + MARKET + SELL` conditional stop.
- Partial entry fills are managed and protected at actual filled quantity.
- Conditional-order modifications adopt the new `conditionalOrderId` returned by Toss.
- Ambiguous modify responses are not blindly retried; the executor queries broker OPEN conditionals and adopts only one exact matching replacement.
- The executor reconciles holdings, normal open orders, and open conditional orders after restarts.
- Protective conditional expiry is refreshed before it approaches expiration.
- If a residual position cannot be verified as protected, `SOR_LIVE.KILL` is created to block new entries.

### TP1 / trend exits

- TP1 and trend exits are daemon-driven.
- Broker-side protective stops remain the fail-safe if the process dies.
- TP1 sells the cumulative target of 50% of original filled whole shares, then moves all remaining protection to breakeven.
- Trend state is refreshed again before the next regular-session open in case the post-close daily bar was delayed.
- Trend-off exits are sent at the next regular-session open after new-entry capacity decisions, matching the conservative V010/V014 same-open convention.

## Operational invariants

- Do not manually buy or sell a ticker while the bot is managing that ticker.
- Do not delete `sor_live_state.local.json` while any bot-managed position is open.
- Do not run another process using the same Toss client credentials in a way that repeatedly reissues OAuth tokens while the live daemon is active.
- `SOR_LIVE.KILL` blocks new entries; broker protection and managed exits remain active.
- Orders requiring Toss's high-value confirmation (KRW 100m equivalent or above) are intentionally not auto-confirmed by V1.0.
- No forward/paper validation gate is part of this deployment decision; direct live deployment was selected after the historical/minute validation above.
