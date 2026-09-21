"""Operational priorities differ from morning crew planning."""
import pytest

from conftest import engineer, job
from test_planning import travel
from field_router.models import Transport
from field_router.planning import make_plan
from field_router.solvers.ortools_solver import OrToolsSolver, objective_key, _reference_visits
from field_router.travel import MatrixTravel
from field_router.validation import require_valid


def delayed(plan, starts):
    routes = []
    for route in plan.routes:
        previous_end = None
        visits = []
        for visit in route.visits:
            start = starts.get(visit.request_id, visit.start)
            arrival = visit.arrival if previous_end is None else previous_end + visit.travel_minutes
            end = start + visit.end - visit.start
            visits.append(visit.model_copy(update={'arrival': arrival, 'start': start, 'end': end}))
            previous_end = end
        routes.append(route.model_copy(update={'visits': visits}))
    return plan.model_copy(update={'routes': routes})


def different_travel(s):
    ids = ['office'] + [r.id for r in s.requests]
    minutes = [[0, 5, 5], [5, 0, 50], [5, 50, 0]]
    metres = [[0, 1000, 1000], [1000, 0, 1000], [1000, 1000, 0]]
    return MatrixTravel(ids, {m: minutes for m in Transport}, {m: metres for m in Transport})


def test_day_cost_prefers_less_driving_with_two_engineers(make_scenario):
    s = make_scenario([job('a'), job('b')], [engineer('first'), engineer('second')])
    t = different_travel(s)
    one = make_plan(s, t, {'first': ['a', 'b']}, 'test', 'test')
    two = make_plan(s, t, {'first': ['a'], 'second': ['b']}, 'test', 'test')
    assert objective_key(two, s, profile='cost') < objective_key(one, s, profile='cost')
    assert objective_key(one, s) < objective_key(two, s)
    result = OrToolsSolver().solve(s, t, profile='cost', time_limit_seconds=.12)
    assert (result.metrics.travel_minutes, result.metrics.engineers_used) == (10, 2)
    assert require_valid(s, t, result)


def test_reference_keeps_time_when_previous_engineer_disappears(make_scenario):
    old = make_scenario([job('a')], [engineer('gone')])
    reference = delayed(make_plan(old, travel(old), {'gone': ['a']}, 'test', 'test'), {'a': 680})
    s = make_scenario(old.requests)
    assert _reference_visits(s, reference) == {'a': ('gone', 680)}
    plan = make_plan(s, travel(s), {'e1': ['a']}, 'test', 'test')
    assert objective_key(plan, s, profile='stable', reference=reference)[3:5] == (0, 80)
    result = OrToolsSolver().solve(s, travel(s), profile='stable', reference=reference, time_limit_seconds=.12)
    assert result.routes[0].visits[0].start == 680
    assert require_valid(s, travel(s), result)


def test_sla_focus_prefers_urgent_response_over_unrelated_high_start(make_scenario):
    s = make_scenario([job('high', priority='high'), job('urgent', priority='urgent')])
    t = travel(s)
    early_high = make_plan(s, t, {'e1': ['high', 'urgent']}, 'test', 'test')
    early_urgent = make_plan(s, t, {'e1': ['urgent', 'high']}, 'test', 'test')
    assert objective_key(early_urgent, s, profile='sla', focus_request_id='urgent') < objective_key(early_high, s, profile='sla', focus_request_id='urgent')
    result = OrToolsSolver().solve(s, t, profile='sla', focus_request_id='urgent', time_limit_seconds=.12)
    assert result.routes[0].visits[0].request_id == 'urgent'
    assert result.routes[0].visits[0].start == 600


def test_response_targets_and_unassigned_penalty(make_scenario):
    from field_router.objectives import response_targets
    s = make_scenario([job('high', priority='high'), job('u1', priority='urgent'), job('u2', priority='urgent')])
    assert response_targets(s) == {'u1', 'u2'}
    assert response_targets(s, 'u2') == {'u2'}
    plan = make_plan(s, travel(s), {'e1': ['u1']}, 'test', 'test')
    assert objective_key(plan, s, profile='sla')[3] == 600 + 1441
    for invalid in ('high', 'missing'):
        with pytest.raises(ValueError, match='urgent'):
            response_targets(s, invalid)


@pytest.mark.parametrize('profile', ['cost', 'sla'])
def test_operational_ties_preserve_promised_start(make_scenario, profile):
    s = make_scenario([job('a')])
    t = travel(s)
    reference = delayed(make_plan(s, t, {'e1': ['a']}, 'test', 'test'), {'a': 680})
    result = OrToolsSolver().solve(s, t, profile=profile, reference=reference, time_limit_seconds=.1)
    assert result.routes[0].visits[0].start == 680
    assert require_valid(s, t, result)


def test_morning_objective_and_diagnostics_stay_separate(make_scenario):
    s = make_scenario([job('a'), job('b')], [engineer('first'), engineer('second')])
    result = OrToolsSolver().solve(s, different_travel(s), time_limit_seconds=.1)
    assert objective_key(result, s) == (0, 0, 0, 1, 2000)
    assert result.diagnostics['profile'] == 'morning'


@pytest.mark.parametrize('profile', ['stable', 'cost', 'sla'])
def test_huge_distances_keep_exact_incumbent_and_safe_search(make_scenario, profile):
    s = make_scenario([job('a'), job('b')], [engineer('first'), engineer('second')])
    t = travel(s, metres=10**40)
    reference = delayed(make_plan(s, t, {'first': ['a'], 'second': ['b']}, 'test', 'test'), {'a': 680, 'b': 680})
    result = OrToolsSolver().solve(s, t, profile=profile, reference=reference, time_limit_seconds=.1)
    assert objective_key(result, s, profile=profile, reference=reference) <= objective_key(reference, s, profile=profile, reference=reference)
    assert result.diagnostics['search_objective_bound'] < 2**60
    omitted = result.diagnostics['search_omitted_tiers']
    assert 'distance_m' in omitted
    assert omitted[-1] == 'engineers_used'
    assert require_valid(s, t, result)


def test_sla_preserves_unrelated_promise_after_speeding_response(make_scenario):
    s = make_scenario([job('high', priority='high'), job('urgent', priority='urgent')],
                      [engineer('first'), engineer('second')])
    t = travel(s)
    reference = delayed(make_plan(s, t, {'first': ['high'], 'second': ['urgent']}, 'test', 'test'), {'high': 680, 'urgent': 680})
    result = OrToolsSolver().solve(s, t, profile='sla', reference=reference, focus_request_id='urgent', time_limit_seconds=.15)
    visits = {v.request_id: (r.engineer_id, v.start) for r in result.routes for v in r.visits}
    assert visits == {'high': ('first', 680), 'urgent': ('second', 600)}
    assert require_valid(s, t, result)


def test_hybrid_forwards_focus_and_preserves_unfocused_urgent_promise(make_scenario):
    from field_router.solvers.selection import HybridSolver
    s = make_scenario([job('old', priority='urgent'), job('new', priority='urgent')],
                      [engineer('first'), engineer('second')])
    t = travel(s)
    reference = delayed(make_plan(s, t, {'first': ['old'], 'second': ['new']}, 'test', 'test'), {'old': 680, 'new': 680})
    result = HybridSolver().solve(s, t, profile='sla', reference=reference, focus_request_id='new', time_limit_seconds=.2)
    starts = {v.request_id: v.start for r in result.routes for v in r.visits}
    assert starts == {'old': 680, 'new': 600}
    assert require_valid(s, t, result)


@pytest.mark.parametrize('focus', ['high', 'missing'])
def test_solver_rejects_invalid_response_focus(make_scenario, focus):
    s = make_scenario([job('high', priority='high')])
    with pytest.raises(ValueError, match='urgent'):
        OrToolsSolver().solve(s, travel(s), profile='sla', focus_request_id=focus, time_limit_seconds=.1)


def test_cost_preserves_waiting_when_search_omits_shift_tier(make_scenario):
    s = make_scenario([job('new', start=600, end=600), job('old', start=600, end=720)])
    t = travel(s, minutes=0, metres=10**15)
    reference = delayed(make_plan(s, t, {'e1': ['old']}, 'test', 'test'), {'old': 680})
    result = OrToolsSolver().solve(s, t, profile='cost', reference=reference, time_limit_seconds=.1)
    assert 'absolute_start_shift' in result.diagnostics['search_omitted_tiers']
    assert [(v.request_id, v.start) for r in result.routes for v in r.visits] == [('new', 600), ('old', 680)]
    assert objective_key(result, s, profile='cost', reference=reference)[6] == 0
    assert require_valid(s, t, result)


@pytest.mark.parametrize('profile, expected', [(None, [600, 610, 620]),
                                               ('stable', [650, 660, 740]),
                                               ('cost', [650, 660, 740]),
                                               ('sla', [600, 610, 740])])
def test_fixed_route_waiting_respects_focus_windows_and_morning(make_scenario, profile, expected):
    from field_router.models import TimeWindow
    from field_router.scheduling import optimize_waiting
    first = job('first', start=600, end=700, duration=10).model_copy(update={
        'windows': [TimeWindow(start=600, end=610), TimeWindow(start=650, end=700)]})
    s = make_scenario([first, job('urgent', duration=10, priority='urgent'),
                      job('last', start=600, end=800, duration=10)])
    t = travel(s, minutes=0, metres=10**15)
    reference = delayed(make_plan(s, t, {'e1': ['first', 'last']}, 'test', 'test'),
                        {'first': 650, 'last': 740})
    plan = make_plan(s, t, {'e1': ['first', 'urgent', 'last']}, 'test', 'test')
    scheduled = optimize_waiting(s, plan, profile=profile, reference=reference, focus_request_id='urgent')
    assert [v.start for v in scheduled.routes[0].visits] == expected
    assert scheduled.metrics == plan.metrics
    assert require_valid(s, t, scheduled)
