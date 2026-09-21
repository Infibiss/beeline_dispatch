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


@pytest.fixture
def continuation_case(make_scenario, monkeypatch):
    """A deterministic clock separates the retention contract from search noise."""
    from field_router.planning import make_plan
    from field_router.solvers import selection
    scenario = make_scenario([job('a', start=600, end=600),
                              job('b', start=650, end=650),
                              job('c', start=700, end=700)])
    matrix = travel(scenario)
    plans = [make_plan(scenario, matrix, {'e1': list('abc')[:count]},
                       'ortools' if count == 1 else 'alns', 'valid')
             for count in (1, 2, 3)]
    clock = [0.0]
    calls = []
    monkeypatch.setattr(selection, 'perf_counter', lambda: clock[0])

    def record(backend, kwargs):
        calls.append((backend, kwargs))
        clock[0] += kwargs['time_limit_seconds']

    def ortools(*args, **kwargs):
        record('ortools', kwargs)
        return plans[0]

    monkeypatch.setattr(selection.OrToolsSolver, 'solve', ortools)
    return scenario, matrix, plans, calls, record


@pytest.mark.parametrize('outcome', ['worse', 'better', 'invalid', 'error'])
def test_long_hybrid_preserves_short_checkpoint_within_one_budget(continuation_case, monkeypatch, outcome):
    from field_router.solvers.selection import AlnsSolver
    s, t, plans, calls, record = continuation_case

    def alns(*args, **kwargs):
        record('alns', kwargs)
        if len(calls) == 2:
            return plans[1]
        assert kwargs['initial_plan'].routes == plans[1].routes
        if outcome == 'error':
            raise RuntimeError('continuation failed')
        if outcome == 'invalid':
            return plans[2].model_copy(update={'routes': []})
        return plans[2] if outcome == 'better' else plans[0]

    monkeypatch.setattr(AlnsSolver, 'solve', alns)
    result = get_solver('hybrid').solve(s, t, time_limit_seconds=10)
    assert [backend for backend, _ in calls] == ['ortools', 'alns', 'alns']
    assert [kwargs['time_limit_seconds'] for _, kwargs in calls] == pytest.approx([1.2, 1.8, 7])
    assert result.elapsed_seconds == pytest.approx(10)
    assert result.routes == plans[2 if outcome == 'better' else 1].routes
    assert not validate_plan(s, t, result)
    checkpoint = result.diagnostics['checkpoint']
    assert checkpoint['metrics']['assigned'] == 2
    assert tuple(result.diagnostics['objective_key']) <= tuple(checkpoint['objective_key'])
    assert result.diagnostics['continuation']['improved'] == (outcome == 'better')
    assert result.status == ('fallback_alns_error' if outcome in ('invalid', 'error') else 'feasible_search_completed')


def test_hybrid_retains_supplied_plan_without_replacing_reference(continuation_case, monkeypatch):
    from field_router.solvers.selection import AlnsSolver
    s, t, plans, calls, record = continuation_case

    def alns(*args, **kwargs):
        record('alns', kwargs)
        assert kwargs['reference'] is plans[0]
        assert kwargs['initial_plan'] is plans[2]
        return plans[1]

    monkeypatch.setattr(AlnsSolver, 'solve', alns)
    result = get_solver('hybrid').solve(s, t, time_limit_seconds=10,
                                      initial_plan=plans[2], reference=plans[0], profile='stable')
    assert result.routes == plans[2].routes
    assert not validate_plan(s, t, result)


def test_hybrid_rejects_invalid_initial_plan_before_search(continuation_case):
    s, t, plans, calls, _ = continuation_case
    invalid = plans[2].model_copy(update={'routes': []})
    with pytest.raises(ValueError, match='Invalid plan'):
        get_solver('hybrid').solve(s, t, initial_plan=invalid)
    assert calls == []


def test_short_hybrid_keeps_existing_budget_and_has_no_continuation(continuation_case, monkeypatch):
    from field_router.solvers.selection import AlnsSolver
    s, t, plans, calls, record = continuation_case

    def alns(*args, **kwargs):
        record('alns', kwargs)
        return plans[1]

    monkeypatch.setattr(AlnsSolver, 'solve', alns)
    result = get_solver('hybrid').solve(s, t, time_limit_seconds=3)
    assert [kwargs['time_limit_seconds'] for _, kwargs in calls] == pytest.approx([1.2, 1.8])
    assert result.routes == plans[1].routes
    assert 'continuation' not in result.diagnostics


def test_hybrid_does_not_restart_budget_after_slow_short_phase(continuation_case, monkeypatch):
    from field_router.solvers.selection import AlnsSolver
    s, t, plans, calls, record = continuation_case

    def slow_alns(*args, **kwargs):
        record('alns', kwargs)
        # Account for 2 seconds spent finishing/validating the short stage.
        if len(calls) == 2:
            record('overhead', {'time_limit_seconds': 2})
        return plans[1]

    monkeypatch.setattr(AlnsSolver, 'solve', slow_alns)
    result = get_solver('hybrid').solve(s, t, time_limit_seconds=10)
    assert calls[-1][0] == 'alns'
    assert calls[-1][1]['time_limit_seconds'] == pytest.approx(5)
    assert result.elapsed_seconds == pytest.approx(10)
    assert result.metrics.assigned == 2
