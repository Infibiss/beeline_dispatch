"""Companion dashboards must not revive another simulation run's state."""
import json
import shutil
import subprocess

import pytest

from test_strategy_ui import HARNESS, Markup, STATIC


def companion(script_name, expression, before=''):
    executable=shutil.which('node')
    if not executable:pytest.skip('Node.js is required for client checks')
    markup=Markup((STATIC/'scenarios.html').read_text(encoding='utf-8')).tree
    boundary=r'''
document.createElementNS=(namespace,tag)=>Object.assign(new Element(tag),{namespaceURI:namespace});
document.addEventListener=()=>{};
Element.prototype.contains=function(target){return this===target || this.children.some(child=>child.contains(target));};
const planning=detail=>window.dispatchEvent(new CustomEvent('planning-state',{detail}));
const tab=name=>window.dispatchEvent(new CustomEvent('workbench-tab',{detail:{name}}));
'''
    script='const markup='+json.dumps(markup)+';'+HARNESS+boundary+before
    script+='\nrequire('+json.dumps(str(STATIC/script_name))+');'
    script+='\n(async()=>{await settle();'+expression+'})().catch(error=>{console.error(error);process.exitCode=1;});'
    result=subprocess.run([executable,'-'],input=script,capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stderr


def test_same_version_new_run_discards_inflight_risk_and_clears_old_cards():
    companion('risk-dashboard.js',r'''
planning({version:1,demoRunId:'run-a'});
tab('risk');
pending[0]({ok:true,json:async()=>snapshot('Старый запуск')});
await settle();
assert.match($('risk-context').textContent,/Старый запуск/);
assert.equal($('risk-indicators').children.length,3);
const lateRefresh=$('risk-refresh').onclick();
assert.equal(pending.length,2);
planning({version:1,demoRunId:'run-b'});
assert.equal(pending.length,3,'Same version but a new run requests fresh risk');
assert.equal($('risk-indicators').children.length,0,'Old risk is removed before replacement arrives');
pending[2]({ok:true,json:async()=>snapshot('Новый запуск')});
await settle();
assert.match($('risk-context').textContent,/Новый запуск/);
pending[1]({ok:true,json:async()=>snapshot('Просроченный ответ')});
await lateRefresh;
assert.match($('risk-context').textContent,/Новый запуск/);
assert.doesNotMatch($('risk-context').textContent,/Просроченный/);
assert.equal($('risk-indicators').children.length,3);
''',before=r'''
const pending=[];
global.fetch=()=>new Promise(resolve=>pending.push(resolve));
function snapshot(region) {
  return {schema_version:'1.0',context:{region,date:'2026-09-27',plan_version:1,time:540,needs_replanning:false},
    summary:{engineers:2,roster:2,unassigned:0},forecast:{status:'not_connected'},
    indicators:['capacity','windows','equipment'].map(id=>({id,value:0,title:id,caption:'',detail:'',explanation:'',recommendation:''}))};
}
''')


def test_gps_accepts_lower_version_only_for_new_run_and_clears_old_work():
    companion('gps-dispatcher.js',r'''
planning({version:14,demoRunId:'run-a',time:600,engineers:[{id:'e1',name:'Иванов'}],excludedEngineerIds:['e1']});
assert.equal($('gps-exclusion-time').value,'10:00');
assert.equal($('gps-excluded-now').hidden,false);
$('gps-people').textContent='Старые координаты';
$('gps-metrics').textContent='Старые показатели';
$('gps-trace').textContent='Старый трек';
$('gps-diagnostics').textContent='Старый журнал';
planning({version:1,demoRunId:'run-b',time:540,engineers:[{id:'e1',name:'Иванов'}],excludedEngineerIds:[]});
assert.equal($('gps-exclusion-time').value,'09:00');
assert.equal($('gps-excluded-now').hidden,true);
for(const id of ['gps-people','gps-metrics','gps-trace','gps-diagnostics']) assert.equal($(id).textContent,'',id);
assert.equal($('gps-replay-run').disabled,true);
planning({version:2,demoRunId:'run-b',time:550,engineers:[{id:'e1',name:'Иванов'}],excludedEngineerIds:[]});
planning({version:1,demoRunId:'run-b',time:540,engineers:[{id:'e1',name:'Иванов'}],excludedEngineerIds:['e1']});
assert.equal($('gps-exclusion-time').value,'09:10','Stale lower version within the same run is still ignored');
assert.equal($('gps-excluded-now').hidden,true);
''')


def test_gps_initial_state_response_cannot_restore_previous_run_after_reset():
    companion('gps-dispatcher.js',r'''
tab('gps');
assert.equal(typeof finishInitial,'function');
planning({version:1,demoRunId:'run-b',time:540,engineers:[],excludedEngineerIds:[]});
finishInitial({ok:true,json:async()=>({version:14,demo_run_id:'run-a',time:600,engineers:[],excluded_engineer_ids:[]})});
await settle();await settle();
assert.equal($('gps-exclusion-time').value,'09:00','Late bootstrap result cannot replace reset state');
''',before=r'''
let finishInitial;
global.fetch=url=>url==='/api/state' ? new Promise(resolve=>{finishInitial=resolve;}) : Promise.resolve({ok:true,json:async()=>({
  rows:[],plan_date:'2026-09-27',plan_version:1,config:{stale_seconds:120,retention_seconds:86400},health:{enabled:false},recent_events:[]
})});
''')


def test_gps_late_history_error_stays_out_of_clean_run():
    companion('gps-dispatcher.js',r'''
planning({version:14,demoRunId:'run-a',time:600,engineers:[],excludedEngineerIds:[]});
$('gps-trace-engineer').value='e1';
const pending=$('gps-history-load').dispatch('click');
planning({version:1,demoRunId:'run-b',time:540,engineers:[],excludedEngineerIds:[]});
failHistory(Error('Ошибка прошлого запуска'));
await pending;
assert.equal($('gps-trace').textContent,'');
''',before=r'''
let failHistory;
global.fetch=()=>new Promise((resolve,reject)=>{failHistory=reject;});
''')
