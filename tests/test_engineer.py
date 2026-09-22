import json
import threading
from http.client import HTTPConnection
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from field_router.engineer import engineer_state, record_engineer_fact
from field_router.replanning import ScenarioSession
from field_router.scenario_server import create_server
from test_replanning import demo


@pytest.fixture
def session(make_scenario):
    scenario, plan = demo(make_scenario)
    return ScenarioSession(scenario, plan)


def record(session, status, time, **kwargs):
    return record_engineer_fact(session, {
        'version': session.version, 'engineer_id': 'e1', 'status': status,
        'time': time, **kwargs,
    })


def test_mobile_day_shares_facts_inventory_and_archive(session):
    assert [t['id'] for t in engineer_state(session, 'e1')['tasks']] == ['a', 'b']
    assert engineer_state(session, 'e2')['tasks'] == []
    record(session, 'available', 540, location_id='office')
    record(session, 'traveling', 541, request_id='a', location_id='office', remaining_minutes=9)
    record(session, 'working', 550, request_id='a', remaining_minutes=30)
    result = record(session, 'completed', 580, request_id='a', consumed_router=1,
                    consumed_tv_box=0, comment='Связь проверена. <img src=x onerror=alert(1)>')
    assert result['version'] == 5
    assert result['tasks'][0]['status'] == 'completion_pending'
    assert result['tasks'][0]['reported_completed_at'] == 580
    assert result['inventory']['reserved']['router'] == 1
    assert result['inventory']['consumed']['router'] == 0
    assert session.state()['needs_replanning']
    assert all(f['source'] == 'engineer' for f in session.fact_log)
    restored = ScenarioSession.restore(json.loads(json.dumps(session.dump())))
    assert engineer_state(restored, 'e1') == result
    batch = session.preview({'version': session.version, 'event': {
        'id': 'after-completion', 'type': 'replan', 'time': 580},
        'preserve_in_progress': True}, time_limit=.03)
    session.approve({'version': session.version, 'batch_id': batch['id'],
                     'candidate_id': batch['candidates'][0]['id']})
    archived = next(t for t in engineer_state(session, 'e1')['tasks'] if t['id'] == 'a')
    assert archived['status'] == 'completion_pending'
    assert archived['history'][-1]['comment'].startswith('Связь проверена')


def test_mobile_does_not_infer_completion_from_time(session):
    session.time = 800
    assert all(t['status'] == 'planned' for t in engineer_state(session, 'e1')['tasks'])


def test_invalid_mobile_changes_do_not_mutate_session(session):
    before = session.dump()
    invalid = [
        dict(version=0, engineer_id='e1', status='available', time=540, location_id='office'),
        dict(version=1, engineer_id='e2', status='working', time=550, request_id='a', remaining_minutes=30),
        dict(version=1, engineer_id='e1', status='completed', time=580, request_id='a'),
        dict(version=1, engineer_id='e1', status='unavailable', time=540, location_id='office'),
        dict(version=1, engineer_id='e1', status='available', time=540, location_id='office', fact_id='fake'),
    ]
    for payload in invalid:
        with pytest.raises(ValueError):
            record_engineer_fact(session, payload)
        assert session.dump() == before


def test_only_one_active_mobile_trip_and_explicit_interruption(session):
    record(session, 'traveling', 541, request_id='a', location_id='office', remaining_minutes=9)
    with pytest.raises(ValueError, match='текущего выезда'):
        record(session, 'working', 550, request_id='b', remaining_minutes=30)
    record(session, 'working', 550, request_id='a', remaining_minutes=30)
    with pytest.raises(ValueError, match='завершите'):
        record(session, 'unavailable', 551, location_id='a', comment='Поломка инструмента')
    record(session, 'interrupted', 555, request_id='a', remaining_minutes=25,
           consumed_router=0, consumed_tv_box=0, comment='Нужен другой инструмент')
    result = record(session, 'unavailable', 556, location_id='a', comment='Возвращаюсь за инструментом')
    assert result['status']['status'] == 'unavailable'
    assert result['tasks'][0]['status'] == 'interrupted'


def test_dispatcher_correction_and_late_replay_preserve_mobile_report(session):
    record(session, 'available', 540, location_id='office', comment='Вышел на смену')
    identity = session.fact_log[0]['id']
    session.record_fact({'version': session.version, 'engineer_id': 'e2',
                         'status': 'available', 'time': 541, 'location_id': 'office'})
    assert session.fact_log[0]['source'] == 'engineer'
    session.record_fact({'version': session.version, 'engineer_id': 'e1',
                         'status': 'available', 'time': 539, 'location_id': 'office',
                         'fact_id': identity, 'correction_reason': 'Уточнение времени'})
    assert session.fact_log[0]['source'] == 'engineer'
    assert session.fact_log[0]['comment'] == 'Вышел на смену'


def test_dispatcher_cancel_removes_assignment_from_mobile(session):
    batch = session.preview({'version': 1, 'event': {'id': 'cancel-b', 'type': 'cancel',
        'time': 540, 'request_id': 'b'}}, time_limit=.03)
    session.approve({'version': 1, 'batch_id': batch['id'], 'candidate_id': batch['candidates'][0]['id']})
    assert all(t['id'] != 'b' for t in engineer_state(session, 'e1')['tasks'])


def test_mobile_api_persistence_lan_hosts_and_atomic_write(session, tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr('field_router.scenario_server.local_ipv4_addresses', lambda: ['127.0.0.1', '192.168.77.9'])
    path = tmp_path / 'state.json'
    server = create_server(session, path, host='0.0.0.0', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    def post(payload):
        return urlopen(Request(base + '/api/engineer/facts',
            data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'}))
    try:
        for route in ('/engineer', '/engineer?engineer_id=e1', '/engineer.js', '/engineer.css', '/api/engineers'):
            with urlopen(base + route) as response:
                assert response.status == 200
                if route == '/engineer':
                    policy = response.headers['Content-Security-Policy']
                    assert 'https://tile.openstreetmap.org' in policy
                    assert "script-src 'self';" in policy
        for authority, origin, expected in [
            (f'192.168.77.9:{server.server_port}', None, 200),
            (f'192.168.77.9:{server.server_port}', f'http://192.168.77.9:{server.server_port}', 200),
            (f'192.168.77.9:{server.server_port}', 'https://evil.example', 403),
            (f'192.168.77.8:{server.server_port}', None, 403),
            ('evil.example', None, 403),
        ]:
            connection = HTTPConnection('127.0.0.1', server.server_port)
            headers = {'Host': authority}
            if origin: headers['Origin'] = origin
            connection.request('GET', '/api/engineers', headers=headers)
            response = connection.getresponse()
            assert response.status == expected
            response.read(); connection.close()
        payload = {'version':1, 'engineer_id':'e1', 'status':'available', 'time':540,
                   'location_id':'office', 'comment':'На смене'}
        with post(payload) as response:
            assert json.load(response)['version'] == 2
        assert json.loads(path.read_text(encoding='utf-8'))['fact_log'][0]['source'] == 'engineer'
        with pytest.raises(HTTPError) as error:
            post(payload)
        assert error.value.code == 409
        def fail_replace(*args):
            raise OSError('disk unavailable')
        monkeypatch.setattr(Path, 'replace', fail_replace)
        with pytest.raises(HTTPError) as error:
            post({**payload, 'version':2, 'time':541})
        assert error.value.code == 500
        with urlopen(base + '/api/engineer?engineer_id=e1') as response:
            result = json.load(response)
            assert result['version'] == 2 and len(result['facts']) == 1
        for query in ('', '?engineer_id=e1&engineer_id=e2', '?engineer_id=missing'):
            with pytest.raises(HTTPError) as error:
                urlopen(base + '/api/engineer' + query)
            assert error.value.code == 400
    finally:
        server.shutdown(); server.server_close(); thread.join()
