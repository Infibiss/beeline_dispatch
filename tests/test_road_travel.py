import pytest
from field_router.geo_models import GeoLocation, GeographyBundle
from field_router.road_travel import build_matrices, bundle_travel


class Client:
    def __init__(self,bad=False): self.calls=[]; self.bad=bad
    def get(self,url):
        self.calls.append(url)
        return {'code':'Ok','durations':[[0,None if self.bad else 61],[180,0]],'distances':[[0,1000],[2000,0]],
            'sources':[{'distance':0},{'distance':2}],'destinations':[{'distance':0},{'distance':2}]}


def bundle():
    return GeographyBundle(scenario_signature='sig',locations={
        'office':GeoLocation(lat=55.7,lon=37.7,quality='manual',source='test'),
        'a':GeoLocation(lat=55.71,lon=37.71,quality='manual',source='test')})


def test_matrices_preserve_asymmetry_round_up_and_identify_public_estimate():
    client=Client()
    result=build_matrices(bundle(),client,allow_public_estimate=True)
    travel=bundle_travel(result)
    assert travel.minutes('office','a','car')==2
    assert travel.minutes('a','office','car')==3
    assert travel.metres('a','office','car')==2000
    assert result.modes['public']['kind']=='estimate'
    assert result.modes['walk']['kind']=='road'
    assert len(client.calls)==3


def test_no_silent_null_fallback_or_unapproved_public_estimate():
    with pytest.raises(ValueError,match='обществен'): build_matrices(bundle(),Client())
    with pytest.raises(ValueError,match='маршрут'): build_matrices(bundle(),Client(bad=True),allow_public_estimate=True)


def test_stale_matrix_coordinates_and_invalid_values_rejected():
    result=build_matrices(bundle(),Client(),allow_public_estimate=True)
    loc=dict(result.locations); loc['a']=loc['a'].model_copy(update={'lon':38.0})
    with pytest.raises(ValueError,match='координат'): bundle_travel(result.model_copy(update={'locations':loc}))
    result.matrices['minutes']['car'][0][1]=-1
    with pytest.raises(ValueError): bundle_travel(result)


def test_malformed_shape_cannot_enter_render_bundle():
    geo=bundle().model_dump(mode='json')
    geo['route_shapes']={'baseline':{'e':{'kind':'road','source':'test','coordinates':{'bad':'shape'}}}}
    with pytest.raises(ValueError): GeographyBundle.model_validate(geo)
