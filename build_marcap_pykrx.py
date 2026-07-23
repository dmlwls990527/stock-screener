#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pykrx로 marcap 형식 데이터셋 구축/업데이트.
기존 GitHub marcap clone 완전 대체.

초기 구축 (전체):
  python3 build_marcap_pykrx.py --start 20230101

일별 업데이트 (마지막 날짜 이후 자동):
  python3 build_marcap_pykrx.py

cron 예시 (매일 오후 7시 — 장 마감 후):
  0 19 * * 1-5 /usr/bin/python3 /data/frame/build_marcap_pykrx.py >> /data/frame/logs/build_marcap.log 2>&1
"""

from __future__ import annotations

import argparse
import os
import time
import sys
from datetime import date
from pathlib import Path

import pandas as pd

try:
    from pykrx import stock as krx
except ImportError:
    print("pip install pykrx")
    sys.exit(1)

# ── 설정 ──────────────────────────────────────────────────────────────────────
MARCAP_DIR        = Path(os.environ.get("MARCAP_DIR", "/data/frame/marcap/data"))
START_DATE_DEFAULT = "20230101"
DELAY             = 0.5   # API 호출 간격(초) — 너무 빠르면 KRX 차단
DELAY_DAY         = 0.2   # 날짜 간격 추가 대기


# ── 하루치 수집 ───────────────────────────────────────────────────────────────

_NAME_CACHE: dict = {}

def _get_name(code: str):
    """종목명 캐시 조회 (get_market_cap_by_ticker가 종목명 미반환 → 보강용)."""
    if code not in _NAME_CACHE:
        try:
            _NAME_CACHE[code] = krx.get_market_ticker_name(code)
        except Exception:
            _NAME_CACHE[code] = None
    return _NAME_CACHE[code]


def fetch_day(dt_str: str) -> pd.DataFrame | None:
    """
    YYYYMMDD 문자열로 하루치 KOSPI+KOSDAQ 전종목 데이터 수집.
    휴장일(공휴일/주말)이면 None 반환.
    """
    frames = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            # 시가총액 (종목명 포함)
            cap = krx.get_market_cap_by_ticker(dt_str, market=market)
            time.sleep(DELAY)
            if cap is None or cap.empty:
                continue

            # OHLCV + 등락률
            ohlcv = krx.get_market_ohlcv_by_ticker(dt_str, market=market)
            time.sleep(DELAY)
            if ohlcv is None or ohlcv.empty:
                continue

            # 필요 컬럼만 선택
            cap_cols   = [c for c in ["종목명","시가총액","상장주식수"] if c in cap.columns]
            ohlcv_cols = [c for c in ["시가","고가","저가","종가","거래량","거래대금","등락률"] if c in ohlcv.columns]

            df = cap[cap_cols].join(ohlcv[ohlcv_cols], how="inner")
            df.index.name = "Code"
            df = df.reset_index()
            df["Market"] = market
            frames.append(df)

        except Exception as e:
            print(f"  [{dt_str} {market}] 오류: {e}")

    if not frames:
        return None

    df = pd.concat(frames, ignore_index=True)

    # 컬럼 이름 통일 (기존 marcap 스키마 호환)
    df = df.rename(columns={
        "종목명":   "Name",
        "시가":     "Open",
        "고가":     "High",
        "저가":     "Low",
        "종가":     "Close",
        "거래량":   "Volume",
        "거래대금": "Amount",
        "등락률":   "ChangesRatio",
        "시가총액": "Marcap",
        "상장주식수":"Stocks",
    })

    # 단위 변환: 원 → 백만원 (기존 marcap과 동일)
    if "Amount" in df.columns:
        df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce").round(0)
    if "Marcap" in df.columns:
        df["Marcap"] = pd.to_numeric(df["Marcap"], errors="coerce").round(0)

    # 종목명 보강 (get_market_cap_by_ticker가 종목명을 반환하지 않아 누락 → ticker_name 조회)
    if "Name" not in df.columns:
        df["Name"] = None
    _miss = df["Name"].isna()
    if _miss.any():
        df.loc[_miss, "Name"] = df.loc[_miss, "Code"].map(_get_name)

    df["Date"] = pd.to_datetime(dt_str, format="%Y%m%d")

    # 휴장일·당일 미확정(Marcap 0/결측) 행 제거 — run_etl.py KR 필터와 동일 취지
    df = df[df["Marcap"].notna() & (df["Marcap"] > 0)]
    if df.empty:
        return None

    # 전체 시총 기준 합산 순위
    df = df.sort_values("Marcap", ascending=False).reset_index(drop=True)
    df["Rank"] = range(1, len(df) + 1)

    return df


# ── 연도별 구축/업데이트 ──────────────────────────────────────────────────────

def build_or_update(start: str, end: str) -> None:
    MARCAP_DIR.mkdir(parents=True, exist_ok=True)

    start_ts = pd.to_datetime(start, format="%Y%m%d")
    end_ts   = pd.to_datetime(end,   format="%Y%m%d")

    for year in range(start_ts.year, end_ts.year + 1):
        year_start = max(start_ts, pd.Timestamp(f"{year}-01-01"))
        year_end   = min(end_ts,   pd.Timestamp(f"{year}-12-31"))

        pq_path = MARCAP_DIR / f"marcap-{year}.parquet"

        # 기존 파일 로드 → 마지막 날짜 이후부터만 수집
        existing  = pd.DataFrame()
        fetch_from = year_start

        if pq_path.exists():
            existing = pd.read_parquet(pq_path)
            existing["Date"] = pd.to_datetime(existing["Date"])
            last_dt = existing["Date"].max()
            if last_dt >= year_end:
                print(f"  {year}: 이미 최신 ({last_dt.date()}) — 스킵")
                continue
            fetch_from = last_dt + pd.Timedelta(days=1)
            print(f"  {year}: {last_dt.date()} 이후부터 수집")
        else:
            print(f"  {year}: 전체 수집 ({year_start.date()} ~ {year_end.date()})")

        # 평일만 (KRX 공휴일은 빈 응답으로 자동 스킵)
        date_range = pd.bdate_range(start=fetch_from, end=year_end)
        if len(date_range) == 0:
            print(f"  {year}: 수집할 날짜 없음")
            continue

        day_frames = []
        for i, dt in enumerate(date_range):
            dt_str = dt.strftime("%Y%m%d")
            sys.stdout.write(f"\r  {year}: {i+1}/{len(date_range)} — {dt_str}     ")
            sys.stdout.flush()

            day_df = fetch_day(dt_str)
            if day_df is not None and not day_df.empty:
                day_frames.append(day_df)

            time.sleep(DELAY_DAY)

        print()  # 줄바꿈

        if not day_frames:
            print(f"  {year}: 수집된 데이터 없음 (전부 휴장일)")
            continue

        new_data = pd.concat(day_frames, ignore_index=True)

        if not existing.empty:
            combined = pd.concat([existing, new_data], ignore_index=True)
            combined = combined.drop_duplicates(subset=["Date","Code"], keep="last")
            combined = combined.sort_values(["Date","Rank"]).reset_index(drop=True)
        else:
            combined = new_data

        combined.to_parquet(pq_path, index=False)
        min_d = combined["Date"].min().date()
        max_d = combined["Date"].max().date()
        print(f"  {year}: {len(combined):,}행 저장 ({min_d} ~ {max_d})")


# ── 구버전 marcap git 파일 삭제 ───────────────────────────────────────────────

def remove_old_marcap_git(marcap_root: Path) -> None:
    """
    GitHub clone 된 marcap 저장소의 parquet 파일만 삭제.
    (git 저장소 자체는 유지 — 필요시 수동으로 rm -rf)
    """
    data_dir = marcap_root / "data"
    if not data_dir.exists():
        return
    removed = 0
    for f in sorted(data_dir.glob("marcap-*.parquet")):
        # 새로 만든 파일은 MARCAP_DIR 아래에 있으므로 충돌 없음
        if f.parent.resolve() != MARCAP_DIR.resolve():
            f.unlink()
            removed += 1
            print(f"  삭제: {f}")
    if removed:
        print(f"  총 {removed}개 구버전 파일 삭제 완료")
    else:
        print("  삭제할 구버전 파일 없음 (경로 확인 필요)")


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="pykrx marcap 데이터셋 구축/업데이트")
    parser.add_argument("--start", default=START_DATE_DEFAULT,
                        help=f"시작일 YYYYMMDD (기본: {START_DATE_DEFAULT})")
    parser.add_argument("--end",   default=date.today().strftime("%Y%m%d"),
                        help="종료일 YYYYMMDD (기본: 오늘)")
    parser.add_argument("--clean", action="store_true",
                        help="구버전 marcap git 파일 삭제 후 진행")
    args = parser.parse_args()

    print(f"=== marcap 데이터셋 구축 ===")
    print(f"  출력 디렉토리: {MARCAP_DIR}")
    print(f"  수집 기간: {args.start} ~ {args.end}")

    if args.clean:
        old_root = MARCAP_DIR.parent  # /data/frame/marcap
        print(f"\n구버전 파일 삭제 중: {old_root}/data/")
        remove_old_marcap_git(old_root)

    print()
    build_or_update(args.start, args.end)
    print("\n완료.")


if __name__ == "__main__":
    main()
