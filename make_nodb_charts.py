#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_nodb_charts.py — DB 없이 만들 수 있는 블로그 차트 (한국어 라벨).
  ic_bars.png       : #11 팩터별 평균 IC + 95% 신뢰구간
  ic_tstat.png      : #11 팩터별 t값 + |t|=2 유의선
  weight_scheme.png : #1 16분기 선형 가중치(1.00→1.75)
IC 값은 발행된 글 #11(ic_test.py 33분기) 그대로.
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np

# ── 한글 폰트 (Noto Sans CJK) ────────────────────────────────────────
FPATH = "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"
fm.fontManager.addfont(FPATH)
_KOR = fm.FontProperties(fname=FPATH).get_name()
plt.rcParams["font.family"] = _KOR
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams.update({"figure.dpi": 150, "font.size": 12, "axes.grid": True,
                     "grid.alpha": 0.3, "axes.spines.top": False, "axes.spines.right": False})

OUT = "/data/frame/blog_charts"
os.makedirs(OUT, exist_ok=True)

# ── #11 IC (ic_test.py 33분기 결과 그대로) ───────────────────────────
IC = [
    ("매출 일관성",     0.039,  1.02),
    ("매출 성장률(TTM)", 0.033,  0.81),
    ("매출 가속도",     0.033,  1.13),
    ("시총 가속도",     0.011,  0.26),
    ("거래대금 일관성",  0.009,  0.36),
    ("시총 성장률",     0.009,  0.20),
    ("거래대금 가속도",  0.009,  0.34),
    ("거래대금 증가율", -0.005, -0.18),
    ("시총 일관성",     0.004,  0.12),
    ("영업이익률",     -0.003, -0.11),
]
IC = sorted(IC, key=lambda r: r[1])
names = [r[0] for r in IC]
mean_ic = np.array([r[1] for r in IC])
tval = np.array([r[2] for r in IC])
ci = np.array([1.96 * abs(m / t) if t != 0 else 0 for m, t in zip(mean_ic, tval)])
y = np.arange(len(names))
colors = ["#2E75B6" if v >= 0 else "#C00000" for v in mean_ic]

fig, ax = plt.subplots(figsize=(9, 5.2))
ax.barh(y, mean_ic, color=colors, height=0.6)
ax.errorbar(mean_ic, y, xerr=ci, fmt="none", ecolor="#555", elinewidth=1, capsize=3)
ax.axvline(0, color="#333", lw=0.9)
ax.set_yticks(y); ax.set_yticklabels(names)
for i, (m, t) in enumerate(zip(mean_ic, tval)):
    ax.text(m + (0.0013 if m >= 0 else -0.0013), i, f"t={t:+.2f}",
            va="center", ha="left" if m >= 0 else "right", fontsize=9, color="#333")
ax.set_xlim(-0.06, 0.078)
ax.set_title("팩터별 평균 IC — 33분기, 95% 신뢰구간\n모든 막대의 신뢰구간이 0을 포함 → 통계적으로 유의한 팩터 없음", fontsize=12)
ax.set_xlabel("평균 정보계수 (IC, 스피어만 순위상관)")
fig.tight_layout(); fig.savefig(f"{OUT}/ic_bars.png"); plt.close(fig)

fig, ax = plt.subplots(figsize=(9, 5.2))
ax.barh(y, tval, color=colors, height=0.6)
ax.axvline(0, color="#333", lw=0.9)
ax.axvline(2, color="#C00000", lw=1.3, ls="--")
ax.axvline(-2, color="#C00000", lw=1.3, ls="--")
ax.text(2, len(names) - 0.25, "|t|=2 (유의 기준)", color="#C00000", fontsize=9.5, ha="center")
ax.set_yticks(y); ax.set_yticklabels(names)
ax.set_xlim(-3, 3)
ax.set_title("팩터별 t값 — 어느 것도 |t|=2를 넘지 못함 (최대 1.13, 매출 가속도)", fontsize=12.5)
ax.set_xlabel("t값")
fig.tight_layout(); fig.savefig(f"{OUT}/ic_tstat.png"); plt.close(fig)

# ── #1 16분기 선형 가중치 ───────────────────────────────────────────
W_START, W_STEP, N = 1.00, 0.05, 16
w = [W_START + k * W_STEP for k in range(N)]
xlabels = [f"{N-1-k}분기 전" if k < N - 1 else "최근" for k in range(N)]
fig, ax = plt.subplots(figsize=(9.2, 4.8))
bars = ax.bar(range(N), w, color=plt.cm.Blues(np.linspace(0.4, 0.95, N)))
ax.set_xticks(range(N)); ax.set_xticklabels(xlabels, rotation=45, ha="right", fontsize=9)
ax.set_ylim(0.9, 1.85)
ax.set_title("분기별 선형 가중치 — 최근 분기일수록 크게 (1.00 → 1.75)", fontsize=13)
ax.set_ylabel("가중치 (배)")
ax.bar_label(bars, labels=[f"{v:.2f}" for v in w], fontsize=8, padding=2)
fig.tight_layout(); fig.savefig(f"{OUT}/weight_scheme.png"); plt.close(fig)

print("폰트:", _KOR)
print("saved:", sorted(os.listdir(OUT)))
