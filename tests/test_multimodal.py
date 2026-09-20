import pytest
from field_router.multimodal import TransitNetwork, journey, apply_transit
from test_road_travel import bundle, Client
from field_router.road_travel import build_matrices, bundle_travel


def network():
    return TransitNetwork({'source':'test','stations':[
        {'id':'a','name':'A','line':'one','order':0,'lat':55.,'lon':37.},
        {'id':'b','name':'B','line':'one','order':1,'lat':55.,'lon':37.1},
        {'id':'c','name':'C','line':'two','order':0,'lat':55.,'lon':37.101},
        {'id':'d','name':'D','line':'two','order':1,'lat':55.,'lon':37.2}], 'circular_lines':[]})


def test_transfer_wait_and_leg_totals():
    n=network();p=journey(n,(55.,37.),(55.,37.2),200,20000)
    assert p['kind']=='multimodal'
    assert sum(x['mode']=='wait' for x in p['legs'])==2
    assert any(x['mode']=='transfer' for x in p['legs'])
    assert p['minutes']==sum(x['minutes'] for x in p['legs'])
    assert p['metres']==sum(x['metres'] for x in p['legs'])


def test_short_trip_prefers_direct_road_walk():
    p=journey(network(),(55.,37.),(55.,37.001),2,100)
    assert p['kind']=='walk' and p['minutes']==2 and p['metres']==100


def test_outside_coverage_has_explicit_fallback():
    assert journey(network(),(54.,36.),(54.,36.1),150,10000) is None


def test_disconnected_lines_do_not_teleport():
    raw={'source':'test','stations':[dict(id='a',name='A',line='a',order=0,lat=55.,lon=37.),dict(id='b',name='B',line='b',order=0,lat=55.,lon=38.)]}
    assert journey(TransitNetwork(raw),(55.,37.),(55.,38.),300,60000)['kind']=='walk'


def test_duplicate_station_rejected():
    station=dict(id='a',name='A',line='one',order=0,lat=55.,lon=37.)
    with pytest.raises(ValueError,match='Duplicate transit station'): TransitNetwork({'stations':[station,station]})


def test_matrix_integration_preserves_other_modes_and_sums():
    g=build_matrices(bundle(),Client(),allow_public_estimate=True)
    updated=apply_transit(g,network())
    travel=bundle_travel(updated)
    assert updated.matrices['minutes']['car']==g.matrices['minutes']['car']
    for i,a in enumerate(updated.matrices['locations']):
        for j,b in enumerate(updated.matrices['locations']):
            p=updated.matrices['public_journeys'][a][b]
            assert travel.minutes(a,b,'public')==p['minutes']==sum(x['minutes'] for x in p['legs'])
            assert travel.metres(a,b,'public')==p['metres']


def test_bundled_network_has_no_duplicate_orders_and_routes_are_deterministic():
    from field_router.multimodal import bundled_data
    n=TransitNetwork(bundled_data())
    assert len(n.stations)>100
    a=(55.740746,37.65604);b=(55.745113,37.864052)
    p=journey(n,a,b,250,18000)
    assert p['kind']=='multimodal'
    assert p==journey(n,a,b,250,18000)


def test_solver_uses_combined_time_for_window(make_scenario):
    from conftest import job, engineer
    from field_router.models import Transport
    from field_router.solvers.ortools_solver import OrToolsSolver
    from field_router.validation import require_valid
    g=bundle();locations=dict(g.locations)
    locations['office']=locations['office'].model_copy(update={'lat':55.,'lon':37.})
    locations['a']=locations['a'].model_copy(update={'lat':55.,'lon':37.2})
    g=build_matrices(g.model_copy(update={'locations':locations}),Client(),allow_public_estimate=True)
    g.matrices['minutes']['walk']=[[0,200],[200,0]]
    g.matrices['metres']['walk']=[[0,20000],[20000,0]]
    g=apply_transit(g,network());t=bundle_travel(g)
    e=engineer('e').model_copy(update={'transport':Transport.PUBLIC})
    s=make_scenario([job('a',start=e.shift.start,end=e.shift.start+50)],[e])
    plan=OrToolsSolver().solve(s,t,time_limit_seconds=.03)
    require_valid(s,t,plan)
    assert plan.metrics.assigned==1
    assert plan.routes[0].visits[0].travel_minutes==g.matrices['public_journeys']['office']['a']['minutes']<50
