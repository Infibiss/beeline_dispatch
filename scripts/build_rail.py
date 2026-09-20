import json,math
from pathlib import Path
from field_router.multimodal import distance
import sys
raw=json.load(open(sys.argv[1]));stations=[];edges=[]
for line in raw['lines']:
 lid=line['id']
 # Service snapshot is for the demo date 2026-08-17, before Rublyovo line opening.
 if lid in ('11','171'):continue
 entries=sorted(line['stations'],key=lambda s:s['order'])
 if lid=='97':entries=[s for s in entries if s['name'] not in ('Шелепиха','Деловой центр')]
 if lid=='98':
  names=['Нижегородская','Стахановская','Окская','Юго-Восточная','Косино','Улица Дмитриевского','Лухмановская','Некрасовка']
  entries=[next(s for s in entries if s['name']==name) for name in names]
 if lid=='132':
  marina=next(s for s in entries if s['name']=='Марьина Роща')
  entries=[s for s in entries if s['name'] not in ('Марьина Роща','Покровское-Стрешнево','Москва Товарная')]
  entries.insert(next(i for i,s in enumerate(entries) if s['name']=='Рижская'),marina)
 if lid=='136':entries=[s for s in entries if s['name']!='Новопеределкино']
 current=[]
 for i,s in enumerate(entries):
  current.append(dict(id='hh:'+s['id'],name=s['name'],line=line['name'],order=i,lat=s['lat'],lon=s['lng'],wait_minutes=6 if lid in ('131','132','135','136') else 3))
 stations.extend(current)
 pairs=list(zip(current,current[1:]))
 if lid=='4':
  # Kievskaya branch to Delovoy centre and Moscow City; no Alexandrovsky sad -> branch jump.
  pairs=[(a,b) for a,b in pairs if not (a['name']=='Александровский сад')]
  pairs.append((next(s for s in current if s['name']=='Киевская'),next(s for s in current if s['name']=='Деловой центр (Выставочная)')))
 if lid in ('5','95','97'):pairs.append((current[-1],current[0]))
 for a,b in pairs:
  d=distance((a['lat'],a['lon']),(b['lat'],b['lon']))
  for x,y in ((a,b),(b,a)):
   # D3 Levoberezhnaya northbound is skipped during the published closure.
   if lid=='135' and (x['name']=='Левобережная' or y['name']=='Левобережная'):continue
   edges.append(dict(**{'from':x['id'],'to':y['id']},mode='rail' if lid in ('131','132','135','136','95') else 'metro',minutes=max(1,math.ceil(d/1000/35*60+1)),metres=d))
 if lid=='135':
  # Conservative exclusion in both directions until per-direction stop accessibility is represented.
  stations=[s for s in stations if not (s['line']==line['name'] and s['name']=='Левобережная')]
  a=next(s for s in current if s['name']=='Химки');b=next(s for s in current if s['name']=='Ховрино');d=distance((a['lat'],a['lon']),(b['lat'],b['lon']))
  for x,y in ((a,b),(b,a)):edges.append({'from':x['id'],'to':y['id'],'mode':'rail','minutes':math.ceil(d/1000/35*60+1),'metres':d})
data={'source':'HH open metro directory 2026-09-20; corrected service topology for demo 2026-08-17, see METRO-SOURCE.txt','service_date':'2026-08-17','stations':stations,'edges':edges}
Path(sys.argv[2]).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
print(len(stations),len({s['line'] for s in stations}),len(edges))
