#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db_backup.py — 주식 테이블(미장 5개 + 국장 5개)을 CSV.gz로 덤프 (Tibero 재설치 사고 대비).
복원: create_tables_us.py/create_tables_kr.py로 테이블 만든 뒤 db_restore.py로 적재.
"""
import tb_conn  # 포트를 tip에서 자동 해결 (하드코딩 제거)
import os
import csv, gzip, os
import jaydebeapi as j

OUT = "/data/frame/db_backup"
os.makedirs(OUT, exist_ok=True)
c = j.connect("com.tmax.tibero.jdbc.TbDriver", tb_conn.URL,
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


# --- 안전장치: 지금 DB가 정상인지 먼저 확인. 비정상이면 기존 백업을 덮지 않는다 ---
# (2026-07-30: 서버 재설치로 3주 유실. 복구 도중 크론이 돌아 반쪽 DB가 정상 백업을 덮는 사고 방지)
import json, shutil, datetime, sys
ARCH = "/data/frame/db_backup_archive"
os.makedirs(ARCH, exist_ok=True)
FORCE = "--force" in sys.argv

def _c(q):
    cur.execute(q); return cur.fetchone()[0]

live = {
    "price_us_tickers": _c("SELECT COUNT(DISTINCT code) FROM daily_price_us"),
    "price_us_rows":    _c("SELECT COUNT(*) FROM daily_price_us"),
    "marcap_us_rows":   _c("SELECT COUNT(*) FROM daily_marcap_us"),
}
meta = ARCH + "/last_good.json"
prev = json.load(open(meta)) if os.path.exists(meta) else {}
bad = [k for k, v in live.items() if prev.get(k) and v < prev[k] * 0.5]
if bad and not FORCE:
    print("!! 백업 중단 - 직전 정상 백업의 절반 미만 (복구중/사고 의심)")
    for k in bad:
        print("   %s: 지금 %s vs 직전 %s" % (k, format(live[k], ","), format(prev[k], ",")))
    print("   기존 백업 보호를 위해 덮어쓰지 않았습니다. 강제 실행: --force")
    c.close(); sys.exit(1)
print("사전 점검 OK:", live, flush=True)

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

# --- 날짜별 아카이브 + 보관정책(최근 8개) + 정상 지표 기록 ---
stamp = datetime.datetime.now().strftime("%Y%m%d")
dest = ARCH + "/" + stamp
os.makedirs(dest, exist_ok=True)
for fn in sorted(os.listdir(OUT)):
    if fn.endswith(".csv.gz"):
        shutil.copy2(OUT + "/" + fn, dest + "/" + fn)
json.dump(live, open(meta, "w"), indent=1)
snaps = sorted(d for d in os.listdir(ARCH) if d.isdigit())
for old in snaps[:-8]:
    shutil.rmtree(ARCH + "/" + old, ignore_errors=True)
    print("  오래된 스냅샷 삭제: " + old)
print("백업 완료 - 아카이브 %s / 보관 %d개" % (dest, len(snaps[-8:])))

