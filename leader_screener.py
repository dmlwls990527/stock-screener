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
# 데이터가 짧거나 깨져도 시클리컬로 확실히 태그할 알려진 경기민감 종목(메모리·소재·에너지·자동차 등).
# std/추세 지표가 못 잡을 때의 백업. 유니버스에 없는 티커는 무해.
CYC_HARD={"MU","WDC","STX","SNDK","MRVL",            # 메모리 반도체
          "X","NUE","CLF","STLD",                     # 철강
          "FCX","AA","NEM",                            # 비철금속/광산
          "XOM","CVX","COP","OXY","DVN","EOG","SLB","HAL",  # 에너지(석유·가스)
          "EXE","FANG","CTRA","EQT","AR","APA","MRO","HES", # 에너지 추가(셰일·천연가스 E&P)
          "DOW","LYB","CE",                            # 화학
          "F","GM","PCAR",                             # 자동차/트럭
          "CAT","DE"}                                  # 건설·농기계(매출/물량이 경기 타는 타입 = 마진std 사각지대)
# 시클리컬 판정 임계값(데이터 보정: 최근20분기 마진 std≥6%p AND 추세상관<0.6 = 경기순환)
CYC_STD_T, CYC_TREND_T = 6.0, 0.6
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
        # TTM 영업마진 시계열 — NULL 재무 분기는 건너뜀(=nan 오염 방지). 구 mcyc(=max/median)는
        # median<=0(적자 가는 시클리컬)에서 통째로 NaN이 나 메모리를 못 걸렀음 → std 기반으로 교체.
        mser=[]
        for i in range(3,len(rev)):
            rvw=rev[i-3:i+1]; opw=op[i-3:i+1]
            if np.isfinite(rvw).all() and np.isfinite(opw).all() and rvw.sum()>0:
                mser.append(opw.sum()/rvw.sum()*100)
        mser=np.array(mser)
        if len(mser)>=4:
            r=mser[-20:]                                   # 최근 20분기(≈5년) = 지금 사이클을 타는지(일회성 재평가 배제)
            mstd=float(np.std(r))                          # 마진 변동성(%p): 적자에도 안 깨짐
            mtcorr=float(np.corrcoef(np.arange(len(r)),r)[0,1]) if np.std(r)>0 else 0.0  # 우상향(턴어라운드) vs 오르내림(경기순환)
            mpos=(r[-1]-r.min())/(r.max()-r.min())*100 if r.max()>r.min() else np.nan     # 현재 마진의 최근 레인지 내 위치(%): 100=고점, 0=바닥
            # 되돌림 '기저확률'(예측 아님): 과거에 마진이 얼마나 크게 꺾였나 + 적자 전력. 전체 히스토리 기준.
            peak=np.maximum.accumulate(mser)
            mdd=float((mser-peak).min())                   # 최대 마진 낙폭(pp, 음수). 크게 음수 = 크게 되돌린 전력
            hadloss=int(bool((mser<0).any()))              # 적자 마진 전력(1/0)
            mmed=float(np.median(mser))                    # 전체기간 마진 중앙값: >0=평소엔 버는 회사(진짜 경기민감) / <0=원래 적자(초기·바이오텍)
        else:
            mstd=mtcorr=mpos=mdd=mmed=np.nan; hadloss=np.nan
        # 한 분기에 매출이 확 뛰면 회사를 합친 것(인수합병)일 수 있음 → 진짜 성장 아닌 착시 주의
        rj=[(rev[i]-rev[i-1])/rev[i-1]*100 for i in range(max(1,len(rev)-8),len(rev)) if rev[i-1]>0]
        rev_jump=round(max(rj)) if rj else np.nan
        # 최근 연속으로 매출이 QoQ 증가한 분기 수 = 램프(지속성장) vs 스파이크(단발) 구분.
        # LQDA 3→9→54→133 = streak 3(진짜 램프). CYTK …67→2→19 = streak 1(반짝). ARWR …264→74 = streak 0.
        rev_streak=0
        for i in range(len(rev)-1,0,-1):
            if rev[i]>rev[i-1]: rev_streak+=1
            else: break
        # 최근 꾸준함(%): 최근 6개 QoQ 변화 중 '크게 안 꺾인(≥-5%)' 비율. 램프=높음, 반전 잦으면=낮음.
        # 신흥주(이제 막 변곡)도 최근만 보므로 안 죽고, 대신 최근 30%+ 급락 있으면 감점.
        qoq=[(rev[i]-rev[i-1])/abs(rev[i-1]) for i in range(max(1,len(rev)-6),len(rev)) if rev[i-1]!=0]
        if qoq:
            consist=100.0*sum(1 for q in qoq if q>=-0.05)/len(qoq)
            if min(qoq)<-0.30: consist=max(0.0,consist-25)
            recent_consist=round(consist)
        else:
            recent_consist=np.nan
        ttm_op=float(op[-4:].sum()) if len(op)>=4 and np.isfinite(op[-4:]).all() else np.nan
        out.append(dict(CODE=code, rev_yoy=clip(round(yv[-1],1)), rev_streak=rev_streak, recent_consist=recent_consist,
            rev_accel=clip(round(accel,1)),
            ttm_g=clip(round((ttm-ttmp)/ttmp*100,1)) if ttmp>0 else np.nan,
            margin_trend=round(mtrend,1) if mtrend==mtrend else np.nan,
            pos_ratio=round((yv>0).mean()*100),
            margin_std=round(mstd,1) if mstd==mstd else np.nan,
            margin_tcorr=round(mtcorr,2) if mtcorr==mtcorr else np.nan,
            margin_pos=round(mpos) if mpos==mpos else np.nan,
            margin_dd=round(mdd) if mdd==mdd else np.nan,
            margin_med=round(mmed,1) if mmed==mmed else np.nan,
            rev_jump=rev_jump, had_loss=hadloss, ttm_op=ttm_op))
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
    # 신규진입 판정용 '롤링 최저(=최고)순위': 최근 1년 중 최근 2개월을 뺀 구간에서 도달했던 제일 좋은 순위.
    # 단일 시점(1년 전 하루) 대신 구간 최저를 봐서, 중간에 이미 그 구간에 있었으면 '새 진입 아님'으로 판정.
    lo=(pd.Timestamp(asof)-pd.Timedelta(days=365)).strftime("%Y-%m-%d")
    hi=(pd.Timestamp(asof)-pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    pb=dq(f"SELECT CODE, MIN(\"RANK\") AS RANK_PB FROM daily_marcap_us WHERE date_ BETWEEN TIMESTAMP '{lo} 00:00:00' AND TIMESTAMP '{hi} 00:00:00' GROUP BY CODE")
    df=df.merge(pb,on="CODE",how="left")
    df["rank_up"]=df["RANK_1y"]-df["RANK0"]
    _zs=[z(df["rev_yoy"]),z(df["rev_accel"]),z(df["ttm_g"]),z(df["margin_trend"])]
    df["fund_gate_z"]=pd.concat(_zs,axis=1).mean(axis=1).round(2)                       # 성장 4팩터 = Track B 게이트용(신흥 턴어라운드 안 죽임)
    df["fund_z"]=pd.concat(_zs+[z(df["recent_consist"])],axis=1).mean(axis=1).round(2)  # +최근꾸준 = 순위/표시용(꾸준한 놈 가점)
    # 단발 스파이크: 큰 QoQ 점프는 있었지만 2분기 연속 성장이 아닌 것 = 반짝(바이오 마일스톤·M&A착시). 표시용 fund_z는 두고 순위만 강등.
    df["is_spike"]=((df["rev_jump"]>=80)&(df["rev_streak"].fillna(0)<2)).astype(int)
    df["fund_rank_key"]=df["fund_z"]-df["is_spike"]*1000
    df["emrg_z"]=z(df["rank_up"]).round(2)
    df["MC0_B"]=(df["MC0"]/1e9).round(1)
    # 밸류에이션 프록시: 시총 ÷ TTM영업이익(배). 시클리컬은 '피크 이익' 위라 이게 낮아 보이는 게 함정(피터 린치).
    df["p_op"]=np.where((df["ttm_op"].astype(float)>0), (df["MC0"].astype(float)/df["ttm_op"].astype(float)).round(1), np.nan)
    # Track A: 펀더멘털 가속 순위 (단발 스파이크는 fund_rank_key로 뒤로 밀림)
    df["trackA_rank"]=df["fund_rank_key"].rank(ascending=False,method="min").astype(int)
    # Track B: AND게이트(순위상승>0 & 펀더+) + 대형 제외 후 하이브리드
    gate=(df["rank_up"]>0)&(df["fund_gate_z"]>0)&(df["RANK0"]>20)
    df["hybrid"]=((df["fund_z"]+df["emrg_z"])/2).round(2)
    df["trackB_pass"]=gate
    tb=df[gate].sort_values(["is_spike","hybrid"],ascending=[True,False]).reset_index(drop=True)
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

# ── Track C: 주도주(시장을 끌고 가는 주식) ────────────────────────────────
# Track A/B는 "매출이 빠르게 크는 종목"만 봐서, 기관이 담을 수도 없는 소형주가
# 상위에 올랐다(예: 일거래대금 43M$인 종목이 2위). 주도주는 정의상
#   ① 시장보다 세게 오르고(상대강도) ② 기관이 들어올 수 있는 규모·유동성이 있고
#   ③ 신고가 근처에서 움직이며 ④ 섹터 안에서 앞선다.
# 그래서 가격·유동성 게이트를 따로 세운 트랙을 만든다. A/B와 병행(대체 아님).
LEAD_MC_MIN   = 10e9    # 최소 시총 $10B — 이보다 작으면 시장을 주도할 체급이 안 됨
LEAD_AMT_MIN  = 300e6   # 최소 일평균 거래대금 $300M — 기관 진입 가능성
LEAD_RS_MIN   = 80      # 상대강도 상위 20%
LEAD_HIGH_MIN = 85      # 52주 고점의 85% 이상 (고점 근처에서 버티는 중)

def price_metrics(asof):
    """유니버스 전체의 가격·유동성 지표. 상대강도(RS) 백분위 계산에 전 종목이 필요하다."""
    d=dq(f"""SELECT CODE, TO_CHAR(date_,'YYYY-MM-DD') AS D, CLOSE AS C, HIGH AS H, AMOUNT AS A
             FROM daily_price_us
             WHERE date_ <= TIMESTAMP '{asof} 00:00:00'
               AND date_ >  (SELECT MAX(date_)-420 FROM daily_price_us WHERE date_ <= TIMESTAMP '{asof} 00:00:00')""")
    rows=[]
    for code,g in d.groupby("CODE"):
        g=g.sort_values("D")
        cl=pd.to_numeric(g["C"],errors="coerce").values
        hi=pd.to_numeric(g["H"],errors="coerce").values
        am=pd.to_numeric(g["A"],errors="coerce").values
        cl=cl[np.isfinite(cl)&(cl>0)]
        if len(cl)<120: continue
        # 분할 아티팩트(하루 ±50% 초과) 제거 후 누적수익률 — add_vol과 동일한 가드
        r=np.diff(np.log(cl)); r=r[np.abs(r)<np.log(1.5)]
        def cum(n):
            if len(r)<n*0.6: return np.nan
            return (np.exp(r[-n:].sum())-1)*100
        hh=np.nanmax(hi[-252:]) if np.isfinite(hi[-252:]).any() else np.nanmax(cl[-252:])
        rows.append(dict(CODE=code,
            ret_12m=cum(252), ret_6m=cum(126), ret_3m=cum(63),
            near_high=round(cl[-1]/hh*100,1) if hh and hh>0 else np.nan,
            amt20=float(np.nanmean(am[-20:])) if len(am)>=20 else np.nan,
            amt_grow=round((np.nanmean(am[-20:])/np.nanmean(am[-80:-20])-1)*100) \
                     if (len(am)>=80 and np.nanmean(am[-80:-20])>0) else np.nan))
    p=pd.DataFrame(rows)
    if p.empty: return p
    # RS = 기간별 수익률의 '유니버스 내 백분위'를 가중 평균 (오닐식 12/6/3개월 배합).
    # 절대 수익률을 그대로 합치면 한 기간의 이상치가 전부를 좌우하므로 백분위로 바꿔서 섞는다.
    def pct(s): return s.rank(pct=True)*100
    w={"ret_12m":0.4,"ret_6m":0.3,"ret_3m":0.3}
    acc=None; wsum=None
    for c,wt in w.items():
        pc=pct(p[c]); m=pc.notna()
        acc=(pc.fillna(0)*wt) if acc is None else acc+(pc.fillna(0)*wt)
        wsum=(m*wt) if wsum is None else wsum+(m*wt)
    p["rs_pct"]=(acc/wsum.replace(0,np.nan)).round(1)
    p["amt20_m"]=(p["amt20"]/1e6).round(0)
    return p

def build_leaders(df):
    """Track C 판정. df는 screen_now에서 만든 (재무+시총+섹터) 테이블."""
    if "rs_pct" not in df.columns: return df, pd.DataFrame()
    mc=df["MC0"].astype(float)
    # 단발 스파이크(한 분기만 매출이 튄 종목)는 주도주가 아니다 -> 게이트에서 제외.
    # Track A/B에는 이 강등이 있었는데 Track C에만 빠져 MRNA가 1위로 올라왔었다.
    spike=df["is_spike"].fillna(0).astype(int)==1 if "is_spike" in df.columns else pd.Series(False,index=df.index)
    gate=(mc>=LEAD_MC_MIN)&(df["amt20"].astype(float)>=LEAD_AMT_MIN) \
         &(df["rs_pct"]>=LEAD_RS_MIN)&(df["near_high"]>=LEAD_HIGH_MIN)&(~spike)
    df["leader_pass"]=gate.fillna(False)
    # 섹터 리더십: 체급($10B+) 안에서 같은 섹터끼리 상대강도 순위
    big=df[mc>=LEAD_MC_MIN].copy()
    big["sec_rank"]=big.groupby("섹터")["rs_pct"].rank(ascending=False,method="min")
    df=df.merge(big[["CODE","sec_rank"]],on="CODE",how="left")
    # 점수: 상대강도 + 실적가속 + 거래대금 증가 (+ 섹터 1·2위 가점)
    # 점수는 z가 아니라 '유니버스 내 백분위' 배합. z는 극단값 한 종목이 점수를 지배해
    # (MRNA 7.21 vs 2위 1.40) 순위가 왜곡됐다. RS 계산과 같은 방식으로 통일.
    def _blend(pairs):
        acc=wsum=None
        for cname,wt in pairs:
            pc=df[cname].rank(pct=True)*100; m=pc.notna()
            acc=pc.fillna(0)*wt if acc is None else acc+pc.fillna(0)*wt
            wsum=m*wt if wsum is None else wsum+m*wt
        return acc/wsum.replace(0,np.nan)
    sc=_blend([("rs_pct",0.4),("fund_z",0.3),("amt_grow",0.3)])/100
    df["lead_score"]=(sc+np.where(df["sec_rank"].fillna(99)<=2,0.10,0)).round(3)
    lead=df[df["leader_pass"]].sort_values("lead_score",ascending=False).reset_index(drop=True)
    lead["leader_rank"]=lead.index+1
    df=df.merge(lead[["CODE","leader_rank"]],on="CODE",how="left")
    return df, lead

def screen_now():
    today=dq("SELECT TO_CHAR(MAX(date_),'YYYY-MM-DD') D FROM daily_marcap_us")["D"].iloc[0]
    df=build(today); df=add_vol(df)
    nm=fa.get_name_map(conn,"ticker_master_us") if hasattr(fa,"get_name_map") else {}
    df["NAME"]=df["CODE"].map(nm) if nm else ""
    sec=fa.get_sector_map(conn,"ticker_master_us") if hasattr(fa,"get_sector_map") else {}
    SKO={"Information Technology":"IT","Health Care":"헬스케어","Industrials":"산업재","Consumer Discretionary":"임의소비","Consumer Staples":"필수소비","Financials":"금융","Communication Services":"커뮤니","Energy":"에너지","Materials":"소재","Real Estate":"부동산","Utilities":"유틸"}
    df["섹터"]=df["CODE"].map(lambda c:SKO.get(sec.get(c,""),(sec.get(c,"") or "?")))
    # A) 매출·마진 팩터가 안 맞는 섹터 제외: 부동산 리츠(임대수익 구조라 매출/영업이익 개념이 다름)
    df=df[df["섹터"]!="부동산"].copy()
    # Track C용 가격·유동성 지표 (유니버스 전체 기준으로 RS 백분위 계산 후 병합)
    df=df.merge(price_metrics(today),on="CODE",how="left")
    # 제외 후 순위 다시 매김(Track A = 펀더점수, Track B = 종합점수)
    df["trackA_rank"]=df["fund_rank_key"].rank(ascending=False,method="min").astype(int)
    df=df.drop(columns=["trackB_rank"])
    _tb=df[df["trackB_pass"]].sort_values(["is_spike","hybrid"],ascending=[True,False]).reset_index(drop=True)
    _tb["trackB_rank"]=_tb.index+1
    df=df.merge(_tb[["CODE","trackB_rank"]],on="CODE",how="left")
    def _typ(r):
        code=r.get("CODE"); ms=r.get("margin_std"); tc=r.get("margin_tcorr"); pr=r.get("pos_ratio"); mmed=r.get("margin_med")
        volatile=(ms==ms and ms>=CYC_STD_T)          # 마진 변동성 큼
        uptrend =(tc==tc and tc>=CYC_TREND_T)        # 마진이 추세적 우상향 = 턴어라운드/구조확장(경기순환 아님)
        profitable=(mmed==mmed and mmed>0)           # 평소엔 흑자 = 진짜 경기민감주. 원래 적자(바이오텍·초기 회사)면 경기민감 아님
        if (code in CYC_HARD) or (volatile and not uptrend and profitable): return "시클리컬"
        if volatile and uptrend: return "마진확장"    # 변동성 크지만 우상향(APP·PLTR·NVDA형)
        if pr==pr and pr>=85: return "꾸준복리"
        return ""
    df["유형"]=df.apply(_typ,axis=1)
    def _warn(r):   # 확정 아님, '이럴 수도 있다'는 표시.
        notes=[]; typ=r.get("유형"); mp=r.get("margin_pos"); dd=r.get("margin_dd"); hl=r.get("had_loss"); rj=r.get("rev_jump")
        if typ=="시클리컬" and mp==mp:
            if mp>=80 and ((dd==dd and dd<=-25) or hl==1): notes.append("지금 이익 최고 → 떨어질 수 있음")
            elif mp<=30: notes.append("지금 이익 바닥 → 반등할지 확인")
        st=r.get("rev_streak")
        # 매출 급증이 '여러 분기 지속(램프)'이면 진짜 성장 → 경고 안 붙임. '한 분기 반짝'만 경고.
        if rj==rj and rj>=80 and not (st==st and st>=2):
            notes.append("매출이 한 분기만 반짝 → 단발 이벤트·합병·상장초기인지 확인(지속 성장 아닐 수 있음)")
        return " / ".join(notes)
    df["주의"]=df.apply(_warn,axis=1)
    df["netier"]=[entry_tier(r0,rpb) for r0,rpb in zip(df["RANK0"],df["RANK_PB"])]
    df,lead=build_leaders(df)   # Track C
    ccol=["leader_rank","CODE","NAME","섹터","유형","주의","MC0_B","RANK0","rs_pct","near_high","amt20_m","amt_grow","sec_rank","rev_streak","recent_consist","fund_z","lead_score","vol_ann","mdd_1y"]
    acol=["trackA_rank","CODE","NAME","섹터","유형","주의","MC0_B","RANK0","rev_yoy","rev_streak","recent_consist","margin_trend","margin_std","margin_pos","margin_dd","p_op","pos_ratio","fund_z","vol_ann"]
    bcol=["trackB_rank","CODE","NAME","MC0_B","RANK0","rank_up","rev_streak","recent_consist","fund_z","hybrid","vol_ann","mdd_1y"]
    a=df.sort_values("trackA_rank")[acol].head(20)
    b=df[df["trackB_pass"]].sort_values("trackB_rank")[bcol]
    ne=df[df["netier"].notna()].sort_values(["netier","fund_rank_key"],ascending=[True,False]).copy()
    ne["신규진입"]=ne["netier"].astype(int).map(lambda t:str(t)+"위내진입")
    ne=ne[["신규진입","CODE","NAME","섹터","유형","주의","MC0_B","RANK0","rank_up","rev_streak","fund_z","margin_std","margin_pos","margin_dd","vol_ann"]]
    KOR={"trackA_rank":"순위","trackB_rank":"순위","leader_rank":"순위","CODE":"티커","NAME":"종목명","MC0_B":"시총(십억$)","RANK0":"시총순위","RANK_1y":"1년전순위","rev_yoy":"매출증가율%","rev_streak":"매출연속성장(분기)","recent_consist":"최근꾸준%","rev_accel":"매출가속도","ttm_g":"연간매출성장%","margin_trend":"영업마진추세%p","margin_std":"마진변동성%p","margin_pos":"마진위치%","margin_dd":"예전 이익하락폭%p","p_op":"시총/영업이익(배)","margin_tcorr":"마진추세상관","pos_ratio":"성장지속%","fund_z":"펀더멘털점수","vol_ann":"주가변동성%","mdd_1y":"최대낙폭%","rank_up":"순위상승폭","hybrid":"종합점수","rs_pct":"상대강도(0~100)","near_high":"52주고점대비%","amt20_m":"일거래대금(백만$)","amt_grow":"거래대금증가%","sec_rank":"섹터내순위","lead_score":"주도주점수"}
    cc=lead[ccol].rename(columns=KOR) if len(lead) else pd.DataFrame(columns=[KOR.get(c,c) for c in ccol])
    a=a.rename(columns=KOR); b=b.rename(columns=KOR); ne=ne.rename(columns=KOR)
    out="/data/frame/leader_watchlist_latest.xlsx"
    with pd.ExcelWriter(out,engine="openpyxl") as w:
        cc.to_excel(w,sheet_name="주도주",index=False)
        ne.to_excel(w,sheet_name="신규진입",index=False)
        a.to_excel(w,sheet_name="펀더멘털가속",index=False)
        b.to_excel(w,sheet_name="순위상승",index=False)
        pd.DataFrame({"항목":["기준일","유니버스","시트 3개(주도주/펀더멘털가속/순위상승)","주도주 시트 기준","상대강도(0~100)","52주고점대비%","일거래대금","섹터내순위","마진위치%","주의","예전 이익하락폭%p","시총/영업이익","변동성/MDD","한계"],
            "값":[today,f"{len(df)}종(지금 살아있는 종목만)",
                 "주도주=지금 시장을 끌고 가는 큰 종목 / 펀더멘털가속=실적이 빨리 크는 종목(작은 것 포함) / 순위상승=시총순위가 뛴 종목. 목적이 달라서 따로 본다. 두 시트에 같이 나오면 신호가 겹친 것",
                 f"시총 {LEAD_MC_MIN/1e9:.0f}십억$ 이상 AND 일거래대금 {LEAD_AMT_MIN/1e6:.0f}백만$ 이상 AND 상대강도 상위 {100-LEAD_RS_MIN:.0f}% AND 52주고점의 {LEAD_HIGH_MIN}% 이상 — 넷 다 통과한 종목만",
                 "다른 종목들과 비교해 주가가 얼마나 셌는지(100=가장 셈). 12개월40%+6개월30%+3개월30% 배합. 지수 데이터가 없어 '유니버스 안에서의 순위'로 계산",
                 "지금 주가가 1년 최고가의 몇 %인지. 100에 가까우면 신고가 근처",
                 "최근 20일 하루 평균 거래된 금액. 기관이 사고팔 수 있는 크기인지 보는 값",
                 "같은 섹터 큰 종목들($10십억$ 이상) 중 상대강도 몇 번째인지. 1~2위면 그 업종의 대장",
                 "지금 이익률이 최근 5년 최고~최저 중 어디쯤(100=제일 잘 벌 때, 0=제일 못 벌 때). 앞일을 맞히는 게 아니라 지금 위치만",
                 "확정 아님, '이럴 수도 있다'는 표시. 예전에 이익이 크게 꺾인 적 있는 경기민감주가 지금 최고면 '떨어질 수 있음', 지금 바닥이면 '반등할지 확인'",
                 "예전에 이익이 제일 크게 꺾였던 폭. 클수록 한 번 크게 무너진 적 있다는 뜻",
                 "회사값(시총)이 지금 버는 이익의 몇 배인지. 경기민감주는 제일 잘 벌 때라 싸 보이는 게 함정",
                 "주가 출렁임%·고점 대비 최대하락%=참고용(거르는 데 안 씀), 얼마나 담을지 정할 때만",
                 "이건 '후보 목록'이지 사라는 신호가 아님. 앞일 맞히는 것도 아님. 나눠서 조금씩이 전제"]}).to_excel(w,sheet_name="설명",index=False)
    print(f"기준일 {today} | universe {len(df)}")
    print(f"\n[Track C 주도주 ({len(lead)}종) — 시총≥{LEAD_MC_MIN/1e9:.0f}B·거래대금≥{LEAD_AMT_MIN/1e6:.0f}M·RS≥{LEAD_RS_MIN}·고점{LEAD_HIGH_MIN}%↑]")
    print(cc.head(15).to_string(index=False) if len(cc) else "  (게이트 통과 종목 없음)")
    print(f"\n[신규 Top진입 ({len(ne)}종) — 최근1년 상위권 신규진입]"); print(ne.head(12).to_string(index=False))
    print("\n[Track A Top10]"); print(a.head(10).to_string(index=False))
    print("\n저장:",out)

if __name__=="__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--backtest",action="store_true")
    args=ap.parse_args()
    backtest() if args.backtest else screen_now()
