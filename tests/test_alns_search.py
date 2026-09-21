"""Search improvements must keep feasibility, priority and budget guarantees."""
from test_alns_core import instance, solution, task
from field_router._alns.alns import ALNS
from field_router._alns.solution import validate


def test_reachability_pruning_keeps_indirect_nonmetric_path():
    inst = instance([task(0, duration=10), task(1, end=60), task(2, end=5)],
                    matrix=[[0, 10, 120, 200], [10, 0, 10, 200],
                            [120, 10, 0, 200], [200, 200, 200, 0]])
    search = ALNS(inst)
    assert search.compat == [[0], [0], []]


def test_search_can_temporarily_open_route_without_regressing_incumbent():
    inst = instance([task(i) for i in range(4)], engineers=2)
    search = ALNS(inst, seed=0)
    best = solution(inst, [0, 1, 2, 3])
    # This transition is needed for inter-route exchanges. It must be possible
    # during exploration while the published incumbent uses the exact tuple.
    base = best.objective_key(inst)
    worse = base[:-2] + (base[-2] + 1, base[-1])
    assert any(search.accept(worse, base, base, temperature=1) for _ in range(100))
    result = search.run(time_limit=.03, init=best)
    assert result.objective_key(inst) <= base
    assert not validate(inst, result)


def test_search_does_not_wander_arbitrarily_far_from_best_coverage():
    inst = instance([task(i, priority=3) for i in range(4)])
    search = ALNS(inst)
    assert not search.accept((3, 3, 3, 1, 0), (0, 0, 0, 1, 0),
                             (0, 0, 0, 1, 0), temperature=100)


def test_destroy_targets_blockers_of_a_waiting_high_priority_job():
    inst = instance([task(0), task(1), task(2, priority=3)], engineers=2)
    inst.engineers[1].skills = frozenset()
    search = ALNS(inst, seed=0)
    current = solution(inst, [0, 1])
    search.d_conflict(current)
    assert {0, 1} & current.unassigned
    assert 2 in current.unassigned


def test_search_collects_feasible_rejected_routes_for_recombination():
    inst = instance([task(i) for i in range(3)], engineers=2)
    search = ALNS(inst, seed=0)
    best = solution(inst, [0, 1, 2])
    # A feasible route in a rejected solution can complete a useful partition.
    def destroy(sol):
        sol.remove(inst, 2)
        sol.insert(inst, 2, 1, 0)
    search.destroys = [destroy]
    search.repairs = [lambda sol: sol]
    search.run(time_limit=.1, max_iter=1, init=best)
    assert (1, (2,)) in search.pool.routes


def test_recombination_optimizes_all_cost_tiers_together():
    from field_router._alns.sp import solve_sp
    inst = instance([task(i) for i in range(3)], engineers=2)
    hint = solution(inst, [0, 1])
    hint.insert(inst, 2, 1, 0)
    result, info = solve_sp(inst, {(0, (0, 1)): 0, (1, (2,)): 0,
                                   (0, (0, 1, 2)): 0}, time_limit=.1, hint=hint)
    assert not result.unassigned and len(result.routes) == 1
    assert len(info['stages']) == 1
    assert not validate(inst, result)


def test_recombination_discards_dominated_orders_but_keeps_fixed_prefix():
    from field_router._alns.sp import solve_sp
    inst = instance([task(0), task(1)], matrix=[[0, 8, 1], [1, 0, 1], [1, 1, 0]])
    pool = {(0, (0, 1)): 9, (0, (1, 0)): 2}
    result, info = solve_sp(inst, pool, time_limit=.1)
    assert result.routes[0].seq == [1, 0]
    assert info['columns'] == 1
    result, info = solve_sp(inst, pool, time_limit=.1, fixed={0:[0]})
    assert result.routes[0].seq == [0, 1]


def test_large_distance_coefficients_split_without_trading_priority():
    from field_router._alns.sp import solve_sp
    jobs = [task(0, duration=12, priority=3)] + [task(i) for i in range(1, 13)]
    inst = instance(jobs, end=12)
    inst.kml['car'][0][inst.node(0)] = 1e12
    low = solution(inst, list(range(1, 13)))
    result, info = solve_sp(inst, {(0, (0,)): 1e12,
                                  (0, tuple(range(1, 13))): 0}, hint=low, time_limit=.3)
    assert result.task_owner() == {0: 0}
    assert len(info['stages']) > 1
    assert all(s['solver_status'] in ('OPTIMAL', 'FEASIBLE') for s in info['stages'].values())


def test_stable_scheduling_keeps_fixed_order_and_uses_waiting():
    from field_router._alns.solver import solve
    inst = instance([task(i, duration=10) for i in range(3)], end=100)
    inst.profile, inst.prev_start, inst.prev_owner = 'stable', {2: 20, 0: 50, 1: 80}, {i: 0 for i in range(3)}
    result, _ = solve(inst, time_limit_seconds=.15, fixed={0: [2, 0]}, seed=7)
    assert result.routes[0].seq[:2] == [2, 0]
    assert tuple(result.routes[0].start_times(inst)) == (20, 50, 80)
    assert not validate(inst, result)
