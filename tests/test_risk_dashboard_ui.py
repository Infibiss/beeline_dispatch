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


def test_markup_retains_advisories_and_technical_contract_sections():
    html = (STATIC / 'scenarios.html').read_text(encoding='utf-8')
    assert 'id="risk-engine-meta"' in html
    assert 'id="risk-advisories"' in html
    assert 'id="risk-route-details"' in html
    assert 'id="risk-assumptions"' in html
    assert 'id="risk-replan"' in html


def client(expression):
    executable = shutil.which('node')
    if not executable:
        pytest.skip('Node.js is required for client checks')
    harness = Path(__file__).with_name('risk_ui_harness.cjs')
    script = 'const {assert,$,snapshot,intents,fire,settle,refresh,setResponse}=require(' + json.dumps(str(harness)) + ');'
    script += '\nrequire(' + json.dumps(str(STATIC / 'risk-dashboard.js')) + ');'
    script += '\n(async()=>{await refresh();' + expression + '})().catch(error=>{console.error(error);process.exitCode=1;});'
    result = subprocess.run([executable, '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_control_day_has_compact_actual_indicators_and_no_unavailable_vehicle_filler():
    client(r'''
assert.equal($('risk-indicators').querySelectorAll('svg').length,0);
const windows=$('risk-indicators').children.find(x=>x.dataset.indicator==='windows');
assert.match(windows.textContent,/1 из 4/);
assert.doesNotMatch(windows.textContent,/25%/);
assert.equal($('risk-forecasts').children.filter(x=>x.dataset.risk==='vehicle').length,0);
assert.doesNotMatch($('risk-model-status').textContent,/risk-engine|модель|600/i);
assert.match($('risk-forecasts').textContent,/оценка/i);
assert.doesNotMatch($('risk-forecasts').textContent,/0,62|0\.62|потер|сценари|Монте|симуляц/i);
''')


def test_actual_visit_rows_keep_times_and_actions_without_statistical_loss_columns():
    client(r'''
const routes=$('risk-routes');
assert.match(routes.textContent,/Лесная, 10/);
assert.match(routes.textContent,/10:20/);
assert.match(routes.textContent,/11:30/);
assert.doesNotMatch(routes.textContent,/потер|затронуто|0,62|P50|P80|P90|условн/i);
assert.match($('risk-route-details').querySelector('summary').textContent,/1/);
const action=routes.querySelector('button');assert.ok(action,'Visit has a fact-check action');action.click();
assert.equal(intents.at(-1).action,'facts');assert.equal(intents.at(-1).requestId,'R-1');assert.equal(intents.at(-1).engineerId,'E-1');
assert.match($('risk-engine-meta').textContent,/4,2 условных ед/);
''')


def test_stale_forecasts_are_removed_while_current_gps_actions_remain_available():
    client(r'''
snapshot.context.needs_replanning=true;await refresh();
assert.equal($('risk-forecasts').children.length,0);
assert.equal($('risk-route-details').hidden,true);
$('risk-advisories').querySelector('button').click();
assert.deepEqual(intents.at(-1),{action:'facts',planVersion:7,advisoryId:'gps-1',riskId:null,engineerId:'E-1',requestId:'R-1'});
snapshot.forecast.advisories[0].plan_version=6;await refresh();
assert.equal($('risk-advisories').querySelector('button').disabled,true);
''')


def test_busy_and_plan_version_changes_preserve_advisory_safety():
    client(r'''
fire('planning-busy',{busy:true});assert.equal($('risk-advisories').querySelector('button').disabled,true);
fire('planning-busy',{busy:false});assert.equal($('risk-advisories').querySelector('button').disabled,false);
fire('planning-state',{version:8,demoRunId:'second'});
assert.equal($('risk-routes').children.length,0);assert.equal($('risk-advisories').children.length,0);
await refresh();assert.equal($('risk-error').hidden,false);assert.equal($('risk-forecasts').children.length,0);
''')


def test_demo_visit_rows_never_open_fact_actions_for_fictitious_requests():
    client(r'''
$('risk-demo').click();
assert.match($('risk-model-status').textContent,/Учебный пример/);
assert.equal($('risk-routes').querySelectorAll('button').length,0);
assert.equal($('risk-advisories').children.length,0);
''')


def test_overtime_estimate_is_labelled_for_routes_and_available_vehicle_score_is_honest():
    client(r'''
snapshot.forecast.risks=[{id:'vehicle',measure:'heuristic_score',value:64}];await refresh();
const overtime=$('risk-forecasts').children.find(x=>x.dataset.risk==='overtime');
assert.match(overtime.textContent,/маршрут/);
const vehicle=$('risk-forecasts').children.find(x=>x.dataset.risk==='vehicle');
assert.match(vehicle.textContent,/64 \/ 100/);assert.match(vehicle.textContent,/не вероятность/);
''')


@pytest.mark.parametrize('panel', ['demo', 'technical'])
def test_new_owning_panels_load_data_without_opening_control_day_first(panel):
    client('''
fire('planning-state',{version:7,demoRunId:'fresh'});
assert.equal($('risk-demo').disabled,true);
fire('workbench-tab',{name:''' + json.dumps(panel) + '''});await settle();
assert.equal($('risk-demo').disabled,false);
assert.match($('risk-engine-meta').textContent,/risk-engine/);
''')


def test_full_diagnostics_remain_available_only_for_current_snapshot():
    client(r'''
const full=JSON.parse($('risk-raw-payload').textContent);
assert.equal(full.forecast.routes[0].visits[0].service_duration.p90_minutes,101);
assert.equal(full.forecast.expected_affected_customers,.62);
fire('planning-state',{version:8,demoRunId:'new'});
assert.equal($('risk-raw-payload').textContent,'');assert.equal($('risk-raw-details').hidden,true);
''')
