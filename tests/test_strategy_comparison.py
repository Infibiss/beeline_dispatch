from copy import deepcopy
from test_replanning import demo
from field_router.replanning import ScenarioSession


def test_shared_pool_collapses_identical_plans_to_stable(make_scenario):
 s,p=demo(make_scenario);session=ScenarioSession(s,p)
 batch=session.preview({'version':1,'event':{'id':'same','type':'replan','time':540}},solver='ortools',time_limit=.08)
 assert len(batch['candidates'])==3  # API aliases stay compatible.
 assert batch['recommended_candidate_id']=='stable'
 unique=[c for c in batch['candidates'] if not c['duplicate_of']]
 assert len(unique)==1 and unique[0]['id']=='stable'
 assert unique[0]['strategy_ids']==['stable','sla','cost']
 assert all(c['facts']['response_target_count']==0 for c in batch['candidates'])
 assert session.version==1 and session.plan==p


def test_grouping_keeps_different_route_order_and_arrival():
 from field_router.replanning import group_candidates
 # Deliberately equal assignment/start maps, distinct execution order.
 candidates=[]
 for key,ids in [('sla',['b','a']),('stable',['a','b']),('cost',['a','b'])]:
  candidates.append({'id':key,'plan':{'routes':[{'engineer_id':'e','visits':[
   {'request_id':j,'arrival':590,'start':600,'end':600,'travel_minutes':0,'distance_m':0} for j in ids]}],
   'unassigned':[],'metrics':{}},'duplicate_of':None})
 group_candidates(candidates)
 assert candidates[0]['duplicate_of'] is None
 assert candidates[2]['duplicate_of']=='stable'
 assert candidates[1]['strategy_ids']==['stable','cost']
 changed=deepcopy(candidates);changed[2]['plan']['routes'][0]['visits'][0]['arrival']=580
 group_candidates(changed)
 assert changed[2]['duplicate_of'] is None


def test_event_focus_is_explicit_and_not_carried_to_next_preview(make_scenario,monkeypatch):
 from field_router.models import Skill
 from field_router.solvers.ortools_solver import OrToolsSolver
 import field_router.replanning as replanning
 s,p=demo(make_scenario);s=s.model_copy(update={'engineers':[e.model_copy(update={'skills':[Skill.LOCAL,Skill.EMERGENCY]}) for e in s.engineers]})
 session=ScenarioSession(s,p);seen=[]
 class Spy:
  def solve(self,*args,**kwargs):
   seen.append(kwargs.get('focus_request_id'));return OrToolsSolver().solve(*args,**kwargs)
 monkeypatch.setattr(replanning,'get_solver',lambda _:Spy())
 first=session.preview({'version':1,'event':{'id':'new','type':'emergency','time':540,'anchor_id':'a','duration_minutes':10}},time_limit=.08)
 assert seen==['emergency:new']*3
 assert all(c['facts']['response_target_count']==1 for c in first['candidates'])
 seen.clear()
 session.preview({'version':1,'event':{'id':'other','type':'replan','time':540}},time_limit=.08)
 assert seen==[None]*3
