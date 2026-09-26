"""End-to-end session rules for mobile absence, declined visits and day-off calls."""
from datetime import date, timedelta
import json

import pytest

from conftest import engineer, job
from field_router.availability import day_interval
from field_router.models import Point, Skill
from field_router.planning import make_plan
from field_router.replanning import ScenarioSession
from field_router.travel import synthetic_travel


TRAVEL_CONFIG = {
    'speed_kmh': {mode: 60 for mode in ('car', 'walk', 'bicycle', 'public')},
    'distance_factor': {mode: 1 for mode in ('car', 'walk', 'bicycle', 'public')},
    'boarding_minutes': {mode: 0 for mode in ('car', 'walk', 'bicycle', 'public')},
}


def session_with_route(make_scenario):
    scenario = make_scenario(
        [job('a', start=550, end=650, duration=30),
         job('b', start=610, end=720, duration=30)],
        [engineer('e1'), engineer('e2')],
    ).model_copy(update={'metadata': {'geography': 'synthetic_cartesian_km'},
                            'travel_config': TRAVEL_CONFIG, 'points': {
        'office': Point(x_km=0, y_km=0),
        'a': Point(x_km=1, y_km=0),
        'b': Point(x_km=2, y_km=0),
    }})
    plan = make_plan(scenario, synthetic_travel(scenario), {'e1': ['a', 'b']}, 'test', 'valid')
    return ScenarioSession(scenario, plan)


def report(session, **overrides):
    payload = {'version': session.version, 'engineer_id': 'e1',
               'start_date': session.scenario.date, 'start_time': '09:00',
               'end_date': session.scenario.date, 'end_time': '10:00',
               'reason': 'sick', 'comment': 'Врач назначил приём',
               'location_id': 'office'}
    payload.update(overrides)
    return session.report_unavailability(payload)


def preview_replan(session, event_id='replan'):
    return session.preview({'version': session.version,
                            'event': {'id': event_id, 'type': 'replan', 'time': session.time}},
                           time_limit=.03)


def test_absence_puts_existing_visits_in_pool_until_dispatcher_approves(make_scenario):
    session = session_with_route(make_scenario)
    report(session)
    assert session.needs_replanning
    assert set(session.pending_reassignment_ids('e1')) == {'a', 'b'}
    assert {row['request_id'] for row in session.state()['replanning_pool']} == {'a', 'b'}
    assert {v.request_id for route in session.plan.routes for v in route.visits} == {'a', 'b'}
    batch = preview_replan(session)
    assert session.pending_reassignment_ids('e1') == ['a', 'b']
    for candidate in batch['candidates']:
        assert all(visit['start'] >= 600 for route in candidate['plan']['routes']
                   if route['engineer_id'] == 'e1' for visit in route['visits'])
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    assert session.pending_reassignment_ids() == []
    assert not session.needs_replanning
    assert {request.id for request in session.scenario.requests} == {'a', 'b'}
    restored = ScenarioSession.restore(session.dump())
    assert restored.unavailability == session.unavailability
    assert restored.pending_reassignment_ids() == []


def test_declined_visit_is_not_cancelled_or_returned_to_same_engineer(make_scenario):
    session = session_with_route(make_scenario)
    session.decline_assignment({'version': 1, 'engineer_id': 'e1', 'request_id': 'b',
                                'time': 540, 'comment': 'Нет доступа к объекту'})
    assert session.pending_reassignment_ids('e1') == ['b']
    batch = preview_replan(session)
    for candidate in batch['candidates']:
        assert not any(v['request_id'] == 'b' for route in candidate['plan']['routes']
                       if route['engineer_id'] == 'e1' for v in route['visits'])
        assert any(v['request_id'] == 'b' for route in candidate['plan']['routes']
                   for v in route['visits']) or any(u['request_id'] == 'b'
                   for u in candidate['plan']['unassigned'])
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    assert 'e1' in next(r for r in session.scenario.requests if r.id == 'b').excluded_engineer_ids
    assert 'e1' in session.catalog['b'].excluded_engineer_ids
    assert session.pending_reassignment_ids() == []
    assert ScenarioSession.restore(session.dump()).catalog['b'].excluded_engineer_ids == ['e1']


def test_decline_and_period_validation_is_atomic(make_scenario):
    from field_router.engineer import record_engineer_fact
    session = session_with_route(make_scenario)
    record_engineer_fact(session, {'version': 1, 'engineer_id': 'e1', 'status': 'traveling',
                                   'time': 541, 'request_id': 'a', 'location_id': 'office',
                                   'remaining_minutes': 9})
    before = session.dump()
    with pytest.raises(ValueError, match='начат'):
        session.decline_assignment({'version': session.version, 'engineer_id': 'e1', 'request_id': 'a',
                                    'time': 560, 'comment': 'Невозможно приехать'})
    assert session.dump() == before
    session = session_with_route(make_scenario)
    before = session.dump()
    with pytest.raises(ValueError, match='местоположение'):
        report(session, location_id=None)
    assert session.dump() == before


def test_late_unstarted_decline_returns_job_to_pool(make_scenario):
    session = session_with_route(make_scenario)
    session.decline_assignment({'version': 1, 'engineer_id': 'e1', 'request_id': 'a',
                                'time': 560, 'comment': 'Не смог выехать вовремя'})
    assert session.pending_reassignment_ids('e1') == ['a']
    batch = preview_replan(session)
    assert all(not any(v['request_id'] == 'a' for route in candidate['plan']['routes']
                       if route['engineer_id'] == 'e1' for v in route['visits'])
               for candidate in batch['candidates'])


def test_multi_day_period_is_stored_with_date_aware_projection(make_scenario):
    session = session_with_route(make_scenario)
    tomorrow = date.fromisoformat(session.scenario.date) + timedelta(days=1)
    third_day = tomorrow + timedelta(days=2)
    report(session, start_date=tomorrow.isoformat(), start_time='08:00',
           end_date=third_day.isoformat(), end_time='14:00', location_id=None)
    assert not session.needs_replanning  # This is still the original one-day plan.
    period = session.unavailability[0]
    assert day_interval(period, session.scenario.date) is None
    assert day_interval(period, tomorrow.isoformat()) == (480, 1440)
    assert day_interval(period, (tomorrow + timedelta(days=1)).isoformat()) == (0, 1440)
    assert day_interval(period, third_day.isoformat()) == (0, 840)
    assert ScenarioSession.restore(session.dump()).unavailability == session.unavailability


def test_next_day_load_carries_absence_and_archives_previous_state(make_scenario, tmp_path):
    from field_router.regions import load_session
    old = session_with_route(make_scenario)
    tomorrow = (date.fromisoformat(old.scenario.date) + timedelta(days=1)).isoformat()
    later = (date.fromisoformat(old.scenario.date) + timedelta(days=2)).isoformat()
    report(old, start_date=tomorrow, start_time='08:00',
           end_date=later, end_time='18:00', location_id=None)
    state_path = tmp_path / 'day.json'
    state_path.write_text(json.dumps(old.dump(), ensure_ascii=False), encoding='utf-8')
    scenario = old.scenario.model_copy(update={'date': tomorrow,
        'requests': [request.model_copy(update={'date': tomorrow})
                     for request in old.scenario.requests]})
    plan = make_plan(scenario, synthetic_travel(scenario), {'e1': ['a', 'b']}, 'test', 'valid')
    comparison = tmp_path / 'comparison.json'
    comparison.write_text(json.dumps({'scenario': scenario.model_dump(mode='json'),
        'plans': [plan.model_dump(mode='json')]}, ensure_ascii=False), encoding='utf-8')
    new = load_session(comparison, state_path)
    assert new.scenario.date == tomorrow
    assert new.needs_replanning
    assert set(new.pending_reassignment_ids('e1')) == {'a', 'b'}
    assert (tmp_path / 'day-2026-08-17.json').exists()
    assert ScenarioSession.restore(json.loads(state_path.read_text(encoding='utf-8'))).scenario.date == tomorrow
    assert load_session(comparison, state_path).unavailability == new.unavailability


def test_next_day_does_not_reuse_yesterdays_position_as_return_confirmation(make_scenario, tmp_path):
    from field_router.replanning import Event
    from field_router.regions import load_session
    old = session_with_route(make_scenario)
    tomorrow = (date.fromisoformat(old.scenario.date) + timedelta(days=1)).isoformat()
    report(old, end_date=tomorrow, end_time='11:00')
    state_path = tmp_path / 'day.json'
    state_path.write_text(json.dumps(old.dump(), ensure_ascii=False), encoding='utf-8')
    scenario = old.scenario.model_copy(update={'date': tomorrow,
        'requests': [request.model_copy(update={'date': tomorrow})
                     for request in old.scenario.requests]})
    plan = make_plan(scenario, synthetic_travel(scenario), {'e1': ['a', 'b']}, 'test', 'valid')
    comparison = tmp_path / 'comparison.json'
    comparison.write_text(json.dumps({'scenario': scenario.model_dump(mode='json'),
        'plans': [plan.model_dump(mode='json')]}, ensure_ascii=False), encoding='utf-8')
    new = load_session(comparison, state_path)
    assert new.unavailability[0]['location_id'] is None
    residual, _, _ = new._residual(Event(id='after-absence', type='replan', time=700), False)
    assert next(e for e in residual.engineers if e.id == 'e1').shift.end == 700


def test_future_report_does_not_invent_work_or_return_location(make_scenario):
    session = session_with_route(make_scenario)
    report(session, start_time='10:00', end_time='11:00', location_id=None)
    assert session.pending_reassignment_ids('e1') == ['b']
    for now in (630, 700):
        batch = session.preview({'version': session.version,
            'event': {'id': f'replan-{now}', 'type': 'replan', 'time': now}}, time_limit=.03)
        assert all(not any(route['engineer_id'] == 'e1' and route['visits']
                           for route in candidate['plan']['routes'])
                   for candidate in batch['candidates'])


def test_risk_dashboard_excludes_absent_capacity_and_pending_jobs(make_scenario):
    from field_router.risk_dashboard import capture_dashboard_session, dashboard
    session = session_with_route(make_scenario)
    report(session, start_time='10:00', end_time='11:00', location_id=None)
    future = dashboard(capture_dashboard_session(session))
    capacity = next(row for row in future['indicators'] if row['id'] == 'capacity')
    assert future['summary']['engineers'] == 2
    assert future['summary']['unassigned'] == 1
    assert capacity['facts']['capacity_minutes'] == 600  # e1 until 10:00, e2 whole shift.
    session = session_with_route(make_scenario)
    report(session)
    active = dashboard(capture_dashboard_session(session))
    capacity = next(row for row in active['indicators'] if row['id'] == 'capacity')
    assert active['summary']['engineers'] == 1
    assert active['summary']['unassigned'] == 2
    assert capacity['facts']['capacity_minutes'] == 540


def test_absence_ending_before_shift_does_not_disrupt_assignment(make_scenario):
    scenario = make_scenario([job('a', start=650, end=760, duration=30)],
        [engineer('e1', start=600), engineer('e2', start=540)]).model_copy(update={
            'metadata': {'geography': 'synthetic_cartesian_km'},
            'travel_config': TRAVEL_CONFIG,
            'points': {'office': Point(x_km=0, y_km=0), 'a': Point(x_km=1, y_km=0)}})
    plan = make_plan(scenario, synthetic_travel(scenario), {'e1': ['a']}, 'test', 'valid')
    session = ScenarioSession(scenario, plan)
    report(session, start_time='09:10', end_time='09:30', location_id=None)
    assert session.pending_reassignment_ids() == []
    assert not session.needs_replanning
    batch = preview_replan(session)
    stable = next(candidate for candidate in batch['candidates'] if candidate['id'] == 'stable')
    assert any(route['engineer_id'] == 'e1' and route['visits'][0]['request_id'] == 'a'
               for route in stable['plan']['routes'])


def test_dispatcher_can_revoke_report_with_audit_and_confirmed_return(make_scenario):
    session = session_with_route(make_scenario)
    report(session)
    report_id = session.unavailability[0]['id']
    batch = preview_replan(session)
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    with pytest.raises(ValueError, match='точку'):
        session.revoke_engineer_report({'version': session.version, 'report_id': report_id,
                                        'reason': 'Ошибка при вводе'})
    result = session.revoke_engineer_report({'version': session.version, 'report_id': report_id,
        'reason': 'Ошибка при вводе', 'location_id': 'office'})
    assert session.unavailability[0]['revoked']
    assert result['needs_replanning']
    assert session.fact_log[-1]['status'] == 'available'
    assert ScenarioSession.restore(session.dump()).unavailability[0]['revoked']
    batch = preview_replan(session, 'after-correction')
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    assert not session.needs_replanning


def test_dispatcher_can_revoke_decline_without_deleting_request(make_scenario):
    session = session_with_route(make_scenario)
    session.decline_assignment({'version': 1, 'engineer_id': 'e1', 'request_id': 'b',
                                'time': 540, 'comment': 'Ошибочное сообщение'})
    report_id = session.declined_assignments[0]['id']
    batch = preview_replan(session)
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    assert 'e1' in session.catalog['b'].excluded_engineer_ids
    session.revoke_engineer_report({'version': session.version, 'report_id': report_id,
                                    'reason': 'Уточнил, выехать могу'})
    assert session.declined_assignments[0]['revoked']
    assert 'e1' not in session.catalog['b'].excluded_engineer_ids
    assert 'e1' not in next(r for r in session.scenario.requests if r.id == 'b').excluded_engineer_ids
    assert session.needs_replanning
    assert not session.pending_reassignment_ids()


def test_legacy_unavailable_status_also_puts_future_tasks_in_pool(make_scenario):
    from field_router.engineer import record_engineer_fact
    session = session_with_route(make_scenario)
    record_engineer_fact(session, {'version': 1, 'engineer_id': 'e1',
        'status': 'unavailable', 'time': 540, 'location_id': 'office',
        'comment': 'Автомобиль сломался'})
    assert set(session.pending_reassignment_ids('e1')) == {'a', 'b'}
    assert all(row['report_type'] == 'fact' for row in session.replanning_pool())


def test_day_off_needs_separate_confirmed_emergency_escalation(make_scenario):
    scenario = make_scenario([job('a', start=550, end=700, duration=20)],
        [engineer('e1'), engineer('e2', off_duty=True,
                                  skills=[Skill.LOCAL, Skill.EMERGENCY])]).model_copy(update={
            'metadata': {'geography': 'synthetic_cartesian_km'},
            'travel_config': TRAVEL_CONFIG,
            'points': {'office': Point(x_km=0, y_km=0), 'a': Point(x_km=1, y_km=0)}})
    plan = make_plan(scenario, synthetic_travel(scenario), {'e1': ['a']}, 'test', 'valid')
    session = ScenarioSession(scenario, plan)
    event = {'id': 'accident-1', 'type': 'emergency', 'time': 540,
             'anchor_id': 'a', 'duration_minutes': 20}
    ordinary = session.preview({'version': 1, 'event': event}, time_limit=.03)
    assert all(any(item['request_id'] == 'emergency:accident-1'
                   for item in c['plan']['unassigned']) for c in ordinary['candidates'])
    assert session.scenario.engineers[1].off_duty
    with pytest.raises(ValueError, match='подтверждение'):
        session.preview({'version': 1, 'event': event,
            'call_off_duty_engineer_ids': ['e2'], 'call_off_duty_reason': 'Авария'}, time_limit=.03)
    with pytest.raises(ValueError, match='только'):
        session.preview({'version': 1,
            'event': {'id': 'ordinary', 'type': 'new_request', 'time': 540, 'anchor_id': 'a',
                      'work_kind': 'local', 'window_start': 540, 'window_end': 700},
            'call_off_duty_engineer_ids': ['e2'], 'call_off_duty_confirmed': True,
            'call_off_duty_reason': 'Авария'}, time_limit=.03)
    escalated = session.preview({'version': 1, 'event': event,
        'call_off_duty_engineer_ids': ['e2'], 'call_off_duty_confirmed': True,
        'call_off_duty_reason': 'Согласованный вызов на крупную аварию'}, time_limit=.03)
    assert escalated['off_duty_escalation']['engineer_ids'] == ['e2']
    assert any(any(v['request_id'] == 'emergency:accident-1'
                   for route in c['plan']['routes'] if route['engineer_id'] == 'e2'
                   for v in route['visits']) for c in escalated['candidates'])
    assert session.scenario.engineers[1].off_duty  # Preview never publishes it.
    session.approve({'version': 1, 'batch_id': escalated['id'], 'candidate_id': 'stable'})
    assert not next(e for e in session.scenario.engineers if e.id == 'e2').off_duty
    assert session.history[-1]['policy']['off_duty_escalation']['confirmed']
