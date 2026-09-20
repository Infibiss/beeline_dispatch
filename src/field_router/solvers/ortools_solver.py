"""OR-Tools VRPTW adapter, open routes, service time, compatibility and devices.

Emergency coverage is seeded first and protected by a hard lower bound.
Then objective bounds preserve connection coverage, total coverage,
used engineers, metres. Local search is time-limited, not an optimality proof.
"""
import math
from time import perf_counter

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from ..models import Plan
from ..planning import compatible, make_plan
from ..validation import require_valid
from .baseline import BaselineSolver


def _reference_visits(scenario, reference):
    if reference is None:
        return {}
    jobs = {r.id for r in scenario.requests}
    engineers = {e.id for e in scenario.engineers}
    return {v.request_id: (r.engineer_id, v.start)
            for r in reference.routes if r.engineer_id in engineers
            for v in r.visits if v.request_id in jobs and 0 <= v.start <= 1440}


def objective_key(plan, scenario, *, profile=None, reference=None):
    high_tier = {r.id for r in scenario.requests if r.priority in ("high", "urgent")}
    urgent = {r.id for r in scenario.requests if r.priority == "urgent"}
    coverage = (sum(u.request_id in urgent for u in plan.unassigned), sum(u.request_id in high_tier for u in plan.unassigned), plan.metrics.unassigned)
    resources = (plan.metrics.engineers_used, plan.metrics.distance_m)
    visits = [(r.engineer_id, v) for r in plan.routes for v in r.visits]
    if profile == "sla":
        return coverage + (sum(v.start for _, v in visits if v.request_id in high_tier),) + resources
    if profile == "stable":
        prior = _reference_visits(scenario, reference)
        compared = [(e, v, prior[v.request_id]) for e, v in visits if v.request_id in prior]
        return coverage + (sum(e != old[0] for e, v, old in compared),
                           sum(abs(v.start-old[1]) for e, v, old in compared)) + resources
    return coverage + resources


class OrToolsSolver:
    def solve(self, scenario, travel, *, time_limit_seconds=5, profile=None, reference: Plan | None = None, _priority_seed=True):
        if profile not in (None, "sla", "stable", "cost"):
            raise ValueError("Unknown objective profile")
        prior = _reference_visits(scenario, reference)
        if not math.isfinite(time_limit_seconds) or time_limit_seconds <= 0:
            raise ValueError("time_limit_seconds must be finite and positive")
        started = perf_counter()
        baseline = BaselineSolver().solve(scenario,travel)
        jobs, engineers = scenario.requests, scenario.engineers
        if not jobs:
            return baseline.model_copy(update={"solver":"ortools","status":"empty"})
        n, v = len(jobs), len(engineers)
        urgent_ids = {r.id for r in jobs if r.priority == 'urgent'}
        urgent_floor = 0
        if _priority_seed and urgent_ids:
            urgent_scenario = scenario.model_copy(update={'requests':[r for r in jobs if r.id in urgent_ids]})
            urgent_plan = self.solve(urgent_scenario, travel, time_limit_seconds=time_limit_seconds*.3,
                                    _priority_seed=False)
            seed = make_plan(scenario, travel,
                {r.engineer_id:[x.request_id for x in r.visits] for r in urgent_plan.routes},
                'baseline','emergency_coverage_seed')
            if objective_key(seed,scenario,profile=profile,reference=reference) < objective_key(baseline,scenario,profile=profile,reference=reference):
                baseline = seed
            urgent_floor = sum(v.request_id in urgent_ids for r in baseline.routes for v in r.visits)
            time_limit_seconds *= .7
        manager = pywrapcp.RoutingIndexManager(n+1,v,0)
        routing = pywrapcp.RoutingModel(manager)
        max_edge = 0
        time_evaluators = []
        # Each vehicle has its own start, transport and matrices. End arc only
        # contributes last job service time; no return distance or time is charged.
        for vehicle, engineer in enumerate(engineers):
            locations = [engineer.start_location] + [r.id for r in jobs]
            times = [[travel.minutes(a,b,engineer.transport) for b in locations] for a in locations]
            distances = [[travel.metres(a,b,engineer.transport) for b in locations] for a in locations]
            max_edge = max(max_edge,max(max(row) for row in distances))

            def transit(i,j,times=times):
                a,b = manager.IndexToNode(i),manager.IndexToNode(j)
                service = 0 if a==0 else jobs[a-1].duration_minutes
                return service + (0 if routing.IsEnd(j) else times[a][b])

            def cost(i,j,distances=distances,engineer_id=engineer.id):
                if routing.IsEnd(j):
                    return 0
                node = manager.IndexToNode(j)
                changed = (node > 0 and jobs[node-1].id in prior
                           and prior[jobs[node-1].id][0] != engineer_id)
                return math.ceil(distances[manager.IndexToNode(i)][node] / distance_unit) + reassignment_cost * changed

            time_evaluators.append(routing.RegisterTransitCallback(transit))
            routing.SetArcCostEvaluatorOfVehicle(routing.RegisterTransitCallback(cost),vehicle)

        # A route set has <= n travel legs. Larger conservative bound is harmless.
        # Scale only secondary search distance costs if integer bounds require it.
        # Reported route distances and candidate comparison keep exact metres.
        distance_unit = 1
        while True:
            distance_bound = (n+v)*math.ceil(max_edge/distance_unit)
            engineer_cost = distance_bound+1
            resource_bound = v*engineer_cost+distance_bound
            shift_cost = reassignment_cost = 0
            if profile in ("sla", "stable"):
                shift_cost = resource_bound+1
                resource_bound += n*1440*shift_cost
            if profile == "stable":
                reassignment_cost = resource_bound+1
                resource_bound += n*reassignment_cost
            drop_penalty = resource_bound+1
            high_priority_bonus = n*drop_penalty+resource_bound+1
            if n*(drop_penalty+high_priority_bonus)+resource_bound < 2**60:
                break
            if distance_unit >= max(1,max_edge):
                raise ValueError("Input too large for safe integer objective weights")
            distance_unit *= 10
        if urgent_floor:
            routing.solver().Add(routing.solver().Sum([
                routing.ActiveVar(manager.NodeToIndex(i+1)) for i,r in enumerate(jobs) if r.id in urgent_ids
            ]) >= urgent_floor)
        for vehicle in range(v):
            routing.SetFixedCostOfVehicle(engineer_cost,vehicle)
        routing.AddDimensionWithVehicleTransits(time_evaluators,1440,1440,False,"Time")
        time_dimension = routing.GetDimensionOrDie("Time")
        for vehicle, engineer in enumerate(engineers):
            time_dimension.CumulVar(routing.Start(vehicle)).SetValue(engineer.shift.start)
            time_dimension.CumulVar(routing.End(vehicle)).SetRange(engineer.shift.start,engineer.shift.end)
        for node, request in enumerate(jobs,1):
            index = manager.NodeToIndex(node)
            allowed = [i for i,e in enumerate(engineers) if compatible(request,e)]
            # -1 retains the option to leave the request unassigned, including
            # the case with no eligible vehicle (empty allowed-list APIs differ).
            routing.VehicleVar(index).SetValues([-1]+allowed)
            windows = sorted(request.windows,key=lambda w:w.start)
            cumul = time_dimension.CumulVar(index)
            cumul.SetRange(windows[0].start,max(w.end for w in windows))
            end = windows[0].end
            for window in windows[1:]:
                if window.start > end+1:
                    cumul.RemoveInterval(end+1,window.start-1)
                end = max(end,window.end)
            if profile == "stable" and request.id in prior:
                wanted = prior[request.id][1]
                time_dimension.SetCumulVarSoftLowerBound(index,wanted,shift_cost)
                time_dimension.SetCumulVarSoftUpperBound(index,wanted,shift_cost)
            elif profile == "sla" and request.priority in ("high", "urgent"):
                time_dimension.SetCumulVarSoftUpperBound(index,0,shift_cost)
            routing.AddDisjunction([index],drop_penalty+(high_priority_bonus if request.priority in ("high","urgent") else 0))
        for device in ("router","tv_box"):
            demand = [0]+[getattr(r.equipment,device) for r in jobs]

            def consumption(index,demand=demand):
                return demand[manager.IndexToNode(index)]

            capacities = [sum(demand) if e.stock is None else getattr(e.stock,device) for e in engineers]
            routing.AddDimensionWithVehicleCapacity(routing.RegisterUnaryTransitCallback(consumption),
                                                     0,capacities,True,device)
        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
        params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        params.time_limit.FromMilliseconds(max(1,round(time_limit_seconds*1000)))
        # Start from the known feasible baseline and improve; it also remains an
        # explicit fallback if search returns no solution or a worse candidate.
        node_by_id = {r.id:i+1 for i,r in enumerate(jobs)}
        base_routes = {r.engineer_id:[node_by_id[x.request_id] for x in r.visits] for r in baseline.routes}
        routing.CloseModelWithParameters(params)
        warm = routing.ReadAssignmentFromRoutes([base_routes.get(e.id,[]) for e in engineers],True)
        solution = routing.SolveFromAssignmentWithParameters(warm,params) if warm is not None else routing.SolveWithParameters(params)
        diagnostics = {"routing_status":routing.status(),"time_limit_seconds":time_limit_seconds,
                       "objective": "emergency_unassigned, connection_unassigned, unassigned, " + {
                           "stable":"reassigned, absolute_start_shift, engineers_used, distance_m",
                           "sla":"high_tier_start_sum, engineers_used, distance_m",
                       }.get(profile,"engineers_used, distance_m"),
                       "profile":profile,
                       "priority_tiers":{"emergency":["urgent"],"connection":["high"],"other":["normal"]},
                       "emergency_coverage_floor":urgent_floor,"search_distance_unit_metres":distance_unit,
                       "optimality_proven":False,"warm_start":warm is not None,
                       "weights":{"start_minute":shift_cost,"reassignment":reassignment_cost,"engineer":engineer_cost,"high_priority_bonus":high_priority_bonus,"drop":drop_penalty}}
        if solution is None:
            return require_valid(scenario,travel,baseline.model_copy(update={"solver":"ortools",
                "status":"fallback_baseline_no_solution", "elapsed_seconds":perf_counter()-started,
                "diagnostics":diagnostics}))
        sequences, starts = {}, {}
        for vehicle, engineer in enumerate(engineers):
            index, sequence = routing.Start(vehicle), []
            while not routing.IsEnd(index):
                node = manager.IndexToNode(index)
                if node:
                    sequence.append(jobs[node-1].id)
                    starts[jobs[node-1].id] = solution.Min(time_dimension.CumulVar(index))
                index = solution.Value(routing.NextVar(index))
            sequences[engineer.id] = sequence
        candidate = make_plan(scenario,travel,sequences,"ortools","feasible_search_completed",
                              perf_counter()-started,diagnostics)
        if profile == "stable":
            # Preserve optimized waiting instead of make_plan's earliest schedule.
            rebuilt = []
            engineers_by_id = {e.id:e for e in engineers}
            for route in candidate.routes:
                now = engineers_by_id[route.engineer_id].shift.start
                visits = []
                for visit in route.visits:
                    start = starts[visit.request_id]
                    arrival = now + visit.travel_minutes
                    end = start + (visit.end-visit.start)
                    visits.append(visit.model_copy(update={"arrival":arrival,"start":start,"end":end,
                        "explanation":f"Дорога {visit.travel_minutes} мин, ожидание {start-arrival} мин. "
                                      "Время выбрано целью сохранения утвержденного расписания; допустимость проверена."}))
                    now = end
                rebuilt.append(route.model_copy(update={"visits":visits}))
            candidate = candidate.model_copy(update={"routes":rebuilt})
        require_valid(scenario,travel,candidate)
        if objective_key(candidate,scenario,profile=profile,reference=reference) > objective_key(baseline,scenario,profile=profile,reference=reference):
            return baseline.model_copy(update={"solver":"ortools","status":"fallback_baseline_better",
                "elapsed_seconds":perf_counter()-started,"diagnostics":diagnostics})
        return candidate
