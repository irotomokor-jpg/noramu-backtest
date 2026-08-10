# Noramu LEVEL_RR v0.26-KR-PIT

## 목적
v0.25-KR 결과는 강했지만, 2026년 현재 시가총액 상위 종목을 2023~2026 과거에 적용했다는 **future-selection/survivorship bias**가 있다.

v0.26은 신호/진입/청산/리스크 숫자를 하나도 바꾸지 않고, 60분 데이터가 시작되는 2023-08-08 당시 KRX 시가총액 상위 종목을 KOSPI/KOSDAQ 각각 40개 고정해서 다시 검증한다.

## Point-in-time universe
- Source: FinanceData/marcap historical KRX market-cap dataset
- PIT date: 2023-08-08
- KOSPI top 40 / KOSDAQ top 40 by Marcap
- same common-stock-ish exclusions as v0.25 (SPAC/REIT/preferred-name patterns)
- universe is persisted to `kr_state_pit/kr_universe_v026_pit.csv`

## Frozen strategy
v0.25와 동일:
- pivot confirmation 2
- level lookback 240 x 60m
- cluster tolerance 0.35 ATR
- minimum high-pivot touches 2
- breakout buffer 0.05 ATR
- retest window 6 bars
- retest tolerance 0.25 ATR
- invalid tolerance 0.20 ATR
- stop buffer 0.25 ATR
- cooldown 10
- support/adverse 20/20/60
- no market gate / no MA filter / no CENTER

Portfolio control도 v0.25와 동일하다.

## 남는 편향
PIT universe는 미래 시총선정 편향을 줄이지만 다음은 남는다:
- Yahoo 60m availability bias
- delisted/renamed ticker data availability
- fractional shares
- generic friction instead of exact Korean tax/fees/ticks

## 판정
KOSPI40_PIT, KOSDAQ40_PIT, KR80_PIT을 각각 보고:
- base PnL/PF
- top1/top3/top5 제거
- 10/15/20bps generic friction
- quarter stability
를 확인한다.

PASS는 코드/데이터 완료 의미이며 실거래 승인 아님.
