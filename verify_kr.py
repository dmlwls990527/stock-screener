#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KR 백필 완료 후 품질 검증 (백업 전 최종 체크)."""
import os
import jaydebeapi

URL = "jdbc:tibero:thin:@localhost:44123:tibero"
JAR = "/data/tibero7/tibero7/client/lib/jar/tibero7-jdbc.jar"

conn = jaydebeapi.connect("com.tmax.tibero.jdbc.TbDriver", URL, [os.environ.get("TIBERO_USER", "sys"), os.environ.get("TIBERO_PASS", "")], JAR)
cur = conn.cursor()


def q(sql):
    cur.execute(sql)
    return cur.fetchall()


print("=== 1. 테이블별 행수 / 날짜범위 ===")
print("ticker_master:", q("SELECT COUNT(*) FROM ticker_master"))
for t in ("daily_price", "daily_marcap", "daily_fundamental"):
    r = q(f"SELECT COUNT(*), COUNT(DISTINCT date_), MIN(date_), MAX(date_) FROM {t}")
    print(f"{t}: {r}")

print("\n=== 2. 중복 (date_, code) 체크 (기대값: 모두 0) ===")
for t in ("daily_price", "daily_marcap", "daily_fundamental"):
    r = q(f"""SELECT COUNT(*) FROM (
                SELECT date_, code FROM {t} GROUP BY date_, code HAVING COUNT(*) > 1
              )""")
    print(f"{t} 중복쌍: {r[0][0]}")

print("\n=== 3. 공휴일 phantom row 체크 (기대값: 모두 0행) ===")
holidays = [
    ("20160208", "2016 설날연휴"), ("20160209", "2016 설날"), ("20160210", "2016 설날연휴"),
    ("20170127", "2017 설날연휴"), ("20170130", "2017 설날"),
    ("20200124", "2020 설날연휴"), ("20200127", "2020 설날연휴"),
    ("20201225", "2020 성탄절"),
    ("20220201", "2022 설날"),
    ("20240209", "2024 설날연휴"), ("20240212", "2024 설날연휴"),
    ("20260101", "2026 신정"),
]
for ds, label in holidays:
    row = []
    for t in ("daily_price", "daily_marcap", "daily_fundamental"):
        r = q(f"SELECT COUNT(*) FROM {t} WHERE date_ = TO_DATE('{ds}','YYYYMMDD')")
        row.append(r[0][0])
    status = "OK(전부0)" if row == [0, 0, 0] else f"** 이상 감지 **"
    print(f"{ds} {label}: price={row[0]} marcap={row[1]} fund={row[2]}  {status}")

print("\n=== 4. 안전필터 잔존 오염 체크 (기대값: 모두 0) ===")
print("marcap<=0 잔존행:", q("SELECT COUNT(*) FROM daily_marcap WHERE marcap <= 0")[0][0])
print("price close<=0 AND volume<=0 잔존행:",
      q("SELECT COUNT(*) FROM daily_price WHERE close <= 0 AND volume <= 0")[0][0])

print("\n=== 5. 대표 종목 연속성 체크 (삼성전자 005930, SK하이닉스 000660) ===")
total_dates = q("SELECT COUNT(DISTINCT date_) FROM daily_price")[0][0]
for code, name in [("005930", "삼성전자"), ("000660", "SK하이닉스")]:
    r = q(f"SELECT COUNT(*), MIN(date_), MAX(date_) FROM daily_price WHERE code='{code}'")
    print(f"{name}({code}): {r[0][0]}행 / 전체거래일 {total_dates}일  범위={r[0][1]}~{r[0][2]}")

print("\n=== 6. 연도별 일평균 종목수 추이 (이상치 없는지) ===")
r = q("""SELECT TO_CHAR(date_,'YYYY') yr, COUNT(*)/COUNT(DISTINCT date_) avg_cnt
         FROM daily_price GROUP BY TO_CHAR(date_,'YYYY') ORDER BY yr""")
for yr, avg_cnt in r:
    print(f"{yr}: 일평균 {avg_cnt:.0f}종목")

cur.close()
conn.close()
print("\n검증 스크립트 완료")
