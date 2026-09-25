"""The same newly added engineer must work across crew, mobile, facts and GPS."""
import json
import threading
from copy import deepcopy
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from field_router.engineer import engineer_state
from field_router.replanning import ScenarioSession
from field_router.scenario_server import create_server
from test_replanning import demo


@pytest.mark.parametrize('solver', ['ortools', 'alns', 'hybrid'])
def test_added_engineer_completes_job_and_receives_approved_remaining_plan(make_scenario, tmp_path, solver):
    scenario, plan = demo(make_scenario)
    path = tmp_path / 'day.json'
    server = create_server(ScenarioSession(scenario, plan), path, port=0, time_limit=.1, solver=solver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'

    def api(route, payload=None):
        request = Request(base + route, data=json.dumps(payload).encode() if payload is not None else None,
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=15) as response:
            return json.load(response)

    mobile_path = '/api/engineer?' + urlencode({'engineer_id': 'crew-new'})
    try:
        crew = api('/api/crew')
        rows = deepcopy(crew['engineers'])
        # Only the new engineer can service the local jobs. Assignment cannot
        # depend on solver tie-breaking among equivalent people.
        for row in rows:
            row['skills'] = ['emergency']
        rows.append({**deepcopy(crew['engineers'][0]), 'id': 'crew-new', 'name': 'Новая бригада',
                     'skills': ['local'], 'transport': 'bicycle', 'shift': {'start': 540, 'end': 1050},
                     'start_location': 'b', 'stock_mode': 'fixed', 'router': 1, 'tv_box': 0})
        draft = api('/api/crew/preview', {'version': crew['version'], 'engineers': rows})
        assert {e['id'] for e in draft['engineers']} == {'e1', 'e2', 'crew-new'}
        assert draft['changes'] and draft['assumptions']
        assert sum(row['assigned'] for row in draft['after_coverage'].values()) == draft['after']['assigned']
        assert sum(row['total'] for row in draft['before_coverage'].values()) == len(scenario.requests)
        assert len(api('/api/engineers')['engineers']) == 2
        assert not path.exists(), 'Preview must not publish the new roster'
        with pytest.raises(HTTPError) as unknown:
            api(mobile_path)
        assert unknown.value.code == 400

        applied = api('/api/crew/apply', {'version': crew['version'], 'id': draft['id']})
        assert applied['plan']['solver'] == solver
        mobile = api(mobile_path)
        index = api('/api/engineers')
        gps = api('/api/gps')
        assert applied['version'] == mobile['version'] == index['version'] == gps['plan_version'] == 2
        assert {e['id'] for e in index['engineers']} == {'e1', 'e2', 'crew-new'}
        assert {r['engineer_id'] for r in gps['rows']} == {'e1', 'e2', 'crew-new'}
        assert mobile['engineer']['name'] == 'Новая бригада'
        assert mobile['engineer']['transport'] == 'bicycle'
        assert mobile['engineer']['shift'] == {'start': 540, 'end': 1050}
        assert mobile['engineer']['stock'] == {'router': 1, 'tv_box': 0}
        assert {t['id'] for t in mobile['tasks']} == {'a', 'b'}
        assert all(r['engineer_id'] == 'crew-new' for r in applied['plan']['routes'])
        start = mobile['engineer']['start_location']
        places = {p['id']: p['label'] for p in mobile['locations']}
        assert start == 'home:crew-new'
        assert places[start] == 'Старт: Address b'
        assert places['office'] == 'Офис'

        def fact(status, time, **fields):
            nonlocal mobile
            mobile = api('/api/engineer/facts', {'version': mobile['version'], 'engineer_id': 'crew-new',
                         'status': status, 'time': time, **fields})

        fact('available', 540, location_id=start, comment='Вышел из стартовой точки')
        fact('traveling', 541, request_id='a', location_id=start, remaining_minutes=9)
        fact('working', 550, request_id='a', remaining_minutes=30)
        fact('completed', 580, request_id='a', consumed_router=1, consumed_tv_box=0,
             comment='Установил роутер, связь проверена')
        dispatch = api('/api/state')
        assert dispatch['version'] == mobile['version'] == 6
        assert dispatch['needs_replanning']
        assert [f['status'] for f in dispatch['fact_journal']] == ['available', 'traveling', 'working', 'completion_pending']
        assert all(f['engineer_id'] == 'crew-new' and f['source'] == 'engineer' for f in dispatch['fact_journal'])
        stock = next(i for i in dispatch['inventory'] if i['engineer_id'] == 'crew-new')
        assert stock['consumed']['router'] == 0 and stock['reserved']['router'] == 1
        assert next(t for t in mobile['tasks'] if t['id'] == 'a')['status'] == 'completion_pending'
        dispatch = api('/api/completions/confirm', {'version': dispatch['version'],
            'fact_id': dispatch['pending_completions'][0]['id']})
        mobile = api(mobile_path)
        assert dispatch['version'] == mobile['version'] == 7
        assert not dispatch['pending_completions']
        assert dispatch['fact_journal'][-1]['source'] == 'dispatcher'
        stock = next(i for i in dispatch['inventory'] if i['engineer_id'] == 'crew-new')
        assert stock['consumed']['router'] == 1 and stock['reserved']['router'] == 0 and stock['available']['router'] == 0
        assert not api('/api/crew')['editable'], 'A started day must not be reset via morning settings'

        batch = api('/api/preview', {'version': dispatch['version'], 'event': {
            'id': 'after-mobile-completion', 'type': 'replan', 'time': 580}, 'preserve_in_progress': True})
        assert all(candidate['plan']['solver'] == solver for candidate in batch['candidates'])
        assert api(mobile_path)['version'] == 7, 'Preview must not replace the mobile plan'
        approved = api('/api/approve', {'version': dispatch['version'], 'batch_id': batch['id'],
                                     'candidate_id': batch['candidates'][0]['id']})
        mobile = api(mobile_path)
        assert approved['version'] == mobile['version'] == api('/api/gps')['plan_version'] == 8
        assert not mobile['needs_replanning']
        risk = api('/api/risk')
        assert risk['context']['plan_version'] == mobile['version']
        assert not risk['context']['needs_replanning']
        assert risk['forecast']['status'] == 'ready'
        assert risk['forecast']['model_version'].startswith('risk-engine-v1')
        assert {item['id'] for item in risk['forecast']['risks']} == {'sla', 'late', 'overtime', 'vehicle'}
        tasks = {t['id']: t for t in mobile['tasks']}
        assert tasks['a']['status'] == 'completed' and tasks['a']['completed_at'] == 580
        assert tasks['a']['history'][-1]['comment'] == 'Установил роутер, связь проверена'
        assert tasks['b']['status'] == 'planned'
        assert [v['request_id'] for r in approved['plan']['routes'] for v in r['visits']] == ['b']
        restored = ScenarioSession.restore(json.loads(path.read_text(encoding='utf-8')))
        assert engineer_state(restored, 'crew-new') == mobile
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
