import json
from copy import deepcopy
import pytest

def batch():
    return {'id':'batch-private','version':1,'candidates':[{'id':'sla','facts':{'assigned':5,'unassigned':1,'distance_m':1000,'reassigned':2,'address':'PRIVATE'},'emergency':{'assigned':True,'response_minutes':15,'engineer_id':'PRIVATE'},'explanation':'Резервное объяснение','plan':{'secret':'PRIVATE'}}]}


def add_risk(candidate, *, p_sla=.125, loss=3.4, affected=1, p_overtime=.075):
    candidate['risk']={
        'status':'ready','affected_clients':affected,
        'summary':{'p_sla_breach':p_sla,'expected_loss':loss,
                   'expected_affected_customers':.5,'p_overtime':p_overtime},
        'routes':[{'request_id':'PRIVATE'}],
        'assumptions':['PRIVATE'],
        'duration_evidence':{'PRIVATE':'PRIVATE'},
    }
    return candidate

def test_private_fields_never_sent_and_response_cached():
    from field_router.llm import Explainer
    calls=[]
    def send(body):
        calls.append(body)
        assert 'PRIVATE' not in json.dumps(body) and 'batch-private' not in json.dumps(body)
        return {'output':[{'content':[{'type':'output_text','text':'{"points":["coverage","changes"]}'}]}]}
    e=Explainer(send=send)
    b=batch();before=deepcopy(b)
    assert e.explain(b,'sla')['source']=='yandexgpt'
    assert 'назначено 5' in e.explain(b,'sla')['text']
    assert len(calls)==1 and b==before

@pytest.mark.parametrize('response',[{}, {'output':[{'content':[{'type':'output_text','text':''}]}]}])
def test_invalid_output_falls_back(response):
    from field_router.llm import Explainer
    e=Explainer(send=lambda _:response)
    assert e.explain(batch(),'sla')['source']=='template'

def test_failure_is_cached_and_budget_bounded():
    from field_router.llm import Explainer
    calls=[]
    def send(_):calls.append(1);raise TimeoutError('PRIVATE SECRET')
    e=Explainer(send=send,max_calls=1)
    b=batch()
    result=e.explain(b,'sla')
    assert 'SECRET' not in str(result) and result['text']=='Резервное объяснение'
    e.explain(b,'sla');b['version']=2;e.explain(b,'sla')
    assert len(calls)==1

def test_emergency_service_is_80():
    from field_router.replanning import Event
    assert Event(id='new',type='emergency',time=600).duration_minutes==80

def test_unverified_free_text_rejected():
    from field_router.llm import Explainer
    e=Explainer(send=lambda _: {'output':[{'content':[{'type':'output_text','text':'В cost время реакции 243, sla лучший.'}]}]})
    assert e.explain(batch(),'sla')['source']=='template'


def test_fenced_json_is_accepted_but_unknown_points_are_not():
    from field_router.llm import Explainer
    def response(text):return {'output':[{'content':[{'type':'output_text','text':text}]}]}
    e=Explainer(send=lambda _:response('```json\n{"points":["coverage","changes"]}\n```'))
    assert e.explain(batch(),'sla')['source']=='yandexgpt'
    e=Explainer(send=lambda _:response('{"points":["coverage","invented"]}'))
    assert e.explain(batch(),'sla')['source']=='template'


def test_recommendation_validates_choice_and_rejects_unknown_plan():
    from field_router.llm import Explainer
    b=batch()
    def result(choice):return {'output':[{'content':[{'type':'output_text','text':json.dumps({'candidate_id':choice,'points':['coverage','changes']})}]}]}
    e=Explainer(send=lambda _:result('sla'))
    assert e.recommend(b)['candidate_id']=='sla'
    assert e.recommend(b)['source']=='yandexgpt'
    e=Explainer(send=lambda _:result('invented'))
    assert e.recommend(b)['source']=='rule'


def test_recommendation_cannot_reduce_emergency_coverage():
    from field_router.llm import Explainer
    b=batch();b['candidates'][0]['facts']['emergency_assigned']=1
    other=deepcopy(b['candidates'][0]);other['id']='cost';other['facts']['emergency_assigned']=0;b['candidates'].append(other)
    e=Explainer(send=lambda _: {'output':[{'content':[{'type':'output_text','text':'{"candidate_id":"cost","points":["coverage","changes"]}'}]}]})
    r=e.recommend(b)
    assert r['source']=='rule' and r['candidate_id']=='sla'


def test_recommendation_explains_ties_without_repeating_three_plans():
    from field_router.llm import recommendation_text
    b=batch();other=deepcopy(b['candidates'][0]);other['id']='stable';b['candidates'].append(other)
    text=recommendation_text(b,other,['coverage','changes'])
    assert 'одинаковы' in text and 'без назначения — 1' in text
    assert 'Меньше изменений' not in text


def test_recommendation_explains_advantage_and_tradeoff():
    from field_router.llm import recommendation_text
    b=batch();other=deepcopy(b['candidates'][0]);other['id']='cost'
    other['facts'].update(unassigned=2,reassigned=0);b['candidates'].append(other)
    text=recommendation_text(b,b['candidates'][0],['coverage','changes'])
    assert 'Наименьшее' in text and 'у лучшей альтернативы — 0' in text


def test_recommendation_prompt_has_one_unambiguous_schema():
    from field_router.llm import Explainer
    def send(body):
        assert '"candidate_id":"sla"' in body['input']
        assert '"candidate_id":"stable"' not in body['input']
        assert '"selected"' not in body['input']
        return {'output':[{'content':[{'type':'output_text','text':'{"candidate_id":"sla","points":["coverage","changes"]}'}]}]}
    assert Explainer(send=send).recommend(batch())['source']=='yandexgpt'


@pytest.mark.parametrize('fence', ['```', '```json'])
def test_provider_literal_newline_fence_keeps_strict_choice_validation(fence):
    from field_router.llm import Explainer
    def response(choice):
        text=fence+r'\n'+json.dumps({'candidate_id':choice,'points':['coverage','changes']})+r'\n```'
        return {'output':[{'content':[{'type':'output_text','text':text}]}]}
    assert Explainer(send=lambda _:response('sla')).recommend(batch())['source']=='yandexgpt'
    assert Explainer(send=lambda _:response('invented')).recommend(batch())['source']=='rule'


def test_recommendation_protects_total_coverage_before_response():
 from field_router.llm import eligible_candidates
 b=batch();fast=b['candidates'][0];slow=deepcopy(fast);slow['id']='stable'
 fast['emergency']['response_minutes']=1
 slow['facts'].update(assigned=6,unassigned=0)
 slow['emergency']['response_minutes']=50
 b['candidates'].append(slow)
 assert [c['id'] for c in eligible_candidates(b)]==['stable']


def test_normal_day_recommendation_prefers_stability_and_preserved_times():
 from field_router.llm import eligible_candidates
 b=batch();a=b['candidates'][0];a['id']='cost';a['emergency']=None
 a['facts'].update(avoidable_reassigned=1,time_shift_minutes=0,travel_minutes=1)
 stable=deepcopy(a);stable['id']='stable';stable['facts'].update(avoidable_reassigned=0,travel_minutes=100)
 b['candidates'].append(stable)
 assert [c['id'] for c in eligible_candidates(b)]==['stable']


def test_llm_cannot_override_verified_recommendation_with_travel_tradeoff():
 from field_router.llm import Explainer
 b=batch();a=b['candidates'][0];a['id']='cost';a['emergency']=None
 a['facts'].update(reassigned=2,travel_minutes=1,time_shift_minutes=0)
 stable=deepcopy(a);stable['id']='stable';stable['facts'].update(reassigned=0,travel_minutes=100)
 b['candidates'].append(stable)
 e=Explainer(send=lambda _: {'output':[{'content':[{'type':'output_text','text':'{"candidate_id":"cost","points":["coverage","travel"]}'}]}]})
 result=e.recommend(b)
 assert result['candidate_id']=='stable' and result['source']=='rule'


def test_only_allowlisted_risk_aggregates_are_sent_and_rendered_by_server():
 from field_router.llm import Explainer
 b=batch();add_risk(b['candidates'][0])
 def send(body):
  context=json.loads(body['input'].splitlines()[-1]);variant=context['variants'][0]
  assert set(variant['risk'])=={'p_sla_breach','expected_loss','affected_clients','p_overtime'}
  assert variant['risk']=={'p_sla_breach':.125,'expected_loss':3.4,'affected_clients':1,'p_overtime':.075}
  assert 'routes' not in json.dumps(context) and 'duration_evidence' not in json.dumps(context)
  assert 'PRIVATE' not in json.dumps(context)
  return {'output':[{'content':[{'type':'output_text','text':'{"candidate_id":"sla","points":["coverage","risk"]}'}]}]}
 result=Explainer(send=send).recommend(b)
 assert result['source']=='yandexgpt'
 assert 'P(SLA) — 12.5%' in result['text']
 assert 'ожидаемые потери — 3.4 усл. ед.' in result['text']
 assert 'затронуто клиентов — 1' in result['text']
 assert 'P(переработки) — 7.5%' in result['text']


@pytest.mark.parametrize('bad', [float('nan'),float('inf'),True,-.1,1.1])
def test_invalid_risk_is_not_sent_or_available_as_a_point(bad):
 from field_router.llm import Explainer
 b=batch();add_risk(b['candidates'][0],p_sla=bad)
 def send(body):
  context=json.loads(body['input'].splitlines()[-1])
  assert 'risk' not in context['variants'][0]
  assert 'risk' not in context['available_points']
  return {'output':[{'content':[{'type':'output_text','text':'{"candidate_id":"sla","points":["coverage","risk"]}'}]}]}
 result=Explainer(send=send).recommend(b)
 assert result['source']=='rule' and 'P(SLA)' not in result['text']


def test_llm_cannot_override_lower_risk_verified_recommendation():
 from field_router.llm import Explainer
 b=batch();best=b['candidates'][0];best['id']='stable';add_risk(best,p_sla=.1,loss=1,affected=1,p_overtime=.1)
 worse=deepcopy(best);worse['id']='cost';add_risk(worse,p_sla=.2,loss=.1,affected=0,p_overtime=0)
 b['candidates'].append(worse)
 response={'output':[{'content':[{'type':'output_text','text':'{"candidate_id":"cost","points":["coverage","risk"]}'}]}]}
 result=Explainer(send=lambda _:response).recommend(b)
 assert result['source']=='rule' and result['candidate_id']=='stable'
