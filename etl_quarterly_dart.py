#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
etl_quarterly_dart.py — DART OpenAPI 분기 재무제표(매출액/영업이익/당기순이익/자본·부채총계) 적재.
quarterly_financials_us(SEC EDGAR)의 국장 대응판. quarterly_financials_kr 테이블에 적재.
2026-07-08 ROE/부채비율 게이트용 net_income/total_equity/total_liabilities 컬럼 추가
(같은 fnlttSinglAcnt 응답에 이미 포함돼 있던 항목이라 API 호출은 늘지 않음).

계정 매핑 확인(2026-07-08, 삼성전자 실측):
  - 1분기(11013)/반기(11012)/3분기(11014) 보고서의 매출액·영업이익·당기순이익 thstrm_amount는
    이미 '해당 분기 단독' 값 (분기 시작~끝 3개월 duration, 누적이 아님).
  - 사업보고서(11011)만 연간 누적값 -> 4분기 단독값 = 연간 - (Q1+Q2+Q3 단독 합)
  - 자본총계/부채총계는 재무상태표(BS) 항목이라 애초에 특정 시점 스냅샷 -> 분기별 보고서 값을
    그대로 쓰면 됨 (매출/영업이익/순이익처럼 4분기 역산할 필요 없음)
  - fs_div 파라미터는 응답을 필터링하지 않으므로 각 항목의 fs_div 필드로 직접 골라야 함
    (CFS 우선, 없으면 OFS로 대체 - 연결재무제표 미작성 기업 대응)
"""
import os
import sys
import time
import json
import xml.etree.ElementTree as ET
import urllib.request
import jaydebeapi
import tb_conn  # 포트를 tip에서 자동 해결 (하드코딩 제거)

DART_KEY = __import__("os").environ["DART_API_KEY"]
JAR = "/data/tibero7/tibero7/client/lib/jar/tibero7-jdbc.jar"
URL = tb_conn.URL
DELAY = 0.15

START_YEAR = 2018
END_YEAR = 2026
REPRT_CODES = ["11013", "11012", "11014", "11011"]  # Q1, 반기, Q3, 사업(연간)
QUARTER_END = {"11013": "-03-31", "11012": "-06-30", "11014": "-09-30", "11011": "-12-31"}

REV_NAMES = ["매출액", "영업수익", "수익(매출액)"]
OP_NAMES = ["영업이익", "영업이익(손실)"]
NI_NAMES = ["당기순이익(손실)", "당기순이익"]
EQUITY_NAMES = ["자본총계"]
LIAB_NAMES = ["부채총계"]


def load_corp_code_map(path="/tmp/CORPCODE.xml"):
    tree = ET.parse(path)
    m = {}
    for item in tree.getroot().findall("list"):
        sc = item.findtext("stock_code", "").strip()
        if sc:
            m[sc] = item.findtext("corp_code")
    return m


def resolve_targets(codes, code_map):
    """종목코드 -> corp_code. 우선주 등 매핑 실패 시 마지막 자리 0으로 재시도."""
    resolved = {}
    for code in codes:
        if code in code_map:
            resolved[code] = code_map[code]
        else:
            base = code[:-1] + "0"
            if base in code_map:
                resolved[code] = code_map[base]
    return resolved


def fetch_report(corp_code, year, reprt_code):
    url = (f"https://opendart.fss.or.kr/api/fnlttSinglAcnt.json"
           f"?crtfc_key={DART_KEY}&corp_code={corp_code}"
           f"&bsns_year={year}&reprt_code={reprt_code}")
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            d = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return None, f"요청실패: {e!r}"
    if d.get("status") != "000":
        return None, d.get("message", d.get("status"))
    return d.get("list", []), None


def extract_amount(items, names, fs_pref=("CFS", "OFS")):
    by_fs = {}
    for it in items:
        if it.get("account_nm") in names:
            by_fs.setdefault(it.get("fs_div"), it)
    for fs in fs_pref:
        it = by_fs.get(fs)
        if it and it.get("thstrm_amount"):
            try:
                return float(it["thstrm_amount"].replace(",", ""))
            except (ValueError, AttributeError):
                pass
    return None


def get_quarter_financials(corp_code, year):
    """연도별 4개 보고서를 모두 가져와 (분기말, 매출액, 영업이익, 당기순이익, 자본총계, 부채총계) 반환."""
    raw = {}
    for rc in REPRT_CODES:
        items, err = fetch_report(corp_code, year, rc)
        time.sleep(DELAY)
        if items is None:
            raw[rc] = (None, None, None, None, None)
            continue
        rev = extract_amount(items, REV_NAMES)
        op = extract_amount(items, OP_NAMES)
        ni = extract_amount(items, NI_NAMES)
        eq = extract_amount(items, EQUITY_NAMES)
        li = extract_amount(items, LIAB_NAMES)
        raw[rc] = (rev, op, ni, eq, li)

    q1_rev, q1_op, q1_ni, q1_eq, q1_li = raw["11013"]
    h1_rev, h1_op, h1_ni, h1_eq, h1_li = raw["11012"]
    q3_rev, q3_op, q3_ni, q3_eq, q3_li = raw["11014"]
    fy_rev, fy_op, fy_ni, fy_eq, fy_li = raw["11011"]

    # 자본총계/부채총계는 BS 스냅샷이므로 보고서 값을 그대로 사용 (역산 불필요)
    out = []
    out.append((f"{year}-03-31", q1_rev, q1_op, q1_ni, q1_eq, q1_li))
    out.append((f"{year}-06-30", h1_rev, h1_op, h1_ni, h1_eq, h1_li))
    out.append((f"{year}-09-30", q3_rev, q3_op, q3_ni, q3_eq, q3_li))
    # Q1/반기/3분기 보고서의 thstrm_amount는 이미 '해당 분기 단독' 값이므로 (IS 항목만 해당)
    # 4분기 단독 = 연간누적(사업보고서) - (Q1+Q2+Q3 단독 합)
    # rev/op/ni 각각 독립적으로 역산 (매출액 없는 금융업 등도 영업이익/순이익은 역산되게)
    def _q4(fy, q1, h1, q3):
        return fy - (q1 + h1 + q3) if None not in (fy, q1, h1, q3) else None

    q4_rev = _q4(fy_rev, q1_rev, h1_rev, q3_rev)
    q4_op  = _q4(fy_op, q1_op, h1_op, q3_op)
    q4_ni  = _q4(fy_ni, q1_ni, h1_ni, q3_ni)
    out.append((f"{year}-12-31", q4_rev, q4_op, q4_ni, fy_eq, fy_li))
    return out


def main():
    test_mode = "--test" in sys.argv
    code_map = load_corp_code_map()

    conn = jaydebeapi.connect("com.tmax.tibero.jdbc.TbDriver", URL, [os.environ.get("TIBERO_USER", "sys"), os.environ.get("TIBERO_PASS", "")], JAR)
    cur = conn.cursor()
    cur.execute("SELECT code FROM daily_marcap WHERE date_=(SELECT MAX(date_) FROM daily_marcap) AND rank<=110 ORDER BY rank")
    target_codes = [r[0] for r in cur.fetchall()]

    resolved = resolve_targets(target_codes, code_map)
    missing = [c for c in target_codes if c not in resolved]
    print(f"대상 {len(target_codes)}개 중 corp_code 매핑 {len(resolved)}개, 실패 {len(missing)}개: {missing}")

    if test_mode:
        target_codes = target_codes[:3]
        print(f"[TEST MODE] {target_codes} 만 실행")

    corp_to_codes = {}
    for code in target_codes:
        cc = resolved.get(code)
        if cc:
            corp_to_codes.setdefault(cc, []).append(code)

    ins_sql = """INSERT INTO quarterly_financials_kr
                 (code, end_date, revenue, op_income, net_income, total_equity, total_liabilities)
                 VALUES (?, TO_DATE(?, 'YYYY-MM-DD'), ?, ?, ?, ?, ?)"""

    total_rows = 0
    n_corps = len(corp_to_codes)
    for i, (corp_code, codes) in enumerate(corp_to_codes.items(), 1):
        print(f"[{i}/{n_corps}] corp_code={corp_code} codes={codes}", flush=True)
        rows_for_corp = []
        for year in range(START_YEAR, END_YEAR + 1):
            for end_date, rev, op, ni, eq, li in get_quarter_financials(corp_code, year):
                # 매출/영업이익/순이익 전부 없어도 자본·부채총계(은행/보험 등)는 있을 수 있음
                # -> 전부 None일 때만 스킵 (ROE/D-E 게이트가 금융업종도 쓸 수 있게)
                if all(v is None for v in (rev, op, ni, eq, li)):
                    continue
                rows_for_corp.append((end_date, rev, op, ni, eq, li))
        for code in codes:
            for end_date, rev, op, ni, eq, li in rows_for_corp:
                cur.execute(ins_sql, [code, end_date, rev, op, ni, eq, li])
                total_rows += 1
        conn.commit()
        print(f"  -> {len(rows_for_corp)}분기 x {len(codes)}종목 적재 (누적 {total_rows}행)", flush=True)

    cur.close()
    conn.close()
    print(f"완료: 총 {total_rows}행 적재")


if __name__ == "__main__":
    main()
