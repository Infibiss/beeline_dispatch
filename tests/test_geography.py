import json
import pytest
from field_router.geo_models import GeoLocation, GeographyBundle
from field_router.geography import normalize_address, choose_candidate, attach_geography, scenario_signature
from field_router.geo_http import CachedHttp
from conftest import job, engineer


def feature(house='84', street='улица Юных Ленинцев', lon=37.76, lat=55.69):
    return {'geometry':{'type':'Point','coordinates':[lon,lat]},'properties':{'street':street,'housenumber':house,'city':'Москва','osm_id':123,'osm_type':'W'}}


def test_address_normalization_preserves_building_and_source_matching():
    address='Город Москва, ул.Юных Ленинцев, д. 84 к 2'
    assert '84к2' in normalize_address(address)
    match=choose_candidate(address,[feature('84'),feature('84 к2')])
    assert match.quality=='building_match'
    assert match.lon==37.76
    assert choose_candidate(address,[feature('84')]) is None
    assert choose_candidate(address,[feature('84к2',street='улица Ленина')]) is None


def test_ambiguous_out_of_area_and_non_building_are_not_silently_accepted():
    address='Москва, ул.Юных Ленинцев, д.84'
    assert choose_candidate(address,[feature(),feature(lon=37.78)]) is None
    assert choose_candidate(address,[feature(lon=10,lat=50)]) is None
    assert choose_candidate(address,[feature(house='')]) is None
    with pytest.raises(ValueError): GeoLocation(lat=91,lon=30,quality='manual',source='reviewed')


def test_geography_must_cover_exact_scenario_and_prevent_stale_addresses(make_scenario):
    s=make_scenario([job('a')],[engineer('e')])
    point=GeoLocation(lat=55.7,lon=37.7,quality='manual',source='reviewed')
    geo=GeographyBundle(scenario_signature=scenario_signature(s),locations={'office':point,'a':point})
    result=attach_geography(s,geo)
    assert result.points=={}
    assert result.metadata['geography']=='wgs84'
    with pytest.raises(ValueError): attach_geography(s,geo.model_copy(update={'locations':{'office':point}}))
    changed=s.model_copy(update={'office_address':'different office'})
    with pytest.raises(ValueError): attach_geography(changed,geo)


def test_http_cache_avoids_network_and_caches_empty_results(tmp_path):
    calls=[]
    def fetch(url): calls.append(url); return {'features':[]}
    client=CachedHttp(tmp_path,fetch=fetch,interval=0)
    assert client.get('https://example.org/api?q=abc')=={'features':[]}
    assert client.get('https://example.org/api?q=abc')=={'features':[]}
    assert len(calls)==1
    offline=CachedHttp(tmp_path,offline=True)
    assert offline.get('https://example.org/api?q=abc')=={'features':[]}
    with pytest.raises(ValueError,match='кэш'): offline.get('https://example.org/missing')


def test_normalization_is_idempotent_and_does_not_strip_full_street_words():
    source='Город Москва, улица Гурьянова, дом 84 корпус 2'
    value=normalize_address(source)
    assert value=='москва улица гурьянова 84к2'
    assert normalize_address(value)==value


def test_same_street_and_house_in_another_town_is_not_a_match():
    other=feature()
    other['properties']['city']='Балашиха'
    assert choose_candidate('Город Москва, ул.Юных Ленинцев, д.84',[other]) is None


@pytest.mark.parametrize('address,street',[
    ('Москва, Большая Черкизовская улица, 10','Черкизовская улица'),
    ('Москва, улица Ленина, 10','переулок Ленина'),
])
def test_partial_street_name_or_different_street_type_is_not_a_match(address,street):
    assert choose_candidate(address,[feature('10',street=street)]) is None


def test_lane_abbreviation_matches_full_name():
    assert choose_candidate('Город Москва, пер.Маяковского, д.2',[feature('2',street='переулок Маяковского')]) is not None


def test_unique_building_preferred_over_poi_at_same_address_but_not_two_buildings():
    building=feature();building['properties']['osm_key']='building'
    poi=feature(lat=55.6901);poi['properties']['osm_key']='office';poi['properties']['osm_id']=999
    result=choose_candidate('Москва, ул.Юных Ленинцев, д.84',[poi,building])
    assert result and result.lat==55.69
    other=feature(lon=37.78);other['properties']['osm_key']='building';other['properties']['osm_id']=555
    assert choose_candidate('Москва, ул.Юных Ленинцев, д.84',[building,other]) is None


def test_lettered_building_with_structure_and_corpus_only_are_preserved():
    assert normalize_address('Москва, ул.Красноказарменная, д. 9Б стр. 1').endswith('9бс1')
    assert choose_candidate('Москва, ул.Красноказарменная, д. 9Б стр. 1',[feature('9Б с1',street='Красноказарменная улица')])
    assert choose_candidate('Москва, Самаркандский бульвар квартал 137а, д. к5',[feature('к5',street='Самаркандский бульвар квартал 137а')])


def test_photon_query_expands_corpus_for_reliable_search():
    from field_router.geography import photon_query
    assert photon_query('Москва, ул.Грайвороновская, д.10 к2')=='москва улица грайвороновская 10 корпус 2'
    assert photon_query('Москва, ул.Красноказарменная, д.9Б стр.1').endswith('9б строение 1')

@pytest.mark.parametrize('requested,provider', [('8','6-8'),('3','73 c3'),('8','6/14 c8')])
def test_provider_house_suffix_is_not_a_complete_address_match(requested,provider):
    assert choose_candidate(f'Москва, улица Юных Ленинцев, {requested}',[feature(provider)]) is None


def test_house_ranges_match_only_as_complete_numbers():
    assert choose_candidate('Москва, улица Юных Ленинцев, 6-8',[feature('6-8')])
    assert choose_candidate('Москва, улица Юных Ленинцев, 6-8',[feature('8')]) is None

@pytest.mark.parametrize('address,street',[
    ('Москва, ш.Каширское, д.136','Каширское шоссе'),
    ('Москва, Булатниковский пр-зд., д.6к1','Булатниковский проезд'),
])
def test_road_type_abbreviations_match_without_changing_street_identity(address,street):
    house='136' if 'Каширское' in address else '6к1'
    assert choose_candidate(address,[feature(house,street=street)])


def test_approximate_location_requires_explanation_and_original_address():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        GeoLocation(lat=55.7,lon=37.7,quality='approximate',source='Photon')
    point=GeoLocation(lat=55.7,lon=37.7,quality='approximate',source='Photon',
        original_address='Москва, дом 9Бс1',label='Москва, дом 9',approximation_reason='Согласован соседний дом')
    assert point.original_address!='' and point.approximation_reason
