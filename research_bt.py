#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
research_bt.py — 사전등록 후보 5개 백테스트 (2026-09-29).

  설계 구간 2016-01-04 ~ 2022-12-30 : 규칙 확인용. 여기 결과를 보고 규칙을 바꾸지 않는다.
  봉인 구간 2023-01-03 ~ 최신       : 마지막에 한 번만 연다 (--period holdout). SPY 를 못 이기면 버린다.

  C1  12-1 모멘텀: 매월 마지막 거래일 T, 그때 S&P 500 구성종목 중 CLOSE[T-21]/CLOSE[T-252]-1 상위 50, 동일비중
  C2  모멘텀+질: C1 순위 상위 100 중 TTM 영업이익 > 0 인 종목을 TTM 영업이익률 순 상위 50, 동일비중
      (재무는 분기말 + 45일 뒤에 안 것으로. 과거 부채 데이터는 DB 에 없어 '낮은 부채'는 못 넣음)
  C3  추세: 매일 종가 SPY > SPY 200일 이동평균 이면 SPY, 아니면 BIL(단기 국채) — 다음 날 시가 전환
  C4a SPY 80% + 스크리너 20% (replay F3_nostop: 상위10·순위가중·3주, 시점별 S&P 500), 매년 첫 거래일 80/20
  C4b QQQ 80% + 스크리너 20%
  기준: SPY, QQQ 매수 후 보유 (배당 포함 수정주가)

  매매: 신호는 T 종가, 체결은 T+1 시가. 비용 = 거래금액 × (수수료 0.1% + 슬리피지 0.05%).
  한계: 종목 가격은 지금 DB 에 남은 종목뿐 (상장폐지 종목 없음 → C1·C2·스크리너가 실제보다 유리).
"""
import argparse
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, "/data/frame")

import numpy as np          # noqa: E402
import pandas as pd         # noqa: E402

import replay               # noqa: E402

COST = 0.001 + 0.0005
PERIODS = {"design": ("2016-01-04", "2022-12-30"), "holdout": ("2023-01-03", None)}
ETF_FILE = "/data/frame/data/etf_prices.pkl"
FIN_FILE = "/data/frame/data/quarterly_fin_us.pkl"
SAT_FILE = "/data/frame/paper_replay_final_2016_pit.xlsx"
SAT_COL = "F3_nostop 총자산"


def etf_prices():
    if os.path.exists(ETF_FILE):
        return pd.read_pickle(ETF_FILE)
    import yfinance as yf
    raw = yf.download(["SPY", "QQQ", "BIL"], start="2014-06-01", end="2026-12-31", auto_adjust=True, progress=False)
    d = {"OPEN": raw["Open"], "CLOSE": raw["Close"]}
    for k in d:
        d[k].index = d[k].index.strftime("%Y-%m-%d")
    pd.to_pickle(d, ETF_FILE)
    return d


def fin_table():
    if os.path.exists(FIN_FILE):
        return pd.read_pickle(FIN_FILE)
    import factor_analysis as fa
    c = fa.get_conn()
    cur = c.cursor()
    cur.execute("SELECT code, TO_CHAR(end_date, 'YYYY-MM-DD'), revenue, op_income FROM quarterly_financials_us")
    f = pd.DataFrame(cur.fetchall(), columns=["code", "end", "rev", "op"])
    c.close()
    f["end"] = pd.to_datetime(f["end"])
    f[["rev", "op"]] = f[["rev", "op"]].apply(pd.to_numeric, errors="coerce")
    f = f.dropna().sort_values(["code", "end"])
    pd.to_pickle(f, FIN_FILE)
    return f


def ttm_margin(fin, asof, lag=45):
    cut = pd.Timestamp(asof) - pd.Timedelta(days=lag)
    g = fin[fin["end"] <= cut].groupby("code").tail(4)
    s = g.groupby("code").agg(n=("rev", "size"), rev=("rev", "sum"), op=("op", "sum"))
    s = s[(s["n"] == 4) & (s["rev"] > 0)]
    return (s["op"] / s["rev"]).rename("margin"), s["op"]


class Book:
    """보유 수량 기반 일별 시뮬레이션. rebalance(가중치) 는 그날 시가 체결 (시가 없으면 마지막 종가)."""

    def __init__(self, cash):
        self.cash, self.sh, self.fees, self.turn = float(cash), pd.Series(dtype=float), 0.0, 0.0
        self.last = pd.Series(dtype=float)          # 종목별 마지막으로 본 가격 (시세 없는 날 평가용)

    def _px(self, px, idx):
        p = px.reindex(idx)
        return p.where(p > 0).fillna(self.last.reindex(idx))

    def value(self, px):
        p = self._px(px, self.sh.index).fillna(0)
        return self.cash + float((self.sh * p).sum())

    def mark(self, px):
        good = px[px.notna() & (px > 0)]
        self.last = pd.concat([self.last[~self.last.index.isin(good.index)], good])
        return self.value(px)

    def rebalance(self, w, px_open):
        idx = w.index.union(self.sh.index)
        p = self._px(px_open, idx)
        w = w[p.reindex(w.index).notna()]
        w = w / w.sum() if len(w) and w.sum() > 0 else w
        val = self.cash + float((self.sh * p.reindex(self.sh.index).fillna(0)).sum())
        cur = self.sh.reindex(idx).fillna(0)
        new = (w * val / p.reindex(w.index)).reindex(idx).fillna(0)
        fee = float(((new - cur).abs() * p.fillna(0)).sum()) * COST
        new = new * (1 - fee / val) if val > 0 else new          # 수수료만큼 줄여 현금이 음수가 되지 않게
        trade = float(((new - cur).abs() * p.fillna(0)).sum())
        fee = trade * COST
        self.cash = val - float((new * p.fillna(0)).sum()) - fee
        self.sh = new[new > 1e-12]
        self.fees += fee
        self.turn += trade


def run_book(days, open_, close, weights_by_day, cash=10000.0):
    """weights_by_day: {체결일: Series(가중치)} → (일별 자산 Series, Book)."""
    b = Book(cash)
    out = {}
    for d in days:
        if d in weights_by_day:
            b.rebalance(weights_by_day[d], open_.loc[d])
        out[d] = b.mark(close.loc[d])
    return pd.Series(out), b


def month_end_signals(days):
    s = pd.Series(days, index=pd.to_datetime(days))
    last = s.groupby([s.index.year, s.index.month]).last().tolist()
    nxt = {last[i]: days[days.index(last[i]) + 1] for i in range(len(last)) if days.index(last[i]) + 1 < len(days)}
    return nxt                                       # 신호일 → 체결일


def mom_scores(close, T, members):
    i = close.index.get_loc(T)
    if i < 252:
        return pd.Series(dtype=float)
    p0, p1 = close.iloc[i - 252], close.iloc[i - 21]
    cols = [c for c in members if c in close.columns]
    m = (p1[cols] / p0[cols] - 1).replace([np.inf, -np.inf], np.nan).dropna()
    return m[close.loc[T, m.index].notna()]


def stats(eq, days_all):
    eq = eq.dropna()
    yrs = (pd.Timestamp(eq.index[-1]) - pd.Timestamp(eq.index[0])).days / 365.25
    tot = eq.iloc[-1] / eq.iloc[0]
    r = eq.pct_change().dropna()
    y = eq.groupby(pd.to_datetime(eq.index).year).last()
    ys = (y / y.shift(1).fillna(eq.iloc[0]) - 1) * 100
    return {"총수익률%": round((tot - 1) * 100, 1), "연평균%": round((tot ** (1 / yrs) - 1) * 100, 2),
            "최대낙폭%": round(((eq / eq.cummax()) - 1).min() * 100, 1),
            "연변동성%": round(r.std() * np.sqrt(252) * 100, 1)}, ys.round(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=list(PERIODS), default="design")
    a = ap.parse_args()
    start, end = PERIODS[a.period]
    latest = replay.db_latest_date()
    end = end or latest
    panel, _ = replay.load_prices("2014-12-25", latest)
    op, cl = panel["OPEN"], panel["CLOSE"]
    etf = etf_prices()
    days = [d for d in cl.index if start <= d <= end and d in etf["CLOSE"].index]
    print(f"기간 {a.period}: {days[0]} ~ {days[-1]} ({len(days)}거래일)")
    fin = fin_table()
    sig = month_end_signals([d for d in cl.index if d <= end])
    res, extra = {}, {}

    # C1 / C2
    w1, w2 = {}, {}
    for T, D in sig.items():
        if D < days[0] or D > days[-1]:
            continue
        mem = replay.sp500_members(T)
        m = mom_scores(cl, T, mem)
        if len(m) < 60:
            continue
        top50 = m.nlargest(50).index
        w1[D] = pd.Series(1 / 50, index=top50)
        top100 = m.nlargest(100).index
        mg, opi = ttm_margin(fin, T)
        q = mg.reindex(top100)
        q = q[(opi.reindex(top100) > 0) & q.notna()]
        pick = q.nlargest(50).index
        if len(pick):
            w2[D] = pd.Series(1 / len(pick), index=pick)
    first_signal = min(w1)
    d1 = [d for d in days if d >= first_signal]
    for name, W in (("C1 12-1모멘텀 상위50", w1), ("C2 모멘텀+이익률 상위50", w2)):
        eq, b = run_book(d1, op, cl, W)
        res[name] = eq
        extra[name] = {"누적수수료$": round(b.fees, 0), "연회전율%": round(b.turn / eq.mean() / ((len(d1)) / 252) * 100, 0)}

    # 기준 ETF 매수후보유, C3 추세
    eo, ec = etf["OPEN"], etf["CLOSE"]
    for s in ("SPY", "QQQ"):
        eq, b = run_book(d1, eo, ec, {d1[0]: pd.Series({s: 1.0})})
        res[f"기준 {s} 보유"] = eq
        extra[f"기준 {s} 보유"] = {"누적수수료$": round(b.fees, 0), "연회전율%": 0}
    sma = ec["SPY"].rolling(200).mean()
    up = (ec["SPY"] > sma)
    W3, state = {}, None
    for i, d in enumerate(d1):
        prev = ec.index[ec.index.get_loc(d) - 1]          # 전날 종가 신호 → 오늘 시가 체결
        want = "SPY" if bool(up.loc[prev]) else "BIL"
        if want != state:
            W3[d] = pd.Series({want: 1.0})
            state = want
    eq, b = run_book(d1, eo, ec, W3)
    res["C3 SPY 200일선 추세"] = eq
    extra["C3 SPY 200일선 추세"] = {"누적수수료$": round(b.fees, 0), "전환횟수": len(W3) - 1}

    # C4 코어-위성 (위성 = replay 스크리너 곡선, 매년 첫 거래일 80/20)
    sat = pd.read_excel(SAT_FILE, sheet_name="자산추이")
    sat = pd.Series(sat[SAT_COL].values, index=sat["거래일"].astype(str)).reindex(d1).ffill()
    for core in ("SPY", "QQQ"):
        c_eq, _ = run_book(d1, eo, ec, {d1[0]: pd.Series({core: 1.0})})
        cr, sr = c_eq.pct_change().fillna(0), sat.pct_change().fillna(0)
        vc, vs, out, fees, yr = 8000.0, 2000.0, {}, 0.0, d1[0][:4]
        for d in d1:
            if d[:4] != yr:                                 # 매년 첫 거래일 80/20 로 되돌림
                tot = vc + vs
                move = abs(vc - 0.8 * tot)
                fees += move * COST * 2
                vc, vs, yr = 0.8 * (tot - move * COST * 2), 0.2 * (tot - move * COST * 2), d[:4]
            vc *= 1 + cr.loc[d]
            vs *= 1 + sr.loc[d]
            out[d] = vc + vs
        k = f"C4{'a' if core == 'SPY' else 'b'} {core}80+스크리너20"
        res[k] = pd.Series(out)
        extra[k] = {"누적수수료$(배분만)": round(fees, 0)}

    rows, years = [], {}
    for k, eq in res.items():
        st, ys = stats(eq, d1)
        rows.append({"전략": k, **st, **extra.get(k, {})})
        years[k] = ys
    tab = pd.DataFrame(rows)
    spy = tab.loc[tab["전략"] == "기준 SPY 보유", "총수익률%"].iloc[0]
    tab["SPY 대비 총수익%p"] = (tab["총수익률%"] - spy).round(1)
    ydf = pd.DataFrame(years).T
    ydf.columns = [str(c) for c in ydf.columns]
    spy_y = ydf.loc["기준 SPY 보유"]
    wins = ((ydf.sub(spy_y, axis=1)) > 0).sum(axis=1)
    tab["SPY 이긴 해"] = tab["전략"].map(lambda k: f"{int(wins[k])}/{ydf.shape[1]}")
    pd.set_option("display.width", 250)
    print(tab.to_string(index=False))
    print(ydf.to_string())
    out = f"/data/frame/research_{a.period}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        tab.to_excel(xw, sheet_name="요약", index=False)
        ydf.to_excel(xw, sheet_name="연도별")
        pd.DataFrame(res).to_excel(xw, sheet_name="자산추이")
        pd.DataFrame({"항목": ["기간", "비용", "C1", "C2", "C3", "C4", "한계"],
                      "값": [f"{d1[0]} ~ {d1[-1]}", "매매금액 × 0.15% (수수료 0.1% + 슬리피지 0.05%)",
                            "매월 말 그때 S&P 500 중 12-1개월 수익률 상위 50, 동일비중, 다음 날 시가",
                            "C1 상위 100 중 TTM 영업이익 흑자, 영업이익률 순 상위 50 (재무 45일 지연)",
                            "전날 종가 SPY>200일선이면 SPY, 아니면 BIL, 시가 전환",
                            "SPY 또는 QQQ 80% + 스크리너(상위10·순위가중·3주) 20%, 매년 초 80/20",
                            "종목 가격은 지금 DB 에 남은 종목뿐(상장폐지 없음) → C1·C2·스크리너 유리. 부채 데이터 없음"]}
                     ).to_excel(xw, sheet_name="규칙", index=False)
    print("저장:", out)


if __name__ == "__main__":
    main()
