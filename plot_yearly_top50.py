#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
월말(해당 월 마지막 거래일) 기준 시가총액 Top N 을 가로 막대 그래프로 저장합니다.
(test.py 와 동일한 기간: 2024-01 ~ 오늘 또는 2026-12-31)

사전: pip install pandas matplotlib pyarrow
실행: test.py 에서 함께 호출됨. 단독: python3 plot_yearly_top50.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from utils import marcap_to_choeok  # noqa: E402
sys.path.insert(0, str(BASE_DIR / "marcap"))

from marcap import marcap_data  # noqa: E402

# test.py 와 동일 기간
START_DATE = "2024-01-01"
END_DATE = min(date.today(), date(2026, 12, 31)).strftime("%Y-%m-%d")

BAR_TOP_N = 30

CHART_DIR = BASE_DIR / "charts"
MONTHLY_DIR = CHART_DIR / "monthly"


def _setup_korean_font() -> None:
    from matplotlib import font_manager

    candidates = [
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    plt.rcParams["axes.unicode_minus"] = False
    for path in candidates:
        p = Path(path)
        if not p.is_file():
            continue
        try:
            font_manager.fontManager.addfont(str(p))
            prop = font_manager.FontProperties(fname=str(p))
            plt.rcParams["font.family"] = prop.get_name()
            print(f"폰트 사용: {p}")
            return
        except Exception:
            continue
    print("[경고] 한글 폰트를 찾지 못했습니다.")


def load_monthly_last_day_rows() -> tuple[pd.DataFrame, pd.Series]:
    print(f"데이터 로딩 중... {START_DATE} ~ {END_DATE}")
    df = marcap_data(START_DATE, END_DATE)
    df = df.reset_index()
    df["Date"] = pd.to_datetime(df["Date"])
    df["YearMonth"] = df["Date"].dt.to_period("M")
    monthly_last = df.groupby("YearMonth", sort=True)["Date"].max()
    df_m = df[df["Date"].isin(monthly_last.values)].copy()
    del df
    return df_m, monthly_last


def plot_monthly_top_marcap_bar(day_df: pd.DataFrame, period_key: str, out: Path) -> None:
    """Y: 회사 이름, X: 시가총액(조원) 가로 막대. 상위가 위쪽."""
    t = day_df.sort_values("Marcap", ascending=False).head(BAR_TOP_N).reset_index(drop=True)
    if t.empty:
        print("[경고] 데이터 없음:", period_key)
        return

    y = range(len(t))
    w = marcap_to_choeok(t["Marcap"])
    labels = (t["Name"].astype(str) + " (" + t["Code"].astype(str) + ")").tolist()

    fig_h = max(8.0, 0.32 * len(t) + 2.0)
    fig, ax = plt.subplots(figsize=(11, fig_h))
    ax.barh(list(y), w, height=0.72, color="steelblue", alpha=0.85)
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlabel("시가총액 (조원)")
    ax.set_title(f"시가총액 Top {BAR_TOP_N} — {period_key} (월말 기준)")
    ax.grid(True, axis="x", alpha=0.35)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print("저장:", out)


def run_monthly_barcharts() -> None:
    """월별 시총 Top N 가로 막대 PNG 생성."""
    _setup_korean_font()
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)

    df_m, monthly_last = load_monthly_last_day_rows()

    for ym in monthly_last.sort_index().index:
        last_d = monthly_last.loc[ym]
        period_key = ym.strftime("%Y-%m")
        one = df_m[df_m["Date"] == last_d]
        out = MONTHLY_DIR / f"bar_top{BAR_TOP_N}_{period_key}.png"
        plot_monthly_top_marcap_bar(one, period_key, out)

    print(f"\n완료. 월별 PNG: {MONTHLY_DIR}/bar_top{BAR_TOP_N}_YYYY-MM.png")


def main() -> None:
    run_monthly_barcharts()


if __name__ == "__main__":
    main()
