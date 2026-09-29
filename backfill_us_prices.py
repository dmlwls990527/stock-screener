#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_us_prices.py — 미국 과거 가격 구멍 메우기 (2026-09-29).

왜: DB 가격은 2016-01 부터이고, 2016~2021 은 약 500종목만 있다 (2022~ 은 약 1,500종목).
    7월에 넣었던 2010~2015 는 7/30 DB 초기화·복원 때 빠졌다. 리플레이를 2016 부터 하려면
    1년 전(2015) 가격과, 2016~2021 의 빠진 종목이 필요하다.
왜 run_etl.py --force 가 아닌가: --force 는 2016~2026 전 날짜를 지우고 다시 넣는다(수백만 행을
    한 줄씩). 멀쩡한 2022~ 까지 바뀌어 리플레이 캐시도 전부 무효가 된다.

하는 일 (START ≤ 날짜 < END, 기본 2015-01-01 ~ 2022-01-01):
  1) ticker_master_us 전 종목을 yfinance 로 받는다 (run_etl 과 같은 auto_adjust=True, 120개씩).
  2) daily_price_us: DB 에 없는 (종목, 날짜) 행만 넣는다. 있는 행은 그대로.
  3) daily_marcap_us: 그 기간 날짜마다 기존 행 + 새 행으로 시총 순위(RANK)를 다시 매겨 그 날짜를 다시 쓴다.
     새 행의 시총 = 종가 × 상장주식수(각 종목의 가장 최근 STOCKS — run_etl 과 같은 근사).
     기존 행의 종가·시총·주식수는 바꾸지 않고 순위만 다시 매긴다.
  날짜 단위로 커밋하므로 중간에 끊겨도 다시 돌리면 이어서 된다.

실행: ./.venv/bin/python backfill_us_prices.py [--start 2015-01-01] [--end 2022-01-01] [--dry-run]
"""
import argparse
import math
import sys
import time

import pandas as pd

sys.path.insert(0, "/data/frame")
import factor_analysis as fa          # get_conn (tb_conn 포트 자동)

BATCH = 5000


def f(x):
    try:
        v = float(x)
        return None if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return None


def i(x):
    v = f(x)
    return None if v is None else int(round(v))


def q(cur, sql):
    cur.execute(sql)
    return cur.fetchall()


def download(codes, start, end):
    import yfinance as yf
    parts = []
    for k in range(0, len(codes), 120):
        syms = codes[k:k + 120]
        for attempt in (1, 2):
            try:
                raw = yf.download(syms, start=start, end=end, interval="1d", progress=False,
                                  auto_adjust=True, threads=True)
                break
            except Exception as e:
                print(f"  청크 {k // 120 + 1} 실패({attempt}): {e!r}")
                raw = None
                time.sleep(5)
        if raw is None or raw.empty:
            print(f"  청크 {k // 120 + 1} 비어있음")
            continue
        if isinstance(raw.columns, pd.MultiIndex):
            p = raw.stack(level="Ticker", future_stack=True).reset_index()
        else:
            p = raw.reset_index()
            p["Ticker"] = syms[0]
        parts.append(p)
        print(f"  {min(k + 120, len(codes))}/{len(codes)} 받음")
        time.sleep(1)
    df = pd.concat(parts, ignore_index=True)
    df.columns.name = None
    df = df.rename(columns={"Date": "date_", "Ticker": "code", "Open": "open", "High": "high",
                            "Low": "low", "Close": "close", "Volume": "volume"})
    df = df.dropna(subset=["close"])
    df["d"] = pd.to_datetime(df["date_"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values(["code", "d"])
    df["amount"] = df["close"] * df["volume"]
    df["chg"] = (df.groupby("code")["close"].pct_change() * 100).round(2)
    return df[["d", "code", "open", "high", "low", "close", "volume", "amount", "chg"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2022-01-01", help="이 날짜 전까지 (이후는 건드리지 않음)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    conn = fa.get_conn()
    conn.jconn.setAutoCommit(False)
    cur = conn.cursor()

    codes = [r[0] for r in q(cur, "SELECT code FROM ticker_master_us ORDER BY code")]
    print(f"[1] 종목 {len(codes)}개, 기간 {a.start} ~ {a.end} (미포함)")
    stocks_now = {r[0]: f(r[1]) for r in q(cur, """
        SELECT code, stocks FROM (
          SELECT code, stocks, ROW_NUMBER() OVER (PARTITION BY code ORDER BY date_ DESC) rn
          FROM daily_marcap_us WHERE stocks IS NOT NULL) WHERE rn = 1""")}
    print(f"    상장주식수 있는 종목 {len(stocks_now)}개")

    have_p = set((r[0], r[1]) for r in q(cur, f"""
        SELECT code, TO_CHAR(date_, 'YYYY-MM-DD') FROM daily_price_us
        WHERE date_ >= DATE '{a.start}' AND date_ < DATE '{a.end}'"""))
    old_m = pd.DataFrame(q(cur, f"""
        SELECT TO_CHAR(date_, 'YYYY-MM-DD'), code, close, marcap, stocks FROM daily_marcap_us
        WHERE date_ >= DATE '{a.start}' AND date_ < DATE '{a.end}'"""),
        columns=["d", "code", "close", "marcap", "stocks"])
    print(f"    기존 가격행 {len(have_p):,} / 기존 시총행 {len(old_m):,} ({time.time() - t0:.0f}s)")

    import os
    cache = f"/tmp/backfill_dl_{a.start}_{a.end}.pkl"
    if os.path.exists(cache):
        print(f"[2] 다운로드 캐시 사용 {cache}")
        df = pd.read_pickle(cache)
    else:
        print("[2] yfinance 다운로드")
        df = download(codes, a.start, a.end)
        df.to_pickle(cache)
    df = df[(df["d"] >= a.start) & (df["d"] < a.end)]
    print(f"    받은 행 {len(df):,}, 종목 {df['code'].nunique()}, 날짜 {df['d'].nunique()} ({time.time() - t0:.0f}s)")

    key = list(zip(df["code"], df["d"]))
    new_p = df[[k not in have_p for k in key]]
    print(f"[3] 새로 넣을 가격행 {len(new_p):,} (기존과 겹쳐 건너뛴 행 {len(df) - len(new_p):,})")
    by_year = new_p.groupby(new_p["d"].str[:4])["code"].nunique()
    print("    연도별 새 종목 수:", dict(by_year))

    # 시총: 기존 행 유지 + (기존 시총행이 없는) 새 (종목, 날짜) 추가 → 날짜별 순위 재계산
    old_keys = set(zip(old_m["code"], old_m["d"]))
    add_m = df[[k not in old_keys for k in key]][["d", "code", "close"]].copy()
    add_m["stocks"] = add_m["code"].map(stocks_now)
    add_m["marcap"] = add_m["close"] * add_m["stocks"]
    allm = pd.concat([old_m, add_m[["d", "code", "close", "marcap", "stocks"]]], ignore_index=True)
    allm["marcap"] = pd.to_numeric(allm["marcap"], errors="coerce")
    allm["rank"] = allm.groupby("d")["marcap"].rank(ascending=False, method="first")
    dates = sorted(allm["d"].unique())
    print(f"[4] 시총 다시 쓸 날짜 {len(dates)}개, 행 {len(allm):,} (새 행 {len(add_m):,})")
    if a.dry_run:
        print("dry-run: DB 쓰기 안 함")
        return 0

    sql_p = ("INSERT INTO daily_price_us (date_, code, open, high, low, close, volume, amount, changes_ratio) "
             "VALUES (TO_DATE(?, 'YYYY-MM-DD'), ?, ?, ?, ?, ?, ?, ?, ?)")
    rows = [[r.d, r.code, f(r.open), f(r.high), f(r.low), f(r.close), i(r.volume), f(r.amount), f(r.chg)]
            for r in new_p.itertuples(index=False)]
    for k in range(0, len(rows), BATCH):
        cur.executemany(sql_p, rows[k:k + BATCH])
        conn.commit()
        if (k // BATCH) % 20 == 0:
            print(f"    가격 {min(k + BATCH, len(rows)):,}/{len(rows):,} ({time.time() - t0:.0f}s)")
    print(f"[5] 가격 적재 완료 {len(rows):,}행 ({time.time() - t0:.0f}s)")

    sql_m = ("INSERT INTO daily_marcap_us (date_, code, close, marcap, stocks, rank) "
             "VALUES (TO_DATE(?, 'YYYY-MM-DD'), ?, ?, ?, ?, ?)")
    g = allm.groupby("d")
    for n, d in enumerate(dates):
        part = g.get_group(d)
        cur.execute("DELETE FROM daily_marcap_us WHERE date_ = TO_DATE(?, 'YYYY-MM-DD')", [d])
        cur.executemany(sql_m, [[d, r.code, f(r.close), f(r.marcap), i(r.stocks), i(r.rank)]
                                for r in part.itertuples(index=False)])
        conn.commit()
        if n % 100 == 0:
            print(f"    시총 {n + 1}/{len(dates)} {d} {len(part)}종목 ({time.time() - t0:.0f}s)")
    print(f"[6] 시총 재작성 완료 {len(dates)}일 ({time.time() - t0:.0f}s)")

    print("[7] 검증: 연도별 가격 종목 수")
    for r in q(cur, f"""SELECT TO_CHAR(date_, 'YYYY'), COUNT(DISTINCT code), COUNT(*) FROM daily_price_us
                        WHERE date_ < DATE '{a.end}' GROUP BY TO_CHAR(date_, 'YYYY') ORDER BY 1"""):
        print("   ", r)
    for r in q(cur, f"""SELECT TO_CHAR(date_, 'YYYY'), COUNT(DISTINCT code), SUM(CASE WHEN rank IS NULL THEN 1 ELSE 0 END)
                        FROM daily_marcap_us WHERE date_ < DATE '{a.end}' GROUP BY TO_CHAR(date_, 'YYYY') ORDER BY 1"""):
        print("    marcap", r)
    conn.close()
    print(f"완료 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
