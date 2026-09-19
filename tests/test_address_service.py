from copy import deepcopy
import pytest
from test_real_replanning import real_session
from field_router.address_service import AddressService

class Client:
    def get(self,url):
        if '/api/' in url:
            return {'features':[{'properties':{'city':'Москва','street':'Тестовая улица','housenumber':'7'},'geometry':{'coordinates':[37.73,55.73]}}]}
        n=url.split('?')[0].count(';')+1
        return {'code':'Ok','durations':[[0 if i==j else 600 for j in range(n)] for i in range(n)],'distances':[[0 if i==j else 1000 for j in range(n)] for i in range(n)]}


def test_confirmed_new_point_extends_roads_without_publishing_plan(real_session):
    service=AddressService(Client());session=real_session
    result=service.search(session,{'version':1,'address':'Москва Тестовая улица 7'})
    candidate=result['candidates'][0];before=session.plan
    service.confirm(session,{'version':1,'candidate_id':candidate['id']})
    assert session.version==2 and session.plan==before
    location=next(k for k in session.geo_locations if k.startswith('address:'))
    batch=session.preview({'version':2,'event':{'id':'newaddress','type':'emergency','time':540,'anchor_id':location,'duration_minutes':20}},time_limit=.03)
    assert batch['candidates']
    assert session._travel(session.scenario).minutes('office',location,'car')==10


def test_failed_matrix_fetch_leaves_state_unchanged(real_session):
    class Failing(Client):
        def get(self,url):
            if '/table/' in url:raise ValueError('Сеть недоступна')
            return super().get(url)
    service=AddressService(Failing());session=real_session;before=deepcopy(session.dump())
    result=service.search(session,{'version':1,'address':'Москва Тестовая улица 7'})
    with pytest.raises(ValueError):service.confirm(session,{'version':1,'candidate_id':result['candidates'][0]['id']})
    assert session.dump()==before


def test_unknown_or_stale_candidate_is_rejected(real_session):
    service=AddressService(Client())
    with pytest.raises(ValueError):service.confirm(real_session,{'version':1,'candidate_id':'forged'})
    found=service.search(real_session,{'version':1,'address':'Москва Тестовая улица 7'})
    real_session.record_fact({'version':1,'time':540,'engineer_id':'e1','status':'available','location_id':'office'})
    with pytest.raises(ValueError):service.confirm(real_session,{'version':2,'candidate_id':found['candidates'][0]['id']})
