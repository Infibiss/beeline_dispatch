"""Employee corrections retain an audit and use the dispatch approval path."""
from copy import deepcopy
from pathlib import Path

import pytest

from field_router.availability import availability_at
from field_router.engineer import engineer_state, record_engineer_fact
from field_router.gps.context import project_context
from field_router.replanning import ScenarioSession
from test_auth import Client, secured
from test_engineer_unavailability import session_with_route, report, preview_replan


def edit_payload(session, index=0, **changes):
    period = session.unavailability[index]
    return {'version': session.version, 'report_id': period['id'],
            **{key: period[key] for key in ('engineer_id', 'start_date', 'start_time',
                'end_date', 'end_time', 'reason', 'comment', 'location_id')}, **changes}


def cancel_payload(session, index=0, **changes):
    period = session.unavailability[index]
    return {'version': session.version, 'engineer_id': period['engineer_id'],
            'report_id': period['id'], 'correction_reason': 'Ошибочно отправлено',
            'location_id': 'office', **changes}


def test_now_is_resolved_by_server_even_with_stale_absolute_start(make_scenario):
    session = session_with_route(make_scenario)
    report(session, start_mode='now', start_time='16:38', end_time='18:00')
    state = engineer_state(session, 'e1')
    assert state['active_absence']['start_time'] == '09:00'
    assert state['active_absence']['is_active']
    assert state['active_absence']['can_edit'] and state['active_absence']['can_cancel']
    assert not state['scheduled_absences']
    assert state['pending_reassignment_ids'] == ['a', 'b']
    gps = project_context(session)['engineers']['e1']
    assert availability_at(gps['availability'], 540) == 'absent'


def test_future_edit_changes_constraint_and_invalidates_preview_without_publishing(make_scenario):
    session = session_with_route(make_scenario)
    report(session, start_time='12:00', end_time='13:00', location_id=None)
    old_plan = session.plan.model_dump()
    old_period = deepcopy(session.unavailability[0])
    batch = preview_replan(session)
    session.update_unavailability(edit_payload(session, start_time='10:00', end_time='11:00',
                                             reason='transport', comment='Ремонт машины'))
    period = session.unavailability[0]
    assert period['id'] == old_period['id'] and period['recorded_at'] == old_period['recorded_at']
    assert period['revisions'][0]['previous']['start_time'] == '12:00'
    assert session.plan.model_dump() == old_plan
    assert session.needs_replanning and not session.batches
    assert session.dispatch_notifications[-1]['report_id'] == period['id']
    with pytest.raises(ValueError):
        session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    restored = ScenarioSession.restore(session.dump())
    assert restored.unavailability == session.unavailability
    state = engineer_state(restored, 'e1')
    assert state['active_absence'] is None
    assert state['scheduled_absences'][0]['start_time'] == '10:00'
    gps = project_context(restored)['engineers']['e1']['availability']
    assert availability_at(gps, 600) == 'absent'
    assert availability_at(gps, 720) != 'absent'


def test_future_can_be_moved_to_now(make_scenario):
    session = session_with_route(make_scenario)
    report(session, start_time='12:00', end_time='13:00', location_id=None)
    session.update_unavailability(edit_payload(session, start_mode='now', location_id='office'))
    assert engineer_state(session, 'e1')['active_absence']['start_time'] == '09:00'


def test_future_absence_without_affected_jobs_still_notifies_dispatch(make_scenario):
    session = session_with_route(make_scenario)
    report(session, start_date='2026-08-18', end_date='2026-08-18', location_id=None)
    assert not session.needs_replanning
    assert session.dispatch_notifications[-1]['engineer_id'] == 'e1'
    assert session.dispatch_notifications[-1]['report_id'] == session.unavailability[0]['id']
    assert session.dispatch_notifications[-1]['request_ids'] == []
    before = session.dump()
    engineer_state(session, 'e1')
    assert session.dump() == before


def test_current_end_can_change_without_rewriting_elapsed_start(make_scenario):
    session = session_with_route(make_scenario)
    report(session, end_time='12:00')
    session.time = 570
    session.update_unavailability(edit_payload(session, end_time='11:00', reason='personal'))
    assert session.unavailability[0]['start_time'] == '09:00'
    assert session.unavailability[0]['end_time'] == '11:00'
    assert session.fact_log == []


@pytest.mark.parametrize('next_day', [False, True])
def test_scheduled_unknown_location_remains_unknown_when_active_period_is_edited(make_scenario, next_day):
    session = session_with_route(make_scenario)
    report(session, start_time='12:00', end_time='13:00', location_id=None,
           end_date='2026-08-18' if next_day else session.scenario.date)
    session.time = 750
    if next_day:
        # A multi-day absence retains the fact that its original starting
        # location was not reported, even on the following planning day.
        session.scenario = session.scenario.model_copy(update={'date': '2026-08-18',
            'requests': [request.model_copy(update={'date': '2026-08-18'})
                         for request in session.scenario.requests]})
    session.update_unavailability(edit_payload(session, end_time='14:00', reason='personal',
                                             comment='Приём продлили'))
    period = session.unavailability[0]
    assert period['start_date'] == '2026-08-17' and period['start_time'] == '12:00'
    assert period['location_id'] is None and period['end_time'] == '14:00'
    assert period['reason'] == 'personal' and period['comment'] == 'Приём продлили'
    assert not session.fact_log
    state = engineer_state(session, 'e1')
    assert state['active_absence']['id'] == period['id']
    gps = project_context(session)['engineers']['e1']['availability']
    assert availability_at(gps, 750) == 'absent'
    assert availability_at(gps, 840) == 'awaiting_return'
    restored = ScenarioSession.restore(session.dump())
    assert restored.unavailability[0]['location_id'] is None


@pytest.mark.parametrize('changes', [
    {'version': -1}, {'version': True}, {'engineer_id': 'e2'}, {'report_id': 'missing'},
    {'start_time': '09:01'}, {'end_time': '09:20'}, {'end_time': '09:30'},
    {'location_id': 'a'}, {'reason': 'invalid'}, {'comment': ''}, {'unexpected': True},
])
def test_invalid_active_update_is_atomic(make_scenario, changes):
    session = session_with_route(make_scenario)
    report(session, end_time='12:00')
    session.time = 570
    before = session.dump()
    with pytest.raises(ValueError):
        session.update_unavailability(edit_payload(session, **changes))
    assert session.dump() == before


def test_update_overlap_and_work_in_progress_rejected_atomically(make_scenario):
    session = session_with_route(make_scenario)
    report(session, start_time='12:00', end_time='13:00', location_id=None)
    report(session, start_time='14:00', end_time='15:00', location_id=None)
    before = session.dump()
    with pytest.raises(ValueError, match='пересекается'):
        session.update_unavailability(edit_payload(session, end_time='14:01'))
    assert session.dump() == before
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'working', 'request_id': 'a', 'time': 550, 'remaining_minutes': 30})
    before = session.dump()
    with pytest.raises(ValueError, match='начатую работу'):
        session.update_unavailability(edit_payload(session, start_mode='now', location_id='a'))
    assert session.dump() == before


def test_future_cancel_removes_only_its_constraint_keeps_audit_and_approved_plan(make_scenario):
    session = session_with_route(make_scenario)
    report(session, start_time='12:00', end_time='13:00', location_id=None)
    report(session, start_time='14:00', end_time='15:00', location_id=None)
    before = session.plan.model_dump()
    session.cancel_unavailability(cancel_payload(session, location_id=None))
    assert session.unavailability[0]['revoked_by'] == 'engineer'
    assert not session.unavailability[1].get('revoked')
    assert session.unavailability[0]['id'] not in session.pending_report_ids
    assert session.plan.model_dump() == before and session.needs_replanning
    assert not session.return_pending_replan('e1')
    state = engineer_state(session, 'e1')
    assert len(state['scheduled_absences']) == 1
    assert not state['unavailability'][0]['can_cancel']
    assert not session.fact_log
    assert session.history[-1]['event']['type'] == 'unavailability_cancelled'


@pytest.mark.parametrize('initial_available', [False, True])
def test_active_cancel_keeps_audit_and_gates_jobs_until_dispatch_approval(make_scenario, initial_available):
    session = session_with_route(make_scenario)
    if initial_available:
        record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
            'status': 'available', 'time': 540, 'location_id': 'office'})
    facts_before = deepcopy(session.fact_log)
    report(session, end_time='12:00')
    session.cancel_unavailability(cancel_payload(session))
    assert engineer_state(session, 'e1')['active_absence'] is None
    assert session.return_pending_replan('e1')
    assert session.pending_reassignment_ids('e1') == ['a', 'b']
    assert session.fact_log[:len(facts_before)] == facts_before
    assert session.statuses['e1']['status'] == 'available'
    assert session.time == 540
    with pytest.raises(ValueError, match='перепланирование'):
        record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
            'status': 'traveling', 'request_id': 'a', 'time': 541,
            'location_id': 'office', 'remaining_minutes': 1})
    restored = ScenarioSession.restore(session.dump())
    assert restored.return_pending_replan('e1')
    batch = preview_replan(restored)
    restored.approve({'version': restored.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    assert not restored.return_pending_replan('e1')
    assert not restored.needs_replanning


def test_cancelling_period_does_not_erase_separate_indefinite_absence(make_scenario):
    session = session_with_route(make_scenario)
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'unavailable', 'time': 540, 'location_id': 'office', 'comment': 'Сломалась машина'})
    report(session, end_time='12:00')
    session.cancel_unavailability(cancel_payload(session))
    assert session.statuses['e1']['status'] == 'unavailable'
    assert engineer_state(session, 'e1')['indefinite_absence']


def test_cancel_immediately_after_work_interruption_preserves_fact_and_remainder(make_scenario):
    session = session_with_route(make_scenario)
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'working', 'request_id': 'a', 'time': 550, 'remaining_minutes': 30})
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'interrupted', 'request_id': 'a', 'time': 551, 'remaining_minutes': 20,
        'consumed_router': 0, 'consumed_tv_box': 0, 'comment': 'Плохо себя чувствую'})
    original = deepcopy(session.fact_log)
    report(session, start_mode='now', end_time='12:00', location_id='a')
    session.cancel_unavailability(cancel_payload(session, location_id='a'))
    assert session.time == 551
    assert session.fact_log == original
    state = engineer_state(session, 'e1')
    assert state['active_absence'] is None and state['return_pending_replan']
    assert next(task for task in state['tasks'] if task['id'] == 'a')['work_remaining_minutes'] == 20
    batch = preview_replan(session)
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    assert not session.return_pending_replan('e1')
    assert next(request for request in session.scenario.requests if request.id == 'a').duration_minutes == 20
    assert session.fact_log == original


@pytest.mark.parametrize('changes', [
    {'version': -1}, {'engineer_id': 'e2'}, {'report_id': 'unknown'},
    {'correction_reason': ''}, {'location_id': 'unknown'},
    {'unexpected': True},
])
def test_invalid_active_cancel_is_atomic(make_scenario, changes):
    session = session_with_route(make_scenario)
    report(session)
    before = session.dump()
    with pytest.raises(ValueError):
        session.cancel_unavailability(cancel_payload(session, **changes))
    assert session.dump() == before


@pytest.mark.parametrize('state', ['past', 'revoked', 'returned', 'dispatcher'])
@pytest.mark.parametrize('method', ['update_unavailability', 'cancel_unavailability'])
def test_closed_or_not_employee_report_cannot_be_changed(make_scenario, state, method):
    session = session_with_route(make_scenario)
    report(session)
    if state == 'past':
        session.time = 600
    elif state == 'revoked':
        session.cancel_unavailability(cancel_payload(session))
    elif state == 'returned':
        session.return_from_absence({'version': session.version, 'engineer_id': 'e1',
            'report_id': session.unavailability[0]['id'], 'time': 570, 'location_id': 'office'})
    else:
        session.unavailability[0]['source'] = 'dispatcher'
    before = session.dump()
    payload = edit_payload(session, end_time='11:00') if method.startswith('update') else cancel_payload(session)
    with pytest.raises(ValueError):
        getattr(session, method)(payload)
    assert session.dump() == before
    assert not engineer_state(session, 'e1')['unavailability'][0]['can_edit']


@pytest.mark.parametrize('action', ['update', 'cancel'])
def test_employee_http_correction_is_scoped_and_persisted_atomically(secured, tmp_path, monkeypatch, action):
    server, _, _ = secured
    employee, admin = Client(server), Client(server)
    assert employee.login('worker1')[0] == admin.login('admin')[0] == 200
    create = {'version': 1, 'engineer_id': 'e1', 'start_mode': 'now',
        'start_date': '2026-08-17', 'start_time': '16:00',
        'end_date': '2026-08-17', 'end_time': '18:00', 'reason': 'sick',
        'comment': 'Болезнь', 'location_id': 'office'}
    status, mobile, _ = employee.request('/api/engineer/unavailability', create)
    assert status == 200
    rid = mobile['active_absence']['id']
    payload = {**create, 'version': mobile['version'], 'report_id': rid,
               'start_time': '09:00', 'end_time': '12:00'}
    payload.pop('start_mode')
    if action == 'cancel':
        payload = {'version': mobile['version'], 'engineer_id': 'e1', 'report_id': rid,
                   'correction_reason': 'Ошибочно отправлено', 'location_id': 'office'}
    path = '/api/engineer/unavailability/' + action
    for url, change, options in [
        (path, {'engineer_id': 'e2'}, {}), ('/r/south-east'+path, {}, {}),
        (path, {}, {'csrf': False}), (path, {}, {'origin': 'https://foreign.example'}),
        ('/api/engineer/revoke-report', {}, {}),
    ]:
        assert employee.request(url, {**payload, **change}, **options)[0] == 403
    before = (tmp_path/'state.json').read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'replace', lambda *_: (_ for _ in ()).throw(OSError('save failed')))
        assert employee.request(path, payload)[0] == 500
    assert (tmp_path/'state.json').read_bytes() == before
    assert employee.request('/api/engineer?engineer_id=e1')[1]['active_absence']['end_time'] == '18:00'
    status, mobile, _ = employee.request(path, payload)
    assert status == 200
    assert employee.request(path, payload)[0] == 409
    dispatch = admin.request('/api/state')[1]
    assert dispatch['needs_replanning'] and dispatch['version'] == mobile['version']
    assert dispatch['dispatch_notifications'][-1]['report_id'] == rid
    if action == 'cancel':
        assert mobile['active_absence'] is None and mobile['return_pending_replan']
    else:
        assert mobile['active_absence']['end_time'] == '12:00'
