# v0.25-KR research branch

이 브랜치는 미국 `NORA_LEVEL_RR_A_RAW` 신호 규칙을 바꾸지 않고
KOSPI/KOSDAQ에 그대로 이식하는 cross-market replication 전용입니다.

## 실행
GitHub Actions:
`Noramu v0.25-KR Cross-Market Replication`

첫 성공 실행에서:
- FinanceDataReader listing으로 KOSPI 상위 40 / KOSDAQ 상위 40을 동결
- `kr_state/kr_universe_v025.csv` 저장
- Yahoo 60m 정규장 데이터로 exact LEVEL_RR 신호 생성
- KOSPI / KOSDAQ / KR80 분리 백테스트
- concentration / quarter / generic cost stress 출력

## 중요한 제한
- 현재 universe를 과거에 적용하므로 survivorship bias.
- Yahoo native 60m의 마지막 15:00 bar는 30분일 수 있음.
- baseline 5bps는 한국 실제 수수료/세금 모델이 아님.
- fractional shares는 미국 엔진과 구조 비교용.
- 이 결과로 실거래 승인하지 않음.

다음 단계는 결과를 본 뒤에도 미국 frozen rule은 건드리지 않고:
1. KR data/session sanity audit
2. exact Korean whole-share/tick/tax execution model
3. KOSPI/KOSDAQ robustness
순서로 진행합니다.
