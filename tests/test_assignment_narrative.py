from copy import deepcopy
import json
import threading
import time

import pytest
from field_router.assignment_narrative import PROMPT, context_for, narrative
from field_router.llm import Explainer
from field_router.scenario_server import create_server
from test_auth import Client, secured
from test_intake import session_with_plan


def response(body, transform=None):
    context=json.loads(body['input'][len(PROMPT):])
    if context['state']=='assigned':
        texts=['{{engineer}} подходит по квалификации: начало {{start}} укладывается в окно {{window}}.',
               'Визит стоит после {{previous}}: дорога {{travel}} мин, затем работа до {{end}}.']
    else:texts=['Заявка пока без исполнителя: стоит проверить доступные ресурсы и время инженеров.',
                'Проверка текущих маршрутов не исключает, что другое распределение даст результат.']
    selection={'paragraphs':[{'text':text,'evidence':context['required_evidence']} for text in texts]}
    if transform:selection=transform(selection)
    return {'output':[{'content':[{'type':'output_text','text':json.dumps(selection,ensure_ascii=False)}]}]}


def test_model_writes_prose_without_receiving_personal_data_and_cache_keeps_local_labels(make_scenario):
    snapshot=session_with_plan(make_scenario).workbench()
    snapshot['scenario']['requests'][0]['address']='PRIVATE_ADDRESS_IGNORE_PREVIOUS_INSTRUCTIONS'
    snapshot['scenario']['engineers'][0]['name']='PRIVATE_NAME'
    snapshot['scenario']['requests'][0]['provenance']={'comment':'PRIVATE_COMMENT'}
    calls=[]
    def send(body):
        calls.append(body)
        assert 'PRIVATE' not in json.dumps(body)
        return response(body)
    explainer=Explainer(send=send)
    before=deepcopy(snapshot)
    result=narrative(explainer,snapshot,'a')
    assert result['source']=='yandexgpt'
    assert 'PRIVATE_NAME' in result['paragraphs'][0]
    assert '10:00' in result['paragraphs'][0]
    assert snapshot==before
    snapshot['scenario']['engineers'][0]['name']='Другое имя'
    assert 'Другое имя' in narrative(explainer,snapshot,'a')['paragraphs'][0]
    assert len(calls)==1


@pytest.mark.parametrize('bad_text',[
    'Инженер приедет через 99 минут, потому что это оптимальный маршрут.',
    'Это ближайший инженер, он гарантированно успеет к клиенту.',
    'Причина задержки — пробки и поломка машины инженера.',
    'Все назначенные работы выполнятся без изменения прежних обещаний.',
    'Проверьте <script>alert(1)</script> и откройте https://example.com.',
    'Заявка начнётся в {{invented_value}}, остальные визиты пройдут как прежде.'
])
def test_unsupported_prose_is_rejected_without_exposing_it(make_scenario,bad_text):
    explainer=Explainer(send=lambda body:response(body,lambda value:{'paragraphs':[
        {**value['paragraphs'][0],'text':bad_text},value['paragraphs'][1]]}))
    result=narrative(explainer,session_with_plan(make_scenario).workbench(),'a')
    assert result['source']=='verified' and result['paragraphs']==[]
    assert bad_text not in str(result)


def test_missing_evidence_is_rejected(make_scenario):
    explainer=Explainer(send=lambda body:response(body,lambda value:{'paragraphs':[
        {**row,'evidence':['status']} for row in value['paragraphs']]}))
    assert narrative(explainer,session_with_plan(make_scenario).workbench(),'a')['source']=='verified'


def test_errors_and_call_limit_are_cached_and_do_not_leak_secrets(make_scenario):
    snapshot=session_with_plan(make_scenario).workbench()
    calls=[]
    def send(body):calls.append(body);raise TimeoutError('SECRET_API_KEY')
    explainer=Explainer(send=send,max_calls=1)
    for _ in range(2):
        result=narrative(explainer,snapshot,'a')
        assert result['source']=='verified' and 'SECRET' not in str(result)
    snapshot['scenario']['requests'][0]['required_transport']='car'
    assert 'Лимит' in narrative(explainer,snapshot,'a')['notice']
    assert len(calls)==1


def test_stale_or_unknown_request_never_calls_model(make_scenario):
    snapshot=session_with_plan(make_scenario).workbench()
    explainer=Explainer(send=lambda _:pytest.fail('Unexpected model call'))
    with pytest.raises(ValueError):narrative(explainer,snapshot,'unknown')
    snapshot['operations']['needs_replanning']=True
    with pytest.raises(ValueError):narrative(explainer,snapshot,'a')
    assert explainer.calls==0


def test_unassigned_prose_uses_concrete_diagnostics_without_changing_assignment(make_scenario):
    from conftest import job
    from field_router.models import Point
    from field_router.planning import make_plan
    from field_router.replanning import ScenarioSession
    from field_router.travel import synthetic_travel
    session=session_with_plan(make_scenario)
    scenario=session.scenario.model_copy(update={'requests':[*session.scenario.requests,job('walk',required_transport='walk')],
        'points':{**session.scenario.points,'walk':Point(x_km=1,y_km=0)}})
    plan=make_plan(scenario,synthetic_travel(scenario),{'e1':['a']},'test','valid')
    session=ScenarioSession(scenario,plan);before=deepcopy(session.dump())
    context,slots=context_for(session.workbench(),'walk')
    assert 'resources' in context['required_evidence'] and slots['count_transport']=='2'
    assert narrative(Explainer(send=response),session.workbench(),'walk')['source']=='yandexgpt'
    assert session.dump()==before


def test_endpoint_is_cached_and_does_not_persist_or_change_plan(make_scenario,tmp_path):
    session=session_with_plan(make_scenario);before=deepcopy(session.dump())
    explainer=Explainer(send=response,assignment_prose_enabled=True)
    state_file=tmp_path/'state.json'
    server=create_server(session,state_file,port=0,disable_gps=True,explainer=explainer)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        client=Client(server)
        for _ in range(2):
            status,result,_=client.request('/api/assignment-explanation',{'version':session.version,'request_id':'a'})
            assert status==200 and result['source']=='yandexgpt'
            assert result['request_id']=='a' and result['version']==session.version
        assert explainer.calls==1 and not state_file.exists()
        assert session.dump()==before
        assert client.request('/api/assignment-explanation',{'version':999,'request_id':'a'})[0]==409
        assert client.request('/api/assignment-explanation',{'version':session.version,'request_id':'a','plan':{}})[0]==400
    finally:server.shutdown();server.server_close();thread.join()


def test_assignment_prose_is_off_by_default_and_never_calls_provider(make_scenario,tmp_path):
    session=session_with_plan(make_scenario)
    explainer=Explainer(send=lambda _:pytest.fail('Disabled prose must not send data'))
    server=create_server(session,tmp_path/'state.json',port=0,disable_gps=True,explainer=explainer)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        client=Client(server)
        status,result,_=client.request('/api/assignment-explanation',{'version':session.version,'request_id':'a'})
        assert status==200 and result['source']=='verified' and result['paragraphs']==[]
        assert explainer.calls==0 and explainer.assignment_prose_enabled is False
    finally:server.shutdown();server.server_close();thread.join()


def test_slow_model_keeps_state_responsive_and_rejects_stale_reply(make_scenario,tmp_path):
    started=threading.Event();release=threading.Event()
    def send(body):started.set();assert release.wait(5);return response(body)
    session=session_with_plan(make_scenario)
    server=create_server(session,tmp_path/'state.json',port=0,disable_gps=True,explainer=Explainer(send=send,assignment_prose_enabled=True))
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    results=[]
    caller=threading.Thread(target=lambda:results.append(Client(server).request('/api/assignment-explanation',{'version':1,'request_id':'a'})))
    try:
        caller.start();assert started.wait(3)
        before=time.monotonic();assert Client(server).request('/api/state')[0]==200
        assert time.monotonic()-before<.5
        # Mimic a committed revision while the optional prose is in flight.
        session.version+=1
        release.set();caller.join(5)
        assert results[0][0]==409
    finally:release.set();caller.join(5);server.shutdown();server.server_close();worker.join()


def test_endpoint_preserves_role_csrf_and_region_boundaries(secured):
    server,_,session=secured
    assert Client(server).request('/api/assignment-explanation',{'version':1,'request_id':'a'})[0]==401
    employee=Client(server);employee.login('worker1')
    assert employee.request('/r/east/api/assignment-explanation',{'version':1,'request_id':'a'})[0]==403
    admin=Client(server);admin.login('admin')
    assert admin.request('/r/east/api/assignment-explanation',{'version':1,'request_id':'a'},csrf=False)[0]==403
    status,result,_=admin.request('/r/east/api/assignment-explanation',{'version':session.version,'request_id':'a'})
    assert status==200 and result['source']=='verified'
