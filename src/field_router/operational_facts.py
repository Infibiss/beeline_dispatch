"""Explicit dispatcher observations; elapsed clock time never confirms work."""
from copy import deepcopy
from .models import Equipment, TimeWindow, Visit, Route


def validate_fact(session,payload):
    if payload.get('version')!=session.version:raise ValueError('Устаревшая версия плана')
    now=payload.get('time')
    if type(now) is not int or not session.time<=now<=1440:raise ValueError('Недопустимое время факта')
    eid=payload.get('engineer_id')
    engineers=session.initial_engineers
    if eid not in engineers:raise ValueError('Неизвестный инженер')
    kind=payload.get('status')
    if kind not in ('available','traveling','working','completed','interrupted','unavailable'):raise ValueError('Неизвестный статус')
    key=payload.get('request_id') if kind in ('working','completed','interrupted','traveling') else None
    old=session.statuses.get(eid,{})
    if old and now<=old['time']:raise ValueError('Новый статус инженера должен быть позже предыдущего')
    if old.get('status')=='working' and kind in ('available','traveling','unavailable'):
        raise ValueError('Сначала явно завершите или прервите начатую работу')
    if kind in ('working','completed','interrupted','traveling'):
        if key not in session.catalog:raise ValueError('Нужна известная заявка')
        if any(f['status']=='completed' and f.get('request_id')==key for f in session.fact_log):raise ValueError('Заявка уже завершена фактически')
        owner=next((r.engineer_id for r in session.plan.routes for v in r.visits if v.request_id==key),None)
        if owner is None:owner=next((r['engineer_id'] for r in session.frozen for v in r['visits'] if v['request_id']==key),None)
        if hasattr(session,'fact_owner_override'):owner=session.fact_owner_override.get(key,owner)
        if owner!=eid:raise ValueError('Заявка не назначена этому инженеру; сначала измените план')
        if any(f.get('status')=='working' and f.get('request_id')==key and other!=eid for other,f in session.statuses.items()):raise ValueError('Эта работа не завершена другим инженером')
        if kind=='interrupted' and not (old.get('status')=='working' and old.get('request_id')==key):raise ValueError('Прерывание возможно после ручного статуса работы')
        if old.get('status')=='working' and old.get('request_id')!=key:raise ValueError('Сначала завершите текущую работу')
    location=key if kind in ('working','completed','interrupted') else payload.get('location_id')
    if location not in session.scenario.points and location not in getattr(session,'geo_locations',{}):raise ValueError('Укажите известную фактическую точку')
    remaining=payload.get('remaining_minutes') if kind in ('working','interrupted','traveling') else None
    if kind in ('working','interrupted','traveling') and (type(remaining) is not int or not 0<remaining<=1440):raise ValueError('Укажите положительное оставшееся время')
    consumed=Equipment()
    if kind in ('completed','interrupted'):
        request=session.catalog[key]
        values={}
        for device in ('router','tv_box'):
            value=payload.get('consumed_'+device)
            if value is None and kind=='completed':
                used=sum(f['consumed'][device] for f in session.fact_log if f.get('request_id')==key)
                value=max(0,getattr(request.equipment,device)-used)
            if type(value) is not int or value<0:raise ValueError('Явно укажите расход устройств при прерывании')
            values[device]=value
        consumed=Equipment(**values)
        spent={k:0 for k in values}
        for f in session.fact_log:
            if f['engineer_id']==eid:
                for k in spent:spent[k]+=f['consumed'][k]
        for k in spent:
            if spent[k]+getattr(consumed,k)>getattr(session.initial_stock[eid],k):raise ValueError('Расход превышает утренний запас')
    return {'time':now,'engineer_id':eid,'status':kind,'request_id':key,'location_id':location,
            'remaining_minutes':remaining,'consumed':consumed.model_dump(),'source':'dispatcher'}


def apply_observations(session,s,routes,frozen,now):
    """Project a remaining-day problem from explicit latest observations.

    An observed worker remains busy for the declared remainder from the new
    planning instant, until a completion is explicitly reported. This conservative
    estimate is labelled; it is not an automatic completion timestamp.
    """
    jobs={r.id:r for r in s.requests}
    points=dict(s.points)
    engineers={e.id:e for e in s.engineers}
    frozen=deepcopy(frozen)
    # Rebuild ledger rows from effective facts, including corrected completions.
    cancelled={h['event'].get('request_id') for h in session.history if h['event']['type']=='cancel'}
    observed_from={eid:min(f['time'] for f in session.fact_log if f['engineer_id']==eid) for eid in session.statuses}
    for route in frozen:
        for visit in list(route['visits']):
            start=observed_from.get(route['engineer_id'])
            if visit.get('fact_id') or (start is not None and visit['end']>=start):
                if visit['request_id'] not in cancelled:jobs.setdefault(visit['request_id'],session.catalog[visit['request_id']])
                route['visits'].remove(visit)
    for revision in getattr(session,'fact_audit',[]):
        for old in (revision.get('previous'),revision.get('fact')):
            if old and old.get('request_id') in session.catalog and old['request_id'] not in cancelled:
                jobs.setdefault(old['request_id'],session.catalog[old['request_id']])
    for eid,fact in session.statuses.items():
        if eid not in engineers:engineers[eid]=session.initial_engineers[eid]
        e=engineers[eid].model_copy(update={'shift':session.initial_engineers[eid].shift})
        # Restore commitments that were projected beyond this observation.
        for route in frozen:
            if route['engineer_id']!=eid:continue
            for visit in list(route['visits']):
                if visit['end']>fact['time'] or visit['request_id']==fact.get('request_id'):
                    jobs.setdefault(visit['request_id'],session.catalog[visit['request_id']])
                    route['visits'].remove(visit)
        routes.pop(eid,None)
        explicit=[f for f in session.fact_log if f['engineer_id']==eid and f['status'] in ('completed','interrupted')]
        for f in explicit:
            key=f['request_id']
            # Each recorded consumption has its own ledger row. Completed visits
            # leave the pending set; interrupted work retains its declared remainder.
            marker=f"fact:{eid}:{f['time']}:{key}:{f['status']}"
            if not any(v.get('fact_id')==marker for r in frozen for v in r['visits']):
                for r in frozen:
                    r['visits'][:]=[v for v in r['visits'] if not (v['request_id']==key and not v.get('fact_id'))]
                visit={'request_id':key,'arrival':f['time'],'start':f['time'],'end':f['time'],
                    'travel_minutes':0,'distance_m':0,'explanation':'Фактическое завершение' if f['status']=='completed' else 'Фактическое прерывание',
                    'fact_id':marker,'consumed_equipment':f['consumed'],'status':f['status']}
                frozen.append({'engineer_id':eid,'visits':[visit],'origins':{}})
            if f['status']=='completed':jobs.pop(key,None)
            else:
                request=jobs.get(key,session.catalog[key])
                jobs[key]=request.model_copy(update={'duration_minutes':f['remaining_minutes'],
                    'equipment':Equipment(**{k:max(0,getattr(session.catalog[key].equipment,k)-sum(row['consumed'][k] for row in session.fact_log if row.get('request_id')==key and row['time']<=f['time'])) for k in ('router','tv_box')})})
        spent={'router':0,'tv_box':0}
        for r in frozen:
            if r['engineer_id']!=eid:continue
            for v in r['visits']:
                usage=v.get('consumed_equipment',session.catalog[v['request_id']].equipment.model_dump())
                for k in spent:spent[k]+=usage[k]
        stock=Equipment(**{k:getattr(session.initial_stock[eid],k)-spent[k] for k in spent})
        location=fact['location_id']
        alias=f'observed:{session.version}:{eid}:{now}'
        if location in points:points[alias]=points[location]
        session._alias_location(alias,location)
        start=min(max(now,e.shift.start),e.shift.end)
        engineers[eid]=e.model_copy(update={'start_location':alias,'shift':TimeWindow(start=start,end=e.shift.end),'stock':stock})
        kind=fact['status'];key=fact.get('request_id')
        if kind in ('working','traveling'):
            request=jobs.get(key,session.catalog[key]);jobs[key]=request
            if kind=='working':
                duration=fact['remaining_minutes']
                jobs[key]=request.model_copy(update={'duration_minutes':duration})
                visit=Visit(request_id=key,arrival=now,start=now,end=now+duration,travel_minutes=0,distance_m=0,explanation='Ручной статус: работа идет; остаток задан диспетчером')
            else:
                arrival=now+fact['remaining_minutes']
                starts=[max(arrival,w.start) for w in request.windows if max(arrival,w.start)<=w.end]
                if not starts:raise ValueError('Подтвержденный переезд не укладывается в окно: уточните окно или разрешите перенаправление при аварии')
                begin=min(starts)
                visit=Visit(request_id=key,arrival=arrival,start=begin,end=begin+request.duration_minutes,travel_minutes=fact['remaining_minutes'],distance_m=0,explanation='Ручной статус: в пути; остаток времени задан диспетчером')
                engineers[eid]=engineers[eid].model_copy(update={'shift':TimeWindow(start=max(0,now-1),end=e.shift.end)})
            if visit.end>e.shift.end:raise ValueError('Фактический остаток работы/поездки выходит за смену: уточните условия, сверхурочные автоматически не разрешаются')
            routes[eid]=Route(engineer_id=eid,visits=[visit],distance_m=0,travel_minutes=0,service_minutes=0,loadout=request.equipment)
        elif kind=='unavailable':
            engineers[eid]=engineers[eid].model_copy(update={'shift':TimeWindow(start=start,end=start)})
    completed={f['request_id'] for f in session.fact_log if f['status']=='completed'}
    for key in completed:jobs.pop(key,None)
    frozen=[r for r in frozen if r['visits']]
    return s.model_copy(update={'requests':list(jobs.values()),'engineers':list(engineers.values()),'points':points}),routes,frozen
