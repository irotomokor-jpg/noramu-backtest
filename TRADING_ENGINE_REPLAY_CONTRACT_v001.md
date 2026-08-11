# Trading Engine v0.01 — Replay Execution Contract

Status: **NO_ORDERS / SHADOW-PROGRAM SPEC**

This contract is derived from the frozen-strategy replay audits. It defines execution and state-machine behavior only. It must not be used to retune strategy thresholds from the seen 2026-08-03..2026-08-10 window.

## 1. Frozen strategy adapters

The engine must support independent strategy adapters without mixing parameters:

- KR Noramu: `PB_WIDE|FAST|DIRECT|H26|TRAIL_P70`
- US Dororong: `DORO_D1_AGG + BULL`
- US ETF Momentum:
  - TQQQ self signal / MA200 / +/-3% hysteresis / OFF -> QQQ
  - SOXL self signal / MA200 / +/-8% hysteresis / OFF -> SOXX

A strategy adapter may emit candidates and desired-position state, but it may not place broker orders directly.

## 2. Required architecture

`MarketDataAdapter -> SignalEngine -> CandidateQueue -> RiskEngine -> ExecutionEngine -> PositionStateMachine -> AuditStore`

A future broker integration must sit behind an explicit `BrokerAdapter`. v0.01 uses `ShadowBroker` only.

### MarketDataAdapter

- Normalize ticker symbols separately from internal candidate IDs.
- Normalize timestamps to the strategy market timezone.
- Track feed fidelity (`tick`, `1m`, `2m`, `5m`, `60m`, `1d`).
- Build higher-timeframe bars only from **completed** lower-timeframe bars.
- Never expose an incomplete future 60m/daily close to a signal adapter.
- Persist the raw bar timestamp and source identifier/hash used for every decision.

### SignalEngine

- Accept only information available at decision time.
- Emit immutable `SignalCandidate` objects with a unique `setup_id`.
- No broker calls.
- No portfolio sizing.

### CandidateQueue

- Sort simultaneous candidates deterministically before risk allocation.
- Initial production contract uses stable ascending internal key/ticker order to reproduce the frozen portfolio executor.
- Record queue rank for every simultaneous candidate.

### RiskEngine

Run before any order intent is emitted.

Must support at minimum:

- total reserved-risk cap
- per-symbol/gross exposure cap
- max open positions
- daily realized-loss stop
- portfolio drawdown reduction/halt rules
- duplicate/same-symbol open-position rejection

Every rejection must be an explicit event with a reason such as `TOTAL_RISK_CAP`, never a silent drop.

### ExecutionEngine

#### Entry

- A setup confirmed on bar `t` cannot be filled retroactively on that same completed bar.
- Entry occurs at the strategy-defined **next executable bar/open**, with modeled or actual slippage and fees applied separately.
- Replay evidence showed KR NAVER and all four Dororong accepted entries matched the first available 2-minute open at the model entry timestamp.

#### Gap stop

- At a new session/bar open, check gap-stop conditions before processing intrabar target/trail logic.
- If open is beyond the active stop, exit at the executable open plus adverse slippage, not at the stale stop price.

#### Intrabar exits

- Stop / target / trailing exits are processed by scanning available lower-timeframe bars **chronologically**.
- Exit occurs on the first causally observed trigger.
- Do not assign a target/stop price merely because the containing 60m bar touched it at some unknown point.
- If both stop and target are touched inside the same available bar and ordering cannot be resolved, use the conservative resolution policy and mark `AMBIGUOUS_INTRABAR` in the audit log. Prefer finer feed when available.

#### Replay/report boundary

- A reporting window ending is not a trading signal.
- Never force-close an overnight/open position merely because a replay or report reaches its last row unless the frozen strategy explicitly says to close.

### PositionStateMachine

Minimum states:

`WATCH -> READY -> ORDER_PENDING -> OPEN -> TRAIL_ARMED -> EXIT_PENDING -> CLOSED`

Alternative terminal state:

`REJECTED`

Required persistence:

- open positions survive process restarts and session boundaries
- active stop/trail state survives restarts
- duplicate replay/feed events are idempotent

### ETF state machine

For TQQQ/SOXL daily MA strategies:

- the **previous completed close** determines the asset held during the next session
- a close signal may switch the desired asset only for the following session/execution point
- hysteresis state persists across days
- do not reinterpret the frozen daily strategy as an intraday strategy

## 3. Event-sourced audit contract

Every state change must append an immutable event with at least:

- `event_id`
- `strategy_id`
- `setup_id`
- `ticker`
- `event_time`
- `event_type`
- `reason`
- `queue_rank`
- `bar_interval`
- `bar_time`
- `raw_price`
- `execution_price`
- `quantity`
- `fee`
- `tax`
- `position_state_before`
- `position_state_after`
- `account_equity`
- `reserved_risk`
- `data_fidelity`
- `source_hash`
- `idempotency_key`

Expected event types include:

`SIGNAL`, `REJECT`, `ORDER_INTENT`, `FILL`, `STOP_UPDATE`, `TRAIL_ARMED`, `EXIT_INTENT`, `CLOSED`, `STATE_RESTORE`.

## 4. Replay-derived acceptance fixtures

The no-order engine is not considered replay-compatible until these fixtures pass.

### KR fixture — 2026-08-03..2026-08-10

Frozen configuration: `PB_WIDE|FAST|DIRECT|H26|TRAIL_P70`.

Expected 5M/1T replay facts:

- 4 candidates
- NAVER (`035420.KS`) accepted
- three simultaneous/later candidates rejected by `TOTAL_RISK_CAP`
- NAVER raw entry timestamp: 2026-08-03 10:00 KST
- raw entry price: 206,500 KRW
- modeled 1-tick execution: 207,000 KRW
- gap-stop raw exit timestamp: 2026-08-07 09:00 KST
- raw exit/open price: 219,500 KRW
- historical minute audit fidelity: 2m
- both entry and gap-exit raw model prices matched the corresponding available 2m opens

The new engine must reproduce candidate/risk/event sequencing. Cost-model PnL should match the frozen executor within deterministic rounding tolerance.

### Dororong fixture — 2026-08-03..2026-08-10 ET

Frozen configuration: `DORO_D1_AGG + BULL`.

Expected facts:

- 5 setups
- 4 accepted trades
- LLY rejected by `TOTAL_RISK_CAP`
- accepted entry tickers: V, WMT, XOM, INTU
- all four model entry prices matched the corresponding available 2m opens
- historical minute audit fidelity: 2m for all four names

The old 60m executor's exit prices are **not** a forced golden value because replay showed meaningful intrabar differences. The new lower-timeframe first-touch executor is allowed, and expected, to differ where it resolves execution timing more faithfully. Such differences must be labeled as execution corrections rather than strategy retuning.

### ETF fixture — 2026-08-03..2026-08-10

Expected facts:

- TQQQ starts `LEVER`, remains `LEVER` for all six sessions, 0 switches
- SOXL starts `LEVER`, remains `LEVER` for all six sessions, 0 switches
- the held asset for a session comes from the prior completed close state

A separate historical switch fixture is required before the ETF switch path is considered verified.

## 5. Mandatory unit scenarios before broker integration

Create deterministic synthetic fixtures for:

1. next-open entry
2. gap below/above active stop
3. stop and target in same minute/bar ambiguity
4. trail arm -> stop update -> first-touch exit
5. simultaneous candidates causing `TOTAL_RISK_CAP`
6. max-position and duplicate-symbol rejection
7. restart with an open position and active trail
8. duplicate feed/event idempotency
9. ETF upper-band OFF->ON switch
10. ETF lower-band ON->OFF switch

## 6. Promotion sequence

1. Build no-order engine and pass synthetic fixtures.
2. Feed the August replay fixtures and compare event streams.
3. Correct execution-model defects only; do **not** retune strategy signals.
4. Run live-data shadow mode with no broker write path.
5. Only after shadow/replay equivalence is stable may a paper-broker adapter be added.
6. Real-order integration remains a separate approval and safety milestone.

`live_approval = false` for this entire v0.01 contract.
