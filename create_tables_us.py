#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
create_tables_us.py — 빈 Tibero(tibero, 8629)에 미국 주식 테이블 DDL 생성.
원래 DDL 파일이 없어 run_etl.py / etl_*.py 의 INSERT·SELECT 컬럼에서 역설계함.
인덱스는 대량 적재 속도를 위해 적재 후 별도 추가(add_indexes_us.py).
"""
import os
import jaydebeapi
import tb_conn  # 포트를 tip에서 자동 해결 (하드코딩 제거)

URL = tb_conn.URL
JAR = "/data/tibero7/tibero7/client/lib/jar/tibero7-jdbc.jar"

DDL = {
    "ticker_master_us": """
        CREATE TABLE ticker_master_us (
            code   VARCHAR2(20)  NOT NULL,
            name   VARCHAR2(200),
            sector VARCHAR2(100),
            market VARCHAR2(40),
            CONSTRAINT pk_ticker_master_us PRIMARY KEY (code)
        )""",
    "daily_price_us": """
        CREATE TABLE daily_price_us (
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
    "daily_marcap_us": """
        CREATE TABLE daily_marcap_us (
            date_  DATE        NOT NULL,
            code   VARCHAR2(20) NOT NULL,
            close  NUMBER,
            marcap NUMBER,
            stocks NUMBER,
            rank   NUMBER
        )""",
    "daily_fundamental_us": """
        CREATE TABLE daily_fundamental_us (
            date_            DATE        NOT NULL,
            code             VARCHAR2(20) NOT NULL,
            per              NUMBER,
            pbr              NUMBER,
            div              NUMBER,
            eps              NUMBER,
            eps_growth       NUMBER,
            roe              NUMBER,
            revenue_growth   NUMBER,
            debt_to_equity   NUMBER,
            operating_margin NUMBER
        )""",
    "quarterly_financials_us": """
        CREATE TABLE quarterly_financials_us (
            code      VARCHAR2(20) NOT NULL,
            end_date  DATE        NOT NULL,
            fp        VARCHAR2(10),
            revenue   NUMBER,
            op_income NUMBER,
            CONSTRAINT pk_quarterly_financials_us PRIMARY KEY (code, end_date)
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
        print(f"  CREATE {name}  FAIL: {str(e)[:120]}")

cur.execute("SELECT table_name FROM all_tables "
            "WHERE owner='SYS' AND (table_name LIKE '%_US' OR table_name LIKE 'QUARTERLY%') "
            "ORDER BY table_name")
print("생성된 테이블:", [r[0] for r in cur.fetchall()])
conn.close()
print("완료")
