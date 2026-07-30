#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db_restore.py — db_backup.py가 만든 CSV.gz 백업을 재생성된 테이블에 복원.
create_tables_us.py / create_tables_kr.py로 빈 테이블을 만든 뒤 이 스크립트를 실행한다.
"""
import os
import csv
import gzip
import time
import jaydebeapi as j
import tb_conn  # 포트를 tip에서 자동 해결 (하드코딩 제거)

SRC = "/data/frame/db_backup"
JAR = "/data/tibero7/tibero7/client/lib/jar/tibero7-jdbc.jar"
URL = tb_conn.URL
BATCH = 5000

TABLES = {
    "ticker_master_us": {
        "cols": ["code", "name", "sector", "market"],
        "date_cols": [],
    },
    "daily_price_us": {
        "cols": ["date_", "code", "open", "high", "low", "close", "volume", "amount", "changes_ratio"],
        "date_cols": ["date_"],
    },
    "daily_marcap_us": {
        "cols": ["date_", "code", "close", "marcap", "stocks", "rank"],
        "date_cols": ["date_"],
    },
    "daily_fundamental_us": {
        "cols": ["date_", "code", "per", "pbr", "div", "eps", "eps_growth",
                "roe", "revenue_growth", "debt_to_equity", "operating_margin"],
        "date_cols": ["date_"],
    },
    "quarterly_financials_us": {
        "cols": ["code", "end_date", "fp", "revenue", "op_income"],
        "date_cols": ["end_date"],
    },
    "ticker_master": {
        "cols": ["code", "name", "market"],
        "date_cols": [],
    },
    "daily_price": {
        "cols": ["date_", "code", "open", "high", "low", "close", "volume", "amount", "changes_ratio"],
        "date_cols": ["date_"],
    },
    "daily_marcap": {
        "cols": ["date_", "code", "close", "marcap", "volume", "amount", "stocks", "rank"],
        "date_cols": ["date_"],
    },
    "daily_fundamental": {
        "cols": ["date_", "code", "bps", "per", "pbr", "eps", "div", "dps"],
        "date_cols": ["date_"],
    },
    "quarterly_financials_kr": {
        "cols": ["code", "end_date", "fp", "revenue", "op_income",
                "net_income", "total_equity", "total_liabilities"],
        "date_cols": ["end_date"],
    },
}
# 순서 중요: 대용량 테이블부터 먼저 (실패 시 빨리 알아채기 위해 작은 것 먼저도 무방)
ORDER = ["ticker_master_us", "daily_marcap_us", "daily_price_us",
        "quarterly_financials_us", "daily_fundamental_us",
        "ticker_master", "daily_marcap", "daily_price", "daily_fundamental",
        "quarterly_financials_kr"]


def to_val(v):
    return None if v == "" else v


def build_sql(table, cols, date_cols):
    placeholders = []
    for c in cols:
        if c in date_cols:
            placeholders.append(f"TO_DATE(?, 'YYYY-MM-DD')")
        else:
            placeholders.append("?")
    return f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})"


conn = j.connect("com.tmax.tibero.jdbc.TbDriver", URL, [os.environ.get("TIBERO_USER", "sys"), os.environ.get("TIBERO_PASS", "")], JAR)
conn.jconn.setAutoCommit(False)
cur = conn.cursor()

for table in ORDER:
    meta = TABLES[table]
    cols, date_cols = meta["cols"], meta["date_cols"]
    sql = build_sql(table, cols, date_cols)
    path = f"{SRC}/{table}.csv.gz"
    t0 = time.time()
    n = 0
    batch = []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        assert header == cols, f"{table} 헤더 불일치: {header} != {cols}"
        for row in reader:
            batch.append([to_val(v) for v in row])
            if len(batch) >= BATCH:
                cur.executemany(sql, batch)
                conn.commit()
                n += len(batch)
                batch = []
        if batch:
            cur.executemany(sql, batch)
            conn.commit()
            n += len(batch)
    print(f"{table}: {n:,}행 복원 ({time.time()-t0:.0f}s)", flush=True)

cur.close()
conn.close()
print("복원 완료")
