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
