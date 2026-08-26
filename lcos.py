#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LCOS (Levelized Cost of Storage) 계산기
ESGC LCOS Workbook v2024 (PNNL) 구현
"""

import argparse
import sys
import io
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


# ── 기술 카탈로그 ─────────────────────────────────────────────

class TechCategory(Enum):
    LITHIUM     = "리튬배터리"
    NON_LITHIUM = "논리튬배터리"
    HYDROGEN    = "수소"
    OTHER       = "그외 기술"

# 코드: (표시명, 카테고리, MACRS 기간)
# 배터리 계열(전기화학): MACRS 7년 / 기계·기타: MACRS 20년
STORAGE_CATALOG = {
    # 리튬배터리
    "LFP":    ("Lithium-ion LFP",    TechCategory.LITHIUM,      7),
    "NMC":    ("Lithium-ion NMC",    TechCategory.LITHIUM,      7),
    # 논리튬배터리
    "VRF":    ("Vanadium Redox Flow", TechCategory.NON_LITHIUM,  7),
    "ZINC":   ("Zinc",                TechCategory.NON_LITHIUM,  7),
    "LEAD":   ("Lead Acid",           TechCategory.NON_LITHIUM,  7),
    # 수소
    "H2":     ("Hydrogen",            TechCategory.HYDROGEN,    20),
    # 그외 기술
    "PSH":    ("PSH",                 TechCategory.OTHER,       20),
    "CAES":   ("CAES",                TechCategory.OTHER,       20),
    "GES":    ("Gravitational",       TechCategory.OTHER,       20),
    "THERMAL":         ("Thermal",                   TechCategory.OTHER, 20),
    "CARNOT_CONCRETE": ("Retrofit Carnot (Concrete)", TechCategory.OTHER, 20),
}


# ── MACRS 감가상각 일정 ───────────────────────────────────────

MACRS = {
    3:  [0.3333, 0.4445, 0.1481, 0.0741],
    5:  [0.2000, 0.3200, 0.1920, 0.1152, 0.1152, 0.0576],
    7:  [0.1429, 0.2449, 0.1749, 0.1249, 0.0893, 0.0892, 0.0893, 0.0446],
    10: [0.1000, 0.1800, 0.1440, 0.1152, 0.0922, 0.0737, 0.0655, 0.0655, 0.0656, 0.0655, 0.0328],
    15: [0.0500, 0.0950, 0.0855, 0.0770, 0.0693, 0.0623, 0.0590, 0.0590, 0.0591, 0.0590,
         0.0591, 0.0590, 0.0591, 0.0590, 0.0591, 0.0295],
    20: [0.0375, 0.07219, 0.06677, 0.06177, 0.05713, 0.05285, 0.04888, 0.04522,
         0.04462, 0.04461, 0.04462, 0.04461, 0.04462, 0.04461, 0.04462, 0.04461,
         0.04462, 0.04461, 0.04462, 0.04461, 0.02231],
}


# ── Dataclass 정의 ────────────────────────────────────────────
@dataclass
class CapitalCostField:
    """OCC 항목 정의 — 이름과 단위만 (수치 없음)"""
    name: str
    unit: str   # '$/kW' 또는 '$/kWh'


@dataclass
class CapitalCostItem:
    """초기투자비(OCC) 구성 항목 — 수치 포함"""
    name:  str
    value: float
    unit:  str   # '$/kW' 또는 '$/kWh'

    def to_total(self, power_mw: float, duration_hr: float) -> float:
        power_kw = power_mw * 1000
        if self.unit == '$/kW':
            return self.value * power_kw
        elif self.unit == '$/kWh':
            return self.value * power_kw * duration_hr
        raise ValueError(f"지원하지 않는 단위: {self.unit}")


@dataclass
class ScheduledCostItem:
    """특정 연도에 일시 발생하는 비용 항목"""
    name:  str
    cost:  float       # 1회 발생 비용 ($)
    years: list[int]   # 발생 연도 목록


@dataclass
class TechParams:
    """기술 파라미터 (ESGC Workbook v2024 기본값)"""
    storage_type:       str   = "LFP"   # STORAGE_CATALOG 키
    power_mw:           float = 100.0   # 정격 출력 (MW)
    duration_hr:        float = 4.0     # 저장 지속시간 (hr)
    rte_dc:             float = 0.52    # DC 왕복효율 (J22, 전력변환 손실 제외)
    dod:                float = 0.80    # 1차 방전심도 (Primary DOD)
    secondary_dod:              Optional[float] = None  # 2차 방전심도 (LFP/NMC 전용, None이면 미해당)
    rest_charge_hr:             float = 0.0     # 충전 후 휴지시간 (hr)
    rest_discharge_hr:          float = 0.0     # 방전 후 휴지시간 (hr)
    calendar_life_yr:           float = 60.0    # 달력 수명 (년)
    project_life_yr:            float = 60.0    # 사업 기간 (년)
    cycle_limit_per_yr:         float = 365.0   # 연간 사이클 제한 (100% DOD 기준)
    max_cycles_dod:             Optional[float] = None  # DOD 기준 최대 사이클 수
    cycle_life_at_primary_dod:  Optional[float] = None  # 1차 DOD 기준 사이클 수명
    cycle_life_at_secondary_dod: Optional[float] = None  # 2차 DOD 기준 사이클 수명 (LFP/NMC)
    macrs_period:       Optional[int]   = None  # None → storage_type에서 자동 결정
    # 전력변환 효율 (PNNL Assumptions & Parameters 기본값, 단방향 one-way)
    bidirectional_inverter_efficiency:    float = 0.98
    bidirectional_transformer_efficiency: float = 0.98
    unidirectional_inverter_efficiency:   float = 0.98  # H2 방전(인버터)
    rectifier_efficiency:                 float = 0.98  # H2 충전(정류기)

    def __post_init__(self):
        if self.storage_type not in STORAGE_CATALOG:
            raise ValueError(
                f"알 수 없는 기술 코드: '{self.storage_type}'.\n"
                f"선택 가능: {list(STORAGE_CATALOG)}"
            )
        if self.macrs_period is None:
            self.macrs_period = STORAGE_CATALOG[self.storage_type][2]

    @property
    def rte(self) -> float:
        """AC-AC 시스템 왕복효율 (PNNL I98 공식)
        Battery : rte_dc × bi_inv_eff² × bi_trans_eff²
        Hydrogen: rte_dc × (uni_inv_eff × rectifier_eff) × bi_trans_eff²
        Other   : rte_dc (PSH/CAES 등은 이미 AC-AC)
        """
        bi_inv_sq   = self.bidirectional_inverter_efficiency   ** 2
        bi_trans_sq = self.bidirectional_transformer_efficiency ** 2
        cat = self.category
        if cat in (TechCategory.LITHIUM, TechCategory.NON_LITHIUM):
            return self.rte_dc * bi_inv_sq * bi_trans_sq
        elif cat == TechCategory.HYDROGEN:
            return self.rte_dc * (self.unidirectional_inverter_efficiency * self.rectifier_efficiency) * bi_trans_sq
        else:
            return self.rte_dc

    @property
    def display_name(self) -> str:
        return STORAGE_CATALOG[self.storage_type][0]

    @property
    def category(self) -> TechCategory:
        return STORAGE_CATALOG[self.storage_type][1]


@dataclass
class CostParams:
    """비용 파라미터 (ESGC Workbook v2024 기본값)"""
    occ_per_kw:               float = 1221.66  # 초기투자비 ($/kW) — capital_items 제공 시 무시됨
    fixed_om_per_kw_yr:       float = 18.72    # 고정 O&M ($/kW-yr)
    variable_om_per_kwh:      float = 0.0      # 변동 O&M ($/kWh)
    electricity_cost_per_kwh: float = 0.03     # 전력 구매단가 ($/kWh)
    capital_items:  list[CapitalCostItem] = field(default_factory=list)  # OCC 구성 항목
    warranty_items: list[ScheduledCostItem]    = field(default_factory=list)  # 보증비용 항목
    armo_items:     list[ScheduledCostItem]    = field(default_factory=list)  # ARMO 비용 항목
    decomm_items:   list[ScheduledCostItem]    = field(default_factory=list)  # 해체비용 항목


@dataclass
class FinancialParams:
    """재무 파라미터 (ESGC Workbook v2024 기본값)"""
    inflation:              float = 0.028   # 인플레이션율
    interest_rate_nominal:  float = 0.08    # 명목 이자율 (부채)
    coe_nominal:            float = 0.13    # 자기자본비용 (명목)
    debt_fraction:          float = 0.5     # 부채 비율
    tax_rate:               float = 0.257   # 세율 (연방+주)
    property_tax_rate:      float = 0.0084  # 재산세율
    insurance_rate:         float = 0.004   # 보험료율
    analysis_period_n:      int   = 20      # 분석 기간 (년)
    om_escalation_real:     float = 0.02    # O&M 실질 상승률
    prevailing_wage:        bool  = False   # 우대 임금 요건 충족 여부
    energy_community:       bool  = False   # 에너지 커뮤니티 가산 여부
    domestic_content:       bool  = False   # 국내산 콘텐츠 가산 여부


# ── 기술별 OCC 항목 스키마 (ESGC Database 2023 기준) ─────────
# 수치 없이 항목명과 단위만 정의 — 실제 값은 CapitalCostItem으로 입력

_KW  = '$/kW'
_KWH = '$/kWh'

_BATTERY_ITEMS = [
    CapitalCostField('DC Storage Block',    _KWH),
    CapitalCostField('DC Storage BOS',      _KWH),
    CapitalCostField('EPC',                 _KWH),
    CapitalCostField('Project Development', _KWH),
    CapitalCostField('Systems Integration', _KWH),
    CapitalCostField('Power Equipment',     _KW),
    CapitalCostField('Grid Integration',    _KW),
    CapitalCostField('C&C',                 _KW),
]

_THERMAL_ITEMS = [
    CapitalCostField('Thermal Capital (SB + BOS)',                               _KWH),
    CapitalCostField('Combined EPC Fee, Project Development & Grid Integration', _KWH),
    CapitalCostField('Power Equipment',                                          _KW),
]

_CARNOT_CONCRETE_ITEMS = [
    CapitalCostField('Storage Block',        _KWH),
    CapitalCostField('Balance of Storage',   _KWH),
    CapitalCostField('Power Equipment',      _KW),
    CapitalCostField('EPC',                  _KWH),
    CapitalCostField('Project Development',  _KWH),
    CapitalCostField('Grid Integration',     _KW),
]

CAPITAL_CATALOG: dict[str, list[CapitalCostField]] = {
    'LFP':  _BATTERY_ITEMS,
    'NMC':  _BATTERY_ITEMS,
    'LEAD': _BATTERY_ITEMS,
    'VRF':  _BATTERY_ITEMS,
    'ZINC': _BATTERY_ITEMS,
    'H2': [
        CapitalCostField('HESS Electrolyzer',  _KW),
        CapitalCostField('HESS Fuel Cell',     _KW),
        CapitalCostField('HESS Compressor',    _KW),
        CapitalCostField('HESS Inverter',      _KW),
        CapitalCostField('HESS Rectifier',     _KW),
        CapitalCostField('Grid Integration',   _KW),
        CapitalCostField('C&C',                _KW),
        CapitalCostField('Cavern Storage',     _KWH),
    ],
    'CAES': [
        CapitalCostField('CAES Capital (Turbine, Compressor, BOP & EPC)', _KW),
        CapitalCostField('Cavern Storage',                                 _KWH),
    ],
    'PSH': [
        CapitalCostField('PSH Electromechanical',                            _KW),
        CapitalCostField('PSH Powerhouse Construction & Infrastructure',      _KW),
        CapitalCostField('PSH EPC Contractor Indirect Costs + Contingency',   _KW),
        CapitalCostField('PSH Reservoir Construction & Infrastructure',       _KWH),
    ],
    'GES': [
        CapitalCostField('Gravitational Capital (SB + BOS)',                      _KWH),
        CapitalCostField('Combined Project Development & Grid Integration',        _KWH),
        CapitalCostField('Power Equipment',                                        _KW),
    ],
    'THERMAL':         _THERMAL_ITEMS,
    'CARNOT_CONCRETE': _CARNOT_CONCRETE_ITEMS,
}


# ── Capital Cost 항목 Enum (자동완성 지원) ─────────────────────

class BatteryCapital(Enum):
    """LFP, NMC, LEAD, VRF, ZINC 공통"""
    DC_STORAGE_BLOCK    = 'DC Storage Block'
    DC_STORAGE_BOS      = 'DC Storage BOS'
    EPC                 = 'EPC'
    PROJECT_DEVELOPMENT = 'Project Development'
    SYSTEMS_INTEGRATION = 'Systems Integration'
    POWER_EQUIPMENT     = 'Power Equipment'
    GRID_INTEGRATION    = 'Grid Integration'
    CC                  = 'C&C'

class H2Capital(Enum):
    HESS_ELECTROLYZER   = 'HESS Electrolyzer'
    HESS_FUEL_CELL      = 'HESS Fuel Cell'
    HESS_COMPRESSOR     = 'HESS Compressor'
    HESS_INVERTER       = 'HESS Inverter'
    HESS_RECTIFIER      = 'HESS Rectifier'
    GRID_INTEGRATION    = 'Grid Integration'
    CC                  = 'C&C'
    CAVERN_STORAGE      = 'Cavern Storage'

class CAESCapital(Enum):
    CAES_CAPITAL        = 'CAES Capital (Turbine, Compressor, BOP & EPC)'
    CAVERN_STORAGE      = 'Cavern Storage'

class PSHCapital(Enum):
    ELECTROMECHANICAL       = 'PSH Electromechanical'
    POWERHOUSE_CONSTRUCTION = 'PSH Powerhouse Construction & Infrastructure'
    EPC_INDIRECT_COSTS      = 'PSH EPC Contractor Indirect Costs + Contingency'
    RESERVOIR_CONSTRUCTION  = 'PSH Reservoir Construction & Infrastructure'

class GESCapital(Enum):
    GRAVITATIONAL_CAPITAL = 'Gravitational Capital (SB + BOS)'
    COMBINED_PROJECT_DEV  = 'Combined Project Development & Grid Integration'
    POWER_EQUIPMENT       = 'Power Equipment'

class ThermalCapital(Enum):
    """THERMAL"""
    THERMAL_CAPITAL  = 'Thermal Capital (SB + BOS)'
    COMBINED_EPC_FEE = 'Combined EPC Fee, Project Development & Grid Integration'
    POWER_EQUIPMENT  = 'Power Equipment'


# ── Capital Cost 입력 유틸리티 ────────────────────────────────

# STORAGE_CATALOG 코드 → DB 기술명 매핑
_DB_TECH_MAP: dict[str, str] = {
    'LFP':    'Lithium-ion LFP',
    'NMC':    'Lithium-ion NMC',
    'VRF':    'Vanadium Redox Flow',
    'ZINC':   'Zinc',
    'LEAD':   'Lead Acid',
    'H2':     'Hydrogen',
    'PSH':    'PSH',
    'CAES':   'CAES',
    'GES':    'Gravitational',
    'THERMAL':         'Thermal',
    'CARNOT_CONCRETE': 'Retrofit Carnot (Concrete)',
}


def build_capital_items(tech_code: str, values: dict) -> list[CapitalCostItem]:
    """
    값 딕셔너리로 CapitalCostItem 리스트 생성.
    키는 Enum 멤버 또는 문자열 모두 가능. 단위는 CAPITAL_CATALOG에서 자동으로 가져옴.

    예시:
        build_capital_items('LFP', {
            BatteryCapital.DC_STORAGE_BLOCK: 165.9,
            BatteryCapital.POWER_EQUIPMENT:   57.1,
        })
    """
    fields = CAPITAL_CATALOG.get(tech_code)
    if fields is None:
        raise ValueError(f"CAPITAL_CATALOG에 없는 기술 코드: '{tech_code}'")
    unit_map = {f.name: f.unit for f in fields}
    items = []
    for key, value in values.items():
        name = key.value if isinstance(key, Enum) else key
        if name not in unit_map:
            raise ValueError(f"'{tech_code}' 항목에 없는 이름: '{name}'\n"
                             f"사용 가능: {list(unit_map)}")
        items.append(CapitalCostItem(name, value, unit_map[name]))
    return items


def load_capital_from_db(
    tech_code:   str,
    db_path:     str,
    estimate:    str   = 'High',
    year:        int   = 2023,
    power_mw:    float = 100.0,
    duration_hr: float = 4.0,
) -> list[CapitalCostItem]:
    """
    엑셀 DB에서 Capital Cost 항목을 읽어 CapitalCostItem 리스트로 반환.

    예시:
        load_capital_from_db('LFP', 'ESGC_Cost_Performance_Database_v2024.XLSX', estimate='High')
    """
    import openpyxl

    db_tech = _DB_TECH_MAP.get(tech_code)
    if db_tech is None:
        raise ValueError(f"DB 매핑 없는 기술 코드: '{tech_code}'")

    wb = openpyxl.load_workbook(db_path, data_only=True)
    ws = wb['Database']

    items = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        tech, yr, pw, dur, est_type, cat, param, value = row
        if not (tech == db_tech and yr == year and pw == power_mw
                and dur == duration_hr and est_type == estimate
                and cat == 'Capital Cost' and 'Total Installed Cost' not in str(param)):
            continue
        if str(param).endswith('($/kWh)'):
            name, unit = param[:-7].strip(), '$/kWh'
        elif str(param).endswith('($/kW)'):
            name, unit = param[:-6].strip(), '$/kW'
        else:
            continue
        items.append(CapitalCostItem(name, value, unit))

    if not items:
        raise ValueError(f"DB에서 데이터 없음: tech={tech_code}, estimate={estimate}, "
                         f"year={year}, {power_mw}MW/{duration_hr}hr")
    return items


# ── 순수 계산 함수 ────────────────────────────────────────────

def calc_cycles_per_year(tech: TechParams) -> float:
    """Primary DOD 기준 연간 사이클 수 계산.

    물리적 한계(24hr/cycle)와 cycle_limit_per_yr 중 작은 값을 적용한다.
    Secondary DOD 기준이 필요할 때는 tech.secondary_dod를 직접 참조해 별도 계산한다.
    """
    d            = tech.dod
    discharge_hr = tech.duration_hr * d
    charge_hr    = discharge_hr / tech.rte
    total_hr     = discharge_hr + charge_hr + tech.rest_charge_hr + tech.rest_discharge_hr
    cycle_day    = min(24.0 / total_hr, tech.cycle_limit_per_yr / 365.0 / d)
    return cycle_day * 365.0


def calc_aeo(tech: TechParams):
    """연간 에너지 출력량(AEO) 계산 (kWh/yr)"""
    power_kw     = tech.power_mw * 1000
    discharge_hr = tech.duration_hr * tech.dod
    charge_hr    = discharge_hr / tech.rte
    total_hr     = discharge_hr + charge_hr + tech.rest_charge_hr + tech.rest_discharge_hr
    cycle_day    = min(24.0 / total_hr, tech.cycle_limit_per_yr / 365.0 / tech.dod)
    aeo          = cycle_day * 365.0 * power_kw * tech.duration_hr * tech.dod

    return aeo, {
        'discharge_time_hr':  discharge_hr,
        'charge_time_hr':     charge_hr,
        'total_hr_per_cycle': total_hr,
        'cycles_per_day':     cycle_day,
        'cycles_per_year':    cycle_day * 365.0,
    }


def build_dc_sb_armo(
    tech:              TechParams,
    capital_items:     list[CapitalCostItem],
    cycle_life_at_primary_dod: float,
    proj_life:         int,
) -> list[ScheduledCostItem]:
    """
    DC Storage Block 교체 ARMO 생성.

    교체 간격 = round(min(cycle_life_at_primary_dod / cycles_per_year, calendar_life))
    proj_life 이전의 교체 연도에 ScheduledCostItem 으로 반환한다.
    """
    cycles_per_year = calc_cycles_per_year(tech)
    if cycles_per_year <= 0:
        return []
    dc_sb = next(
        (it for it in capital_items if it.name == 'DC Storage Block'),
        None,
    )
    if dc_sb is None:
        return []
    repl_cost = dc_sb.to_total(tech.power_mw, tech.duration_hr)
    exhaust = cycle_life_at_primary_dod / cycles_per_year
    interval = round(min(exhaust, tech.calendar_life_yr))
    if interval <= 0:
        return []
    years = [interval * i for i in range(1, 10_000) if interval * i < proj_life]
    return [ScheduledCostItem('DC Storage Block Replacement', repl_cost, years)] if years else []


def build_vrf_stack_armo(
    tech:              TechParams,
    replacement_items: list[CapitalCostItem],
    proj_life:         int,
) -> list[ScheduledCostItem]:
    """
    VRF Redox Flow Stack/Pump 교체 ARMO 생성.

    교체 간격 = calendar_life (사이클 수명 무관, 달력 수명 기준)
    proj_life 이전의 교체 연도에 ScheduledCostItem 으로 반환한다.
    replacement_items 는 DB 'Replacement' 카테고리 항목 ($/kW 단위).
    """
    if not replacement_items:
        return []
    interval = round(tech.calendar_life_yr)
    if interval <= 0:
        return []
    years = [interval * i for i in range(1, 10_000) if interval * i < proj_life]
    if not years:
        return []
    return [
        ScheduledCostItem(it.name, it.to_total(tech.power_mw, tech.duration_hr), years)
        for it in replacement_items
    ]


def build_gravitational_armo(
    tech:          TechParams,
    capital_items: list[CapitalCostItem],
    proj_life:     int,
) -> list[ScheduledCostItem]:
    """
    Gravitational Power Equipment 교체 ARMO 생성.

    교체 간격 = 30년 (고정, PNNL 정의)
    proj_life 이전의 교체 연도에 ScheduledCostItem 으로 반환한다.
    """
    pe = next((it for it in capital_items if it.name == 'Power Equipment'), None)
    if pe is None:
        return []
    repl_cost = pe.to_total(tech.power_mw, tech.duration_hr)
    years = [30 * i for i in range(1, 10_000) if 30 * i < proj_life]
    return [ScheduledCostItem('Power Equipment Replacement', repl_cost, years)] if years else []


def build_hydrogen_armo(
    tech:          TechParams,
    capital_items: list[CapitalCostItem],
    proj_life:     int,
) -> list[ScheduledCostItem]:
    """
    Hydrogen ARMO 생성 (PNNL 정의):
    - Fuel Cell Stack  : 40,000 FC 운전시간 간격
    - Electrolyzer     : 60,000 EZ 운전시간 간격
    - BOP (Compressor, Inverter, Rectifier): 15년 간격 -> FOM에 이미 포함
    """
    cycles_per_year = calc_cycles_per_year(tech)
    discharge_hr = tech.duration_hr * tech.dod
    charge_hr    = discharge_hr / tech.rte   # EZ 운전시간 = 충전 시간

    items: list[ScheduledCostItem] = []

    def _add(name_key: str, label: str, interval: int) -> None:
        it = next((x for x in capital_items if x.name == name_key), None)
        if it is None or interval <= 0:
            return
        years = [interval * i for i in range(1, 10_000) if interval * i < proj_life]
        if years:
            items.append(ScheduledCostItem(label, it.to_total(tech.power_mw, tech.duration_hr), years))

    # Fuel Cell Stack
    if cycles_per_year > 0 and discharge_hr > 0:
        fc_interval = round(40_000 / (discharge_hr * cycles_per_year))
        _add('HESS Fuel Cell', 'Fuel Cell Stack Replacement', fc_interval)

    # Electrolyzer
    if cycles_per_year > 0 and charge_hr > 0:
        ez_interval = round(60_000 / (charge_hr * cycles_per_year))
        _add('HESS Electrolyzer', 'Electrolyzer Replacement', ez_interval)

    # BOP (Compressor, Inverter, Rectifier) 교체는 Fixed O&M에 포함되어 있으므로 제외

    return items

def build_lithumion_augmentation(
    tech: TechParams,
    capital_items: list[CapitalCostItem],
    cycles_life_at_primary_dod: float,
    cycles_life_at_secondary_dod: float,
    proj_life: int,
) -> list[ScheduledCostItem]:
    """
    Lithium-ion LFP/NMC DC Storage Block 증설(Augmentation) ARMO 생성.

    tech.dod           = Primary DOD (1차 방전심도)
    tech.secondary_dod = Secondary DOD (2차 방전심도)

    사이클 구조 (주기 T = T_aug + T_sec):
      Phase A [0 → T_aug]          : Battery 1이 primary DOD로 운전
      Phase B [T_aug → T_aug+T_sec]: Battery 1이 secondary DOD로 운전 + Battery 2(aug_frac) 추가
      T 년도                        : Battery 1 폐기, Battery 3(full) 추가 → 주기 반복

    증설 연도 : round(n×T + T_aug), n = 0, 1, 2, ...  (비용 = augmentation_fraction × DC_SB)
    교체 연도 : round(n×T),         n = 1, 2, 3, ...  (비용 = full DC_SB)
    """
    primary_dod   = tech.dod
    secondary_dod = tech.secondary_dod

    dc_sb = next((it for it in capital_items if it.name == 'DC Storage Block'), None)
    if dc_sb is None or secondary_dod is None:
        return []

    repl_cost = dc_sb.to_total(tech.power_mw, tech.duration_hr)

    # ── Primary DOD 기준 연간 사이클 수 ─────────────────────────
    cycles_per_year = calc_cycles_per_year(tech)

    # ── Secondary DOD 기준 연간 사이클 수 (인라인 계산) ──────────
    d2          = secondary_dod
    dhr2        = tech.duration_hr * d2
    total_hr2   = dhr2 + dhr2 / tech.rte + tech.rest_charge_hr + tech.rest_discharge_hr
    cycle_day2  = min(24.0 / total_hr2, tech.cycle_limit_per_yr / 365.0 / d2)
    cycles_per_year_secondary = cycle_day2 * 365.0

    if cycles_per_year <= 0 or cycles_per_year_secondary <= 0:
        return []

    # ── 주기 계산 ────────────────────────────────────────────────
    # Phase A 기간: primary DOD 사이클 소진 또는 달력 수명 중 빠른 쪽
    cycle_exhaust = cycles_life_at_primary_dod / cycles_per_year
    T_aug         = min(cycle_exhaust, tech.calendar_life_yr)

    # calendar_life 가 binding 인 경우: 사이클 소진 전에 배터리가 노후화 → 단순 전체 교체
    if tech.calendar_life_yr <= cycle_exhaust:
        interval = round(T_aug)
        if interval <= 0:
            return []
        repl_years_cal = [interval * i for i in range(1, 10_000) if interval * i < proj_life]
        return [ScheduledCostItem('DC Storage Block Replacement', repl_cost, repl_years_cal)] if repl_years_cal else []

    # cycle life 가 binding 인 경우: primary DOD 소진 → secondary DOD 전환 + 증설
    # Battery 1의 secondary DOD 잔여 수명 (calendar life 잔여분으로 제한)
    cycles_remain_primary_dc_sb          = (1 - (1 - primary_dod) / (1 - secondary_dod)) * cycles_life_at_secondary_dod
    years_primary_dc_sb_at_secondary_dod = cycles_remain_primary_dc_sb / cycles_per_year_secondary
    remaining_cal_after_taug             = tech.calendar_life_yr - T_aug
    years_secondary_phase                = min(years_primary_dc_sb_at_secondary_dod, remaining_cal_after_taug)

    T1 = T_aug + years_secondary_phase   # Battery 1 총 수명 (primary + secondary 잔여)

    # Battery 2, 3, 4, 5, ... 는 처음부터 secondary DOD로 운전 → 전체 secondary 수명
    T_secondary = min(cycles_life_at_secondary_dod / cycles_per_year_secondary, tech.calendar_life_yr)

    # ── 증설 비율: Battery 2의 규모 (secondary DOD 기준 출력 부족분 보충) ──
    augmentation_fraction = (primary_dod - secondary_dod) / secondary_dod
    aug_cost = repl_cost * augmentation_fraction

    # ── Minor aug: Battery 1 → secondary 전환(n=0), Battery 2 수명 종료(n=1), ...
    # n=0: round(T_aug),  n≥1: round(round(T_aug) + n×T_secondary)
    # n≥1은 Battery 2의 실제 가동 시작 연도(round(T_aug))를 기준으로 계산한다.
    aug_yr_0 = round(T_aug)
    aug_years: list[int] = []
    for n in range(0, 10_000):
        yr = aug_yr_0 if n == 0 else round(aug_yr_0 + n * T_secondary)
        if yr <= 0:
            continue
        if yr >= proj_life:
            break
        aug_years.append(yr)

    # ── Major aug: Battery 1 퇴역(n=0), Battery 3 수명 종료(n=1), ...
    # n=0: round(T1),  n≥1: round(round(T1) + n×T_secondary)
    major_yr_0 = round(T1)
    major_years: list[int] = []
    for n in range(0, 10_000):
        yr = major_yr_0 if n == 0 else round(major_yr_0 + n * T_secondary)
        if yr <= 0:
            continue
        if yr >= proj_life:
            break
        major_years.append(yr)

    items: list[ScheduledCostItem] = []
    if aug_years:
        items.append(ScheduledCostItem('DC Storage Block Augmentation', aug_cost, aug_years))
    if major_years:
        items.append(ScheduledCostItem('DC Storage Block Augmentation (Major)', repl_cost, major_years))
    return items


def build_nmc_warranty(
    armo_items:        list[ScheduledCostItem],
    warranty_per_kwh:  float,
    warranty_delay_yr: int,
    proj_life:         int,
    power_mw:          float,
    duration_hr:       float,
) -> list[ScheduledCostItem]:
    """
    NMC warranty ScheduledCostItem 생성.
    DC SB replacement (Major augmentation) 연도 기준으로만 적용.
    각 replacement 연도 + warranty_delay_yr 후부터 proj_life 까지 매년 발생.
    """
    w_total    = warranty_per_kwh * power_mw * 1000 * duration_hr
    major_item = next((it for it in armo_items if 'Major' in it.name), None)
    if major_item is None:
        return []
    items: list[ScheduledCostItem] = []
    for repl_yr in major_item.years:
        years = list(range(repl_yr + warranty_delay_yr, proj_life + 1))
        if years:
            items.append(ScheduledCostItem('Warranty', w_total, years))
    return items


def build_repl_decomm(
    repl_years:  list[int],
    decomm_base: list[CapitalCostItem],
    proj_life:   int,
    power_mw:    float,
    duration_hr: float,
) -> list[ScheduledCostItem]:
    """
    배터리 교체 시마다 구 배터리 처리(해체) 비용이 발생하는 기술용.

    교체 연도 + 사업 종료(proj_life) 연도 전체에 해체 비용을 반영한
    ScheduledCostItem 리스트를 반환한다.
    """
    years = sorted(set(list(repl_years) + [proj_life]))
    return [
        ScheduledCostItem(it.name, it.to_total(power_mw, duration_hr), years)
        for it in decomm_base
    ]


def build_armo_and_decomm(
    tech:              'TechParams',
    capital_items:     list[CapitalCostItem],
    decomm_base:       list[CapitalCostItem],
    proj_life:         int,
    replacement_items: list[CapitalCostItem] | None = None,
    warranty_per_kwh:  float = 0.0,
    warranty_delay_yr: int   = 0,
    include_decomm:    bool  = True,
) -> tuple[list[ScheduledCostItem], list[ScheduledCostItem], list[ScheduledCostItem]]:
    """
    기술 유형에 따라 ARMO / 해체비용 / 보증비용 ScheduledCostItem 을 자동 생성한다.
    cycle_life 값들은 tech.cycle_life_at_primary_dod / tech.cycle_life_at_secondary_dod 에서 읽는다.

    - LFP, NMC + secondary_dod 존재 시 → Augmentation ARMO (증설 + 교체 복합)
    - ZINC, LEAD + cycle_life_at_primary_dod 존재 시 → DC Storage Block 교체 ARMO
    - VRF + replacement_items 존재 시 → Stack 교체 ARMO (calendar_life 간격)
    - GES → Power Equipment 교체 ARMO (30년 간격)
    - LEAD → 해체비용을 교체 연도 + 사업 종료 연도에 반영
    - NMC → 각 augmentation 연도 + warranty_delay_yr 후부터 proj_life 까지 매년 warranty
    - VRF → calendar_life 기간 동안 매년 warranty
    - 그 외 → year 1 warranty
    반환: (armo_items, decomm_items, warranty_items)
    """
    cl_primary   = tech.cycle_life_at_primary_dod
    cl_secondary = tech.cycle_life_at_secondary_dod

    armo_items: list[ScheduledCostItem] = []
    if tech.storage_type in ('LFP', 'NMC') and tech.secondary_dod and cl_primary and cl_secondary:
        armo_items = build_lithumion_augmentation(
            tech, capital_items,
            float(cl_primary), float(cl_secondary), proj_life,
        )
    elif tech.storage_type in ('ZINC', 'LEAD') and cl_primary:
        armo_items = build_dc_sb_armo(tech, capital_items, float(cl_primary), proj_life)
    elif tech.storage_type == 'VRF' and replacement_items:
        armo_items = build_vrf_stack_armo(tech, replacement_items, proj_life)
    elif tech.storage_type == 'GES':
        armo_items = build_gravitational_armo(tech, capital_items, proj_life)
    elif tech.storage_type == 'H2':
        armo_items = build_hydrogen_armo(tech, capital_items, proj_life)

    if not include_decomm:
        decomm_items = []
    elif tech.storage_type == 'LEAD' and armo_items:
        decomm_items = build_repl_decomm(
            armo_items[0].years, decomm_base, proj_life,
            tech.power_mw, tech.duration_hr,
        )
    else:
        decomm_items = [
            ScheduledCostItem(it.name, it.to_total(tech.power_mw, tech.duration_hr), [proj_life])
            for it in decomm_base
        ]

    warranty_items: list[ScheduledCostItem] = []
    if warranty_per_kwh > 0:
        w_total = warranty_per_kwh * tech.power_mw * 1000 * tech.duration_hr
        if tech.storage_type == 'VRF':
            cal_years = round(tech.calendar_life_yr)
            warranty_items.append(ScheduledCostItem('Warranty', w_total,
                                                    list(range(1, cal_years + 1))))
        elif tech.storage_type == 'NMC':
            warranty_items.extend(
                build_nmc_warranty(armo_items, warranty_per_kwh, warranty_delay_yr,
                                   proj_life, tech.power_mw, tech.duration_hr)
            )
        else:
            warranty_items.append(ScheduledCostItem('Warranty', w_total, [1]))

    return armo_items, decomm_items, warranty_items


def calc_pvd(wacc_nominal: float, macrs_period: int) -> float:
    """MACRS 기반 감가상각 현재가치(PVD) 계산 (명목 WACC로 할인)"""
    rates = MACRS.get(macrs_period)
    if rates is None:
        raise ValueError(f"지원하지 않는 MACRS 기간: {macrs_period}년. 선택 가능: {sorted(MACRS)}")
    return sum(r / (1 + wacc_nominal) ** yr for yr, r in enumerate(rates, 1))


def calculate_lcos(tech: TechParams, cost: CostParams, fin: FinancialParams) -> dict:
    """
    LCOS 전체 계산.
    엑셀 수식을 그대로 구현:
    - FCR  = ((CRF*(1 - t*PVD*(1-ITC/2) - ITC)) + p1 + p2) / (1-t)
    - PV_RV = (1-ratio)*OCC_net + NPV_costs_N - ratio*NPV_costs_PL
    - LCOS  = (FCR*OCC + CRF*NPV_costs_N - CRF*PV_RV) / AEO
    """
    power_kw = tech.power_mw * 1000
    N  = fin.analysis_period_n
    PL = int(tech.project_life_yr)
    tax = fin.tax_rate

    # AEO
    aeo, aeo_detail = calc_aeo(tech)

    # WACC, CRF
    wacc_nom = (fin.debt_fraction * fin.interest_rate_nominal * (1 - tax)
                + (1 - fin.debt_fraction) * fin.coe_nominal)
    r   = (1 + wacc_nom) / (1 + fin.inflation) - 1 #real값은 nominal 값에 인플레이션을 나눠줘야 실질적인 값으로 평가 가능. nominal은 인플레이션까지 반영된 명목 값
    crf = r * (1 + r) ** N / ((1 + r) ** N - 1)

    # ITC (투자세액공제)
    large = tech.power_mw >= 1.0
    if fin.prevailing_wage or not large:
        base_itc, adder = 0.30, 0.10
    else:
        base_itc, adder = 0.06, 0.02
    itc = base_itc
    if fin.energy_community:
        itc += adder
    if fin.domestic_content:
        itc += adder

    # PVD, FCR
    pvd        = calc_pvd(wacc_nom, tech.macrs_period)
    occ_factor = 1 - tax * pvd * (1 - itc / 2) - itc # 감가상각, ITC 할인 등을 반영하여 OCC에 할인을 적용
    fcr        = (crf * occ_factor + fin.property_tax_rate + fin.insurance_rate) / (1 - tax)
    # 1항: 할인 적용을 위한 occ_factor 반영 후 1년 균등화, 2항 매년 세금 비율, 3항 보험 비율, (1-tax)로 나누어 세금을 낼 것까지 고려한 비용 계산

    # 연간 비용 기초값
    if cost.capital_items:
        occ_total = sum(item.to_total(tech.power_mw, tech.duration_hr) for item in cost.capital_items)
    else:
        occ_total = cost.occ_per_kw * power_kw
    fom_0           = cost.fixed_om_per_kw_yr * power_kw
    g               = fin.om_escalation_real
    ecc_annual      = aeo * cost.electricity_cost_per_kwh / tech.rte
    rte_loss_annual = aeo * cost.electricity_cost_per_kwh * (1 / tech.rte - 1)
    vom_annual      = cost.variable_om_per_kwh * aeo

    def lump_items(items, yr):
        return sum(item.cost for item in items if yr in item.years)

    def npv_over(horizon, rte_only=False):
        npv_c = npv_a = 0.0
        for yr in range(1, horizon + 1):
            df   = (1 + r) ** yr
            fom  = fom_0 * (1 + g) ** (yr - 1)
            ecc  = rte_loss_annual if rte_only else ecc_annual
            misc = lump_items(cost.warranty_items, yr) + lump_items(cost.armo_items, yr) + lump_items(cost.decomm_items, yr)
            npv_c += (fom + vom_annual + ecc + misc) / df
            npv_a += aeo / df
        return npv_c, npv_a

    npv_costs_n,      aeo_npv_n  = npv_over(N)
    npv_costs_pl,     aeo_npv_pl = npv_over(PL)
    npv_costs_n_rte,  _          = npv_over(N,  rte_only=True)
    npv_costs_pl_rte, _          = npv_over(PL, rte_only=True)

    # 잔존가치 PV (엑셀 G155/G156 수식)
    occ_net = occ_total * occ_factor
    if PL > N and aeo_npv_pl > 0:
        ratio     = aeo_npv_n / aeo_npv_pl
        pv_rv     = (1 - ratio) * occ_net + npv_costs_n     - ratio * npv_costs_pl
        pv_rv_rte = (1 - ratio) * occ_net + npv_costs_n_rte - ratio * npv_costs_pl_rte
    else:
        pv_rv = pv_rv_rte = 0.0

    # LCOS
    annualized_occ = fcr * occ_total
    arr     = annualized_occ + crf * npv_costs_n     - crf * pv_rv
    arr_rte = annualized_occ + crf * npv_costs_n_rte - crf * pv_rv_rte

    npv_fom_n      = sum(fom_0 * (1+g)**(yr-1)            / (1+r)**yr for yr in range(1, N+1))
    npv_vom_n      = sum(vom_annual                        / (1+r)**yr for yr in range(1, N+1))
    npv_ecc_n      = sum(ecc_annual                        / (1+r)**yr for yr in range(1, N+1))
    npv_rte_loss_n = sum(rte_loss_annual                   / (1+r)**yr for yr in range(1, N+1))
    npv_warranty_n = sum(lump_items(cost.warranty_items, yr) / (1+r)**yr for yr in range(1, N+1))
    npv_armo_n     = sum(lump_items(cost.armo_items,     yr) / (1+r)**yr for yr in range(1, N+1))
    npv_decomm_n   = sum(lump_items(cost.decomm_items,   yr) / (1+r)**yr for yr in range(1, N+1))

    return {
        'aeo':                 aeo,
        'aeo_detail':          aeo_detail,
        'wacc_nominal':        wacc_nom,
        'wacc_real':           r,
        'crf':                 crf,
        'itc':                 itc,
        'pvd':                 pvd,
        'fcr':                 fcr,
        'occ_total':           occ_total,
        'annualized_occ':      annualized_occ,
        'annualized_fom':      crf * npv_fom_n,
        'annualized_vom':      crf * npv_vom_n,
        'annualized_ecc':      crf * npv_ecc_n,
        'annualized_rte_loss': crf * npv_rte_loss_n,
        'annualized_warranty': crf * npv_warranty_n,
        'annualized_armo':     crf * npv_armo_n,
        'annualized_decomm':   crf * npv_decomm_n,
        'npv_occ':             occ_total,
        'npv_fom':             npv_fom_n,
        'npv_vom':             npv_vom_n,
        'npv_ecc':             npv_ecc_n,
        'npv_rte_loss':        npv_rte_loss_n,
        'npv_warranty':        npv_warranty_n,
        'npv_armo':            npv_armo_n,
        'npv_decomm':          npv_decomm_n,
        'armo_years':          sorted({y for it in cost.armo_items    for y in it.years}),
        'decomm_years':        sorted({y for it in cost.decomm_items   for y in it.years}),
        'pv_rv':               pv_rv,
        'arr':                 arr,
        'arr_rte':             arr_rte,
        'lcos':                arr / aeo,
        'lcos_rte':            arr_rte / aeo,
    }


# ── 출력 ──────────────────────────────────────────────────────

def print_results(tech: TechParams, cost: CostParams, fin: FinancialParams, res: dict):
    w = 58
    print()
    print("=" * w)
    print("  LCOS 계산 결과  (ESGC Workbook v2024 기준)")
    print("=" * w)

    print(f"\n[기술 분류]")
    print(f"  카테고리             : {tech.category.value}")
    print(f"  기술                 : {tech.display_name}")
    print(f"  MACRS 감가상각 기간  : {tech.macrs_period}년")

    print("\n[기술 파라미터]")
    print(f"  정격 출력            : {tech.power_mw:>10.1f}  MW")
    print(f"  저장 지속시간        : {tech.duration_hr:>10.1f}  hr")
    print(f"  왕복효율 (DC, rte_dc): {tech.rte_dc*100:>10.2f}  %  (J22)")
    print(f"  왕복효율 (시스템 RTE): {tech.rte*100:>10.2f}  %  (AC-AC)")
    print(f"  방전심도 (DOD)       : {tech.dod*100:>10.1f}  %")
    print(f"  사업 기간 (PL)       : {int(tech.project_life_yr):>10d}  년")
    print(f"  연간 사이클 제한     : {tech.cycle_limit_per_yr:>10.0f}  회/년 (100% DOD)")

    print("\n[비용 파라미터]")
    _pw  = tech.power_mw * 1000          # kW
    _en  = _pw * tech.duration_hr        # kWh
    _occ = res['occ_total']
    if cost.capital_items:
        kw_items  = [it for it in cost.capital_items if it.unit == '$/kW']
        kwh_items = [it for it in cost.capital_items if it.unit == '$/kWh']
        print(f"\n  [Power  ($/kW)  — {_pw:,.0f} kW]")
        kw_sub = 0.0
        for i, it in enumerate(kw_items):
            total  = it.value * _pw
            kw_sub += total
            branch = '└' if i == len(kw_items) - 1 else '├'
            print(f"    {branch} {it.name:<50} {it.value:>9,.2f} $/kW    ${total:>15,.0f}")
        if kw_items:
            print(f"    {'소계':<55}             ${kw_sub:>15,.0f}")
        print(f"\n  [Energy ($/kWh) — {_en:,.0f} kWh]")
        kwh_sub = 0.0
        for i, it in enumerate(kwh_items):
            total   = it.value * _en
            kwh_sub += total
            branch  = '└' if i == len(kwh_items) - 1 else '├'
            print(f"    {branch} {it.name:<50} {it.value:>9,.2f} $/kWh   ${total:>15,.0f}")
        if kwh_items:
            print(f"    {'소계':<55}             ${kwh_sub:>15,.0f}")
        print(f"  {'─'*72}")
        print(f"  {'총 OCC':<56}             ${_occ:>15,.0f}")
        print(f"  {'총 OCC / kW':<55}   {_occ/_pw:>9,.2f} $/kW")
    else:
        print(f"  초기투자비 (OCC)     : {_occ/_pw:>10,.2f}  $/kW")
    print(f"  고정 O&M             : {cost.fixed_om_per_kw_yr:>10,.2f}  $/kW-yr")
    print(f"  변동 O&M             : {cost.variable_om_per_kwh:>10.4f}  $/kWh")
    print(f"  전력 구매단가        : {cost.electricity_cost_per_kwh:>10.4f}  $/kWh")

    print("\n[재무 파라미터]")
    print(f"  분석 기간 (N)        : {fin.analysis_period_n:>10d}  년")
    print(f"  WACC (명목)          : {res['wacc_nominal']*100:>10.4f}  %")
    print(f"  WACC (실질)          : {res['wacc_real']*100:>10.4f}  %")
    print(f"  CRF                  : {res['crf']:>10.6f}")
    print(f"  ITC 합계             : {res['itc']*100:>10.1f}  %")
    print(f"  PVD (MACRS)          : {res['pvd']:>10.6f}")
    print(f"  FCR                  : {res['fcr']*100:>10.4f}  %")

    d = res['aeo_detail']
    print("\n[AEO 계산]")
    print(f"  방전/충전 시간       : {d['discharge_time_hr']:.2f} / {d['charge_time_hr']:.4f}  hr")
    print(f"  사이클/일            : {(d['cycles_per_day']):>10,.2f}  회/일")
    print(f"  사이클/년            : {(d['cycles_per_year']):>10,.2f}  회/년")
    print(f"  연간 발전량 (AEO)    : {res['aeo']:>12,.0f}  kWh/yr")

    crf          = res['crf']
    ann_occ      = res['annualized_occ']
    ann_fom      = res['annualized_fom']
    ann_vom      = res['annualized_vom']
    ann_ecc      = res['annualized_ecc']
    ann_rte_loss = res['annualized_rte_loss']
    ann_warranty = res['annualized_warranty']
    ann_armo     = res['annualized_armo']
    ann_decomm   = res['annualized_decomm']
    ann_rv       = crf * res['pv_rv']
    total_cost   = ann_occ + ann_fom + ann_vom + ann_ecc + ann_warranty + ann_armo + ann_decomm

    # ── 1회 비용 / 총 횟수 (ARMO, Decomm) ───────────────────────
    armo_yrs   = res['armo_years']
    decomm_yrs = res['decomm_years']
    armo_yr_str   = ', '.join(map(str, armo_yrs))   if armo_yrs   else '없음'
    decomm_yr_str = ', '.join(map(str, decomm_yrs)) if decomm_yrs else '없음'
    armo_per_occ   = sum(it.cost for it in cost.armo_items)
    decomm_per_occ = sum(it.cost for it in cost.decomm_items)
    armo_n_occ     = len(armo_yrs)
    decomm_n_occ   = len(decomm_yrs)

    _u = '$/yr'
    SEP = '─' * 42

    # ── 연환산 비용 ──────────────────────────────────────────────
    print("\n[비용 구성 — 연환산 ($/yr)]")
    print(f"  {SEP}")
    print(f"  {'초기투자비 (OCC)':<25}: {ann_occ:>14,.0f}  {_u}")
    print(f"  {'O&M':<25}: {ann_fom+ann_vom:>14,.0f}  {_u}")
    print(f"    {'├ 고정 O&M (FOM)':<23}: {ann_fom:>14,.0f}  {_u}")
    print(f"    {'└ 변동 O&M (VOM)':<23}: {ann_vom:>14,.0f}  {_u}")
    print(f"  {'전력 충전비 (ECC)':<25}: {ann_ecc:>14,.0f}  {_u}")
    print(f"    {'├ RTE 손실비용':<23}: {ann_rte_loss:>14,.0f}  {_u}")
    print(f"    {'└ 유효 충전비':<23}: {ann_ecc-ann_rte_loss:>14,.0f}  {_u}")
    print(f"  {'보증비 (Warranty)':<25}: {ann_warranty:>14,.0f}  {_u}")
    print(f"  {'ARMO':<25}: {ann_armo:>14,.0f}  {_u}")
    print(f"  {'해체비 (Decomm.)':<25}: {ann_decomm:>14,.0f}  {_u}")
    print(f"  {SEP}")
    print(f"  {'총 발생 비용':<25}: {total_cost:>14,.0f}  {_u}")
    print(f"  {'(-) 잔존가치':<25}: {ann_rv:>14,.0f}  {_u}")
    print(f"  {SEP}")
    print(f"  {'총 연간 수입요건':<25}: {res['arr']:>14,.0f}  {_u}")

    # ── 프로젝트 기간 총액 ────────────────────────────────────────
    npv_om  = res['npv_fom'] + res['npv_vom']
    print("\n[프로젝트 기간 비용 ($)]")
    print(f"  {'':25}   {'금액':>14}   비고")
    print(f"  {SEP}")
    print(f"  {'초기투자비 (OCC)':<25}: {res['occ_total']:>14,.0f}  $  (미할인)")
    print(f"  {'O&M':<25}: {npv_om:>14,.0f}  $  (NPV)")
    print(f"  {'전력 충전비 (ECC)':<25}: {res['npv_ecc']:>14,.0f}  $  (NPV)")
    print(f"  {'보증비 (Warranty)':<25}: {res['npv_warranty']:>14,.0f}  $  (NPV)")
    if armo_n_occ:
        print(f"  {'ARMO':<25}: {res['npv_armo']:>14,.0f}  $  (NPV, 1회 {armo_per_occ:,.0f} $  [{armo_yr_str}년])")
    else:
        print(f"  {'ARMO':<25}: {'없음':>14}")
    if decomm_n_occ:
        print(f"  {'해체비 (Decomm.)':<25}: {res['npv_decomm']:>14,.0f}  $  (NPV, 1회 {decomm_per_occ:,.0f} $  [{decomm_yr_str}년])")
    else:
        print(f"  {'해체비 (Decomm.)':<25}: {'없음':>14}")
    print(f"  {SEP}")

    print()
    print("=" * w)
    print(f"  LCOS (충전비 포함)  :  $ {res['lcos']:>8.4f}  /kWh")
    print(f"  LCOS (RTE 손실만)   :  $ {res['lcos_rte']:>8.4f}  /kWh")
    print("=" * w)
    print()


def print_catalog():
    """지원 기술 목록 출력"""
    print("\n지원 기술 목록:")
    current_cat = None
    for code, (name, cat, macrs) in STORAGE_CATALOG.items():
        if cat != current_cat:
            print(f"\n  [{cat.value}]")
            current_cat = cat
        print(f"    {code:<10} {name}  (MACRS {macrs}년)")
    print()


# ── CLI ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='LCOS 계산기 (ESGC Workbook v2024)',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('--list-types', action='store_true', help='지원 기술 목록 출력')

    g = parser.add_argument_group('기술 파라미터')
    g.add_argument('--type',         default='LFP',   help=f'기술 코드 (--list-types 참조)')
    g.add_argument('--power-mw',     type=float, default=100.0,  help='정격 출력 (MW)')
    g.add_argument('--duration',     type=float, default=4.0,    help='저장 지속시간 (hr)')
    g.add_argument('--rte',          type=float, default=0.52,   help='왕복효율 (0~1)')
    g.add_argument('--dod',          type=float, default=0.80,   help='방전심도 (0~1)')
    g.add_argument('--project-life', type=int,   default=60,     help='사업 기간 (년)')
    g.add_argument('--cycle-limit',  type=float, default=365.0,  help='연간 사이클 제한 (100% DOD 기준)')

    g2 = parser.add_argument_group('비용 파라미터')
    g2.add_argument('--occ',       type=float, default=1221.66, help='초기투자비 ($/kW)')
    g2.add_argument('--fom',       type=float, default=18.72,   help='고정 O&M ($/kW-yr)')
    g2.add_argument('--vom',       type=float, default=0.0,     help='변동 O&M ($/kWh)')
    g2.add_argument('--elec-cost', type=float, default=0.03,    help='전력 구매단가 ($/kWh)')

    g3 = parser.add_argument_group('재무 파라미터')
    g3.add_argument('--analysis-period', type=int,   default=20,    help='분석 기간 (년)')
    g3.add_argument('--inflation',       type=float, default=0.028,  help='인플레이션율')
    g3.add_argument('--interest-rate',   type=float, default=0.08,   help='명목 이자율 (부채)')
    g3.add_argument('--coe',             type=float, default=0.13,   help='자기자본비용 (명목)')
    g3.add_argument('--debt-fraction',   type=float, default=0.5,    help='부채 비율')
    g3.add_argument('--tax-rate',        type=float, default=0.257,  help='세율 (연방+주)')
    g3.add_argument('--prevailing-wage',  action='store_true', help='우대 임금 요건 충족 → ITC 30%%')
    g3.add_argument('--energy-community', action='store_true', help='에너지 커뮤니티 가산 적용')
    g3.add_argument('--domestic-content', action='store_true', help='국내산 콘텐츠 가산 적용')

    args = parser.parse_args()

    if args.list_types:
        print_catalog()
        return

    tech = TechParams(
        storage_type       = args.type.upper(),
        power_mw           = args.power_mw,
        duration_hr        = args.duration,
        rte_dc             = args.rte,
        dod                = args.dod,
        project_life_yr    = args.project_life,
        cycle_limit_per_yr = args.cycle_limit,
    )
    cost = CostParams(
        occ_per_kw               = args.occ,
        fixed_om_per_kw_yr       = args.fom,
        variable_om_per_kwh      = args.vom,
        electricity_cost_per_kwh = args.elec_cost,
    )
    fin = FinancialParams(
        analysis_period_n     = args.analysis_period,
        inflation             = args.inflation,
        interest_rate_nominal = args.interest_rate,
        coe_nominal           = args.coe,
        debt_fraction         = args.debt_fraction,
        tax_rate              = args.tax_rate,
        prevailing_wage       = args.prevailing_wage,
        energy_community      = args.energy_community,
        domestic_content      = args.domestic_content,
    )

    results = calculate_lcos(tech, cost, fin)
    print_results(tech, cost, fin, results)


if __name__ == '__main__':
    project_life = 24
    storage_type = "LEAD"
    cycle_life_at_primary_dod = 3603.48

    tech = TechParams(
        storage_type=storage_type, power_mw=1, duration_hr=2,
        rte_dc=0.77, dod=0.58, rest_charge_hr=1.77, rest_discharge_hr=1.77,
        calendar_life_yr=13, project_life_yr=project_life, cycle_limit_per_yr=365,
    )
    capital_items = build_capital_items(storage_type, {
        BatteryCapital.DC_STORAGE_BLOCK:    242.13,
        BatteryCapital.DC_STORAGE_BOS:       85.79,
        BatteryCapital.POWER_EQUIPMENT:     106.87,
        BatteryCapital.CC:                   39.49,
        BatteryCapital.SYSTEMS_INTEGRATION:  73.31,
        BatteryCapital.EPC:                  54.19,
        BatteryCapital.PROJECT_DEVELOPMENT:  69.10,
        BatteryCapital.GRID_INTEGRATION:     33.97,
    })

    # 해체 비용: CapitalCostItem(단위비용, 단위) — to_total()은 내부에서 자동 계산
    decomm_fields = [CapitalCostItem('Recycling', 12.83, '$/kWh')]

    armo_items, decomm_items = build_armo_and_decomm(
        tech, capital_items, decomm_fields, cycle_life_at_primary_dod, project_life
    )

    cost = CostParams(
        fixed_om_per_kw_yr=4.35,
        capital_items=capital_items,
        armo_items=armo_items,
        decomm_items=decomm_items,
    )
    fin = FinancialParams(analysis_period_n=project_life)
    results = calculate_lcos(tech, cost, fin)
    print_results(tech, cost, fin, results)