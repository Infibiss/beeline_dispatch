"""Versioned scenario proposals for a clearly labelled Cartesian day simulation."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator
from .models import Model, Scenario, Plan, Request, Point, Equipment, TimeWindow, Skill, Visit, Route, Metrics
from .travel import synthetic_travel
from .geo_models import GeographyBundle
from .geography import attach_geography
from .road_travel import bundle_travel
from .operational_facts import validate_fact, apply_observations
from .validation import require_valid
from .objectives import objective_key, reference_visits, response_targets
from .recommendation import LABELS, STRATEGY_ORDER, eligible_candidates
from .solvers.selection import get_solver
from .ordinary_insertion import PRESERVATION_ERROR, insertion_candidates
from .completion import pending_completions
from .risk_engine import (RISK_MODEL_VERSION, RiskEngine, application_risk_config,
                          application_risk_seed)
from .service_observations import confirmed_service_observations
from .availability import (day_blocks, day_interval, make_period, shift_blocks, stamp,
                           project_availability, availability_at)


WORK_KINDS = {'connection': ('Подключение', Skill.CONNECTION, 'high'),
              'local': ('Локальная проблема', Skill.LOCAL, 'normal'),
              'addon': ('Доп. заказ', Skill.CONNECTION, 'normal')}


class Event(Model):
    id: str = Field(min_length=1,max_length=100)
    type: str
    time: int = Field(ge=0,le=1440,strict=True)
    request_id: str | None = None
    engineer_id: str | None = None
    engineer_ids: list[str] = Field(default_factory=list,max_length=100)
    anchor_id: str | None = None
    duration_minutes: int = Field(default=80,gt=0,le=1440,strict=True)
    router: int = Field(default=0,ge=0,le=100,strict=True)
    tv_box: int = Field(default=0,ge=0,le=100,strict=True)
    work_kind: Literal['connection','local','addon'] | None = None
    window_start: int | None = Field(default=None,ge=0,le=1440,strict=True)
    window_end: int | None = Field(default=None,ge=0,le=1440,strict=True)

    @model_validator(mode='after')
    def ordinary_fields(self):
        if self.type=='new_request':
            if self.work_kind is None or self.window_start is None or self.window_end is None:
                raise ValueError('Для обычной заявки нужны вид работы и обе границы окна')
            TimeWindow(start=self.window_start,end=self.window_end)
        return self


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def preview_risk_seed(session, event, payload):
    """Stable seed for semantically identical preview requests.

    The browser's event ID is an idempotency/audit token, not a model input.
    Excluding it prevents a second click with unchanged fields from producing
    different percentages solely because the UI generated another UUID.
    """
    event_context = event.model_dump(mode='json')
    event_context.pop('id', None)
    event_context['engineer_ids'] = sorted(event_context.get('engineer_ids', []))
    return application_risk_seed(
        session.plan,
        session.scenario,
        context={
            'surface': 'candidate-preview',
            'event': event_context,
            'allow_redirect': payload.get('allow_redirect', False),
            'preserve_in_progress': True,
            'local_scope_engineer_ids': sorted(payload.get('local_scope_engineer_ids', [])),
            'call_off_duty_engineer_ids': sorted(payload.get('call_off_duty_engineer_ids', [])),
            'call_off_duty_window_start': payload.get('call_off_duty_window_start'),
            'call_off_duty_window_end': payload.get('call_off_duty_window_end'),
        },
    )


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


def preserved_approved_seed(scenario, travel, reference):
    """Feasible fallback retaining every still-feasible published visit and start.

    A changed availability/fact can invalidate an old visit. In that case the
    visit is left for the solver; no unavailable or off-duty worker is silently
    returned to service. New intake and previously unassigned work remain in
    the residual scenario as explicit unassigned jobs until search finds a slot.
    """
    from .ordinary_insertion import _fixed_route
    from .planning import explain_unassigned

    jobs={request.id:request for request in scenario.requests}
    engineers={engineer.id:engineer for engineer in scenario.engineers}
    start_times={key:start for key,(_,start) in _assignments(reference).items()}
    routes=[];assigned=set()
    for old_route in reference.routes:
        engineer=engineers.get(old_route.engineer_id)
        if engineer is None or engineer.off_duty:
            continue
        kept=[];fixed=None
        for visit in old_route.visits:
            key=visit.request_id
            if key not in jobs or key in assigned:
                continue
            trial=_fixed_route(engineer,kept+[key],jobs,start_times,travel)
            if trial is not None:
                kept.append(key);fixed=trial;assigned.add(key)
        if fixed is not None:
            routes.append(fixed)
    unassigned=[explain_unassigned(request,scenario,travel)
                for request in scenario.requests if request.id not in assigned]
    result=Plan(solver='approved_seed',status='feasible',scenario_id=scenario.id,
        routes=routes,unassigned=unassigned,
        metrics=Metrics(assigned=len(assigned),unassigned=len(unassigned),
            engineers_used=len(routes),distance_m=sum(route.distance_m for route in routes),
            travel_minutes=sum(route.travel_minutes for route in routes),
            service_minutes=sum(route.service_minutes for route in routes)),
        diagnostics={'policy':'preserve_feasible_approved_visits_and_starts'})
    return require_valid(scenario,travel,result)


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
        self.inventory=[{'engineer_id':e.id,'consumed':Equipment().model_dump(),'reserved':Equipment().model_dump(),
            'available':e.stock.model_dump(),'dispatchable':not e.off_duty} for e in engineers]
        self.statuses={}
        self.fact_log=[]
        self.fact_audit=[]
        self.unavailability=[]
        self.declined_assignments=[]
        self.pending_report_ids=[]
        self.activated_off_duty_windows={}
        self.planning_settings={'mode':'manual','every_n_requests':1,'every_n_minutes':None}
        self.incoming_requests=[]
        self.dispatch_notifications=[]
        self.decision_log=[]
        self.autopilot_last_failure_at=None
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
                'needs_replanning':self.needs_replanning,'mode':self.mode(),'statuses':deepcopy(list(self.statuses.values())),
                'assignment_summary':self.assignment_summary()},
            'validation':{'valid':not self.needs_replanning,'violations':[]}}
        if self.geography:
            # A historical workbench needs only the journeys shown in this plan.
            # Copying the all-pairs database into every view made each status
            # transaction multiply memory usage (and exhausted a 512 MiB demo).
            journeys={}
            all_journeys=self.geography.matrices.get('public_journeys',{})
            engineers={e.id:e for e in self.scenario.engineers}
            for route in self.plan.routes:
                origin=engineers[route.engineer_id].start_location
                for visit in route.visits:
                    a=self.aliases.get(origin,origin)
                    b=self.aliases.get(visit.request_id,visit.request_id)
                    trip=all_journeys.get(a,{}).get(b)
                    if trip is not None:journeys.setdefault(a,{})[b]=deepcopy(trip)
                    origin=visit.request_id
            payload['geography']={
                'location_aliases':dict(self.aliases),
                'locations':{k:v.model_dump(mode='json') for k,v in self.geo_locations.items()},
                'modes':deepcopy(self.geography.modes),
                'matrices':{'public_journeys':journeys},
                'route_shapes':deepcopy(self.geography.route_shapes) if self.version==1 else {}}
        self.views[self.version]=payload

    def workbench(self,version=None):
        chosen=self.version if version is None else version
        if chosen not in self.views:raise ValueError('Версия плана не найдена')
        payload=deepcopy(self.views[chosen])
        payload['request_labels']=self.request_labels()
        payload['operations']['historical']=chosen!=self.version
        if chosen==self.version:
            # Old saved views predate operational assignment counters. Derive
            # the current view from facts; never rewrite historical snapshots.
            payload['operations']['assignment_summary']=self.assignment_summary()
        return payload

    def record_fact(self,payload):
        candidate=deepcopy(self)
        result=candidate._record_fact(payload)
        self.__dict__.update(candidate.__dict__)
        return result

    def _record_fact(self,payload):
        from .fact_journal import record
        fact=record(self,payload)
        # Check the newly submitted fact, not historical facts replayed by the
        # journal. A correction must not silently override a declared absence.
        blocks=shift_blocks(self.unavailability,fact['engineer_id'],self.scenario.date,
                            self._availability_shift(fact['engineer_id']))
        availability=availability_at(
            project_availability(blocks,self.fact_log,fact['engineer_id']),fact['time'])
        if fact['status'] in ('available','traveling','working') and availability=='absent':
            raise ValueError('Действует период недоступности. Для досрочного возвращения нажмите «Я снова доступен».')
        if fact['status'] in ('traveling','working') and availability=='awaiting_return':
            raise ValueError('После периода недоступности сначала подтвердите возвращение и текущую точку.')
        from .completion import refresh_frozen_report
        refresh_frozen_report(self, fact)
        self.version+=1;self.needs_replanning=True
        self._refresh_fact_inventory()
        self.batches.clear()
        self.history.append({'event':{'id':f'fact:{self.version}','type':'fact','time':self.time},'version':self.version,'fact':fact})
        self._save_view('Фактический статус; требуется пересчет')
        return self.state()

    def report_unavailability(self,payload):
        candidate=deepcopy(self)
        result=candidate._report_unavailability(payload)
        self.__dict__.update(candidate.__dict__)
        return result

    def _availability_shift(self,engineer_id):
        """An explicitly activated day-off worker follows the approved call window."""
        initial=self.initial_engineers[engineer_id]
        current=next((e for e in self.scenario.engineers if e.id==engineer_id),None)
        if initial.off_duty and current and not current.off_duty:
            return self.activated_off_duty_windows.get(engineer_id,current.shift)
        return initial.shift

    def _report_unavailability(self,payload):
        if type(payload.get('version')) is not int or payload['version']!=self.version:
            raise ValueError('Устаревшая версия плана')
        locations=set(self.scenario.points)|set(self.geo_locations)
        engineer_roster={eid:e.model_copy(update={'shift':self._availability_shift(eid)})
                         for eid,e in self.initial_engineers.items()}
        period=make_period(payload,self.scenario.date,self.time,engineer_roster,locations,self.unavailability)
        engineer_id=period['engineer_id']
        block=day_interval(period,self.scenario.date)
        shift=self._availability_shift(engineer_id)
        if block and block[0]<shift.end and shift.start<block[1]:
            # Reporting an absence must not silently complete or interrupt work.
            current=self.statuses.get(engineer_id,{})
            if current.get('status')=='working' and block[0]<self.time+current['remaining_minutes']:
                raise ValueError('Сначала прервите или завершите начатую работу')
            for route in self.plan.routes:
                if route.engineer_id!=engineer_id:continue
                if any(v.start<=self.time<v.end and block[0]<v.end for v in route.visits):
                    if not current and self.execution_mode!='manual':
                        raise ValueError('Сначала прервите или завершите начатую работу')
        self.unavailability.append(period)
        self.version+=1
        if block and block[0]<shift.end and shift.start<block[1]:
            self.needs_replanning=True
            self.pending_report_ids.append(period['id'])
            affected=self.pending_reassignment_ids(engineer_id)
            if affected:
                self.dispatch_notifications.append({'id':uuid4().hex,'kind':'replan_required',
                    'time':datetime.now(timezone.utc).isoformat(),'version':self.version,
                    'title':f'Инженер недоступен: визитов для пересчёта {len(affected)}',
                    'text':'Проверьте заявки, требующие внимания, и подтвердите новый план после расчёта.',
                    'request_ids':affected})
        self.batches.clear()
        self.history.append({'version':self.version,'event':{'id':period['id'],'type':'unavailability_report',
            'time':self.time,'engineer_id':engineer_id,'start_date':period['start_date'],
            'start_time':period['start_time'],'end_date':period['end_date'],
            'end_time':period['end_time'],'reason':period['reason']}})
        self._refresh_fact_inventory()
        self._save_view('Инженер сообщил период недоступности')
        return self.state()

    def return_from_absence(self,payload):
        candidate=deepcopy(self)
        result=candidate._return_from_absence(payload)
        self.__dict__.update(candidate.__dict__)
        return result

    def _return_from_absence(self,payload):
        allowed={'version','engineer_id','report_id','unavailable_fact_id','time','location_id'}
        if set(payload)-allowed:raise ValueError('Неизвестные поля возвращения')
        if type(payload.get('version')) is not int or payload['version']!=self.version:
            raise ValueError('Устаревшая версия плана')
        eid=payload.get('engineer_id')
        if not isinstance(eid,str) or eid not in self.initial_engineers:
            raise ValueError('Неизвестный инженер')
        now=payload.get('time')
        if type(now) is not int or not self.time<=now<1440:
            raise ValueError('Укажите время возвращения не раньше текущего времени плана')
        location=payload.get('location_id')
        if not isinstance(location,str) or location not in (set(self.scenario.points)|set(self.geo_locations)):
            raise ValueError('Укажите подтверждённую текущую точку')
        clock=f'{now//60:02}:{now%60:02}'
        instant=stamp(self.scenario.date,clock)
        report_id=payload.get('report_id')
        period=None
        unavailable_fact_id=None
        return_info={'date':self.scenario.date,'time':clock,'location_id':location,
                     'recorded_at':datetime.now(timezone.utc).isoformat(),'source':'engineer'}
        if report_id is not None:
            if payload.get('unavailable_fact_id') is not None:
                raise ValueError('Укажите один источник недоступности')
            period=next((item for item in self.unavailability if item['id']==report_id
                         and item['engineer_id']==eid),None)
            if period is None:raise ValueError('Период недоступности не найден')
            if period.get('revoked') or period.get('early_return') or period.get('return_confirmation'):
                raise ValueError('Период уже завершён или отменён')
            start=stamp(period['start_date'],period['start_time'])
            end=stamp(period['end_date'],period['end_time'])
            if instant<start:
                raise ValueError('Время возвращения должно попадать в действующий период')
            if instant<end:
                # Preserve the elapsed absence and the originally promised end.
                period['early_return']={**return_info,
                    'original_end_date':period['end_date'],'original_end_time':period['end_time']}
                period.update(end_date=self.scenario.date,end_time=clock)
            else:
                blocks=shift_blocks([period],eid,self.scenario.date,self._availability_shift(eid))
                if availability_at(project_availability(blocks,self.fact_log,eid),now)!='awaiting_return':
                    raise ValueError('Период уже завершён; подтверждение возвращения не требуется')
                period['return_confirmation']=return_info
            marker=period['id']
        else:
            unavailable=self.statuses.get(eid,{})
            if unavailable.get('status')!='unavailable':
                raise ValueError('Нет действующей недоступности; обновите смену')
            unavailable_fact_id=unavailable.get('id') or next((fact['id'] for fact in reversed(self.fact_log)
                if fact['engineer_id']==eid and fact['status']=='unavailable' and fact['time']==unavailable['time']),None)
            if not unavailable_fact_id:raise ValueError('Сообщение о недоступности не найдено; обновите смену')
            expected=payload.get('unavailable_fact_id')
            if expected is not None and expected!=unavailable_fact_id:
                raise ValueError('Сообщение о недоступности изменилось; обновите смену')
            marker='return:'+unavailable_fact_id
        # A return from one reason cannot silently override another active period.
        blocks=shift_blocks(self.unavailability,eid,self.scenario.date,self._availability_shift(eid))
        if availability_at(project_availability(blocks,self.fact_log,eid),now)=='absent':
            raise ValueError('Действует другой период недоступности; обновите смену')
        from .fact_journal import record
        fact=record(self,{'version':self.version,'engineer_id':eid,'status':'available',
                         'time':now,'location_id':location,'source':'engineer',
                         'comment':'Возвращение после недоступности'})
        if marker not in self.pending_report_ids:self.pending_report_ids.append(marker)
        self.version+=1;self.needs_replanning=True;self.batches.clear()
        event={'id':'return:'+marker,'type':'engineer_return','time':now,
               'engineer_id':eid,'pending_report_id':marker}
        if period:event['report_id']=period['id']
        if unavailable_fact_id:event['unavailable_fact_id']=unavailable_fact_id
        self.history.append({'version':self.version,'fact':fact,'event':event})
        self.dispatch_notifications.append({
            'id':uuid4().hex,'kind':'replan_required','version':self.version,
            'time':datetime.now(timezone.utc).isoformat(),
            'title':self.initial_engineers[eid].name+' снова доступен',
            'text':'Возвращение в '+clock+'. Пересчитайте план.',
            'engineer_id':eid,'report_id':period['id'] if period else None,
            'request_ids':self.pending_reassignment_ids(eid)})
        self._refresh_fact_inventory()
        self._save_view('Инженер вернулся; требуется пересчёт')
        return self.state()

    def return_pending_replan(self,engineer_id):
        """Both dated and open-ended returns await a new approved assignment."""
        pending=set(self.pending_report_ids)
        if any(item['engineer_id']==engineer_id and item['id'] in pending and
               (item.get('early_return') or item.get('return_confirmation')) and not item.get('revoked')
               for item in self.unavailability):return True
        return any(item.get('event',{}).get('type')=='engineer_return' and
                   item['event'].get('engineer_id')==engineer_id and
                   item['event'].get('pending_report_id') in pending for item in self.history)

    def decline_assignment(self,payload):
        candidate=deepcopy(self)
        result=candidate._decline_assignment(payload)
        self.__dict__.update(candidate.__dict__)
        return result

    def _decline_assignment(self,payload):
        if set(payload)-{'version','engineer_id','request_id','time','comment'}:
            raise ValueError('Неизвестные поля отказа от выезда')
        if type(payload.get('version')) is not int or payload['version']!=self.version:
            raise ValueError('Устаревшая версия плана')
        eid=payload.get('engineer_id');key=payload.get('request_id');now=payload.get('time')
        if eid not in self.initial_engineers:raise ValueError('Неизвестный инженер')
        if type(now) is not int or not self.time<=now<1440:raise ValueError('Недопустимое время отказа')
        comment=payload.get('comment')
        if not isinstance(comment,str) or not 3<=len(comment.strip())<=1000:
            raise ValueError('Укажите причину отказа от выезда для диспетчера')
        if any(f.get('request_id')==key and f.get('engineer_id')==eid and
               f['status'] in ('traveling','working','interrupted','completed','completion_pending')
               for f in self.fact_log):
            raise ValueError('Выезд уже начат: сообщите фактическое прерывание или недоступность')
        visits=[v for route in self.plan.routes if route.engineer_id==eid for v in route.visits if v.request_id==key]
        visits += [v for route in self.frozen if route['engineer_id']==eid for v in route['visits'] if v['request_id']==key]
        if not visits:raise ValueError('Заявка больше не назначена этому инженеру')
        if self.statuses.get(eid,{}).get('request_id')==key and self.statuses[eid]['status'] in ('traveling','working'):
            raise ValueError('Сначала сообщите диспетчеру об остановке текущего выезда')
        if any(item['request_id']==key and item['engineer_id']==eid and not item.get('revoked')
               for item in self.declined_assignments):
            raise ValueError('Этот выезд уже передан на перепланирование')
        report={'id':uuid4().hex,'engineer_id':eid,'request_id':key,'time':now,
            'comment':comment.strip(),'source':'engineer','recorded_at':datetime.now(timezone.utc).isoformat(),
            'exclusion_added':eid not in self.catalog[key].excluded_engineer_ids}
        self.declined_assignments.append(report);self.pending_report_ids.append(report['id'])
        self.time=now;self.version+=1;self.needs_replanning=True;self.batches.clear()
        self.dispatch_notifications.append({'id':uuid4().hex,'kind':'replan_required',
            'time':datetime.now(timezone.utc).isoformat(),'version':self.version,
            'title':'Инженер отказался от выезда: нужен пересчёт',
            'text':'Заявка сохранена и ожидает переназначения диспетчером.',
            'request_ids':[key]})
        self.history.append({'version':self.version,'event':{'id':report['id'],'type':'declined_assignment',
            'time':now,'engineer_id':eid,'request_id':key}})
        self._refresh_fact_inventory();self._save_view('Выезд передан в пул перепланирования')
        return self.state()

    def pending_reassignment_ids(self,engineer_id=None):
        pending=set(self.pending_report_ids)
        declines={(item['engineer_id'],item['request_id']) for item in self.declined_assignments
                  if item['id'] in pending and not item.get('revoked')}
        periods=[item for item in self.unavailability if item['id'] in pending and not item.get('revoked')]
        returning={eid for eid in self.initial_engineers if self.return_pending_replan(eid)}
        done={f['request_id'] for f in self.fact_log if f['status'] in ('completed','completion_pending')}
        result=set()
        routes=[(r.engineer_id,[v.model_dump() for v in r.visits]) for r in self.plan.routes]
        routes += [(r['engineer_id'],r['visits']) for r in self.frozen]
        for eid,visits in routes:
            if engineer_id is not None and eid!=engineer_id:continue
            blocks=[block for (block,item) in shift_blocks(periods,eid,self.scenario.date,
                    self._availability_shift(eid))]
            for visit in visits:
                key=visit['request_id']
                if key in done:continue
                if eid in returning or (eid,key) in declines or any(visit['end']>start for start,_ in blocks):
                    result.add(key)
                elif self.needs_replanning and self.statuses.get(eid,{}).get('status')=='unavailable':
                    result.add(key)
        return sorted(result)

    def assignment_summary(self):
        """Current residual assignments, excluding reported invalid promises.

        Keep the approved routes intact for comparison and replanning. A visit
        on an unavailable engineer is not counted as an effective assignment.
        New JSON arrivals are separate until they enter a calculated plan.
        """
        active={request.id for request in self.scenario.requests}
        planned={visit.request_id for route in self.plan.routes for visit in route.visits} & active
        pending=set(self.pending_reassignment_ids()) & active
        missing=active-planned
        attention=missing|pending
        return {'total':len(active),'assigned':len(active-attention),
                'requires_assignment':len(attention),'awaiting_replan':len(pending),
                'unassigned':len(missing), 'pending_request_ids':sorted(pending)}

    def replanning_pool(self):
        pending=set(self.pending_reassignment_ids())
        owners={v.request_id:r.engineer_id for r in self.plan.routes for v in r.visits}
        owners.update({v['request_id']:r['engineer_id'] for r in self.frozen for v in r['visits']})
        previous_owners={}
        for view in self.views.values():
            for route in view['plans'][0]['routes']:
                previous_owners.update({visit['request_id']:route['engineer_id'] for visit in route['visits']})
            for route in view['operations'].get('frozen',[]):
                previous_owners.update({visit['request_id']:route['engineer_id'] for visit in route['visits']})
        visits={v.request_id:v.model_dump() for r in self.plan.routes for v in r.visits}
        visits.update({v['request_id']:v for r in self.frozen for v in r['visits']})
        unassigned={u.request_id:u for u in self.plan.unassigned}
        pending_reports=set(self.pending_report_ids)
        declines={(item['engineer_id'],item['request_id']):item for item in self.declined_assignments
                  if item['id'] in pending_reports and not item.get('revoked')}
        periods={eid:[item for item in self.unavailability if item['engineer_id']==eid and
                      item['id'] in pending_reports and not item.get('revoked')] for eid in self.initial_engineers}
        rows=[]
        for key in sorted(pending|set(unassigned)):
            if key not in self.catalog:continue
            eid=owners.get(key)
            report=declines.get((eid,key))
            visit=visits.get(key)
            period=next((item for item in periods.get(eid,[]) if item.get('early_return') or item.get('return_confirmation') or (visit and any(
                visit['end']>block[0] for block,_ in shift_blocks(
                    [item],eid,self.scenario.date,self._availability_shift(eid))))),None)
            detail={'report_type':'decline','reason':'Отказ от выезда',
                    'report_id':report['id'],'comment':report['comment']} if report else (
                {'report_type':'unavailability','reason':('Досрочное возвращение' if period.get('early_return') else 'Инженер вернулся')
                 if period.get('early_return') or period.get('return_confirmation') else period['reason_label'],
                 'report_id':period['id'],'comment':period['comment'],'start_date':period['start_date'],
                 'start_time':period['start_time'],'end_date':period['end_date'],
                 'end_time':period['end_time']} if period and key in pending else
                {'report_type':'fact' if key in pending else None,
                 'reason':unassigned[key].reason if key in unassigned else (
                     'Инженер вернулся; требуется пересчёт' if eid in self.initial_engineers and self.return_pending_replan(eid)
                     else 'Инженер сообщил недоступность через статус')})
            rows.append({'request_id':key,'address':self.catalog[key].address,
                'engineer_id':eid,'status':'awaiting_replan' if key in pending else 'unassigned',
                'previous_engineer_id':previous_owners.get(key) if eid is None else None,
                'code':unassigned[key].code if key in unassigned else None,
                'windows':[window.model_dump() for window in self.catalog[key].windows],
                'duration_minutes':self.catalog[key].duration_minutes,
                **detail})
        return rows

    def revoke_engineer_report(self,payload):
        candidate=deepcopy(self)
        result=candidate._revoke_engineer_report(payload)
        self.__dict__.update(candidate.__dict__)
        return result

    def _revoke_engineer_report(self,payload):
        if set(payload)-{'version','report_id','reason','location_id'}:
            raise ValueError('Неизвестные поля исправления сообщения')
        if type(payload.get('version')) is not int or payload['version']!=self.version:
            raise ValueError('Устаревшая версия плана')
        reason=payload.get('reason')
        if not isinstance(reason,str) or not 3<=len(reason.strip())<=1000:
            raise ValueError('Укажите причину исправления для журнала')
        report_id=payload.get('report_id')
        period=next((item for item in self.unavailability if item['id']==report_id),None)
        decline=next((item for item in self.declined_assignments if item['id']==report_id),None)
        item=period or decline
        if item is None or item.get('revoked'):
            raise ValueError('Действующее сообщение не найдено')
        eid=item['engineer_id']
        now=self.time
        started=False
        if period:
            interval=day_interval(period,self.scenario.date)
            started=interval is not None and interval[0]<=now and bool(shift_blocks(
                [period],eid,self.scenario.date,self._availability_shift(eid)))
            location=payload.get('location_id')
            if started and location not in (set(self.scenario.points)|set(self.geo_locations)):
                raise ValueError('Для досрочного возвращения укажите подтверждённую текущую точку')
            if not started and location is not None and location not in (
                    set(self.scenario.points)|set(self.geo_locations)):
                raise ValueError('Неизвестная текущая точка')
        elif payload.get('location_id') is not None:
            raise ValueError('Для отзыва отказа точка возвращения не требуется')
        item.update(revoked=True,revoked_reason=reason.strip(),
                    revoked_at=datetime.now(timezone.utc).isoformat(),revoked_by='dispatcher')
        self.pending_report_ids=[key for key in self.pending_report_ids if key!=report_id]
        if period:
            if started:
                from .fact_journal import record
                record(self,{'version':self.version,'engineer_id':eid,'status':'available',
                    'time':now,'location_id':payload['location_id'],
                    'comment':'Исправление периода: '+reason.strip()})
        elif decline['exclusion_added'] and not any(other is not decline and
                not other.get('revoked') and other['engineer_id']==eid and
                other['request_id']==decline['request_id'] for other in self.declined_assignments):
            key=decline['request_id']
            def restore_request(request):
                return request.model_copy(update={'excluded_engineer_ids':[
                    value for value in request.excluded_engineer_ids if value!=eid]})
            if key in self.catalog:self.catalog[key]=restore_request(self.catalog[key])
            self.scenario=self.scenario.model_copy(update={'requests':[
                restore_request(request) if request.id==key else request
                for request in self.scenario.requests]})
        self.version+=1;self.needs_replanning=True;self.batches.clear()
        self.history.append({'version':self.version,'event':{'id':'revoke:'+report_id,
            'type':'engineer_report_revoked','time':now,'engineer_id':eid,
            'report_id':report_id,'report_type':'unavailability' if period else 'decline',
            'reason':reason.strip()}})
        self._refresh_fact_inventory()
        self._save_view('Исправлено сообщение инженера; требуется пересчет')
        return self.state()


    def _refresh_fact_inventory(self):
        result=[]
        recorded={f['request_id'] for f in self.fact_log if f['status'] in ('completed','completion_pending','interrupted')}
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
                    target=reserved if f['status']=='completion_pending' else consumed
                    for k in target:target[k]+=f['consumed'][k]
            if fact.get('status')=='working':
                key=fact['request_id']
                for k in reserved:reserved[k]+=max(0,getattr(self.catalog[key].equipment,k)-sum(f['consumed'][k] for f in self.fact_log if f.get('request_id')==key))
            available={k:getattr(initial,k)-consumed[k]-reserved[k] for k in consumed}
            if any(v<0 for v in available.values()):raise ValueError('Фактический расход/резерв превышает запас')
            active_absence=any(start<=self.time<end for (start,end),_ in shift_blocks(
                self.unavailability,eid,self.scenario.date,self._availability_shift(eid)))
            current_engineer=next((e for e in self.scenario.engineers if e.id==eid),None)
            result.append({'engineer_id':eid,'consumed':consumed,'reserved':reserved,'available':available,
                'dispatchable':not active_absence and fact.get('status')!='unavailable' and
                current_engineer is not None and not current_engineer.off_duty and
                (self.execution_mode!='manual' or eid in self.statuses) and current_engineer.shift.end>self.time})
        self.inventory=result

    def mode(self):
        return 'manual' if self.execution_mode=='manual' else 'mixed' if self.statuses else 'simulation'

    def receive_requests(self,payload):
        """Accept a bounded, idempotent JSON batch without changing the plan."""
        from .intake import normalize
        if not isinstance(payload,dict) or set(payload)-{'requests','request'}:
            raise ValueError('Ожидается JSON с полем requests')
        rows=payload.get('requests',payload.get('request'))
        if isinstance(rows,dict):rows=[rows]
        if not isinstance(rows,list) or not 1<=len(rows)<=100:
            raise ValueError('Передайте от 1 до 100 заявок')
        candidate=deepcopy(self)
        existing={item['id']:item for item in candidate.incoming_requests}
        known={r.provenance.get('event') for r in candidate.catalog.values()}
        added=[];seen=set()
        for raw in rows:
            if isinstance(raw,dict) and raw.get('id') in existing and 'time' not in raw:
                raw={**raw,'time':existing[raw['id']]['event']['time']}
            item=normalize(candidate,raw,allow_past=isinstance(raw,dict) and raw.get('id') in existing)
            key=item['id']
            if key in seen:raise ValueError('Повтор ID внутри пакета: '+key)
            seen.add(key)
            if key in existing:
                old=existing[key]
                if old['event']!=item['event'] or old['address']!=item['address']:
                    raise ValueError('Повтор ID с другими полями: '+key)
                continue
            if key in known or 'request:'+key in candidate.catalog or 'emergency:'+key in candidate.catalog:
                raise ValueError('Заявка с таким ID уже есть в плане: '+key)
            candidate.incoming_requests.append(item);existing[key]=item;added.append(item)
        if added:
            candidate.time=max(candidate.time,*(item['event']['time'] for item in added))
            candidate.version+=1;candidate.needs_replanning=True;candidate.batches.clear()
            candidate.dispatch_notifications.append({'id':uuid4().hex,'kind':'incoming',
                'time':datetime.now(timezone.utc).isoformat(),'version':candidate.version,
                'title':f'Поступило заявок: {len(added)}',
                'text':'Новые заявки ожидают распределения.', 'request_ids':[item['id'] for item in added]})
            candidate._save_view(f'Поступило заявок: {len(added)}')
        self.__dict__.update(candidate.__dict__)
        return {'added':len(added),'duplicates':len(rows)-len(added),'state':self.state()}

    def set_planning_settings(self,payload):
        if not isinstance(payload,dict) or set(payload)-{'version','mode','every_n_requests','every_n_minutes'}:
            raise ValueError('Некорректные настройки режима')
        if type(payload.get('version')) is not int or payload['version']!=self.version:
            raise ValueError('Устаревшая версия плана')
        mode=payload.get('mode')
        if mode not in ('manual','autopilot'):
            raise ValueError('Выберите ручной режим или автопилот')
        count=payload.get('every_n_requests')
        minutes=payload.get('every_n_minutes')
        if count is not None and (type(count) is not int or not 1<=count<=100):
            raise ValueError('Порог по заявкам должен быть от 1 до 100')
        if minutes is not None and (type(minutes) is not int or not 1<=minutes<=240):
            raise ValueError('Интервал должен быть от 1 до 240 минут')
        if mode=='autopilot' and count is None and minutes is None:
            raise ValueError('Для автопилота укажите хотя бы один порог')
        self.planning_settings={'mode':mode,'every_n_requests':count,'every_n_minutes':minutes}
        self.autopilot_last_failure_at=None
        self.version+=1;self.batches.clear()
        self.history.append({'version':self.version,'event':{'id':'planning-mode:'+str(self.version),
            'type':'planning_mode','time':self.time},'settings':deepcopy(self.planning_settings)})
        self.dispatch_notifications.append({'id':uuid4().hex,'kind':'planning_mode',
            'time':datetime.now(timezone.utc).isoformat(),'version':self.version,
            'title':'Автопилот включён' if mode=='autopilot' else 'Включён ручной режим',
            'text':(f'Порог: {count} новых заявок' if count is not None else '')+
                   ((' или ' if count is not None else '')+f'{minutes} минут ожидания' if minutes is not None else '')
                   if mode=='autopilot' else 'Новые заявки ожидают решения диспетчера.'})
        self._save_view('Режим планирования: '+('автопилот' if mode=='autopilot' else 'ручной'))
        return self.state()

    def autopilot_due(self,now=None):
        if self.planning_settings['mode']!='autopilot':return False
        pending=[item for item in self.incoming_requests if item['status']=='new']
        if not pending:return False
        now=now or datetime.now(timezone.utc)
        if self.autopilot_last_failure_at and (
                now-datetime.fromisoformat(self.autopilot_last_failure_at)).total_seconds()<60:
            return False
        count=self.planning_settings.get('every_n_requests')
        if count is not None and len(pending)>=count:return True
        minutes=self.planning_settings.get('every_n_minutes')
        if minutes is None:return False
        oldest=min(datetime.fromisoformat(item['received_at']) for item in pending)
        return (now-oldest).total_seconds()>=minutes*60

    def record_autopilot_failure(self,message):
        self.autopilot_last_failure_at=datetime.now(timezone.utc).isoformat()
        self.version+=1;self.batches.clear()
        self.dispatch_notifications.append({'id':uuid4().hex,'kind':'auto_error',
            'time':self.autopilot_last_failure_at,'version':self.version,
            'title':'Автопилот не применил новый план',
            'text':message,'request_ids':[item['id'] for item in self.incoming_requests if item['status']=='new']})
        self._save_view('Автопилот: требуется внимание диспетчера')

    def set_mode(self,payload):
        if payload.get('version')!=self.version:raise ValueError('Устаревшая версия')
        if payload.get('mode') not in ('manual','simulation'):raise ValueError('Неизвестный режим')
        self.execution_mode=payload['mode'];self.version+=1;self.needs_replanning=True;self.batches.clear()
        self.history.append({'version':self.version,'event':{'id':f'mode:{self.version}','type':'mode','time':self.time},'mode':self.execution_mode})
        self._refresh_fact_inventory();self._save_view('Изменен режим учета')
        return self.state()

    def request_labels(self):
        labels={key:f'Авария №{i+1}' for i,key in enumerate(k for k in self.catalog if k.startswith('emergency:'))}
        counts={kind:0 for kind in WORK_KINDS}
        for key,request in self.catalog.items():
            kind=request.provenance.get('work_kind')
            if key.startswith('request:') and kind in WORK_KINDS:
                counts[kind]+=1
                labels[key]=f'{WORK_KINDS[kind][0]} №{counts[kind]}'
        for item in self.incoming_requests:
            event=item['event'];key=('emergency:' if event['type']=='emergency' else 'request:')+item['id']
            labels.setdefault(key,('Аварийная заявка ' if event['type']=='emergency' else 'Заявка ')+item['id'])
        return labels

    def fact_requests(self):
        owners={v['request_id']:r['engineer_id'] for r in self.frozen for v in r['visits']}
        owners.update({v.request_id:r.engineer_id for r in self.plan.routes for v in r.visits})
        done={f['request_id'] for f in self.fact_log if f['status'] in ('completed','completion_pending')}
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
        last_applied=next((row for row in reversed(self.history) if row.get('candidate_id')),None)
        return {'pending_completions':pending_completions(self),'historical_requests':self.historical_requests(),'fact_journal':deepcopy(self.fact_log),'fact_audit':deepcopy(self.fact_audit),'execution_mode':self.execution_mode,'version':self.version,'time':self.time,'region':self.scenario.region,'date':self.scenario.date,'approximate_locations':sum(p.quality=='approximate' for p in self.geography.locations.values()) if self.geography else 0,'simulation':not bool(self.statuses),'geography_mode':'road' if self.geography else 'synthetic',
            'mode':self.mode(),'statuses':deepcopy(list(self.statuses.values())),'needs_replanning':self.needs_replanning,
            'last_applied':({'version':last_applied['version'],'automatic':last_applied.get('automatic'),
                'incoming_count':len(last_applied.get('incoming_ids',[])),
                'changed_count':len(last_applied.get('changed_request_ids',[]))} if last_applied else None),
            'unavailability':deepcopy(self.unavailability),'declined_assignments':deepcopy(self.declined_assignments),
            'replanning_pool':self.replanning_pool(),
            'assignment_summary':self.assignment_summary(),
            'planning_settings':deepcopy(self.planning_settings),
            'incoming_requests':deepcopy(self.incoming_requests),
            'dispatch_notifications':deepcopy(self.dispatch_notifications[-100:]),
            'decision_log':deepcopy(self.decision_log[-300:]),
            'excluded_engineer_ids':sorted(self.excluded_engineers()),
            'locations':[{'id':k,'label':self.catalog[k].address if k in self.catalog else ('Офис' if k=='office' else (self.geo_locations[k].original_address or self.geo_locations[k].label) if k in self.geo_locations else k)} for k in (self.geo_locations if self.geography else self.scenario.points) if not k.startswith(('start:','position:','resume:','closed:','observed:')) and (not k.startswith(('emergency:','request:')) or k in self.catalog)],
            'versions':[{'version':v,'time':p['operations']['time'],'label':p['operations']['label']} for v,p in self.views.items()],
            'requests':[{'id':r.id,'address':r.address,'priority':r.priority,'duration_minutes':r.duration_minutes} for r in self.scenario.requests],
            'engineers':[{'id':e.id,'name':e.name,'off_duty':next((current.off_duty for current in self.scenario.engineers if current.id==e.id),e.off_duty),
                          'initially_off_duty':e.off_duty,'shift_start':self._availability_shift(e.id).start,
                          'shift_end':self._availability_shift(e.id).end} for e in self.initial_engineers.values()],
            'fact_requests':self.fact_requests(),'request_labels':self.request_labels(),
            'plan':self.plan.model_dump(mode='json'),'history':deepcopy(self.history),
            'frozen':deepcopy(self.frozen),'inventory':deepcopy(self.inventory)}

    def excluded_engineers(self):
        """Approved exclusions survive later replans and factual projections.

        A newer explicit 'available' fact returns a worker to planning.
        The roster and mobile identity are never removed.
        """
        excluded={}
        for entry in self.history:
            event=entry['event']
            if event['type']=='unavailable':
                for eid in event.get('engineer_ids') or [event.get('engineer_id')]:
                    if eid in self.initial_engineers:excluded[eid]=event['time']
        for fact in self.fact_log:
            eid=fact['engineer_id']
            if fact['status']=='available' and eid in excluded and fact['time']>excluded[eid]:
                excluded.pop(eid)
        return set(excluded)

    def _residual(self,event,allow_redirect,preserve=True,activation_ids=(),activation_window=None):
        s=self.scenario
        now=event.time
        if self.activated_off_duty_windows:
            s=s.model_copy(update={'engineers':[e.model_copy(update={
                'shift':self.activated_off_duty_windows[e.id]})
                if e.id in self.activated_off_duty_windows and not e.off_duty
                else e for e in s.engineers]})
        revoked_workers={item['engineer_id'] for item in self.unavailability if item.get('revoked')}
        if revoked_workers:
            s=s.model_copy(update={'engineers':[e.model_copy(update={'shift':TimeWindow(
                start=min(e.shift.start,self.initial_engineers[e.id].shift.start),
                end=self.initial_engineers[e.id].shift.end)})
                if e.id in revoked_workers and not (
                    self.initial_engineers[e.id].off_duty and not e.off_duty)
                else e for e in s.engineers]})
        if activation_ids:
            selected=set(activation_ids)
            s=s.model_copy(update={'engineers':[e.model_copy(update={
                'off_duty':False,'shift':activation_window or e.shift})
                if e.id in selected else e for e in s.engineers]})
        unavailable=self.excluded_engineers()
        if event.type=='unavailable':
            unavailable.update(event.engineer_ids or [event.engineer_id])
        declined={(item['engineer_id'],item['request_id']) for item in self.declined_assignments
                  if not item.get('revoked')}
        blocks={eid:shift_blocks(self.unavailability,eid,s.date,
                    current.shift if self.initial_engineers[eid].off_duty and not current.off_duty
                    else self.initial_engineers[eid].shift)
                for eid,current in ((e.id,e) for e in s.engineers)}
        # A single route has one continuous shift. Before a future absence we
        # plan only up to its start; when the interval is active we can plan
        # again from its end. This never puts travel or work inside an absence.
        current_blocks={eid:next(((start,end,item) for (start,end),item in items
                                  if end>now),None) for eid,items in blocks.items()}
        pending_periods=set(self.pending_report_ids)
        impact_blocks={eid:next(((start,end,item) for (start,end),item in items
                                 if end>now or item['id'] in pending_periods),None)
                       for eid,items in blocks.items()}
        absent_now={eid for eid,block in current_blocks.items()
                    if block is not None and block[0]<=now<block[1]}
        actual_started={(f['engineer_id'],f['request_id']) for f in self.fact_log
                        if f.get('request_id') and f['status'] in
                        ('working','completed','completion_pending')}
        if self.execution_mode=='manual':
            s=s.model_copy(update={'engineers':[e.model_copy(update={'shift':TimeWindow(start=min(now,e.shift.end),end=min(now,e.shift.end))}) if e.id not in self.statuses else e for e in s.engineers]})
        for route in self.frozen:
            if route['engineer_id'] in self.statuses:continue
            for visit in route['visits']:
                if visit['start']<=now<visit['end']:
                    if route['engineer_id'] in unavailable or (
                            route['engineer_id'] in absent_now and
                            (route['engineer_id'],visit['request_id']) in actual_started):
                        name=self.initial_engineers[route['engineer_id']].name
                        raise ValueError(name+': выполняет закрепленную работу. Сначала отметьте завершение или прерывание в разделе «Бригады».')
        routes={r.engineer_id:r for r in self.plan.routes if self.execution_mode!='manual' or r.engineer_id in self.statuses}
        prior_frozen=deepcopy(self.frozen)
        if self.statuses:s,routes,prior_frozen=apply_observations(self,s,routes,prior_frozen,now,
            allow_redirect=allow_redirect and event.type=='emergency')
        if event.type=='new_request':
            # An observation may replace an old route with its current trip.
            # Keep that trip's promised service start before freezing it too.
            promised={v['request_id']:v['start'] for r in self.frozen for v in r['visits']}
            promised.update({v.request_id:v.start for r in self.plan.routes for v in r.visits})
            for eid,fact in self.statuses.items():
                if fact['status']!='traveling' or eid not in routes:continue
                visits=[]
                for visit in routes[eid].visits:
                    start=promised.get(visit.request_id)
                    if start is None or visit.arrival>start:raise ValueError(PRESERVATION_ERROR)
                    visits.append(visit.model_copy(update={'start':start,'end':start+visit.end-visit.start}))
                routes[eid]=routes[eid].model_copy(update={'visits':visits})
        points=dict(s.points)
        if self.geography:points={k:Point(x_km=v.lon,y_km=v.lat) for k,v in self.geo_locations.items()}
        restored_jobs=[]
        updated_engineers={e.id:e for e in s.engineers}
        for route in prior_frozen:
            released=[]
            for visit in list(route['visits']):
                eid=route['engineer_id']
                block=impact_blocks.get(eid)
                can_release=((event.type=='emergency' and allow_redirect) or eid in unavailable or
                    (eid,visit['request_id']) in declined or
                    (block is not None and visit['end']>block[0]))
                if not can_release:continue
                factual_release=((eid,visit['request_id']) not in actual_started and
                    ((block is not None and visit['end']>block[0]) or
                     (eid,visit['request_id']) in declined))
                if visit['start']<=now and not factual_release:continue
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
        declined_by_request={}
        for eid,key in declined:declined_by_request.setdefault(key,set()).add(eid)
        requests=[]
        for request in s.requests+restored_jobs:
            excluded=declined_by_request.get(request.id,set())
            requests.append(request.model_copy(update={
                'excluded_engineer_ids':sorted(set(request.excluded_engineer_ids)|excluded)})
                if excluded else request)
        s=s.model_copy(update={'requests':requests,'engineers':list(updated_engineers.values())})
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
                freeze=v.end<=now or in_work or (on_road and not (allow_redirect and event.type=='emergency'))
                block=impact_blocks.get(e.id)
                if ((e.id,v.request_id) in declined or
                        (block is not None and v.end>block[0])):
                    if (e.id,v.request_id) in actual_started:
                        if in_work:raise ValueError(self.initial_engineers[e.id].name+': выполняет работу. Сначала отметьте завершение или прерывание в разделе «Бригады».')
                    else:freeze=False
                # A failed engineer cannot finish the trip. Work already started
                # needs an explicit actual-state update, not a fictional finish.
                if e.id in unavailable:
                    if in_work:raise ValueError(self.initial_engineers[e.id].name+': выполняет работу. Сначала отметьте завершение или прерывание в разделе «Бригады».')
                    if v.end>now:freeze=False
                if freeze:
                    origins[v.request_id]={'location':location,'departure':departure}
                    locked.add(v.request_id);keep.append({**v.model_dump(mode='json'),'consumed_equipment':jobs[v.request_id].equipment.model_dump()})
                    for device in stock:stock[device]-=getattr(jobs[v.request_id].equipment,device)
                    location=v.request_id
                    ready=max(ready,v.end)
                else:
                    if on_road and e.id not in unavailable:
                        observation=self.statuses.get(e.id,{})
                        if e.id in absent_now:
                            location=block[2].get('location_id') or location
                        elif observation.get('status')=='traveling' and observation.get('time')==now:
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
            if e.id in unavailable:continue
            block=current_blocks.get(e.id)
            engineer_blocks=blocks.get(e.id,[])
            shift_end=e.shift.end
            scheduled_end=(activation_window.end if e.id in activation_ids and activation_window
                           else self._availability_shift(e.id).end)
            availability=availability_at(
                project_availability(engineer_blocks,self.fact_log,e.id),now)
            if any(end<=now for (start,end),_ in engineer_blocks):
                shift_end=max(shift_end,scheduled_end)
            if block is not None:
                start,end,item=block
                if now<start:
                    shift_end=min(shift_end,start)
                else:
                    ready=max(ready,end)
                    # A current-location declaration prevents planning a
                    # fictional trip from the last published stop after an
                    # active absence. It is deliberately required at intake.
                    if item.get('location_id'):
                        location=item['location_id']
                    shift_end=max(shift_end,scheduled_end)
                    following=next(((a,b) for (a,b),_ in engineer_blocks if a>=end),None)
                    if following:shift_end=min(shift_end,following[0])
            elif engineer_blocks and now>=engineer_blocks[-1][0][1]:
                shift_end=max(shift_end,scheduled_end)
            if (availability=='awaiting_return' or
                    (block is not None and block[0]<=now and not block[2].get('location_id'))):
                # A future leave was reported without a start/resume location.
                # Returning to the route requires an explicit available fact;
                # never invent a drive from a stale position.
                shift_end=min(shift_end,now)
            ready=min(ready,shift_end)  # Retain identity for later release of a frozen trip.
            alias=f'start:{self.version}:{e.id}:{now}'
            points[alias]=points[location]
            self._alias_location(alias,location)
            engineers.append(e.model_copy(update={'start_location':alias,'shift':TimeWindow(start=ready,end=shift_end),'stock':Equipment(**stock)}))
        if event.type=='cancel' and event.request_id in locked:
            raise ValueError('Нельзя отменить завершенную или закрепленную текущую заявку')
        remaining=[r for r in s.requests if r.id not in locked and not (event.type=='cancel' and r.id==event.request_id)]
        arrivals=([(event,None)] if event.type in ('emergency','new_request') else [])+[
            (Event.model_validate(item['event']),item) for item in self.incoming_requests
            if item['status']=='new']
        for arrival,incoming in arrivals:
            if arrival.anchor_id=='office':source_address=s.office_address
            elif incoming is not None:source_address=incoming['address']
            elif arrival.anchor_id in self.catalog:source_address=self.catalog[arrival.anchor_id].address
            else:source_address=self.geo_locations[arrival.anchor_id].original_address or self.geo_locations[arrival.anchor_id].label
            key=('emergency:' if arrival.type=='emergency' else 'request:')+arrival.id
            if key in self.catalog:raise ValueError('ID новой заявки уже существует')
            if key in points:raise ValueError('ID заявки пересекается с дорожной точкой')
            points[key]=points[arrival.anchor_id]
            self._alias_location(key,arrival.anchor_id)
            if arrival.type=='emergency':
                kind,skill,priority='Глобальная проблема',Skill.EMERGENCY,'urgent'
                windows=[TimeWindow(start=arrival.time,end=1440)]
            else:
                kind,skill,priority=WORK_KINDS[arrival.work_kind]
                windows=[TimeWindow(start=arrival.window_start,end=arrival.window_end)]
            remaining.append(Request(id=key,region=s.region,date=s.date,address=source_address,
                locality=self.catalog[arrival.anchor_id].locality if arrival.anchor_id in self.catalog else '',
                bk_type=kind,hd_type='Новая аварийная заявка' if arrival.type=='emergency' else 'Новая обычная заявка',windows=windows,
                duration_minutes=arrival.duration_minutes,skill=skill,priority=priority,
                equipment=Equipment(router=arrival.router,tv_box=arrival.tv_box),
                provenance={'event':arrival.id,'received_at':str(arrival.time),
                    **({'work_kind':arrival.work_kind} if arrival.type=='new_request' else {}),
                    'coordinates':('Дорожная точка: ' if self.geography else 'Условная точка: ')+arrival.anchor_id,
                    **({'source':'json_intake'} if incoming is not None else {})}))
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
                    target=reserved if v.get('status')=='completion_pending' else consumed if v['end']<=now else reserved
                    for device in target:target[device]+=v.get('consumed_equipment',catalog[v['request_id']].equipment.model_dump())[device]
            available={k:getattr(initial,k)-consumed[k]-reserved[k] for k in consumed}
            inventory.append({'engineer_id':eid,'consumed':consumed,'reserved':reserved,'available':available,
                'dispatchable':any(e.id==eid and not e.off_duty and e.shift.end>now for e in engineers)})
        return residual,frozen,inventory

    def preview(self,payload,*,time_limit=1,solver='hybrid'):
        aliases,locations=dict(self.aliases),dict(self.geo_locations)
        try:
            return self._preview(payload,time_limit=time_limit,solver=solver)
        except Exception:
            # Failed draft preparation must not leave selectable addresses or
            # aliases behind in a subsequently persisted working day.
            self.aliases=aliases;self.geo_locations=locations
            raise

    def _preview(self,payload,*,time_limit=1,solver='hybrid'):
        if payload.get('version')!=self.version:raise ValueError('Устаревшая версия плана; обновите состояние')
        event=Event.model_validate(payload.get('event',{}))
        if event.id in {h['event']['id'] for h in self.history}:raise ValueError('Запрещен повтор уже примененного события')
        if event.time<self.time:raise ValueError('Событие не может быть раньше текущего времени')
        if event.type not in ('emergency','new_request','cancel','unavailable','replan'):raise ValueError('Неизвестный тип события')
        ids={r.id for r in self.scenario.requests}
        if event.type=='cancel' and event.request_id not in ids:raise ValueError('Неизвестная или уже завершенная заявка')
        if event.type=='cancel' and any(f.get('request_id')==event.request_id and f['status'] in ('completed','completion_pending') for f in self.fact_log):
            raise ValueError('Нельзя отменить завершенную заявку или заявку, ожидающую подтверждения завершения')
        if event.type in ('emergency','new_request'):
            anchors=set(self.catalog)|set(self.geo_locations)
            if 'office' in self.scenario.points:anchors.add('office')
            if event.anchor_id not in anchors:raise ValueError('Неизвестная точка новой заявки')
        if event.type=='unavailable':
            if event.engineer_id is not None and event.engineer_ids:
                raise ValueError('Укажите engineer_id или engineer_ids, не оба поля')
            selected=event.engineer_ids or ([event.engineer_id] if event.engineer_id is not None else [])
            if not selected:raise ValueError('Выберите хотя бы одного инженера')
            if len(selected)!=len(set(selected)):raise ValueError('Инженеры в списке не должны повторяться')
            if set(selected)-set(self.initial_engineers):raise ValueError('Неизвестный инженер в выбранном районе')
            if set(selected) & self.excluded_engineers():raise ValueError('Один из инженеров уже исключён из планирования; обновите список')
        elif event.engineer_ids:
            raise ValueError('Список инженеров поддерживается только для события недоступности')
        for key in ('allow_redirect','preserve_in_progress'):
            if type(payload.get(key,False)) is not bool:raise ValueError('Ожидается явный переключатель '+key)
        protect_approved=payload.get('protect_approved_assignments',False)
        if type(protect_approved) is not bool:
            raise ValueError('Некорректный режим сохранения утверждённых назначений')
        local_scope=payload.get('local_scope_engineer_ids',[])
        if (not isinstance(local_scope,list) or len(local_scope)>5 or
                any(not isinstance(eid,str) or not eid for eid in local_scope) or len(set(local_scope))!=len(local_scope)):
            raise ValueError('Некорректный локальный контур перепланирования')
        if local_scope and event.type!='replan':raise ValueError('Локальный контур поддерживается только для перепланирования')
        if set(local_scope)-set(self.initial_engineers):raise ValueError('В локальном контуре указан неизвестный инженер')
        activation_ids=payload.get('call_off_duty_engineer_ids',[])
        confirmation=payload.get('call_off_duty_confirmed',False)
        escalation_reason=payload.get('call_off_duty_reason')
        window_start=payload.get('call_off_duty_window_start')
        window_end=payload.get('call_off_duty_window_end')
        if (window_start is None)!=(window_end is None):
            raise ValueError('Для вызова с выходного укажите обе границы окна выхода')
        if window_start is not None and (type(window_start) is not int or type(window_end) is not int or
                not 0<=window_start<window_end<=1440):
            raise ValueError('Некорректное окно выхода сотрудника с выходного')
        activation_window=TimeWindow(start=window_start,end=window_end) if window_start is not None else None
        if (not isinstance(activation_ids,list) or len(activation_ids)>5 or
                any(not isinstance(eid,str) or not eid for eid in activation_ids) or
                len(activation_ids)!=len(set(activation_ids))):
            raise ValueError('Некорректный список сотрудников для вызова с выходного')
        if type(confirmation) is not bool:
            raise ValueError('Подтвердите решение о вызове сотрудника с выходного')
        if event.type!='emergency' and (activation_ids or confirmation or escalation_reason or activation_window):
            raise ValueError('Вызов с выходного допускается только как ручная эскалация при аварии')
        if activation_ids:
            if not confirmation or not isinstance(escalation_reason,str) or not 3<=len(escalation_reason.strip())<=1000:
                raise ValueError('Нужно явное подтверждение и причина вызова с выходного')
            current={e.id:e for e in self.scenario.engineers}
            if any(eid not in current or not current[eid].off_duty for eid in activation_ids):
                raise ValueError('Выберите только доступного в районе сотрудника с выходным')
            effective={eid:activation_window or current[eid].shift for eid in activation_ids}
            if activation_window and activation_window.start<event.time:
                raise ValueError('Окно выхода не может начинаться раньше времени аварии')
            if any(effective[eid].end<=event.time for eid in activation_ids):
                raise ValueError('Смена выбранного сотрудника уже закончилась; укажите фактическое окно выхода')
            if any(eid in self.excluded_engineers() or
                    self.statuses.get(eid,{}).get('status')=='unavailable' or
                    any(start<effective[eid].end and end>max(event.time,effective[eid].start) for
                        (start,end),_ in day_blocks(self.unavailability,eid,self.scenario.date))
                    for eid in activation_ids):
                raise ValueError('Нельзя вызвать сотрудника в период подтверждённой недоступности')
        elif confirmation or escalation_reason or activation_window:
            raise ValueError('Выберите сотрудника для явной эскалации')
        s,frozen,inventory=self._residual(event,payload.get('allow_redirect',False),True,
            activation_ids,activation_window)
        travel=self._travel(s)
        ref_routes={r.engineer_id:r for r in self.plan.routes}
        for frozen_route in self.frozen:
            eid=frozen_route['engineer_id']
            visits=[Visit.model_validate({k:v[k] for k in Visit.model_fields}) for v in frozen_route['visits']
                    if event.type!='new_request' or not v.get('fact_id')]
            if eid in ref_routes:
                ref_routes[eid]=ref_routes[eid].model_copy(update={'visits':visits+ref_routes[eid].visits})
            else:
                ref_routes[eid]=Route(engineer_id=eid,visits=visits,distance_m=0,travel_minutes=0,service_minutes=0,loadout=Equipment())
        reference=self.plan.model_copy(update={'routes':list(ref_routes.values())})
        labels=self.request_labels()
        if event.type=='emergency':labels['emergency:'+event.id]=f'Авария №{sum(k.startswith("emergency:") for k in labels)+1}'
        if event.type=='new_request':
            number=sum(r.provenance.get('work_kind')==event.work_kind for r in self.catalog.values())+1
            labels['request:'+event.id]=f'{WORK_KINDS[event.work_kind][0]} №{number}'
        candidates=[]
        focus='emergency:'+event.id if event.type=='emergency' else None
        targets=response_targets(s,focus)
        prior=reference_visits(s,reference)
        available={e.id for e in s.engineers}
        # Every alternative receives the same deterministic random streams, so
        # its risk delta is caused by the plan rather than Monte Carlo noise.
        duration_evidence = confirmed_service_observations(self)
        risk_seed = preview_risk_seed(self, event, payload)
        risk_engine=RiskEngine(
            application_risk_config(risk_seed),
            service_observations=duration_evidence.observations,
            service_group_key=duration_evidence.group_key(self))
        if event.type=='new_request':
            pool=insertion_candidates(s,travel,reference,'request:'+event.id)
        else:
            backend=get_solver(solver)
            pool=[backend.solve(s,travel,time_limit_seconds=time_limit,profile=p,reference=reference,focus_request_id=focus) for p in ('sla','stable','cost')]
        protected=set()
        if event.type=='replan':
            # All residual jobs, including previously unassigned requests and
            # the whole new intake batch, are considered by the solvers above.
            # Their coverage-first objective can nevertheless evict a visit
            # already published to a customer. Offer a validated reference
            # seed also for a manual retry: Stable should be truly stable, and
            # the dispatcher can still explicitly choose an SLA/Cost trade-off.
            seed=preserved_approved_seed(s,travel,reference)
            if seed is not None:
                pool.append(seed)
                protected=set(_assignments(seed)) & set(prior)
        def lost_promises(plan):
            return len(protected-set(_assignments(plan)))
        if protect_approved and event.type=='replan' and protected:
            # Autopilot may trade promises only for a genuinely new emergency,
            # not merely because an old emergency remains on the day plan.
            if pool:
                emergencies={('emergency:'+item['id']) for item in self.incoming_requests
                    if item['status']=='new' and item['event']['type']=='emergency'}
                if emergencies:
                    most=max(len(emergencies & set(_assignments(plan))) for plan in pool)
                    pool=[plan for plan in pool if len(emergencies & set(_assignments(plan)))==most]
                fewest=min(lost_promises(plan) for plan in pool)
                pool=[plan for plan in pool if lost_promises(plan)==fewest]
        for profile in ('sla','stable','cost'):
            profile_pool=pool
            if profile=='stable' and protected:
                fewest=min(lost_promises(plan) for plan in pool)
                profile_pool=[plan for plan in pool if lost_promises(plan)==fewest]
            plan=min(profile_pool,key=lambda candidate:objective_key(candidate,s,profile=profile,reference=reference,focus_request_id=focus))
            require_valid(s,travel,plan)
            diff_ids={r.id for r in s.requests}
            if event.type=='cancel':diff_ids.add(event.request_id)
            diff=differences(reference,plan,diff_ids)
            operational_diff=[d for d in diff if not (event.type=='cancel' and d['request_id']==event.request_id)]
            facts=facts_for(s,plan,operational_diff)
            assignments=_assignments(plan)
            facts.update(avoidable_reassigned=sum(old[0] in available and assignments[key][0]!=old[0]
                         for key,old in prior.items() if key in assignments),
                         priority_start_sum=sum(assignments[key][1] for key in targets if key in assignments),
                         response_target_count=len(targets),response_unassigned=len(targets-set(assignments)),
                         previous_approved_lost=lost_promises(plan))
            facts['outside_scope_changes']=sum(
                old[0] not in local_scope and assignments.get(key)!=old
                for key,old in prior.items()) if local_scope else 0
            by_id={r.id:r for r in s.requests}
            lost={d['request_id'] for d in diff if d['before_engineer'] and d['after_engineer'] is None}
            emergency=None
            if event.type=='emergency':
                match=next(((r.engineer_id,v) for r in plan.routes for v in r.visits if v.request_id=='emergency:'+event.id),None)
                emergency={'assigned':bool(match),'engineer_id':match[0] if match else None,
                    'arrival':match[1].arrival if match else None,'start':match[1].start if match else None,
                    'end':match[1].end if match else None,'response_minutes':match[1].start-event.time if match else None,
                    'received_at':event.time,'response_target_minutes':120,'response_target_at':event.time+120,
                    'exceeds_response_target':match[1].start-event.time>120 if match else None}
            ordinary=None
            if event.type=='new_request':
                match=next(((r.engineer_id,v) for r in plan.routes for v in r.visits if v.request_id=='request:'+event.id),None)
                ordinary={'assigned':bool(match),'engineer_id':match[0] if match else None,
                    'arrival':match[1].arrival if match else None,'start':match[1].start if match else None,
                    'end':match[1].end if match else None}
            unassigned_details=[{'request_id':u.request_id,'address':by_id[u.request_id].address,
                'reason':u.reason,'lost_assignment':u.request_id in lost} for u in plan.unassigned]
            affected_clients=sum(d['before_engineer'] is not None for d in operational_diff)
            try:
                estimate=risk_engine.analyze(plan,s)
                risk_data=estimate.to_dict()
                risk={'status':'ready','model_version':RISK_MODEL_VERSION,'calibrated':False,
                    'method':'monte_carlo','loss_unit':'условные единицы','affected_clients':affected_clients,
                    'summary':{key:risk_data[key] for key in ('p_late_start','p_sla_breach','p_overtime',
                        'expected_overtime_minutes','expected_loss','expected_affected_customers','affected_customer_count')},
                    'routes':risk_data['routes'],'iterations':risk_data['iterations'],
                    'assumptions':list(estimate.assumptions),
                    'duration_evidence':duration_evidence.metadata()}
            except Exception:
                # Risk is an advisory layer: a forecast failure must not make a
                # valid operational plan impossible to inspect or approve.
                risk={'status':'error','model_version':RISK_MODEL_VERSION,'calibrated':False,
                    'method':'monte_carlo','loss_unit':'условные единицы','affected_clients':affected_clients,
                    'summary':None,'routes':[],'iterations':0,'assumptions':[],
                    'duration_evidence':duration_evidence.metadata()}
            candidates.append({'id':profile,'label':LABELS[profile],'plan':plan.model_dump(mode='json'),'diff':diff,'facts':facts,
                'objective_key':list(objective_key(plan,s,profile=profile,reference=reference,focus_request_id=focus)),
                'emergency':emergency,'ordinary_request':ordinary,'unassigned_details':unassigned_details,
                'risk':risk,'explanation':explanation(facts),'duplicate_of':None,
                'llm':{'status':'not_connected','facts_only':True,'instruction':'Объясни компромиссы только по facts и diff. Не придумывай SLA, деньги, причины невозможности или оптимальность. Решение принимает диспетчер.'}})
        group_candidates(candidates)
        batch={'request_labels':labels,'observation_warnings':[f"{self.initial_engineers[eid].name}: последний ручной факт в {f['time']//60:02}:{f['time']%60:02}; последующее выполнение не подтверждено." for eid,f in self.statuses.items() if f['time']<event.time],'id':uuid4().hex,'version':self.version,'event':event.model_dump(),'candidates':candidates,'frozen':frozen,'inventory':inventory,
            'protect_approved_assignments':protect_approved,
            'incoming_ids':[item['id'] for item in self.incoming_requests if item['status']=='new'],
            'local_scope':{'engineer_ids':list(local_scope),'mode':'minimal_disruption_priority','hard_isolation':False} if local_scope else None,
            'off_duty_escalation':{'engineer_ids':list(activation_ids),'reason':escalation_reason.strip(),
                'confirmed':True,'window_start':activation_window.start if activation_window else None,
                'window_end':activation_window.end if activation_window else None} if activation_ids else None,
            'risk_model':{'version':RISK_MODEL_VERSION,'method':'monte_carlo','calibrated':False,
                'loss_unit':'условные единицы','iterations':risk_engine.config.iterations,
                'seed_fingerprint':risk_seed,'duration_evidence':duration_evidence.metadata()},
            'notice':('Дорожные матрицы. ' if self.geography else 'Условная география. ')+('Ручные статусы и симуляция остальных инженеров. ' if self.statuses else 'Исполнение плана моделируется. ')+'Показатели относятся к остатку дня; текущие закрепленные работы показаны отдельно. Базовое объяснение сформировано по проверенным фактам. Риски оценены Монте-Карло по априорным распределениям'+(f" и {duration_evidence.observation_count} подтверждённым длительностям" if duration_evidence.observation_count else '')+'; калибровка вероятностей на независимой истории исходов пока не подтверждена, потери — условные единицы.'}
        plain_retry=(event.type=='replan' and not batch['incoming_ids'] and
                     not self.pending_report_ids and not self.needs_replanning and event.time==self.time)
        # A plain retry is an audit of the unchanged day. The shared selector
        # applies its no-loss policy for both UI and LLM recommendations;
        # explicit SLA/Cost alternatives remain available to the dispatcher.
        batch['recommendation_policy']='preserve_approved' if plain_retry else 'risk_aware'
        batch['recommended_candidate_id']=eligible_candidates(batch)[0]['id']
        from .explanation_card import build_card
        for candidate in batch['candidates']:
            candidate['explanation_card']=build_card(batch,candidate)
        batch['response_focus_request_id']=focus
        if event.type=='new_request':
            batch['insertion_policy']='preserve_existing_assignments_and_starts'
            batch['notice']+=' Обычная новая заявка добавляется только в свободный интервал; владельцы, порядок и начало прежних работ сохраняются.'
        if event.type=='emergency':
            batch['notice']+=' 120 минут от поступления до начала работ — ориентир реакции; превышение показано и не запрещает назначение.'
            if activation_ids:
                batch['notice']+=' Вызов сотрудника с выходного — отдельная подтверждённая диспетчером эскалация; обычные события выходных не задействуют.'
        excluded=self.excluded_engineers()
        if event.type=='unavailable':excluded.update(event.engineer_ids or [event.engineer_id])
        batch['excluded_engineers']=[{'id':eid,'name':self.initial_engineers[eid].name} for eid in sorted(excluded)]
        # Internal payload never trusts a plan sent back from the browser.
        self.batches[batch['id']]={'batch':deepcopy(batch),'scenario':s,'hash':fingerprint(batch),
            'policy':{'allow_redirect':payload.get('allow_redirect',False),'preserve_in_progress':True,
                'off_duty_escalation':deepcopy(batch['off_duty_escalation'])}}
        return batch

    def approve(self,payload,*,automatic=False):
        if payload.get('version')!=self.version:raise ValueError('Устаревшая версия плана')
        stored=self.batches.get(payload.get('batch_id'))
        if stored is None:raise ValueError('Предложение не найдено; пересчитайте')
        batch=stored['batch']
        if batch['version']!=self.version or fingerprint(batch)!=stored['hash']:raise ValueError('Измененная или устаревшая версия предложения')
        candidate=next((c for c in batch['candidates'] if c['id']==payload.get('candidate_id')),None)
        if candidate is None:raise ValueError('Неизвестный вариант')
        plan=Plan.model_validate(candidate['plan'])
        require_valid(stored['scenario'],self._travel(stored['scenario']),plan)
        previous_assignments={visit.request_id:(route.engineer_id,visit.start)
            for route in self.plan.routes for visit in route.visits}
        previous_unassigned={row.request_id for row in self.plan.unassigned}
        before_assigned_count=self.plan.metrics.assigned
        before_unassigned_count=self.plan.metrics.unassigned
        before_assignment_summary=self.assignment_summary()
        escalation=stored['policy'].get('off_duty_escalation') or {}
        requested=set(escalation.get('engineer_ids',[]))
        used={r.engineer_id for r in plan.routes if r.visits} & requested
        scenario=stored['scenario']
        if requested-used:
            previous_engineers={e.id:e for e in self.scenario.engineers}
            scenario=scenario.model_copy(update={'engineers':[e.model_copy(update={
                'off_duty':True,'shift':previous_engineers[e.id].shift})
                if e.id in requested-used else e for e in scenario.engineers]})
            require_valid(scenario,self._travel(scenario),plan)
        self.scenario=scenario;self.plan=plan
        for eid in used:
            self.activated_off_duty_windows[eid]=TimeWindow(
                start=escalation['window_start'],end=escalation['window_end']) if (
                    escalation.get('window_start') is not None) else self.initial_engineers[eid].shift
        for request in self.scenario.requests:
            old=self.catalog.get(request.id)
            if old is None:self.catalog[request.id]=request
            elif old.excluded_engineer_ids!=request.excluded_engineer_ids:
                self.catalog[request.id]=old.model_copy(update={
                    'excluded_engineer_ids':list(request.excluded_engineer_ids)})
        self.time=batch['event']['time'];self.version+=1;self.frozen=batch['frozen'];self.inventory=batch['inventory']
        for row in self.inventory:
            if row['engineer_id'] in requested-used:row['dispatchable']=False
        policy=deepcopy(stored['policy'])
        if policy.get('off_duty_escalation'):
            policy['off_duty_escalation']['activated_ids']=sorted(used)
            policy['off_duty_escalation']['not_required_ids']=sorted(requested-used)
        self.history.append({'event':batch['event'],'candidate_id':candidate['id'],'version':self.version,
            'policy':policy,'automatic':automatic,'incoming_ids':list(batch.get('incoming_ids',[]))})
        cancelled_request_ids=([batch['event']['request_id']]
            if batch['event']['type']=='cancel' else [])
        if cancelled_request_ids:
            from .intake import cancellation_entry
            key=cancelled_request_ids[0]
            for item in self.incoming_requests:
                item_key=('emergency:' if item['event']['type']=='emergency' else 'request:')+item['id']
                if item_key==key:
                    item['status']='cancelled'
                    item['applied_version']=self.version
            self.decision_log.append(cancellation_entry(self,candidate,key,
                previous_assignments.get(key),automatic))
        if batch.get('incoming_ids'):
            from .intake import decision_entry
            assigned={visit.request_id for route in plan.routes for visit in route.visits}
            for item in self.incoming_requests:
                if item['id'] not in batch['incoming_ids'] or item['status']!='new':continue
                key=('emergency:' if item['event']['type']=='emergency' else 'request:')+item['id']
                item['status']='assigned' if key in assigned else 'unassigned'
                item['applied_version']=self.version
                self.decision_log.append(decision_entry(self,candidate,item,automatic))
        # A later dispatcher replan may recover a previously unassigned JSON
        # request, or move an assigned one without changing its status. Keep
        # the inbox's applied_version aligned with the actual current plan.
        current_assignments={visit.request_id:(route.engineer_id,visit.start)
            for route in plan.routes for visit in route.visits}
        assigned=set(current_assignments)
        unassigned_ids={item.request_id for item in plan.unassigned}
        for item in self.incoming_requests:
            if item['status']=='new':continue
            key=('emergency:' if item['event']['type']=='emergency' else 'request:')+item['id']
            current_status='assigned' if key in assigned else 'unassigned' if key in unassigned_ids else None
            if current_status and (item['status']!=current_status or
                    previous_assignments.get(key)!=current_assignments.get(key)):
                item['status']=current_status
                item['applied_version']=self.version
        from .intake import reassignment_entry
        incoming_keys={('emergency:' if item['event']['type']=='emergency' else 'request:')+item['id']
            for item in self.incoming_requests if item['id'] in batch.get('incoming_ids',[])}
        changed=list(cancelled_request_ids)
        for key in sorted(set(previous_assignments)|previous_unassigned|set(current_assignments)|unassigned_ids):
            if key in incoming_keys or key not in current_assignments and key not in unassigned_ids:
                continue
            before=previous_assignments.get(key)
            after=current_assignments.get(key)
            if before==after and (before is not None or key in previous_unassigned):
                continue
            changed.append(key)
            self.decision_log.append(reassignment_entry(self,candidate,key,before,automatic))
        self.history[-1]['changed_request_ids']=changed
        unresolved=[item.request_id for item in plan.unassigned]
        lost_request_ids=sorted(set(previous_assignments) & unassigned_ids)
        recovered_request_ids=sorted(previous_unassigned & set(current_assignments))
        active_previous_unassigned=previous_unassigned-set(cancelled_request_ids)
        backlog_remaining_request_ids=sorted(active_previous_unassigned & unassigned_ids)
        self.history[-1]['lost_request_ids']=lost_request_ids
        self.history[-1]['recovered_request_ids']=recovered_request_ids
        title=(('Автопилот применил план' if automatic else 'Диспетчер применил план')+
               f' №{self.version} · новых {len(batch.get("incoming_ids",[]))} · изменено назначений {len(changed)}')
        if unresolved:title+=f' · без исполнителя {len(unresolved)}'
        if cancelled_request_ids:
            detail='Отменённая заявка исключена из активного плана и не входит в список без исполнителя. '
        elif lost_request_ids:
            detail=f'Ранее назначенных заявок теперь без исполнителя: {len(lost_request_ids)}. '
        elif recovered_request_ids:
            detail=f'Восстановлены назначения: {len(recovered_request_ids)}. '
        else:
            detail='Ранее назначенные заявки не потеряли исполнителя. '
        if active_previous_unassigned:
            detail+=(f'Повторно проверено заявок без исполнителя: {len(active_previous_unassigned)}; '
                     f'назначено из них: {len(recovered_request_ids)}. ')
        self.dispatch_notifications.append({'id':uuid4().hex,'kind':'auto_applied' if automatic else 'manual_applied',
            'time':datetime.now(timezone.utc).isoformat(),'version':self.version,
            'title':title,
            'text':detail+('После расчёта часть заявок всё ещё без исполнителя. Откройте «Заявки, требующие внимания».'
                    if unresolved else 'Все заявки остатка дня распределены.'),
            'plan_explanation':candidate.get('explanation',''),
            'explanation_card':candidate.get('explanation_card'),
            'request_ids':list(batch.get('incoming_ids',[])), 'changed_request_ids':changed,
            'cancelled_request_ids':cancelled_request_ids,
            'unassigned_request_ids':unresolved,
            'unassigned_details':deepcopy(candidate['unassigned_details']),
            'lost_request_ids':lost_request_ids,'recovered_request_ids':recovered_request_ids,
            'backlog_attempted_request_ids':sorted(active_previous_unassigned),
            'backlog_remaining_request_ids':backlog_remaining_request_ids,
            'before_assigned_count':before_assigned_count,'after_assigned_count':plan.metrics.assigned,
            'before_effective_assigned_count':before_assignment_summary['assigned'],
            'before_requires_assignment_count':before_assignment_summary['requires_assignment'],
            'before_unassigned_count':before_unassigned_count,'after_unassigned_count':plan.metrics.unassigned})
        self.batches.clear()
        self.pending_report_ids.clear()
        self.needs_replanning=False
        self._save_view('Утвержден '+candidate['label'])
        return self.state()

    def dump(self):
        return {'execution_mode':self.execution_mode,'fact_audit':self.fact_audit,'initial_engineers':{k:v.model_dump(mode='json') for k,v in self.initial_engineers.items()},'geography':self.geography.model_dump(mode='json') if self.geography else None,'aliases':self.aliases,'geo_locations':{k:v.model_dump(mode='json') for k,v in self.geo_locations.items()},'statuses':self.statuses,'fact_log':self.fact_log,'needs_replanning':self.needs_replanning,'views':self.views,'scenario':self.scenario.model_dump(mode='json'),'plan':self.plan.model_dump(mode='json'),
            'planning_settings':self.planning_settings,'incoming_requests':self.incoming_requests,
            'dispatch_notifications':self.dispatch_notifications,'decision_log':self.decision_log,
            'autopilot_last_failure_at':self.autopilot_last_failure_at,
            'unavailability':self.unavailability,'declined_assignments':self.declined_assignments,
            'pending_report_ids':self.pending_report_ids,
            'activated_off_duty_windows':{eid:window.model_dump(mode='json') for eid,window in self.activated_off_duty_windows.items()},
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
        obj.unavailability=payload.get('unavailability',[])
        obj.declined_assignments=payload.get('declined_assignments',[])
        obj.pending_report_ids=payload.get('pending_report_ids',[])
        obj.activated_off_duty_windows={eid:TimeWindow.model_validate(window) for eid,window in
            payload.get('activated_off_duty_windows',{}).items()}
        obj.planning_settings=payload.get('planning_settings',{'mode':'manual','every_n_requests':1,'every_n_minutes':None})
        obj.incoming_requests=payload.get('incoming_requests',[])
        obj.dispatch_notifications=payload.get('dispatch_notifications',[])
        obj.decision_log=payload.get('decision_log',[])
        obj.autopilot_last_failure_at=payload.get('autopilot_last_failure_at')
        obj.execution_mode=payload.get('execution_mode','simulation');obj.fact_audit=payload.get('fact_audit',[])
        from .fact_journal import migrate
        migrate(obj)
        obj.needs_replanning=payload.get('needs_replanning',False)
        obj.views={int(k):v for k,v in payload.get('views',{}).items()}
        if not obj.views:obj._save_view('Восстановленный план')
        return obj
