import pytest
from field_router.transit_access import build_access
from field_router.multimodal import TransitNetwork, journey
from test_multimodal import network
from test_road_travel import bundle

class Client:
    def __init__(self,missing=False):self.missing=missing;self.calls=[]
    def get(self,url):
        self.calls.append(url);n=len(url.split('?')[0].split('/')[-1].split(';'))
        return {'code':'Ok','durations':[[0 if i==j else (None if self.missing else 60*(i+1)) for j in range(n)] for i in range(n)],'distances':[[0 if i==j else (None if self.missing else 100*(i+1)) for j in range(n)] for i in range(n)],'sources':[{'distance':0} for _ in range(n)],'destinations':[{'distance':0} for _ in range(n)]}


def test_road_access_is_directional_and_missing_paths_excluded():
    g=bundle();loc={k:v.model_copy(update={'lat':55.,'lon':37.}) for k,v in g.locations.items()};g=g.model_copy(update={'locations':loc})
    n=network();a=build_access(g,n,Client())
    entry=a['points']['55.0000000,37.0000000']
    assert entry['in']['a']['minutes']!=entry['out']['a']['minutes']
    missing=build_access(g,n,Client(True))
    assert missing['points']['55.0000000,37.0000000']['in']=={}


def test_routed_approach_changes_choice_and_no_straight_line_shortcut():
    n=network();p=(55.,37.);q=(55.,37.2)
    n.set_access({'network_signature':n.signature,'points':{'55.0000000,37.0000000':{'in':{'a':{'minutes':150,'metres':10000}},'out':{}},'55.0000000,37.2000000':{'in':{},'out':{'d':{'minutes':150,'metres':10000}}}}})
    assert journey(n,p,q,100,5000)['kind']=='walk'


def test_stale_network_access_rejected():
    with pytest.raises(ValueError,match='устар'):network().set_access({'network_signature':'wrong','points':{}})


def test_routed_transfer_is_not_created_from_proximity_when_road_missing():
    n=network()
    n.set_access({'network_signature':n.signature,'points':{'55.0000000,37.0000000':{'in':{'a':{'minutes':1,'metres':50}},'out':{}},'55.0000000,37.2000000':{'in':{},'out':{'d':{'minutes':1,'metres':50}}}},'transfers':[]})
    assert journey(n,(55.,37.),(55.,37.2),200,20000)['kind']=='walk'


def test_poor_snap_rejected_instead_of_teleporting():
    from field_router.transit_access import table
    class Bad(Client):
        def get(self,url):
            r=super().get(url);r['sources'][0]['distance']=151;return r
    get=table(Bad(),[(55.,37.),(55.,37.1)])
    assert get(0,1) is None and get(1,0) is not None
