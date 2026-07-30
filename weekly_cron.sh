#!/bin/bash
# weekly_cron.sh — 매주 월요일 08:00 KST 크론 실행: 미장+국장 증분 적재 + 스크리닝 + 구형 스크리너
source ~/.bashrc
cd /data/frame
LOG="logs/cron_weekly_$(date +%Y%m%d).log"

echo "=== $(date) 시작 ===" >> "$LOG"
./.venv/bin/python run_etl.py >> "$LOG" 2>&1
./.venv/bin/python factor_analysis.py >> "$LOG" 2>&1
./.venv/bin/python factor_analysis.py --kr >> "$LOG" 2>&1
./.venv/bin/python leader_screener.py >> "$LOG" 2>&1

echo "--- 구형 스크리너 (momentum/paradigm/sector/theme/monthly) ---" >> "$LOG"
./.venv/bin/python append_marcap_gap.py >> "$LOG" 2>&1
./.venv/bin/python test.py >> "$LOG" 2>&1
./.venv/bin/python screen.py theme-daily >> "$LOG" 2>&1
./.venv/bin/python sector_dashboard.py >> "$LOG" 2>&1


# --- DB 스냅샷 백업 (Tibero 재설치 사고 대비 — 2026-07 3주 유실 재발 방지) ---
echo "--- DB 백업 ---" >> "$LOG"
./.venv/bin/python db_backup.py >> "$LOG" 2>&1 && echo "백업 OK: $(du -sh /data/frame/db_backup | cut -f1)" >> "$LOG"

echo "=== $(date) 완료 ===" >> "$LOG"
