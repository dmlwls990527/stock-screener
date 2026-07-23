#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""etl_flow.png — 데이터 파이프라인 아키텍처 다이어그램 (B 시리즈 공용, 한국어)"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

FPATH = "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"
fm.fontManager.addfont(FPATH)
plt.rcParams["font.family"] = fm.FontProperties(fname=FPATH).get_name()
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.dpi"] = 150
OUT = "/data/frame/blog_charts"
os.makedirs(OUT, exist_ok=True)

fig, ax = plt.subplots(figsize=(10.4, 5.8))
ax.set_xlim(0, 14); ax.set_ylim(0, 10); ax.axis("off")

def box(x, y, w, h, color, title, items, tfs=12.5, ifs=10, tcolor="#111", icolor="#333"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.12",
                                facecolor=color, edgecolor="#555"))
    ax.text(x + w/2, y + h - 0.55, title, ha="center", fontsize=tfs, fontweight="bold", color=tcolor)
    ax.text(x + w/2, y + (h - 0.8)/2 - 0.05, items, ha="center", va="center", fontsize=ifs, color=icolor)

def arrow(x1, y1, x2, y2, label=None):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=15, color="#555"))
    if label:
        ax.text((x1+x2)/2, (y1+y2)/2 + 0.28, label, ha="center", fontsize=9.5, color="#555")

# 소스
box(0.2, 6.4, 3.2, 3.0, "#DEEAF1", "yfinance", "미국 주식 (S&P500+NASDAQ100)\nOHLCV·시총·펀더멘털")
box(0.2, 2.6, 3.2, 3.0, "#DEEAF1", "PyKrx / SEC EDGAR", "국내 주식 OHLCV·시총\n美 분기 재무(XBRL)")
# ETL
box(4.6, 3.6, 4.2, 4.6, "#FFEB9C", "Python ETL (run_etl.py)",
    "① Extract — API 호출·수집\n\n② Transform — 컬럼 정규화,\n등락률·시총·rank 계산,\n휴장일(0값)·결측 필터\n\n③ Load — 날짜별 INSERT,\n적재된 날짜는 스킵(증분)")
# DB
box(9.8, 3.0, 4.0, 5.6, "#C6EFCE", "Tibero DB",
    "ticker_master(_us)\ndaily_price(_us)\ndaily_marcap(_us)\ndaily_fundamental(_us)\nquarterly_financials_us")
arrow(3.45, 7.6, 4.55, 6.6)
arrow(3.45, 4.2, 4.55, 5.2)
arrow(8.85, 5.9, 9.75, 5.9, "JDBC (jaydebeapi)")
# 소비자
box(4.6, 0.3, 9.2, 2.0, "#EDEDED", "분석 단계", "factor_analysis.py (스크리닝)  ·  backtest.py (백테스트)  ·  ic_test.py (팩터 검증)")
arrow(11.8, 2.9, 11.8, 2.4)
ax.set_title("주식 데이터 파이프라인 전체 구조 — 수집(API) → ETL(Python) → 적재(Tibero) → 분석", fontsize=13)
fig.tight_layout()
fig.savefig(f"{OUT}/etl_flow.png", bbox_inches="tight")
print("saved etl_flow.png", os.path.getsize(f"{OUT}/etl_flow.png")//1024, "KB")
