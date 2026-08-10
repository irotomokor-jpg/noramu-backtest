# 미국장 / 한국장 분리 실행

GitHub Actions에서 두 워크플로를 완전히 따로 실행한다.

## 미국장
- 워크플로: `Noramu US v0.24 Prospective Shadow`
- 파일: `.github/workflows/noramu-shadow.yml`
- 상태: `state/`
- 최신 결과: `latest_output/`
- 자동 스케줄 + 수동 실행
- 기존 prospective baseline/state를 그대로 유지한다.

## 한국장
- 워크플로: `Noramu KR v0.27 Execution Validation`
- 파일: `.github/workflows/noramu-kr-v027.yml`
- PIT universe 상태: `kr_state_pit/`
- 최신 결과: `kr_execution_latest_output/`
- 수동 실행 가능. KR 코드 변경 시에도 별도 실행.
- 미국 state/output에는 손대지 않는다.

## v0.27-KR 변경점
LEVEL_RR 신호 규칙은 변경하지 않는다.
실행 현실성만 추가한다:
- 1주 단위
- KRX 가격대별 tick
- Toss KRX 0.015%/side commission
- 연도별 매도세/농특세
- 0/1/2 tick adverse fill
- 500만/1000만/2000만/5000만원 계좌 크기

KOSPI PIT가 primary이고 KOSDAQ PIT는 comparator이다.
PASS는 연구 파이프라인 정상 완료이지 실전 승인 뜻이 아니다.
