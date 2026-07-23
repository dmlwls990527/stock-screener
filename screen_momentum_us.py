#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
미국 S&P500 패러다임 투자 스크리너
Yahoo Finance 월별 주가 + SSGA SPY 홀딩스(SharesHeld × Close = 시총)

──────────────────────────────────────────────────────────────────────────────
핵심 구조

① 시총 계산: Close × SharesHeld → 진짜 시총($B)
   - 이미 캐시에 28개월치 주가 있음
   - SPY 홀딩스의 SharesHeld와 곱해서 월별 시총 계산
   - 매달 실행할 필요 없이 캐시 파일로 자동 히스토리 생성

② 시총 순위 히스토리: 캐시 기반 자동 계산
   - 국장 marcap 방식과 동일
   - 전월/3개월전/6개월전 순위 자동 비교

③ 패러다임 신호: 시총 Top20/50 신규 진입
   - SPY Weight가 아닌 실제 시총 기반 순위

④ 과열 신호: 급발진(3M>6M) + 수급과열(거래대금>80%) + 고변동
⑤ 건강한 종목 시트: 꾸준한 추세 + 과열 없는 종목

점수: 24M(×3.0) + 12M(×2.0) + 6M(×1.5) + 3M(×0.5)
    + 거래대금(×1.0) + 일관성(×1.5) + 변동성(×1.0) + 급발진패널티(×1.0)
"""

from __future__ import annotations

import io
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

_PKG_DIR  = Path(__file__).resolve().parent
CACHE_DIR = _PKG_DIR / "us_monthly_cache"

# ── 파라미터 ──────────────────────────────────────────────────────────────────
UNIVERSE_N              = 500
OUTPUT_TOP              = 80
PARADIGM_TOP_N          = 20
SINGLE_MONTH_DROP_LIMIT = -0.4
MIN_MONTHS_REQUIRED     = 7
AMOUNT_MIN_B            = 0.5
CV_HEALTHY_MAX          = 0.5     # 건강종목 변동성 CV 상한 (고변동 직접 차단)

# ── 점수 가중치 ───────────────────────────────────────────────────────────────
W_24M     = 3.0
W_12M     = 2.0
W_6M      = 1.5
W_3M      = 0.5
W_AMT     = 1.0
W_CONS    = 1.5
W_VOL     = 1.0
W_PENALTY = 1.0   # 급발진 패널티
W_TOTAL   = W_24M + W_12M + W_6M + W_3M + W_AMT + W_CONS + W_VOL + W_PENALTY



# ── Yahoo Finance 월별 주가 ───────────────────────────────────────────────────


def _fetch_yahoo_monthly(symbol: str, start: date, end: date) -> pd.DataFrame:
    """yfinance ticker.history() 기반 월봉 데이터 수집"""
    try:
        df = yf.Ticker(symbol).history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval="1mo",
            auto_adjust=True,
        )
        if df.empty:
            return pd.DataFrame()
        df = df[["Close", "Volume"]].copy()
        # timezone 제거 후 월 시작일로 정규화
        df.index = df.index.tz_localize(None).to_period('M').to_timestamp()
        df = df[~df.index.duplicated(keep='last')].sort_index()
        df = df.dropna(subset=["Close"])
        return df
    except Exception:
        return pd.DataFrame()


def _load_or_fetch_monthly(symbol: str, today: date) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp = CACHE_DIR / f"{symbol}.parquet"
    required_start = date(2000, 1, 1)

    existing = pd.DataFrame()
    if cp.exists():
        existing = pd.read_parquet(cp)

    if not existing.empty:
        first_cached = existing.index.min().date()
        last_cached  = existing.index.max().date()
        this_month   = today.replace(day=1)
        if first_cached <= required_start and last_cached >= this_month:
            return existing

    new = _fetch_yahoo_monthly(symbol, required_start, today)
    if new.empty:
        return existing

    if not existing.empty:
        combined = pd.concat([existing, new[~new.index.isin(existing.index)]])
    else:
        combined = new

    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    combined.to_parquet(cp)
    return combined


# ── SPY 홀딩스 ────────────────────────────────────────────────────────────────

_SPY_URL = (
    "https://www.ssga.com/us/en/intermediary/etfs/library-content"
    "/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
)
_SPY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Referer": "https://www.ssga.com/",
}


def _fetch_spy_holdings(today: date) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today_cache = CACHE_DIR / "_universe.parquet"

    if today_cache.exists():
        cached = pd.read_parquet(today_cache)
        if not cached.empty and str(cached.get("fetch_date", pd.Series([""]))[0]) == str(today):
            return cached

    print("  SSGA SPY 홀딩스 다운로드 중...")
    try:
        r = requests.get(_SPY_URL, headers=_SPY_HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"  [경고] SPY 다운로드 실패: {e}")
        if today_cache.exists():
            return pd.read_parquet(today_cache)
        return pd.DataFrame()

    # skiprows 탐색: 헤더 행 위치가 버전마다 다름 (3~5행)
    df = pd.DataFrame()
    for skip in (4, 3, 5):
        try:
            raw = pd.read_excel(io.BytesIO(r.content), skiprows=skip, engine="openpyxl")
            raw.columns = [str(c).strip() for c in raw.columns]
            # 티커 컬럼이 있고 실제 데이터가 있으면 성공
            ticker_col = next((c for c in raw.columns if "ticker" in c.lower()), None)
            if ticker_col and len(raw) > 10:
                break
        except Exception:
            continue

    col_map: dict[str, str] = {}
    for c in raw.columns:
        cl = c.lower()
        if "ticker" in cl:            col_map[c] = "Code"
        elif cl == "name":            col_map[c] = "Name"
        elif "weight" in cl:          col_map[c] = "Weight"
        elif "sector" in cl:          col_map[c] = "Sector"
        elif "shares" in cl:          col_map[c] = "SharesHeld"
        elif "asset" in cl and "class" in cl: col_map[c] = "AssetClass"
    raw = raw.rename(columns=col_map)

    needed = [c for c in ["Code","Name","Weight","Sector","SharesHeld"] if c in raw.columns]
    df = raw[needed].copy().dropna(subset=["Code"])
    df = df[df["Code"].astype(str).str.match(r"^[A-Z]{1,5}$", na=False)]
    df["Weight"]     = pd.to_numeric(df.get("Weight",     0), errors="coerce").fillna(0.0)
    df["SharesHeld"] = pd.to_numeric(df.get("SharesHeld", 0), errors="coerce").fillna(0)
    df = df[df["Weight"] > 0].sort_values("Weight", ascending=False).reset_index(drop=True)

    # Sector가 없거나 대부분 NaN이면 GICS 섹터 하드코딩 보완
    if "Sector" not in df.columns or df["Sector"].isna().mean() > 0.8:
        print("  [경고] Sector 컬럼 없음 — 기본값 적용")
        df["Sector"] = "-"

    df["fetch_date"] = str(today)
    df.to_parquet(today_cache, index=False)
    print(f"  SPY 홀딩스 저장: {len(df)}종  (Sector 유효: {(df['Sector'] != '-').sum()}개)")
    return df


# ── 시총 기반 월별 순위 계산 ─────────────────────────────────────────────────

def _build_monthly_marcap(
    stock_data: dict[str, pd.DataFrame],
    shares_map: dict[str, float],
) -> pd.DataFrame:
    """
    캐시 데이터 + SharesHeld → 월별 시총($B) + 순위 계산
    국장 marcap 방식과 동일한 구조
    반환: Date, Code, Marcap_B (시총), Rank_M (시총순위)
    """
    rows = []
    for sym, df in stock_data.items():
        shares = shares_map.get(sym, 0)
        if shares <= 0:
            continue
        for dt, row in df.iterrows():
            close = row.get("Close") or row.get("close")
            if close and close > 0:
                rows.append({
                    "Date":    dt,
                    "Code":    sym,
                    "Marcap_B": close * shares / 1e9,
                })

    if not rows:
        return pd.DataFrame()

    df_mc = pd.DataFrame(rows)
    df_mc["Date"] = pd.to_datetime(df_mc["Date"])

    # 월별 시총 순위 계산
    df_mc = df_mc.sort_values(["Date", "Marcap_B"], ascending=[True, False])
    df_mc["Rank_M"] = df_mc.groupby("Date")["Marcap_B"].rank(
        ascending=False, method="min"
    ).astype(int)

    return df_mc.sort_values(["Code", "Date"]).reset_index(drop=True)


# ── 신호 생성 ─────────────────────────────────────────────────────────────────

def _make_rank_signal(rank_df: pd.DataFrame, top_n: int, top_k: int = 10) -> pd.DataFrame:
    filtered = rank_df[
        (rank_df["rank_up_3m"] > 0) &
        (rank_df["Rank_M"] <= top_n)
    ].drop_duplicates("Code").copy()
    if filtered.empty:
        return pd.DataFrame()
    cols = [c for c in [
        "Code","Name","Sector","Rank_M","Rank_M_3m",
        "rank_up_3m","rank_accel",
        "수익률_24M(%)","수익률_12M(%)","수익률_6M(%)","수익률_3M(%)",
    ] if c in filtered.columns]
    return (
        filtered.sort_values("rank_up_3m", ascending=False)
        .head(top_k)[cols]
        .rename(columns={
            "Code":"티커","Name":"종목명","Sector":"섹터",
            "Rank_M":"현재순위(시총)","Rank_M_3m":"3개월전순위(시총)",
            "rank_up_3m":"3M순위상승","rank_accel":"순위가속도",
        })
        .reset_index(drop=True)
    )


def _make_new_entry(
    cur_mc: pd.DataFrame,
    prev_mc: pd.DataFrame,
    top_n: int,
    ret_df: pd.DataFrame,
    consistency: pd.Series,
    universe: pd.DataFrame,
) -> pd.DataFrame:
    if cur_mc.empty or prev_mc.empty:
        return pd.DataFrame()
    cur_top  = set(cur_mc[cur_mc["Rank_M"] <= top_n]["Code"])
    prev_top = set(prev_mc[prev_mc["Rank_M"] <= top_n]["Code"])
    new_codes = cur_top - prev_top
    if not new_codes:
        return pd.DataFrame()

    rows = cur_mc[cur_mc["Code"].isin(new_codes)].drop_duplicates("Code").copy()
    rows = rows.merge(universe[["Code","Name","Sector"]].drop_duplicates("Code"), on="Code", how="left")
    rows = rows.merge(ret_df, on="Code", how="left")
    rows = rows.merge(consistency.rename("consistency"), on="Code", how="left")
    rows["수익률_6M(%)"]  = (rows["ret_6m"]  * 100).round(1)
    rows["수익률_12M(%)"] = (rows["ret_12m"] * 100).round(1)
    rows["수익률_24M(%)"] = (rows["ret_24m"] * 100).round(1)
    rows["일관성"]        = rows["consistency"].round(2)
    cols = ["Code","Name","Sector","Rank_M","수익률_6M(%)","수익률_12M(%)","수익률_24M(%)","일관성"]
    return (
        rows[[c for c in cols if c in rows.columns]]
        .rename(columns={"Code":"티커","Name":"종목명","Sector":"섹터","Rank_M":"현재순위(시총)"})
        .sort_values("현재순위(시총)")
        .reset_index(drop=True)
    )


# ── 메인 ─────────────────────────────────────────────────────────────────────

def run_momentum_us(base_dir: Path | None = None) -> None:
    root  = base_dir if base_dir is not None else _PKG_DIR
    today = date.today()

    # ── 1. 유니버스 로드 ──────────────────────────────────────────────
    print("S&P500 유니버스 로드 중...")
    universe = _fetch_spy_holdings(today)
    if universe.empty:
        print("[오류] 유니버스 로드 실패"); return
    universe = universe.drop_duplicates("Code").head(UNIVERSE_N)
    print(f"  유니버스: {len(universe)}종")

    symbols    = universe["Code"].tolist()
    shares_map = universe.set_index("Code")["SharesHeld"].to_dict()

    # ── 2. 월별 주가 로드 (캐시 우선) ────────────────────────────────
    print("Yahoo Finance 월별 주가 로드 중 (캐시 우선)...")
    stock_data: dict[str, pd.DataFrame] = {}
    for i, sym in enumerate(symbols):
        df = _load_or_fetch_monthly(sym, today)
        if len(df) >= 7:
            stock_data[sym] = df
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(symbols)} 처리...")
    print(f"  유효 종목: {len(stock_data)}개")
    if not stock_data:
        print("[오류] 데이터 없음"); return

    # ── 3. 시총 기반 월별 순위 계산 (캐시로 자동 생성) ───────────────
    print("시총 기반 월별 순위 계산 중...")
    df_mc = _build_monthly_marcap(stock_data, shares_map)
    if df_mc.empty:
        print("[경고] 시총 계산 실패 — SharesHeld 데이터 확인 필요")
    else:
        months = df_mc["Date"].dt.to_period("M").nunique()
        print(f"  시총 히스토리: {months}개월치")

    # ── 4. long-format 주가 DataFrame ────────────────────────────────
    frames = []
    for sym, df in stock_data.items():
        tmp = df[["Close","Volume"]].copy()
        tmp["Code"]   = sym
        tmp["Amount"] = tmp["Close"] * tmp["Volume"]
        frames.append(tmp.reset_index())

    df_m = pd.concat(frames, ignore_index=True)
    df_m["Date"] = pd.to_datetime(df_m["Date"])
    df_m = df_m.rename(columns={"Close":"Price"})
    df_m = df_m.drop_duplicates(["Code","Date"]).sort_values(["Code","Date"])

    asof     = df_m["Date"].max()
    asof_str = str(asof.date())
    print(f"기준일: {asof_str}")

    # ── 5. 수익률 지표 ────────────────────────────────────────────────
    g = df_m.groupby("Code", group_keys=False)
    df_m["price_1m_ago"]  = g["Price"].shift(1)
    df_m["price_3m_ago"]  = g["Price"].shift(3)
    df_m["price_6m_ago"]  = g["Price"].shift(6)
    df_m["price_12m_ago"] = g["Price"].shift(12)
    df_m["price_24m_ago"] = g["Price"].shift(24)
    df_m["amt_roll2"]      = g["Amount"].transform(lambda s: s.rolling(2, min_periods=1).mean())
    df_m["amt_roll2_prev"] = g["amt_roll2"].shift(2)

    def _sr(a, b):
        return (a / b.replace(0, float("nan")) - 1).astype(float)

    df_m["ret_1m"]        = _sr(df_m["Price"], df_m["price_1m_ago"])
    df_m["ret_3m"]        = _sr(df_m["Price"], df_m["price_3m_ago"])
    df_m["ret_6m"]        = _sr(df_m["Price"], df_m["price_6m_ago"])
    df_m["ret_12m"]       = _sr(df_m["Price"], df_m["price_12m_ago"])
    df_m["ret_24m"]       = _sr(df_m["Price"], df_m["price_24m_ago"])
    df_m["amt_accel"]     = _sr(df_m["amt_roll2"], df_m["amt_roll2_prev"])
    df_m["single_m_jump"] = df_m["ret_1m"]

    # ── 6. 일관성 & 변동성 ────────────────────────────────────────────
    recent12 = df_m[df_m["Date"] > (asof - pd.DateOffset(months=12))].copy()

    consistency = (
        recent12.groupby("Code")["ret_1m"]
        .apply(lambda s: float((s > 0).sum() / max(len(s), 1)))
        .rename("consistency").reset_index()
    )

    def _cv(s):
        m = s.mean()
        return float(s.std() / m) if m > 0 else float("nan")

    volatility = (
        recent12.groupby("Code")["Price"]
        .apply(_cv).rename("vol_cv").reset_index()
    )

    # ── 7. 비정상 제거 ────────────────────────────────────────────────
    recent9_count = (
        df_m[df_m["Date"] > (asof - pd.DateOffset(months=9))]
        .groupby("Code")["Date"].count().rename("months_count")
    )
    continuous_codes = recent9_count[recent9_count >= MIN_MONTHS_REQUIRED].index

    recent6  = df_m[df_m["Date"] >= (asof - pd.DateOffset(months=6))]
    min_drop = recent6.groupby("Code")["single_m_jump"].min()
    normal_codes = min_drop[min_drop >= SINGLE_MONTH_DROP_LIMIT].index
    normal_codes = normal_codes.intersection(continuous_codes)
    print(f"비정상 필터: 급락 {int((min_drop < SINGLE_MONTH_DROP_LIMIT).sum())}개 제거")

    # ── 8. 기준일 스냅샷 ──────────────────────────────────────────────
    cur_price = df_m[df_m["Date"] == asof].drop_duplicates("Code").copy()
    cur_all = cur_price.merge(
        universe[["Code","Name","Sector","Weight","SharesHeld"]].drop_duplicates("Code"),
        on="Code", how="left"
    )

    # 시총 기반 순위 (Marcap_B = Close × SharesHeld)
    cur_all["Marcap_B"] = (
        cur_all["Price"] * cur_all["SharesHeld"].fillna(0) / 1e9
    ).round(2)
    cur_all["Rank_M"] = cur_all["Marcap_B"].rank(
        ascending=False, method="min", na_option="bottom"
    ).astype(int)

    cur = cur_all[cur_all["Code"].isin(normal_codes)].copy()
    cur = cur.merge(consistency, on="Code", how="left")
    cur = cur.merge(volatility,  on="Code", how="left")
    cur = cur[cur["price_6m_ago"].notna() & (cur["price_6m_ago"] > 0)].copy()

    amount_min_raw = AMOUNT_MIN_B * 1e9
    before_amt = len(cur)
    cur = cur[cur["amt_roll2"] >= amount_min_raw]
    print(f"거래대금 하한 필터: {before_amt - len(cur)}개 제거")

    # ── 9. 패러다임 신호 — 시총 기반 Top20/50 신규 진입 ─────────────
    # df_mc 에서 전월/3개월전 스냅샷 추출
    ret_snap = df_m[df_m["Date"] == asof][
        ["Code","ret_6m","ret_12m","ret_24m"]
    ].drop_duplicates("Code")

    cur_mc_snap = df_mc[df_mc["Date"] == asof].drop_duplicates("Code") if not df_mc.empty else pd.DataFrame()

    # 전월 스냅샷
    prev_dates = df_mc["Date"].drop_duplicates().sort_values() if not df_mc.empty else pd.Series(dtype="datetime64[ns]")
    prev_dates_lt = prev_dates[prev_dates < asof]

    prev1_mc = pd.DataFrame()
    prev3_mc = pd.DataFrame()
    prev6_mc = pd.DataFrame()
    if len(prev_dates_lt) >= 1:
        prev1_mc = df_mc[df_mc["Date"] == prev_dates_lt.iloc[-1]].drop_duplicates("Code")
    if len(prev_dates_lt) >= 3:
        prev3_mc = df_mc[df_mc["Date"] == prev_dates_lt.iloc[-3]].drop_duplicates("Code")
    if len(prev_dates_lt) >= 6:
        prev6_mc = df_mc[df_mc["Date"] == prev_dates_lt.iloc[-6]].drop_duplicates("Code")

    paradigm_top20_df = _make_new_entry(
        cur_mc_snap, prev1_mc, 20, ret_snap,
        consistency.set_index("Code")["consistency"], universe
    )
    paradigm_top50_df = _make_new_entry(
        cur_mc_snap, prev1_mc, 50, ret_snap,
        consistency.set_index("Code")["consistency"], universe
    )

    for label, sig_df in [
        ("패러다임 신호 — 시총 Top20 신규 진입", paradigm_top20_df),
        ("패러다임 신호 — 시총 Top50 신규 진입", paradigm_top50_df),
    ]:
        if not sig_df.empty:
            print(f"\n[{label}] {len(sig_df)}개:")
            print(sig_df.to_string(index=False))
        else:
            print(f"\n[{label}] 없음")

    # ── 10. 순위 상승 신호 — 시총 구간별 Top10 ───────────────────────
    rank_top50_df  = pd.DataFrame()
    rank_top100_df = pd.DataFrame()
    rank_top200_df = pd.DataFrame()

    if not prev3_mc.empty and not cur_mc_snap.empty:
        cur_rk  = cur_mc_snap[["Code","Rank_M"]].drop_duplicates("Code")
        prev3_rk = prev3_mc[["Code","Rank_M"]].drop_duplicates("Code").rename(columns={"Rank_M":"Rank_M_3m"})
        prev6_rk = prev6_mc[["Code","Rank_M"]].drop_duplicates("Code").rename(columns={"Rank_M":"Rank_M_6m"}) if not prev6_mc.empty else pd.DataFrame()

        rank_df = cur_rk.merge(prev3_rk, on="Code", how="inner")
        if not prev6_rk.empty:
            rank_df = rank_df.merge(prev6_rk, on="Code", how="left")
        else:
            rank_df["Rank_M_6m"] = float("nan")

        rank_df["rank_up_3m"]      = rank_df["Rank_M_3m"] - rank_df["Rank_M"]
        rank_df["rank_up_3m_prev"] = rank_df["Rank_M_6m"] - rank_df["Rank_M_3m"]
        rank_df["rank_accel"]      = (
            rank_df["rank_up_3m"] - rank_df["rank_up_3m_prev"].fillna(0)
        ).round(0).astype(int)

        # 수익률 + 종목명 합치기
        rank_df = rank_df.merge(
            df_m[df_m["Date"] == asof][["Code","ret_3m","ret_6m","ret_12m","ret_24m"]].drop_duplicates("Code"),
            on="Code", how="left"
        ).merge(universe[["Code","Name","Sector"]].drop_duplicates("Code"), on="Code", how="left")

        rank_df["수익률_24M(%)"] = (rank_df["ret_24m"] * 100).round(1)
        rank_df["수익률_12M(%)"] = (rank_df["ret_12m"] * 100).round(1)
        rank_df["수익률_6M(%)"]  = (rank_df["ret_6m"]  * 100).round(1)
        rank_df["수익률_3M(%)"]  = (rank_df["ret_3m"]  * 100).round(1)

        rank_top50_df  = _make_rank_signal(rank_df, 50,  top_k=10)
        rank_top100_df = _make_rank_signal(rank_df, 100, top_k=10)
        rank_top200_df = _make_rank_signal(rank_df, 200, top_k=10)

        for label, sig_df in [
            ("순위상승 — Top50  구간 Top10", rank_top50_df),
            ("순위상승 — Top100 구간 Top10", rank_top100_df),
            ("순위상승 — Top200 구간 Top10", rank_top200_df),
        ]:
            if not sig_df.empty:
                print(f"\n[{label}]")
                print(sig_df.to_string(index=False))
    else:
        print("\n[순위상승 신호] 데이터 부족 — 캐시 28개월치 필요")

    # ── 11. 모멘텀 필터 ───────────────────────────────────────────────
    pos = cur[
        (cur["ret_6m"] > 0) &
        (cur["ret_12m"].notna() & (cur["ret_12m"] > 0) | cur["ret_12m"].isna()) &
        (cur["amt_accel"] > 0) & cur["amt_accel"].notna()
    ].copy()

    relaxed = False
    if len(pos) < 20:
        pos = cur[(cur["ret_6m"] > 0) & (cur["ret_3m"] > 0)].copy()
        relaxed = True
        print("[안내] 조건 완화")

    if pos.empty:
        print("[경고] 모멘텀 양수 종목 없음"); return

    # ── 12. 점수 산출 ─────────────────────────────────────────────────
    def pct_rank(s: pd.Series, ascending: bool = True) -> pd.Series:
        return s.rank(pct=True, ascending=ascending, method="average")

    pos = pos.copy()
    pos["pr24"]    = pct_rank(pos["ret_24m"].fillna(pos["ret_12m"].fillna(pos["ret_6m"])))
    pos["pr12"]    = pct_rank(pos["ret_12m"].fillna(pos["ret_6m"]))
    pos["pr6"]     = pct_rank(pos["ret_6m"])
    pos["pr3"]     = pct_rank(pos["ret_3m"].fillna(0))
    pos["pr_cons"] = pct_rank(pos["consistency"].fillna(0.5))

    # 급발진 패널티: 3M/6M 비율 낮을수록 건강 → 낮은 비율이 높은 점수
    pos["accel_ratio"]       = (pos["ret_3m"].fillna(0) / pos["ret_6m"].replace(0, float("nan"))).clip(0, 5)
    pos["pr_accel_penalty"]  = pct_rank(pos["accel_ratio"], ascending=False)

    # 거래대금 과열 클램프 (80% 초과분 이득 없음)
    pos["amt_accel_capped"]  = pos["amt_accel"].clip(upper=0.8)
    pos["pr_amt_capped"]     = pct_rank(pos["amt_accel_capped"])

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

    # 시총 계산
    out["Marcap_B"] = (out["Price"] * out["SharesHeld"].fillna(0) / 1e9).round(1)

    # 섹터 편중 경고
    if "Sector" in out.columns:
        for sector, cnt in out.head(20)["Sector"].value_counts().items():
            if cnt > 5 and sector not in ("-", "", None):
                print(f"[주의] 섹터 편중: 상위 20위 중 '{sector}' {cnt}개")

    # ── 13. 출력 포맷 ─────────────────────────────────────────────────
    out["매수순위"]           = range(1, len(out) + 1)
    out["현재가($)"]          = out["Price"].round(2)
    out["시총($B)"]           = out["Marcap_B"]
    out["S&P500비중(%)"]      = out["Weight"].round(3) if "Weight" in out.columns else None
    out["수익률_24M(%)"]      = (out["ret_24m"] * 100).round(1)
    out["수익률_12M(%)"]      = (out["ret_12m"] * 100).round(1)
    out["수익률_6M(%)"]       = (out["ret_6m"]  * 100).round(1)
    out["수익률_3M(%)"]       = (out["ret_3m"]  * 100).round(1)
    out["거래대금가속_2M(%)"]  = (out["amt_accel"] * 100).round(1) if not relaxed else None
    out["모멘텀일관성(12M)"]   = out["consistency"].round(2)
    out["변동성_CV"]           = out["vol_cv"].round(3) if not relaxed else None
    out["모멘텀점수"]          = out["score"].round(4)
    out["급발진비율(3M/6M)"]   = out["accel_ratio"].round(2)

    # 데이터 신뢰도: 24M 있으면 ★★★, 12M만 있으면 ★★, 6M만 있으면 ★
    def _reliability(row):
        if pd.notna(row.get("ret_24m")) and row.get("ret_24m") != 0:
            return "★★★ (24M)"
        elif pd.notna(row.get("ret_12m")) and row.get("ret_12m") != 0:
            return "★★ (12M)"
        else:
            return "★ (6M)"
    out["데이터신뢰도"] = out.apply(_reliability, axis=1)

    # 과열 신호 판정
    cv_70pct = out["vol_cv"].quantile(0.7) if "vol_cv" in out.columns else 9999

    def _overheat(row):
        sigs = []
        if row.get("accel_ratio", 0) > 1.0:                       sigs.append("급발진")
        if (row.get("amt_accel", 0) or 0) > 0.8:                  sigs.append("수급과열")
        if (row.get("vol_cv", 0) or 0) > cv_70pct:                sigs.append("고변동")
        n = len(sigs)
        if n == 0:   return "✓ 건강"
        elif n == 1: return f"△ 주의({sigs[0]})"
        else:        return f"⚠ 과열({'·'.join(sigs)})"

    out["과열신호"] = out.apply(_overheat, axis=1)

    export_cols = [
        "매수순위","Code","Name","Sector",
        "시총($B)","S&P500비중(%)","Rank_M",
        "현재가($)",
        "수익률_24M(%)","수익률_12M(%)","수익률_6M(%)","수익률_3M(%)",
        "급발진비율(3M/6M)","거래대금가속_2M(%)","모멘텀일관성(12M)","변동성_CV",
        "모멘텀점수","과열신호","데이터신뢰도",
    ]
    export = out[[c for c in export_cols if c in out.columns]].rename(columns={
        "Code":"티커","Name":"종목명","Sector":"섹터","Rank_M":"시총순위",
    })

    # 건강한 종목 필터 (top80 중, CV 하드상한 포함)
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

    # 꾸준한 종목 — 전체 유니버스에서 탐색
    cur_accel_ratio_us = (cur["ret_3m"].fillna(0) / cur["ret_6m"].replace(0, float("nan"))).clip(0, 5)
    steady_mask_us = (
        (cur["consistency"].fillna(0)    >= 0.7) &
        (cur["ret_12m"].fillna(0)        >  0  ) &
        (cur["ret_6m"].fillna(0)         >  0  ) &
        (cur_accel_ratio_us.fillna(99)   <= 1.0) &
        (cur["amt_accel"].fillna(0)      <= 0.8) &
        (cur["vol_cv"].fillna(999)       <= CV_HEALTHY_MAX)
    )
    steady_cur_us = cur[steady_mask_us].copy()
    if not steady_cur_us.empty:
        steady_cur_us["accel_ratio_s"] = cur_accel_ratio_us[steady_mask_us].values
        steady_cur_us["꾸준함점수"] = (
            steady_cur_us["consistency"] *
            steady_cur_us["ret_12m"].fillna(steady_cur_us["ret_6m"]) /
            steady_cur_us["vol_cv"].replace(0, 0.01)
        ).round(3)
        steady_cur_us = steady_cur_us.sort_values("꾸준함점수", ascending=False).head(50)
        steady_cur_us["현재가($)"]       = steady_cur_us["Price"].round(2)
        steady_cur_us["시총($B)"]        = (steady_cur_us["Price"] * steady_cur_us["SharesHeld"].fillna(0) / 1e9).round(1)
        steady_cur_us["수익률_24M(%)"]   = (steady_cur_us["ret_24m"] * 100).round(1)
        steady_cur_us["수익률_12M(%)"]   = (steady_cur_us["ret_12m"] * 100).round(1)
        steady_cur_us["수익률_6M(%)"]    = (steady_cur_us["ret_6m"]  * 100).round(1)
        steady_cur_us["수익률_3M(%)"]    = (steady_cur_us["ret_3m"]  * 100).round(1)
        steady_cur_us["모멘텀일관성(12M)"] = steady_cur_us["consistency"].round(2)
        steady_cur_us["변동성_CV"]       = steady_cur_us["vol_cv"].round(3)
        steady_cur_us["급발진비율(3M/6M)"] = steady_cur_us["accel_ratio_s"].round(2)
        us_s_cols = [
            "Code","Name","Sector","시총($B)","Rank_M","현재가($)",
            "수익률_24M(%)","수익률_12M(%)","수익률_6M(%)","수익률_3M(%)",
            "급발진비율(3M/6M)","모멘텀일관성(12M)","변동성_CV","꾸준함점수",
        ]
        steady_us_df = steady_cur_us[
            [c for c in us_s_cols if c in steady_cur_us.columns]
        ].rename(columns={
            "Code":"티커","Name":"종목명","Sector":"섹터","Rank_M":"시총순위"
        }).reset_index(drop=True)
        steady_us_df.insert(0, "꾸준함순위", range(1, len(steady_us_df) + 1))
    else:
        steady_us_df = pd.DataFrame()

    # ── 14. 산출조건 ─────────────────────────────────────────────────
    method_df = pd.DataFrame([
        ("기준일",                asof_str),
        ("투자 철학",             "패러다임 투자 — 장기 보유. 이미 많이 오른 종목도 배제 안 함."),
        ("유니버스",              f"S&P500 (SSGA SPY 홀딩스 상위 {UNIVERSE_N}종)"),
        ("시총 계산",             "Close × SharesHeld — 진짜 시총 기반. 캐시 파일로 자동 히스토리."),
        ("순위 히스토리",         "캐시 28개월치 → 월별 시총 순위 자동 계산. 매달 실행 불필요."),
        ("과열신호",              "급발진(3M>6M) + 수급과열(거래대금>80%) + 고변동(CV상위30%)"),
        ("✓ 건강",                "3가지 모두 해당 없음"),
        ("△ 주의",                "1가지 해당 — 모니터링"),
        ("⚠ 과열",                "2가지 이상 — 눌림 대기 or 비중 축소 고려"),
        ("건강한종목 기준",       f"일관성≥0.7, 12M·6M양수, 3M≤6M, 거래대금가속≤80%, CV≤{CV_HEALTHY_MAX}"),
        ("꾸준한종목 시트",        "전체 유니버스에서 건강 필터 후 꾸준함점수(일관성×12M/CV) 정렬"),
        ("---",                   "---"),
        ("수익률_24M (가중 3.0)", "2년 주가 상승률 — 패러다임 핵심"),
        ("수익률_12M (가중 2.0)", "1년 주가 상승률"),
        ("수익률_6M  (가중 1.5)", "6개월"),
        ("수익률_3M  (가중 0.5)", "3개월 — 참고용"),
        ("거래대금가속 (가중 1.0)", "최근 2M vs 이전 2M (80% 초과 클램프)"),
        ("모멘텀일관성 (가중 1.5)", "12개월 중 상승한 달 비율"),
        ("변동성패널티 (가중 1.0)", "12개월 가격 CV — 낮을수록 우대"),
        ("급발진패널티 (가중 1.0)", "3M/6M 비율 — 낮을수록 건강한 추세"),
        ("시총($B)",              "10억달러. 100B ≈ 140조원"),
    ], columns=["항목","설명"])

    # ── 15. 저장 ─────────────────────────────────────────────────────
    # 섹터별 요약 (저장 전 미리 계산)
    sector_summary_df = pd.DataFrame()
    if "섹터" in export.columns and (export["섹터"] != "-").any():
        try:
            sector_summary_df = (
                export[export["섹터"] != "-"]
                .groupby("섹터").agg(
                    종목수=("티커", "count"),
                    평균모멘텀점수=("모멘텀점수", "mean"),
                    평균6M수익률=("수익률_6M(%)", "mean"),
                    평균12M수익률=("수익률_12M(%)", "mean"),
                    건강종목수=("과열신호", lambda x: (x == "✓ 건강").sum()),
                )
                .round(2)
                .sort_values("평균모멘텀점수", ascending=False)
                .reset_index()
            )
        except Exception:
            pass

    tag = asof.strftime("%Y%m%d")
    for fname in [f"us_paradigm_{tag}.xlsx", "us_paradigm_latest.xlsx"]:
        path = root / fname
        with pd.ExcelWriter(path, engine="openpyxl") as w:
            export.to_excel(w,            sheet_name="모멘텀후보(매수순위)",      index=False)
            if not healthy_df.empty:
                healthy_df.to_excel(w,    sheet_name="건강한종목(과열제외)",      index=False)
            if not steady_us_df.empty:
                steady_us_df.to_excel(w,  sheet_name="꾸준한종목(전유니버스)",    index=False)
            if not paradigm_top20_df.empty:
                paradigm_top20_df.to_excel(w, sheet_name="패러다임_Top20신규진입", index=False)
            if not paradigm_top50_df.empty:
                paradigm_top50_df.to_excel(w, sheet_name="패러다임_Top50신규진입", index=False)
            if not rank_top50_df.empty:
                rank_top50_df.to_excel(w,  sheet_name="순위상승_Top50구간",       index=False)
            if not rank_top100_df.empty:
                rank_top100_df.to_excel(w, sheet_name="순위상승_Top100구간",      index=False)
            if not rank_top200_df.empty:
                rank_top200_df.to_excel(w, sheet_name="순위상승_Top200구간",      index=False)
            if not sector_summary_df.empty:
                sector_summary_df.to_excel(w, sheet_name="섹터별요약",            index=False)
            method_df.to_excel(w,         sheet_name="산출조건",                  index=False)

    print(f"\n저장: {root / f'us_paradigm_{tag}.xlsx'}")
    print(f"저장(고정명): {root / 'us_paradigm_latest.xlsx'}")

    sig_counts = export["과열신호"].value_counts()
    print(f"\n▶ 과열 신호 분포:")
    for sig, cnt in sig_counts.items():
        print(f"  {sig}: {cnt}개")
    print(f"  건강한종목(top80): {len(healthy_df)}개")
    print(f"  꾸준한종목(전유니버스): {len(steady_us_df)}개")

    print(f"\n▶ 매수 우선순위 상위 20:")
    print(export.head(20).to_string(index=False))

    if not healthy_df.empty:
        print(f"\n▶ 건강한 종목 (top80 중, CV≤{CV_HEALTHY_MAX}):")
        print(healthy_df.to_string(index=False))

    if not steady_us_df.empty:
        print(f"\n▶ 꾸준한 종목 Top20 (전유니버스, CV≤{CV_HEALTHY_MAX}):")
        print(steady_us_df.head(20).to_string(index=False))


def main() -> None:
    run_momentum_us()


if __name__ == "__main__":
    main()
