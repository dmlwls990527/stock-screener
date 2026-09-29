#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_candidates.py — 자동매수 후보 엑셀 (live). 백테스트(replay)와 같은 정의로 만든다.

  유니버스 : 오늘의 S&P 500 구성종목 (data/sp500_ticker_start_end.csv, 주간 크론이 갱신)
  목록     : candidates.top_fill — 조건 통과 종목 점수순 → 모자라면 점수순으로 채워 항상 N(10)종목
  비중     : 순위가중 (1위 N … N위 1) — auto_buy 가 sizing.weighting=rank 로 계산, 여기엔 참고로만 적는다
출력: /data/frame/auto_buy_candidates_latest.xlsx  (시트 '자동매수후보', '설명')
      auto_buy_config.json 의 source.file / sheet 가 이 파일을 가리킨다.
실행: ./.venv/bin/python auto_candidates.py
"""
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, "/data/frame")

import pandas as pd                 # noqa: E402

import candidates                   # noqa: E402
import replay                       # noqa: E402
from auto_buy import load_config    # noqa: E402

OUT = "/data/frame/auto_buy_candidates_latest.xlsx"


def main():
    cfg, _ = load_config()
    p = candidates.params(cfg)
    p["mode"] = "top_fill"
    asof = replay.db_latest_date()
    members = replay.sp500_members(asof)
    scr = replay.screen_asof(asof, use_cache=False, universe="sp500_pit")
    out = candidates.top_fill(scr["universe"], **p)
    n = len(out)
    tot = n * (n + 1) / 2 if n else 1
    out.insert(out.columns.get_loc("구분") + 1, "목표비중%", [round((n - i) / tot * 100, 2) for i in range(n)])
    desc = pd.DataFrame([
        ("기준일", asof),
        ("유니버스", f"그날의 S&P 500 구성종목 {len(members)}종 중 가격·재무 있는 {scr['n_universe']}종"),
        ("목록", f"항상 {p['n']}종목: 조건 통과 종목을 주도주점수 순으로, 모자라면 크기·품질 통과 종목을 점수 순으로 채움"),
        ("크기 조건", f"시가총액 상위 {p['mc_top'] * 100:.0f}% AND 20일 평균 거래대금 상위 {p['amt_top'] * 100:.0f}% (유니버스 안 백분위)"),
        ("모멘텀 조건", f"상대강도 ≥ {p['rs_min']} AND 52주 고점 대비 ≥ {p['high_min']}%"),
        ("품질 조건", "영업적자/재무없음·단발 스파이크 제외 (채울 때도)"),
        ("비중", "순위가중: 1위 N, 2위 N−1 … N위 1"),
        ("통과 / 채움", f"{int((out['구분'] == '통과').sum())} / {int((out['구분'] == '채움').sum())}"),
    ], columns=["항목", "값"])
    with pd.ExcelWriter(OUT, engine="openpyxl") as xw:
        out.to_excel(xw, sheet_name="자동매수후보", index=False)
        desc.to_excel(xw, sheet_name="설명", index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                w = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(60, max(9, w + 2))
    print(f"기준일 {asof} | 유니버스 {scr['n_universe']} | 후보 {n}")
    print(out[["순위", "티커", "종목명", "구분", "목표비중%", "주도주점수"]].to_string(index=False))
    print(f"저장: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
