"""Effective-time fact journal with immutable revisions and deterministic replay."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
from .models import Plan
from .operational_facts import validate_fact


def migrate(session):
    for index,fact in enumerate(session.fact_log):
        fact.setdefault('id',f'legacy-{index+1}')
        fact.setdefault('recorded_at','unknown (legacy)')
        if 'input' not in fact:
            fact['input']={k:fact.get(k) for k in ('time','engineer_id','status','request_id','location_id','remaining_minutes')}
            if fact['status'] in ('completed','interrupted'):
                fact['input'].update({'consumed_'+k:v for k,v in fact['consumed'].items()})
    if not hasattr(session,'fact_audit'):session.fact_audit=[]


def historical_owner(session,key,when):
    views=sorted((p for p in session.views.values() if p['operations']['time']<=when),key=lambda p:(p['operations']['time'],p['operations']['version']),reverse=True)
    for view in views:
        for route in view['plans'][0]['routes']:
            if any(v['request_id']==key for v in route['visits']):return route['engineer_id']
        for route in view['operations'].get('frozen',[]):
            if any(v['request_id']==key for v in route['visits']):return route['engineer_id']
    return None


def record(session,payload):
    if type(payload.get('version')) is not int or payload['version']!=session.version:raise ValueError('Устаревшая версия плана')
    migrate(session)
    identity=payload.get('fact_id')
    old=next((f for f in session.fact_log if f['id']==identity),None)
    reason=payload.get('correction_reason','')
    if identity:
        if old is None:raise ValueError('Исправляемая запись не найдена')
        if not isinstance(reason,str) or len(reason.strip())<3:raise ValueError('Укажите причину исправления')
        if payload.get('engineer_id')!=old['engineer_id']:raise ValueError('Исправление не может менять инженера записи')
    raw={k:payload[k] for k in ('time','engineer_id','status','request_id','location_id','remaining_minutes','consumed_router','consumed_tv_box') if k in payload}
    if type(raw.get('time')) is not int or not 0<=raw['time']<=1439:raise ValueError('Время факта должно быть в пределах текущего дня')
    entry={'id':identity or uuid4().hex,'recorded_at':datetime.now(timezone.utc).isoformat(),'input':raw}
    records=[deepcopy(f) for f in session.fact_log if f['id']!=identity]+[entry]
    records.sort(key=lambda f:(f['input']['time'],f['recorded_at'],f['id']))
    shadow=deepcopy(session);shadow.statuses={};shadow.fact_log=[];shadow.time=0
    for item in records:
        request=item['input'].get('request_id')
        owner=historical_owner(session,request,item['input']['time']) if request else None
        shadow.fact_owner_override={request:owner} if request else {}
        normalized=validate_fact(shadow,{'version':session.version,**item['input']})
        normalized.update(id=item['id'],recorded_at=item['recorded_at'],input=item['input'])
        shadow.fact_log.append(normalized);shadow.statuses[normalized['engineer_id']]=normalized
    current=next(f for f in shadow.fact_log if f['id']==entry['id'])
    session.fact_log=shadow.fact_log;session.statuses=shadow.statuses
    session.fact_audit.append({'fact':deepcopy(current),'replaces':identity,'reason':reason.strip() if identity else '',
                              'previous':deepcopy(old) if old else None})
    session.time=max(session.time,raw['time'])
    return current
