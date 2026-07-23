# Stock Screener — 미국·국내 주식 팩터 분석 시스템

미국·국내 주식 시장의 시세·재무 데이터를 직접 수집(ETL)해 자체 DB(Tibero)에 적재하고, SQL·Python으로 팩터를 설계해 종목을 스크리닝하는 개인 퀀트 분석 프로젝트입니다. 데이터 수집부터 DB 모델링, 팩터 스코어링, 통계 검증(IC), 백테스트, 주간 자동화까지 **1인으로 기획·개발·운영**하고 있습니다.

> 📝 **개발 과정 전체 기록 (실제 에러·시행착오 포함, Velog 13편):**
> [미국 주식 팩터 분석 시리즈 — 목차](https://velog.io/@dmlwls40166878/미국-주식-팩터-분석-0-시리즈-목차-읽는-법)

---

## 목차

1. [전체 아키텍처](#1-전체-아키텍처)
2. [ETL 파이프라인](#2-etl-파이프라인)
3. [DB 테이블 구조 (ERD)](#3-db-테이블-구조-erd)
4. [팩터 스코어링 & 검증 흐름](#4-팩터-스코어링--검증-흐름)
5. [파일 구성](#5-파일-구성)
6. [이 프로젝트에서 실제로 배운 것들](#6-이-프로젝트에서-실제로-배운-것들)
7. [실행 방법](#7-실행-방법)

---

## 1. 전체 아키텍처

외부 데이터 소스에서 수집한 데이터를 Tibero DB에 적재하고, 팩터 스코어링을 거쳐 Excel 리포트로 내보냅니다. 무거운 수집·분석은 항상 켜져 있는 리눅스 서버가 crontab으로 독립 수행하고, 로컬 PC는 결과 파일만 받아옵니다.

```mermaid
flowchart LR
    subgraph EXT["외부 데이터 소스"]
        direction TB
        S1["pykrx<br/>(국내 시세·펀더멘털)"]
        S2["yfinance<br/>(미국 시세·펀더멘털)"]
        S3["SEC EDGAR XBRL<br/>(미국 분기 재무)"]
        S4["DART OpenAPI<br/>(국내 분기 재무)"]
    end

    subgraph SRV["리눅스 서버 (crontab 주간 자동화)"]
        direction TB
        ETL["ETL<br/>run_etl.py / etl_*.py"]
        DB[("Tibero DB<br/>10 tables")]
        FA["팩터 스코어링<br/>factor_analysis.py"]
        VER["검증<br/>ic_test.py / backtest.py"]
        XLSX["Excel 리포트<br/>factor_result_*.xlsx"]
    end

    LOCAL["로컬 PC (Windows)<br/>결과 파일 수신"]

    S1 & S2 & S3 & S4 --> ETL
    ETL --> DB
    DB --> FA
    DB --> VER
    FA --> XLSX
    XLSX -->|"scp 주간 pull"| LOCAL
```

---

## 2. ETL 파이프라인

수집(Extract) → 적재(Load)를 시장별·데이터 종류별로 나눠서 수행합니다. 매주 실행 시 이미 적재된 날짜는 자동 스킵(증분 적재)하고, 외부 소스 수집이 실패하면 기존 DB 유니버스로 **폴백**해 파이프라인이 통째로 멈추지 않게 설계했습니다.

```mermaid
flowchart TD
    START(["run_etl.py 실행<br/>(--kr / --us)"])

    subgraph KR["국내 (pykrx)"]
        direction TB
        K1["종목 마스터 수집"] --> K2["영업일 루프<br/>증분 적재"]
        K2 --> K3["시세 / 시가총액 / 펀더멘털<br/>daily_price · daily_marcap · daily_fundamental"]
        K3 -.->|"공휴일 0값 행 필터"| K3
    end

    subgraph US["미국 (yfinance + 위키피디아 유니버스)"]
        direction TB
        U1["S&P500 + NASDAQ-100<br/>유니버스 스크래핑"]
        U1 -.->|"스크래핑 실패 시<br/>기존 DB 유니버스로 폴백"| U2
        U2["OHLCV 벌크 다운로드<br/>증분 적재"]
        U2 --> U3["시세 / 시가총액 / 펀더멘털<br/>daily_price_us · daily_marcap_us · daily_fundamental_us"]
    end

    subgraph FIN["분기 재무 (별도 스크립트)"]
        direction TB
        F1["etl_quarterly_sec.py<br/>SEC EDGAR XBRL"] --> F2["ticker→CIK 매핑<br/>ASC606 태그 통합<br/>단일분기(70~100일)만 인정"]
        F2 --> F3["quarterly_financials_us<br/>UPSERT (MERGE)"]
        F4["etl_quarterly_dart.py<br/>DART OpenAPI"] --> F5["quarterly_financials_kr<br/>UPSERT (MERGE)"]
    end

    START --> KR
    START --> US
    START --> FIN
```

**설계 포인트**
- **증분 적재**: `get_loaded_dates()`로 이미 적재된 날짜를 조회해 새 거래일만 수집 → 매주 재실행해도 몇 초.
- **UPSERT**: 분기 재무는 Tibero `MERGE INTO`로 (code, end_date) 기준 있으면 UPDATE, 없으면 INSERT.
- **SEC 태그 함정 처리**: 2018년 회계기준(ASC 606) 변경으로 매출 태그가 갈리는 문제 → 여러 태그를 우선순위로 통합. 단일분기와 누적치(YTD)가 섞여 나오는 문제 → `(end-start)`가 70~100일인 것만 단일분기로 인정.

---

## 3. DB 테이블 구조 (ERD)

시장별로 5개씩, 총 **10개 테이블**입니다. 모든 테이블은 종목코드(`code`)를 공통 키로 `ticker_master`(종목 마스터)와 논리적으로 연결됩니다. (실제 DDL은 대량 적재 속도를 위해 FK 제약 대신 PK/인덱스만 사용 — 아래 관계선은 조인 기준을 나타냅니다.)

### 미국 (US)

```mermaid
erDiagram
    TICKER_MASTER_US ||--o{ DAILY_PRICE_US : "code"
    TICKER_MASTER_US ||--o{ DAILY_MARCAP_US : "code"
    TICKER_MASTER_US ||--o{ DAILY_FUNDAMENTAL_US : "code"
    TICKER_MASTER_US ||--o{ QUARTERLY_FINANCIALS_US : "code"

    TICKER_MASTER_US {
        varchar code PK
        varchar name
        varchar sector
        varchar market
    }
    DAILY_PRICE_US {
        date   date_
        varchar code
        number open
        number high
        number low
        number close
        number volume
        number amount
        number changes_ratio
    }
    DAILY_MARCAP_US {
        date   date_
        varchar code
        number close
        number marcap
        number stocks
        number rank
    }
    DAILY_FUNDAMENTAL_US {
        date   date_
        varchar code
        number per
        number pbr
        number eps
        number eps_growth
        number roe
        number revenue_growth
        number debt_to_equity
        number operating_margin
    }
    QUARTERLY_FINANCIALS_US {
        varchar code PK
        date   end_date PK
        varchar fp
        number revenue
        number op_income
    }
```

### 국내 (KR)

```mermaid
erDiagram
    TICKER_MASTER ||--o{ DAILY_PRICE : "code"
    TICKER_MASTER ||--o{ DAILY_MARCAP : "code"
    TICKER_MASTER ||--o{ DAILY_FUNDAMENTAL : "code"
    TICKER_MASTER ||--o{ QUARTERLY_FINANCIALS_KR : "code"

    TICKER_MASTER {
        varchar code PK
        varchar name
        varchar market
    }
    DAILY_PRICE {
        date   date_
        varchar code
        number open
        number high
        number low
        number close
        number volume
        number amount
        number changes_ratio
    }
    DAILY_MARCAP {
        date   date_
        varchar code
        number close
        number marcap
        number volume
        number amount
        number stocks
        number rank
    }
    DAILY_FUNDAMENTAL {
        date   date_
        varchar code
        number bps
        number per
        number pbr
        number eps
        number div
        number dps
    }
    QUARTERLY_FINANCIALS_KR {
        varchar code PK
        date   end_date PK
        varchar fp
        number revenue
        number op_income
        number net_income
        number total_equity
        number total_liabilities
    }
```

**테이블 역할 요약**

| 테이블 | 역할 | 핵심 컬럼 |
|---|---|---|
| `ticker_master(_us)` | 종목 마스터 (코드↔종목명↔섹터) | code(PK), name, sector, market |
| `daily_price(_us)` | 일별 시세(OHLCV) + 거래대금 | date_, code, close, amount |
| `daily_marcap(_us)` | 일별 시가총액 + **그날의 시총 순위** | date_, code, marcap, rank |
| `daily_fundamental(_us)` | 펀더멘털 **현재값 스냅샷** (과거 히스토리 없음) | per, pbr, roe, debt_to_equity … |
| `quarterly_financials_us/kr` | **분기별 재무 히스토리** (point-in-time 검증의 핵심) | code+end_date(PK), revenue, op_income |

> `daily_fundamental`(yfinance 오늘값 스냅샷)과 `quarterly_financials`(SEC/DART 분기 히스토리)를 **분리**한 게 핵심 설계입니다. IC 검증·백테스트처럼 "그 시점에 알 수 있었던 값"이 필요한 작업엔 반드시 후자를 씁니다. 안 그러면 미래 데이터를 과거에 쓰는 look-ahead bias에 빠집니다.

---

## 4. 팩터 스코어링 & 검증 흐름

시총 Top100 종목에 팩터 **4종**을 계산해 하나의 종합점수로 합치고, 그 점수 순으로 종목을 줄 세웁니다. 점수가 실제로 맞는지는 IC 검증·백테스트로 따로 확인합니다.

```mermaid
flowchart TD
    U(["유니버스<br/>시총 Top100"])
    U --> M["모멘텀"]
    U --> V["밸류에이션"]
    U --> Q["품질"]
    U --> R["매출"]
    M --> S["종합점수"]
    V --> S
    Q --> S
    R --> S
    S --> OUT(["종목 랭킹<br/>→ Excel"])
    S -. 검증 .-> CHK{{"IC 검증<br/>· 백테스트"}}
```

### 각 팩터 설명

| 팩터 그룹 | 무엇을 보나 | 세부 지표 |
|---|---|---|
| **모멘텀** | 주가·거래가 얼마나 빠르고 꾸준히 커지는가 | 시총·거래대금의 성장률 / 가속도 / 일관성 |
| **밸류에이션** | 지금 얼마나 비싸게 사는가 | PER · PBR · PEG |
| **품질** | 회사가 실제로 돈을 잘 버는가 | ROE · 영업이익률 · 부채비율 |
| **매출** | 매출이 꾸준히·빠르게 늘고 있는가 | 매출 YoY 일관성 / 가속도 / 성장률 (SEC·DART) |

### 계산 과정

1. **유니버스 선정** — `daily_marcap`에서 시총 상위 100종목을 고릅니다.
2. **순위 정규화** — 각 팩터를 값의 크기가 아니라 **순위(0~1)** 로 바꿉니다. (이상치 하나가 점수를 독식하는 min-max의 문제를 피하려고)
3. **게이트 필터** — 값이 확실히 나쁜 종목만 후보에서 뺍니다(ROE < 0, 부채비율 D/E > 500). 값이 없으면 통과시킵니다.
4. **종합점수** — `Σ(정규화값 × 가중치) × 100`. 가중치는 감이 아니라 **IC 검증 결과**를 근거로 배분합니다.
5. **랭킹 → Excel** — 종합점수 순으로 정렬해 리포트를 만듭니다.

### 최근 분기에 더 무게를 줍니다 (기간 가중치)

모멘텀 팩터(시총·거래대금 성장률)는 최근 4년치(16분기) 분기별 성장률(QoQ%)의 **가중 평균**입니다. 이때 오래된 분기와 최근 분기를 똑같이 취급하지 않고, **최근 분기일수록 가중치를 높게** 줍니다. 3년 전에 반짝했던 것보다 지금 잘하고 있는 게 더 중요하다고 봤기 때문입니다.

```
분기 가중치 = 1.00 + (분기 인덱스 × 0.05)
성장률 점수 = Σ(분기 QoQ% × 가중치) ÷ Σ(가중치)
```

가장 오래된 분기(인덱스 0)는 **1.00배**, 가장 최근 분기(인덱스 15)는 **1.75배**로 선형 증가합니다.

| 시점 | 분기 인덱스 | 가중치 |
|---|---|---|
| 4년 전 (가장 오래됨) | 0 | ×1.00 |
| 3년 전 | 4 | ×1.20 |
| 2년 전 | 8 | ×1.40 |
| 1년 전 | 12 | ×1.60 |
| 최근 분기 (가장 최신) | 15 | ×1.75 |

여기에 더해 **가속도**(최근 4분기 평균 QoQ − 전체 평균 QoQ)를 따로 계산해서, 성장 속도가 최근 들어 빨라지는지(양수) 느려지는지(음수)도 봅니다. 즉 "얼마나 컸나(성장률)" 뿐 아니라 "요즘 더 빨라지고 있나(가속도)"까지 함께 보는 구조입니다.

> ⚠️ 헷갈리기 쉬운 두 가지 가중치를 구분하면 —
> - **기간 가중치** (여기): 팩터 *하나*를 계산할 때, 최근 분기에 더 무게를 주는 것.
> - **종합 가중치** (위 4번): 팩터 *4종을 서로 합칠 때* 쓰는 비중으로, IC 검증 결과를 근거로 배분한 것.

> **핵심 원칙**
> - 모든 계산은 **point-in-time** — 그 시점에 실제로 알 수 있었던 데이터만 씁니다 (SEC 매출은 공시 지연 90일 반영).
> - 팩터는 "그럴듯한 것"이 아니라 **"IC로 검증된 것"만** 가중치를 줍니다 — 유의성 없는 팩터는 점수에서 빼거나 게이트로 강등합니다.

---

## 5. 파일 구성

| 파일 | 역할 |
|---|---|
| `create_tables_us.py` / `create_tables_kr.py` | DB 스키마(DDL) 생성 |
| `run_etl.py` | 가격·시가총액 ETL (pykrx / yfinance, 증분·폴백) |
| `etl_quarterly_sec.py` | SEC EDGAR 분기 매출 수집 (미국) |
| `etl_quarterly_dart.py` | DART OpenAPI 분기 재무 수집 (국내) |
| `etl_fundamental_us.py` | PER/PBR/ROE 등 펀더멘털 스냅샷 수집 |
| `factor_analysis.py` | 팩터 정규화·가중합·게이트 → Excel 리포트 |
| `ic_test.py` | 팩터별 IC(예측력) 통계 검증 |
| `backtest.py` | point-in-time 백테스트 (look-ahead bias 검증 포함) |
| `db_backup.py` / `db_restore.py` | 전 테이블 CSV 스냅샷 백업·복구 |
| `weekly_cron.sh` | 주간 자동화 (ETL → 스크리닝 순차 실행) |
| `screen_*.py`, `*_viz.py`, `sector_dashboard.py` | 구형 스크리닝/시각화 스크립트 (초기 버전, 계속 사용) |

---

## 6. 이 프로젝트에서 실제로 배운 것들

- **min-max 정규화의 함정**: 이상치 하나가 0~1 스케일을 독식해 다른 팩터가 종합점수에 반영 안 되던 문제 → **순위(퍼센타일) 정규화**로 해결 (PBR 440배짜리 이상치 하나가 나머지 종목을 전부 뭉개고 있었음).
- **look-ahead bias**: 현재 시총 Top100을 고정 유니버스로 과거를 보면 수익률이 실제보다 부풀려짐 (CAGR 31.7% → point-in-time 16.5%, 약 2배 차이) → **시점별 유니버스 재구성**으로 해결.
- **"에러 없이 끝났다" ≠ "데이터가 최신화됐다"**: 외부 데이터 소스(위키피디아) 구조 변경으로 ETL 일부가 조용히 실패해도 전체 파이프라인은 "정상 종료"를 찍는 경우를 겪음 → 실패 시 자동 폴백 + `SELECT MAX(date_)`로 실제 최신성 직접 검증하는 습관.
- **통계적으로 유의한 팩터는 생각보다 드물다**: 검증 가능한 10개 팩터를 33분기 IC로 전수검증했더니 `|t|>2`를 넘는 게 하나도 없었음 → "그럴듯한 지표"와 "실제로 작동하는 지표"는 다르다는 걸 숫자로 확인.

> 각 항목의 상세 과정은 위 Velog 시리즈에 코드·에러 메시지·시행착오까지 전부 기록돼 있습니다.

---

## 7. 실행 방법

```bash
# 0) 환경변수 (코드에는 비밀값 없음 — 실행 환경에서 주입)
export TIBERO_USER=... TIBERO_PASS=...
export DART_API_KEY=...          # 국내 분기재무
export KRX_ID=... KRX_PW=...     # 국내 펀더멘털

# 1) 테이블 생성 (최초 1회)
python3 create_tables_us.py
python3 create_tables_kr.py

# 2) ETL (증분 적재)
python3 run_etl.py --us          # 미국 시세·시총
python3 etl_quarterly_sec.py     # 미국 분기 매출
python3 etl_fundamental_us.py    # 미국 펀더멘털 스냅샷
python3 run_etl.py --kr          # 국내

# 3) 팩터 스코어링 → Excel 리포트
python3 factor_analysis.py            # 미국
python3 factor_analysis.py --kr       # 국내
python3 factor_analysis.py --refresh  # ETL 최신화 후 스크리닝까지 한 번에

# 4) 검증
python3 backtest.py              # point-in-time 백테스트
python3 ic_test.py               # 팩터 IC 전수검증
```

**실행 환경**: Python 3.11, Tibero 7 (JayDeBeApi / JDBC), `requirements.txt` 참고.
