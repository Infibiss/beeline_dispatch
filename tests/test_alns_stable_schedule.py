"""Stable scheduling must optimize real waiting without changing route feasibility."""
from itertools import product
import random

import numpy as np
import pytest

from conftest import engineer, job
from test_planning import travel
from field_router._alns.instance import Engineer, Instance, Task
from field_router._alns.solution import RouteEval, Solution, schedule
from field_router.planning import make_plan
from field_router.solvers.alns_solver import AlnsSolver
from field_router.solvers.ortools_solver import objective_key
from field_router.validation import validate_plan


def instance(windows, reference, *, service=1, start=0, end=40, matrix=None):
    jobs = [Task(i, str(i), 0, 0, min(a for a, _ in ws), max(b for _, b in ws),
                 service, 'local', None, 1, windows=tuple(ws))
            for i, ws in enumerate(windows)]
    engineer = Engineer(0, 'e', 'e', 0, 0, start, end, frozenset({'local'}), 'car')
    matrix = np.zeros((len(jobs) + 1, len(jobs) + 1)) if matrix is None else matrix
    return Instance('stable', jobs, [engineer], matrices={
        'km': {'car': matrix}, 'minutes': {'car': matrix}}, profile='stable',
        prev_start=reference, prev_owner={j: 0 for j in reference}).build_matrices()


def starts(inst, seq=None):
    return [v['start'] for v in schedule(inst, 0, list(range(inst.N)) if seq is None else seq)]


def test_feasible_reference_waits_are_preserved_without_changing_earliest_bounds():
    inst = instance([[(0, 35)], [(0, 35)]], {0: 20, 1: 30})
    route = RouteEval(inst, 0, [0, 1])
    assert route.E == [0, 1]
    assert route.profile_key(inst) == (0, 0)
    assert starts(inst) == [20, 30]
    assert route.E == [0, 1]


def test_joint_optimum_can_start_before_reference_instead_of_greedily_clamping():
    # Greedily choosing 8, 9, 10 shifts 10 minutes; the joint optimum shifts 5.
    inst = instance([[(0, 10)]] * 3, {0: 8, 1: 4, 2: 5}, end=11)
    assert RouteEval(inst, 0, [0, 1, 2]).profile_key(inst) == (0, 5)
    assert starts(inst) == [3, 4, 5]


def test_reference_in_window_gap_uses_best_feasible_window_globally():
    inst = instance([[(0, 2), (8, 10)], [(0, 12)]], {0: 6, 1: 6}, end=13)
    assert starts(inst) == [2, 6]
    assert RouteEval(inst, 0, [0, 1]).profile_key(inst) == (0, 4)


def test_residual_shift_and_start_arc_bound_waiting_and_shift_completion():
    inst = instance([[(0, 30)], [(0, 30)]], {0: 12, 1: 20}, service=2,
                    start=7, end=22, matrix=[[0, 2, 9], [2, 0, 3], [9, 3, 0]])
    visits = schedule(inst, 0, [0, 1])
    assert [(v['arrival'], v['start'], v['end']) for v in visits] == [(9, 12, 14), (17, 20, 22)]
    inst.prev_start = {0: 2, 1: 13}
    assert starts(inst) == [9, 14]


def test_unreferenced_jobs_do_not_add_deviation_and_keep_earliest_feasible_ties():
    inst = instance([[(0, 35)], [(0, 35)]], {1: 20})
    assert starts(inst) == [0, 20]
    assert RouteEval(inst, 0, [0, 1]).profile_key(inst) == (0, 0)


@pytest.mark.parametrize('profile, expected', [(None, [0, 1]), ('cost', [20, 30]), ('sla', [20, 30])])
def test_morning_starts_early_and_operational_profiles_preserve_nonurgent_promises(profile, expected):
    inst = instance([[(0, 35)], [(0, 35)]], {0: 20, 1: 30})
    inst.profile = profile
    assert starts(inst) == expected


def test_insertion_uses_earliest_bounds_and_recomputes_optimized_waiting():
    inst = instance([[(0, 30)], [(0, 10)]], {0: 20})
    sol = Solution({0: RouteEval(inst, 0, [0])}, {1})
    assert sol.routes[0].profile_key(inst) == (0, 0)
    assert sol.routes[0].insertion(inst, 1, min_pos=1) is not None
    sol.insert(inst, 1, 0, 1)
    assert sol.routes[0].E == [0, 1]
    assert sol.routes[0].profile_key(inst) == (0, 11)
    assert starts(inst) == [9, 10]


def test_schedule_matches_exhaustive_integer_minute_optimum():
    rng = random.Random(8402)
    checked = 0
    for _ in range(40):
        windows = [[(rng.randrange(3), 4), (7, rng.randrange(8, 12))] for _ in range(3)]
        reference = {i: rng.randrange(13) for i in range(3) if rng.random() < .8}
        matrix = [[rng.randrange(3) for _ in range(4)] for _ in range(4)]
        inst = instance(windows, reference, start=rng.randrange(3), end=12, matrix=matrix)
        route = RouteEval(inst, 0, [0, 1, 2])
        if not route.ok:
            continue
        feasible = {}
        for times in product(range(12), repeat=3):
            ready, previous = inst.engineers[0].shift_start, 0
            for i, time in enumerate(times):
                if (time < ready + matrix[previous][i + 1]
                        or not any(a <= time <= b for a, b in windows[i])):
                    break
                ready, previous = time + 1, i + 1
            else:
                feasible[times] = sum(abs(times[i] - value) for i, value in reference.items())
        actual = starts(inst)
        assert tuple(actual) in feasible
        assert feasible[tuple(actual)] == min(feasible.values())
        assert route.profile_key(inst)[1] == min(feasible.values())
        checked += 1
    assert checked >= 15


def test_adapter_keeps_waiting_when_adding_a_job_to_reference_plan(make_scenario):
    scenario = make_scenario([job('new', start=0, end=0, duration=1),
                              job('kept', start=0, end=30, duration=1)],
                             [engineer(start=0, end=35)])
    matrix = travel(scenario, minutes=0, metres=0)
    reference = make_plan(scenario, matrix, {'e1': ['kept']}, 'reference', 'valid')
    route = reference.routes[0]
    visit = route.visits[0].model_copy(update={'start': 20, 'end': 21})
    reference = reference.model_copy(update={'routes': [route.model_copy(update={'visits': [visit]})]})
    assert validate_plan(scenario, matrix, reference) == []
    result = AlnsSolver().solve(scenario, matrix, profile='stable', reference=reference, time_limit_seconds=.1)
    assert validate_plan(scenario, matrix, result) == []
    assert result.metrics.assigned == 2
    assert {v.request_id: v.start for r in result.routes for v in r.visits} == {'new': 0, 'kept': 20}
    assert objective_key(result, scenario, profile='stable', reference=reference)[4] == 0


def test_solution_copy_keeps_independent_cached_schedule_and_route_state():
    inst = instance([[(0, 30)], [(0, 10)]], {0: 20})
    original = Solution({0: RouteEval(inst, 0, [0])}, {1})
    assert original.routes[0].profile_key(inst) == (0, 0)
    copied = original.copy(inst)
    copied.insert(inst, 1, 0, 1)
    assert original.routes[0].seq == [0]
    assert original.routes[0].E == [0]
    assert original.unassigned == {1}
    assert original.routes[0].profile_key(inst) == (0, 0)
    assert copied.routes[0].profile_key(inst) == (0, 11)
    original.routes[0].used['router'] = 7
    assert copied.routes[0].used['router'] == 0


def test_cached_schedule_observes_changed_reference_and_profile():
    inst = instance([[(0, 30)]], {0: 20})
    route = RouteEval(inst, 0, [0])
    assert list(route.start_times(inst)) == [20]
    inst.prev_start[0] = 12
    assert list(route.start_times(inst)) == [12]
    inst.profile = 'sla'
    inst.tasks[0].priority = 3
    assert list(route.start_times(inst)) == [0]
    inst.profile = 'stable'
    assert route.profile_key(inst) == (0, 0)
