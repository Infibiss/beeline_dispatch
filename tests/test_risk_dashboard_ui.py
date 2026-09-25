"""Static client contract checks for Risk Engine v1 rendering."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


STATIC = Path(__file__).resolve().parents[1] / 'src/field_router/static'


def node(expression):
    executable = shutil.which('node')
    if not executable:
        pytest.skip('Node.js is required for client contract checks')
    script = STATIC / 'risk-dashboard.js'
    result = subprocess.run(
        [executable, '-e', 'const risk=require(' + json.dumps(str(script)) + ');' + expression],
        capture_output=True, text=True, encoding='utf-8', check=True,
    )
    return json.loads(result.stdout)


def test_direct_risk_engine_contract_has_four_safe_ui_categories():
    result = node(r'''
const forecast={model_version:'risk-engine-v1.1',calibrated:false,iterations:600,
  loss_unit:'conditional_loss_units',p_sla_breach:.31,p_late_start:.46,p_overtime:.17,
  expected_affected_customers:.62,routes:[{engineer_id:'E-1',visits:[{request_id:'R-1',
    p_sla_breach:.2,service_duration:{p50_minutes:70,p80_minutes:88,p90_minutes:101}}]}],
  assumptions:['synthetic priors']};
const risks=risk.engineRisks(forecast);
console.log(JSON.stringify({modern:risk.isEngineForecast(forecast),unit:risk.lossUnitLabel(forecast),
  risks:risks.map(x=>({id:x.id,measure:x.measure,value:x.value})),visits:risk.collectVisits(forecast).length}));
''')
    assert result == {
        'modern': True,
        'unit': 'условных ед.',
        'risks': [
            {'id': 'sla', 'measure': 'probability', 'value': .31},
            {'id': 'late', 'measure': 'probability', 'value': .46},
            {'id': 'overtime', 'measure': 'probability', 'value': .17},
            {'id': 'vehicle', 'measure': 'not_available', 'value': None},
        ],
        'visits': 1,
    }


def test_explicit_vehicle_score_and_flat_visits_are_supported():
    result = node(r'''
const forecast={risks:[{id:'vehicle',measure:'heuristic_score',value:64,
  reasons:['ТО просрочено'],recommendation:'Проверить машину'}],visits:[{request_id:'R-flat'}]};
console.log(JSON.stringify({modern:risk.isEngineForecast(forecast),vehicle:risk.engineRisks(forecast)[3],
  visits:risk.collectVisits(forecast).map(x=>x.request_id)}));
''')
    assert result['modern'] is True
    assert result['vehicle']['measure'] == 'heuristic_score'
    assert result['vehicle']['value'] == 64
    assert result['visits'] == ['R-flat']


def test_legacy_forecast_is_not_misread_as_risk_engine_v1():
    result = node(r'''
const forecast={risks:[{id:'windows',measure:'probability',value:.4}]};
console.log(JSON.stringify({modern:risk.isEngineForecast(forecast)}));
''')
    assert result == {'modern': False}


def test_markup_explains_preview_only_advisories_and_real_contract_sections():
    html = (STATIC / 'scenarios.html').read_text(encoding='utf-8')
    assert 'id="risk-engine-meta"' in html
    assert 'id="risk-advisories"' in html
    assert 'id="risk-route-details"' in html
    assert 'id="risk-assumptions"' in html
    assert 'Кнопка только рассчитывает варианты; применение всегда отдельно' in html
