#!/usr/bin/env python3
"""
미장 시총 Top30 연도별 시각화 (DB + pandas)
1. 연도별 Top30 시총 테이블 → PNG (us_top30_table.png)
2. 연도별 막대그래프 → PNG (us_top30_{year}.png)
기준: 연말 마지막 거래일 (12월 아닐 경우 YTD 표기)
"""
import sys
sys.path.insert(0, '/data/frame')

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
import warnings
warnings.filterwarnings('ignore')

from run_etl import get_conn

OUT_DIR = '/data/frame/top30_yearly/'
import os
os.makedirs(OUT_DIR, exist_ok=True)

# ── 1. DB에서 연말 Top30 조회 ─────────────────────────────────────────────────
print("[1] DB에서 연말 Top30 데이터 조회 중...")
conn = get_conn()
cur  = conn.cursor()

cur.execute("""
    SELECT EXTRACT(YEAR FROM date_) AS yr,
           MAX(date_)               AS last_day
    FROM daily_marcap_us
    GROUP BY EXTRACT(YEAR FROM date_)
    ORDER BY yr
""")
year_info = {}
for r in cur.fetchall():
    yr       = int(r[0])
    last_day = str(r[1])[:10]
    month    = int(last_day[5:7])
    is_ytd   = (month != 12)
    year_info[yr] = (last_day, is_ytd)

records = []
for year, (last_day, is_ytd) in year_info.items():
    cur.execute("""
        SELECT m.code, COALESCE(t.name, m.code) AS name,
               m.marcap, m.rank
        FROM daily_marcap_us m
        LEFT JOIN ticker_master_us t ON t.code = m.code
        WHERE m.date_ = TO_DATE(?, 'YYYY-MM-DD') AND m.rank <= 30
        ORDER BY m.rank
    """, [last_day])
    for row in cur.fetchall():
        records.append({
            'year'    : year,
            'last_day': last_day,
            'is_ytd'  : is_ytd,
            'code'    : row[0],
            'name'    : row[1],
            'marcap'  : float(row[2]),
            'rank'    : int(row[3]),
        })

conn.close()

df = pd.DataFrame(records)
all_years = sorted(df['year'].unique())
print(f"  → 완성된 연도: {all_years}")
print(f"  → 총 {len(df)}개 레코드")

# ── 이름 정리 함수 ─────────────────────────────────────────────────────────────
def clean_name(name, max_len=28):
    n = name.split('.')[0].split('(')[0].strip()
    if len(n) > max_len:
        n = n[:max_len] + '..'
    return n

# ── 2. 테이블 이미지 저장 ──────────────────────────────────────────────────────
print("[2] 시총 테이블 이미지 생성 중...")

pivot_rows = []
for year in all_years:
    yr_df = df[df['year'] == year].sort_values('rank')
    _, is_ytd = year_info[year]
    col_label = f"{year}\n(YTD)" if is_ytd else str(year)
    for _, row in yr_df.iterrows():
        mc_b = row['marcap'] / 1e9
        pivot_rows.append({
            'rank' : row['rank'],
            'year' : col_label,
            'label': f"{clean_name(row['name'], 18)}\n${mc_b:,.0f}B"
        })

pivot_df = pd.DataFrame(pivot_rows).pivot(
    index='rank', columns='year', values='label'
).fillna('')

n_cols = len(all_years)
n_rows = 30
fig_w  = max(24, n_cols * 2.2)
fig_h  = max(18, n_rows * 0.62)

fig, ax = plt.subplots(figsize=(fig_w, fig_h))
fig.patch.set_facecolor('#0d1117')
ax.set_facecolor('#0d1117')
ax.axis('off')

tbl = ax.table(
    cellText  = pivot_df.values,
    rowLabels = [str(i) for i in range(1, n_rows + 1)],
    colLabels = pivot_df.columns.tolist(),
    cellLoc   = 'center',
    loc       = 'center',
)
tbl.auto_set_font_size(False)
tbl.set_fontsize(8.5)
tbl.scale(1.0, 2.1)

for (row, col), cell in tbl.get_celld().items():
    cell.set_edgecolor('#30363d')
    if row == 0:
        cell.set_facecolor('#21262d')
        cell.set_text_props(color='white', fontweight='bold')
    elif col == -1:
        cell.set_facecolor('#161b22')
        cell.set_text_props(color='#6e7681')
    else:
        cell.set_facecolor('#161b22')
        cell.set_text_props(color='white')

ax.set_title('US Market Cap Top 30  —  연도별 시총 순위 테이블',
             color='white', fontsize=15, fontweight='bold', pad=20)

tbl_path = f'{OUT_DIR}us_top30_table.png'
plt.savefig(tbl_path, dpi=130, bbox_inches='tight', facecolor='#0d1117')
plt.close()
print(f"  저장: {tbl_path}")

# ── 3. 연도별 막대그래프 ───────────────────────────────────────────────────────
print("[3] 연도별 막대그래프 생성 중...")

BG_MAIN    = '#0d1117'
BG_AX      = '#161b22'
COL_BAR    = '#4A90D9'
COL_BORDER = '#30363d'

years_desc = sorted(all_years, reverse=True)

for year in years_desc:
    yr_df = df[df['year'] == year].sort_values('rank', ascending=False).reset_index(drop=True)
    _, is_ytd = year_info[year]

    names   = [clean_name(n) for n in yr_df['name'].tolist()]
    marcaps = (yr_df['marcap'] / 1e9).tolist()
    ranks   = yr_df['rank'].tolist()

    fig, ax = plt.subplots(figsize=(22, 16))
    fig.patch.set_facecolor(BG_MAIN)

    bars = ax.barh(
        range(len(names)), marcaps,
        color=COL_BAR, height=0.78,
        alpha=0.92, edgecolor=BG_AX, linewidth=0.4
    )

    max_mc = max(marcaps) if marcaps else 1
    for bar, name, mc in zip(bars, names, marcaps):
        label = f'  {name}   ${mc:,.0f}B'
        ax.text(
            mc * 0.005 + max_mc * 0.003,
            bar.get_y() + bar.get_height() / 2,
            label,
            va='center', ha='left',
            color='white',
            fontsize=12,
        )

    ax.set_yticks(range(len(ranks)))
    ax.set_yticklabels([str(r) for r in ranks], color='#6e7681', fontsize=11)

    last_day   = year_info[year][0]
    year_label = f'{year}  (YTD {last_day[5:7]}월)' if is_ytd else str(year)
    ax.set_title(
        f'  US Market Cap Top 30  —  {year_label}',
        color='white', fontsize=18, fontweight='bold', pad=16, loc='left'
    )
    ax.set_xlabel('Market Cap ($B)', color='#6e7681', fontsize=11)
    ax.set_facecolor(BG_AX)
    ax.set_xlim(0, max_mc * 1.18)
    ax.tick_params(colors='#6e7681', length=0)
    for spine in ax.spines.values():
        spine.set_edgecolor(COL_BORDER)
    ax.xaxis.label.set_color('#6e7681')
    ax.tick_params(axis='x', colors='#6e7681')
    ax.xaxis.grid(True, color=COL_BORDER, linewidth=0.5, alpha=0.6)
    ax.set_axisbelow(True)

    plt.tight_layout(pad=2.0)
    out_path = f'{OUT_DIR}us_top30_{year}.png'
    plt.savefig(out_path, dpi=150, bbox_inches='tight', facecolor=BG_MAIN)
    plt.close()
    print(f"  저장: {out_path}")

print(f"\n[완료] 테이블 1개 + 막대그래프 {len(years_desc)}개 → {OUT_DIR}")
