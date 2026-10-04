"""Short day briefing: model orders verified findings; no personal data leaves."""
import hashlib
import json
from .risk_signals import current_forecast

PROMPT = '''Ты помогаешь диспетчеру понять, на что обратить внимание в контроле дня.
Выбери до трёх самых полезных тезисов из findings и расположи по важности.
Сначала неназначенные заявки, новые факты и дефицит оборудования, затем риск
нарушения срока, переработки и позднего начала. Модельные сигналы — доля сценариев,
а не проверенная вероятность реального события. Счётчики категорий пересекаются.
Верни только JSON {"points":["идентификатор"]}. Не добавляй текст или новые факты.
'''


def findings(data):
    points=[]
    def add(key,title,detail,action,essential=False):
        points.append(dict(id=key,title=title,detail=detail,action=action,essential=essential))
    context=data.get('context',{})
    if context.get('needs_replanning'):
        add('stale','План ждёт пересчёта','Поступили новые факты о работе бригад.',
            'Уточните статусы и рассчитайте изменения плана.',True)
    attention=data.get('attention') or {}
    if (attention.get('plan_version')==context.get('plan_version')
            and attention.get('time')==context.get('time')):
        immediate=(attention.get('tiers') or {}).get('now')
        if type(immediate) is int and immediate>0:
            add('act_now','Действовать сейчас',f'Ситуаций, требующих реакции сейчас: {immediate}.',
                'Начните с раздела «Действовать сейчас»: причины и действия указаны в каждой ситуации.',True)
        queued=(attention.get('incoming') or {}).get('new')
        if type(queued) is int and queued>0:
            add('incoming','Новые заявки в очереди',f'Ещё не включены в утверждённый план: {queued}.',
                'Откройте входящую очередь и проверьте срочные заявки.',True)
    count=data.get('summary',{}).get('unassigned')
    if type(count) is int and count>0:
        add('unassigned','Без исполнителя',f'Требуют назначения: {count}.',
            'Откройте «Заявки и изменения» и разберите неназначенные заявки.',True)
    for item in data.get('indicators',[]):
        facts=item.get('facts',{})
        if item.get('id')=='equipment':
            rows=facts.get('equipment',[])
            missing=sum(max(0,r['needed']-r['on_hand']) for r in rows
                        if type(r.get('needed')) is int and type(r.get('on_hand')) is int)
            if missing:
                add('equipment','Не хватает оборудования',f'Дефицит под план: {missing} шт.',
                    'Проверьте оборудование на руках и назначения бригад.',True)
        if item.get('id')=='windows' and type(facts.get('tight_visits')) is int and facts['tight_visits']>0:
            add('windows','Небольшой запас времени',f"Визитов с плотным окном: {facts['tight_visits']}.",
                'Уточните время освобождения инженеров перед ближайшими визитами.')
    forecast=data.get('forecast',{})
    valid=current_forecast(forecast, context)
    review=forecast.get('review') or {}
    incomplete=not valid
    for key,title,noun,action in [
        ('sla','Модельный сигнал нарушения срока','визитов','Проверьте визиты с риском срока и текущие статусы инженеров.'),
        ('overtime','Модельный сигнал переработки','бригад','Проверьте последние визиты и окончание смен.'),
        ('late','Модельный сигнал сдвига начала','визитов','Уточните остаток текущих работ; при изменении обещаний передайте их поддержке.')]:
        row=review.get(key,{}) if valid else {}
        count,total=row.get('count'),row.get('total')
        if type(count) is int and type(total) is int and 0<=count<=total:
            if count:add(key,title,f'Стоит проверить: {count} из {total} {noun}.',action)
        else:incomplete=True
    if incomplete:
        add('forecast_missing','Прогноз неполный','Оценка части рисков пока недоступна.',
            'Обновите показатели; текущие статусы проверяйте по информации бригад.')
    if not points:
        add('clear','Явных проблем не выявлено','По доступным показателям сейчас нет приоритетных отклонений.',
            'Следите за поступлениями и подтверждайте фактическое выполнение.')
    return points


def summarize(explainer,data):
    points=findings(data)
    body={'input':PROMPT+json.dumps({'findings':points},ensure_ascii=False),'temperature':.1,'max_output_tokens':200}
    key='day:'+hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
    with explainer.lock:
        if key in explainer.cache:return dict(explainer.cache[key])
        order=[];source='template';notice='ИИ недоступен — сводка по показателям.'
        if explainer.calls<explainer.max_calls:
            explainer.calls+=1
            try:
                response=explainer.send(body)
                if response.get('status') in ('failed','incomplete'):raise ValueError()
                raw='\n'.join(c['text'] for item in response.get('output',[]) for c in item.get('content',[])
                              if c.get('type')=='output_text' and isinstance(c.get('text'),str)).strip()
                if raw.startswith('```'):
                    raw='\n'.join(raw.splitlines()[1:-1])
                if len(raw)>2000:raise ValueError()
                order=json.loads(raw).get('points')
                allowed={p['id'] for p in points}
                if (not isinstance(order,list) or not 1<=len(order)<=3
                    or any(not isinstance(k,str) or k not in allowed for k in order)
                    or len(set(order))!=len(order)):raise ValueError()
                source='yandexgpt';notice=''
            except (OSError,ValueError,TypeError,KeyError,AttributeError):order=[]
        mandatory=[p['id'] for p in points if p['essential']]
        order=list(dict.fromkeys(mandatory+order+[p['id'] for p in points]))[:3]
        chosen=[next(p for p in points if p['id']==key) for key in order]
        result={'title':'На что обратить внимание','items':[{k:p[k] for k in ('id','title','detail')} for p in chosen],
                'action':chosen[0]['action'],'source':source,'notice':notice}
        explainer.cache[key]=result
        if len(explainer.cache)>128:explainer.cache.pop(next(iter(explainer.cache)))
        return dict(result)
