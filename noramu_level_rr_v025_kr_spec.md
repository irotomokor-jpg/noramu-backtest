# Noramu LEVEL_RR v0.25-KR — KOSPI/KOSDAQ exact-rule replication

## 목적
미국 v0.22에서 고정된 `NORA_LEVEL_RR_A_RAW`의 **신호 문법을 수정하지 않고**
한국 KOSPI/KOSDAQ에 그대로 이식한다.

이 단계는 한국장 최적화가 아니다.
미국 규칙이 다른 시장에서도 구조적으로 재현되는지 보는 cross-market replication이다.

## 미국에서 동결된 신호 숫자
- pivot confirmation = 2 x 60m bars
- level lookback = 240 x 60m bars
- horizontal high-pivot cluster tolerance = 0.35 ATR14
- minimum pivot-high touches = 2
- breakout buffer = 0.05 ATR
- retest window = 6 x 60m bars
- retest tolerance = 0.25 ATR
- invalid tolerance = 0.20 ATR
- stop buffer = 0.25 ATR
- signal cooldown = 10 bars
- entry = confirmation 다음 60m bar open
- sizing family = support/adverse 20/20/60
- no market gate
- no moving-average filter
- no CENTER branch

## 한국장 adapter
KRX 정규장은 09:00~15:30이다.
v0.25-KR은 Yahoo의 native 60m bars를 Asia/Seoul로 변환해
09:00 <= bar start < 15:30인 정규장 bar만 사용한다.

주의:
마지막 15:00 bar는 실제로 30분짜리일 수 있다.
미국과 동일한 시간 길이로 강제 재샘플링하지 않는다.
이는 cross-market replication의 명시적 한계다.

## Universe freeze
첫 성공 실행 시 FinanceDataReader의 KOSPI/KOSDAQ 현재 listing에서
시가총액 상위 보통주 후보를 각각 40종목 선택하여 `kr_state/kr_universe_v025.csv`에 고정한다.

사후에 universe를 바꾸지 않는다.
현재 구성종목을 과거에 적용하므로 survivorship bias가 남는다.

## 실행 엔진
미국 shared-account A-scheme과 최대한 같은 구조를 KR timezone으로 다시 구현:
- 5,000,000 KRW research account
- starter 20%
- -0.40R에서 20% add
- -0.80R에서 60% add
- +1R에서 보유수량 50% 청산, stop을 starter BE로
- +2R에서 잔여 청산
- max hold 26 bars
- max 4 positions
- symbol cap 20%
- base risk 1%
- total risk cap 2%
- gross exposure cap 80%
- daily realized-loss stop 1.5%
- DD 5%부터 risk 0.5x, DD 8% halt

분할주식은 미국 엔진과의 구조 비교를 위해 허용한다.
실제 한국주식 1주 단위 체결은 다음 execution 단계에서 별도 검증한다.

## 비용
v0.25-KR의 baseline 5bps/side는 실제 한국 세금·수수료를 뜻하지 않는다.
오직 미국 연구와 구조 비교를 위한 공통 friction benchmark다.

추가로 10/15/20bps generic stress를 출력한다.
한국 세금/브로커 수수료/호가단위/가격제한폭을 정확히 반영한 execution test는 다음 단계다.

## 결과 해석
- KOSPI와 KOSDAQ을 분리 집계한다.
- 둘을 합친 KR80도 보조 집계한다.
- 최고 1/3/5종목 제거
- 분기별 성과
- generic cost stress
- data coverage
를 함께 본다.

PASS = 코드/데이터 파이프라인 완료.
전략 승인이나 실거래 승인이 아니다.
