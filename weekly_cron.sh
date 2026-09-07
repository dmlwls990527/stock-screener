#!/bin/bash
# weekly_cron.sh — 매주 월요일 08:00 KST: 미장+국장 증분 적재 + 스크리닝 + 백업 + 주도주 push
source ~/.bashrc
cd /data/frame
LOG="logs/cron_weekly_$(date +%Y%m%d).log"

# 단계 하나가 외부 API(SEC EDGAR / yfinance / DART)에서 멈춰도 주간 실행 전체가
# 물리지 않도록 timeout 을 걸고, 성공/실패/타임아웃을 로그에 한 줄로 남긴다.
run_stage() {   # run_stage "<이름>" <제한초> <스크립트> [인자...]
  local name="$1" tmo="$2"; shift 2
  local t0=$SECONDS
  echo "--- $name ---" >> "$LOG"
  if timeout "$tmo" ./.venv/bin/python "$@" >> "$LOG" 2>&1; then
    echo "  [OK] $name ($((SECONDS-t0))s)" >> "$LOG"
  else
    local rc=$?
    if [ "$rc" -eq 124 ]; then
      echo "  [TIMEOUT] $name (${tmo}s 초과)" >> "$LOG"
    else
      echo "  [FAIL] $name (exit $rc, $((SECONDS-t0))s)" >> "$LOG"
    fi
  fi
}

echo "=== $(date) 시작 ===" >> "$LOG"
run_stage "가격·시총 적재 (KR+US)" 5400 run_etl.py

# --- 분기재무 ETL (2026-09-07 추가) ---
# factor_analysis.py 는 --refresh 를 줘야만 이 3개를 돌리는데 크론은 --refresh 없이 부른다.
# 그래서 가격만 매주 최신이고 재무는 수동 실행 때까지 조용히 낡고 있었다.
# 실측(2026-09-07): quarterly_financials_us 8/02, daily_fundamental_us 7/07(2개월 정지),
#                   quarterly_financials_kr 2026-03-31 — 2분기가 통째로 비어 있었다.
run_stage "미국 분기재무 (SEC EDGAR)"   2700 etl_quarterly_sec.py
run_stage "미국 밸류에이션 (PER/PBR/ROE)" 1200 etl_fundamental_us.py
run_stage "국내 분기재무 (DART)"        1800 etl_quarterly_dart.py

run_stage "팩터분석 US" 1800 factor_analysis.py
run_stage "팩터분석 KR" 1800 factor_analysis.py --kr
run_stage "주도주 스크리너" 1800 leader_screener.py

# 구형 스크리너(monthly_top50 / theme_daily / sector_screen / theme_screen)는 2026-09-03 제외.
# 6월에 만든 모멘텀·테마 팩터 기반인데, 이후 IC 검증에서 예측력이 유의하지 않게 나와
# leader_screener(주도주/펀더가속/순위상승)로 대체됐다. 매주 안 보는 파일만 만들며
# 실행 시간을 잡아먹어 중단. 필요하면 수동 실행:
#   ./.venv/bin/python append_marcap_gap.py && ./.venv/bin/python test.py
#   ./.venv/bin/python screen.py theme-daily
run_stage "국장 섹터 대시보드" 1800 sector_dashboard.py


# --- DB 스냅샷 백업 (Tibero 재설치 사고 대비 — 2026-07 3주 유실 재발 방지) ---
echo "--- DB 백업 ---" >> "$LOG"
./.venv/bin/python db_backup.py >> "$LOG" 2>&1 && echo "백업 OK: $(du -sh /data/frame/db_backup | cut -f1)" >> "$LOG"


# --- 미국 주도주 리스트를 GitHub에 주간 보관 (주도주 엑셀만) ---
echo "--- 주도주 리스트 GitHub push ---" >> "$LOG"
bash /data/frame/push_watchlist.sh >> "$LOG" 2>&1

echo "=== $(date) 완료 ===" >> "$LOG"
