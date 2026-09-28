"""Absence corrections retain independent constraints and require a new plan."""
from copy import deepcopy

import pytest

from field_router.engineer import engineer_state, record_engineer_fact
from field_router.replanning import Event, ScenarioSession
from field_router.models import TimeWindow
from test_engineer_unavailability import session_with_route, report, preview_replan


def revoke(session):
    return session.revoke_engineer_report({
        'version': session.version, 'report_id': session.unavailability[0]['id'],
        'reason': 'Период указан по ошибке', 'location_id': 'office'})


def test_dispatcher_correction_same_minute_reuses_confirmed_available_fact(make_scenario):
    session = session_with_route(make_scenario)
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'available', 'time': 540, 'location_id': 'office'})
    original = deepcopy(session.fact_log)
    report(session)
    revoke(session)
    assert session.fact_log == original
    assert session.unavailability[0]['revoked']
    assert engineer_state(session, 'e1')['return_pending_replan']


def test_dispatcher_correction_does_not_clear_separate_indefinite_absence(make_scenario):
    session = session_with_route(make_scenario)
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'unavailable', 'time': 540, 'location_id': 'office', 'comment': 'Заболел'})
    original = deepcopy(session.statuses['e1'])
    report(session)
    session.time = 541
    revoke(session)
    assert session.statuses['e1'] == original
    assert engineer_state(session, 'e1')['indefinite_absence']
    residual, _, _ = session._residual(Event(id='still-absent', type='replan', time=541), False)
    worker = next(item for item in residual.engineers if item.id == 'e1')
    assert worker.shift.start == worker.shift.end


def test_dispatcher_absence_correction_blocks_stale_assignments_until_approval(make_scenario):
    session = session_with_route(make_scenario)
    report(session)
    revoke(session)
    mobile = engineer_state(session, 'e1')
    assert mobile['return_pending_replan']
    assert mobile['pending_reassignment_ids'] == ['a', 'b']
    before = session.dump()
    with pytest.raises(ValueError, match='перепланирование'):
        record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
            'status': 'traveling', 'time': 541, 'request_id': 'a',
            'location_id': 'office', 'remaining_minutes': 1})
    assert session.dump() == before
    restored = ScenarioSession.restore(session.dump())
    assert engineer_state(restored, 'e1')['return_pending_replan']
    batch = preview_replan(restored)
    restored.approve({'version': restored.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    assert not engineer_state(restored, 'e1')['return_pending_replan']


def test_dispatcher_correction_rejects_conflicting_same_minute_position_atomically(make_scenario):
    session = session_with_route(make_scenario)
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'available', 'time': 540, 'location_id': 'office'})
    report(session)
    before = session.dump()
    with pytest.raises(ValueError, match='другое место'):
        session.revoke_engineer_report({'version': session.version,
            'report_id': session.unavailability[0]['id'], 'reason': 'Ошибка периода',
            'location_id': 'a'})
    assert session.dump() == before


def test_replanning_releases_trip_crossing_future_absence_without_scheduling_inside_it(make_scenario):
    session = session_with_route(make_scenario)
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'traveling', 'time': 541, 'request_id': 'a',
        'location_id': 'office', 'remaining_minutes': 90})
    report(session, start_time='10:00', end_time='11:00', location_id=None)
    original_fact = deepcopy(session.statuses['e1'])
    batch = preview_replan(session)
    assert session.statuses['e1'] == original_fact
    for candidate in batch['candidates']:
        for route in candidate['plan']['routes']:
            if route['engineer_id'] != 'e1':
                continue
            assert all(visit['end'] <= 600 for visit in route['visits'])


@pytest.mark.parametrize('now,expected', [(600, (660, 780)), (700, (700, 780)),
                                          (800, (840, 1080)), (850, (850, 1080))])
def test_capacity_recovers_between_and_after_approved_absences(make_scenario, now, expected):
    session = session_with_route(make_scenario)
    report(session, start_time='10:00', end_time='11:00')
    report(session, start_time='13:00', end_time='14:00')
    batch = preview_replan(session)
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    session = ScenarioSession.restore(session.dump())
    residual, _, _ = session._residual(Event(id='capacity', type='replan', time=now), False)
    worker = next(item for item in residual.engineers if item.id == 'e1')
    assert worker.shift == TimeWindow(start=expected[0], end=expected[1])


def test_inventory_excludes_unconfirmed_return_and_pending_assignments(make_scenario):
    session = session_with_route(make_scenario)
    report(session, start_time='10:00', end_time='11:00', location_id=None)
    session.record_fact({'version': session.version, 'engineer_id': 'e2',
        'status': 'available', 'time': 660, 'location_id': 'office'})
    assert engineer_state(session, 'e1')['return_confirmation_required']
    assert not next(row['dispatchable'] for row in session.inventory if row['engineer_id'] == 'e1')
    session.return_from_absence({'version': session.version, 'engineer_id': 'e1',
        'report_id': session.unavailability[0]['id'], 'time': 660, 'location_id': 'office'})
    assert engineer_state(session, 'e1')['return_pending_replan']
    assert not next(row['dispatchable'] for row in session.inventory if row['engineer_id'] == 'e1')
    session = ScenarioSession.restore(session.dump())
    assert not next(row['dispatchable'] for row in session.inventory if row['engineer_id'] == 'e1')
    batch = preview_replan(session)
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    assert next(row['dispatchable'] for row in session.inventory if row['engineer_id'] == 'e1')


def test_approval_keeps_current_absence_out_of_live_inventory(make_scenario):
    session = session_with_route(make_scenario)
    report(session)
    assert not next(row['dispatchable'] for row in session.inventory if row['engineer_id'] == 'e1')
    batch = preview_replan(session)
    # The proposed residual can use the worker after 10:00; the live clock is
    # still 09:00. That future capacity must not turn the worker available now.
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    assert session.time == 540
    assert engineer_state(session, 'e1')['active_absence']
    assert not next(row['dispatchable'] for row in session.inventory if row['engineer_id'] == 'e1')
    assert not next(row['dispatchable'] for row in ScenarioSession.restore(session.dump()).inventory
                    if row['engineer_id'] == 'e1')


def test_restore_normalizes_legacy_absence_flag_without_rewriting_stock(make_scenario):
    session = session_with_route(make_scenario)
    report(session)
    payload = deepcopy(session.dump())
    row = next(row for row in payload['inventory'] if row['engineer_id'] == 'e1')
    row['dispatchable'] = True  # Previously saved preview-derived flag.
    original_stock = {key: deepcopy(row[key]) for key in ('consumed', 'reserved', 'available')}
    restored = ScenarioSession.restore(payload)
    assert next(row['dispatchable'] for row in payload['inventory'] if row['engineer_id'] == 'e1')
    row = next(row for row in restored.inventory if row['engineer_id'] == 'e1')
    assert not row['dispatchable']
    assert {key: row[key] for key in original_stock} == original_stock


def test_incoming_request_clock_activates_absence_in_live_inventory(make_scenario):
    from test_intake import incoming
    session = session_with_route(make_scenario)
    report(session, start_time='10:00', end_time='11:00', location_id=None)
    assert next(row['dispatchable'] for row in session.inventory if row['engineer_id'] == 'e1')
    session.receive_requests({'requests': [{**incoming('during-absence'), 'time': '10:00'}]})
    assert session.time == 600
    assert engineer_state(session, 'e1')['active_absence']
    assert not next(row['dispatchable'] for row in session.inventory if row['engineer_id'] == 'e1')
