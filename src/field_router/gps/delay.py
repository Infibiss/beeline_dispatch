"""Read-only proposal. Only the normal, explicitly confirmed fact API writes."""
from .contracts import GpsInputError
from .models import hhmm
from ..availability import availability_at, trip_stopped_by_absence

def draft(context, snapshot, engineer_id):
    engineer=context['engineers'].get(engineer_id)
    row=next((r for r in snapshot['rows'] if r['engineer_id']==engineer_id),None)
    if not engineer or not row:
        raise GpsInputError('GPS: неизвестный инженер')
    if availability_at(engineer.get('availability', {}), row['clock']['minute']) != 'available':
        raise GpsInputError('Инженер сообщил недоступность или ещё не подтвердил возвращение. Сначала уточните его статус.')
    status=engineer.get('status') or {}
    if trip_stopped_by_absence(engineer.get('availability', {}), status, row['clock']['minute']):
        raise GpsInputError('Предыдущий выезд прерван периодом недоступности. Подтвердите новый выезд.')
    if status.get('status')!='traveling':
        raise GpsInputError('Сначала подтвердите статус «В пути». GPS сам не подтверждает выезд.')
    analysis=row.get('analysis') or {}
    target,eta=analysis.get('target'),analysis.get('eta')
    if (not row['date_matches'] or row.get('quality_reason') or not target or not eta
        or target.get('basis')!='confirmed' or target['id']!=status.get('request_id')):
        raise GpsInputError('Нет свежей пригодной оценки доезда. Дождитесь координаты и расчёта.')
    now=row['clock']['minute']
    if now < context.get('time',0) or now <= status.get('time',-1):
        raise GpsInputError('В журнале уже есть запись на эту минуту или позже. Дождитесь следующей минуты; время факта не подменяется.')
    remaining=max(1,eta['high_minutes'])
    if now+remaining>=engineer['shift']['end']:
        raise GpsInputError('Доезд выходит за смену. Уточните условия или рассмотрите другого инженера.')
    tasks=engineer['tasks']
    start=next(i for i,t in enumerate(tasks) if t['id']==target['id'])
    affected=[];arrival=now+remaining
    for index,task in enumerate(tasks[start:]):
        if index:arrival+=task.get('travel_minutes',0)
        possible=[max(arrival,task['start'],w['start']) for w in task['windows']
                  if max(arrival,task['start'],w['start'])<=w['end']]
        begin=min(possible) if possible else max(arrival,task['start'])
        affected.append({'request_id':task['id'],'planned_start':task['start'],'estimated_start':begin,
                         'shift_minutes':max(0,begin-task['start']),'window_missed':not possible})
        arrival=begin+task['duration_minutes']
    return {'version':context['version'],'engineer_id':engineer_id,'request_id':target['id'],
            'time':now,'remaining_minutes':remaining,'location_id':status.get('location_id'),
            'point_measured_at_ms':row['position']['measured_at_ms'],'point_age_seconds':row['point_age_seconds'],
            'expires_at_ms':row['position']['measured_at_ms']+snapshot['config']['stale_seconds']*1000,
            'affected':affected,
            'explanation':f"Оценка доезда {eta['low_minutes']}–{eta['high_minutes']} мин. Предлагаем подтвердить верхнюю оценку {remaining} мин на {hhmm(now)}. "
                "Свяжитесь с инженером и при необходимости исправьте значение. Изменится оставшееся время пути. Местоположение в журнале останется прежним; уточните его отдельно при необходимости.",
            'comment':f"Диспетчер подтвердил оценку доезда по GPS на {hhmm(now)}; исходный диапазон {eta['low_minutes']}–{eta['high_minutes']} мин."}


def confirm(session, gps, payload):
    import math
    if not gps:
        raise GpsInputError('GPS недоступен. Можно уточнить статус вручную.')
    allowed={'version','engineer_id','request_id','time','status','location_id','remaining_minutes','comment','mode','gps_point_measured_at_ms'}
    if not isinstance(payload,dict) or set(payload)-allowed:
        raise GpsInputError('Неизвестные поля подтверждения GPS')
    proposal=gps.delay_draft({'version':payload.get('version'),'engineer_id':payload.get('engineer_id')})
    stamp=payload.get('gps_point_measured_at_ms')
    if (type(stamp) not in (int,float) or not math.isfinite(stamp)
        or not 0 <= gps.clock()-stamp/1000 <= gps.config.stale_seconds
        or stamp != proposal['point_measured_at_ms']):
        raise GpsInputError('GPS-оценка устарела. Получите свежую перед подтверждением.')
    if (session.version!=proposal['version'] or payload.get('status')!='traveling' or
        payload.get('request_id')!=proposal['request_id'] or payload.get('time')!=proposal['time'] or
        payload.get('location_id')!=proposal['location_id']):
        raise GpsInputError('Условия доезда изменились. Получите оценку GPS заново.')
    fact={k:v for k,v in payload.items() if k not in ('gps_point_measured_at_ms','mode')}
    return session.record_fact(fact)
