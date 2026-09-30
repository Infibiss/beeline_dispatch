from pathlib import Path
import sys,json,hashlib,math,runpy,time
root=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(root/'src'))
from field_router.models import Scenario
from field_router.geo_models import GeographyBundle
from field_router.road_travel import bundle_travel
from field_router.solvers.selection import get_solver
from field_router.validation import require_valid
from field_router.replanning import ScenarioSession
from field_router.solvers.alns_solver import _instance
h=runpy.run_path(str(root/'scripts/check_resilience.py'))
out=root/'docs/reviews/latest-main-412ad44'
out.mkdir(exist_ok=True,parents=True)
result={'revision':'412ad44','morning_seconds':10,'day_seconds':3,'solver':'hybrid','regions':[]}
files=list((root/'output/local').glob('*.json'))
original_states={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
for path in sorted((root/'data/demo-road').glob('*.json')):
 raw=path.read_bytes(); data=json.loads(raw)
 scenario=Scenario.model_validate(data['scenario']); travel=bundle_travel(GeographyBundle.model_validate(data['geography']))
 assert not _instance(scenario,travel,None,None).depots
 start=time.perf_counter(); plan=get_solver('hybrid').solve(scenario,travel,time_limit_seconds=10)
 require_valid(scenario,travel,plan)
 assert plan.diagnostics['objective_key']<=plan.diagnostics['checkpoint']['objective_key']
 row={'region':scenario.region,'source_sha256':hashlib.sha256(raw).hexdigest(),
      'morning':{'metrics':plan.metrics.model_dump(),'elapsed':time.perf_counter()-start,'diagnostics':plan.diagnostics},'stress':[]}
 print('Morning',scenario.region,plan.metrics.model_dump(),flush=True)
 issued=ScenarioSession(scenario.model_copy(deep=True),plan,data['geography']).scenario
 for label,tf,sf in [('travel+20%',1.2,1),('travel+40%',1.4,1),('service+20%',1,1.2),('travel+40% service+20%',1.4,1.2)]:
  stressed=issued.model_copy(update={'requests':[r.model_copy(update={'duration_minutes':math.ceil(r.duration_minutes*sf)}) for r in issued.requests]})
  st=h['ScaledTravel'](travel,tf)
  revised=get_solver('hybrid').solve(stressed,st,time_limit_seconds=10)
  require_valid(stressed,st,revised)
  row['stress'].append({'name':label,'valid':True,'metrics':revised.metrics.model_dump(),
    'fixed_execution':h['execute_fixed'](scenario,plan,travel,travel_factor=tf,service_factor=sf)})
  print('Stress',scenario.region,label,'pending',revised.metrics.unassigned,flush=True)
 row['dynamic_day']=h['dynamic_day'](scenario,plan,data['geography'],3,solver='hybrid')
 assert raw==path.read_bytes()
 result['regions'].append(row)
 (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
 print('Validated whole day',scenario.region,len(row['dynamic_day']['stages']),'stages',flush=True)
assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in original_states.items())
result['existing_state_files_unchanged']=True
(out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print('ALL REGIONS PASSED; original state files unchanged',flush=True)
