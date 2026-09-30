#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
research_tilt30.py — 시총 상위 30 안에서 '최근 많이 오른 · 거래대금 늘어난 · 영업이익 늘어난' 종목의 비중을 올리는 규칙
(2026-09-30, 사용자 제안 "상위 30개 중에서 최근에 많이 오르고 거래대금이랑 영업이익 많이 오른 애들 비중을 올리는 건?").

  T0 : 시총 상위 30 그대로 시총 가중 (대조군)
  F1 : T0 × 12-1 모멘텀 3분위 (상위 1/3 ×1.5, 하위 1/3 ×0.5)
  F2 : T0 × 거래대금 증가율(최근 6개월 ÷ 그 전 6개월) 3분위
  F3 : T0 × TTM 영업이익 전년 대비 증가율 3분위 (재무 45일 지연, 전년 TTM <= 0 이면 중간값)
  F4 : T0 × 세 점수 평균 3분위                      <- 사용자 제안(주 후보)
  F5 : F4 를 세게: 상위 1/3 ×2, 하위 1/3 ×0 (빼 버림)
  F6 : F4 + 섹터 상한 40% (초과분은 나머지 섹터에 비례 배분; 섹터는 지금 GICS 기준, 시점별 아님)
  공통: 분기 1회(3·6·9·12월 말 신호 -> 다음 날 시가), 목표와 ±20% 이내면 매매 안 함, 비용 0.15%. 결측 점수는 중간(0.5).
판정(사전 고정): 설계 구간(2016~2022)에서 T0 와 SPY 를 둘 다 이겨야 후보.
              봉인 구간(2023~)은 2026-09-29 에 이미 열렸으므로 이 계열에는 더 이상 "안 보고 맞힌" 검증이 아니다 — 참고값.
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
import research_tilt as T     # noqa: E402

N = 30
SECTOR_CAP = 0.40
NAMES = {"T0": "T0 시총상위30 그대로(대조군)", "F1": "F1 ×모멘텀 3분위", "F2": "F2 ×거래대금증가 3분위",
         "F3": "F3 ×영업이익증가 3분위", "F4": "F4 ×셋평균 3분위(제안)", "F5": "F5 셋평균 상위×2·하위 제외",
         "F6": "F6 F4+섹터상한40%"}


def sector_map():
    import factor_analysis as fa
    c = fa.get_conn()
    cur = c.cursor()
    cur.execute("SELECT code, sector FROM ticker_master_us")
    m = {r[0]: (r[1] or "기타") for r in cur.fetchall()}
    c.close()
    return m


def top_cols(Tt, cl, MC):
    mem = replay.index_members(Tt, "sp500_pit")
    row = gf._at(MC, Tt)
    cols = [c for c in mem if c in row.index and pd.notna(row[c]) and row[c] > 0 and c in cl.columns and pd.notna(cl.at[Tt, c])]
    mc = row[cols].astype(float).nlargest(N)
    return mc / mc.sum()


def signals(Tt, cols, cl, P, fin):
    mom = R.mom_scores(cl, Tt, cols).reindex(cols)
    amt = gf.features(Tt, P)["amt_g6"].reindex(cols)
    _, op_now = R.ttm_margin(fin, Tt)
    _, op_prev = R.ttm_margin(fin, pd.Timestamp(Tt) - pd.Timedelta(days=365))
    opg = (op_now / op_prev - 1).where(op_prev > 0).reindex(cols)

    def pct(s):
        return s.rank(pct=True).reindex(cols).fillna(0.5)

    sc = {"mom": pct(mom), "amt": pct(amt), "opi": pct(opg)}
    sc["all"] = (sc["mom"] + sc["amt"] + sc["opi"]) / 3
    return sc


def tilt(base, score, hi=1.5, lo=0.5):
    mult = pd.Series(1.0, index=base.index)
    mult[score >= 2 / 3] = hi
    mult[score < 1 / 3] = lo
    w = base * mult
    return w / w.sum()


def sector_capped(w, sec, cap=SECTOR_CAP):
    w = w.copy()
    for _ in range(6):
        s = w.groupby(w.index.map(lambda c: sec.get(c, "기타"))).sum()
        over = s[s > cap + 1e-9]
        if over.empty:
            break
        excess = 0.0
        for k, v in over.items():
            idx = [c for c in w.index if sec.get(c, "기타") == k]
            w[idx] *= cap / v
            excess += v - cap
        free = [c for c in w.index if sec.get(c, "기타") not in over.index]
        if not free:
            break
        w[free] += excess * w[free] / w[free].sum()
    return w / w.sum()


def all_weights(Tt, cl, MC, P, fin, sec):
    base = top_cols(Tt, cl, MC)
    sc = signals(Tt, list(base.index), cl, P, fin)
    w = {"T0": base, "F1": tilt(base, sc["mom"]), "F2": tilt(base, sc["amt"]), "F3": tilt(base, sc["opi"]),
         "F4": tilt(base, sc["all"]), "F5": tilt(base, sc["all"], 2.0, 0.0)}
    w["F5"] = w["F5"][w["F5"] > 0]
    w["F6"] = sector_capped(w["F4"], sec)
    return w


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
    P = gf.load_panels()
    MC = P["MC"]
    fin = R.fin_table()
    sec = sector_map()
    days = [d for d in cl.index if start <= d <= end and d in etf["CLOSE"].index]
    sig = {Tt: D for Tt, D in T.quarter_signals([d for d in cl.index if d <= end]).items() if days[0] <= D <= days[-1]}
    d1 = [d for d in days if d >= min(sig.values())]
    print(f"기간 {a.period}: {d1[0]} ~ {d1[-1]} | 분기 리밸런스 {len(sig)}회")
    W = {D: all_weights(Tt, cl, MC, P, fin, sec) for Tt, D in sig.items()}
    res, extra, secw = {}, {}, {}
    for mode, label in NAMES.items():
        wd = {D: w[mode] for D, w in W.items()}
        eq, b = T.run_band(d1, op, cl, wd)
        res[label] = eq
        extra[label] = {"누적수수료$": round(b.fees, 0),
                        "연회전율%": round(b.turn / eq.mean() / (len(d1) / 252) * 100, 0),
                        "평균종목수": round(np.mean([len(w) for w in wd.values()]), 0)}
        s = pd.concat([w.groupby(w.index.map(lambda c: sec.get(c, "기타"))).sum() for w in wd.values()], axis=1).fillna(0)
        secw[label] = (s.mean(axis=1) * 100).round(1)
    eo, ec = etf["OPEN"], etf["CLOSE"]
    for s_ in ("SPY", "QQQ"):
        eq, b = R.run_book(d1, eo, ec, {d1[0]: pd.Series({s_: 1.0})})
        res[f"기준 {s_} 보유"] = eq
        extra[f"기준 {s_} 보유"] = {"누적수수료$": round(b.fees, 0)}
    rows, years = [], {}
    for k, eq in res.items():
        st, ys = R.stats(eq, d1)
        rows.append({"전략": k, **st, **extra.get(k, {})})
        years[k] = ys
    tab = pd.DataFrame(rows)
    Y = pd.DataFrame(years).T
    Y.columns = [str(c) for c in Y.columns]
    for ref, col in (("기준 SPY 보유", "SPY 이긴 해"), (NAMES["T0"], "T0 이긴 해")):
        wins = (Y.sub(Y.loc[ref], axis=1) > 0).sum(axis=1)
        tab[col] = tab["전략"].map(lambda k: f"{int(wins[k])}/{Y.shape[1]}")
    base = tab.set_index("전략")["연평균%"]
    tab["T0 대비 연%p"] = tab["전략"].map(lambda k: round(base[k] - base[NAMES["T0"]], 2))
    tab["SPY 대비 연%p"] = tab["전략"].map(lambda k: round(base[k] - base["기준 SPY 보유"], 2))
    S = pd.DataFrame(secw).fillna(0)
    S = S.loc[S.max(axis=1).sort_values(ascending=False).index]
    pd.set_option("display.width", 250)
    print(tab.to_string(index=False))
    print(Y.to_string())
    print("평균 섹터 비중 %:")
    print(S.to_string())
    out = f"/data/frame/research_tilt30_{a.period}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        tab.to_excel(xw, sheet_name="요약", index=False)
        Y.to_excel(xw, sheet_name="연도별")
        S.to_excel(xw, sheet_name="평균섹터비중%")
        pd.DataFrame(res).to_excel(xw, sheet_name="자산추이")
        pd.DataFrame({"항목": list(NAMES.values()) + ["공통", "판정", "한계"],
                      "값": ["그 시점 S&P 500 시총 상위 30 을 시총 가중 (대조군)",
                            "T0 × 12-1 모멘텀 3분위 (상위 1/3 ×1.5, 하위 1/3 ×0.5)",
                            "T0 × 거래대금 증가율(최근 6개월 ÷ 그 전 6개월) 3분위",
                            "T0 × TTM 영업이익 전년 대비 증가율 3분위 (45일 지연)",
                            "T0 × 세 점수 평균 3분위 (사용자 제안)",
                            "세 점수 평균 상위 1/3 ×2, 하위 1/3 은 뺌",
                            f"F4 에 섹터 상한 {int(SECTOR_CAP * 100)}% (지금 GICS 섹터, 시점별 아님)",
                            "분기 1회, ±20% 밴드, 비용 0.15%, 결측 점수는 중간",
                            "설계 구간에서 T0 와 SPY 를 둘 다 이겨야 후보. 봉인 구간은 이미 열려 참고값",
                            "세금 미반영. 상장폐지 종목 없음. 시총 = 종가 × 지금 주식 수(근사)"]}
                     ).to_excel(xw, sheet_name="규칙", index=False)
    print("저장:", out)


if __name__ == "__main__":
    main()
