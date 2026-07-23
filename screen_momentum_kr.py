#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
국장(KRX) 패러다임 투자 스크리너 — screen_momentum_us.py 국장 버전
데이터: FinanceData/marcap GitHub (parquet) — 이미 시총 데이터 있음

──────────────────────────────────────────────────────────────────────────────
미국판과의 구조 차이

① 수익률 기준: 주가 대신 시총(Marcap) 증가율 사용
   - 국장은 주가 × 주식수 = 시총이 이미 marcap에 있음
   - 미국처럼 Close × SharesHeld 계산 불필요

② 유니버스: KOSPI + KOSDAQ 시총 상위 500종
   - 최소 시총 3,000억 이상 (극소형 제외)

③ 동일한 패러다임 신호
   - 시총 Top50 신규 진입 탐지
   - 순위 상승 Top50/100/200 구간별 Top10
   - 과열 신호 (급발진 + 수급과열 + 고변동)
   - 건강한 종목 시트

④ 점수 가중치 (미국과 동일)
   - 24M(×3.0) + 12M(×2.0) + 6M(×1.5) + 3M(×0.5)
   + 거래대금(×1.0) + 일관성(×1.5) + 변동성(×1.0) + 급발진패널티(×1.0)

실행: python3 screen_momentum_kr.py
데이터 경로: /data/frame/marcap/data/marcap-YYYY.parquet
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd

_PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_PKG_DIR))

from utils import load_marcap_data, marcap_to_choeok, load_fundamental_data, MARCAP_DIR, START_DATE, END_DATE

# ── 파라미터 ──────────────────────────────────────────────────────────────────
UNIVERSE_TOP_N         = 500     # 시총 상위 N종
UNIVERSE_MIN_MARCAP_억 = 3_000   # 최소 시총 3,000억 (극소형 제외)
AMOUNT_MIN_억          = 50      # 월평균 거래대금 최소 50억
OUTPUT_TOP             = 80
PARADIGM_TOP_N         = 50      # Top50 신규 진입 탐지 (국장은 50, 미국은 20)
SINGLE_MONTH_DROP_LIMIT = -0.4   # 단월 -40% 급락 → 제거
MIN_MONTHS_REQUIRED    = 7       # 최근 9개월 중 최소 7개월
CV_HEALTHY_MAX         = 0.5     # 건강종목 변동성 CV 상한 (고변동 직접 차단)

# ── 점수 가중치 (미국판과 동일) ───────────────────────────────────────────────
W_24M     = 3.0
W_12M     = 2.0
W_6M      = 1.5
W_3M      = 0.5
W_AMT     = 1.0
W_CONS    = 1.5
W_VOL     = 1.0
W_PENALTY = 1.0
W_TOTAL   = W_24M + W_12M + W_6M + W_3M + W_AMT + W_CONS + W_VOL + W_PENALTY


# ── 메인 ─────────────────────────────────────────────────────────────────────

def run_paradigm_kr(base_dir: Path | None = None) -> None:
    root = base_dir if base_dir is not None else _PKG_DIR

    print(f"marcap 데이터 로드: {START_DATE} ~ {END_DATE}")
    df = load_marcap_data(START_DATE, END_DATE)
    df["Date"] = pd.to_datetime(df["Date"])
    print(f"  로드 완료: {len(df):,}행, 최신: {df['Date'].max().date()}")

    # ── 월말 행 추출 ──────────────────────────────────────────────────
    ym = df["Date"].dt.to_period("M")
    last_per_m = df.groupby(ym, sort=True)["Date"].transform("max")
    df_m = df[df["Date"] == last_per_m].copy()
    df_m = df_m.sort_values(["Code", "Date"]).reset_index(drop=True)

    asof     = df_m["Date"].max()
    asof_str = str(asof.date())
    print(f"기준일(월말): {asof_str}")

    # ── 시총 기반 수익률 계산 (미국의 주가 대신 시총 사용) ──────────
    g = df_m.groupby("Code", group_keys=False)

    df_m["marcap_1m_ago"]  = g["Marcap"].shift(1)
    df_m["marcap_3m_ago"]  = g["Marcap"].shift(3)
    df_m["marcap_6m_ago"]  = g["Marcap"].shift(6)
    df_m["marcap_12m_ago"] = g["Marcap"].shift(12)
    df_m["marcap_24m_ago"] = g["Marcap"].shift(24)

    df_m["amt_roll2"]      = g["Amount"].transform(lambda s: s.rolling(2, min_periods=1).mean())
    df_m["amt_roll2_prev"] = g["amt_roll2"].shift(2)

    def _sr(a: pd.Series, b: pd.Series) -> pd.Series:
        return (a / b.replace(0, float("nan")) - 1).astype(float)

    df_m["ret_1m"]  = _sr(df_m["Marcap"], df_m["marcap_1m_ago"])
    df_m["ret_3m"]  = _sr(df_m["Marcap"], df_m["marcap_3m_ago"])
    df_m["ret_6m"]  = _sr(df_m["Marcap"], df_m["marcap_6m_ago"])
    df_m["ret_12m"] = _sr(df_m["Marcap"], df_m["marcap_12m_ago"])
    df_m["ret_24m"] = _sr(df_m["Marcap"], df_m["marcap_24m_ago"])
    df_m["amt_accel"]     = _sr(df_m["amt_roll2"], df_m["amt_roll2_prev"])
    df_m["single_m_jump"] = df_m["ret_1m"]

    # 시총 순위 변화 (패러다임 신호용)
    df_m["rank_3m_ago"] = g["Rank"].shift(3)
    df_m["rank_6m_ago"] = g["Rank"].shift(6)

    # ── 모멘텀 일관성 & 변동성 (최근 12개월) ────────────────────────
    recent12 = df_m[df_m["Date"] > (asof - pd.DateOffset(months=12))].copy()

    consistency = (
        recent12.groupby("Code")["ret_1m"]
        .apply(lambda s: float((s > 0).sum() / max(len(s), 1)))
        .rename("consistency").reset_index()
    )

    def _cv(s: pd.Series) -> float:
        m = s.mean()
        return float(s.std() / m) if m > 0 else float("nan")

    volatility = (
        recent12.groupby("Code")["Marcap"]
        .apply(_cv).rename("vol_cv").reset_index()
    )

    # ── 데이터 연속성 필터 ────────────────────────────────────────────
    recent9_count = (
        df_m[df_m["Date"] > (asof - pd.DateOffset(months=9))]
        .groupby("Code")["Date"].count().rename("months_count")
    )
    continuous_codes = recent9_count[recent9_count >= MIN_MONTHS_REQUIRED].index

    # ── 비정상 제거 (급락만, 급등 필터 없음) ────────────────────────
    recent6  = df_m[df_m["Date"] >= (asof - pd.DateOffset(months=6))]
    min_drop = recent6.groupby("Code")["single_m_jump"].min()
    normal_codes = min_drop[min_drop >= SINGLE_MONTH_DROP_LIMIT].index
    normal_codes = normal_codes.intersection(continuous_codes)
    print(f"비정상 필터: 급락 {int((min_drop < SINGLE_MONTH_DROP_LIMIT).sum())}개 제거")

    # ── 기준일 스냅샷 (ret 컬럼 포함) ───────────────────────────────
    # df_m에서 기준일 행 추출 — ret_6m/12m/24m 등 이미 계산된 값 포함
    cur_all = df_m[df_m["Date"] == asof].drop_duplicates("Code").copy()

    # 유니버스 구성: 시총 상위 N종 + 최소 시총
    min_marcap_백만 = UNIVERSE_MIN_MARCAP_억 * 100
    universe_codes = (
        cur_all[cur_all["Marcap"] >= min_marcap_백만]
        .nlargest(UNIVERSE_TOP_N, "Marcap")["Code"]
    )

    # ── 펀더멘털 (PER/PBR/EPS/DIV) ───────────────────────────────
    print("펀더멘털 데이터 로드 중...")
    _fund_df = pd.DataFrame()
    _has_fund = False
    try:
        _fund_df = load_fundamental_data(
            date_str=asof.strftime("%Y%m%d"),
            codes=universe_codes.tolist(),
        )
        _has_fund = not _fund_df.empty
        if _has_fund:
            per_valid = (_fund_df["PER"].fillna(0) > 0).sum()
            print(f"  펀더멘털 완료: {len(_fund_df)}개 (PER 유효: {per_valid}개)")
    except Exception as e:
        print(f"  [경고] 펀더멘털 로드 실패: {e}")

    cur = cur_all[
        cur_all["Code"].isin(normal_codes) &
        cur_all["Code"].isin(universe_codes)
    ].copy()
    cur = cur.merge(consistency, on="Code", how="left")
    cur = cur.merge(volatility,  on="Code", how="left")
    if _has_fund:
        fund_cols = [c for c in ("Code", "PER", "PBR", "EPS", "BPS", "DIV") if c in _fund_df.columns]
        cur = cur.merge(_fund_df[fund_cols].drop_duplicates("Code"), on="Code", how="left")
    else:
        for col in ("PER", "PBR", "EPS", "BPS", "DIV"):
            cur[col] = float("nan")
    cur = cur[cur["marcap_6m_ago"].notna() & (cur["marcap_6m_ago"] > 0)].copy()

    # 거래대금 하한
    amount_min_백만 = AMOUNT_MIN_억 * 100
    before_amt = len(cur)
    cur = cur[cur["amt_roll2"] >= amount_min_백만]
    print(f"거래대금 하한 필터: {before_amt - len(cur)}개 제거 (월평균 <{AMOUNT_MIN_억}억)")

    # ── 패러다임 신호 — 시총 Top50 신규 진입 ─────────────────────────
    prev_dates = df_m[df_m["Date"] < asof]["Date"].drop_duplicates().sort_values()
    prev1_snap = pd.DataFrame()
    prev3_snap = pd.DataFrame()
    prev6_snap = pd.DataFrame()

    if len(prev_dates) >= 1:
        prev1_snap = df_m[df_m["Date"] == prev_dates.iloc[-1]].drop_duplicates("Code")
    if len(prev_dates) >= 3:
        prev3_snap = df_m[df_m["Date"] == prev_dates.iloc[-3]].drop_duplicates("Code")
    if len(prev_dates) >= 6:
        prev6_snap = df_m[df_m["Date"] == prev_dates.iloc[-6]].drop_duplicates("Code")

    paradigm_df = pd.DataFrame()
    if not prev1_snap.empty:
        cur_top  = set(cur_all[cur_all["Rank"] <= PARADIGM_TOP_N]["Code"])
        prev_top = set(prev1_snap[prev1_snap["Rank"] <= PARADIGM_TOP_N]["Code"])
        new_codes = cur_top - prev_top

        if new_codes:
            rows = cur_all[cur_all["Code"].isin(new_codes)].drop_duplicates("Code").copy()
            # ret 컬럼이 cur_all에 이미 있음 (df_m 기준일 스냅샷)
            # 없는 경우를 대비해 fillna 처리
            for col in ("ret_6m", "ret_12m", "ret_24m"):
                if col not in rows.columns:
                    rows[col] = float("nan")
            rows = rows.merge(consistency, on="Code", how="left")
            rows["시총_조원"]      = marcap_to_choeok(rows["Marcap"]).round(2)
            rows["수익률_6M(%)"]   = (rows["ret_6m"]  * 100).round(1)
            rows["수익률_12M(%)"]  = (rows["ret_12m"] * 100).round(1)
            rows["수익률_24M(%)"]  = (rows["ret_24m"] * 100).round(1)
            rows["일관성"]         = rows["consistency"].round(2)
            paradigm_df = rows[[
                "Code","Name","Rank","시총_조원",
                "수익률_6M(%)","수익률_12M(%)","수익률_24M(%)","일관성"
            ]].rename(columns={"Code":"종목코드","Name":"종목명","Rank":"현재순위"}).sort_values("현재순위")

    if not paradigm_df.empty:
        print(f"\n[패러다임 신호] 시총 Top{PARADIGM_TOP_N} 신규 진입 {len(paradigm_df)}개:")
        print(paradigm_df.to_string(index=False))
    else:
        print(f"\n[패러다임 신호] 시총 Top{PARADIGM_TOP_N} 신규 진입 없음")

    # ── 순위 상승 신호 — 구간별 Top10 ────────────────────────────────
    rank_top50_df  = pd.DataFrame()
    rank_top100_df = pd.DataFrame()
    rank_top200_df = pd.DataFrame()

    if not prev3_snap.empty:
        cur_rk   = cur_all[["Code","Rank"]].drop_duplicates("Code")
        prev3_rk = prev3_snap[["Code","Rank"]].drop_duplicates("Code").rename(columns={"Rank":"Rank_3m"})
        prev6_rk = prev6_snap[["Code","Rank"]].drop_duplicates("Code").rename(columns={"Rank":"Rank_6m"}) if not prev6_snap.empty else pd.DataFrame()

        rank_df = cur_rk.merge(prev3_rk, on="Code", how="inner")
        if not prev6_rk.empty:
            rank_df = rank_df.merge(prev6_rk, on="Code", how="left")
        else:
            rank_df["Rank_6m"] = float("nan")

        rank_df["rank_up_3m"]      = rank_df["Rank_3m"] - rank_df["Rank"]
        rank_df["rank_up_3m_prev"] = rank_df["Rank_6m"] - rank_df["Rank_3m"]
        rank_df["rank_accel"]      = (
            rank_df["rank_up_3m"] - rank_df["rank_up_3m_prev"].fillna(0)
        ).round(0).astype(int)

        ret_now = df_m[df_m["Date"] == asof][
            ["Code","Name","ret_3m","ret_6m","ret_12m","ret_24m","Marcap"]
        ].drop_duplicates("Code")
        rank_df = rank_df.merge(ret_now, on="Code", how="left")

        rank_df["시총_조원"]      = marcap_to_choeok(rank_df["Marcap"]).round(2)
        rank_df["수익률_24M(%)"]  = (rank_df["ret_24m"] * 100).round(1)
        rank_df["수익률_12M(%)"]  = (rank_df["ret_12m"] * 100).round(1)
        rank_df["수익률_6M(%)"]   = (rank_df["ret_6m"]  * 100).round(1)
        rank_df["수익률_3M(%)"]   = (rank_df["ret_3m"]  * 100).round(1)

        def _make_rank_signal(df: pd.DataFrame, top_n: int, top_k: int = 10) -> pd.DataFrame:
            f = df[(df["rank_up_3m"] > 0) & (df["Rank"] <= top_n)].drop_duplicates("Code")
            if f.empty: return pd.DataFrame()
            cols = [c for c in ["Code","Name","Rank","Rank_3m","rank_up_3m","rank_accel",
                                "시총_조원","수익률_24M(%)","수익률_12M(%)","수익률_6M(%)","수익률_3M(%)"] if c in f.columns]
            return (
                f.sort_values("rank_up_3m", ascending=False).head(top_k)[cols]
                .rename(columns={"Code":"종목코드","Name":"종목명","Rank":"현재순위","Rank_3m":"3개월전순위",
                                 "rank_up_3m":"3M순위상승","rank_accel":"순위가속도"})
                .reset_index(drop=True)
            )

        rank_top50_df  = _make_rank_signal(rank_df, 50)
        rank_top100_df = _make_rank_signal(rank_df, 100)
        rank_top200_df = _make_rank_signal(rank_df, 200)

        for label, sig_df in [
            ("순위상승 — Top50  구간 Top10", rank_top50_df),
            ("순위상승 — Top100 구간 Top10", rank_top100_df),
            ("순위상승 — Top200 구간 Top10", rank_top200_df),
        ]:
            if not sig_df.empty:
                print(f"\n[{label}]")
                print(sig_df.to_string(index=False))

    # ── 모멘텀 필터 ───────────────────────────────────────────────────
    pos = cur[
        (cur["ret_6m"] > 0) &
        (cur["ret_12m"].notna() & (cur["ret_12m"] > 0) | cur["ret_12m"].isna()) &
        (cur["amt_accel"] > 0) & cur["amt_accel"].notna()
    ].copy()

    relaxed = False
    if len(pos) < 20:
        pos = cur[(cur["ret_6m"] > 0) & (cur["ret_3m"] > 0)].copy()
        relaxed = True
        print("[안내] 조건 완화 (6M·3M만)")

    if pos.empty:
        print("[경고] 모멘텀 양수 종목 없음"); return

    # ── 점수 산출 (미국판과 완전 동일한 로직) ────────────────────────
    def pct_rank(s: pd.Series, ascending: bool = True) -> pd.Series:
        return s.rank(pct=True, ascending=ascending, method="average")

    pos = pos.copy()
    pos["pr24"]    = pct_rank(pos["ret_24m"].fillna(pos["ret_12m"].fillna(pos["ret_6m"])))
    pos["pr12"]    = pct_rank(pos["ret_12m"].fillna(pos["ret_6m"]))
    pos["pr6"]     = pct_rank(pos["ret_6m"])
    pos["pr3"]     = pct_rank(pos["ret_3m"].fillna(0))
    pos["pr_cons"] = pct_rank(pos["consistency"].fillna(0.5))

    # 급발진 패널티
    pos["accel_ratio"]      = (pos["ret_3m"].fillna(0) / pos["ret_6m"].replace(0, float("nan"))).clip(0, 5)
    pos["pr_accel_penalty"] = pct_rank(pos["accel_ratio"], ascending=False)

    # 거래대금 과열 클램프
    pos["amt_accel_capped"] = pos["amt_accel"].clip(upper=0.8)
    pos["pr_amt_capped"]    = pct_rank(pos["amt_accel_capped"])

    if relaxed:
        pos["score"] = (
            pos["pr24"] * W_24M + pos["pr12"] * W_12M +
            pos["pr6"]  * W_6M  + pos["pr3"]  * W_3M  +
            pos["pr_cons"] * W_CONS + pos["pr_accel_penalty"] * W_PENALTY
        ) / (W_24M + W_12M + W_6M + W_3M + W_CONS + W_PENALTY)
        pos["pr_vol"] = pd.NA
    else:
        pos["pr_vol"] = pct_rank(pos["vol_cv"].fillna(pos["vol_cv"].median()), ascending=False)
        pos["score"]  = (
            pos["pr24"] * W_24M + pos["pr12"] * W_12M +
            pos["pr6"]  * W_6M  + pos["pr3"]  * W_3M  +
            pos["pr_amt_capped"] * W_AMT + pos["pr_cons"] * W_CONS +
            pos["pr_vol"] * W_VOL + pos["pr_accel_penalty"] * W_PENALTY
        ) / W_TOTAL

    pos = pos.sort_values("score", ascending=False)
    out = pos.head(OUTPUT_TOP).copy()

    # ── 출력 포맷 ─────────────────────────────────────────────────────
    out["매수순위"]          = range(1, len(out) + 1)
    out["시총_조원"]         = marcap_to_choeok(out["Marcap"]).round(2)
    out["시총증가율_24M(%)"] = (out["ret_24m"] * 100).round(1)
    out["시총증가율_12M(%)"] = (out["ret_12m"] * 100).round(1)
    out["시총증가율_6M(%)"]  = (out["ret_6m"]  * 100).round(1)
    out["시총증가율_3M(%)"]  = (out["ret_3m"]  * 100).round(1)
    out["거래대금가속_2M(%)"] = (out["amt_accel"] * 100).round(1) if not relaxed else None
    out["모멘텀일관성(12M)"] = out["consistency"].round(2)
    out["변동성_CV"]         = out["vol_cv"].round(3) if not relaxed else None
    out["모멘텀점수"]        = out["score"].round(4)
    out["급발진비율(3M/6M)"] = out["accel_ratio"].round(2)

    # 과열 신호
    cv_70pct = out["vol_cv"].quantile(0.7) if "vol_cv" in out.columns else 9999

    def _overheat(row):
        sigs = []
        if row.get("accel_ratio", 0) > 1.0:              sigs.append("급발진")
        if (row.get("amt_accel", 0) or 0) > 0.8:         sigs.append("수급과열")
        if (row.get("vol_cv", 0) or 0) > cv_70pct:       sigs.append("고변동")
        n = len(sigs)
        if n == 0:   return "✓ 건강"
        elif n == 1: return f"△ 주의({sigs[0]})"
        else:        return f"⚠ 과열({'·'.join(sigs)})"

    out["과열신호"] = out.apply(_overheat, axis=1)

    # 데이터 신뢰도
    def _reliability(row):
        if pd.notna(row.get("ret_24m")) and row.get("ret_24m") != 0: return "★★★ (24M)"
        elif pd.notna(row.get("ret_12m")) and row.get("ret_12m") != 0: return "★★ (12M)"
        else: return "★ (6M)"

    out["데이터신뢰도"] = out.apply(_reliability, axis=1)

    # PER 관련 컬럼
    out["PER"]     = out["PER"].round(1)  if "PER" in out.columns else float("nan")
    out["PBR"]     = out["PBR"].round(2)  if "PBR" in out.columns else float("nan")
    out["EPS(원)"] = out["EPS"].round(0)  if "EPS" in out.columns else float("nan")
    out["DIV(%)"]  = out["DIV"].round(2)  if "DIV" in out.columns else float("nan")

    export_cols = [
        "매수순위","Code","Name",
        "시총_조원","Rank",
        "시총증가율_24M(%)","시총증가율_12M(%)","시총증가율_6M(%)","시총증가율_3M(%)",
        "급발진비율(3M/6M)","거래대금가속_2M(%)","모멘텀일관성(12M)","변동성_CV",
        "모멘텀점수","과열신호","데이터신뢰도",
        "PER","PBR","EPS(원)","DIV(%)",
    ]
    export = out[[c for c in export_cols if c in out.columns]].rename(columns={
        "Code":"종목코드","Name":"종목명","Rank":"시총순위(당일)"
    })

    # 건강한 종목 (top80 중, CV 하드상한 포함)
    healthy_mask = (
        (out["consistency"].fillna(0)  >= 0.7) &
        (out["ret_12m"].fillna(0)      >  0  ) &
        (out["ret_6m"].fillna(0)       >  0  ) &
        (out["accel_ratio"].fillna(99) <= 1.0) &
        (out["amt_accel"].fillna(0)    <= 0.8) &
        (out["vol_cv"].fillna(999)     <= CV_HEALTHY_MAX)
    )
    healthy_df = export[healthy_mask.values].copy().reset_index(drop=True)
    healthy_df.insert(0, "건강순위", range(1, len(healthy_df) + 1))

    # 꾸준한 종목 — 전체 유니버스에서 탐색 (점수 상위80 밖의 숨은 건강주 발굴)
    cur_accel_ratio = (cur["ret_3m"].fillna(0) / cur["ret_6m"].replace(0, float("nan"))).clip(0, 5)
    steady_mask = (
        (cur["consistency"].fillna(0)  >= 0.7) &
        (cur["ret_12m"].fillna(0)      >  0  ) &
        (cur["ret_6m"].fillna(0)       >  0  ) &
        (cur_accel_ratio.fillna(99)    <= 1.0) &
        (cur["amt_accel"].fillna(0)    <= 0.8) &
        (cur["vol_cv"].fillna(999)     <= CV_HEALTHY_MAX)
    )
    steady_cur = cur[steady_mask].copy()
    if not steady_cur.empty:
        steady_cur["accel_ratio_s"] = cur_accel_ratio[steady_mask].values
        # 꾸준함점수 = 일관성 × 12M수익률 / CV (낮은 변동성에 높은 가중)
        steady_cur["꾸준함점수"] = (
            steady_cur["consistency"] *
            steady_cur["ret_12m"].fillna(steady_cur["ret_6m"]) /
            steady_cur["vol_cv"].replace(0, 0.01)
        ).round(3)
        steady_cur = steady_cur.sort_values("꾸준함점수", ascending=False).head(50)
        steady_cur["시총_조원"]         = marcap_to_choeok(steady_cur["Marcap"]).round(2)
        steady_cur["시총증가율_24M(%)"] = (steady_cur["ret_24m"] * 100).round(1)
        steady_cur["시총증가율_12M(%)"] = (steady_cur["ret_12m"] * 100).round(1)
        steady_cur["시총증가율_6M(%)"]  = (steady_cur["ret_6m"]  * 100).round(1)
        steady_cur["시총증가율_3M(%)"]  = (steady_cur["ret_3m"]  * 100).round(1)
        steady_cur["모멘텀일관성(12M)"] = steady_cur["consistency"].round(2)
        steady_cur["변동성_CV"]         = steady_cur["vol_cv"].round(3)
        steady_cur["급발진비율(3M/6M)"] = steady_cur["accel_ratio_s"].round(2)
        if _has_fund:
            for col, src, dec in [("PER","PER",1),("PBR","PBR",2),("EPS(원)","EPS",0),("DIV(%)","DIV",2)]:
                steady_cur[col] = steady_cur[src].round(dec) if src in steady_cur.columns else float("nan")
        s_cols = [
            "Code","Name","시총_조원","Rank",
            "시총증가율_24M(%)","시총증가율_12M(%)","시총증가율_6M(%)","시총증가율_3M(%)",
            "급발진비율(3M/6M)","모멘텀일관성(12M)","변동성_CV","꾸준함점수",
            "PER","PBR","EPS(원)","DIV(%)",
        ]
        steady_df = steady_cur[
            [c for c in s_cols if c in steady_cur.columns]
        ].rename(columns={"Code":"종목코드","Name":"종목명","Rank":"시총순위(당일)"}).reset_index(drop=True)
        steady_df.insert(0, "꾸준함순위", range(1, len(steady_df) + 1))
    else:
        steady_df = pd.DataFrame()

    # ── 산출조건 ─────────────────────────────────────────────────────
    method_df = pd.DataFrame([
        ("기준일",                asof_str),
        ("투자 철학",             "패러다임 투자 — 장기 보유. 이미 많이 오른 종목도 배제 안 함."),
        ("유니버스",              f"KOSPI+KOSDAQ 시총 상위 {UNIVERSE_TOP_N}종 (최소 {UNIVERSE_MIN_MARCAP_억:,}억)"),
        ("수익률 기준",           "주가 대신 시총(Marcap) 증가율 — 기업 가치 성장 측정"),
        ("과열신호",              "급발진(3M>6M) + 수급과열(거래대금>80%) + 고변동(CV상위30%)"),
        ("✓ 건강",                "3가지 모두 해당 없음"),
        ("△ 주의",                "1가지 해당"),
        ("⚠ 과열",                "2가지 이상 — 눌림 대기 or 비중 축소"),
        ("건강한종목 기준",       f"일관성≥0.7, 12M·6M양수, 3M≤6M, 거래대금가속≤80%, CV≤{CV_HEALTHY_MAX}"),
        ("꾸준한종목 시트",        "전체 유니버스(~400종)에서 건강 필터 적용 후 꾸준함점수(일관성×12M/CV) 정렬"),
        ("---",                   "---"),
        ("시총증가율_24M (가중 3.0)", "2년 시총 성장률 — 패러다임 핵심"),
        ("시총증가율_12M (가중 2.0)", "1년 시총 성장률"),
        ("시총증가율_6M  (가중 1.5)", "6개월"),
        ("시총증가율_3M  (가중 0.5)", "3개월 — 참고용"),
        ("거래대금가속 (가중 1.0)",   "최근 2M vs 이전 2M (80% 초과 클램프)"),
        ("모멘텀일관성 (가중 1.5)",   "12개월 중 시총 상승한 달 비율"),
        ("변동성패널티 (가중 1.0)",   "12개월 시총 CV — 낮을수록 우대"),
        ("급발진패널티 (가중 1.0)",   "3M/6M 비율 — 낮을수록 건강한 추세"),
        ("데이터신뢰도",          "★★★=24M있음 ★★=12M있음 ★=6M만"),
        ("---",                   "---"),
        ("PER 출처",              "pykrx(KRX) 우선, 미설정 시 NAVER Finance 스크래핑 자동 폴백"),
        ("PER 기준",              "<10 저평가 / 10~20 적정 / 20~35 고평가 / 35+ 과대평가 / N/A 적자·미집계"),
        ("PBR",                  "주가순자산비율 — 1 미만이면 장부가 이하"),
        ("EPS(원)",               "주당순이익 — 음수면 적자"),
        ("DIV(%)",                "배당수익률"),
        ("KRX 인증 설정",         "export KRX_ID=아이디  export KRX_PW=비밀번호  (서버 ~/.bashrc)"),
    ], columns=["항목","설명"])

    # ── 저장 ─────────────────────────────────────────────────────────
    tag = asof.strftime("%Y%m%d")
    for fname in [f"kr_paradigm_{tag}.xlsx", "kr_paradigm_latest.xlsx"]:
        path = root / fname
        with pd.ExcelWriter(path, engine="openpyxl") as w:
            export.to_excel(w,           sheet_name="모멘텀후보(매수순위)",      index=False)
            if not healthy_df.empty:
                healthy_df.to_excel(w,   sheet_name="건강한종목(과열제외)",      index=False)
            if not steady_df.empty:
                steady_df.to_excel(w,    sheet_name="꾸준한종목(전유니버스)",     index=False)
            if not paradigm_df.empty:
                paradigm_df.to_excel(w,  sheet_name="패러다임_Top50신규진입",    index=False)
            if not rank_top50_df.empty:
                rank_top50_df.to_excel(w,  sheet_name="순위상승_Top50구간",      index=False)
            if not rank_top100_df.empty:
                rank_top100_df.to_excel(w, sheet_name="순위상승_Top100구간",     index=False)
            if not rank_top200_df.empty:
                rank_top200_df.to_excel(w, sheet_name="순위상승_Top200구간",     index=False)
            method_df.to_excel(w,        sheet_name="산출조건",                  index=False)

    print(f"\n저장: {root / f'kr_paradigm_{tag}.xlsx'}")
    print(f"저장(고정명): {root / 'kr_paradigm_latest.xlsx'}")

    sig_counts = export["과열신호"].value_counts()
    print(f"\n▶ 과열 신호 분포:")
    for sig, cnt in sig_counts.items():
        print(f"  {sig}: {cnt}개")
    print(f"  건강한종목(top80): {len(healthy_df)}개")
    print(f"  꾸준한종목(전유니버스): {len(steady_df)}개")

    print(f"\n▶ 매수 우선순위 상위 20:")
    print(export.head(20).to_string(index=False))

    if not healthy_df.empty:
        print(f"\n▶ 건강한 종목 (top80 중, CV≤{CV_HEALTHY_MAX}):")
        print(healthy_df.to_string(index=False))

    if not steady_df.empty:
        print(f"\n▶ 꾸준한 종목 Top20 (전유니버스, CV≤{CV_HEALTHY_MAX}):")
        print(steady_df.head(20).to_string(index=False))


def main() -> None:
    run_paradigm_kr()


if __name__ == "__main__":
    main()
