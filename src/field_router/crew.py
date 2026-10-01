"""Versioned, preview-first morning roster changes. Never resets an active day."""
from copy import deepcopy
from uuid import uuid4
import math
from typing import Literal
from pydantic import Field
from .models import Model, Engineer, Equipment, Scenario, Skill, Transport, TimeWindow
from .replanning import ScenarioSession
from .geography import scenario_signature
from .road_travel import build_matrices, bundle_travel, coordinates_signature
from .travel import synthetic_travel
from .solvers.selection import get_solver
from .planning import build_route

CONTROL_COUNTS={'Восток':12,'Юго-восток':12,'Югоцентр':11}


def current_rows(session):
    saved=session.scenario.metadata.get('crew_inputs')
    if saved is not None:
        rows=deepcopy(saved)
        for row in rows:
            worker=session.initial_engineers.get(row['id'])
            row['off_duty']=worker.off_duty if worker is not None else row.get('off_duty',False)
        return rows
    return [dict(id=e.id,name=e.name,shift=e.shift.model_dump(),skills=e.skills,transport=e.transport,
        off_duty=e.off_duty,vehicle_id=e.vehicle_id,start_location=e.start_location,stock_mode='fixed',router=e.stock.router,tv_box=e.stock.tv_box)
        for e in session.initial_engineers.values()]


def _engineer(session,row,**changes):
    return Engineer(id=row['id'],name=row['name'],region=session.scenario.region,
        shift=row['shift'],off_duty=row.get('off_duty',False),skills=row['skills'],transport=row['transport'],
        vehicle_id=row.get('vehicle_id'),
        start_location=row['start_location'],stock=Equipment(router=row['router'],tv_box=row['tv_box'])
        if row['stock_mode']=='fixed' else None).model_copy(update=changes)


def _insert(engineer,sequence,request,travel):
    choices=[]
    for i in range(len(sequence)+1):
        trial=sequence[:i]+[request]+sequence[i:]
        route=build_route(engineer,trial,travel)
        if route is not None:choices.append((route.travel_minutes,route.distance_m,i,trial))
    return min(choices,key=lambda x:x[:3])[-1] if choices else None


def _local_route(engineer,jobs,travel):
    sequence=[]
    for request in sorted(jobs,key=lambda r:({'urgent':0,'high':1,'normal':2}[r.priority],max(w.end for w in r.windows),r.id)):
        trial=_insert(engineer,sequence,request,travel)
        if trial is not None:sequence=trial
    return sequence


def home_starts(session,rows):
    """A deterministic roster hypothesis, not a claimed optimal home location.

    Only starts change. Known city points are medoids of saved travel times;
    short feasible city routes match the existing skills, shifts and stock.
    A quarter of a shift is reserved for travel/waiting in the staffing heuristic.
    The full routing solver validates and compares this hypothesis afterwards.
    """
    result=deepcopy(rows);travel=session._travel(session.scenario)
    jobs_by_city={city:sorted([r for r in session.scenario.requests if r.locality==city],key=lambda r:r.id)
                  for city in ('Кашира','Домодедово','Ступино')}
    available={e['id']:e for e in result if e['start_location']=='office' and not e.get('start_candidate_id')
               and not e.get('off_duty',False)}
    max_moves=max(0,len(available)-1)
    for city,jobs in sorted(jobs_by_city.items(),key=lambda x:(-sum(r.duration_minutes for r in x[1]),x[0])):
        if not jobs or not available or max_moves<=0:continue
        # Do not pile another inferred crew on a city with a user-selected home.
        points=session.geography.locations if session.geography else session.scenario.points
        locations={key:(p.lat,p.lon) if session.geography else (p.x_km,p.y_km) for key,p in points.items()}
        if any(e['start_location']!='office' and e['start_location'] in locations and
               any(locations[e['start_location']]==locations[r.id] for r in jobs) for e in result):continue
        span=max(e['shift']['end']-e['shift']['start'] for e in available.values())
        quantity=max(1,math.ceil(sum(r.duration_minutes for r in jobs)/max(1,span*.75)))
        if not any(all(any(max(e['shift']['start'],w.start)<=min(w.end,e['shift']['end']-r.duration_minutes)
                              for w in r.windows) for r in jobs) for e in available.values()):
            quantity=max(quantity,2)
        medoids={mode:min(jobs,key=lambda a:(sum((travel.minutes(a.id,b.id,mode)+travel.minutes(b.id,a.id,mode))
                        *b.duration_minutes for b in jobs),a.id)).id for mode in {e['transport'] for e in available.values()}}
        remaining=list(jobs)
        for _ in range(min(quantity,max_moves)):
            choices=[]
            for eid,row in sorted(available.items()):
                start=medoids[row['transport']]
                engineer=_engineer(session,row,start_location=start)
                sequence=_local_route(engineer,remaining,travel)
                if not sequence:continue
                route=build_route(engineer,sequence,travel)
                key=tuple(-sum(r.priority==p for r in sequence) for p in ('urgent','high','normal'))
                choices.append((key+(route.travel_minutes,route.distance_m,eid),eid,start,sequence))
            if not choices:break
            _,eid,start,sequence=min(choices,key=lambda c:c[0])
            available.pop(eid)['start_location']=start;max_moves-=1
            assigned={r.id for r in sequence};remaining=[r for r in remaining if r.id not in assigned]
            if not remaining:break
    return result


def skill_gaps(session,rows):
    """Explicit qualification hypotheses with a feasible insertion witness.

    Never grants skills to the operational roster; it only fills a preview form.
    Existing request order is retained and every suggested addition has a valid
    route under the current start, shift, transport and equipment policy.
    """
    result=deepcopy(rows);travel=session._travel(session.scenario)
    sequences={route.engineer_id:[session.catalog[v.request_id] for v in route.visits] for route in session.plan.routes}
    for u in sorted(session.plan.unassigned,key=lambda u:({'urgent':0,'high':1,'normal':2}[session.catalog[u.request_id].priority],u.request_id)):
        request=session.catalog[u.request_id];choices=[]
        for row in result:
            if request.skill in row['skills'] or row.get('start_candidate_id'):continue
            e=_engineer(session,row,skills=row['skills']+[request.skill])
            old=sequences.get(e.id,[]);sequence=_insert(e,old,request,travel)
            if sequence is not None:
                route=build_route(e,sequence,travel);before=build_route(e,old,travel)
                choices.append(((route.travel_minutes-before.travel_minutes,e.id),row,sequence))
        if choices:
            _,row,sequence=min(choices,key=lambda c:c[0]);row['skills'].append(request.skill)
            sequences[row['id']]=sequence
    return result


def roster_changes(before,after):
    old={r['id']:r for r in before};new={r['id']:r for r in after};changes=[]
    fields=('name','shift','off_duty','skills','transport','vehicle_id','start_location','start_candidate_id','stock_mode','router','tv_box')
    for eid in sorted(old.keys()|new.keys()):
        a,b=old.get(eid),new.get(eid)
        diff={'crew':{'before':a,'after':b}} if a is None or b is None else {
            key:{'before':a.get(key),'after':b.get(key)} for key in fields if a.get(key)!=b.get(key)}
        if diff:changes.append({'engineer_id':eid,'name':(b or a)['name'],'fields':diff})
    return changes


def coverage(session,plan):
    assigned={v.request_id for route in plan.routes for v in route.visits}
    return {p:{'assigned':sum(r.id in assigned for r in session.scenario.requests if r.priority==p),
               'total':sum(r.priority==p for r in session.scenario.requests)} for p in ('urgent','high','normal')}


CREW_ASSUMPTIONS=[
    'Домашние старты моделируются по известным адресам заявок; фактические дома неизвестны.',
    'Добавление навыка — гипотеза квалификации; перед применением подтвердите её у бригады.',
    'Выдача под план предполагает достаточный склад. Оборудование у бригады до начала смены, включая домашний старт.',
]


def presets(session,rows):
    homes=home_starts(session,rows);skills=skill_gaps(session,rows)
    reserve=[{**deepcopy(e),'stock_mode':'plan','router':1,'tv_box':0} for e in rows]
    combined=home_starts(session,skill_gaps(session,reserve))
    variants=[('current','Текущий состав','Без изменения параметров.',rows),
        ('homes','Только домашние старты','Те же бригады, смены, навыки, транспорт и запас. Старты — в удалённых городах.',homes),
        ('skills','Только дополнительные навыки','Навыки для неназначенных работ, которые можно вставить при текущем запасе. Требуют подтверждения.',skills),
        ('reserve','Только выдача и резерв','Выдача под новый план + один запасной роутер каждой бригаде.',reserve),
        ('combined','Старты, навыки и резерв','Текущий штат и транспорт; домашние старты, предложенные навыки, выдача под план + резерв.',combined),
        ('control','Контрольная численность','Отдельная модель 12/12/11: меняются также состав, смены и транспорт. Охват может снизиться.',recommended(session))]
    return [dict(id=key,label=label,description=description,engineers=deepcopy(crew),
        changes=roster_changes(rows,crew),assumptions=CREW_ASSUMPTIONS if key!='current' else []) for key,label,description,crew in variants]


def recommended(session):
    count=CONTROL_COUNTS.get(session.scenario.region,len(session.initial_engineers))
    starts=[]
    for city in ('Кашира','Домодедово','Ступино'):
        jobs=[r for r in session.scenario.requests if r.locality==city]
        if jobs:
            quantity=max(1,math.ceil(sum(r.duration_minutes for r in jobs)/(8*60)*1.4))
            starts.extend([jobs[0].id]*min(quantity,3))
    starts=starts[:max(0,count-1)]
    starts+=['office']*(count-len(starts))
    result=[];occurrences={}
    for i,start in enumerate(starts):
        n=occurrences.get(start,0);occurrences[start]=n+1
        early=n%2==0 if start!='office' else i%2==0
        result.append(dict(id=f'model-{i+1:02d}',name=f'Бригада {i+1:02d}',
            shift={'start':540 if early else 780,'end':1080 if early else 1320},
            skills=['local','connection']+(['emergency'] if n%2==0 else []),
            transport='car' if start!='office' and n==0 else 'public',
            start_location=start,stock_mode='plan',router=1,tv_box=0))
    return result


class CrewInput(Model):
    id: str = Field(min_length=1,max_length=100,pattern=r'^[\w:-]+$')
    name: str = Field(min_length=1,max_length=100)
    shift: TimeWindow
    off_duty: bool = Field(default=False, strict=True)
    skills: list[Skill] = Field(min_length=1,max_length=3)
    transport: Transport
    vehicle_id: str | None = None
    start_location: str = Field(min_length=1)
    start_candidate_id: str | None = None
    stock_mode: Literal['plan','fixed'] = 'plan'
    router: int = Field(default=0,ge=0,le=100,strict=True)
    tv_box: int = Field(default=0,ge=0,le=100,strict=True)


class CrewRequest(Model):
    version: int = Field(strict=True)
    engineers: list[CrewInput] = Field(min_length=1,max_length=50)


def editable(session):
    return not session.fact_log and not session.statuses and not any(
        item.get('event',{}).get('type') not in ('crew','address') for item in session.history)


def cloned_geography(geo,scenario,locations,origins):
    """Reuse exact matrix values when a home is a copy of an already known point."""
    matrix=geo.matrices;old={k:i for i,k in enumerate(matrix['locations'])};ids=list(locations)
    sources=[old[origins.get(k,k)] for k in ids]
    updated={'locations':ids,'coordinates_signature':coordinates_signature(locations)}
    for field in ('minutes','metres'):
        updated[field]={mode:[[values[i][j] for j in sources] for i in sources] for mode,values in matrix[field].items()}
    modes=deepcopy(geo.modes)
    if 'public_journeys' in matrix:
        trips=matrix['public_journeys'];updated['public_journeys']={a:{b:deepcopy(trips[origins.get(a,a)][origins.get(b,b)]) for b in ids} for a in ids}
        counts={}
        for row in updated['public_journeys'].values():
            for trip in row.values():counts[trip['kind']]=counts.get(trip['kind'],0)+1
        modes['public']['journey_counts']=counts
    return geo.model_copy(update={'scenario_signature':scenario_signature(scenario),'locations':locations,'matrices':updated,'modes':modes,'route_shapes':{}})


class CrewService:
    def __init__(self,address_service=None):
        self.address_service=address_service;self.draft=None

    def settings(self,session):
        saved=current_rows(session)
        return {'version':session.version,'region':session.scenario.region,'editable':editable(session),
            'assignments':{r.engineer_id:len(r.visits) for r in session.plan.routes},
            'control_count':CONTROL_COUNTS.get(session.scenario.region),'recommended_engineers':recommended(session),'engineers':deepcopy(saved),
            'presets':presets(session,saved) if editable(session) else [],
            'locations':session.state()['locations'],'network':self.address_service is not None,
            'metrics':session.plan.metrics.model_dump()}

    def preview(self,session,payload,*,time_limit=3,solver='hybrid'):
        request=CrewRequest.model_validate(payload)
        if request.version!=session.version:raise ValueError('Устаревшая версия')
        if not editable(session):raise ValueError('Бригады и утренний запас меняются только до начала дня')
        if len({e.id for e in request.engineers})!=len(request.engineers):raise ValueError('Повторный ID бригады')
        points=dict(session.scenario.points);geo=session.geography
        locations=dict(geo.locations) if geo else {};origins={};engineers=[];new_points=False;inputs=[]
        catalog={r.id:r.address for r in session.scenario.requests}
        for row in request.engineers:
            if not row.name.strip() or row.shift.start>=row.shift.end:raise ValueError('Проверьте имя и ненулевую смену')
            start=row.start_location;address=None
            if row.start_candidate_id:
                if not geo or not self.address_service:raise ValueError('Поиск адресов недоступен')
                point=self.address_service.selected(session,{'version':session.version,'candidate_id':row.start_candidate_id})
                start='home:'+row.id;locations[start]=point;address=point.original_address or point.label;new_points=True
            elif start!='office':
                if start not in (geo.locations if geo else points):raise ValueError('Неизвестная стартовая точка')
                source=start;start='home:'+row.id
                if geo:
                    locations[start]=geo.locations[source];origins[start]=source
                    point=locations[start];address=catalog.get(source) or point.original_address or point.label or source
                else:
                    points[start]=points[source];address=catalog.get(source,source)
            stock=Equipment(router=row.router,tv_box=row.tv_box) if row.stock_mode=='fixed' else None
            engineers.append(Engineer(id=row.id,name=row.name.strip(),region=session.scenario.region,shift=row.shift,
                off_duty=row.off_duty,skills=row.skills,transport=row.transport,vehicle_id=row.vehicle_id,
                start_location=start,start_address=address,stock=stock))
            inputs.append({**row.model_dump(mode='json'),'start_location':start,'start_candidate_id':None})
        active_starts={e.start_location for e in engineers}
        points={k:v for k,v in points.items() if not k.startswith('home:') or k in active_starts}
        locations={k:v for k,v in locations.items() if not k.startswith('home:') or k in active_starts}
        data=session.scenario.model_dump();data.update(engineers=[e.model_dump() for e in engineers],points={k:v.model_dump() for k,v in points.items()})
        scenario=Scenario.model_validate(data)
        if geo:
            if new_points:
                geo=build_matrices(geo.model_copy(update={'locations':locations,'scenario_signature':scenario_signature(scenario)}),self.address_service.client,allow_public_estimate=True,routed_access=getattr(self.address_service,'routed_access',False))
            else:geo=cloned_geography(geo,scenario,locations,origins)
            travel=bundle_travel(geo)
        else:travel=synthetic_travel(scenario)
        plan=get_solver(solver).solve(scenario,travel,time_limit_seconds=time_limit)
        planning_scenario=scenario
        loads={r.engineer_id:r.loadout for r in plan.routes}
        issued=[]
        for row,e in zip(request.engineers,engineers):
            load=loads.get(e.id,Equipment())
            stock=e.stock if e.stock is not None else Equipment(router=load.router+row.router,tv_box=load.tv_box+row.tv_box)
            issued.append(e.model_copy(update={'stock':stock}))
        scenario=scenario.model_copy(update={'engineers':issued,'metadata':{**scenario.metadata,'crew_inputs':inputs,
            'crew_assumptions':'Домашний старт после утренней выдачи; склад достаточен. Резерв — параметры диспетчера.'}})
        candidate=ScenarioSession(scenario,plan,geo.model_dump() if geo else None,planning_scenario=planning_scenario)
        candidate.version=session.version+1;candidate.execution_mode=session.execution_mode
        candidate.views=deepcopy(session.views);candidate.history=deepcopy(session.history)+[{'version':candidate.version,'event':{'type':'crew','id':'crew:'+uuid4().hex,'time':candidate.time}}]
        candidate._save_view('Бригады, старты и утренняя выдача')
        token=uuid4().hex;self.draft=(token,session.version,candidate)
        return {'id':token,'version':session.version,'before':session.plan.metrics.model_dump(),'after':plan.metrics.model_dump(),
            'before_coverage':coverage(session,session.plan),'after_coverage':coverage(session,plan),
            'changes':roster_changes(current_rows(session),[r.model_dump(mode='json') for r in request.engineers]),
            'assumptions':CREW_ASSUMPTIONS,
            'engineers':[{'id':e.id,'name':e.name,'region':e.region,
                'assigned':next((len(r.visits) for r in plan.routes if r.engineer_id==e.id),0)} for e in issued],
            'unassigned':[u.model_dump() for u in plan.unassigned],
            'issue':[{'name':e.name,'router':e.stock.router,'tv_box':e.stock.tv_box} for e in issued]}

    def selected(self,session,payload):
        if payload.get('version')!=session.version or not self.draft or self.draft[1]!=session.version:
            raise ValueError('Устаревшая версия; пересчитайте бригады')
        if payload.get('id')!=self.draft[0]:raise ValueError('Предпросмотр не найден')
        if not editable(session):raise ValueError('Настройка доступна только до начала дня')
        return deepcopy(self.draft[2])
