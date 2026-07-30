#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
create_tables_kr.py — 빈 Tibero에 국내(KR) 주식 테이블 DDL 생성.
컬럼은 run_etl.py의 run_kr_etl() INSERT문에서 정확히 역설계함.
"""
import os
import jaydebeapi
import tb_conn  # 포트를 tip에서 자동 해결 (하드코딩 제거)

URL = tb_conn.URL
JAR = "/data/tibero7/tibero7/client/lib/jar/tibero7-jdbc.jar"

DDL = {
    "ticker_master": """
        CREATE TABLE ticker_master (
            code   VARCHAR2(20)  NOT NULL,
            name   VARCHAR2(200),
            market VARCHAR2(40),
            CONSTRAINT pk_ticker_master PRIMARY KEY (code)
        )""",
    "daily_price": """
        CREATE TABLE daily_price (
            date_         DATE        NOT NULL,
            code          VARCHAR2(20) NOT NULL,
            open          NUMBER,
            high          NUMBER,
            low           NUMBER,
            close         NUMBER,
            volume        NUMBER,
            amount        NUMBER,
            changes_ratio NUMBER
        )""",
    "daily_marcap": """
        CREATE TABLE daily_marcap (
            date_  DATE        NOT NULL,
            code   VARCHAR2(20) NOT NULL,
            close  NUMBER,
            marcap NUMBER,
            volume NUMBER,
            amount NUMBER,
            stocks NUMBER,
            rank   NUMBER
        )""",
    "daily_fundamental": """
        CREATE TABLE daily_fundamental (
            date_ DATE        NOT NULL,
            code  VARCHAR2(20) NOT NULL,
            bps   NUMBER,
            per   NUMBER,
            pbr   NUMBER,
            eps   NUMBER,
            div   NUMBER,
            dps   NUMBER
        )""",
    "quarterly_financials_kr": """
        CREATE TABLE quarterly_financials_kr (
            code               VARCHAR2(20) NOT NULL,
            end_date           DATE        NOT NULL,
            fp                 VARCHAR2(10),
            revenue            NUMBER,
            op_income          NUMBER,
            net_income         NUMBER,
            total_equity       NUMBER,
            total_liabilities  NUMBER,
            CONSTRAINT pk_quarterly_financials_kr PRIMARY KEY (code, end_date)
        )""",
}

conn = jaydebeapi.connect("com.tmax.tibero.jdbc.TbDriver", URL, [os.environ.get("TIBERO_USER", "sys"), os.environ.get("TIBERO_PASS", "")], JAR)
conn.jconn.setAutoCommit(True)
cur = conn.cursor()

for t in DDL:
    try:
        cur.execute(f"DROP TABLE {t}")
        print(f"  drop {t}")
    except Exception as e:
        print(f"  (no drop {t}: {str(e)[:50]})")

for name, ddl in DDL.items():
    try:
        cur.execute(ddl)
        print(f"  CREATE {name}  OK")
    except Exception as e:
        print(f"  CREATE {name}  FAIL: {str(e)[:150]}")

cur.execute("SELECT table_name FROM all_tables WHERE owner='SYS' AND "
            "table_name IN ('TICKER_MASTER','DAILY_PRICE','DAILY_MARCAP','DAILY_FUNDAMENTAL','QUARTERLY_FINANCIALS_KR') "
            "ORDER BY table_name")
print("생성된 KR 테이블:", [r[0] for r in cur.fetchall()])
conn.close()
print("완료")
