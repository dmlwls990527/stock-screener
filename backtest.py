#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py — 팩터 모멘텀 전략 워크포워드 백테스트 (point-in-time 유니버스)

factor_analysis.py 의 치명적 결함(= 현재 시총 Top100을 '고정' 유니버스로 잡고
과거를 본다 → look-ahead / 생존 편향)을 바로잡아,
각 리밸런싱 시점의 시총 상위 N 종목으로 유니버스를 '매번 다시' 구성해 백테스트한다.

전략
  - 분기마다 그 시점 시총 상위 TOP_N(point-in-time) 종목 중,
    최근 TRAIL 분기 시총 성장률(선형가중 QoQ) + 일관성으로 종합점수를 매겨
    상위 TOP_K 를 '동일가중' 매수 → 다음 분기말에 리밸런싱. 거래비용 반영.
  - 벤치마크: 같은 시점 유니버스(Top N) '동일가중' 보유.
  - 비교용으로 '고정 유니버스(현재 Top N)' 모드도 함께 돌려 look-ahead 편향 크기를 보여준다.

데이터 전제(확인 완료)
  - close 는 split(액면분할) 보정됨 → 수익률 계산에 그대로 사용.
  - 모멘텀 점수는 marcap(시가총액) QoQ 성장률 = factor_analysis 의 '시총 성장률' 팩터.
  - 펀더멘털 팩터(PER/ROE 등)는 daily_fundamental_us 에 과거 스냅샷이 없어
    point-in-time 백테스트 불가 → 이번 백테스트에서는 제외(모멘텀 팩터만).

실행
  python3 backtest.py
  python3 backtest.py --topn 100 --topk 20 --trail 8 --cost 0.002
"""
import os
import argparse
import pandas as pd
import numpy as np
import jaydebeapi

# ── DB (factor_analysis.py 와 동일) ───────────────────────────────────────────
TIBERO_HOST = "localhost"
TIBERO_PORT = 44123
TIBERO_SID  = "tibero"
TIBERO_USER = os.environ.get("TIBERO_USER", "sys")
TIBERO_PASS = os.environ.get("TIBERO_PASS", "")
JDBC_JAR    = "/data/tibero7/tibero7/client/lib/jar/tibero7-jdbc.jar"
JDBC_CLASS  = "com.tmax.tibero.jdbc.TbDriver"

W_START, W_STEP = 1.00, 0.05      # 선형 가중치(최근 분기일수록 큼) — factor_analysis 와 동일
EXCLUDE = {"GOOG"}                # 중복 주식 클래스 제거(GOOGL 유지)


def get_conn():
    url = f"jdbc:tibero:thin:@{TIBERO_HOST}:{TIBERO_PORT}:{TIBERO_SID}"
    return jaydebeapi.connect(JDBC_CLASS, url, [TIBERO_USER, TIBERO_PASS], JDBC_JAR)


def fetchall(conn, sql):
    cur = conn.cursor()
    cur.execute(sql)
    rows = cur.fetchall()
    cur.close()
    return rows


# ── 데이터 적재 ───────────────────────────────────────────────────────────────
def quarter_ends(conn):
    """전체 기간 각 분기의 마지막 거래일."""
    rows = fetchall(conn, """
        SELECT MAX(date_) FROM daily_marcap_us
        GROUP BY EXTRACT(YEAR FROM date_), CEIL(EXTRACT(MONTH FROM date_)/3)
        ORDER BY MAX(date_)
    """)
    return [str(r[0])[:10] for r in rows]


def load_universe(conn, qdates, top_n):
    """각 분기말의 시총 상위 top_n (point-in-time 유니버스)."""
    ins = ", ".join(f"TO_DATE('{d}','YYYY-MM-DD')" for d in qdates)
    rows = fetchall(conn, f"""
        SELECT TO_CHAR(date_,'YYYY-MM-DD'), code, rank
        FROM daily_marcap_us
        WHERE date_ IN ({ins}) AND rank <= {top_n}
    """)
    df = pd.DataFrame(rows, columns=["d", "code", "rank"])
    df = df[~df["code"].isin(EXCLUDE)]
    return df


def load_prices(conn, qdates, codes):
    """유니버스에 한 번이라도 든 종목의 분기말 close/marcap → 피벗."""
    ins = ", ".join(f"TO_DATE('{d}','YYYY-MM-DD')" for d in qdates)
    inc = ", ".join(f"'{c}'" for c in codes)
    rows = fetchall(conn, f"""
        SELECT TO_CHAR(date_,'YYYY-MM-DD'), code, close, marcap
        FROM daily_marcap_us
        WHERE date_ IN ({ins}) AND code IN ({inc})
    """)
    df = pd.DataFrame(rows, columns=["d", "code", "close", "marcap"])
    df["close"]  = pd.to_numeric(df["close"],  errors="coerce")
    df["marcap"] = pd.to_numeric(df["marcap"], errors="coerce")
    close_pv = df.pivot_table(index="d", columns="code", values="close").sort_index()
    mc_pv    = df.pivot_table(index="d", columns="code", values="marcap").sort_index()
    return close_pv, mc_pv


# ── 모멘텀 점수 (factor_analysis 의 시총 성장률 산식) ──────────────────────────
def momentum_scores(qoq_win):
    """
    qoq_win: 행=최근 TRAIL분기(오래된→최신), 열=종목 의 QoQ% DataFrame
    반환: 종목별 (선형가중 QoQ 점수, 일관성, 유효분기수)
    """
    n = len(qoq_win)
    w = np.array([W_START + k * W_STEP for k in range(n)])   # 최근일수록 큰 가중치
    valid = qoq_win.notna()
    wsum  = valid.mul(w, axis=0).sum()
    score = qoq_win.mul(w, axis=0).sum() / wsum.replace(0, np.nan)   # 가중평균 QoQ
    cons  = (qoq_win > 0).sum() / valid.sum().replace(0, np.nan)     # 일관성(0~1)
    cnt   = valid.sum()
    return score, cons, cnt


def perf(qret):
    """분기 수익률 리스트 → 성과 지표."""
    s = pd.Series(qret).dropna()
    n = len(s)
    if n == 0:
        return dict(quarters=0, total=0, cagr=float("nan"), mdd=float("nan"),
                    sharpe=float("nan"), final=1.0)
    eq  = (1 + s).cumprod()
    yrs = n / 4.0
    final = eq.iloc[-1]
    cagr  = final ** (1 / yrs) - 1 if final > 0 else float("nan")
    mdd   = (eq / eq.cummax() - 1).min()
    sharpe = s.mean() / s.std() * np.sqrt(4) if s.std() > 0 else float("nan")
    return dict(quarters=n, total=final - 1, cagr=cagr, mdd=mdd,
                sharpe=sharpe, final=final)


# ── 백테스트 엔진 ─────────────────────────────────────────────────────────────
def run(dates, uni, close_pv, mc_pv, top_n, top_k, trail, cost, fixed_codes=None):
    """
    fixed_codes=None  → point-in-time 유니버스(올바른 백테스트)
    fixed_codes=set() → 고정 유니버스(look-ahead 편향 재현)
    """
    qoq_all = mc_pv.pct_change()
    port, bench, hold_log = [], [], []
    prev = set()
    for i in range(trail, len(dates) - 1):
        T, Tn = dates[i], dates[i + 1]
        if fixed_codes is not None:
            uni_codes = [c for c in fixed_codes if c in mc_pv.columns]
        else:
            uni_codes = uni[uni["d"] == T]["code"].tolist()
        uni_codes = [c for c in uni_codes if c in mc_pv.columns]
        if len(uni_codes) < top_k:
            continue

        win = qoq_all.loc[dates[i - trail + 1:i + 1], uni_codes]   # 최근 trail분기 QoQ
        score, cons, cnt = momentum_scores(win)
        good = [c for c in uni_codes if cnt.get(c, 0) >= max(3, trail // 2)]
        if len(good) < top_k:
            good = [c for c in uni_codes if cnt.get(c, 0) >= 2]
        sc = score[good]
        rng = sc.max() - sc.min()
        snorm = (sc - sc.min()) / rng if rng > 0 else sc * 0          # min-max 정규화
        combined = 0.7 * snorm + 0.3 * cons[good].fillna(0)          # 성장률 70% + 일관성 30%
        selected = combined.sort_values(ascending=False).head(top_k).index.tolist()

        fwd = close_pv.loc[Tn, uni_codes] / close_pv.loc[T, uni_codes] - 1   # 다음 분기 수익률
        sel_ret = fwd[selected].dropna()
        if len(sel_ret) == 0:
            continue
        cur = set(selected)
        turn = len(cur - prev) / top_k if prev else 1.0             # 교체율
        net  = sel_ret.mean() - 2 * cost * turn                     # 매수+매도 비용
        prev = cur

        port.append(net)
        bench.append(fwd.dropna().mean())                           # 유니버스 동일가중
        hold_log.append((Tn, selected[:top_k]))
    return perf(port), perf(bench), port, bench, hold_log


def fmt(m):
    return (f"기간 {m['quarters']:>2}분기 | 총수익 {m['total']*100:>8.1f}% | "
            f"CAGR {m['cagr']*100:>6.1f}% | MDD {m['mdd']*100:>6.1f}% | "
            f"Sharpe {m['sharpe']:>5.2f}")


def main():
    ap = argparse.ArgumentParser(description="팩터 모멘텀 백테스트")
    ap.add_argument("--topn",  type=int,   default=100)
    ap.add_argument("--topk",  type=int,   default=20)
    ap.add_argument("--trail", type=int,   default=8)
    ap.add_argument("--cost",  type=float, default=0.002)
    args = ap.parse_args()

    conn = get_conn()
    print("=" * 78)
    print(f"  팩터 모멘텀 백테스트  |  유니버스 Top{args.topn}  포트 Top{args.topk}  "
          f"모멘텀 {args.trail}분기  비용 {args.cost*100:.1f}%/회")
    print("=" * 78)

    qdates = quarter_ends(conn)
    uni = load_universe(conn, qdates, args.topn)
    fixed_codes = uni[uni["d"] == qdates[-1]]["code"].tolist()      # 현재(마지막) Top N
    all_codes = sorted(set(uni["code"]) | set(fixed_codes))
    close_pv, mc_pv = load_prices(conn, qdates, all_codes)
    dates = [d for d in qdates if d in close_pv.index]

    print(f"  분기말 {len(dates)}개 ({dates[0]} ~ {dates[-1]})")
    print(f"  point-in-time 유니버스 누적 종목수: {uni['code'].nunique()}  "
          f"(고정 유니버스는 {len(fixed_codes)}개)")
    print(f"  백테스트 구간: {dates[args.trail+1]} ~ {dates[-1]}  "
          f"(앞 {args.trail}분기는 모멘텀 워밍업)\n")

    pit_s, pit_b, _, _, hold = run(dates, uni, close_pv, mc_pv,
                                   args.topn, args.topk, args.trail, args.cost)
    fix_s, fix_b, _, _, _    = run(dates, uni, close_pv, mc_pv,
                                   args.topn, args.topk, args.trail, args.cost,
                                   fixed_codes=set(fixed_codes))

    print("  [전략] point-in-time 유니버스 (올바른 백테스트)")
    print("     ", fmt(pit_s))
    print("  [벤치] 시점 유니버스 Top%d 동일가중" % args.topn)
    print("     ", fmt(pit_b))
    print()
    print("  [참고] 고정 유니버스 = 현재 Top%d 로 과거를 본 경우 (look-ahead 편향)" % args.topn)
    print("     ", fmt(fix_s))
    print("     → 같은 전략인데 미래에 살아남아 커진 종목만 써서 수익률이 부풀려짐\n")

    ar = pit_s["cagr"]; bn = pit_b["cagr"]; bias = fix_s["cagr"]
    if all(x == x for x in (ar, bn)):
        print(f"  ▶ 초과수익(전략-벤치): 연 {(ar-bn)*100:+.1f}%p")
    if all(x == x for x in (ar, bias)):
        print(f"  ▶ look-ahead 편향 크기: 연 {(bias-ar)*100:+.1f}%p (고정 - 올바른)")

    print("\n  최근 리밸런싱 선정 종목(예시):")
    for Tn, names in hold[-3:]:
        print(f"   {Tn}: {', '.join(names[:12])}{' ...' if len(names) > 12 else ''}")

    print("\n완료")


if __name__ == "__main__":
    main()
