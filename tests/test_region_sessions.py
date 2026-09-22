import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from field_router.replanning import ScenarioSession
from field_router.scenario_server import create_server
from field_router.regions import RegionRegistry
from field_router.address_service import AddressService
from field_router.geo_models import GeoLocation
from test_replanning import demo


def test_region_rosters_facts_gps_and_persistence_are_isolated(make_scenario, tmp_path):
    scenario, plan = demo(make_scenario)
    def region(label):
        return scenario.model_copy(update={'region': label,
            'requests': [r.model_copy(update={'region':label}) for r in scenario.requests],
            'engineers': [e.model_copy(update={'region':label}) for e in scenario.engineers]})
    east = region('Восток')
    source = tmp_path / 'south.json'
    source.write_text(json.dumps({'scenario':region('Юго-восток').model_dump(mode='json'),
                                 'plans':[plan.model_dump(mode='json')]}), encoding='utf-8')
    sources = {'east':{'label':'Восток','state':tmp_path/'east.json'},
               'south-east':{'label':'Юго-восток','comparison':source,'state':tmp_path/'south-state.json'}}
    addresses = AddressService(None)
    addresses.candidates['east-address'] = {'version':1, 'point':GeoLocation(
        lat=55.7,lon=37.7,quality='manual',source='test',label='East point')}

    def start():
        server = create_server(ScenarioSession(east,plan), tmp_path/'east.json', port=0,
                               time_limit=.05, disable_gps=True, region_sources=sources,address_service=addresses)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        return server, thread, f'http://127.0.0.1:{server.server_port}'

    server, thread, base = start()
    def api(path, payload=None):
        with urlopen(Request(base+path,data=json.dumps(payload).encode() if payload is not None else None,
                             headers={'Content-Type':'application/json'}),timeout=10) as response:
            return json.load(response)
    try:
        assert api('/api/regions')['current']=='east'
        assert api('/r/south-east/api/regions')['current']=='south-east'
        with urlopen(base+'/address-preview?candidate_id=east-address&version=1') as response:
            assert response.status==200
        with pytest.raises(HTTPError) as cross_region:
            urlopen(base+'/r/south-east/address-preview?candidate_id=east-address&version=1')
        assert cross_region.value.code==400
        with urlopen(base+'/r/south-east/crew') as response:
            html=response.read().decode()
            assert 'src="/r/south-east/app-context.js"' in html
            assert 'href="/r/south-east/"' in html
        with pytest.raises(HTTPError):api('/r/missing/api/state')
        crew=api('/r/south-east/api/crew')
        rows=crew['engineers'];rows.append({**rows[0],'id':'crew-added','name':'Added'})
        draft=api('/r/south-east/api/crew/preview',{'version':1,'engineers':rows})
        assert len(draft['engineers'])==3
        assert all(e['region']=='Юго-восток' for e in draft['engineers'])
        assert sum(e['assigned'] for e in draft['engineers'])==draft['after']['assigned']
        api('/r/south-east/api/crew/apply',{'version':1,'id':draft['id']})
        assert len(api('/api/engineers')['engineers'])==2
        assert len(api('/r/east/api/engineers')['engineers'])==2
        assert len(api('/r/south-east/api/engineers')['engineers'])==3
        # e1 exists in both areas: the URL must isolate even identical IDs/versions.
        api('/r/south-east/api/engineer/facts',{'version':2,'engineer_id':'e1','status':'available',
            'time':540,'location_id':'office','comment':'South only'})
        assert not api('/api/state')['fact_journal']
        assert api('/api/gps')['plan_version']==1
        assert api('/r/south-east/api/gps')['plan_version']==3
        assert not (tmp_path/'east.json').exists()
        assert (tmp_path/'south-state.json').exists()
    finally:
        server.shutdown();server.server_close();thread.join()
    server,thread,base=start()
    try:
        restored=api('/r/south-east/api/state')
        assert restored['version']==3 and len(restored['engineers'])==3
        assert restored['fact_journal'][0]['comment']=='South only'
        assert api('/api/state')['version']==1
    finally:
        server.shutdown();server.server_close();thread.join()


def test_regions_cannot_share_a_state_file(make_scenario,tmp_path):
    scenario,plan=demo(make_scenario)
    with pytest.raises(ValueError,match='разные файлы'):
        RegionRegistry(ScenarioSession(scenario,plan),tmp_path/'same.json',lambda *args:None,
            {'other':{'label':'other','state':tmp_path/'same.json'}})


def test_explicit_directory_preserves_existing_region_keys_and_state_files(tmp_path):
    from field_router.regions import discover_regions
    from pathlib import Path
    source=Path('data/demo-road/Восток.json')
    state=tmp_path/'mobile-east.json'
    automatic=discover_regions(source,state)
    explicit=discover_regions(source,state,source.parent)
    assert automatic==explicit
    assert set(explicit)=={'east','south-east','south-center'}


def test_explicit_directory_accepts_additional_regions_without_cross_loading(make_scenario,tmp_path):
    from field_router.regions import discover_regions
    scenario,plan=demo(make_scenario)
    def save(path,label):
        data=scenario.model_dump(mode='json');data['region']=label
        for row in data['requests']+data['engineers']:row['region']=label
        path.write_text(json.dumps({'scenario':data,'plans':[plan.model_dump(mode='json')]}),encoding='utf-8')
    primary=tmp_path/'primary.json';save(primary,'Primary')
    other=tmp_path/'other';other.mkdir();save(other/'custom.json','Custom')
    sources=discover_regions(primary,tmp_path/'state.json',other)
    from field_router.regions import load_session
    registry=RegionRegistry(load_session(primary,tmp_path/'state.json'),tmp_path/'state.json',
                            lambda session,path,primary:{'session':session,'path':path},sources)
    key=next(key for key,spec in sources.items() if spec['label']=='Custom')
    first=registry.resolve('/r/'+key+'/api/state')[-1]
    second=registry.resolve('/regions/'+key+'/api/state')[-1]
    assert first is second and first['session'].scenario.region=='Custom'
    assert registry.resolve('/api/state')[-1]['session'].scenario.region=='Primary'
