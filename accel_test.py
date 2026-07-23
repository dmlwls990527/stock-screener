#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
accel_test.py — 가속도(acceleration)가 '극단적으로 높으면 과열이라 나쁜가'를 데이터로 검증.

방법(point-in-time): 매 분기 T에서
  - 가속도 = 최근4Q 평균 QoQ − 전체 trailing 평균 QoQ (시총 기준)
  - 레벨 모멘텀(성장률) = trailing 평균 QoQ
  - 다음분기 수익률 = close[T+1]/close[T] − 1
를 구한 뒤, 가속도 5분위(1=낮음 ... 5=과열)별 다음분기 평균수익률을 본다.
5분위(과열)가 3~4분위보다 낮으면 → '∩자(역U)' = 사용자 가설(과열은 나쁨) 입증.

backtest.py(point-in-time 엔진) 재사용. 수익률은 split 보정 close.
"""
import sys
sys.path.insert(0, "/data/frame")
import backtest as bt
import pandas as pd
import numpy as np

TRAIL = 8
conn = bt.get_conn()
qdates = bt.quarter_ends(conn)
uni = bt.load_universe(conn, qdates, 100)
all_codes = sorted(set(uni["code"]))
close_pv, mc_pv = bt.load_prices(conn, qdates, all_codes)
qoq = mc_pv.pct_change()
dates = [d for d in qdates if d in close_pv.index]

rows = []
for i in range(TRAIL, len(dates) - 1):
    T, Tn = dates[i], dates[i + 1]
    codes = [c for c in uni[uni["d"] == T]["code"].tolist() if c in mc_pv.columns]
    win = qoq.loc[dates[i - TRAIL + 1:i + 1], codes]
    accel  = win.tail(4).mean() - win.mean()      # 가속도
    growth = win.mean()                            # 레벨 모멘텀
    fwd = close_pv.loc[Tn, codes] / close_pv.loc[T, codes] - 1
    for c in codes:
        a, g, f = accel.get(c), growth.get(c), fwd.get(c)
        if pd.notna(a) and pd.notna(f):
            rows.append((T, c, a, g, f))

df = pd.DataFrame(rows, columns=["q", "code", "accel", "growth", "fwd"])
print("표본:", len(df), "종목-분기 /", df["q"].nunique(), "분기  (", dates[TRAIL+1], "~", dates[-1], ")")

def quintile_table(col):
    d = df.dropna(subset=[col]).copy()
    d["bin"] = d.groupby("q")[col].transform(
        lambda x: pd.qcut(x.rank(method="first"), 5, labels=[1, 2, 3, 4, 5]))
    g = d.groupby("bin", observed=True)["fwd"].agg(["mean", "median", "count"])
    g["mean(%)"]   = (g["mean"] * 100).round(2)
    g["median(%)"] = (g["median"] * 100).round(2)
    return g[["mean(%)", "median(%)", "count"]]

print("\n[가속도 5분위별 다음분기 수익률]  1=낮음 ... 5=과열(최고가속)")
print(quintile_table("accel").to_string())
print("\n[시총성장률(레벨) 5분위별 다음분기 수익률]  (대조군)")
print(quintile_table("growth").to_string())

ica = df.groupby("q").apply(lambda gr: gr["accel"].rank().corr(gr["fwd"].rank()))
icg = df.groupby("q").apply(lambda gr: gr["growth"].rank().corr(gr["fwd"].rank()))
print("\n[IC] (양수=예측력, 음수=반전 / |t|>2면 유의)")
print("  가속도   IC mean %+.3f  t=%+.2f" % (ica.mean(), ica.mean()/ica.std()*(len(ica)**0.5)))
print("  성장률   IC mean %+.3f  t=%+.2f" % (icg.mean(), icg.mean()/icg.std()*(len(icg)**0.5)))

# 가속도^2(극단 정도)와 수익률 — 음수면 '극단일수록 나쁨'
df["accel_z"] = df.groupby("q")["accel"].transform(lambda x: (x - x.mean()) / x.std())
df["accel_sq"] = df["accel_z"] ** 2
ic_sq = df.groupby("q").apply(lambda gr: gr["accel_sq"].rank().corr(gr["fwd"].rank()))
print("  가속도극단도(z^2) IC mean %+.3f  t=%+.2f  (음수=극단일수록 다음분기 나쁨)" % (
    ic_sq.mean(), ic_sq.mean()/ic_sq.std()*(len(ic_sq)**0.5)))
print("\n완료")
