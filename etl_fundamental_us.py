#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
미장 Top100 펀더멘털 (PER, PBR, DIV, EPS, EPS성장률, ROE, 매출성장률, 영업이익률, 부채비율) 수집 → DAILY_FUNDAMENTAL_US 적재
yfinance ticker.info 사용 — 오늘 기준 현재 값

실행:
  source /data/tibero7/t7.profile
  source /data/frame/.venv/bin/activate
  python3 etl_fundamental_us.py
"""

import os
import sys
import time
from datetime import date

import pandas as pd
import yfinance as yf

try:
    import jaydebeapi
except ImportError:
    print("pip install jaydebeapi JPype1")
    sys.exit(1)

TIBERO_HOST = "localhost"
TIBERO_PORT = 44123
TIBERO_SID  = "tibero"
TIBERO_USER = os.environ.get("TIBERO_USER", "sys")
TIBERO_PASS = os.environ.get("TIBERO_PASS", "")
JDBC_JAR    = "/data/tibero7/tibero7/client/lib/jar/tibero7-jdbc.jar"
JDBC_CLASS  = "com.tmax.tibero.jdbc.TbDriver"

TOP_N      = 100
TODAY_STR  = date.today().strftime("%Y-%m-%d")


def get_conn():
    url = f"jdbc:tibero:thin:@{TIBERO_HOST}:{TIBERO_PORT}:{TIBERO_SID}"
    conn = jaydebeapi.connect(JDBC_CLASS, url, [TIBERO_USER, TIBERO_PASS], JDBC_JAR)
    conn.jconn.setAutoCommit(False)
    return conn


def to_float(val):
    try:
        if val is None:
            return None
        f = float(val)
        return None if (f != f) else f   # NaN 체크
    except Exception:
        return None


def get_top100_codes(conn):
    """현재 시총 Top100 코드 조회"""
    sql = f"""
    SELECT code FROM daily_marcap_us
    WHERE date_ = (SELECT MAX(date_) FROM daily_marcap_us)
      AND rank <= {TOP_N}
    ORDER BY rank
    """
    cur = conn.cursor()
    cur.execute(sql)
    codes = [r[0] for r in cur.fetchall()]
    cur.close()
    return codes


def fetch_fundamentals(codes):
    """yfinance info에서 PER/PBR/DIV/EPS/성장률 수집"""
    rows = []
    total = len(codes)
    for i, code in enumerate(codes):
        sys.stdout.write(f"\r  [{i+1}/{total}] {code:<8} 수집 중...")
        sys.stdout.flush()
        try:
            info = yf.Ticker(code).info
            rows.append({
                "code":              code,
                "per":               to_float(info.get("trailingPE")),
                "pbr":               to_float(info.get("priceToBook")),
                "div":               to_float(info.get("dividendYield")),
                "eps":               to_float(info.get("trailingEps")),
                "eps_growth":        to_float(info.get("earningsGrowth")),    # YoY (소수점)
                "revenue_growth":    to_float(info.get("revenueGrowth")),     # YoY (소수점)
                "roe":               to_float(info.get("returnOnEquity")),    # 소수점
                "debt_to_equity":    to_float(info.get("debtToEquity")),      # 배수
                "operating_margin":  to_float(info.get("operatingMargins")), # 소수점
                "forward_pe":        to_float(info.get("forwardPE")),
            })
        except Exception as e:
            print(f"\n  [{code}] 오류: {e}")
            rows.append({"code": code})
        time.sleep(0.15)

    print(f"\n  → {len(rows)}개 수집 완료")
    return pd.DataFrame(rows)


def upsert_fundamentals(conn, df):
    """DAILY_FUNDAMENTAL_US UPSERT (오늘 날짜 기준)"""
    # 오늘 날짜 이미 있으면 삭제 후 재삽입
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM daily_fundamental_us WHERE date_ = TO_DATE(?, 'YYYY-MM-DD')",
        [TODAY_STR]
    )
    deleted = cur.rowcount
    if deleted:
        print(f"  기존 오늘({TODAY_STR}) 데이터 {deleted}건 삭제")

    sql = """INSERT INTO daily_fundamental_us
        (date_, code, per, pbr, div, eps, eps_growth,
         roe, revenue_growth, debt_to_equity, operating_margin)
        VALUES (TO_DATE(?, 'YYYY-MM-DD'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""

    inserted = 0
    for r in df.itertuples(index=False):
        per              = to_float(getattr(r, "per",              None))
        pbr              = to_float(getattr(r, "pbr",              None))
        div              = to_float(getattr(r, "div",              None))
        eps              = to_float(getattr(r, "eps",              None))
        eps_growth       = to_float(getattr(r, "eps_growth",       None))
        roe              = to_float(getattr(r, "roe",              None))
        revenue_growth   = to_float(getattr(r, "revenue_growth",   None))
        debt_to_equity   = to_float(getattr(r, "debt_to_equity",   None))
        operating_margin = to_float(getattr(r, "operating_margin", None))
        if any(v is not None for v in [per, pbr, eps, roe]):
            cur.execute(sql, [TODAY_STR, r.code, per, pbr, div, eps, eps_growth,
                              roe, revenue_growth, debt_to_equity, operating_margin])
            inserted += 1

    conn.commit()
    cur.close()
    print(f"  {inserted}건 삽입 완료")
    return df


def main():
    print("=" * 60)
    print(f"  [US Fundamental ETL]  기준일: {TODAY_STR}")
    print(f"  대상: 현재 시총 Top{TOP_N}")
    print("=" * 60)

    conn = get_conn()
    print("  DB 연결 성공\n")

    print(f"[1] Top{TOP_N} 코드 조회...")
    codes = get_top100_codes(conn)
    print(f"  {len(codes)}개: {', '.join(codes[:10])} ...\n")

    print(f"[2] yfinance 펀더멘털 수집 ({len(codes)}종목)...")
    df = fetch_fundamentals(codes)

    print(f"\n[3] DB 적재...")
    upsert_fundamentals(conn, df)

    conn.close()

    # 결과 미리보기
    print(f"\n{'='*75}")
    print("  수집 결과 (PEG = PER / EPS성장률%)")
    print(f"{'='*75}")
    df["peg"] = df.apply(
        lambda r: round(r["per"] / (r["eps_growth"] * 100), 2)
        if pd.notna(r.get("per")) and pd.notna(r.get("eps_growth")) and r.get("eps_growth", 0) > 0
        else None,
        axis=1
    )
    show_cols = ["code", "per", "pbr", "eps_growth", "peg", "roe", "revenue_growth", "operating_margin", "debt_to_equity"]
    show_cols = [c for c in show_cols if c in df.columns]
    print(df[show_cols].to_string(index=False))
    print(f"\n완료: {TODAY_STR} 기준 펀더멘털 적재됨")


if __name__ == "__main__":
    main()
