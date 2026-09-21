"""CP-SAT set partitioning over validated ALNS routes, with exact priority stages.

A restricted route pool yields a heuristic incumbent, never a global certificate.
"""
from __future__ import annotations
import time
from ortools.sat.python import cp_model
from .instance import Instance
from .solution import Solution, RouteEval


def enrich_pool(inst: Instance, pool: dict, deadline=float('inf')) -> dict:
    """Re-evaluate every transferred route, including its engineer-specific arcs."""
    out = dict(pool)
    for _, seq in list(pool):
        if time.perf_counter() >= deadline:
            break
        for e, engineer in enumerate(inst.engineers):
            if time.perf_counter() >= deadline:
                return out
            if (e, seq) in out or not all(inst.compatible(engineer, inst.tasks[j]) for j in seq):
                continue
            route = RouteEval(inst, e, seq)
            if route.ok:
                out[(e, seq)] = route.km
    return out


def solve_sp(inst: Instance, pool: dict, time_limit=30, hint: Solution | None = None,
             fixed=None, workers=1, seed=0, deadline=None):
    start = time.perf_counter()
    deadline = min(start + max(0.0, time_limit), deadline if deadline is not None else float('inf'))
    keys, routes = [], []
    for key in pool:
        if time.perf_counter() >= deadline:
            return hint, dict(status='deadline', columns=len(keys), stages={})
        e, seq = key
        if not seq or len(set(seq)) != len(seq):
            continue
        if fixed and e in fixed and tuple(seq[:len(fixed[e])]) != tuple(fixed[e]):
            continue
        route = RouteEval(inst, e, seq)
        if route.ok:
            keys.append(key)
            routes.append(route)
    model = cp_model.CpModel()
    x = [model.NewBoolVar(f'x{p}') for p in range(len(keys))]
    y = [model.NewBoolVar(f'y{j}') for j in range(inst.N)]
    by_task, by_eng = [[] for _ in inst.tasks], [[] for _ in inst.engineers]
    for p, (e, seq) in enumerate(keys):
        if time.perf_counter() >= deadline:
            return hint, dict(status='deadline', columns=len(keys), stages={})
        by_eng[e].append(x[p])
        for j in seq:
            by_task[j].append(x[p])
    for j in range(inst.N):
        model.Add(sum(by_task[j]) + y[j] == 1)
    for e in range(inst.E):
        if by_eng[e]:
            model.AddAtMostOne(by_eng[e])
        if fixed and fixed.get(e):
            model.Add(sum(by_eng[e]) == 1)
    if hint is not None:
        hint_keys = {(e, tuple(r.seq)) for e, r in hint.routes.items()}
        for p, key in enumerate(keys):
            model.AddHint(x[p], key in hint_keys)
        for j in range(inst.N):
            model.AddHint(y[j], j in hint.unassigned)
    objectives = [('unassigned_urgent', sum(y[j] for j, t in enumerate(inst.tasks) if t.priority == 3)),
                  ('unassigned_high', sum(y[j] for j, t in enumerate(inst.tasks) if t.priority >= 2)),
                  ('unassigned', sum(y))]
    if inst.profile == 'sla':
        objectives.append(('high_tier_start_sum', sum(int(r.profile_key(inst)[0])*x[p]
                                                   for p, r in enumerate(routes))))
    elif inst.profile == 'stable':
        for i, name in enumerate(('reassigned', 'absolute_start_shift')):
            objectives.append((name, sum(int(r.profile_key(inst)[i])*x[p] for p, r in enumerate(routes))))
    objectives += [('engineers', sum(x)),
                   ('distance_m', sum(int(round(r.km*1000))*x[p] for p, r in enumerate(routes)))]
    stages, values_x, values_y = {}, None, None
    for name, expression in objectives:
        remaining = deadline - time.perf_counter()
        if remaining <= .001:
            break
        model.Minimize(expression)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = remaining
        solver.parameters.num_search_workers = workers
        solver.parameters.random_seed = seed
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            stages[name] = dict(value=None, solver_status=solver.StatusName(status))
            break
        value = int(round(solver.ObjectiveValue()))
        stages[name] = dict(value=value, solver_status=solver.StatusName(status))
        values_x, values_y = [solver.Value(v) for v in x], [solver.Value(v) for v in y]
        # Never let a later stage worsen any already found coverage/profile value.
        model.Add(expression <= value)
        model.ClearHints()
        for var, val in zip(x + y, values_x + values_y):
            model.AddHint(var, val)
    result = hint
    if values_x is not None:
        result = Solution({keys[p][0]: routes[p] for p, selected in enumerate(values_x) if selected},
                          {j for j, dropped in enumerate(values_y) if dropped})
    return result, dict(status='ok' if values_x is not None else 'deadline',
                        time=time.perf_counter()-start, columns=len(keys), stages=stages)
