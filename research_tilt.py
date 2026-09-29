#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
research_tilt.py — '지수를 이기기 위한' 비중·매매 규칙 사전등록 후보 (2026-09-29).

지수에 진 이유(초대형주 비중 부족 · 회전율 · 집중 · 약한 신호)를 고치는 방향으로:
  E0 대조군 : 그 시점 S&P 500 구성종목 시가총액 가중 (우리 데이터로 만든 지수 복제 — 생존편향 동일)
  E1        : E0 비중 × 12-1 모멘텀 3분위 (상위 1/3 ×1.5, 하위 1/3 ×0.5, 모멘텀 없음 ×1.0)
  E2        : E1 과 같되 점수 = 모멘텀 백분위 50% + TTM 영업이익률 백분위 50% (적자·재무없음은 이익률 0)
  E3        : 시총 상위 50 중 모멘텀 하위 1/4 제외, 나머지 시가총액 가중
  공통: 분기 1회 (3·6·9·12월 마지막 거래일 신호 → 다음 날 시가), 목표와의 차이가 목표의 20% 이내면 매매 안 함
        (편입·편출은 항상 매매). 비용 = 거래금액 × 0.15%.
판정(사전 고정): 설계 구간(2016~2022)에서 E0 와 SPY 를 둘 다 이긴 후보만 봉인 구간으로 넘긴다.
한계: 시가총액 = 종가 × 지금 주식 수 (근사). 상장폐지 종목 없음 — E0 와 비교하면 편향이 상쇄된다.
"""
import argparse
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, "/data/frame")

import numpy as np            # noqa: E402
import pandas as pd           # noqa: E402

import growth_factors as gf   # noqa: E402
import replay                 # noqa: E402
import research_bt as R       # noqa: E402

BAND = 0.20


class BandBook(R.Book):
    """목표와의 차이가 목표의 BAND 이내인 보유 종목은 그대로 둔다 (매매 줄이기)."""

    def rebalance(self, w, px_open):
        idx = w.index.union(self.sh.index)
        p = self._px(px_open, idx)
        w = w[p.reindex(w.index).notna()]
        w = w / w.sum() if len(w) and w.sum() > 0 else w
        val = self.cash + float((self.sh * p.reindex(self.sh.index).fillna(0)).sum())
        cur = self.sh.reindex(idx).fillna(0)
        tgt = (w * val / p.reindex(w.index)).reindex(idx).fillna(0)
        pv = p.fillna(0)
        keep = (tgt > 0) & (cur > 0) & ((cur - tgt).abs() * pv <= BAND * tgt * pv)
        new = tgt.where(~keep, cur)
        fee = float(((new - cur).abs() * pv).sum()) * R.COST
        kept_val = float((new[keep] * pv[keep]).sum())
        trade_val = float((new[~keep] * pv[~keep]).sum())
        room = val - kept_val - fee
        if trade_val > 0 and trade_val > room:                 # 현금이 모자라면 새로 사는 쪽을 줄인다
            new[~keep] = new[~keep] * max(room, 0) / trade_val
        trade = float(((new - cur).abs() * pv).sum())
        fee = trade * R.COST
        self.cash = val - float((new * pv).sum()) - fee
        self.sh = new[new > 1e-12]
        self.fees += fee
        self.turn += trade


def run_band(days, open_, close, weights_by_day, cash=10000.0):
    b = BandBook(cash)
    out = {}
    for d in days:
        if d in weights_by_day:
            b.rebalance(weights_by_day[d], open_.loc[d])
        out[d] = b.mark(close.loc[d])
    return pd.Series(out), b


def quarter_signals(days):
    s = pd.Series(days, index=pd.to_datetime(days))
    q = s[s.index.month.isin([3, 6, 9, 12])]
    last = q.groupby([q.index.year, q.index.month]).last().tolist()
    return {T: days[days.index(T) + 1] for T in last if days.index(T) + 1 < len(days)}


def weights(mode, T, cl, MC, fin):
    mem = replay.index_members(T, "sp500_pit")
    mcrow = gf._at(MC, T)
    cols = [c for c in mem if c in mcrow.index and pd.notna(mcrow[c]) and mcrow[c] > 0
            and c in cl.columns and pd.notna(cl.at[T, c])]
    mc = mcrow[cols].astype(float)
    base = mc / mc.sum()
    mom = R.mom_scores(cl, T, cols).reindex(cols)
    if mode == "E0":
        return base
    if mode in ("E1", "E2"):
        mp = mom.rank(pct=True)
        if mode == "E1":
            score = mp
        else:
            mg, opi = R.ttm_margin(fin, T)
            gp = mg.reindex(cols).where(opi.reindex(cols) > 0).rank(pct=True).fillna(0)
            score = 0.5 * mp.fillna(0.5) + 0.5 * gp
        mult = pd.Series(1.0, index=cols)
        mult[score >= 2 / 3] = 1.5
        mult[score < 1 / 3] = 0.5
        w = base * mult
        return w / w.sum()
    if mode == "E3":
        top = mc.nlargest(50).index
        m = mom.reindex(top)
        drop = m[m.rank(pct=True) <= 0.25].index
        keep = top.difference(drop)
        return mc[keep] / mc[keep].sum()
    raise ValueError(mode)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=list(R.PERIODS), default="design")
    a = ap.parse_args()
    start, end = R.PERIODS[a.period]
    latest = replay.db_latest_date()
    end = end or latest
    panel, _ = replay.load_prices("2014-12-25", latest)
    op, cl = panel["OPEN"], panel["CLOSE"]
    etf = R.etf_prices()
    MC = gf.load_panels()["MC"]
    fin = R.fin_table()
    days = [d for d in cl.index if start <= d <= end and d in etf["CLOSE"].index]
    sig = {T: D for T, D in quarter_signals([d for d in cl.index if d <= end]).items() if days[0] <= D <= days[-1]}
    d1 = [d for d in days if d >= min(sig.values())]
    print(f"기간 {a.period}: {d1[0]} ~ {d1[-1]} | 분기 리밸런스 {len(sig)}회")
    res, extra = {}, {}
    names = {"E0": "E0 대조군 시총가중(우리 데이터)", "E1": "E1 시총가중×모멘텀 기울기",
             "E2": "E2 시총가중×모멘텀·이익률 기울기", "E3": "E3 시총상위50−모멘텀하위1/4"}
    for mode, label in names.items():
        W = {D: weights(mode, T, cl, MC, fin) for T, D in sig.items()}
        eq, b = run_band(d1, op, cl, W)
        res[label] = eq
        extra[label] = {"누적수수료$": round(b.fees, 0),
                        "연회전율%": round(b.turn / eq.mean() / (len(d1) / 252) * 100, 0),
                        "평균종목수": round(np.mean([len(w) for w in W.values()]), 0)}
    eo, ec = etf["OPEN"], etf["CLOSE"]
    for s in ("SPY", "QQQ"):
        eq, b = R.run_book(d1, eo, ec, {d1[0]: pd.Series({s: 1.0})})
        res[f"기준 {s} 보유"] = eq
        extra[f"기준 {s} 보유"] = {"누적수수료$": round(b.fees, 0)}
    rows, years = [], {}
    for k, eq in res.items():
        st, ys = R.stats(eq, d1)
        rows.append({"전략": k, **st, **extra.get(k, {})})
        years[k] = ys
    tab = pd.DataFrame(rows)
    Y = pd.DataFrame(years).T
    Y.columns = [str(c) for c in Y.columns]
    for ref, col in ((f"기준 SPY 보유", "SPY 이긴 해"), (names["E0"], "E0 이긴 해")):
        wins = (Y.sub(Y.loc[ref], axis=1) > 0).sum(axis=1)
        tab[col] = tab["전략"].map(lambda k: f"{int(wins[k])}/{Y.shape[1]}")
    base = tab.set_index("전략")["연평균%"]
    tab["E0 대비 연%p"] = tab["전략"].map(lambda k: round(base[k] - base[names["E0"]], 2))
    tab["SPY 대비 연%p"] = tab["전략"].map(lambda k: round(base[k] - base["기준 SPY 보유"], 2))
    pd.set_option("display.width", 250)
    print(tab.to_string(index=False))
    print(Y.to_string())
    out = f"/data/frame/research_tilt_{a.period}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        tab.to_excel(xw, sheet_name="요약", index=False)
        Y.to_excel(xw, sheet_name="연도별")
        pd.DataFrame(res).to_excel(xw, sheet_name="자산추이")
        pd.DataFrame({"항목": list(names.values()) + ["공통", "판정", "한계"],
                      "값": ["그 시점 S&P 500 시가총액 가중 (대조군)",
                            "시총 비중 × 12-1 모멘텀 상위 1/3 1.5배 / 하위 1/3 0.5배",
                            "시총 비중 × (모멘텀 50% + 영업이익률 50%) 점수 3분위 1.5 / 1.0 / 0.5배",
                            "시총 상위 50 중 모멘텀 하위 1/4 제외, 나머지 시총 가중",
                            "분기 1회, 목표와 20% 이내면 매매 안 함, 비용 0.15%",
                            "설계 구간에서 E0 와 SPY 를 둘 다 이긴 후보만 봉인 구간으로",
                            "시총 = 종가 × 지금 주식 수. 상장폐지 종목 없음 (E0 와 비교하면 상쇄)"]}
                     ).to_excel(xw, sheet_name="규칙", index=False)
    print("저장:", out)


if __name__ == "__main__":
    main()
