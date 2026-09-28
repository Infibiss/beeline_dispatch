"""An early return closes only the elapsed absence and awaits dispatch approval."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from field_router.engineer import engineer_state, record_engineer_fact
from field_router.gps.context import project_context
from field_router.availability import availability_at
from field_router.replanning import ScenarioSession
from test_auth import Client, secured
from test_engineer_unavailability import session_with_route, report, preview_replan


def return_payload(session, **changes):
    period = next(p for p in session.unavailability if p['engineer_id'] == 'e1')
    payload = {'version': session.version, 'engineer_id': 'e1', 'report_id': period['id'],
               'time': 570, 'location_id': 'office'}
    return {**payload, **changes}


@pytest.mark.parametrize('approve_absence', [False, True])
def test_early_return_keeps_history_and_waits_for_new_assignments(make_scenario, approve_absence):
    session = session_with_route(make_scenario)
    report(session, engineer_id='e2', end_time='18:00')
    report(session, end_time='12:00')
    if approve_absence:
        batch = preview_replan(session)
        session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    before = session.plan.model_dump()
    old_batch = preview_replan(session, 'obsolete')
    session.return_from_absence(return_payload(session))
    period = next(p for p in session.unavailability if p['engineer_id'] == 'e1')
    assert (period['start_time'], period['end_time']) == ('09:00', '09:30')
    assert period['early_return']['original_end_time'] == '12:00'
    assert not period.get('revoked')
    assert session.statuses['e1']['status'] == 'available'
    assert session.statuses['e1']['source'] == 'engineer'
    assert session.plan.model_dump() == before
    assert session.needs_replanning and not session.batches
    assert session.history[-1]['event']['type'] == 'engineer_return'
    assert session.dispatch_notifications[-1]['engineer_id'] == 'e1'
    assert session.dispatch_notifications[-1]['kind'] == 'replan_required'
    assert engineer_state(session, 'e1')['return_pending_replan']
    with pytest.raises(ValueError):
        session.approve({'version': session.version, 'batch_id': old_batch['id'], 'candidate_id': 'stable'})
    restored = ScenarioSession.restore(session.dump())
    assert restored.unavailability == session.unavailability
    assert restored.pending_report_ids == session.pending_report_ids
    batch = preview_replan(restored, 'after-return')
    restored.approve({'version': restored.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    mobile = engineer_state(restored, 'e1')
    assert not mobile['return_pending_replan']
    assert mobile['active_absence'] is None
    assert mobile['pending_reassignment_ids'] == []
    assert mobile['tasks']
    first = mobile['tasks'][0]
    record_engineer_fact(restored, {'version': restored.version, 'engineer_id': 'e1',
        'status': 'traveling', 'request_id': first['id'], 'time': 571,
        'location_id': 'office', 'remaining_minutes': 1})


@pytest.mark.parametrize('now', [540, 570])
def test_unapproved_old_assignments_cannot_be_started_even_for_zero_length_absence(make_scenario, now):
    session = session_with_route(make_scenario)
    report(session, end_time='12:00')
    session.return_from_absence(return_payload(session, time=now))
    assert session.pending_reassignment_ids('e1') == ['a', 'b']
    before = session.dump()
    with pytest.raises(ValueError, match='перепланирование'):
        record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
            'status': 'traveling', 'time': now+1, 'request_id': 'a',
            'location_id': 'office', 'remaining_minutes': 1})
    assert session.dump() == before


def test_return_does_not_clear_other_absences_or_audit(make_scenario):
    session = session_with_route(make_scenario)
    report(session, end_time='12:00')
    report(session, start_time='13:00', end_time='14:00', location_id=None)
    report(session, engineer_id='e2', end_time='18:00')
    others = deepcopy(session.unavailability[1:])
    session.return_from_absence(return_payload(session))
    assert session.unavailability[1:] == others
    gps = project_context(session)['engineers']['e1']
    assert availability_at(gps['availability'], 570) == 'available'
    assert availability_at(gps['availability'], 780) == 'absent'
    assert availability_at(gps['availability'], 840) == 'awaiting_return'
    before = session.dump()
    with pytest.raises(ValueError, match='завершён'):
        session.return_from_absence(return_payload(session))
    assert session.dump() == before


def test_multi_day_return_keeps_original_dates(make_scenario):
    session = session_with_route(make_scenario)
    report(session, end_date='2026-08-20', end_time='18:00')
    session.return_from_absence(return_payload(session))
    period = session.unavailability[0]
    assert period['end_date'] == '2026-08-17'
    assert period['early_return']['original_end_date'] == '2026-08-20'


@pytest.mark.parametrize('changes', [
    {'version': 1}, {'engineer_id': 'e2'}, {'report_id': 'missing'},
    {'time': 539}, {'time': 720}, {'time': True}, {'time': '09:30'},
    {'location_id': 'missing'}, {'unexpected': True},
])
def test_invalid_return_is_atomic(make_scenario, changes):
    session = session_with_route(make_scenario)
    report(session, end_time='12:00')
    before = session.dump()
    with pytest.raises(ValueError):
        session.return_from_absence(return_payload(session, **changes))
    assert session.dump() == before


def test_http_return_is_scoped_atomic_and_notifies_dispatcher(secured, tmp_path, monkeypatch):
    server, _, _ = secured
    employee, admin = Client(server), Client(server)
    assert employee.login('worker1')[0] == admin.login('admin')[0] == 200
    status, mobile, _ = employee.request('/api/engineer/unavailability', {
        'version': 1, 'engineer_id': 'e1', 'start_date': '2026-08-17', 'start_time': '09:00',
        'end_date': '2026-08-17', 'end_time': '12:00',
        'reason': 'transport', 'comment': 'Ремонт автомобиля', 'location_id': 'office'})
    assert status == 200
    payload = {'version': mobile['version'], 'engineer_id': 'e1',
               'report_id': mobile['active_absence']['id'], 'time': 570, 'location_id': 'office'}
    for path, change, options in [
        ('/api/engineer/return', {'engineer_id': 'e2'}, {}),
        ('/r/south-east/api/engineer/return', {}, {}),
        ('/api/engineer/return', {}, {'csrf': False}),
        ('/api/engineer/return', {}, {'origin': 'https://foreign.example'}),
    ]:
        assert employee.request(path, {**payload, **change}, **options)[0] == 403
    saved_before = (tmp_path/'state.json').read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'replace', lambda *_: (_ for _ in ()).throw(OSError('save failed')))
        assert employee.request('/api/engineer/return', payload)[0] == 500
    assert (tmp_path/'state.json').read_bytes() == saved_before
    assert employee.request('/api/engineer?engineer_id=e1')[1]['active_absence']['end_time'] == '12:00'
    status, mobile, _ = employee.request('/api/engineer/return', payload)
    assert status == 200 and mobile['active_absence'] is None
    assert mobile['return_pending_replan'] and mobile['pending_reassignment_ids']
    assert employee.request('/api/engineer/return', payload)[0] == 409
    state = admin.request('/api/state')[1]
    assert state['version'] == mobile['version']
    assert state['dispatch_notifications'][-1]['title'].endswith('снова доступен')
    restored = ScenarioSession.restore(json.loads((tmp_path/'state.json').read_text(encoding='utf-8')))
    assert restored.history[-1]['event']['type'] == 'engineer_return'
    assert restored.unavailability[0]['end_time'] == '09:30'
