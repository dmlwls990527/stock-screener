#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
factor_eval.py — 스크리너에 새 규칙을 넣기 전에 반드시 통과시키는 검증 도구.

왜 만들었나 (2026-09-23):
  "PLTR 같은 걸 잡게 보완해줘" 요청으로 규칙 5개를 만들어 6개 연도에 걸어봤더니
  전부 현행 Track B 보다 못했다. 특히 PLTR 이야기에 맞춰 만든 R3(급락+실적유지)는
  PLTR 이 있던 2022년만 +0.52 이고 다른 해는 마이너스였다 — 전형적 과최적화.
  그 검증을 매번 임시 스크립트로 하지 않도록 도구로 고정한다.

사용:
  ./.venv/bin/python factor_eval.py                 # 현행 트랙 + 기본 후보 규칙 비교
  ./.venv/bin/python factor_eval.py --years 2020-12-31 2022-12-30
  코드에서 RULES 에 람다 한 줄 추가하면 바로 같이 평가됨.

읽는 법:
  초과중앙 = 규칙이 뽑은 종목들의 수익 중앙값 − 같은 시점 유니버스 전체 중앙값.
  0 보다 커야 규칙이 값을 한 것. **한 해만 크게 이기는 규칙은 의심할 것** —
  연도별로 흩어보고 '이긴 해' 수를 같이 본다.

한계 (결과 해석 시 반드시 감안):
  - 상장폐지 종목은 현재가가 없어 제외된다 → 규칙·유니버스 양쪽 다 생존편향으로
    부풀려져 있다. 상대 비교는 유효하나 절대 수익률은 과대평가다.
  - 유니버스 크기가 연도마다 다르다(2018년 450종 → 2023년 1379종). SEC 분기재무
    적재 범위가 늘어난 탓이라 초기 연도는 큰 회사 위주로 치우쳐 있다.
  - 지평선 3년 고정. 최근 연도는 3년이 안 차면 가능한 만큼만 쓴다(출력에 표시).
"""
import sys, argparse
sys.path.insert(0, "/data/frame")
import numpy as np
import pandas as pd

import leader_screener as L   # __main__ 가드 있어 import 해도 실행되지 않음

DEFAULT_YEARS = ["2018-12-31", "2019-12-31", "2020-12-31",
                 "2021-12-31", "2022-12-30", "2023-12-29"]
HORIZON_YEARS = 3

# 평가할 규칙. 이름 → df 를 받아 불리언 시리즈를 주는 함수.
RULES = {
    "현행 TrackB":            lambda d: d["trackB_pass"] == True,
    "현행 TrackA 상위20":      lambda d: d["trackA_rank"] <= 20,
    "현행 TrackC 주도주":       lambda d: d.get("leader_pass", pd.Series(False, index=d.index)) == True,
    "연속성장>=6":             lambda d: d["rev_streak"] >= 6,
    "연속성장>=8":             lambda d: d["rev_streak"] >= 8,
    "급락+실적유지":            lambda d: (d["rank_up"] < -50) & (d["rev_streak"] >= 4),
}


def load_prices():
    px = L.dq("SELECT CODE, date_ AS D, CLOSE AS C FROM daily_marcap_us")
    px["D"] = pd.to_datetime(px["D"])
    px["C"] = px["C"].astype(float)
    return px


def evaluate(years=DEFAULT_YEARS, rules=RULES):
    px = load_prices()
    last = px["D"].max()

    def close_on(ts):
        return px[px["D"] <= ts].sort_values("D").groupby("CODE")["C"].last()

    frames, spans = {}, {}
    for asof in years:
        t0 = pd.Timestamp(asof)
        t1 = min(t0 + pd.DateOffset(years=HORIZON_YEARS), last)
        df = L.build(asof)
        if df.empty:
            continue
        # screen_now() 와 같은 순서를 그대로 재현해야 실제 스크리너가 그때 뽑았을
        # 결과와 같아진다. 예전 backtest() 는 build() 만 불러서 ① 섹터 매핑 ② 부동산 제외
        # ③ 제외 후 순위 재계산 ④ price_metrics/build_leaders 가 전부 빠져 있었고,
        # 그래서 Track C(주도주)는 만들어진 이후 한 번도 검증된 적이 없었다.
        sec = L.fa.get_sector_map(L.conn, "ticker_master_us") if hasattr(L.fa, "get_sector_map") else {}
        SKO = {"Information Technology": "IT", "Health Care": "헬스케어", "Industrials": "산업재",
               "Consumer Discretionary": "임의소비", "Consumer Staples": "필수소비",
               "Financials": "금융", "Communication Services": "커뮤니", "Energy": "에너지",
               "Materials": "소재", "Real Estate": "부동산", "Utilities": "유틸"}
        # 주의: 섹터는 현재 시점 매핑이라 과거 시점엔 미세한 선견(look-ahead)이 있다.
        # 섹터는 거의 안 바뀌어 영향은 작지만, 결과 해석 시 감안할 것.
        df["섹터"] = df["CODE"].map(lambda c: SKO.get(sec.get(c, ""), (sec.get(c, "") or "?")))
        df = df[df["섹터"] != "부동산"].copy()
        try:
            pm = L.price_metrics(asof)
            if not pm.empty:
                df = df.merge(pm, on="CODE", how="left")
        except Exception as e:
            print("  [경고] %s price_metrics 실패: %s" % (asof, str(e)[:70]))
        # 부동산 제외 후 순위 재계산 (screen_now 와 동일)
        df["trackA_rank"] = df["fund_rank_key"].rank(ascending=False, method="min").astype(int)
        if "rs_pct" in df.columns:
            df, _lead = L.build_leaders(df)
        df["배수"] = df["CODE"].map(close_on(t1)) / df["CODE"].map(close_on(t0))
        frames[asof] = df[df["배수"].notna()].copy()
        spans[asof] = round((t1 - t0).days / 365.25, 1)

    rows = []
    for name, fn in rules.items():
        for asof, d in frames.items():
            try:
                sel = d[fn(d).fillna(False)]
            except Exception as e:
                print("  [건너뜀] %s @ %s — %s" % (name, asof, str(e)[:60]))
                continue
            base_med = d["배수"].median()
            rows.append(dict(
                규칙=name, 연도=asof[:4], 종목수=len(sel),
                중앙=round(sel["배수"].median(), 2) if len(sel) else np.nan,
                초과중앙=round(sel["배수"].median() - base_med, 2) if len(sel) else np.nan,
                승률2배=round(100 * (sel["배수"] >= 2).mean()) if len(sel) else np.nan,
                유니버스승률2배=round(100 * (d["배수"] >= 2).mean()),
            ))
    return pd.DataFrame(rows), spans, frames


def report(r, spans, frames):
    print("=" * 104)
    print(" 지평선 %d년 | 연도별 유니버스: %s" % (
        HORIZON_YEARS, ", ".join("%s=%d종(%.1f년)" % (a[:4], len(d), spans[a])
                                 for a, d in frames.items())))
    print("=" * 104)
    piv = r.pivot(index="규칙", columns="연도", values="초과중앙")
    ycols = list(piv.columns)
    piv["평균초과"] = piv[ycols].mean(axis=1).round(3)
    piv["연도중앙"] = piv[ycols].median(axis=1).round(3)
    piv["이긴해"] = (piv[ycols] > 0).sum(axis=1)

    # 한 해가 평균을 떠받치는지 일반적으로 검사한다. 특정 연도를 하드코딩하면
    # (예전 '2018제외평균') 다른 해가 지배하는 규칙을 놓친다 — 실제로 '급락+실적유지'는
    # 2018이 아니라 2022 한 해가 전부였다.
    dom, wo = [], []
    for idx in piv.index:
        vals = piv.loc[idx, ycols].astype(float)
        if vals.notna().sum() < 2:
            dom.append("-"); wo.append(np.nan); continue
        # 빼면 평균이 가장 많이 떨어지는 해 = 가장 크게 기여한 해
        best, bestdrop = None, None
        for y in ycols:
            rest = vals.drop(labels=[y])
            if rest.notna().sum() == 0: continue
            drop = vals.mean() - rest.mean()
            if bestdrop is None or drop > bestdrop:
                bestdrop, best = drop, y
        dom.append(best or "-")
        wo.append(round(vals.drop(labels=[best]).mean(), 3) if best else np.nan)
    piv["지배연도"] = dom
    piv["지배연도제외"] = wo

    print(piv.sort_values("연도중앙", ascending=False).to_string())
    print()
    print("  ※ 읽는 순서: ①연도중앙(한 해 이상치에 안 흔들림) ②이긴해 ③지배연도제외.")
    print("    평균초과가 커도 '지배연도제외'가 음수면 그 한 해가 만든 착시다.")
    print("    이긴해가 3/6 이하면 동전던지기와 구분되지 않는다.")
    print()
    print("=" * 104)
    print(" 2배 이상 달성률 — 규칙 vs 같은 해 유니버스")
    print("=" * 104)
    for name in r["규칙"].unique():
        s = r[r["규칙"] == name]
        print("%-22s %s" % (name, "  ".join(
            "%s:%s%%(유니버스%s%%)" % (a, b, c)
            for a, b, c in zip(s["연도"], s["승률2배"], s["유니버스승률2배"]))))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="팩터 규칙을 여러 해에 걸쳐 검증")
    ap.add_argument("--years", nargs="+", default=DEFAULT_YEARS)
    a = ap.parse_args()
    r, spans, frames = evaluate(a.years)
    report(r, spans, frames)
