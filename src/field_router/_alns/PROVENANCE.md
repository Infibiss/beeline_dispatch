# Adapted ALNS kernel

Source: teammate repository [ruslanchainik/ALNS-OptimizationKernel](https://github.com/ruslanchainik/ALNS-OptimizationKernel), commit `f2f1cc0ad61d6591003ba998b5facf493bf3b6b6` (audited 2026-09-21).

The initial `instance.py`, `solution.py`, `alns.py`, `sp.py`, and `solver.py` were copied from `src/alns_kernel`. This internal package adapts that work to field-router's contract. The original repository under `ALNS-OptimizationKernel/` is unchanged. Its API/server, input readers, standalone geography, replanning, baseline, and column-generation modules are not used.

The retained architecture is adaptive large-neighborhood search, cached greedy/regret-2/regret-3 repair, adaptive operator weights, annealing, and a route pool recombined through CP-SAT set partitioning. Route transfers are evaluated against each engineer's actual matrix row and stock. The later upgrade adds a pending-job conflict destroy operator, actual perturbation of repair rankings, bounded intermediate exploration, stagnation restarts, and a fleet-reduction pass. Best incumbents still use the exact lexicographic ordering; rejected feasible plans also contribute useful routes to the bounded pool.

Integration changes:

- Priorities 1/2/3 mean normal/high/urgent. All incumbent comparisons use the tuple `(missing urgent, missing high-or-urgent, missing total, profile terms, engineers, distance)`. CP-SAT packs tiers using conservative upper bounds on each suffix; groups split before integer/double-representation limits. Multipliers are derived from bounds, never fixed priority ratios. For one engineer and the same covered jobs, only the best feasible route order is kept in the restricted partition model (after fixed-prefix filtering).
- Service-start windows are a union of intervals. Forward and backward route labels respect window gaps, service duration, and shift end. Compatibility includes opaque application skill values, required travel mode, and router/TV-box stock; route consumption is cumulative. Unknown stock (`None`) means sufficient morning loading.
- External travel matrices are mandatory for every used mode. No distance/time fallback, direct-start reachability filter, or triangle-inequality assumption is used. Search eligibility uses an optimistic shortest-path closure, ignoring intermediate service/eligibility, so reachable indirect paths cannot be falsely excluded. Removing a job can invalidate a nonmetric route; repair returns affected stops to the pending set. Every transferred/recombined route is checked again.
- Morning/default (`profile=None`) minimizes engineer count then metres after coverage. Since 2026-09-22, daytime `cost` minimizes travel minutes, metres, avoidable owner changes and absolute start deviations; `stable` puts changes/deviations before travel. `sla` first targets the explicitly identified new emergency, or urgent jobs only when no explicit focus exists; an unassigned target scores 1441. All daytime profiles then break remaining ties by engineer count. Departed owners do not count as avoidable changes, but their previous appointment times remain relevant. Exact integer-minute scheduling optimizes response then deviations for fixed route orders, including waiting and disjoint windows. The adapter exports these same starts; insertion, route-pool dominance and CP-SAT use the same objective tiers. Feasibility bounds remain earliest/latest; route copies preserve evaluated bounds and immutable schedule caches. The application also reuses this scheduler through a lightweight fixed-route adapter for OR-Tools results.
- A shared monotonic deadline covers ALNS construction/search, pool processing, CP-SAT stages, and polish. No stage receives a minimum second after budget expiration. Cooperative checks and incumbent preservation make time limits approximate; Python/CP-SAT shutdown and validation have small overhead.
- Runs with at least six seconds of kernel search budget recombine mid-search and use the resulting plan for a second ALNS round. Short runs pay CP-SAT construction overhead only once. Restarts are sequential within the same total budget; there are no hidden parallel ALNS workers.
- Column generation is deliberately excluded. `certificate` is empty, `optimality_proven` is false, and `bounds_status` explains the disabled nonmetric pricing bounds. CP-SAT stage statuses describe the restricted route pool only, not global optimality.

- 2026-09-24, port of the kernel v2 research (upstream `research/README.md`; review in `docs/reviews/alns-v2-port.md`):
  - SISR string removal and greedy insertion with blinks (Christiaens & Vanden Berghe 2020), mixed with the classic operators (`ruins='mixed'`).
  - Uniform operator selection by default (`adaptive=False`), following the Turkeš et al. 2021 meta-analysis and the upstream ablation. The adaptive weights remain available.
  - Absence-based reduction phases inside the shared deadline (`solve(..., absence_phases=True)`):
    - first, fewer unassigned jobs, in every profile;
    - then, one engineer fewer without losing coverage, only for `profile=None`.
  - A phase result is accepted only when the full objective tuple strictly improves.
  - The v2 configuration applies only from `V2_MIN_SECONDS = 2.5` s per call. Shorter calls (the 1 s day scenario, ALNS inside a 3 s hybrid) keep the previous classic adaptive search unchanged, because the phases took time from the main search there.
  - Not ported:
    - upstream multistart processes and PyVRP, because the app keeps one process per request;
    - column generation, which stays disabled here.

The application adapter owns day facts, residual stock/availability, conversions, authoritative objective comparison, independent validation, and fallback. `tests/test_alns_core.py` exercises the internal contract and audited defects.
