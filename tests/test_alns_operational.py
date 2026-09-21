"""Operational ALNS decisions must match the public objective and real schedule."""
from itertools import product

import pytest

from conftest import engineer, job
from test_alns_core import instance, solution, task
from test_alns_stable_schedule import instance as waiting_instance
from test_planning import travel
from field_router._alns.alns import ALNS
from field_router._alns.solution import RouteEval, Solution
from field_router._alns.sp import solve_sp
from field_router.models import Transport
from field_router.objectives import objective_key
from field_router.planning import make_plan
from field_router.solvers.alns_solver import AlnsSolver, _instance, _with_stable_schedule
from field_router.travel import MatrixTravel
from field_router.validation import validate_plan


def test_cost_prefers_less_road_time_even_with_more_metres_and_engineers():
    inst = instance([task(0), task(1)], engineers=2)
    inst.profile = 'cost'
    inst.ttl['car'][inst.node(0)][inst.node(1)] = 25
    inst.kml['car'][0][inst.node(0)] = 1
    inst.kml['car'][1][inst.node(1)] = 10
    one = solution(inst, [0, 1])
    two = solution(inst, [0])
    two.insert(inst, 1, 1, 0)
    assert two.objective_key(inst) < one.objective_key(inst)
    result, _ = solve_sp(inst, {(0, (0, 1)): 1, (0, (0,)): 1, (1, (1,)): 10}, time_limit=.2)
    assert result.objective_key(inst) == two.objective_key(inst)


def test_cost_ties_keep_owner_and_promised_time_before_fleet_size():
    inst = instance([task(0), task(1)], engineers=2)
    inst.profile, inst.prev_owner, inst.prev_start = 'cost', {0: 0, 1: 1}, {0: 20, 1: 30}
    one = solution(inst, [0, 1])
    two = solution(inst, [0])
    two.insert(inst, 1, 1, 0)
    assert tuple(two.routes[0].start_times(inst)) == (20,)
    assert tuple(two.routes[1].start_times(inst)) == (30,)
    assert two.objective_key(inst) < one.objective_key(inst)
    result, _ = solve_sp(inst, {(0, (0, 1)): 0, (0, (0,)): 0, (1, (1,)): 0}, time_limit=.2)
    assert result.task_owner() == {0: 0, 1: 1}


def test_sla_focus_penalty_prefers_serving_focus_when_coverage_ties():
    inst = instance([task(0, priority=3, duration=100), task(1, priority=3, duration=100)])
    inst.profile, inst.response_targets = 'sla', {1}
    ignored, focused = solution(inst, [0]), solution(inst, [1])
    assert focused.objective_key(inst) < ignored.objective_key(inst)
    result, _ = solve_sp(inst, {(0, (0,)): 0, (0, (1,)): 0}, time_limit=.2)
    assert result.task_owner() == {1: 0}
    search = ALNS(inst)
    assert search._move(Solution(unassigned={0, 1}), 1, 0)[0] < search._move(Solution(unassigned={0, 1}), 0, 0)[0]


def test_sla_default_targets_urgent_and_preserves_high_reference_waiting():
    inst = waiting_instance([[(0, 35)], [(0, 35)]], {0: 20, 1: 30})
    inst.profile = 'sla'
    inst.tasks[0].priority, inst.tasks[1].priority = 3, 2
    route = RouteEval(inst, 0, [0, 1])
    assert tuple(route.start_times(inst)) == (0, 30)
    inst.response_targets = {1}
    assert tuple(route.start_times(inst)) == (0, 1)


def test_sla_schedule_matches_exhaustive_response_then_absolute_shift():
    inst = waiting_instance([[(0, 3), (7, 10)], [(0, 10)], [(0, 10)]], {0: 8, 1: 4, 2: 9}, end=11)
    inst.profile, inst.response_targets = 'sla', {1}
    route = RouteEval(inst, 0, [0, 1, 2])
    feasible = [times for times in product(range(11), repeat=3)
                if times[0] + 1 <= times[1] and times[1] + 1 <= times[2]
                and all(any(a <= t <= b for a, b in inst.tasks[j].service_windows) for j, t in enumerate(times))]
    key = lambda times: (times[1], sum(abs(times[j] - old) for j, old in inst.prev_start.items()))
    assert key(tuple(route.start_times(inst))) == min(map(key, feasible))


@pytest.mark.parametrize('profile', [None, 'stable', 'cost', 'sla'])
def test_kernel_key_equals_adapter_key_with_dropped_focus_and_departed_owner(make_scenario, profile):
    scenario = make_scenario([job('urgent', start=0, end=80, duration=1, priority='urgent'),
                              job('high', start=0, end=80, duration=1, priority='high')],
                             [engineer('e1', start=0, end=100)])
    matrix = travel(scenario, minutes=3, metres=1013)
    old = scenario.model_copy(update={'engineers': [engineer('gone', start=0, end=100)]})
    reference = make_plan(old, matrix, {'gone': ['high']}, 'reference', 'valid')
    visit = reference.routes[0].visits[0].model_copy(update={'start': 40, 'end': 41})
    reference = reference.model_copy(update={'routes': [reference.routes[0].model_copy(update={'visits': [visit]})]})
    inst = _instance(scenario, matrix, profile, reference, focus_request_id='urgent')
    kernel = Solution({0: RouteEval(inst, 0, [1])}, {0})
    plan = make_plan(scenario, matrix, {'e1': ['high']}, 'alns', 'valid')
    plan = _with_stable_schedule(plan, inst, kernel)
    assert validate_plan(scenario, matrix, plan) == []
    assert kernel.objective_key(inst) == objective_key(plan, scenario, profile=profile, reference=reference,
                                                      focus_request_id='urgent')
    if profile is not None:
        assert plan.routes[0].visits[0].start == 40


@pytest.mark.parametrize('profile', ['stable', 'cost', 'sla'])
def test_adapter_exports_waiting_for_every_operational_profile(make_scenario, profile):
    scenario = make_scenario([job('new', start=0, end=0, duration=1, priority='urgent'),
                              job('kept', start=0, end=30, duration=1, priority='high')],
                             [engineer(start=0, end=35)])
    matrix = travel(scenario, minutes=0, metres=0)
    reference = make_plan(scenario, matrix, {'e1': ['kept']}, 'reference', 'valid')
    route = reference.routes[0]
    reference = reference.model_copy(update={'routes': [route.model_copy(update={'visits': [
        route.visits[0].model_copy(update={'start': 20, 'end': 21})]})]})
    result = AlnsSolver().solve(scenario, matrix, profile=profile, reference=reference,
                                focus_request_id='new', time_limit_seconds=.1)
    assert validate_plan(scenario, matrix, result) == []
    assert {v.request_id: v.start for r in result.routes for v in r.visits} == {'new': 0, 'kept': 20}


@pytest.mark.parametrize('focus', ['missing', 'high'])
def test_adapter_rejects_invalid_focus(make_scenario, focus):
    scenario = make_scenario([job('high', priority='high')])
    with pytest.raises(ValueError, match='focus_request_id'):
        AlnsSolver().solve(scenario, travel(scenario), profile='sla', focus_request_id=focus)


@pytest.mark.parametrize('profile, expected_engineers', [(None, 1), ('cost', 2)])
def test_adapter_keeps_morning_fleet_goal_and_operational_road_time_distinct(make_scenario, profile, expected_engineers):
    scenario = make_scenario([job('a', start=0, end=100, duration=1), job('b', start=0, end=100, duration=1)],
                             [engineer('e1', start=0, end=100), engineer('e2', start=0, end=100)])
    matrix = MatrixTravel(['office', 'a', 'b'],
                          {mode: [[0, 10, 10], [10, 0, 25], [10, 25, 0]] for mode in Transport},
                          {mode: [[0, 1000, 1000], [1000, 0, 0], [1000, 0, 0]] for mode in Transport})
    result = AlnsSolver().solve(scenario, matrix, profile=profile, time_limit_seconds=.15)
    assert validate_plan(scenario, matrix, result) == []
    assert result.metrics.assigned == 2
    assert result.metrics.engineers_used == expected_engineers
    assert result.metrics.travel_minutes == (35 if profile is None else 20)


def test_adapter_focus_reorders_two_emergencies_and_keeps_later_promise(make_scenario):
    scenario = make_scenario([job('old', start=0, end=100, duration=10, priority='urgent'),
                              job('new', start=0, end=100, duration=10, priority='urgent')],
                             [engineer(start=0, end=100)])
    matrix = travel(scenario, minutes=0, metres=0)
    reference = make_plan(scenario, matrix, {'e1': ['old']}, 'reference', 'valid')
    visit = reference.routes[0].visits[0].model_copy(update={'start': 70, 'end': 80})
    reference = reference.model_copy(update={'routes': [reference.routes[0].model_copy(update={'visits': [visit]})]})
    result = AlnsSolver().solve(scenario, matrix, profile='sla', reference=reference,
                                focus_request_id='new', time_limit_seconds=.15)
    assert validate_plan(scenario, matrix, result) == []
    assert [(v.request_id, v.start) for r in result.routes for v in r.visits] == [('new', 0), ('old', 70)]


def test_route_pool_dominance_uses_road_minutes_before_distance():
    inst = instance([task(0), task(1)], matrix=[[0, 8, 1], [1, 0, 1], [1, 1, 0]])
    inst.profile = 'cost'
    # Route 0,1 uses nine km but no travel time; 1,0 uses two km and ten minutes.
    inst.ttl['car'] = [[0, 0, 10], [0, 0, 0], [0, 0, 0]]
    result, info = solve_sp(inst, {(0, (0, 1)): 9, (0, (1, 0)): 2}, time_limit=.1)
    assert result.routes[0].seq == [0, 1]
    assert info['columns'] == 1
