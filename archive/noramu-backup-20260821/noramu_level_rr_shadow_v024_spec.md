# Noramu LEVEL_RR v0.24 — Prospective Shadow

## 왜 이제 prospective인가
v0.22/v0.23에서 LEVEL_RR는 FOURTH40에서 양수였고,
완료기간/LOQO/체결 스트레스까지 일정 부분 버텼다.

하지만:
- FOURTH40도 역사 데이터다.
- prior107 backward replication은 이미 연구에서 본 종목군이다.
- Holdout40은 음수였다.
- realistic + top3 제거에서는 엣지가 매우 얇다.

따라서 다음 증거는 역사 파라미터 조정이 아니라 **미래 데이터**에서 쌓는다.

## Frozen strategy
NORA_LEVEL_RR_A_RAW의 신호 문법은 v0.22 그대로:
- pivot confirmation 2 bars
- level lookback 240 x 60m bars
- horizontal cluster tolerance 0.35 ATR
- minimum high-pivot touches 2
- breakout buffer 0.05 ATR
- retest window 6 bars
- retest tolerance 0.25 ATR
- invalid tolerance 0.20 ATR
- stop buffer 0.25 ATR
- cooldown 10 bars
- no market gate
- no MA filter
- no CENTER branch
- no v0.21/v0.22 post-hoc filter

## Paper universe
현재까지 고정했던 4개 미국 universe의 합집합 147종목을 이번 시점에 동결.
향후 신호는 이 universe에서만 prospectively 기록.

## First run
첫 실행은 **baseline initialization**이다.
그 시점까지 과거 데이터에서 이미 생긴 모든 setup_id를 `seen`으로 저장한다.
과거 신호는 paper 성과로 절대 소급하지 않는다.

## Subsequent runs
매 실행마다 fresh 60m data를 다운로드하고:
- 미완성 60m bar를 보수적으로 제거
- exact LEVEL_RR setup 재생성
- baseline 이후 처음 나타난 setup_id만 NEW signal로 append

기록:
- signal/confirm time
- theoretical next-bar open이 이미 존재하면 그 가격
- structural stop
- horizontal level
- level touch count
- breakout/retest/confirm index
- ticker

## 중요한 제한
이 버전은 주문을 내지 않는다.
또한 yfinance 60m는 broker execution feed가 아니므로:
- 신호 발생 시각/가격의 연구용 shadow 기록
- 실제 체결 검증은 나중의 broker-connected paper engine
으로 분리한다.

## v0.23 market diagnostic bug
v0.23의 corrected market-gate diagnostic은
`retest_tol_atr` argument 누락으로 실행되지 않았다.
Primary RAW에는 영향이 없고, v0.24는 애초에 RAW만 사용한다.

## KOSPI/KOSDAQ
미국 shadow가 시작되면 별도 KR exact-rule replication을 진행.
미국 규칙과 한국장 규칙을 한 backtest에서 섞어 튜닝하지 않는다.
