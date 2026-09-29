#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
candidates.py — 자동매수 후보 목록: 항상 상위 N종목 (2026-09-29 사용자 결정). replay 와 live 가 같이 쓴다.

규칙
  크기 조건 : 시가총액 상위 mc_top(97%) AND 20일 평균 거래대금 상위 amt_top(66%) — 유니버스 안 백분위.
              금액 기준(시총 100억 달러·거래대금 3억 달러)은 2016년 S&P 500 에서 거래대금 통과가 16% 뿐이라
              1~5종목만 남는 시기가 있었다. 비율은 2026-09-25 S&P 500 에서 금액 기준 통과 비율로 맞췄다.
  모멘텀 조건 : 상대강도 ≥ rs_min(80) AND 52주 고점 대비 ≥ high_min(85%)
  품질 조건 : 영업적자/재무없음 제외, 단발 스파이크 제외 — 채울 때도 적용
  순서 : 세 조건 모두 통과한 종목을 주도주점수 순으로 → 모자라면 크기·품질은 통과했지만
         모멘텀에서 떨어진 종목을 주도주점수 순으로 채워 n 개를 맞춘다.
입력 uni: replay.screen_asof()['universe'] (CODE, NAME, 섹터, 유형, 주의, MC0_B, amt20_m, rs_pct,
          near_high, lead_score, 탈락사유 …). 출력은 주도주 시트와 같은 한글 열 + 순위/우선점수/구분.
"""
import pandas as pd

DEFAULTS = {"mode": "leaders", "n": 10, "mc_top": 0.97, "amt_top": 0.66, "rs_min": 80, "high_min": 85}
QUALITY_FAIL = "단발스파이크|영업적자/무재무"
OUT_COLS = ["순위", "티커", "종목명", "섹터", "유형", "주의", "구분", "주도주점수", "우선점수",
            "시총(십억$)", "일거래대금(백만$)", "상대강도(0~100)", "52주고점대비%"]


def params(cfg):
    p = dict(DEFAULTS)
    p.update((cfg or {}).get("candidates") or {})
    return p


def top_fill(uni, n=10, mc_top=0.97, amt_top=0.66, rs_min=80, high_min=85, **_):
    if uni is None or len(uni) == 0:
        return pd.DataFrame(columns=OUT_COLS)
    d = uni.copy()
    num = lambda c: pd.to_numeric(d[c], errors="coerce") if c in d.columns else pd.Series(float("nan"), index=d.index)
    mc, am = num("MC0_B"), num("amt20_m")
    rs, hi, sc = num("rs_pct"), num("near_high"), num("lead_score")
    why = d["탈락사유"].astype(str) if "탈락사유" in d.columns else pd.Series("", index=d.index)
    quality = ~why.str.contains(QUALITY_FAIL, regex=True)
    size_ok = (mc.rank(pct=True) >= 1 - mc_top) & (am.rank(pct=True) >= 1 - amt_top)
    mom_ok = (rs >= rs_min) & (hi >= high_min)
    base = size_ok & quality & sc.notna()
    d["_sc"] = sc
    passed = d[base & mom_ok].sort_values("_sc", ascending=False).assign(구분="통과")
    fill = d[base & ~mom_ok].sort_values("_sc", ascending=False).assign(구분="채움")
    out = pd.concat([passed, fill]).head(int(n)).reset_index(drop=True)
    out["순위"] = range(1, len(out) + 1)
    out["우선점수"] = [len(out) - i for i in range(len(out))]     # select_candidates 는 내림차순 → 순위 순서 유지
    out = out.rename(columns={"CODE": "티커", "NAME": "종목명", "_sc": "주도주점수", "MC0_B": "시총(십억$)",
                              "amt20_m": "일거래대금(백만$)", "rs_pct": "상대강도(0~100)", "near_high": "52주고점대비%"})
    for c in OUT_COLS:
        if c not in out.columns:
            out[c] = None
    return out[OUT_COLS]


def watchlist_for(scr, cfg):
    """replay: 스크리닝 결과 → 매매에 쓸 목록. candidates.mode=top_fill 이면 상위 N 채움 목록, 아니면 주도주 시트."""
    p = params(cfg)
    if p["mode"] == "top_fill":
        return top_fill(scr.get("universe"), **p)
    return scr.get("watchlist")
