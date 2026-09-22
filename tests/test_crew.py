import pytest
from test_replanning import demo
from field_router.replanning import ScenarioSession
from field_router.crew import CrewService


def setup(make_scenario):
 s,p=demo(make_scenario);session=ScenarioSession(s,p);service=CrewService()
 data=service.settings(session)
 return session,service,{'version':1,'engineers':data['engineers']}


def test_preview_issue_reserve_and_apply_does_not_touch_original(make_scenario):
 s,service,p=setup(make_scenario)
 for e in p['engineers']:e.update(stock_mode='plan',router=2,tv_box=0)
 result=service.preview(s,p,time_limit=.03)
 assert s.version==1 and not s.history
 candidate=service.selected(s,{'version':1,'id':result['id']})
 loads={r.engineer_id:r.loadout.router for r in candidate.plan.routes}
 assert all(e.stock.router==loads.get(e.id,0)+2 for e in candidate.scenario.engineers)
 assert candidate.version==2 and 1 in candidate.views
 assert ScenarioSession.restore(candidate.dump()).plan==candidate.plan


def test_fixed_zero_stock_prevents_device_job(make_scenario):
 s,service,p=setup(make_scenario)
 for e in p['engineers']:e.update(stock_mode='fixed',router=0,tv_box=0)
 result=service.preview(s,p,time_limit=.03)
 candidate=service.selected(s,{'version':1,'id':result['id']})
 assert any(u.request_id=='a' for u in candidate.plan.unassigned)


def test_home_start_uses_selected_point_without_colliding_with_job(make_scenario):
 s,service,p=setup(make_scenario);p['engineers'][0]['start_location']='b'
 r=service.preview(s,p,time_limit=.03);c=service.selected(s,{'version':1,'id':r['id']})
 e=c.scenario.engineers[0]
 assert e.start_location!='b' and c.scenario.points[e.start_location]==s.scenario.points['b']


def test_reject_empty_skills_shift_duplicate_and_stale(make_scenario):
 from copy import deepcopy
 s,service,p=setup(make_scenario)
 for change in ('skills','shift','duplicate','version'):
  bad=deepcopy(p)
  if change=='skills':bad['engineers'][0]['skills']=[]
  if change=='shift':bad['engineers'][0]['shift']={'start':700,'end':600}
  if change=='duplicate':bad['engineers'].append(bad['engineers'][0])
  if change=='version':bad['version']=0
  with pytest.raises(ValueError):service.preview(s,bad,time_limit=.03)


def test_no_morning_reset_after_event(make_scenario):
 s,service,p=setup(make_scenario)
 s.history.append({'event':{'type':'replan'}})
 with pytest.raises(ValueError,match='начала дня'):service.preview(s,p,time_limit=.03)


def test_preview_is_invalid_after_other_change(make_scenario):
 s,service,p=setup(make_scenario);r=service.preview(s,p,time_limit=.03);s.version+=1
 with pytest.raises(ValueError,match='верси'):service.selected(s,{'version':1,'id':r['id']})


def test_home_clone_keeps_road_matrices_and_original_intact():
 import json
 from pathlib import Path
 from field_router.models import Scenario,Plan
 from field_router.road_travel import bundle_travel
 p=json.loads(Path('data/demo-road/Восток.json').read_text())
 s=ScenarioSession(Scenario.model_validate(p['scenario']),Plan.model_validate(p['plans'][1]),p['geography'])
 service=CrewService();rows=service.settings(s)['engineers'];rows[0]['start_location']='35675'
 r=service.preview(s,{'version':1,'engineers':rows},time_limit=.03);c=service.selected(s,{'version':1,'id':r['id']})
 start=c.scenario.engineers[0].start_location
 assert bundle_travel(c.geography).minutes(start,'35675','public')==0
 assert start not in s.geography.locations
 assert c.scenario.engineers[0].start_address==s.catalog['35675'].address


def test_recommended_roster_uses_control_count_and_remote_starts():
 import json
 from pathlib import Path
 from field_router.models import Scenario,Plan
 p=json.loads(Path('data/demo-road/Юго-восток.json').read_text())
 s=ScenarioSession(Scenario.model_validate(p['scenario']),Plan.model_validate(p['plans'][1]),p['geography'])
 service=CrewService();a=service.settings(s)['recommended_engineers'];b=service.settings(s)['recommended_engineers']
 assert a==b and len(a)==12
 starts={s.catalog[e['start_location']].locality for e in a if e['start_location']!='office'}
 assert starts=={'Кашира','Ступино','Домодедово'}
 assert all(e['stock_mode']=='plan' and e['router']==1 for e in a)


def test_all_home_starts_can_apply_restore_and_return_to_office():
 import json
 from pathlib import Path
 from field_router.models import Scenario,Plan
 p=json.loads(Path('data/demo-road/Восток.json').read_text(encoding='utf-8'))
 original=ScenarioSession(Scenario.model_validate(p['scenario']),Plan.model_validate(p['plans'][1]),p['geography'])
 service=CrewService();rows=service.settings(original)['engineers']
 for row in rows:row['start_location']='35675'
 draft=service.preview(original,{'version':1,'engineers':rows},time_limit=.03)
 home=service.selected(original,{'version':1,'id':draft['id']})
 restored=ScenarioSession.restore(home.dump())
 assert all(e.start_location.startswith('home:') for e in restored.initial_engineers.values())
 assert restored.geography.locations['office']==original.geography.locations['office']
 assert any(p['id']=='office' for p in service.settings(restored)['locations'])
 rows=service.settings(restored)['engineers'];rows[0]['start_location']='office'
 draft=service.preview(restored,{'version':restored.version,'engineers':rows},time_limit=.03)
 mixed=service.selected(restored,{'version':restored.version,'id':draft['id']})
 assert mixed.scenario.engineers[0].start_location=='office'
 assert original.version==1


def test_crew_api_persists_only_apply_and_keeps_old_view(make_scenario,tmp_path,monkeypatch):
 import json,threading
 from urllib.request import Request,urlopen
 from urllib.error import HTTPError
 from pathlib import Path
 from field_router.scenario_server import create_server
 s,service,p=setup(make_scenario);path=tmp_path/'state.json'
 server=create_server(s,path,port=0,time_limit=.03);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 url=f'http://127.0.0.1:{server.server_port}'
 def post(route,data):
  with urlopen(Request(url+route,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})) as r:return json.load(r)
 try:
  with urlopen(url+'/crew') as r:assert 'Подготовка дня' in r.read().decode()
  draft=post('/api/crew/preview',p);assert not path.exists()
  with urlopen(url+'/crew-plan?id='+draft['id']+'&version=1') as response:
   html=response.read().decode();assert 'Предпросмотр · не применён' in html and '/crew-report?id=' in html
  original=Path.replace
  def fail(self,target):raise OSError('disk unavailable')
  monkeypatch.setattr(Path,'replace',fail)
  with pytest.raises(HTTPError) as exc:post('/api/crew/apply',{'version':1,'id':draft['id']})
  assert exc.value.code==500
  with urlopen(url+'/api/crew') as r:assert json.load(r)['version']==1
  monkeypatch.setattr(Path,'replace',original)
  result=post('/api/crew/apply',{'version':1,'id':draft['id']});assert result['version']==2
  # Crew replacement must be published consistently to dispatcher, mobile and GPS.
  with urlopen(url+'/api/engineers') as r:mobile=json.load(r)
  with urlopen(url+'/api/gps') as r:gps=json.load(r)
  assert mobile['version']==gps['plan_version']==2
  assert {e['id'] for e in mobile['engineers']}=={e['engineer_id'] for e in gps['rows']}
  for engineer in mobile['engineers']:
   with urlopen(url+'/api/engineer?engineer_id='+engineer['id']) as r:
    assert json.load(r)['version']==2
  saved=json.loads(path.read_text());assert set(saved['views'])=={'1','2'}
  with pytest.raises(HTTPError):post('/api/crew/apply',{'version':1,'id':draft['id']})
 finally:server.shutdown();server.server_close();thread.join()


def test_confirmed_new_home_builds_geography_without_publishing_on_failure():
 import json
 from pathlib import Path
 from field_router.models import Scenario,Plan
 from field_router.geo_models import GeoLocation
 p=json.loads(Path('data/demo-road/Восток.json').read_text())
 s=ScenarioSession(Scenario.model_validate(p['scenario']),Plan.model_validate(p['plans'][1]),p['geography'])
 class Client:
  bad=False
  def get(self,url):
   if self.bad:raise ValueError('provider unavailable')
   n=len(url.split('?')[0].split('/')[-1].split(';'))
   return {'code':'Ok','durations':[[0 if i==j else 600 for j in range(n)] for i in range(n)],'distances':[[0 if i==j else 1000 for j in range(n)] for i in range(n)]}
 class Addresses:
  client=Client()
  def selected(self,session,payload):return GeoLocation(lat=55.75,lon=37.6,quality='manual',source='test',label='Новый дом',original_address='Москва, тестовый дом')
 a=Addresses();service=CrewService(a);rows=service.settings(s)['engineers'];rows[0]['start_candidate_id']='token'
 result=service.preview(s,{'version':1,'engineers':rows},time_limit=.03);c=service.selected(s,{'version':1,'id':result['id']})
 assert c.scenario.engineers[0].start_address=='Москва, тестовый дом'
 assert c.geography.locations[c.scenario.engineers[0].start_location].lat==55.75
 a.client.bad=True
 with pytest.raises(ValueError,match='provider unavailable'):service.preview(s,{'version':1,'engineers':rows},time_limit=.03)
 assert s.version==1 and len(s.geography.locations)==len(p['geography']['locations'])


def road_session(region):
 import json
 from pathlib import Path
 from field_router.models import Scenario,Plan
 p=json.loads((Path('data/demo-road')/(region+'.json')).read_text())
 return ScenarioSession(Scenario.model_validate(p['scenario']),Plan.model_validate(p['plans'][1]),p['geography'])


def test_presets_change_only_the_selected_axis_and_keep_current_roster():
 from field_router.crew import presets
 s=road_session('Юго-восток');data=CrewService().settings(s)
 variants={p['id']:p for p in presets(s,data['engineers'])}
 assert set(variants)=={'current','homes','skills','reserve','combined','control'}
 base={e['id']:e for e in data['engineers']}
 for key,allowed in [('current',set()),('homes',{'start_location'}),('skills',{'skills'}),('reserve',{'stock_mode','router','tv_box'})]:
  assert len(variants[key]['engineers'])==len(base)
  for row in variants[key]['engineers']:
   assert {k for k in row if row[k]!=base[row['id']][k]}<=allowed
 assert variants['homes']['changes']
 assert all(e['stock_mode']=='plan' and e['router']==1 for e in variants['reserve']['engineers'])
 assert len(variants['control']['engineers'])==12
 assert s.version==1 and not s.history


def test_home_model_uses_known_medoid_points_and_is_deterministic():
 from field_router.crew import home_starts
 s=road_session('Юго-восток');rows=CrewService().settings(s)['engineers']
 a=home_starts(s,rows);b=home_starts(s,list(reversed(rows)))
 assert {e['id']:e['start_location'] for e in a}=={e['id']:e['start_location'] for e in b}
 starts=[s.catalog[e['start_location']].locality for e in a if e['start_location']!='office']
 assert set(starts)=={'Кашира','Домодедово','Ступино'}
 assert any(e['start_location']=='office' for e in a)
 assert home_starts(road_session('Восток'),CrewService().settings(road_session('Восток'))['engineers'])==CrewService().settings(road_session('Восток'))['engineers']


def test_crew_preview_reports_actual_resource_and_coverage_changes(make_scenario):
 s,service,p=setup(make_scenario);old=s.initial_engineers[p['engineers'][0]['id']]
 p['engineers'][0].update(stock_mode='plan',router=2)
 result=service.preview(s,p,time_limit=.03)
 assert result['changes']
 assert result['before_coverage']['urgent']['total']==sum(r.priority=='urgent' for r in s.scenario.requests)
 assert sum(x['assigned'] for x in result['after_coverage'].values())==result['after']['assigned']
 assert result['assumptions']
 assert s.initial_engineers[old.id]==old


def test_skill_suggestion_needs_equipment_and_combined_issues_it():
 from field_router.crew import current_rows,skill_gaps,presets
 from field_router.planning import build_route
 from field_router.models import Skill
 s=road_session('Югоцентр');rows=current_rows(s)
 assert skill_gaps(s,rows)==rows  # 78036 needs a router AND a TV box.
 issued=[{**e,'stock_mode':'plan','router':0,'tv_box':0} for e in rows]
 proposal=skill_gaps(s,issued)
 changed=[e for e,old in zip(proposal,issued) if e['skills']!=old['skills']]
 assert len(changed)==1 and Skill.CONNECTION in changed[0]['skills']
 e=s.initial_engineers[changed[0]['id']].model_copy(update={'skills':changed[0]['skills'],'stock':None})
 old=next((r for r in s.plan.routes if r.engineer_id==e.id),None)
 jobs=[s.catalog[v.request_id] for v in old.visits] if old else []
 request=s.catalog['78036']
 assert any(build_route(e,jobs[:i]+[request]+jobs[i:],s._travel(s.scenario)) for i in range(len(jobs)+1))
 combined=next(p for p in presets(s,rows) if p['id']=='combined')
 assert any('skills' in c['fields'] for c in combined['changes'])


def test_homes_do_not_replace_manual_starts_or_grant_skills():
 from field_router.crew import current_rows,home_starts
 s=road_session('Юго-восток');rows=current_rows(s)
 rows[0]['start_location']='62011'
 proposed=home_starts(s,rows)
 assert proposed[0]==rows[0]
 assert all(a['skills']==b['skills'] and a['shift']==b['shift'] and a['transport']==b['transport'] for a,b in zip(rows,proposed))
 assert not any(e['start_location']!='office' and e['start_location']!='62011' and s.catalog[e['start_location']].locality=='Кашира' for e in proposed)


def test_manual_home_same_coordinates_different_provider_is_recognized():
 from field_router.crew import current_rows,home_starts,cloned_geography
 s=road_session('Юго-восток');rows=current_rows(s)
 locations={**s.geography.locations,'manual-home':s.geography.locations['62011'].model_copy(update={'label':'Дом инженера','source':'manual'})}
 s.geography=cloned_geography(s.geography,s.scenario,locations,{'manual-home':'62011'})
 rows[0]['start_location']='manual-home'
 proposed=home_starts(s,rows)
 assert proposed[0]==rows[0]
 assert not any(e['start_location'] in s.catalog and s.catalog[e['start_location']].locality=='Кашира' for e in proposed)


@pytest.mark.parametrize('transport', ['walk', 'bicycle'])
@pytest.mark.parametrize('start', ['office', 'b'])
def test_roster_allows_same_region_city_routes_and_explicit_stock_without_transport_cap(make_scenario, transport, start):
 from field_router.models import Equipment
 from field_router.planning import make_plan
 from field_router.travel import synthetic_travel
 scenario, plan=demo(make_scenario)
 jobs=[r.model_copy(update={'locality':'Москва' if r.id=='a' else 'Кашира', 'equipment':Equipment(router=2)}) for r in scenario.requests]
 scenario=scenario.model_copy(update={'requests':jobs})
 original=ScenarioSession(scenario,make_plan(scenario,synthetic_travel(scenario),{'e1':['a','b']},'demo','valid'))
 service=CrewService();rows=service.settings(original)['engineers'][:1]
 rows[0].update(transport=transport,start_location=start,stock_mode='fixed',router=5,tv_box=3)
 draft=service.preview(original,{'version':1,'engineers':rows},time_limit=.05,solver='alns')
 candidate=service.selected(original,{'version':1,'id':draft['id']})
 assert draft['after']['assigned']==2
 assert {r.locality for r in candidate.scenario.requests}=={'Москва','Кашира'}
 assert candidate.scenario.engineers[0].stock==Equipment(router=5,tv_box=3)
 assert candidate.scenario.engineers[0].region==scenario.region
 assert original.version==1
