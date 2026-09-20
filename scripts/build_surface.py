import csv,io,zipfile,json,collections,statistics,math,hashlib
from datetime import date
from pathlib import Path
import sys
z=zipfile.ZipFile(sys.argv[1])
def rows(name):return csv.DictReader(io.TextIOWrapper(z.open(name+'.txt'),encoding='utf-8-sig'))
day=sys.argv[3] if len(sys.argv)>3 else '20260817'
service_date=date(int(day[:4]),int(day[4:6]),int(day[6:8]))
weekday=('monday','tuesday','wednesday','thursday','friday','saturday','sunday')[service_date.weekday()]
active={r['service_id'] for r in rows('calendar') if r[weekday]=='1' and r['start_date']<=day<=r['end_date']}
routes={r['route_id']:r for r in rows('routes') if r['route_type'] in ('0','3')}
trips={r['trip_id']:r['route_id']+':'+r['direction_id'] for r in rows('trips') if r['service_id'] in active and r['route_id'] in routes}
stops={r['stop_id']:{'id':'gtfs:'+r['stop_id'],'name':r['stop_name'],'lat':float(r['stop_lat']),'lon':float(r['stop_lon'])} for r in rows('stops') if r.get('stop_lat') and r.get('stop_lon')}
def tm(v):
 h,m,s=map(int,v.split(':'));return h*3600+m*60+s
times=collections.defaultdict(list);invalid=0
for r in rows('stop_times'):
 if r['trip_id'] not in trips or r['stop_id'] not in stops:continue
 try:
  a=tm(r['arrival_time']);d=tm(r['departure_time'])
  if a>d:invalid+=1;continue
  times[r['trip_id']].append((int(r['stop_sequence']),r['stop_id'],a,d))
 except (ValueError,KeyError):invalid+=1
print('active trips',len(times),'invalid',invalid,flush=True)
durations=collections.defaultdict(list);departures=collections.defaultdict(set);all_departures=collections.defaultdict(set)
for trip,events in times.items():
 events.sort();route=trips[trip]+':'+hashlib.sha256('|'.join(e[1] for e in events).encode()).hexdigest()[:12]
 for seq,stop,a,d in events:
  all_departures[(route,str(seq)+'~'+stop)].add(d)
  if 9*3600<=d<=22*3600:departures[(route,str(seq)+'~'+stop)].add(d)
 for prev,nxt in zip(events,events[1:]):
  seconds=nxt[2]-prev[3]
  if 0<=seconds<=3600 and 9*3600<=prev[3]<=22*3600 and prev[1]!=nxt[1]:
   durations[(route,str(prev[0])+'~'+prev[1],str(nxt[0])+'~'+nxt[1])].append(seconds)
def percentile(values,p):
 values=sorted(values);return values[max(0,math.ceil(len(values)*p)-1)]
stations=[];edges=[];used=set()
for (route,node),values in departures.items():
 stop=node.split('~',1)[1]
 values=sorted(values)
 # Static day model only includes frequent service spanning the working day.
 if len(values)<12 or values[0]>10*3600 or values[-1]<21*3600:continue
 gaps=[b-a for a,b in zip(values,values[1:])]
 wait=math.ceil(percentile(gaps,.9)/120)
 if wait>20:continue
 rid='ride:'+route+':'+node;s=stops[stop];r=routes[route.split(':')[0]]
 mode='tram' if r['route_type']=='0' else 'bus'
 stations.append({**s,'id':rid,'line':r['route_short_name'],'order':0,'access':False,'ride_mode':mode})
 used.add(stop)
 full=sorted(all_departures[(route,node)])
 # A static day matrix must not invent service before the first/after the last departure.
 if full[0]<=9*3600 and full[-1]>=22*3600:
  gaps=[b-a for a,b in zip(full,full[1:]) if b>=9*3600 and a<=22*3600]
  edges.append({'from':s['id'],'to':rid,'mode':'wait','minutes':math.ceil(max(gaps)/60),'metres':0})
 edges.append({'from':rid,'to':s['id'],'mode':'alight','minutes':0,'metres':0})
ids={s['id'] for s in stations}
from field_router.multimodal import distance
for (route,a,b),values in durations.items():
 x='ride:'+route+':'+a;y='ride:'+route+':'+b
 if x not in ids or y not in ids:continue
 sa=stops[a.split('~',1)[1]];sb=stops[b.split('~',1)[1]]
 mode='tram' if routes[route.split(':')[0]]['route_type']=='0' else 'bus'
 edges.append({'from':x,'to':y,'mode':mode,'minutes':max(1,math.ceil(max(values)/60)),'metres':distance((sa['lat'],sa['lon']),(sb['lat'],sb['lon']))})
stations.extend({**stops[k],'line':'Остановка','order':0,'access':True,'surface':True} for k in sorted(used))
result={'source':'GTFS Moscow, MobilityDatabase mdb-3226, snapshot 2026-06-30','service_date':service_date.isoformat(),'stations':stations,'edges':edges,'routes':len({r.split(':')[0] for r,s in departures if 'ride:'+r+':'+s in ids}),'limitations':'Maximum observed segment time and full maximum headway over the working day; frequent daytime services only, no realtime or exact departure-time routing. Distances between stops are straight estimates.'}
Path(sys.argv[2]).write_text(json.dumps(result,ensure_ascii=False,separators=(',',':'))+'\n')
print('compiled',len(stations),len(edges),result['routes'],flush=True)
