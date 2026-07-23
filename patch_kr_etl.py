#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_etl.py의 run_kr_etl 휴장일 방어 패치 (원격에서 실행)."""
PATH = "/data/frame/run_etl.py"

OLD = '''        cur = conn.cursor()
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

            # daily_marcap
            df_m = krx.get_market_cap_by_ticker(ds, market="KOSPI")
            time.sleep(DELAY)
            n2 = 0
            if df_m is not None and not df_m.empty:
                df_m.index.name = "code"
                df_m = df_m.reset_index().rename(columns={
                    "종가":"close","시가총액":"marcap",
                    "거래량":"volume","거래대금":"amount","상장주식수":"stocks"
                })
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
            n3 = 0
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

            conn.commit()'''

NEW = '''        cur = conn.cursor()
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

            conn.commit()'''

with open(PATH, "r", encoding="utf-8") as f:
    src = f.read()

n = src.count(OLD)
if n != 1:
    print(f"FAIL: OLD block found {n} times (expected 1)")
    raise SystemExit(1)

src = src.replace(OLD, NEW)
with open(PATH, "w", encoding="utf-8") as f:
    f.write(src)
print("PATCH_OK")
