#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
replay.py — 과거 구간 되감기 시뮬레이션 (auto_buy 의 규칙을 그대로 과거에 적용).

무엇을 하나:
  리밸런스일(매주 월요일 / 매월 첫 월요일)마다
    ① 기준일 asof = (리밸런스일 − 3일) 이하의 마지막 거래일  → 보통 직전 금요일
    ② 그 시점의 주도주 목록을 leader_screener.screen_now() 와 같은 순서로 다시 계산
       (factor_eval.py 의 재현 순서를 복사. 결과는 paper/replay_cache/ 에 캐시)
    ③ rules.select_candidates → rules.size_orders   (auto_buy.py 와 같은 함수·같은 설정)
    ④ PaperBroker 로 리밸런스일 이후 첫 거래일 시가 × (1+슬리피지) 에 체결, 수수료 차감
    ⑤ 매 거래일 종가로 평가 (equity_history) → 최대낙폭
    ⑥ exit.enabled 면 목록(주도주 시트)에서 weeks_absent 회 연속 빠진 보유 종목을 다음 시가에 매도
  결과: paper_replay_latest.xlsx
        (요약 / 자산추이 / 거래내역 / 리밸런스별목록 / 리밸런스요약 / 설정 — 헤더 한글)

사용:
  ./.venv/bin/python auto_buy.py replay --cadence monthly --start 2026-03-02
  ./.venv/bin/python replay.py --start 2025-01-06 --cadence weekly [--end ...] [--refresh]
  코드에서: replay.replay(start, end, cadence, cfg) → 요약 DataFrame

정직한 한계 (결과 해석 시 감안):
  - 분기 재무: quarterly_financials_us 에 공시일이 없어 END_DATE + financial_lag_days(기본 45일,
    10-Q 제출 지연 흉내) 이후에만 안 것으로 취급한다(cfg.replay.financial_lag_days). 캐시 파일명에 지연 일수가
    들어간다. 섹터·이름은 현재 매핑. 상장폐지 종목은 유니버스에 없음(생존편향).
  - daily_price_us 는 분할 미조정이다. 하루에 시가·종가가 동시에 전일종가의 ×0.6 미만 또는
    ×1.65 초과로 불연속이고 그 배수가 정수비(2:1, 10:1, 1:2 …)에 ±3% 이내이며,
    **daily_marcap_us.STOCKS(발행주식수)가 그 날 ±5거래일 안에 같은 배수(±3%)로 변한 경우에만**
    분할로 확정하고 그 이전 가격을 소급 조정한다. STOCKS 가 안 변했으면(인수 발표·급락 같은 진짜
    가격 이벤트, 거래량 20일 평균의 10배 이상이면 '가격 이벤트' 로 표시) 조정하지 않는다.
    조정/미조정 내역은 요약 시트에 적는다.
  - 벤치마크 두 가지(비용 0): ① 유니버스 동일가중 매수후보유(첫 체결일 전액 투입) ② 전략과 같은 현금
    스케줄(리밸런스마다 전략이 실제 쓴 금액만큼 동일가중 투입, 나머지 현금). 전략은 슬리피지·수수료 반영.
  - weekly_cap_usd 는 '리밸런스 1회당' 한도로 적용된다(monthly 도 같은 값).
"""
import argparse
import bisect
import json
import logging
import os
import pickle
import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import rules                                          # noqa: E402
import strategy                                  # noqa: E402  (v2 규칙)
from broker_paper import PaperBroker, KST             # noqa: E402

CACHE_DIR = os.path.join(BASE_DIR, "paper", "replay_cache")
CACHE_VER = "v2"               # v2: 재무 지연(lag) 적용 + 분할을 STOCKS 로 검증 (v1 캐시는 무시된다)
DEFAULT_START = "2025-01-06"
DEFAULT_FIN_LAG_DAYS = 45      # 분기말(END_DATE) 뒤 이 일수가 지나야 그 분기 재무를 '아는' 것으로
OUT_XLSX = os.path.join(BASE_DIR, "paper_replay_latest.xlsx")
REPLAY_STATE = os.path.join(BASE_DIR, "paper", "replay_state.json")

# 분할 감지 밴드: 시가·종가가 모두 전일종가 대비 이 범위를 벗어나면 불연속으로 본다
SPLIT_LO, SPLIT_HI = 0.6, 1.65
SPLIT_SNAP_TOL = 0.03          # 배수가 정수비에서 ±3% 이내여야 분할 후보 (구 ±8% 는 급락/급등을 분할로 오인)
SPLIT_STOCKS_TOL = 0.03        # 발행주식수(STOCKS) 변화 배수가 분할 배수의 ±3% 이내면 확정
SPLIT_STOCKS_WINDOW = 5        # STOCKS 변화를 찾는 창: 불연속일 ±5 거래일 (APH 처럼 하루 늦게 반영되는 경우)
EVENT_VOL_MULT = 10.0          # 거래량이 직전 20일 평균의 10배 이상 + STOCKS 불변 → '가격 이벤트'

log = logging.getLogger("auto_buy.replay")

# ── leader_screener.screen_now() 에서 그대로 복사한 표시용 매핑 ────────────
# (leader_screener.py 를 고치면 여기도 맞춰야 한다. rules 가 쓰는 열: 티커/종목명/섹터/주의/주도주점수)
SKO = {"Information Technology": "IT", "Health Care": "헬스케어", "Industrials": "산업재",
       "Consumer Discretionary": "임의소비", "Consumer Staples": "필수소비", "Financials": "금융",
       "Communication Services": "커뮤니", "Energy": "에너지", "Materials": "소재",
       "Real Estate": "부동산", "Utilities": "유틸"}
KOR = {"trackA_rank": "순위", "trackB_rank": "순위", "leader_rank": "순위", "CODE": "티커", "NAME": "종목명",
       "MC0_B": "시총(십억$)", "RANK0": "시총순위", "RANK_1y": "1년전순위", "rev_yoy": "매출증가율%",
       "rev_streak": "매출연속성장(분기)", "recent_consist": "최근꾸준%", "rev_accel": "매출가속도",
       "ttm_g": "연간매출성장%", "margin_trend": "영업마진추세%p", "margin_std": "마진변동성%p",
       "margin_pos": "마진위치%", "margin_dd": "예전 이익하락폭%p", "p_op": "시총/영업이익(배)",
       "margin_tcorr": "마진추세상관", "pos_ratio": "성장지속%", "fund_z": "펀더멘털점수",
       "vol_ann": "주가변동성%", "mdd_1y": "최대낙폭%", "rank_up": "순위상승폭", "hybrid": "종합점수",
       "rs_pct": "상대강도(0~100)", "near_high": "52주고점대비%", "amt20_m": "일거래대금(백만$)",
       "amt_grow": "거래대금증가%", "sec_rank": "섹터내순위", "lead_score": "주도주점수",
       "is_spike": "단발스파이크", "탈락사유": "탈락사유"}
# 주도주 시트 열 순서 (screen_now 의 ccol). vol_ann/mdd_1y 는 add_vol() 이 '현재' 기준이라(선견) 뺀다.
CCOL = ["leader_rank", "CODE", "NAME", "섹터", "유형", "주의", "MC0_B", "RANK0", "rs_pct", "near_high",
        "amt20_m", "amt_grow", "sec_rank", "rev_streak", "recent_consist", "p_op", "fund_z", "lead_score"]
UNIVERSE_COLS = ["CODE", "NAME", "섹터", "유형", "주의", "MC0_B", "RANK0", "rs_pct", "near_high", "amt20_m",
                 "amt_grow", "fund_z", "lead_score", "leader_pass", "leader_rank", "탈락사유", "trackA_rank",
                 "trackB_pass"]


def empty_watchlist():
    return pd.DataFrame(columns=[KOR.get(c, c) for c in CCOL])


# ── leader_screener 지연 import (import 시 DB 접속 + 분기재무 로드 → 1 커넥션) ──
_L = None


def screener():
    global _L
    if _L is None:
        import leader_screener as L   # __main__ 가드가 있어 import 해도 실행되지 않음
        _L = L
    return _L


def dq(sql):
    return screener().dq(sql)


def db_latest_date():
    return dq("SELECT TO_CHAR(MAX(date_),'YYYY-MM-DD') D FROM daily_price_us")["D"].iloc[0]


# ── screen_now() 의 유형/주의 판정 복사본 (내부 함수라 import 불가) ──────────
def _typ(r, cyc_hard, std_t, trend_t):
    code = r.get("CODE"); ms = r.get("margin_std"); tc = r.get("margin_tcorr")
    pr = r.get("pos_ratio"); mmed = r.get("margin_med")
    volatile = (ms == ms and ms >= std_t)
    uptrend = (tc == tc and tc >= trend_t)
    profitable = (mmed == mmed and mmed > 0)
    if (code in cyc_hard) or (volatile and not uptrend and profitable):
        return "시클리컬"
    if volatile and uptrend:
        return "마진확장"
    if pr == pr and pr >= 85:
        return "꾸준복리"
    return ""


def _warn(r):
    notes = []
    typ = r.get("유형"); mp = r.get("margin_pos"); dd = r.get("margin_dd")
    hl = r.get("had_loss"); rj = r.get("rev_jump")
    if typ == "시클리컬" and mp == mp:
        if mp >= 80 and ((dd == dd and dd <= -25) or hl == 1):
            notes.append("지금 이익 최고 → 떨어질 수 있음")
        elif mp <= 30:
            notes.append("지금 이익 바닥 → 반등할지 확인")
    st = r.get("rev_streak")
    if rj == rj and rj >= 80 and not (st == st and st >= 2):
        notes.append("매출이 한 분기만 반짝 → 단발 이벤트·합병·상장초기인지 확인(지속 성장 아닐 수 있음)")
    return " / ".join(notes)


# ── as-of 스크리닝 (캐시) ────────────────────────────────────────────────
def cache_path(asof, lag_days=DEFAULT_FIN_LAG_DAYS):
    return os.path.join(CACHE_DIR, f"screen_{CACHE_VER}_lag{int(lag_days)}_{asof}.pkl")


_warned_stale = False


class _LaggedFinancials:
    """leader_screener.rev_all 을 'asof − lag_days 이전에 분기말이 끝난 분기' 만 남기도록 일시 교체.
    leader_screener 는 고치지 않고 모듈 전역만 바꿨다가 with 블록을 나가면 원래대로 돌린다.
    (rev_factors 가 모듈 전역 rev_all 을 읽으므로 build(asof) 안의 모든 재무 계산에 적용된다)"""

    def __init__(self, L, asof, lag_days):
        self.L, self.asof, self.lag = L, asof, int(lag_days)
        self.orig = None

    def __enter__(self):
        self.orig = self.L.rev_all
        if self.lag > 0:
            cut = pd.Timestamp(self.asof) - pd.Timedelta(days=self.lag)
            self.L.rev_all = self.orig[self.orig["END_DATE"] <= cut]
        return self

    def __exit__(self, *exc):
        self.L.rev_all = self.orig
        return False


def screen_asof(asof, refresh=False, use_cache=True, lag_days=DEFAULT_FIN_LAG_DAYS):
    """asof 시점의 주도주 목록을 screen_now() 와 같은 순서로 재현.
    lag_days: 분기말(END_DATE) 이 asof − lag_days 이전인 분기 재무만 쓴다 (공시 지연 흉내, 캐시 키에 포함).
    반환 dict: {asof, watchlist(주도주 시트와 같은 한글 열), universe(축약 열), n_universe, elapsed, cached}
    factor_eval.py 58~117행의 재현 순서를 복사한 것 — leader_screener 는 수정하지 않는다."""
    global _warned_stale
    path = cache_path(asof, lag_days)
    if use_cache and not refresh and os.path.exists(path):
        try:
            with open(path, "rb") as f:
                d = pickle.load(f)
            if d.get("ver") == CACHE_VER and "watchlist" in d and int(d.get("lag_days", -1)) == int(lag_days):
                d["cached"] = True
                ls = os.path.join(BASE_DIR, "leader_screener.py")
                if (not _warned_stale and os.path.exists(ls)
                        and os.path.getmtime(ls) > float(d.get("screener_mtime") or 0)):
                    log.warning("replay 캐시가 leader_screener.py 수정 전에 만들어짐 → 최신 규칙으로 다시 계산하려면 --refresh")
                    _warned_stale = True
                return d
        except Exception as e:
            log.warning("캐시 읽기 실패 %s: %s (다시 계산)", path, e)

    L = screener()
    t0 = time.time()
    with _LaggedFinancials(L, asof, lag_days):
        df = L.build(asof)
    wl = empty_watchlist()
    if df.empty:
        uni = pd.DataFrame(columns=UNIVERSE_COLS)
    else:
        # screen_now() 와 같은 순서: 섹터 매핑 → 부동산 제외 → price_metrics 병합 → 순위 재계산 → build_leaders
        sec = L.fa.get_sector_map(L.conn, "ticker_master_us") if hasattr(L.fa, "get_sector_map") else {}
        nm = L.fa.get_name_map(L.conn, "ticker_master_us") if hasattr(L.fa, "get_name_map") else {}
        df["NAME"] = df["CODE"].map(nm) if nm else ""
        # 주의: 섹터·이름은 현재 시점 매핑이라 과거 시점엔 미세한 선견이 있다 (factor_eval 과 동일)
        df["섹터"] = df["CODE"].map(lambda c: SKO.get(sec.get(c, ""), (sec.get(c, "") or "?")))
        df = df[df["섹터"] != "부동산"].copy()
        try:
            pm = L.price_metrics(asof)
            if not pm.empty:
                df = df.merge(pm, on="CODE", how="left")
        except Exception as e:
            log.warning("  [경고] %s price_metrics 실패: %s", asof, str(e)[:70])
        df["trackA_rank"] = df["fund_rank_key"].rank(ascending=False, method="min").astype(int)
        df["유형"] = df.apply(lambda r: _typ(r, L.CYC_HARD, L.CYC_STD_T, L.CYC_TREND_T), axis=1)
        df["주의"] = df.apply(_warn, axis=1)
        if "rs_pct" in df.columns:
            df, lead = L.build_leaders(df)
        else:
            lead = pd.DataFrame()
        if len(lead):
            cols = [c for c in CCOL if c in lead.columns]
            wl = lead[cols].rename(columns=KOR).reset_index(drop=True)
        uni = df[[c for c in UNIVERSE_COLS if c in df.columns]].copy()
    elapsed = time.time() - t0
    d = {"ver": CACHE_VER, "asof": asof, "lag_days": int(lag_days),
         "created": datetime.now(KST).isoformat(timespec="seconds"),
         "elapsed": round(elapsed, 1), "watchlist": wl, "universe": uni, "n_universe": len(uni),
         "screener_mtime": os.path.getmtime(os.path.join(BASE_DIR, "leader_screener.py"))
         if os.path.exists(os.path.join(BASE_DIR, "leader_screener.py")) else 0, "cached": False}
    if use_cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(d, f)
        os.replace(tmp, path)
    return d


# ── 가격 패널 (분할 조정) ────────────────────────────────────────────────
def _stocks_ratio_near(stocks_by_code, code, pos_of_day, D, window=SPLIT_STOCKS_WINDOW):
    """code 의 STOCKS(발행주식수) 시계열에서 D ±window 거래일 안의 변화 배수 후보들.
    앞 구간(D−window−1 … D−1)의 각 값 b 와 뒤 구간(D−window … D+window)의 각 값 a 로 a/b 를 전부 만든다.
    → 주식수 반영이 며칠 빠르거나 늦거나(APH 처럼) 하루 흔들려도 진짜 변화면 어떤 쌍은 배수에 맞고,
      안 변했으면(MRNA·AVB 같은 가격 이벤트) 전부 1.0 근처라 절대 맞지 않는다.
    반환: (배수 리스트 | None(데이터 없음))"""
    s = stocks_by_code.get(code)
    if s is None or len(s) == 0:
        return None
    i = pos_of_day.get((code, D))
    if i is None:
        return None
    vals = s.values.astype(float)
    before = vals[max(0, i - window - 1):i]
    before = before[np.isfinite(before) & (before > 0)]
    after = vals[max(0, i - window):i + window + 1]
    after = after[np.isfinite(after) & (after > 0)]
    if len(before) == 0 or len(after) == 0:
        return None
    return [float(a / b) for b in np.unique(before) for a in np.unique(after)]


def adjust_splits(px_long, stocks_long=None, lo=SPLIT_LO, hi=SPLIT_HI, tol=SPLIT_SNAP_TOL,
                  stocks_tol=SPLIT_STOCKS_TOL, window=SPLIT_STOCKS_WINDOW, vol_mult=EVENT_VOL_MULT):
    """px_long: DataFrame[CODE, D, OPEN, CLOSE (, VOLUME)] (분할 미조정).
    stocks_long: DataFrame[CODE, D, STOCKS] (daily_marcap_us 발행주식수). None 이면 분할을 확정할 수 없어
                 후보만 기록하고 조정하지 않는다(reason='stocks-missing').
    판정 순서:
      ① 시가·종가가 모두 전일종가 대비 [lo, hi] 밖 → 불연속 후보
      ② 배수 k = 전일종가/당일시가 가 정수비(n:1 또는 1:n, n≥2)에 ±tol 이내가 아니면 reason='not-integer-ratio'
      ③ STOCKS 가 D ±window 거래일 안에 같은 배수(±stocks_tol)로 변했으면 applied=True (reason='split')
         아니면: 거래량이 직전 20일 평균의 vol_mult 배 이상이면 reason='price-event' (인수·급락 등 진짜 이벤트),
                 그 외 reason='stocks-unconfirmed'. 어느 쪽도 조정하지 않는다.
    applied 인 이벤트만 그 이전 가격(OPEN/CLOSE)을 factor 로 나눈다.
    반환: (조정된 long DataFrame, events 리스트[{CODE, D, ratio, factor, applied, reason, stocks_ratio, vol_ratio}])"""
    px = px_long.drop_duplicates(["CODE", "D"], keep="last").sort_values(["CODE", "D"]).reset_index(drop=True)
    px["OPEN"] = pd.to_numeric(px["OPEN"], errors="coerce")
    px["CLOSE"] = pd.to_numeric(px["CLOSE"], errors="coerce")
    px.loc[px["OPEN"] <= 0, "OPEN"] = np.nan
    px.loc[px["CLOSE"] <= 0, "CLOSE"] = np.nan
    has_vol = "VOLUME" in px.columns
    if has_vol:
        px["VOLUME"] = pd.to_numeric(px["VOLUME"], errors="coerce")
    prev = px.groupby("CODE")["CLOSE"].shift(1)
    r_c = px["CLOSE"] / prev
    r_o = px["OPEN"] / prev
    flag = ((r_c < lo) & (r_o < lo)) | ((r_c > hi) & (r_o > hi))
    flagged = px.index[flag.fillna(False)]

    stocks_by_code, pos_of_day = {}, {}
    if stocks_long is not None and len(stocks_long) and len(flagged):
        st = stocks_long.drop_duplicates(["CODE", "D"], keep="last")
        st = st[st["CODE"].isin(set(px.loc[flagged, "CODE"]))].sort_values(["CODE", "D"])
        st["STOCKS"] = pd.to_numeric(st["STOCKS"], errors="coerce")
        for code, g in st.groupby("CODE"):
            ser = pd.Series(g["STOCKS"].values, index=g["D"].values)
            stocks_by_code[code] = ser
            for j, D in enumerate(ser.index):
                pos_of_day[(code, D)] = j
    vol_avg20 = None
    if has_vol and len(flagged):
        vol_avg20 = px.groupby("CODE")["VOLUME"].transform(lambda v: v.shift(1).rolling(20, min_periods=5).mean())

    events = []
    for idx in flagged:
        code, D = px.at[idx, "CODE"], px.at[idx, "D"]
        k = float(prev[idx] / px.at[idx, "OPEN"])
        if k >= 1:
            n = round(k)
            integer_ok = n >= 2 and abs(k - n) <= tol * n
            snap = float(n)
        else:
            n = round(1 / k)
            integer_ok = n >= 2 and abs(1 / k - n) <= tol * n
            snap = 1.0 / n
        vol_ratio = None
        if vol_avg20 is not None:
            v, a = px.at[idx, "VOLUME"], vol_avg20[idx]
            if v == v and a == a and a > 0:
                vol_ratio = round(float(v / a), 1)
        ev = {"CODE": code, "D": D, "ratio": round(k, 3), "factor": None, "applied": False,
              "reason": "not-integer-ratio", "stocks_ratio": None, "vol_ratio": vol_ratio}
        if integer_ok:
            ratios = _stocks_ratio_near(stocks_by_code, code, pos_of_day, D, window) if stocks_by_code else None
            if ratios is None:
                ev["reason"] = "stocks-missing"
            else:
                # 분할이면 STOCKS 는 가격과 반대로 움직인다: 가격 ÷k → 주식수 ×k (snap 배)
                best = min(ratios, key=lambda r: abs(r - snap))
                ev["stocks_ratio"] = round(best, 3)
                if abs(best - snap) <= stocks_tol * snap:
                    ev.update({"factor": snap, "applied": True, "reason": "split"})
                elif vol_ratio is not None and vol_ratio >= vol_mult and abs(best - 1.0) <= stocks_tol:
                    ev["reason"] = "price-event"
                else:
                    ev["reason"] = "stocks-unconfirmed"
        events.append(ev)
    for ev in events:
        if not ev["applied"]:
            continue
        m = (px["CODE"] == ev["CODE"]) & (px["D"] < ev["D"])
        px.loc[m, ["OPEN", "CLOSE"]] = px.loc[m, ["OPEN", "CLOSE"]] / ev["factor"]
    return px, events


def panel_from_long(px_long):
    """long → {'OPEN': wide, 'CLOSE': wide} (index=D 문자열 정렬, columns=CODE)."""
    px_long = px_long.drop_duplicates(["CODE", "D"], keep="last")
    o = px_long.pivot(index="D", columns="CODE", values="OPEN").sort_index()
    c = px_long.pivot(index="D", columns="CODE", values="CLOSE").sort_index()
    return {"OPEN": o, "CLOSE": c}


def load_prices(d0, d1, use_cache=True):
    """daily_price_us 전 종목 [d0, d1] 시가·종가 → 분할 조정 패널 (+events). 구간별 pickle 캐시."""
    path = os.path.join(CACHE_DIR, f"prices_{CACHE_VER}_{d0}_{d1}.pkl")
    if use_cache and os.path.exists(path):
        try:
            with open(path, "rb") as f:
                d = pickle.load(f)
            if d.get("ver") == CACHE_VER:
                return d["panel"], d["events"]
        except Exception as e:
            log.warning("가격 캐시 읽기 실패: %s", e)
    t0 = time.time()
    # STOCKS 검증 창(±5거래일)이 구간 경계에서도 잡히도록 주식수는 앞뒤 2주를 더 읽는다
    s0 = (pd.Timestamp(d0) - pd.Timedelta(days=14)).strftime("%Y-%m-%d")
    s1 = (pd.Timestamp(d1) + pd.Timedelta(days=14)).strftime("%Y-%m-%d")
    px = dq(f"""SELECT CODE, TO_CHAR(date_,'YYYY-MM-DD') AS D, OPEN, CLOSE, VOLUME FROM daily_price_us
                WHERE date_ BETWEEN TIMESTAMP '{d0} 00:00:00' AND TIMESTAMP '{d1} 00:00:00'""")
    stocks = dq(f"""SELECT CODE, TO_CHAR(date_,'YYYY-MM-DD') AS D, STOCKS FROM daily_marcap_us
                    WHERE date_ BETWEEN TIMESTAMP '{s0} 00:00:00' AND TIMESTAMP '{s1} 00:00:00'""")
    px, events = adjust_splits(px, stocks)
    panel = panel_from_long(px)
    n_app = sum(1 for e in events if e["applied"])
    n_by = {}
    for e in events:
        n_by[e["reason"]] = n_by.get(e["reason"], 0) + 1
    log.info("가격 패널 %s~%s: %d종목 × %d거래일 (%.1fs), 분할 소급조정 %d건 / 미조정 %s",
             d0, d1, panel["CLOSE"].shape[1], panel["CLOSE"].shape[0], time.time() - t0, n_app,
             {k: v for k, v in n_by.items() if k != "split"})
    if use_cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump({"ver": CACHE_VER, "panel": panel, "events": events}, f)
        os.replace(tmp, path)
    return panel, events


# ── 달력 ────────────────────────────────────────────────────────────────
def rebalance_dates(start, end, cadence="weekly"):
    """weekly: start 이후 모든 월요일. monthly: 매월 첫 월요일 (시작 달은 start 이후 첫 월요일)."""
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    first_mon = s + pd.Timedelta(days=(7 - s.weekday()) % 7)
    mons = [d.strftime("%Y-%m-%d") for d in pd.date_range(first_mon, e, freq="W-MON")]
    if cadence == "weekly":
        return mons
    if cadence == "monthly":
        out, seen = [], set()
        for d in mons:
            key = d[:7]
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
        return out
    raise ValueError(f"cadence 는 weekly|monthly: {cadence}")


def pick_asof(date, trading_days):
    """(date − 3일) 이하의 마지막 거래일. 없으면 None."""
    cut = (pd.Timestamp(date) - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    i = bisect.bisect_right(trading_days, cut)
    return trading_days[i - 1] if i > 0 else None


def next_trading_day(date, trading_days):
    """date 이후(포함) 첫 거래일. 없으면 None."""
    i = bisect.bisect_left(trading_days, date)
    return trading_days[i] if i < len(trading_days) else None


# ── 시뮬레이션용 시세/시계 ───────────────────────────────────────────────
class ReplayMarket:
    """PaperBroker 에 주입하는 quote_fn / session_fn / now_fn.
    set(date, 'OPEN'|'CLOSE') 로 '지금' 시점을 옮긴다. 세션은 항상 regularMarket.
    시각은 편의상 서머타임 KST: 시가 체결 = D 22:31, 종가 평가 = D 23:59 (거래일 D 가 ts[:10] 에 남도록)."""

    def __init__(self, panel):
        self.open = panel["OPEN"]
        self.close = panel["CLOSE"]
        self.date = None
        self.field = "CLOSE"
        self.now = datetime.now(KST)

    def set(self, date, field):
        self.date, self.field = date, field
        y, m, d = (int(x) for x in date.split("-"))
        hh, mm = (22, 31) if field == "OPEN" else (23, 59)
        self.now = datetime(y, m, d, hh, mm, tzinfo=KST)

    def quote(self, symbol):
        tbl = self.open if self.field == "OPEN" else self.close
        try:
            v = tbl.at[self.date, str(symbol).upper()]
        except KeyError:
            raise RuntimeError(f"시세 없음 {symbol} {self.date}")
        if v is None or not (v == v) or v <= 0:
            raise RuntimeError(f"시세 없음 {symbol} {self.date}")
        return float(v)

    def quotes(self, symbols):
        out = {}
        for s in symbols:
            try:
                out[str(s).upper()] = self.quote(s)
            except Exception:
                out[str(s).upper()] = None
        return out

    def session_fn(self):
        return "regularMarket"

    def now_fn(self):
        return self.now


# ── 시뮬레이션 본체 (DB 무관 — 테스트에서 가짜 screen_fn/panel 주입) ───────
def _place(broker, o):
    return broker.place_order(symbol=o["symbol"], side="BUY", order_type=o["order_type"],
                              quantity=o.get("quantity"), price=o.get("price"),
                              order_amount=o.get("order_amount"), time_in_force="DAY",
                              client_order_id=o.get("client_order_id"))


def _fill_row(res, ctx):
    f = res.get("fill") or {}
    return {"리밸런스일": ctx["rd"], "기준일": ctx["asof"], "체결일": ctx["fill_date"],
            "티커": res.get("symbol"), "종목명": ctx.get("name", ""), "매매": res.get("side"),
            "수량": f.get("qty"), "체결가": f.get("fillPrice"), "시가": f.get("quotePrice"),
            "체결금액": f.get("grossValue"), "수수료": f.get("commission"),
            "실현손익": f.get("realizedPnl"), "주문금액": res.get("orderAmount"),
            "주문ID": res.get("orderId"), "비고": ctx.get("note", "")}


def _mark_days(broker, mkt, trading_days, after, until):
    """after < D <= until 인 거래일마다 종가 평가 (tick)."""
    lo = bisect.bisect_right(trading_days, after) if after else 0
    hi = bisect.bisect_right(trading_days, until)
    for D in trading_days[lo:hi]:
        mkt.set(D, "CLOSE")
        broker.tick()


def benchmark_curve(panel, universe, start_date, end_date):
    """유니버스 동일가중 매수후보유(시가 진입, 비용 0). 상장폐지는 마지막 종가 유지.
    반환: (Series[D → 1.0 기준 배수], 종목별 최종배수 Series, 실제 쓰인 종목 수)"""
    codes = [c for c in dict.fromkeys(str(c).upper() for c in universe) if c in panel["OPEN"].columns]
    if not codes or start_date not in panel["OPEN"].index:
        return pd.Series(dtype=float), pd.Series(dtype=float), 0
    p0 = panel["OPEN"].loc[start_date, codes]
    p0 = p0[p0.notna() & (p0 > 0)]
    codes = list(p0.index)
    if not codes:
        return pd.Series(dtype=float), pd.Series(dtype=float), 0
    cl = panel["CLOSE"].loc[start_date:end_date, codes].ffill()
    rel = cl.div(p0, axis=1)
    curve = rel.mean(axis=1)
    return curve, rel.iloc[-1], len(codes)


def schedule_benchmark_curve(panel, universe, flows, initial_cash, days):
    """전략과 같은 현금 스케줄의 벤치마크 (비용 0, 현금 무이자).
    flows: [(체결일, 순투입 USD)] — 양수면 그 날 시가에 유니버스 동일가중으로 매수, 음수면 보유분을 비례 매도.
    전략과 같은 초기현금에서 출발하므로 '수익률%(총자산 기준)' 끼리 바로 비교할 수 있다(현금 드래그 동일).
    반환: (Series[D → 자산 USD], 총투입 USD, 종목 수)"""
    codes = [c for c in dict.fromkeys(str(c).upper() for c in universe) if c in panel["OPEN"].columns]
    days = [d for d in days if d in panel["CLOSE"].index]
    if not codes or not days:
        return pd.Series(dtype=float), 0.0, 0
    cl = panel["CLOSE"][codes].ffill()
    op = panel["OPEN"][codes]
    flow_by_day = {}
    for d, amt in flows:
        flow_by_day[d] = flow_by_day.get(d, 0.0) + float(amt)
    cash = float(initial_cash)
    units = pd.Series(0.0, index=codes)
    invested = 0.0
    out = {}
    for D in days:
        amt = flow_by_day.get(D, 0.0)
        if amt > 0:
            p0 = op.loc[D]
            p0 = p0[p0.notna() & (p0 > 0)]
            if len(p0):
                amt = min(amt, cash)
                units.loc[p0.index] += (amt / len(p0)) / p0
                cash -= amt
                invested += amt
        elif amt < 0:
            p0 = op.loc[D].where(lambda s: s > 0).fillna(cl.loc[D]).fillna(0.0)
            val = float((units * p0).sum())
            if val > 0:
                sell = min(-amt, val)
                units = units * (1 - sell / val)
                cash += sell
        out[D] = cash + float((units * cl.loc[D]).fillna(0.0).sum())
    return pd.Series(out), invested, len(codes)


def simulate(dates, trading_days, screen_fn, panel, cfg, state_path, end=None, progress=True):
    """dates: 리밸런스일 목록. trading_days: 정렬된 거래일('YYYY-MM-DD') 목록.
    screen_fn(asof) → {'watchlist': DataFrame(한글 열), 'universe': [CODE...]} (+ 'cached', 'elapsed' 선택)
    panel: {'OPEN': wide, 'CLOSE': wide}. cfg: auto_buy_config 전체 dict.
    반환 dict: broker, trades(list), rebal_rows, rebal_summary, equity(DataFrame), bench, cash_out_date, ..."""
    if strategy.is_v2(cfg):
        return simulate_v2(dates, trading_days, screen_fn, panel, cfg, state_path, end=end, progress=progress)
    trading_days = list(trading_days)
    end = end or trading_days[-1]
    if os.path.exists(state_path):
        os.remove(state_path)
    mkt = ReplayMarket(panel)
    paper_cfg = replay_paper_cfg(cfg)
    broker = PaperBroker(state_path, mkt.quote, mkt.session_fn, paper_cfg,
                         calendar_fn=None, now_fn=mkt.now_fn, fx_fn=None)
    exit_cfg = cfg.get("exit") or {}
    exit_on = bool(exit_cfg.get("enabled"))
    exit_rule = str(exit_cfg.get("rule") or "drop_from_list")
    weeks_absent = max(1, int(exit_cfg.get("weeks_absent") or 2))
    if exit_on and exit_rule != "drop_from_list":
        log.warning("exit.rule=%s 는 지원하지 않음 → drop_from_list 로 처리", exit_rule)

    trades, rebal_rows, rebal_summary = [], [], []
    absent = {}
    cash_out_date = None
    bench_universe, bench_start = None, None
    flows = []                    # (체결일, 전략 순투입 USD) — 같은 현금 스케줄 벤치마크용
    marked_through = None
    n = len(dates)
    n_screens = n_cached = 0

    for i, rd in enumerate(dates):
        asof = pick_asof(rd, trading_days)
        fill_date = next_trading_day(rd, trading_days)
        if asof is None:
            log.info("[%d/%d] %s: 기준일을 잡을 거래일이 없음 → 건너뜀", i + 1, n, rd)
            continue
        if fill_date is None or fill_date > end:
            log.info("[%d/%d] %s: 체결할 거래일이 데이터 끝(%s) 이후 → 종료", i + 1, n, rd, end)
            break
        t0 = time.time()
        scr = screen_fn(asof)
        n_screens += 1
        n_cached += 1 if scr.get("cached") else 0
        wl = scr.get("watchlist")
        wl = wl if wl is not None else empty_watchlist()
        if bench_universe is None:
            uni = scr.get("universe")
            if isinstance(uni, pd.DataFrame):          # screen_asof 는 축약 유니버스 DataFrame 을 준다
                uni = uni["CODE"].tolist() if "CODE" in uni.columns else []
            bench_universe = [str(c).upper() for c in (uni if uni is not None else [])]
            bench_start = fill_date
        # 직전 체결일 이후 ~ 이번 체결일 전날까지 종가 평가 (첫 리밸런스 전에는 평가 안 함)
        if marked_through is not None:
            _mark_days(broker, mkt, trading_days, marked_through, _prev_day(fill_date))

        cands, dropped = rules.select_candidates(wl, cfg, return_dropped=True)
        listed = set(str(t).upper() for t in wl["티커"].tolist()) if len(wl) and "티커" in wl.columns else set()
        name_of = {}
        if len(wl) and "티커" in wl.columns:
            for _, r in wl.iterrows():
                name_of[str(r["티커"]).upper()] = r.get("종목명", "")

        mkt.set(fill_date, "OPEN")
        outcome = {}
        n_sell, sell_usd = 0, 0.0
        # ⑥ 이탈 매도 (목록에서 weeks_absent 회 연속 빠진 보유 종목)
        if exit_on:
            for sym in list(broker.holdings()):
                absent[sym] = 0 if sym in listed else absent.get(sym, 0) + 1
            for sym, cnt in list(absent.items()):
                if cnt < weeks_absent or sym not in broker.holdings():
                    continue
                qty = float(broker.holdings()[sym]["qty"])
                res = broker.place_order(symbol=sym, side="SELL", order_type="MARKET", quantity=qty,
                                         client_order_id=f"rx-{asof}-{sym}"[:36])
                ctx = {"rd": rd, "asof": asof, "fill_date": fill_date, "name": name_of.get(sym, ""),
                       "note": f"목록 이탈 {cnt}회 연속 → 매도"}
                if str(res.get("status")) == "FILLED":
                    trades.append(_fill_row(res, ctx))
                    n_sell += 1
                    sell_usd += float(res["fill"]["grossValue"])
                    absent.pop(sym, None)
                    log.info("  매도 %-6s %.6f주 @%.4f (목록 이탈 %d회)", sym, res["fill"]["qty"],
                             res["fill"]["fillPrice"], cnt)
                else:
                    log.info("  매도 거절 %-6s %s: %s", sym, res.get("code"), res.get("reason"))
            for sym in list(absent):
                if sym not in broker.holdings():
                    absent.pop(sym, None)

        # ③ 후보 → 주문 크기 (auto_buy 와 같은 함수·같은 인자: 미체결도 넘긴다)
        holdings = broker.holdings()
        quotes = {s: v for s, v in mkt.quotes([c["symbol"] for c in cands]).items() if v}
        cash = float(broker.buying_power("USD"))
        plan = rules.size_orders(cands, cash, holdings, cfg, quotes, client_id_prefix=f"rp-{asof}",
                                 open_orders=broker.open_orders())
        for d in dropped:
            outcome[d["symbol"]] = f"제외: {d['reason']}"
        for s in plan.skipped:
            outcome[s["symbol"]] = f"건너뜀: {s['reason']}"
        if cash_out_date is None and any(str(s["reason"]).startswith("예산") for s in plan.skipped):
            cash_out_date = rd
        n_buy, buy_usd = 0, 0.0
        for o in plan:
            res = _place(broker, o)
            ctx = {"rd": rd, "asof": asof, "fill_date": fill_date, "name": o.get("name", ""),
                   "note": o.get("reason", "")}
            if str(res.get("status")) == "FILLED":
                f = res["fill"]
                trades.append(_fill_row(res, ctx))
                n_buy += 1
                buy_usd += float(f["grossValue"])
                outcome[o["symbol"]] = f"매수 ${f['grossValue']:,.2f} ({f['qty']}주 @{f['fillPrice']:.4f})"
            else:
                outcome[o["symbol"]] = f"거절: {res.get('code')} {res.get('reason')}"
                log.info("  거절 %-6s %s: %s", o["symbol"], res.get("code"), res.get("reason"))
        min_order = float((cfg.get("sizing") or {}).get("min_order_usd", 50))
        if cash_out_date is None and (
                any(str(v).startswith("거절: insufficient") for v in outcome.values())
                or float(broker.buying_power("USD")) < min_order):
            cash_out_date = rd
        if buy_usd or sell_usd:
            flows.append((fill_date, buy_usd - sell_usd))

        # ⑤ 체결일 종가 평가
        _mark_days(broker, mkt, trading_days, _prev_day(fill_date), fill_date)
        marked_through = fill_date
        snap = broker.state["equity_history"][-1] if broker.state["equity_history"] else {}
        eq = float(snap.get("equity_usd") or 0.0)

        # 리밸런스별 목록 (주도주 시트 전체 행 + 결과)
        if len(wl):
            for j, r in wl.iterrows():
                sym = str(r.get("티커", "")).upper()
                row = {"리밸런스일": rd, "기준일": asof, "체결일": fill_date, "순위": r.get("순위", j + 1),
                       "티커": sym, "종목명": r.get("종목명", ""), "섹터": r.get("섹터", ""),
                       "유형": r.get("유형", ""), "주의": r.get("주의", ""),
                       "주도주점수": r.get("주도주점수"), "상대강도(0~100)": r.get("상대강도(0~100)"),
                       "52주고점대비%": r.get("52주고점대비%"), "시총(십억$)": r.get("시총(십억$)"),
                       "결과": outcome.get(sym, "")}
                rebal_rows.append(row)
        else:
            rebal_rows.append({"리밸런스일": rd, "기준일": asof, "체결일": fill_date, "순위": None,
                               "티커": "", "종목명": "(주도주 게이트 통과 종목 없음)", "결과": ""})
        rebal_summary.append({"리밸런스일": rd, "기준일": asof, "체결일": fill_date,
                              "목록수": len(wl), "후보수": len(cands), "매수건수": n_buy, "매도건수": n_sell,
                              "매수금액": round(buy_usd, 2), "현금(체결후)": round(broker.state["cash"]["USD"], 2),
                              "보유종목수": len(broker.holdings()), "총자산(체결일종가)": round(eq, 2),
                              "스크리닝": "캐시" if scr.get("cached") else f"계산 {scr.get('elapsed', 0)}s"})
        if progress:
            log.info("[%d/%d] 리밸런스 %s 기준일 %s 체결일 %s | 목록 %d 후보 %d 매수 %d 매도 %d | "
                     "현금 %s 총자산 %s | %s (%.1fs)",
                     i + 1, n, rd, asof, fill_date, len(wl), len(cands), n_buy, n_sell,
                     f"${broker.state['cash']['USD']:,.0f}", f"${eq:,.0f}",
                     "캐시" if scr.get("cached") else f"스크리닝 {scr.get('elapsed', 0)}s", time.time() - t0)

    # 마지막 체결일 이후 ~ end 까지 종가 평가
    if marked_through is not None:
        _mark_days(broker, mkt, trading_days, marked_through, end)
        mkt.set(min(end, trading_days[bisect.bisect_right(trading_days, end) - 1]), "CLOSE")

    equity = pd.DataFrame(broker.state["equity_history"],
                          columns=["ts", "equity_usd", "cash_usd", "cash_krw", "positions_value"])
    if len(equity):
        equity["거래일"] = equity["ts"].str[:10]
        peak = equity["equity_usd"].cummax()
        equity["drawdown_pct"] = ((equity["equity_usd"] / peak - 1) * 100).round(3)
    bench = {"curve": pd.Series(dtype=float), "final": pd.Series(dtype=float), "n": 0,
             "start": bench_start, "end": end, "ew_return_pct": None, "median_return_pct": None,
             "schedule_curve": pd.Series(dtype=float), "schedule_return_pct": None,
             "schedule_invested": 0.0, "flows": flows}
    if bench_universe and bench_start:
        curve, final, nb = benchmark_curve(panel, bench_universe, bench_start, end)
        bench.update({"curve": curve, "final": final, "n": nb})
        if len(curve):
            bench["ew_return_pct"] = (float(curve.iloc[-1]) - 1) * 100
            bench["median_return_pct"] = (float(final.median()) - 1) * 100
        initial = float(paper_cfg.get("initial_cash_usd", 10000))
        days = equity["거래일"].tolist() if len(equity) else []
        sc, invested, _ = schedule_benchmark_curve(panel, bench_universe, flows, initial, days)
        bench.update({"schedule_curve": sc, "schedule_invested": invested})
        if len(sc) and initial:
            bench["schedule_return_pct"] = (float(sc.iloc[-1]) / initial - 1) * 100
    return {"broker": broker, "trades": trades, "rebal_rows": rebal_rows, "rebal_summary": rebal_summary,
            "equity": equity, "bench": bench, "cash_out_date": cash_out_date,
            "n_screens": n_screens, "n_cached": n_cached, "dates": dates, "end": end,
            "first_fill": bench_start}


def _prev_day(d):
    return (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")


# ── 리포트 ───────────────────────────────────────────────────────────────
def _flatten(d, prefix=""):
    rows = []
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            rows.extend(_flatten(v, key + "."))
        else:
            rows.append((key, json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v))
    return rows


def build_summary(res, cfg, cadence, split_events=None):
    broker = res["broker"]
    s = broker.summary()
    eq = res["equity"]
    initial = float(s["initial_equity_usd"])
    final = float(s["equity_usd"])
    avg_eq = float(eq["equity_usd"].mean()) if len(eq) else initial
    bought, sold = float(s["total_bought"]), float(s["total_sold"])
    turnover = ((bought + sold) / 2 / avg_eq * 100) if avg_eq else 0.0
    b = res["bench"]
    ew, med, sch = b.get("ew_return_pct"), b.get("median_return_pct"), b.get("schedule_return_pct")
    ret = (final / initial - 1) * 100 if initial else 0.0
    invested_pct = (float(s["positions_value"]) / final * 100) if final else 0.0
    dates = res["dates"]
    split_events = split_events or []
    applied = [e for e in split_events if e["applied"]]
    unapplied = [e for e in split_events if not e["applied"]]
    by_reason = {}
    for e in unapplied:
        by_reason.setdefault(e.get("reason", "not-integer-ratio"), []).append(e)
    reason_ko = {"not-integer-ratio": "정수비 아님", "stocks-unconfirmed": "STOCKS 미확인",
                 "stocks-missing": "STOCKS 데이터 없음", "price-event": "가격 이벤트(거래량 폭증·STOCKS 불변)"}
    n_unconfirmed = sum(len(v) for k, v in by_reason.items() if k != "not-integer-ratio")
    lag_days = int((cfg.get("replay") or {}).get("financial_lag_days", DEFAULT_FIN_LAG_DAYS))
    sz, src, ex = cfg.get("sizing", {}), cfg.get("source", {}), cfg.get("exit", {})
    rows = [
        ("기간(체결 시작~평가 종료)", f"{res.get('first_fill') or '-'} ~ {res['end']}"),
        ("리밸런스 구간", f"{dates[0]} ~ {dates[-1]}" if dates else "-"),
        ("주기", cadence),
        ("규칙", res.get("rule_label") or strategy.rule_label(cfg)),
        ("매도 사유별 건수(추적손절/게이트탈락/비중초과)", "{stop} / {gate} / {trim}".format(**res["counters"])
         if res.get("counters") else "-"),
        ("리밸런스 횟수", len(res["rebal_summary"])),
        ("스크리닝(기준일) 수 / 캐시 사용", f"{res['n_screens']} / {res['n_cached']}"),
        ("초기자산(USD)", round(initial, 2)),
        ("총매수금액(USD)", round(bought, 2)),
        ("총매도금액(USD)", round(sold, 2)),
        ("누적수수료(USD)", round(float(s["commissions"]), 2)),
        ("최종자산(USD)", round(final, 2)),
        ("최종현금(USD)", round(float(s["cash_usd"]), 2)),
        ("최종주식평가(USD)", round(float(s["positions_value"]), 2)),
        ("보유종목수(최종)", int(s["positions_count"])),
        ("투자비중%(최종)", round(invested_pct, 1)),
        ("수익률%(현금 포함 총자산 기준)", round(ret, 2)),
        ("순손익(USD) = 실현 + 평가", round(float(s["net_pnl"]), 2)),
        ("최대낙폭%", round(float(s["max_drawdown_pct"]), 2)),
        ("회전율%((총매수+총매도)÷2÷평균자산)", round(turnover, 2)),
        ("현금소진 시점(현금<min_order_usd 또는 예산 부족 건너뜀이 처음 생긴 리밸런스)", res["cash_out_date"] or "없음"),
        ("체결건수 / 거절건수", f"{int(s['fills'])} / {int(s['rejected'])}"),
        ("벤치마크 유니버스", f"{b.get('n', 0)}종목 (첫 기준일 스크리너 유니버스, 부동산 제외)"),
        ("벤치마크① 동일가중 매수후보유 수익률%(첫 체결일 전액 투입)", round(ew, 2) if ew is not None else "-"),
        ("벤치마크① 유니버스 중앙값 수익률%", round(med, 2) if med is not None else "-"),
        ("벤치마크② 같은 현금 스케줄 동일가중 수익률%(리밸런스마다 전략과 같은 금액 투입, 총자산 기준)",
         round(sch, 2) if sch is not None else "-"),
        ("벤치마크② 총투입(USD)", round(float(b.get("schedule_invested") or 0.0), 2)),
        ("초과수익%p(전략 − 벤치마크①, 현금 드래그 포함)", round(ret - ew, 2) if ew is not None else "-"),
        ("초과수익%p(전략 − 벤치마크②, 같은 현금 스케줄 → 종목 선택 효과)", round(ret - sch, 2) if sch is not None else "-"),
        ("설정: 후보", f"{src.get('sheet')} 상위 {src.get('top_n')} ({src.get('sort_by')} 내림차순), "
                     f"주의 제외={src.get('exclude_if_주의')}"),
        ("설정: 크기", f"종목당 ${sz.get('per_stock_usd')}, 리밸런스당 한도 ${sz.get('weekly_cap_usd')}, "
                     f"최대 {sz.get('max_positions')}종목, 보유 skip={sz.get('skip_if_held')}, "
                     f"{sz.get('order_type')} 금액주문={sz.get('use_amount_orders')}"),
        ("설정: 이탈", f"enabled={ex.get('enabled')} rule={ex.get('rule')} weeks_absent={ex.get('weeks_absent')}"),
        ("설정: 비용", f"슬리피지 {cfg.get('paper', {}).get('slippage_bps')}bp, "
                     f"수수료 {cfg.get('paper', {}).get('commission_pct')}%"),
        ("분할 소급조정(STOCKS 로 확인된 것만)", f"{len(applied)}건: " + ", ".join(
            f"{e['CODE']} {e['D']} ×{e['factor']:g} (STOCKS ×{e.get('stocks_ratio')})" for e in applied[:40])
         if applied else "0건"),
        ("분할 후보 중 STOCKS 미확인(미조정)", f"{n_unconfirmed}건" + (": " + "; ".join(
            f"[{reason_ko.get(k, k)}] " + ", ".join(
                f"{e['CODE']} {e['D']} ratio {e['ratio']}"
                + (f" 거래량×{e['vol_ratio']}" if e.get("vol_ratio") is not None else "")
                for e in v[:20])
            for k, v in by_reason.items() if k != "not-integer-ratio") if n_unconfirmed else "")),
        ("불연속 미조정(정수비 아님)", f"{len(by_reason.get('not-integer-ratio', []))}건: " + ", ".join(
            f"{e['CODE']} {e['D']} ratio {e['ratio']}" for e in by_reason.get("not-integer-ratio", [])[:40])
         if by_reason.get("not-integer-ratio") else "0건"),
        ("한계1", f"분기 재무는 분기말(END_DATE) + {lag_days}일 지연 적용(replay.financial_lag_days, 10-Q 제출 지연 흉내). "
                 "DB 에 공시일 컬럼이 없어 근사치. 섹터·이름은 현재 매핑"),
        ("한계2", "상장폐지 종목은 유니버스에 없음(생존편향). 벤치마크는 비용 0, 전략은 슬리피지·수수료 반영"),
        ("한계3", "시가 체결 가정(실제 정규장 시작 1분 후 시장가와 다를 수 있음). 시세 없는 날은 마지막 가격 유지"),
        ("한계4", "weekly_cap_usd 는 리밸런스 1회당 한도. 현금은 무이자. 배당 미반영"),
    ]
    return pd.DataFrame(rows, columns=["항목", "값"])


def write_report(res, cfg, out, cadence, split_events=None):
    summary_df = build_summary(res, cfg, cadence, split_events)
    eq = res["equity"].copy()
    b = res["bench"]
    initial = float(res["broker"].summary()["initial_equity_usd"])
    if len(eq):
        eq_df = pd.DataFrame({
            "거래일": eq["거래일"], "총자산": eq["equity_usd"].round(2), "현금USD": eq["cash_usd"].round(2),
            "주식평가액": eq["positions_value"].round(2), "고점대비낙폭%": eq["drawdown_pct"],
        })
        curve = b.get("curve")
        if curve is not None and len(curve):
            eq_df["벤치마크(동일가중B&H)자산"] = eq_df["거래일"].map(curve).astype(float).mul(initial).round(2)
        sc = b.get("schedule_curve")
        if sc is not None and len(sc):
            eq_df["벤치마크(같은현금스케줄)자산"] = eq_df["거래일"].map(sc).astype(float).round(2)
        rebal_days = {r["체결일"] for r in res["rebal_summary"]}
        eq_df["리밸런스"] = eq_df["거래일"].map(lambda d: "●" if d in rebal_days else "")
    else:
        eq_df = pd.DataFrame(columns=["거래일", "총자산", "현금USD", "주식평가액", "고점대비낙폭%"])
    trade_cols = ["리밸런스일", "기준일", "체결일", "티커", "종목명", "매매", "수량", "체결가", "시가", "체결금액",
                  "수수료", "실현손익", "주문금액", "주문ID", "비고"]
    trades_df = pd.DataFrame(res["trades"], columns=trade_cols)
    rebal_cols = ["리밸런스일", "기준일", "체결일", "순위", "티커", "종목명", "섹터", "유형", "주의", "주도주점수",
                  "상대강도(0~100)", "52주고점대비%", "시총(십억$)", "결과"]
    rebal_df = pd.DataFrame(res["rebal_rows"], columns=rebal_cols)
    rs_cols = ["리밸런스일", "기준일", "체결일", "목록수", "후보수", "매수건수", "매도건수", "매수금액",
               "현금(체결후)", "보유종목수", "총자산(체결일종가)", "스크리닝"]
    rs_df = pd.DataFrame(res["rebal_summary"], columns=rs_cols)
    cfg_df = pd.DataFrame(_flatten(cfg), columns=["설정키", "값"])
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        summary_df.to_excel(xw, sheet_name="요약", index=False)
        eq_df.to_excel(xw, sheet_name="자산추이", index=False)
        trades_df.to_excel(xw, sheet_name="거래내역", index=False)
        rebal_df.to_excel(xw, sheet_name="리밸런스별목록", index=False)
        rs_df.to_excel(xw, sheet_name="리밸런스요약", index=False)
        cfg_df.to_excel(xw, sheet_name="설정", index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(70, max(10, width + 2))
    return summary_df


# ── 진입점 ───────────────────────────────────────────────────────────────
# ── v2 규칙 시뮬레이션 (strategy.py — auto_buy 와 같은 함수) ─────────────────
# 리밸런스일(월): 기준일 = 직전 금요일, 체결일 = 그 주 첫 거래일 시가.
#   ① 게이트 탈락 주수 갱신 → 매도 대기 전량 시가 매도
#   ② (mode C) 비중초과 매도  ③ 신규(점수 비중) → 추가매수(B/C) 시가 매수
# 매일: 종가로 최고가 갱신·추적손절 판정 → 걸리면 다음 거래일 시가에 매도
def replay_paper_cfg(cfg):
    """paper 설정 사본. convert_at_start 면 KRW 를 paper.fx_rate×(1+장중 스프레드) 로 USD 전환해 시작."""
    p = dict(cfg.get("paper") or {})
    krw = float(p.get("initial_cash_krw") or 0.0)
    if p.get("convert_at_start") and krw > 0:
        rate = float(p.get("fx_rate") or 1357.2)
        spread = float(p.get("fx_spread_pct_market", 0.05))
        p["initial_cash_usd"] = float(p.get("initial_cash_usd") or 0.0) + strategy.floor2(
            krw / (rate * (1 + spread / 100.0)))
        p["initial_cash_krw"] = 0.0
    return p


def simulate_v2(dates, trading_days, screen_fn, panel, cfg, state_path, end=None, progress=True):
    trading_days = list(trading_days)
    end = end or trading_days[-1]
    if os.path.exists(state_path):
        os.remove(state_path)
    mkt = ReplayMarket(panel)
    paper_cfg = replay_paper_cfg(cfg)
    broker = PaperBroker(state_path, mkt.quote, mkt.session_fn, paper_cfg,
                         calendar_fn=None, now_fn=mkt.now_fn, fx_fn=None)
    ex = strategy.exit_cfg(cfg)
    exit_on, pct, weeks = ex["enabled"], ex["trailing_stop_pct"], ex["gate_absent_weeks"]
    es = {}
    names = {}
    counters = {"stop": 0, "gate": 0, "trim": 0}
    trades, rebal_rows, rebal_summary, flows = [], [], [], []
    cash_out_date = None
    bench_universe, bench_start = None, None
    marked_through = None
    n = len(dates)
    n_screens = n_cached = 0

    def quotes_at(D, field, syms):
        mkt.set(D, field)
        return {k: v for k, v in mkt.quotes(list(syms)).items() if v}

    def sell_pending(D, rd="", asof=""):
        """D 시가에 매도 대기 전량 매도 → {sym: (사유, 체결금액)}."""
        mkt.set(D, "OPEN")
        h = broker.holdings()
        out = {}
        for sym in strategy.pending_exits(es, h):
            reason = es[sym]["pending_exit"]["reason"]
            res = broker.place_order(symbol=sym, side="SELL", order_type="MARKET",
                                     quantity=strategy.floor6(float(h[sym]["qty"])),
                                     client_order_id=strategy.client_id(f"rx-{D}", sym))
            if str(res.get("status")) == "FILLED":
                trades.append(_fill_row(res, {"rd": rd or D, "asof": asof, "fill_date": D,
                                              "name": names.get(sym, ""), "note": reason}))
                counters["gate" if reason.startswith(strategy.REASON_GATE) else "stop"] += 1
                out[sym] = (reason, float(res["fill"]["grossValue"]))
                es.pop(sym, None)
            else:
                log.info("  매도 거절 %-6s %s: %s", sym, res.get("code"), res.get("reason"))
        return out

    def run_days(after, until):
        """after < D <= until: (대기 매도가 있으면 D 시가 매도) → D 종가 평가 → 최고가·추적손절 판정."""
        lo = bisect.bisect_right(trading_days, after) if after else 0
        hi = bisect.bisect_right(trading_days, until)
        for D in trading_days[lo:hi]:
            if exit_on and any(st.get("pending_exit") for st in es.values()):
                sold = sell_pending(D)
                cash_in = sum(v[1] for v in sold.values())
                if cash_in:
                    flows.append((D, -cash_in))
            mkt.set(D, "CLOSE")
            broker.tick()
            if exit_on:
                h = broker.holdings()
                strategy.sync_exit_state(es, h)
                q = quotes_at(D, "CLOSE", h)
                strategy.mark_high_water(es, q)
                strategy.check_trailing(es, q, pct, D, ex["stop_type"])

    for i, rd in enumerate(dates):
        asof = pick_asof(rd, trading_days)
        fill_date = next_trading_day(rd, trading_days)
        if asof is None:
            log.info("[%d/%d] %s: 기준일을 잡을 거래일이 없음 → 건너뜀", i + 1, n, rd)
            continue
        if fill_date is None or fill_date > end:
            log.info("[%d/%d] %s: 체결할 거래일이 데이터 끝(%s) 이후 → 종료", i + 1, n, rd, end)
            break
        t0 = time.time()
        scr = screen_fn(asof)
        n_screens += 1
        n_cached += 1 if scr.get("cached") else 0
        wl = scr.get("watchlist")
        wl = wl if wl is not None else empty_watchlist()
        if bench_universe is None:
            uni = scr.get("universe")
            if isinstance(uni, pd.DataFrame):
                uni = uni["CODE"].tolist() if "CODE" in uni.columns else []
            bench_universe = [str(c).upper() for c in (uni if uni is not None else [])]
            bench_start = fill_date
        if marked_through is not None:
            run_days(marked_through, _prev_day(fill_date))

        cands, dropped = rules.select_candidates(wl, cfg, return_dropped=True)
        listed = set()
        if len(wl) and "티커" in wl.columns:
            for _, r in wl.iterrows():
                sym = str(r["티커"]).upper()
                listed.add(sym)
                names[sym] = r.get("종목명", "")
        outcome = {}
        n_sell, sell_usd = 0, 0.0

        # ① 게이트 탈락 주수 → 매도 대기 전량 시가 매도
        sold = {}
        if exit_on:
            strategy.sync_exit_state(es, broker.holdings())
            strategy.update_gate_absence(es, listed, weeks, rd)
            sold = sell_pending(fill_date, rd, asof)
            for sym, (reason, gross) in sold.items():
                outcome[sym] = f"매도 ${gross:,.2f}: {reason}"
                n_sell += 1
                sell_usd += gross

        # ② 비중초과 매도 (mode C)
        h = broker.holdings()
        q = quotes_at(fill_date, "OPEN", list(h) + [c["symbol"] for c in cands])
        for o in strategy.plan_trims(cands, h, q, float(broker.buying_power("USD")), cfg, asof, exclude=set(sold)):
            res = broker.place_order(symbol=o["symbol"], side="SELL", order_type="MARKET",
                                     quantity=o["quantity"], client_order_id=o["client_order_id"])
            if str(res.get("status")) == "FILLED":
                trades.append(_fill_row(res, {"rd": rd, "asof": asof, "fill_date": fill_date,
                                              "name": o.get("name", ""), "note": o["reason"]}))
                counters["trim"] += 1
                n_sell += 1
                sell_usd += float(res["fill"]["grossValue"])
                outcome[o["symbol"]] = f"비중매도 ${res['fill']['grossValue']:,.2f}"

        # ③ 신규 → 추가매수
        plan = strategy.plan_buys(cands, broker.holdings(), q, float(broker.buying_power("USD")), cfg, asof,
                                  open_orders=broker.open_orders(), exclude=set(sold))
        for d in dropped:
            outcome.setdefault(d["symbol"], f"제외: {d['reason']}")
        for s_ in plan.skipped:
            outcome.setdefault(s_["symbol"], f"건너뜀: {s_['reason']}")
        if cash_out_date is None and i > 0 and any(o.get("kind") == "new" for o in plan) and plan.scale < 0.5:
            cash_out_date = rd        # 신규 종목이 목표의 절반도 못 받은 첫 리밸런스
        n_buy, buy_usd = 0, 0.0
        for o in plan:
            res = _place(broker, o)
            if str(res.get("status")) == "FILLED":
                f = res["fill"]
                trades.append(_fill_row(res, {"rd": rd, "asof": asof, "fill_date": fill_date,
                                              "name": o.get("name", ""), "note": o.get("reason", "")}))
                n_buy += 1
                buy_usd += float(f["grossValue"])
                kind = "신규" if o.get("kind") == "new" else "추가"
                outcome[o["symbol"]] = f"{kind}매수 ${f['grossValue']:,.2f} (목표비중 {o['target_weight'] * 100:.2f}%)"
            else:
                outcome[o["symbol"]] = f"거절: {res.get('code')} {res.get('reason')}"
        if buy_usd or sell_usd:
            flows.append((fill_date, buy_usd - sell_usd))
        if exit_on:
            strategy.sync_exit_state(es, broker.holdings())

        run_days(_prev_day(fill_date), fill_date)            # 체결일 종가 평가 + 추적손절 판정
        marked_through = fill_date
        snap = broker.state["equity_history"][-1] if broker.state["equity_history"] else {}
        eq = float(snap.get("equity_usd") or 0.0)

        w = plan.weights
        for j, r in (wl.iterrows() if len(wl) else []):
            sym = str(r.get("티커", "")).upper()
            rebal_rows.append({"리밸런스일": rd, "기준일": asof, "체결일": fill_date, "순위": r.get("순위", j + 1),
                               "티커": sym, "종목명": r.get("종목명", ""), "섹터": r.get("섹터", ""),
                               "유형": r.get("유형", ""), "주의": r.get("주의", ""),
                               "주도주점수": r.get("주도주점수"), "상대강도(0~100)": r.get("상대강도(0~100)"),
                               "52주고점대비%": r.get("52주고점대비%"), "시총(십억$)": r.get("시총(십억$)"),
                               "목표비중%": round(w[sym] * 100, 2) if sym in w else None,
                               "결과": outcome.get(sym, "")})
        for sym, txt in outcome.items():                     # 목록 밖 보유 종목 매도 결과도 남긴다
            if sym not in listed:
                rebal_rows.append({"리밸런스일": rd, "기준일": asof, "체결일": fill_date, "티커": sym,
                                   "종목명": names.get(sym, ""), "결과": txt})
        rebal_summary.append({"리밸런스일": rd, "기준일": asof, "체결일": fill_date,
                              "목록수": len(wl), "후보수": len(cands), "매수건수": n_buy, "매도건수": n_sell,
                              "매수금액": round(buy_usd, 2), "현금(체결후)": round(broker.state["cash"]["USD"], 2),
                              "보유종목수": len(broker.holdings()), "총자산(체결일종가)": round(eq, 2),
                              "스크리닝": "캐시" if scr.get("cached") else f"계산 {scr.get('elapsed', 0)}s"})
        if progress:
            log.info("[%d/%d] %s 기준일 %s 체결 %s | 목록 %d 매수 %d 매도 %d | 보유 %d 현금 $%s 총자산 $%s | %s (%.1fs)",
                     i + 1, n, rd, asof, fill_date, len(wl), n_buy, n_sell, len(broker.holdings()),
                     f"{broker.state['cash']['USD']:,.0f}", f"{eq:,.0f}",
                     "캐시" if scr.get("cached") else f"스크리닝 {scr.get('elapsed', 0)}s", time.time() - t0)

    if marked_through is not None:
        run_days(marked_through, end)

    equity = pd.DataFrame(broker.state["equity_history"],
                          columns=["ts", "equity_usd", "cash_usd", "cash_krw", "positions_value"])
    if len(equity):
        equity["거래일"] = equity["ts"].str[:10]
        peak = equity["equity_usd"].cummax()
        equity["drawdown_pct"] = ((equity["equity_usd"] / peak - 1) * 100).round(3)
    bench = {"curve": pd.Series(dtype=float), "final": pd.Series(dtype=float), "n": 0,
             "start": bench_start, "end": end, "ew_return_pct": None, "median_return_pct": None,
             "schedule_curve": pd.Series(dtype=float), "schedule_return_pct": None,
             "schedule_invested": 0.0, "flows": flows}
    if bench_universe and bench_start:
        curve, final, nb = benchmark_curve(panel, bench_universe, bench_start, end)
        bench.update({"curve": curve, "final": final, "n": nb})
        if len(curve):
            bench["ew_return_pct"] = (float(curve.iloc[-1]) - 1) * 100
            bench["median_return_pct"] = (float(final.median()) - 1) * 100
        initial = float(paper_cfg.get("initial_cash_usd", 10000))
        days = equity["거래일"].tolist() if len(equity) else []
        sc, invested, _ = schedule_benchmark_curve(panel, bench_universe, flows, initial, days)
        bench.update({"schedule_curve": sc, "schedule_invested": invested})
        if len(sc) and initial:
            bench["schedule_return_pct"] = (float(sc.iloc[-1]) / initial - 1) * 100
    return {"broker": broker, "trades": trades, "rebal_rows": rebal_rows, "rebal_summary": rebal_summary,
            "equity": equity, "bench": bench, "cash_out_date": cash_out_date,
            "n_screens": n_screens, "n_cached": n_cached, "dates": dates, "end": end,
            "first_fill": bench_start, "counters": counters, "rule_label": strategy.rule_label(cfg)}


# ── 규칙 비교 (같은 스크리닝·같은 가격으로 4개 설정) ─────────────────────────
_V1 = {"sizing": {"method": "equal", "per_stock_usd": 1000, "weekly_cap_usd": 3000, "max_positions": 10,
                  "skip_if_held": True, "min_order_usd": 50},
       "source": {"top_n": 5, "exclude_if_주의": True}, "exit": {"enabled": False}}


def _R(every, top_n=50, stop=0, weighting="score", stop_type="trailing"):
    """정기 리밸런스: every 주마다 목록·비중 재계산, 목록 밖은 매도(탈락 1회), 비중 ±30% 벗어나면 양방향 조정."""
    return {"sizing": {"method": "score_weight", "weighting": weighting}, "source": {"top_n": top_n},
            "rebalance": {"mode": "C", "every_weeks": every},
            "exit": {"enabled": True, "gate_absent_weeks": 1, "trailing_stop_pct": stop, "stop_type": stop_type}}


# 세트 이름 → ([(키, 설명, 설정 덮어쓰기)], 요약 시트에 쓸 대표 키)
VARIANT_SETS = {
    "rules": ([("v1_equal", "v1 균등(상위5·종목당$1000·매도없음)", _V1),
               ("v2_A", "v2-A 보유유지", {"sizing": {"method": "score_weight"}, "rebalance": {"mode": "A"}}),
               ("v2_B", "v2-B 추가매수만", {"sizing": {"method": "score_weight"}, "rebalance": {"mode": "B"}}),
               ("v2_C", "v2-C 양방향", {"sizing": {"method": "score_weight"}, "rebalance": {"mode": "C"}})],
              "v2_B"),
    "rebal": ([("v2_B_weekly", "기존 v2-B (매주 신규만·게이트2주·손절15%)",
                {"sizing": {"method": "score_weight"}, "rebalance": {"mode": "B", "every_weeks": 1},
                 "exit": {"enabled": True, "gate_absent_weeks": 2, "trailing_stop_pct": 15}}),
               ("R1", "정기 리밸런스 매주 · 50종 · 손절없음", _R(1)),
               ("R2", "정기 리밸런스 2주 · 50종 · 손절없음", _R(2)),
               ("R4", "정기 리밸런스 4주 · 50종 · 손절없음", _R(4)),
               ("R4_stop15", "정기 리밸런스 4주 · 50종 · 손절 −15%", _R(4, stop=15)),
               ("R4_top20", "정기 리밸런스 4주 · 상위 20종 · 손절없음", _R(4, top_n=20))],
              "R4"),
    # 2020~ 규칙 정하기용: 주기 1/2/3/4주 × 상위 20/50, 손절 없음, 점수가중 (+ 3주·50 균등가중 하나)
    "grid": ([("R1_50", "매주 · 50종 · 점수가중", _R(1)),
              ("R2_50", "2주 · 50종 · 점수가중", _R(2)),
              ("R3_50", "3주 · 50종 · 점수가중", _R(3)),
              ("R4_50", "4주 · 50종 · 점수가중", _R(4)),
              ("R1_20", "매주 · 상위20 · 점수가중", _R(1, top_n=20)),
              ("R2_20", "2주 · 상위20 · 점수가중", _R(2, top_n=20)),
              ("R3_20", "3주 · 상위20 · 점수가중", _R(3, top_n=20)),
              ("R4_20", "4주 · 상위20 · 점수가중", _R(4, top_n=20)),
              ("R3_50_eq", "3주 · 50종 · 균등가중", _R(3, weighting="equal"))],
             "R3_50"),
    # 손절 비율 비교: 3주 · 50종 · 점수가중 고정, 손절만 바꾼다
    "stops": ([("R3_nostop", "3주·50종 · 손절 없음", _R(3))]
              + [(f"T{p}", f"3주·50종 · 추적손절 −{p}% (최고가 대비)", _R(3, stop=p)) for p in (10, 15, 20, 25, 30)]
              + [(f"F{p}", f"3주·50종 · 손절 −{p}% (매입가 대비)", _R(3, stop=p, stop_type="fixed")) for p in (10, 20, 30)],
              "R3_nostop"),
}


def _yearly_returns(series, initial):
    """거래일(str)→자산 Series → {연도: 수익률%}. 첫해는 초기자본 대비, 이후는 전년 말 대비."""
    out, prev = {}, float(initial)
    s = series.dropna()
    for y in sorted({str(d)[:4] for d in s.index}):
        part = s[[str(d)[:4] == y for d in s.index]]
        if not len(part) or not prev:
            continue
        out[y] = (float(part.iloc[-1]) / prev - 1) * 100
        prev = float(part.iloc[-1])
    return out


def yearly_table(results, variants, main_key):
    """설정별 연도 수익률·벤치마크 초과·이긴 해·지배연도(초과가 가장 큰 해)를 빼고 본 평균 초과."""
    b = results[main_key]["bench"]
    initial = float(results[main_key]["broker"].summary()["initial_equity_usd"])
    curve = b.get("curve")
    bench_y = _yearly_returns(curve * initial, initial) if curve is not None and len(curve) else {}
    years = sorted(bench_y)
    rows = [dict({"설정": "벤치마크", "설명": "첫 기준일 유니버스 동일가중 매수후보유"},
                 **{y: round(bench_y[y], 1) for y in years})]
    stats = {}
    for key, label in variants:
        e = results[key]["equity"]
        ry = _yearly_returns(pd.Series(e["equity_usd"].values, index=e["거래일"].values), initial) if len(e) else {}
        ex = {y: ry[y] - bench_y[y] for y in years if y in ry}
        wins = sum(1 for v in ex.values() if v > 0)
        dom = max(ex, key=lambda y: ex[y]) if ex else None
        rest = [v for y, v in ex.items() if y != dom]
        st = {"이긴해": f"{wins}/{len(ex)}",
              "연도중앙 초과%p": round(float(pd.Series(list(ex.values())).median()), 1) if ex else None,
              "지배연도": dom,
              "지배연도제외 평균초과%p": round(sum(rest) / len(rest), 1) if rest else None}
        stats[key] = st
        rows.append(dict({"설정": key, "설명": label}, **{y: round(ry[y], 1) for y in years if y in ry}))
        rows.append(dict({"설정": key, "설명": "  └ 벤치마크 대비 %p"}, **{y: round(ex[y], 1) for y in ex}, **st))
    return pd.DataFrame(rows), stats
VARIANTS = [(k, lbl) for k, lbl, _ in VARIANT_SETS["rules"][0]]


def _merge(base, over):
    import copy
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out.get(k) or {}, v) if isinstance(v, dict) else copy.deepcopy(v)
    return out


def variant_configs(cfg, variant_set="rules"):
    return {key: _merge(cfg, over) for key, _lbl, over in VARIANT_SETS[variant_set][0]}


def compare_row(key, label, res):
    s = res["broker"].summary()
    eq = res["equity"]
    initial, final = float(s["initial_equity_usd"]), float(s["equity_usd"])
    rets = eq["equity_usd"].pct_change().dropna() if len(eq) else pd.Series(dtype=float)
    vol = float(rets.std() * (252 ** 0.5) * 100) if len(rets) > 1 else 0.0
    tr = pd.DataFrame(res["trades"], columns=["매매", "비고"]) if res["trades"] else pd.DataFrame(columns=["매매", "비고"])
    sells = tr[tr["매매"] == "SELL"]["비고"].astype(str)
    avg_eq = float(eq["equity_usd"].mean()) if len(eq) else initial
    worst = "-"
    if len(eq):
        k = int(eq["drawdown_pct"].idxmin())
        peak_i = int(eq["equity_usd"].iloc[:k + 1].idxmax())
        worst = f"{eq['거래일'].iloc[peak_i]} → {eq['거래일'].iloc[k]} ({eq['drawdown_pct'].iloc[k]:.1f}%)"
    rs = res["rebal_summary"]
    return {
        "설정": key, "설명": label, "초기자산($)": round(initial, 2), "최종자산($)": round(final, 2),
        "수익률%": round((final / initial - 1) * 100, 2) if initial else 0.0,
        "최대낙폭%": round(float(s["max_drawdown_pct"]), 2), "최대낙폭 구간": worst,
        "연환산변동성%": round(vol, 2),
        "매수건수": int((tr["매매"] == "BUY").sum()), "매도건수": int(len(sells)),
        "손절 매도": int((sells.str.startswith(strategy.REASON_TRAIL) | sells.str.startswith(strategy.REASON_FIXED)).sum()),
        "게이트탈락 매도": int((sells.str.startswith(strategy.REASON_GATE) | sells.str.startswith("목록 이탈")).sum()),
        "비중초과 매도": int(sells.str.startswith(strategy.REASON_TRIM).sum()),
        "평균보유종목수": round(sum(r["보유종목수"] for r in rs) / len(rs), 1) if rs else 0,
        "평균현금비중%": round(float((eq["cash_usd"] / eq["equity_usd"]).mean() * 100), 1) if len(eq) else 0,
        "회전율%": round((float(s["total_bought"]) + float(s["total_sold"])) / 2 / avg_eq * 100, 1) if avg_eq else 0,
        "누적수수료($)": round(float(s["commissions"]), 2),
        "최종보유종목수": int(s["positions_count"]),
    }


def compare(start=DEFAULT_START, end=None, cadence="weekly", cfg=None, out=OUT_XLSX,
            refresh=False, use_cache=True, variant_set="rules"):
    """v1 균등 / v2-A / v2-B / v2-C 를 같은 스크리닝(캐시)·같은 가격으로 돌려 비교표를 만든다.
    첫 설정에서 스크리닝을 계산(느림)하고, 나머지는 캐시를 쓴다."""
    cfg = cfg or _load_cfg()
    cadence = (cadence or "weekly").lower()
    start = start or DEFAULT_START
    latest = db_latest_date()
    end = min(end, latest) if end else latest
    dates = rebalance_dates(start, end, cadence)
    if not dates:
        raise ValueError(f"{start}~{end} 구간에 리밸런스일(월요일)이 없음")
    d0 = (pd.Timestamp(start) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    lag_days = int((cfg.get("replay") or {}).get("financial_lag_days", DEFAULT_FIN_LAG_DAYS))
    log.info("compare %s: %s ~ %s 리밸런스 %d회 × 설정 %d개 | 재무 지연 %d일", cadence, start, end,
             len(dates), len(VARIANTS), lag_days)
    panel, split_events = load_prices(d0, end, use_cache=use_cache)
    trading_days = [d for d in panel["CLOSE"].index if d <= end]

    def screen_fn(asof):
        return screen_asof(asof, refresh=refresh, use_cache=use_cache, lag_days=lag_days)

    variants, main_key = VARIANT_SETS[variant_set]
    cfgs = variant_configs(cfg, variant_set)
    results, rows = {}, []
    for k, (key, label, _over) in enumerate(variants):
        t0 = time.time()
        sp = os.path.join(BASE_DIR, "paper", f"replay_state_{key}.json")
        ew = strategy.every_weeks(cfgs[key]) if cadence == "weekly" else 1
        res = simulate(dates[::ew], trading_days, screen_fn, panel, cfgs[key], sp, end=end, progress=(k == 0))
        results[key] = res
        rows.append(compare_row(key, label, res))
        log.info("  %s 완료 %.1fs: 수익률 %s%% 최대낙폭 %s%%", key, time.time() - t0,
                 rows[-1]["수익률%"], rows[-1]["최대낙폭%"])
    b = results[main_key]["bench"]
    cmp_df = pd.DataFrame(rows)
    cmp_df["벤치마크 동일가중B&H%"] = round(b["ew_return_pct"], 2) if b.get("ew_return_pct") is not None else None
    cmp_df["벤치마크 중앙값%"] = round(b["median_return_pct"], 2) if b.get("median_return_pct") is not None else None
    ydf, ystats = yearly_table(results, [(k, l) for k, l, _ in variants], main_key)
    for col in ("이긴해", "연도중앙 초과%p", "지배연도", "지배연도제외 평균초과%p"):
        cmp_df[col] = cmp_df["설정"].map(lambda k: ystats.get(k, {}).get(col))
    first = results[main_key].get("first_fill")
    if first:
        yrs = (pd.Timestamp(end) - pd.Timestamp(first)).days / 365.25
        cmp_df["연평균수익률%"] = ((cmp_df["최종자산($)"] / cmp_df["초기자산($)"]) ** (1 / yrs) - 1).mul(100).round(2)
    cmp_df.attrs["yearly"] = ydf
    write_compare_report(cmp_df, results, cfgs, out, cadence, split_events,
                         variants=[(k, l) for k, l, _ in variants], main_key=main_key)
    log.info("compare 완료 → %s", out)
    return cmp_df


def write_compare_report(cmp_df, results, cfgs, out, cadence, split_events=None, variants=None, main_key="v2_B"):
    variants = variants or VARIANTS
    res = results[main_key]
    summary_df = build_summary(res, cfgs[main_key], cadence, split_events)
    # 자산추이: 설정별 총자산 + 벤치마크
    eq = None
    for key, _ in variants:
        e = results[key]["equity"]
        if not len(e):
            continue
        col = e[["거래일", "equity_usd"]].rename(columns={"equity_usd": f"{key} 총자산"})
        col[f"{key} 낙폭%"] = e["drawdown_pct"].values
        eq = col if eq is None else eq.merge(col, on="거래일", how="outer")
    if eq is not None:
        initial = float(res["broker"].summary()["initial_equity_usd"])
        curve = res["bench"].get("curve")
        if curve is not None and len(curve):
            eq["벤치마크(동일가중B&H)"] = eq["거래일"].map(curve).astype(float).mul(initial).round(2)
    trade_cols = ["리밸런스일", "기준일", "체결일", "티커", "종목명", "매매", "수량", "체결가", "시가", "체결금액",
                  "수수료", "실현손익", "주문금액", "주문ID", "비고"]
    rebal_cols = ["리밸런스일", "기준일", "체결일", "순위", "티커", "종목명", "섹터", "유형", "주의", "주도주점수",
                  "상대강도(0~100)", "52주고점대비%", "시총(십억$)", "목표비중%", "결과"]
    rs_cols = ["리밸런스일", "기준일", "체결일", "목록수", "후보수", "매수건수", "매도건수", "매수금액",
               "현금(체결후)", "보유종목수", "총자산(체결일종가)", "스크리닝"]
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        cmp_df.to_excel(xw, sheet_name="비교", index=False)
        if isinstance(cmp_df.attrs.get("yearly"), pd.DataFrame):
            cmp_df.attrs["yearly"].to_excel(xw, sheet_name="연도별", index=False)
        summary_df.to_excel(xw, sheet_name=f"요약({main_key})", index=False)
        (eq if eq is not None else pd.DataFrame()).to_excel(xw, sheet_name="자산추이", index=False)
        for key, _ in variants:
            pd.DataFrame(results[key]["trades"], columns=trade_cols).to_excel(
                xw, sheet_name=f"거래_{key}", index=False)
        pd.DataFrame(res["rebal_rows"], columns=rebal_cols).to_excel(xw, sheet_name=f"리밸런스별목록({main_key})"[:31], index=False)
        pd.DataFrame(res["rebal_summary"], columns=rs_cols).to_excel(xw, sheet_name=f"리밸런스요약({main_key})"[:31], index=False)
        pd.DataFrame(_flatten(cfgs[main_key]), columns=["설정키", "값"]).to_excel(xw, sheet_name="설정", index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(60, max(10, width + 2))


def _load_cfg(path=None):
    from auto_buy import load_config
    cfg, _ = load_config(path)
    return cfg


def replay(start=DEFAULT_START, end=None, cadence="weekly", cfg=None, out=OUT_XLSX,
           refresh=False, use_cache=True, state_path=REPLAY_STATE):
    """과거 시뮬레이션 실행 → 요약 DataFrame(항목/값). 엑셀은 out 에 저장.
    start: 첫 리밸런스 후보일(그 이후 첫 월요일부터). end: 평가 종료일(기본 DB 최신).
    cadence: weekly | monthly. refresh: 스크리닝 캐시 무시하고 다시 계산."""
    cfg = cfg or _load_cfg()
    cadence = (cadence or "weekly").lower()
    start = start or DEFAULT_START
    latest = db_latest_date()
    end = min(end, latest) if end else latest
    if pd.Timestamp(start) > pd.Timestamp(end):
        raise ValueError(f"start {start} > end {end}")
    dates = rebalance_dates(start, end, cadence)
    if not dates:
        raise ValueError(f"{start}~{end} 구간에 리밸런스일(월요일)이 없음")
    d0 = (pd.Timestamp(start) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    lag_days = int((cfg.get("replay") or {}).get("financial_lag_days", DEFAULT_FIN_LAG_DAYS))
    log.info("replay %s: %s ~ %s (리밸런스 %d회, 첫 %s 마지막 %s) DB 최신 %s | 재무 지연 %d일 | "
             "스크리닝 1회 10~20s, 캐시 %s",
             cadence, start, end, len(dates), dates[0], dates[-1], latest, lag_days, CACHE_DIR)
    panel, split_events = load_prices(d0, end, use_cache=use_cache)
    trading_days = [d for d in panel["CLOSE"].index if d <= end]

    def screen_fn(asof):
        return screen_asof(asof, refresh=refresh, use_cache=use_cache, lag_days=lag_days)

    t0 = time.time()
    if cadence == "weekly":
        dates = dates[::strategy.every_weeks(cfg)]
    res = simulate(dates, trading_days, screen_fn, panel, cfg, state_path, end=end)
    summary_df = write_report(res, cfg, out, cadence, split_events)
    log.info("replay 완료 %.1fs → %s (상태 %s)", time.time() - t0, out, state_path)
    return summary_df


def main(argv=None):
    ap = argparse.ArgumentParser(description="주도주 자동매수 과거 시뮬레이션")
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=None)
    ap.add_argument("--cadence", default="weekly", choices=["weekly", "monthly"])
    ap.add_argument("--config", default=None, help="auto_buy_config.json 경로")
    ap.add_argument("--out", default=OUT_XLSX)
    ap.add_argument("--refresh", action="store_true", help="스크리닝 캐시 무시하고 다시 계산")
    ap.add_argument("--no-cache", action="store_true", help="캐시 읽기/쓰기 모두 안 함")
    a = ap.parse_args(argv)
    if not logging.getLogger().handlers and not log.handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    cfg = _load_cfg(a.config)
    df = replay(a.start, a.end, a.cadence, cfg, out=a.out, refresh=a.refresh, use_cache=not a.no_cache)
    print(df.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
