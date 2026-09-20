"""A dispatcher day: facts, delay, cancellation, emergency, stock and restart."""
import json
import pytest
from conftest import job, engineer
from field_router.models import Equipment, Skill, Point
from field_router.planning import make_plan
from field_router.travel import synthetic_travel
from field_router.replanning import ScenarioSession


def day(make_scenario):
    jobs=[job('a',start=540,end=1320,equipment=Equipment(router=1)),job('b',start=540,end=1320),job('c',start=540,end=1320),job('d',start=540,end=1320)]
    es=[engineer('e1',end=1320,skills=[Skill.LOCAL,Skill.EMERGENCY],stock=Equipment(router=2)),engineer('e2',end=1320,skills=[Skill.LOCAL,Skill.EMERGENCY],stock=Equipment(router=2))]
    s=make_scenario(jobs,es).model_copy(update={'points':{key:Point(x_km=i,y_km=0) for i,key in enumerate(['office','a','b','c','d'])},'metadata':{'geography':'synthetic_cartesian_km'},'travel_config':{'speed_kmh':{m:60 for m in ('car','walk','bicycle','public')},'distance_factor':{m:1 for m in ('car','walk','bicycle','public')},'boarding_minutes':{m:0 for m in ('car','walk','bicycle','public')}}})
    return ScenarioSession(s,make_plan(s,synthetic_travel(s),{'e1':['a','b'],'e2':['c','d']},'test','valid'))


def fact(s,**kw):s.record_fact({'version':s.version,**kw})
def replan(s,event,profile='stable'):
    b=s.preview({'version':s.version,'event':event,'preserve_in_progress':True},time_limit=.05)
    s.approve({'version':s.version,'batch_id':b['id'],'candidate_id':profile})
    for row in s.inventory:
        for device in ('router','tv_box'):
            assert row['consumed'][device]+row['reserved'][device]+row['available'][device]==getattr(s.initial_stock[row['engineer_id']],device)
    return b


def test_full_manual_day(make_scenario):
    s=day(make_scenario);s.set_mode({'version':s.version,'mode':'manual'})
    for eid in ('e1','e2'):fact(s,time=540,engineer_id=eid,status='available',location_id='office')
    fact(s,time=541,engineer_id='e1',request_id='a',status='traveling',location_id='office',remaining_minutes=5)
    fact(s,time=546,engineer_id='e1',request_id='a',status='working',remaining_minutes=30)
    fact(s,time=560,engineer_id='e1',request_id='a',status='working',remaining_minutes=40)
    replan(s,{'id':'delay','type':'replan','time':560})
    replan(s,{'id':'cancel','type':'cancel','time':561,'request_id':'d'})
    replan(s,{'id':'alarm','type':'emergency','time':570,'anchor_id':'b','router':1},'sla')
    assert 'd' not in {r.id for r in s.scenario.requests}
    fact(s,time=580,engineer_id='e1',request_id='a',status='interrupted',remaining_minutes=20,consumed_router=1,consumed_tv_box=0)
    replan(s,{'id':'resume','type':'replan','time':585})
    owner=next(r.engineer_id for r in s.plan.routes if any(v.request_id=='a' for v in r.visits))
    fact(s,time=600,engineer_id=owner,request_id='a',status='completed')
    replan(s,{'id':'after-a','type':'replan','time':601})
    assert 'a' not in {r.id for r in s.scenario.requests}
    # Persist and resume the same day, preserving consumed equipment and history.
    s=ScenarioSession.restore(json.loads(json.dumps(s.dump())))
    assert s.inventory[0]['consumed']['router']==1
    # Finish remaining allocated work by explicit observations, never by clock alone.
    timeline=sorted((v.end,r.engineer_id,v.request_id) for r in s.plan.routes for v in r.visits)
    for when,eid,key in timeline:fact(s,time=when,engineer_id=eid,request_id=key,status='completed')
    replan(s,{'id':'close-day','type':'replan','time':1320})
    assert not s.scenario.requests
    assert s.plan.metrics.assigned==0
    assert len({f['request_id'] for f in s.fact_log if f['status']=='completed'})==4


def test_cannot_record_completion_after_cancellation(make_scenario):
    s=day(make_scenario);s.set_mode({'version':s.version,'mode':'manual'})
    replan(s,{'id':'cancel','type':'cancel','time':540,'request_id':'d'})
    with pytest.raises(ValueError,match='отмен'):
        fact(s,time=550,engineer_id='e2',request_id='d',status='completed')


def test_late_completion_before_cancel_is_allowed(make_scenario):
    s=day(make_scenario);s.set_mode({'version':s.version,'mode':'manual'})
    replan(s,{'id':'cancel','type':'cancel','time':560,'request_id':'d'})
    fact(s,time=550,engineer_id='e2',request_id='d',status='completed')
    assert s.time==560 and s.fact_log[-1]['request_id']=='d'


@pytest.mark.parametrize('region',['Восток','Юго-восток','Югоцентр'])
def test_saved_road_workday_to_shift_end(region):
    from pathlib import Path
    from field_router.models import Scenario,Plan
    payload=json.loads((Path('data/demo-road')/(region+'.json')).read_text())
    s=ScenarioSession(Scenario.model_validate(payload['scenario']),Plan.model_validate(payload['plans'][1]),payload['geography'])
    original=set(s.catalog)
    replan(s,{'id':'morning','type':'replan','time':540})
    replan(s,{'id':'alarm','type':'emergency','time':710,'anchor_id':next(iter(s.catalog)),'router':1},'sla')
    locked={v['request_id'] for route in s.frozen for v in route['visits']}
    shifts={e.id:e.shift.start for e in s.scenario.engineers}
    for route in s.plan.routes:
        departure=shifts[route.engineer_id]
        for v in route.visits:
            if v.end<=720 or v.start<=720<v.end or departure<720<v.arrival:
                locked.add(v.request_id)
            departure=v.end
    cancellable=[r.id for r in s.scenario.requests if r.id not in locked and min(w.start for w in r.windows)>720]
    cancelled=cancellable[-1] if cancellable else None
    if cancelled:replan(s,{'id':'cancel','type':'cancel','time':720,'request_id':cancelled})
    replan(s,{'id':'afternoon','type':'replan','time':900})
    s=ScenarioSession.restore(json.loads(json.dumps(s.dump())))
    replan(s,{'id':'shift-end','type':'replan','time':1320})
    assert s.plan.metrics.assigned==0
    ledger={v['request_id'] for route in s.frozen for v in route['visits']}
    pending={r.id for r in s.scenario.requests}
    assert not ledger & pending
    assert original|{'emergency:alarm'}==ledger|pending|({cancelled} if cancelled else set())
