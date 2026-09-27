"""Optional, bounded explanations of allowlisted aggregates. Never plans."""
import hashlib
import json
import math
import os
from pathlib import Path
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from .recommendation import LABELS, recommendation_rank, eligible_candidates

FIELDS = ('assigned','unassigned','emergency_assigned','emergency_total','connection_assigned',
          'connection_total','lost_assignments','new_assignments','reassigned','shifted',
          'time_shift_minutes','distance_m','travel_minutes','engineers_used','avoidable_reassigned',
          'priority_start_sum','response_target_count','response_unassigned')
PROMPT = """Ты выбираешь важные тезисы для объяснения диспетчеру сравнения маршрутов.
Верни ТОЛЬКО JSON вида {"points":["coverage","reaction","changes"]}, без markdown и текста.
Выбери от 2 до 4 разных идентификаторов из списка available_points.
coverage — охват; reaction — время до начала аварии; changes — смена исполнителей;
travel — время дороги; priorities — охват аварий и подключений.
Первый тезис всегда coverage. Затем важные различия: reaction для новой аварии,
  risk — P(SLA), ожидаемые потери, затронутые клиенты и P(переработки);
  changes при смене исполнителей, travel при разнице дороги. Не сочиняй другие идентификаторы.
 Все стратегии соблюдают одинаковый порядок приоритетов: аварии, подключения, остальные.
"""


def risk_metrics(candidate):
    """Return only finite, non-sensitive aggregates used by the server rank."""
    risk=candidate.get('risk')
    if not isinstance(risk,dict) or risk.get('status')!='ready':return None
    summary=risk.get('summary')
    if not isinstance(summary,dict):return None
    affected=risk.get('affected_clients',summary.get('expected_affected_customers'))
    values={
        'p_sla_breach':summary.get('p_sla_breach'),
        'expected_loss':summary.get('expected_loss'),
        'affected_clients':affected,
        'p_overtime':summary.get('p_overtime'),
    }
    if not all(type(value) in (int,float) and math.isfinite(value) for value in values.values()):return None
    if not 0<=values['p_sla_breach']<=1 or not 0<=values['p_overtime']<=1:return None
    if values['expected_loss']<0 or values['affected_clients']<0:return None
    return values


def _number(value):
    return f'{value:.2f}'.rstrip('0').rstrip('.')


def _percent(value):
    return f'{value*100:.1f}'.rstrip('0').rstrip('.')+'%'

def verified_points(batch):
    candidates=[c for c in batch['candidates'] if not c.get('duplicate_of')]
    def label(c):return '«'+LABELS.get(c['id'],'Вариант')+'»'
    def comparison(render):return '; '.join(label(c)+': '+render(c) for c in candidates)+'.'
    points={'coverage':'Охват — '+comparison(lambda c:f"назначено {c['facts']['assigned']}, без назначения {c['facts']['unassigned']}")}
    for key,field,title in [('changes','reassigned','Заявок со сменой исполнителя'),('travel','travel_minutes','Оставшееся время дороги, мин')]:
        if all(field in c['facts'] for c in candidates):
            points[key]=title+' — '+comparison(lambda c:str(c['facts'][field]))
    # A grouped autopilot batch is a generic replan, even if it contains an
    # emergency request. Candidate.emergency is meaningful only when the
    # preview event itself focuses on one newly arrived emergency.
    if (batch.get('event') or {}).get('type') in (None,'emergency') and any(c.get('emergency') for c in candidates):
        points['reaction']='От поступления аварии до начала работы — '+comparison(
            lambda c:str(c['emergency']['response_minutes'])+' мин' if c.get('emergency',{}).get('assigned') else 'авария не назначена')
        points['reaction']+=' Включает ожидание освобождения бригады, а не только дорогу.'
    required=('emergency_assigned','emergency_total','connection_assigned','connection_total')
    if all(all(k in c['facts'] for k in required) for c in candidates):
        points['priorities']='Приоритетные заявки — '+comparison(lambda c:
            f"аварии {c['facts']['emergency_assigned']}/{c['facts']['emergency_total']}, подключения {c['facts']['connection_assigned']}/{c['facts']['connection_total']}")
    if candidates and all(risk_metrics(c) is not None for c in candidates):
        points['risk']='Риски — '+comparison(lambda c:(
            f"P(SLA) {_percent(risk_metrics(c)['p_sla_breach'])}, "
            f"ожидаемые потери {_number(risk_metrics(c)['expected_loss'])} усл. ед., "
            f"затронуто клиентов {_number(risk_metrics(c)['affected_clients'])}, "
            f"P(переработки) {_percent(risk_metrics(c)['p_overtime'])}"))
    return points

def recommendation_text(batch,choice,order):
    """Explain the selected plan's advantages/tradeoffs with verified numbers only."""
    candidates=[c for c in batch['candidates'] if not c.get('duplicate_of')];parts=[]
    if batch.get('recommendation_policy')=='preserve_approved':
        parts.append('Новых событий нет: при повторном расчёте сначала сохраняем выполнимые обещанные визиты, затем ищем назначения оставшимся заявкам.')
    fields={'coverage':('unassigned','Число заявок без назначения','Наименьшее число заявок без назначения'),
            'changes':('reassigned','Смена исполнителя, заявок','Наименьшее число смен исполнителя'),
            'travel':('travel_minutes','Дорога, мин','Наименьшее время дороги')}
    for key in order:
        if key in fields:
            field,label,advantage=fields[key]
            if not all(field in c['facts'] for c in candidates):continue
            values=[c['facts'][field] for c in candidates];value=choice['facts'][field]
            if len(set(values))==1:
                if key=='coverage':parts.append(f"По охвату варианты одинаковы: назначено — {choice['facts']['assigned']}, без назначения — {value}.")
                else:parts.append(f'{label} — {value}; варианты одинаковы.')
            elif value==min(values):parts.append(f'{advantage} — {value} (в остальных вариантах до {max(values)}).')
            else:parts.append(f'{label} — {value}; у лучшей альтернативы — {min(values)}.')
            if key=='changes' and 'time_shift_minutes' in choice['facts']:
                f=choice['facts']
                parts.append(f"Суммарный сдвиг начала работ — {f['time_shift_minutes']} мин.")
                if f.get('avoidable_reassigned',f['reassigned'])!=f['reassigned']:
                    parts.append(f"Изменений исполнителя, которых можно избежать, — {f['avoidable_reassigned']}; остальные связаны с выбытием исполнителя.")
        elif key=='reaction':
            if (batch.get('event') or {}).get('type') not in (None,'emergency'):
                continue
            e=choice.get('emergency') or {}
            if not e.get('assigned'):parts.append('Новая авария не назначена.')
            else:
                values=[c['emergency']['response_minutes'] for c in candidates if (c.get('emergency') or {}).get('assigned')]
                value=e['response_minutes']
                parts.append(f'До начала новой аварии — {value} мин; минимум среди вариантов — {min(values)} мин. Включает ожидание освобождения бригады.')
        elif key=='priorities':
            f=choice['facts']
            if all(k in f for k in ('emergency_assigned','emergency_total','connection_assigned','connection_total')):
                coverage=(f['emergency_assigned'],f['connection_assigned'])
                best=max((c['facts'].get('emergency_assigned',-1),
                          c['facts'].get('connection_assigned',-1)) for c in candidates)
                outcome=('это максимальный охват приоритетов среди вариантов' if coverage==best else
                         f'лучшая альтернатива назначает {best[0]} аварий и {best[1]} подключений')
                parts.append(f"Назначены аварии {f['emergency_assigned']}/{f['emergency_total']} и подключения {f['connection_assigned']}/{f['connection_total']}; {outcome}.")
        elif key=='risk':
            risk=risk_metrics(choice)
            if risk is not None and all(risk_metrics(c) is not None for c in candidates):
                parts.append(
                    f"P(SLA) — {_percent(risk['p_sla_breach'])}; ожидаемые потери — "
                    f"{_number(risk['expected_loss'])} усл. ед.; затронуто клиентов — "
                    f"{_number(risk['affected_clients'])}; P(переработки) — {_percent(risk['p_overtime'])}."
                )
    return ' '.join(parts)


def selected_plan_text(batch, choice, emphasis):
    """Explain an already applied choice, never ask the model to choose again."""
    label=LABELS.get(choice['id'],choice['id'])
    available=verified_points(batch)
    # The model may choose the order of emphasis. Essential operational facts
    # remain visible even if it leaves a point out of its short JSON answer.
    order=list(dict.fromkeys(['coverage','priorities']+
        [key for key in emphasis if key in available]+['risk','reaction','changes','travel']))
    facts=recommendation_text(batch,choice,order)
    assigned=choice['facts'].get('assigned')
    unassigned=choice['facts'].get('unassigned')
    total=assigned+unassigned if type(assigned) is int and type(unassigned) is int else None
    opening=f'Вариант «{label}». '
    if total is not None:
        opening+=f'На остаток дня назначено {assigned} из {total} заявок. '
    incoming=len(batch.get('incoming_ids',[]))
    if incoming:
        opening+=f'В этом пересчёте учтено {incoming} новых поступлений. '
    focus=next((key for key in emphasis if key in available and key!='coverage'),None)
    focus_text={
        'risk':'Главный компромисс — риск опозданий и переработки, а не только длина маршрута.',
        'changes':'Главный компромисс — влияние на уже обещанные визиты.',
        'travel':'Главный компромисс — дополнительные минуты дороги.',
        'priorities':'Главный компромисс — охват аварий и подключений.',
        'reaction':'Главный компромисс — время начала новой аварийной работы.',
    }
    why='Почему этот вариант: '+(focus_text.get(focus,'Сопоставлены охват, риски и изменения маршрутов.')+' ')+facts
    if type(unassigned) is int and unassigned:
        next_step=f'Что проверить диспетчеру: {unassigned} заявок остаются без исполнителя; они не исчезли из плана и будут повторно рассмотрены при следующем событии.'
    else:
        next_step='Что проверить диспетчеру: все заявки остатка дня получили исполнителя; проверьте изменения клиентского времени перед публикацией.'
    if risk_metrics(choice) is not None:
        next_step+=' Риски получены Монте-Карло и пока не откалиброваны на независимой истории.'
    next_step+=' Назначения рассчитаны алгоритмом с проверкой ограничений; их применяет диспетчер или настроенный автопилот.'
    return opening+'\n'+why+'\n'+next_step


def decision_detail(entry, candidate):
    """Per-visit detail built only from the persisted decision and risk facts."""
    parts=[entry['explanation'].strip()]
    risk=candidate.get('risk') or {}
    if risk.get('status')!='ready':return ' '.join(parts)
    visit=next((visit for route in risk.get('routes',[]) for visit in route.get('visits',[])
                if visit.get('request_id')==entry.get('request_id')),None)
    if not isinstance(visit,dict):return ' '.join(parts)
    duration=visit.get('service_duration') or {}
    quantiles=[duration.get(key) for key in ('p50_minutes','p80_minutes','p90_minutes')]
    if all(type(value) in (int,float) and math.isfinite(value) and value>=0 for value in quantiles):
        parts.append('Прогноз длительности работы: P50 — '+_number(quantiles[0])+
                     ', P80 — '+_number(quantiles[1])+', P90 — '+_number(quantiles[2])+' мин.')
    p_sla=visit.get('p_sla_breach')
    p_late=visit.get('p_late_start')
    if type(p_sla) in (int,float) and math.isfinite(p_sla) and 0<=p_sla<=1:
        detail='Симуляционный риск нарушения окна/SLA этого визита — '+_percent(p_sla)
        if type(p_late) in (int,float) and math.isfinite(p_late) and 0<=p_late<=1:
            detail+='; риск позднего начала — '+_percent(p_late)
        parts.append(detail+'.')
    if len(parts)>1:parts.append('Эти вероятности пока не откалиброваны на независимой истории.')
    return ' '.join(parts)


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

    def recommend(self,batch):
        return self.explain(batch,None)

    def explain(self,batch,candidate_id):
        recommend=candidate_id is None
        if recommend:
            candidate_id=min(eligible_candidates(batch),key=recommendation_rank)['id']
        candidate=next((c for c in batch['candidates'] if c['id']==candidate_id),None)
        if candidate is None:raise ValueError('Неизвестный вариант')
        summaries=[]
        for c in batch['candidates']:
            if c.get('duplicate_of'):continue
            facts={k:v for k,v in c['facts'].items() if k in FIELDS and type(v) in (int,float,bool)}
            emergency={k:v for k,v in (c.get('emergency') or {}).items()
                if k in ('assigned','response_minutes') and (v is None or type(v) in (int,float,bool))}
            summary={'strategy':c['id'],'facts':facts,'emergency':emergency}
            risk=risk_metrics(c)
            if risk is not None:summary['risk']=risk
            summaries.append(summary)
        points=verified_points(batch)
        # The generic schema example must not suggest "reaction" on a batch
        # without a newly arriving emergency. Otherwise the provider can copy
        # that example and the valid response is needlessly discarded.
        example_points=['coverage']+[key for key in ('risk','reaction','changes','travel','priorities')
            if key in points][:2]
        instruction=PROMPT.replace('{"points":["coverage","reaction","changes"]}',
            json.dumps({'points':example_points},ensure_ascii=False,separators=(',',':')))
        instruction+='\nТолько эти идентификаторы разрешены в этом расчёте: '+json.dumps(list(points),ensure_ascii=False)+'.\n'
        if recommend:
            instruction=instruction.replace(json.dumps({'points':example_points},ensure_ascii=False,separators=(',',':')),
                json.dumps({'candidate_id':candidate_id,'points':example_points},ensure_ascii=False,separators=(',',':')))
            instruction+='\nВерни candidate_id из eligible_candidates: сервер уже выбрал рекомендацию по правилу. Сначала охват аварий, затем подключений, затем остальных заявок. При равном охвате далее риск: P(SLA), ожидаемые потери, затронутые клиенты и P(переработки). Для новой аварии затем реакция на неё; далее сохранение исполнителей и времени, минуты дороги и метры. Объясни выгоды и компромиссы, не меняй эту политику.\n'
            if batch.get('recommendation_policy')=='preserve_approved':
                instruction+='Это повторный расчёт без новых событий. Приоритет этой рекомендации: сохранить выполнимые обещанные визиты, назначить оставшиеся, затем уменьшить изменения. Объясняй только уже выбранный eligible_candidates.\n'
        context={'variants':summaries,'available_points':list(points)}
        if recommend:context['eligible_candidates']=[c['id'] for c in eligible_candidates(batch)]
        else:context['selected']=candidate_id
        body={'input':instruction+json.dumps(context,ensure_ascii=False),
              'temperature':0.1,'max_output_tokens':500}
        key=hashlib.sha256(json.dumps([batch['id'],batch['version'],body],sort_keys=True).encode()).hexdigest()
        with self.lock:
            if key in self.cache:return dict(self.cache[key])
            result={'text':candidate['explanation'],'source':'template','notice':'LLM недоступна; показано объяснение по проверенным показателям.'}
            if recommend:
                result.update(candidate_id=candidate_id,source='rule',notice='Объяснение по правилу: охват по приоритетам → риск → реакция на новую аварию → стабильность → дорога.')
                if batch.get('recommendation_policy')=='preserve_approved':
                    result['notice']='Повторный расчёт без новых событий: сохранение обещанных визитов → назначение оставшихся → минимум изменений.'
                order=['coverage']+(['risk'] if 'risk' in points else [])+(['reaction'] if 'reaction' in points else [])+[k for k in ('changes','travel','priorities') if k in points]
                result['text']=recommendation_text(batch,candidate,order[:4])
            if self.calls>=self.max_calls:result['notice']='Лимит вызовов LLM исчерпан; показано обычное объяснение.'
            else:
                self.calls+=1
                try:
                    response=self.send(body)
                    if response.get('status') in ('failed','incomplete'):raise ValueError('Незавершённый ответ')
                    text='\n'.join(c['text'] for item in response.get('output',[]) for c in item.get('content',[])
                        if c.get('type')=='output_text' and isinstance(c.get('text'),str)).strip()
                    if not text or len(text)>8000:raise ValueError('Нет корректного текста')
                    # Lite can use literal backslash-n around a Markdown fence.
                    # Strip only that wrapper, never rewrite escapes inside JSON.
                    for prefix in ('```'+r'\n','```json'+r'\n'):
                        if text.startswith(prefix) and text.endswith(r'\n```'):
                            text=text[len(prefix):-len(r'\n```')].strip()
                    lines=text.splitlines()
                    if len(lines)>=3 and lines[0].strip() in ('```','```json') and lines[-1].strip()=='```':
                        text='\n'.join(lines[1:-1])
                    selection=json.loads(text)
                    order=selection.get('points')
                    known={'coverage','risk','reaction','changes','travel','priorities'}
                    if not isinstance(order,list) or not 2<=len(order)<=4 or not all(isinstance(k,str) and k in known for k in order) or len(set(order))!=len(order) or order[0]!='coverage':raise ValueError('Непроверенные тезисы')
                    # A recognized point can be irrelevant in this particular
                    # batch (e.g. emergency reaction on a routine replan). It
                    # cannot become prose without verified facts. Keep the
                    # valid model emphasis instead of rejecting all of it.
                    order=[key for key in order if key in points]
                    if len(order)<2:raise ValueError('Нет двух применимых тезисов')
                    if recommend:
                        choice=next((c for c in batch['candidates'] if c['id']==selection.get('candidate_id')),None)
                        if choice is None or choice['id'] not in {c['id'] for c in eligible_candidates(batch)}:raise ValueError('Нарушены приоритеты')
                        # Essential reason and stability price stay visible even
                        # when the model chooses different secondary accents.
                        essential=['coverage']+(['risk'] if 'risk' in points else [])+(['reaction'] if 'reaction' in points else [])+(['changes'] if 'changes' in points else [])
                        order=(essential+[k for k in order if k not in essential])[:4]
                    text=(recommendation_text(batch,choice,order) if recommend else
                          selected_plan_text(batch,candidate,order))
                    result={'text':text,'source':'yandexgpt','notice':'YandexGPT выбрала акценты; текст и числа сформированы из проверенных показателей.'}
                    if recommend:result.update(candidate_id=choice['id'],notice='YandexGPT объясняет выбор по правилам. Показатели проверены; решение за диспетчером.')
                except HTTPError as exc:
                    result['notice']=f'YandexGPT не ответила (HTTP {exc.code}); показано объяснение по проверенным данным.'
                except (URLError,OSError):
                    result['notice']='Нет защищённого соединения с YandexGPT; показано объяснение по проверенным данным.'
                except (ValueError,TypeError,KeyError,AttributeError):
                    result['notice']='Ответ YandexGPT не прошёл проверку; показано объяснение по проверенным данным.'
            self.cache[key]=result
            if len(self.cache)>128:self.cache.pop(next(iter(self.cache)))
            return dict(result)
