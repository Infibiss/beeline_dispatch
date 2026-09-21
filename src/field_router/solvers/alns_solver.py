"""Adapt the teammate's ALNS route-pool search to the dispatcher contract.

All geography, state reduction and acceptance checks belong to field_router.
No default distances, guessed inventory, historical replay or quality certificate.
"""
import json
import math
from time import perf_counter

from ..models import Plan, Transport
from ..planning import build_route, make_plan
from ..validation import require_valid, validate_plan
from .baseline import BaselineSolver
from .ortools_solver import objective_key, _reference_visits

_MODES = {Transport.CAR:'car', Transport.WALK:'walk', Transport.BICYCLE:'bike', Transport.PUBLIC:'transit'}


def _instance(scenario, travel, profile, reference):
    from .._alns.instance import Task, Engineer, Instance
    locations = [e.start_location for e in scenario.engineers] + [r.id for r in scenario.requests]
    matrices = {'km':{}, 'minutes':{}}
    for mode, name in _MODES.items():
        matrices['km'][name] = [[travel.metres(a,b,mode)/1000 for b in locations] for a in locations]
        matrices['minutes'][name] = [[travel.minutes(a,b,mode) for b in locations] for a in locations]
    tasks = [Task(idx=i,id=r.id,lat=0,lon=0,a=min(w.start for w in r.windows),b=max(w.end for w in r.windows),
        service=r.duration_minutes,skill=r.skill.value,transport=_MODES.get(r.required_transport),
        priority={'normal':1,'high':2,'urgent':3}[r.priority],windows=tuple((w.start,w.end) for w in r.windows),
        equipment=r.equipment.model_dump()) for i,r in enumerate(scenario.requests)]
    engineers = [Engineer(idx=i,id=e.id,name=e.name,lat=0,lon=0,shift_start=e.shift.start,shift_end=e.shift.end,
        skills=frozenset(s.value for s in e.skills),mode=_MODES[e.transport],start_label=e.start_location,
        stock=e.stock.model_dump() if e.stock is not None else None) for i,e in enumerate(scenario.engineers)]
    prior = _reference_visits(scenario,reference)
    eng_index = {e.id:i for i,e in enumerate(scenario.engineers)}
    return Instance(scenario.id,tasks,engineers,matrices=matrices,profile=profile,
        prev_owner={i:eng_index[prior[r.id][0]] for i,r in enumerate(scenario.requests) if r.id in prior},
        prev_start={i:prior[r.id][1] for i,r in enumerate(scenario.requests) if r.id in prior}).build_matrices()


def _reference_seed(scenario, travel, reference):
    """Keep feasible surviving jobs, never import facts into the optimizer."""
    if reference is None:
        return None
    if not validate_plan(scenario, travel, reference):
        return reference
    jobs = {r.id:r for r in scenario.requests}
    engineers = {e.id:e for e in scenario.engineers}
    sequences, seen = {}, set()
    for route in reference.routes:
        if route.engineer_id not in engineers:
            continue
        kept = sequences.setdefault(route.engineer_id,[])
        for visit in route.visits:
            key = visit.request_id
            if key in jobs and key not in seen and build_route(engineers[route.engineer_id],[jobs[i] for i in kept+[key]],travel) is not None:
                kept.append(key)
                seen.add(key)
    return make_plan(scenario,travel,sequences,'alns','reference_seed')


def _with_stable_schedule(plan, inst, solution):
    """Export the same optimized starts used by the kernel objective."""
    engineer_index = {e.id: i for i, e in enumerate(inst.engineers)}
    rebuilt = []
    for route in plan.routes:
        e = engineer_index[route.engineer_id]
        starts = solution.routes[e].start_times(inst)
        ready, visits = inst.engineers[e].shift_start, []
        for visit, start in zip(route.visits, starts):
            arrival = ready + visit.travel_minutes
            end = start + visit.end - visit.start
            visits.append(visit.model_copy(update={'arrival':arrival,'start':start,'end':end,
                'explanation':f'Дорога {visit.travel_minutes} мин, ожидание {start-arrival} мин. '
                              'Время выбрано целью сохранения утвержденного расписания; допустимость проверена.'}))
            ready = end
        rebuilt.append(route.model_copy(update={'visits':visits}))
    return plan.model_copy(update={'routes':rebuilt})


class AlnsSolver:
    def solve(self, scenario, travel, *, time_limit_seconds=5, profile=None, reference: Plan | None = None, seed=0, initial_plan: Plan | None = None):
        if profile not in (None,'sla','stable','cost'):
            raise ValueError('Unknown objective profile')
        if not math.isfinite(time_limit_seconds) or time_limit_seconds <= 0:
            raise ValueError('time_limit_seconds must be finite and positive')
        started = perf_counter()
        baseline = BaselineSolver().solve(scenario,travel)
        candidates = [baseline]
        for source in (reference,initial_plan):
            ref = _reference_seed(scenario,travel,source)
            if ref is not None:
                candidates.append(ref)
        key = lambda p: objective_key(p,scenario,profile=profile,reference=reference)
        incumbent = min(candidates,key=key)
        diagnostics = {'optimality_proven':False,'certificate':'disabled', 'profile':profile or 'cost',
                       'seed':seed,'time_limit_seconds':time_limit_seconds,'source':'ALNS-OptimizationKernel/f2f1cc0 (adapted)'}
        if not scenario.requests:
            return incumbent.model_copy(update={'solver':'alns','status':'empty','diagnostics':diagnostics})
        from .._alns.solution import Solution, RouteEval
        from .._alns.solver import solve
        inst = _instance(scenario,travel,profile,reference)
        eng_index = {e.id:i for i,e in enumerate(scenario.engineers)}
        job_index = {r.id:i for i,r in enumerate(scenario.requests)}
        init = Solution(unassigned={job_index[u.request_id] for u in incumbent.unassigned})
        for route in incumbent.routes:
            e = eng_index[route.engineer_id]
            init.routes[e] = RouteEval(inst,e,[job_index[v.request_id] for v in route.visits])
        if profile == 'stable':
            incumbent = _with_stable_schedule(incumbent, inst, init)
            candidates.append(incumbent)
        remaining = time_limit_seconds-(perf_counter()-started)
        status='budget_exhausted_before_search'
        if remaining > 0:
            solution, info = solve(inst,time_limit_seconds=remaining,seed=seed,init=init)
            diagnostics['search'] = json.loads(json.dumps(info,allow_nan=False))
            candidate = make_plan(scenario,travel,
                {scenario.engineers[e].id:[scenario.requests[j].id for j in route.seq] for e,route in solution.routes.items()},
                'alns','feasible_search_completed')
            if profile == 'stable':
                candidate = _with_stable_schedule(candidate, inst, solution)
            require_valid(scenario,travel,candidate)
            candidates.append(candidate)
            incumbent=min(candidates,key=key)
            status='feasible_search_completed' if incumbent is candidate else 'incumbent_retained'
        return require_valid(scenario,travel,incumbent.model_copy(update={'solver':'alns','status':status,
            'elapsed_seconds':perf_counter()-started,'diagnostics':diagnostics}))
