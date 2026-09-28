"""An absence/return declaration does not invent a route origin."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from field_router.availability import availability_at
from field_router.engineer import engineer_state, record_engineer_fact
from field_router.gps.context import project_context
from field_router.gps.models import choose_target
from field_router.replanning import ScenarioSession
from test_engineer_unavailability import session_with_route, report, preview_replan
from test_engineer_absence_edit import edit_payload, cancel_payload
from test_gps import StoreOnly, packet, point, codes


def approve(session, identity):
    batch = preview_replan(session, identity)
    for candidate in batch['candidates']:
        assert not any(route['engineer_id'] == 'e1' and route['visits']
                       for route in candidate['plan']['routes'])
    session.approve({'version': session.version, 'batch_id': batch['id'],
                     'candidate_id': 'stable'})


@pytest.mark.parametrize('action', ['return', 'cancel', 'indefinite'])
def test_unknown_return_is_held_across_approval_and_restore_then_known_fact_releases(make_scenario, action):
    session = session_with_route(make_scenario)
    session.record_fact({'version': session.version, 'engineer_id': 'e1',
                         'status': 'available', 'time': 540, 'location_id': 'office'})
    if action == 'indefinite':
        session.record_fact({'version': session.version, 'engineer_id': 'e1',
                             'status': 'unavailable', 'time': 540})
        session.return_from_absence({'version': session.version, 'engineer_id': 'e1', 'time': 540})
    else:
        report(session, location_id=None)
        if action == 'return':
            session.return_from_absence({'version': session.version, 'engineer_id': 'e1',
                'report_id': session.unavailability[0]['id'], 'time': 540})
        else:
            session.cancel_unavailability(cancel_payload(session, location_id=None))
    assert session.statuses['e1']['location_id'] is None
    assert session.location_confirmation_pending('e1')
    mobile = engineer_state(session, 'e1')
    assert not mobile['return_confirmation_required']
    assert mobile['location_confirmation_pending']
    assert session.return_pending_replan('e1')
    original_aliases = dict(session.aliases)
    approve(session, 'unknown-location')
    assert not any(':e1:' in key for key in set(session.aliases) - set(original_aliases))
    assert session.location_confirmation_pending('e1')
    assert not session.return_pending_replan('e1')
    restored = ScenarioSession.restore(session.dump())
    assert restored.location_confirmation_pending('e1')
    approve(restored, 'still-unknown')
    gps = project_context(restored)['engineers']['e1']
    assert availability_at(gps['availability'], 540) == 'awaiting_location'
    assert choose_target(gps, 540) == (None, 0, 'none')

    # Actual whereabouts can be clarified without waiting an artificial minute.
    restored.record_fact({'version': restored.version, 'engineer_id': 'e1',
                          'status': 'available', 'time': 540, 'location_id': 'office'})
    assert not restored.location_confirmation_pending('e1')
    batch = preview_replan(restored, 'confirmed-location')
    # The engineer is usable again; a cost strategy need not assign every worker.
    assert any(row['engineer_id'] == 'e1' and row['dispatchable']
               for row in batch['inventory'])


def test_live_report_edit_use_wall_clock_without_advancing_plan(make_scenario):
    session = session_with_route(make_scenario)
    session.execution_mode = 'manual'
    session.report_unavailability({'version': session.version, 'engineer_id': 'e1',
        'start_mode': 'now', 'start_date': session.scenario.date, 'start_time': '09:00',
        'end_date': session.scenario.date, 'end_time': '12:00',
        'reason': 'personal', 'comment': 'Приём по другому адресу'}, current_minute=570)
    period = session.unavailability[0]
    assert period['start_time'] == '09:30' and period['location_id'] is None
    assert session.time == 540 and session.history[-1]['event']['time'] == 570
    session.update_unavailability(edit_payload(session, end_time='11:30'), current_minute=580)
    assert session.time == 540 and session.history[-1]['event']['time'] == 580
    assert period['start_time'] == '09:30'
    session.cancel_unavailability(cancel_payload(session, location_id=None), current_minute=590)
    assert session.unavailability[0]['revoked']
    assert session.statuses['e1']['time'] == 590 and session.location_confirmation_pending('e1')


def test_active_unknown_absence_is_reportable_but_never_guesses_a_departure(make_scenario):
    session = session_with_route(make_scenario)
    report(session, location_id=None)
    approve(session, 'active-without-point')
    assert not session.fact_log
    session.time = 600
    assert engineer_state(session, 'e1')['return_confirmation_required']
    session.return_from_absence({'version': session.version, 'engineer_id': 'e1',
        'report_id': session.unavailability[0]['id'], 'time': 600})
    assert not engineer_state(session, 'e1')['return_confirmation_required']
    assert session.location_confirmation_pending('e1')
    approve(session, 'ended-without-point')


def test_cancel_without_point_keeps_separate_indefinite_absence(make_scenario):
    session = session_with_route(make_scenario)
    session.record_fact({'version': session.version, 'engineer_id': 'e1',
                         'status': 'unavailable', 'time': 540})
    original = deepcopy(session.fact_log)
    report(session, location_id=None)
    session.cancel_unavailability(cancel_payload(session, location_id=None))
    assert session.fact_log == original
    assert session.statuses['e1']['status'] == 'unavailable'
    assert engineer_state(session, 'e1')['indefinite_absence']
    approve(session, 'indefinite-remains')


def test_same_minute_declarations_keep_accepted_order_when_timestamps_tie(make_scenario, monkeypatch):
    import field_router.fact_journal as journal

    class FrozenDateTime:
        @staticmethod
        def now(*args):
            return datetime(2026, 8, 17, 6, tzinfo=timezone.utc)

    monkeypatch.setattr(journal, 'datetime', FrozenDateTime)
    session = session_with_route(make_scenario)
    for status, location in [('available', 'office'), ('unavailable', None),
                             ('available', None), ('available', 'office')]:
        session.record_fact({'version': session.version, 'engineer_id': 'e1',
                             'status': status, 'time': 540, 'location_id': location})
    assert [fact['sequence'] for fact in session.fact_log] == [1, 2, 3, 4]
    restored = ScenarioSession.restore(session.dump())
    restored.record_fact({'version': restored.version, 'engineer_id': 'e2',
                          'status': 'available', 'time': 540})
    assert restored.statuses['e1']['status'] == 'available'
    assert restored.statuses['e1']['location_id'] == 'office'
    assert restored.fact_log[:4] == session.fact_log


@pytest.mark.parametrize('status', ['available', 'unavailable'])
def test_gps_position_remains_visible_but_unknown_route_origin_has_no_eta(make_scenario, status):
    session = session_with_route(make_scenario)
    session.record_fact({'version': session.version, 'engineer_id': 'e1',
                         'status': status, 'time': 540})
    now = datetime.fromisoformat(session.scenario.date).replace(hour=6, tzinfo=timezone.utc).timestamp()
    gps = StoreOnly(project_context(session), clock=lambda: now)
    try:
        gps.ingest(packet(point(now)))
        row = next(row for row in gps.snapshot()['rows'] if row['engineer_id'] == 'e1')
        assert row['position'] == point(now)
        assert row['assignment'] is None
        assert row.get('eta') is None
        absence = next(item for item in row['alerts'] if item['code'] == 'reported_absence')
        assert absence['title'] == ('Точка для нового маршрута не подтверждена' if status == 'available'
                                   else 'Инженер сообщил о недоступности')
        assert not codes(row) & {'late', 'stopped', 'moving_away', 'near_site'}
    finally:
        gps.close()
