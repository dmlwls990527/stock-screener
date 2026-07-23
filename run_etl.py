#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KR + US DB 적재 통합 스크립트

실행:
  source /data/tibero7/t7.profile
  source /data/frame/.venv/bin/activate

  python3 run_etl.py          # KR + US 전체
  python3 run_etl.py --kr     # 국장만
  python3 run_etl.py --us     # 미장만
"""

import os
import sys
import time
import argparse
from datetime import date

import pandas as pd

try:
    import jaydebeapi
except ImportError:
    print("pip install jaydebeapi JPype1")
    sys.exit(1)

# ── DB 설정 ───────────────────────────────────────────────────
TIBERO_HOST = "localhost"
TIBERO_PORT = 44123
TIBERO_SID  = "tibero"
TIBERO_USER = os.environ.get("TIBERO_USER", "sys")
TIBERO_PASS = os.environ.get("TIBERO_PASS", "")
JDBC_JAR    = "/data/tibero7/tibero7/client/lib/jar/tibero7-jdbc.jar"
JDBC_CLASS  = "com.tmax.tibero.jdbc.TbDriver"


def get_conn():
    url = f"jdbc:tibero:thin:@{TIBERO_HOST}:{TIBERO_PORT}:{TIBERO_SID}"
    conn = jaydebeapi.connect(JDBC_CLASS, url, [TIBERO_USER, TIBERO_PASS], JDBC_JAR)
    conn.jconn.setAutoCommit(False)
    return conn


def get_loaded_dates(conn, table: str) -> set:
    cur = conn.cursor()
    cur.execute(f"SELECT DISTINCT TO_CHAR(date_, 'YYYYMMDD') FROM {table}")
    rows = cur.fetchall()
    cur.close()
    return {r[0] for r in rows}


def to_float(val):
    try:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        f = float(val)
        return None if f == 0.0 else f
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# 국장 (KR) — pykrx → Tibero
# ═══════════════════════════════════════════════════════════════

def run_kr_etl(start: str = "20160101"):
    from pykrx import stock as krx

    end   = date.today().strftime("%Y%m%d")
    DELAY = 0.5

    print("=" * 60)
    print(f"  [KR ETL] pykrx → Tibero")
    print(f"  기간: {start} ~ {end} / KOSPI")
    print("=" * 60)

    conn = get_conn()
    print("  DB 연결 성공\n")

    # ── ticker_master ─────────────────────────────────────
    print("[KR-1] ticker_master 수집 중...")
    tickers = krx.get_market_ticker_list(end, market="KOSPI")
    sql_merge = """
        MERGE INTO ticker_master tgt
        USING (SELECT ? AS code, ? AS name, ? AS market FROM dual) src
        ON (tgt.code = src.code)
        WHEN MATCHED THEN
            UPDATE SET tgt.name = src.name, tgt.market = src.market
        WHEN NOT MATCHED THEN
            INSERT (code, name, market) VALUES (src.code, src.name, src.market)
    """
    cur = conn.cursor()
    for i, t in enumerate(tickers):
        name = krx.get_market_ticker_name(t)
        cur.execute(sql_merge, [t, name, "KOSPI"])
        if (i + 1) % 100 == 0:
            print(f"    {i+1}/{len(tickers)}")
        time.sleep(0.05)
    conn.commit()
    cur.close()
    print(f"  → {len(tickers)}개 완료\n")

    # ── 영업일 루프 ──────────────────────────────────────────
    loaded     = get_loaded_dates(conn, "daily_price")
    date_range = pd.bdate_range(start=start, end=end)
    total = len(date_range)
    skipped = inserted = 0

    print(f"[KR-2] 영업일 적재 — 총 {total}일 (기적재 {len(loaded)}일)")
    print("-" * 60)

    for i, dt in enumerate(date_range):
        ds = dt.strftime("%Y%m%d")
        if ds in loaded:
            skipped += 1
            continue

        sys.stdout.write(f"\r  [{i+1}/{total}] {ds} 적재 중...  ")
        sys.stdout.flush()

        cur = conn.cursor()
        try:
            # daily_price
            df_p = krx.get_market_ohlcv_by_ticker(ds, market="KOSPI")
            time.sleep(DELAY)
            n1 = 0
            if df_p is not None and not df_p.empty:
                df_p.index.name = "code"
                df_p = df_p.reset_index().rename(columns={
                    "시가":"open","고가":"high","저가":"low","종가":"close",
                    "거래량":"volume","거래대금":"amount","등락률":"changes_ratio"
                })
                # 휴장일 0 데이터 제거 (pykrx가 공휴일에도 0으로 반환하는 문제)
                df_p = df_p[(df_p["close"] > 0) | (df_p["volume"] > 0)]
                sql = """INSERT INTO daily_price
                    (date_, code, open, high, low, close, volume, amount, changes_ratio)
                    VALUES (TO_DATE(?, 'YYYYMMDD'), ?, ?, ?, ?, ?, ?, ?, ?)"""
                for r in df_p.itertuples(index=False):
                    cur.execute(sql, [ds, r.code,
                        int(r.open), int(r.high), int(r.low), int(r.close),
                        int(r.volume), int(r.amount),
                        float(r.changes_ratio) if not pd.isna(r.changes_ratio) else None])
                n1 = len(df_p)

            n2 = n3 = 0
            if n1 == 0:
                # 휴장일(공휴일 등) — daily_price가 비었으면 marcap/fundamental도 조회하지 않고 스킵
                # (pykrx가 휴장일에도 marcap=0/전종목 행을 반환해 phantom row로 오염시키는 것을 방지)
                pass
            else:
                # daily_marcap
                df_m = krx.get_market_cap_by_ticker(ds, market="KOSPI")
                time.sleep(DELAY)
                if df_m is not None and not df_m.empty:
                    df_m.index.name = "code"
                    df_m = df_m.reset_index().rename(columns={
                        "종가":"close","시가총액":"marcap",
                        "거래량":"volume","거래대금":"amount","상장주식수":"stocks"
                    })
                    # 휴장일/거래정지 등으로 시총 0인 행 제거 (daily_price와 동일한 방어)
                    df_m = df_m[df_m["marcap"] > 0]
                    df_m = df_m.sort_values("marcap", ascending=False).reset_index(drop=True)
                    df_m["rank"] = range(1, len(df_m) + 1)
                    sql = """INSERT INTO daily_marcap
                        (date_, code, close, marcap, volume, amount, stocks, rank)
                        VALUES (TO_DATE(?, 'YYYYMMDD'), ?, ?, ?, ?, ?, ?, ?)"""
                    for r in df_m.itertuples(index=False):
                        cur.execute(sql, [ds, r.code,
                            int(r.close), int(r.marcap),
                            int(r.volume), int(r.amount),
                            int(r.stocks), int(r.rank)])
                    n2 = len(df_m)

                # daily_fundamental
                df_f = krx.get_market_fundamental(ds, market="KOSPI")
                time.sleep(DELAY)
                if df_f is not None and not df_f.empty:
                    df_f.index.name = "code"
                    df_f = df_f.reset_index().rename(columns={
                        "BPS":"bps","PER":"per","PBR":"pbr",
                        "EPS":"eps","DIV":"div","DPS":"dps"
                    })
                    sql = """INSERT INTO daily_fundamental
                        (date_, code, bps, per, pbr, eps, div, dps)
                        VALUES (TO_DATE(?, 'YYYYMMDD'), ?, ?, ?, ?, ?, ?, ?)"""
                    for r in df_f.itertuples(index=False):
                        cur.execute(sql, [ds, r.code,
                            to_float(r.bps), to_float(r.per), to_float(r.pbr),
                            to_float(r.eps), to_float(r.div), to_float(r.dps)])
                    n3 = len(df_f)

            conn.commit()
            cur.close()
            inserted += 1
            sys.stdout.write(f"\r  [{i+1}/{total}] {ds}  price:{n1}  marcap:{n2}  fund:{n3}    \n")
            sys.stdout.flush()

        except Exception as e:
            conn.rollback()
            cur.close()
            print(f"\n  [{ds}] 오류: {e}")

    print("-" * 60)
    print(f"  KR 완료 — 신규:{inserted}일  스킵:{skipped}일")
    print("=" * 60)
    conn.close()


# ═══════════════════════════════════════════════════════════════
# 미장 (US) — yfinance → Tibero
# ═══════════════════════════════════════════════════════════════

def run_us_etl(start: str = "2016-01-01", force: bool = False):
    import yfinance as yf
    import requests
    from io import StringIO

    end     = date.today().strftime("%Y-%m-%d")
    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    print("=" * 60)
    print(f"  [US ETL] yfinance → Tibero")
    print(f"  기간: {start} ~ {end}")
    print("=" * 60)

    # ── 유니버스 (S&P500 + NASDAQ-100) ───────────────────────
    def get_sp500():
        resp = requests.get(
            "en.wikipedia.org/wiki/List_of_S%26P_500_companies".replace("en.", "https://en."),
            headers=HEADERS)
        df = pd.read_html(StringIO(resp.text))[0]
        df["Symbol"] = df["Symbol"].str.replace(".", "-", regex=False)
        return df[["Symbol","Security","GICS Sector"]].rename(
            columns={"Symbol":"code","Security":"name","GICS Sector":"sector"})

    def get_nasdaq100():
        resp = requests.get(
            "en.wikipedia.org/wiki/Nasdaq-100".replace("en.", "https://en."),
            headers=HEADERS)
        tables = pd.read_html(StringIO(resp.text))
        df = next((t for t in tables if "Ticker" in t.columns), None)
        if df is None:
            raise ValueError("Nasdaq-100 테이블 없음")
        df["Ticker"] = df["Ticker"].str.replace(".", "-", regex=False)
        sector_col  = next((c for c in df.columns if "sector" in c.lower()), None)
        company_col = next((c for c in df.columns if c in ("Company","Security","Name")), "Company")
        cols = {"Ticker":"code", company_col:"name"}
        sel  = ["Ticker", company_col]
        if sector_col:
            cols[sector_col] = "sector"; sel.append(sector_col)
        result = df[sel].rename(columns=cols)
        if "sector" not in result.columns:
            result["sector"] = None
        return result

    sp500  = get_sp500();  sp500["market"]  = "SP500"
    try:
        nq100  = get_nasdaq100(); nq100["market"] = "NQ100"
    except Exception as e:
        # 위키피디아 Nasdaq-100 페이지 구조 변경 등으로 스크래핑 실패 시,
        # 기존 ticker_master_us(이미 적재된 전체 유니버스)로 폴백해 유니버스 회귀 방지.
        print(f"  ⚠️  Nasdaq-100 스크래핑 실패({e!r}) → 기존 ticker_master_us 유니버스로 대체")
        _c = get_conn(); _cur = _c.cursor()
        _cur.execute("SELECT code, name, sector FROM ticker_master_us")
        nq100 = pd.DataFrame(_cur.fetchall(), columns=["code", "name", "sector"])
        nq100["market"] = "NQ100"
        _c.close()
    universe = pd.concat([sp500, nq100], ignore_index=True)\
                 .drop_duplicates(subset=["code"], keep="first")\
                 .reset_index(drop=True)
    # 섹터 라벨을 표준 GICS 11개로 정규화 — 소스마다 industry/sub-industry 라벨이 섞여
    # 들어오는 걸(예: 'Semiconductors','Software' → Information Technology) 적재 시점에 통일.
    GICS_NORMALIZE = {
        "Software": "Information Technology", "EDP Services": "Information Technology",
        "Computer Services": "Information Technology", "Semiconductors": "Information Technology",
        "Biotechnology": "Health Care",
        "Industrial Machinery": "Industrials", "Aerospace": "Industrials",
        "Military, Government, Technical": "Industrials",
        "Catalog/Specialty Distribution": "Consumer Discretionary",
        "Soft Drinks": "Consumer Staples",
        "Telecommunications Services": "Communication Services",
    }
    if "sector" in universe.columns:
        universe["sector"] = universe["sector"].replace(GICS_NORMALIZE)
    symbols = universe["code"].tolist()
    print(f"  유니버스: {len(symbols)}개 (S&P500 + NASDAQ-100)\n")

    conn = get_conn()
    print("  DB 연결 성공\n")

    # ── ticker_master_us ─────────────────────────────────────
    print("[US-1] ticker_master_us 적재 중...")
    sql_merge = """
        MERGE INTO ticker_master_us tgt
        USING (SELECT ? AS code, ? AS name, ? AS sector, ? AS market FROM dual) src
        ON (tgt.code = src.code)
        WHEN MATCHED THEN
            UPDATE SET tgt.name=src.name, tgt.sector=src.sector, tgt.market=src.market
        WHEN NOT MATCHED THEN
            INSERT (code, name, sector, market) VALUES (src.code, src.name, src.sector, src.market)
    """
    cur = conn.cursor()
    for r in universe.itertuples(index=False):
        cur.execute(sql_merge, [r.code, r.name, r.sector, r.market])
    conn.commit()
    cur.close()
    print(f"  → {len(universe)}개 완료\n")

    # ── OHLCV 벌크 다운로드 ──────────────────────────────────
    loaded  = get_loaded_dates(conn, "daily_price_us")
    if loaded and not force:
        dl_start = (pd.Timestamp(max(loaded)) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
        print(f"[US-2] 일일 업데이트 모드 — {dl_start}부터 다운로드")
    else:
        dl_start = start
        print(f"[US-2] 전체 적재 모드 — {dl_start}부터 다운로드 (force={force})")

    print(f"  OHLCV 다운로드 중... ({len(symbols)}종목)")
    raw = yf.download(symbols, start=dl_start, end=end,
                      interval="1d", progress=True, auto_adjust=True)
    df_ohlcv = raw.stack(level="Ticker", future_stack=True).reset_index()
    df_ohlcv.columns.name = None
    df_ohlcv = df_ohlcv.rename(columns={
        "Date":"date_","Ticker":"code",
        "Open":"open","High":"high","Low":"low","Close":"close","Volume":"volume"
    })
    df_ohlcv["amount"]   = df_ohlcv["close"] * df_ohlcv["volume"]
    df_ohlcv["date_str"] = df_ohlcv["date_"].dt.strftime("%Y-%m-%d")
    df_ohlcv = df_ohlcv.sort_values(["code","date_"])
    df_ohlcv["changes_ratio"] = (
        df_ohlcv.groupby("code")["close"].pct_change() * 100
    ).round(2)
    df_ohlcv = df_ohlcv[["date_str","code","open","high","low","close",
                          "volume","amount","changes_ratio"]].dropna(subset=["close"])
    print(f"  → shape: {df_ohlcv.shape}\n")

    # ── 상장주식수 ────────────────────────────────────────────
    print(f"[US-3] 상장주식수 조회 중... ({len(symbols)}종목)")
    shares_map = {}
    for i, sym in enumerate(symbols):
        try:
            shares_map[sym] = yf.Ticker(sym).fast_info.shares
        except Exception:
            shares_map[sym] = None
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(symbols)}")
        time.sleep(0.05)
    print(f"  → 완료\n")

    # ── 펀더멘털 ─────────────────────────────────────────────
    print(f"[US-4] 펀더멘털 조회 중... ({len(symbols)}종목)")
    fund_rows = []
    for i, sym in enumerate(symbols):
        try:
            info = yf.Ticker(sym).info
            fund_rows.append({
                "code": sym,
                "per":  to_float(info.get("trailingPE")),
                "pbr":  to_float(info.get("priceToBook")),
                "div":  to_float(info.get("dividendYield")),
                "eps":  to_float(info.get("trailingEps")),
            })
        except Exception:
            pass
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(symbols)}")
        time.sleep(0.1)
    fund_df = pd.DataFrame(fund_rows)
    print(f"  → 완료\n")

    # ── 날짜별 DB 적재 ────────────────────────────────────────
    today_str  = end
    date_list  = sorted(df_ohlcv["date_str"].unique())
    total      = len(date_list)
    skipped = inserted = 0

    print(f"[US-5] 날짜별 적재 — 총 {total}개 거래일")
    print("-" * 60)

    for i, date_str in enumerate(date_list):
        if date_str.replace("-", "") in loaded:  # YYYY-MM-DD -> YYYYMMDD (loaded 포맷 일치)
            skipped += 1
            continue

        df_day = df_ohlcv[df_ohlcv["date_str"] == date_str]
        sys.stdout.write(f"\r  [{i+1}/{total}] {date_str} 적재 중...  ")
        sys.stdout.flush()

        cur = conn.cursor()
        try:
            # daily_price_us
            sql_p = """INSERT INTO daily_price_us
                (date_, code, open, high, low, close, volume, amount, changes_ratio)
                VALUES (TO_DATE(?, 'YYYY-MM-DD'), ?, ?, ?, ?, ?, ?, ?, ?)"""
            for r in df_day.itertuples(index=False):
                cur.execute(sql_p, [
                    r.date_str, r.code,
                    to_float(r.open), to_float(r.high),
                    to_float(r.low),  float(r.close),
                    int(r.volume),    to_float(r.amount),
                    to_float(r.changes_ratio)
                ])
            n1 = len(df_day)

            # daily_marcap_us
            marcap_rows = []
            for r in df_day.itertuples(index=False):
                shares = shares_map.get(r.code)
                marcap = float(r.close) * shares if shares else None
                marcap_rows.append({"date_str":r.date_str,"code":r.code,
                                    "close":float(r.close),"marcap":marcap,"stocks":shares})
            mdf = pd.DataFrame(marcap_rows)\
                    .sort_values("marcap", ascending=False, na_position="last")\
                    .reset_index(drop=True)
            valid = mdf["marcap"].notna()
            mdf["rank"] = None
            mdf.loc[valid, "rank"] = range(1, valid.sum() + 1)
            sql_m = """INSERT INTO daily_marcap_us
                (date_, code, close, marcap, stocks, rank)
                VALUES (TO_DATE(?, 'YYYY-MM-DD'), ?, ?, ?, ?, ?)"""
            for r in mdf.itertuples(index=False):
                cur.execute(sql_m, [
                    r.date_str, r.code, r.close, r.marcap,
                    int(r.stocks) if r.stocks else None,
                    int(r.rank)   if r.rank   else None
                ])
            n2 = len(mdf)

            # daily_fundamental_us (오늘 1회만)
            n3 = 0
            if date_str == today_str and not fund_df.empty:
                sql_f = """INSERT INTO daily_fundamental_us
                    (date_, code, per, pbr, div, eps)
                    VALUES (TO_DATE(?, 'YYYY-MM-DD'), ?, ?, ?, ?, ?)"""
                for r in fund_df.itertuples(index=False):
                    cur.execute(sql_f, [date_str, r.code, r.per, r.pbr, r.div, r.eps])
                n3 = len(fund_df)

            conn.commit()
            cur.close()
            inserted += 1
            sys.stdout.write(
                f"\r  [{i+1}/{total}] {date_str}  price:{n1}  marcap:{n2}  fund:{n3}    \n")
            sys.stdout.flush()

        except Exception as e:
            conn.rollback()
            cur.close()
            print(f"\n  [{date_str}] 오류: {e}")

    print("-" * 60)
    print(f"  US 완료 — 신규:{inserted}일  스킵:{skipped}일")
    print("=" * 60)
    conn.close()


# ═══════════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KR/US DB 적재")
    parser.add_argument("--kr", action="store_true", help="국장만 실행")
    parser.add_argument("--us", action="store_true", help="미장만 실행")
    parser.add_argument("--force", action="store_true", help="US 전체 재다운로드 (2016부터 강제)")
    args = parser.parse_args()

    run_both = not args.kr and not args.us  # 인자 없으면 둘 다

    if args.kr or run_both:
        run_kr_etl(start="20160101")

    if args.us or run_both:
        run_us_etl(start="2016-01-01", force=args.force)

    print("\n모든 ETL 완료.")
