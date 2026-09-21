"""Time-limited OR-Tools search with exact final dispatcher comparison.

Search weights encode a safe lexicographic prefix. If the full objective cannot
fit signed integer costs, only trailing search tiers are omitted; the incumbent
and final candidate are still compared using every exact objective component.
"""
import math
from time import perf_counter

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from ..models import Plan
from ..objectives import objective_key, reference_visits as _reference_visits, response_targets
from ..planning import build_route, compatible, make_plan
from ..scheduling import optimize_waiting
from ..validation import require_valid, validate_plan
from .baseline import BaselineSolver


_SEARCH_LIMIT = 2**60


def _search_weights(tiers):
    """Largest exact lexicographic prefix that fits conservative integer bounds."""
    included = list(tiers)
    while included:
        total, weights = 0, {}
        for name, bound in reversed(included):
            weights[name] = total + 1 if bound else 0
            total += bound * weights[name]
        if total < _SEARCH_LIMIT:
            return weights, [name for name, _ in tiers[len(included):]], total
        included.pop()
    raise ValueError('Input too large for safe integer objective weights')


def _reference_incumbent(scenario, travel, reference):
    """Retain a valid reference or its feasible surviving route subsequences."""
    if reference is None:
        return None
    if not validate_plan(scenario, travel, reference):
        return reference
    jobs = {request.id: request for request in scenario.requests}
    engineers = {engineer.id: engineer for engineer in scenario.engineers}
    sequences, seen = {}, set()
    for route in reference.routes:
        if route.engineer_id not in engineers:
            continue
        kept = sequences.setdefault(route.engineer_id, [])
        for visit in route.visits:
            key = visit.request_id
            if key in jobs and key not in seen and build_route(
                    engineers[route.engineer_id], [jobs[item] for item in kept + [key]], travel) is not None:
                kept.append(key)
                seen.add(key)
    return make_plan(scenario, travel, sequences, 'ortools', 'reference_seed')


class OrToolsSolver:
    def solve(self, scenario, travel, *, time_limit_seconds=5, profile=None,
              reference: Plan | None = None, focus_request_id=None, _priority_seed=True):
        if profile not in (None, 'sla', 'stable', 'cost'):
            raise ValueError('Unknown objective profile')
        targets = response_targets(scenario, focus_request_id)
        prior = _reference_visits(scenario, reference)
        if not math.isfinite(time_limit_seconds) or time_limit_seconds <= 0:
            raise ValueError('time_limit_seconds must be finite and positive')
        started = perf_counter()
        key = lambda plan: objective_key(plan, scenario, profile=profile, reference=reference,
                                          focus_request_id=focus_request_id)
        schedule = lambda plan: optimize_waiting(scenario, plan, profile=profile, reference=reference,
                                                 focus_request_id=focus_request_id)
        baseline = schedule(BaselineSolver().solve(scenario, travel))
        previous = _reference_incumbent(scenario, travel, reference)
        if previous is not None:
            previous = schedule(previous)
            if key(previous) < key(baseline):
                baseline = previous
        jobs, engineers = scenario.requests, scenario.engineers
        if not jobs:
            return require_valid(scenario, travel, baseline.model_copy(update={
                'solver': 'ortools', 'status': 'empty', 'diagnostics': {'profile': profile or 'morning'}}))
        n, v = len(jobs), len(engineers)
        available = {engineer.id for engineer in engineers}
        urgent_ids = {request.id for request in jobs if request.priority == 'urgent'}
        urgent_floor = 0
        if _priority_seed and urgent_ids:
            urgent_scenario = scenario.model_copy(update={'requests': [r for r in jobs if r.id in urgent_ids]})
            urgent_plan = self.solve(urgent_scenario, travel, time_limit_seconds=time_limit_seconds * .3,
                                    _priority_seed=False)
            seed = make_plan(scenario, travel,
                {route.engineer_id: [visit.request_id for visit in route.visits] for route in urgent_plan.routes},
                'baseline', 'emergency_coverage_seed')
            seed = schedule(seed)
            if key(seed) < key(baseline):
                baseline = seed
            urgent_floor = sum(visit.request_id in urgent_ids for route in baseline.routes for visit in route.visits)
            time_limit_seconds *= .7
        matrices = []
        max_distance = 0
        for engineer in engineers:
            locations = [engineer.start_location] + [request.id for request in jobs]
            minutes = [[travel.minutes(a, b, engineer.transport) for b in locations] for a in locations]
            distances = [[travel.metres(a, b, engineer.transport) for b in locations] for a in locations]
            max_distance = max(max_distance, max(max(row) for row in distances))
            matrices.append((minutes, distances))
        bounds = {'missing_urgent': len(urgent_ids),
                  'missing_high_tier': sum(r.priority in ('urgent', 'high') for r in jobs),
                  'unassigned': n, 'response_score': 1441 * len(targets),
                  'avoidable_reassigned': sum(old[0] in available for old in prior.values()),
                  'absolute_start_shift': 1440 * len(prior),
                  'travel_minutes': n * 1441, 'distance_m': n * max_distance,
                  'engineers_used': min(n, v)}
        names = ['missing_urgent', 'missing_high_tier', 'unassigned']
        names += {
            None: ['engineers_used', 'distance_m'],
            'stable': ['avoidable_reassigned', 'absolute_start_shift', 'travel_minutes', 'distance_m', 'engineers_used'],
            'cost': ['travel_minutes', 'distance_m', 'avoidable_reassigned', 'absolute_start_shift', 'engineers_used'],
            'sla': ['response_score', 'avoidable_reassigned', 'absolute_start_shift', 'travel_minutes', 'distance_m', 'engineers_used'],
        }[profile]
        weights, omitted, objective_bound = _search_weights([(name, bounds[name]) for name in names])
        weight = lambda name: weights.get(name, 0)
        manager = pywrapcp.RoutingIndexManager(n + 1, v, 0)
        routing = pywrapcp.RoutingModel(manager)
        time_evaluators = []
        # Open routes: no return travel, but the last service must fit the shift.
        for vehicle, (engineer, (minutes, distances)) in enumerate(zip(engineers, matrices)):
            def transit(i, j, minutes=minutes):
                a, b = manager.IndexToNode(i), manager.IndexToNode(j)
                service = 0 if a == 0 else jobs[a - 1].duration_minutes
                return service + (0 if routing.IsEnd(j) else min(1441, minutes[a][b]))

            def cost(i, j, minutes=minutes, distances=distances, engineer_id=engineer.id):
                if routing.IsEnd(j):
                    return 0
                a, b = manager.IndexToNode(i), manager.IndexToNode(j)
                old = prior.get(jobs[b - 1].id) if b else None
                changed = old is not None and old[0] in available and old[0] != engineer_id
                return (distances[a][b] * weight('distance_m')
                        + min(1441, minutes[a][b]) * weight('travel_minutes')
                        + changed * weight('avoidable_reassigned'))

            time_evaluators.append(routing.RegisterTransitCallback(transit))
            routing.SetArcCostEvaluatorOfVehicle(routing.RegisterTransitCallback(cost), vehicle)
            routing.SetFixedCostOfVehicle(weight('engineers_used'), vehicle)
        if urgent_floor:
            routing.solver().Add(routing.solver().Sum([
                routing.ActiveVar(manager.NodeToIndex(i + 1)) for i, request in enumerate(jobs)
                if request.id in urgent_ids]) >= urgent_floor)
        routing.AddDimensionWithVehicleTransits(time_evaluators, 1440, 1440, False, 'Time')
        time_dimension = routing.GetDimensionOrDie('Time')
        response_dimension = None
        if profile == 'sla' and targets and weight('response_score'):
            # A separate, equal clock lets urgent starts and two-sided reference
            # deviations both contribute; one soft upper bound cannot encode both.
            routing.AddDimensionWithVehicleTransits(time_evaluators, 1440, 1440, False, 'ResponseTime')
            response_dimension = routing.GetDimensionOrDie('ResponseTime')
            for index in range(manager.GetNumberOfIndices()):
                routing.solver().Add(response_dimension.CumulVar(index) == time_dimension.CumulVar(index))
        for vehicle, engineer in enumerate(engineers):
            time_dimension.CumulVar(routing.Start(vehicle)).SetValue(engineer.shift.start)
            time_dimension.CumulVar(routing.End(vehicle)).SetRange(engineer.shift.start, engineer.shift.end)
        for node, request in enumerate(jobs, 1):
            index = manager.NodeToIndex(node)
            allowed = [i for i, engineer in enumerate(engineers) if compatible(request, engineer)]
            routing.VehicleVar(index).SetValues([-1] + allowed)
            windows = sorted(request.windows, key=lambda window: window.start)
            cumul = time_dimension.CumulVar(index)
            cumul.SetRange(windows[0].start, max(window.end for window in windows))
            end = windows[0].end
            for window in windows[1:]:
                if window.start > end + 1:
                    cumul.RemoveInterval(end + 1, window.start - 1)
                end = max(end, window.end)
            if profile is not None and request.id in prior and weight('absolute_start_shift'):
                wanted = prior[request.id][1]
                time_dimension.SetCumulVarSoftLowerBound(index, wanted, weight('absolute_start_shift'))
                time_dimension.SetCumulVarSoftUpperBound(index, wanted, weight('absolute_start_shift'))
            response_cost = 0
            if response_dimension is not None and request.id in targets:
                response_dimension.SetCumulVarSoftUpperBound(index, 0, weight('response_score'))
                response_cost = 1441 * weight('response_score')
            penalty = (weight('unassigned') + response_cost
                       + (weight('missing_high_tier') if request.priority in ('high', 'urgent') else 0)
                       + (weight('missing_urgent') if request.priority == 'urgent' else 0))
            routing.AddDisjunction([index], penalty)
        for device in ('router', 'tv_box'):
            demand = [0] + [getattr(request.equipment, device) for request in jobs]

            def consumption(index, demand=demand):
                return demand[manager.IndexToNode(index)]

            capacities = [sum(demand) if engineer.stock is None else getattr(engineer.stock, device)
                          for engineer in engineers]
            routing.AddDimensionWithVehicleCapacity(routing.RegisterUnaryTransitCallback(consumption),
                                                     0, capacities, True, device)
        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
        params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        params.time_limit.FromMilliseconds(max(1, round(time_limit_seconds * 1000)))
        node_by_id = {request.id: i + 1 for i, request in enumerate(jobs)}
        base_routes = {route.engineer_id: [node_by_id[visit.request_id] for visit in route.visits]
                       for route in baseline.routes}
        routing.CloseModelWithParameters(params)
        warm = routing.ReadAssignmentFromRoutes([base_routes.get(engineer.id, []) for engineer in engineers], True)
        solution = (routing.SolveFromAssignmentWithParameters(warm, params) if warm is not None
                    else routing.SolveWithParameters(params))
        diagnostics = {'routing_status': routing.status(), 'time_limit_seconds': time_limit_seconds,
                       'objective': ', '.join(names), 'profile': profile or 'morning',
                       'focus_request_id': focus_request_id, 'response_targets': sorted(targets),
                       'priority_tiers': {'emergency': ['urgent'], 'connection': ['high'], 'other': ['normal']},
                       'emergency_coverage_floor': urgent_floor, 'search_distance_unit_metres': 1,
                       'search_omitted_tiers': omitted, 'search_objective_bound': objective_bound,
                       'optimality_proven': False, 'warm_start': warm is not None, 'weights': weights}
        if solution is None:
            return require_valid(scenario, travel, baseline.model_copy(update={
                'solver': 'ortools', 'status': 'fallback_baseline_no_solution',
                'elapsed_seconds': perf_counter() - started, 'diagnostics': diagnostics}))
        sequences = {}
        for vehicle, engineer in enumerate(engineers):
            index, sequence = routing.Start(vehicle), []
            while not routing.IsEnd(index):
                node = manager.IndexToNode(index)
                if node:
                    sequence.append(jobs[node - 1].id)
                index = solution.Value(routing.NextVar(index))
            sequences[engineer.id] = sequence
        candidate = make_plan(scenario, travel, sequences, 'ortools', 'feasible_search_completed',
                              perf_counter() - started, diagnostics)
        # Optimize waiting against the complete objective, including tiers that
        # could not fit safe OR-Tools search weights. Fixed routes keep travel,
        # assignment and coverage unchanged; only response and shifts can improve.
        candidate = schedule(candidate)
        require_valid(scenario, travel, candidate)
        if key(candidate) > key(baseline):
            return require_valid(scenario, travel, baseline.model_copy(update={
                'solver': 'ortools', 'status': 'fallback_baseline_better',
                'elapsed_seconds': perf_counter() - started, 'diagnostics': diagnostics}))
        return candidate
