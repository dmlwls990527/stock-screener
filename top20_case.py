# -*- coding: utf-8 -*-
"""
top20_case.py — 지금 시총 상위 20 회사의 상장(야후 데이터 시작)~현재 전 구간에 같은 필터를 걸어 본다 (2026-09-30).
질문: "필터에 걸렸고, 그 뒤 충분히 이득을 볼 수 있었나?" (끝까지 들고 있을 필요 없음 → 1년·3년·손절·게이트탈락 매도로 각각 계산)

필터(월말 평가, 사전 고정):
  P1 12-1 모멘텀이 S&P 500(^GSPC) 보다 +20%p 이상   P2 종가가 52주 고점의 85% 이상   P3 최근 6개월 거래대금 > 그 전 6개월
  F  (재무 있는 2009~) TTM 매출 전년 대비 +20% 이상 & TTM 영업이익 > 0   (45일 지연)
  가격필터 = P1&P2&P3,  전체필터 = 가격필터 & F
에피소드: 필터가 6개월 이상 꺼져 있다가 켜진 달. 다음 달 첫 거래일 시가 매수.
매도 규칙: 1년 보유 / 3년 보유 / 고정손절 −15%(최대 3년) / 추적손절 −25%(최대 3년) / 필터 2개월 연속 꺼지면(최대 3년)
대조군: DB 전체 종목(지금 구성 — 생존편향 있음) 2013~ 같은 필터·같은 규칙. 상위 20 = 살아남은 승자 20 이므로 필터 실력은 대조군과의 차이로만 읽는다.
기준지수 ^GSPC 는 배당 제외(SPY 총수익보다 연 약 2%p 낮게 잡힘) → 초과수익이 그만큼 후하게 나온다.
"""
import sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, "/data/frame")
import numpy as np, pandas as pd, yfinance as yf
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
import factor_analysis as fa, research_bt as R, replay, growth_factors as gf

OFF_MONTHS, MAXH = 6, 756
TICKERS = None


def top20():
    c = fa.get_conn(); cur = c.cursor()
    cur.execute("""SELECT m.code, t.name FROM daily_marcap_us m LEFT JOIN ticker_master_us t ON t.code=m.code
                   WHERE m.date_=(SELECT MAX(date_) FROM daily_marcap_us) AND m."RANK"<=25 ORDER BY m."RANK" """)
    rows = [(r[0], r[1]) for r in cur.fetchall()]; c.close()
    out = [(k, n) for k, n in rows if k != "GOOG"][:20]                 # 알파벳 중복 제거
    return out


_TTM = {}


def ttm(fin, asof, lag=45):
    cut = pd.Timestamp(asof) - pd.Timedelta(days=lag)
    if cut in _TTM:
        return _TTM[cut]
    g = fin[(fin["end"] <= cut) & (fin["end"] > cut - pd.Timedelta(days=430))].groupby("code").tail(4)
    s = g.groupby("code").agg(n=("rev", "size"), rev=("rev", "sum"), op=("op", "sum"))
    _TTM[cut] = s[s["n"] == 4]
    return _TTM[cut]


def fund_ok(fin, asof, codes):
    """+1 통과 / 0 탈락 / NaN 재무 없음"""
    now, prev = ttm(fin, asof), ttm(fin, pd.Timestamp(asof) - pd.Timedelta(days=365))
    g = (now["rev"] / prev["rev"].reindex(now.index) - 1)
    ok = ((g >= 0.20) & (now["op"] > 0) & (prev["rev"].reindex(now.index) > 0)).astype(float)
    ok[g.isna()] = np.nan
    return ok.reindex(codes)


def month_ends(idx):
    s = pd.Series(idx, index=idx)
    return s.groupby([idx.year, idx.month]).last().tolist()


def signals_one(C, O, DV, B, fin, code):
    """월말별 P1·P2·P3·F 와 가격/전체 필터 (Series, index=월말)."""
    me = [d for d in month_ends(C.index) if C.index.get_loc(d) >= 252]
    rows = []
    b = B.reindex(C.index).ffill()
    for d in me:
        i = C.index.get_loc(d)
        mom = C.iloc[i - 21] / C.iloc[i - 252] - 1; bm = b.iloc[i - 21] / b.iloc[i - 252] - 1
        p1 = (mom - bm) >= 0.20
        p2 = C.iloc[i] / C.iloc[i - 251:i + 1].max() >= 0.85
        p3 = DV.iloc[i - 125:i + 1].mean() > DV.iloc[i - 251:i - 125].mean()
        rows.append({"d": d, "P1": p1, "P2": p2, "P3": p3})
    S = pd.DataFrame(rows).set_index("d")
    S["price"] = S.P1 & S.P2 & S.P3
    F = pd.Series([fund_ok(fin, d, [code]).iloc[0] for d in S.index], index=S.index)
    S["F"] = F
    S["full"] = S["price"] & (F == 1)
    S.loc[F.isna(), "full"] = np.nan
    return S


def episodes(sig):
    """sig: bool/NaN Series(월말). 6개월 꺼져 있다가 켜진 달."""
    s = sig.fillna(False).astype(bool)
    out = []
    for k in range(len(s)):
        if s.iloc[k] and not s.iloc[max(0, k - OFF_MONTHS):k].any():
            out.append(s.index[k])
    return out


def outcome(C, O, B, sig_m, t_signal):
    """t_signal 월말 → 다음 거래일 시가 매수. 규칙별 (수익률, 기준지수 수익률, 보유일)."""
    i0 = C.index.get_loc(t_signal) + 1
    if i0 >= len(C):
        return None
    e = O.iloc[i0] if pd.notna(O.iloc[i0]) and O.iloc[i0] > 0 else C.iloc[i0]
    b = B.reindex(C.index).ffill(); be = b.iloc[i0]
    n = len(C) - 1
    res = {"매수일": C.index[i0].date(), "매수가": e, "지금배수": C.iloc[-1] / e}
    def close_at(k):
        k = min(k, n); return C.iloc[k], b.iloc[k], k - i0, k == min(k, n) and (k <= n)
    for name, h in (("1년보유", 252), ("3년보유", 756)):
        if i0 + h > n:
            res[name] = res[name + "_지수"] = np.nan; res[name + "_일"] = np.nan; continue
        c1, b1, hd, _ = close_at(i0 + h)
        res[name], res[name + "_지수"], res[name + "_일"] = c1 / e - 1, b1 / be - 1, hd
    seg = C.iloc[i0:min(i0 + MAXH, n) + 1]
    res["1년내 최대낙폭"] = (C.iloc[i0:min(i0 + 252, n) + 1] / e).min() - 1
    full3 = i0 + MAXH <= n
    # 고정 −15%
    hit = seg[seg <= e * 0.85]
    k = C.index.get_loc(hit.index[0]) if len(hit) else min(i0 + MAXH, n)
    res["고정손절15"], res["고정손절15_지수"], res["고정손절15_일"] = C.iloc[k] / e - 1, b.iloc[k] / be - 1, k - i0
    res["고정손절15_걸림"] = bool(len(hit))
    # 추적 −25%
    tr = seg[seg <= seg.cummax() * 0.75]
    k = C.index.get_loc(tr.index[0]) if len(tr) else min(i0 + MAXH, n)
    res["추적손절25"], res["추적손절25_지수"], res["추적손절25_일"] = C.iloc[k] / e - 1, b.iloc[k] / be - 1, k - i0
    res["추적손절25_걸림"] = bool(len(tr))
    # 필터 2개월 연속 꺼지면 다음 날 시가 매도 (최대 3년)
    s = sig_m.fillna(False).astype(bool)
    after = s[s.index > t_signal]
    k = min(i0 + MAXH, n); hitg = False
    for j in range(1, len(after)):
        if not after.iloc[j] and not after.iloc[j - 1]:
            kk = C.index.get_loc(after.index[j]) + 1
            if kk < k:
                k, hitg = kk, True
            break
    res["게이트탈락"], res["게이트탈락_지수"], res["게이트탈락_일"] = C.iloc[k] / e - 1, b.iloc[k] / be - 1, k - i0
    res["게이트탈락_걸림"] = hitg
    res["3년완결"] = full3
    return res


RULES = ["1년보유", "3년보유", "고정손절15", "추적손절25", "게이트탈락"]


def summarize(E, label):
    """에피소드 표 → 규칙별 중앙값 초과수익·승률."""
    rows = []
    for r in RULES:
        d = E.dropna(subset=[r])
        if r == "3년보유":
            d = d[d["3년완결"]]
        ex = d[r] - d[r + "_지수"]
        rows.append({"집합": label, "규칙": r, "에피소드": len(d), "중앙 수익%": round(d[r].median() * 100, 1),
                     "중앙 지수%": round(d[r + "_지수"].median() * 100, 1), "중앙 초과%p": round(ex.median() * 100, 1),
                     "평균 초과%p": round(ex.mean() * 100, 1), "지수 이긴 비율%": round((ex > 0).mean() * 100, 0),
                     "중앙 보유일": round(d[r + "_일"].median(), 0),
                     "걸린 비율%": round(d[r + "_걸림"].mean() * 100, 0) if r + "_걸림" in d.columns and len(d) else np.nan})
    return pd.DataFrame(rows)


def main():
    fin = R.fin_table()
    tk = top20()
    B = yf.download("^GSPC", start="1950-01-01", auto_adjust=True, progress=False)["Close"]
    B = B.iloc[:, 0] if isinstance(B, pd.DataFrame) else B
    B = B.dropna()
    data, comp, epis, sigs = {}, [], [], {}
    for code, name in tk:
        df = yf.download(code, start="1950-01-01", auto_adjust=True, progress=False)
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        df = df.dropna(subset=["Close"])
        C, O, DV = df["Close"], df["Open"], df["Close"] * df["Volume"]
        data[code] = (C, O, DV)
        S = signals_one(C, O, DV, B, fin, code); sigs[code] = S
        ep_p, ep_f = episodes(S["price"]), episodes(S["full"])
        for kind, eps in (("가격", ep_p), ("전체", ep_f)):
            for t in eps:
                o = outcome(C, O, B, S["price"] if kind == "가격" else S["full"], t)
                if o:
                    epis.append({"티커": code, "회사": name, "필터": kind, "신호월말": t.date(), **o})
        first_p = ep_p[0] if ep_p else None
        first_f = ep_f[0] if ep_f else None
        yahoo_floor = C.index[0] <= pd.Timestamp("1962-01-31")
        comp.append({"티커": code, "회사": name, "데이터 시작": C.index[0].date(), "1962 이전 상장(야후 시작)": "O" if yahoo_floor else "",
                     "지금 가격": round(C.iloc[-1], 2), "시작→지금 배수": round(C.iloc[-1] / C.iloc[0], 1),
                     "가격필터 첫 신호": first_p.date() if first_p is not None else None,
                     "첫 신호 뒤 배수": round(C.iloc[-1] / C[C.index > first_p].iloc[0], 1) if first_p is not None else np.nan,
                     "가격필터 에피소드": len(ep_p), "전체필터 첫 신호(2009~)": first_f.date() if first_f is not None else None,
                     "전체필터 에피소드": len(ep_f),
                     "필터 켜진 달 비율%": round(S["price"].mean() * 100, 0), "재무 있는 달": int(S["F"].notna().sum())})
        print(f"{code:6} {name[:22]:22} 시작 {C.index[0].date()} 가격필터 첫 {first_p.date() if first_p is not None else '-'} "
              f"에피소드 {len(ep_p):2}/{len(ep_f):2} 켜진달 {S['price'].mean()*100:3.0f}%", flush=True)
    COMP = pd.DataFrame(comp); EP = pd.DataFrame(epis)
    for c in RULES + ["1년내 최대낙폭"]:
        pass
    # ---------- 대조군: DB 전체 종목 2013~ ----------
    print("대조군 계산…", flush=True)
    latest = replay.db_latest_date()
    panel, _ = replay.load_prices("2012-01-01", latest)
    CL, OP = panel["CLOSE"].copy(), panel["OPEN"].copy()
    CL.index = pd.to_datetime(CL.index); OP.index = pd.to_datetime(OP.index)          # load_prices 는 문자열 날짜
    AMT = gf.load_panels()["AMT"].copy()
    AMT.index = pd.to_datetime(AMT.index)
    AMT = AMT.reindex(CL.index).astype(float)
    bC = B.reindex(CL.index).ffill()
    me = [d for d in month_ends(CL.index) if CL.index.get_loc(d) >= 252]
    P, FULL = {}, {}
    for d in me:
        i = CL.index.get_loc(d)
        mom = CL.iloc[i - 21] / CL.iloc[i - 252] - 1; bm = bC.iloc[i - 21] / bC.iloc[i - 252] - 1
        p1 = (mom - bm) >= 0.20
        p2 = CL.iloc[i] / CL.iloc[i - 251:i + 1].max() >= 0.85
        p3 = AMT.iloc[i - 125:i + 1].mean() > AMT.iloc[i - 251:i - 125].mean()
        pr = (p1 & p2 & p3).fillna(False)
        f = fund_ok(fin, d, CL.columns)
        P[d] = pr
        fu = pr & (f == 1); fu[f.isna()] = np.nan
        FULL[d] = fu
    P = pd.DataFrame(P).T; FULL = pd.DataFrame(FULL).T
    ctrl = []
    for kind, SIG in (("가격", P), ("전체", FULL)):
        for code in SIG.columns:
            s = SIG[code]
            if s.fillna(False).astype(bool).sum() == 0:
                continue
            C = CL[code].dropna(); O = OP[code].reindex(C.index)
            if len(C) < 300:
                continue
            sm = s.reindex([d for d in s.index if d in C.index])
            for t in episodes(sm):
                o = outcome(C, O, B, sm, t)
                if o:
                    ctrl.append({"티커": code, "필터": kind, "신호월말": t.date(), **o})
    CT = pd.DataFrame(ctrl)
    top_set = set(COMP["티커"])
    CT["상위20"] = CT["티커"].isin(top_set)
    EP["신호연도"] = pd.to_datetime(EP["신호월말"]).dt.year
    summ = pd.concat([summarize(EP[EP["필터"] == "가격"], "상위20 · 가격필터 · 전구간"),
                      summarize(EP[(EP["필터"] == "가격") & (EP["신호연도"] >= 2013)], "상위20 · 가격필터 · 2013~"),
                      summarize(EP[EP["필터"] == "전체"], "상위20 · 전체필터 · 2009~"),
                      summarize(EP[(EP["필터"] == "전체") & (EP["신호연도"] >= 2013)], "상위20 · 전체필터 · 2013~"),
                      summarize(CT[(CT["필터"] == "가격")], "대조군 DB전체 · 가격필터 · 2013~"),
                      summarize(CT[(CT["필터"] == "가격") & ~CT["상위20"]], "대조군 상위20 제외 · 가격필터 · 2013~"),
                      summarize(CT[(CT["필터"] == "전체")], "대조군 DB전체 · 전체필터 · 2013~"),
                      summarize(CT[(CT["필터"] == "전체") & ~CT["상위20"]], "대조군 상위20 제외 · 전체필터 · 2013~")],
                     ignore_index=True)
    # 대조군 에피소드 1년 뒤 분포 (필터가 고른 종목이 얼마나 갈렸나)
    d1 = CT[(CT["필터"] == "가격")].dropna(subset=["1년보유"])
    ex1 = (d1["1년보유"] - d1["1년보유_지수"]) * 100
    dist = pd.DataFrame({"구간": ["< −30%p", "−30~−10", "−10~+10", "+10~+30", "+30~+100", "> +100%p"],
                         "에피소드 수": [int((ex1 < -30).sum()), int(((ex1 >= -30) & (ex1 < -10)).sum()), int(((ex1 >= -10) & (ex1 < 10)).sum()),
                                     int(((ex1 >= 10) & (ex1 < 30)).sum()), int(((ex1 >= 30) & (ex1 < 100)).sum()), int((ex1 >= 100).sum())]})
    dist["비율%"] = (dist["에피소드 수"] / len(ex1) * 100).round(1)
    pd.set_option("display.width", 260); pd.set_option("display.max_columns", 40)
    print("=== 종목별 ==="); print(COMP.to_string(index=False))
    print("=== 규칙별 요약 ==="); print(summ.to_string(index=False))
    print("=== 대조군 가격필터 1년 초과수익 분포 ==="); print(dist.to_string(index=False))
    # ---------- 차트 ----------
    kf = [f for f in font_manager.findSystemFonts() if any(k in f.lower() for k in ("nanum", "notosanscjk", "malgun", "batang", "gulim"))]
    if kf: plt.rcParams["font.family"] = font_manager.FontProperties(fname=kf[0]).get_name()
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(5, 4, figsize=(22, 20))
    for ax, (code, name) in zip(axes.ravel(), tk):
        C, O, DV = data[code]; S = sigs[code]
        ax.plot(C.index, C, lw=.8, color="#e67e22")
        on = S.index[S["price"].fillna(False).astype(bool)]
        ax.scatter(on, C.reindex(on), s=6, color="#2980b9", zorder=3, label="가격필터 켜진 달")
        ep = episodes(S["price"]); ax.scatter(ep, C.reindex(ep) * 0.8, marker="^", s=45, color="#27ae60", zorder=4, label="에피소드 시작(6개월+ 꺼진 뒤)")
        epf = episodes(S["full"]); ax.scatter(epf, C.reindex(epf) * 0.65, marker="*", s=70, color="#8e44ad", zorder=5, label="전체필터(재무 포함) 시작")
        ax.set_yscale("log"); ax.grid(alpha=.3, which="both")
        r = COMP[COMP["티커"] == code].iloc[0]
        ax.set_title(f"{code} {name[:18]} | {C.index[0].year}~ | 시작→지금 {r['시작→지금 배수']:,}배 | 첫 신호 {r['가격필터 첫 신호']} → {r['첫 신호 뒤 배수']}배", fontsize=9)
        ax.tick_params(labelsize=7)
    axes.ravel()[0].legend(fontsize=7, loc="upper left")
    plt.suptitle("지금 시총 상위 20 — 상장(야후 시작)부터 지금까지, 가격필터(모멘텀 +20%p · 52주고점 85% · 거래대금↑) 가 켜진 달", fontsize=12)
    plt.tight_layout(rect=(0, 0, 1, .98)); plt.savefig("/data/frame/top20_charts.png", dpi=95)
    with pd.ExcelWriter("/data/frame/top20_case_study.xlsx", engine="openpyxl") as xw:
        COMP.to_excel(xw, sheet_name="종목별", index=False)
        summ.to_excel(xw, sheet_name="규칙별요약", index=False)
        dist.to_excel(xw, sheet_name="대조군1년분포", index=False)
        E2 = EP.copy()
        for c in E2.columns:
            if E2[c].dtype == float and c not in ("매수가", "지금배수") and not c.endswith("_일"):
                E2[c] = (E2[c] * 100).round(1)
        E2.to_excel(xw, sheet_name="에피소드_상위20", index=False)
        CT2 = CT.copy()
        for c in CT2.columns:
            if CT2[c].dtype == float and c not in ("매수가", "지금배수") and not c.endswith("_일"):
                CT2[c] = (CT2[c] * 100).round(1)
        CT2.to_excel(xw, sheet_name="에피소드_대조군", index=False)
        pd.DataFrame({"항목": ["필터", "에피소드", "매도 규칙", "대조군", "기준지수", "주의"],
                      "값": ["P1 12-1 모멘텀 ≥ S&P500 +20%p, P2 종가 ≥ 52주고점 85%, P3 최근 6개월 거래대금 > 그 전 6개월; 전체필터 = + TTM 매출 +20%↑ & 영업이익>0 (2009~, 45일 지연)",
                            "필터가 6개월 이상 꺼져 있다가 켜진 달 → 다음 거래일 시가 매수",
                            "1년 보유 / 3년 보유 / 고정손절 −15% / 추적손절 −25% / 필터 2개월 연속 꺼지면 매도 (손절·게이트는 최대 3년)",
                            "DB 전체 종목(지금 S&P 구성 — 생존편향) 2013~ 같은 필터·규칙. '상위20 제외' 가 진짜 비교 대상",
                            "^GSPC 배당 제외 → 초과수익이 연 약 2%p 후하게 나옴",
                            "상위 20 은 살아남은 승자를 뒤돌아보는 것. 이 표의 '상위20' 숫자는 필터 실력이 아니라 승자의 숫자다. 필터 실력 = 대조군 숫자"]}
                     ).to_excel(xw, sheet_name="설명", index=False)
    print("저장: top20_charts.png, top20_case_study.xlsx")


if __name__ == "__main__":
    main()
