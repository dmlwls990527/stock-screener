#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
월말 KRX 데이터로 국장 모멘텀 후보 종목을 선별합니다.
데이터 소스: FinanceData/marcap GitHub 저장소 (parquet).
  - 서버 경로: /data/frame/marcap/data/marcap-YYYY.parquet
  - 환경변수 MARCAP_DIR 로 경로 변경 가능

──────────────────────────────────────────────────────────────────────────────
[v2 보강 내용]

① 유니버스 확장 (300 → 500종) + 최소 시총 하한 추가
   - 급부상 중소형 모멘텀 종목을 포착
   - UNIVERSE_MIN_MARCAP_억 미만 극소형주는 진입 차단

② 필터링 강화 (노이즈 제거)
   - 기존: 단월 150% 초과 급등만 제거
   - 추가: 단월 -40% 이하 급락 종목 제거 (급락 후 기술적 반등 노이즈)
   - 추가: 최근 9개월 중 7개월 이상 데이터 있어야 통과 (데이터 연속성)
   - 추가: 월평균 거래대금 하한 (AMOUNT_MIN_억) — 유령 종목 차단

③ 점수 팩터 추가 (3개 → 6개)
   - 기존 3개: 6M 시총증가율, 3M 시총증가율, 거래대금가속_2M
   - 신규 추가:
     a) 1M 시총증가율: 단기 모멘텀이 살아있는지 확인 (최신성)
     b) 모멘텀 일관성: 최근 6개월 중 상승한 달 비율 → 꾸준히 오른 종목 우대
     c) 변동성 패널티: 월간 시총 변동계수(CV) — 같은 수익률이면 변동성 낮은 쪽 우대

④ 가중치 재조정
   - 기존: 6M(×2) + 3M(×1) + 거래대금(×1) / 4
   - 신규: 6M(×2.5) + 3M(×1.5) + 1M(×1) + 거래대금(×1) + 일관성(×1.5) − 변동성(×1) / 8.5

⑤ 클램프 제거
   - 기존 하드컷(0~200%)이 실제 강한 종목에 페널티 주던 문제 해소
   - rank(pct=True)가 이상값을 자연스럽게 처리

⑥ 패러다임 신호 강화
   - 신규 진입 종목에 6M 수익률·일관성 컬럼 추가
   - 신규: "상승가속 신호" 시트 — 3M 순위 상승 + 가속도 양수 종목 별도 표시

실행: test.py 에서 자동 호출. 단독: python3 screen_momentum.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_PKG_DIR))

from utils import load_marcap_data, marcap_to_choeok, MARCAP_DIR, START_DATE, END_DATE  # noqa: E402

# ── 파라미터 ──────────────────────────────────────────────────────────────────
UNIVERSE_TOP_BY_MARCAP  = 500    # ① 유니버스 확장 (기존 300 → 500)
UNIVERSE_MIN_MARCAP_억  = 3_000  # 최소 시총 3000억 이상 (극소형주 차단)
AMOUNT_MIN_억           = 50     # 월평균 거래대금 최소 50억 이상 (유령 종목 차단)
PARADIGM_TOP_N          = 50
OUTPUT_TOP              = 80
SINGLE_MONTH_JUMP_LIMIT = 1.5    # 단월 시총 150% 초과 급등 → 제거
SINGLE_MONTH_DROP_LIMIT = -0.4   # 단월 -40% 이하 급락 → 제거
MIN_MONTHS_REQUIRED     = 7      # 최근 9개월 중 최소 7개월 데이터 필요
OVERBOUGHT_6M_LIMIT     = 1.5    # 6M 시총 +150% 초과 → 과열, 제외 (이미 오를대로 오름)

# ── 점수 가중치 ───────────────────────────────────────────────────────────────
W_6M    = 0.5   # 장기 모멘텀 (과열 회피 목적으로 비중 대폭 축소)
W_3M    = 2.0   # 중기 모멘텀
W_1M    = 3.0   # 단기 모멘텀 최우선 (막 오르기 시작한 종목 포착)
W_AMT   = 1.0   # 거래대금 가속
W_CONS  = 1.5   # 모멘텀 일관성
W_VOL   = 1.0   # 변동성 패널티 (낮은 변동성 → 높은 점수)
W_FRESH = 1.0   # 모멘텀 지속성 (1M이 3M 월평균 대비 가속 중인지)
W_TOTAL = W_6M + W_3M + W_1M + W_AMT + W_CONS + W_VOL + W_FRESH


# ── 지표 설명 / 파라미터 시트 ─────────────────────────────────────────────────

def _methodology_rows(relaxed: bool) -> pd.DataFrame:
    rows = [
        ("데이터 출처",        "FinanceData/marcap GitHub (parquet). 월말 = 해당 달 마지막 거래일 행만 사용."),
        ("원천 컬럼",          "Marcap=시가총액(백만원), Amount=거래대금, Rank=당일 시총순위."),
        ("유니버스",           f"시총 상위 {UNIVERSE_TOP_BY_MARCAP}종 + 최소 시총 {UNIVERSE_MIN_MARCAP_억:,}억 이상."),
        ("비정상 필터",        f"단월 시총 급등 >{int(SINGLE_MONTH_JUMP_LIMIT*100)}% 또는 급락 <{int(SINGLE_MONTH_DROP_LIMIT*100)}% 종목 제거."),
        ("데이터 연속성 필터", f"최근 9개월 중 {MIN_MONTHS_REQUIRED}개월 이상 데이터 없으면 제거."),
        ("거래대금 필터",      f"최근 2개월 평균 거래대금 {AMOUNT_MIN_억:,}억 미만 종목 제거."),
        ("과열 필터",          f"6M 시총증가율 >{int(OVERBOUGHT_6M_LIMIT*100)}% 초과 종목 제외 — 이미 오를대로 오른 종목."),
        ("시총증가율_6M(%)",   "기준 월말 Marcap ÷ 6개월 전 Marcap − 1."),
        ("시총증가율_3M(%)",   "기준 월말 Marcap ÷ 3개월 전 Marcap − 1."),
        ("시총증가율_1M(%)",   "기준 월말 Marcap ÷ 1개월 전 Marcap − 1."),
        ("거래대금가속_2M(%)", "최근 2개월 Amount 평균 ÷ 이전 2개월 평균 − 1."),
        ("모멘텀일관성",       "최근 6개월 중 Marcap이 전월 대비 상승한 달 비율 (0~1)."),
        ("변동성_CV",          "최근 6개월 Marcap 변동계수(표준편차/평균). 낮을수록 안정적 상승."),
        ("모멘텀지속성",       "1M 수익률 ÷ (3M 수익률/3). >1이면 가속 중, <1이면 감속. 5로 클램프."),
        (
            "모멘텀점수",
            f"백분위 가중합 / {W_TOTAL}: "
            f"6M(×{W_6M}) + 3M(×{W_3M}) + 1M(×{W_1M}) + 거래대금(×{W_AMT}) "
            f"+ 일관성(×{W_CONS}) + 변동성역순(×{W_VOL}) + 지속성(×{W_FRESH})"
        ),
        ("후보 필터",          "6M·3M·1M 시총증가율 모두 양수 + 거래대금 가속 양수. 20종 미만이면 단계적 완화."),
        ("완화 모드",          "예" if relaxed else "아니오"),
        ("주의",               "참고용이며 투자 권유 아님."),
    ]
    return pd.DataFrame(rows, columns=["항목", "설명"])


def _params_rows(asof: str, relaxed: bool) -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("기준일(월말)",              asof),
            ("조회 시작",                START_DATE),
            ("조회 끝",                  END_DATE),
            ("유니버스 (시총 상위 N종)", str(UNIVERSE_TOP_BY_MARCAP)),
            ("최소 시총 기준(억)",       f"{UNIVERSE_MIN_MARCAP_억:,}"),
            ("최소 거래대금(억/월)",     f"{AMOUNT_MIN_억:,}"),
            ("패러다임 신호 기준 순위",   str(PARADIGM_TOP_N)),
            ("출력 상위",                str(OUTPUT_TOP)),
            ("단월 급등 필터",           f">{int(SINGLE_MONTH_JUMP_LIMIT*100)}%"),
            ("단월 급락 필터",           f"<{int(SINGLE_MONTH_DROP_LIMIT*100)}%"),
            ("과열 필터 (6M 상한)",      f"<={int(OVERBOUGHT_6M_LIMIT*100)}%"),
            ("데이터 연속성 기준",       f"최근 9개월 중 {MIN_MONTHS_REQUIRED}개월 이상"),
            ("점수 가중치 (6M/3M/1M/거래대금/일관성/변동성역순/지속성)",
             f"{W_6M}/{W_3M}/{W_1M}/{W_AMT}/{W_CONS}/{W_VOL}/{W_FRESH}"),
            ("거래대금 조건 완화",       "예" if relaxed else "아니오"),
        ],
        columns=["항목", "값"],
    )


def _write_xlsx(
    path: Path,
    export: pd.DataFrame,
    paradigm: pd.DataFrame,
    accel_signal: pd.DataFrame,
    asof: str,
    relaxed: bool,
) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        export.to_excel(writer,       sheet_name="후보",       index=False)
        paradigm.to_excel(writer,     sheet_name="패러다임신호", index=False)
        accel_signal.to_excel(writer, sheet_name="상승가속신호", index=False)
        _methodology_rows(relaxed).to_excel(writer, sheet_name="지표설명",  index=False)
        _params_rows(asof, relaxed).to_excel(writer, sheet_name="산출조건", index=False)


# ── 핵심 로직 ─────────────────────────────────────────────────────────────────

def run_momentum_screen(base_dir: Path | None = None) -> None:
    root = base_dir if base_dir is not None else _PKG_DIR

    print(f"로드: {START_DATE} ~ {END_DATE}  (marcap: {MARCAP_DIR})")
    df = load_marcap_data(START_DATE, END_DATE)
    df["Date"] = pd.to_datetime(df["Date"])
    print(f"  로드 완료: {len(df):,}행, 최신 날짜: {df['Date'].max().date()}")

    # ── 월말 행 추출 ─────────────────────────────────────────────────
    ym = df["Date"].dt.to_period("M")
    last_per_m = df.groupby(ym, sort=True)["Date"].transform("max")
    df_m = df[df["Date"] == last_per_m].copy()
    df_m = df_m.sort_values(["Code", "Date"]).reset_index(drop=True)

    g = df_m.groupby("Code", group_keys=False)

    # ── 기본 지표 계산 ────────────────────────────────────────────────
    df_m["marcap_1m_ago"] = g["Marcap"].shift(1)
    df_m["marcap_3m_ago"] = g["Marcap"].shift(3)
    df_m["marcap_6m_ago"] = g["Marcap"].shift(6)

    df_m["amt_roll2"]      = g["Amount"].transform(lambda s: s.rolling(2, min_periods=1).mean())
    df_m["amt_roll2_prev"] = g["amt_roll2"].shift(2)

    df_m["ret_mcap_1m"] = (df_m["Marcap"] / df_m["marcap_1m_ago"].replace(0, float("nan")) - 1).astype(float)
    df_m["ret_mcap_3m"] = (df_m["Marcap"] / df_m["marcap_3m_ago"].replace(0, float("nan")) - 1).astype(float)
    df_m["ret_mcap_6m"] = (df_m["Marcap"] / df_m["marcap_6m_ago"].replace(0, float("nan")) - 1).astype(float)
    df_m["amt_accel"]   = (df_m["amt_roll2"] / df_m["amt_roll2_prev"].replace(0, float("nan")) - 1).astype(float)
    df_m["single_m_jump"] = df_m["ret_mcap_1m"]

    # 순위 지표
    df_m["rank_3m_ago"]           = g["Rank"].shift(3)
    df_m["rank_6m_ago"]           = g["Rank"].shift(6)
    df_m["rank_velocity_6m"]      = df_m["rank_6m_ago"] - df_m["Rank"]
    df_m["rank_velocity_3m"]      = df_m["rank_3m_ago"] - df_m["Rank"]
    df_m["rank_velocity_3m_prev"] = df_m["rank_6m_ago"] - df_m["rank_3m_ago"]
    df_m["rank_accel"]            = df_m["rank_velocity_3m"] - df_m["rank_velocity_3m_prev"]

    asof = df_m["Date"].max()
    asof_str = str(asof.date())
    print(f"기준일(월말): {asof_str}")

    # ── 신규 팩터: 모멘텀 일관성 & 변동성 ───────────────────────────
    asof_6m_start = asof - pd.DateOffset(months=6)
    recent6 = df_m[df_m["Date"] > asof_6m_start].copy()

    consistency = (
        recent6.groupby("Code")["ret_mcap_1m"]
        .apply(lambda s: (s > 0).sum() / max(len(s), 1))
        .rename("consistency")
    )

    def _cv(s: pd.Series) -> float:
        m = s.mean()
        return float(s.std() / m) if m > 0 else float("nan")

    volatility = (
        recent6.groupby("Code")["Marcap"]
        .apply(_cv)
        .rename("vol_cv")
    )

    # ── 데이터 연속성 필터 ────────────────────────────────────────────
    asof_9m_start = asof - pd.DateOffset(months=9)
    recent9_count = (
        df_m[df_m["Date"] > asof_9m_start]
        .groupby("Code")["Date"]
        .count()
        .rename("months_count")
    )
    continuous_codes = recent9_count[recent9_count >= MIN_MONTHS_REQUIRED].index

    # ── 비정상 종목 제거 ──────────────────────────────────────────────
    recent = df_m[df_m["Date"] >= (asof - pd.DateOffset(months=6))]
    max_jump = recent.groupby("Code")["single_m_jump"].max()
    min_drop = recent.groupby("Code")["single_m_jump"].min()

    normal_codes = max_jump[
        (max_jump <= SINGLE_MONTH_JUMP_LIMIT) &
        (min_drop >= SINGLE_MONTH_DROP_LIMIT)
    ].index
    normal_codes = normal_codes.intersection(continuous_codes)

    n_removed_jump = int((max_jump > SINGLE_MONTH_JUMP_LIMIT).sum())
    n_removed_drop = int((min_drop < SINGLE_MONTH_DROP_LIMIT).sum())
    n_removed_cont = len(df_m["Code"].unique()) - len(recent9_count[recent9_count >= MIN_MONTHS_REQUIRED])
    print(f"비정상 필터: 급등 {n_removed_jump}개, 급락 {n_removed_drop}개, 연속성 미달 {n_removed_cont}개 제거")

    # ── 기준일 스냅샷 + 신규 팩터 합치기 ─────────────────────────────
    cur = df_m[df_m["Date"] == asof].copy()
    cur = cur[cur["Code"].isin(normal_codes)]
    cur = cur.merge(consistency, on="Code", how="left")
    cur = cur.merge(volatility,  on="Code", how="left")

    cur = cur[
        cur["marcap_6m_ago"].notna() & (cur["marcap_6m_ago"] > 0) &
        cur["marcap_3m_ago"].notna() & (cur["marcap_3m_ago"] > 0) &
        cur["marcap_1m_ago"].notna() & (cur["marcap_1m_ago"] > 0)
    ].copy()

    # ── 거래대금 절대값 하한 ──────────────────────────────────────────
    amount_min_백만 = AMOUNT_MIN_억 * 100
    before_amt = len(cur)
    cur = cur[cur["amt_roll2"] >= amount_min_백만]
    print(f"거래대금 하한 필터: {before_amt - len(cur)}개 종목 제거 (월평균 <{AMOUNT_MIN_억}억)")

    # ── 패러다임 신호: 상위 N위 신규 진입 ───────────────────────────
    prev_dates = df_m[df_m["Date"] < asof]["Date"]
    prev_asof  = prev_dates.max() if not prev_dates.empty else None

    if prev_asof is not None:
        prev_month = df_m[df_m["Date"] == prev_asof]
        cur_top    = set(cur[cur["Rank"] <= PARADIGM_TOP_N]["Code"])
        prev_top   = set(prev_month[prev_month["Rank"] <= PARADIGM_TOP_N]["Code"])
        new_codes  = cur_top - prev_top
    else:
        new_codes = set()

    paradigm_df = cur[cur["Code"].isin(new_codes)][
        ["Code", "Name", "Rank", "Marcap", "rank_velocity_6m", "rank_accel",
         "ret_mcap_6m", "ret_mcap_3m", "consistency"]
    ].copy()
    paradigm_df["시가총액_조원"] = marcap_to_choeok(paradigm_df["Marcap"]).round(2)
    paradigm_df["순위변화_6M"]   = paradigm_df["rank_velocity_6m"].round(0)
    paradigm_df["순위가속도"]    = paradigm_df["rank_accel"].round(0)
    paradigm_df["6M수익률(%)"]   = (paradigm_df["ret_mcap_6m"] * 100).round(1)
    paradigm_df["일관성"]        = paradigm_df["consistency"].round(2)
    paradigm_df = paradigm_df.sort_values("Rank")[
        ["Code", "Name", "Rank", "시가총액_조원", "순위변화_6M", "순위가속도", "6M수익률(%)", "일관성"]
    ].rename(columns={"Code": "종목코드", "Name": "종목명", "Rank": "현재순위"})

    if not paradigm_df.empty:
        print(f"\n[패러다임 신호] 상위 {PARADIGM_TOP_N}위 신규 진입 {len(paradigm_df)}개:")
        print(paradigm_df.to_string(index=False))
    else:
        print(f"\n[패러다임 신호] 상위 {PARADIGM_TOP_N}위 신규 진입 없음")

    # ── 상승가속 신호: 3M 순위 상승 + 가속도 양수 ────────────────────
    accel_signal_df = cur[
        (cur["rank_velocity_3m"] > 0) &
        (cur["rank_accel"] > 0) &
        (cur["Rank"] <= UNIVERSE_TOP_BY_MARCAP)
    ][["Code", "Name", "Rank", "Marcap",
       "rank_velocity_3m", "rank_accel", "ret_mcap_3m", "ret_mcap_6m"]].copy()

    accel_signal_df["시가총액_조원"] = marcap_to_choeok(accel_signal_df["Marcap"]).round(2)
    accel_signal_df["3M순위상승"]    = accel_signal_df["rank_velocity_3m"].round(0)
    accel_signal_df["순위가속도"]    = accel_signal_df["rank_accel"].round(0)
    accel_signal_df["3M수익률(%)"]   = (accel_signal_df["ret_mcap_3m"] * 100).round(1)
    accel_signal_df["6M수익률(%)"]   = (accel_signal_df["ret_mcap_6m"] * 100).round(1)
    accel_signal_df = accel_signal_df.sort_values("rank_accel", ascending=False)[
        ["Code", "Name", "Rank", "시가총액_조원", "3M순위상승", "순위가속도", "3M수익률(%)", "6M수익률(%)"]
    ].rename(columns={"Code": "종목코드", "Name": "종목명", "Rank": "현재순위"}).head(30)

    print(f"\n[상승가속 신호] 순위 가속 상위 {len(accel_signal_df)}개:")
    print(accel_signal_df.head(10).to_string(index=False))

    # ── 모멘텀 유니버스 구성 ─────────────────────────────────────────
    min_marcap_백만 = UNIVERSE_MIN_MARCAP_억 * 100
    cur_filtered = cur[cur["Marcap"] >= min_marcap_백만]
    big_codes = cur_filtered.nlargest(UNIVERSE_TOP_BY_MARCAP, "Marcap")["Code"]
    u = cur[cur["Code"].isin(big_codes)].copy()

    # 과열 필터: 6M 이미 너무 많이 오른 종목 제외
    before_ob = len(u)
    u = u[u["ret_mcap_6m"] <= OVERBOUGHT_6M_LIMIT]
    print(f"과열 필터: {before_ob - len(u)}개 제거 (6M 시총 >{int(OVERBOUGHT_6M_LIMIT*100)}%)")

    # 모멘텀 지속성: 1M 수익률 vs 3M 월평균 비교 (>1이면 가속, <1이면 감속)
    monthly_rate_3m = u["ret_mcap_3m"] / 3
    u = u.copy()
    u["mom_freshness"] = u["ret_mcap_1m"] / monthly_rate_3m.replace(0, float("nan"))
    u.loc[monthly_rate_3m <= 0, "mom_freshness"] = float("nan")
    u["mom_freshness"] = u["mom_freshness"].clip(-1, 5)

    # 단계적 필터 (6M/3M/1M + 거래대금)
    pos = u[
        (u["ret_mcap_6m"] > 0) &
        (u["ret_mcap_3m"] > 0) &
        (u["ret_mcap_1m"] > 0) &
        (u["amt_accel"] > 0) &
        u["amt_accel"].notna()
    ].copy()

    relaxed = False
    if len(pos) < 20:
        pos = u[
            (u["ret_mcap_6m"] > 0) &
            (u["ret_mcap_3m"] > 0) &
            (u["amt_accel"] > 0) &
            u["amt_accel"].notna()
        ].copy()
        if len(pos) < 20:
            pos = u[(u["ret_mcap_6m"] > 0) & (u["ret_mcap_3m"] > 0)].copy()
            relaxed = True
            print("[안내] 거래대금까지 양수인 종목 20개 미만 → 6M·3M만 사용합니다.")

    u = pos
    if u.empty:
        print("[경고] 시총 6M·3M 모두 양수인 종목이 없습니다.")
        return

    # ── 점수 산출 (클램프 제거, 백분위로 자연 처리) ──────────────────
    u = u.copy()

    def pct_rank(series: pd.Series, ascending: bool = True) -> pd.Series:
        return series.rank(pct=True, ascending=ascending, method="average")

    u["pr6"]    = pct_rank(u["ret_mcap_6m"])
    u["pr3"]    = pct_rank(u["ret_mcap_3m"])
    u["pr1"]    = pct_rank(u["ret_mcap_1m"])
    u["pr_cons"] = pct_rank(u["consistency"].fillna(0.5))

    if relaxed:
        u["pr_fresh"] = pct_rank(u["mom_freshness"].fillna(0.5))
        u["score"] = (
            u["pr6"]      * W_6M   +
            u["pr3"]      * W_3M   +
            u["pr1"]      * W_1M   +
            u["pr_cons"]  * W_CONS +
            u["pr_fresh"] * W_FRESH
        ) / (W_6M + W_3M + W_1M + W_CONS + W_FRESH)
        u["pra"]    = pd.NA
        u["pr_vol"] = pd.NA
    else:
        # 변동성 패널티: CV 낮을수록 점수↑ (ascending=False → 낮은 CV = 높은 백분위)
        u["pr_vol"]   = pct_rank(u["vol_cv"].fillna(u["vol_cv"].median()), ascending=False)
        u["pra"]      = pct_rank(u["amt_accel"])
        u["pr_fresh"] = pct_rank(u["mom_freshness"].fillna(0.5))
        u["score"]    = (
            u["pr6"]      * W_6M   +
            u["pr3"]      * W_3M   +
            u["pr1"]      * W_1M   +
            u["pra"]      * W_AMT  +
            u["pr_cons"]  * W_CONS +
            u["pr_vol"]   * W_VOL  +
            u["pr_fresh"] * W_FRESH
        ) / W_TOTAL

    u = u.sort_values("score", ascending=False)
    out = u.head(OUTPUT_TOP).copy()

    out["시가총액_조원"]   = marcap_to_choeok(out["Marcap"]).round(2)
    out["시총증가율_6M"]   = (out["ret_mcap_6m"] * 100).round(2)
    out["시총증가율_3M"]   = (out["ret_mcap_3m"] * 100).round(2)
    out["시총증가율_1M"]   = (out["ret_mcap_1m"] * 100).round(2)
    out["거래대금가속_2M"] = (out["amt_accel"] * 100).round(2) if not relaxed else pd.NA
    out["모멘텀일관성"]    = out["consistency"].round(2)
    out["변동성_CV"]       = out["vol_cv"].round(3) if not relaxed else pd.NA
    out["모멘텀지속성"]    = out["mom_freshness"].round(2) if not relaxed else pd.NA
    out["모멘텀점수"]      = out["score"].round(4)
    out["순위변화_6M"]     = out["rank_velocity_6m"].round(0)
    out["순위가속도"]      = out["rank_accel"].round(0)

    cols = [
        "Code", "Name", "시가총액_조원",
        "시총증가율_6M", "시총증가율_3M", "시총증가율_1M",
        "거래대금가속_2M", "모멘텀일관성", "변동성_CV", "모멘텀지속성",
        "모멘텀점수", "순위변화_6M", "순위가속도", "Rank",
    ]
    export = out[[c for c in cols if c in out.columns]].rename(
        columns={"Code": "종목코드", "Name": "종목명", "Rank": "시총순위(당일)"}
    )

    tag = asof.strftime("%Y%m%d")
    for path in [root / f"momentum_screen_{tag}.xlsx", root / "momentum_screen_latest.xlsx"]:
        _write_xlsx(path, export, paradigm_df, accel_signal_df, asof_str, relaxed)

    print(f"\n저장: {root / f'momentum_screen_{tag}.xlsx'}")
    print(f"저장(고정명): {root / 'momentum_screen_latest.xlsx'}")
    print("\n상위 15 (미리보기):")
    print(export.head(15).to_string(index=False))


def main() -> None:
    run_momentum_screen()


if __name__ == "__main__":
    main()
