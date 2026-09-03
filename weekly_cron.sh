#!/bin/bash
# weekly_cron.sh — 매주 월요일 08:00 KST: 미장+국장 증분 적재 + 스크리닝 + 백업 + 주도주 push
source ~/.bashrc
cd /data/frame
LOG="logs/cron_weekly_$(date +%Y%m%d).log"

echo "=== $(date) 시작 ===" >> "$LOG"
./.venv/bin/python run_etl.py >> "$LOG" 2>&1
./.venv/bin/python factor_analysis.py >> "$LOG" 2>&1
./.venv/bin/python factor_analysis.py --kr >> "$LOG" 2>&1
./.venv/bin/python leader_screener.py >> "$LOG" 2>&1

# 구형 스크리너(monthly_top50 / theme_daily / sector_screen / theme_screen)는 2026-09-03 제외.
# 6월에 만든 모멘텀·테마 팩터 기반인데, 이후 IC 검증에서 예측력이 유의하지 않게 나와
# leader_screener(주도주/펀더가속/순위상승)로 대체됐다. 매주 안 보는 파일만 만들며
# 실행 시간을 잡아먹어 중단. 필요하면 수동 실행:
#   ./.venv/bin/python append_marcap_gap.py && ./.venv/bin/python test.py
#   ./.venv/bin/python screen.py theme-daily
echo "--- 국장 섹터 대시보드 ---" >> "$LOG"
./.venv/bin/python sector_dashboard.py >> "$LOG" 2>&1


# --- DB 스냅샷 백업 (Tibero 재설치 사고 대비 — 2026-07 3주 유실 재발 방지) ---
echo "--- DB 백업 ---" >> "$LOG"
./.venv/bin/python db_backup.py >> "$LOG" 2>&1 && echo "백업 OK: $(du -sh /data/frame/db_backup | cut -f1)" >> "$LOG"


# --- 미국 주도주 리스트를 GitHub에 주간 보관 (주도주 엑셀만) ---
echo "--- 주도주 리스트 GitHub push ---" >> "$LOG"
bash /data/frame/push_watchlist.sh >> "$LOG" 2>&1

echo "=== $(date) 완료 ===" >> "$LOG"
