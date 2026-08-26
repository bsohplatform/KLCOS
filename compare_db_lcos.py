#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESGC_Cost_Performance_Database_v2024 의 파라미터로 LCOS를 계산하고
DB에 저장된 참조 LCOS 값과 비교한다.  lcos.py 는 수정하지 않는다.
"""

import sys
import openpyxl
from collections import defaultdict

sys.path.insert(0, '.')

from lcos import (
    TechParams, CostParams, FinancialParams,
    CapitalCostItem,
    calculate_lcos, build_armo_and_decomm,
)

# ── DB 기술명 → lcos.py 코드 매핑 ────────────────────────────────
DB_TO_CODE = {
    'Lithium-ion LFP':    'LFP',
    'Lithium-ion NMC':    'NMC',
    'Vanadium Redox Flow':'VRF',
    'Zinc':               'ZINC',
    'Lead Acid':          'LEAD',
    'Hydrogen':           'H2',
    'PSH':                'PSH',
    'CAES':               'CAES',
    'Gravitational':              'GES',
    'Thermal':                    'THERMAL',
    'Retrofit Carnot (Concrete)': 'CARNOT_CONCRETE',
}


def load_db(db_path: str) -> dict:
    """DB 시트를 읽어 {(tech, yr, mw, hr, estimate): {(cat, param): value}} 반환"""
    wb = openpyxl.load_workbook(db_path, data_only=True)
    ws = wb['Database']
    combos: dict = defaultdict(dict)
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] is None:
            continue
        tech, yr, mw, hr, est, cat, param, value = row[:8]
        combos[(tech, yr, mw, hr, est)][(cat, param)] = value
    return combos


def _strip_unit(param_str: str):
    """'Parameter Name ($/kWh)' → ('Parameter Name', '$/kWh'), 없으면 None"""
    s = str(param_str)
    if s.endswith('($/kWh)'):
        return s[:-7].strip(), '$/kWh'
    if s.endswith('($/kW)'):
        return s[:-6].strip(), '$/kW'
    return None


def build_and_calc(key, params: dict, fin: FinancialParams = None):
    """
    DB params 딕셔너리 → TechParams / CostParams 생성 → calculate_lcos 실행.
    반환: (lcos_ref, result_dict)  또는  (lcos_ref, error_str)
    """
    db_tech, yr, power_mw, dur_hr, estimate = key
    tech_code = DB_TO_CODE.get(db_tech)
    if tech_code is None:
        return None, f"매핑 없음: {db_tech}"

    lcos_ref = params.get(('LCOS', 'LCOS ($/kWh)'))
    if lcos_ref is None:
        return None, "LCOS 참조값 없음"
    proj_life = int(params.get(('LCOS', 'LCOS_Project_Life (yrs)'), 20))

    if fin is None:
        fin = FinancialParams()

    # ── 성능 파라미터 ────────────────────────────────────────────
    rte            = float(params.get(('Performance', 'RTE (%)'),                    0.52))
    dod            = float(params.get(('Performance', 'Primary DOD (%)'),            0.80))
    rest_charge    = float(params.get(('Performance', 'Rest Before Charge (hrs)'),   0.0) or 0.0)
    rest_discharge = float(params.get(('Performance', 'Rest After Discharge (hrs)'), 0.0) or 0.0)
    cal_life           = float(params.get(('Performance', 'Calendar Life (yrs)'), proj_life))
    secondary_dod_v    = params.get(('Performance', 'Secondary DOD (%)'))
    secondary_dod      = float(secondary_dod_v) if secondary_dod_v is not None else None
    cycle_life_dod_v   = params.get(('Performance', 'Cycle Life at Primary DOD (#)'))
    cycle_life_sec_v   = params.get(('Performance', 'Cycle Life at Secondary DOD (#)'))
    cycle_limit_per_yr = 365.0

    # ── TechParams 생성 ──────────────────────────────────────────
    tech = TechParams(
        storage_type                = tech_code,
        power_mw                    = power_mw,
        duration_hr                 = dur_hr,
        rte_dc                      = rte,
        dod                         = dod,
        secondary_dod               = secondary_dod,
        rest_charge_hr              = rest_charge,
        rest_discharge_hr           = rest_discharge,
        calendar_life_yr            = cal_life,
        project_life_yr             = float(proj_life),
        cycle_limit_per_yr          = cycle_limit_per_yr,
        cycle_life_at_primary_dod   = float(cycle_life_dod_v)  if cycle_life_dod_v  else None,
        cycle_life_at_secondary_dod = float(cycle_life_sec_v)  if cycle_life_sec_v  else None,
    )

    # ── Capital Cost 항목 ─────────────────────────────────────────
    capital_items: list[CapitalCostItem] = []
    for (cat, param), value in params.items():
        if cat != 'Capital Cost' or value is None:
            continue
        if 'Total Installed Cost' in str(param):
            continue
        parsed = _strip_unit(param)
        if parsed is None:
            continue
        name, unit = parsed
        capital_items.append(CapitalCostItem(name, float(value), unit))

    # ── 고정 O&M ─────────────────────────────────────────────────
    fom = float(params.get(('O&M Cost', 'Fixed O&M ($/kW-year)'), 0.0) or 0.0)

    # ── 해체 비용 원가 (DB 읽기) ─────────────────────────────────
    decomm_base: list[CapitalCostItem] = []
    for (cat, param), value in params.items():
        if cat != 'Decommissioning' or value is None:
            continue
        parsed = _strip_unit(param)
        if parsed is None:
            continue
        name, unit = parsed
        # [Lead Acid] 재활용 산업이 성숙하여 납 회수(초기 비용의 10~12%)를 통한
        # 잔존가치가 재활용 비용과 상쇄됨. PNNL ESGC DB 참조 LCOS에 Recycling이
        # 미포함된 것을 DB 수치 검증으로 확인 → Lead Acid에 한해 제외.
        # 참고: PNNL ESGC Workbook v2024 §4.2.3.2 (Buchanan, 2021)
        #
        # [LFP] Recycling 포함. 회수 가능 금속이 적어 소유자가 순비용 부담.
        # 현재 $0.50~0.70/lb × 4.42 lb/kWh ≈ $2.21~3.09/kWh (DB: $2.39~2.92/kWh).
        # 2030년에는 중국 black mass 시장 발달로 $0 전망.
        # 참고: PNNL ESGC Workbook v2024 §4.2.3.2 (Hickley 2021, Kane 2021)
        #
        # [NMC] DB에 Recycling 항목 없음. 니켈·코발트 등 고가 금속 회수가로
        # 운송비가 상쇄되어 순비용 $0 → 포함할 비용 자체가 없음.
        # 참고: PNNL ESGC Workbook v2024 §4.2.3.2 (Hickley 2021, Kane 2021)
        #
        # [VRF] Recycling은 LCOS에 포함. 전해질 내 바나듐(V₂O₅) 회수가 핵심이며
        # 해체비는 초기 EPC의 역산(건설비 재발생 - 바나듐 회수금)으로 산정됨.
        # 리스 모델에서는 전해질을 개발사가 무상 회수하고, 스택 등 나머지는
        # 소유자 부담(잔존가치 없음). 순 재활용·폐기 비용: $36.50/kWh (10MW/4hr).
        # 참고: PNNL ESGC Workbook v2024 §4.3.3.1-2 (Vartanian 2021, Brown 2021)
        if 'Recycling' in name and tech_code == 'LEAD':
            continue
        decomm_base.append(CapitalCostItem(name, float(value), unit))

    # ── VRF Stack 교체 비용 (DB 'Replacement' 카테고리) ─────────────
    replacement_items: list[CapitalCostItem] = []
    for (cat, param), value in params.items():
        if cat != 'Replacement' or value is None:
            continue
        parsed = _strip_unit(param)
        if parsed is None:
            continue
        name, unit = parsed
        replacement_items.append(CapitalCostItem(name, float(value), unit))

    # ── ARMO / 해체 비용 / 보증 비용 (기술별 로직은 lcos.py 내부에서 결정) ───
    w_val   = params.get(('Warranty', 'Warranty ($/kWh)'))
    w_delay = int(params.get(('Warranty', 'Warranty Delay (yrs)'), 0) or 0)
    armo_items, decomm_items, warranty_items = build_armo_and_decomm(
        tech, capital_items, decomm_base, proj_life,
        replacement_items = replacement_items,
        warranty_per_kwh  = float(w_val) if w_val else 0.0,
        warranty_delay_yr = w_delay,
    )

    cost = CostParams(
        fixed_om_per_kw_yr = fom,
        capital_items      = capital_items,
        warranty_items     = warranty_items,
        armo_items         = armo_items,
        decomm_items       = decomm_items,
    )

    try:
        res = calculate_lcos(tech, cost, fin)
        return float(lcos_ref), res
    except Exception as e:
        return float(lcos_ref), str(e)


def main():
    DB_PATH = 'ESGC_Cost_Performance_Database_v2024.xlsx'
    combos  = load_db(DB_PATH)
    fin     = FinancialParams()   # Workbook 기본 재무 파라미터 (N=20)

    rows = []
    for key in sorted(combos.keys()):
        db_tech, yr, mw, hr, est = key
        if DB_TO_CODE.get(db_tech) is None:
            continue
        params       = combos[key]
        lcos_ref, result = build_and_calc(key, params, fin)
        if lcos_ref is None:
            continue

        if isinstance(result, dict):
            calc     = result['lcos']
            diff_pct = (calc - lcos_ref) / lcos_ref * 100
            rows.append((db_tech, yr, mw, hr, est, lcos_ref, calc, diff_pct, None))
        else:
            rows.append((db_tech, yr, mw, hr, est, lcos_ref, None, None, result))

    # ── 결과 출력 ─────────────────────────────────────────────────
    W = 108
    print()
    print("=" * W)
    print("  ESGC DB_v2024  파라미터 → LCOS 계산값  vs  DB 참조값")
    print("=" * W)
    print(f"  {'기술':<24} {'연도':>4} {'MW':>6} {'hr':>5}  {'추정':<6}"
          f"  {'DB LCOS':>8}  {'계산값':>8}  {'차이%':>7}")
    print("-" * W)

    prev_tech = None
    for row in rows:
        tech, yr, mw, hr, est, db_v, calc, diff, err = row
        if tech != prev_tech:
            if prev_tech is not None:
                print()
            prev_tech = tech
        if err:
            print(f"  {tech:<24} {yr:>4} {mw:>6.0f} {hr:>5.1f}  {est:<6}"
                  f"  {db_v:>8.4f}  {'ERROR':>8}  {'':>7}  [{err}]")
        else:
            flag = '  <-- !' if abs(diff) > 5 else ''
            print(f"  {tech:<24} {yr:>4} {mw:>6.0f} {hr:>5.1f}  {est:<6}"
                  f"  {db_v:>8.4f}  {calc:>8.4f}  {diff:>+7.2f}%{flag}")

    print("=" * W)
    valid = [(r[6], r[7]) for r in rows if r[6] is not None]
    if valid:
        diffs = [abs(d) for _, d in valid]
        print(f"\n  총 {len(valid)}개 케이스 | 평균 절대 오차: {sum(diffs)/len(diffs):.2f}%"
              f" | 최대 오차: {max(diffs):.2f}%")
    print()


if __name__ == '__main__':
    main()
