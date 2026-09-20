"""Optional, bounded explanations of allowlisted aggregates. Never plans."""
import hashlib
import json
import os
from pathlib import Path
import threading
from urllib.request import Request, urlopen

FIELDS = ('assigned','unassigned','emergency_assigned','emergency_total','connection_assigned',
          'connection_total','lost_assignments','new_assignments','reassigned','shifted',
          'time_shift_minutes','distance_m','travel_minutes','engineers_used')
PROMPT = """Ты выбираешь важные тезисы для объяснения диспетчеру сравнения маршрутов.
Верни ТОЛЬКО JSON вида {"points":["coverage","reaction","changes"]}, без markdown и текста.
Выбери от 2 до 4 разных идентификаторов из списка available_points.
coverage — охват; reaction — время до начала аварии; changes — смена исполнителей;
travel — время дороги; priorities — охват аварий и подключений.
Первый тезис всегда coverage. Затем важные различия: reaction для новой аварии,
changes при смене исполнителей, travel при разнице дороги. Не сочиняй другие идентификаторы.
Все стратегии соблюдают одинаковый порядок приоритетов: аварии, подключения, остальные.
"""

LABELS={'sla':'Раньше приоритетные','stable':'Меньше изменений','cost':'Меньше ресурсов'}

def verified_points(batch):
    candidates=batch['candidates']
    def label(c):return '«'+LABELS.get(c['id'],'Вариант')+'»'
    def comparison(render):return '; '.join(label(c)+': '+render(c) for c in candidates)+'.'
    points={'coverage':'Охват — '+comparison(lambda c:f"назначено {c['facts']['assigned']}, без назначения {c['facts']['unassigned']}")}
    for key,field,title in [('changes','reassigned','Заявок со сменой исполнителя'),('travel','travel_minutes','Оставшееся время дороги, мин')]:
        if all(field in c['facts'] for c in candidates):
            points[key]=title+' — '+comparison(lambda c:str(c['facts'][field]))
    if any(c.get('emergency') for c in candidates):
        points['reaction']='От поступления аварии до начала работы — '+comparison(
            lambda c:str(c['emergency']['response_minutes'])+' мин' if c.get('emergency',{}).get('assigned') else 'авария не назначена')
        points['reaction']+=' Включает ожидание освобождения бригады, а не только дорогу.'
    required=('emergency_assigned','emergency_total','connection_assigned','connection_total')
    if all(all(k in c['facts'] for k in required) for c in candidates):
        points['priorities']='Приоритетные заявки — '+comparison(lambda c:
            f"аварии {c['facts']['emergency_assigned']}/{c['facts']['emergency_total']}, подключения {c['facts']['connection_assigned']}/{c['facts']['connection_total']}")
    return points

def settings(path=Path('.env')):
    values={}
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value=line.removeprefix('export ').partition('=')
            if sep and key.strip() in ('YANDEX_FOLDER_ID','YANDEX_API_KEY','YANDEX_MODEL','YANDEX_LLM_MAX_CALLS'):
                values[key.strip()]=value.strip().strip('"').strip("'")
    for key in ('YANDEX_FOLDER_ID','YANDEX_API_KEY','YANDEX_MODEL','YANDEX_LLM_MAX_CALLS'):
        if key in os.environ:values[key]=os.environ[key]
    return values


def configured(path=Path('.env')):
    cfg=settings(path)
    if not cfg.get('YANDEX_FOLDER_ID') or not cfg.get('YANDEX_API_KEY'):return None
    folder=cfg['YANDEX_FOLDER_ID'];model=cfg.get('YANDEX_MODEL','yandexgpt-5-lite')
    if model.startswith('gpt://'):model=model.rsplit('/',1)[-1]
    def send(body):
        request=Request('https://ai.api.cloud.yandex.net/v1/responses',
            data=json.dumps({**body,'model':f'gpt://{folder}/{model}'},ensure_ascii=False).encode(),
            headers={'Authorization':'Api-Key '+cfg['YANDEX_API_KEY'],'Content-Type':'application/json','x-folder-id':folder})
        with urlopen(request,timeout=25) as response:
            return json.loads(response.read(262144))
    return Explainer(send=send,max_calls=int(cfg.get('YANDEX_LLM_MAX_CALLS','20')))


class Explainer:
    def __init__(self,*,send,max_calls=20):
        if not 0<=max_calls<=1000:raise ValueError('YANDEX_LLM_MAX_CALLS должен быть от 0 до 1000')
        self.send=send;self.max_calls=max_calls;self.calls=0;self.cache={};self.lock=threading.Lock()

    def explain(self,batch,candidate_id):
        candidate=next((c for c in batch['candidates'] if c['id']==candidate_id),None)
        if candidate is None:raise ValueError('Неизвестный вариант')
        summaries=[]
        for c in batch['candidates']:
            facts={k:v for k,v in c['facts'].items() if k in FIELDS and type(v) in (int,float,bool)}
            emergency={k:v for k,v in (c.get('emergency') or {}).items()
                if k in ('assigned','response_minutes') and (v is None or type(v) in (int,float,bool))}
            summaries.append({'strategy':c['id'],'facts':facts,'emergency':emergency})
        points=verified_points(batch)
        body={'input':PROMPT+json.dumps({'selected':candidate_id,'variants':summaries,'available_points':list(points)},ensure_ascii=False),
              'temperature':0.1,'max_output_tokens':500}
        key=hashlib.sha256(json.dumps([batch['id'],batch['version'],body],sort_keys=True).encode()).hexdigest()
        with self.lock:
            if key in self.cache:return dict(self.cache[key])
            result={'text':candidate['explanation'],'source':'template','notice':'LLM недоступна; показано объяснение по проверенным показателям.'}
            if self.calls>=self.max_calls:result['notice']='Лимит вызовов LLM исчерпан; показано обычное объяснение.'
            else:
                self.calls+=1
                try:
                    response=self.send(body)
                    if response.get('status') in ('failed','incomplete'):raise ValueError('Незавершённый ответ')
                    text='\n'.join(c['text'] for item in response.get('output',[]) for c in item.get('content',[])
                        if c.get('type')=='output_text' and isinstance(c.get('text'),str)).strip()
                    if not text or len(text)>8000:raise ValueError('Нет корректного текста')
                    lines=text.splitlines()
                    if len(lines)>=3 and lines[0].strip() in ('```','```json') and lines[-1].strip()=='```':
                        text='\n'.join(lines[1:-1])
                    selection=json.loads(text)
                    order=selection.get('points')
                    if not isinstance(order,list) or not 2<=len(order)<=4 or not all(isinstance(k,str) and k in points for k in order) or len(set(order))!=len(order) or order[0]!='coverage':raise ValueError('Непроверенные тезисы')
                    text=' '.join(points[k] for k in order)+' План утверждает диспетчер; оптимальность не доказана.'
                    result={'text':text,'source':'yandexgpt','notice':'YandexGPT выбрала акценты; текст и числа сформированы из проверенных показателей.'}
                except (OSError,ValueError,TypeError,KeyError,AttributeError):
                    pass  # Never expose provider bodies, credentials, or internal errors.
            self.cache[key]=result
            if len(self.cache)>128:self.cache.pop(next(iter(self.cache)))
            return dict(result)
