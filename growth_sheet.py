#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
growth_sheet.py — 주도주 워치리스트에 '꾸준한 성장 / 순위 상승' 참고 지표를 붙인다 (2026-09-29).

leader_screener.py 가 만든 leader_watchlist_latest.xlsx 를 읽어
  · 주도주 / 게이트진단 시트에 열 추가: 거래대금3년연속↑, 시총3년연속↑, 3년거래대금증가율%, 3년시총증가율%,
    시총순위, 1년전순위, 2년전순위, 순위상승배수
  · 새 시트 'Top20진입전후보': 시총순위 21~100 · 2년 연속 순위 개선 · 2년 전 순위÷지금 ≥ 1.5 (상승 배수 순)
참고용이다. 2016~2022 백테스트에서 두 조건 모두 SPY 를 크게 밑돌았다(설계 구간, research 참고).
주간 크론에서 leader_screener 다음에 실행한다.
"""
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, "/data/frame")

import pandas as pd            # noqa: E402

import growth_factors as gf    # noqa: E402

WL = "/data/frame/leader_watchlist_latest.xlsx"
ADD = {"amt_steady": "거래대금3년연속↑", "mc_steady": "시총3년연속↑", "amt_cagr3": "3년거래대금증가율%",
       "mc_cagr3": "3년시총증가율%", "rank0": "시총순위(DB)", "rank1": "1년전순위", "rank2": "2년전순위",
       "rank_ratio2": "순위상승배수"}


def growth_cols(f):
    g = f[list(ADD)].copy()
    g["amt_steady"] = g["amt_steady"].map({True: "O", False: "X"})
    g["mc_steady"] = g["mc_steady"].map({True: "O", False: "X"})
    g["amt_cagr3"] = (g["amt_cagr3"] * 100).round(1)
    g["mc_cagr3"] = (g["mc_cagr3"] * 100).round(1)
    g["rank_ratio2"] = g["rank_ratio2"].round(2)
    return g.rename(columns=ADD)


def main():
    xl = pd.ExcelFile(WL)
    sheets = {s: xl.parse(s) for s in xl.sheet_names}
    desc = sheets.get("설명")
    asof = str(desc.loc[desc["항목"] == "기준일", "값"].iloc[0])[:10]
    start = (pd.Timestamp(asof) - pd.DateOffset(years=4, months=2)).strftime("%Y-%m-%d")
    P = gf.load_panels(start=start, refresh=True, persist=False)   # 매주 새로 받는다 (최근 4년치, 파일로 안 남김)
    f = gf.features(asof, P)
    g = growth_cols(f)
    for s in ("주도주", "게이트진단"):
        if s in sheets and "티커" in sheets[s].columns:
            base = sheets[s].drop(columns=[c for c in ADD.values() if c in sheets[s].columns])
            sheets[s] = base.merge(g, left_on="티커", right_index=True, how="left")
    gd = sheets.get("게이트진단")
    r = f[f["riser"]].sort_values("rank_ratio2", ascending=False)
    riser = growth_cols(r).reset_index().rename(columns={"CODE": "티커"})
    if gd is not None and "티커" in gd.columns:
        info = [c for c in ("종목명", "섹터", "시총(십억$)", "상대강도(0~100)", "52주고점대비%", "탈락사유") if c in gd.columns]
        riser = riser.merge(gd[["티커"] + info], on="티커", how="left")
    riser.insert(0, "순위", range(1, len(riser) + 1))
    sheets["Top20진입전후보"] = riser
    if desc is not None:
        desc = pd.concat([desc, pd.DataFrame([
            ("성장·순위 지표(참고)", "거래대금3년연속↑ = 연 평균 일 거래대금이 3년 연속 증가, 시총3년연속↑ = 시총 3년 연속 증가, "
                               "순위상승배수 = 2년 전 시총순위 ÷ 지금 순위. 과거 시총 = 종가 × 지금 주식 수 (근사)"),
            ("Top20진입전후보", "시총순위 21~100 AND 2년 연속 순위 개선 AND 순위상승배수 ≥ 1.5 — 참고용. "
                             "2016~2022 백테스트에서 이 조건(최대 10종목)은 연 0.6%, SPY 11.6%"),
        ], columns=["항목", "값"])], ignore_index=True)
        sheets["설명"] = desc
    order = [s for s in xl.sheet_names if s != "설명"] + ["Top20진입전후보", "설명"]
    order = list(dict.fromkeys(order))
    with pd.ExcelWriter(WL, engine="openpyxl") as xw:
        for s in order:
            sheets[s].to_excel(xw, sheet_name=s, index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                w = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(50, max(8, w + 2))
    print(f"기준일 {asof} | Top20진입전후보 {len(riser)}종: {riser['티커'].tolist()[:15]}")
    print(f"주도주 시트 3년연속(거래대금·시총 둘 다) {int(((sheets['주도주']['거래대금3년연속↑'] == 'O') & (sheets['주도주']['시총3년연속↑'] == 'O')).sum())}"
          f"/{len(sheets['주도주'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
