# -*- coding: utf-8 -*-
"""
amzn_case.py — 아마존 상장(1997-05-15)부터 지금까지: 어떤 필터가 언제 걸렸을지, 우리 규칙이면 들고 있었을지 (2026-09-30).
가격: Yahoo(분할 반영). 재무 1997~2008: 10-K 연간치(백만$, 근사, 손으로 넣음). 2009~: DB quarterly_financials_us(SEC).
"""
import sys, json, glob, pickle, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, "/data/frame")
import numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

# ---------- 데이터 ----------
A = pd.read_pickle("/tmp/amzn_full.pkl"); S = pd.read_pickle("/tmp/spy_full.pkl")
A.columns = [c[0] for c in A.columns]; S.columns = [c[0] for c in S.columns]
A = A.dropna(subset=["Close"]); S = S.dropna(subset=["Close"])
px, spy = A["Close"], S["Close"].reindex(A.index).ffill()
vol_usd = (A["Close"] * A["Volume"]) / 1e6                       # 일 거래대금(백만$)

# 10-K 연간 매출·영업이익 (백만$) — 1997~2008 은 기억 기반 근사, 2009~ 는 DB 로 덮어씀
FIN = {1997: (148, -33), 1998: (610, -109), 1999: (1640, -606), 2000: (2762, -864), 2001: (3122, -412),
       2002: (3933, 64), 2003: (5264, 271), 2004: (6921, 440), 2005: (8490, 432), 2006: (10711, 389),
       2007: (14835, 655), 2008: (19166, 842), 2009: (24509, 1129), 2010: (34204, 1406), 2011: (48077, 862),
       2012: (61093, 676), 2013: (74452, 745), 2014: (88988, 178), 2015: (107006, 2233), 2016: (135987, 4186),
       2017: (177866, 4106), 2018: (232887, 12421), 2019: (280522, 14541), 2020: (386064, 22899),
       2021: (469822, 24879), 2022: (513983, 12248), 2023: (574785, 36852), 2024: (637959, 68593)}
src = {y: "10-K(근사)" for y in FIN}
import factor_analysis as fa
c = fa.get_conn(); cur = c.cursor()
cur.execute("""SELECT TO_CHAR(end_date,'YYYY'), fp, revenue, op_income FROM quarterly_financials_us
               WHERE code='AMZN' ORDER BY end_date""")
q = pd.DataFrame(cur.fetchall(), columns=["y", "fp", "rev", "op"])
q[["rev", "op"]] = q[["rev", "op"]].apply(pd.to_numeric, errors="coerce")
for y, g in q.groupby("y"):
    g4 = g[g.fp.astype(str).str.upper().str.startswith("Q")]
    if len(g4) == 4 and g4.rev.notna().all():
        FIN[int(y)] = (round(g4.rev.sum() / 1e6), round(g4.op.sum() / 1e6)); src[int(y)] = "DB(SEC)"
cur.execute("""SELECT TO_CHAR(date_,'YYYY'), MIN("RANK") KEEP (DENSE_RANK LAST ORDER BY date_), MAX(marcap) KEEP (DENSE_RANK LAST ORDER BY date_)
               FROM daily_marcap_us WHERE code='AMZN' GROUP BY TO_CHAR(date_,'YYYY') ORDER BY 1""")
rk = {int(r[0]): (int(r[1]) if r[1] is not None else None, float(r[2]) / 1e9 if r[2] is not None else None) for r in cur.fetchall()}
c.close()
# 분할 반영 주식수(십억주) 근사 — 시총 추정용 (1997 5.8 → 2012 9.1 → 2024 10.5, 선형)
SH = {1997: 5.8, 1999: 6.9, 2005: 8.3, 2012: 9.1, 2020: 10.1, 2024: 10.5, 2026: 10.7}
sh = pd.Series(SH); sh = sh.reindex(range(1997, 2027)).interpolate()

# ---------- 연도별 표 ----------
ye = px.groupby(px.index.year).last(); ys = spy.groupby(spy.index.year).last()
rows = []
ath = px.cummax()
for y in ye.index:
    p = px[px.index.year == y]
    r = ye[y] / ye.get(y - 1, px.iloc[0]) - 1 if y > 1997 else ye[y] / px.iloc[0] - 1
    rs = ys[y] / ys.get(y - 1, spy.iloc[0]) - 1 if y > 1997 else ys[y] / spy.iloc[0] - 1
    dd_in_year = (p / p.cummax() - 1).min()
    dd_ath = ye[y] / ath[p.index[-1]] - 1
    rev, op = FIN.get(y, (np.nan, np.nan))
    prev = FIN.get(y - 1, (np.nan, np.nan))[0]
    mcap = rk.get(y, (None, None))[1] or ye[y] * sh[y]
    rows.append({"연도": y, "연말가격$": round(ye[y], 3), "연수익률%": round(r * 100, 1), "SPY%": round(rs * 100, 1),
                 "SPY대비%p": round((r - rs) * 100, 1), "연중최대낙폭%": round(dd_in_year * 100, 1),
                 "고점대비%(연말)": round(dd_ath * 100, 1), "52주고점대비%(연말)": round((ye[y] / px[:p.index[-1]].iloc[-252:].max() - 1) * 100, 1),
                 "시총(십억$,근사)": round(mcap, 0), "S&P시총순위(DB)": rk.get(y, (None,))[0],
                 "매출(백만$)": rev, "매출증가%": round((rev / prev - 1) * 100, 0) if prev and prev == prev else np.nan,
                 "영업이익(백만$)": op, "영업이익률%": round(op / rev * 100, 1) if rev == rev and rev else np.nan, "재무출처": src.get(y, ""),
                 "지금까지 배수": round(px.iloc[-1] / ye[y], 1)})
Y = pd.DataFrame(rows)

# ---------- 큰 낙폭 ----------
dd = px / px.cummax() - 1
falls, in_dd, start = [], False, None
for d, v in dd.items():
    if not in_dd and v < -0.30: in_dd, start = True, d
    if in_dd and v == 0:
        seg = dd[start:d]; t = seg.idxmin()
        falls.append({"고점": ath[:start].idxmax().date(), "저점": t.date(), "낙폭%": round(seg.min() * 100, 1),
                      "회복": d.date(), "고점→회복(년)": round((d - ath[:start].idxmax()).days / 365.25, 1)}); in_dd = False
if in_dd:
    seg = dd[start:]; t = seg.idxmin()
    falls.append({"고점": ath[:start].idxmax().date(), "저점": t.date(), "낙폭%": round(seg.min() * 100, 1), "회복": "아직", "고점→회복(년)": np.nan})
D = pd.DataFrame(falls)

# ---------- 규칙 판정: 각 해 첫 거래일에 샀다면 ----------
rules = []
for y in range(1998, 2026):
    p = px[px.index.year >= y]; e = p.iloc[0]
    def stopped(kind, pct):
        if kind == "fixed": hit = p[p <= e * (1 - pct)]
        else: hit = p[p <= p.cummax() * (1 - pct)]
        return hit.index[0].date() if len(hit) else None
    f15, f25, t15, t25 = stopped("fixed", .15), stopped("fixed", .25), stopped("trail", .15), stopped("trail", .25)
    i = px.index.get_loc(p.index[0])
    mom = px.iloc[i - 21] / px.iloc[i - 252] - 1 if i >= 252 else np.nan
    smom = spy.iloc[i - 21] / spy.iloc[i - 252] - 1 if i >= 252 else np.nan
    nh = px.iloc[i] / px.iloc[max(0, i - 252):i + 1].max()
    rev, op = FIN.get(y - 1, (np.nan, np.nan)); prev = FIN.get(y - 2, (np.nan, np.nan))[0]
    g = rev / prev - 1 if prev and prev == prev else np.nan
    a1 = vol_usd.iloc[i - 126:i].mean(); a0 = vol_usd.iloc[i - 252:i - 126].mean() if i >= 252 else np.nan
    rules.append({"매수연도": y, "매수가$": round(e, 3), "지금 배수": round(px.iloc[-1] / e, 1),
                  "1년후%": round((px.iloc[min(i + 252, len(px) - 1)] / e - 1) * 100, 0),
                  "3년후%": round((px.iloc[min(i + 756, len(px) - 1)] / e - 1) * 100, 0),
                  "고정손절-15%": f15 or "안 걸림", "고정손절-25%": f25 or "안 걸림", "추적손절-15%": t15 or "안 걸림", "추적손절-25%": t25 or "안 걸림",
                  "12-1모멘텀%": round(mom * 100, 0) if mom == mom else np.nan, "SPY 12-1%": round(smom * 100, 0) if smom == smom else np.nan,
                  "모멘텀 SPY보다 +20p": "O" if mom == mom and mom - smom >= .20 else "X",
                  "52주고점대비%": round(nh * 100, 0), "고점 85% 이상": "O" if nh >= .85 else "X",
                  "전년 매출증가 ≥20%": "O" if g == g and g >= .20 else "X", "전년 영업이익>0": "O" if op == op and op > 0 else "X",
                  "거래대금 6개월↑": "O" if a0 == a0 and a1 > a0 else "X"})
RU = pd.DataFrame(rules)

# ---------- 우리 스크리너(2015-12 ~ 2026-09 캐시)가 AMZN 을 골랐나 ----------
import candidates as C
cfg = json.load(open("/data/frame/auto_buy_config.json", encoding="utf-8"))["candidates"]
sel = []
for f in sorted(glob.glob("/data/frame/paper/replay_cache/screen_v2_lag45_sp500pit_*.pkl")):
    d = pickle.load(open(f, "rb")); u = d["universe"]
    if u is None or not len(u) or "CODE" not in u.columns:
        continue
    row = u[u.CODE == "AMZN"]
    def g(col, cast=float):
        try:
            return cast(row[col].iloc[0]) if len(row) and col in row.columns and pd.notna(row[col].iloc[0]) else np.nan
        except Exception:
            return np.nan
    try:
        top = C.top_fill(u, n=cfg["n"], mc_top=cfg["mc_top"], amt_top=cfg["amt_top"], rs_min=cfg["rs_min"], high_min=cfg["high_min"])
        tick = list(top["티커"]) if "티커" in top.columns else list(top.get("CODE", top.index))
        picked = "O" if "AMZN" in tick else "X"
    except Exception as e:
        picked = f"계산불가({type(e).__name__})"
    ls = g("lead_score")
    sel.append({"기준일": d["asof"], "AMZN 상위10 선정": picked,
                "게이트 통과": g("leader_pass", bool) if "leader_pass" in u.columns else np.nan,
                "탈락사유": g("탈락사유", str) if len(row) else "유니버스에 없음",
                "rs_pct": g("rs_pct"), "near_high": g("near_high"),
                "점수순위": int((u.lead_score > ls).sum() + 1) if ls == ls and "lead_score" in u.columns else np.nan,
                "시총순위": g("RANK0", int)})
SEL = pd.DataFrame(sel)
SEL["연도"] = SEL["기준일"].str[:4]
sel_year = SEL.groupby("연도").agg(회차=("기준일", "size"), 선정=("AMZN 상위10 선정", lambda s: (s == "O").sum()),
                                 게이트통과=("게이트 통과", lambda s: int(pd.Series(s).fillna(False).sum())),
                                 평균RS=("rs_pct", "mean"), 평균고점대비=("near_high", "mean"), 평균점수순위=("점수순위", "mean")).round(0)
# 선정 구간에서 AMZN 성과 vs 미선정 구간
SEL["d"] = pd.to_datetime(SEL["기준일"])
nxt = SEL["d"].shift(-1).fillna(px.index[-1])
ret = [(px[px.index >= b].iloc[0] / px[px.index >= a].iloc[0] - 1) if (px.index >= b).any() else np.nan for a, b in zip(SEL["d"], nxt)]
SEL["다음 회차까지 AMZN%"] = np.round(np.array(ret, dtype=float) * 100, 1)

pd.set_option("display.width", 260); pd.set_option("display.max_columns", 40)
print("=== 연도별 ==="); print(Y.to_string(index=False))
print("=== 30% 이상 낙폭 ==="); print(D.to_string(index=False))
print("=== 그 해 첫날 샀다면 ==="); print(RU.to_string(index=False))
print("=== 우리 스크리너 캐시 ==="); print(sel_year.to_string())
print("선정 O 구간 평균%:", round(SEL.loc[SEL["AMZN 상위10 선정"] == "O", "다음 회차까지 AMZN%"].mean(), 2),
      "| X 구간 평균%:", round(SEL.loc[SEL["AMZN 상위10 선정"] == "X", "다음 회차까지 AMZN%"].mean(), 2),
      "| 선정 횟수", int((SEL["AMZN 상위10 선정"] == "O").sum()), "/", len(SEL))
print("탈락사유 분포:", SEL["탈락사유"].value_counts().head(8).to_dict())

# ---------- 차트 ----------
kf = [f for f in font_manager.findSystemFonts() if any(k in f.lower() for k in ("nanum", "notosanscjk", "malgun", "batang", "gulim"))]
if kf: plt.rcParams["font.family"] = font_manager.FontProperties(fname=kf[0]).get_name()
plt.rcParams["axes.unicode_minus"] = False
fig, (ax, ax2) = plt.subplots(2, 1, figsize=(15, 9), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
ax.plot(px.index, px, lw=1.1, color="#e67e22", label="AMZN (분할 반영)")
ax.plot(spy.index, spy / spy.iloc[0] * px.iloc[0], lw=0.9, color="#7f8c8d", label="SPY (같은 출발점)")
ax.set_yscale("log"); ax.grid(alpha=.3, which="both"); ax.legend(loc="upper left")
ax.set_title("Amazon 1997-05-15 상장 ~ 2026-09 (로그축)")
pk99 = px[:"2001-12-31"].idxmax(); bt01 = px["2000":"2002"].idxmin(); rec = px[px.index > pk99][px[px.index > pk99] >= px[pk99]].index[0]
pk07 = px["2007"].idxmax(); bt08 = px["2008-06":"2009-06"].idxmin(); pk21 = px["2021"].idxmax(); bt22 = px["2022"].idxmin()
for d, txt, dy in ((px.index[0], f"상장 ${px.iloc[0]:.3f}\n(액면 18$)", 1.8), (pk99, f"닷컴 고점 ${px[pk99]:.2f}", 1.8),
                   (bt01, f"저점 ${px[bt01]:.2f}\n고점대비 {px[bt01]/px[pk99]-1:.0%}", 0.35), (rec, "1999 고점 회복\n(%.1f년 걸림)" % ((rec - pk99).days / 365.25), 0.45),
                   (bt08, f"2008 {px[bt08]/px[pk07]-1:.0%}", 0.4), (pd.Timestamp("2015-04-24"), "AWS 실적 첫 공개", 0.45),
                   (bt22, f"2022 {px[bt22]/px[pk21]-1:.0%}", 0.45)):
    ax.annotate(txt, (d, px[d]), xytext=(d, px[d] * dy), fontsize=8, ha="center", arrowprops=dict(arrowstyle="-", color="gray", lw=.6))
for y in range(1998, 2027, 2):
    ax.axvline(pd.Timestamp(f"{y}-01-01"), color="k", alpha=.05)
ax2.fill_between(dd.index, dd * 100, 0, color="#c0392b", alpha=.5); ax2.set_ylabel("고점 대비 %"); ax2.grid(alpha=.3)
ax2.axhline(-15, color="k", ls="--", lw=.7); ax2.text(px.index[30], -13, "-15% (손절선)", fontsize=8)
ax2.axhline(-50, color="k", ls=":", lw=.7)
plt.tight_layout(); plt.savefig("/data/frame/amzn_chart.png", dpi=110)

with pd.ExcelWriter("/data/frame/amzn_case_study.xlsx", engine="openpyxl") as xw:
    Y.to_excel(xw, sheet_name="연도별", index=False); D.to_excel(xw, sheet_name="30%이상낙폭", index=False)
    RU.to_excel(xw, sheet_name="그해첫날샀다면", index=False); sel_year.to_excel(xw, sheet_name="우리스크리너_연도별")
    SEL.drop(columns=["d"]).to_excel(xw, sheet_name="우리스크리너_회차별", index=False)
    pd.DataFrame({"항목": ["가격", "재무", "시총", "우리 스크리너", "주의"],
                  "값": ["Yahoo Finance, 분할 반영(1998 2:1, 1999 3:1·2:1, 2022 20:1 → 액면 18$ = 지금 0.075$)",
                        "1997~2008 10-K 연간치 기억 기반 근사(백만$), 2009~ DB quarterly_financials_us(SEC) 4분기 합",
                        "2012~ DB daily_marcap_us, 그 전은 가격 × 분할반영 주식수(근사)",
                        "paper/replay_cache 의 시점별 S&P 500 스크린(2015-12~2026-09) 에 지금 설정(top_fill n=10, rs≥80, 고점≥85%) 적용",
                        "AMZN 은 살아남은 회사를 뒤돌아보는 것(생존편향). 같은 필터가 1999 에 고른 다른 회사들의 결과는 이 표에 없다"]}
                 ).to_excel(xw, sheet_name="설명", index=False)
print("저장: amzn_chart.png, amzn_case_study.xlsx")
