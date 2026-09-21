"""Bounded ALNS + route-pool set partitioning, without unverified certificates."""
from __future__ import annotations
import math
import time
from .instance import Instance
from .alns import ALNS
from .sp import solve_sp, enrich_pool
from .solution import Solution, validate


def better(inst, candidate: Solution, incumbent: Solution) -> bool:
    return candidate.objective_key(inst) < incumbent.objective_key(inst)


def solve(inst: Instance, time_limit_seconds=5.0, seed=0, fixed=None,
          init: Solution | None = None, log=False):
    """Return the best feasible incumbent within one shared wall-clock budget.

    Cooperative checks cover repair, pool construction, and solver stages. CP-SAT
    receives only the remaining budget. Small interpreter/solver shutdown overhead
    can remain; this is not an operating-system hard realtime guarantee.
    """
    if not math.isfinite(time_limit_seconds) or time_limit_seconds <= 0:
        raise ValueError('time_limit_seconds must be finite and positive')
    if inst.profile not in (None, 'cost', 'sla', 'stable'):
        raise ValueError('Unknown objective profile')
    start = time.perf_counter()
    deadline = start + time_limit_seconds
    best = init.copy(inst) if init is not None else Solution(unassigned=set(range(inst.N)))
    if validate(inst, best):
        raise ValueError('Initial solution is infeasible')
    if fixed:
        required = Solution(unassigned=set(range(inst.N)))
        for e, seq in fixed.items():
            for j in seq:
                required.insert(inst, j, e, len(required.routes[e].seq) if e in required.routes else 0)
        if validate(inst, required):
            raise ValueError('Fixed route prefixes are infeasible')
        if init is None:
            best = required
        elif any(tuple(best.routes[e].seq[:len(seq)]) != tuple(seq)
                 if e in best.routes else bool(seq) for e, seq in fixed.items()):
            raise ValueError('Initial solution does not preserve fixed prefixes')
    info = dict(certificate={}, optimality_proven=False,
                bounds_status='disabled_unverified_nonmetric_pricing', seed=seed,
                time_limit_seconds=time_limit_seconds, profile=inst.profile)
    if not inst.N:
        info['total_time'] = time.perf_counter()-start
        return best, info
    # Longer runs recombine mid-search, so ALNS explores the improved partition
    # instead of only seeing it after exploration has ended. Short runs avoid
    # paying model-construction overhead twice.
    stages = [(.34, .37, .46), (.73, .77, .91)] if time_limit_seconds >= 6 else [(.63, .69, .90)]
    pool_search = None
    rounds = []
    for round_index, (search_end, pool_end, sp_end) in enumerate(stages):
        alns = ALNS(inst, seed=seed + 101*round_index, fixed=fixed)
        if pool_search is not None:
            alns.pool = pool_search
        alns_deadline = start + search_end*time_limit_seconds
        candidate = alns.run(time_limit=max(0, alns_deadline-time.perf_counter()),
                             deadline=alns_deadline, init=best, log=log)
        if not validate(inst, candidate) and better(inst, candidate, best):
            best = candidate
        pool_search = alns.pool
        run_info = dict(iterations=alns.iterations, restarts=alns.restarts,
                        metrics=best.metrics(inst), time=time.perf_counter()-start)
        pool_deadline = start + pool_end*time_limit_seconds
        pool = alns.pool.materialize(inst, deadline=pool_deadline)
        pool = enrich_pool(inst, pool, deadline=pool_deadline)
        for e, route in best.routes.items():
            pool[(e, tuple(route.seq))] = route.km
        sp_deadline = start + sp_end*time_limit_seconds
        if time.perf_counter() < sp_deadline:
            candidate, sp_info = solve_sp(inst, pool, time_limit=max(0, sp_deadline-time.perf_counter()),
                                         deadline=sp_deadline, hint=best, fixed=fixed, seed=seed)
            info['sp'] = run_info['sp'] = sp_info
            if candidate is not None and not validate(inst, candidate) and better(inst, candidate, best):
                best = candidate
                pool_search.add_solution(best)
        rounds.append(run_info)
    info['alns'] = dict(iterations=sum(r['iterations'] for r in rounds),
                        restarts=sum(r['restarts'] for r in rounds), metrics=best.metrics(inst))
    info['rounds'] = rounds
    remaining = deadline-time.perf_counter()
    if remaining > .002:
        # Explicitly try to pack the same coverage into one fewer route.
        # Retain the unconstrained incumbent if this attempt is unsuccessful.
        polish = ALNS(inst, seed=seed+1, fixed=fixed,
                      max_routes=max(0, len(best.routes)-1) if inst.profile in (None, 'cost') else None)
        candidate = polish.run(time_limit=max(0, deadline-time.perf_counter()), deadline=deadline, init=best)
        if not validate(inst, candidate) and better(inst, candidate, best):
            best = candidate
        info['polish_iterations'] = polish.iterations
    errors = validate(inst, best)
    if errors:
        raise RuntimeError(f'Internal ALNS returned invalid incumbent: {errors}')
    info['objective_key'] = best.objective_key(inst)
    info['total_time'] = time.perf_counter()-start
    return best, info
