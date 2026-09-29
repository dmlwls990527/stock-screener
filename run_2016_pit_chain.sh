#!/bin/bash
# 2016~ 백테스트 체인 (2026-09-29): ① 3주·손절없음 PIT ∥ DB(편향 기준) → ② 손절 비교 PIT → ③ 주기·종목수 비교 PIT
source ~/.bashrc
cd /data/frame || exit 1
L=logs/chain_2016pit_20260929.log
R="./.venv/bin/python auto_buy.py replay --cadence weekly --start 2016-01-04"
echo "$(date +%T) 시작" >> $L
$R --universe sp500_pit --out /data/frame/paper_replay_2016_r3_pit.xlsx > logs/replay_r3_2016_pit.log 2>&1 & P1=$!
$R --universe db --out /data/frame/paper_replay_2016_r3_db.xlsx > logs/replay_r3_2016_db.log 2>&1 & P2=$!
wait $P1; echo "$(date +%T) 3주 PIT rc=$?" >> $L
wait $P2; echo "$(date +%T) 3주 DB rc=$?" >> $L
$R --compare --compare-set stops --universe sp500_pit --out /data/frame/paper_replay_stops_2016_pit.xlsx > logs/replay_stops_2016_pit.log 2>&1
echo "$(date +%T) 손절 비교 rc=$?" >> $L
$R --compare --compare-set grid --universe sp500_pit --out /data/frame/paper_replay_grid_2016_pit.xlsx > logs/replay_grid_2016_pit.log 2>&1
echo "$(date +%T) 주기 비교 rc=$?" >> $L
