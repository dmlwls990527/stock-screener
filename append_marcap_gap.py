#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
append_marcap_gap.py — marcap-2026.parquet의 공백 구간(6/24~오늘)을 pykrx로 채움.
기존 로컬 수정본(6/24 이전, namefix/unitsfix/zerofix 적용분)은 절대 건드리지 않고
새 날짜 행만 뒤에 추가한다. git pull 은 하지 않음.
"""
import sys
import time
from datetime import date, timedelta

import pandas as pd
from pykrx import stock as krx

PARQUET = "/data/frame/marcap/data/marcap-2026.parquet"
MARKETS = ["KOSPI", "KOSDAQ", "KONEX"]
DELAY = 0.15
COLUMNS = ["Code", "Name", "Close", "Dept", "ChangeCode", "Changes", "ChangesRatio",
           "Volume", "Amount", "Open", "High", "Low", "Marcap", "Stocks",
           "Market", "MarketId", "Rank", "Date"]

name_cache = {}


def get_name(code):
    if code not in name_cache:
        try:
            name_cache[code] = krx.get_market_ticker_name(code)
        except Exception:
            name_cache[code] = None
        time.sleep(0.02)
    return name_cache[code]


def fetch_one_day(ds, dt):
    """ds: 'YYYYMMDD', dt: date 객체. 3개 시장 합쳐서 하나의 DataFrame 반환 (없으면 None)."""
    frames = []
    for market in MARKETS:
        df_p = krx.get_market_ohlcv_by_ticker(ds, market=market)
        time.sleep(DELAY)
        if df_p is None or df_p.empty:
            continue
        df_p = df_p[(df_p["종가"] > 0) | (df_p["거래량"] > 0)]  # 휴장일 0값 방어
        if df_p.empty:
            continue
        df_m = krx.get_market_cap_by_ticker(ds, market=market)
        time.sleep(DELAY)
        if df_m is None or df_m.empty:
            continue
        df_m = df_m[df_m["시가총액"] > 0]

        df_p = df_p.drop(columns=["시가총액"], errors="ignore")  # df_m 쪽 값을 쓸 것이므로 중복 제거
        df_p.index.name = "Code"
        df_m.index.name = "Code"
        merged = df_p.reset_index().merge(df_m.reset_index()[["Code", "시가총액", "상장주식수"]],
                                           on="Code", how="inner")
        merged["Market"] = market
        frames.append(merged)

    if not frames:
        return None
    day_df = pd.concat(frames, ignore_index=True)
    day_df = day_df.sort_values("시가총액", ascending=False).reset_index(drop=True)
    day_df["Rank"] = range(1, len(day_df) + 1)

    out = pd.DataFrame({
        "Code":         day_df["Code"],
        "Name":         day_df["Code"].map(get_name),
        "Close":        day_df["종가"].astype(float),
        "Dept":         None,
        "ChangeCode":   None,
        "Changes":      float("nan"),
        "ChangesRatio": day_df["등락률"].astype(float),
        "Volume":       day_df["거래량"].astype(float),
        "Amount":       day_df["거래대금"].astype(float),
        "Open":         day_df["시가"].astype(float),
        "High":         day_df["고가"].astype(float),
        "Low":          day_df["저가"].astype(float),
        "Marcap":       day_df["시가총액"].astype(float),
        "Stocks":       day_df["상장주식수"].astype("int64"),
        "Market":       day_df["Market"],
        "MarketId":     None,
        "Rank":         day_df["Rank"].astype("int64"),
        "Date":         pd.Timestamp(dt),
    })
    return out[COLUMNS]


def main():
    test_mode = "--test" in sys.argv
    existing = pd.read_parquet(PARQUET)
    last_date = existing["Date"].max().date()
    print(f"기존 데이터 마지막 날짜: {last_date}")

    end_date = date.today()
    dates = []
    d = last_date + timedelta(days=1)
    while d <= end_date:
        if d.weekday() < 5:  # 평일만 (주말 제외, 공휴일은 fetch_one_day에서 자연 스킵)
            dates.append(d)
        d += timedelta(days=1)

    if test_mode:
        dates = dates[:2]
        print(f"[TEST MODE] {dates}")

    print(f"채울 날짜 {len(dates)}개: {dates[0] if dates else 'N/A'} ~ {dates[-1] if dates else 'N/A'}")

    new_frames = []
    for i, d in enumerate(dates, 1):
        ds = d.strftime("%Y%m%d")
        df = fetch_one_day(ds, d)
        if df is None:
            print(f"[{i}/{len(dates)}] {ds}  휴장일 또는 데이터 없음 (스킵)")
            continue
        new_frames.append(df)
        print(f"[{i}/{len(dates)}] {ds}  {len(df)}종목 수집", flush=True)

    if not new_frames:
        print("추가할 데이터 없음 (전부 휴장일이거나 이미 최신)")
        return

    new_df = pd.concat(new_frames, ignore_index=True)
    combined = pd.concat([existing, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["Code", "Date"], keep="last")
    combined = combined.sort_values(["Date", "Rank"]).reset_index(drop=True)

    if test_mode:
        print(f"[TEST MODE] 저장하지 않음. 추가됐을 행수: {len(new_df)}, 최종 총행수: {len(combined)}")
        return

    combined.to_parquet(PARQUET, index=False)
    print(f"저장 완료: {PARQUET}  (기존 {len(existing):,}행 + 신규 {len(new_df):,}행 = 총 {len(combined):,}행)")
    print(f"새 날짜범위: {combined['Date'].min()} ~ {combined['Date'].max()}")


if __name__ == "__main__":
    main()
