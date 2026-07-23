#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KRX 시가총액 데이터를 FinanceDataReader로 조회합니다.
KRX 직접 API 대신 FDR을 사용해 차단 문제를 완전히 우회합니다.

필요 패키지: pip install finance-datareader pandas pyarrow openpyxl
"""

from __future__ import annotations

import calendar
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

CACHE_DIR = Path(__file__).resolve().parent / "krx_cache"


# ── FDR 기반 시가총액 조회 ────────────────────────────────────────────────────

def _fetch_marcap_raw(date_str: str) -> pd.DataFrame:
    """
    FinanceDataReader로 KOSPI+KOSDAQ+KONEX 전 종목 시가총액 조회.
    반환 컬럼: Date, Code, Name, Market, Close, Volume, Amount, Marcap(백만원), Stocks, Rank
    """
    try:
        import FinanceDataReader as fdr
    except ImportError:
        raise ImportError("finance-datareader 없음. 설치: pip install finance-datareader")

    dfs = []
    for market in ("KOSPI", "KOSDAQ", "KONEX"):
        try:
            df = fdr.StockListing(market)
            if df is None or df.empty:
                continue

            df = df.reset_index(drop=True)

            # 컬럼명 정규화 (FDR 반환: Code, Name, Market, Close, Marcap, Stocks, Amount, Volume 등)
            col_map = {}
            for c in df.columns:
                cl = str(c).strip()
                if cl in ("Code", "code", "종목코드"):              col_map[c] = "Code"
                elif cl in ("Name", "name", "종목명"):              col_map[c] = "Name"
                elif cl in ("Marcap", "marcap", "시가총액"):        col_map[c] = "Marcap"
                elif cl in ("Volume", "volume", "거래량"):          col_map[c] = "Volume"
                elif cl in ("Amount", "amount", "거래대금"):        col_map[c] = "Amount"
                elif cl in ("Stocks", "stocks", "상장주식수"):      col_map[c] = "Stocks"
                elif cl in ("Close", "close", "종가"):              col_map[c] = "Close"
                elif cl in ("Market", "market", "시장"):            col_map[c] = "Market"

            df = df.rename(columns=col_map)

            # Market 컬럼 없으면 추가
            if "Market" not in df.columns:
                df["Market"] = market

            dfs.append(df)
            time.sleep(0.3)

        except Exception as e:
            print(f"  [{market}] 오류: {e}")
            continue

    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)
    return _finalize(df, date_str)


def _finalize(df: pd.DataFrame, date_str: str) -> pd.DataFrame:
    """숫자 변환, 단위 확인, 정렬, Rank 부여."""
    if "Code" not in df.columns:
        print(f"  [경고] Code 컬럼 없음. 컬럼: {df.columns.tolist()}")
        return pd.DataFrame()

    for col in ("Marcap", "Amount", "Volume", "Stocks", "Close"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "Marcap" not in df.columns:
        print(f"  [경고] Marcap 없음. 컬럼: {df.columns.tolist()}")
        return pd.DataFrame()

    # FDR의 Marcap은 원(KRW) 단위 → 백만원으로 변환
    # Marcap > 1조(1e12)면 원 단위, 이미 백만원이면 그대로
    sample = df["Marcap"].dropna()
    if not sample.empty and sample.median() > 1e10:
        df["Marcap"] = df["Marcap"] / 1_000_000.0   # 원 → 백만원

    if "Amount" not in df.columns: df["Amount"] = 0.0
    if "Close"  not in df.columns: df["Close"]  = float("nan")
    if "Name"   not in df.columns: df["Name"]   = ""

    df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce").fillna(0.0)
    df["Date"]   = pd.to_datetime(date_str, format="%Y%m%d")

    df = df.dropna(subset=["Marcap"])
    df = df[df["Marcap"] > 0]
    df = df.sort_values("Marcap", ascending=False).reset_index(drop=True)
    df["Rank"] = range(1, len(df) + 1)
    return df


# ── 거래일 탐색 ──────────────────────────────────────────────────────────────

def _last_trading_day(year: int, month: int) -> str | None:
    """월말 기준 마지막 거래일(YYYYMMDD) 반환."""
    last_day = calendar.monthrange(year, month)[1]
    for delta in range(10):
        d = date(year, month, last_day) - timedelta(days=delta)
        if d.month != month:
            break
        if d.weekday() >= 5:   # 토/일 skip
            continue
        return d.strftime("%Y%m%d")
    return None


# ── 공개 API ─────────────────────────────────────────────────────────────────

def krx_monthly_data(start_date: str, end_date: str) -> pd.DataFrame:
    """
    월말 기준 KRX 전체 종목 시가총액 데이터를 반환합니다.

    주의: FDR StockListing은 '오늘 현재' 데이터만 반환합니다.
    과거 특정 날짜 데이터가 필요하면 marcap GitHub 저장소를 사용하세요.

    캐시: krx_cache/YYYY-MM.parquet (오늘 날짜 것만 저장)
    반환 컬럼: Date, Code, Name, Marcap(백만원), Amount, Volume, Stocks, Rank, Market
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    periods      = pd.period_range(start=start_date, end=end_date, freq="M")
    today_period = pd.Period(date.today(), "M")
    all_dfs: list[pd.DataFrame] = []
    total = len(periods)

    for i, period in enumerate(periods):
        cache_file = CACHE_DIR / f"{period}.parquet"

        # 과거 월 캐시 로드
        if cache_file.exists() and period < today_period:
            print(f"  [{i+1}/{total}] {period} 캐시 로드")
            all_dfs.append(pd.read_parquet(cache_file))
            continue

        # 오늘 이전 달은 FDR로 과거 데이터 가져오기 불가
        # → 해당 월 캐시 없으면 skip (marcap GitHub 사용 권장)
        if period < today_period:
            print(f"  [{i+1}/{total}] {period} — 과거 데이터 없음 (marcap GitHub 사용)")
            continue

        # 이번 달: 오늘 현재 데이터 조회
        year, month = period.year, period.month
        date_str = _last_trading_day(year, month) or date.today().strftime("%Y%m%d")

        print(f"  [{i+1}/{total}] {date_str} FDR 조회 중...")
        try:
            df_month = _fetch_marcap_raw(date_str)
        except Exception as e:
            print(f"  [{i+1}/{total}] 오류: {e}")
            continue

        if df_month.empty:
            print(f"  [{i+1}/{total}] 데이터 없음")
            continue

        print(f"  [{i+1}/{total}] 완료 — 종목 {len(df_month)}개")
        df_month.to_parquet(cache_file, index=False)
        all_dfs.append(df_month)

    if not all_dfs:
        raise RuntimeError("조회된 데이터가 없습니다.")

    result = pd.concat(all_dfs, ignore_index=True)
    result["Date"] = pd.to_datetime(result["Date"])
    return result


def get_today_marcap() -> pd.DataFrame:
    """
    오늘 현재 KOSPI+KOSDAQ+KONEX 전 종목 시가총액을 반환합니다.
    크론 등으로 매일 실행해서 최신 데이터 확보에 사용합니다.
    """
    today_str = date.today().strftime("%Y%m%d")
    print(f"FDR로 오늘({today_str}) 시가총액 조회 중...")
    df = _fetch_marcap_raw(today_str)
    if not df.empty:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        ym = date.today().strftime("%Y-%m")
        cache_file = CACHE_DIR / f"{ym}.parquet"
        df.to_parquet(cache_file, index=False)
        print(f"저장: {cache_file} ({len(df)}종목)")
    return df


def clear_cache(before: str | None = None) -> None:
    for f in CACHE_DIR.glob("*.parquet"):
        if before is None or f.stem <= before:
            f.unlink()
            print(f"삭제: {f}")


# ── 단독 실행 테스트 ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("=== FDR StockListing 테스트 ===")
    try:
        import FinanceDataReader as fdr
        df_test = fdr.StockListing("KOSPI")
        print(f"KOSPI 컬럼: {df_test.columns.tolist()}")
        # Marcap 컬럼 확인
        marcap_col = next((c for c in df_test.columns if "arcap" in c or "시가총액" in c), None)
        if marcap_col:
            df_test = df_test.rename(columns={marcap_col: "Marcap"})
            print(f"Marcap 샘플 (원 단위): {df_test['Marcap'].dropna().head(3).values}")
            print(f"종목수: {len(df_test)}")
        else:
            print(f"Marcap 컬럼 없음. 컬럼: {df_test.columns.tolist()}")
    except Exception as e:
        print(f"오류: {e}")
        sys.exit(1)

    print()
    print("=== 오늘 시가총액 조회 ===")
    df = get_today_marcap()
    if not df.empty:
        print(f"성공! 종목수: {len(df)}")
        print(f"컬럼: {df.columns.tolist()}")
        print(df[["Code", "Name", "Marcap", "Rank"]].head(10).to_string(index=False))
    else:
        print("실패")

    # marcap 단위 확인
    if not df.empty:
        top1_marcap = df["Marcap"].iloc[0]
        print(f"\n1위 시총: {top1_marcap:,.0f} 백만원 = {top1_marcap/1e6:.1f}조원")
