#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""screen.py — 섹터/테마 통합 스크리너 (sector / theme / theme-daily).

  • sector      : 업종(섹터) 회전, 월간 marcap 시총 모멘텀 (1종목 1섹터 + 키워드 + 기타)
  • theme       : 시장 테마 회전, 월간 marcap 시총 모멘텀 (1종목 다(多)테마)
  • theme-daily : 단기 테마 회전, 일별 가격 모멘텀 (FinanceDataReader)

세 모드 모두 동일 점수 엔진(utils): pct_rank · momentum_score · consistency_ratio · assign_rank.
월간(sector/theme)은 MONTHLY_WEIGHTS(6M·3M·1M·거래대금·일관성·참여도 = 8.5),
일별(theme-daily)은 DAILY_WEIGHTS(2W·1W·1M·거래대금·일관성·참여도 = 8.0).

분류 정의(SECTOR_DEFS / THEMES)도 이 파일이 단일 출처.

실행:
  python screen.py sector
  python screen.py theme
  python screen.py theme-daily
  python screen.py all          # 셋 다 (기본값)
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

_PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_PKG_DIR))

from utils import (load_marcap_data, marcap_to_choeok, MARCAP_DIR, START_DATE, END_DATE,  # noqa: E402
                   momentum_score, consistency_ratio, assign_rank, rank_change_arrow,
                   MONTHLY_WEIGHTS, DAILY_WEIGHTS)

try:
    import FinanceDataReader as fdr  # noqa: E402
except ImportError:
    fdr = None


# ── 분류 정의 (단일 출처) ──
SECTOR_DEFS: list[tuple[str, list[str], list[str]]] = [
    ("반도체", [
        "005930","000660","042700","009150","000990","067310","098460",
        "312960","336370","102120","036930","054620","083930","240810",
        "357780","388790","079940","033640","131970","078600","095340",
        "107320","087730","036830","050960","025950",
    ], ["반도체","마이크로","파운드리","웨이퍼","실리콘","OLED","디스플레이"]),

    ("자동차/부품", [
        "005380","000270","012330","161390","073240","064960","204320",
        "011390","093240","007340","015360","263020","014620","058420",
    ], ["자동차","모터스","오토","타이어","부품","모비스"]),

    ("조선", [
        "329180","042660","009540","010140","267250","032560","009580",
    ], ["조선","중공업","해양","선박"]),

    ("방산/항공", [
        "012450","064350","047810","079550","272210","000155","086280",
        "047050","293490","018470",
    ], ["항공","방산","우주","미사일","레이더","방위"]),

    ("2차전지/배터리", [
        "373220","006400","247540","003670","096770","051910","066970",
        "298040","011790","006360","267260",
    ], ["배터리","에너지솔루션","이차전지","양극재","음극재","전해질","분리막"]),

    ("바이오/제약", [
        "207940","068270","196170","298380","128940","069620","000100",
        "105630","326030","214450","145020","091990","018290","013310",
        "000250","078160","023410",
    ], ["바이오","제약","헬스","의약","셀","진단","의료","치료","백신"]),

    ("금융/은행", [
        "105560","055550","086790","316140","024110","138930","175330",
        "138040","139130","279570","006220","060000",
    ], ["금융지주","은행"]),

    ("보험", [
        "000810","005830","032830","088350","003690","001450",
    ], ["보험","화재","생명","손해보험"]),

    ("증권", [
        "006800","039490","005940","016360","071050","071055","030610",
        "016610","003470","001270","001490",
    ], ["증권","투자증권","자산운용","캐피탈"]),

    ("IT/인터넷/플랫폼", [
        "035420","035720","323410","263750","251270","041510","039130",
    ], ["NAVER","카카오","인터넷","플랫폼","소프트웨어","클라우드"]),

    ("전력/전기기기", [
        "015760","010120","267260","298040","001440","023760","018260",
        "034020","012170","108670",
    ], ["전력","전기","LS ELECTRIC","일렉트릭","변압기","배전"]),

    ("화학/정유", [
        "051910","096770","010950","011170","006360","010600","004370",
        "003560","007570","009830","002360",
    ], ["화학","정유","케미칼","케미","폴리","비료","농약"]),

    ("철강/소재", [
        "005490","010130","004020","023430","001430","014820","004140",
    ], ["철강","포스코","POSCO","아연","구리","니켈","알루미늄","소재"]),

    ("건설/엔지니어링", [
        "000720","028050","047040","034300","000880",
        "078930","009415",
    ], ["건설","엔지니어링","건축","시공","시멘트","레미콘"]),

    ("물류/해운/항공", [
        "011200","180640","003490","086280","020560","006110",
    ], ["항공","해운","물류","운송","HMM","대한항공","아시아나"]),

    ("엔터/미디어/게임", [
        "352820","041510","035900","036570","259960","251270","293490",
        "047080","376300","263750",
    ], ["엔터","미디어","게임","콘텐츠","하이브","방탄","BTS","크래프톤","넥슨"]),

    ("통신", [
        "030200","017670","032640",
    ], ["KT","텔레콤","통신","SK텔레","LG유플"]),

    ("로봇/자동화", [
        "277810","090410","056190","014580","064480","298050","108490",
    ], ["로봇","자동화","레인보우","스마트팩토리","FA"]),

    ("지주/복합", [
        "402340","034730","003550","000880","003670","028260",
        "000150","000155","034020",
    ], ["지주","홀딩스","그룹"]),
]

THEMES: dict[str, list[str]] = {

    "방산": [
        "012450",  # 한화에어로스페이스
        "064350",  # 현대로템
        "047810",  # 한국항공우주
        "079550",  # LIG넥스원
        "272210",  # 한화시스템
        "000880",  # 한화
        "047050",  # 포스코인터내셔널
        "293490",  # 스페코
        "018470",  # 조일알미늄
        "071970",  # STX엔진
        "030200",  # KT (군 통신)
    ],

    "AI반도체": [
        "005930",  # 삼성전자
        "000660",  # SK하이닉스
        "042700",  # 한미반도체
        "009150",  # 삼성전기
        "036930",  # 주성엔지니어링
        "357780",  # 솔브레인
        "357870",  # 로지시스
        "095340",  # ISC
        "088790",  # 화승코퍼레이션
        "240810",  # 원익IPS
        "312960",  # 씨앤에프
        "083930",  # 아티스트유나이티드
        "131970",  # 두산테스나
        "078600",  # 오픈엣지테크놀로지
        "033640",  # 네패스아크
    ],

    "전력/전기기기/원전": [
        "267260",  # HD현대일렉트릭
        "010120",  # LS ELECTRIC
        "298040",  # 효성중공업
        "034020",  # 두산에너빌리티
        "015760",  # 한국전력
        "001440",  # 대한전선
        "023760",  # 하이트론
        "018260",  # 삼성에스디에스
        "108670",  # LX하우시스
        "036830",  # 솔브레인홀딩스
    ],

    "조선": [
        "329180",  # HD현대중공업
        "042660",  # 한화오션
        "009540",  # HD한국조선해양
        "010140",  # 삼성중공업
        "267250",  # HD현대
        "032560",  # 황금에스티
    ],

    "2차전지": [
        "373220",  # LG에너지솔루션
        "006400",  # 삼성SDI
        "247540",  # 에코프로비엠
        "086520",  # 에코프로
        "003670",  # 포스코퓨처엠
        "051910",  # LG화학
        "066970",  # LG이노텍
        "298040",  # 효성중공업
        "011790",  # SKC
        "006360",  # GS칼텍스(GS)
        "064290",  # 인지디스플레이
    ],

    "바이오/신약": [
        "207940",  # 삼성바이오로직스
        "068270",  # 셀트리온
        "196170",  # 알테오젠
        "298380",  # 에이비엘바이오
        "128940",  # 한미약품
        "069620",  # 대웅제약
        "000100",  # 유한양행
        "326030",  # SK바이오팜
        "214450",  # 파마리서치
        "145020",  # 휴젤
        "023410",  # 쎌바이오텍
        "000250",  # 삼천당제약
        "091990",  # 셀트리온헬스케어
    ],

    "로봇/자동화": [
        "277810",  # 레인보우로보틱스
        "090410",  # 고영
        "056190",  # 에스에프에이
        "064480",  # 케이엔솔
        "298050",  # 효성ITX
        "108490",  # 로보스타
        "014580",  # 태광
        "090360",  # 바이넥스
        "060250",  # NHN KCP
        "348210",  # 넥스트칩
    ],

    "현대차그룹": [
        "005380",  # 현대차
        "000270",  # 기아
        "012330",  # 현대모비스
        "064350",  # 현대로템
        "086280",  # 현대글로비스
        "267250",  # HD현대
        "329180",  # HD현대중공업
        "009540",  # HD한국조선해양
        "267260",  # HD현대일렉트릭
    ],

    "엔터/IP/콘텐츠": [
        "352820",  # 하이브
        "041510",  # SM엔터테인먼트
        "035900",  # JYP엔터테인먼트
        "036570",  # 엔씨소프트
        "259960",  # 크래프톤
        "251270",  # 넷마블
        "047080",  # 웹젠
        "376300",  # 디어유
        "293490",  # 카카오게임즈
        "263750",  # 펄어비스
    ],

    "플랫폼/AI서비스": [
        "035420",  # NAVER
        "035720",  # 카카오
        "323410",  # 카카오뱅크
        "263750",  # 펄어비스
        "018260",  # 삼성에스디에스
        "039130",  # 하나투어
        "041510",  # SM(카카오 연관)
    ],

    "철강/소재": [
        "005490",  # POSCO홀딩스
        "010130",  # 고려아연
        "004020",  # 현대제철
        "023430",  # 풍산
        "001430",  # 세아베스틸지주
    ],

    "해운/항공": [
        "011200",  # HMM
        "180640",  # 한진칼
        "003490",  # 대한항공
        "086280",  # 현대글로비스
        "020560",  # 아시아나항공
    ],

    "금융지주": [
        "105560",  # KB금융
        "055550",  # 신한지주
        "086790",  # 하나금융지주
        "316140",  # 우리금융지주
        "024110",  # 기업은행
        "138930",  # BNK금융지주
        "138040",  # 메리츠금융지주
    ],

    "증권": [
        "006800",  # 미래에셋증권
        "039490",  # 키움증권
        "005940",  # NH투자증권
        "016360",  # 삼성증권
        "071050",  # 한국금융지주
        "030610",  # 교보증권
        "001270",  # 부국증권
    ],

    "친환경/수소/ESG": [
        "096770",  # SK이노베이션
        "010950",  # S-Oil
        "006360",  # GS
        "011170",  # 롯데케미칼
        "009830",  # 한화솔루션
        "002360",  # 한화큐셀(한화솔루션)
        "034020",  # 두산에너빌리티 (SMR)
        "298380",  # 에이비엘바이오
    ],
}


# ════════════════════════════════════════════════════════════════════
#  분류
# ════════════════════════════════════════════════════════════════════

def _assign_sector(df: pd.DataFrame) -> pd.DataFrame:
    """업종: 종목코드 1:1 매핑 → 이름키워드 폴백 → 기타."""
    code_map: dict[str, str] = {}
    for sec, codes, _ in SECTOR_DEFS:
        for c in codes:
            c = c.strip()
            if c and c not in code_map:
                code_map[c] = sec
    df = df.copy()
    df["Sector"] = df["Code"].map(code_map)
    mask = df["Sector"].isna()
    for sec, _, kws in SECTOR_DEFS:
        if not kws:
            continue
        hit = mask & df["Name"].str.contains("|".join(kws), na=False)
        df.loc[hit, "Sector"] = sec
        mask = df["Sector"].isna()
    df["Sector"] = df["Sector"].fillna("기타")
    return df


_MCFG = {
    "sector": dict(gcol="Sector", label="업종", capcol="섹터시총_조원",
                   retfmt="섹터시총_{}M(%)", rankcol="모멘텀순위",
                   fprefix="sector_screen", head=8, multi=False),
    "theme":  dict(gcol="Theme", label="테마", capcol="테마시총_조원",
                   retfmt="시총증가율_{}M(%)", rankcol="테마순위",
                   fprefix="theme_screen", head=None, multi=True),
}


# ════════════════════════════════════════════════════════════════════
#  월간(시총) 엔진 — sector / theme 공용
# ════════════════════════════════════════════════════════════════════

def run_monthly(mode: str, base_dir: Path | None = None):
    cfg = _MCFG[mode]
    gcol = cfg["gcol"]
    root = base_dir if base_dir is not None else _PKG_DIR

    print(f"로드: {START_DATE} ~ {END_DATE}")
    df = load_marcap_data(START_DATE, END_DATE)
    print(f"  완료: {len(df):,}행, 최신: {df['Date'].max().date()}")

    ym = df["Date"].dt.to_period("M")
    last = df.groupby(ym, sort=True)["Date"].transform("max")
    df_m = df[df["Date"] == last].copy()          # 월말, 종목당 1행
    asof = df_m["Date"].max()
    print(f"기준일: {asof.date()}  (mode={mode})")

    if cfg["multi"]:                              # theme: 다대다
        tmap = pd.DataFrame([{"Theme": t, "Code": c.strip()}
                             for t, cs in THEMES.items() for c in cs])
        grouped_src = df_m.merge(tmap, on="Code", how="inner")
    else:                                         # sector: 1:1 + 기타
        df_m = _assign_sector(df_m)
        grouped_src = df_m

    gm = (grouped_src.groupby(["Date", gcol], sort=True)
          .agg(Marcap_sum=("Marcap", "sum"), Amount_sum=("Amount", "sum"), cnt=("Code", "count"))
          .reset_index().sort_values([gcol, "Date"]))
    g = gm.groupby(gcol, group_keys=False)
    gm["mc_1m_ago"] = g["Marcap_sum"].shift(1)
    gm["mc_3m_ago"] = g["Marcap_sum"].shift(3)
    gm["mc_6m_ago"] = g["Marcap_sum"].shift(6)
    gm["amt_roll2"] = g["Amount_sum"].transform(lambda s: s.rolling(2, min_periods=1).mean())
    gm["amt_roll2_prev"] = g["amt_roll2"].shift(2)
    gm["ret_1m"]    = (gm["Marcap_sum"] / gm["mc_1m_ago"].replace(0, float("nan")) - 1).astype(float)
    gm["ret_3m"]    = (gm["Marcap_sum"] / gm["mc_3m_ago"].replace(0, float("nan")) - 1).astype(float)
    gm["ret_6m"]    = (gm["Marcap_sum"] / gm["mc_6m_ago"].replace(0, float("nan")) - 1).astype(float)
    gm["amt_accel"] = (gm["amt_roll2"] / gm["amt_roll2_prev"].replace(0, float("nan")) - 1).astype(float)

    prev_dates = gm[gm["Date"] < asof]["Date"]
    prev_asof  = prev_dates.max() if not prev_dates.empty else None

    def _risk(sub: pd.DataFrame) -> pd.Series:
        valid = sub["ret_6m_stk"].dropna()
        tot = sub["Marcap"].sum()
        return pd.Series({
            "down_ratio":  float((valid < 0).sum() / max(len(valid), 1)),
            "top_weight":  float(sub["Marcap"].max() / tot * 100) if tot > 0 else float("nan"),
            "participate": float((valid > 0).sum() / max(len(valid), 1)),
        })

    def _snapshot(asof_date):
        snap = gm[gm["Date"] == asof_date].copy()
        snap = snap[snap["mc_6m_ago"].notna() & (snap["mc_6m_ago"] > 0)].copy()
        win = gm[(gm["Date"] > asof_date - pd.DateOffset(months=6)) & (gm["Date"] <= asof_date)]
        cons = win.groupby(gcol)["ret_1m"].apply(consistency_ratio).rename("consistency")
        tg = grouped_src[grouped_src["Date"] == asof_date].copy()
        base = df_m[df_m["Date"] <= (asof_date - pd.DateOffset(months=5))]
        if not base.empty:
            cprev = df_m[df_m["Date"] == base["Date"].max()].set_index("Code")["Marcap"]
            tg["ret_6m_stk"] = tg["Marcap"] / tg["Code"].map(cprev) - 1
        else:
            tg["ret_6m_stk"] = float("nan")
        rsk = tg.groupby(gcol).apply(_risk).reset_index()
        snap = snap.merge(cons, on=gcol, how="left").merge(rsk, on=gcol, how="left")
        snap["consistency"] = snap["consistency"].fillna(0.5)
        snap["participate"] = snap["participate"].fillna(0.5)
        snap["score"] = momentum_score(snap, MONTHLY_WEIGHTS)
        return snap, tg

    cur, cur_tagged = _snapshot(asof)
    cur = assign_rank(cur, "score", cfg["rankcol"])
    if prev_asof is not None:
        prev, _ = _snapshot(prev_asof)
        prev = assign_rank(prev, "score", "prev_rank")
        cur = cur.merge(prev[[gcol, "prev_rank"]], on=gcol, how="left")
        cur["순위변화"] = (cur["prev_rank"] - cur[cfg["rankcol"]]).fillna(0).astype(int)
    else:
        cur["순위변화"] = 0
    cur["순위변화_표시"] = cur["순위변화"].apply(rank_change_arrow)

    # ── 랭킹 시트 ──
    cap = marcap_to_choeok(cur_tagged.groupby(gcol)["Marcap"].sum())
    rf = cfg["retfmt"]
    export = cur[[gcol, "cnt", "ret_6m", "ret_3m", "ret_1m", "amt_accel",
                  "consistency", "participate", "score", cfg["rankcol"],
                  "순위변화_표시", "down_ratio", "top_weight"]].copy()
    export.insert(2, cfg["capcol"], export[gcol].map(cap).round(1))
    export[rf.format(6)] = (export["ret_6m"] * 100).round(2)
    export[rf.format(3)] = (export["ret_3m"] * 100).round(2)
    export[rf.format(1)] = (export["ret_1m"] * 100).round(2)
    export["거래대금가속_2M(%)"] = (export["amt_accel"] * 100).round(2)
    export["모멘텀일관성"]       = export["consistency"].round(2)
    export["종목참여도"]         = export["participate"].round(2)
    export["모멘텀점수"]         = export["score"].round(4)
    export["하락종목비율(%)"]    = (export["down_ratio"] * 100).round(1)
    export["최대종목비중(%)"]    = export["top_weight"].round(1)
    export = export.rename(columns={gcol: cfg["label"], "cnt": "구성종목수"})[
        [cfg["rankcol"], "순위변화_표시", cfg["label"], cfg["capcol"], "구성종목수",
         rf.format(6), rf.format(3), rf.format(1), "거래대금가속_2M(%)",
         "모멘텀일관성", "종목참여도", "모멘텀점수", "하락종목비율(%)", "최대종목비중(%)"]
    ]

    # ── 구성종목 상세 시트 ──
    code_cur = df_m[df_m["Date"] == asof].set_index("Code")["Marcap"]
    base3 = df_m[df_m["Date"] <= (asof - pd.DateOffset(months=2))]
    if not base3.empty:
        cprev3 = df_m[df_m["Date"] == base3["Date"].max()].set_index("Code")["Marcap"]
        ret3 = (code_cur / cprev3 - 1).rename("ret_3m_stock")
    else:
        ret3 = pd.Series(dtype=float, name="ret_3m_stock")
    det = cur_tagged.join(ret3, on="Code")
    det["시총_조원"]        = marcap_to_choeok(det["Marcap"]).round(2)
    det["시총증가율_6M(%)"]  = (det["ret_6m_stk"] * 100).round(2)
    det["시총증가율_3M(%)"]  = (det["ret_3m_stock"] * 100).round(2)
    rmap = export.set_index(cfg["label"])[cfg["rankcol"]].to_dict()
    det.insert(0, cfg["rankcol"], det[gcol].map(rmap))
    det = det.sort_values([cfg["rankcol"], "Marcap"], ascending=[True, False])
    if cfg["head"]:
        det = det.groupby(gcol, group_keys=False).head(cfg["head"])
    det = det[[cfg["rankcol"], gcol, "Code", "Name", "Rank",
               "시총_조원", "시총증가율_6M(%)", "시총증가율_3M(%)"]].rename(
        columns={gcol: cfg["label"], "Code": "종목코드", "Name": "종목명", "Rank": "시총순위"})

    tag = asof.strftime("%Y%m%d")
    for fn in [f"{cfg['fprefix']}_{tag}.xlsx", f"{cfg['fprefix']}_latest.xlsx"]:
        with pd.ExcelWriter(root / fn, engine="openpyxl") as w:
            export.to_excel(w, sheet_name=f"{cfg['label']}랭킹", index=False)
            det.to_excel(w, sheet_name=f"{cfg['label']}별종목", index=False)
    print(f"저장: {cfg['fprefix']}_{tag}.xlsx / {cfg['fprefix']}_latest.xlsx")
    print(export.head(15).to_string(index=False))
    return export


# 하위호환 별칭
def run_sector_screen(base_dir: Path | None = None):
    return run_monthly("sector", base_dir)


def run_theme_screen(base_dir: Path | None = None):
    return run_monthly("theme", base_dir)


# ════════════════════════════════════════════════════════════════════
#  일별(가격) 엔진 — theme-daily
# ════════════════════════════════════════════════════════════════════

CACHE_DIR      = _PKG_DIR / "daily_cache"
PREV_RANK_FILE = CACHE_DIR / "_prev_daily_rank.parquet"
FETCH_DAYS     = 90
MIN_DAYS       = 22
PERIOD_1W  = 5
PERIOD_2W  = 10
PERIOD_1M  = 21
CONSIST_N  = 10


def _cache_path(code: str) -> Path:
    return CACHE_DIR / f"{code}.parquet"


def _load_or_fetch(code: str, today: date) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp = _cache_path(code)

    existing = pd.DataFrame()
    fetch_start = today - timedelta(days=int(FETCH_DAYS * 1.5))

    if cp.exists():
        existing = pd.read_parquet(cp)
        if not existing.empty:
            last_cached = existing.index.max().date()
            if last_cached >= today:
                return existing
            fetch_start = last_cached + timedelta(days=1)

    try:
        new = fdr.DataReader(code, fetch_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))
    except Exception:
        return existing

    if new.empty:
        return existing

    new.index = pd.to_datetime(new.index)
    combined = pd.concat([existing, new[~new.index.isin(existing.index)]]) if not existing.empty else new
    combined = combined.sort_index()

    # 최근 180일만 보관
    cutoff = pd.Timestamp(today) - pd.Timedelta(days=180)
    combined = combined[combined.index >= cutoff]

    combined.to_parquet(cp)
    return combined


def _calc_stock_metrics(df: pd.DataFrame) -> dict | None:
    if len(df) < MIN_DAYS:
        return None

    close  = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    amount = close * volume

    c_now = close.iloc[-1]
    c_1w  = close.iloc[-1 - PERIOD_1W]  if len(close) > PERIOD_1W  else None
    c_2w  = close.iloc[-1 - PERIOD_2W]  if len(close) > PERIOD_2W  else None
    c_1m  = close.iloc[-1 - PERIOD_1M]  if len(close) > PERIOD_1M  else None

    ret_1w = float(c_now / c_1w - 1) if c_1w else None
    ret_2w = float(c_now / c_2w - 1) if c_2w else None
    ret_1m = float(c_now / c_1m - 1) if c_1m else None

    # 거래대금 가속: 최근 5일 vs 직전 5일
    amt_r = amount.iloc[-PERIOD_1W:].mean()
    amt_p = amount.iloc[-PERIOD_1W * 2:-PERIOD_1W].mean()
    vol_accel = float(amt_r / amt_p - 1) if (amt_p and amt_p > 0) else None

    # 단기 일관성: 최근 N일 중 상승 일수 비율
    recent_n = close.iloc[-CONSIST_N:]
    daily_ret = recent_n.pct_change().dropna()
    consistency = float((daily_ret > 0).sum() / max(len(daily_ret), 1))

    # MDD (최대낙폭, 최근 21일)
    window = close.iloc[-PERIOD_1M:]
    rolling_max = window.cummax()
    drawdowns = (window / rolling_max - 1)
    mdd = float(drawdowns.min()) if len(drawdowns) > 0 else 0.0

    return {
        "ret_1w":      ret_1w,
        "ret_2w":      ret_2w,
        "ret_1m":      ret_1m,
        "vol_accel":   vol_accel,
        "consistency": consistency,
        "mdd":         mdd,
        "close":       c_now,
        "last_date":   df.index[-1].date(),
    }


def run_theme_daily_screen(base_dir: Path | None = None) -> None:
    root  = base_dir if base_dir is not None else _PKG_DIR
    today = date.today()

    all_codes = sorted({c.strip() for codes in THEMES.values() for c in codes})
    print(f"종목 {len(all_codes)}개 데이터 로드 중 (캐시 활용)...")

    stock_metrics: dict[str, dict] = {}
    for i, code in enumerate(all_codes):
        df = _load_or_fetch(code, today)
        m  = _calc_stock_metrics(df)
        if m:
            stock_metrics[code] = m
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(all_codes)} 완료...")
        time.sleep(0.1)

    if not stock_metrics:
        print("[오류] 데이터를 가져올 수 없습니다.")
        return

    latest_date = max(v["last_date"] for v in stock_metrics.values())
    print(f"기준일: {latest_date}  유효 종목: {len(stock_metrics)}개")

    # ── 테마별 집계 ───────────────────────────────────────────────────
    rows       = []
    stock_rows = []

    for theme, codes in THEMES.items():
        valid = [c for c in codes if c.strip() in stock_metrics]
        if not valid:
            continue

        ml = [stock_metrics[c] for c in valid]

        def _avg(key: str) -> float | None:
            vals = [m[key] for m in ml if m.get(key) is not None]
            return float(sum(vals) / len(vals)) if vals else None

        # 종목 참여도: 1M 상승 종목 비율
        ret1m_vals = [m["ret_1m"] for m in ml if m.get("ret_1m") is not None]
        participate = float(sum(1 for r in ret1m_vals if r > 0) / max(len(ret1m_vals), 1))

        # 1W 하락 종목 비율 (리스크)
        ret1w_vals = [m["ret_1w"] for m in ml if m.get("ret_1w") is not None]
        down_1w    = float(sum(1 for r in ret1w_vals if r < 0) / max(len(ret1w_vals), 1))

        rows.append({
            "테마":        theme,
            "구성종목수":   len(valid),
            "ret_1w":       _avg("ret_1w"),
            "ret_2w":       _avg("ret_2w"),
            "ret_1m":       _avg("ret_1m"),
            "vol_accel":    _avg("vol_accel"),
            "consistency":  _avg("consistency"),
            "mdd_avg":      _avg("mdd"),
            "participate":  participate,
            "down_1w":      down_1w,
        })

        for code in valid:
            m = stock_metrics[code]
            stock_rows.append({
                "테마":           theme,
                "종목코드":       code,
                "수익률_1W(%)":   round(m["ret_1w"]    * 100, 2) if m["ret_1w"]    is not None else None,
                "수익률_2W(%)":   round(m["ret_2w"]    * 100, 2) if m["ret_2w"]    is not None else None,
                "수익률_1M(%)":   round(m["ret_1m"]    * 100, 2) if m["ret_1m"]    is not None else None,
                "거래대금가속(%)": round(m["vol_accel"] * 100, 2) if m["vol_accel"] is not None else None,
                "일관성":         round(m["consistency"], 2),
                "MDD(%)":         round(m["mdd"] * 100, 2),
                "현재가":         int(m["close"]),
                "기준일":         str(m["last_date"]),
            })

    theme_df = pd.DataFrame(rows)
    if theme_df.empty:
        print("[경고] 집계된 테마가 없습니다.")
        return

    # ── 점수 산출 (클램프 제거) ───────────────────────────────────────
    for col in ["ret_1w", "ret_2w", "ret_1m", "vol_accel", "consistency", "mdd_avg", "participate"]:
        theme_df[col] = pd.to_numeric(theme_df[col], errors="coerce")

    # 공용 산식: 일별 가중치(DAILY_WEIGHTS)로 momentum_score
    theme_df["consistency"] = theme_df["consistency"].fillna(0.5)
    theme_df["participate"] = theme_df["participate"].fillna(0.5)
    theme_df["score"] = momentum_score(theme_df, DAILY_WEIGHTS)
    theme_df = assign_rank(theme_df, "score", "테마순위")

    # ── 이전 실행 순위와 비교 ─────────────────────────────────────────
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if PREV_RANK_FILE.exists():
        prev_rank_df = pd.read_parquet(PREV_RANK_FILE)
        prev_map = prev_rank_df.set_index("테마")["테마순위"].to_dict()
        theme_df["이전순위"] = theme_df["테마"].map(prev_map).fillna(0).astype(int)
        theme_df["순위변화"] = (theme_df["이전순위"] - theme_df["테마순위"]).astype(int)
        theme_df.loc[theme_df["이전순위"] == 0, "순위변화"] = 0
    else:
        theme_df["순위변화"] = 0

    theme_df["순위변화_표시"] = theme_df["순위변화"].apply(rank_change_arrow)

    # 현재 순위 저장 (다음 실행 비교용)
    theme_df[["테마", "테마순위"]].to_parquet(PREV_RANK_FILE, index=False)

    # ── 출력 테이블 ───────────────────────────────────────────────────
    export = theme_df[[
        "테마순위", "순위변화_표시", "테마", "구성종목수",
        "ret_1w", "ret_2w", "ret_1m", "vol_accel",
        "consistency", "participate", "mdd_avg", "down_1w", "score"
    ]].copy()
    export["수익률_1W(%)"]     = (export["ret_1w"]     * 100).round(2)
    export["수익률_2W(%)"]     = (export["ret_2w"]     * 100).round(2)
    export["수익률_1M(%)"]     = (export["ret_1m"]     * 100).round(2)
    export["거래대금가속(%)"]  = (export["vol_accel"]  * 100).round(2)
    export["단기일관성"]       = export["consistency"].round(2)
    export["종목참여도"]       = export["participate"].round(2)
    export["MDD평균(%)"]       = (export["mdd_avg"]    * 100).round(2)
    export["1W하락종목비율(%)"] = (export["down_1w"]   * 100).round(1)
    export["모멘텀점수"]       = export["score"].round(4)
    export = export[[
        "테마순위", "순위변화_표시", "테마", "구성종목수",
        "수익률_1W(%)", "수익률_2W(%)", "수익률_1M(%)", "거래대금가속(%)",
        "단기일관성", "종목참여도", "모멘텀점수",
        "MDD평균(%)", "1W하락종목비율(%)"
    ]]

    # ── 종목 상세 ─────────────────────────────────────────────────────
    detail_df = pd.DataFrame(stock_rows)
    rank_map  = export.set_index("테마")["테마순위"].to_dict()
    detail_df.insert(0, "테마순위", detail_df["테마"].map(rank_map))

    # 종목명 조회
    try:
        marcap_dir  = MARCAP_DIR
        latest_year = max(int(p.stem.split("-")[1]) for p in marcap_dir.glob("marcap-*.parquet"))
        name_df     = pd.read_parquet(marcap_dir / f"marcap-{latest_year}.parquet",
                                      columns=["Code", "Name"]).drop_duplicates("Code")
        name_map    = name_df.set_index("Code")["Name"].to_dict()
        detail_df.insert(3, "종목명", detail_df["종목코드"].map(name_map))
    except Exception:
        detail_df.insert(3, "종목명", "")

    detail_df = detail_df.sort_values(["테마순위", "수익률_2W(%)"], ascending=[True, False])

    # ── 저장 ─────────────────────────────────────────────────────────
    tag = latest_date.strftime("%Y%m%d")
    for fname in [f"theme_daily_{tag}.xlsx", "theme_daily_latest.xlsx"]:
        path = root / fname
        with pd.ExcelWriter(path, engine="openpyxl") as w:
            export.to_excel(w,    sheet_name="테마랭킹",    index=False)
            detail_df.to_excel(w, sheet_name="테마별종목",  index=False)

    print(f"\n저장: {root / f'theme_daily_{tag}.xlsx'}")
    print(f"저장(고정명): {root / 'theme_daily_latest.xlsx'}")
    print("\n테마 단기 모멘텀 순위:")
    print(export.to_string(index=False))


# ════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="섹터/테마 통합 스크리너")
    ap.add_argument("mode", nargs="?", default="all",
                    choices=["sector", "theme", "theme-daily", "all"],
                    help="실행 모드 (기본 all)")
    args = ap.parse_args()
    if args.mode in ("sector", "all"):
        print("\n" + "=" * 60 + "\n  [sector] 업종 회전 (월간)\n" + "=" * 60)
        run_monthly("sector")
    if args.mode in ("theme", "all"):
        print("\n" + "=" * 60 + "\n  [theme] 테마 회전 (월간)\n" + "=" * 60)
        run_monthly("theme")
    if args.mode in ("theme-daily", "all"):
        print("\n" + "=" * 60 + "\n  [theme-daily] 테마 단기 (일별)\n" + "=" * 60)
        if fdr is None:
            print("[건너뜀] FinanceDataReader 미설치")
        else:
            run_theme_daily_screen()


if __name__ == "__main__":
    main()
