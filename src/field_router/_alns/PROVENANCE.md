# Adapted ALNS kernel

Source: teammate repository [ruslanchainik/ALNS-OptimizationKernel](https://github.com/ruslanchainik/ALNS-OptimizationKernel), commit `f2f1cc0ad61d6591003ba998b5facf493bf3b6b6` (audited 2026-09-21).

The initial `instance.py`, `solution.py`, `alns.py`, `sp.py`, and `solver.py` were copied from `src/alns_kernel`. This internal package adapts that work to field-router's contract. The original repository under `ALNS-OptimizationKernel/` is unchanged. Its API/server, input readers, standalone geography, replanning, baseline, and column-generation modules are not used.

The retained architecture is adaptive large-neighborhood search with five destroy operators (random, Shaw relatedness, worst distance, route removal, time slice), cached greedy/regret-2/regret-3 repair, adaptive operator weights, distance annealing, and a route pool recombined through CP-SAT set partitioning. Route transfers are evaluated against each engineer's actual matrix row and stock.

Integration changes:

- Priorities 1/2/3 mean normal/high/urgent. All comparisons use the tuple `(missing urgent, missing high-or-urgent, missing total, profile terms, engineers, distance)`. CP-SAT minimizes these in separate stages. No fixed priority ratio remains.
- Service-start windows are a union of intervals. Forward and backward route labels respect window gaps, service duration, and shift end. Compatibility includes opaque application skill values, required travel mode, and router/TV-box stock; route consumption is cumulative. Unknown stock (`None`) means sufficient morning loading.
- External travel matrices are mandatory for every used mode. No distance/time fallback, singleton reachability filter, or triangle-inequality assumption is used. Removing a job can invalidate a nonmetric route; repair returns affected stops to the pending set. Every transferred/recombined route is checked again.
- `cost`/default minimizes engineer count then distance after coverage; `sla` inserts high-tier start sum before resources; `stable` inserts owner changes and absolute start deviations before resources. All schedules are earliest feasible schedules, matching the application reconstruction; waiting specifically to match reference times is not optimized here.
- A shared monotonic deadline covers ALNS construction/search, pool processing, CP-SAT stages, and polish. No stage receives a minimum second after budget expiration. Cooperative checks and incumbent preservation make time limits approximate; Python/CP-SAT shutdown and validation have small overhead.
- Column generation is deliberately excluded. `certificate` is empty, `optimality_proven` is false, and `bounds_status` explains the disabled nonmetric pricing bounds. CP-SAT stage statuses describe the restricted route pool only, not global optimality.

The application adapter owns day facts, residual stock/availability, conversions, authoritative objective comparison, independent validation, and fallback. `tests/test_alns_core.py` exercises the internal contract and audited defects.
