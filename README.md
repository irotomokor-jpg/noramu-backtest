# Noramu Backtest / Shadow Research

The repository now runs US and KR research separately.

- US: `Noramu US v0.24 Prospective Shadow`
- KR: `Noramu KR v0.27 Execution Validation`

See `RUN_US_KR_SEPARATELY.md` for the exact state/output separation and execution notes.

US prospective state remains under `state/` and `latest_output/`.
KR PIT/execution state uses `kr_state_pit/` and `kr_execution_latest_output/`.

Research only. No live-order approval.

## v0.29 staged research

`noramu_v029_research.py`는 저장된 v0.28 손실을 진단한 뒤, 청산 변경
(v0.29A)과 실행 필터 완화(v0.29B)를 분리해 KOSPI/KOSDAQ/미국장을
독립 검증한다. 2026년 7월 이후는 후보 선택에 사용하지 않는 잠금
스트레스 구간이다. 세부 기준은 `V029_PROTOCOL.md`를 따른다.

## v0.30 source-separated research

`noramu_dororong_v030_separated.py`는 노라무와 잡주의도로롱의 원문 기반
신호를 독립 전략군으로 실행한다. 혼합형 `ND_C1_R`과 `RSI_PULLBACK`은
후보군에서 제외하고, 비용·슬리피지·계좌제약·청산·보고만 공유한다.
잡주의도로롱 PRE3 페이크 돌파는 완전한 숏 규칙이 확정될 때까지
신호만 기록한다. 세부 경계와 통과 기준은 `V030_PROTOCOL.md`를 따른다.

## v0.31 signal-edge diagnostics

`noramu_dororong_v031_diagnostics.py`는 계좌 크기·정수주·동시위험 제한을
제거하고 모든 정규 신호를 분수주 1R로 독립 평가한다. 반복 Envelope
touch는 제외하지 않고 0·1·2+로 분해하며, 종목코드 순서 대신 거래량,
상대강도, 유동성, 구조 품질을 같은 비중으로 순위화한다. 진입 분할,
26봉/구조 청산, 60분 MA120/MA200, 월별 동적 유니버스는 2025년까지의
개발구간에서만 비교하고, 선택된 조합 하나만 2026년 검증·잠금 스트레스에
적용한다. 세부 기준은 `V031_PROTOCOL.md`를 따른다.

## v0.32 US PRE2 runner-exit diagnostics

`noramu_dororong_v032_runner_exits.py`는 v0.31에서 선택된 미국
`DORORONG_PRE2` 전량진입·구조청산·MA120/MA200·동적 유니버스를 고정하고
익절만 비교한다. 기존 +1R 절반/+2R 전량 방식과, 과거에 전고점을 회복한
정상 눌림의 75백분위를 이용한 퍼센트·ATR 추적손절, 확인된 60분봉
Higher Low 추적손절을 2025년 분기별 워크포워드로 평가한다. 2026년
상반기와 7월 이후는 선택에 쓰지 않는 잠금 진단이다. 세부 기준은
`V032_PROTOCOL.md`를 따르며 실행 결과는 `V032_RESULT.md`에 보존한다.
