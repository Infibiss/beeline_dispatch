"""A demo restart is an isolated, durable new run, including async work."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

import pytest

from field_router.scenario_server import create_server
from field_router.replanning import ScenarioSession
from test_intake import incoming, session_with_plan


@contextmanager
def running(session, path, **options):
    server=create_server(session,path,port=0,time_limit=.03,disable_gps=True,**options)
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    opener=build_opener(ProxyHandler({}))
    def call(route, payload=None):
        request=Request(f'http://127.0.0.1:{server.server_port}'+route,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Content-Type':'application/json'})
        with opener.open(request,timeout=10) as response:return json.load(response)
    try:yield call
    finally:
        server.shutdown();server.server_close();worker.join()


def apply_request(call, run_id, key='arrival'):
    call('/api/inbox/requests',{'demo_run_id':run_id,'request':incoming(key)})
    state=call('/api/state')
    batch=call('/api/preview',{'demo_run_id':run_id,'version':state['version'],
        'event':{'id':'replan-'+key,'type':'replan','time':state['time']}})
    return call('/api/approve',{'demo_run_id':run_id,'version':state['version'],
        'batch_id':batch['id'],'candidate_id':batch['recommended_candidate_id']})


def test_reset_restores_identical_baseline_and_archives_each_run(make_scenario,tmp_path):
    baseline=session_with_plan(make_scenario)
    expected=baseline.dump()
    path=tmp_path/'demo.json'
    with running(deepcopy(baseline),path,demo_reset_session=baseline,
                 demo_stream_requests=[incoming('arrival')]) as call:
        state=call('/api/state')
        for iteration in range(2):
            old_id=state['demo_run_id']
            for route in ('/api/revision','/api/inbox','/api/demo-stream/requests'):
                assert call(route)['demo_run_id']==old_id
            state=apply_request(call,old_id)
            assert call('/api/inbox')['decision_log']
            old_bytes=path.read_bytes()
            state=call('/api/demo-stream/reset',{'version':state['version'],'demo_run_id':old_id})
            assert state['demo_run_id']!=old_id
            assert state['demo_reset_enabled'] and state['demo_stream_enabled']
            assert state['version']==1 and state['time']==baseline.time
            saved=json.loads(path.read_text(encoding='utf-8'))
            assert saved.pop('demo_run_id')==state['demo_run_id']
            assert saved==json.loads(json.dumps(expected))
            inbox=call('/api/inbox')
            assert inbox['incoming_requests']==[]
            assert inbox['dispatch_notifications']==[]
            assert inbox['decision_log']==[]
            assert inbox['last_applied'] is None
            archives=list((tmp_path/'demo-archives').glob('*.json'))
            assert len(archives)==iteration+1
            assert old_bytes in [archive.read_bytes() for archive in archives]


def test_stale_demo_run_cannot_reenter_after_version_is_reused(make_scenario,tmp_path):
    baseline=session_with_plan(make_scenario)
    with running(deepcopy(baseline),tmp_path/'demo.json',demo_reset_session=baseline) as call:
        first=call('/api/state')
        fresh=call('/api/demo-stream/reset',{'version':1,'demo_run_id':first['demo_run_id']})
        with pytest.raises(HTTPError) as caught:
            call('/dispatcher?version=1&demo_run_id='+first['demo_run_id'])
        assert caught.value.code==409
        for route,payload in (
            ('/api/inbox/requests',{'request':incoming('stale')}),
            ('/api/inbox/settings',{'version':1,'mode':'autopilot','every_n_requests':1,'every_n_minutes':None}),
            ('/api/demo-stream/reset',{'version':1})):
            with pytest.raises(HTTPError) as caught:
                call(route,{**payload,'demo_run_id':first['demo_run_id']})
            assert caught.value.code==409
        with pytest.raises(HTTPError) as caught:
            call('/api/inbox/requests',{'request':incoming('missing-generation')})
        assert caught.value.code==409
        assert call('/api/state')['version']==1
        assert call('/api/inbox')['incoming_requests']==[]
        assert call('/api/inbox/requests',{'demo_run_id':fresh['demo_run_id'],
            'request':incoming('new-run')})['added']==1


def test_demo_generation_is_retained_when_saved_run_is_resumed(make_scenario,tmp_path):
    baseline=session_with_plan(make_scenario)
    path=tmp_path/'demo.json'
    with running(deepcopy(baseline),path,demo_reset_session=baseline) as call:
        first=call('/api/state')
        call('/api/inbox/requests',{'demo_run_id':first['demo_run_id'],'request':incoming('persisted')})
    restored=ScenarioSession.restore(json.loads(path.read_text(encoding='utf-8')))
    with running(restored,path,demo_reset_session=baseline) as call:
        state=call('/api/state')
        assert state['demo_run_id']==first['demo_run_id']
        assert call('/api/inbox')['incoming_requests'][0]['id']=='persisted'
        fresh=call('/api/demo-stream/reset',{'version':state['version'],'demo_run_id':state['demo_run_id']})
        assert fresh['demo_run_id']!=first['demo_run_id']


def test_reset_accepts_version_advanced_by_autopilot_but_not_future_version(make_scenario,tmp_path):
    baseline=session_with_plan(make_scenario)
    with running(deepcopy(baseline),tmp_path/'demo.json',demo_reset_session=baseline) as call:
        before=call('/api/state')
        applied=apply_request(call,before['demo_run_id'])
        assert applied['version']>before['version']
        with pytest.raises(HTTPError) as caught:
            call('/api/demo-stream/reset',{'version':applied['version']+1,'demo_run_id':before['demo_run_id']})
        assert caught.value.code==409
        fresh=call('/api/demo-stream/reset',{'version':before['version'],'demo_run_id':before['demo_run_id']})
        assert fresh['version']==1
        assert fresh['demo_run_id']!=before['demo_run_id']
        assert not call('/api/inbox')['incoming_requests']


def test_failed_reset_does_not_publish_state_or_generation(make_scenario,tmp_path,monkeypatch):
    baseline=session_with_plan(make_scenario)
    path=tmp_path/'demo.json'
    with running(deepcopy(baseline),path,demo_reset_session=baseline) as call:
        state=call('/api/state')
        call('/api/inbox/requests',{'demo_run_id':state['demo_run_id'],'request':incoming('kept')})
        before=call('/api/state')
        saved=path.read_bytes()
        original=Path.replace
        def fail_replace(self,target):
            if Path(target)==path:raise OSError('test disk unavailable')
            return original(self,target)
        monkeypatch.setattr(Path,'replace',fail_replace)
        with pytest.raises(HTTPError) as caught:
            call('/api/demo-stream/reset',{'version':before['version'],'demo_run_id':before['demo_run_id']})
        assert caught.value.code==500
        assert call('/api/state')==before
        assert path.read_bytes()==saved
        assert len(call('/api/inbox')['incoming_requests'])==1


def test_reset_disabled_outside_explicit_default_demo(make_scenario,tmp_path):
    baseline=session_with_plan(make_scenario)
    with running(deepcopy(baseline),tmp_path/'normal.json') as call:
        assert not call('/api/state')['demo_reset_enabled']
        with pytest.raises(HTTPError) as caught:call('/api/demo-stream/reset',{'version':1})
        assert caught.value.code==404
        assert call('/api/inbox/requests',{'request':incoming('normal')})['added']==1
    other=deepcopy(baseline)
    other.scenario=other.scenario.model_copy(update={'region':'Other'})
    with running(deepcopy(baseline),tmp_path/'demo.json',demo_reset_session=baseline,
                 demo_stream_requests=[],regions={'other':(other,tmp_path/'other.json')}) as call:
        assert not call('/r/other/api/state')['demo_reset_enabled']
        assert not call('/r/other/api/state')['demo_stream_enabled']
        with pytest.raises(HTTPError) as caught:call('/r/other/api/demo-stream/reset',{'version':1})
        assert caught.value.code==404
        assert call('/r/other/api/state')['version']==1
        assert not (tmp_path/'other.json').exists()


def test_old_async_explanation_cannot_fill_new_run_with_same_version(make_scenario,tmp_path):
    baseline=session_with_plan(make_scenario)
    entered=threading.Event()
    release=threading.Event()
    returned=threading.Event()
    class Explainer:
        calls=0
        def explain(self,batch,chosen_id):
            self.calls+=1
            if self.calls==1:
                entered.set()
                release.wait(5)
                returned.set()
                return {'source':'yandexgpt','text':'OLD RUN EXPLANATION'}
            return {'source':'yandexgpt','text':'NEW RUN EXPLANATION'}
    with running(deepcopy(baseline),tmp_path/'demo.json',demo_reset_session=baseline,
                 explainer=Explainer()) as call:
        first=call('/api/state')
        applied=apply_request(call,first['demo_run_id'])
        assert entered.wait(2)
        fresh=call('/api/demo-stream/reset',{'version':applied['version'],'demo_run_id':first['demo_run_id']})
        new_applied=apply_request(call,fresh['demo_run_id'])
        assert new_applied['version']==applied['version']
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            inbox=call('/api/inbox')
            if any(row.get('kind')=='explanation_ready' for row in inbox['dispatch_notifications']):break
            time.sleep(.02)
        assert inbox['decision_log'][0]['plan_explanation']=='NEW RUN EXPLANATION'
        release.set()
        assert returned.wait(2)
        time.sleep(.1)
        final=call('/api/inbox')
        assert final['decision_log']==inbox['decision_log']
        assert final['dispatch_notifications']==inbox['dispatch_notifications']
