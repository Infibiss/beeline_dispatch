"""Frequency-based metro + walking model; all non-road durations are estimates.

A deliberately incomplete, versioned network. No timetables, closures or claim
that a straight-line approach is a verified pedestrian route.
"""
from copy import deepcopy
from functools import lru_cache
from heapq import heappop, heappush
from importlib.resources import files
import json
import hashlib
import math


def point_key(point):
    return f"{point[0]:.7f},{point[1]:.7f}"


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
        self.signature=hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        self.access=None
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
        return [(d,k) for d,k in candidates[:6] if d<=2500]

    def set_access(self,access):
        if access.get('network_signature')!=self.signature:raise ValueError('Пешеходные подходы устарели после изменения сети')
        self.access=access;self.cache={}
        if 'transfers' in access:
            self.graph={k:[edge for edge in edges if edge[1][0]['mode']!='transfer'] for k,edges in self.graph.items()}
            for edge in access['transfers']:
                a=self.stations[edge['from']];b=self.stations[edge['to']]
                origin=a['name']+' ('+a['line']+')';destination=b['name']+' ('+b['line']+')'
                self.graph[a['id']].append((b['id'],[leg('transfer',origin,destination,edge['minutes']+4,edge['metres']),leg('wait',destination,destination,3)]))

    def access_paths(self,point,direction):
        if self.access is not None:
            return [(k,v['minutes']+2,v['metres']) for k,v in self.access['points'].get(point_key(point),{}).get(direction,{}).items()]
        return [(k,math.ceil(math.ceil(d*1.3)/1000/4.5*60)+2,math.ceil(d*1.3)) for d,k in self.approaches(point)]

    def reachable(self,point):
        if point in self.cache:return self.cache[point]
        best={};heap=[]
        for key,minutes,walk in self.access_paths(point,'in'):
            s=self.stations[key];label=s['name']+' ('+s['line']+')'
            legs=[leg('walk','Адрес',label,minutes,walk),leg('wait',label,label,3)]
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
    destinations=network.access_paths(destination,'out')
    if not network.access_paths(origin,'in') or not destinations:return None
    best=direct;reachable=network.reachable(origin)
    for key,minutes,walk in destinations:
        if key not in reachable:continue
        s=network.stations[key]
        legs=reachable[key][1]+[leg('walk',s['name']+' ('+s['line']+')','Адрес',minutes,walk)]
        if not any(x['mode']=='metro' for x in legs):continue
        candidate=result('multimodal',legs)
        if candidate['minutes']<best['minutes']:best=candidate
    return best


@lru_cache(maxsize=1)
def bundled_data():
    return json.loads(files('field_router').joinpath('data/metro.json').read_text())


def apply_transit(geo,network=None):
    network=network or TransitNetwork(bundled_data())
    access=geo.metadata.get('transit_access')
    if access:network.set_access(access)
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
    modes['public']={'kind':'estimate','model':'walk-metro-walk-v2' if access else 'walk-metro-walk-v1','network_signature':network.signature,'pedestrian_access':'road-to-station-point' if access else 'distance-estimate','source':network.source,
        'description':('Подходы и наземные пересадки по пешеходной сети OSRM до точек станций, без гарантии конкретного входа. ' if access else 'Подходы оценены по расстоянию. ')+'Поезда и ожидания оценочные; вне покрытия средняя оценка.',
        'assumptions':{'walk_kmh':None if access else 4.5,'walk_detour_factor':None if access else 1.3,'max_access_straight_metres':2500,'nearest_stations':6,'metro_kmh':35,'stop_minutes':1,'wait_minutes':3,'entry_exit_minutes':2,'transfer_radius_metres':700 if access else 350,'max_approach_minutes':45 if access else None,'max_transfer_walk_minutes':20 if access else None},'journey_counts':counts}
    return geo.model_copy(update={'matrices':matrix,'modes':modes,'route_shapes':{}})
