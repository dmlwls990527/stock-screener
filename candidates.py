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

DEFAULTS = {"mode": "leaders", "n": 10, "mc_top": 0.97, "amt_top": 0.66, "rs_min": 80, "high_min": 85,
            "amt_signal": "20d"}
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
    uni = rescore(scr.get("universe"), scr.get("asof"), p.get("amt_signal", "20d"))
    if p["mode"] == "top_fill":
        return top_fill(uni, **p)
    if p["mode"] == "growth_leader":
        return growth_leader(uni, scr.get("asof"), **p)
    if p["mode"] == "rank_riser":
        return rank_riser(uni, scr.get("asof"), **p)
    return scr.get("watchlist")


# ── 꾸준한 성장 / Top 20 진입 전 후보 (2026-09-29) ──────────────────────────
GROWTH_COLS = ["3년거래대금증가율%", "3년시총증가율%", "시총순위", "1년전순위", "2년전순위", "순위상승배수"]


def _with_growth(uni, asof):
    import growth_factors
    f = growth_factors.features(asof)
    keep = ["amt_steady", "mc_steady", "amt_cagr3", "mc_cagr3", "rank0", "rank1", "rank2", "rank_ratio2", "riser"]
    d = uni.merge(f[keep], left_on="CODE", right_index=True, how="left")
    for c in ("amt_steady", "mc_steady", "riser"):
        d[c] = d[c].fillna(False).astype(bool)
    return d


def _finish(out, n):
    out = out.head(int(n)).reset_index(drop=True)
    out["순위"] = range(1, len(out) + 1)
    out["우선점수"] = [len(out) - i for i in range(len(out))]
    out = out.rename(columns={"CODE": "티커", "NAME": "종목명", "_sc": "주도주점수", "MC0_B": "시총(십억$)",
                              "amt20_m": "일거래대금(백만$)", "rs_pct": "상대강도(0~100)", "near_high": "52주고점대비%",
                              "rank0": "시총순위", "rank1": "1년전순위", "rank2": "2년전순위", "rank_ratio2": "순위상승배수"})
    out["3년거래대금증가율%"] = (pd.to_numeric(out.get("amt_cagr3"), errors="coerce") * 100).round(1)
    out["3년시총증가율%"] = (pd.to_numeric(out.get("mc_cagr3"), errors="coerce") * 100).round(1)
    cols = OUT_COLS + GROWTH_COLS
    for c in cols:
        if c not in out.columns:
            out[c] = None
    return out[cols]


def _masks(d, mc_top, amt_top, rs_min, high_min):
    num = lambda c: pd.to_numeric(d[c], errors="coerce") if c in d.columns else pd.Series(float("nan"), index=d.index)
    mc, am, rs, hi, sc = num("MC0_B"), num("amt20_m"), num("rs_pct"), num("near_high"), num("lead_score")
    why = d["탈락사유"].astype(str) if "탈락사유" in d.columns else pd.Series("", index=d.index)
    quality = ~why.str.contains(QUALITY_FAIL, regex=True)
    size_ok = (mc.rank(pct=True) >= 1 - mc_top) & (am.rank(pct=True) >= 1 - amt_top)
    mom_ok = (rs >= rs_min) & (hi >= high_min)
    return size_ok & quality & sc.notna(), mom_ok, sc


def growth_leader(uni, asof, n=10, mc_top=0.97, amt_top=0.66, rs_min=80, high_min=85, **_):
    """주도주 조건 + 3년 연속 거래대금·시총 증가. 모자라면 (크기·품질·꾸준성장) 종목을 점수 순으로 채움."""
    if uni is None or len(uni) == 0:
        return pd.DataFrame(columns=OUT_COLS + GROWTH_COLS)
    d = _with_growth(uni, asof)
    base, mom_ok, sc = _masks(d, mc_top, amt_top, rs_min, high_min)
    base = base & d["amt_steady"] & d["mc_steady"]
    d["_sc"] = sc
    passed = d[base & mom_ok].sort_values("_sc", ascending=False).assign(구분="통과")
    fill = d[base & ~mom_ok].sort_values("_sc", ascending=False).assign(구분="채움")
    return _finish(pd.concat([passed, fill]), n)


def rank_riser(uni, asof, n=10, mc_top=0.97, amt_top=0.66, **_):
    """Top 20 진입 전 후보: 시총순위 21~100, 2년 연속 순위 개선, 2년 전 순위÷지금 ≥ 1.5. 상승 배수 순. 채우지 않음."""
    if uni is None or len(uni) == 0:
        return pd.DataFrame(columns=OUT_COLS + GROWTH_COLS)
    d = _with_growth(uni, asof)
    base, _mom, sc = _masks(d, mc_top, amt_top, 80, 85)
    d["_sc"] = sc
    pick = d[base & d["riser"]].sort_values("rank_ratio2", ascending=False).assign(구분="순위상승")
    return _finish(pick, n)


# ── 주도주점수의 거래대금 항목 창 바꾸기 (2026-09-29) ─────────────────────────
# leader_screener: lead_score = 백분위배합(rs_pct 0.4, fund_z 0.3, amt_grow 0.3) + 섹터 1·2위 가점 0.10
#   amt_grow = 최근 20일 평균 거래대금 ÷ 그 전 60일 평균 − 1  (너무 짧다는 의견 → 6개월 / 1년 / 제거 비교)
AMT_SIGNALS = {"20d": None, "6m": "amt_g6", "12m": "amt_g12", "none": None}


def _blend(d, pairs):
    acc = wsum = None
    for c, w in pairs:
        pc = pd.to_numeric(d[c], errors="coerce").rank(pct=True) * 100
        m = pc.notna()
        acc = pc.fillna(0) * w if acc is None else acc + pc.fillna(0) * w
        wsum = m * w if wsum is None else wsum + m * w
    return acc / wsum.replace(0, float("nan"))


def rescore(uni, asof, amt_signal):
    """amt_signal 에 맞춰 lead_score 를 다시 계산한 사본. 섹터 가점은 원래 점수에서 역산해 그대로 둔다."""
    if amt_signal in (None, "20d") or uni is None or len(uni) == 0:
        return uni
    if amt_signal not in AMT_SIGNALS:
        raise ValueError(f"candidates.amt_signal 은 {list(AMT_SIGNALS)} 중 하나: {amt_signal!r}")
    d = uni.copy()
    orig = _blend(d, [("rs_pct", 0.4), ("fund_z", 0.3), ("amt_grow", 0.3)]) / 100
    bonus = (pd.to_numeric(d["lead_score"], errors="coerce") - orig).round(1).clip(lower=0)   # 0 또는 0.1
    if amt_signal == "none":
        new = _blend(d, [("rs_pct", 0.4), ("fund_z", 0.3)]) / 100
    else:
        import growth_factors
        col = AMT_SIGNALS[amt_signal]
        f = growth_factors.features(asof)[[col]]
        d = d.merge(f, left_on="CODE", right_index=True, how="left")
        new = _blend(d, [("rs_pct", 0.4), ("fund_z", 0.3), (col, 0.3)]) / 100
    d["lead_score"] = (new + bonus.fillna(0)).round(3)
    return d
