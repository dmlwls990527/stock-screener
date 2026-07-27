#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""leader_screener.py — 미래 주도주 후보 워치리스트 (2-Track)

Track A (펀더멘털 가속): 매출 YoY + 가속 + TTM성장 z합. TSLA-2012형(성장이 일찍 보임).
Track B (초기 순위상승): 1년 시총순위 상승 z + 펀더멘털 확인 AND게이트. NVDA-2016형(변곡 돌파).
  - 게이트: rank_up > 0 AND fund_z > 0  (펀더 없는 순위급등 = 휘프소/꼭지 제외)
  - 대형 틸트: 당시 시총 Top20은 Track B 제외 (이미 거인)

실행:
  leader_screener.py             # 오늘 기준 워치리스트 → leader_watchlist_latest.xlsx
  leader_screener.py --backtest  # 2012~2020 되감기 검증 (NVDA/TSLA/AMD 잡히나)
한계(정직): 유니버스 = 현존 대형주 ∩ 재무보유(~100종목) → 생존자편향, 진짜 오탐률 측정불가.
"""
import sys, argparse
sys.path.insert(0,"/data/frame")
import pandas as pd, numpy as np
import factor_analysis as fa

CAP=300.0
conn=fa.get_conn(); cur=conn.cursor()

def dq(sql):
    cur.execute(sql); cols=[d[0] for d in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)

rev_all=dq("SELECT CODE, END_DATE, REVENUE, OP_INCOME FROM quarterly_financials_us")
rev_all["END_DATE"]=pd.to_datetime(rev_all["END_DATE"]); rev_all=rev_all.sort_values(["CODE","END_DATE"])

def clip(x): return max(-CAP,min(CAP,x)) if x==x else x

def rev_factors(asof):
    cut=pd.Timestamp(asof); d=rev_all[rev_all["END_DATE"]<=cut]; out=[]
    for code,g in d.groupby("CODE"):
        g=g.drop_duplicates("END_DATE").sort_values("END_DATE"); rev=g["REVENUE"].astype(float).values; op=g["OP_INCOME"].astype(float).values
        if len(rev)<8: continue
        yoy=np.array([(rev[i]-rev[i-4])/rev[i-4]*100 if rev[i-4]>0 else np.nan for i in range(4,len(rev))],dtype=float)
        yv=yoy[~np.isnan(yoy)]
        if len(yv)<4: continue
        ttm=rev[-4:].sum(); ttmp=rev[-8:-4].sum()
        rw=min(4,len(yoy)//2)               # 데이터 짧으면 창 축소
        recent=yoy[-rw:]; prior=yoy[-(rw+8):-rw]
        recent=recent[~np.isnan(recent)]; prior=prior[~np.isnan(prior)]
        accel=(np.mean(recent)-np.mean(prior)) if (len(recent)>=1 and len(prior)>=1) else np.nan
        om_now=op[-4:].sum()/ttm*100 if ttm>0 else np.nan
        om_prev=op[-8:-4].sum()/ttmp*100 if ttmp>0 else np.nan
        mtrend=(om_now-om_prev) if (om_now==om_now and om_prev==om_prev) else np.nan
        mser=np.array([op[i-3:i+1].sum()/rev[i-3:i+1].sum()*100 for i in range(3,len(rev)) if rev[i-3:i+1].sum()>0])
        mcyc=(mser.max()/np.median(mser)) if (len(mser)>0 and np.median(mser)>0) else np.nan
        out.append(dict(CODE=code, rev_yoy=clip(round(yv[-1],1)),
            rev_accel=clip(round(accel,1)),
            ttm_g=clip(round((ttm-ttmp)/ttmp*100,1)) if ttmp>0 else np.nan,
            margin_trend=round(mtrend,1) if mtrend==mtrend else np.nan,
            pos_ratio=round((yv>0).mean()*100), mcyc=round(mcyc,1) if mcyc==mcyc else np.nan))
    return pd.DataFrame(out)

def mcap(date):
    return dq(f"SELECT CODE, CLOSE, MARCAP, \"RANK\" FROM daily_marcap_us WHERE date_=(SELECT MAX(date_) FROM daily_marcap_us WHERE date_<=TIMESTAMP '{date} 00:00:00')")

def z(s):
    s=s.astype(float); sd=s.std(ddof=0); return (s-s.mean())/sd if sd and sd>0 else s*0

def build(asof):
    y1=(pd.Timestamp(asof)-pd.DateOffset(years=1)).strftime("%Y-%m-%d")
    mc=mcap(asof).rename(columns={"MARCAP":"MC0","RANK":"RANK0","CLOSE":"CL0"})
    mc1=mcap(y1)[["CODE","RANK"]].rename(columns={"RANK":"RANK_1y"})
    df=mc.merge(mc1,on="CODE",how="left").merge(rev_factors(asof),on="CODE",how="inner")
    if df.empty: return df
    df["rank_up"]=df["RANK_1y"]-df["RANK0"]
    df["fund_z"]=pd.concat([z(df["rev_yoy"]),z(df["rev_accel"]),z(df["ttm_g"]),z(df["margin_trend"])],axis=1).mean(axis=1).round(2)  # 매출YoY+가속+TTM성장+마진추세, NaN안전
    df["emrg_z"]=z(df["rank_up"]).round(2)
    df["MC0_B"]=(df["MC0"]/1e9).round(1)
    # Track A: 펀더멘털 가속 순위
    df["trackA_rank"]=df["fund_z"].rank(ascending=False,method="min").astype(int)
    # Track B: AND게이트(순위상승>0 & 펀더+) + 대형 제외 후 하이브리드
    gate=(df["rank_up"]>0)&(df["fund_z"]>0)&(df["RANK0"]>20)
    df["hybrid"]=((df["fund_z"]+df["emrg_z"])/2).round(2)
    df["trackB_pass"]=gate
    tb=df[gate].sort_values("hybrid",ascending=False).reset_index(drop=True)
    tb["trackB_rank"]=tb.index+1
    df=df.merge(tb[["CODE","trackB_rank"]],on="CODE",how="left")
    return df

def backtest():
    latest=mcap("2026-07-23").rename(columns={"MARCAP":"MCn","RANK":"RANK_now","CLOSE":"CLn"})
    WIN=["NVDA","TSLA","AMD","AAPL"]
    for asof in ["2012-12-31","2014-12-31","2016-12-30","2018-12-31","2020-12-31"]:
        df=build(asof)
        if df.empty: continue
        df=df.merge(latest[["CODE","RANK_now","CLn"]],on="CODE",how="left")
        df["retX"]=(df["CLn"]/df["CL0"]).round(1)
        show=["CODE","RANK0","MC0_B","fund_z","rank_up","trackA_rank","trackB_rank","retX"]
        print("="*70); print("AS-OF",asof,f"(universe={len(df)})")
        tb=df[df["trackB_pass"]].sort_values("trackB_rank")
        print(f"[Track B 워치리스트] {len(tb)}종 (게이트 통과):")
        print(tb[show].head(8).to_string(index=False))
        print("[WINNERS]")
        print(df[df["CODE"].isin(WIN)][show].to_string(index=False))

def add_vol(df):
    codes="','".join(df["CODE"].tolist())
    d=dq(f"SELECT CODE, date_ AS D, CLOSE AS C FROM daily_marcap_us WHERE CODE IN ('{codes}') AND date_>=(SELECT MAX(date_)-400 FROM daily_marcap_us) ORDER BY CODE, date_")
    d["D"]=pd.to_datetime(d["D"]); rows=[]
    for code,g in d.groupby("CODE"):
        pr=g.sort_values("D")["C"].astype(float).values[-252:]
        if len(pr)<60: rows.append(dict(CODE=code,vol_ann=np.nan,mdd_1y=np.nan)); continue
        ret=np.diff(np.log(pr))
        ret=ret[np.abs(ret)<np.log(1.5)]        # 하루 |±50%|↑ 제외 = 주식분할 아티팩트 컷(KLAC 10:1 등)
        peak=np.maximum.accumulate(pr); dd=(pr-peak)/peak
        dd=dd[dd>-0.6]                            # -60%↓ 단일점(분할)도 MDD에서 제외
        rows.append(dict(CODE=code,vol_ann=round(np.std(ret)*np.sqrt(252)*100) if len(ret)>10 else np.nan,mdd_1y=round(dd.min()*100) if len(dd) else np.nan))
    return df.merge(pd.DataFrame(rows),on="CODE",how="left")

def entry_tier(r0,r1):
    if not (r0==r0 and r1==r1): return np.nan
    for T in [10,20,30,50,75,100,150,200]:
        if r0<=T and r1>T: return T
    return np.nan

def screen_now():
    today=dq("SELECT TO_CHAR(MAX(date_),'YYYY-MM-DD') D FROM daily_marcap_us")["D"].iloc[0]
    df=build(today); df=add_vol(df)
    nm=fa.get_name_map(conn,"ticker_master_us") if hasattr(fa,"get_name_map") else {}
    df["NAME"]=df["CODE"].map(nm) if nm else ""
    sec=fa.get_sector_map(conn,"ticker_master_us") if hasattr(fa,"get_sector_map") else {}
    SKO={"Information Technology":"IT","Health Care":"헬스케어","Industrials":"산업재","Consumer Discretionary":"임의소비","Consumer Staples":"필수소비","Financials":"금융","Communication Services":"커뮤니","Energy":"에너지","Materials":"소재","Real Estate":"부동산","Utilities":"유틸"}
    df["섹터"]=df["CODE"].map(lambda c:SKO.get(sec.get(c,""),(sec.get(c,"") or "?")))
    def _typ(x):
        if x.get("mcyc")==x.get("mcyc") and x.get("mcyc",0)>=2.5: return "시클리컬"
        if x.get("pos_ratio")==x.get("pos_ratio") and x.get("pos_ratio",0)>=85: return "꾸준복리"
        return ""
    df["유형"]=df.apply(lambda r:_typ({"mcyc":r.get("mcyc"),"pos_ratio":r.get("pos_ratio")}),axis=1)
    df["netier"]=[entry_tier(r0,r1) for r0,r1 in zip(df["RANK0"],df["RANK_1y"])]
    acol=["trackA_rank","CODE","NAME","섹터","유형","MC0_B","RANK0","rev_yoy","margin_trend","mcyc","pos_ratio","fund_z","vol_ann"]
    bcol=["trackB_rank","CODE","NAME","MC0_B","RANK0","rank_up","fund_z","hybrid","vol_ann","mdd_1y"]
    a=df.sort_values("trackA_rank")[acol].head(20)
    b=df[df["trackB_pass"]].sort_values("trackB_rank")[bcol]
    ne=df[df["netier"].notna()].sort_values(["netier","fund_z"],ascending=[True,False]).copy()
    ne["신규진입"]=ne["netier"].astype(int).map(lambda t:str(t)+"위내진입")
    ne=ne[["신규진입","CODE","NAME","섹터","유형","MC0_B","RANK0","rank_up","fund_z","mcyc","vol_ann"]]
    KOR={"trackA_rank":"순위","trackB_rank":"순위","CODE":"티커","NAME":"종목명","MC0_B":"시총(십억$)","RANK0":"시총순위","RANK_1y":"1년전순위","rev_yoy":"매출증가율%","rev_accel":"매출가속도","ttm_g":"연간매출성장%","margin_trend":"영업마진추세%p","mcyc":"마진변동성(배)","pos_ratio":"성장지속%","fund_z":"펀더멘털점수","vol_ann":"주가변동성%","mdd_1y":"최대낙폭%","rank_up":"순위상승폭","hybrid":"종합점수"}
    a=a.rename(columns=KOR); b=b.rename(columns=KOR); ne=ne.rename(columns=KOR)
    out="/data/frame/leader_watchlist_latest.xlsx"
    with pd.ExcelWriter(out,engine="openpyxl") as w:
        ne.to_excel(w,sheet_name="신규진입",index=False)
        a.to_excel(w,sheet_name="펀더멘털가속",index=False)
        b.to_excel(w,sheet_name="순위상승",index=False)
        pd.DataFrame({"항목":["기준일","유니버스","변동성/MDD","한계"],"값":[today,f"{len(df)}종(생존자편향)","연율변동성%·최대낙폭%=참고용(필터X), 비중조절용","워치리스트=후보, 매수신호 아님. 분산·소액 전제"]}).to_excel(w,sheet_name="설명",index=False)
    print(f"기준일 {today} | universe {len(df)}")
    print(f"\n[신규 Top진입 ({len(ne)}종) — 최근1년 상위권 신규진입]"); print(ne.head(12).to_string(index=False))
    print("\n[Track A Top10]"); print(a.head(10).to_string(index=False))
    print("\n저장:",out)

if __name__=="__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--backtest",action="store_true")
    args=ap.parse_args()
    backtest() if args.backtest else screen_now()
