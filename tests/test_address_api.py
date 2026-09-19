import json
import threading
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import pytest
from test_real_replanning import real_session
from test_address_service import Client
from field_router.address_service import AddressService
from field_router.scenario_server import create_server


def test_address_selection_api_and_mode_persist(real_session,tmp_path):
    path=tmp_path/'session.json';service=AddressService(Client())
    server=create_server(real_session,path,port=0,address_service=service,time_limit=.03)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    def post(endpoint,payload):
        with urlopen(Request(url+endpoint,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})) as response:return json.load(response)
    try:
        candidates=post('/api/address/search',{'version':1,'address':'Москва Тестовая улица 7'})['candidates']
        with urlopen(url+'/address-preview?version=1&candidate_id='+candidates[0]['id']) as response:
            assert 'Тестовая'.encode() in response.read()
        state=post('/api/address/confirm',{'version':1,'candidate_id':candidates[0]['id']})
        assert state['version']==2 and state['added_location_id']
        assert json.loads(path.read_text())['version']==2
        with pytest.raises(HTTPError):post('/api/address/confirm',{'version':2,'candidate_id':candidates[0]['id']})
        state=post('/api/mode',{'version':2,'mode':'manual'})
        assert state['mode']=='manual' and state['needs_replanning']
    finally:server.shutdown();server.server_close();thread.join()
