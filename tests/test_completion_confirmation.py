import json

import pytest

from field_router.engineer import engineer_state, record_engineer_fact
from field_router.replanning import ScenarioSession
from test_replanning import demo


@pytest.fixture
def day(make_scenario):
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    record_engineer_fact(session, {'version': 1, 'engineer_id': 'e1', 'status': 'working',
                                   'time': 550, 'request_id': 'a', 'remaining_minutes': 30})
    return session


def report(day):
    return record_engineer_fact(day, {'version': day.version, 'engineer_id': 'e1',
        'status': 'completed', 'time': 580, 'request_id': 'a', 'consumed_router': 1,
        'consumed_tv_box': 0, 'comment': 'Связь проверена'})


def test_engineer_report_is_pending_and_devices_are_reserved(day):
    result = report(day)
    task = next(t for t in result['tasks'] if t['id'] == 'a')
    assert task['status'] == 'completion_pending'
    assert task['completed_at'] is None
    assert task['reported_completed_at'] == 580
    assert result['inventory']['consumed']['router'] == 0
    assert result['inventory']['reserved']['router'] == 1
    assert result['inventory']['available']['router'] == 0
    assert not any(f['status'] == 'completed' for f in day.fact_log)
    assert day.state()['pending_completions'][0]['request_id'] == 'a'
    assert 'a' not in {r['id'] for r in day.fact_requests()}


def test_pending_work_is_not_reassigned_and_worker_can_continue(day):
    report(day)
    record_engineer_fact(day, {'version': day.version, 'engineer_id': 'e1',
        'status': 'traveling', 'time': 581, 'request_id': 'b', 'location_id': 'a',
        'remaining_minutes': 1})
    batch = day.preview({'version': day.version,
        'event': {'id': 'pending-replan', 'type': 'replan', 'time': 581},
        'preserve_in_progress': True}, time_limit=.03)
    for candidate in batch['candidates']:
        assert 'a' not in {v['request_id'] for r in candidate['plan']['routes'] for v in r['visits']}
        assert 'a' not in {u['request_id'] for u in candidate['plan']['unassigned']}
    day.approve({'version': day.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    stock = next(i for i in day.inventory if i['engineer_id'] == 'e1')
    assert (stock['consumed']['router'], stock['reserved']['router'], stock['available']['router']) == (0, 1, 0)
    restored = ScenarioSession.restore(json.loads(json.dumps(day.dump())))
    assert restored.state()['pending_completions'] == day.state()['pending_completions']
    assert next(t for t in engineer_state(restored, 'e1')['tasks'] if t['id'] == 'a')['status'] == 'completion_pending'
    from field_router.completion import confirm_completion
    identity = restored.state()['pending_completions'][0]['id']
    confirm_completion(restored, {'version': restored.version, 'fact_id': identity})
    assert restored.statuses['e1']['status'] == 'traveling'
    assert next(t for t in engineer_state(restored, 'e1')['tasks'] if t['id'] == 'a')['completed_at'] == 580
    stock = next(i for i in restored.inventory if i['engineer_id'] == 'e1')
    assert (stock['consumed']['router'], stock['reserved']['router']) == (1, 0)
    ledger = next(v for r in restored.frozen for v in r['visits'] if v['request_id'] == 'a')
    assert ledger['status'] == 'completed'
    from field_router.dispatcher import render_dispatcher
    html = render_dispatcher(restored.workbench())
    assert 'Завершена по факту' in html and 'Списано:' in html


def test_confirmation_replays_report_time_after_later_facts_without_double_consumption(day):
    from field_router.completion import confirm_completion
    report(day)
    identity = day.fact_log[-1]['id']
    record_engineer_fact(day, {'version': day.version, 'engineer_id': 'e1',
        'status': 'traveling', 'time': 581, 'request_id': 'b', 'location_id': 'a', 'remaining_minutes': 1})
    result = confirm_completion(day, {'version': day.version, 'fact_id': identity})
    completed = next(f for f in day.fact_log if f['id'] == identity)
    assert completed['status'] == 'completed' and completed['time'] == 580
    assert completed['source'] == 'dispatcher'
    assert day.statuses['e1']['status'] == 'traveling'
    assert day.fact_audit[-1]['previous']['status'] == 'completion_pending'
    assert not result['pending_completions']
    stock = next(i for i in day.inventory if i['engineer_id'] == 'e1')
    assert (stock['consumed']['router'], stock['reserved']['router']) == (1, 0)
    before = day.dump()
    with pytest.raises(ValueError, match='подтвержден|подтверждения'):
        confirm_completion(day, {'version': day.version, 'fact_id': identity})
    assert day.dump() == before


def test_pending_report_cannot_be_cancelled_or_completed_twice(day):
    report(day)
    before = day.dump()
    with pytest.raises(ValueError):
        report(day)
    assert day.dump() == before
    with pytest.raises(ValueError):
        day.preview({'version': day.version, 'event': {'id': 'cancel-report', 'type': 'cancel',
            'time': 580, 'request_id': 'a'}, 'preserve_in_progress': True}, time_limit=.03)


def test_invalid_or_stale_confirmation_is_atomic(day):
    from field_router.completion import confirm_completion
    report(day)
    before = day.dump()
    for payload in ({'version': day.version - 1, 'fact_id': day.fact_log[-1]['id']},
                    {'version': day.version, 'fact_id': 'missing'},
                    {'version': day.version, 'fact_id': day.fact_log[-1]['id'], 'consumed_router': 0}):
        with pytest.raises(ValueError):
            confirm_completion(day, payload)
        assert day.dump() == before


def test_risk_supply_does_not_treat_reported_consumption_as_spare(day):
    from field_router.risk_dashboard import dashboard
    report(day)
    equipment = next(row for row in dashboard(day)['indicators'] if row['id'] == 'equipment')
    router = next(row for row in equipment['facts']['equipment'] if row['id'] == 'router')
    assert router['on_hand'] == 0 and router['spare'] == 0


def test_authorized_confirmation_is_persisted_and_engineer_cannot_confirm(make_scenario, tmp_path, monkeypatch):
    from pathlib import Path
    from test_auth import Client, PASSWORD
    from field_router.auth import AccountStore
    from field_router.scenario_server import create_server
    import threading
    scenario, plan = demo(make_scenario)
    day = ScenarioSession(scenario, plan)
    accounts = AccountStore(tmp_path / 'accounts.sqlite3')
    accounts.bootstrap(PASSWORD)
    accounts.create_engineer('engineer', PASSWORD, 'default', 'e1')
    state_path = tmp_path / 'state.json'
    server = create_server(day, state_path, port=0, accounts=accounts, disable_gps=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        employee, admin = Client(server), Client(server)
        assert employee.login('engineer')[0] == admin.login('admin')[0] == 200
        assert employee.request('/api/engineer/facts', {'version': 1, 'engineer_id': 'e1',
            'status': 'working', 'time': 550, 'request_id': 'a', 'remaining_minutes': 30})[0] == 200
        status, body, _ = employee.request('/api/engineer/facts', {'version': 2, 'engineer_id': 'e1',
            'status': 'completed', 'time': 580, 'request_id': 'a'})
        assert status == 200 and body['status']['status'] == 'completion_pending'
        current = admin.request('/api/state')[1]
        command = {'version': current['version'], 'fact_id': current['pending_completions'][0]['id']}
        assert employee.request('/api/completions/confirm', command)[0] == 403
        assert admin.request('/api/completions/confirm', command, csrf=False)[0] == 403
        original = Path.replace
        def broken(*args, **kwargs):
            raise OSError('no disk space')
        monkeypatch.setattr(Path, 'replace', broken)
        assert admin.request('/api/completions/confirm', command)[0] == 500
        assert admin.request('/api/state')[1] == current
        monkeypatch.setattr(Path, 'replace', original)
        assert admin.request('/api/completions/confirm', command)[0] == 200
        stored = ScenarioSession.restore(json.loads(state_path.read_text()))
        assert stored.fact_log[-1]['status'] == 'completed'
        assert stored.fact_log[-1]['source'] == 'dispatcher'
        assert not stored.state()['pending_completions']
    finally:
        server.shutdown(); server.server_close(); thread.join()
