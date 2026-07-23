#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Top 30 시가총액 Bar Chart Race
- 한국 KOSPI : 1995 ~ 현재 (연말 기준)
- 미국 S&P500+NASDAQ : 2024 ~ 현재 (월말 기준)

출력: charts/top30_kospi.html  /  charts/top30_us.html
"""

from __future__ import annotations

import sys
import glob
import io
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import requests

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "marcap"))

CHART_DIR = BASE_DIR / "charts"
CHART_DIR.mkdir(parents=True, exist_ok=True)

TOP_N = 30

# ── 색상 팔레트 (회사별 고정색) ───────────────────────────────────────────────
PALETTE = [
    "#4E79A7","#F28E2B","#E15759","#76B7B2","#59A14F",
    "#EDC948","#B07AA1","#FF9DA7","#9C755F","#BAB0AC",
    "#1f77b4","#ff7f0e","#2ca02c","#d62728","#9467bd",
    "#8c564b","#e377c2","#7f7f7f","#bcbd22","#17becf",
    "#aec7e8","#ffbb78","#98df8a","#ff9896","#c5b0d5",
    "#c49c94","#f7b6d2","#c7c7c7","#dbdb8d","#9edae5",
]


def _color_map(names: list[str]) -> dict[str, str]:
    """회사명 → 고정 색상 매핑 (같은 회사는 연도 달라도 같은 색)."""
    unique = sorted(set(names))
    return {n: PALETTE[i % len(PALETTE)] for i, n in enumerate(unique)}


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 한국 KOSPI 데이터
# ═══════════════════════════════════════════════════════════════════════════════

def load_kospi_yearly() -> dict[str, pd.DataFrame]:
    """연말 기준 KOSPI Top 30 시가총액 딕셔너리 반환 {label: df}."""
    marcap_files = sorted(glob.glob(str(BASE_DIR / "marcap/data/marcap-*.parquet")))
    result: dict[str, pd.DataFrame] = {}

    for fpath in marcap_files:
        year = Path(fpath).stem.split("-")[1]
        df = pd.read_parquet(fpath)
        df["Date"] = pd.to_datetime(df["Date"])

        # KOSPI만, 연말 마지막 거래일
        kospi = df[df["Market"] == "KOSPI"].copy()
        if kospi.empty:
            continue
        last_date = kospi["Date"].max()
        day_df = kospi[kospi["Date"] == last_date].copy()
        day_df = day_df.sort_values("Marcap", ascending=False).head(TOP_N).reset_index(drop=True)
        day_df["Marcap_T"] = day_df["Marcap"] / 1e12        # 조원

        label = f"{year} ({last_date.strftime('%m/%d')})"
        result[label] = day_df[["Name", "Code", "Marcap_T"]].copy()

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 미국 S&P500 데이터
# ═══════════════════════════════════════════════════════════════════════════════

_SPY_URL = (
    "https://www.ssga.com/us/en/intermediary/etfs/library-content"
    "/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
)
_SPY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "Referer": "https://www.ssga.com/",
}


def _fetch_spy_holdings() -> pd.DataFrame:
    print("  SPY 홀딩스 다운로드 중...")
    try:
        r = requests.get(_SPY_URL, headers=_SPY_HEADERS, timeout=30)
        r.raise_for_status()
        raw = pd.read_excel(io.BytesIO(r.content), header=None)
    except Exception as e:
        print(f"  [경고] SPY 다운로드 실패: {e}")
        return pd.DataFrame()

    # 헤더 행 탐색 — "Ticker" 컬럼이 있는 행
    hdr_row = None
    for i, row in raw.iterrows():
        vals = [str(v).strip().lower() for v in row if pd.notna(v)]
        if "ticker" in vals:
            hdr_row = i
            break
    if hdr_row is None:
        print("  [경고] SPY 헤더 행 탐색 실패")
        return pd.DataFrame()

    raw.columns = raw.iloc[hdr_row]
    raw = raw.iloc[hdr_row + 1:].reset_index(drop=True)
    raw.columns = [str(c).strip() for c in raw.columns]

    col_map: dict[str, str] = {}
    for c in raw.columns:
        cl = c.lower().strip()
        if cl == "ticker":
            col_map[c] = "Code"
        elif cl == "name":
            col_map[c] = "Name"
        elif "weight" in cl:
            col_map[c] = "Weight"
        elif "sector" in cl:
            col_map[c] = "Sector"
        elif "shares" in cl:
            col_map[c] = "SharesHeld"
    raw = raw.rename(columns=col_map)

    needed = [c for c in ["Code", "Name", "SharesHeld"] if c in raw.columns]
    df = raw[needed].dropna(subset=["Code"]).copy()
    df = df[df["Code"].astype(str).str.match(r"^[A-Z]{1,6}$")]
    df["SharesHeld"] = pd.to_numeric(df.get("SharesHeld", 0), errors="coerce").fillna(0)
    df = df[df["SharesHeld"] > 0]
    print(f"  SPY 홀딩스: {len(df)}종목")
    return df


def load_us_monthly() -> dict[str, pd.DataFrame]:
    """월말 기준 S&P500 Top 30 시가총액 딕셔너리 반환 {label: df}."""
    spy = _fetch_spy_holdings()
    if spy.empty:
        print("  [경고] SPY 홀딩스 없음 — US 차트 생략")
        return {}

    shares_map = spy.set_index("Code")["SharesHeld"].to_dict()
    name_map   = spy.set_index("Code")["Name"].to_dict() if "Name" in spy.columns else {}

    cache_dir = BASE_DIR / "us_monthly_cache"
    price_files = sorted(cache_dir.glob("*.parquet"))

    # 전체 monthly price 로드
    frames = []
    for f in price_files:
        ticker = f.stem
        if ticker not in shares_map:
            continue
        df = pd.read_parquet(f)[["Close"]].copy()
        df["Code"] = ticker
        df["SharesHeld"] = shares_map[ticker]
        df["Marcap_B"] = df["Close"] * df["SharesHeld"] / 1e9
        df["Name"] = name_map.get(ticker, ticker)
        frames.append(df)

    if not frames:
        return {}

    all_df = pd.concat(frames)
    all_df.index = pd.to_datetime(all_df.index)
    all_df["YearMonth"] = all_df.index.to_period("M")

    result: dict[str, pd.DataFrame] = {}
    for ym, grp in all_df.groupby("YearMonth"):
        top = grp.sort_values("Marcap_B", ascending=False).head(TOP_N).reset_index(drop=True)
        label = str(ym)   # "2024-01"
        result[label] = top[["Name", "Code", "Marcap_B"]].rename(columns={"Marcap_B": "Value"})

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Plotly Bar Chart Race HTML 생성
# ═══════════════════════════════════════════════════════════════════════════════

def _make_bar_frame(
    df: pd.DataFrame,
    label: str,
    val_col: str,
    unit: str,
    color_map: dict[str, str],
) -> go.Frame:
    """Plotly Frame 1개 생성."""
    df = df.sort_values(val_col, ascending=True).reset_index(drop=True)  # 위쪽이 1위
    colors = [color_map.get(n, "#888888") for n in df["Name"]]
    text_vals = [f" {v:,.1f}{unit}" for v in df[val_col]]

    bar = go.Bar(
        x=df[val_col],
        y=df["Name"],
        orientation="h",
        marker_color=colors,
        text=text_vals,
        textposition="outside",
        hovertemplate="%{y}<br>%{x:,.1f}" + unit + "<extra></extra>",
    )
    return go.Frame(data=[bar], name=label,
                    layout=go.Layout(annotations=[dict(
                        text=label,
                        x=0.98, y=0.05,
                        xref="paper", yref="paper",
                        showarrow=False,
                        font=dict(size=52, color="rgba(200,200,200,0.4)"),
                        xanchor="right",
                    )]))


def build_html(
    periods: dict[str, pd.DataFrame],
    val_col: str,
    unit: str,
    title: str,
    out_path: Path,
) -> None:
    if not periods:
        print(f"  [건너뜀] 데이터 없음: {title}")
        return

    all_names = [n for df in periods.values() for n in df["Name"]]
    cmap = _color_map(all_names)

    labels = sorted(periods.keys())
    frames = [_make_bar_frame(periods[lb], lb, val_col, unit, cmap) for lb in labels]

    # 초기 프레임
    init_df = periods[labels[-1]].sort_values(val_col, ascending=True)
    init_colors = [cmap.get(n, "#888888") for n in init_df["Name"]]
    init_text   = [f" {v:,.1f}{unit}" for v in init_df[val_col]]

    fig = go.Figure(
        data=[go.Bar(
            x=init_df[val_col],
            y=init_df["Name"],
            orientation="h",
            marker_color=init_colors,
            text=init_text,
            textposition="outside",
        )],
        frames=frames,
    )

    # 슬라이더 + 재생 버튼
    sliders = [dict(
        active=len(labels) - 1,
        currentvalue=dict(prefix="기준: ", font=dict(size=14)),
        pad=dict(t=50),
        steps=[dict(
            args=[[lb], dict(frame=dict(duration=600, redraw=True),
                             mode="immediate",
                             transition=dict(duration=300))],
            label=lb,
            method="animate",
        ) for lb in labels],
    )]

    x_max = max(df[val_col].max() for df in periods.values()) * 1.15

    fig.update_layout(
        title=dict(text=title, font=dict(size=18)),
        xaxis=dict(range=[0, x_max], title=f"시가총액 ({unit.strip()})"),
        yaxis=dict(autorange=True, tickfont=dict(size=11)),
        height=760,
        margin=dict(l=200, r=80, t=70, b=80),
        plot_bgcolor="#1a1a2e",
        paper_bgcolor="#16213e",
        font_color="#e0e0e0",
        updatemenus=[dict(
            type="buttons",
            showactive=False,
            y=0,
            x=0.02,
            xanchor="left",
            yanchor="top",
            pad=dict(t=45),
            buttons=[
                dict(label="▶ 재생",
                     method="animate",
                     args=[None, dict(frame=dict(duration=700, redraw=True),
                                      fromcurrent=True,
                                      transition=dict(duration=300))]),
                dict(label="⏸ 정지",
                     method="animate",
                     args=[[None], dict(frame=dict(duration=0, redraw=False),
                                        mode="immediate",
                                        transition=dict(duration=0))]),
            ],
        )],
        sliders=sliders,
        annotations=[dict(
            text=labels[-1],
            x=0.98, y=0.05,
            xref="paper", yref="paper",
            showarrow=False,
            font=dict(size=52, color="rgba(200,200,200,0.4)"),
            xanchor="right",
        )],
    )

    fig.write_html(str(out_path), include_plotlyjs="cdn")
    print(f"저장: {out_path}  ({len(labels)}개 기간)")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. main
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    print("=" * 60)
    print("▶ KOSPI Top 30 (1995 ~ 현재) 로딩...")
    kospi_data = load_kospi_yearly()
    build_html(
        kospi_data,
        val_col="Marcap_T",
        unit=" 조원",
        title=f"KOSPI Top {TOP_N} 시가총액 순위 변화 (연말 기준)",
        out_path=CHART_DIR / "top30_kospi.html",
    )

    print()
    print("▶ S&P500 Top 30 (2024 ~ 현재) 로딩...")
    us_data = load_us_monthly()
    build_html(
        us_data,
        val_col="Value",
        unit=" B$",
        title=f"S&P 500 Top {TOP_N} Market Cap 순위 변화 (월말 기준)",
        out_path=CHART_DIR / "top30_us.html",
    )

    print()
    print("완료!")
    print(f"  KOSPI: {CHART_DIR}/top30_kospi.html")
    print(f"  US   : {CHART_DIR}/top30_us.html")


if __name__ == "__main__":
    main()
