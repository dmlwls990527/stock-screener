#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
safety_ic_test.py — '안전/위험' 후보 팩터의 IC(예측력) 검증 + 강건성(전·후반)

설계(워크플로 합성안) 기반. 사전 등록·고정 shortlist:
  S1 실현변동성   = std(일별 log수익률, 252d) * sqrt(252)         (낮을수록 안전)
  S2 하방편차     = sqrt(mean(min(r,0)^2), 252d) * sqrt(252)      (낮을수록 안전)
  S3 매출불안정성 = std/mean of 최근 8분기 revenue (CV)           (낮을수록 안전)
  S4 영업흑자지속 = 최근 8분기 중 op_income>0 비율                (높을수록 안전)
  S5 최대낙폭(MDD)= |min(close/cummax - 1)| over 252d             (낮을수록 안전, 게이트 전용)
제외: 베타(벤치마크 순환성), 유동성변동성(Top100 거의 상수), 하락장수익률(자유도).

방법(point-in-time):
  - 매 분기말 T에서 daily_marcap_us 의 date_<=T 직전 252거래일 일별 close 로 위험팩터 계산.
  - 펀더멘털 안정성(S3·S4)은 quarterly_financials_us, 공시지연 LAG_DAYS 적용한 분기만.
  - IC = 매 분기 팩터 횡단면 순위 vs 다음분기 수익률 순위의 Spearman(.rank().corr()).
  - 평균 IC, t값(mean/std*sqrt(n)), 양수%, 전·후반 분리.
  유의기준(사전등록): 멀티테스팅 감안 |t| >= 2.6 + 전·후반 부호 일치 → '통과'.
backtest.py 엔진 재사용. 수익률은 split 보정 close.
"""
import sys
sys.path.insert(0, "/data/frame")
import backtest as bt
import pandas as pd
import numpy as np

WIN       = 252      # 위험팩터 trailing 거래일 (단일 고정 — window-shopping 차단)
MINVALID  = 200      # 윈도우 내 유효 일별수익률 최소 (미충족 종목 제외)
TRAIL_Q   = 8        # 펀더멘털 안정성 trailing 분기
LAG_DAYS  = 90       # 공시지연 (분기말+LAG 이후에만 정보로 사용; 보수적)
T_THRESH  = 2.6      # 사전등록 유의문턱 (Bonferroni 근사)

conn = bt.get_conn()
qdates = bt.quarter_ends(conn)
uni = bt.load_universe(conn, qdates, 100)
codes = sorted(set(uni["code"]))
close_pv, mc_pv = bt.load_prices(conn, qdates, codes)   # 분기말 피벗 (fwd 수익률용)
dates = [d for d in qdates if d in close_pv.index]


def load_daily_close(conn, codes):
    """유니버스 종목의 일별 분할보정 close 전체 → 피벗(date × code)."""
    inc = ", ".join("'%s'" % c for c in codes)
    rows = bt.fetchall(conn,
        "SELECT TO_CHAR(date_,'YYYY-MM-DD'), code, close "
        "FROM daily_marcap_us WHERE code IN (%s)" % inc)
    df = pd.DataFrame(rows, columns=["d", "code", "close"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.pivot_table(index="d", columns="code", values="close").sort_index()


daily = load_daily_close(conn, codes)
print("일별 close 적재:", daily.shape[0], "거래일 ×", daily.shape[1], "종목")

# SEC 분기 매출/영업이익
secrows = bt.fetchall(conn,
    "SELECT code, TO_CHAR(end_date,'YYYY-MM-DD'), revenue, op_income "
    "FROM quarterly_financials_us WHERE revenue IS NOT NULL")
sec = pd.DataFrame(secrows, columns=["code", "end", "rev", "op"])
sec["rev"] = pd.to_numeric(sec["rev"], errors="coerce")
sec["op"]  = pd.to_numeric(sec["op"],  errors="coerce")
sec = sec.dropna(subset=["rev"]).sort_values(["code", "end"])
sec_by_code = {c: g.reset_index(drop=True) for c, g in sec.groupby("code")}


def risk_at(T, ucodes):
    """T 직전 252거래일 일별수익률 기반 위험팩터 (point-in-time)."""
    win = daily.loc[:T].tail(WIN)              # date_ <= T 강제
    r = np.log(win / win.shift(1))
    out = {}
    for c in ucodes:
        if c not in win.columns:
            continue
        rc = r[c]
        if rc.notna().sum() < MINVALID:        # 윈도우 미충족 → 제외(짧은 window로 섞지 않음)
            continue
        vol = rc.std() * np.sqrt(252)
        dd  = np.sqrt((rc.clip(upper=0) ** 2).mean()) * np.sqrt(252)
        wc  = win[c].dropna()
        mdd = (wc / wc.cummax() - 1).min()     # 음수
        out[c] = {"변동성": vol, "하방편차": dd, "최대낙폭": abs(float(mdd))}
    return out


def fund_at(T, ucodes):
    """공시지연 적용한 최근 8분기 매출/이익 안정성."""
    cut = (pd.Timestamp(T) - pd.Timedelta(days=LAG_DAYS)).strftime("%Y-%m-%d")
    out = {}
    for c in ucodes:
        s0 = sec_by_code.get(c)
        if s0 is None:
            continue
        s = s0[s0["end"] <= cut].tail(TRAIL_Q)
        if len(s) < TRAIL_Q:
            continue
        rev = s["rev"].values
        op  = s["op"].values
        m = rev.mean()
        if m <= 0:
            continue
        out[c] = {"매출불안정성": float(rev.std() / m),
                  "영업흑자지속": float((op > 0).mean())}
    return out


panel = []
for i in range(len(dates) - 1):
    T, Tn = dates[i], dates[i + 1]
    ucodes = [c for c in uni[uni["d"] == T]["code"].tolist() if c in close_pv.columns]
    rk = risk_at(T, ucodes)
    fd = fund_at(T, ucodes)
    fwd = close_pv.loc[Tn, ucodes] / close_pv.loc[T, ucodes] - 1
    for c in ucodes:
        row = {"q": T, "code": c, "fwd": fwd.get(c)}
        row.update(rk.get(c, {}))
        row.update(fd.get(c, {}))
        panel.append(row)

P = pd.DataFrame(panel).dropna(subset=["fwd"])
facs = ["변동성", "하방편차", "최대낙폭", "매출불안정성", "영업흑자지속"]
print("표본:", len(P), "종목-분기 /", P["q"].nunique(), "분기")

# 직교성 사전점검: 변동성 vs 하방편차
if {"변동성", "하방편차"}.issubset(P.columns):
    cc = P[["변동성", "하방편차"]].corr().iloc[0, 1]
    print("변동성 ↔ 하방편차 상관: %+.2f  (높으면 둘 중 하나만 점수후보)" % cc)

# 안전도(표시용 합성): 변동성·하방편차 역순위 평균 — 이 지표 자체의 IC도 본다
P["안전도"] = (0.5 * (1 - P.groupby("q")["변동성"].rank(pct=True))
             + 0.5 * (1 - P.groupby("q")["하방편차"].rank(pct=True)))


def ic(d, f):
    s = d.dropna(subset=[f]).groupby("q").apply(
        lambda g: g[f].rank().corr(g["fwd"].rank()) if g[f].notna().sum() >= 10 else np.nan)
    return s.dropna()


allq = sorted(P["q"].unique())
h1q, h2q = allq[:len(allq) // 2], allq[len(allq) // 2:]

print("\n* 위험팩터(변동성·하방편차·최대낙폭·매출불안정성)는 '높을수록 위험' → "
      "안전이 보상받으면 IC 음(-), 위험이 보상받으면 IC 양(+)")
print("* 영업흑자지속·안전도는 '높을수록 안전' → 안전이 보상받으면 IC 양(+)")
print("\n%-14s %8s %6s %7s | %7s %7s  %s" % (
    "팩터", "IC평균", "t값", "양수%", "전반IC", "후반IC", "판정"))
print("-" * 72)
rows = []
for f in facs + ["안전도"]:
    a = ic(P, f)
    if len(a) < 4:
        continue
    t = a.mean() / a.std() * (len(a) ** 0.5)
    h1 = ic(P[P["q"].isin(h1q)], f).mean()
    h2 = ic(P[P["q"].isin(h2q)], f).mean()
    sign_ok = (h1 > 0) == (h2 > 0)
    verdict = "통과" if (abs(t) >= T_THRESH and sign_ok) else (
              "부호불안정" if not sign_ok else "유의미달")
    rows.append((f, a.mean(), t, (a > 0).mean(), h1, h2, verdict))

for r in rows:
    print("%-14s %+8.3f %+6.2f %6.0f%% | %+7.3f %+7.3f  %s" % (
        r[0], r[1], r[2], r[3] * 100, r[4], r[5], r[6]))

print("\n유의기준(사전등록): |t| >= %.1f (멀티테스팅 Bonferroni 근사) & 전·후반 부호 일치 → 통과" % T_THRESH)
print("통합 원칙: 점수 합산 금지. 통과해도 게이트(C)+안전도 컬럼(D)로만. MDD는 게이트 전용.")
print("한계: 상폐/피인수 종목 fwd 결측은 drop → 안전팩터 IC 약간 과대평가 가능. n≈%d 소표본." % (len(allq) - 1))
