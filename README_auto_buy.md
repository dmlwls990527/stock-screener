# auto_buy — 주도주 자동 매수 (페이퍼 모의투자 → 실계좌)

토스증권 Open API 에는 모의투자가 없다. 그래서 **실제 시세·실제 장 세션**으로 가상 계좌에
주문을 넣는 페이퍼 브로커(`broker_paper.py`)를 만들고, 실계좌 브로커(`broker_live.py`)와
**같은 코드 경로**(`auto_buy.py` + `rules.py`)를 타게 했다. 설정값 하나와 플래그로 전환한다.

```
leader_watchlist_latest.xlsx (주도주 시트, 기준일)
        │  rules.load_watchlist / select_candidates  (정렬·주의 제외·top_n)
        ▼
   후보 N종목  ── rules.size_orders (보유 skip, max_positions, 주간 한도, 종목당 금액) ──▶ 주문 의도
        │
        ▼
 auto_buy.py run ──▶ PaperBroker (paper/paper_state.json)   ← 기본
                └──▶ LiveBroker  (토스 실계좌)                ← 3중 잠금 모두 해제 시에만
```

## 1. 실행 — 번호 메뉴

```bash
cd /data/frame
./.venv/bin/python auto_buy.py          # 메뉴 (현재 모드가 맨 위에 [PAPER 모의] / [LIVE 실계좌] 로 표시)
```

| 번호 | 명령 | 하는 일 |
|---|---|---|
| 1 | `plan` | 이번 주 주문 계획 출력 + 현금 확인. 상태 변경 없음. `paper/last_plan.json` 저장 |
| 2 | `run` | 주문 실행. 같은 기준일은 두 번 실행 안 함(`--force` 로 강제). 정규장이 아니면 MARKET 은 전부 거절 |
| 3 | `tick` | 미체결 LIMIT 재검사(체결/만료) + 자산 평가 스냅샷 기록 |
| 4 | `status` | 현금, 보유(평가손익), 미체결, 총자산, 최대낙폭 |
| 5 | `report` | `paper_report_latest.xlsx` (요약/보유/체결내역/자산추이/설정) |
| 6 | `replay` | 과거 시뮬레이션 (`replay.py`, 기본 2025-01-06 ~ DB 최신) |
| 7 | `reset` | 페이퍼 상태 초기화 (`--yes` 없으면 y/N 확인) |
| 8 | `config` | 설정 출력 |

명령줄에서 바로: `./.venv/bin/python auto_buy.py plan` 처럼 서브커맨드로 호출.
모든 결정(후보/제외/건너뜀/거절/체결 사유)은 `logs/auto_buy_YYYYMMDD.log` 에 남는다.

## 2. 설정 — `auto_buy_config.json`

| 키 | 기본값 | 의미 |
|---|---|---|
| `mode` | `paper` | `paper` 모의 / `live` 실계좌 (live 는 잠금 ①) |
| `source.file` / `sheet` | 주도주 엑셀 / `주도주` | 후보를 읽을 시트. `설명` 시트의 `기준일` 이 실행 단위가 됨 |
| `source.sort_by` | `주도주점수` | 이 열 내림차순으로 정렬 |
| `source.top_n` | 5 | 상위 N 종목만 후보 |
| `source.exclude_if_주의` | true | `주의` 열이 비어있지 않은 종목 제외 (시클리컬 이익 정점 경고 등) |
| `source.exclude_sectors` / `exclude_tickers` | [] | 섹터명 / 티커 제외 목록 |
| `sizing.per_stock_usd` | 1000 | 종목당 매수 금액(USD) |
| `sizing.max_positions` | 10 | 보유 + 미체결 매수 + 신규 합쳐 최대 종목 수 (보유 종목 추가 매수는 자리를 안 먹음) |
| `sizing.weekly_cap_usd` | 3000 | 한 번 실행에 쓰는 총액 상한. 미체결 매수 주문이 잡아둔 금액은 이미 쓴 것으로 계산 |
| `sizing.skip_if_held` | true | 이미 보유한 종목은 건너뜀. 미체결 매수 주문이 있는 종목은 설정과 무관하게 항상 건너뜀 |
| `sizing.order_type` | `MARKET` | `MARKET`(정규장에서만 접수) / `LIMIT`(pre/regular/after/day 모두 접수, 아래 §4 주의) |
| `sizing.use_amount_orders` | true | MARKET 일 때 금액주문(소수점 수량). 끄면 `floor(금액/시세)` 주 |
| `sizing.limit_offset_pct` | 0.5 | LIMIT 일 때 시세 × (1+0.5%) 를 지정가로 (호가 단위 반올림) |
| `sizing.min_order_usd` | 50 | 이보다 작은 주문은 안 냄 |
| `exit.enabled` | false | (replay 전용) 리스트에서 `weeks_absent` 회 연속 빠진 종목 매도 |
| `paper.initial_cash_usd` / `krw` | 10000 / 0 | 가상 계좌 초기 현금 |
| `paper.slippage_bps` | 5 | MARKET 체결가 = 시세 × (1 ± 0.05%) |
| `paper.commission_pct` | 0.1 | 체결금액의 0.1% 수수료 |
| `paper.fx_spread_pct_market` / `_off` | 0.05 / 0.5 | KRW→USD 환전 스프레드 (09:00~15:30 KST / 그 외) |
| `paper.auto_fx` | false | USD 부족 시 KRW 자동 환전(페이퍼 전용 가정, 토스에 환전 API 없음) |
| `paper.state_path` | `/data/frame/paper/paper_state.json` | 가상 계좌 상태 파일 |
| `schedule.place_at_session` | `regularMarket` | 주문 넣는 세션 (안내용) |
| `live.account_seq` | null | 실계좌 accountSeq (토스 `GET /accounts` 의 값, 보통 1) |
| `replay.financial_lag_days` | 45 | (replay 전용) 분기 재무를 분기말 + N일 뒤에야 안 것으로 취급 (10-Q 제출 지연 흉내) |

페이퍼 브로커가 흉내 내는 토스 규칙: MARKET 은 정규장(22:30~05:00 KST, 서머타임)만 / 금액주문은
정규장 종료 1시간 전까지 / BUY 소수점은 금액주문으로만 / 호가단위 $1 미만 0.0001·이상 0.01 /
같은 종목 반대방향 미체결 있으면 409 / 같은 날 같은 clientOrderId 거절 / DAY 주문은 정규장 종료에 만료.
거절 코드 문자열은 토스 스펙과 같게 맞췄다(`order-hours-closed`, `insufficient-buying-power`,
`amount-order-outside-regular-hours`, `fractional-quantity-outside-regular-hours`, `opposite-pending-order-exists`)
→ 페이퍼 로그와 실계좌 로그를 같은 코드로 비교할 수 있다.

**손익 회계(토스 방식)**: 매수 수수료는 평균단가에 포함, 매도 수수료는 실현손익에서 차감.
그래서 `순손익 = 실현손익 + 평가손익 = 총자산 − 초기자산` 이 정확히 맞는다. `누적수수료` 는 참고용(이미 손익에 반영).
지정가 체결가는 BUY `min(지정가, 시세)` / SELL `max(지정가, 시세)`.

**`run` 이 기준일을 "실행 완료" 로 기록하는 조건**: 체결이 1건 이상이거나, 주문 0건의 사유가 전부 정당(이미 보유 /
max_positions / 주간 한도 / 예산 부족)할 때만. 후보가 0개(시트 비었음), 시세 조회 실패(토스 API 장애), 지정가 미체결만
남은 경우에는 기록하지 않아 다음 실행(겨울철 23:31 재실행, 수동 재시도)이 그대로 통과한다. 시세 장애·빈 시트는 exit 1.

## 3. 페이퍼 → 실전 전환 절차 (3중 잠금)

실계좌 주문은 아래 **셋을 전부** 만족할 때만 나간다. 하나라도 빠지면 `broker_live.py` 를 import 조차
하지 않고 exit code 3 으로 멈춘다 (페이퍼로 조용히 떨어지지도 않는다).

1. `auto_buy_config.json` 에서 `"mode": "live"`, `"live": {"account_seq": 1}` 로 바꾼다.
2. 실행 시 `--live` 플래그를 붙인다.
3. 환경변수 `AUTO_BUY_LIVE_OK=1` 을 준다. 터미널(tty)에서 실행하면 추가로 `LIVE` 를 직접 타이핑해야 한다.

```bash
# 실계좌 주문 (실제 돈이 움직인다)
AUTO_BUY_LIVE_OK=1 ./.venv/bin/python auto_buy.py run --live
# 실계좌 잔고 기준 계획만 보기 (주문 안 나감, env 불필요)
./.venv/bin/python auto_buy.py plan --live
```

권장 순서: 페이퍼로 최소 4~8주 돌려 `report` 의 수익률·최대낙폭·거절 사유를 확인 → `replay` 로
과거 구간 검증 → 실계좌 USD 예수금 입금(auto_fx 는 페이퍼 전용) → 위 3단계.
메뉴의 2번(run)은 `--live` 를 붙이지 않으므로 메뉴에서는 절대 실계좌 주문이 나가지 않는다.

## 4. 크론 제안 (서머타임 기준, 미국 정규장 22:30~05:00 KST)

```cron
# 월요일 08:00 weekly_cron.sh 가 주도주 엑셀을 갱신한 뒤, 정규장 시작 1분 후 매수
31 22 * * 1   cd /data/frame && source ~/.bashrc && ./.venv/bin/python auto_buy.py run   >> logs/auto_buy_cron.log 2>&1
# 화~토 06:10 (정규장 종료 05:00 후) 미체결 정리 + 자산 평가 기록
10 6  * * 2-6 cd /data/frame && source ~/.bashrc && ./.venv/bin/python auto_buy.py tick  >> logs/auto_buy_cron.log 2>&1
```

겨울(11월 첫째 일요일 ~ 3월 둘째 일요일)에는 정규장이 23:30~06:00 KST 로 1시간 밀린다.
그때는 `31 23 * * 1` / `10 7 * * 2-6` 으로 바꾸거나, 안전하게 `run` 을 두 번 걸어둔다
(22:31 은 preMarket 이라 MARKET 이 전부 거절되고 기준일이 기록되지 않으므로 23:31 재실행이 그대로 통과한다).
같은 기준일은 한 번만 실행되므로 두 줄이 겹쳐도 이중 매수는 없다.

**`sizing.order_type = LIMIT` 로 쓸 때의 제약**: DAY 지정가는 정규장 종료(05:00)에 만료되는데 위 크론은 정규장 중에
`tick` 을 돌리지 않는다 → 즉시 체결되지 않은 지정가는 06:10 tick 에서 EXPIRED 만 된다. 지정가를 쓰려면 정규장 중
tick 을 추가할 것 (겨울에는 `23,0-5`):

```cron
# LIMIT 사용 시에만: 정규장 중 30분마다 미체결 지정가 재검사 (화~토 새벽 = 월~금 미국 정규장)
*/30 23,0-4 * * 2-6 cd /data/frame && source ~/.bashrc && ./.venv/bin/python auto_buy.py tick >> logs/auto_buy_cron.log 2>&1
```

지정가가 하나도 체결되지 않고 만료되면 기준일이 기록되지 않은 상태라 다음 `run` 이 다시 시도한다. 대기 중에 `run` 이
다시 돌아도 같은 종목은 `미체결 매수 대기` 로 건너뛰고 예약금은 주간 한도에서 빠지므로 이중 주문·한도 초과는 없다.
기본값(MARKET 금액주문)은 즉시 체결이라 이 제약과 무관하다.

## 5. 파일 위치

| 파일 | 내용 |
|---|---|
| `/data/frame/auto_buy.py` | CLI + 메뉴 + 브로커 선택(잠금) |
| `/data/frame/rules.py` | 후보 선정·주문 크기 (auto_buy 와 replay 공용) |
| `/data/frame/broker_paper.py` | 페이퍼 브로커 (상태 JSON, 원자적 저장) |
| `/data/frame/broker_live.py` | 실계좌 브로커 (`allow_live=True` + `AUTO_BUY_LIVE_OK` 필요) |
| `/data/frame/replay.py` | 과거 시뮬레이션 (`paper_replay_latest.xlsx`, 캐시 `paper/replay_cache/`) |
| `/data/frame/auto_buy_config.json` | 설정 (없으면 기본값으로 자동 생성) |
| `/data/frame/paper/paper_state.json` | 가상 계좌 상태 (현금·보유·미체결·체결·거절·자산추이·meta) |
| `/data/frame/paper/last_plan.json` | 마지막 plan/run 의 계획 |
| `/data/frame/paper_report_latest.xlsx` | `report` 결과 |
| `/data/frame/logs/auto_buy_YYYYMMDD.log` | 실행 로그 |
| `/data/frame/tests/test_paper.py` | `./.venv/bin/python -m unittest tests.test_paper -v` |
| `/data/frame/tests/test_replay.py` | `./.venv/bin/python -m unittest tests.test_replay -v` (DB 없이 가짜 시세로 replay 검증) |
| `/data/frame/paper_replay_latest.xlsx` | `replay` 결과 (요약/자산추이/거래내역/리밸런스별목록/리밸런스요약/설정) |
| `/data/frame/paper/replay_state.json` | replay 전용 가상 계좌 (매 실행마다 새로 만듦, `paper_state.json` 과 별개) |
| `/data/frame/paper/replay_cache/` | 기준일별 스크리닝 결과 + 가격 패널 캐시 (`screen_v2_lag45_YYYY-MM-DD.pkl`, `prices_v2_*.pkl`; v1 파일은 무시됨, 지워도 됨) |

`paper/` 디렉토리와 `paper_report_latest.xlsx` / `paper_replay_latest.xlsx` 는 산출물이라 git 에 넣지 않는다(`.gitignore`).

## 6. replay — 과거 구간 되감기 (`replay.py`)

같은 `rules.py` 규칙과 같은 설정으로 과거에 매주(또는 매월) 샀다면 어땠는지 돌려본다.
페이퍼 계좌(`paper_state.json`)는 건드리지 않고 `paper/replay_state.json` 을 매번 새로 만든다.

```bash
./.venv/bin/python auto_buy.py replay                                   # 2025-01-06 ~ DB 최신, 매주
./.venv/bin/python auto_buy.py replay --cadence monthly --start 2026-03-02
./.venv/bin/python auto_buy.py replay --start 2025-06-02 --end 2025-12-31 --refresh   # 캐시 무시하고 재계산
```

| 단계 | 내용 |
|---|---|
| 리밸런스일 | `weekly` = 시작일 이후 모든 월요일, `monthly` = 매월 첫 월요일 (시작 달은 시작일 이후 첫 월요일) |
| 기준일 | 리밸런스일 − 3일 이하의 마지막 거래일 (보통 직전 금요일). 실제 크론(월 08:00 엑셀 갱신)과 같은 타이밍 |
| 스크리닝 | `leader_screener` 의 `screen_now()` 를 그 기준일로 재현 (factor_eval.py 와 같은 순서). 분기 재무는 분기말 + `replay.financial_lag_days`(45일) 뒤에야 안 것으로 자른다(공시 지연 흉내). 1회 10~20초, 기준일·지연일수별로 `paper/replay_cache/` 에 캐시 → 두 번째 실행부터는 수 초 |
| 후보·크기 | `rules.select_candidates` → `rules.size_orders` (auto_buy 와 완전히 같은 함수·설정·인자) |
| 체결 | 리밸런스일 이후 첫 거래일 **시가 × (1 + slippage_bps)**, 수수료 `commission_pct` 차감 — PaperBroker 그대로 사용 |
| 평가 | 매 거래일 종가로 총자산 기록 → 최대낙폭 |
| 이탈 | `exit.enabled=true` 면 주도주 시트에서 `weeks_absent` 회 연속 빠진 보유 종목을 다음 시가에 전량 매도 |
| 벤치마크① | 첫 기준일의 스크리너 유니버스(재무 보유 종목, 부동산 제외) 동일가중 매수후보유(첫 체결일 전액 투입) 수익률 + 종목별 수익률 중앙값 (비용 0) |
| 벤치마크② | 같은 유니버스를 **전략과 같은 현금 스케줄**로 (리밸런스마다 전략이 실제 쓴 금액만큼 동일가중 투입, 나머지 현금) → 현금 드래그를 뺀 순수 종목 선택 효과 비교 |

요약 시트에서 볼 것: 수익률(현금 포함), 순손익(=실현+평가), 최대낙폭, 회전율(`(총매수+총매도)÷2÷평균자산`),
현금소진 시점(현금이 `min_order_usd` 아래로 떨어진 첫 리밸런스), 초과수익 두 줄(전략 − ①, 전략 − ②).
`weekly_cap_usd` 는 리밸런스 1회당 한도라 `monthly` 에서도 같은 값이 쓰인다.

정직한 한계: 분기 재무는 `financial_lag_days` 로 지연을 흉내 낼 뿐 실제 공시일은 DB 에 없다(근사) / 섹터·이름은 현재 매핑 /
상장폐지 종목은 유니버스에 없음(생존편향) / `daily_price_us` 는 분할 미조정이라 하루에 시가·종가가 정수비(2:1, 10:1, 1:2…)에
±3% 로 튀고 **`daily_marcap_us.STOCKS`(발행주식수)가 ±5거래일 안에 같은 배수로 변한 날만** 분할로 보고 소급 조정한다.
STOCKS 가 안 변한 급등락(인수 발표·급락 등 진짜 가격 이벤트)은 조정하지 않고 요약 시트에 "STOCKS 미확인" 으로 따로 적는다 /
시가 체결 가정, 배당·이자 미반영. `leader_screener.py` 를 고친 뒤에는 `--refresh` 로 캐시를 다시 만들 것(경고가 뜬다).

---

## v2 규칙 (2026-09-28 확정, `sizing.method = "score_weight"`)

| 항목 | 값 | 설정 키 |
|---|---|---|
| 대상 | 주도주 시트 상위 50종 (지금은 통과 36종 전부) | `source.top_n` |
| 자본 | 1,000만원 → 첫 초기화 때 토스 환율 × (1+0.05%) 로 달러 전환 | `paper.initial_cash_krw`, `convert_at_start` |
| 매수 비중 | 점수 가중: 목표비중 = 내 점수 ÷ 목록 점수 합, 목표금액 = 비중 × 총자산 | — |
| 신규 매수 | 목표금액만큼 금액주문(소수점). 현금이 모자라면 신규끼리 비중대로 축소 | `sizing.min_order_usd` |
| 보유 종목 | **B 추가매수만**: 목표보다 30% 이상 모자라면 차이만큼 더 산다. 줄이지 않음 | `rebalance.mode` (A/B/C), `topup_threshold_pct` |
| 매도 | 추적손절 −15% (보유 후 최고가 대비) 또는 게이트 탈락 2주 연속, 먼저 걸리는 쪽 → 전량 시장가 | `exit.trailing_stop_pct`, `exit.gate_absent_weeks` |
| 재매수 | 제한 없음 (같은 실행 안에서 판 종목만 다시 안 삼) | — |
| 주문 시각 | 정규장 시작 + 45분 ~ 마감 1시간 전 (금액주문 가능 구간) | `schedule.entry_offset_min` |

현금 우선순위: 매도 대기 청산 → (C 모드) 비중초과 매도 → 신규 → 추가매수. 둘 다 점수 순.

### 명령
- `run` — 이번 기준일 첫 실행만 동작: 매도조건 갱신 → 매도 대기 청산 → 매수. 같은 기준일 두 번째부터는 rc 3.
- `exits` — 매수 없이 매도 대기 종목만 청산 (화~금).
- `tick` — 장 마감 뒤: 최고가 갱신, 추적손절 판정(다음 진입 시각에 매도), 자산 기록, 리포트 갱신.
- `replay --compare` — v1 균등 / v2-A / v2-B / v2-C 를 같은 스크리닝으로 비교 → `paper_replay_latest.xlsx` 의 `비교` 시트.

### 크론 (claude 사용자 crontab)
```
15 23 * * 1-5 /bin/bash /data/frame/auto_buy_cron.sh run exits
15 0  * * 2-6 /bin/bash /data/frame/auto_buy_cron.sh run exits
10 6  * * 2-6 /bin/bash /data/frame/auto_buy_cron.sh tick
```
23:15 와 00:15 두 번 거는 이유: 서머타임엔 정규장이 22:30, 겨울엔 23:30 에 열린다. 진입 창(+45분)에 맞지 않는 쪽은 rc 3 으로 그냥 끝난다.
`run` 을 매일 거는 이유: 월요일이 미국 휴장이면 화요일에 그 주 매수를 한다 (이미 했으면 rc 3).
로그: `logs/auto_buy_cron_YYYYMM.log` + `logs/auto_buy_YYYYMMDD.log`.
