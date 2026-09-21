from copy import deepcopy

from field_router.replanning import ScenarioSession
from field_router.risk_dashboard import dashboard
from field_router.models import TimeWindow
from test_replanning import demo
from test_auth import Client, secured


def indicators(result):
    return {item['id']: item for item in result['indicators']}


def test_readonly_indicators_do_not_invent_probabilities(make_scenario):
    scenario,plan=demo(make_scenario)
    session=ScenarioSession(scenario,plan)
    before=deepcopy(session.dump())
    result=dashboard(session); rows=indicators(result)
    # Two 9-hour shifts; 2 minutes on the road and 60 working, waiting excluded.
    assert rows['capacity']['facts']=={'occupied_minutes':62,'capacity_minutes':1080}
    assert rows['capacity']['value']==5.7
    assert rows['windows']['facts']['tight_visits']==0
    assert rows['equipment']['value']==100
    assert result['forecast']['status']=='not_connected'
    assert result['forecast']['risks']==[]
    assert session.dump()==before


def test_window_endpoint_and_later_separate_window_are_not_extra_buffer(make_scenario):
    scenario,plan=demo(make_scenario)
    session=ScenarioSession(scenario,plan)
    first=plan.routes[0].visits[0]
    session.catalog['a']=session.catalog['a'].model_copy(update={'windows':[
        TimeWindow(start=first.start,end=first.start+15),TimeWindow(start=900,end=1000)]})
    row=indicators(dashboard(session))['windows']
    assert row['facts']['tight_visits']==1 and row['value']==50


def test_frozen_duplicate_not_double_counted_and_elapsed_work_is_clipped(make_scenario):
    scenario,plan=demo(make_scenario)
    session=ScenarioSession(scenario,plan)
    session.frozen=[{'engineer_id':'e1','visits':[v.model_dump() for v in plan.routes[0].visits]}]
    assert indicators(dashboard(session))['capacity']['facts']['occupied_minutes']==62
    session.time=560
    # First job has 20 min left, the second trip takes 1 min and work 30 min.
    assert indicators(dashboard(session))['capacity']['facts']=={'occupied_minutes':51,'capacity_minutes':1040}


def test_no_capacity_and_no_equipment_are_unknown_not_zero_risk(make_scenario):
    scenario,plan=demo(make_scenario)
    session=ScenarioSession(scenario,plan)
    session.execution_mode='manual'
    rows=indicators(dashboard(session))
    assert all(row['value'] is None for row in rows.values())
    session.execution_mode='simulation';session.time=1080
    assert indicators(dashboard(session))['capacity']['value'] is None


def test_equipment_type_without_demand_does_not_hide_other_stock(make_scenario):
    scenario,plan=demo(make_scenario)
    session=ScenarioSession(scenario,plan)
    session.time=700
    session.inventory[0]['available']={'router':0,'tv_box':2}
    session.inventory[1]['available']={'router':0,'tv_box':0}
    assert indicators(dashboard(session))['equipment']['value']==0


def test_risk_endpoint_requires_dispatcher_and_is_region_scoped(secured):
    server,_,session=secured
    anonymous=Client(server);assert anonymous.request('/api/risk')[0]==401
    worker=Client(server);worker.login('worker1')
    assert worker.request('/r/east/api/risk')[0]==403
    admin=Client(server);admin.login('admin')
    for prefix,region in [('/r/east','Восток'),('/regions/south-east','Юго-восток')]:
        code,data,_=admin.request(prefix+'/api/risk')
        assert code==200 and data['context']['region']==region
        assert data['forecast']['region']==region
    assert session.version==1 and not session.fact_log


def test_failed_dashboard_does_not_break_planning(secured,monkeypatch):
    import field_router.risk_dashboard as risk
    server,_,_=secured
    admin=Client(server);admin.login('admin')
    def fail(session):raise RuntimeError('Isolated projection failure')
    monkeypatch.setattr(risk,'dashboard',fail)
    assert admin.request('/api/risk')[0]==503
    assert admin.request('/api/state')[0]==200


def test_fresh_facts_mark_metrics_as_requiring_replan(secured):
    server,_,_=secured
    admin=Client(server);admin.login('admin')
    payload={'version':1,'engineer_id':'e1','status':'available','time':540,'location_id':'office'}
    assert admin.request('/api/engineer/facts',payload)[0]==200
    data=admin.request('/api/risk')[1]
    assert data['context']['plan_version']==2 and data['context']['needs_replanning']
