"""Explicit address selection; a failed provider never publishes partial geography."""
from copy import deepcopy
from uuid import uuid4
from urllib.parse import urlencode
from .geography import choose_candidate
from .geo_models import GeoLocation
from .road_travel import build_matrices


class AddressService:
    def __init__(self,client,*,routed_access=False):
        self.client=client
        self.routed_access=routed_access
        self.candidates={}

    def search(self,session,payload):
        if payload.get('version')!=session.version:raise ValueError('Устаревшая версия')
        if not session.geography:raise ValueError('Новый адрес доступен в дорожной версии')
        address=payload.get('address','')
        if not isinstance(address,str) or not 6<=len(address.strip())<=300:raise ValueError('Введите город, улицу и дом (6–300 символов)')
        address=address.strip()
        url='https://photon.komoot.io/api/?'+urlencode({'q':address,'limit':5,'bbox':'36.5,54.8,39,56.6'})
        features=self.client.get(url).get('features',[])
        exact=choose_candidate(address,features)
        result=[];seen=set()
        for feature in features:
            coordinates=feature.get('geometry',{}).get('coordinates',[])
            props=feature.get('properties',{})
            if len(coordinates)!=2:continue
            lon,lat=coordinates
            if not (isinstance(lat,(int,float)) and isinstance(lon,(int,float)) and 54.8<=lat<=56.6 and 36.5<=lon<=39):continue
            if (lat,lon) in seen:continue
            seen.add((lat,lon))
            label=', '.join(str(props[k]) for k in ('city','street','housenumber','name') if props.get(k))
            if not label:continue
            matched=bool(exact and abs(exact.lat-lat)<.000001 and abs(exact.lon-lon)<.000001)
            point=GeoLocation(lat=lat,lon=lon,quality='building_match' if matched else 'approximate',source=url,
                original_address=address,label=label,query=address,
                approximation_reason='' if matched else 'Геокодер предложил эту точку; точное совпадение здания не подтверждено. Выбор подтверждает диспетчер.')
            token=uuid4().hex
            self.candidates[token]={'version':session.version,'point':point}
            result.append({'id':token,**point.model_dump()})
        # Bounded ephemeral selection cache; tokens are never trusted from the browser.
        while len(self.candidates)>100:self.candidates.pop(next(iter(self.candidates)))
        return {'version':session.version,'candidates':result}

    def selected(self,session,payload):
        selected=self.candidates.get(payload.get('candidate_id'))
        if payload.get('version')!=session.version or not selected or selected['version']!=session.version:
            raise ValueError('Выбранная точка не найдена или версия устарела; повторите поиск')
        return selected['point']

    def confirm(self,session,payload):
        point=self.selected(session,payload)
        updated=deepcopy(session)
        key='address:'+uuid4().hex
        geo=updated.geography.model_copy(update={'locations':{**updated.geography.locations,key:point}})
        updated.geography=build_matrices(geo,self.client,allow_public_estimate=True,routed_access=self.routed_access)
        updated.geo_locations[key]=point;updated.aliases[key]=key
        updated.version+=1;updated.batches.clear()
        updated.history.append({'version':updated.version,'event':{'id':key,'type':'address','time':updated.time},'point':point.model_dump()})
        updated._save_view('Добавлен адрес: '+point.original_address)
        session.__dict__.update(updated.__dict__)
        return {**session.state(),'added_location_id':key}
