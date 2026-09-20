"""Address matching never substitutes a street centre for an unknown building."""
import hashlib
import json
import re
import math
from urllib.parse import urlencode
from datetime import datetime, timezone
from .geo_models import GeoLocation, GeographyBundle
from .models import Scenario


def normalize_address(address):
    value=address.lower().replace('ё','е')
    value=re.sub(r'\b(?:город\b|г(?=\.|\s))\.?\s*','',value)
    value=re.sub(r'\bул(?=\.|\s)\.?\s*','улица ',value)
    value=re.sub(r'\bпр-кт\.?\s*','проспект ',value)
    value=re.sub(r'\bб-р\.?\s*','бульвар ',value)
    value=re.sub(r'\bпер(?=\.|\s)\.?\s*','переулок ',value)
    value=re.sub(r'\bш(?=\.|\s)\.?\s*','шоссе ',value)
    value=re.sub(r'\bпр-зд\.?\s*','проезд ',value)
    value=re.sub(r'\bнаб(?=\.|\s)\.?\s*','набережная ',value)
    value=re.sub(r'\b(?:дом|д)\.?\s*(?=к?\d)','',value)
    value=re.sub(r'(\d+[а-яa-z]?)\s*(?:корпус|корп\.?|к\.?)\s*(?=\d)',r'\1к',value)
    value=re.sub(r'(\d+[а-яa-z]?)\s*(?:строение|стр\.?|с\.?)\s*(?=\d)',r'\1с',value)
    return re.sub(r'\s+',' ',value.replace(',',' ')).strip()


def _house_number(value):
    value=normalize_address(value)
    match=re.fullmatch(r'(?:к\d+|\d+[а-яa-z]?(?:[-/]\d+[а-яa-z]?)?(?:[кс]\d+[а-я]?)*)',value)
    return value if match else None


def _house(address):
    # Only the complete last token may be a house number, never its suffix.
    return _house_number(normalize_address(address).rsplit(' ',1)[-1])


def photon_query(address):
    value=normalize_address(address)
    value=re.sub(r'(\d+[а-яa-z]?)(к|с)(?=\d)',lambda m:m[1]+(' корпус ' if m[2]=='к' else ' строение '),value)
    return value


def _tokens(text):
    return sorted(re.findall(r'[а-яa-z0-9]+',normalize_address(text)))


def choose_candidate(address,features,*,source='Photon / OpenStreetMap'):
    house=_house(address)
    if not house: return None
    address_street=re.sub(r'\s*'+re.escape(house)+r'\s*$','',normalize_address(address))
    candidates={}
    buildings={}
    for item in features:
        props=item.get('properties',{})
        city=normalize_address(str(props.get('city','')))
        if city and city not in normalize_address(address): continue
        if not city: continue
        if _house_number(str(props.get('housenumber','')))!=house: continue
        street=_tokens(props.get('street',''))
        local_street=re.split(r'\b'+re.escape(city)+r'\b',address_street)[-1]
        if not street or street != _tokens(local_street): continue
        coords=item.get('geometry',{}).get('coordinates',[])
        if len(coords)!=2: continue
        lon,lat=coords
        if not (36.5<=lon<=39 and 54.8<=lat<=56.6): continue
        point=GeoLocation(lat=lat,lon=lon,quality='building_match',source=source,
            label=', '.join(str(props[k]) for k in ('city','street','housenumber') if props.get(k)),
            query=photon_query(address),osm_id=f"{props.get('osm_type','')}{props.get('osm_id','')}")
        key=(round(lat,6),round(lon,6))
        candidates[key]=point
        if props.get('osm_key')=='building': buildings[key]=point
    if len(buildings)==1:
        building=next(iter(buildings.values()))
        # A POI inside the same building is not a second address. Distant
        # duplicate-address results stay ambiguous rather than being discarded.
        if all(math.hypot((p.lon-building.lon)*math.cos(math.radians(building.lat)),p.lat-building.lat)*111320<=100 for p in candidates.values()):
            return building
    return next(iter(candidates.values())) if len(candidates)==1 else None


def addresses(scenario):
    result={r.id:r.address for r in scenario.requests}
    for e in scenario.engineers:
        address = scenario.office_address if e.start_location=='office' else e.start_address or result.get(e.start_location)
        if not address: raise ValueError('Для нестандартных стартов требуется отдельный источник адресов')
        if e.start_location in result and result[e.start_location] != address:
            raise ValueError('Противоречивые адреса одной стартовой точки')
        result[e.start_location]=address
    return result


def scenario_signature(scenario):
    return hashlib.sha256(json.dumps({'region':scenario.region,'addresses':addresses(scenario)},sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def geocode_scenario(scenario,client,*,endpoint='https://photon.komoot.io/api/',overrides=None,progress=None):
    supplied=overrides or {}
    source_addresses=addresses(scenario)
    if set(supplied)-set(source_addresses): raise ValueError('Неизвестные ID в ручных координатах')
    locations,unresolved={},{}
    for index,(key,address) in enumerate(source_addresses.items(),1):
        if key in supplied:
            point=GeoLocation.model_validate(supplied[key])
            if point.quality not in ('manual','approximate'): raise ValueError('Ручная поправка должна иметь quality=manual и источник проверки')
            locations[key]=point
        else:
            query=photon_query(address)
            url=endpoint.rstrip('/')+'/?'+urlencode({'q':query,'limit':5,'bbox':'36.5,54.8,39,56.6'})
            result=client.get(url)
            point=choose_candidate(address,result.get('features',[]),source=endpoint+' (OpenStreetMap)')
            if point: locations[key]=point.model_copy(update={"query":query})
            else: unresolved[key]={'address':address,'query':query,'reason':'Точный дом не найден или совпадение неоднозначно','candidates':result.get('features',[])}
        if progress: progress(index,len(source_addresses),key,key in locations)
    return GeographyBundle(scenario_signature=scenario_signature(scenario),locations=locations,unresolved=unresolved,
        metadata={'provider':endpoint,'created_at':datetime.now(timezone.utc).isoformat(),'attribution':'© OpenStreetMap contributors, ODbL','matching':'same street tokens and complete house number; Moscow area'})


def attach_geography(scenario:Scenario,geo:GeographyBundle):
    if scenario_signature(scenario)!=geo.scenario_signature: raise ValueError('География относится к другим адресам/участку')
    if set(geo.locations)!=set(addresses(scenario)) or geo.unresolved:
        raise ValueError('Неполные или непроверенные координаты: исправьте географию до расчета')
    requests=[r.model_copy(update={'provenance':{**r.provenance,'coordinates':geo.locations[r.id].source+'; '+geo.locations[r.id].quality}}) for r in scenario.requests]
    return scenario.model_copy(update={'points':{},'requests':requests,
        'metadata':{**scenario.metadata,'geography':'wgs84','geography_signature':geo.scenario_signature},
        'assumptions':[a for a in scenario.assumptions if not a.startswith(('A12:','I05:'))]+[
            'G01: координаты из OSM или ручной проверки; разрешенные приближения явно помечены',
            'G02: дорожные времена модельные, без актуальных пробок; общественный транспорт без расписаний — только явно разрешенная оценка']})
