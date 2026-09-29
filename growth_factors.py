#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
growth_factors.py — 꾸준한 성장 / 순위 상승 지표 (2026-09-29, 사용자 요청). leader 후보 선정과 replay 가 같이 쓴다.

as-of T 기준 (전부 T 이전 데이터만):
  amt_y0..y3   : 연 평균 일 거래대금 — (T−1년, T], (T−2년, T−1년], … 구간 평균 (구간 유효일 150일 미만이면 NaN)
  amt_steady   : amt_y0 > amt_y1 > amt_y2 > amt_y3 (3년 연속 증가)
  amt_cagr3    : (amt_y0 / amt_y3)^(1/3) − 1
  mc_0..3      : T, T−1년, T−2년, T−3년 시점 시가총액 (그 날짜 이전 마지막 값)
  mc_steady    : mc_0 > mc_1 > mc_2 > mc_3
  mc_cagr3     : (mc_0 / mc_3)^(1/3) − 1
  rank0..2     : T, T−1년, T−2년 시점 시총 순위 (daily_marcap_us RANK, DB 전 종목 기준)
  rank_ratio2  : rank2 / rank0  (2년 전 순위 ÷ 지금 순위 — 규모와 무관한 상승 속도)
  riser        : 21 ≤ rank0 ≤ 100 AND rank0 < rank1 < rank2 AND rank_ratio2 ≥ 1.5  (Top 20 진입 전 후보)
한계: 과거 시가총액 = 그날 종가 × 지금 주식 수 (run_etl 근사). 자사주 매입이 큰 회사는 과거 시총이 작게,
      증자가 큰 회사는 크게 잡힌다. 순위도 이 시총으로 매긴 것.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/data/frame")

PANEL_FILE = "/data/frame/data/growth_panels_{start}.pkl"
RISER_MIN, RISER_MAX, RISER_RATIO = 21, 100, 1.5
MIN_DAYS = 150
_P = None


def load_panels(start="2012-01-01", refresh=False, persist=True):
    """AMOUNT(daily_price_us), MARCAP·RANK(daily_marcap_us) 와이드 패널. 한 번 받아 pickle 로 둔다."""
    global _P
    path = PANEL_FILE.format(start=start)
    if _P is not None and not refresh:
        return _P
    if os.path.exists(path) and not refresh:
        _P = pd.read_pickle(path)
        return _P
    import factor_analysis as fa
    c = fa.get_conn()
    cur = c.cursor()
    cur.execute(f"SELECT TO_CHAR(date_, 'YYYY-MM-DD'), code, amount FROM daily_price_us WHERE date_ >= DATE '{start}'")
    a = pd.DataFrame(cur.fetchall(), columns=["d", "code", "v"])
    cur.execute(f"""SELECT TO_CHAR(date_, 'YYYY-MM-DD'), code, marcap, "RANK" FROM daily_marcap_us
                    WHERE date_ >= DATE '{start}'""")
    m = pd.DataFrame(cur.fetchall(), columns=["d", "code", "mc", "rk"])
    c.close()
    for df, cols in ((a, ["v"]), (m, ["mc", "rk"])):
        df[cols] = df[cols].apply(pd.to_numeric, errors="coerce")
    _P = {"AMT": a.pivot(index="d", columns="code", values="v").sort_index(),
          "MC": m.pivot(index="d", columns="code", values="mc").sort_index(),
          "RK": m.pivot(index="d", columns="code", values="rk").sort_index()}
    if persist:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pd.to_pickle(_P, path)
    return _P


def _ago(T, years):
    return (pd.Timestamp(T) - pd.DateOffset(years=years)).strftime("%Y-%m-%d")


def _at(panel, date):
    """date 이전(포함) 마지막 행."""
    i = panel.index.searchsorted(date, side="right") - 1
    return panel.iloc[i] if i >= 0 else pd.Series(np.nan, index=panel.columns)


def features(T, P=None):
    P = P or load_panels()
    amt, mc, rk = P["AMT"], P["MC"], P["RK"]
    ys = []
    for k in range(4):
        lo, hi = _ago(T, k + 1), _ago(T, k)
        sl = amt[(amt.index > lo) & (amt.index <= hi)]
        m = sl.mean(skipna=True)
        m[sl.notna().sum() < MIN_DAYS] = np.nan
        ys.append(m)
    # 6개월 창 (거래대금 증가 신호용): (T−6개월, T], (T−12개월, T−6개월]
    h = []
    for k in range(2):
        lo = (pd.Timestamp(T) - pd.DateOffset(months=6 * (k + 1))).strftime("%Y-%m-%d")
        hi = (pd.Timestamp(T) - pd.DateOffset(months=6 * k)).strftime("%Y-%m-%d")
        sl = amt[(amt.index > lo) & (amt.index <= hi)]
        m = sl.mean(skipna=True)
        m[sl.notna().sum() < MIN_DAYS // 2] = np.nan
        h.append(m)
    mcs = [_at(mc, _ago(T, k)) for k in range(4)]
    rks = [_at(rk, _ago(T, k)) for k in range(3)]
    f = pd.DataFrame({f"amt_y{k}": ys[k] for k in range(4)})
    for k in range(4):
        f[f"mc_{k}"] = mcs[k]
    for k in range(3):
        f[f"rank{k}"] = rks[k]
    f["amt_6m0"], f["amt_6m1"] = h[0], h[1]
    f["amt_g6"] = f.amt_6m0 / f.amt_6m1 - 1          # 최근 6개월 ÷ 그 전 6개월 − 1
    f["amt_g12"] = f.amt_y0 / f.amt_y1 - 1           # 최근 1년 ÷ 그 전 1년 − 1
    f["amt_steady"] = (f.amt_y0 > f.amt_y1) & (f.amt_y1 > f.amt_y2) & (f.amt_y2 > f.amt_y3)
    f["amt_cagr3"] = (f.amt_y0 / f.amt_y3) ** (1 / 3) - 1
    f["mc_steady"] = (f.mc_0 > f.mc_1) & (f.mc_1 > f.mc_2) & (f.mc_2 > f.mc_3)
    f["mc_cagr3"] = (f.mc_0 / f.mc_3) ** (1 / 3) - 1
    f["rank_ratio2"] = f.rank2 / f.rank0
    f["riser"] = ((f.rank0 >= RISER_MIN) & (f.rank0 <= RISER_MAX) & (f.rank0 < f.rank1) & (f.rank1 < f.rank2)
                  & (f.rank_ratio2 >= RISER_RATIO))
    f.index.name = "CODE"
    return f
