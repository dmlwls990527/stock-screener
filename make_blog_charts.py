#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_blog_charts.py — #9 백테스팅 글용 차트 (실제 DB 데이터, 한국어 라벨)
  equity_curve.png   : 자산곡선 (전략 PIT vs 벤치마크 vs 고정유니버스)
  annual_returns.png : 연도별 수익률 막대 (전략 vs 벤치마크)
backtest.py 엔진 재사용 (point-in-time 유니버스, 동일 산식).
"""
import sys, os
sys.path.insert(0, "/data/frame")
import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import backtest as bt

FPATH = "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"
fm.fontManager.addfont(FPATH)
plt.rcParams["font.family"] = fm.FontProperties(fname=FPATH).get_name()
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams.update({"figure.dpi": 150, "font.size": 12, "axes.grid": True,
                     "grid.alpha": 0.3, "axes.spines.top": False, "axes.spines.right": False})

OUT = "/data/frame/blog_charts"
os.makedirs(OUT, exist_ok=True)
C_STRAT, C_BENCH, C_FIX = "#1F4E79", "#7F7F7F", "#C00000"

TOP_N, TOP_K, TRAIL, COST = 100, 20, 8, 0.002
conn = bt.get_conn()
qdates = bt.quarter_ends(conn)
uni = bt.load_universe(conn, qdates, TOP_N)
fixed_codes = uni[uni["d"] == qdates[-1]]["code"].tolist()
all_codes = sorted(set(uni["code"]) | set(fixed_codes))
close_pv, mc_pv = bt.load_prices(conn, qdates, all_codes)
dates = [d for d in qdates if d in close_pv.index]
qoq_all = mc_pv.pct_change()


def series(fixed=None):
    rows, prev = [], set()
    for i in range(TRAIL, len(dates) - 1):
        T, Tn = dates[i], dates[i + 1]
        codes = fixed if fixed is not None else uni[uni["d"] == T]["code"].tolist()
        codes = [c for c in codes if c in mc_pv.columns]
        if len(codes) < TOP_K:
            continue
        win = qoq_all.loc[dates[i - TRAIL + 1:i + 1], codes]
        score, cons, cnt = bt.momentum_scores(win)
        good = [c for c in codes if cnt.get(c, 0) >= max(3, TRAIL // 2)]
        if len(good) < TOP_K:
            good = [c for c in codes if cnt.get(c, 0) >= 2]
        sc = score[good]
        rng = sc.max() - sc.min()
        snorm = (sc - sc.min()) / rng if rng > 0 else sc * 0
        combined = 0.7 * snorm + 0.3 * cons[good].fillna(0)
        selected = combined.sort_values(ascending=False).head(TOP_K).index.tolist()
        fwd = close_pv.loc[Tn, codes] / close_pv.loc[T, codes] - 1
        sel = fwd[selected].dropna()
        if len(sel) == 0:
            continue
        cur = set(selected)
        turn = len(cur - prev) / TOP_K if prev else 1.0
        prev = cur
        rows.append((Tn, sel.mean() - 2 * COST * turn, fwd.dropna().mean()))
    return pd.DataFrame(rows, columns=["d", "strat", "bench"]).set_index("d")


pit = series()
fix = series(fixed=fixed_codes)
df = pit.copy()
df["fixed"] = fix["strat"]
df = df.dropna(subset=["strat", "bench"])
x = pd.to_datetime(df.index)
eq = (1 + df[["strat", "bench", "fixed"]].fillna(0)).cumprod()


def cagr(s):
    s = s.dropna(); yrs = len(s) / 4.0
    return ((1 + s).prod()) ** (1 / yrs) - 1


# ── 1) 자산곡선 ───────────────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(9, 5))
ax.plot(x, eq["strat"], lw=2.2, color=C_STRAT,
        label=f"전략 · point-in-time 유니버스 (CAGR {cagr(df['strat'])*100:.1f}%)")
ax.plot(x, eq["bench"], lw=1.7, color=C_BENCH,
        label=f"벤치마크 · Top100 동일가중 (CAGR {cagr(df['bench'])*100:.1f}%)")
ax.plot(x, eq["fixed"], lw=1.7, ls="--", color=C_FIX,
        label=f"고정 유니버스 · 미래참조 편향 (CAGR {cagr(df['fixed'])*100:.1f}%)")
ax.set_yscale("log")
ax.set_title("백테스트 자산곡선 (2018~2026, 분기 리밸런싱)")
ax.set_ylabel("$1 투자 시 자산 (로그 스케일)")
ax.legend(frameon=False, fontsize=10, loc="upper left")
fig.tight_layout(); fig.savefig(f"{OUT}/equity_curve.png"); plt.close(fig)

# ── 2) 연도별 수익률 ──────────────────────────────────────────────────────────
ann = df.copy()
ann["year"] = pd.to_datetime(ann.index).year
yr = ann.groupby("year").agg(strat=("strat", lambda s: (1 + s).prod() - 1),
                             bench=("bench", lambda s: (1 + s).prod() - 1))
fig, ax = plt.subplots(figsize=(9, 4.8))
xs = np.arange(len(yr)); w = 0.38
ax.bar(xs - w/2, yr["strat"] * 100, w, color=C_STRAT, label="전략 (point-in-time)")
ax.bar(xs + w/2, yr["bench"] * 100, w, color=C_BENCH, label="벤치마크 (Top100 동일가중)")
ax.axhline(0, color="#333", lw=0.8)
ax.set_xticks(xs); ax.set_xticklabels(yr.index)
ax.set_title("연도별 수익률 — 전략 vs 벤치마크")
ax.set_ylabel("수익률 (%)")
ax.legend(frameon=False, fontsize=10)
fig.tight_layout(); fig.savefig(f"{OUT}/annual_returns.png"); plt.close(fig)

print("CAGR  전략=%.3f 벤치=%.3f 고정=%.3f  분기수=%d" % (
    cagr(df["strat"]), cagr(df["bench"]), cagr(df["fixed"]), len(df)))
print("연도:", list(yr.index))
print("saved:", sorted(os.listdir(OUT)))
