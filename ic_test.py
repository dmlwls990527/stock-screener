#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ic_test.py — 검증 가능한 10개 팩터의 IC(예측력) 전수검증 + 강건성(전·후반 분리)

IC = 매 분기, 팩터값과 '다음 분기 수익률'의 순위상관(Spearman). 전 분기 평균.
  - IC>0.03 & |t|>2 면 쓸만한 신호, 0 근처면 노이즈.
  - 전반/후반 부호가 같아야 안정적(과적합/우연 아님).
point-in-time: 그 시점까지의 데이터만 사용. SEC 매출은 90일 lag(공시 지연) 적용.
backtest.py 엔진 재사용. 수익률은 split 보정 close.
"""
import sys
sys.path.insert(0, "/data/frame")
import backtest as bt
import pandas as pd
import numpy as np

TRAIL = 8
LAG_DAYS = 90

conn = bt.get_conn()
qdates = bt.quarter_ends(conn)
uni = bt.load_universe(conn, qdates, 100)
codes = sorted(set(uni["code"]))
close_pv, mc_pv = bt.load_prices(conn, qdates, codes)
dates = [d for d in qdates if d in close_pv.index]

# 분기별 거래대금 합계 (daily_price_us)
def load_amount(qdates, codes):
    inc = ", ".join("'%s'" % c for c in codes)
    frames = []
    for d in qdates:
        y = int(d[:4]); m = int(d[5:7]); qn = (m - 1) // 3 + 1
        qs = "%04d-%02d-01" % (y, (qn - 1) * 3 + 1)
        rows = bt.fetchall(conn, "SELECT code, SUM(amount) FROM daily_price_us "
            "WHERE date_ >= TO_DATE('%s','YYYY-MM-DD') AND date_ <= TO_DATE('%s','YYYY-MM-DD') "
            "AND code IN (%s) GROUP BY code" % (qs, d, inc))
        for c, a in rows:
            frames.append({"d": d, "code": c, "amt": float(a) if a is not None else None})
    return pd.DataFrame(frames).pivot_table(index="d", columns="code", values="amt").sort_index()

amt_pv = load_amount(qdates, codes)

# SEC 분기 매출/영업이익
secrows = bt.fetchall(conn, "SELECT code, TO_CHAR(end_date,'YYYY-MM-DD'), revenue, op_income "
                            "FROM quarterly_financials_us WHERE revenue IS NOT NULL")
sec = pd.DataFrame(secrows, columns=["code", "end", "rev", "op"])
sec["rev"] = pd.to_numeric(sec["rev"], errors="coerce")
sec["op"]  = pd.to_numeric(sec["op"],  errors="coerce")
sec = sec.dropna(subset=["rev"]).sort_values(["code", "end"])
sec_by_code = {c: g.reset_index(drop=True) for c, g in sec.groupby("code")}

mc_qoq  = mc_pv.pct_change()
amt_qoq = amt_pv.pct_change()

def mom(qoq_pv, i, ucodes):
    cs = [c for c in ucodes if c in qoq_pv.columns]
    win = qoq_pv.loc[dates[i - TRAIL + 1:i + 1], cs]
    return win.mean(), (win.tail(4).mean() - win.mean()), (win > 0).sum() / win.notna().sum()

def sec_at(T_date, ucodes):
    cut = (pd.Timestamp(T_date) - pd.Timedelta(days=LAG_DAYS)).strftime("%Y-%m-%d")
    out = {}
    for c in ucodes:
        s0 = sec_by_code.get(c)
        if s0 is None:
            continue
        s = s0[s0["end"] <= cut]
        if len(s) < 12:
            continue
        s = s.tail(12); rev = s["rev"].values; op = s["op"].values
        yoy = (rev[4:] - rev[:-4]) / rev[:-4]
        ttm_now, ttm_prev = rev[-4:].sum(), rev[-8:-4].sum()
        out[c] = {
            "매출일관성": float((yoy > 0).mean()),
            "매출가속도": float(yoy[-4:].mean() - yoy.mean()),
            "매출성장TTM": float((ttm_now - ttm_prev) / ttm_prev) if ttm_prev else None,
            "영업이익률": float(op[-4:].sum() / rev[-4:].sum()) if rev[-4:].sum() else None,
        }
    return out

panel = []
for i in range(TRAIL, len(dates) - 1):
    T, Tn = dates[i], dates[i + 1]
    ucodes = [c for c in uni[uni["d"] == T]["code"].tolist() if c in mc_pv.columns]
    mg, ma, mco = mom(mc_qoq, i, ucodes)
    ag, aa, aco = mom(amt_qoq, i, ucodes)
    sf = sec_at(T, ucodes)
    fwd = close_pv.loc[Tn, ucodes] / close_pv.loc[T, ucodes] - 1
    for c in ucodes:
        s = sf.get(c, {})
        panel.append({"q": T, "code": c, "fwd": fwd.get(c),
            "시총성장률": mg.get(c), "시총가속도": ma.get(c), "시총일관성": mco.get(c),
            "거래대금증가율": ag.get(c), "거래대금가속도": aa.get(c), "거래대금일관성": aco.get(c),
            "매출일관성": s.get("매출일관성"), "매출가속도": s.get("매출가속도"),
            "매출성장TTM": s.get("매출성장TTM"), "영업이익률": s.get("영업이익률")})

P = pd.DataFrame(panel).dropna(subset=["fwd"])
facs = ["시총성장률", "시총가속도", "시총일관성", "거래대금증가율", "거래대금가속도",
        "거래대금일관성", "매출일관성", "매출가속도", "매출성장TTM", "영업이익률"]
print("표본:", len(P), "종목-분기 /", P["q"].nunique(), "분기")

def ic(d, f):
    s = d.dropna(subset=[f]).groupby("q").apply(
        lambda g: g[f].rank().corr(g["fwd"].rank()) if g[f].notna().sum() >= 10 else np.nan)
    return s.dropna()

allq = sorted(P["q"].unique())
h1q, h2q = allq[:len(allq) // 2], allq[len(allq) // 2:]
print("\n%-14s %8s %6s %7s | %7s %7s" % ("팩터", "IC평균", "t값", "양수%", "전반IC", "후반IC"))
print("-" * 60)
rows = []
for f in facs:
    a = ic(P, f)
    if len(a) < 4:
        continue
    t = a.mean() / a.std() * (len(a) ** 0.5)
    h1 = ic(P[P["q"].isin(h1q)], f).mean()
    h2 = ic(P[P["q"].isin(h2q)], f).mean()
    rows.append((f, a.mean(), t, (a > 0).mean(), h1, h2))
for r in sorted(rows, key=lambda x: -abs(x[1])):
    print("%-14s %+8.3f %+6.2f %6.0f%% | %+7.3f %+7.3f" % (
        r[0], r[1], r[2], r[3] * 100, r[4], r[5]))
print("\n판정 기준: |t|>2 & 전·후반 부호 일치 → 살림 / 그 외 → 노이즈(버림)")
print("검증불가(과거 데이터 없음): PEG, ROE, 부채비율")
