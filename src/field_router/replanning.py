"""Versioned scenario proposals for a clearly labelled Cartesian day simulation."""
from copy import deepcopy
import hashlib
import json
from uuid import uuid4

from pydantic import Field
from .models import Model, Scenario, Plan, Request, Point, Equipment, TimeWindow, Skill, Visit, Route
from .travel import synthetic_travel
from .geo_models import GeographyBundle
from .geography import attach_geography
from .road_travel import bundle_travel
from .operational_facts import validate_fact, apply_observations
from .validation import require_valid
from .objectives import objective_key, reference_visits, response_targets
from .recommendation import LABELS, STRATEGY_ORDER, eligible_candidates
from .solvers.selection import get_solver


class Event(Model):
    id: str = Field(min_length=1,max_length=100)
    type: str
    time: int = Field(ge=0,le=1440,strict=True)
    request_id: str | None = None
    engineer_id: str | None = None
    anchor_id: str | None = None
    duration_minutes: int = Field(default=80,gt=0,le=1440,strict=True)
    router: int = Field(default=0,ge=0,le=100,strict=True)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def _assignments(plan):
    return {v.request_id:(r.engineer_id,v.start) for r in plan.routes for v in r.visits}


def group_candidates(candidates):
    """Keep API aliases but show only unique execution plans, Stable first."""
    groups={}
    for candidate in sorted(candidates,key=lambda c:STRATEGY_ORDER.index(c['id'])):
        plan=candidate['plan']
        signature=fingerprint({'routes':[
            {'engineer_id':r['engineer_id'],'visits':[
                {k:v[k] for k in ('request_id','arrival','start','end','travel_minutes','distance_m')}
                for v in r['visits']]} for r in sorted(plan['routes'],key=lambda r:r['engineer_id'])],
            'unassigned':sorted(u['request_id'] for u in plan['unassigned']),'metrics':plan['metrics']})
        groups.setdefault(signature,[]).append(candidate)
    for group in groups.values():
        ids=[c['id'] for c in group]
        for candidate in group:
            candidate['duplicate_of']=None if candidate is group[0] else group[0]['id']
            candidate['strategy_ids']=list(ids)
    return candidates


def differences(before,after,ids):
    old,new=_assignments(before),_assignments(after)
    result=[]
    for key in sorted(ids):
        a,b=old.get(key,(None,None)),new.get(key,(None,None))
        if a!=b:result.append({'request_id':key,'before_engineer':a[0],'after_engineer':b[0],
                              'before_start':a[1],'after_start':b[1]})
    return result


def facts_for(scenario,plan,diff):
    assigned={v.request_id for r in plan.routes for v in r.visits}
    high={r.id for r in scenario.requests if r.priority in ('high','urgent')}
    urgent={r.id for r in scenario.requests if r.priority=='urgent'}
    connections={r.id for r in scenario.requests if r.priority=='high'}
    return {**plan.metrics.model_dump(),'high_assigned':len(high & assigned),'high_total':len(high),
        'emergency_assigned':len(urgent & assigned),'emergency_total':len(urgent),
        'connection_assigned':len(connections & assigned),'connection_total':len(connections),
        'lost_assignments':sum(d['before_engineer'] is not None and d['after_engineer'] is None for d in diff),
        'new_assignments':sum(d['before_engineer'] is None and d['after_engineer'] is not None for d in diff),
        'reassigned':sum(d['before_engineer'] is not None and d['after_engineer'] is not None and d['before_engineer']!=d['after_engineer'] for d in diff),
        'shifted':sum(d['before_start'] is not None and d['after_start'] is not None and d['before_start']!=d['after_start'] for d in diff),
        'time_shift_minutes':sum(abs(d['after_start']-d['before_start']) for d in diff if d['before_start'] is not None and d['after_start'] is not None)}


def explanation(facts):
    return (f"В оставшейся части дня назначено {facts['assigned']} заявок, без назначения — {facts['unassigned']}. "
            f"Аварии: {facts['emergency_assigned']} из {facts['emergency_total']}; подключения: {facts['connection_assigned']} из {facts['connection_total']}. "
            f"Смена инженера у {facts['reassigned']} заявок; сдвиг начала у {facts['shifted']}. "
            f"Оставшийся путь: {facts['distance_m']/1000:.1f} км. "
            "Вариант проверен на окна, смены, навыки и доступное оборудование. Оптимальность не доказана.")


class ScenarioSession:
    def __init__(self,scenario,plan,geography=None):
        self.geography=GeographyBundle.model_validate(geography) if geography else None
        self.geo_locations=dict(self.geography.locations) if self.geography else {}
        self.aliases={key:key for key in self.geo_locations}
        if self.geography:scenario=attach_geography(scenario,self.geography)
        self.scenario=scenario
        require_valid(scenario,self._travel(scenario),plan)
        routes={r.engineer_id:r for r in plan.routes}
        # Morning issue is based on the published plan. An idle engineer does not
        # acquire arbitrary warehouse stock when a new request appears later.
        engineers=[e.model_copy(update={'stock':e.stock if e.stock is not None else
            (routes[e.id].loadout if e.id in routes else Equipment())}) for e in scenario.engineers]
        self.scenario=scenario.model_copy(update={'engineers':engineers})
        self.plan=plan
        self.version=1
        self.time=min(e.shift.start for e in engineers)
        self.history=[]
        self.frozen=[]
        self.catalog={r.id:r for r in scenario.requests}
        self.initial_stock={e.id:e.stock for e in engineers}
        self.initial_engineers={e.id:e for e in engineers}
        self.batches={}
        self.inventory=[{'engineer_id':e.id,'consumed':Equipment().model_dump(),'reserved':Equipment().model_dump(),'available':e.stock.model_dump(),'dispatchable':True} for e in engineers]
        self.statuses={}
        self.fact_log=[]
        self.fact_audit=[]
        self.execution_mode='simulation'
        self.needs_replanning=False
        self.views={}
        self._save_view('Первоначальный план')

    def _travel(self,scenario):
        if not self.geography:return synthetic_travel(scenario)
        base=bundle_travel(self.geography)
        aliases=self.aliases
        class Travel:
            def minutes(self,a,b,mode):return base.minutes(aliases.get(a,a),aliases.get(b,b),mode)
            def metres(self,a,b,mode):return base.metres(aliases.get(a,a),aliases.get(b,b),mode)
        return Travel()

    def _alias_location(self,alias,source):
        if self.geography:
            self.aliases[alias]=self.aliases.get(source,source)
            self.geo_locations[alias]=self.geo_locations[source]

    def _save_view(self,label):
        payload={'scenario':self.scenario.model_dump(mode='json'),'plans':[self.plan.model_dump(mode='json')],
            'operations':{'version':self.version,'time':self.time,'label':label,'frozen':deepcopy(self.frozen),
                'needs_replanning':self.needs_replanning,'mode':self.mode(),'statuses':deepcopy(list(self.statuses.values()))},
            'validation':{'valid':not self.needs_replanning,'violations':[]}}
        if self.geography:
            payload['geography']={**self.geography.model_dump(mode='json'),'location_aliases':dict(self.aliases),'locations':{k:v.model_dump(mode='json') for k,v in self.geo_locations.items()},'route_shapes':self.geography.route_shapes if self.version==1 else {}}
        self.views[self.version]=payload

    def workbench(self,version=None):
        chosen=self.version if version is None else version
        if chosen not in self.views:raise ValueError('Версия плана не найдена')
        payload=deepcopy(self.views[chosen])
        payload['request_labels']=self.request_labels()
        payload['operations']['historical']=chosen!=self.version
        return payload

    def record_fact(self,payload):
        candidate=deepcopy(self)
        result=candidate._record_fact(payload)
        self.__dict__.update(candidate.__dict__)
        return result

    def _record_fact(self,payload):
        from .fact_journal import record
        fact=record(self,payload)
        self.version+=1;self.needs_replanning=True
        self._refresh_fact_inventory()
        self.batches.clear()
        self.history.append({'event':{'id':f'fact:{self.version}','type':'fact','time':self.time},'version':self.version,'fact':fact})
        self._save_view('Фактический статус; требуется пересчет')
        return self.state()


    def _refresh_fact_inventory(self):
        result=[]
        recorded={f['request_id'] for f in self.fact_log if f['status'] in ('completed','interrupted')}
        for eid,initial in self.initial_stock.items():
            consumed={'router':0,'tv_box':0};reserved={'router':0,'tv_box':0}
            fact=self.statuses.get(eid,{})
            for route in self.frozen:
                if route['engineer_id']!=eid:continue
                for v in route['visits']:
                    if v.get('fact_id') or v['request_id'] in recorded or (fact.get('status')=='working' and fact.get('request_id')==v['request_id']):continue
                    target=consumed if v['end']<=self.time else reserved
                    for k in target:target[k]+=v.get('consumed_equipment',self.catalog[v['request_id']].equipment.model_dump())[k]
            for f in self.fact_log:
                if f['engineer_id']==eid:
                    for k in consumed:consumed[k]+=f['consumed'][k]
            if fact.get('status')=='working':
                key=fact['request_id']
                for k in reserved:reserved[k]+=max(0,getattr(self.catalog[key].equipment,k)-sum(f['consumed'][k] for f in self.fact_log if f.get('request_id')==key))
            available={k:getattr(initial,k)-consumed[k]-reserved[k] for k in consumed}
            if any(v<0 for v in available.values()):raise ValueError('Фактический расход/резерв превышает запас')
            result.append({'engineer_id':eid,'consumed':consumed,'reserved':reserved,'available':available,
                'dispatchable':fact.get('status')!='unavailable' and (self.execution_mode!='manual' or eid in self.statuses) and any(e.id==eid and e.shift.end>self.time for e in self.scenario.engineers)})
        self.inventory=result

    def mode(self):
        return 'manual' if self.execution_mode=='manual' else 'mixed' if self.statuses else 'simulation'

    def set_mode(self,payload):
        if payload.get('version')!=self.version:raise ValueError('Устаревшая версия')
        if payload.get('mode') not in ('manual','simulation'):raise ValueError('Неизвестный режим')
        self.execution_mode=payload['mode'];self.version+=1;self.needs_replanning=True;self.batches.clear()
        self.history.append({'version':self.version,'event':{'id':f'mode:{self.version}','type':'mode','time':self.time},'mode':self.execution_mode})
        self._refresh_fact_inventory();self._save_view('Изменен режим учета')
        return self.state()

    def request_labels(self):
        return {key:f'Авария №{i+1}' for i,key in enumerate(k for k in self.catalog if k.startswith('emergency:'))}

    def fact_requests(self):
        owners={v['request_id']:r['engineer_id'] for r in self.frozen for v in r['visits']}
        owners.update({v.request_id:r.engineer_id for r in self.plan.routes for v in r.visits})
        done={f['request_id'] for f in self.fact_log if f['status']=='completed'}
        return [{'id':key,'address':self.catalog[key].address,'engineer_id':eid} for key,eid in owners.items() if key not in done]

    def historical_requests(self):
        owners=set()
        for view in self.views.values():
            for route in view['plans'][0]['routes']:
                owners.update((v['request_id'],route['engineer_id']) for v in route['visits'])
            for route in view['operations'].get('frozen',[]):
                owners.update((v['request_id'],route['engineer_id']) for v in route['visits'])
        return [{'id':key,'address':self.catalog[key].address,'engineer_id':eid} for key,eid in sorted(owners) if key in self.catalog]

    def state(self):
        return {'historical_requests':self.historical_requests(),'fact_journal':deepcopy(self.fact_log),'fact_audit':deepcopy(self.fact_audit),'execution_mode':self.execution_mode,'version':self.version,'time':self.time,'region':self.scenario.region,'approximate_locations':sum(p.quality=='approximate' for p in self.geography.locations.values()) if self.geography else 0,'simulation':not bool(self.statuses),'geography_mode':'road' if self.geography else 'synthetic',
            'mode':self.mode(),'statuses':deepcopy(list(self.statuses.values())),'needs_replanning':self.needs_replanning,
            'locations':[{'id':k,'label':self.catalog[k].address if k in self.catalog else ('Офис' if k=='office' else (self.geo_locations[k].original_address or self.geo_locations[k].label) if k in self.geo_locations else k)} for k in (self.geo_locations if self.geography else self.scenario.points) if not k.startswith(('start:','position:','resume:','closed:','observed:'))],
            'versions':[{'version':v,'time':p['operations']['time'],'label':p['operations']['label']} for v,p in self.views.items()],
            'requests':[{'id':r.id,'address':r.address,'priority':r.priority,'duration_minutes':r.duration_minutes} for r in self.scenario.requests],
            'engineers':[{'id':e.id,'name':e.name} for e in self.initial_engineers.values()],
            'fact_requests':self.fact_requests(),'request_labels':self.request_labels(),
            'plan':self.plan.model_dump(mode='json'),'history':deepcopy(self.history),
            'frozen':deepcopy(self.frozen),'inventory':deepcopy(self.inventory)}

    def _residual(self,event,allow_redirect,preserve):
        s=self.scenario
        now=event.time
        if self.execution_mode=='manual':
            s=s.model_copy(update={'engineers':[e.model_copy(update={'shift':TimeWindow(start=min(now,e.shift.end),end=min(now,e.shift.end))}) if e.id not in self.statuses else e for e in s.engineers]})
        for route in self.frozen:
            if route['engineer_id'] in self.statuses:continue
            for visit in route['visits']:
                if visit['start']<=now<visit['end']:
                    if not preserve:raise ValueError('Есть начатая закрепленная работа: явно включите сохранение до завершения')
                    if event.type=='unavailable' and event.engineer_id==route['engineer_id']:
                        raise ValueError('Инженер выполняет закрепленную работу: уточните фактическое завершение/прерывание')
        routes={r.engineer_id:r for r in self.plan.routes if self.execution_mode!='manual' or r.engineer_id in self.statuses}
        prior_frozen=deepcopy(self.frozen)
        if self.statuses:s,routes,prior_frozen=apply_observations(self,s,routes,prior_frozen,now,
            allow_redirect=allow_redirect and event.type=='emergency')
        points=dict(s.points)
        if self.geography:points={k:Point(x_km=v.lon,y_km=v.lat) for k,v in self.geo_locations.items()}
        restored_jobs=[]
        updated_engineers={e.id:e for e in s.engineers}
        for route in prior_frozen:
            released=[]
            for visit in list(route['visits']):
                can_release=(event.type=='emergency' and allow_redirect) or (event.type=='unavailable' and event.engineer_id==route['engineer_id'])
                if visit['start']<=now or not can_release:continue
                origin=route['origins'][visit['request_id']]
                e=updated_engineers[route['engineer_id']]
                request=self.catalog[visit['request_id']]
                restored_jobs.append(request)
                alias=f"resume:{self.version}:{e.id}:{now}"
                points[alias]=points[origin['location']]
                self._alias_location(alias,origin['location'])
                updated_engineers[e.id]=e.model_copy(update={'start_location':alias,
                    'shift':TimeWindow(start=origin['departure'],end=e.shift.end),
                    'stock':Equipment(router=e.stock.router+visit.get('consumed_equipment',request.equipment.model_dump())['router'],tv_box=e.stock.tv_box+visit.get('consumed_equipment',request.equipment.model_dump())['tv_box'])})
                released.append(Visit.model_validate({k:visit[k] for k in Visit.model_fields}));route['visits'].remove(visit)
            if released:
                eid=route['engineer_id']
                old=routes.get(eid)
                if old:routes[eid]=old.model_copy(update={'visits':released+old.visits})
                else:routes[eid]=Route(engineer_id=eid,visits=released,distance_m=0,travel_minutes=0,service_minutes=0,loadout=Equipment())
        s=s.model_copy(update={'requests':s.requests+restored_jobs,'engineers':list(updated_engineers.values())})
        jobs={r.id:r for r in s.requests}
        engineers=[]
        locked=set()
        frozen=[r for r in prior_frozen if r['visits']]
        for e in s.engineers:
            stock=e.stock.model_dump()
            location=e.start_location
            ready=max(now,e.shift.start)
            departure=e.shift.start
            keep=[];origins={}
            for v in routes[e.id].visits if e.id in routes else []:
                in_work=v.start<=now<v.end
                on_road=departure<now<v.arrival
                if in_work and not preserve:
                    raise ValueError('Есть начатая работа: явно включите сохранение до завершения')
                freeze=v.end<=now or in_work or (on_road and not (allow_redirect and event.type=='emergency'))
                # A failed engineer cannot finish the trip. Work already started
                # needs an explicit actual-state update, not a fictional finish.
                if event.type=='unavailable' and event.engineer_id==e.id:
                    if in_work:raise ValueError('Инженер выполняет работу: сначала требуется уточнить фактическое завершение/прерывание')
                    if v.end>now:freeze=False
                if freeze:
                    origins[v.request_id]={'location':location,'departure':departure}
                    locked.add(v.request_id);keep.append({**v.model_dump(mode='json'),'consumed_equipment':jobs[v.request_id].equipment.model_dump()})
                    for device in stock:stock[device]-=getattr(jobs[v.request_id].equipment,device)
                    location=v.request_id
                    ready=max(ready,v.end)
                else:
                    # This engineer is removed, not rerouted. Its current position
                    # is unnecessary; release future work without inventing GPS.
                    if event.type=='unavailable' and event.engineer_id==e.id:break
                    if on_road:
                        observation=self.statuses.get(e.id,{})
                        if observation.get('status')=='traveling' and observation.get('time')==now:
                            location=observation['location_id']
                        else:
                            if self.geography:raise ValueError('Для перенаправления на реальных дорогах нужен свежий ручной статус с подтвержденной текущей точкой; интерполяция дороги запрещена')
                            a,b=points[location],points[v.request_id]
                            fraction=(now-departure)/(v.arrival-departure)
                            alias=f'position:{self.version}:{e.id}:{now}'
                            points[alias]=Point(x_km=a.x_km+(b.x_km-a.x_km)*fraction,y_km=a.y_km+(b.y_km-a.y_km)*fraction)
                            location=alias
                    elif v.arrival<=now<v.start:
                        location=v.request_id
                    break
                departure=v.end
            if keep:frozen.append({'engineer_id':e.id,'visits':keep,'origins':origins})
            if any(v<0 for v in stock.values()):raise ValueError('Отрицательный остаток оборудования')
            if event.type=='unavailable' and event.engineer_id==e.id:continue
            ready=min(ready,e.shift.end)  # Retain identity for later release of a frozen trip.
            alias=f'start:{self.version}:{e.id}:{now}'
            points[alias]=points[location]
            self._alias_location(alias,location)
            engineers.append(e.model_copy(update={'start_location':alias,'shift':TimeWindow(start=ready,end=e.shift.end),'stock':Equipment(**stock)}))
        if event.type=='cancel' and event.request_id in locked:
            raise ValueError('Нельзя отменить завершенную или закрепленную текущую заявку')
        remaining=[r for r in s.requests if r.id not in locked and not (event.type=='cancel' and r.id==event.request_id)]
        if event.type=='emergency':
            if event.anchor_id=='office':source_address=s.office_address
            elif event.anchor_id in self.catalog:source_address=self.catalog[event.anchor_id].address
            else:source_address=self.geo_locations[event.anchor_id].original_address or self.geo_locations[event.anchor_id].label
            key='emergency:'+event.id
            if key in self.catalog:raise ValueError('ID новой заявки уже существует')
            points[key]=points[event.anchor_id]
            self._alias_location(key,event.anchor_id)
            remaining.append(Request(id=key,region=s.region,date=s.date,address=source_address,
                bk_type='Глобальная проблема',hd_type='Новая аварийная заявка',windows=[TimeWindow(start=now,end=1440)],
                duration_minutes=event.duration_minutes,skill=Skill.EMERGENCY,priority='urgent',equipment=Equipment(router=event.router),
                provenance={'event':event.id,'coordinates':('Дорожная точка: ' if self.geography else 'Условная точка: ')+event.anchor_id}))
        if not engineers:
            # Preserve a zero-capacity retired slot so the solver-neutral Scenario
            # remains valid. Its shift cannot fit any positive-duration request.
            e=s.engineers[0]
            alias=f'closed:{self.version}:{now}'
            points[alias]=points[e.start_location]
            self._alias_location(alias,e.start_location)
            engineers=[e.model_copy(update={'start_location':alias,'shift':TimeWindow(start=now,end=now),'stock':Equipment()})]
        residual=Scenario.model_validate({**s.model_dump(mode='json'),'id':s.id+f'/event/{event.id}',
            'requests':[r.model_dump(mode='json') for r in remaining],
            'engineers':[e.model_dump(mode='json') for e in engineers],
            'points':{} if self.geography else {k:p.model_dump() for k,p in points.items()}})
        inventory=[]
        catalog={**self.catalog,**{r.id:r for r in remaining}}
        for eid,initial in self.initial_stock.items():
            consumed={'router':0,'tv_box':0};reserved={'router':0,'tv_box':0}
            for route in frozen:
                if route['engineer_id']!=eid:continue
                for v in route['visits']:
                    target=consumed if v['end']<=now else reserved
                    for device in target:target[device]+=v.get('consumed_equipment',catalog[v['request_id']].equipment.model_dump())[device]
            available={k:getattr(initial,k)-consumed[k]-reserved[k] for k in consumed}
            inventory.append({'engineer_id':eid,'consumed':consumed,'reserved':reserved,'available':available,'dispatchable':any(e.id==eid and e.shift.end>now for e in engineers)})
        return residual,frozen,inventory

    def preview(self,payload,*,time_limit=1,solver='hybrid'):
        if payload.get('version')!=self.version:raise ValueError('Устаревшая версия плана; обновите состояние')
        event=Event.model_validate(payload.get('event',{}))
        if event.id in {h['event']['id'] for h in self.history}:raise ValueError('Запрещен повтор уже примененного события')
        if event.time<self.time:raise ValueError('Событие не может быть раньше текущего времени')
        if event.type not in ('emergency','cancel','unavailable','replan'):raise ValueError('Неизвестный тип события')
        ids={r.id for r in self.scenario.requests}
        if event.type=='cancel' and event.request_id not in ids:raise ValueError('Неизвестная или уже завершенная заявка')
        if event.type=='emergency':
            anchors=set(self.catalog)|set(self.geo_locations)
            if 'office' in self.scenario.points:anchors.add('office')
            if event.anchor_id not in anchors:raise ValueError('Неизвестная точка новой аварии')
        if event.type=='unavailable' and event.engineer_id not in {e.id for e in self.scenario.engineers}:raise ValueError('Неизвестный инженер')
        for key in ('allow_redirect','preserve_in_progress'):
            if type(payload.get(key,False)) is not bool:raise ValueError('Ожидается явный переключатель '+key)
        s,frozen,inventory=self._residual(event,payload.get('allow_redirect',False),payload.get('preserve_in_progress',False))
        travel=self._travel(s)
        ref_routes={r.engineer_id:r for r in self.plan.routes}
        for frozen_route in self.frozen:
            eid=frozen_route['engineer_id']
            visits=[Visit.model_validate({k:v[k] for k in Visit.model_fields}) for v in frozen_route['visits']]
            if eid in ref_routes:
                ref_routes[eid]=ref_routes[eid].model_copy(update={'visits':visits+ref_routes[eid].visits})
            else:
                ref_routes[eid]=Route(engineer_id=eid,visits=visits,distance_m=0,travel_minutes=0,service_minutes=0,loadout=Equipment())
        reference=self.plan.model_copy(update={'routes':list(ref_routes.values())})
        labels=self.request_labels()
        if event.type=='emergency':labels['emergency:'+event.id]=f'Авария №{len(labels)+1}'
        candidates=[]
        focus='emergency:'+event.id if event.type=='emergency' else None
        targets=response_targets(s,focus)
        prior=reference_visits(s,reference)
        available={e.id for e in s.engineers}
        backend=get_solver(solver)
        pool=[backend.solve(s,travel,time_limit_seconds=time_limit,profile=p,reference=reference,focus_request_id=focus) for p in ('sla','stable','cost')]
        for profile in ('sla','stable','cost'):
            plan=min(pool,key=lambda candidate:objective_key(candidate,s,profile=profile,reference=reference,focus_request_id=focus))
            require_valid(s,travel,plan)
            diff_ids={r.id for r in s.requests}
            if event.type=='cancel':diff_ids.add(event.request_id)
            diff=differences(reference,plan,diff_ids)
            facts=facts_for(s,plan,[d for d in diff if not (event.type=='cancel' and d['request_id']==event.request_id)])
            assignments=_assignments(plan)
            facts.update(avoidable_reassigned=sum(old[0] in available and assignments[key][0]!=old[0]
                         for key,old in prior.items() if key in assignments),
                         priority_start_sum=sum(assignments[key][1] for key in targets if key in assignments),
                         response_target_count=len(targets),response_unassigned=len(targets-set(assignments)))
            by_id={r.id:r for r in s.requests}
            lost={d['request_id'] for d in diff if d['before_engineer'] and d['after_engineer'] is None}
            emergency=None
            if event.type=='emergency':
                match=next(((r.engineer_id,v) for r in plan.routes for v in r.visits if v.request_id=='emergency:'+event.id),None)
                emergency={'assigned':bool(match),'engineer_id':match[0] if match else None,
                    'arrival':match[1].arrival if match else None,'start':match[1].start if match else None,
                    'end':match[1].end if match else None,'response_minutes':match[1].start-event.time if match else None}
            unassigned_details=[{'request_id':u.request_id,'address':by_id[u.request_id].address,
                'reason':u.reason,'lost_assignment':u.request_id in lost} for u in plan.unassigned]
            candidates.append({'id':profile,'label':LABELS[profile],'plan':plan.model_dump(mode='json'),'diff':diff,'facts':facts,
                'objective_key':list(objective_key(plan,s,profile=profile,reference=reference,focus_request_id=focus)),
                'emergency':emergency,'unassigned_details':unassigned_details,'explanation':explanation(facts),'duplicate_of':None,
                'llm':{'status':'not_connected','facts_only':True,'instruction':'Объясни компромиссы только по facts и diff. Не придумывай SLA, деньги, причины невозможности или оптимальность. Решение принимает диспетчер.'}})
        group_candidates(candidates)
        batch={'request_labels':labels,'observation_warnings':[f"{self.initial_engineers[eid].name}: последний ручной факт в {f['time']//60:02}:{f['time']%60:02}; последующее выполнение не подтверждено." for eid,f in self.statuses.items() if f['time']<event.time],'id':uuid4().hex,'version':self.version,'event':event.model_dump(),'candidates':candidates,'frozen':frozen,'inventory':inventory,
            'notice':('Дорожные матрицы. ' if self.geography else 'Условная география. ')+('Ручные статусы и симуляция остальных инженеров. ' if self.statuses else 'Исполнение плана моделируется. ')+'Показатели относятся к остатку дня; текущие закрепленные работы показаны отдельно. Базовое объяснение сформировано по проверенным фактам.'}
        batch['recommended_candidate_id']=eligible_candidates(batch)[0]['id']
        batch['response_focus_request_id']=focus
        # Internal payload never trusts a plan sent back from the browser.
        self.batches[batch['id']]={'batch':deepcopy(batch),'scenario':s,'hash':fingerprint(batch),'policy':{'allow_redirect':payload.get('allow_redirect',False),'preserve_in_progress':payload.get('preserve_in_progress',False)}}
        return batch

    def approve(self,payload):
        if payload.get('version')!=self.version:raise ValueError('Устаревшая версия плана')
        stored=self.batches.get(payload.get('batch_id'))
        if stored is None:raise ValueError('Предложение не найдено; пересчитайте')
        batch=stored['batch']
        if batch['version']!=self.version or fingerprint(batch)!=stored['hash']:raise ValueError('Измененная или устаревшая версия предложения')
        candidate=next((c for c in batch['candidates'] if c['id']==payload.get('candidate_id')),None)
        if candidate is None:raise ValueError('Неизвестный вариант')
        plan=Plan.model_validate(candidate['plan'])
        require_valid(stored['scenario'],self._travel(stored['scenario']),plan)
        self.scenario=stored['scenario'];self.plan=plan
        [self.catalog.setdefault(r.id,r) for r in self.scenario.requests]
        self.time=batch['event']['time'];self.version+=1;self.frozen=batch['frozen'];self.inventory=batch['inventory']
        self.history.append({'event':batch['event'],'candidate_id':candidate['id'],'version':self.version,'policy':stored['policy']})
        self.batches.clear()
        self.needs_replanning=False
        self._save_view('Утвержден '+candidate['label'])
        return self.state()

    def dump(self):
        return {'execution_mode':self.execution_mode,'fact_audit':self.fact_audit,'initial_engineers':{k:v.model_dump(mode='json') for k,v in self.initial_engineers.items()},'geography':self.geography.model_dump(mode='json') if self.geography else None,'aliases':self.aliases,'geo_locations':{k:v.model_dump(mode='json') for k,v in self.geo_locations.items()},'statuses':self.statuses,'fact_log':self.fact_log,'needs_replanning':self.needs_replanning,'views':self.views,'scenario':self.scenario.model_dump(mode='json'),'plan':self.plan.model_dump(mode='json'),
            'version':self.version,'time':self.time,'history':self.history,'frozen':self.frozen,'inventory':self.inventory,
            'catalog':{k:v.model_dump(mode='json') for k,v in self.catalog.items()},
            'initial_stock':{k:v.model_dump() for k,v in self.initial_stock.items()}}

    @classmethod
    def restore(cls,payload):
        obj=cls.__new__(cls)
        obj.geography=GeographyBundle.model_validate(payload['geography']) if payload.get('geography') else None
        from .geo_models import GeoLocation
        obj.geo_locations={k:GeoLocation.model_validate(v) for k,v in payload.get('geo_locations',{}).items()}
        obj.aliases=payload.get('aliases',{})
        obj.scenario=Scenario.model_validate(payload['scenario']);obj.plan=Plan.model_validate(payload['plan'])
        require_valid(obj.scenario,obj._travel(obj.scenario),obj.plan)
        obj.batches={}
        obj.version=payload['version'];obj.time=payload['time'];obj.history=payload['history'];obj.frozen=payload['frozen']
        obj.catalog={k:Request.model_validate(v) for k,v in payload['catalog'].items()}
        obj.initial_stock={k:Equipment.model_validate(v) for k,v in payload['initial_stock'].items()}
        from .models import Engineer
        obj.initial_engineers={k:Engineer.model_validate(v) for k,v in payload.get('initial_engineers',{}).items()} or {e.id:e for e in obj.scenario.engineers}
        obj.inventory=payload['inventory']
        obj.statuses=payload.get('statuses',{});obj.fact_log=payload.get('fact_log',[])
        obj.execution_mode=payload.get('execution_mode','simulation');obj.fact_audit=payload.get('fact_audit',[])
        from .fact_journal import migrate
        migrate(obj)
        obj.needs_replanning=payload.get('needs_replanning',False)
        obj.views={int(k):v for k,v in payload.get('views',{}).items()}
        if not obj.views:obj._save_view('Восстановленный план')
        return obj
