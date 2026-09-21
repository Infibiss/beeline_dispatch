from copy import deepcopy
import json
import pytest
from conftest import engineer
from field_router.models import Equipment
from field_router.replanning import ScenarioSession
from test_replanning import demo
from test_real_replanning import real_session
from test_auth import secured, Client
from test_scenarios_ui import client


def session_with_spare(make_scenario):
    scenario,plan=demo(make_scenario)
    scenario=scenario.model_copy(update={'engineers':scenario.engineers+[engineer('e3',stock=Equipment(router=2))]})
    return ScenarioSession(scenario,plan)


def exclude(session, ids, time=540, **event):
    return session.preview({'version':session.version,'event':{'id':'exclude-'+str(session.version),
        'type':'unavailable','time':time,'engineer_ids':ids,**event},'preserve_in_progress':True},time_limit=.06)


def approve(session,batch):
    return session.approve({'version':session.version,'batch_id':batch['id'],'candidate_id':'stable'})


def assert_no_assignment(batch, ids):
    for candidate in batch['candidates']:
        assert not any(route['engineer_id'] in ids and route['visits'] for route in candidate['plan']['routes'])


def test_group_preview_is_unpublished_then_reassigns_and_keeps_roster(make_scenario):
    session=session_with_spare(make_scenario)
    original=deepcopy(session.state())
    batch=exclude(session,['e1','e2'])
    assert session.state()==original
    assert {e['id'] for e in batch['excluded_engineers']}=={'e1','e2'}
    assert_no_assignment(batch,{'e1','e2'})
    assert all(candidate['facts']['assigned']==2 for candidate in batch['candidates'])
    result=approve(session,batch)
    assert result['version']==2 and result['excluded_engineer_ids']==['e1','e2']
    assert {e['id'] for e in result['engineers']}=={'e1','e2','e3'}
    assert {r.engineer_id for r in session.plan.routes if r.visits}=={'e3'}
    assert not session.fact_log


def test_all_engineers_can_be_excluded_and_losses_are_visible(make_scenario):
    scenario,plan=demo(make_scenario);session=ScenarioSession(scenario,plan)
    batch=exclude(session,['e1','e2'])
    assert_no_assignment(batch,{'e1','e2'})
    assert all(c['facts']['unassigned']==2 and c['facts']['lost_assignments']==2 for c in batch['candidates'])
    approve(session,batch)
    restored=ScenarioSession.restore(json.loads(json.dumps(session.dump())))
    again=restored.preview({'version':2,'event':{'id':'again','type':'replan','time':540}},time_limit=.03)
    assert_no_assignment(again,{'e1','e2'})


@pytest.mark.parametrize('ids,extra',[
    ([],{}),(['e1','e1'],{}),(['ghost'],{}),([''],{}),
    (['e1'],{'engineer_id':'e2'}),(['e1'],{'type':'replan'}),
])
def test_invalid_group_is_rejected_without_partial_changes(make_scenario,ids,extra):
    session=session_with_spare(make_scenario);before=deepcopy(session.state())
    with pytest.raises(ValueError):exclude(session,ids,**extra)
    assert session.state()==before and not session.batches


def test_started_work_blocks_whole_group_and_other_work_is_preserved(make_scenario):
    session=session_with_spare(make_scenario);before=deepcopy(session.state())
    with pytest.raises(ValueError,match='e1.*работ'):
        exclude(session,['e2','e1'],time=560)
    assert session.state()==before and not session.batches
    batch=exclude(session,['e2'],time=560)
    assert any(route['engineer_id']=='e1' and route['visits'][0]['request_id']=='a' for route in batch['frozen'])


def test_exclusion_persists_across_facts_restore_and_new_preview(make_scenario):
    session=session_with_spare(make_scenario)
    session.record_fact({'version':1,'engineer_id':'e1','status':'available','time':540,'location_id':'office'})
    approve(session,exclude(session,['e1','e2'],time=541))
    session=ScenarioSession.restore(json.loads(json.dumps(session.dump())))
    session.record_fact({'version':session.version,'engineer_id':'e3','status':'available','time':542,'location_id':'office'})
    batch=session.preview({'version':session.version,'event':{'id':'next','type':'replan','time':542},'preserve_in_progress':True},time_limit=.04)
    assert_no_assignment(batch,{'e1','e2'})
    assert session.state()['excluded_engineer_ids']==['e1','e2']
    session.record_fact({'version':session.version,'engineer_id':'e1','status':'available','time':543,'location_id':'office'})
    assert session.state()['excluded_engineer_ids']==['e2']
    restored=session.preview({'version':session.version,'event':{'id':'return','type':'replan','time':543},'preserve_in_progress':True},time_limit=.04)
    assert 'e1' in {e.id for e in session.batches[restored['id']]['scenario'].engineers}


def test_road_worker_can_be_excluded_mid_trip_without_invented_position(real_session):
    # This worker is being removed, so no fresh road position is needed for new work.
    batch=exclude(real_session,['e1'],time=545)
    assert_no_assignment(batch,{'e1'})
    assert all(c['facts']['unassigned']==2 for c in batch['candidates'])


def test_group_releases_a_previously_frozen_road_trip(real_session):
    first=real_session.preview({'version':1,'event':{'id':'freeze','type':'replan','time':545},
                                'preserve_in_progress':True},time_limit=.03)
    approve(real_session,first)
    assert real_session.frozen
    batch=exclude(real_session,['e1'],time=546)
    assert not batch['frozen']
    assert_no_assignment(batch,{'e1'})
    assert all(c['facts']['unassigned']==2 for c in batch['candidates'])


def test_http_group_requires_dispatcher_and_approval_and_is_versioned(secured):
    server,_,session=secured
    employee=Client(server);employee.login('worker1')
    admin=Client(server);admin.login('admin')
    payload={'version':1,'event':{'id':'group','type':'unavailable','time':540,'engineer_ids':['e1','e2']},'preserve_in_progress':True}
    assert employee.request('/api/preview',payload)[0]==403
    before=admin.request('/api/state')[1]
    code,batch,_=admin.request('/api/preview',payload)
    assert code==200 and admin.request('/api/state')[1]==before
    assert admin.request('/api/approve',{'version':1,'batch_id':batch['id'],'candidate_id':'stable'})[0]==200
    assert admin.request('/api/preview',payload)[0]==409
    after=admin.request('/api/state')[1]
    assert after['excluded_engineer_ids']==['e1','e2']
    assert len(admin.request('/api/engineers')[1]['engineers'])==2
    assert employee.request('/api/engineer?engineer_id=e1')[0]==200
    assert admin.request('/r/south-east/api/state')[1]['excluded_engineer_ids']==[]


def test_client_group_payload_and_empty_selection():
    result=client("console.log(JSON.stringify(core.eventPayload(4,{id:'gps-group',type:'unavailable',time:'09:30',engineerIds:['e1','e2'],preserve:true})))")
    assert result['event']['engineer_ids']==['e1','e2'] and result['event']['time']==570
    assert result['preserve_in_progress'] is True
    assert 'engineer_id' not in result['event']
    rejected=client("console.log(JSON.stringify([[],['e1','e1']].map(ids=>{try{core.eventPayload(4,{id:'x',type:'unavailable',time:'09:30',engineerIds:ids});return false}catch{return true}})))")
    assert rejected==[True,True]
