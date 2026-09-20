import json
from copy import deepcopy
import pytest

def batch():
    return {'id':'batch-private','version':1,'candidates':[{'id':'sla','facts':{'assigned':5,'unassigned':1,'distance_m':1000,'reassigned':2,'address':'PRIVATE'},'emergency':{'assigned':True,'response_minutes':15,'engineer_id':'PRIVATE'},'explanation':'Резервное объяснение','plan':{'secret':'PRIVATE'}}]}

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
