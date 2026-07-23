#!/usr/bin/env python3
"""
미장 시총 Top30 — 히트맵 (Heatmap)
행=종목, 열=연도, 색=순위 (1위=진한색, 30위=연한색)
"""
import sys
sys.path.insert(0, '/data/frame')

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

from run_etl import get_conn

OUT_PATH = '/data/frame/top30_yearly/us_heatmap.png'

# ── 1. DB 조회 ────────────────────────────────────────────────────────────────
print("[1] DB 조회 중...")
conn = get_conn()
cur  = conn.cursor()

cur.execute("""
    SELECT EXTRACT(YEAR FROM date_) AS yr, MAX(date_) AS last_day
    FROM daily_marcap_us
    GROUP BY EXTRACT(YEAR FROM date_)
    ORDER BY yr
""")
year_info = {}
for r in cur.fetchall():
    yr       = int(r[0])
    last_day = str(r[1])[:10]
    year_info[yr] = last_day

records = []
for year, last_day in year_info.items():
    cur.execute("""
        SELECT m.code, COALESCE(t.name, m.code) AS name, m.rank
        FROM daily_marcap_us m
        LEFT JOIN ticker_master_us t ON t.code = m.code
        WHERE m.date_ = TO_DATE(?, 'YYYY-MM-DD') AND m.rank <= 30
        ORDER BY m.rank
    """, [last_day])
    for row in cur.fetchall():
        records.append({
            'year': year,
            'code': row[0],
            'name': row[1],
            'rank': int(row[2]),
        })

conn.close()
df = pd.DataFrame(records)
all_years = sorted(df['year'].unique())

def clean_name(name, max_len=25):
    n = name.split('.')[0].split('(')[0].strip()
    return n[:max_len] + '..' if len(n) > max_len else n

df['name'] = df['name'].apply(clean_name)

# ── 2. 종목 정렬 — 최신 연도 순위 기준 ────────────────────────────────────────
latest_year = max(all_years)
latest_df   = df[df['year'] == latest_year][['code', 'name', 'rank']].sort_values('rank')

# 최신 연도 기준 정렬된 종목 리스트
ordered_codes = latest_df['code'].tolist()
ordered_names = latest_df['name'].tolist()

# 히스토리에만 있고 최신엔 없는 종목 추가 (Top30 밖으로 나간 종목)
all_codes = df['code'].unique()
extra_codes = [c for c in all_codes if c not in ordered_codes]
for ec in extra_codes:
    row = df[df['code'] == ec].iloc[0]
    ordered_codes.append(ec)
    ordered_names.append(clean_name(row['name']))

# ── 3. pivot 테이블 생성 (행=종목, 열=연도, 값=순위) ─────────────────────────
pivot = df.pivot_table(index='code', columns='year', values='rank', aggfunc='first')
pivot = pivot.reindex(ordered_codes)   # 최신 순위 기준 정렬

# NaN = 해당 연도 Top30 밖 → 31로 채움 (색상 구분용)
pivot_filled = pivot.fillna(31)

# ── 4. 히트맵 그리기 ──────────────────────────────────────────────────────────
print("[2] 히트맵 생성 중...")

BG_MAIN    = '#0d1117'
BG_AX      = '#161b22'
COL_BORDER = '#30363d'

n_stocks = len(ordered_codes)
n_years  = len(all_years)

fig, ax = plt.subplots(figsize=(n_years * 1.6 + 5, n_stocks * 0.55 + 3))
fig.patch.set_facecolor(BG_MAIN)
ax.set_facecolor(BG_AX)

# 색상: 1위=진한 파란색, 30위=연한색, Top30 밖=거의 투명
cmap = matplotlib.colormaps.get_cmap('YlOrRd_r')   # 진할수록 높은 순위

data_matrix = pivot_filled.values  # shape: (종목수, 연도수)

for i, code in enumerate(ordered_codes):
    for j, year in enumerate(all_years):
        rank_val = data_matrix[i][j]
        is_out   = (rank_val == 31)   # Top30 밖

        if is_out:
            color = '#1a1f27'
            text_color = '#3d444d'
            text = '—'
        else:
            norm_val = (31 - rank_val) / 30   # 1위=1.0, 30위≈0.03
            color    = cmap(norm_val)
            # 밝은 셀은 어두운 텍스트, 어두운 셀은 밝은 텍스트
            luminance  = 0.299*color[0] + 0.587*color[1] + 0.114*color[2]
            text_color = '#0d1117' if luminance > 0.5 else 'white'
            text       = str(int(rank_val))

        # 셀 사각형
        rect = plt.Rectangle([j - 0.5, i - 0.5], 1, 1,
                              facecolor=color, edgecolor=COL_BORDER,
                              linewidth=0.5)
        ax.add_patch(rect)

        # 순위 숫자
        ax.text(j, i, text,
                ha='center', va='center',
                fontsize=11, fontweight='bold',
                color=text_color)

# ── 5. 축 설정 ────────────────────────────────────────────────────────────────
ax.set_xlim(-0.5, n_years - 0.5)
ax.set_ylim(-0.5, n_stocks - 0.5)
ax.invert_yaxis()   # 1위 종목이 위쪽

# X축 — 연도
x_labels = []
for y in all_years:
    ld    = year_info[y]
    month = int(ld[5:7])
    x_labels.append(f'{y}\n(YTD)' if month != 12 else str(y))

ax.set_xticks(range(n_years))
ax.set_xticklabels(x_labels, color='#c9d1d9', fontsize=12)
ax.xaxis.set_ticks_position('top')
ax.xaxis.set_label_position('top')

# Y축 — 종목명
ax.set_yticks(range(n_stocks))
ax.set_yticklabels(ordered_names, color='#c9d1d9', fontsize=11)

ax.tick_params(length=0)
for spine in ax.spines.values():
    spine.set_edgecolor(COL_BORDER)

# 컬러바 범례
sm = plt.cm.ScalarMappable(cmap=cmap,
                            norm=mcolors.Normalize(vmin=1, vmax=30))
sm.set_array([])
cbar = fig.colorbar(sm, ax=ax, orientation='vertical',
                    fraction=0.015, pad=0.02)
cbar.set_label('순위 (1위 = 진한색)', color='#c9d1d9', fontsize=11)
cbar.ax.yaxis.set_tick_params(color='#6e7681')
cbar.ax.set_yticklabels(
    [str(int(t)) for t in cbar.get_ticks()],
    color='#c9d1d9', fontsize=10
)
cbar.outline.set_edgecolor(COL_BORDER)

ax.set_title('  US Market Cap Top 30  —  연도별 순위 히트맵\n'
             '  (진한색 = 높은 순위 / 회색 = 해당 연도 Top30 밖)',
             color='white', fontsize=16, fontweight='bold',
             pad=50, loc='left')

plt.tight_layout(pad=2.5)
plt.savefig(OUT_PATH, dpi=150, bbox_inches='tight', facecolor=BG_MAIN)
plt.close()
print(f"  저장: {OUT_PATH}")
print("[완료]")
