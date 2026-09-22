"""Strict, small GPS track imports. Test clock is explicit; facts stay untouched."""
import csv
from copy import deepcopy
import io
import json
from uuid import uuid4
from .contracts import GpsInputError, number, validate_packet


def replay(service, payload):
    required = {'engineer_id','request_id','version','minute','format','text'}
    if not isinstance(payload, dict) or set(payload)!=required:
        raise GpsInputError('Укажите инженера, заявку, время проверки и GPS-трек')
    content = payload['text']
    if not isinstance(content,str) or not 1<=len(content)<=48000:
        raise GpsInputError('Трек должен содержать от 1 до 48000 символов')
    try:
        if payload['format']=='csv':
            reader=csv.DictReader(io.StringIO(content.lstrip('\ufeff')))
            if set(reader.fieldnames or [])!={'seconds','lat','lon','accuracy_m'}:
                raise GpsInputError('CSV: нужны колонки seconds,lat,lon,accuracy_m')
            rows=[]
            for row in reader:
                if len(row)!=4 or None in row: raise ValueError()
                rows.append({key:float(value) for key,value in row.items()})
        elif payload['format']=='json':
            rows=json.loads(content)
        else: raise GpsInputError('Поддерживаются CSV и JSON')
    except (ValueError, TypeError, csv.Error) as exc:
        if isinstance(exc,GpsInputError): raise
        raise GpsInputError('Не удалось прочитать трек. Проверьте формат и числовые значения.') from None
    if not isinstance(rows,list) or not 1<=len(rows)<=service.config.max_points:
        raise GpsInputError(f'Нужно от 1 до {service.config.max_points} точек')
    previous=-1
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'seconds','lat','lon','accuracy_m'}:
            raise GpsInputError('Каждая точка: seconds, lat, lon, accuracy_m')
        seconds=number(row['seconds'],'seconds',0,service.config.max_sample_age_seconds-5)
        if seconds<=previous: raise GpsInputError('seconds должны строго возрастать')
        previous=seconds
    minute=number(payload['minute'],'minute',0,1439)
    context=service.context()
    eid=payload['engineer_id']
    if not isinstance(eid,str) or eid not in context['engineers']:
        raise GpsInputError('Выберите существующего инженера')
    if type(payload['version']) is not int or payload['version']!=context['version']:
        raise GpsInputError('План изменился. Обновите тестовые параметры.',409)
    engineer=deepcopy(context['engineers'][eid])
    task=next((task for task in engineer['tasks'] if task['id']==payload['request_id'] and task['point']),None)
    if task is None: raise GpsInputError('Выберите назначенную этому инженеру заявку с координатами')
    engineer['status']={'status':'traveling','request_id':task['id']}
    now, tracker=service.clock(), 'replay-'+uuid4().hex
    packets=[{'engineer_id':eid,'tracker_id':tracker,'state':'active',
              'point':{'lat':row['lat'],'lon':row['lon'],'accuracy_m':row['accuracy_m'],
                       'measured_at_ms':(now+row['seconds']-rows[-1]['seconds'])*1000}}
             for row in rows]
    for packet in packets: validate_packet(packet,now,service.config)
    outcomes=service.replace_demo(eid,packets,minute,engineer,context['version'])
    accepted=sum(bool(item['accepted']) for item in outcomes)
    return {'channel':'demo','engineer_id':eid,'points':len(rows),'accepted':accepted,
            'rejected':len(rows)-accepted,'reasons':[item['reason'] for item in outcomes],
            'plan_unchanged':True,'minute':minute,
            'note':'Последняя точка перенесена на сейчас, интервалы сохранены. Тестовый статус: в пути к выбранной заявке.'}
