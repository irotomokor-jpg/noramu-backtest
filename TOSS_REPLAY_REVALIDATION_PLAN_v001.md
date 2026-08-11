# Toss replay revalidation plan v0.01

Status: **research / read-only / NO_ORDERS**

## Source contract

Toss Securities is the preferred candle source for replay validation.

- Stock candles: `/api/v1/candles`, intervals `1m` and `1d`, backward pagination via `before` / `nextBefore`, max 200 bars/page.
- Market-index candles: `/api/v1/market-indicators/{symbol}/candles` for KOSPI/KOSDAQ context.
- Market calendars: `/api/v1/market-calendar/KR` and `/api/v1/market-calendar/US` for session/holiday verification.
- Execution audit uses **adjusted=false** raw stock candles.
- Signal-reconstruction research may additionally cache **adjusted=true** stock candles to reconcile the old Yahoo-adjusted research history.
- Official documentation does not state a fixed 1m retention horizon. The code must probe actual depth before attempting a full-universe download.

## Two-stage replay workflow

### Stage A — history-depth proof

Probe representative sources backward to at least 2026-01-01:

- `035420` NAVER — Noramu KR representative
- `AAPL` — Doro US representative
- `218410` RFHIC — KOSDAQ theme representative
- `TQQQ` — leveraged ETF representative
- market indicator `KOSPI`
- market indicator `KOSDAQ`

If any source does not reach the target, record the actual earliest timestamp and downgrade the affected suite to the available overlap window. Do not silently substitute Yahoo for missing minute bars inside a Toss-labelled replay.

### Stage B1 — exact execution replay first

Before downloading full universes, fetch raw 1m windows around the already-known historical candidate/trade timestamps. Re-evaluate:

- next-executable-bar entries
- adverse tick / bps entry model
- gap stops at open
- stop/target/trailing chronological first touch
- TIME exits at the next executable minute after the completed decision bar
- replay-window boundaries do not force `eod_final` liquidation unless the strategy itself requires it

This is the cheapest and highest-value cross-provider validation.

### Stage B2 — full causal signal replay

Only after Stage A succeeds and B1 shows acceptable price agreement, cache the full required 1m universe and regenerate session-anchored 60m bars.

- KR regular session anchor: 09:00 Asia/Seoul
- US regular session anchor: 09:30 America/New_York
- preserve the final partial session bar
- exclude non-regular-session bars unless a strategy explicitly requires them

## Strategy suites

### Noramu KR v0.35

**Can Toss price data revalidate it? Yes, with one metadata exception.**

Toss can replace Yahoo for stock candles and KOSPI market-index candles. The historical point-in-time Top40 membership/marcap snapshots are not a price-candle problem and remain sourced from the existing KRX/marcap PIT dataset. Therefore:

- price / execution layer: Toss
- KOSPI index layer: Toss market indicator
- historical PIT universe membership: existing KRX/marcap snapshot source
- strategy/risk/exit code: frozen Noramu v0.35

This is still a valid **Toss price-data replay**, but not literally a Toss-only metadata pipeline.

### Dororong US v0.16 BULL

**Can be revalidated end-to-end on Toss price candles** if 1m depth is sufficient.

- static 27-stock universe: Toss stock candles
- QQQ / SOXX BULL state proxies: Toss stock candles
- 60m signals: rebuilt from regular-session 1m
- execution: raw Toss 1m
- costs: retain 5/10/20/30 bps scenarios

### TQQQ / SOXL leveraged ETF v0.03

**Can be revalidated on Toss candles** if daily history reaches the required lookback.

- TQQQ / QQQ
- SOXL / SOXX
- MA200 hysteresis uses completed daily closes
- transition execution can be checked with raw 1m on actual switch dates

### KOSDAQ thematic v0.01

**Can be revalidated on Toss price data and KOSDAQ market-indicator data** if 1m depth reaches the executed Jan-May 2026 windows.

Current thematic labels remain research labels, not exchange classifications:

- cosmetics
- optical communications / telecom equipment
- shipping-related/logistics
- oil-related/petroleum distribution
- convenience-store remains sample-insufficient until a clean direct KOSDAQ universe is defined

## Comparison outputs required for every suite

1. provider coverage / earliest timestamp
2. signal timestamp agreement
3. raw entry price agreement
4. exit first-touch agreement
5. exit reason agreement
6. PnL under the frozen execution-cost model
7. monthly trade count / PnL
8. reject-reason funnel
9. data-fidelity label per trade (`TOSS_1M`, `TOSS_1D`, `LEGACY_60M_ONLY`, etc.)
10. explicit unresolved discrepancies

## Safety

- No account header is used by replay tooling.
- No account, holdings, order, or conditional-order endpoint is implemented.
- `LIVE_APPROVAL = False`.
- Credentials remain environment variables only.
- Historical data should be cached outside git when large; only compact scorecards/manifests belong in the repository.
