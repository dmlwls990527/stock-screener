#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Top 30 시가총액 연도별 PNG 생성
- charts/png/kospi/  : KOSPI 연도별
- charts/png/us/     : S&P500 월별
"""

from __future__ import annotations
import sys, glob, io
from pathlib import Path
from datetime import date

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import requests

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "marcap"))

TOP_N    = 30
OUT_KR   = BASE_DIR / "charts" / "png" / "kospi"
OUT_US   = BASE_DIR / "charts" / "png" / "us"
OUT_KR.mkdir(parents=True, exist_ok=True)
OUT_US.mkdir(parents=True, exist_ok=True)

# ── 한글 폰트 ─────────────────────────────────────────────────────────────────
def _setup_font():
    candidates = [
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/data/.nvm/versions/node/v18.20.8/bin/../../../share/fonts",
    ]
    plt.rcParams["axes.unicode_minus"] = False
    for p in candidates:
        pp = Path(p)
        if pp.is_file():
            try:
                fm.fontManager.addfont(str(pp))
                prop = fm.FontProperties(fname=str(pp))
                plt.rcParams["font.family"] = prop.get_name()
                return
            except Exception:
                continue

_setup_font()

# 색상 팔레트 (종목명 고정 색)
PALETTE = [
    "#4E79A7","#F28E2B","#E15759","#76B7B2","#59A14F",
    "#EDC948","#B07AA1","#FF9DA7","#9C755F","#BAB0AC",
    "#1f77b4","#ff7f0e","#2ca02c","#d62728","#9467bd",
    "#8c564b","#e377c2","#7f7f7f","#bcbd22","#17becf",
    "#aec7e8","#ffbb78","#98df8a","#ff9896","#c5b0d5",
    "#c49c94","#f7b6d2","#c7c7c7","#dbdb8d","#9edae5",
]

def _cmap(all_names):
    return {n: PALETTE[i % len(PALETTE)] for i, n in enumerate(sorted(set(all_names)))}


# ═══════════════════════════════════════════════════════════════════════════════
# PNG 생성 함수
# ═══════════════════════════════════════════════════════════════════════════════

def save_bar_png(df: pd.DataFrame, val_col: str, unit: str,
                 title: str, out: Path):
    df = df.sort_values(val_col, ascending=True).head(TOP_N).reset_index(drop=True)

    fig, ax = plt.subplots(figsize=(13, 10))
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#1a1a2e")

    colors = PALETTE[:len(df)]
    bars = ax.barh(df["Name"], df[val_col], color=colors[::-1], height=0.7)

    # 값 레이블
    for bar, val in zip(bars, df[val_col]):
        ax.text(bar.get_width() * 1.01, bar.get_y() + bar.get_height() / 2,
                f"{val:,.1f}{unit}", va="center", ha="left",
                fontsize=9, color="white")

    ax.set_xlabel(f"시가총액 ({unit.strip()})", color="white", fontsize=11)
    ax.set_title(title, color="white", fontsize=14, pad=15, fontweight="bold")
    ax.tick_params(colors="white", labelsize=9)
    ax.spines[:].set_color("#444")
    ax.xaxis.label.set_color("white")
    for spine in ax.spines.values():
        spine.set_edgecolor("#444")
    ax.grid(axis="x", color="#333", linestyle="--", alpha=0.5)

    # 연도 워터마크
    year_label = title.split("—")[-1].strip() if "—" in title else ""
    if year_label:
        ax.text(0.97, 0.04, year_label, transform=ax.transAxes,
                fontsize=44, color="rgba(200,200,200,0.15)" if False else "#ffffff22",
                ha="right", va="bottom", fontweight="bold",
                alpha=0.18)

    plt.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  저장: {out.name}")


# ═══════════════════════════════════════════════════════════════════════════════
# KOSPI
# ═══════════════════════════════════════════════════════════════════════════════

def run_kospi():
    print("\n▶ KOSPI PNG 생성 중...")
    files = sorted(glob.glob(str(BASE_DIR / "marcap/data/marcap-*.parquet")))

    # 전체 이름 수집 (색상 고정용)
    all_names = []
    snapshots = {}
    for fpath in files:
        year = Path(fpath).stem.split("-")[1]
        df = pd.read_parquet(fpath)
        df["Date"] = pd.to_datetime(df["Date"])
        kospi = df[df["Market"] == "KOSPI"]
        if kospi.empty:
            continue
        last_date = kospi["Date"].max()
        day = kospi[kospi["Date"] == last_date].copy()
        day = day.sort_values("Marcap", ascending=False).head(TOP_N).reset_index(drop=True)
        day["Marcap_T"] = day["Marcap"] / 1e12
        all_names += day["Name"].tolist()
        snapshots[year] = (day, last_date)

    cmap = _cmap(all_names)

    for year, (day, last_date) in snapshots.items():
        day = day.sort_values("Marcap_T", ascending=True)
        day["_color"] = day["Name"].map(lambda n: cmap.get(n, "#888"))

        fig, ax = plt.subplots(figsize=(13, 10))
        fig.patch.set_facecolor("#1a1a2e")
        ax.set_facecolor("#1a1a2e")

        bars = ax.barh(day["Name"], day["Marcap_T"],
                       color=day["_color"].tolist(), height=0.7)

        for bar, val in zip(bars, day["Marcap_T"]):
            ax.text(bar.get_width() * 1.01, bar.get_y() + bar.get_height() / 2,
                    f"{val:,.1f}조", va="center", ha="left",
                    fontsize=8.5, color="white")

        ax.set_xlabel("시가총액 (조원)", color="white", fontsize=11)
        ax.set_title(
            f"KOSPI 시가총액 Top {TOP_N}  —  {year}년 ({last_date.strftime('%m/%d')} 기준)",
            color="white", fontsize=14, pad=15, fontweight="bold")
        ax.tick_params(colors="white", labelsize=9)
        for sp in ax.spines.values():
            sp.set_edgecolor("#444")
        ax.grid(axis="x", color="#333", linestyle="--", alpha=0.5)
        ax.text(0.97, 0.03, year, transform=ax.transAxes,
                fontsize=52, color="white", ha="right", va="bottom",
                fontweight="bold", alpha=0.12)

        plt.tight_layout()
        out = OUT_KR / f"kospi_{year}.png"
        fig.savefig(out, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        print(f"  저장: {out.name}")

    print(f"  완료: {len(snapshots)}개 파일 → {OUT_KR}")


# ═══════════════════════════════════════════════════════════════════════════════
# US S&P500
# ═══════════════════════════════════════════════════════════════════════════════

_SPY_URL = (
    "https://www.ssga.com/us/en/intermediary/etfs/library-content"
    "/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
)
_SPY_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"),
    "Referer": "https://www.ssga.com/",
}

def _fetch_spy():
    r = requests.get(_SPY_URL, headers=_SPY_HEADERS, timeout=30)
    r.raise_for_status()
    raw = pd.read_excel(io.BytesIO(r.content), header=None)
    hdr = next(i for i, row in raw.iterrows()
               if "ticker" in [str(v).strip().lower() for v in row if pd.notna(v)])
    raw.columns = raw.iloc[hdr]
    raw = raw.iloc[hdr+1:].reset_index(drop=True)
    raw.columns = [str(c).strip() for c in raw.columns]
    col_map = {}
    for c in raw.columns:
        cl = c.lower().strip()
        if cl == "ticker":      col_map[c] = "Code"
        elif cl == "name":      col_map[c] = "Name"
        elif "shares" in cl:    col_map[c] = "SharesHeld"
    raw = raw.rename(columns=col_map)
    df = raw[["Code","Name","SharesHeld"]].dropna(subset=["Code"])
    df = df[df["Code"].astype(str).str.match(r"^[A-Z]{1,6}$")]
    df["SharesHeld"] = pd.to_numeric(df["SharesHeld"], errors="coerce").fillna(0)
    return df[df["SharesHeld"] > 0]


def run_us():
    print("\n▶ S&P500 PNG 생성 중...")
    print("  SPY 홀딩스 로딩...")
    spy = _fetch_spy()
    shares_map = spy.set_index("Code")["SharesHeld"].to_dict()
    name_map   = spy.set_index("Code")["Name"].to_dict()

    cache_dir = BASE_DIR / "us_monthly_cache"
    frames = []
    for f in sorted(cache_dir.glob("*.parquet")):
        ticker = f.stem
        if ticker not in shares_map:
            continue
        df = pd.read_parquet(f)[["Close"]].copy()
        df["Code"]       = ticker
        df["Name"]       = name_map.get(ticker, ticker)
        df["Marcap_B"]   = df["Close"] * shares_map[ticker] / 1e9
        frames.append(df)

    all_df = pd.concat(frames)
    all_df.index = pd.to_datetime(all_df.index)

    # 연말(12월) 스냅샷만 + 올해 최신월
    all_df["Year"]  = all_df.index.year
    all_df["Month"] = all_df.index.month

    # 연말 기준: 12월 데이터 있으면 12월, 없으면 해당 연도 마지막
    all_names = []
    snapshots = {}
    for year, grp in all_df.groupby("Year"):
        dec = grp[grp["Month"] == 12]
        target = dec if not dec.empty else grp[grp["Month"] == grp["Month"].max()]
        top = target.sort_values("Marcap_B", ascending=False).head(TOP_N).reset_index(drop=True)
        month_label = target.index.max().strftime("%m")
        all_names += top["Name"].tolist()
        snapshots[f"{year}"] = (top, month_label)

    cmap = _cmap(all_names)

    for label, (top, month_label) in sorted(snapshots.items()):
        top = top.sort_values("Marcap_B", ascending=True)

        fig, ax = plt.subplots(figsize=(13, 10))
        fig.patch.set_facecolor("#1a1a2e")
        ax.set_facecolor("#1a1a2e")

        colors = [cmap.get(n, "#888") for n in top["Name"]]
        bars = ax.barh(top["Name"], top["Marcap_B"], color=colors, height=0.7)

        for bar, val in zip(bars, top["Marcap_B"]):
            ax.text(bar.get_width() * 1.01, bar.get_y() + bar.get_height() / 2,
                    f"{val:,.0f}B$", va="center", ha="left",
                    fontsize=8.5, color="white")

        ax.set_xlabel("Market Cap (B USD)", color="white", fontsize=11)
        ax.set_title(
            f"S&P 500 Market Cap Top {TOP_N}  —  {label}년 ({month_label}월 기준)",
            color="white", fontsize=14, pad=15, fontweight="bold")
        ax.tick_params(colors="white", labelsize=9)
        for sp in ax.spines.values():
            sp.set_edgecolor("#444")
        ax.grid(axis="x", color="#333", linestyle="--", alpha=0.5)
        ax.text(0.97, 0.03, label, transform=ax.transAxes,
                fontsize=52, color="white", ha="right", va="bottom",
                fontweight="bold", alpha=0.12)

        plt.tight_layout()
        out = OUT_US / f"us_{label}.png"
        fig.savefig(out, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        print(f"  저장: {out.name}")

    print(f"  완료: {len(snapshots)}개 파일 → {OUT_US}")


if __name__ == "__main__":
    run_kospi()
    run_us()
    print("\n전체 완료!")
