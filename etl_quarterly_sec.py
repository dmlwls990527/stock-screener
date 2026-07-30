"""
SEC EDGAR → quarterly_financials_us ETL
- Top 100 US 종목 분기별 매출/영업이익 히스토리 수집
- 여러 회계기준 태그 통합 (ASC606 전후 합치기)
- 중복 제거 후 UPSERT
"""
import os
import jaydebeapi
import urllib.request
import json
import time
import logging
from datetime import datetime
import tb_conn  # 포트를 tip에서 자동 해결 (하드코딩 제거)

logging.basicConfig(
    filename="/tmp/etl_quarterly_sec.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger()

HEADERS = {"User-Agent": "stock-analysis research@study.com"}

REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueGoodsNet",
    "NetRevenues",
]
OPINCOME_TAGS = [
    "OperatingIncomeLoss",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
]

def sec_get(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if i < retries - 1:
                time.sleep(2 ** i)
            else:
                raise e

def extract_quarters(us_gaap, tags):
    """
    여러 태그에서 단일분기 데이터 수집 → end_date 기준 중복 제거 후 병합
    반환: {end_date: val}
    """
    combined = {}  # end_date → val

    for tag in tags:
        if tag not in us_gaap:
            continue
        usd = us_gaap[tag].get("units", {}).get("USD", [])

        # 방법1: start 있고 단일 분기 (70~100일)
        singles = []
        for d in usd:
            if d.get("form") not in ("10-Q", "10-K"):
                continue
            if d.get("start") and d.get("end"):
                try:
                    days = (datetime.strptime(d["end"], "%Y-%m-%d")
                            - datetime.strptime(d["start"], "%Y-%m-%d")).days
                    if 70 <= days <= 100:
                        singles.append(d)
                except Exception:
                    pass
            elif d.get("form") == "10-Q" and d.get("fp","").startswith("Q") and not d.get("start"):
                singles.append(d)

        # 같은 end_date 중 accn 최신만
        for d in singles:
            end = d["end"]
            if end not in combined:
                combined[end] = d
            elif d.get("accn","") > combined[end].get("accn",""):
                combined[end] = d

    return {k: v["val"] for k, v in combined.items()}


def main():
    # ── DB 연결 ──────────────────────────────────────────────
    conn = jaydebeapi.connect(
        'com.tmax.tibero.jdbc.TbDriver',
        tb_conn.URL,
        [os.environ.get('TIBERO_USER', 'sys'), os.environ.get('TIBERO_PASS', '')],
        '/data/tibero7/tibero7/client/lib/jar/tibero7-jdbc.jar'
    )
    conn.jconn.setAutoCommit(False)
    cur = conn.cursor()

    # ── 유니버스 조회 ─────────────────────────────────────────
    # 예전엔 시총 상위 100개만 가져왔음(FETCH FIRST 100). 유니버스 확장 1단계로
    # 가격 있는 종목 전체(현재 ~524종)의 재무를 채운다. 환경변수 SEC_LIMIT로 상한 조절 가능(기본=전체).
    limit = os.environ.get("SEC_LIMIT", "").strip()
    limit_sql = f"FETCH FIRST {int(limit)} ROWS ONLY" if limit.isdigit() else ""
    cur.execute(f"""
        SELECT code FROM daily_marcap_us
        WHERE date_ = (SELECT MAX(date_) FROM daily_marcap_us)
        ORDER BY marcap DESC
        {limit_sql}
    """)
    universe = [r[0] for r in cur.fetchall()]
    log.info(f"유니버스 {len(universe)}종목")
    print(f"유니버스: {len(universe)}종목")

    # ── ticker → CIK 매핑 ────────────────────────────────────
    tickers_data = sec_get("https://www.sec.gov/files/company_tickers.json")
    ticker_to_cik = {v['ticker']: str(v['cik_str']).zfill(10)
                     for v in tickers_data.values()}

    # ── 종목별 수집 ───────────────────────────────────────────
    upsert_sql = """
        MERGE INTO quarterly_financials_us t
        USING (SELECT ? AS code, TO_DATE(?, 'YYYY-MM-DD') AS end_date,
                      ? AS fp, ? AS revenue, ? AS op_income FROM dual) s
        ON (t.code = s.code AND t.end_date = s.end_date)
        WHEN MATCHED THEN
            UPDATE SET fp=s.fp, revenue=s.revenue, op_income=s.op_income
        WHEN NOT MATCHED THEN
            INSERT (code, end_date, fp, revenue, op_income)
            VALUES (s.code, s.end_date, s.fp, s.revenue, s.op_income)
    """

    ok_cnt, skip_cnt, err_cnt = 0, 0, 0

    for i, sym in enumerate(universe, 1):
        cik = ticker_to_cik.get(sym)
        if not cik:
            log.warning(f"[{sym}] CIK 없음")
            skip_cnt += 1
            print(f"  [{i:03d}/{len(universe)}] {sym:<8} CIK 없음 (스킵)")
            continue

        try:
            facts = sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
            ug = facts.get("facts", {}).get("us-gaap", {})

            rev_map = extract_quarters(ug, REVENUE_TAGS)
            op_map  = extract_quarters(ug, OPINCOME_TAGS)

            if not rev_map:
                log.warning(f"[{sym}] 매출 데이터 없음")
                skip_cnt += 1
                print(f"  [{i:03d}/{len(universe)}] {sym:<8} 매출 데이터 없음 (스킵)")
                continue

            # 모든 분기 날짜 (매출 기준)
            rows = []
            for end_date, rev_val in rev_map.items():
                op_val = op_map.get(end_date)
                rows.append((sym, end_date, None, rev_val, op_val))

            # UPSERT
            for row in rows:
                cur.execute(upsert_sql, list(row))
            conn.commit()

            oldest = min(rev_map.keys())
            newest = max(rev_map.keys())
            log.info(f"[{sym}] {len(rows)}분기 저장 ({oldest} ~ {newest})")
            print(f"  [{i:03d}/{len(universe)}] {sym:<8} {len(rows):3d}분기 ({oldest} ~ {newest})")
            ok_cnt += 1

        except Exception as e:
            log.error(f"[{sym}] 오류: {e}")
            err_cnt += 1
            print(f"  [{i:03d}/{len(universe)}] {sym:<8} 오류: {e}")

        time.sleep(0.35)  # SEC rate limit (10 req/sec 제한)

    print(f"\n완료: 성공={ok_cnt}, 스킵={skip_cnt}, 오류={err_cnt}")
    log.info(f"완료: 성공={ok_cnt}, 스킵={skip_cnt}, 오류={err_cnt}")

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
