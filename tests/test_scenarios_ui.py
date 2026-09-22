"""Small client contract checks; end-to-end API behavior lives in server tests."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

STATIC = Path(__file__).resolve().parents[1] / 'src/field_router/static'


def client(expression):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for client contract checks')
    script = STATIC / 'scenarios.js'
    assert script.exists(), 'Scenario Center client is required'
    result = subprocess.run([node, '-e', 'const core=require(' + json.dumps(str(script)) + ');' + expression], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_event_input_preserves_redirect_choice_but_never_interrupts_work():
    result = client("console.log(JSON.stringify(core.eventPayload(4,{id:'evt-1',type:'emergency',time:'10:35',target:'R-1',duration:'80',router:'0',redirect:true,preserve:false})))")
    assert result == {'version': 4, 'event': {'id': 'evt-1', 'type': 'emergency', 'time': 635, 'anchor_id': 'R-1', 'duration_minutes': 80, 'router': 0}, 'allow_redirect': True, 'preserve_in_progress': True}


def test_event_time_rejects_invalid_values_before_request():
    result = client("console.log(JSON.stringify(['25:00','08:60','', '8:00'].map(time=>{try{core.eventPayload(1,{id:'x',type:'cancel',time,target:'r'});return false}catch{return true}})))")
    assert result == [True, True, True, True]


def test_screen_has_local_assets_and_explicit_disabled_approval():
    html = (STATIC / 'scenarios.html').read_text()
    assert '<html lang="ru">' in html
    assert 'src="scenarios.js"' in html
    assert 'href="scenarios.css"' in html
    assert 'id="approve" type="button" disabled' in html
    assert 'role="alert"' in html
    assert '<noscript>' in html
    assert 'https://' not in html


def test_replan_has_no_request_target():
    result = client("console.log(JSON.stringify(core.eventPayload(4,{id:'replan-1',type:'replan',time:'11:00',target:''})))")
    assert result == {'version': 4, 'event': {'id': 'replan-1', 'type': 'replan', 'time': 660}, 'allow_redirect': False, 'preserve_in_progress': True}


def test_completion_omits_blank_consumption_for_server_defaults():
    result = client("console.log(JSON.stringify(core.factPayload(4,{engineer_id:'E-1',status:'completed',request_id:'R-1',time:'11:00',consumed_router:'',consumed_tv_box:''})))")
    assert result == {'version': 4, 'mode': 'manual', 'engineer_id': 'E-1', 'status': 'completed', 'request_id': 'R-1', 'time': 660}


def test_interruption_requires_explicit_remaining_and_consumption():
    result = client("const base={engineer_id:'E-1',status:'interrupted',request_id:'R-1',time:'11:00',remaining_minutes:'25',consumed_router:'0',consumed_tv_box:'1'}; console.log(JSON.stringify({valid:core.factPayload(4,base),rejected:['remaining_minutes','consumed_router','consumed_tv_box'].map(key=>{try{core.factPayload(4,{...base,[key]:''});return false}catch{return true}})}))")
    assert result['valid']['remaining_minutes'] == 25
    assert result['valid']['consumed_router'] == 0
    assert result['valid']['consumed_tv_box'] == 1
    assert result['rejected'] == [True, True, True]


def test_working_location_is_resolved_from_request_on_server():
    result = client("console.log(JSON.stringify(core.factPayload(4,{engineer_id:'E-1',status:'working',request_id:'R-1',location_id:'wrong',time:'11:00',remaining_minutes:'30'})))")
    assert 'location_id' not in result
    assert result['request_id'] == 'R-1'


def test_fact_input_rejects_missing_request_and_negative_devices():
    result = client("console.log(JSON.stringify([{status:'working'},{status:'completed'},{status:'interrupted'},{status:'completed',request_id:'R-1',consumed_router:'-1'}].map(input=>{try{core.factPayload(4,{engineer_id:'E-1',time:'11:00',...input});return false}catch{return true}})))")
    assert result == [True, True, True, True]


def test_fact_request_filter_and_clear_irrelevant_fields():
    result=client("console.log(JSON.stringify(core.requestsForEngineer({fact_requests:[{id:'a',engineer_id:'e1'},{id:'b',engineer_id:'e2'}]},'e1')))")
    assert [r['id'] for r in result]==['a']
    result=client("console.log(JSON.stringify(core.factPayload(1,{engineer_id:'e1',time:'10:00',status:'available',request_id:'a',location_id:'office',remaining_minutes:'5'})))")
    assert 'request_id' not in result


def test_fact_input_requires_known_location_for_mobile_and_available_statuses():
    result = client("console.log(JSON.stringify(['available','traveling','unavailable'].map(status=>{try{core.factPayload(1,{engineer_id:'e1',time:'10:00',status,request_id:'r1',remaining_minutes:'15'});return false}catch{return true}})))")
    assert result == [True, True, True]


def test_fact_input_requires_bounded_remaining_time_before_submission():
    result = client("console.log(JSON.stringify(['', ' ', '0', '1441'].map(remaining_minutes=>{try{core.factPayload(1,{engineer_id:'e1',time:'10:00',status:'working',request_id:'r1',remaining_minutes});return false}catch{return true}})))")
    assert result == [True, True, True, True]
