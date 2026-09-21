"""Backend selection and a budget-sharing OR-Tools → ALNS portfolio."""
import math
from time import perf_counter

from ..validation import require_valid
from .alns_solver import AlnsSolver, _reference_seed
from .ortools_solver import OrToolsSolver, objective_key


class HybridSolver:
    def solve(self, scenario, travel, *, time_limit_seconds=5, profile=None, reference=None, seed=0):
        if not math.isfinite(time_limit_seconds) or time_limit_seconds<=0:
            raise ValueError('time_limit_seconds must be finite and positive')
        started=perf_counter()
        first=OrToolsSolver().solve(scenario,travel,time_limit_seconds=.4*time_limit_seconds,
                                    profile=profile,reference=reference)
        require_valid(scenario,travel,first)
        selected=first
        key=lambda plan:objective_key(plan,scenario,profile=profile,reference=reference)
        previous=_reference_seed(scenario,travel,reference)
        if previous is not None and key(previous)<key(selected):
            selected=previous
        diagnostics={'optimality_proven':False,'time_limit_seconds':time_limit_seconds,
                     'profile':profile or 'cost','seed':seed,'ortools':first.diagnostics}
        remaining=time_limit_seconds-(perf_counter()-started)
        status='feasible_search_completed'
        if remaining>0 and scenario.requests:
            try:
                second=AlnsSolver().solve(scenario,travel,time_limit_seconds=remaining,profile=profile,
                                         reference=reference,initial_plan=selected,seed=seed)
                require_valid(scenario,travel,second)
                diagnostics['alns']=second.diagnostics
                if key(second)<key(selected):
                    selected=second
            except (ValueError,RuntimeError,ImportError) as exc:
                # Do not publish failed search output; keep the independent-validated plan.
                diagnostics['alns_error']=f'{type(exc).__name__}: {exc}'
                status='fallback_alns_error'
        diagnostics['selected_backend']=selected.solver
        return require_valid(scenario,travel,selected.model_copy(update={'solver':'hybrid','status':status,
            'elapsed_seconds':perf_counter()-started,'diagnostics':diagnostics}))


def get_solver(name):
    solvers={'ortools':OrToolsSolver,'alns':AlnsSolver,'hybrid':HybridSolver}
    if name not in solvers:
        raise ValueError('Unknown solver; choose ortools, alns or hybrid')
    return solvers[name]()
