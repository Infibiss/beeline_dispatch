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


def client(expression, before=''):
    executable = shutil.which('node')
    if not executable:
        pytest.skip('Node.js is required for client checks')
    harness = Path(__file__).with_name('risk_ui_harness.cjs')
    script = 'const {assert,$,snapshot,intents,fire,settle,refresh,setResponse}=require(' + json.dumps(str(harness)) + ');'
    script += '\n' + before
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
assert.match($('risk-model-status').textContent,/Точность.*не подтверждена/);
assert.match($('risk-forecasts').textContent,/стоит проверить/i);
assert.doesNotMatch($('risk-forecasts').textContent,/0,62|0\.62|потер/i);
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
assert.match(overtime.textContent,/бригад/);
const vehicle=$('risk-forecasts').children.find(x=>x.dataset.risk==='vehicle');
assert.match(vehicle.textContent,/64 \/ 100/);assert.match(vehicle.textContent,/не вероятность/);
''')


@pytest.mark.parametrize('panel', ['demo'])
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


def test_forecast_counts_flagged_visits_and_crews_not_plan_wide_probability():
    result = node('''
      const f={p_sla_breach:1,review:{sla:{count:2,total:3},late:{count:0,total:3},overtime:{count:1,total:2}}};
      console.log(JSON.stringify(risk.forecastCounts(f)));
    ''')
    assert result == {'sla': {'count': 2, 'total': 3}, 'late': {'count': 0, 'total': 3}, 'overtime': {'count': 1, 'total': 2}}


def test_finite_simulation_frequency_does_not_round_near_edges_to_certainty():
    result = node('console.log(JSON.stringify([0,1/600,599/600,1,null].map(p=>risk.simulationFrequency(p,600))));')
    assert result == ['0% сценариев · 0 из 600', '<1% сценариев · 1 из 600',
                      '>99% сценариев · 599 из 600', '100% сценариев · 600 из 600', 'Нет оценки']


def test_malformed_route_details_and_unknown_evaluation_time_are_not_current():
    client(r'''
snapshot.forecast.routes=[{engineer_id:'E-1',visits:{}}];await refresh();
assert.equal($('risk-routes').children.length,0);
snapshot.forecast.evaluated_at=null;await refresh();
assert.equal($('risk-forecasts').children.length,0);
assert.match($('risk-model-status').textContent,/устарел/);
''')


def test_visit_table_uses_earliest_deadline_and_only_reviewed_future_visits():
    client(r'''
const route=snapshot.forecast.routes[0];
route.visits[0].emergency_response_sla_deadline=800;
route.visits[0].expected_late_minutes=12.4;
route.visits.push({request_id:'completed',planned_start:500,p_sla_breach:1});
await refresh();
assert.match($('risk-routes').textContent,/11:30/);
assert.doesNotMatch($('risk-routes').textContent,/13:20|completed/);
assert.match($('risk-routes').textContent,/31% сценариев.*186 из 600/);
assert.match($('risk-routes').textContent,/≈ 12 мин/);
snapshot.forecast.evaluated_at=590;await refresh();
assert.equal($('risk-forecasts').children.length,0);
assert.equal($('risk-route-details').hidden,true);
''')


def test_partial_missing_probabilities_are_not_reported_as_zero_risk():
    result = node('''
      console.log(JSON.stringify(risk.forecastCounts({routes:[{visits:[{p_sla_breach:.4},{}]}]})));
    ''')
    assert result == {'sla': None, 'late': None, 'overtime': None}


def test_ai_summary_is_opt_in_safe_and_invalidated_by_plan_change():
    client(r'''
snapshot.snapshot_id='snapshot-7';snapshot.llm_enabled=true;await refresh();
let calls=0;
const original=global.fetch;
global.fetch=async(url,opts)=>{
 if(url.endsWith('/summary')){calls++;assert.deepEqual(JSON.parse(opts.body),{snapshot_id:'snapshot-7',version:7,demo_run_id:null});return {ok:true,json:async()=>({snapshot_id:'snapshot-7',title:'На что обратить внимание',items:[{title:'Без исполнителя',detail:'2 заявки <script>опасно</script>'}],action:'Проверьте очередь.'})};}
 return original(url,opts);
};
assert.equal(calls,0);await $('risk-summary-button').onclick();
assert.equal(calls,1);assert.equal($('risk-summary').hidden,false);
assert.match($('risk-summary').textContent,/Проверьте очередь/);
assert.equal($('risk-summary').querySelectorAll('script').length,0);
fire('planning-state',{version:8});
assert.equal($('risk-summary').hidden,true);assert.equal($('risk-summary-button').disabled,true);
''')


def test_late_ai_summary_cannot_reappear_after_reset():
    client(r'''
snapshot.snapshot_id='snapshot-7';snapshot.llm_enabled=true;await refresh();
let resolve;
global.fetch=()=>new Promise(r=>resolve=r);
const pending=$('risk-summary-button').onclick();
fire('planning-state',{version:7,demoRunId:'new-run'});
resolve({ok:true,json:async()=>({snapshot_id:'snapshot-7',title:'Old',items:[{title:'Old',detail:'Old'}],action:'Old'})});
await pending;
assert.equal($('risk-summary').hidden,true);assert.equal($('risk-summary').textContent,'');
''')


def test_background_refresh_preserves_summary_only_while_facts_are_unchanged():
    client(r'''
snapshot.snapshot_id='snapshot-7';snapshot.llm_enabled=true;await refresh();
fire('workbench-tab',{name:'risk'});await settle();
let resolve,calls=0;
const original=global.fetch;
global.fetch=(url,opts)=>{
 if(url.endsWith('/summary'))return new Promise(r=>resolve=r);
 calls++;return original(url,opts);
};
const pending=$('risk-summary-button').onclick();
window.refreshTick();await settle();assert.equal(calls,0,'Do not interrupt a pending summary');
resolve({ok:true,json:async()=>({snapshot_id:'snapshot-7',title:'Сводка',items:[{title:'Без исполнителя',detail:'2 заявки'}],action:'Проверьте очередь.'})});
await pending;
const same=structuredClone(snapshot);same.snapshot_id='snapshot-8';setResponse(same);window.refreshTick();await settle();
assert.equal(calls,1);assert.equal($('risk-summary').hidden,false);
assert.match($('risk-summary').textContent,/Проверьте очередь/);
const changed=structuredClone(same);changed.summary.unassigned=3;setResponse(changed);window.refreshTick();await settle();
assert.equal($('risk-summary').hidden,true);assert.equal($('risk-summary').textContent,'');
''', before="window.setInterval=cb=>{window.refreshTick=cb;};")


ATTENTION = r'''
snapshot.attention={plan_version:7,time:600,counts:{unassigned:8,deadline:0,tight:1,verify:0,overtime:0},notices:['Нет свежих GPS-данных.'],items:
Array.from({length:8},(_,i)=>({id:'issue-'+i,kind:'unassigned',categories:['unassigned'],evidence:'fact',title:'Нет исполнителя',
 detail:'Назначение не найдено. Проверьте ограничения.',request_ids:['R-'+i],requests:[{request_id:'R-'+i,address:'Адрес '+i,priority_label:'Обычная',windows:[{start:600,end:700}],deadline:700}],
 engineer_id:null,engineer_name:null,deadline:700,minutes_to_deadline:100,plan_version:7,severity:'medium',action:'preview'}))};
snapshot.attention.items[1].categories.push('tight');
await refresh();
'''


def test_attention_filters_search_pagination_and_resource_notes():
    client(ATTENTION + r'''
assert.equal($('risk-attention-list').querySelectorAll('.risk-issue').length,6);
$('risk-attention-list').querySelector('.risk-tier-more').click();
assert.equal($('risk-attention-list').querySelectorAll('.risk-issue').length,8);
$('risk-attention-filters').children[2].click();
assert.equal($('risk-attention-list').querySelectorAll('.risk-issue').length,1);
assert.match($('risk-attention-list').textContent,/Адрес 1/);
$('risk-attention-all').click();$('risk-attention-search').value='R-7';
for(const h of $('risk-attention-search').events.input)h();
assert.equal($('risk-attention-list').querySelectorAll('.risk-issue').length,1);
assert.match($('risk-attention-list').textContent,/Адрес 7/);
assert.match($('risk-data-notices').textContent,/GPS/);
assert.equal($('risk-advisories').children.length,0,'No duplicate legacy incidents');
''')


def test_attention_updates_counters_does_not_apply_and_preserves_busy_guard():
    client(ATTENTION + r'''
const action=$('risk-attention-list').querySelector('button');
fire('planning-busy',{busy:true});assert.equal(action.disabled,true);action.click();assert.equal(intents.length,0);
fire('planning-busy',{busy:false});action.click();
assert.equal(intents.at(-1).action,'preview');assert.equal(intents.at(-1).planVersion,7);
snapshot.attention.items=[];snapshot.attention.counts={unassigned:0,tight:0,verify:0,overtime:0};await refresh();
assert.match($('risk-attention-list').textContent,/не найдено/);
assert.equal($('risk-attention-filters').children[0].querySelector('strong').textContent,'0');
''')


def test_attention_is_invalidated_on_time_change_even_without_new_version():
    client(ATTENTION + r'''
fire('planning-state',{version:7,time:610,demoRunId:'same'});
assert.equal($('risk-attention-list').children.length,0);
await refresh();assert.equal($('risk-error').hidden,false);
snapshot.context.time=610;snapshot.attention.time=610;await refresh();
assert.equal($('risk-error').hidden,true);assert.equal($('risk-attention-list').querySelectorAll('.risk-issue').length,6);
snapshot.attention.plan_version=6;await refresh();
assert.match($('risk-attention-list').textContent,/недоступен/);
assert.equal($('risk-attention-filters').children[0].querySelector('strong').textContent,'—');
''')


def test_demo_forecast_never_replaces_real_incidents_and_strings_are_not_html():
    client(ATTENTION + r'''
snapshot.attention.items[0].requests[0].address='<img src=x onerror=alert(1)>';
await refresh();$('risk-demo').click();
assert.equal($('risk-attention-list').querySelectorAll('.risk-issue').length,6);
assert.match($('risk-attention-list').textContent,/<img/);
assert.equal($('risk-attention-list').querySelectorAll('img').length,0);
''')


def test_three_tiers_deadline_filter_and_reason_action_are_visible_without_percentages():
    client(ATTENTION + r'''
snapshot.attention.items=snapshot.attention.items.slice(0,3);
snapshot.attention.items.forEach((item,i)=>{
 item.tier=['now','prepare','watch'][i];item.reason='Фактическая причина неизвестна';
 item.next_step='Связаться с инженером перед выездом';item.impact='Ближайшая заявка R-'+i;
});
snapshot.attention.items[2].categories.push('deadline');snapshot.attention.counts.deadline=1;
await refresh();
let groups=$('risk-attention-list').children;
assert.deepEqual(groups.map(g=>g.dataset.tier),['now','prepare','watch']);
assert.equal(groups[1].open,false);assert.equal(groups[2].open,false);
assert.match(groups[0].textContent,/Связаться с инженером перед выездом/);
assert.match(groups[0].textContent,/Фактическая причина неизвестна/);
$('risk-attention-filters').children[1].click();
groups=$('risk-attention-list').children;
assert.equal(groups[2].open,true,'Category selection reveals matching watch incidents');
assert.equal($('risk-attention-list').querySelectorAll('.risk-issue').length,1);
assert.match(groups[2].textContent,/R-2/);
''')


def test_pending_incoming_is_separate_from_plan_counts_and_cleared_on_invalidation():
    client(ATTENTION + r'''
snapshot.attention.incoming={new:2,emergency:1};await refresh();
assert.equal($('risk-incoming').hidden,false);
assert.match($('risk-incoming').textContent,/2 новых заявок.*аварий: 1/);
assert.equal($('risk-attention-filters').children[0].querySelector('strong').textContent,'8');
$('risk-incoming').querySelector('button').click();assert.equal(intents.at(-1).action,'replan');
snapshot.attention.incoming.new=0;await refresh();assert.equal($('risk-incoming').hidden,true);
snapshot.attention.incoming.new=2;await refresh();
fire('planning-state',{version:8,time:600});assert.equal($('risk-incoming').hidden,true);
''')
