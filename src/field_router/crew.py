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

CONTROL_COUNTS={'Восток':12,'Юго-восток':12,'Югоцентр':11}


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
    skills: list[Skill] = Field(min_length=1,max_length=3)
    transport: Transport
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
        saved=session.scenario.metadata.get('crew_inputs')
        if saved is None:
            saved=[dict(id=e.id,name=e.name,shift=e.shift.model_dump(),skills=e.skills,transport=e.transport,
                start_location=e.start_location,stock_mode='fixed',router=e.stock.router,tv_box=e.stock.tv_box)
                for e in session.initial_engineers.values()]
        return {'version':session.version,'region':session.scenario.region,'editable':editable(session),
            'assignments':{r.engineer_id:len(r.visits) for r in session.plan.routes},
            'control_count':CONTROL_COUNTS.get(session.scenario.region),'recommended_engineers':recommended(session),'engineers':deepcopy(saved),
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
                skills=row.skills,transport=row.transport,start_location=start,start_address=address,stock=stock))
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
        loads={r.engineer_id:r.loadout for r in plan.routes}
        issued=[]
        for row,e in zip(request.engineers,engineers):
            load=loads.get(e.id,Equipment())
            stock=e.stock if e.stock is not None else Equipment(router=load.router+row.router,tv_box=load.tv_box+row.tv_box)
            issued.append(e.model_copy(update={'stock':stock}))
        scenario=scenario.model_copy(update={'engineers':issued,'metadata':{**scenario.metadata,'crew_inputs':inputs,
            'crew_assumptions':'Домашний старт после утренней выдачи; склад достаточен. Резерв — параметры диспетчера.'}})
        candidate=ScenarioSession(scenario,plan,geo.model_dump() if geo else None)
        candidate.version=session.version+1;candidate.execution_mode=session.execution_mode
        candidate.views=deepcopy(session.views);candidate.history=deepcopy(session.history)+[{'version':candidate.version,'event':{'type':'crew','id':'crew:'+uuid4().hex,'time':candidate.time}}]
        candidate._save_view('Бригады, старты и утренняя выдача')
        token=uuid4().hex;self.draft=(token,session.version,candidate)
        return {'id':token,'version':session.version,'before':session.plan.metrics.model_dump(),'after':plan.metrics.model_dump(),
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
