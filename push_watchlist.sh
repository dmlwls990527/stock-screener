#!/bin/bash
# push_watchlist.sh — 미국 주도주 리스트 엑셀을 GitHub에 주간 보관·푸시
# 요청: 다른 산출물 말고 '주도주 리스트' 엑셀만. 날짜별로 쌓아서 이력을 남긴다.
set -u
cd /data/frame || exit 1

SRC=/data/frame/leader_watchlist_latest.xlsx
DIR=/data/frame/watchlist_us
LOG=/data/frame/logs/push_watchlist_$(date +%Y%m%d).log
mkdir -p "$DIR" "$(dirname "$LOG")"

[ -f "$SRC" ] || { echo "$(date) 원본 없음: $SRC" >> "$LOG"; exit 1; }

# 파일 기준일(엑셀 '설명' 시트의 기준일)을 파일명에 쓴다. 못 읽으면 오늘 날짜.
ASOF=$(/data/frame/.venv/bin/python - <<'PY' 2>/dev/null
import pandas as pd
try:
    d = pd.read_excel("/data/frame/leader_watchlist_latest.xlsx", sheet_name="설명")
    print(str(d.loc[d["항목"] == "기준일", "값"].iloc[0])[:10])
except Exception:
    print("")
PY
)
[ -n "$ASOF" ] || ASOF=$(date +%Y-%m-%d)

DEST="$DIR/leader_watchlist_${ASOF}.xlsx"
cp -f "$SRC" "$DEST"
cp -f "$SRC" "$DIR/leader_watchlist_latest.xlsx"

git add "$DIR" >> "$LOG" 2>&1
if git diff --cached --quiet; then
  echo "$(date) 변경 없음 — 커밋 생략" >> "$LOG"
  exit 0
fi

git -c user.name="stock-bot" -c user.email="euijin_jung@tibero.com" \
    commit -m "chore(watchlist): 미국 주도주 리스트 ${ASOF}" >> "$LOG" 2>&1

GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=no" \
  git push >> "$LOG" 2>&1 && echo "$(date) PUSH OK ${ASOF}" >> "$LOG" \
  || echo "$(date) PUSH 실패 (다음 주 재시도)" >> "$LOG"
