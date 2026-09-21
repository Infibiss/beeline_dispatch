"""CP-SAT set partitioning over validated ALNS routes, with exact priority stages.

A restricted route pool yields a heuristic incumbent, never a global certificate.
"""
from __future__ import annotations
import time
from ortools.sat.python import cp_model
from .instance import Instance
from .solution import Solution, RouteEval


def _objective_groups(objectives):
    """Pack exact lexicographic tiers while keeping integer arithmetic safe.

    Each multiplier exceeds the maximum possible suffix, never a guessed
    priority penalty. Large stable-profile problems split into several groups.
    Bounds cover the expression's entire Boolean domain (not just valid plans),
    which also keeps CP-SAT's overflow checks below int64 limits.
    """
    groups, names, expression, bound, domain = [], [], 0, 0, 0
    for name, term, upper, domain_upper in reversed(objectives):
        combined_domain = domain_upper * (bound + 1) + domain
        if names and combined_domain > 2**53:
            groups.append(('+'.join(reversed(names)), expression))
            names, expression, bound, domain = [], 0, 0, 0
        domain = domain_upper * (bound + 1) + domain
        expression = term * (bound + 1) + expression
        bound = (upper + 1) * (bound + 1) - 1
        names.append(name)
    if names:
        groups.append(('+'.join(reversed(names)), expression))
    return list(reversed(groups))


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
    if hint is not None:
        pool = dict(pool)
        for e, route in hint.routes.items():
            pool[(e, tuple(route.seq))] = route.km
    keys, routes, representatives = [], [], {}
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
            # These columns occupy the same engineer and exactly the same jobs.
            # Their only contribution to a partition is their profile/distance;
            # later finishing alone is not an objective or inter-route resource.
            bucket = (e, frozenset(seq))
            score = route.objective_key(inst)
            old = representatives.get(bucket)
            if old is None:
                representatives[bucket] = (len(keys), score)
                keys.append(key)
                routes.append(route)
            elif score < old[1]:
                index = old[0]
                representatives[bucket] = (index, score)
                keys[index], routes[index] = key, route
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
        hint_keys = {keys[representatives[(e, frozenset(r.seq))][0]]
                     for e, r in hint.routes.items() if (e, frozenset(r.seq)) in representatives}
        for p, key in enumerate(keys):
            model.AddHint(x[p], key in hint_keys)
        for j in range(inst.N):
            model.AddHint(y[j], j in hint.unassigned)
    objectives = [('unassigned_urgent', sum(y[j] for j, t in enumerate(inst.tasks) if t.priority == 3),
                   sum(t.priority == 3 for t in inst.tasks), sum(t.priority == 3 for t in inst.tasks)),
                  ('unassigned_high', sum(y[j] for j, t in enumerate(inst.tasks) if t.priority >= 2),
                   sum(t.priority >= 2 for t in inst.tasks), sum(t.priority >= 2 for t in inst.tasks)),
                  ('unassigned', sum(y), inst.N, inst.N)]
    def route_bound(costs):
        maxima = [0] * inst.E
        for (e, _), c in zip(keys, costs):
            maxima[e] = max(maxima[e], c)
        return sum(maxima)
    values = [r.objective_values(inst) for r in routes]
    for name in inst.objective_tiers:
        costs = [int(value[name]) for value in values]
        expression = sum(c*x[p] for p, c in enumerate(costs))
        upper, domain_upper = route_bound(costs), sum(costs)
        if name == 'response_score':
            expression += 1441 * sum(y[j] for j in inst.target_tasks)
            upper += 1441 * len(inst.target_tasks)
            domain_upper += 1441 * len(inst.target_tasks)
        objectives.append((name, expression, upper, domain_upper))
    stages, values_x, values_y = {}, None, None
    for name, expression in _objective_groups(objectives):
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
        # ObjectiveValue() is a double and can round large exact integer scores.
        value = int(solver.Value(expression))
        stages[name] = dict(value=value, solver_status=solver.StatusName(status))
        values_x, values_y = [solver.Value(v) for v in x], [solver.Value(v) for v in y]
        # Never let a later stage worsen any already found coverage/profile value.
        model.Add(expression <= value)
        model.ClearHints()
        for var, val in zip(x + y, values_x + values_y):
            model.AddHint(var, val)
    result = hint
    if values_x is not None:
        candidate = Solution({keys[p][0]: routes[p] for p, selected in enumerate(values_x) if selected},
                             {j for j, dropped in enumerate(values_y) if dropped})
        if result is None or candidate.objective_key(inst) < result.objective_key(inst):
            result = candidate
    return result, dict(status='ok' if values_x is not None else 'deadline',
                        time=time.perf_counter()-start, columns=len(keys), stages=stages)
