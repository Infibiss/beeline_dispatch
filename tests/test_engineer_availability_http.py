"""Engineer availability reports reach the shared day without bypassing identity."""
import json
from pathlib import Path

from field_router.engineer import engineer_state
from field_router.replanning import ScenarioSession
from test_auth import Client, secured


def test_engineer_reports_period_with_own_identity_and_pending_tasks(secured, tmp_path):
    server, _, _ = secured
    employee = Client(server)
    admin = Client(server)
    assert employee.login('worker1')[0] == 200
    assert admin.login('admin')[0] == 200
    report = {'version': 1, 'engineer_id': 'e1',
              'start_date': '2026-08-17', 'start_time': '09:00',
              'end_date': '2026-08-19', 'end_time': '09:00',
              'reason': 'personal', 'comment': 'Не могу выйти на смену',
              'location_id': 'office'}

    for path, payload, options in (
        ('/api/engineer/unavailability', {**report, 'engineer_id': 'e2'}, {}),
        ('/r/south-east/api/engineer/unavailability', report, {}),
        ('/api/engineer/unavailability', report, {'csrf': False}),
        ('/api/engineer/unavailability', report, {'origin': 'https://foreign.example'}),
    ):
        assert employee.request(path, payload, **options)[0] == 403
    assert admin.request('/api/state')[1]['version'] == 1

    status, mobile, _ = employee.request('/r/east/api/engineer/unavailability', report)
    assert status == 200
    assert mobile['version'] == 2
    assert mobile['pending_reassignment_ids'] == ['a', 'b']
    assert all(task['status'] == 'pending_replan' for task in mobile['tasks'])
    assert mobile['unavailability'][0]['comment'] == report['comment']
    assert employee.request('/api/engineer?engineer_id=e1')[1] == mobile
    other_engineer = admin.request('/api/engineer?engineer_id=e2')[1]
    assert other_engineer['unavailability'] == []
    assert other_engineer['pending_reassignment_ids'] == []

    state = admin.request('/api/state')[1]
    assert state['needs_replanning']
    assert {row['request_id'] for row in state['replanning_pool']} == {'a', 'b'}
    assert {row['id'] for row in state['requests']} == {'a', 'b'}
    restored = ScenarioSession.restore(json.loads((tmp_path / 'state.json').read_text(encoding='utf-8')))
    assert engineer_state(restored, 'e1')['pending_reassignment_ids'] == ['a', 'b']

    blocked = {'version': 2, 'engineer_id': 'e1', 'time': 541, 'status': 'traveling',
               'request_id': 'a', 'location_id': 'office', 'remaining_minutes': 10}
    assert employee.request('/api/engineer/facts', blocked)[0] == 400
    assert admin.request('/api/state')[1]['version'] == 2
    assert employee.request('/api/engineer/unavailability', report)[0] == 409


def test_declined_visit_is_saved_for_replanning_and_failed_save_is_not_published(
        secured, tmp_path, monkeypatch):
    server, _, _ = secured
    employee = Client(server)
    admin = Client(server)
    assert employee.login('worker1')[0] == 200
    assert admin.login('admin')[0] == 200
    report = {'version': 1, 'engineer_id': 'e1', 'request_id': 'b',
              'time': 540, 'comment': 'Нет возможности выехать'}
    assert employee.request('/api/engineer/decline', {**report, 'engineer_id': 'e2'})[0] == 403
    assert employee.request('/api/engineer/decline', {**report, 'request_id': 'unknown'})[0] == 400

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'replace', lambda *_: (_ for _ in ()).throw(OSError('save failed')))
        assert employee.request('/api/engineer/decline', report)[0] == 500
    assert admin.request('/api/state')[1]['version'] == 1

    status, mobile, _ = employee.request('/api/engineer/decline', report)
    assert status == 200
    assert mobile['pending_reassignment_ids'] == ['b']
    assert next(task for task in mobile['tasks'] if task['id'] == 'b')['status'] == 'pending_replan'
    assert next(task for task in mobile['tasks'] if task['id'] == 'a')['status'] == 'planned'
    assert mobile['declined_assignments'][0]['comment'] == report['comment']
    state = admin.request('/api/state')[1]
    assert any(row['request_id'] == 'b' for row in state['replanning_pool'])
    assert {row['id'] for row in state['requests']} == {'a', 'b'}
    restored = ScenarioSession.restore(json.loads((tmp_path / 'state.json').read_text(encoding='utf-8')))
    assert engineer_state(restored, 'e1')['pending_reassignment_ids'] == ['b']
    assert employee.request('/api/engineer/decline', {**report, 'version': 2})[0] == 400
    assert employee.request('/api/engineer/facts', {
        'version': 2, 'engineer_id': 'e1', 'time': 541, 'status': 'traveling',
        'request_id': 'b', 'location_id': 'office', 'remaining_minutes': 10})[0] == 400


def test_only_dispatcher_can_revoke_report_and_audit_is_persisted(secured, tmp_path):
    server, _, _ = secured
    employee = Client(server)
    admin = Client(server)
    assert employee.login('worker1')[0] == 200
    assert admin.login('admin')[0] == 200
    status, mobile, _ = employee.request('/api/engineer/decline', {
        'version': 1, 'engineer_id': 'e1', 'request_id': 'b',
        'time': 540, 'comment': 'Ошибочно отказался от выезда'})
    assert status == 200
    report_id = mobile['declined_assignments'][0]['id']
    correction = {'version': 2, 'report_id': report_id,
                  'reason': 'Инженер подтвердил возможность выезда'}
    assert employee.request('/api/engineer/revoke-report', correction)[0] == 403
    assert admin.request('/api/state')[1]['version'] == 2
    status, state, _ = admin.request('/api/engineer/revoke-report', correction)
    assert status == 200
    assert state['version'] == 3
    assert state['declined_assignments'][0]['revoked']
    assert state['history'][-1]['event']['report_type'] == 'decline'
    saved = ScenarioSession.restore(json.loads((tmp_path / 'state.json').read_text(encoding='utf-8')))
    assert saved.declined_assignments[0]['revoked']
