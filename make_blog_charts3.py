#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_blog_charts3.py — 남은 챕터(#2·#3·#4·#6·#10)용 차트 (한국어, 실데이터)
  matrix_2x2.png        #2  일관성×가속도 2×2 개념 매트릭스
  example_series.png    #2  같은 평균성장률, 다른 일관성 예시 시계열
  weights_pie.png       #3  초기 가중치 파이 (시총30/거래대금30/가속도25/일관성15)
  norm_compare.png      #3  단위 다른 팩터(성장률% vs 일관성0~1) 분포 비교
  per_hist.png          #4  현재 유니버스 PER 분포 히스토그램
  factor_structure.png  #6  팩터 3계층 구조 다이어그램
  minmax_distortion.png #10 min-max가 이상치(PBR)에 뭉개지는 것 vs 순위 정규화
실데이터: factor_result_us_latest.xlsx (현재 스크리닝 결과)
"""
import os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

FPATH = "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"
fm.fontManager.addfont(FPATH)
plt.rcParams["font.family"] = fm.FontProperties(fname=FPATH).get_name()
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams.update({"figure.dpi": 150, "font.size": 12, "axes.grid": True,
                     "grid.alpha": 0.3, "axes.spines.top": False, "axes.spines.right": False})
OUT = "/data/frame/blog_charts"
os.makedirs(OUT, exist_ok=True)

df = pd.read_excel("/data/frame/factor_result_us_latest.xlsx", sheet_name=0, header=2)
df.columns = [str(c).replace("\n", " ") for c in df.columns]
# 컬럼: 순위/종목코드/티어/종합점수/... PER/PBR/...
col_per = [c for c in df.columns if c.strip() == "PER"][0]
col_pbr = [c for c in df.columns if c.strip() == "PBR"][0]
col_grw = [c for c in df.columns if "시총 성장률" in c][0]
col_con = [c for c in df.columns if "시총 일관성" in c][0]
col_code = [c for c in df.columns if "종목코드" in c][0]
per = pd.to_numeric(df[col_per], errors="coerce").dropna()
pbr = pd.to_numeric(df[col_pbr], errors="coerce").dropna()
grw = pd.to_numeric(df[col_grw], errors="coerce").dropna()
con = pd.to_numeric(df[col_con], errors="coerce").dropna()
codes = df[col_code]

# ── #2 매트릭스 ───────────────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(8, 6.4))
ax.set_xlim(0, 2); ax.set_ylim(0, 2); ax.axis("off")
cells = [
    (0, 1, "#FFEB9C", "일관성만 높음", "꾸준하지만 둔화 중\n(성숙·안정 구간)"),
    (1, 1, "#C6EFCE", "둘 다 높음 ★", "꾸준한데 더 빨라짐\n→ 진짜 모멘텀"),
    (0, 0, "#FFC7CE", "둘 다 낮음", "들쭉날쭉 + 둔화\n(관심 제외)"),
    (1, 0, "#DEEAF1", "가속만 높음", "급가속이지만 검증 부족\n(반짝 급등 위험)"),
]
for x, y, c, title, desc in cells:
    ax.add_patch(plt.Rectangle((x+0.02, y+0.02), 0.96, 0.96, facecolor=c, edgecolor="#666"))
    ax.text(x+0.5, y+0.66, title, ha="center", va="center", fontsize=15, fontweight="bold")
    ax.text(x+0.5, y+0.34, desc, ha="center", va="center", fontsize=11.5, color="#333")
ax.annotate("", xy=(2.0, -0.06), xytext=(0, -0.06), annotation_clip=False,
            arrowprops=dict(arrowstyle="->", color="#333"))
ax.text(1, -0.16, "가속도 (최근 4분기 − 전체 평균)  →", ha="center", fontsize=12.5, clip_on=False)
ax.annotate("", xy=(-0.06, 2.0), xytext=(-0.06, 0), annotation_clip=False,
            arrowprops=dict(arrowstyle="->", color="#333"))
ax.text(-0.14, 1, "일관성 (QoQ 플러스 분기 비율)  →", va="center", rotation=90, fontsize=12.5, clip_on=False)
ax.set_title("일관성 × 가속도 — 어느 사분면의 종목을 원하는가", fontsize=14, pad=18)
fig.tight_layout(); fig.savefig(f"{OUT}/matrix_2x2.png", bbox_inches="tight"); plt.close(fig)

# ── #2 예시 시계열: 같은 평균 성장률, 다른 일관성 ─────────────────────────────
q = np.arange(1, 17)
np.random.seed(7)
steady = np.full(16, 5.0) + np.random.uniform(-1.2, 1.2, 16)          # 평균 ~5%, 항상 +
lumpy = np.array([22, -9, 14, -11, 19, -8, 16, -12, 21, -7, 15, -10, 18, -6, 17, 1.0])
lumpy = lumpy * (steady.mean() / lumpy.mean())                          # 평균 동일화
fig, ax = plt.subplots(figsize=(9, 4.8))
ax.bar(q - 0.2, steady, 0.38, color="#2E75B6", label=f"A: 꾸준형 (평균 {steady.mean():.1f}%, 일관성 {(steady>0).mean():.2f})")
ax.bar(q + 0.2, lumpy, 0.38, color="#C00000", alpha=0.75, label=f"B: 들쭉날쭉형 (평균 {lumpy.mean():.1f}%, 일관성 {(lumpy>0).mean():.2f})")
ax.axhline(0, color="#333", lw=0.8)
ax.set_xlabel("분기"); ax.set_ylabel("QoQ 성장률 (%)")
ax.set_title("평균 성장률이 같아도 '일관성'은 전혀 다르다 (예시 데이터)")
ax.legend(frameon=False, fontsize=10)
fig.tight_layout(); fig.savefig(f"{OUT}/example_series.png"); plt.close(fig)

# ── #3 가중치 파이 (초기 설계) ────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(7.2, 5.6))
w = [30, 30, 25, 15]
labels = ["시총 성장률 30%", "거래대금 성장률 30%", "가속도 25%", "일관성 15%"]
colors = ["#1F4E79", "#2E75B6", "#7FAEDC", "#C9DCF0"]
ax.pie(w, labels=labels, colors=colors, startangle=90, counterclock=False,
       wedgeprops=dict(edgecolor="white", linewidth=2), textprops=dict(fontsize=12.5))
ax.set_title("종합점수 가중치 — 초기 설계 (#3 시점, 팩터 4개)", fontsize=13.5)
fig.tight_layout(); fig.savefig(f"{OUT}/weights_pie.png"); plt.close(fig)

# ── #3 단위가 다른 팩터 분포 비교 ─────────────────────────────────────────────
fig, axes = plt.subplots(1, 2, figsize=(9.6, 4.2))
axes[0].hist(grw, bins=24, color="#2E75B6", edgecolor="white")
axes[0].set_title(f"시총 성장률 분포 (단위: %)\n범위 {grw.min():.0f}% ~ {grw.max():.0f}%")
axes[0].set_xlabel("가중평균 QoQ (%)")
axes[1].hist(con, bins=16, color="#C00000", alpha=0.8, edgecolor="white")
axes[1].set_title(f"시총 일관성 분포 (단위: 0~1 비율)\n범위 {con.min():.2f} ~ {con.max():.2f}")
axes[1].set_xlabel("플러스 분기 비율")
fig.suptitle("단위가 전혀 다른 두 팩터 — 그대로 더하면 성장률이 점수를 독식한다 (현재 Top100 실데이터)", fontsize=12.5)
fig.tight_layout(); fig.savefig(f"{OUT}/norm_compare.png"); plt.close(fig)

# ── #4 PER 분포 ───────────────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(9, 4.6))
clipped = per.clip(upper=120)
ax.hist(clipped, bins=30, color="#2E75B6", edgecolor="white")
ax.axvline(15, color="#2E9E48", ls="--", lw=1.4); ax.text(15, ax.get_ylim()[1]*0.95, " 15 (저평가 기준)", color="#2E9E48", fontsize=10)
ax.axvline(40, color="#C00000", ls="--", lw=1.4); ax.text(40, ax.get_ylim()[1]*0.95, " 40 (고평가 기준)", color="#C00000", fontsize=10)
ax.set_xlabel("PER (120 초과는 120으로 표시)"); ax.set_ylabel("종목 수")
ax.set_title(f"현재 시총 Top100 PER 분포 — 중앙값 {per.median():.0f}배, 절반 이상이 '고평가' 영역 (적자 제외 {len(per)}종목)")
fig.tight_layout(); fig.savefig(f"{OUT}/per_hist.png"); plt.close(fig)

# ── #6 팩터 구조 다이어그램 ───────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(9.6, 5.6))
ax.set_xlim(0, 10); ax.set_ylim(0, 10); ax.axis("off")
def box(x, y, w_, h, color, title, items, tfs=12.5, ifs=10.5):
    ax.add_patch(FancyBboxPatch((x, y), w_, h, boxstyle="round,pad=0.12",
                                facecolor=color, edgecolor="#555"))
    ax.text(x + w_/2, y + h - 0.55, title, ha="center", fontsize=tfs, fontweight="bold")
    ax.text(x + w_/2, y + (h-0.9)/2, items, ha="center", va="center", fontsize=ifs, color="#222")
box(0.2, 5.6, 3.0, 3.6, "#DEEAF1", "① 모멘텀", "시총 성장률·가속도·일관성\n거래대금 증가율·가속도·일관성\n(daily_marcap_us)")
box(3.5, 5.6, 3.0, 3.6, "#FFEB9C", "② 밸류에이션", "PER · PBR · PEG\n(daily_fundamental_us)")
box(6.8, 5.6, 3.0, 3.6, "#C6EFCE", "③ 품질 (이번 글)", "ROE · 매출성장률\n영업이익률 · 부채비율\n(daily_fundamental_us)")
box(2.6, 0.6, 4.8, 2.6, "#1F4E79", "", "")
ax.text(5, 2.4, "종합점수 (0~100)", ha="center", fontsize=14, fontweight="bold", color="white")
ax.text(5, 1.5, "팩터별 정규화 → 가중합", ha="center", fontsize=11, color="#DDEBF7")
for sx in [1.7, 5.0, 8.3]:
    ax.add_patch(FancyArrowPatch((sx, 5.45), (5, 3.5), arrowstyle="-|>", mutation_scale=16, color="#555"))
ax.set_title("팩터 3계층 구조 — 이번 글에서 ③ 품질 레이어가 추가됨", fontsize=13.5)
fig.tight_layout(); fig.savefig(f"{OUT}/factor_structure.png", bbox_inches="tight"); plt.close(fig)

# ── #10 min-max 왜곡 vs 순위 정규화 (PBR 실데이터) ────────────────────────────
pbr_v = pd.to_numeric(df[col_pbr], errors="coerce")
valid = pbr_v.notna() & (pbr_v > 0)
v = pbr_v[valid]; cds = codes[valid]
mm = (v.max() - v) / (v.max() - v.min())          # 낮을수록 좋음 → 역방향 min-max
rk = v.rank(ascending=False, pct=True)             # 낮을수록 1 근처
fig, axes = plt.subplots(1, 2, figsize=(10.2, 4.4))
axes[0].hist(mm, bins=30, color="#C00000", alpha=0.85, edgecolor="white")
axes[0].set_title(f"min-max 정규화 — PBR 최대 {v.max():.0f}배(STX) 하나가\n스케일을 독식해 나머지가 1 근처에 뭉개짐")
axes[0].set_xlabel("정규화 점수 (1=저평가)"); axes[0].set_ylabel("종목 수")
axes[1].hist(rk, bins=30, color="#2E75B6", edgecolor="white")
axes[1].set_title("순위(퍼센타일) 정규화 — 0~1에 고르게 분포\n→ 모든 종목의 차이가 점수에 반영됨")
axes[1].set_xlabel("정규화 점수 (1=저평가)")
fig.suptitle(f"같은 PBR 데이터({valid.sum()}종목), 정규화 방식만 다를 때", fontsize=13)
fig.tight_layout(); fig.savefig(f"{OUT}/minmax_distortion.png"); plt.close(fig)

print("saved:", sorted(os.listdir(OUT)))
