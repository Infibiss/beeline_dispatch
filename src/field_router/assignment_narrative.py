"""Optional model-written prose over anonymous, server-built assignment facts.

The model never receives addresses, names, IDs or comments. Numerical/identity
placeholders are filled locally; checked facts remain visible beside the prose.
"""
from copy import deepcopy
import hashlib
import json
import re


PROMPT = """Ты объясняешь диспетчеру уже рассчитанное назначение одной заявки.
Напиши по-русски живо, спокойно и коротко, без канцелярита, списков и повторения
всех полей карточки. Не выбирай инженера и не предлагай автоматически менять план.
Верни только JSON: {"paragraphs":[{"text":"...","evidence":["..."]}]}.
Нужно 2–3 коротких абзаца: смысл решения, порядок/ограничения и что проверить,
если заявка без исполнителя. Каждый абзац ссылается на факты из facts по их id.
Все required_evidence должны быть использованы. Нельзя добавлять причины,
сравнения, риски и обещания, которых нет в facts. Допустимость не означает,
что инженер ближайший/лучший, маршрут оптимальный или все другие варианты невозможны.
Перепланирование уже выполнено алгоритмом; объясни только этот результат.
Для времени, количества, имени, адреса и других значений используй только
плейсхолдеры из slots в виде {{start}}, {{engineer}} и т.д. Не пиши числа самостоятельно,
в том числе словами, и не раскрывай плейсхолдеры: их заполнит сервер.
Не придумывай пробки, поломки, болезни, причины отсутствия, гарантии клиенту.
Не вставляй HTML, ссылки, markdown и инструкции вместо объяснения.
"""
SKILLS={'local':'локальные работы','connection':'подключение','emergency':'аварийные работы'}
TRANSPORT={'car':'автомобиль','walk':'пешком','bicycle':'велосипед','public':'общественный транспорт'}


def clock(value):
    return f'{value//60:02}:{value%60:02}'


def context_for(snapshot, request_id):
    operations=snapshot.get('operations',{})
    if operations.get('needs_replanning') or request_id in operations.get('assignment_summary',{}).get('pending_request_ids',[]):
        raise ValueError('Назначение устарело: сначала обновите факты и пересчитайте план')
    scenario=snapshot['scenario'];plan=snapshot['plans'][0]
    jobs={r['id']:r for r in scenario['requests']}
    request=jobs.get(request_id)
    if request is None:raise ValueError('Заявка не найдена в текущем плане')
    slots={'skill':SKILLS.get(request['skill'],request['skill']),
           'window':', '.join(clock(w['start'])+'–'+clock(w['end']) for w in request['windows'])}
    facts=[];required=[]
    def add(key,text,essential=False):
        facts.append({'id':key,'text':text})
        if essential:required.append(key)
    match=next(((route,index,visit) for route in plan['routes'] for index,visit in enumerate(route['visits'])
                if visit['request_id']==request_id),None)
    if match:
        route,index,visit=match
        engineer=next(e for e in scenario['engineers'] if e['id']==route['engineer_id'])
        slots.update(engineer=engineer['name'],start=clock(visit['start']),end=clock(visit['end']),
                     travel=str(visit['travel_minutes']),wait=str(visit['start']-visit['arrival']),
                     position=str(index+1),route_total=str(len(route['visits'])),
                     transport=TRANSPORT.get(engineer['transport'],engineer['transport']))
        add('status','Заявка назначена {{engineer}} на {{start}}.',True)
        if request['skill'] in engineer['skills']:
            add('qualification','Инженер имеет требуемый навык {{skill}}.',True)
        if request.get('required_transport'):
            if request['required_transport']==engineer['transport']:
                add('transport','Тип транспорта {{transport}} совпадает с требованием заявки.')
        else:add('transport','В заявке нет ограничения по типу транспорта.')
        if any(w['start']<=visit['start']<=w['end'] for w in request['windows']):
            add('window','Начало {{start}} попадает в клиентское окно {{window}}.',True)
        if visit['arrival']-visit['travel_minutes']>=engineer['shift']['start'] and visit['end']<=engineer['shift']['end']:
            add('shift','Расчётная дорога и работа до {{end}} помещаются в доступную смену.')
        slots['previous']=jobs[route['visits'][index-1]['request_id']]['address'] if index else 'стартовая точка инженера'
        add('sequence','Визит на месте {{position}} из {{route_total}}. Перед ним {{previous}}; дорога {{travel}} мин.',True)
        if index+1<len(route['visits']):
            next_visit=route['visits'][index+1]
            slots.update(next=jobs[next_visit['request_id']]['address'],next_start=clock(next_visit['start']))
            add('next','Следующий визит: {{next}} в {{next_start}}.')
        else:add('next','Это последний визит в выбранном маршруте.')
        if visit['start']>visit['arrival']:add('wait','До начала по окну предусмотрено ожидание {{wait}} мин.')
        stock=engineer.get('stock');load=route.get('loadout')
        if stock and load and all(load.get(k,0)<=stock.get(k,0) for k in ('router','tv_box')):
            add('equipment','Выданного оборудования по данным плана хватает на весь маршрут.')
        add('limits','Это допустимый выбранный порядок, но не доказательство оптимальности и не сравнение всех инженеров.')
        state='assigned'
    else:
        unresolved=next((u for u in plan['unassigned'] if u['request_id']==request_id),None)
        if unresolved is None:raise ValueError('Для заявки нет подтверждённого результата расчёта')
        state='unassigned';add('status','Заявка пока без исполнителя.',True)
        diagnosis=plan.get('diagnostics',{}).get('unassigned_diagnostics',{}).get(request_id,{})
        counts=diagnosis.get('counts',{})
        labels={'skill':'нет нужного навыка','transport':'не подходит транспорт','off_duty':'не работают',
                'declined':'отказались от заявки','stock':'свободного оборудования не хватает с учётом других назначений',
                'shift_capacity':'работа не помещается в доступную смену','region':'другой район'}
        reasons=[]
        for key,label in labels.items():
            if counts.get(key,0)>0:
                slots['count_'+key]=str(counts[key])
                reasons.append(label+' — {{count_'+key+'}} инженеров')
        if reasons:add('resources','Проверка ресурсов: '+ '; '.join(reasons)+'.',True)
        if diagnosis.get('fixed_slots',0):
            slots['slots']=str(diagnosis['fixed_slots'])
            add('insertion','Есть {{slots}} допустимых мест вставки без переноса начала прежних визитов; в выбранном плане заявка не назначена.',True)
        elif diagnosis.get('retimed_slots',0):
            slots['slots']=str(diagnosis['retimed_slots'])
            add('insertion','Есть {{slots}} допустимых мест вставки только с пересчётом времени прежних визитов.',True)
        elif diagnosis.get('checked_positions',0):
            slots['checked']=str(diagnosis['checked_positions'])
            add('insertion','Проверены {{checked}} мест вставки: в текущем порядке маршрутов допустимая вставка не найдена.',True)
        else:add('insertion','При текущих ресурсах проверка не выявила подходящего инженера для вставки.',True)
        for key,label in [('window','клиентское окно не соблюдается'),('shift','работа выходит за смену')]:
            if counts.get(key,0)>0:
                slots['failed_'+key]=str(counts[key])
                add(key,'При вставке '+label+' в {{failed_'+key+'}} позициях.')
        add('limits','Проверены текущие маршруты и запас. Это не доказывает невозможность всех перестановок и обменов заявками.',True)
        add('action','Диспетчер может проверить доступный запас и время инженеров и рассмотреть варианты пересчёта. Проверка сама ничего не меняет.')
    return {'facts':facts,'required_evidence':required,'slots':list(slots),'state':state},slots


def _selection(response):
    if response.get('status') in ('failed','incomplete'):raise ValueError('Незавершённый ответ')
    raw='\n'.join(c['text'] for item in response.get('output',[]) for c in item.get('content',[])
                  if c.get('type')=='output_text' and isinstance(c.get('text'),str)).strip()
    if len(raw)>8000:raise ValueError('Ответ слишком длинный')
    for prefix in ('```'+r'\n','```json'+r'\n'):
        if raw.startswith(prefix) and raw.endswith(r'\n```'):raw=raw[len(prefix):-len(r'\n```')].strip()
    lines=raw.splitlines()
    if len(lines)>=3 and lines[0].strip() in ('```','```json') and lines[-1].strip()=='```':raw='\n'.join(lines[1:-1])
    return json.loads(raw)


def validate(selection, context):
    if not isinstance(selection,dict) or set(selection)!={'paragraphs'}:raise ValueError('Неверный формат')
    paragraphs=selection['paragraphs']
    if not isinstance(paragraphs,list) or not 2<=len(paragraphs)<=3:raise ValueError('Неверное число абзацев')
    allowed={f['id'] for f in context['facts']};covered=set()
    for paragraph in paragraphs:
        if not isinstance(paragraph,dict) or set(paragraph)!={'text','evidence'}:raise ValueError('Неверный абзац')
        text=paragraph['text'];evidence=paragraph['evidence']
        if (not isinstance(text,str) or not 15<=len(text)<=550 or not isinstance(evidence,list)
                or not evidence or any(not isinstance(key,str) or key not in allowed for key in evidence)):
            raise ValueError('Неподтверждённый текст')
        placeholders=re.findall(r'\{\{([a-z_]+)\}\}',text)
        if any(key not in context['slots'] for key in placeholders):raise ValueError('Непроверенное значение')
        prose=re.sub(r'\{\{[a-z_]+\}\}','',text)
        if (re.search(r'[0-9<>\x00-\x1f]|https?://|www\.|```|\{|\}',prose)
            or re.search(r'пробк|полом|боле[зл]|ближайш|самый|самая|лучший инженер|оптимальн|единственн|гарант|физически невозможно',prose,re.I)):
            raise ValueError('Непроверенное утверждение')
        if 'insertion' not in allowed and re.search(r'без переноса|без изменения|сохранены прежние',prose,re.I):
            raise ValueError('Сохранение прежних обещаний не подтверждено')
        covered.update(evidence)
    if not set(context['required_evidence']).issubset(covered):raise ValueError('Пропущены важные основания')
    return paragraphs


def narrative(explainer, snapshot, request_id):
    context,slots=context_for(snapshot,request_id)
    body={'input':PROMPT+json.dumps(context,ensure_ascii=False),'temperature':.3,'max_output_tokens':750}
    key='assignment:'+hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    fallback={'source':'verified','paragraphs':[],
              'notice':'Пояснение ИИ недоступно — ниже проверенные основания расчёта.'}
    if explainer is None:return fallback
    with explainer.lock:
        cached=deepcopy(explainer.cache.get(key))
        if cached is None:
            cached=fallback
            if explainer.calls>=explainer.max_calls:
                cached={**fallback,'notice':'Лимит пояснений ИИ достигнут — ниже проверенные основания расчёта.'}
            else:
                explainer.calls+=1
                try:
                    paragraphs=validate(_selection(explainer.send(body)),context)
                    cached={'source':'yandexgpt','paragraphs':paragraphs,'notice':''}
                except (OSError,ValueError,TypeError,KeyError,AttributeError):pass
            explainer.cache[key]=deepcopy(cached)
            if len(explainer.cache)>128:explainer.cache.pop(next(iter(explainer.cache)))
    result=deepcopy(cached)
    if result['source']=='yandexgpt':
        result['paragraphs']=[re.sub(r'\{\{([a-z_]+)\}\}',lambda m:slots[m[1]],p['text']) for p in result['paragraphs']]
    return result
