#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LCOS Calculator  — Flask Web Application
lcos.py 를 백엔드 엔진으로 사용하는 웹 UI.
"""

import os
import json
import traceback
from flask import Flask, render_template, request, jsonify

from lcos import (
    TechParams, CostParams, FinancialParams,
    CapitalCostItem, STORAGE_CATALOG, CAPITAL_CATALOG,
    calculate_lcos, build_armo_and_decomm,
)
from compare_db_lcos import load_db as _load_db, DB_TO_CODE

app = Flask(__name__)

_DB_PATH   = os.path.join(os.path.dirname(__file__), 'ESGC_Cost_Performance_Database_v2024.xlsx')
_db_cache  = None


def _get_db():
    global _db_cache
    if _db_cache is None:
        _db_cache = _load_db(_DB_PATH) if os.path.exists(_DB_PATH) else {}
    return _db_cache


def _strip_unit(param_str: str):
    s = str(param_str)
    if s.endswith('($/kWh)'):
        return s[:-7].strip(), '$/kWh'
    if s.endswith('($/kW)'):
        return s[:-6].strip(), '$/kW'
    return None


# ── 페이지 ─────────────────────────────────────────────────────

@app.route('/')
def index():
    techs = {
        code: {'name': name, 'category': cat.value, 'macrs': macrs}
        for code, (name, cat, macrs) in STORAGE_CATALOG.items()
    }
    capital_schema = {
        code: [{'name': f.name, 'unit': f.unit} for f in fields]
        for code, fields in CAPITAL_CATALOG.items()
    }
    # DB_TO_CODE 역방향: code → db_tech_name
    code_to_db = {v: k for k, v in DB_TO_CODE.items()}
    return render_template(
        'index.html',
        techs           = json.dumps(techs,          ensure_ascii=False),
        db_to_code      = json.dumps(DB_TO_CODE,      ensure_ascii=False),
        code_to_db      = json.dumps(code_to_db,      ensure_ascii=False),
        capital_schema  = json.dumps(capital_schema,  ensure_ascii=False),
        db_available    = os.path.exists(_DB_PATH),
    )


# ── DB API ──────────────────────────────────────────────────────

@app.route('/api/db_options')
def db_options():
    combos  = _get_db()
    options = {}
    for (tech, yr, mw, hr, est) in combos:
        o = options.setdefault(tech, {
            'years': set(), 'powers': set(), 'durations': set(), 'estimates': set()
        })
        o['years'].add(yr); o['powers'].add(mw)
        o['durations'].add(hr); o['estimates'].add(est)
    for tech in options:
        for k in ('years', 'powers', 'durations', 'estimates'):
            options[tech][k] = sorted(options[tech][k])
    return jsonify(options)


@app.route('/api/load_from_db', methods=['POST'])
def load_from_db():
    d       = request.json
    tech_db = d['tech']
    yr      = int(d['year'])
    mw      = float(d['power_mw'])
    hr      = float(d['duration_hr'])
    est     = d['estimate']

    combos = _get_db()
    params = combos.get((tech_db, yr, mw, hr, est))
    if params is None:
        return jsonify({'error': f'DB에 데이터 없음: {(tech_db, yr, mw, hr, est)}'}), 404

    tech_code = DB_TO_CODE.get(tech_db)

    def gp(cat, param, default=None):
        v = params.get((cat, param))
        return v if v is not None else default

    def collect_items(cat_filter, skip_recycling_lead=False):
        out = []
        for (cat, param), value in params.items():
            if cat != cat_filter or value is None:
                continue
            if cat == 'Capital Cost' and 'Total Installed Cost' in str(param):
                continue
            parsed = _strip_unit(param)
            if not parsed:
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
            if skip_recycling_lead and 'Recycling' in name and tech_code == 'LEAD':
                continue
            out.append({'name': name, 'value': float(value), 'unit': unit})
        return out

    cycle_prim = gp('Performance', 'Cycle Life at Primary DOD (#)')
    cycle_sec  = gp('Performance', 'Cycle Life at Secondary DOD (#)')
    sec_dod    = gp('Performance', 'Secondary DOD (%)')
    w_val      = gp('Warranty', 'Warranty ($/kWh)')
    w_delay    = int(gp('Warranty', 'Warranty Delay (yrs)', 0) or 0)

    return jsonify({
        'tech_code': tech_code,
        'tech_params': {
            'storage_type':               tech_code,
            'power_mw':                   mw,
            'duration_hr':                hr,
            'rte_dc':                     float(gp('Performance', 'RTE (%)', 0.85)),
            'dod':                        float(gp('Performance', 'Primary DOD (%)', 0.80)),
            'secondary_dod':              float(sec_dod) if sec_dod is not None else '',
            'rest_charge_hr':             float(gp('Performance', 'Rest Before Charge (hrs)', 0) or 0),
            'rest_discharge_hr':          float(gp('Performance', 'Rest After Discharge (hrs)', 0) or 0),
            'calendar_life_yr':           float(gp('Performance', 'Calendar Life (yrs)', 60)),
            'project_life_yr':            int(gp('LCOS', 'LCOS_Project_Life (yrs)', 20)),
            'cycle_limit_per_yr':         365.0,
            'cycle_life_at_primary_dod':  float(cycle_prim) if cycle_prim else '',
            'cycle_life_at_secondary_dod': float(cycle_sec)  if cycle_sec  else '',
        },
        'capital_items':     collect_items('Capital Cost'),
        'decomm_items':      collect_items('Decommissioning', skip_recycling_lead=True),
        'replacement_items': collect_items('Replacement'),
        'fom':               float(gp('O&M Cost', 'Fixed O&M ($/kW-year)', 0) or 0),
        'warranty_per_kwh':  float(w_val) if w_val else 0.0,
        'warranty_delay_yr': w_delay,
    })


# ── 계산 API ───────────────────────────────────────────────────

@app.route('/api/calculate', methods=['POST'])
def calculate():
    d = request.json
    try:
        tech = TechParams(
            storage_type  = d['storage_type'],
            power_mw      = float(d['power_mw']),
            duration_hr   = float(d['duration_hr']),
            rte_dc        = float(d['rte_dc']),
            dod           = float(d['dod']),
            secondary_dod = float(d['secondary_dod']) if d.get('secondary_dod') not in ('', None) else None,
            rest_charge_hr    = float(d.get('rest_charge_hr')    or 0),
            rest_discharge_hr = float(d.get('rest_discharge_hr') or 0),
            calendar_life_yr  = float(d.get('calendar_life_yr')  or 60),
            project_life_yr   = float(d.get('project_life_yr')   or 20),
            cycle_limit_per_yr= float(d.get('cycle_limit_per_yr')or 365),
            cycle_life_at_primary_dod   = float(d['cycle_life_at_primary_dod'])   if d.get('cycle_life_at_primary_dod')   not in ('', None) else None,
            cycle_life_at_secondary_dod = float(d['cycle_life_at_secondary_dod']) if d.get('cycle_life_at_secondary_dod') not in ('', None) else None,
        )

        def parse_items(raw):
            return [CapitalCostItem(it['name'], float(it['value']), it['unit'])
                    for it in (raw or []) if str(it.get('value', '')).strip()]

        capital_items     = parse_items(d.get('capital_items'))
        decomm_base       = parse_items(d.get('decomm_items'))
        replacement_items = parse_items(d.get('replacement_items'))
        proj_life         = int(tech.project_life_yr)

        armo_items, decomm_items, warranty_items = build_armo_and_decomm(
            tech, capital_items, decomm_base, proj_life,
            replacement_items = replacement_items,
            warranty_per_kwh  = float(d.get('warranty_per_kwh')  or 0),
            warranty_delay_yr = int(d.get('warranty_delay_yr')   or 0),
        )

        fin = FinancialParams(
            inflation             = float(d.get('inflation',             0.028)),
            interest_rate_nominal = float(d.get('interest_rate_nominal', 0.08)),
            coe_nominal           = float(d.get('coe_nominal',           0.13)),
            debt_fraction         = float(d.get('debt_fraction',         0.5)),
            tax_rate              = float(d.get('tax_rate',              0.257)),
            property_tax_rate     = float(d.get('property_tax_rate',    0.0084)),
            insurance_rate        = float(d.get('insurance_rate',        0.004)),
            analysis_period_n     = int(d.get('analysis_period_n',      20)),
            om_escalation_real    = float(d.get('om_escalation_real',    0.02)),
            prevailing_wage  = bool(d.get('prevailing_wage',  False)),
            energy_community = bool(d.get('energy_community', False)),
            domestic_content = bool(d.get('domestic_content', False)),
        )

        cost = CostParams(
            fixed_om_per_kw_yr       = float(d.get('fixed_om_per_kw_yr')      or 0),
            variable_om_per_kwh      = float(d.get('variable_om_per_kwh')     or 0),
            electricity_cost_per_kwh = float(d.get('electricity_cost_per_kwh')or 0.03),
            capital_items   = capital_items,
            warranty_items  = warranty_items,
            armo_items      = armo_items,
            decomm_items    = decomm_items,
        )

        res = calculate_lcos(tech, cost, fin)

        def sched(items):
            return [{'name': it.name, 'cost': round(it.cost), 'years': it.years}
                    for it in items]

        ann = res['annualized_occ'] + res['annualized_fom'] + res['annualized_vom'] \
            + res['annualized_ecc'] + res['annualized_warranty'] \
            + res['annualized_armo'] + res['annualized_decomm']

        return jsonify({
            'lcos':     round(res['lcos'],     6),
            'lcos_rte': round(res['lcos_rte'], 6),
            'aeo':      round(res['aeo']),
            'arr':      round(res['arr']),
            'pv_rv':    round(res['pv_rv']),
            'fcr':      round(res['fcr'],  6),
            'itc':      round(res['itc'],  4),
            'wacc_nom': round(res['wacc_nominal'], 6),
            'wacc_real':round(res['wacc_real'],    6),
            'crf':      round(res['crf'],  6),
            'occ':      round(res['occ_total']),
            'annualized': {
                'OCC':     round(res['annualized_occ']),
                'FOM':     round(res['annualized_fom']),
                'VOM':     round(res['annualized_vom']),
                'ECC':     round(res['annualized_ecc']),
                'Warranty':round(res['annualized_warranty']),
                'ARMO':    round(res['annualized_armo']),
                'Decomm':  round(res['annualized_decomm']),
                'Total':   round(ann),
            },
            'npv': {
                'FOM':     round(res['npv_fom']),
                'VOM':     round(res['npv_vom']),
                'ECC':     round(res['npv_ecc']),
                'Warranty':round(res['npv_warranty']),
                'ARMO':    round(res['npv_armo']),
                'Decomm':  round(res['npv_decomm']),
            },
            'schedules': {
                'armo':     sched(cost.armo_items),
                'warranty': sched(cost.warranty_items),
                'decomm':   sched(cost.decomm_items),
            },
            'aeo_detail': res['aeo_detail'],
        })
    except Exception as e:
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 400


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
