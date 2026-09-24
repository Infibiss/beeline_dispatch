"""Optional mid-day equipment replenishment in the internal ALNS kernel.

Depots are not passed by the application adapter yet; these tests pin the
kernel contract so the feature can be wired in later without surprises."""
import time

import numpy as np

from field_router._alns.instance import Depot, Engineer, Instance, Task
from field_router._alns.solution import RouteEval, Solution, schedule, validate


def job(j, *, routers=1, end=600, duration=10):
    t = Task(j, f"t{j}", 0, 0, 0, end, duration, "local", None, 1)
    t.equipment = {"router": routers, "tv_box": 0}
    return t


def instance(jobs, *, depot_minutes=None, stock=1, end=600, reload_minutes=15):
    """One engineer, jobs 10 minutes apart; an optional depot at depot_minutes from every node."""
    eng = Engineer(0, "e0", "Engineer 0", 0, 0, 0, end, frozenset({"local"}), "car")
    eng.stock = {"router": stock, "tv_box": 0}
    depots = [Depot(0, "warehouse", reload_minutes)] if depot_minutes is not None else []
    n = 1 + len(jobs) + len(depots)
    matrix = np.full((n, n), 10.0)
    if depots:
        matrix[-1, :] = matrix[:, -1] = depot_minutes
    np.fill_diagonal(matrix, 0)
    return Instance("replenishment", jobs, [eng], depots=depots, matrices={
        "km": {m: matrix for m in ("car", "walk", "bike", "transit")},
        "minutes": {m: matrix for m in ("car", "walk", "bike", "transit")},
    }).build_matrices()


def test_without_depots_stock_stays_a_hard_route_limit():
    inst = instance([job(0), job(1)])
    assert not RouteEval(inst, 0, [0, 1]).ok
    assert RouteEval(inst, 0, [0]).insertion(inst, 1) is None


def test_reload_before_the_job_that_exceeds_stock():
    inst = instance([job(0), job(1)], depot_minutes=20)
    route = RouteEval(inst, 0, [0, 1])
    assert route.ok
    assert [leg[2] for leg in route.legs] == [None, 0]           # reload only before the second job
    # 10 min to job 0, service 10, then 20 + 15 reload + 20 to job 1
    assert route.E == [10, 75]
    assert route.km == 10 + 20 + 20 and route.travel_minutes == 10 + 40
    stops = schedule(inst, 0, [0, 1])
    assert stops[1]["reload_depot"] == "warehouse" and stops[1]["leg_min"] == 55


def test_reload_detour_respects_windows_and_shift():
    late = job(1, end=60)                                         # must start by 60, reload arrives at 75
    inst = instance([job(0), late], depot_minutes=20)
    assert not RouteEval(inst, 0, [0, 1]).ok
    _, position = RouteEval(inst, 0, [0]).insertion(inst, 1)
    assert position == 0                                          # the tight job goes first, reload before job 0
    reordered = RouteEval(inst, 0, [1, 0])
    assert reordered.ok and [leg[2] for leg in reordered.legs] == [None, 0]
    inst = instance([job(0), job(1)], depot_minutes=20, end=80)   # job 1 would end at 85 > shift end
    assert not RouteEval(inst, 0, [0, 1]).ok


def test_insertion_places_job_where_reload_is_cheapest():
    jobs = [job(0), job(1, routers=0), job(2)]
    inst = instance(jobs, depot_minutes=20)
    route = RouteEval(inst, 0, [0, 1])
    delta, position = route.insertion(inst, 2)
    candidate = RouteEval(inst, 0, route.seq[:position] + [2] + route.seq[position:])
    assert candidate.ok and abs(candidate.km - route.km - delta) < 1e-9
    assert sum(leg[2] is not None for leg in candidate.legs) == 1


def test_solver_serves_more_jobs_when_depots_exist():
    from field_router._alns.solver import solve
    jobs = [job(j) for j in range(4)]
    without, _ = solve(instance(jobs), time_limit_seconds=1.0, seed=0)
    with_depot, _ = solve(instance([job(j) for j in range(4)], depot_minutes=15), time_limit_seconds=1.0, seed=0)
    assert len(without.unassigned) == 3
    assert not with_depot.unassigned
    assert not validate(instance([job(j) for j in range(4)], depot_minutes=15), with_depot)
