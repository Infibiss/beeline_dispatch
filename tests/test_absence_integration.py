"""Regression coverage for mobile absences across facts, planning and GPS."""
import pytest

from field_router.engineer import engineer_state, record_engineer_fact
from field_router.models import TimeWindow
from field_router.replanning import Event, ScenarioSession
from test_engineer_unavailability import session_with_route, report, preview_replan
from test_off_duty_emergency_window import session_with_day_off_worker, emergency_payload
from test_auth import Client, secured


def residual_worker(session, minute, eid='e1'):
    residual, _, _ = session._residual(Event(id='check', type='replan', time=minute), False)
    return next(e for e in residual.engineers if e.id == eid)


def test_second_future_absence_does_not_bypass_return_confirmation(make_scenario):
    session = session_with_route(make_scenario)
    report(session, start_time='10:00', end_time='11:00', location_id=None)
    report(session, start_time='13:00', end_time='14:00', location_id=None)
    worker = residual_worker(session, 700)
    assert worker.shift.start == worker.shift.end
    session.record_fact({'version': session.version, 'engineer_id': 'e1',
        'status': 'available', 'time': 700, 'location_id': 'office'})
    worker = residual_worker(session, 700)
    assert worker.shift == TimeWindow(start=700, end=780)


def test_latest_travel_fact_overrides_planned_work_when_reporting_absence(make_scenario):
    session = session_with_route(make_scenario)
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'traveling', 'time': 560, 'request_id': 'a', 'location_id': 'office',
        'remaining_minutes': 20})
    report(session, start_time='09:20')
    assert session.pending_reassignment_ids('e1') == ['a', 'b']
    assert all(task['status'] == 'pending_replan' for task in engineer_state(session, 'e1')['tasks'])
    batch = preview_replan(session)
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    # The old trip must not reappear after the absence or fail its expired window.
    session.preview({'version': session.version,
        'event': {'id': 'later', 'type': 'replan', 'time': 650}}, time_limit=.03)


@pytest.mark.parametrize('status', ['available', 'traveling', 'working'])
def test_absent_worker_cannot_resume_before_end_after_plan_approval(make_scenario, status):
    session = session_with_route(make_scenario)
    report(session, engineer_id='e2', end_time='18:00')
    report(session)
    batch = preview_replan(session)
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    before = session.dump()
    payload = {'version': session.version, 'engineer_id': 'e1', 'status': status,
               'time': 550, 'location_id': 'office'}
    if status != 'available':
        payload.update(request_id='a', remaining_minutes=15)
    with pytest.raises(ValueError, match='недоступ'):
        record_engineer_fact(session, payload)
    assert session.dump() == before


def test_absence_does_not_extend_a_short_emergency_call_window(make_scenario):
    session = session_with_day_off_worker(make_scenario)
    payload = emergency_payload(session,
        event={'id': 'early-emergency', 'type': 'emergency', 'time': 540,
               'anchor_id': 'a', 'duration_minutes': 20},
        call_off_duty_window_start=550, call_off_duty_window_end=720)
    batch = session.preview(payload, time_limit=.03)
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    report(session, engineer_id='e2', start_time='10:00', end_time='11:00')
    worker = residual_worker(session, 670, 'e2')
    assert worker.shift.end == 720
    assert ScenarioSession.restore(session.dump())._availability_shift('e2').end == 720
    session.record_fact({'version': session.version, 'engineer_id': 'e2',
        'status': 'available', 'time': 670, 'location_id': 'office'})
    assert residual_worker(session, 670, 'e2').shift.end == 720


def test_unknown_return_requires_explicit_location_and_preserves_day_end(make_scenario):
    session = session_with_route(make_scenario)
    report(session, start_time='10:00', end_time='11:00', location_id=None)
    batch = session.preview({'version': session.version,
        'event': {'id': 'during', 'type': 'replan', 'time': 630}}, time_limit=.03)
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    session = ScenarioSession.restore(session.dump())
    assert engineer_state(session, 'e1')['active_absence']
    session.record_fact({'version': session.version, 'engineer_id': 'e2',
        'status': 'available', 'time': 660, 'location_id': 'office'})
    assert engineer_state(session, 'e1')['return_confirmation_required']
    assert residual_worker(session, 660).shift == TimeWindow(start=660, end=660)
    mobile = engineer_state(session, 'e1')
    with pytest.raises(ValueError, match='Я снова доступен'):
        record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
            'status': 'available', 'time': 660, 'location_id': 'office'})
    session.return_from_absence({'version': session.version, 'engineer_id': 'e1',
        'report_id': mobile['return_report_id'], 'time': 660, 'location_id': 'office'})
    mobile = engineer_state(session, 'e1')
    assert not mobile['return_confirmation_required']
    assert mobile['return_pending_replan']
    assert session.unavailability[0]['end_time'] == '11:00'
    assert session.history[-1]['event']['type'] == 'engineer_return'
    assert session.dispatch_notifications[-1]['engineer_id'] == 'e1'
    assert residual_worker(session, 660).shift == TimeWindow(start=660, end=1080)


def test_started_work_requires_explicit_interruption_and_keeps_remaining_job(make_scenario):
    session = session_with_route(make_scenario)
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'working', 'time': 550, 'request_id': 'a', 'remaining_minutes': 30})
    before = session.dump()
    with pytest.raises(ValueError, match='прервите'):
        report(session, start_time='09:10')
    assert session.dump() == before
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'interrupted', 'time': 551, 'request_id': 'a', 'remaining_minutes': 25,
        'consumed_router': 0, 'consumed_tv_box': 0, 'comment': 'Потребовалась помощь'})
    report(session, start_time='09:11')
    preview_replan(session)
    assert not any(f['status'] in ('completed', 'completion_pending') for f in session.fact_log)
    assert 'a' in session.catalog


def test_predicted_work_is_not_a_confirmed_start_blocking_mobile_absence(make_scenario):
    session = session_with_route(make_scenario)
    batch = session.preview({'version': session.version,
        'event': {'id': 'advance', 'type': 'replan', 'time': 560}}, time_limit=.03)
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    assert session.fact_log == []
    report(session, start_time='09:20')
    next_batch = preview_replan(session)
    assert all(v['start'] >= 600 for c in next_batch['candidates'] for r in c['plan']['routes']
               if r['engineer_id'] == 'e1' for v in r['visits'])


def test_employee_period_dispatcher_replan_and_return_over_http(secured, tmp_path):
    import json
    server, _, _ = secured
    employee, admin = Client(server), Client(server)
    assert employee.login('worker1')[0] == admin.login('admin')[0] == 200
    payload = {'version': 1, 'engineer_id': 'e1',
        'start_date': '2026-08-17', 'start_time': '09:00',
        'end_date': '2026-08-17', 'end_time': '10:00',
        'reason': 'transport', 'comment': 'Ремонт автомобиля', 'location_id': 'office'}
    status, mobile, _ = employee.request('/api/engineer/unavailability', payload)
    assert status == 200 and mobile['active_absence']
    state = admin.request('/api/state')[1]
    assert state['needs_replanning'] and state['replanning_pool']
    assert employee.request('/api/preview', {})[0] == 403
    status, batch, _ = admin.request('/api/preview', {'version': state['version'],
        'event': {'id': 'absence-http', 'type': 'replan', 'time': 540}})
    assert status == 200
    assert all(v['start'] >= 600 for c in batch['candidates'] for r in c['plan']['routes']
               if r['engineer_id'] == 'e1' for v in r['visits'])
    status, state, _ = admin.request('/api/approve', {'version': state['version'],
        'batch_id': batch['id'], 'candidate_id': 'stable'})
    assert status == 200
    version = state['version']
    early = {'version': version, 'engineer_id': 'e1', 'status': 'available',
             'time': 599, 'location_id': 'office'}
    assert employee.request('/api/engineer/facts', early)[0] == 400
    assert admin.request('/api/state')[1]['version'] == version
    # A known-location period ends naturally once the plan clock reaches its end.
    # Posting a future 'available' while it is still active cannot bypass return.
    assert employee.request('/api/engineer/facts', {**early, 'time': 600})[0] == 400
    status, state, _ = admin.request('/api/facts', {'version': version, 'engineer_id': 'e2',
        'status': 'available', 'time': 600, 'location_id': 'office'})
    assert status == 200
    status, mobile, _ = employee.request('/api/engineer/facts',
        {**early, 'version': state['version'], 'time': 600})
    assert status == 200 and mobile['active_absence'] is None
    restored = ScenarioSession.restore(json.loads((tmp_path/'state.json').read_text(encoding='utf-8')))
    assert restored.unavailability and restored.statuses['e1']['status'] == 'available'



def test_unknown_period_http_return_confirms_location_and_notifies_dispatcher(secured, tmp_path):
    import json
    server, _, _ = secured
    employee, admin = Client(server), Client(server)
    assert employee.login('worker1')[0] == admin.login('admin')[0] == 200
    status, mobile, _ = employee.request('/api/engineer/unavailability', {
        'version': 1, 'engineer_id': 'e1', 'start_date': '2026-08-17', 'start_time': '10:00',
        'end_date': '2026-08-17', 'end_time': '11:00',
        'reason': 'transport', 'comment': 'Ремонт автомобиля'})
    assert status == 200 and mobile['active_absence'] is None
    report_id = mobile['scheduled_absences'][0]['id']
    status, state, _ = admin.request('/api/facts', {'version': mobile['version'],
        'engineer_id': 'e2', 'status': 'available', 'time': 660, 'location_id': 'office'})
    assert status == 200
    mobile = employee.request('/api/engineer?engineer_id=e1')[1]
    assert mobile['return_confirmation_required'] and mobile['return_report_id'] == report_id
    before_version = mobile['version']
    assert employee.request('/api/engineer/facts', {'version': before_version,
        'engineer_id': 'e1', 'status': 'available', 'time': 660, 'location_id': 'office'})[0] == 400
    assert admin.request('/api/state')[1]['version'] == before_version
    status, mobile, _ = employee.request('/api/engineer/return', {'version': before_version,
        'engineer_id': 'e1', 'report_id': report_id, 'time': 660, 'location_id': 'office'})
    assert status == 200 and not mobile['return_confirmation_required']
    assert mobile['return_pending_replan']
    state = admin.request('/api/state')[1]
    assert state['needs_replanning']
    assert state['dispatch_notifications'][-1]['title'].endswith('снова доступен')
    restored = ScenarioSession.restore(json.loads((tmp_path/'state.json').read_text(encoding='utf-8')))
    assert restored.unavailability[0]['end_time'] == '11:00'
    assert restored.unavailability[0]['return_confirmation']['location_id'] == 'office'
    assert restored.history[-1]['event']['type'] == 'engineer_return'
    assert engineer_state(restored, 'e1')['return_pending_replan']
