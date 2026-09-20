"""Frequency-based metro + walking model; all non-road durations are estimates.

A deliberately incomplete, versioned network. No timetables, closures or claim
that a straight-line approach is a verified pedestrian route.
"""
from copy import deepcopy
from functools import lru_cache
from heapq import heappop, heappush
from importlib.resources import files
import json
import math


def distance(a,b):
    lat1,lon1,lat2,lon2=map(math.radians,(*a,*b))
    value=math.sin((lat2-lat1)/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return math.ceil(6371000*2*math.asin(min(1,math.sqrt(value))))


def leg(mode,origin,destination,minutes,metres=0):
    return dict(mode=mode,origin=origin,destination=destination,minutes=minutes,metres=metres)


def result(kind,legs):
    return dict(kind=kind,minutes=sum(x['minutes'] for x in legs),metres=sum(x['metres'] for x in legs),legs=legs)


class TransitNetwork:
    def __init__(self,data):
        self.source=data.get('source','unspecified')
        self.stations={};self.graph={};lines={}
        for s in data['stations']:
            if s['id'] in self.stations: raise ValueError('Duplicate transit station')
            if not all(math.isfinite(s.get(k,float('nan'))) for k in ('lat','lon')) or not (-90<=s['lat']<=90 and -180<=s['lon']<=180):
                raise ValueError('Invalid transit coordinates')
            self.stations[s['id']]=s;self.graph[s['id']]=[]
            lines.setdefault(s['line'],[]).append(s)
        for name,stations in lines.items():
            stations.sort(key=lambda s:s['order'])
            if len({s['order'] for s in stations})!=len(stations):raise ValueError('Duplicate line order')
            pairs=list(zip(stations,stations[1:]))
            if name in data.get('circular_lines',[]) and len(stations)>2:pairs.append((stations[-1],stations[0]))
            for a,b in pairs:
                d=distance((a['lat'],a['lon']),(b['lat'],b['lon']))
                self.connect(a,b,'metro',max(1,math.ceil(d/1000/35*60+1)),d)
        values=list(self.stations.values())
        for i,a in enumerate(values):
            for b in values[i+1:]:
                d=distance((a['lat'],a['lon']),(b['lat'],b['lon']))
                if a['line']!=b['line'] and d<=350:
                    self.connect(a,b,'transfer',math.ceil(d*1.3/1000/4.5*60)+2,math.ceil(d*1.3))
        self.cache={}

    def connect(self,a,b,mode,minutes,metres):
        for start,end in ((a,b),(b,a)):
            label=lambda s: s['name']+' ('+s['line']+')'
            legs=[leg(mode,label(start),label(end),minutes,metres)]
            if mode=='transfer':legs.append(leg('wait',label(end),label(end),3))
            self.graph[start['id']].append((end['id'],legs))

    def approaches(self,point):
        candidates=sorted((distance(point,(s['lat'],s['lon'])),key) for key,s in self.stations.items())
        return [(d,k) for d,k in candidates[:3] if d<=2500]

    def reachable(self,point):
        if point in self.cache:return self.cache[point]
        best={};heap=[]
        for d,key in self.approaches(point):
            s=self.stations[key];label=s['name']+' ('+s['line']+')';walk=math.ceil(d*1.3)
            legs=[leg('walk','Адрес',label,math.ceil(walk/1000/4.5*60)+2,walk),leg('wait',label,label,3)]
            cost=sum(x['minutes'] for x in legs)
            best[key]=(cost,legs);heappush(heap,(cost,key))
        while heap:
            cost,key=heappop(heap)
            if cost!=best[key][0]:continue
            for end,legs in self.graph[key]:
                value=cost+sum(x['minutes'] for x in legs)
                if end not in best or value<best[end][0]:
                    best[end]=(value,best[key][1]+legs);heappush(heap,(value,end))
        self.cache[point]=best
        return best


def journey(network,origin,destination,walk_minutes,walk_metres):
    direct=result('walk',[leg('walk','Адрес','Адрес',walk_minutes,walk_metres)])
    if origin==destination:return result('walk',[])
    destinations=network.approaches(destination)
    if not network.approaches(origin) or not destinations:return None
    best=direct;reachable=network.reachable(origin)
    for d,key in destinations:
        if key not in reachable:continue
        walk=math.ceil(d*1.3);s=network.stations[key]
        legs=reachable[key][1]+[leg('walk',s['name']+' ('+s['line']+')','Адрес',math.ceil(walk/1000/4.5*60)+2,walk)]
        if not any(x['mode']=='metro' for x in legs):continue
        candidate=result('multimodal',legs)
        if candidate['minutes']<best['minutes']:best=candidate
    return best


@lru_cache(maxsize=1)
def bundled_data():
    return json.loads(files('field_router').joinpath('data/metro.json').read_text())


def apply_transit(geo,network=None):
    network=network or TransitNetwork(bundled_data())
    matrix=deepcopy(geo.matrices);ids=matrix['locations'];journeys={};counts={}
    for i,a in enumerate(ids):
        journeys[a]={}
        for j,b in enumerate(ids):
            origin=geo.locations[a];destination=geo.locations[b]
            walk_minutes=matrix['minutes']['walk'][i][j];walk_metres=matrix['metres']['walk'][i][j]
            p=journey(network,(origin.lat,origin.lon),(destination.lat,destination.lon),walk_minutes,walk_metres)
            if p is None:
                # Outside this network, preserve explicit average-transit fallback;
                # do not silently pretend there is a known metro/bus connection.
                metres=matrix['metres']['car'][i][j]
                minutes=0 if metres==0 else math.ceil(metres/1000/18*60+7)
                p=result('fallback',[leg('estimate','Адрес','Адрес',minutes,metres)])
                if walk_minutes<=minutes:p=result('walk',[leg('walk','Адрес','Адрес',walk_minutes,walk_metres)])
            journeys[a][b]=p
            matrix['minutes']['public'][i][j]=p['minutes'];matrix['metres']['public'][i][j]=p['metres']
            counts[p['kind']]=counts.get(p['kind'],0)+1
    matrix['public_journeys']=journeys
    modes=deepcopy(geo.modes)
    modes['public']={'kind':'estimate','model':'walk-metro-walk-v1','source':network.source,
        'description':'Пешком + метро + пересадки; интервальная оценка, неполная сеть. Вне покрытия — средняя оценка. Подходы не проверены по пешеходной сети.',
        'assumptions':{'walk_kmh':4.5,'walk_detour_factor':1.3,'max_access_straight_metres':2500,'nearest_stations':3,'metro_kmh':35,'stop_minutes':1,'wait_minutes':3,'entry_exit_minutes':2,'transfer_radius_metres':350},'journey_counts':counts}
    return geo.model_copy(update={'matrices':matrix,'modes':modes,'route_shapes':{}})
