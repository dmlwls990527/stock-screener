#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
satellite.py — '판단 위성' 실험 (2026-09-30 시작, 사용자 승인 "시작"). 모의 전용 — 실제 주문은 어디에도 없다.

무엇을 하나
  candidates : 이번 주 '신호 에피소드' 목록. 지금 S&P 500 ∪ 나스닥100 구성종목 중
               P1 12-1 모멘텀이 SPY 보다 +20%p 이상, P2 종가 ≥ 52주 고점 85%, P3 최근 6개월 거래대금 > 그 전 6개월,
               그리고 직전 6개 월말에는 셋 다 켜진 적이 없던 종목 (= 6개월 이상 꺼졌다가 켜짐).
               재무 플래그(TTM 매출 +20%·영업이익 흑자, 45일 지연)와 3년 연속 지표(거래대금·시총)를 붙인다.
               → data/satellite/episodes_latest.csv, satellite_latest.xlsx '신호에피소드' 시트
  decide F   : 주간 판단 JSON(F) 반영. {"asof": "YYYY-MM-DD", "decisions": [{"ticker","action":"매수|관망|제외|매도","confidence":1~3,"thesis","risks"}]}
               → data/satellite/decisions.jsonl (덧붙이기만, 수정 불가), 매수·매도는 '대기 주문'
  report     : 대기 주문 체결(DB 에 들어온 다음 거래일 시가, 슬리피지 0.05% + 수수료 0.1%), 매도 규칙 점검, 성적표
               → satellite_latest.xlsx (신호에피소드 / 판단기록 / 보유·대기 / 성적표 / 설명)
  weekly     : candidates + report (주간 크론)

미리 고정한 매매 규칙 (2026-09-30 백테스트 근거)
  · 장부 1,000만원(모의, USD 환산), 최대 10종목, 새 매수는 (현금+평가) ÷ 10 씩, 최소 $20
  · 손절 없음 (고정·추적 손절은 승자에서도 대조군에서도 최악이었다)
  · 최소 1년 보유. 예외는 매도 규칙 하나: TTM 매출 증가율이 2분기 연속 10% 미만 또는 TTM 영업이익 적자 전환 → 다음 거래일 시가 매도
  · 1년 지난 뒤에는 판단으로 매도 가능 (action "매도")
이겨야 할 기준선 (top20_case.py 대조군, 2013~): 필터가 고른 종목을 판단 없이 사면 1년 뒤 48% 가 지수를 이기고 중앙값 −1.4%p.
  성적표는 '내가 산 것' vs '같은 주에 필터가 골랐지만 안 산 것' vs SPY. 12개월 뒤 둘 다 못 이기면 중단.
"""
import argparse
import datetime as dt
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, "/data/frame")

import numpy as np            # noqa: E402
import pandas as pd           # noqa: E402

BASE = "/data/frame"
DIR = os.path.join(BASE, "data", "satellite")
OUT = os.path.join(BASE, "satellite_latest.xlsx")
STATE, DECS, SEEN, EPIS = (os.path.join(DIR, n) for n in ("state.json", "decisions.jsonl", "seen.json", "episodes_latest.csv"))
INITIAL_KRW, FX_DEFAULT, MAX_POS, MIN_ORDER = 10_000_000, 1357.2, 10, 20.0
COMMISSION, SLIP = 0.001, 0.0005
MIN_HOLD_DAYS = 365
EXCESS_PP, HIGH_PCT, OFF_MONTHS = 0.20, 0.85, 6
SELL_GROWTH, SELL_QUARTERS = 0.10, 2


# ---------------- 공용 ----------------
def load_json(p, default):
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, p)


def load_state():
    st = load_json(STATE, None)
    if st is None:
        fx = FX_DEFAULT
        try:
            fx = float(load_json(os.path.join(BASE, "auto_buy_config.json"), {}).get("paper", {}).get("fx_rate", FX_DEFAULT))
        except Exception:
            pass
        st = {"started": dt.date.today().isoformat(), "initial_krw": INITIAL_KRW, "fx": fx,
              "initial_usd": round(INITIAL_KRW / fx, 2), "cash_usd": round(INITIAL_KRW / fx, 2),
              "holdings": {}, "pending": [], "fills": [], "auto_sells": []}
        save_json(STATE, st)
    return st


def spy_close(start):
    import yfinance as yf
    s = yf.download("SPY", start=start, auto_adjust=True, progress=False)["Close"]
    s = s.iloc[:, 0] if isinstance(s, pd.DataFrame) else s
    s = s.dropna()
    s.index = pd.to_datetime(s.index)
    return s


def names_sectors():
    import factor_analysis as fa
    c = fa.get_conn()
    cur = c.cursor()
    cur.execute("SELECT code, name, sector FROM ticker_master_us")
    m = {r[0]: (r[1] or "", r[2] or "") for r in cur.fetchall()}
    c.close()
    return m


_TTM = {}


def ttm(fin, asof, lag=45):
    cut = pd.Timestamp(asof) - pd.Timedelta(days=lag)
    if cut in _TTM:
        return _TTM[cut]
    g = fin[(fin["end"] <= cut) & (fin["end"] > cut - pd.Timedelta(days=430))].groupby("code").tail(4)
    s = g.groupby("code").agg(n=("rev", "size"), rev=("rev", "sum"), op=("op", "sum"))
    _TTM[cut] = s[s["n"] == 4]
    return _TTM[cut]


def ttm_growth_series(fin, code):
    """분기 말마다 TTM 매출 전년 대비 증가율 (최근 것이 마지막). 매도 규칙용."""
    q = fin[fin["code"] == code].sort_values("end")
    if len(q) < 8:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    rev = q.set_index("end")["rev"].rolling(4).sum()
    op = q.set_index("end")["op"].rolling(4).sum()
    g = rev / rev.shift(4) - 1
    return g.dropna(), op.dropna()


def first_db_date_after(dates, d):
    """dates: 정렬된 DatetimeIndex. d 이후(초과) 첫 거래일."""
    d = pd.Timestamp(d)
    i = dates.searchsorted(d, side="right")
    return dates[i] if i < len(dates) else None


# ---------------- candidates ----------------
def build_candidates(T=None):
    import growth_factors as gf
    import replay
    import research_bt as R
    T = pd.Timestamp(T or replay.db_latest_date())
    Ts = T.strftime("%Y-%m-%d")
    mem = {}
    for u, tag in (("sp500_pit", "S&P500"), ("ndx_pit", "NDX100")):
        for c in replay.index_members(Ts, u):
            mem.setdefault(c, []).append(tag)
    d0 = (T - pd.DateOffset(months=21)).strftime("%Y-%m-%d")
    panel, _ = replay.load_prices(d0, Ts, use_cache=False)
    CL = panel["CLOSE"].copy()
    CL.index = pd.to_datetime(CL.index)
    P = gf.load_panels(start=(T - pd.DateOffset(years=4, months=2)).strftime("%Y-%m-%d"), refresh=True, persist=False)
    AMT = P["AMT"].copy()
    AMT.index = pd.to_datetime(AMT.index)
    cols = [c for c in CL.columns if c in mem]
    CL = CL[cols]
    AMT = AMT.reindex(index=CL.index, columns=cols).astype(float)
    spy = spy_close(d0).reindex(CL.index).ffill()
    n = len(CL) - 1
    if n < 252 + 30:
        raise SystemExit(f"가격 이력 부족: {n + 1}일")

    def sig_at(i):
        mom = CL.iloc[i - 21] / CL.iloc[i - 252] - 1
        bm = spy.iloc[i - 21] / spy.iloc[i - 252] - 1
        nh = CL.iloc[i] / CL.iloc[i - 251:i + 1].max()
        a1, a0 = AMT.iloc[i - 125:i + 1].mean(), AMT.iloc[i - 251:i - 125].mean()
        sig = ((mom - bm) >= EXCESS_PP) & (nh >= HIGH_PCT) & (a1 > a0)
        return sig.fillna(False), mom, bm, nh, a1 / a0 - 1

    now, mom, bm, nh, ag = sig_at(n)
    idx = CL.index
    me = pd.Series(idx, index=idx).groupby([idx.year, idx.month]).last()
    prev_me = [d for d in me if d < idx[n] - pd.Timedelta(days=15)][-OFF_MONTHS:]
    off = pd.Series(True, index=cols)
    for d in prev_me:
        i = idx.get_loc(d)
        off &= ~sig_at(i)[0] if i >= 252 else False
    elig = now & off
    on_cols = list(elig[elig].index)
    # 재무·3년 지표
    fin = R.fin_table()
    tn, tp = ttm(fin, T), ttm(fin, T - pd.Timedelta(days=365))
    f = gf.features(Ts, P)
    ns = names_sectors()
    mcrow = gf._at(P["MC"], Ts)
    rkrow = gf._at(P["RK"], Ts)
    seen = load_json(SEEN, {})
    rows = []
    for c in on_cols:
        rg = (tn["rev"].get(c, np.nan) / tp["rev"].get(c, np.nan) - 1) if c in tn.index and c in tp.index and tp["rev"].get(c, 0) > 0 else np.nan
        op = tn["op"].get(c, np.nan) if c in tn.index else np.nan
        rev = tn["rev"].get(c, np.nan) if c in tn.index else np.nan
        first = seen.get(c, Ts)
        rows.append({
            "티커": c, "종목명": ns.get(c, ("", ""))[0], "섹터": ns.get(c, ("", ""))[1], "지수": "+".join(mem[c]),
            "첫 신호일": first, "신규": "O" if first == Ts else "",
            "종가$": round(float(CL[c].iloc[n]), 2),
            "시총(십억$)": round(float(mcrow.get(c, np.nan)) / 1e9, 1) if pd.notna(mcrow.get(c, np.nan)) else np.nan,
            "시총순위": int(rkrow.get(c)) if pd.notna(rkrow.get(c, np.nan)) else np.nan,
            "12-1수익률%": round(float(mom[c]) * 100, 0), "SPY대비%p": round(float(mom[c] - bm) * 100, 0),
            "52주고점대비%": round(float(nh[c]) * 100, 0), "거래대금6개월증가%": round(float(ag[c]) * 100, 0),
            "TTM매출증가%": round(rg * 100, 0) if pd.notna(rg) else np.nan,
            "TTM영업이익(백만$)": round(op / 1e6, 0) if pd.notna(op) else np.nan,
            "영업이익률%": round(op / rev * 100, 1) if pd.notna(op) and pd.notna(rev) and rev else np.nan,
            "재무필터(매출+20%·흑자)": ("O" if (pd.notna(rg) and rg >= 0.20 and pd.notna(op) and op > 0) else ("X" if pd.notna(rg) else "재무없음")),
            "거래대금3년연속↑": "O" if bool(f["amt_steady"].get(c, False)) else "X",
            "시총3년연속↑": "O" if bool(f["mc_steady"].get(c, False)) else "X",
            "3년거래대금증가율%": round(float(f["amt_cagr3"].get(c, np.nan)) * 100, 0) if pd.notna(f["amt_cagr3"].get(c, np.nan)) else np.nan,
        })
    df = pd.DataFrame(rows)
    if len(df):
        df = df.sort_values(["신규", "SPY대비%p"], ascending=[False, False]).reset_index(drop=True)
        df.insert(0, "순위", range(1, len(df) + 1))
    save_json(SEEN, {c: seen.get(c, Ts) for c in on_cols})       # 이번 주 켜진 것만 유지 → 꺼지면 다음 등장은 '신규'
    os.makedirs(DIR, exist_ok=True)
    df.assign(기준일=Ts).to_csv(EPIS, index=False, encoding="utf-8-sig")
    print(f"[candidates] 기준일 {Ts} | 유니버스 {len(cols)} | 지금 켜짐 {int(now.sum())} | 6개월 꺼졌다 켜짐 {len(df)} | 신규 {int((df['신규'] == 'O').sum()) if len(df) else 0}")
    return df, Ts


# ---------------- decide ----------------
def cmd_decide(path):
    d = json.load(open(path, encoding="utf-8"))
    asof = d["asof"]
    st = load_state()
    ep = pd.read_csv(EPIS) if os.path.exists(EPIS) else pd.DataFrame(columns=["티커"])
    held = set(st["holdings"]) | {p["ticker"] for p in st["pending"] if p["side"] == "BUY"}
    n_buy = 0
    for x in d["decisions"]:
        tk, act = x["ticker"].strip().upper(), x["action"].strip()
        rec = {"asof": asof, "recorded_at": dt.datetime.now().isoformat(timespec="seconds"), "ticker": tk, "action": act,
               "confidence": x.get("confidence"), "thesis": x.get("thesis", ""), "risks": x.get("risks", ""),
               "in_episodes": bool(tk in set(ep["티커"]))}
        note = ""
        if act == "매수":
            if tk in held:
                note = "이미 보유/대기 — 기록만"
            elif len(held) >= MAX_POS:
                note = f"최대 {MAX_POS}종목 초과 — 기록만"
            else:
                st["pending"].append({"ticker": tk, "side": "BUY", "decided": asof, "confidence": x.get("confidence"), "why": "판단"})
                held.add(tk); n_buy += 1
        elif act == "매도":
            h = st["holdings"].get(tk)
            if not h:
                note = "보유 아님 — 기록만"
            elif (pd.Timestamp(asof) - pd.Timestamp(h["entry_date"])).days < MIN_HOLD_DAYS:
                note = f"보유 {(pd.Timestamp(asof) - pd.Timestamp(h['entry_date'])).days}일 < 1년 — 규칙상 불가, 기록만"
            elif any(p["ticker"] == tk and p["side"] == "SELL" for p in st["pending"]):
                note = "이미 매도 대기"
            else:
                st["pending"].append({"ticker": tk, "side": "SELL", "decided": asof, "why": "판단"})
        rec["note"] = note
        with open(DECS, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"  {act:2} {tk:6} {note}")
    save_json(STATE, st)
    print(f"[decide] {asof}: 판단 {len(d['decisions'])}건, 새 매수 대기 {n_buy}건, 보유+대기 {len(held)}")


# ---------------- report ----------------
def cmd_report():
    import replay
    import research_bt as R
    st = load_state()
    fin = R.fin_table()
    latest = pd.Timestamp(replay.db_latest_date())
    decs = [json.loads(l) for l in open(DECS, encoding="utf-8")] if os.path.exists(DECS) else []
    D = pd.DataFrame(decs)
    dates_needed = ([pd.Timestamp(x) for x in D["asof"]] if len(D) else []) + [pd.Timestamp(p["decided"]) for p in st["pending"]] \
        + [pd.Timestamp(h["entry_date"]) for h in st["holdings"].values()]
    d0 = (min(dates_needed) - pd.Timedelta(days=10)) if dates_needed else latest - pd.Timedelta(days=400)
    d0 = min(d0, latest - pd.Timedelta(days=400))
    panel, _ = replay.load_prices(d0.strftime("%Y-%m-%d"), latest.strftime("%Y-%m-%d"), use_cache=False)
    CL, OP = panel["CLOSE"].copy(), panel["OPEN"].copy()
    CL.index = pd.to_datetime(CL.index); OP.index = pd.to_datetime(OP.index)
    spy = spy_close(d0.strftime("%Y-%m-%d")).reindex(CL.index).ffill()

    def px_at(tk, d, kind="OPEN"):
        P = OP if kind == "OPEN" else CL
        if tk not in P.columns or d not in P.index:
            return np.nan
        v = P.at[d, tk]
        if pd.isna(v) or v <= 0:
            v = CL.at[d, tk] if tk in CL.columns else np.nan
        return float(v) if pd.notna(v) else np.nan

    def mark():
        return st["cash_usd"] + sum(h["shares"] * px_at(t, latest, "CLOSE") for t, h in st["holdings"].items()
                                    if pd.notna(px_at(t, latest, "CLOSE")))

    # 1) 매도 규칙 (자동): 보유 종목 재무 점검 → 매도 대기
    for tk, h in list(st["holdings"].items()):
        if any(p["ticker"] == tk and p["side"] == "SELL" for p in st["pending"]):
            continue
        g, op = ttm_growth_series(fin, tk)
        g = g[g.index <= latest - pd.Timedelta(days=45)]; op = op[op.index <= latest - pd.Timedelta(days=45)]
        why = None
        if len(g) >= SELL_QUARTERS and (g.iloc[-SELL_QUARTERS:] < SELL_GROWTH).all():
            why = f"TTM 매출증가 {SELL_QUARTERS}분기 연속 {int(SELL_GROWTH*100)}% 미만 ({', '.join(f'{v*100:.0f}%' for v in g.iloc[-SELL_QUARTERS:])})"
        elif len(op) and op.iloc[-1] < 0:
            why = f"TTM 영업이익 적자 ({op.iloc[-1]/1e6:,.0f}백만$)"
        if why:
            st["pending"].append({"ticker": tk, "side": "SELL", "decided": latest.strftime("%Y-%m-%d"), "why": "규칙: " + why})
            st["auto_sells"].append({"ticker": tk, "date": latest.strftime("%Y-%m-%d"), "why": why})
            print(f"  [매도 규칙] {tk}: {why}")
    # 2) 대기 주문 체결: decided 다음 거래일 시가가 DB 에 있으면
    still = []
    for p in st["pending"]:
        d = first_db_date_after(CL.index, p["decided"])
        px = px_at(p["ticker"], d) if d is not None else np.nan
        if d is None or pd.isna(px):
            still.append(p); continue
        if p["side"] == "BUY":
            eq = mark()
            val = min(st["cash_usd"], eq / MAX_POS)
            if val < MIN_ORDER:
                p["note"] = f"현금 부족 (${st['cash_usd']:.0f})"; still.append(p); continue
            fill = px * (1 + SLIP)
            fee = val * COMMISSION
            shares = (val - fee) / fill
            st["cash_usd"] -= val
            h = st["holdings"].get(p["ticker"])
            if h:
                tot = h["shares"] + shares
                h["entry_px"] = (h["entry_px"] * h["shares"] + fill * shares) / tot; h["shares"] = tot
            else:
                st["holdings"][p["ticker"]] = {"shares": shares, "entry_px": fill, "entry_date": d.strftime("%Y-%m-%d"),
                                               "confidence": p.get("confidence"), "cost_usd": val}
            st["fills"].append({"date": d.strftime("%Y-%m-%d"), "ticker": p["ticker"], "side": "BUY", "shares": round(shares, 4),
                                "price": round(fill, 4), "value_usd": round(val, 2), "fee": round(fee, 2), "decided": p["decided"]})
            print(f"  [체결] BUY  {p['ticker']:6} {d.date()} ${fill:.2f} × {shares:.4f} = ${val:,.0f}")
        else:
            h = st["holdings"].pop(p["ticker"], None)
            if not h:
                continue
            fill = px * (1 - SLIP)
            gross = h["shares"] * fill
            fee = gross * COMMISSION
            st["cash_usd"] += gross - fee
            st["fills"].append({"date": d.strftime("%Y-%m-%d"), "ticker": p["ticker"], "side": "SELL", "shares": round(h["shares"], 4),
                                "price": round(fill, 4), "value_usd": round(gross, 2), "fee": round(fee, 2), "decided": p["decided"],
                                "pnl_pct": round((fill / h["entry_px"] - 1) * 100, 1), "why": p.get("why", "")})
            print(f"  [체결] SELL {p['ticker']:6} {d.date()} ${fill:.2f} 수익 {(fill / h['entry_px'] - 1) * 100:+.1f}% ({p.get('why', '')})")
    st["pending"] = still
    st["last_report"] = latest.strftime("%Y-%m-%d")
    save_json(STATE, st)
    # 3) 표
    eq = mark()
    hold_rows = []
    for tk, h in st["holdings"].items():
        c = px_at(tk, latest, "CLOSE")
        hold_rows.append({"구분": "보유", "티커": tk, "매수일": h["entry_date"], "매수가$": round(h["entry_px"], 2), "수량": round(h["shares"], 4),
                          "현재가$": round(c, 2) if pd.notna(c) else np.nan, "평가$": round(h["shares"] * c, 0) if pd.notna(c) else np.nan,
                          "수익률%": round((c / h["entry_px"] - 1) * 100, 1) if pd.notna(c) else np.nan,
                          "보유일": (latest - pd.Timestamp(h["entry_date"])).days, "확신도": h.get("confidence")})
    for p in st["pending"]:
        hold_rows.append({"구분": f"대기 {p['side']}", "티커": p["ticker"], "매수일": p["decided"], "확신도": p.get("confidence"), "비고": p.get("why", "") + " " + p.get("note", "")})
    hold_rows.append({"구분": "현금", "평가$": round(st["cash_usd"], 0)})
    hold_rows.append({"구분": "합계", "평가$": round(eq, 0), "수익률%": round((eq / st["initial_usd"] - 1) * 100, 2),
                      "비고": f"시작 {st['started']} · 초기 {st['initial_krw']:,}원 = ${st['initial_usd']:,.0f} (환율 {st['fx']})"})
    H = pd.DataFrame(hold_rows)
    # 성적표: 판단별 — 판단 다음 거래일 시가 → 지금 종가, SPY 동일 구간
    score_rows = []
    for r in decs:
        if r.get("note") or r["action"] == "매도":          # 거부된 판단·매도는 '고른 것' 이 아니다 → 성적표 제외
            continue
        d = first_db_date_after(CL.index, r["asof"])
        if d is None:
            continue
        p0, p1 = px_at(r["ticker"], d), px_at(r["ticker"], latest, "CLOSE")
        s0, s1 = spy.get(d, np.nan), spy.get(latest, np.nan)
        if pd.isna(p0) or pd.isna(p1):
            continue
        score_rows.append({"판단일": r["asof"], "티커": r["ticker"], "판단": r["action"], "확신도": r.get("confidence"),
                           "기준가$": round(p0, 2), "현재가$": round(p1, 2), "수익률%": round((p1 / p0 - 1) * 100, 1),
                           "SPY%": round((s1 / s0 - 1) * 100, 1), "초과%p": round(((p1 / p0) - (s1 / s0)) * 100, 1),
                           "경과일": (latest - d).days, "근거": r.get("thesis", "")[:120]})
    S = pd.DataFrame(score_rows)
    agg_rows = []
    if len(S):
        S["그룹"] = np.where(S["판단"] == "매수", "내가 산 것", np.where(S["판단"].isin(["관망", "제외"]), "필터는 골랐지만 안 산 것", S["판단"]))
        for g, d in S.groupby("그룹"):
            agg_rows.append({"그룹": g, "건수": len(d), "평균 수익률%": round(d["수익률%"].mean(), 1), "중앙 수익률%": round(d["수익률%"].median(), 1),
                             "평균 초과%p": round(d["초과%p"].mean(), 1), "SPY 이긴 비율%": round((d["초과%p"] > 0).mean() * 100, 0),
                             "평균 경과일": round(d["경과일"].mean(), 0)})
        first = pd.Timestamp(S["판단일"].min())
        months = (latest - first).days / 30.4
        verdict = ("판정 시점 아님 (%.0f개월 경과, 12개월 뒤 판정)" % months) if months < 12 else "12개월 경과 — '내가 산 것' 이 '안 산 것' 과 SPY 를 둘 다 이기지 못하면 중단"
        agg_rows.append({"그룹": "판정", "비고": verdict})
    A = pd.DataFrame(agg_rows)
    ep = pd.read_csv(EPIS) if os.path.exists(EPIS) else pd.DataFrame()
    Dv = D.copy()
    if len(Dv):
        Dv = Dv[["asof", "ticker", "action", "confidence", "thesis", "risks", "note"]].rename(
            columns={"asof": "판단일", "ticker": "티커", "action": "판단", "confidence": "확신도", "thesis": "근거", "risks": "위험", "note": "비고"})
    desc = pd.DataFrame({"항목": ["신호에피소드", "판단기록", "보유·대기", "성적표", "기준선", "매매 규칙", "주의"],
                         "값": ["S&P 500 ∪ 나스닥100 중 12-1 모멘텀 ≥ SPY +20%p · 종가 ≥ 52주고점 85% · 6개월 거래대금 증가, 직전 6개 월말 모두 꺼져 있던 것. '신규' = 이번 주 처음 등장",
                               "매주 월요일 Claude 세션이 신규 후보(최대 10개)를 조사해 매수/관망/제외 + 근거·위험을 기록. 덧붙이기만 하고 수정 불가",
                               "모의 장부 1,000만원. 매수 = 판단 다음 거래일 시가(DB 에 들어오면 체결), 슬리피지 0.05% + 수수료 0.1%. 실제 주문 없음",
                               "판단 다음 거래일 시가 → 최근 종가. '내가 산 것' vs '필터는 골랐지만 안 산 것' vs SPY. 12개월 뒤 판정",
                               "필터만 믿고 사면(2013~ 대조군 5,630회) 1년 뒤 48% 가 지수를 이기고 중앙값 −1.4%p, 16% 가 +30%p 이상",
                               f"손절 없음 · 최대 {MAX_POS}종목 · 1종목당 (현금+평가)÷{MAX_POS} · 최소 1년 보유 · 자동 매도는 TTM 매출증가 {SELL_QUARTERS}분기 연속 {int(SELL_GROWTH*100)}% 미만 또는 영업이익 적자만",
                               "Claude 의 판단에 실력이 있는지는 아무도 모른다 — 이 파일은 그것을 측정하기 위한 것이다. SPY 배당 포함(총수익)"]})
    with pd.ExcelWriter(OUT, engine="openpyxl") as xw:
        (ep if len(ep) else pd.DataFrame({"안내": ["이번 주 6개월 꺼졌다 켜진 종목 없음"]})).to_excel(xw, sheet_name="신호에피소드", index=False)
        (Dv if len(Dv) else pd.DataFrame({"안내": ["아직 판단 기록 없음"]})).to_excel(xw, sheet_name="판단기록", index=False)
        H.to_excel(xw, sheet_name="보유·대기", index=False)
        (A if len(A) else pd.DataFrame({"안내": ["아직 성적 없음"]})).to_excel(xw, sheet_name="성적표", index=False, startrow=0)
        if len(S):
            S.drop(columns=["그룹"]).to_excel(xw, sheet_name="성적표", index=False, startrow=len(A) + 2)
        desc.to_excel(xw, sheet_name="설명", index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                w = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(60, max(8, w + 2))
    print(f"[report] 기준일 {latest.date()} | 평가 ${eq:,.0f} ({(eq / st['initial_usd'] - 1) * 100:+.2f}%) | 보유 {len(st['holdings'])} 대기 {len(st['pending'])} 판단 {len(decs)} → {OUT}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["candidates", "decide", "report", "weekly"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--asof", default=None, help="candidates 기준일 (기본 DB 최신)")
    a = ap.parse_args()
    if a.cmd in ("candidates", "weekly"):
        build_candidates(a.asof)
    if a.cmd == "decide":
        if not a.file:
            raise SystemExit("decide 에는 판단 JSON 경로가 필요합니다")
        cmd_decide(a.file)
    if a.cmd in ("report", "weekly"):
        cmd_report()


if __name__ == "__main__":
    main()
