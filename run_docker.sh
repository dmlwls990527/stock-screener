#!/bin/bash
# 사용법: ./run_docker.sh <스크립트> [인자...]   예) ./run_docker.sh leader_screener.py --backtest
# 코드·jar 를 호스트와 "같은 경로"로 마운트 → 스크립트의 하드코딩 경로 그대로 동작 (코드수정 0줄)
docker run --rm --network=host \
  -v /data/frame:/data/frame \
  -v /data/tibero7/tibero7/client/lib/jar:/data/tibero7/tibero7/client/lib/jar:ro \
  -w /data/frame \
  --env-file /data/frame/.env \
  stock-pipeline python "$@"
