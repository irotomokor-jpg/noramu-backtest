# RSI_PULLBACK_V001

Research-only strategy. Does not modify `US_FROZEN_V1` LIVE rules.

## Signal -> execution
- QQQ -> TQQQ
- SPY -> UPRO
- SOXX -> SOXL
- EWY -> KORU

## Daily ARM
Uses prior completed daily session from raw regular-session 1m data.

Base regime:
- close > EMA200
- EMA50 > EMA200
- EMA200 10-session slope > 0
- RSI(2) <= 5
- two consecutive down closes

Band ARM adds:
- close <= Bollinger lower band (20, 2)

Knife Shield rejects a setup if any selected falling-knife structure is present:
- repeated lower-band closes with expanding bandwidth
- lower band falling >= 1% over 3 sessions
- three-step lower-close/lower-low staircase

## Intraday confirmation
Signal ETF regular-session raw 1m is causally aggregated to completed 5m bars.

Failed-break reclaim requires:
- intraday low traded below prior-day low
- completed 5m close reclaims prior-day low
- close > session VWAP
- close > previous 5m high

2BAR adds one more completed 5m hold:
- close > VWAP
- low does not break trigger-bar low
- close >= trigger-bar close

Execution is the next available raw 1m OPEN of the leveraged ETF at/after the completed signal.

## ANTI_STOP_SCALP exit
- structural failure: two completed signal-ETF 5m closes below trigger low
- profit-lock grid: +1.0%, +1.5%, +2.0%
- trailing giveback grid: 0.5%, 0.7%, 1.0%
- hard TP grid: +3%, +4%, +5%
- forced same-day exit before Toss fractional-order cutoff
- no capital-gains tax modeling
- actual Toss US commission rate is read from `commission_status.json`

## Comparison variants
- BASE_OPEN
- BAND_1BAR
- ANTI_2BAR
- ANTI_STOP_SCALP

Primary evaluation: return after commission, max drawdown, worst trade, MAE/MFE, deep-adverse frequency, and July 2026 stress performance.
