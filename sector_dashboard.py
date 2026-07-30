#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sector_dashboard.py — KOSPI 전체 섹터 현황·추이 Excel 대시보드.

데이터: Tibero DB(port 8629) daily_price / daily_marcap / daily_fundamental / ticker_master.
섹터 분류: screen_sector.py 의 SECTOR_DEFS(종목코드 + 이름키워드) 재사용(단일 출처).
투자자 순매수(--flows, 기본 ON): pykrx, 환경변수 KRX_ID / KRX_PW 필요(없으면 자동 생략).

실행:
  source /data/tibero7/t7.profile && source /data/frame/.venv/bin/activate
  python3 sector_dashboard.py                  # 최근 12개월, 순매수 포함(가능 시)
  python3 sector_dashboard.py --months 24      # 분석 기간 변경
  python3 sector_dashboard.py --no-flows       # 투자자 순매수 생략(외부조회 안 함)
  python3 sector_dashboard.py --out /tmp/x.xlsx

생성 시트: 섹터현황 / 월별_시총추이 / 월별_거래대금추이 / [투자자순매수] / 종목상세
"""
import os
import argparse, ast, os
from datetime import date
import numpy as np
import pandas as pd
import jaydebeapi as j
from openpyxl.chart import BarChart, LineChart, Reference
import tb_conn  # 포트를 tip에서 자동 해결 (하드코딩 제거)

TIBERO_URL = tb_conn.URL
JDBC_JAR   = "/data/tibero7/tibero7/client/lib/jar/tibero7-jdbc.jar"
SS_PATH    = "/data/frame/screen.py"
JO = 1e12   # 조원


def load_sector_defs(path=SS_PATH):
    """screen_sector.py 를 실행하지 않고 SECTOR_DEFS 리터럴만 AST로 추출."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in tgts:
                if isinstance(t, ast.Name) and t.id == "SECTOR_DEFS":
                    return ast.literal_eval(node.value)
    raise ValueError("SECTOR_DEFS 를 찾지 못했습니다")


def assign_sectors(tm, defs):
    """ticker_master(code,name)에 섹터 부여: 명시코드 우선 → 이름키워드 → 기타."""
    code_map = {}
    for sec, codes, _ in defs:
        for c in codes:
            c = c.strip()
            if c and c not in code_map:
                code_map[c] = sec
    tm = tm.copy()
    tm["sector"] = tm["code"].map(code_map)
    mask = tm["sector"].isna()
    for sec, _, kws in defs:
        if not kws:
            continue
        hit = mask & tm["name"].str.contains("|".join(kws), na=False)
        tm.loc[hit, "sector"] = sec
        mask = tm["sector"].isna()
    tm["sector"] = tm["sector"].fillna("기타")
    return tm


def qdf(cur, sql, params=None):
    cur.execute(sql, params or [])
    cols = [d[0].lower() for d in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)


def main():
    ap = argparse.ArgumentParser(description="KOSPI 섹터 현황·추이 대시보드")
    ap.add_argument("--months", type=int, default=12, help="분석 기간(개월), 기본 12")
    ap.add_argument("--no-flows", action="store_true", help="투자자 순매수 생략(외부조회 안 함)")
    ap.add_argument("--out", default=None, help="출력 xlsx 경로")
    args = ap.parse_args()

    defs = load_sector_defs()
    conn = j.connect("com.tmax.tibero.jdbc.TbDriver", TIBERO_URL, [os.environ.get("TIBERO_USER", "sys"), os.environ.get("TIBERO_PASS", "")], JDBC_JAR)
    cur = conn.cursor()
    cur.execute("SELECT TO_CHAR(MAX(date_),'YYYY-MM-DD') FROM daily_price")
    end_s = cur.fetchone()[0]
    end_ts = pd.Timestamp(end_s)
    buf = (end_ts - pd.DateOffset(months=args.months) - pd.Timedelta(days=12)).strftime("%Y-%m-%d")

    tm = assign_sectors(qdf(cur, "SELECT code,name FROM ticker_master"), defs)
    name_map = dict(zip(tm.code, tm.name))

    price = qdf(cur, "SELECT TO_CHAR(date_,'YYYY-MM-DD') d,code,close,amount FROM daily_price "
                     "WHERE date_>=TO_DATE(?,'YYYY-MM-DD')", [buf])
    mc    = qdf(cur, "SELECT TO_CHAR(date_,'YYYY-MM-DD') d,code,marcap,stocks FROM daily_marcap "
                     "WHERE date_>=TO_DATE(?,'YYYY-MM-DD')", [buf])
    fund  = qdf(cur, "SELECT TO_CHAR(date_,'YYYY-MM-DD') d,code,per,pbr,div FROM daily_fundamental "
                     "WHERE date_>=TO_DATE(?,'YYYY-MM-DD')", [buf])
    conn.close()
    for df in (price, mc, fund):
        for c in df.columns:
            if c not in ("d", "code"):
                df[c] = pd.to_numeric(df[c], errors="coerce")

    close_p = price.pivot_table(index="d", columns="code", values="close").sort_index().ffill()
    amt_p   = price.pivot_table(index="d", columns="code", values="amount").sort_index()
    mc_p    = mc.pivot_table(index="d", columns="code", values="marcap").sort_index().ffill()
    per_p   = fund.pivot_table(index="d", columns="code", values="per").sort_index().ffill()
    pbr_p   = fund.pivot_table(index="d", columns="code", values="pbr").sort_index().ffill()
    div_p   = fund.pivot_table(index="d", columns="code", values="div").sort_index().ffill()

    dates = list(close_p.index)
    end_d = dates[-1]

    def at(piv, c, d=None):
        d = d or end_d
        if c in piv.columns:
            v = piv.loc[d, c]
            return float(v) if pd.notna(v) else None
        return None

    def anchor(months):
        tgt = (end_ts - pd.DateOffset(months=months)).strftime("%Y-%m-%d")
        cand = [d for d in dates if d <= tgt]
        return cand[-1] if cand else dates[0]

    start_d = anchor(args.months)
    win = [d for d in dates if d >= start_d]
    manchor = pd.Series(win).groupby(pd.Series(win).str[:7]).max().tolist()
    horizons = [h for h in (1, 3, 6, 12) if h <= args.months]

    tot_mc_end = float(mc_p.loc[end_d].sum())
    mkt_amt_recent = float(amt_p.loc[win[-20:]].sum(axis=1).sum())

    # 투자자 순매수(선택)
    flows = None
    if not args.no_flows:
        try:
            if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
                raise RuntimeError("KRX_ID/KRX_PW 환경변수 없음")
            from pykrx import stock as krx
            fd, td = start_d.replace("-", ""), end_d.replace("-", "")
            flows = {}
            for inv in ("외국인", "기관합계", "개인", "연기금", "금융투자"):
                parts = []
                for mkt in ("KOSPI", "KOSDAQ"):
                    try:
                        parts.append(krx.get_market_net_purchases_of_equities(fd, td, mkt, inv)["순매수거래대금"])
                    except Exception:
                        pass
                if parts:
                    flows[inv] = pd.concat(parts)
            print(f"  [정보] 투자자 순매수 조회 완료 (KOSPI+KOSDAQ, {len(flows)}구분)")
        except Exception as e:
            print(f"  [경고] 투자자 순매수 생략: {e}")
            flows = None

    sec_order = [s for s, _, _ in defs] + ["기타"]
    rows, monthly_mc, monthly_amt, stock_rows = [], {}, {}, []

    for sec in sec_order:
        codes = [c for c in tm[tm.sector == sec].code if c in close_p.columns and c in mc_p.columns]
        if not codes:
            continue
        mc_e = mc_p.loc[end_d, codes].dropna()
        sec_mc = float(mc_e.sum())

        def capret(d0, d1=end_d):
            c0, c1, w = close_p.loc[d0, codes], close_p.loc[d1, codes], mc_p.loc[d0, codes]
            v = c0.notna() & c1.notna() & w.notna() & (c0 > 0) & (w > 0)
            if not v.any():
                return np.nan
            r = c1[v] / c0[v] - 1
            return float(((w[v] / w[v].sum()) * r).sum() * 100)

        hret = {h: capret(anchor(h)) for h in horizons}
        # 전체기간 시총가중/동일가중/중앙값
        c0, c1, w0 = close_p.loc[start_d, codes], close_p.loc[end_d, codes], mc_p.loc[start_d, codes]
        v = c0.notna() & c1.notna() & w0.notna() & (c0 > 0) & (w0 > 0)
        r = (c1[v] / c0[v] - 1) * 100
        ww = w0[v] / w0[v].sum()
        capW = float((ww * r).sum()) if v.any() else np.nan
        eqW = float(r.mean()) if v.any() else np.nan
        medW = float(r.median()) if v.any() else np.nan
        # 변동성(시총가중 일간수익률, 시작 가중 고정)
        vcodes = list(c0[v].index)
        dr = close_p[vcodes].pct_change().loc[win]
        sret = (dr * (w0[v] / w0[v].sum())).sum(axis=1)
        vol = float(sret.std() * np.sqrt(252) * 100) if len(sret) > 2 else np.nan
        # 거래대금
        amt_win = amt_p[codes].sum(axis=1).loc[win]
        amt_total = float(amt_win.sum())
        amt_recent = float(amt_win.iloc[-20:].mean())
        amt_share = float(amt_win.iloc[-20:].sum() / mkt_amt_recent * 100) if mkt_amt_recent else np.nan
        # 밸류에이션(중앙값, PER은 양수만)
        def med(piv, pos=False):
            s = piv.loc[end_d, [c for c in codes if c in piv.columns]]
            s = s.replace([np.inf, -np.inf], np.nan).dropna()
            if pos:
                s = s[s > 0]
            return float(s.median()) if len(s) else np.nan
        per_m, pbr_m, div_m = med(per_p, pos=True), med(pbr_p), med(div_p)

        row = {"섹터": sec, "구성종목수": len(codes),
               "섹터시총_조": round(sec_mc / JO, 1), "시총비중%": round(sec_mc / tot_mc_end * 100, 1)}
        for h in horizons:
            row[f"{h}M수익률%"] = round(hret[h], 1) if pd.notna(hret[h]) else None
        row.update({"시총가중수익률%": round(capW, 1) if pd.notna(capW) else None,
                    "동일가중수익률%": round(eqW, 1) if pd.notna(eqW) else None,
                    "중앙값수익률%": round(medW, 1) if pd.notna(medW) else None,
                    "변동성%": round(vol, 1) if pd.notna(vol) else None,
                    "거래대금_누적_조": round(amt_total / JO, 1),
                    "거래대금_일평균_조": round(amt_recent / JO, 2),
                    "거래대금비중%": round(amt_share, 1) if pd.notna(amt_share) else None,
                    "PER중앙값": round(per_m, 1) if pd.notna(per_m) else None,
                    "PBR중앙값": round(pbr_m, 2) if pd.notna(pbr_m) else None,
                    "배당%중앙값": round(div_m, 2) if pd.notna(div_m) else None})

        if flows is not None:
            for inv, lab in (("외국인", "외국인순매수_조"), ("기관합계", "기관순매수_조"), ("개인", "개인순매수_조"),
                             ("연기금", "연기금순매수_조"), ("금융투자", "금융투자순매수_조")):
                if inv in flows:
                    s = flows[inv]
                    row[lab] = round(sum(float(s[c]) for c in codes if c in s.index) / JO, 2)

        rows.append(row)
        monthly_mc[sec]  = {m[:7]: round(float(mc_p.loc[m, codes].sum()) / JO, 1) for m in manchor}
        monthly_amt[sec] = {m[:7]: round(float(amt_p[codes].loc[[d for d in win if d[:7] == m[:7]]].sum().sum()) / JO, 1) for m in manchor}

        # 종목상세(정의된 섹터만)
        if sec != "기타":
            for c in codes:
                cs, ce = at(close_p, c, start_d), at(close_p, c, end_d)
                cr = (ce / cs - 1) * 100 if (cs and ce) else None
                mce, per_c, pbr_c = at(mc_p, c), at(per_p, c), at(pbr_p, c)
                sr = {"섹터": sec, "코드": c, "종목": name_map.get(c, c),
                      f"{args.months}M수익률%": round(cr, 1) if cr is not None else None,
                      "시총_조": round(mce / JO, 2) if mce else None,
                      "PER": round(per_c, 1) if per_c else None,
                      "PBR": round(pbr_c, 2) if pbr_c else None}
                if flows is not None:
                    fk = sum(float(flows[i].get(c, 0)) for i in ("외국인", "기관합계")) / JO
                    sr["외국인기관순매수_조"] = round(fk, 2)
                stock_rows.append(sr)

    summary = pd.DataFrame(rows).sort_values("섹터시총_조", ascending=False).reset_index(drop=True)
    order = summary["섹터"].tolist()
    mc_trend = pd.DataFrame(monthly_mc).T.reindex(order)
    amt_trend = pd.DataFrame(monthly_amt).T.reindex(order)
    stocks = pd.DataFrame(stock_rows).sort_values(["섹터", "시총_조"], ascending=[True, False])

    out = args.out or f"/data/frame/sector_dashboard_{date.today():%Y%m%d}.xlsx"
    inv_cols = [c for c in ("외국인순매수_조", "기관순매수_조", "개인순매수_조", "연기금순매수_조", "금융투자순매수_조")
                if c in summary.columns]
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="섹터현황", index=False)
        mc_trend.to_excel(xw, sheet_name="월별_시총추이")
        amt_trend.to_excel(xw, sheet_name="월별_거래대금추이")
        if flows is not None and inv_cols:
            fl = summary[["섹터"] + inv_cols].copy()
            if "외국인순매수_조" in inv_cols and "기관순매수_조" in inv_cols:
                fl["외국인+기관_조"] = (fl["외국인순매수_조"] + fl["기관순매수_조"]).round(2)
            fl.to_excel(xw, sheet_name="투자자순매수", index=False)
        stocks.to_excel(xw, sheet_name="종목상세", index=False)

        for ws in xw.book.worksheets:
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 40)
            ws.freeze_panes = "A2"

        # ── 차트 시트 ──
        try:
            wb = xw.book
            n = len(summary)
            ws_s = wb["섹터현황"]
            ci = {c: summary.columns.get_loc(c) + 1 for c in summary.columns}
            cats = Reference(ws_s, min_col=1, min_row=2, max_row=1 + n)
            cw = wb.create_sheet("차트")
            b1 = BarChart(); b1.type = "col"; b1.title = "섹터별 시총가중 1년 수익률(%)"; b1.height = 9; b1.width = 26; b1.legend = None
            b1.add_data(Reference(ws_s, min_col=ci["시총가중수익률%"], min_row=1, max_row=1 + n), titles_from_data=True)
            b1.set_categories(cats); cw.add_chart(b1, "A1")
            b2 = BarChart(); b2.type = "col"; b2.title = "섹터별 시총 비중(%)"; b2.height = 9; b2.width = 26; b2.legend = None
            b2.add_data(Reference(ws_s, min_col=ci["시총비중%"], min_row=1, max_row=1 + n), titles_from_data=True)
            b2.set_categories(cats); cw.add_chart(b2, "A20")
            ws_m = wb["월별_시총추이"]; nm = mc_trend.shape[1]; ntop = min(6, n)
            lc = LineChart(); lc.title = "월별 섹터 시총 추이(조원, 시총 상위 6)"; lc.height = 10; lc.width = 26
            lc.add_data(Reference(ws_m, min_col=1, min_row=2, max_row=1 + ntop, max_col=1 + nm),
                        titles_from_data=True, from_rows=True)
            lc.set_categories(Reference(ws_m, min_col=2, min_row=1, max_col=1 + nm))
            cw.add_chart(lc, "A39")
        except Exception as e:
            print(f"  [경고] 차트 생성 생략: {e}")

    print(f"\n기간: {start_d} ~ {end_d}  /  섹터 {len(summary)}개  /  종목 {len(stocks)}개(정의 섹터)")
    cols_show = ["섹터", "구성종목수", "섹터시총_조", "시총비중%", "시총가중수익률%", "변동성%", "거래대금비중%", "PER중앙값", "PBR중앙값"]
    print(summary[[c for c in cols_show if c in summary.columns]].to_string(index=False))
    print(f"\nExcel 저장: {out}")
    if args.out is None:
        import shutil
        latest = "/data/frame/sector_dashboard_latest.xlsx"
        shutil.copy(out, latest)
        print(f"최신본 복사: {latest}")


if __name__ == "__main__":
    main()
