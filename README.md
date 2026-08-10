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
