"""OSRM road profiles plus a separately authorized public-transport estimate."""
import hashlib
import json
import math
from .geo_models import GeographyBundle
from .models import Transport
from .travel import MatrixTravel

PROFILES={'car':'driving','walk':'foot','bicycle':'bike'}

DEFAULT_ENDPOINTS={
    'car':'https://routing.openstreetmap.de/routed-car',
    'walk':'https://routing.openstreetmap.de/routed-foot',
    'bicycle':'https://routing.openstreetmap.de/routed-bike',
}


def coordinates_signature(locations):
    values={key:[value.lon,value.lat] for key,value in locations.items()}
    return hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()


def _coordinates(locations,ids):
    return ';'.join(f'{locations[key].lon:.7f},{locations[key].lat:.7f}' for key in ids)


def _matrix(values,n,divisor):
    if not isinstance(values,list) or len(values)!=n or any(not isinstance(row,list) or len(row)!=n for row in values):
        raise ValueError('Неполная матрица маршрутов')
    result=[]
    for i,row in enumerate(values):
        output=[]
        for j,value in enumerate(row):
            if type(value) not in (int,float) or not math.isfinite(value) or value<0:
                raise ValueError('Не найден маршрут или недопустимое значение матрицы')
            output.append(0 if i==j else math.ceil(value/divisor))
        result.append(output)
    return result


def build_matrices(geo:GeographyBundle,client,*,allow_public_estimate=False,endpoints=None):
    if not allow_public_estimate:
        raise ValueError('Нужны расписания общественного транспорта или явное разрешение оценки')
    if geo.unresolved or not geo.locations: raise ValueError('Сначала исправьте координаты')
    endpoints=endpoints or DEFAULT_ENDPOINTS
    ids=list(geo.locations)
    if len(ids)>100: raise ValueError('Демо-матрица ограничена 100 точками; используйте локальный провайдер/разбиение')
    coord=_coordinates(geo.locations,ids)
    times,distances,modes={},{},{}
    for mode in ('car','walk','bicycle'):
        url=endpoints[mode].rstrip('/')+'/table/v1/'+PROFILES[mode]+'/'+coord+'?annotations=duration,distance'
        data=client.get(url)
        if data.get('code')!='Ok': raise ValueError(f'Ошибка дорожного сервиса: {data.get("code")}')
        times[mode]=_matrix(data.get('durations'),len(ids),60)
        distances[mode]=_matrix(data.get('distances'),len(ids),1)
        snapped=data.get('sources',[])+data.get('destinations',[])
        if any(p.get('distance',0)>500 for p in snapped):
            raise ValueError('Привязка к дороге дальше 500 м; проверьте координаты')
        modes[mode]={'kind':'road','source':endpoints[mode], 'profile':mode,
            'traffic':'без актуальных пробок','data_version':data.get('data_version','not supplied'),
            'max_snap_metres':max((p.get('distance',0) for p in snapped),default=0)}
    # Do not call a car profile "public transit". It is a declared approximation.
    distances['public']=[list(row) for row in distances['car']]
    times['public']=[[0 if i==j or d==0 else math.ceil(d/1000/18*60+7)
                      for j,d in enumerate(row)] for i,row in enumerate(distances['public'])]
    modes['public']={'kind':'estimate','source':'road distance from car profile; no transit timetable',
                     'speed_kmh':18,'boarding_minutes':7,'description':'Оценка: длина автомобильного пути / 18 км/ч + 7 мин; не маршрут автобуса/метро'}
    return geo.model_copy(update={'modes':modes,'matrices':{'locations':ids,'minutes':times,'metres':distances,
        'coordinates_signature':coordinates_signature(geo.locations)},'route_shapes':{}})


def bundle_travel(geo:GeographyBundle):
    matrix=geo.matrices
    if matrix.get('coordinates_signature')!=coordinates_signature(geo.locations):
        raise ValueError('Матрицы устарели после изменения координат')
    if set(matrix.get('locations',[]))!=set(geo.locations): raise ValueError('Матрицы не покрывают координаты')
    if set(geo.modes)!=set(m.value for m in Transport): raise ValueError('Не указано происхождение каждого вида поездок')
    return MatrixTravel(matrix['locations'],matrix['minutes'],matrix['metres'])


def route_shapes(geo,scenario,plans,client):
    engineers={e.id:e for e in scenario.engineers}
    shapes={}
    for plan in plans:
        shapes[plan.solver]={}
        for route in plan.routes:
            engineer=engineers[route.engineer_id]
            ids=[engineer.start_location]+[v.request_id for v in route.visits]
            mode=engineer.transport.value
            coordinates=[[geo.locations[i].lon,geo.locations[i].lat] for i in ids]
            if mode=='public':
                value={'coordinates':coordinates,'kind':'estimate','source':'Последовательность визитов; прямые линии, не путь общественного транспорта'}
            else:
                source=geo.modes[mode]['source']
                url=source.rstrip('/')+'/route/v1/'+PROFILES[mode]+'/'+_coordinates(geo.locations,ids)+'?overview=full&geometries=geojson&continue_straight=false'
                data=client.get(url)
                if data.get('code')!='Ok' or not data.get('routes'): raise ValueError('Не удалось получить геометрию маршрута')
                coordinates=data['routes'][0]['geometry']['coordinates']
                if len(coordinates)<2 or any(len(p)!=2 or not (-180<=p[0]<=180 and -90<=p[1]<=90) for p in coordinates):
                    raise ValueError('Недопустимая геометрия маршрута')
                value={'coordinates':coordinates,'kind':'road','source':source}
            value['signature']=route_signature(geo,engineer,route)
            shapes[plan.solver][engineer.id]=value
    return geo.model_copy(update={'route_shapes':shapes})


def route_signature(geo,engineer,route):
    value={'coordinates':coordinates_signature(geo.locations),'mode':engineer.transport.value,
           'ids':[engineer.start_location]+[v.request_id for v in route.visits]}
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def current_shapes(geo,scenario,plans):
    engineers={e.id:e for e in scenario.engineers}
    valid={}
    for plan in plans:
        for route in plan.routes:
            shape=geo.route_shapes.get(plan.solver,{}).get(route.engineer_id)
            if shape and shape.get('signature')==route_signature(geo,engineers[route.engineer_id],route):
                valid.setdefault(plan.solver,{})[route.engineer_id]=shape
    return geo.model_copy(update={'route_shapes':valid})
