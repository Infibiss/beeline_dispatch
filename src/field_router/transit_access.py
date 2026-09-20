"""Directional OSRM foot approaches and surface transfers; no invented paths."""
import math
from datetime import datetime, timezone
from .multimodal import distance, point_key

FOOT='https://routing.openstreetmap.de/routed-foot'


def table(client,points):
    coords=';'.join(f'{lon:.7f},{lat:.7f}' for lat,lon in points)
    data=client.get(FOOT+'/table/v1/foot/'+coords+'?annotations=duration,distance')
    n=len(points)
    if data.get('code')!='Ok':raise ValueError('Пешеходный сервис не вернул матрицу')
    for k in ('durations','distances'):
        if len(data.get(k,[]))!=n or any(len(row)!=n for row in data[k]):raise ValueError('Неполная пешеходная матрица')
    def value(i,j):
        for k,index in (('sources',i),('destinations',j)):
            if len(data.get(k,[]))!=n or data[k][index].get('distance',float('inf'))>150:return None
        seconds=data['durations'][i][j];metres=data['distances'][i][j]
        if seconds is None or metres is None:return None
        if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in (seconds,metres)):raise ValueError('Неверная стоимость пешего пути')
        return {'minutes':math.ceil(seconds/60),'metres':math.ceil(metres)}
    return value


def build_access(geo,network,client,previous=None):
    points={};transfers=[]
    previous=previous if previous and previous.get('network_signature')==network.signature else {}
    unique={point_key((p.lat,p.lon)):(p.lat,p.lon) for p in geo.locations.values()}
    pending=[(key,p) for key,p in unique.items() if key not in previous.get('points',{})]
    for key in unique:
        if key in previous.get('points',{}):points[key]=previous['points'][key]
    for offset in range(0,len(pending),8):
        batch=pending[offset:offset+8];near={key:network.approaches(p) for key,p in batch}
        ids=sorted({station for candidates in near.values() for _,station in candidates})
        if not ids:
            points.update({key:{'in':{},'out':{}} for key,_ in batch});continue
        coordinates=[p for _,p in batch]+[(network.stations[k]['lat'],network.stations[k]['lon']) for k in ids]
        get=table(client,coordinates);indices={k:i+len(batch) for i,k in enumerate(ids)}
        for i,(key,p) in enumerate(batch):
            entry={'in':{},'out':{}}
            for _,station in near[key]:
                for direction,a,b in (('in',i,indices[station]),('out',indices[station],i)):
                    value=get(a,b)
                    if value is not None and value['minutes']<=45:entry[direction][station]=value
            points[key]=entry
    if 'transfers' in previous:transfers=previous['transfers']
    else:
        stations=list(network.stations.values());pairs=[]
        for i,a in enumerate(stations):
            for b in stations[i+1:]:
                if a['line']!=b['line'] and distance((a['lat'],a['lon']),(b['lat'],b['lon']))<=700:pairs.append((a['id'],b['id']))
        for offset in range(0,len(pairs),35):
            batch=pairs[offset:offset+35];ids=sorted({k for pair in batch for k in pair});indices={k:i for i,k in enumerate(ids)}
            get=table(client,[(network.stations[k]['lat'],network.stations[k]['lon']) for k in ids])
            for a,b in batch:
                for start,end in ((a,b),(b,a)):
                    value=get(indices[start],indices[end])
                    if value is not None and value['minutes']<=20:transfers.append({'from':start,'to':end,**value})
    return {'network_signature':network.signature,'prepared_at':datetime.now(timezone.utc).isoformat(),'source':FOOT,'method':'road-to-station-point','max_snap_metres':150,'points':points,'transfers':transfers}
