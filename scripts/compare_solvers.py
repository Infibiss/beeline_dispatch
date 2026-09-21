#!/usr/bin/env python3
"""Compare solvers on identical saved inputs; no network, state or input writes."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
from time import perf_counter

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from field_router.models import Scenario, Plan
from field_router.replanning import ScenarioSession
from field_router.geo_models import GeographyBundle
from field_router.geography import attach_geography
from field_router.road_travel import bundle_travel
from field_router.solvers.alns_solver import AlnsSolver
from field_router.solvers.ortools_solver import OrToolsSolver,objective_key
from field_router.validation import require_valid
from field_router.solvers.selection import get_solver


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=float,nargs='+',default=[3,10])
    parser.add_argument('--solvers',nargs='+',choices=['ortools','alns','hybrid'],default=['ortools','alns','hybrid'])
    parser.add_argument('--seeds',type=int,nargs='+',default=[0,1,2])
    parser.add_argument('--stock',choices=['morning','issued'],default='morning',help='morning: исходные запасы; issued: выдача под сохранённый план')
    parser.add_argument('--inputs',type=Path,default=ROOT/'data/demo-road')
    parser.add_argument('--output',type=Path,default=ROOT/'docs/reviews/alns-comparison.json')
    args=parser.parse_args()
    result={'python':platform.python_version(),'platform':platform.platform(),
        'objective':'miss urgent, miss urgent+high, miss total, engineers, metres',
        'conditions':'Identical input/fleet/windows/service/stock/matrices; no external services, no manual tuning. OR-Tools has no seed option; one run per budget. ALNS seed does not make wall-clock search bitwise deterministic. Hybrid spends 40% of its budget on OR-Tools then uses the remainder on ALNS with that incumbent.',
        'stock_mode':args.stock,'inputs':{},'rows':[]}
    # Load modules outside measured rounds. Standalone solvers use the same baseline; hybrid shares its budget.
    from field_router._alns.solver import solve as _warm_import
    for source in sorted(args.inputs.glob('*.json')):
        raw=source.read_bytes(); payload=json.loads(raw)
        s=Scenario.model_validate(payload['scenario'])
        geo=GeographyBundle.model_validate(payload['geography'])
        s=attach_geography(s,geo); travel=bundle_travel(geo)
        if args.stock=='issued':
            published=next(p for p in payload['plans'] if p['solver']=='ortools')
            s=ScenarioSession(s,Plan.model_validate(published),geo.model_dump()).scenario
        result['inputs'][s.region]={'file':str(source.relative_to(ROOT)) if source.is_relative_to(ROOT) else str(source),
            'sha256':hashlib.sha256(raw).hexdigest(),'requests':len(s.requests),'engineers':len(s.engineers),
            'fixed_stock':sum(e.stock is not None for e in s.engineers)}
        for budget in args.seconds:
            for name,seed in [(name,seed) for name in args.solvers for seed in ([None] if name=='ortools' else args.seeds)]:
                started=perf_counter()
                solver=get_solver(name)
                kw={} if seed is None else {'seed':seed}
                plan=solver.solve(s,travel,time_limit_seconds=budget,**kw)
                elapsed=perf_counter()-started
                require_valid(s,travel,plan)
                missing={u.request_id for u in plan.unassigned}
                row={'region':s.region,'solver':name,'seed':seed,'budget_seconds':budget,'elapsed_seconds':round(elapsed,3),
                    'valid':True,'status':plan.status,'objective_key':list(objective_key(plan,s)),
                    'metrics':plan.metrics.model_dump(),'unassigned':sorted(missing),
                    'assigned_by_priority':{p:sum(r.priority==p and r.id not in missing for r in s.requests) for p in ('urgent','high','normal')}}
                result['rows'].append(row)
                print(json.dumps(row,ensure_ascii=False),flush=True)
                args.output.parent.mkdir(parents=True,exist_ok=True)
                args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':main()
