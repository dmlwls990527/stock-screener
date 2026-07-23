#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
공통 유틸리티
screen_momentum / screen_sector / screen_theme 에서 공유합니다.
"""

from __future__ import annotations

import os
import re
import time
from datetime import date
from pathlib import Path

import pandas as pd

# ── 공통 상수 ─────────────────────────────────────────────────────────────────
_DEFAULT_MARCAP_DIR = Path("/data/frame/marcap/data")
MARCAP_DIR = Path(os.environ.get("MARCAP_DIR", str(_DEFAULT_MARCAP_DIR)))

START_DATE = "2022-01-01"
END_DATE   = min(date.today(), date(2026, 12, 31)).strftime("%Y-%m-%d")


# ── 공통 함수 ─────────────────────────────────────────────────────────────────

def load_marcap_data(start: str, end: str) -> pd.DataFrame:
    """
    marcap-YYYY.parquet 파일에서 start~end 구간 데이터를 로드합니다.
    컬럼: Date, Code, Name, Market, Close, Volume, Amount(백만원),
          Marcap(백만원), Stocks, Rank
    """
    start_year = pd.to_datetime(start).year
    end_year   = pd.to_datetime(end).year

    frames = []
    for year in range(start_year, end_year + 1):
        pq = MARCAP_DIR / f"marcap-{year}.parquet"
        if not pq.exists():
            print(f"  [경고] {pq} 없음 — 건너뜀")
            continue
        frames.append(pd.read_parquet(pq))

    if not frames:
        raise FileNotFoundError(
            f"marcap parquet 파일을 찾을 수 없습니다: {MARCAP_DIR}\n"
            "MARCAP_DIR 환경변수로 경로를 지정하거나 서버에서 실행하세요."
        )

    df = pd.concat(frames, ignore_index=True)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df[(df["Date"] >= start) & (df["Date"] <= end)]

    for col in ("Code", "Name", "Marcap", "Rank"):
        if col not in df.columns:
            raise ValueError(
                f"marcap 데이터에 '{col}' 컬럼이 없습니다. 컬럼: {df.columns.tolist()}"
            )

    if "Amount" not in df.columns:
        df["Amount"] = 0.0

    return df


def marcap_to_choeok(s: pd.Series) -> pd.Series:
    """시가총액 백만원 → 조원 변환."""
    return s / 1_000_000.0


# ── PER/PBR/EPS/DIV 펀더멘털 데이터 ──────────────────────────────────────────

_NAVER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_FUND_CACHE_DIR = Path(__file__).resolve().parent / "fundamental_cache"


def _parse_naver_fundamental(code: str, session) -> dict:
    """NAVER Finance에서 종목 1개의 PER/PBR/EPS/BPS/DIV 파싱."""
    try:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        r = session.get(url, timeout=8)
        t = r.text

        def _val(label: str) -> float | None:
            idx = t.find(label)
            if idx == -1:
                return None
            chunk = t[idx: idx + 1500]   # 배당수익률은 em 태그가 ~800자 이후에 위치
            m = re.search(r"<em[^>]*>([\d,]+\.?\d*)</em>", chunk)
            if m:
                try:
                    return float(m.group(1).replace(",", ""))
                except ValueError:
                    return None
            # 테이블 td 안의 숫자 fallback
            m2 = re.search(r">\s*([\d,]+\.?\d+)\s*<", chunk)
            if m2:
                try:
                    return float(m2.group(1).replace(",", ""))
                except ValueError:
                    return None
            return None

        return {
            "Code": code,
            "PER":  _val("PER(배)"),
            "EPS":  _val("EPS(원)"),
            "PBR":  _val("PBR(배)"),
            "BPS":  _val("BPS(원)"),
            "DIV":  _val("배당수익률"),
        }
    except Exception:
        return {"Code": code, "PER": None, "EPS": None, "PBR": None, "BPS": None, "DIV": None}


def _load_fundamental_pykrx(date_str: str) -> pd.DataFrame:
    """pykrx로 전 종목 PER/PBR/EPS 로드 (KRX_ID / KRX_PW 환경변수 필요)."""
    try:
        from pykrx import stock as krx
    except ImportError:
        raise ImportError("pip install pykrx")

    dfs = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            df = krx.get_market_fundamental(date_str, market=market)
            if df is None or df.empty:
                continue
            df = df.reset_index()
            # 인덱스 컬럼명이 '티커' 또는 'Code'일 수 있음
            ticker_col = next(
                (c for c in df.columns if c in ("티커", "Code", "code")), None
            )
            if ticker_col and ticker_col != "Code":
                df = df.rename(columns={ticker_col: "Code"})
            dfs.append(df)
        except Exception as e:
            print(f"  [pykrx {market}] 오류: {e}")

    if not dfs:
        return pd.DataFrame()

    result = pd.concat(dfs, ignore_index=True)
    return result[["Code", "PER", "PBR", "EPS", "BPS", "DIV", "DPS"]
                  if "DPS" in result.columns else
                  [c for c in ("Code", "PER", "PBR", "EPS", "BPS", "DIV") if c in result.columns]]


def load_fundamental_data(
    date_str: str | None = None,
    codes: list[str] | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    PER/PBR/EPS/BPS/DIV 펀더멘털 데이터 로드.

    우선순위:
      1) 캐시 파일 (당일)
      2) pykrx  (환경변수 KRX_ID / KRX_PW 설정 시)
      3) NAVER Finance 스크래핑 (자동 폴백)

    Parameters
    ----------
    date_str : YYYYMMDD 문자열 (None 이면 오늘)
    codes    : 종목코드 리스트 (None 이면 전 종목 — pykrx 경로만 해당)
    use_cache: True면 캐시 우선 사용

    Returns
    -------
    DataFrame: Code, PER, PBR, EPS, BPS, DIV
    """
    if date_str is None:
        date_str = date.today().strftime("%Y%m%d")

    _FUND_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _FUND_CACHE_DIR / f"fund_{date_str}.parquet"

    # ── 1. 캐시 ──────────────────────────────────────────────
    if use_cache and cache_file.exists():
        print(f"  펀더멘털 캐시 로드: {cache_file.name}")
        return pd.read_parquet(cache_file)

    # ── 2. pykrx ─────────────────────────────────────────────
    if os.environ.get("KRX_ID") and os.environ.get("KRX_PW"):
        try:
            print(f"  pykrx 펀더멘털 조회: {date_str}")
            df = _load_fundamental_pykrx(date_str)
            if not df.empty:
                df.to_parquet(cache_file, index=False)
                print(f"  pykrx 완료: {len(df)}개 종목 저장")
                return df
        except Exception as e:
            print(f"  [경고] pykrx 실패 → NAVER 폴백: {e}")
    else:
        print("  KRX_ID/KRX_PW 미설정 → NAVER 스크래핑 사용")
        print("  (서버에서: export KRX_ID=아이디  export KRX_PW=비밀번호)")

    # ── 3. NAVER 스크래핑 폴백 ────────────────────────────────
    if codes is None:
        print("  [안내] NAVER 스크래핑은 codes 리스트 필요 (전 종목 불가)")
        return pd.DataFrame()

    print(f"  NAVER 스크래핑: {len(codes)}개 종목...")
    import requests as _req
    session = _req.Session()
    session.headers.update(_NAVER_HEADERS)

    rows = []
    for i, code in enumerate(codes):
        row = _parse_naver_fundamental(code, session)
        rows.append(row)
        if (i + 1) % 20 == 0:
            print(f"    {i+1}/{len(codes)} 완료...")
        time.sleep(0.15)   # 과도한 요청 방지

    df = pd.DataFrame(rows)
    if not df.empty:
        # 비정상 PER/PBR 클렌징 (파싱 오류 제거)
        if "PER" in df.columns:
            df.loc[(df["PER"] > 500) | (df["PER"] < 0), "PER"] = float("nan")
        if "PBR" in df.columns:
            df.loc[(df["PBR"] > 100) | (df["PBR"] < 0), "PBR"] = float("nan")
        if "DIV" in df.columns:
            df.loc[(df["DIV"] > 30) | (df["DIV"] < 0), "DIV"] = float("nan")
        df.to_parquet(cache_file, index=False)
        print(f"  NAVER 스크래핑 완료: {len(df)}개 저장")
    return df


def per_signal(per) -> str:
    """PER 값을 신호 문자열로 변환."""
    try:
        per = float(per)
    except (TypeError, ValueError):
        return "N/A"
    if per <= 0 or pd.isna(per):
        return "N/A(적자/미집계)"
    elif per < 10:
        return f"★저평가({per:.1f})"
    elif per < 20:
        return f"◎적정({per:.1f})"
    elif per < 35:
        return f"△고평가({per:.1f})"
    else:
        return f"⚠과대평가({per:.1f})"


# ── 모멘텀 스코어링 공용 (screen_sector / screen_theme / screen_theme_daily 공유) ──
def pct_rank(s, ascending: bool = True):
    """0~1 백분위 순위 (method='average'). 값이 클수록 1에 가까움."""
    return s.rank(pct=True, ascending=ascending, method="average")


def consistency_ratio(s) -> float:
    """시계열에서 양(>0) 비율. NaN 제외 후 길이 기준(분모 0 가드)."""
    s = s.dropna()
    return float((s > 0).sum() / max(len(s), 1))


def momentum_score(df, weights: dict):
    """팩터별 pct_rank 가중합 / Σweight → 0~1 종합점수 Series.
    weights={컬럼명: 가중치}. 결측 채움(fillna)은 호출측 책임."""
    total = sum(weights.values())
    acc = None
    for col, w in weights.items():
        term = pct_rank(df[col]) * w
        acc = term if acc is None else acc + term
    return acc / total


def assign_rank(df, score_col: str = "score", rank_col: str = "순위"):
    """score 내림차순 정렬 후 1..N 순위 부여(reset_index)."""
    df = df.sort_values(score_col, ascending=False).reset_index(drop=True)
    df[rank_col] = range(1, len(df) + 1)
    return df


def rank_change_arrow(v) -> str:
    """순위변화 정수 → ▲n / ▼n / ─ 표시."""
    v = int(v)
    return f"▲{v}" if v > 0 else (f"▼{abs(v)}" if v < 0 else "─")


# 월간(시총 기반) 스크리너 공용 팩터 가중치 — screen_sector / screen_theme 동일
MONTHLY_WEIGHTS = {
    "ret_6m": 2.5, "ret_3m": 1.5, "ret_1m": 1.0,
    "amt_accel": 1.0, "consistency": 1.5, "participate": 1.0,
}  # Σ = 8.5

# 일별(가격 기반) 스크리너 공용 팩터 가중치 — screen_theme_daily
DAILY_WEIGHTS = {
    "ret_2w": 2.0, "ret_1w": 1.0, "ret_1m": 1.0,
    "vol_accel": 1.5, "consistency": 1.5, "participate": 1.0,
}  # Σ = 8.0
