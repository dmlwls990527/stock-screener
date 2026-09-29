#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clean_zero_volume.py — 미국 가격 테이블에서 '거래량 0' 행 제거 + 그 날짜 시총 순위 재계산 (2026-09-29).

왜: yfinance 가 미국 상장 전 기간을 가격 고정·거래량 0 인 가짜 행으로 채워 둔 종목이 있다
    (FER 2016-01~2024-05 19.698 고정, SW·AMCR·INDV 등). 이 행들은
      ① 거래대금이 NULL → leader_screener.price_metrics 의 round(NaN) 에서 그날 계산 전체가 멈추고(주도주 0개)
      ② 가짜 시가총액이 daily_marcap_us 순위에 끼어든다.
    S&P 급 종목이 하루 종일 거래량 0 인 날은 사실상 없으므로 거래량 0 행을 전부 지운다.
지운 행은 /data/frame/data/removed_zero_volume_YYYYMMDD.csv.gz 로 남긴다 (되돌리기용).

실행: ./.venv/bin/python clean_zero_volume.py [--dry-run]
"""
import argparse
import sys
import time
from datetime import date

import pandas as pd

sys.path.insert(0, "/data/frame")
import factor_analysis as fa


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    conn = fa.get_conn()
    conn.jconn.setAutoCommit(False)
    cur = conn.cursor()
    cur.execute("""SELECT TO_CHAR(date_, 'YYYY-MM-DD'), code, open, high, low, close, volume, amount, changes_ratio
                   FROM daily_price_us WHERE volume = 0 OR volume IS NULL""")
    bad = pd.DataFrame(cur.fetchall(), columns=["d", "code", "open", "high", "low", "close", "volume", "amount", "chg"])
    print(f"거래량 0 행 {len(bad):,} / 종목 {bad['code'].nunique()} / 날짜 {bad['d'].nunique()}")
    print("종목별 상위:", bad.groupby("code").size().sort_values(ascending=False).head(15).to_dict())
    if a.dry_run or bad.empty:
        return 0
    out = f"/data/frame/data/removed_zero_volume_{date.today():%Y%m%d}.csv.gz"
    bad.to_csv(out, index=False)
    cur.execute("""SELECT TO_CHAR(m.date_, 'YYYY-MM-DD'), m.code, m.close, m.marcap, m.stocks, m.rank
                   FROM daily_marcap_us m JOIN daily_price_us p ON p.code = m.code AND p.date_ = m.date_
                   WHERE p.volume = 0 OR p.volume IS NULL""")
    badm = pd.DataFrame(cur.fetchall(), columns=["d", "code", "close", "marcap", "stocks", "rank"])
    badm.to_csv(out.replace(".csv.gz", "_marcap.csv.gz"), index=False)
    print(f"백업 {out} (+ _marcap, {len(badm):,}행)")

    keys = [[r.d, r.code] for r in bad.itertuples(index=False)]
    for k in range(0, len(keys), 5000):
        cur.executemany("DELETE FROM daily_marcap_us WHERE date_ = TO_DATE(?, 'YYYY-MM-DD') AND code = ?", keys[k:k + 5000])
        cur.executemany("DELETE FROM daily_price_us WHERE date_ = TO_DATE(?, 'YYYY-MM-DD') AND code = ?", keys[k:k + 5000])
        conn.commit()
    print(f"삭제 완료 ({time.time() - t0:.0f}s)")

    dates = sorted(bad["d"].unique())
    for n, d in enumerate(dates):
        cur.execute("SELECT code, marcap FROM daily_marcap_us WHERE date_ = TO_DATE(?, 'YYYY-MM-DD')", [d])
        m = pd.DataFrame(cur.fetchall(), columns=["code", "marcap"])
        m["marcap"] = pd.to_numeric(m["marcap"], errors="coerce")
        m["rank"] = m["marcap"].rank(ascending=False, method="first")
        cur.executemany("UPDATE daily_marcap_us SET rank = ? WHERE date_ = TO_DATE(?, 'YYYY-MM-DD') AND code = ?",
                        [[None if pd.isna(r.rank) else int(r.rank), d, r.code] for r in m.itertuples(index=False)])
        conn.commit()
        if n % 200 == 0:
            print(f"  순위 재계산 {n + 1}/{len(dates)} {d} ({time.time() - t0:.0f}s)")
    cur.execute("SELECT COUNT(*) FROM daily_price_us WHERE volume = 0 OR volume IS NULL OR amount IS NULL")
    print("남은 거래량0/거래대금NULL 행:", cur.fetchall()[0][0])
    conn.close()
    print(f"완료 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
