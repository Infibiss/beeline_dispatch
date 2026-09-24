"""Bounded ALNS + route-pool set partitioning, without unverified certificates."""
from __future__ import annotations
import math
import time
from .instance import Instance
from .alns import ALNS, coverage, reduce_by_absences
from .sp import solve_sp, enrich_pool
from .solution import Solution, validate


V2_MIN_SECONDS = 2.5


def better(inst, candidate: Solution, incumbent: Solution) -> bool:
    return candidate.objective_key(inst) < incumbent.objective_key(inst)


def solve(inst: Instance, time_limit_seconds=5.0, seed=0, fixed=None,
          init: Solution | None = None, log=False, absence_phases=True):
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
    # Конфигурация v2 (SISR + равномерный выбор + фазы по «отсутствиям») — только с бюджета V2_MIN_SECONDS.
    # На коротком бюджете она ухудшала покрытие: сценарий дня по 1 с, hybrid на 3 с (ALNS ≈ 1,7 с);
    # там остаётся прежний поиск без изменений (docs/reviews/alns-v2-port.md).
    v2 = time_limit_seconds >= V2_MIN_SECONDS
    absence_phases = absence_phases and v2
    ops = {} if v2 else dict(ruins='classic', adaptive=True)
    if absence_phases:
        # Доли бюджета под фазы по «отсутствиям»: сначала невыполненные, утром — ещё и число бригад.
        stages = ([(.30, .33, .41), (.62, .65, .76)] if time_limit_seconds >= 6 else [(.52, .57, .72)])
    pool_search = None
    rounds = []
    for round_index, (search_end, pool_end, sp_end) in enumerate(stages):
        alns = ALNS(inst, seed=seed + 101*round_index, fixed=fixed, **ops)
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
    if absence_phases:
        info['absence'] = _absence_phases(inst, best, start, time_limit_seconds, seed, fixed)
        improved = info['absence'].pop('solution', None)
        if improved is not None and not validate(inst, improved) and better(inst, improved, best):
            best = improved
    remaining = deadline-time.perf_counter()
    if remaining > .002:
        # Morning planning tries one fewer route; operational profiles polish
        # freely because fewer engineers may worsen their earlier priorities.
        polish = ALNS(inst, seed=seed+1, fixed=fixed, **ops,
                      max_routes=max(0, len(best.routes)-1) if inst.profile is None else None)
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


def _absence_phases(inst, best, start, budget, seed, fixed):
    """Фазы по «отсутствиям» в общем бюджете: (1) меньше невыполненных заявок при любом числе бригад;
    (2) только утром — план на одну бригаду меньше без потери покрытия. Каждая фаза ограничена своей
    долей бюджета; результат принимается только при строгом улучшении полного кортежа цели."""
    out, current = {}, best
    unassigned_end = start + (.84 if budget >= 6 else .81) * budget
    if current.unassigned and time.perf_counter() < unassigned_end:
        target = coverage(inst, current)
        found = reduce_by_absences(inst, current, deadline=unassigned_end, seed=seed + 300, fixed=fixed,
                                   goal=lambda s, t=target: coverage(inst, s) < t)
        out['unassigned_improved'] = found is not None
        if found is not None and not validate(inst, found):
            current = found
    fleet_end = start + (.92 if budget >= 6 else .90) * budget
    tries = 0
    while inst.profile is None and len(current.routes) > 1 and time.perf_counter() < fleet_end:
        k = len(current.routes) - 1
        trial = current.copy(inst)
        victims = sorted((len(r.seq), e) for e, r in trial.routes.items() if not (fixed and e in fixed))
        if not victims:
            break
        for j in list(trial.routes[victims[0][1]].seq):
            trial.remove(inst, j, victims[0][1])
        target = coverage(inst, current)
        found = reduce_by_absences(inst, trial, deadline=fleet_end, seed=seed + 400 + tries, fixed=fixed,
                                   max_routes=k, goal=lambda s, t=target, k=k: coverage(inst, s) <= t
                                   and len(s.routes) <= k)
        tries += 1
        if found is None or validate(inst, found):
            break
        current = found
    out['fleet_tries'] = tries
    out['solution'] = current if current is not best else None
    return out
