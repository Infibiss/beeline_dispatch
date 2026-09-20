from field_router.multimodal import TransitNetwork,journey

def test_explicit_branch_does_not_connect_by_order():
    stations=[dict(id=k,name=k,line='branch',order=i,lat=55,lon=37+i*.05) for i,k in enumerate('abcd')]
    n=TransitNetwork({'stations':stations,'edges':[{'from':'a','to':'b','minutes':3,'metres':1000,'mode':'metro'},{'from':'a','to':'c','minutes':3,'metres':1000,'mode':'metro'}]})
    assert not n.graph['d']
    assert {k for k,_ in n.graph['a']}=={'b','c'}

def test_board_wait_is_once_for_continuous_bus_ride():
    raw={'stations':[dict(id=k,name=k,line='bus',order=i,lat=55,lon=37+i*.05,access=k in ('a','d'),surface=k in ('a','d')) for i,k in enumerate('abcd')],
         'edges':[{'from':'a','to':'b','mode':'wait','minutes':5,'metres':0},{'from':'b','to':'c','mode':'bus','minutes':4,'metres':1000},{'from':'c','to':'d','mode':'bus','minutes':4,'metres':1000}]}
    n=TransitNetwork(raw)
    n.set_access({'network_signature':n.signature,'points':{'55.0000000,37.0000000':{'in':{'a':{'minutes':0,'metres':0}}},'55.0000000,37.1500000':{'out':{'d':{'minutes':0,'metres':0}}}},'transfers':[]})
    p=journey(n,(55,37),(55,37.15),100,10000)
    assert p['kind']=='multimodal'
    assert sum(x['minutes'] for x in p['legs'] if x['mode']=='bus')==8
    assert sum(x['minutes'] for x in p['legs'] if x['mode']=='wait')==5
    assert p['minutes']==13


def test_bundled_topology_excludes_old_nekrasovskaya_extension_and_false_filevskaya_link():
    from field_router.multimodal import bundled_data
    n=TransitNetwork(bundled_data())
    def station(line,name):return next(s for s in n.stations.values() if s['line']==line and s['name']==name)
    nekrasovskaya=[s['name'] for s in n.stations.values() if s['line']=='Некрасовская']
    assert len(nekrasovskaya)==8 and 'Лефортово' not in nekrasovskaya
    garden=station('Филевская','Александровский сад')
    assert all(n.stations[k]['name']!='Деловой центр (Выставочная)' for k,_ in n.graph[garden['id']])
    assert sum(s.get('surface',False) for s in n.stations.values())>1000


def test_new_network_does_not_invent_public_route_outside_coverage():
    from test_road_travel import bundle,Client
    from field_router.road_travel import build_matrices
    from field_router.multimodal import apply_transit
    g=build_matrices(bundle(),Client(),allow_public_estimate=True)
    g.matrices['minutes']['walk']=[[0,40],[40,0]]
    g.matrices['metres']['walk']=[[0,5000],[5000,0]]
    g.matrices['metres']['car']=[[0,1000],[1000,0]]
    n=TransitNetwork({'stations':[],'edges':[],'surface_source':'test'})
    result=apply_transit(g,n)
    assert result.matrices['minutes']['public'][0][1]==40
    assert result.matrices['public_journeys']['office']['a']['kind']=='walk'


def test_nearest_stop_search_reuses_result_for_same_coordinate(monkeypatch):
    import field_router.multimodal as m
    n=m.TransitNetwork({'stations':[dict(id='a',name='a',line='x',order=0,lat=55,lon=37)],'edges':[]})
    n.approaches((55,37))
    def unexpected(*args):raise AssertionError('Repeated geographic scan')
    monkeypatch.setattr(m,'distance',unexpected)
    assert n.approaches((55,37))==[(0,'a')]
