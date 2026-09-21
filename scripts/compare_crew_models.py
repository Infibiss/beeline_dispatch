#!/usr/bin/env python3
"""Compare independent morning roster hypotheses without changing any active day.

Example: python scripts/compare_crew_models.py --seconds 3 --solver hybrid
All comparisons use sufficient morning stock; reserve is reported separately
because adding it cannot improve morning feasibility under that assumption.
"""
import argparse
import hashlib
import json
from pathlib import Path

from field_router.crew import CrewService,coverage,current_rows,presets
from field_router.models import Plan,Scenario
from field_router.replanning import ScenarioSession
from field_router.validation import require_valid


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=float,default=3)
    parser.add_argument('--solver',choices=['alns','hybrid','ortools'],default='hybrid')
    parser.add_argument('--regions',nargs='+',default=['Восток','Юго-восток','Югоцентр'])
    parser.add_argument('--variants',nargs='+',default=['current','homes','skills','reserve','combined','control'])
    parser.add_argument('--output',type=Path,default=Path('output/crew-model-comparison.json'))
    args=parser.parse_args();records=[]
    sources={}
    for name in args.regions:
        payload=json.loads((Path('data/demo-road')/(name+'.json')).read_text())
        sources[name]=digest(payload)
        session=ScenarioSession(Scenario.model_validate(payload['scenario']),Plan.model_validate(payload['plans'][1]),payload['geography'])
        rows=[{**e,'stock_mode':'plan','router':0,'tv_box':0} for e in current_rows(session)]
        variants=presets(session,rows);service=CrewService();cache={}
        for variant in variants:
            if variant['id'] not in args.variants:continue
            # Reuse an identical routing input (e.g. reserve changes only loading)
            # instead of attributing time-limited search noise to a crew change.
            key=digest([{k:v for k,v in e.items() if k not in ('router','tv_box')} if e['stock_mode']=='plan' else e for e in variant['engineers']])
            reused=key in cache
            if reused:candidate=cache[key]
            else:
                result=service.preview(session,{'version':session.version,'engineers':variant['engineers']},time_limit=args.seconds,solver=args.solver)
                candidate=service.selected(session,{'version':session.version,'id':result['id']});cache[key]=candidate
            require_valid(candidate.scenario,candidate._travel(candidate.scenario),candidate.plan)
            loads={r.engineer_id:r.loadout for r in candidate.plan.routes}
            record={'region':name,'variant':variant['id'],'solver':args.solver,'seconds':args.seconds,'seed':0,
                'input_sha256':key,'same_routing_input_reused':reused,'metrics':candidate.plan.metrics.model_dump(),
                'coverage':coverage(session,candidate.plan),'changes':variant['changes'],'engineers':variant['engineers'],
                'issued':{device:sum(getattr(loads[e['id']],device,0) if e['id'] in loads else 0 for e in variant['engineers'])+
                         sum(e[device] for e in variant['engineers']) for device in ('router','tv_box')},
                'unassigned':[u.model_dump() for u in candidate.plan.unassigned],
                'plan':candidate.plan.model_dump(mode='json'),'valid':True}
            records.append(record)
            print(name,variant['id'],record['metrics'],'reused' if reused else '',flush=True)
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps({'sources':sources,'morning_stock':'sufficient warehouse; reserve separate','results':records},ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':main()
