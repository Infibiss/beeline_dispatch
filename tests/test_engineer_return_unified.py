"""The mobile return action covers dated and open-ended absences consistently."""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from field_router.engineer import engineer_state, record_engineer_fact
from field_router.replanning import ScenarioSession
from test_auth import Client, secured
from test_engineer_unavailability import session_with_route, report, preview_replan


def unavailable(session):
    return record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'unavailable', 'time': 540, 'location_id': 'office', 'comment': 'Заболел'})


def return_payload(session, **changes):
    return {'version': session.version, 'engineer_id': 'e1', 'time': 570,
            'location_id': 'office', **changes}


@pytest.mark.parametrize('approve_absence', [False, True])
def test_indefinite_return_persists_audit_and_waits_for_dispatch(make_scenario, approve_absence):
    session = session_with_route(make_scenario)
    report(session, engineer_id='e2', end_time='18:00')
    mobile = unavailable(session)
    previous = deepcopy(session.statuses['e1'])
    assert mobile['indefinite_absence']['id'] == previous['id']
    assert mobile['return_report_id'] is None
    if approve_absence:
        batch = preview_replan(session)
        session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    plan_before = session.plan.model_dump()
    session.return_from_absence(return_payload(session, unavailable_fact_id=previous['id']))
    mobile = engineer_state(session, 'e1')
    assert mobile['status']['status'] == 'available'
    assert mobile['indefinite_absence'] is None
    assert mobile['return_pending_replan']
    assert session.plan.model_dump() == plan_before
    assert next(f for f in session.fact_log if f['id'] == previous['id']) == previous
    assert session.history[-1]['event']['unavailable_fact_id'] == previous['id']
    assert session.dispatch_notifications[-1]['title'].endswith('снова доступен')
    if not approve_absence:
        assert mobile['pending_reassignment_ids'] == ['a', 'b']
        assert {t['status'] for t in mobile['tasks']} == {'pending_replan'}
        assert all(row['reason'] == 'Инженер вернулся; требуется пересчёт'
                   for row in session.state()['replanning_pool'])
    before = session.dump()
    with pytest.raises(ValueError, match='перепланирование'):
        record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
            'status': 'traveling', 'time': 571, 'request_id': 'a',
            'location_id': 'office', 'remaining_minutes': 1})
    assert session.dump() == before
    restored = ScenarioSession.restore(deepcopy(session.dump()))
    assert engineer_state(restored, 'e1')['return_pending_replan']
    batch = preview_replan(restored, "after-return")
    restored.approve({'version': restored.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    mobile = engineer_state(restored, 'e1')
    assert not mobile['return_pending_replan']
    assert mobile['tasks']
    record_engineer_fact(restored, {'version': restored.version, 'engineer_id': 'e1',
        'status': 'traveling', 'time': 571, 'request_id': mobile['tasks'][0]['id'],
        'location_id': 'office', 'remaining_minutes': 1})


@pytest.mark.parametrize('kind', ['available', 'traveling', 'working'])
def test_mobile_facts_cannot_bypass_indefinite_absence(make_scenario, kind):
    session = session_with_route(make_scenario)
    unavailable(session)
    before = session.dump()
    with pytest.raises(ValueError, match='Я снова доступен'):
        record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
            'status': kind, 'time': 550, 'request_id': 'a',
            'location_id': 'office', 'remaining_minutes': 10})
    assert session.dump() == before
    # Dispatch corrections remain possible through the fact journal.
    session.record_fact({'version': session.version, 'engineer_id': 'e1',
                         'status': 'available', 'time': 550, 'location_id': 'office'})
    assert session.statuses['e1']['source'] == 'dispatcher'


@pytest.mark.parametrize('changes', [
    {'engineer_id': 'e2'}, {'version': 1}, {'unavailable_fact_id': 'different'},
    {'time': 539}, {'time': True}, {'location_id': 'unknown'},
    {'report_id': 'missing'}, {'unexpected': True},
])
def test_invalid_indefinite_return_is_atomic(make_scenario, changes):
    session = session_with_route(make_scenario)
    unavailable(session)
    before = session.dump()
    with pytest.raises(ValueError):
        session.return_from_absence(return_payload(session, **changes))
    assert session.dump() == before


def test_ended_absence_uses_same_return_and_keeps_original_end(make_scenario):
    session = session_with_route(make_scenario)
    report(session, start_time='10:00', end_time='11:00', location_id=None)
    period_id = session.unavailability[0]['id']
    mobile = engineer_state(session, 'e1')
    assert mobile['active_absence'] is None and mobile['return_report_id'] is None
    assert [p['id'] for p in mobile['scheduled_absences']] == [period_id]
    session.time = 660
    mobile = engineer_state(session, 'e1')
    assert mobile['scheduled_absences'] == []
    assert mobile['return_confirmation_required']
    assert mobile['return_report_id'] == period_id
    with pytest.raises(ValueError, match='Я снова доступен'):
        record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
            'status': 'available', 'time': 660, 'location_id': 'office'})
    session.return_from_absence(return_payload(session, time=660, report_id=period_id))
    period = session.unavailability[0]
    assert period['end_time'] == '11:00' and not period.get('early_return')
    assert period['return_confirmation']['time'] == '11:00'
    mobile = engineer_state(session, 'e1')
    assert not mobile['return_confirmation_required']
    assert mobile['return_report_id'] is None
    assert mobile['return_pending_replan']
    assert mobile['pending_reassignment_ids'] == ['a', 'b']
    assert all(row['reason'] == 'Инженер вернулся' for row in session.state()['replanning_pool'])


def test_indefinite_return_cannot_override_separate_period(make_scenario):
    session = session_with_route(make_scenario)
    unavailable(session)
    report(session, end_time='12:00')
    before = session.dump()
    with pytest.raises(ValueError, match='Действует другой период'):
        session.return_from_absence(return_payload(session))
    assert session.dump() == before
    # Closing the active dated period confirms the current availability too.
    session.return_from_absence(return_payload(session, report_id=session.unavailability[0]['id']))
    assert engineer_state(session, 'e1')['indefinite_absence'] is None


def test_http_indefinite_return_scoped_atomic_and_persistent(secured, tmp_path, monkeypatch):
    server, _, _ = secured
    employee, admin = Client(server), Client(server)
    assert employee.login('worker1')[0] == admin.login('admin')[0] == 200
    code, mobile, _ = employee.request('/api/engineer/facts', {
        'version': 1, 'engineer_id': 'e1', 'status': 'unavailable', 'time': 540,
        'location_id': 'office', 'comment': 'Проблема с транспортом'})
    assert code == 200 and mobile['indefinite_absence']
    payload = {'version': mobile['version'], 'engineer_id': 'e1', 'time': 550,
               'location_id': 'office', 'unavailable_fact_id': mobile['indefinite_absence']['id']}
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
    assert employee.request('/api/engineer?engineer_id=e1')[1]['indefinite_absence']
    code, mobile, _ = employee.request('/api/engineer/return', payload)
    assert code == 200 and mobile['return_pending_replan']
    assert employee.request('/api/engineer/return', payload)[0] == 409
    state = admin.request('/api/state')[1]
    assert state['dispatch_notifications'][-1]['engineer_id'] == 'e1'
    restored = ScenarioSession.restore(json.loads((tmp_path/'state.json').read_text(encoding='utf-8')))
    assert engineer_state(restored, 'e1')['return_pending_replan']


@pytest.mark.parametrize('kind', ['completed', 'interrupted'])
@pytest.mark.parametrize('now', [542, 551])
def test_started_work_can_be_resolved_when_scheduled_absence_invalidates_old_plan(make_scenario, kind, now):
    session = session_with_route(make_scenario)
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'working', 'request_id': 'a', 'time': 541, 'remaining_minutes': 1})
    # Actual remainder fits before the absence; the original planned end does not.
    report(session, start_time='09:10', end_time='10:00', location_id=None)
    session.time = now
    mobile = engineer_state(session, 'e1')
    assert mobile['pending_reassignment_ids'] == ['a', 'b']
    assert next(t for t in mobile['tasks'] if t['id'] == 'a')['status'] == 'working'
    assert bool(mobile['active_absence']) == (now >= 550)
    payload = {'version': session.version, 'engineer_id': 'e1',
        'status': kind, 'request_id': 'a', 'time': now, 'remaining_minutes': 1,
        'consumed_router': 0, 'consumed_tv_box': 0, 'comment': 'Работа остановлена'}
    mobile = record_engineer_fact(session, payload)
    assert mobile['status']['status'] == ('completion_pending' if kind == 'completed' else 'interrupted')
    assert session.needs_replanning
    if now >= 550:
        session.return_from_absence(return_payload(session, time=now+1,
            report_id=session.unavailability[0]['id']))
        assert engineer_state(session, 'e1')['return_pending_replan']
