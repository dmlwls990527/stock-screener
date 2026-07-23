# Stock Screener — 미국·국내 주식 팩터 분석 시스템

미국·국내 주식 시장의 시세·재무 데이터를 직접 수집(ETL)해 Tibero DB에 적재하고, SQL·Python으로 팩터를 설계해 종목을 스크리닝하는 개인 퀀트 분석 프로젝트입니다. 1인으로 기획·개발·운영하고 있습니다.

📝 **개발 과정(실제 에러·시행착오 포함) 전체 기록**: [Velog 시리즈 — 미국 주식 팩터 분석 (전체 13편)](https://velog.io/@dmlwls40166878/미국-주식-팩터-분석-0-시리즈-목차-읽는-법)

## 무엇을 하는 프로젝트인가

1. **수집(Extract)**: 국내는 pykrx + DART Open API, 미국은 yfinance + SEC EDGAR XBRL API에서 일별 시세·시가총액·분기 재무제표를 수집
2. **적재(Load)**: Tibero(Oracle 호환 RDBMS)에 스키마 설계 후 증분 적재
3. **분석(Transform)**: 모멘텀·밸류에이션·품질·매출 팩터를 계산해 종합점수로 종목 랭킹 산출
4. **검증**: IC(Information Coefficient) 통계 검증으로 각 팩터의 실제 예측력을 확인하고, look-ahead bias를 배제한 point-in-time 백테스트로 실제 수익률까지 확인
5. **자동화**: Linux crontab으로 매주 자동 갱신, Excel 리포트 생성

## 핵심 파일

| 파일 | 역할 |
|---|---|
| `create_tables_us.py` / `create_tables_kr.py` | DB 스키마(DDL) 생성 |
| `run_etl.py` | 가격·시가총액 ETL (pykrx / yfinance) |
| `etl_quarterly_sec.py` | SEC EDGAR 분기 매출 수집 |
| `etl_quarterly_dart.py` | DART Open API 분기 재무 수집(국내) |
| `etl_fundamental_us.py` | PER/PBR/ROE 등 펀더멘털 스냅샷 수집 |
| `factor_analysis.py` | 팩터 정규화·가중합·게이트 필터링 → Excel 리포트 |
| `backtest.py` | point-in-time 백테스트 (look-ahead bias 검증 포함) |
| `ic_test.py` | 팩터별 IC(예측력) 통계 검증 |
| `sector_dashboard.py`, `screen_*.py` 등 | 구형 스크리닝/시각화 스크립트(초기 버전, 계속 사용 중) |

## 이 프로젝트에서 실제로 배운 것들

- **min-max 정규화의 함정**: 이상치 하나가 스케일을 독식해 다른 팩터가 종합점수에 반영 안 되던 문제 → 순위(퍼센타일) 정규화로 해결
- **look-ahead bias**: 현재 시총 Top100을 고정 유니버스로 과거를 보면 수익률이 실제보다 부풀려짐(연 CAGR 기준 약 15%p 차이) → 시점별 유니버스 재구성으로 해결
- **"에러 없이 끝났다" ≠ "데이터가 최신화됐다"**: 외부 데이터 소스 구조 변경으로 ETL 일부가 조용히 실패해도 전체 파이프라인은 "정상 종료"를 찍는 경우를 겪고, 실패 시 자동 폴백 + 정기적 데이터 최신성 직접 검증으로 개선
- 자세한 과정은 위 Velog 시리즈에 전부 기록돼 있습니다.

## 실행 환경

- Python 3.11, Tibero 7 (JDBC), `requirements.txt` 참고
- 필요 환경변수: `TIBERO_USER`, `TIBERO_PASS`, `DART_API_KEY`, `KRX_ID`, `KRX_PW` (전부 코드에는 없고 실행 환경의 환경변수로 주입)

## 다음 계획

- 국내(KR) 버전 IC 검증 및 팩터 재설계
- Docker화, CI/CD 자동 검증 파이프라인
