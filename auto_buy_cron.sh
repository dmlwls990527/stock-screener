#!/bin/bash
# auto_buy_cron.sh — 크론에서 auto_buy 서브커맨드를 순서대로 실행 (같은 상태 파일을 동시에 쓰지 않도록 한 프로세스씩)
#   예) auto_buy_cron.sh run exits     /  auto_buy_cron.sh tick
# 실계좌 전환은 이 스크립트로 되지 않는다: config mode=live + --live + AUTO_BUY_LIVE_OK=1 이 모두 필요하고
# 여기서는 --live 를 붙이지 않는다.
source ~/.bashrc
cd /data/frame || exit 1
LOG=logs/auto_buy_cron_$(date +%Y%m).log
for sub in "$@"; do
  echo "--- $(date '+%F %T') $sub ---" >> "$LOG"
  ./.venv/bin/python auto_buy.py "$sub" >> "$LOG" 2>&1
  echo "  rc=$?  (0 완료 / 3 진입시각 아님·이미 실행 / 1 실패)" >> "$LOG"
done
