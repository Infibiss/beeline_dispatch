"""Absence handling must preserve telemetry and suspend only work predictions."""
from copy import deepcopy

import pytest

from field_router.gps.config import GpsConfig
from field_router.gps.context import project_context
from field_router.gps.contracts import GpsInputError
from field_router.gps.delay import draft
from field_router.gps.models import BaselineGpsModel, choose_target
from test_engineer_unavailability import session_with_route, report
from test_gps import NOW, context, point, packet, service, codes


@pytest.mark.parametrize('minute,expected', [(599, 'confirmed'), (600, None),
                                           (659, None), (660, 'plan')])
def test_gps_respects_absence_boundaries_and_requires_a_new_trip(make_scenario, minute, expected):
    session = session_with_route(make_scenario)
    report(session, start_time='10:00', end_time='11:00')
    engineer = deepcopy(context()['engineers']['e1'])
    engineer['status']['time'] = 540
    engineer['availability'] = project_context(session)['engineers']['e1']['availability']
    model = BaselineGpsModel()
    model.routing.estimate = lambda *args: {
        'kind': 'road', 'minutes': 5, 'metres': 1500,
        'description': 'Тестовый маршрут', 'legs': []}
    result = model.analyze({'engineer': engineer, 'date': '2026-09-20', 'now': NOW,
        'demo_minute': minute, 'routing': {}, 'config': GpsConfig().public(),
        'points': [point(NOW-120), point(NOW-60), point(NOW)]})
    if expected is None:
        assert result['target'] is None and result['eta'] is None and result['alerts'] == []
    else:
        assert result['target']['basis'] == expected


def test_gps_context_drops_pending_assignments_and_restores_after_confirmation(make_scenario):
    session = session_with_route(make_scenario)
    assert project_context(session)['engineers']['e1']['tasks']
    report(session, start_time='10:00', end_time='11:00', location_id=None)
    ctx = project_context(session)
    assert [task['id'] for task in ctx['engineers']['e1']['tasks']] == ['a']
    assert session.pending_reassignment_ids('e1') == ['b']
    engineer = deepcopy(context()['engineers']['e1'])
    engineer['availability'] = ctx['engineers']['e1']['availability']
    assert choose_target(engineer, 660)[0] is None
    session.record_fact({'version': session.version, 'engineer_id': 'e1',
        'status': 'available', 'time': 660, 'location_id': 'office'})
    engineer['availability'] = project_context(session)['engineers']['e1']['availability']
    engineer['status'] = session.statuses['e1']
    assert choose_target(engineer, 660)[2] == 'plan'


def test_snapshot_keeps_position_and_suppresses_shift_alert_during_absence(make_scenario):
    session = session_with_route(make_scenario)
    report(session)
    ctx = context()
    ctx['engineers']['e1']['availability'] = project_context(session)['engineers']['e1']['availability']
    ctx['engineers']['e1']['status'] = None
    now = NOW+20*60
    with service(clock=lambda: now) as gps:
        gps.update_context(ctx)
        gps.ingest(packet(point(now)))
        row = gps.snapshot()['rows'][0]
        assert row['position'] == point(now)
        assert row['assignment'] is None
        assert 'shift_unconfirmed' not in codes(row)
        assert 'reported_absence' in codes(row)
        with pytest.raises(GpsInputError, match='недоступность'):
            draft(ctx, gps.snapshot(), 'e1')
