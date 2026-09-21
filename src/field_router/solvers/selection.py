"""Backend selection and a budget-sharing OR-Tools → ALNS portfolio."""
import math
from time import perf_counter

from ..validation import require_valid
from .alns_solver import AlnsSolver, _reference_seed
from .ortools_solver import OrToolsSolver, objective_key


class HybridSolver:
    def solve(self, scenario, travel, *, time_limit_seconds=5, profile=None, reference=None,
              seed=0, focus_request_id=None, initial_plan=None):
        """Preserve a short search checkpoint before spending the remaining budget.

        initial_plan carries an actual earlier result on the same scenario; reference
        independently defines the dispatcher promises used by operational objectives.
        """
        if not math.isfinite(time_limit_seconds) or time_limit_seconds<=0:
            raise ValueError('time_limit_seconds must be finite and positive')
        started=perf_counter()
        if initial_plan is not None:
            require_valid(scenario,travel,initial_plan)
        short_budget=min(time_limit_seconds,3.0)
        first=OrToolsSolver().solve(scenario,travel,time_limit_seconds=.4*short_budget,
                                    profile=profile,reference=reference,focus_request_id=focus_request_id)
        require_valid(scenario,travel,first)
        selected=first
        key=lambda plan:objective_key(plan,scenario,profile=profile,reference=reference,focus_request_id=focus_request_id)
        previous=_reference_seed(scenario,travel,reference)
        for incumbent in (initial_plan,previous):
            if incumbent is not None and key(incumbent)<key(selected):
                selected=incumbent
        diagnostics={'optimality_proven':False,'time_limit_seconds':time_limit_seconds,
                     'profile':profile or 'morning','focus_request_id':focus_request_id,'seed':seed,'ortools':first.diagnostics}
        status='feasible_search_completed'
        if initial_plan is not None:
            diagnostics['initial_objective_key']=list(key(initial_plan))

        def improve_until(deadline,stage_seed):
            nonlocal selected,status
            remaining=deadline-(perf_counter()-started)
            stage={'budget_seconds':max(0.0,remaining),'seed':stage_seed,'improved':False}
            if remaining<=0 or not scenario.requests:
                stage['skipped']=True
                return stage
            try:
                second=AlnsSolver().solve(scenario,travel,time_limit_seconds=remaining,profile=profile,
                                         reference=reference,initial_plan=selected,seed=stage_seed,focus_request_id=focus_request_id)
                require_valid(scenario,travel,second)
                stage['alns']=second.diagnostics
                if key(second)<key(selected):
                    selected=second
                    stage['improved']=True
            except (ValueError,RuntimeError,ImportError) as exc:
                # Do not publish failed search output; keep the independent-validated plan.
                stage['error']=f'{type(exc).__name__}: {exc}'
                status='fallback_alns_error'
            return stage

        short_stage=improve_until(short_budget,seed)
        if 'alns' in short_stage:
            diagnostics['alns']=short_stage['alns']
        if 'error' in short_stage:
            diagnostics['alns_error']=short_stage['error']
        diagnostics['checkpoint']={'budget_seconds':short_budget,'elapsed_seconds':perf_counter()-started,
                                   'objective_key':list(key(selected)),
                                   'metrics':selected.metrics.model_dump(mode='json')}
        if time_limit_seconds>short_budget:
            # A fresh ALNS search diversifies from the retained plan, never from scratch.
            diagnostics['continuation']=improve_until(time_limit_seconds,seed+1)
        diagnostics['objective_key']=list(key(selected))
        diagnostics['selected_backend']=selected.solver
        return require_valid(scenario,travel,selected.model_copy(update={'solver':'hybrid','status':status,
            'elapsed_seconds':perf_counter()-started,'diagnostics':diagnostics}))


def get_solver(name):
    solvers={'ortools':OrToolsSolver,'alns':AlnsSolver,'hybrid':HybridSolver}
    if name not in solvers:
        raise ValueError('Unknown solver; choose ortools, alns or hybrid')
    return solvers[name]()
