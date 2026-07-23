#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
전체 파이프라인 실행 스크립트
한 번 실행으로 모든 스크리닝 결과를 생성합니다.

실행: python3 test.py

생성 파일:
  monthly_top50_report.xlsx  — 국장 월별 Top50 리포트
  momentum_screen_latest.xlsx — 국장 단기 모멘텀
  kr_paradigm_latest.xlsx    — 국장 패러다임 (장기)
  sector_screen_latest.xlsx  — 섹터 로테이션
  theme_screen_latest.xlsx   — 테마 월간
  theme_daily_latest.xlsx    — 테마 단기 (일별)
  us_paradigm_latest.xlsx    — 미국 S&P500 패러다임
  charts/monthly/*.png       — 월별 시총 막대 차트
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR / "marcap"))
sys.path.insert(0, str(BASE_DIR))

from marcap import marcap_data

# ── 설정 ─────────────────────────────────────────────────────────────────────
START_DATE = "2024-01-01"
END_DATE   = min(date.today(), date(2026, 12, 31)).strftime("%Y-%m-%d")
TOP_N      = 50

# ─────────────────────────────────────────────────────────────────────────────
# 1. 국장 월별 Top50 리포트
# ─────────────────────────────────────────────────────────────────────────────
print(f"{'='*60}")
print(f"[1/7] 국장 월별 Top50 리포트 ({START_DATE} ~ {END_DATE})")
print(f"{'='*60}")

df = marcap_data(START_DATE, END_DATE).reset_index()
print("컬럼:", df.columns.tolist())

df["Date"]      = pd.to_datetime(df["Date"])
df["YearMonth"] = df["Date"].dt.to_period("M")

monthly_last = df.groupby("YearMonth", sort=True)["Date"].max()
df = df[df["Date"].isin(monthly_last.values)].copy()

monthly_top: dict[str, pd.DataFrame] = {}
for ym in monthly_last.sort_index().index:
    last_date  = monthly_last.loc[ym]
    period_key = ym.strftime("%Y-%m")
    one_day    = df[df["Date"] == last_date]
    top        = one_day.sort_values(by="Marcap", ascending=False).head(TOP_N).copy()
    top["순위"] = range(1, len(top) + 1)
    monthly_top[period_key] = top

# 순위 히스토리 (종목코드 기준 merge — 동일 종목명 중복으로 인한 merge 폭발 방지)
rank_history = pd.DataFrame()
name_map: dict[str, str] = {}
for period_key in sorted(monthly_top.keys()):
    data = monthly_top[period_key].drop_duplicates(subset=["Code"])
    name_map.update(dict(zip(data["Code"], data["Name"])))
    temp = data[["Code", "순위"]].copy()
    temp.columns = ["종목코드", period_key]
    rank_history = temp if rank_history.empty else pd.merge(rank_history, temp, on="종목코드", how="outer")
if not rank_history.empty:
    rank_history.insert(1, "종목명", rank_history["종목코드"].map(name_map))

# 변화 분석
change_list = []
periods = sorted(monthly_top.keys())
for i in range(1, len(periods)):
    prev_p, curr_p = periods[i-1], periods[i]
    prev_set = set(monthly_top[prev_p]["Name"])
    curr_set = set(monthly_top[curr_p]["Name"])
    change_list.append({
        "기간":       f"{prev_p} -> {curr_p}",
        "신규진입":   ", ".join(sorted(curr_set - prev_set)),
        "탈락":       ", ".join(sorted(prev_set - curr_set)),
        "유지종목수": len(curr_set & prev_set),
    })
change_df = pd.DataFrame(change_list)

# 생존 종목
common = set(monthly_top[periods[0]]["Name"])
for p in periods[1:]:
    common &= set(monthly_top[p]["Name"])
common_df = pd.DataFrame({"계속TOP50": sorted(common)})

output_file = BASE_DIR / "monthly_top50_report.xlsx"
with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
    for period_key, data in sorted(monthly_top.items()):
        data.to_excel(writer, sheet_name=period_key, index=False)
    rank_history.to_excel(writer, sheet_name="Rank_History", index=False)
    change_df.to_excel(writer,    sheet_name="Changes",      index=False)
    common_df.to_excel(writer,    sheet_name="Survivors",    index=False)

print(f"완료: {output_file} (월 수: {len(monthly_top)})")


# ─────────────────────────────────────────────────────────────────────────────
# 2. 국장 단기 모멘텀 (screen_momentum.py)
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print("[2/7] 국장 단기 모멘텀 스크리닝")
print(f"{'='*60}")
try:
    from screen_momentum import run_momentum_screen
    run_momentum_screen(BASE_DIR)
except Exception as e:
    print(f"[오류] screen_momentum: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 3. 국장 패러다임 (screen_momentum_kr.py)
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print("[3/7] 국장 패러다임 스크리닝 (장기)")
print(f"{'='*60}")
try:
    from screen_momentum_kr import run_paradigm_kr
    run_paradigm_kr(BASE_DIR)
except Exception as e:
    print(f"[오류] screen_momentum_kr: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 4. 섹터 로테이션 (screen_sector.py)
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print("[4/7] 섹터 로테이션 스크리닝")
print(f"{'='*60}")
try:
    from screen import run_sector_screen
    run_sector_screen(BASE_DIR)
except Exception as e:
    print(f"[오류] screen_sector: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 5. 테마 월간 (screen_theme.py)
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print("[5/7] 테마 월간 스크리닝")
print(f"{'='*60}")
try:
    from screen import run_theme_screen
    run_theme_screen(BASE_DIR)
except Exception as e:
    print(f"[오류] screen_theme: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 6. 미국 S&P500 패러다임 (screen_momentum_us.py)
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print("[6/7] 미국 S&P500 패러다임 스크리닝")
print(f"{'='*60}")
try:
    from screen_momentum_us import run_momentum_us
    run_momentum_us(BASE_DIR)
except Exception as e:
    print(f"[오류] screen_momentum_us: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 7. 월별 시총 막대 차트 (plot_yearly_top50.py)
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print("[7/7] 월별 시총 막대 차트 생성")
print(f"{'='*60}")
try:
    from plot_yearly_top50 import run_monthly_barcharts
    run_monthly_barcharts()
except Exception as e:
    print(f"[오류] plot_yearly_top50: {e}")


# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print("전체 파이프라인 완료")
print("생성 파일:")
files = [
    "monthly_top50_report.xlsx",
    "momentum_screen_latest.xlsx",
    "kr_paradigm_latest.xlsx",
    "sector_screen_latest.xlsx",
    "theme_screen_latest.xlsx",
    "theme_daily_latest.xlsx",
    "us_paradigm_latest.xlsx",
    "charts/monthly/*.png",
]
for f in files:
    p = BASE_DIR / f
    exists = "✓" if ("*" in f or p.exists()) else "✗"
    print(f"  {exists} {f}")
print("윈도우 배치 파일로 결과를 바탕화면에 받아 확인하세요.")
print(f"{'='*60}")
