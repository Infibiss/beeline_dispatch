import pytest
from test_replanning import demo
from field_router.replanning import ScenarioSession


def test_workbench_tracks_published_versions(make_scenario):
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    assert session.workbench(1)['plans'][0]['metrics']['assigned']==2
    batch=session.preview({'version':1,'event':{'id':'c','type':'cancel','time':540,'request_id':'b'}},time_limit=.03)
    session.approve({'version':1,'batch_id':batch['id'],'candidate_id':'stable'})
    assert session.workbench(1)['plans'][0]['metrics']['assigned']==2
    assert session.workbench(2)['plans'][0]['metrics']['assigned']==1
    assert len(session.state()['versions'])==2


def test_manual_work_does_not_complete_by_clock(make_scenario):
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    session.record_fact({'version':1,'time':550,'engineer_id':'e1','request_id':'a','status':'working','remaining_minutes':45})
    batch=session.preview({'version':2,'event':{'id':'r','type':'replan','time':610},'preserve_in_progress':True},time_limit=.03)
    assert batch['inventory'][0]['consumed']['router']==0
    assert batch['inventory'][0]['reserved']['router']==1
    assert any(v['request_id']=='a' and v['end']>=655 for r in batch['frozen'] for v in r['visits'])


def test_completion_spends_once_and_invalidates_proposals(make_scenario):
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    batch=session.preview({'version':1,'event':{'id':'r','type':'cancel','time':540,'request_id':'b'}},time_limit=.03)
    session.record_fact({'version':1,'time':590,'engineer_id':'e1','request_id':'a','status':'completed'})
    with pytest.raises(ValueError):session.approve({'version':1,'batch_id':batch['id'],'candidate_id':'stable'})
    with pytest.raises(ValueError):session.record_fact({'version':2,'time':591,'engineer_id':'e1','request_id':'a','status':'completed'})
    new=session.preview({'version':2,'event':{'id':'r2','type':'replan','time':600}},time_limit=.03)
    assert new['inventory'][0]['consumed']['router']==1
    assert all(not any(v['request_id']=='a' for r in c['plan']['routes'] for v in r['visits']) for c in new['candidates'])


def test_interruption_requires_explicit_consumption_and_remaining_work(make_scenario):
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    session.record_fact({'version':1,'time':550,'engineer_id':'e1','request_id':'a','status':'working','remaining_minutes':30})
    with pytest.raises(ValueError):session.record_fact({'version':2,'time':560,'engineer_id':'e1','request_id':'a','status':'interrupted','remaining_minutes':20})
    session.record_fact({'version':2,'time':560,'engineer_id':'e1','request_id':'a','status':'interrupted','remaining_minutes':20,'consumed_router':1,'consumed_tv_box':0})
    batch=session.preview({'version':3,'event':{'id':'r','type':'replan','time':560}},time_limit=.03)
    assert batch['inventory'][0]['consumed']['router']==1
    pending=session.batches[batch['id']]['scenario']
    assert next(r for r in pending.requests if r.id=='a').duration_minutes==20
    assert next(r for r in pending.requests if r.id=='a').equipment.router==0


def test_completion_after_interruption_does_not_spend_device_twice(make_scenario):
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    for payload in [
        {'time':550,'status':'working','remaining_minutes':30},
        {'time':560,'status':'interrupted','remaining_minutes':20,'consumed_router':1,'consumed_tv_box':0},
        {'time':565,'status':'working','remaining_minutes':20},
        {'time':585,'status':'completed'}]:
        session.record_fact({'version':session.version,'engineer_id':'e1','request_id':'a',**payload})
    batch=session.preview({'version':session.version,'event':{'id':'r','type':'replan','time':590}},time_limit=.03)
    assert batch['inventory'][0]['consumed']['router']==1


def test_available_restores_original_shift_after_unavailable(make_scenario):
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    for now,status in [(550,'unavailable'),(560,'available')]:
        session.record_fact({'version':session.version,'time':now,'engineer_id':'e1','status':status,'location_id':'office'})
        batch=session.preview({'version':session.version,'event':{'id':status,'type':'replan','time':now}},time_limit=.03)
        session.approve({'version':session.version,'batch_id':batch['id'],'candidate_id':'stable'})
    assert next(e for e in session.scenario.engineers if e.id=='e1').shift.end==1080


def test_consumption_is_idempotent_over_repeated_replans(make_scenario):
    from field_router.models import Equipment
    from field_router.planning import make_plan
    from field_router.travel import synthetic_travel
    s,p=demo(make_scenario)
    s=s.model_copy(update={'requests':[r.model_copy(update={'equipment':Equipment(router=2)}) if r.id=='a' else r for r in s.requests]})
    p=make_plan(s,synthetic_travel(s),{'e1':['a','b']},'demo','valid');session=ScenarioSession(s,p)
    session.record_fact({'version':1,'time':550,'engineer_id':'e1','request_id':'a','status':'working','remaining_minutes':30})
    session.record_fact({'version':2,'time':560,'engineer_id':'e1','request_id':'a','status':'interrupted','remaining_minutes':20,'consumed_router':1,'consumed_tv_box':0})
    for i in range(2):
        batch=session.preview({'version':session.version,'event':{'id':str(i),'type':'replan','time':560}},time_limit=.03)
        session.approve({'version':session.version,'batch_id':batch['id'],'candidate_id':'stable'})
        assert next(r for r in session.scenario.requests if r.id=='a').equipment.router==1


def test_fact_catalog_has_owner_and_excludes_completed(make_scenario):
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    assert all(r['engineer_id']=='e1' for r in session.state()['fact_requests'])
    session.record_fact({'version':1,'time':590,'engineer_id':'e1','request_id':'a','status':'completed'})
    assert 'a' not in [r['id'] for r in session.state()['fact_requests']]


def test_available_does_not_carry_stale_request_fields(make_scenario):
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    session.record_fact({'version':1,'time':550,'engineer_id':'e1','request_id':'a','status':'available','location_id':'office','remaining_minutes':50})
    assert session.statuses['e1']['request_id'] is None
    assert session.statuses['e1']['remaining_minutes'] is None


@pytest.mark.parametrize('fact',[
    {'engineer_id':'e2','request_id':'a','status':'completed'},
    {'engineer_id':'e1','request_id':'a','status':'working','remaining_minutes':0},
    {'engineer_id':'e1','request_id':'a','status':'completed','consumed_router':999},
    {'engineer_id':'e1','status':'available','location_id':'missing'},
])
def test_invalid_facts_leave_state_unchanged(make_scenario,fact):
    from copy import deepcopy
    s,p=demo(make_scenario);session=ScenarioSession(s,p);before=deepcopy(session.dump())
    with pytest.raises(ValueError):session.record_fact({'version':1,'time':550,**fact})
    assert session.dump()==before


@pytest.mark.parametrize('status,request_id', [('available',None),('traveling','b'),('unavailable',None),('working','b'),('completed','b')])
def test_busy_engineer_requires_explicit_end_before_switch(make_scenario,status,request_id):
    from copy import deepcopy
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    session.record_fact({'version':1,'time':550,'engineer_id':'e1','request_id':'a','status':'working','remaining_minutes':30})
    before=deepcopy(session.dump())
    with pytest.raises(ValueError):session.record_fact({'version':2,'time':555,'engineer_id':'e1','request_id':request_id,'status':status,'remaining_minutes':5,'location_id':'office'})
    assert session.dump()==before
