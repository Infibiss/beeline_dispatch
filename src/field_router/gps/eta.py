"""ETA adapter for live positions; bounded IO inside the isolated model worker.

No GPS coordinate leaves this process unless a routing endpoint is explicitly
configured. Public transit reuses the project's versioned network, never a car
route relabelled as a bus. All outputs remain estimates, not traffic forecasts.
"""
from collections import OrderedDict
from copy import deepcopy
import json
import math
from pathlib import Path
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .contracts import distance


def load_routing(path=None):
    config = json.loads(Path(path).read_text(encoding='utf-8')) if path else {}
    if not isinstance(config, dict) or set(config)-{'car', 'walk', 'bicycle'}:
        raise ValueError('GPS routing: допустимы только car, walk, bicycle')
    for value in config.values():
        parsed = urlsplit(value)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or
            (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost','127.0.0.1','::1')))):
            raise ValueError('GPS routing: нужен HTTPS или локальный HTTP OSRM, без credentials/query')
    return config


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # A routing host must not forward private GPS data to a different host.
        return None


def fetch_json(url):
    with build_opener(NoRedirect).open(Request(url, headers={'User-Agent': 'FieldRouter-GPS/1.0'}), timeout=.6) as response:
        body = response.read(262145)
    if len(body)>262144:
        raise ValueError('routing_response_size')
    data = json.loads(body)
    if data.get('code') != 'Ok':
        raise ValueError('routing_unavailable')
    return data


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10000000:
        raise ValueError('routing_cost')
    return value


class RoutingEta:
    def __init__(self, get=fetch_json):
        self.get = get
        self.cache = OrderedDict()
        self.networks = {}

    def network(self, date, minute):
        from ..multimodal import TransitNetwork, bundled_data
        data = bundled_data()
        surface = data.get('service_date') == date and 540 <= minute <= 1320
        if surface not in self.networks:
            subset = deepcopy(data)
            if not surface:
                excluded = {s['id'] for s in subset['stations'] if s.get('surface')}
                subset['stations'] = [s for s in subset['stations'] if s['id'] not in excluded]
                if 'edges' in subset:
                    subset['edges'] = [e for e in subset['edges'] if e['from'] not in excluded and e['to'] not in excluded]
            self.networks[surface] = TransitNetwork(subset)
        return self.networks[surface], surface

    def estimate(self, origin, destination, mode, routing, date, minute):
        from ..multimodal import journey, point_key, result, leg
        p, q = (origin['lat'], origin['lon']), (destination['lat'], destination['lon'])
        key = (round(p[0],4), round(p[1],4), q, mode, tuple(sorted(routing.items())), date, 540 <= minute <= 1320)
        cached = self.cache.get(key)
        if cached and time.monotonic()-cached[0] < 45:
            return deepcopy(cached[1])
        metres = math.ceil(distance(origin, destination)*1.3)
        speed = {'car':25, 'walk':4.5, 'bicycle':14, 'public':4.5}[mode]
        minutes = math.ceil(metres/(speed*1000/60))
        estimate = result('distance_estimate', [leg(mode, 'Текущая точка', 'Адрес', minutes, metres)])
        estimate['description'] = 'Оценка по расстоянию; дорожный сервис не подключён или недоступен. Без актуальных пробок.'
        if mode == 'public':
            network, surface = self.network(date, minute)
            network.access = None
            network.cache.clear()
            routed = False
            if routing.get('walk'):
                try:
                    ids = sorted({k for _, k in network.approaches(p)+network.approaches(q)})
                    coordinates = [p, q]+[(network.stations[k]['lat'],network.stations[k]['lon']) for k in ids]
                    coords = ';'.join(f'{lon:.7f},{lat:.7f}' for lat,lon in coordinates)
                    data = self.get(routing['walk'].rstrip('/')+'/table/v1/foot/'+coords+'?annotations=duration,distance')
                    n = len(coordinates)
                    if any(len(data[k])!=n or any(len(row)!=n for row in data[k]) for k in ('durations','distances')):
                        raise ValueError('routing_matrix')
                    def cost(a,b):
                        if finite(data['sources'][a]['distance'])>150 or finite(data['destinations'][b]['distance'])>150:
                            raise ValueError('routing_snap')
                        return {'minutes': math.ceil(finite(data['durations'][a][b])/60),
                                'metres': math.ceil(finite(data['distances'][a][b]))}
                    direct = cost(0,1)
                    minutes,metres = direct['minutes'],direct['metres']
                    access = {point_key(p): {'in':{},'out':{}}, point_key(q): {'in':{},'out':{}}}
                    for i,k in enumerate(ids,2):
                        for point,index,direction,a,b in ((p,0,'in',0,i),(q,1,'out',i,1)):
                            try:
                                value = cost(a,b)
                                if value['minutes']<=45:
                                    access[point_key(point)][direction][k] = value
                            except (KeyError, TypeError, ValueError):
                                pass
                    network.set_access({'network_signature': network.signature, 'points': access})
                    routed = True
                except Exception:
                    network.access = None
                    network.cache.clear()
            route = journey(network,p,q,minutes,metres)
            estimate = route or result('walk', [leg('walk','Текущая точка','Адрес',minutes,metres)])
            estimate['description'] = ('Комбинированный путь по транспортной сети проекта. ' if estimate['kind']=='multimodal' else 'Пеший путь; подходящего комбинированного маршрута в сети нет. ')
            estimate['description'] += ('Подходы по дорожной сети. ' if routed else 'Пешеходные подходы оценены по расстоянию. ')
            estimate['description'] += 'Пересадки и ожидание оценочные; без актуальных пробок и расписания на минуту отправления.'
            if not surface:
                estimate['description'] += 'Наземные расписания на эту дату/время не подтверждены: автобус и трамвай исключены.'
            estimate['road_access'] = routed
        elif routing.get(mode):
            try:
                profile = {'car':'driving','walk':'foot','bicycle':'bike'}[mode]
                coords = f'{p[1]:.7f},{p[0]:.7f};{q[1]:.7f},{q[0]:.7f}'
                data = self.get(routing[mode].rstrip('/')+'/route/v1/'+profile+'/'+coords+'?overview=false')
                if len(data['waypoints'])!=2 or any(finite(w['distance'])>150 for w in data['waypoints']):
                    raise ValueError('routing_snap')
                route = data['routes'][0]
                minutes,metres = math.ceil(finite(route['duration'])/60),math.ceil(finite(route['distance']))
                estimate = result('road', [leg(mode,'Текущая точка','Адрес',minutes,metres)])
                estimate['description'] = 'Путь по дорожной сети OSRM с текущей GPS-точки; без актуальных пробок.'
            except Exception:
                pass
        self.cache[key] = (time.monotonic(),deepcopy(estimate))
        while len(self.cache)>64:
            self.cache.popitem(last=False)
        return estimate
