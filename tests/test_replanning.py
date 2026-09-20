import pytest
from conftest import job, engineer
from field_router.models import Point, Equipment
from field_router.planning import make_plan
from field_router.travel import synthetic_travel


def demo(make_scenario):
    s=make_scenario([job('a',start=550,end=650,duration=30,equipment=Equipment(router=1)),job('b',start=610,end=700)],
        [engineer('e1'),engineer('e2')])
    s=s.model_copy(update={'points':{'office':Point(x_km=0,y_km=0),'a':Point(x_km=1,y_km=0),'b':Point(x_km=2,y_km=0)},
        'metadata':{'geography':'synthetic_cartesian_km'},'travel_config':{'speed_kmh':{m:60 for m in ('car','walk','bicycle','public')},'distance_factor':{m:1 for m in ('car','walk','bicycle','public')},'boarding_minutes':{m:0 for m in ('car','walk','bicycle','public')}}})
    return s,make_plan(s,synthetic_travel(s),{'e1':['a','b']},'demo','valid')


def test_preview_does_not_publish_and_approval_invalidates_old_batch(make_scenario):
    from field_router.replanning import ScenarioSession
    s,p=demo(make_scenario); session=ScenarioSession(s,p)
    payload={'version':1,'event':{'id':'cancel-1','type':'cancel','time':540,'request_id':'b'},'allow_redirect':False,'preserve_in_progress':True}
    batch=session.preview(payload,time_limit=.03)
    assert session.version==1
    assert len(batch['candidates'])==3
    session.approve({'version':1,'batch_id':batch['id'],'candidate_id':batch['candidates'][0]['id']})
    assert session.version==2
    assert [r.id for r in session.scenario.requests]==['a']
    with pytest.raises(ValueError,match='верси'):session.approve({'version':1,'batch_id':batch['id'],'candidate_id':batch['candidates'][0]['id']})
    with pytest.raises(ValueError,match='повтор'):session.preview({**payload,'version':2},time_limit=.03)


def test_started_work_requires_explicit_preservation_and_reserves_equipment(make_scenario):
    from field_router.replanning import ScenarioSession
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    payload={'version':1,'event':{'id':'cancel','type':'cancel','time':560,'request_id':'b'},'allow_redirect':False,'preserve_in_progress':False}
    with pytest.raises(ValueError,match='начат'):session.preview(payload,time_limit=.03)
    batch=session.preview({**payload,'preserve_in_progress':True},time_limit=.03)
    assert batch['inventory'][0]['reserved']['router']==1
    assert batch['inventory'][0]['consumed']['router']==0
    assert batch['frozen'][0]['visits'][0]['request_id']=='a'


def test_completed_work_consumes_stock_and_cannot_be_cancelled(make_scenario):
    from field_router.replanning import ScenarioSession
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    with pytest.raises(ValueError,match='заверш|закреп'):
        session.preview({'version':1,'event':{'id':'c','type':'cancel','time':590,'request_id':'a'},'preserve_in_progress':True},time_limit=.03)
    batch=session.preview({'version':1,'event':{'id':'c2','type':'cancel','time':590,'request_id':'b'},'preserve_in_progress':True},time_limit=.03)
    assert batch['inventory'][0]['consumed']['router']==1
    assert batch['inventory'][0]['reserved']['router']==0


def test_emergency_no_warehouse_restock_during_day(make_scenario):
    from field_router.replanning import ScenarioSession
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    batch=session.preview({'version':1,'event':{'id':'new','type':'emergency','time':590,'anchor_id':'b','router':1,'duration_minutes':20},'preserve_in_progress':True},time_limit=.03)
    assert all(any(u['request_id']=='emergency:new' for u in c['plan']['unassigned']) for c in batch['candidates'])


def test_reject_backdated_and_unknown_events(make_scenario):
    from field_router.replanning import ScenarioSession
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    for event in [{'id':'x','type':'cancel','time':539,'request_id':'b'}, {'id':'x','type':'unknown','time':540}]:
        with pytest.raises(ValueError):session.preview({'version':1,'event':event},time_limit=.03)


def test_second_event_cannot_remove_engineer_from_frozen_work(make_scenario):
    from field_router.replanning import ScenarioSession
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    batch=session.preview({'version':1,'event':{'id':'c','type':'cancel','time':560,'request_id':'b'},'preserve_in_progress':True},time_limit=.03)
    session.approve({'version':1,'batch_id':batch['id'],'candidate_id':'stable'})
    with pytest.raises(ValueError,match='работ'):
        session.preview({'version':2,'event':{'id':'u','type':'unavailable','time':561,'engineer_id':'e1'},'preserve_in_progress':True},time_limit=.03)


def test_cancellation_is_in_diff(make_scenario):
    from field_router.replanning import ScenarioSession
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    batch=session.preview({'version':1,'event':{'id':'c','type':'cancel','time':540,'request_id':'b'}},time_limit=.03)
    assert all(any(d['request_id']=='b' and d['after_engineer'] is None for d in c['diff']) for c in batch['candidates'])


def test_later_emergency_can_redirect_previously_frozen_trip(make_scenario):
    from field_router.replanning import ScenarioSession
    from field_router.models import Skill
    s,p=demo(make_scenario)
    s=s.model_copy(update={'requests':[r.model_copy(update={'windows':[r.windows[0].model_copy(update={'start':610,'end':750})]}) for r in s.requests],
        'engineers':[e.model_copy(update={'skills':[Skill.LOCAL,Skill.EMERGENCY], 'shift':e.shift.model_copy(update={'end':640 if e.id=='e1' else e.shift.end})}) for e in s.engineers],
        'points':{**s.points,'a':Point(x_km=60,y_km=0)}})
    p=make_plan(s,synthetic_travel(s),{'e1':['a'],'e2':['b']},'demo','valid')
    session=ScenarioSession(s,p)
    first=session.preview({'version':1,'event':{'id':'new1','type':'emergency','time':550,'anchor_id':'b','duration_minutes':10},'allow_redirect':False},time_limit=.03)
    session.approve({'version':1,'batch_id':first['id'],'candidate_id':'stable'})
    second=session.preview({'version':2,'event':{'id':'new2','type':'emergency','time':560,'anchor_id':'b','duration_minutes':10},'allow_redirect':True},time_limit=.03)
    assert not any(v['request_id']=='a' for route in second['frozen'] for v in route['visits'])
    assert any(v['request_id']=='a' for c in second['candidates'] for route in c['plan']['routes'] for v in route['visits']) or any(u['request_id']=='a' for c in second['candidates'] for u in c['plan']['unassigned'])
    for candidate in second['candidates']:
        for d in candidate['diff']:
            if d['request_id']=='a':
                assert d['before_engineer']=='e1'
                assert d['before_start']==610


def test_comparison_exposes_losses_and_emergency_response(make_scenario):
    from field_router.replanning import ScenarioSession
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    batch=session.preview({'version':1,'event':{'id':'new','type':'emergency','time':540,'anchor_id':'b','duration_minutes':20}},time_limit=.03)
    assert batch['request_labels']['emergency:new']=='Авария №1'
    for c in batch['candidates']:
        assert 'lost_assignments' in c['facts']
        assert 'emergency' in c
        assert isinstance(c['unassigned_details'],list)
    assert session.workbench()['request_labels']=={}


def test_explicit_cancellation_is_not_a_lost_assignment(make_scenario):
    from field_router.replanning import ScenarioSession
    s,p=demo(make_scenario);session=ScenarioSession(s,p)
    batch=session.preview({'version':1,'event':{'id':'cancel','type':'cancel','time':540,'request_id':'b'}},time_limit=.03)
    assert all(c['facts']['lost_assignments']==0 for c in batch['candidates'])


def test_new_emergency_default_service_is_80_and_cannot_start_before_arrival(make_scenario):
    from field_router.replanning import Event, ScenarioSession
    s,p=demo(make_scenario)
    from field_router.models import Skill
    s=s.model_copy(update={'engineers':[e.model_copy(update={'skills':[Skill.LOCAL,Skill.EMERGENCY]}) for e in s.engineers]})
    session=ScenarioSession(s,p)
    assert Event(id='new',type='emergency',time=600).duration_minutes==80
    batch=session.preview({'version':1,'event':{'id':'arrived','type':'emergency','time':600,'anchor_id':'a'},'preserve_in_progress':True},time_limit=.05)
    assert all(c['emergency']['assigned'] for c in batch['candidates'])
    for candidate in batch['candidates']:
        for route in candidate['plan']['routes']:
            for visit in route['visits']:
                if visit['request_id']=='emergency:arrived':
                    assert visit['start']>=600 and visit['end']-visit['start']==80
