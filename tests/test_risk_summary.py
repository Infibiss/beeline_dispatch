import json
from field_router.llm import Explainer


def snapshot():
    return {'context':{'plan_version':7,'region':'PRIVATE'},'summary':{'unassigned':2},
            'indicators':[], 'forecast':{'status':'ready','plan_version':7,'region':'PRIVATE',
            'review':{'sla':{'count':1,'total':1},'late':{'count':0,'total':1},'overtime':{'count':1,'total':1}},
            'routes':[{'engineer_name':'PRIVATE','p_overtime':.4,'visits':[{'address':'PRIVATE','p_sla_breach':.3,'p_late_start':.5}]}]}}


def test_summary_sends_only_verified_points_and_caches():
    calls=[]
    def send(body):
        calls.append(body)
        assert 'PRIVATE' not in json.dumps(body)
        return {'output':[{'content':[{'type':'output_text','text':'{"points":["sla","overtime"]}'}]}]}
    e=Explainer(send=send)
    result=e.summarize_day(snapshot())
    assert result['source']=='yandexgpt'
    assert result['items'][0]['id']=='unassigned'
    assert len(result['items'])<=3 and result['action']
    assert e.summarize_day(snapshot())==result and len(calls)==1


def test_summary_rejects_invented_findings_and_respects_budget():
    e=Explainer(send=lambda _: {'output':[{'content':[{'type':'output_text','text':'{"points":["invented"]}'}]}]},max_calls=1)
    result=e.summarize_day(snapshot())
    assert result['source']=='template' and 'invented' not in str(result)
    changed=snapshot();changed['summary']['unassigned']=3
    assert e.summarize_day(changed)['source']=='template' and e.calls==1


def test_missing_forecast_is_not_all_clear():
    e=Explainer(send=lambda _: {},max_calls=0)
    result=e.summarize_day({'context':{},'summary':{'unassigned':0},'indicators':[],'forecast':{'status':'error'}})
    assert any(x['id']=='forecast_missing' for x in result['items'])


def test_immediate_actions_cannot_be_hidden_by_ai_model_ranking():
    data=snapshot()
    data['context']['time']=600
    data['attention']={'plan_version':7,'time':600,'tiers':{'now':2},'incoming':{'new':1}}
    explainer=Explainer(send=lambda _: {'output':[{'content':[{'type':'output_text','text':'{"points":["sla"]}'}]}]})
    result=explainer.summarize_day(data)
    assert [r['id'] for r in result['items']]==['act_now','incoming','unassigned']


def test_summary_http_rejects_foreign_and_stale_snapshots(make_scenario,tmp_path):
    import threading
    from field_router.scenario_server import create_server
    from field_router.replanning import ScenarioSession
    from test_replanning import demo
    from test_auth import Client
    scenario,plan=demo(make_scenario)
    session=ScenarioSession(scenario,plan)
    other=ScenarioSession(scenario,plan)
    explainer=Explainer(send=lambda _: {},max_calls=0)
    server=create_server(session,tmp_path/'state.json',port=0,disable_gps=True,explainer=explainer,
                         regions={'other':(other,tmp_path/'other.json')})
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        client=Client(server)
        status,data,_=client.request('/api/risk');assert status==200 and data['llm_enabled']
        payload={'snapshot_id':data['snapshot_id'],'version':session.version}
        assert client.request('/api/risk/summary',payload)[0]==200
        assert client.request('/regions/other/api/risk/summary',payload)[0]==409
        session.version+=1
        assert client.request('/api/risk/summary',payload)[0]==409
    finally:server.shutdown();server.server_close();thread.join()
