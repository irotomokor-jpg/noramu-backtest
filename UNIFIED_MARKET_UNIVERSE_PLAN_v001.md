# Unified Market Universe v0.01 — KR + US Broad Scan

Research only. No orders. Frozen production/shadow strategies are not modified.

## Goal
Increase opportunity count without relaxing the frozen signal grammar by widening the *information universe* first, then keeping execution selective.

## Two different universe concepts

### 1) Forward/current scanner universe
Used only for current and future signal discovery. Current membership is valid here.

- KOSPI broad liquid universe
- KOSDAQ broad liquid universe
- S&P 500 constituents
- Nasdaq-100 constituents

US lists are deduplicated before data collection.

### 2) Historical replay universe
Used for historical performance claims. Must be point-in-time (PIT) so that a stock is eligible only when it belonged to the historical universe at that time.

- KR: dynamic PIT market-cap snapshots for KOSPI and KOSDAQ
- US: historical PIT membership snapshots for S&P 500 and Nasdaq-100

Current constituent lists MUST NOT be used to label historical replay as unbiased OOS.

## Market sleeves

The same structural signal family may be tested, but market regime and execution assumptions are separated.

| Sleeve | Candidate universe | Regime gauge | Currency/execution |
|---|---|---|---|
| KR_KOSPI | dynamic PIT liquid leaders | KOSPI index + KOSPI breadth | KRW / KRX tick + taxes |
| KR_KOSDAQ | dynamic PIT liquid leaders | KOSDAQ index + KOSDAQ breadth | KRW / KRX tick + taxes |
| US_SP500 | S&P 500 PIT members | S&P 500 / broad US breadth | USD / bps costs |
| US_NDX | Nasdaq-100 PIT members | Nasdaq-100 / growth breadth | USD / bps costs |

No sleeve is allowed to borrow another sleeve's market-regime state.

## Data architecture

Toss Open API remains read-only.

1. Download **adjusted=true 1m** for the signal universe into SQLite.
2. Aggregate session-anchored 60m locally.
3. Generate causal candidates using only completed bars.
4. Download **adjusted=false raw 1m** only for candidate/holding windows.
5. Run strict 1m execution replay.
6. Persist all candidates, rejects, fills, exits and data fingerprints.

This avoids downloading raw history for every stock twice.

## Expansion tiers

### Tier A — controlled broadening
- KOSPI top 100 PIT
- KOSDAQ top 100 PIT
- Nasdaq-100 PIT
- S&P 500 PIT

This is the first serious research target.

### Tier B — wider liquid universe
Only after Tier A is stable:
- wider KRX liquid names outside top-100
- US large/mid-cap universe outside SP500/NDX

Do not jump directly to every listed microcap. More symbols can add noise, illiquidity and false breakout density faster than useful opportunities.

## Portfolio rule for comparison

First compare sleeves independently. Do not immediately pool KRW and USD into one equity curve.

After each sleeve has a stable result, run a combined portfolio with:
- global risk cap
- per-sleeve risk cap
- same-symbol and highly-correlated exposure guards
- deterministic simultaneous-signal ordering

## Required diagnostics

For every sleeve/universe size report:
- candidate count
- executed trades
- reject reasons
- win/loss
- PnL / return
- PF
- MDD
- cost/slippage stress
- concentration by symbol/sector
- monthly signal density
- PIT membership coverage
- adjusted/raw corporate-action diagnostics

## Decision rule

Universe expansion is useful only if it raises opportunity count **without** causing a disproportionate deterioration in PF, MDD, cost sensitivity or concentration.

No live trading is authorized by this plan.
