"""나스닥100 연도별 변경 YAML(jmccarrell/n100tickers) → (ticker, start_date, end_date) 구간 CSV. 코드 실행 없이 데이터만 읽는다."""
import sys, glob, os
sys.path.insert(0, "/data/frame")
import re, pandas as pd


def load_simple_yaml(path):
    """이 저장소 형식 전용 파서: year, tickers_on_Jan_1 (목록), changes (날짜 → difference/union 목록)."""
    out = {"year": None, "tickers_on_Jan_1": [], "changes": {}}
    sec, day, kind = None, None, None
    for raw in open(path, encoding="utf-8"):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or line.strip() == "---":
            continue
        ind = len(line) - len(line.lstrip())
        s = line.strip()
        if ind == 0:
            key, _, val = s.partition(":")
            sec = key.strip()
            if sec == "year":
                out["year"] = int(val.strip())
            continue
        if s.startswith("- "):
            t = s[2:].strip().strip("'\"")
            if sec == "tickers_on_Jan_1":
                out["tickers_on_Jan_1"].append(t)
            elif sec == "changes" and day and kind:
                out["changes"][day].setdefault(kind, []).append(t)
            continue
        if sec == "changes":
            m = re.match(r"^['\"]?(\d{4}-\d{2}-\d{2})['\"]?:$", s)
            if m:
                day, kind = m.group(1), None
                out["changes"].setdefault(day, {})
            elif s.rstrip(":") in ("difference", "union"):
                kind = s.rstrip(":")
    return out
files = sorted(glob.glob("/data/frame/data/ndx/n100-ticker-changes-*.yaml"))
events = []                       # (date, 'in'|'out', ticker)
prev_end = None
cur = set()
for f in files:
    y = load_simple_yaml(f)
    jan1 = set(y["tickers_on_Jan_1"])
    d0 = f"{y['year']}-01-01"
    if not cur:
        for t in jan1: events.append((d0, "in", t))
    else:                                      # 연말 집합과 다음 해 1/1 목록이 다르면 1/1 에 맞춘다
        for t in cur - jan1: events.append((d0, "out", t))
        for t in jan1 - cur: events.append((d0, "in", t))
    cur = set(jan1)
    for d, ch in sorted((y.get("changes") or {}).items()):
        d = str(d)
        for t in ch.get("difference") or []:
            if t in cur: events.append((d, "out", t)); cur.discard(t)
        for t in ch.get("union") or []:
            if t not in cur: events.append((d, "in", t)); cur.add(t)
rows, open_ = [], {}
for d, k, t in sorted(events, key=lambda e: (e[0], e[1] != "out")):
    t2 = t.replace(".", "-")
    if k == "in": open_[t2] = d
    else:
        if t2 in open_: rows.append((t2, open_.pop(t2), d))
for t, s in open_.items(): rows.append((t, s, ""))
df = pd.DataFrame(rows, columns=["ticker", "start_date", "end_date"]).sort_values(["ticker", "start_date"])
df.to_csv("/data/frame/data/ndx_ticker_start_end.csv", index=False)
print("구간", len(df), "종목", df.ticker.nunique(), "| 현재 구성", int((df.end_date == "").sum()))
# 이름변경 후보: 같은 날 빠진 X(DB 없음) ↔ 들어온 Y(DB 있고 그 전부터 가격 있음)
import factor_analysis as fa
c = fa.get_conn(); cur_ = c.cursor()
cur_.execute("SELECT code, TO_CHAR(MIN(date_), 'YYYY-MM-DD') FROM daily_price_us GROUP BY code")
first = {r[0]: r[1] for r in cur_.fetchall()}; c.close()
dfe = df[df.end_date != ""]
for _, r in df.iterrows():
    if r.ticker not in first or r.start_date < "2014-06-01": continue
    if first[r.ticker] > (pd.Timestamp(r.start_date) - pd.Timedelta(days=60)).strftime("%Y-%m-%d"): continue
    xs = dfe[(dfe.end_date == r.start_date) & (~dfe.ticker.isin(first))].ticker.tolist()
    if xs: print(f"  후보 {r.ticker:6} {r.start_date} <- {','.join(xs)}")
