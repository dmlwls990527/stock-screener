#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db_backup.py — 주식 테이블(미장 5개 + 국장 5개)을 CSV.gz로 덤프 (Tibero 재설치 사고 대비).
복원: create_tables_us.py/create_tables_kr.py로 테이블 만든 뒤 db_restore.py로 적재.
"""
import os
import csv, gzip, os
import jaydebeapi as j

OUT = "/data/frame/db_backup"
os.makedirs(OUT, exist_ok=True)
c = j.connect("com.tmax.tibero.jdbc.TbDriver", "jdbc:tibero:thin:@localhost:44123:tibero",
              [os.environ.get("TIBERO_USER", "sys"), os.environ.get("TIBERO_PASS", "")], "/data/tibero7/tibero7/client/lib/jar/tibero7-jdbc.jar")
cur = c.cursor()

TABLES = {
    "ticker_master_us":        "SELECT code,name,sector,market FROM ticker_master_us",
    "daily_price_us":          "SELECT TO_CHAR(date_,'YYYY-MM-DD'),code,open,high,low,close,volume,amount,changes_ratio FROM daily_price_us",
    "daily_marcap_us":         "SELECT TO_CHAR(date_,'YYYY-MM-DD'),code,close,marcap,stocks,rank FROM daily_marcap_us",
    "daily_fundamental_us":    "SELECT TO_CHAR(date_,'YYYY-MM-DD'),code,per,pbr,div,eps,eps_growth,roe,revenue_growth,debt_to_equity,operating_margin FROM daily_fundamental_us",
    "quarterly_financials_us": "SELECT code,TO_CHAR(end_date,'YYYY-MM-DD'),fp,revenue,op_income FROM quarterly_financials_us",
    "ticker_master":           "SELECT code,name,market FROM ticker_master",
    "daily_price":             "SELECT TO_CHAR(date_,'YYYY-MM-DD'),code,open,high,low,close,volume,amount,changes_ratio FROM daily_price",
    "daily_marcap":            "SELECT TO_CHAR(date_,'YYYY-MM-DD'),code,close,marcap,volume,amount,stocks,rank FROM daily_marcap",
    "daily_fundamental":       "SELECT TO_CHAR(date_,'YYYY-MM-DD'),code,bps,per,pbr,eps,div,dps FROM daily_fundamental",
    "quarterly_financials_kr": "SELECT code,TO_CHAR(end_date,'YYYY-MM-DD'),fp,revenue,op_income,net_income,total_equity,total_liabilities FROM quarterly_financials_kr",
}
HEADERS = {
    "ticker_master_us":        ["code","name","sector","market"],
    "daily_price_us":          ["date_","code","open","high","low","close","volume","amount","changes_ratio"],
    "daily_marcap_us":         ["date_","code","close","marcap","stocks","rank"],
    "daily_fundamental_us":    ["date_","code","per","pbr","div","eps","eps_growth","roe","revenue_growth","debt_to_equity","operating_margin"],
    "quarterly_financials_us": ["code","end_date","fp","revenue","op_income"],
    "ticker_master":           ["code","name","market"],
    "daily_price":             ["date_","code","open","high","low","close","volume","amount","changes_ratio"],
    "daily_marcap":            ["date_","code","close","marcap","volume","amount","stocks","rank"],
    "daily_fundamental":       ["date_","code","bps","per","pbr","eps","div","dps"],
    "quarterly_financials_kr": ["code","end_date","fp","revenue","op_income","net_income","total_equity","total_liabilities"],
}

for t, sql in TABLES.items():
    path = f"{OUT}/{t}.csv.gz"
    cur.execute(sql)
    n = 0
    with gzip.open(path, "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADERS[t])
        while True:
            rows = cur.fetchmany(50000)
            if not rows:
                break
            w.writerows(rows)
            n += len(rows)
    print(f"{t}: {n:,}행 → {path} ({os.path.getsize(path)/1e6:.1f}MB)", flush=True)

c.close()
print("백업 완료")
