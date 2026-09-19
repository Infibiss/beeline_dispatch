from time import perf_counter

from ..planning import build_route, make_plan
from ..validation import require_valid


class BaselineSolver:
    """PDF baseline: input order, first eligible engineer, append only."""
    def solve(self, scenario, travel, *, time_limit_seconds=5):
        started = perf_counter()
        assigned = {e.id:[] for e in scenario.engineers}
        for request in scenario.requests:
            for engineer in scenario.engineers:
                if build_route(engineer,assigned[engineer.id]+[request],travel) is not None:
                    assigned[engineer.id].append(request)
                    break
        plan = make_plan(scenario,travel,{key:[r.id for r in value] for key,value in assigned.items()},
                         "baseline","sequential_complete",perf_counter()-started,
                         {"policy":"PDF: input order, first feasible engineer, append only"})
        return require_valid(scenario,travel,plan)
