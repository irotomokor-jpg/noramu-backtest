# RSI_PULLBACK_V002_KNIFE_LITE

Research-only successor to RSI_PULLBACK_V001. It does not modify US_FROZEN_V1 LIVE rules.

## Why V002
The July 2026 quick replay showed that BASE_OPEN produced trades but allowed deep adverse excursion, while the strict V001 confirmation stack reduced trade count too aggressively. V002 keeps the anti-falling-knife idea but relaxes confirmation so the filter does not eliminate nearly every setup.

## Signal -> execution
- QQQ -> TQQQ
- SPY -> UPRO
- SOXX -> SOXL
- EWY -> KORU

## Daily ARM
Keep the V001 base regime unchanged for the next diagnostic round:
- close > EMA200
- EMA50 > EMA200
- EMA200 10-session slope > 0
- RSI(2) <= 5
- two consecutive down closes

Do not loosen RSI yet; change one dimension at a time.

## BANDWALK_BLOCK
Block only a strong lower-band walk, instead of requiring a perfect lower-band recovery before every entry.

Block when either of these is true:
1. two consecutive closes below Bollinger lower band AND lower band is falling AND Bollinger bandwidth is expanding, or
2. three-session lower-low/lower-close staircase AND lower band is falling.

A single lower-band touch or one close below the band does not block the setup by itself.

## Intraday trigger families
Wait until at least 09:45 New York time. A completed 5m bar may trigger when either family is satisfied.

### A. FAILED_BREAK_RECLAIM
- intraday low trades below prior-day low
- completed 5m close reclaims prior-day low
- completed 5m close > session VWAP

Previous-5m-high break is not mandatory in V002.

### B. VWAP_HIGHER_LOW_RECLAIM
- current 5m low >= previous 5m low
- completed 5m close > session VWAP
- completed 5m close > current 5m open

V002 does not require an additional 2BAR hold before entry.
Execution remains next available leveraged-ETF raw 1m OPEN at/after the completed trigger bar.

## Entry variants for the next comparison
- BASE_OPEN: V001 control
- BAND_1BAR: V001 control
- RECLAIM_OR: trigger A OR trigger B, no BANDWALK_BLOCK
- KNIFE_LITE: RECLAIM_OR + BANDWALK_BLOCK
- KNIFE_LITE_STOP1: KNIFE_LITE + one completed 5m close below trigger low exits
- KNIFE_LITE_STOP2: KNIFE_LITE + two completed 5m closes below trigger low exits

Keep profit logic fixed during this entry-filter comparison:
- profit lock +1.5%
- trailing giveback 0.7%
- hard TP +4.0%
- same-day forced exit

Only after the entry architecture survives should the profit-lock/trailing/TP grid be optimized.

## Evaluation priority
1. sufficient trade count
2. worst MAE and frequency of MAE <= -2%
3. worst trade / drawdown
4. average and compounded return after actual Toss commission
5. missed-rebound rate versus BASE_OPEN

Capital-gains tax remains ignored.
