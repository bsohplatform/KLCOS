#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESGC DB vs 계산 LCOS 산점도 — 2행 레이아웃
compare_db_lcos.py 의 load_db / build_and_calc 를 호출한다.
"""

import sys, math
sys.path.insert(0, '.')

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from matplotlib.lines import Line2D

from lcos import FinancialParams, STORAGE_CATALOG
from compare_db_lcos import load_db, build_and_calc, DB_TO_CODE

# ── 설정 ──────────────────────────────────────────────────────
DB_PATH   = 'ESGC_Cost_Performance_Database_v2024.xlsx'
SAVE_PATH = 'lcos_scatter_all.png'

# DB 기술명 순서 (표시 순서 고정, 사용자 추가 기술 제외)
EXCLUDE_TECHS = {'Retrofit Carnot (Concrete)'}
DB_TECH_ORDER = [t for t in DB_TO_CODE.keys() if t not in EXCLUDE_TECHS]

# Duration → 색상·마커
DURATIONS  = [2.0, 4.0, 6.0, 8.0, 10.0, 24.0, 100.0]
_PALETTE   = ['#3B82F6','#10B981','#F59E0B','#EF4444',
              '#8B5CF6','#EC4899','#6B7280']
DUR_COLOR  = dict(zip(DURATIONS, _PALETTE))
DUR_MARKER = {2.0:'o', 4.0:'s', 6.0:'P', 8.0:'X',
              10.0:'^', 24.0:'D', 100.0:'*'}

# Estimate → 투명도
EST_ALPHA  = {'High': 0.95, 'Low': 0.95, 'Point': 0.95}

# 기술별 테두리 색 (카테고리)
CAT_COLOR = {
    'LITHIUM':     '#1D4ED8',
    'NON_LITHIUM': '#065F46',
    'HYDROGEN':    '#92400E',
    'OTHER':       '#4B5563',
}


# ── 데이터 수집 ───────────────────────────────────────────────
def collect(db_path: str, fin: FinancialParams) -> dict:
    combos = load_db(db_path)
    # {db_tech_name: {hr: {est: (x_list, y_list)}}}
    result: dict = {t: {} for t in DB_TECH_ORDER}

    for key in sorted(combos.keys()):
        db_tech, yr, mw, hr, est = key
        if db_tech not in result:
            continue
        params = combos[key]
        lcos_ref, res = build_and_calc(key, params, fin)
        if lcos_ref is None or not isinstance(res, dict):
            continue

        hr_d = result[db_tech].setdefault(hr, {'x': [], 'y': [], 'est': []})
        hr_d['x'].append(res['lcos'])
        hr_d['y'].append(lcos_ref)
        hr_d['est'].append(est)

    return result


# ── 메인 플롯 ─────────────────────────────────────────────────
def plot_scatter(data: dict, save_path: str):
    n     = len(DB_TECH_ORDER)
    ncols = math.ceil(n / 2)          # 기술 수에 따라 열 수 자동 결정
    nrows = 2

    fig, axes = plt.subplots(
        nrows, ncols,
        figsize=(3.8 * ncols, 4.2 * nrows),
        facecolor='#F8FAFC',
    )
    axes_flat = axes.flatten()

    plotted_hrs: set = set()

    for idx, db_tech in enumerate(DB_TECH_ORDER):
        ax        = axes_flat[idx]
        tech_code = DB_TO_CODE[db_tech]
        cat_name  = STORAGE_CATALOG[tech_code][1].name   # TechCategory name
        frame_col = CAT_COLOR.get(cat_name, '#4B5563')
        disp_name = STORAGE_CATALOG[tech_code][0]        # 표시명

        ax.set_facecolor('#FFFFFF')
        for sp in ax.spines.values():
            sp.set_linewidth(1.8)
            sp.set_edgecolor(frame_col)

        tech_data = data[db_tech]
        all_vals  = []

        for hr in sorted(tech_data.keys()):
            d      = tech_data[hr]
            color  = DUR_COLOR.get(hr, '#6B7280')
            marker = DUR_MARKER.get(hr, 'o')
            ax.scatter(
                d['x'], d['y'],
                color=color, marker=marker,
                s=45, alpha=0.88,
                edgecolors='white', linewidths=0.5,
                zorder=3,
            )
            all_vals += d['x'] + d['y']
            plotted_hrs.add(hr)

        if not all_vals:
            ax.set_visible(False)
            continue

        mn = min(all_vals)
        mx = max(all_vals)
        pad = (mx - mn) * 0.06 if mx > mn else 0.05
        lo, hi = mn - pad, mx + pad

        # Y=X 기준선
        ax.plot([lo, hi], [lo, hi], color='#94A3B8', lw=1.2,
                ls='--', zorder=1, label='Y = X')
        ax.set_xlim(lo, hi)
        ax.set_ylim(lo, hi)
        ax.set_aspect('equal', adjustable='box')

        # 눈금 포맷
        ax.xaxis.set_major_formatter(ticker.FormatStrFormatter('%.2f'))
        ax.yaxis.set_major_formatter(ticker.FormatStrFormatter('%.2f'))
        ax.xaxis.set_major_locator(ticker.MaxNLocator(4, prune='both'))
        ax.yaxis.set_major_locator(ticker.MaxNLocator(4, prune='both'))
        ax.tick_params(labelsize=7.5)
        ax.grid(True, alpha=0.25, color='#CBD5E1', linewidth=0.6)

        # 축 레이블 (외곽만)
        row, col = divmod(idx, ncols)
        if row == nrows - 1:
            ax.set_xlabel('Calculated ($/kWh)', fontsize=8, color='#475569')
        if col == 0:
            ax.set_ylabel('DB Reference ($/kWh)', fontsize=8, color='#475569')

        # 제목
        ax.set_title(disp_name, fontsize=9, fontweight='bold',
                     color=frame_col, pad=5)

        # MAPE + 케이스 수
        pairs = [(x, y)
                 for hr_d in tech_data.values()
                 for x, y in zip(hr_d['x'], hr_d['y'])]
        mape = (sum(abs(x-y)/y*100 for x,y in pairs) / len(pairs)
                if pairs else 0)
        ax.text(0.04, 0.96,
                f'MAPE = {mape:.2f}%\nn = {len(pairs)}',
                transform=ax.transAxes, fontsize=7.5,
                va='top', ha='left', color='#334155',
                bbox=dict(boxstyle='round,pad=0.3', fc='#F1F5F9',
                          ec='#CBD5E1', lw=0.6))

    # 빈 subplot 숨기기
    for ax in axes_flat[n:]:
        ax.set_visible(False)

    # ── 공통 범례 (Duration) ──────────────────────────────────
    legend_handles = []
    for hr in sorted(plotted_hrs):
        h = Line2D([0], [0],
                   marker=DUR_MARKER.get(hr, 'o'),
                   color='w',
                   markerfacecolor=DUR_COLOR.get(hr, '#6B7280'),
                   markersize=7,
                   label=f'{hr:.0f} hr')
        legend_handles.append(h)
    legend_handles.append(
        Line2D([0], [0], color='#94A3B8', lw=1.2, ls='--', label='Y = X')
    )

    # 카테고리 색 범례
    cat_handles = []
    cat_labels  = {
        'LITHIUM':     'Lithium Battery',
        'NON_LITHIUM': 'Non-Lithium Battery',
        'HYDROGEN':    'Hydrogen',
        'OTHER':       'Other',
    }
    for cat, col in CAT_COLOR.items():
        from matplotlib.patches import Patch
        cat_handles.append(Patch(facecolor='white', edgecolor=col,
                                 linewidth=2, label=cat_labels[cat]))

    leg1 = fig.legend(handles=legend_handles,
                      title='Storage Duration', title_fontsize=8,
                      fontsize=8, loc='lower center',
                      bbox_to_anchor=(0.38, -0.04),
                      ncol=len(legend_handles), framealpha=0.95,
                      edgecolor='#CBD5E1')
    fig.legend(handles=cat_handles,
               title='Tech Category (frame color)', title_fontsize=8,
               fontsize=8, loc='lower center',
               bbox_to_anchor=(0.78, -0.04),
               ncol=2, framealpha=0.95,
               edgecolor='#CBD5E1')
    fig.add_artist(leg1)

    fig.suptitle(
        'Calculated LCOS  vs  DB Reference LCOS   (Y = X line)',
        fontsize=12, fontweight='bold', color='#1E293B', y=1.01,
    )
    plt.tight_layout(h_pad=1.8, w_pad=1.2)
    plt.savefig(save_path, dpi=160, bbox_inches='tight',
                facecolor='#F8FAFC')
    print(f'저장: {save_path}  ({nrows}×{ncols} grid, {n}개 기술)')
    plt.close()


# ── 진입점 ────────────────────────────────────────────────────
if __name__ == '__main__':
    fin  = FinancialParams()
    data = collect(DB_PATH, fin)
    plot_scatter(data, SAVE_PATH)
