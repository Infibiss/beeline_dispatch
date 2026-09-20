#!/usr/bin/env python3
"""Offline audit of saved unassigned jobs; never rewrites input or solver code."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import platform
import time

from field_router.models import Plan, Scenario, Transport
from field_router.planning import build_route, compatible, make_plan
from field_router.solvers.ortools_solver import OrToolsSolver, objective_key
from field_router.travel import MatrixTravel
from field_router.validation import require_valid


class AliasedStarts:
    """Counterfactual start points reuse an existing request's matrix location."""
    def __init__(self, travel, aliases):
        self.travel, self.aliases = travel, aliases

    def minutes(self, a, b, mode):
        return self.travel.minutes(self.aliases.get(a, a), self.aliases.get(b, b), mode)

    def metres(self, a, b, mode):
        return self.travel.metres(self.aliases.get(a, a), self.aliases.get(b, b), mode)


def summary(plan, scenario):
    missing = {u.request_id for u in plan.unassigned}
    return dict(metrics=plan.metrics.model_dump(), status=plan.status,
                objective_key=list(objective_key(plan, scenario)),
                unassigned=sorted(missing),
                assigned_by_priority={p: sum(r.priority == p and r.id not in missing for r in scenario.requests)
                                      for p in ('urgent', 'high', 'normal')},
                elapsed_seconds=plan.elapsed_seconds)


def shortest_times(travel, mode):
    """Relax time matrix through every node: valid lower bound even if nonmetric."""
    d = [list(row) for row in travel._minutes[mode]]
    for k in range(len(d)):
        for i, row in enumerate(d):
            via = row[k]
            for j in range(len(d)):
                row[j] = min(row[j], via + d[k][j])
    return d


def inspect_request(request, scenario, plan, travel, shortest):
    jobs = {r.id: r for r in scenario.requests}
    sequences = {route.engineer_id: [v.request_id for v in route.visits] for route in plan.routes}
    eligible = [e for e in scenario.engineers if compatible(request, e)]
    candidates, insertions = [], []
    for e in eligible:
        stock_ok = e.stock is None or (e.stock.router >= request.equipment.router and e.stock.tv_box >= request.equipment.tv_box)
        minutes = travel.minutes(e.start_location, request.id, e.transport)
        lower = shortest[e.transport][travel.index[e.start_location]][travel.index[request.id]]
        def allowed(t):
            return stock_ok and any(max(e.shift.start+t,w.start) <= w.end and
                max(e.shift.start+t,w.start)+request.duration_minutes <= e.shift.end for w in request.windows)
        candidates.append(dict(engineer=e.id, transport=e.transport.value, shift=e.shift.model_dump(),
            stock_ok=stock_ok, direct_minutes=minutes, earliest_arrival=e.shift.start+minutes,
            relaxed_minutes=lower, singleton_feasible=build_route(e,[request],travel) is not None,
            relaxed_singleton_feasible=allowed(lower), zero_initial_travel_feasible=allowed(0)))
        seq = sequences.get(e.id, [])
        for position in range(len(seq)+1):
            augmented = seq[:position]+[request.id]+seq[position:]
            route = build_route(e,[jobs[i] for i in augmented],travel)
            if route is not None:
                witness = make_plan(scenario,travel,{**sequences,e.id:augmented},'audit','insertion_witness')
                require_valid(scenario,travel,witness)
                visit = next(v for v in route.visits if v.request_id == request.id)
                insertions.append(dict(engineer=e.id,position=position,start=visit.start,end=visit.end,
                    resulting_objective_key=list(objective_key(witness,scenario))))
    return dict(request=request.model_dump(mode='json'),eligible_engineers=candidates,
                singleton_feasible_engineers=[x['engineer'] for x in candidates if x['singleton_feasible']],
                impossible_under_relaxed_travel=not any(x['relaxed_singleton_feasible'] for x in candidates),
                saved_route_insertions=insertions)


def idle_engineer_skill_witness(scenario, saved, travel):
    """One new skill on an idle existing engineer; keep all saved visits intact."""
    sequences = {r.engineer_id: [v.request_id for v in r.visits] for r in saved.routes}
    missing = {u.request_id for u in saved.unassigned}
    witnesses = []
    for request in scenario.requests:
        if request.id not in missing:
            continue
        for engineer in scenario.engineers:
            if engineer.id in sequences or request.skill in engineer.skills:
                continue
            upgraded = engineer.model_copy(update={'skills': [*engineer.skills, request.skill]})
            if build_route(upgraded, [request], travel) is None:
                continue
            case = scenario.model_copy(update={'engineers': [upgraded if e.id == engineer.id else e for e in scenario.engineers]})
            plan = make_plan(case, travel, {**sequences, engineer.id: [request.id]}, 'audit', 'skill_counterfactual_witness')
            require_valid(case, travel, plan)
            witnesses.append(dict(request_id=request.id, engineer=engineer.id,
                added_skill=request.skill.value, result=summary(plan,case)))
    return witnesses


def audit(path, seconds, counterfactuals=True):
    raw=path.read_bytes(); payload=json.loads(raw)
    scenario=Scenario.model_validate(payload['scenario'])
    saved=next(Plan.model_validate(p) for p in payload['plans'] if p['solver']=='ortools')
    matrix=payload['geography']['matrices']
    travel=MatrixTravel(matrix['locations'],matrix['minutes'],matrix['metres'])
    require_valid(scenario,travel,saved)
    shortest={mode:shortest_times(travel,mode) for mode in Transport}
    missing={u.request_id for u in saved.unassigned}
    result=dict(source=str(path),sha256=hashlib.sha256(raw).hexdigest(),region=scenario.region,
        request_count=len(scenario.requests),engineers=[e.model_dump(mode='json') for e in scenario.engineers],
        travel_modes=payload['geography']['modes'],saved=summary(saved,scenario),
        requests=[inspect_request(r,scenario,saved,travel,shortest) for r in scenario.requests if r.id in missing],experiments={})
    experiments=[('original',scenario,travel)]
    if counterfactuals:
        # Hypothetical homes exactly at each saved route's first client. No new geocoding.
        first={route.engineer_id:route.visits[0].request_id for route in saved.routes}
        aliases={f'audit-home-{e.id}': first.get(e.id,e.start_location) for e in scenario.engineers}
        homes=[e.model_copy(update={'start_location':f'audit-home-{e.id}'}) for e in scenario.engineers]
        experiments.append(('home_at_saved_first_client',scenario.model_copy(update={'engineers':homes}),AliasedStarts(travel,aliases)))
        if scenario.region=='Юго-восток':
            # Isolate car availability in the morning: retain 540-minute shift length.
            early=[e.model_copy(update={'shift':e.shift.model_copy(update={'start':540,'end':1080})})
                   if e.transport==Transport.CAR else e for e in scenario.engineers]
            experiments.append(('existing_cars_09_18',scenario.model_copy(update={'engineers':early}),travel))
            cars=[e.model_copy(update={'transport':Transport.CAR}) for e in scenario.engineers]
            experiments.append(('all_engineers_car_same_shifts',scenario.model_copy(update={'engineers':cars}),travel))
        result['home_start_aliases']=aliases
        result['idle_engineer_skill_witnesses']=idle_engineer_skill_witness(scenario,saved,travel)
    for name, case, provider in experiments:
        print(f'{scenario.region}: {name}, budget={seconds}s',flush=True)
        start=time.perf_counter()
        plan=OrToolsSolver().solve(case,provider,time_limit_seconds=seconds)
        require_valid(case,provider,plan)
        result['experiments'][name]={**summary(plan,case),'wall_seconds':time.perf_counter()-start,
            'requested_budget_seconds':seconds,'diagnostics':plan.diagnostics,
            'saved_missing_recovered':sorted(missing-{u.request_id for u in plan.unassigned}),
            'newly_missing':sorted({u.request_id for u in plan.unassigned}-missing)}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir',type=Path,default=Path('data/demo-road'))
    parser.add_argument('--output',type=Path,default=Path('docs/planning/unassigned-audit.json'))
    parser.add_argument('--seconds',type=float,default=20)
    parser.add_argument('--no-counterfactuals',action='store_true')
    args=parser.parse_args()
    import ortools
    report=dict(python=platform.python_version(),ortools=ortools.__version__,
        method='Fixed matrix offline replay; exhaustive single insertion; bounded OR-Tools search. No optimality claim.',
        regions=[audit(args.input_dir/f'{name}.json',args.seconds,not args.no_counterfactuals)
                 for name in ('Юго-восток','Югоцентр')])
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(args.output)


if __name__=='__main__':
    main()
