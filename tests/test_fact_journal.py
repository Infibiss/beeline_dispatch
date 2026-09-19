from copy import deepcopy
import pytest
from test_replanning import demo
from field_router.replanning import ScenarioSession


def test_late_completion_keeps_clock_and_versions(make_scenario):
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    session.record_fact({'version':1,'time':620,'engineer_id':'e2','status':'available','location_id':'office'})
    old=deepcopy(session.workbench(2))
    session.record_fact({'version':2,'time':580,'engineer_id':'e1','request_id':'a','status':'completed'})
    assert session.time==620
    old['operations']['historical']=True
    assert session.workbench(2)==old
    assert session.fact_log[-1]['id'] and session.fact_log[-1]['recorded_at']
    batch=session.preview({'version':3,'event':{'id':'late','type':'replan','time':620},'preserve_in_progress':True},time_limit=.03)
    assert all('a' not in [v['request_id'] for r in c['plan']['routes'] for v in r['visits']] for c in batch['candidates'])


def test_correct_completion_replays_consumption_and_preserves_audit(make_scenario):
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    session.record_fact({'version':1,'time':580,'engineer_id':'e1','request_id':'a','status':'completed'})
    identity=session.fact_log[0]['id']
    session.record_fact({'version':2,'fact_id':identity,'correction_reason':'Роутер не понадобился','time':585,'engineer_id':'e1','request_id':'a','status':'completed','consumed_router':0,'consumed_tv_box':0})
    assert len(session.fact_log)==1
    assert session.inventory[0]['consumed']['router']==0
    assert len(session.fact_audit)==2
    assert session.fact_audit[-1]['replaces']==identity
    restored=ScenarioSession.restore(session.dump())
    assert restored.fact_audit==session.fact_audit


def test_conflicting_late_fact_is_atomic(make_scenario):
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    session.record_fact({'version':1,'time':590,'engineer_id':'e1','request_id':'a','status':'completed'})
    before=deepcopy(session.dump())
    with pytest.raises(ValueError):session.record_fact({'version':2,'time':580,'engineer_id':'e1','request_id':'b','status':'working','remaining_minutes':20})
    assert session.dump()==before


def test_correction_after_approval_can_restore_unfinished_job(make_scenario):
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    session.record_fact({'version':1,'time':580,'engineer_id':'e1','request_id':'a','status':'completed'})
    identity=session.fact_log[0]['id']
    b=session.preview({'version':2,'event':{'id':'r','type':'replan','time':590}},time_limit=.03)
    session.approve({'version':2,'batch_id':b['id'],'candidate_id':'stable'})
    session.record_fact({'version':3,'fact_id':identity,'correction_reason':'Работа ещё идет','time':580,'engineer_id':'e1','request_id':'a','status':'working','remaining_minutes':10})
    b=session.preview({'version':4,'event':{'id':'r2','type':'replan','time':590},'preserve_in_progress':True},time_limit=.03)
    assert any(v['request_id']=='a' for r in b['frozen'] for v in r['visits'])
    assert b['inventory'][0]['consumed']['router']==0
    assert b['inventory'][0]['reserved']['router']==1


def test_manual_mode_does_not_assume_unreported_engineers_available(make_scenario):
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    session.set_mode({'version':1,'mode':'manual'})
    b=session.preview({'version':2,'event':{'id':'manual','type':'replan','time':540}},time_limit=.03)
    assert all(c['facts']['assigned']==0 for c in b['candidates'])
    assert session.state()['mode']=='manual'


def test_late_interruption_recomputes_default_completion_consumption(make_scenario):
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    session.record_fact({'version':1,'time':550,'engineer_id':'e1','request_id':'a','status':'working','remaining_minutes':30})
    session.record_fact({'version':2,'time':590,'engineer_id':'e1','request_id':'a','status':'completed'})
    session.record_fact({'version':3,'time':560,'engineer_id':'e1','request_id':'a','status':'interrupted','remaining_minutes':20,'consumed_router':1,'consumed_tv_box':0})
    assert sum(f['consumed']['router'] for f in session.fact_log)==1
    assert session.statuses['e1']['status']=='completed'
    assert session.time==590


def test_correction_requires_reason_and_preserves_previous_on_failure(make_scenario):
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    session.record_fact({'version':1,'time':580,'engineer_id':'e1','request_id':'a','status':'completed'})
    before=deepcopy(session.dump())
    with pytest.raises(ValueError):session.record_fact({'version':2,'fact_id':session.fact_log[0]['id'],'time':585,'engineer_id':'e1','request_id':'a','status':'completed'})
    assert session.dump()==before
