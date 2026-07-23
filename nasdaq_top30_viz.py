#!/usr/bin/env python3
"""
나스닥 Top 30 연도별 시총 순위 시각화
2016 ~ 2026 (연말 기준 / 2026은 4월 기준)
"""
import yfinance as yf
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import warnings
warnings.filterwarnings('ignore')

# ── 유니버스: NASDAQ-100 주요 종목 + 과거 구성 종목 ─────────────────────────
UNIVERSE = [
    # 현재 주요 종목
    'AAPL', 'MSFT', 'NVDA', 'AMZN', 'META', 'GOOGL', 'TSLA', 'AVGO', 'COST',
    'NFLX', 'TMUS', 'AMD', 'PEP', 'LIN', 'CSCO', 'ADBE', 'TXN', 'QCOM',
    'AMGN', 'INTU', 'ISRG', 'CMCSA', 'BKNG', 'MU', 'SBUX', 'HON', 'VRTX',
    'AMAT', 'ADP', 'GILD', 'ADI', 'REGN', 'PANW', 'INTC', 'LRCX', 'KLAC',
    'MELI', 'SNPS', 'CDNS', 'CSX', 'ORLY', 'CTAS', 'FTNT', 'MRVL', 'PCAR',
    'CPRT', 'ROST', 'CRWD', 'MNST', 'WDAY', 'NXPI', 'IDXX', 'ODFL', 'VRSK',
    # 과거 구성 종목 (2016~2022 시절)
    'EBAY', 'PYPL', 'ATVI', 'XLNX', 'ALXN', 'CTXS', 'EXPE', 'NTES',
    'CERN', 'VRSN', 'CHKP', 'NTAP', 'WDC', 'SWKS', 'LULU', 'ILMN',
    'BIIB', 'DLTR', 'EA', 'FAST', 'CTSH', 'CDNS', 'ANSS',
    # 시총 상위권 경쟁 종목
    'ORCL', 'CRM', 'NOW', 'UBER', 'ABNB', 'ZM', 'SHOP', 'SNOW',
]
UNIVERSE = list(dict.fromkeys(UNIVERSE))  # 중복 제거

YEARS      = list(range(2016, 2026))      # 2016~2025 (연말 기준)
TOP_N      = 30
OUT_PATH   = '/data/frame/nasdaq_top30_yearly.png'

# ── 1. 주가 다운로드 ──────────────────────────────────────────────────────────
print(f"[1] 주가 다운로드 ({len(UNIVERSE)}종목, 2015-2026)...")
raw = yf.download(
    UNIVERSE,
    start='2015-12-01',
    end='2026-04-25',
    interval='1d',
    auto_adjust=True,
    progress=True,
    threads=True,
)
prices = raw['Close'] if 'Close' in raw.columns else raw

# ── 2. 발행 주식수 ────────────────────────────────────────────────────────────
print("[2] 발행 주식수 조회 중...")
shares_map = {}
for sym in UNIVERSE:
    try:
        shares_map[sym] = yf.Ticker(sym).fast_info.shares or None
    except Exception:
        shares_map[sym] = None

# ── 3. 연말 시총 계산 ─────────────────────────────────────────────────────────
print("[3] 연말 시총 계산 중...")
yearly_top30 = {}
for year in YEARS:
    year_prices = prices[prices.index.year == year]
    if year_prices.empty:
        continue
    last_prices = year_prices.iloc[-1]  # 연말 종가
    mc = {}
    for sym in UNIVERSE:
        p = last_prices.get(sym)
        s = shares_map.get(sym)
        if p is not None and s and not pd.isna(p) and p > 0:
            mc[sym] = float(p) * float(s)
    top30 = sorted(mc.items(), key=lambda x: x[1], reverse=True)[:TOP_N]
    if top30:
        yearly_top30[year] = top30

# 2026 YTD (4월 기준)
ytd_prices = prices[prices.index.year == 2026]
if not ytd_prices.empty:
    last_prices = ytd_prices.iloc[-1]
    mc = {}
    for sym in UNIVERSE:
        p = last_prices.get(sym)
        s = shares_map.get(sym)
        if p is not None and s and not pd.isna(p) and p > 0:
            mc[sym] = float(p) * float(s)
    top30 = sorted(mc.items(), key=lambda x: x[1], reverse=True)[:TOP_N]
    if top30:
        yearly_top30[2026] = top30

all_years = sorted(yearly_top30.keys())
print(f"  → 완성된 연도: {all_years}")

# ── 4. 패러다임 탐지: 이전 연도에 없다가 새로 진입 후 2년 이상 유지 ──────────
print("[4] 패러다임 진입 종목 탐지 중...")
# year → 그 해 처음 Top30 진입한 종목 (전년도에는 없었던 종목)
new_entrants_by_year = {}
for i, year in enumerate(all_years):
    if i == 0:
        new_entrants_by_year[year] = set()  # 첫 해는 비교 대상 없으므로 제외
        continue
    curr = {s for s, _ in yearly_top30[year]}
    prev = {s for s, _ in yearly_top30[all_years[i-1]]}
    new_entrants_by_year[year] = curr - prev  # 이번에 새로 들어온 종목

# 새로 진입 후 이후 2년 연속 Top30 유지 여부 확인
paradigm_map = {}   # stock → 처음 진입 연도
paradigm_stocks = set()

for i, year in enumerate(all_years[1:], 1):   # 2017년부터
    for stock in new_entrants_by_year[year]:
        future_years = all_years[i+1:i+3]  # 이후 최대 2년
        if not future_years:
            continue
        stayed = all(
            stock in {s for s, _ in yearly_top30.get(fy, [])}
            for fy in future_years
        )
        if stayed:
            paradigm_stocks.add(stock)
            paradigm_map[stock] = year

print(f"  → 패러다임 종목 ({len(paradigm_stocks)}개): {sorted(paradigm_stocks)}")

# ── 5. 시각화 ─────────────────────────────────────────────────────────────────
print("[5] 시각화 생성 중...")
years_desc = sorted(all_years, reverse=True)  # 최신 → 과거
n = len(years_desc)

fig, axes = plt.subplots(n, 1, figsize=(22, n * 9))
if n == 1:
    axes = [axes]

BG_MAIN    = '#0d1117'
BG_AX      = '#161b22'
COL_NORMAL = '#4A90D9'
COL_PARA   = '#FF6B35'
COL_BORDER = '#30363d'

fig.patch.set_facecolor(BG_MAIN)

for ax, year in zip(axes, years_desc):
    top30 = yearly_top30[year]
    stocks  = [s for s, _ in top30]
    marcaps = [mc / 1e9 for _, mc in top30]  # 단위: $B

    # 역순으로 (1위 → 위쪽)
    stocks_r  = stocks[::-1]
    marcaps_r = marcaps[::-1]
    ranks_r   = list(range(TOP_N, 0, -1))

    bar_colors = [COL_PARA if s in paradigm_stocks else COL_NORMAL for s in stocks_r]

    bars = ax.barh(range(TOP_N), marcaps_r, color=bar_colors,
                   height=0.75, alpha=0.92, edgecolor=BG_AX, linewidth=0.5)

    # 바 안에 종목명 + 시총 표시
    max_mc = max(marcaps_r) if marcaps_r else 1
    for i, (bar, stock, mc) in enumerate(zip(bars, stocks_r, marcaps_r)):
        is_para = stock in paradigm_stocks
        entry_yr = paradigm_map.get(stock, '')
        txt_color = '#FFD700' if is_para else 'white'
        suffix    = f' ★({entry_yr}년 진입)' if is_para else ''
        txt       = f'  {stock}{suffix}   ${mc:.0f}B'

        ax.text(
            mc * 0.01,
            bar.get_y() + bar.get_height() / 2,
            txt,
            va='center', ha='left',
            color=txt_color,
            fontsize=11,
            fontweight='bold' if is_para else 'normal',
        )

    # 순위 레이블
    ax.set_yticks(range(TOP_N))
    ax.set_yticklabels([f'{r}위' for r in ranks_r], color='#8b949e', fontsize=10)

    year_label = f'{year}년 (YTD 4월)' if year == 2026 else f'{year}년'
    ax.set_title(f'NASDAQ Top {TOP_N}  —  {year_label}  (시가총액 기준)',
                 color='white', fontsize=15, fontweight='bold', pad=12, loc='left')
    ax.set_xlabel('시가총액 ($B)', color='#8b949e', fontsize=9)

    ax.set_facecolor(BG_AX)
    ax.tick_params(colors='#8b949e', length=0)
    ax.set_xlim(0, max_mc * 1.08)
    for spine in ax.spines.values():
        spine.set_edgecolor(COL_BORDER)
    ax.xaxis.label.set_color('#8b949e')
    ax.tick_params(axis='x', colors='#8b949e')

# 범례
leg_normal = mpatches.Patch(color=COL_NORMAL, label='일반 종목')
leg_para   = mpatches.Patch(color=COL_PARA,   label='★ 패러다임 진입종목 (신규 진입 후 2년 이상 유지)')
fig.legend(
    handles=[leg_normal, leg_para],
    loc='upper right', ncol=2,
    facecolor='#21262d', edgecolor=COL_BORDER,
    labelcolor='white', fontsize=11,
    framealpha=0.95, bbox_to_anchor=(0.99, 0.995)
)

plt.tight_layout(pad=2.5, h_pad=3.0)
plt.savefig(OUT_PATH, dpi=150, bbox_inches='tight', facecolor=BG_MAIN)
print(f"[완료] 저장: {OUT_PATH}")
