import pytest
from conftest import job
from test_planning import travel
from field_router.solvers.selection import get_solver
from field_router.validation import validate_plan


def test_hybrid_returns_valid_plan_and_reports_backend(make_scenario):
    s=make_scenario([job('a')]); t=travel(s)
    p=get_solver('hybrid').solve(s,t,time_limit_seconds=.2)
    assert p.solver=='hybrid' and not validate_plan(s,t,p)
    assert p.metrics.assigned==1
    assert p.diagnostics['selected_backend'] in ('ortools','alns')


def test_hybrid_retains_ortools_if_alns_fails(make_scenario,monkeypatch):
    from field_router.solvers.alns_solver import AlnsSolver
    s=make_scenario([job('a')]);t=travel(s)
    def broken(*args,**kwargs):raise ValueError('invalid candidate')
    monkeypatch.setattr(AlnsSolver,'solve',broken)
    p=get_solver('hybrid').solve(s,t,time_limit_seconds=.15)
    assert p.metrics.assigned==1 and not validate_plan(s,t,p)
    assert p.diagnostics['selected_backend']=='ortools'
    assert p.status=='fallback_alns_error'


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError):get_solver('unknown')


@pytest.mark.parametrize('backend',['alns','hybrid','ortools'])
def test_residual_day_uses_selected_backend_and_never_travels_before_event(make_scenario,backend):
    from test_replanning import demo
    from field_router.replanning import ScenarioSession
    s,p=demo(make_scenario)
    session=ScenarioSession(s,p)
    batch=session.preview({'version':1,'event':{'id':'backend-test','type':'emergency','time':540,
        'anchor_id':'b','duration_minutes':20}},time_limit=.1,solver=backend)
    assert all(c['plan']['solver']==backend for c in batch['candidates'])
    session.approve({'version':1,'batch_id':batch['id'],'candidate_id':'sla'})
    assert not validate_plan(session.scenario,session._travel(session.scenario),session.plan)
    for route in session.plan.routes:
        first=route.visits[0]
        assert first.arrival-first.travel_minutes>=540


def test_hybrid_never_loses_valid_reference_when_search_fails(make_scenario,monkeypatch):
    from conftest import engineer
    from field_router.solvers.alns_solver import AlnsSolver
    from field_router.solvers.ortools_solver import OrToolsSolver
    from field_router.planning import make_plan
    s=make_scenario([job('a',start=600,end=600),job('b',start=650,end=650)],[engineer()]);t=travel(s)
    reference=make_plan(s,t,{'e1':['a','b']},'published','valid')
    worse=make_plan(s,t,{'e1':['a']},'ortools','feasible_search_completed')
    monkeypatch.setattr(OrToolsSolver,'solve',lambda *args,**kwargs:worse)
    def broken(*args,**kwargs):raise ValueError('invalid candidate')
    monkeypatch.setattr(AlnsSolver,'solve',broken)
    p=get_solver('hybrid').solve(s,t,time_limit_seconds=.15,reference=reference,profile='stable')
    assert p.metrics.assigned==2
    assert p.routes==reference.routes
