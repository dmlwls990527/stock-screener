# 시총 상위 N 그대로(거르기 없음, 분기 리밸런스) — N 을 바꿔 가며 '20' 이 우연한 숫자인지 본다.
import sys, warnings; warnings.filterwarnings("ignore"); sys.path.insert(0, "/data/frame")
import pandas as pd, research_tilt as T, research_bt as R, replay, growth_factors as gf
latest = replay.db_latest_date()
panel, _ = replay.load_prices("2014-12-25", latest)
op, cl = panel["OPEN"], panel["CLOSE"]
etf = R.etf_prices(); MC = gf.load_panels()["MC"]
def topn(Tt, n):
    mem = replay.index_members(Tt, "sp500_pit"); row = gf._at(MC, Tt)
    cols = [c for c in mem if c in row.index and pd.notna(row[c]) and row[c] > 0 and c in cl.columns and pd.notna(cl.at[Tt, c])]
    mc = row[cols].astype(float).nlargest(n); return mc / mc.sum()
for period in ("design", "holdout"):
    start, end = R.PERIODS[period]; end = end or latest
    days = [d for d in cl.index if start <= d <= end and d in etf["CLOSE"].index]
    sig = {t: d for t, d in T.quarter_signals([d for d in cl.index if d <= end]).items() if days[0] <= d <= days[-1]}
    d1 = [d for d in days if d >= min(sig.values())]
    print(f"== {period} {d1[0]}~{d1[-1]}")
    for n in (5, 10, 20, 30, 50, 100):
        eq, b = T.run_band(d1, op, cl, {d: topn(t, n) for t, d in sig.items()})
        st, ys = R.stats(eq, d1)
        print(f"top{n:>3}: 연평균 {st['연평균%']:5.2f}  MDD {st['최대낙폭%']:6.1f}  변동성 {st['연변동성%']:5.1f}  회전 {b.turn/eq.mean()/(len(d1)/252)*100:4.0f}% | "
              + " ".join(f"{y}:{v:+.1f}" for y, v in ys.items()))
    for s in ("SPY", "QQQ"):
        eq, b = R.run_book(d1, etf["OPEN"], etf["CLOSE"], {d1[0]: pd.Series({s: 1.0})})
        st, ys = R.stats(eq, d1)
        print(f"{s:>6}: 연평균 {st['연평균%']:5.2f}  MDD {st['최대낙폭%']:6.1f}  변동성 {st['연변동성%']:5.1f}          | "
              + " ".join(f"{y}:{v:+.1f}" for y, v in ys.items()))
