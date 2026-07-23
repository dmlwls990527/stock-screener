#!/usr/bin/env python3
"""
미장 시총 Top30 — 범프 차트 (Bump Chart)
전체 회색 배경 + 주목 종목만 컬러 강조
"""
import sys
sys.path.insert(0, '/data/frame')

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
import pandas as pd
import warnings
warnings.filterwarnings('ignore')

from run_etl import get_conn

OUT_PATH = '/data/frame/top30_yearly/us_bump_chart.png'

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

def clean_name(name, max_len=22):
    n = name.split('.')[0].split('(')[0].strip()
    return n[:max_len] + '..' if len(n) > max_len else n

df['name'] = df['name'].apply(clean_name)

# ── 2. 강조할 종목 지정 (스토리가 있는 종목) ──────────────────────────────────
# 순위 급상승 / 급하락 / 새로 진입한 종목 위주
HIGHLIGHT = {
    'NVDA': ('#FF6B35', 'Nvidia'),       # AI 급부상
    'AAPL': ('#4A90D9', 'Apple'),        # 꾸준한 1위권
    'MSFT': ('#2ECC71', 'Microsoft'),    # 클라우드로 상승
    'META': ('#E74C3C', 'Meta'),         # 추락 후 반등
    'LLY':  ('#F39C12', 'Eli Lilly'),    # 바이오 급부상
    'TSLA': ('#9B59B6', 'Tesla'),        # 급등 후 하락
    'INTC': ('#95A5A6', 'Intel'),        # 꾸준한 하락
    'AVGO': ('#1ABC9C', 'Broadcom'),     # 반도체 상승
}

# ── 3. 범프 차트 그리기 ───────────────────────────────────────────────────────
print("[2] 범프 차트 생성 중...")

BG_MAIN    = '#0d1117'
BG_AX      = '#161b22'
COL_BORDER = '#30363d'

fig, ax = plt.subplots(figsize=(28, 20))
fig.patch.set_facecolor(BG_MAIN)
ax.set_facecolor(BG_AX)

# ① 전체 종목 회색 얇은 선 (배경)
for code, grp in df.groupby('code'):
    if code in HIGHLIGHT:
        continue
    grp = grp.sort_values('year')
    ax.plot(grp['year'], grp['rank'],
            color='#2d333b', linewidth=1.2, alpha=0.5, zorder=1)

# ② 강조 종목 컬러 굵은 선
for code, (col, label) in HIGHLIGHT.items():
    grp = df[df['code'] == code].sort_values('year')
    if grp.empty:
        continue

    ax.plot(grp['year'], grp['rank'],
            color=col, linewidth=3.0, alpha=0.95, zorder=3,
            marker='o', markersize=8, markerfacecolor=col,
            markeredgecolor=BG_AX, markeredgewidth=1.5)

    # 각 연도 순위 숫자 표시
    for _, row in grp.iterrows():
        ax.text(row['year'], row['rank'] - 0.55,
                str(int(row['rank'])),
                ha='center', va='bottom',
                color=col, fontsize=8, fontweight='bold',
                path_effects=[pe.withStroke(linewidth=2, foreground=BG_AX)],
                zorder=4)

    # 왼쪽 이름 라벨
    first = grp.iloc[0]
    ax.text(first['year'] - 0.2, first['rank'],
            label,
            ha='right', va='center',
            color=col, fontsize=13, fontweight='bold',
            path_effects=[pe.withStroke(linewidth=3, foreground=BG_AX)],
            zorder=5)

    # 오른쪽 이름 라벨
    last = grp.iloc[-1]
    ax.text(last['year'] + 0.2, last['rank'],
            label,
            ha='left', va='center',
            color=col, fontsize=13, fontweight='bold',
            path_effects=[pe.withStroke(linewidth=3, foreground=BG_AX)],
            zorder=5)

# ── 4. 축 / 스타일 ────────────────────────────────────────────────────────────
ax.set_ylim(31, 0)
ax.set_yticks(range(1, 31))
ax.set_yticklabels([str(i) for i in range(1, 31)],
                   color='#6e7681', fontsize=12)
ax.set_ylabel('Rank', color='#6e7681', fontsize=13)

x_labels = []
for y in all_years:
    ld    = year_info[y]
    month = int(ld[5:7])
    x_labels.append(f'{y}\n(YTD)' if month != 12 else str(y))

ax.set_xticks(all_years)
ax.set_xticklabels(x_labels, color='#c9d1d9', fontsize=13)
ax.set_xlim(min(all_years) - 1.8, max(all_years) + 1.8)

ax.set_title('  US Market Cap Top 30  —  연도별 순위 변화 (Bump Chart)',
             color='white', fontsize=20, fontweight='bold', pad=20, loc='left')

ax.yaxis.grid(True, color=COL_BORDER, linewidth=0.6, alpha=0.5)
ax.xaxis.grid(True, color=COL_BORDER, linewidth=0.6, alpha=0.3)
ax.set_axisbelow(True)
ax.tick_params(colors='#6e7681', length=0)
for spine in ax.spines.values():
    spine.set_edgecolor(COL_BORDER)

# 범례
from matplotlib.lines import Line2D
legend_items = [
    Line2D([0], [0], color=col, linewidth=3, marker='o', markersize=7, label=label)
    for code, (col, label) in HIGHLIGHT.items()
]
legend_items.append(
    Line2D([0], [0], color='#2d333b', linewidth=1.5, label='기타 종목')
)
ax.legend(handles=legend_items,
          loc='lower left', fontsize=12,
          facecolor='#21262d', edgecolor=COL_BORDER,
          labelcolor='white', framealpha=0.95)

plt.tight_layout(pad=2.5)
plt.savefig(OUT_PATH, dpi=150, bbox_inches='tight', facecolor=BG_MAIN)
plt.close()
print(f"  저장: {OUT_PATH}")
print("[완료]")
