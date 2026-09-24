"""Contract regressions for the integrated, adapted teammate kernel."""
import numpy as np
import time

import pytest

from field_router._alns.instance import Engineer, Instance, Task
from field_router._alns.solution import RouteEval, Solution, validate


def task(j, *, priority=1, duration=1, end=100, skill="local"):
    return Task(j, f"t{j}", 0, 0, 0, end, duration, skill, None, priority)


def instance(tasks, *, stock=None, end=100, matrix=None, engineers=1):
    eng = [Engineer(i, f"e{i}", f"Engineer {i}", 0, 0, 0, end,
                    frozenset({"local"}), "car") for i in range(engineers)]
    for e in eng:
        e.stock = stock
    n = len(tasks) + engineers
    matrix = np.zeros((n, n)) if matrix is None else np.array(matrix)
    return Instance("regression", tasks, eng, matrices={
        "km": {m: matrix for m in ("car", "walk", "bike", "transit")},
        "minutes": {m: matrix for m in ("car", "walk", "bike", "transit")},
    }).build_matrices()


def solution(inst, seq, e=0):
    return Solution({e: RouteEval(inst, e, seq)} if seq else {},
                    set(range(inst.N)) - set(seq))


def test_high_priority_cannot_be_traded_for_eleven_normal_jobs():
    inst = instance([task(0, priority=2, duration=11)] + [task(j) for j in range(1, 12)], end=11)
    high, normal = solution(inst, [0]), solution(inst, list(range(1, 12)))
    assert not validate(inst, high) and not validate(inst, normal)
    assert high.cost(inst) < normal.cost(inst)


def test_route_checks_aggregate_stock_not_only_single_job_demand():
    jobs = [task(0), task(1)]
    for t in jobs:
        t.equipment = {"router": 1, "tv_box": 0}
    inst = instance(jobs, stock={"router": 1, "tv_box": 0})
    route = RouteEval(inst, 0, [0])
    assert route.ok
    assert route.insertion(inst, 1) is None
    assert not RouteEval(inst, 0, [0, 1]).ok
    assert validate(inst, solution(inst, [0, 1]))


def test_disjoint_windows_require_waiting_for_later_opening():
    job = task(0, end=40)
    job.windows = ((0, 10), (30, 40))
    inst = instance([job], matrix=[[0, 20], [20, 0]])
    assert RouteEval(inst, 0, [0]).E == [30]


def test_route_rejects_missing_skill_without_coercion():
    inst = instance([task(0, skill="unsupported")])
    assert not RouteEval(inst, 0, [0]).ok


def test_external_matrices_never_fill_missing_used_mode_with_haversine():
    inst = instance([task(0)])
    inst.matrices = {"km": {}, "minutes": {}}
    try:
        inst.build_matrices()
    except ValueError:
        return
    assert False, "Missing travel mode must fail explicitly"


def test_urgent_is_strictly_preferred_to_many_high_jobs():
    inst = instance([task(0, priority=3, duration=12)] +
                    [task(j, priority=2) for j in range(1, 13)], end=12)
    assert solution(inst, [0]).cost(inst) < solution(inst, list(range(1, 13))).cost(inst)


def test_unknown_stock_is_unlimited_but_zero_tv_stock_is_not():
    jobs = [task(0), task(1)]
    for t in jobs:
        t.equipment = {"tv_box": 1}
    assert RouteEval(instance(jobs), 0, [0, 1]).ok
    assert not RouteEval(instance(jobs, stock={"router": 100, "tv_box": 0}), 0, [0]).ok


def test_multiple_window_insertion_agrees_with_full_route_evaluation():
    rng = np.random.default_rng(94)
    for _ in range(40):
        jobs = [task(j, duration=3) for j in range(5)]
        for t in jobs:
            t.windows = ((0, 15), (25, 40), (65, 85))
        inst = instance(jobs, matrix=rng.integers(0, 22, size=(6, 6)))
        for seq in ([0], [0, 1], [1, 2, 3]):
            route = RouteEval(inst, 0, seq)
            if not route.ok:
                continue
            feasible = [p for p in range(len(seq) + 1)
                        if RouteEval(inst, 0, seq[:p] + [4] + seq[p:]).ok]
            inserted = route.insertion(inst, 4)
            assert bool(feasible) == (inserted is not None)
            if inserted:
                assert inserted[1] in feasible


def test_profile_keys_prioritize_start_or_owner_before_resource_cost():
    inst = instance([task(0, priority=3)], engineers=2, matrix=[[0, 0, 20], [0, 0, 2], [0, 0, 0]])
    early, late = solution(inst, [0], 1), solution(inst, [0], 0)
    inst.profile = 'sla'
    assert early.objective_key(inst) < late.objective_key(inst)
    inst.profile, inst.prev_owner, inst.prev_start = 'stable', {0: 0}, {0: 20}
    assert late.objective_key(inst) < early.objective_key(inst)


def test_solver_serves_task_only_reachable_via_another_job_without_certifying_bound():
    from field_router._alns.solver import solve
    inst = instance([task(0, duration=10), task(1, priority=3, end=60)], end=100,
                    matrix=[[0, 10, 120], [10, 0, 10], [120, 10, 0]])
    result, info = solve(inst, time_limit_seconds=.12, seed=4)
    assert not result.unassigned
    assert not validate(inst, result)
    assert info['certificate'] == {}
    assert info['optimality_proven'] is False


def test_sp_rechecks_stock_and_external_start_arcs_when_enriching_routes():
    from field_router._alns.sp import enrich_pool
    job = task(0)
    job.equipment = {'router': 1}
    inst = instance([job], engineers=2, matrix=[[0, 0, 2], [0, 0, 8], [0, 0, 0]])
    inst.engineers[1].stock = {'router': 0}
    assert (1, (0,)) not in enrich_pool(inst, {(0, (0,)): 2})
    inst.engineers[1].stock = None
    assert enrich_pool(inst, {(0, (0,)): 2})[(1, (0,))] == 8


def test_solver_obeys_overall_budget_and_returns_feasible_incumbent():
    from field_router._alns.solver import solve
    jobs = [task(j, priority=(j % 3) + 1) for j in range(120)]
    inst = instance(jobs, engineers=8)
    start = time.perf_counter()
    result, info = solve(inst, time_limit_seconds=.06, seed=7)
    elapsed = time.perf_counter() - start
    assert elapsed < .30
    assert not validate(inst, result)
    assert info['total_time'] < .30


@pytest.mark.parametrize('budget', [0, -1, float('inf'), float('nan')])
def test_solver_rejects_invalid_global_budgets(budget):
    from field_router._alns.solver import solve
    with pytest.raises(ValueError):
        solve(instance([task(0)]), time_limit_seconds=budget)


@pytest.mark.parametrize('priority', [2, 3])
def test_alns_and_sp_preserve_priority_when_many_low_jobs_compete(priority):
    from field_router._alns.solver import solve
    from field_router._alns.sp import solve_sp
    jobs = [task(0, priority=priority, duration=12)] + [task(j) for j in range(1, 13)]
    inst = instance(jobs, end=12)
    low = solution(inst, list(range(1, 13)))
    result, _ = solve(inst, time_limit_seconds=.12, init=low, seed=1)
    assert result.task_owner().get(0) == 0
    assert not validate(inst, result)
    pool = {(0, (0,)): 0, (0, tuple(range(1, 13))): 0}
    result, _ = solve_sp(inst, pool, time_limit=.1, hint=low)
    assert result.task_owner().get(0) == 0


@pytest.mark.parametrize('profile, expected', [('cost', 1), ('sla', 1), ('stable', 0)])
def test_sp_and_search_use_same_profile_order(profile, expected):
    from field_router._alns.solver import solve
    from field_router._alns.sp import solve_sp
    inst = instance([task(0, priority=3)], engineers=2,
                    matrix=[[0, 0, 20], [0, 0, 2], [0, 0, 0]])
    inst.profile, inst.prev_owner, inst.prev_start = profile, {0: 0}, {0: 20}
    pool = {(0, (0,)): 20, (1, (0,)): 2}
    result, _ = solve_sp(inst, pool, time_limit=.1)
    assert result.task_owner()[0] == expected
    result, _ = solve(inst, time_limit_seconds=.1, seed=2)
    assert result.task_owner()[0] == expected


def test_alns_repair_restores_feasibility_after_nonmetric_removal():
    from field_router._alns.alns import ALNS
    inst = instance([task(0, duration=10), task(1, priority=3, end=60)], end=100,
                    matrix=[[0, 10, 120], [10, 0, 10], [120, 10, 0]])
    incumbent = solution(inst, [0, 1])
    incumbent.remove(inst, 0)
    assert not incumbent.routes[0].ok
    repaired = ALNS(inst, seed=0).r_greedy(incumbent)
    assert not repaired.unassigned
    assert not validate(inst, repaired)


def test_sp_rejects_invalid_columns_even_when_km_was_supplied():
    from field_router._alns.sp import solve_sp
    jobs = [task(0), task(1)]
    for t in jobs:
        t.equipment = {'router': 1}
    inst = instance(jobs, stock={'router': 1})
    result, _ = solve_sp(inst, {(0, (0, 1)): 0, (0, (0,)): 1}, time_limit=.1)
    assert not validate(inst, result)
    assert len(result.unassigned) == 1


def test_empty_problem_returns_immediately_and_fixed_prefix_survives():
    from field_router._alns.solver import solve
    empty, _ = solve(instance([]), time_limit_seconds=5)
    assert not empty.routes and not empty.unassigned
    inst = instance([task(j) for j in range(4)], engineers=2)
    result, _ = solve(inst, time_limit_seconds=.1, fixed={1: [2, 1]}, seed=42)
    assert result.routes[1].seq[:2] == [2, 1]
    assert not validate(inst, result)


# ---- v2: SISR + равномерный выбор операторов + фазы по «отсутствиям» ----

def _spread(n=12, engineers=3):
    rng = np.random.default_rng(0)
    pts = rng.uniform(0, 30, size=(n + engineers, 2))
    matrix = np.ceil(np.linalg.norm(pts[:, None] - pts[None], axis=2))
    return instance([task(j, duration=10, end=240) for j in range(n)], end=240, matrix=matrix, engineers=engineers)


@pytest.mark.parametrize("ruins,adaptive", [("mixed", False), ("sisr", False), ("classic", True)])
def test_v2_operator_sets_return_valid_incumbents(ruins, adaptive):
    from field_router._alns.alns import ALNS
    inst = _spread()
    alns = ALNS(inst, seed=1, ruins=ruins, adaptive=adaptive)
    best = alns.run(time_limit=0.5, deadline=time.perf_counter() + 0.5)
    assert not validate(inst, best)


def test_blink_insertion_skips_positions_but_never_returns_infeasible():
    inst = _spread(n=6, engineers=1)
    route = RouteEval(inst, 0, [0, 1, 2])
    rng = np.random.default_rng(3)
    for _ in range(50):
        ins = route.insertion(inst, 3, blink=0.5, rng=rng)
        if ins is not None:
            seq = list(route.seq)
            seq.insert(ins[1], 3)
            assert RouteEval(inst, 0, seq).ok


def test_absence_reduction_keeps_fixed_prefix_and_never_worsens_coverage():
    from field_router._alns.alns import coverage, reduce_by_absences
    inst = _spread()
    start = solution(inst, [0, 1])
    fixed = {0: [0]}
    target = coverage(inst, start)
    found = reduce_by_absences(inst, start, deadline=time.perf_counter() + 1.0, seed=0, fixed=fixed,
                               goal=lambda s: coverage(inst, s) < target)
    assert found is not None
    assert not validate(inst, found)
    assert found.routes[0].seq[0] == 0
    assert coverage(inst, found) < target


def test_solve_with_absence_phases_respects_shared_deadline():
    from field_router._alns.solver import solve
    inst = _spread(n=20, engineers=4)
    t0 = time.perf_counter()
    best, info = solve(inst, time_limit_seconds=3.0, seed=0)
    assert time.perf_counter() - t0 < 3.0 + 0.5
    assert not validate(inst, best)
    assert 'absence' in info
    _, short = solve(inst, time_limit_seconds=1.0, seed=0)     # короткий бюджет: только основной поиск
    assert 'absence' not in short
