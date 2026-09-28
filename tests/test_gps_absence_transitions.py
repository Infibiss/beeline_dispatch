"""Absence edits and return boundaries must not revive an old GPS journey."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from field_router.availability import availability_at
from field_router.gps.config import GpsConfig
from field_router.gps.context import project_context
from field_router.gps.models import BaselineGpsModel
from field_router.replanning import ScenarioSession
from test_auth import Client, secured
from test_gps import NOW, StoreOnly, codes, context, packet, point


@pytest.mark.parametrize('offsets,expected_alert', [
    ([.01, .01, .01], 'stopped'),
    ([.001, .005, .01], 'moving_away'),
    ([0, 0, 0], 'near_site'),
])
def test_old_trip_after_absence_has_no_confirmed_motion_alerts(offsets, expected_alert):
    engineer = deepcopy(context()['engineers']['e1'])
    engineer['status']['time'] = 540
    engineer['availability'] = {
        'intervals': [{'start': 600, 'end': 660, 'location_known': True}],
        'return_confirmations': [],
    }
    model = BaselineGpsModel()
    model.routing.estimate = lambda *args: {
        'kind': 'road', 'minutes': 5, 'metres': 1500,
        'description': 'Test route', 'legs': [],
    }
    job = {'engineer': engineer, 'date': '2026-09-20', 'now': NOW,
           'demo_minute': 661, 'routing': {}, 'config': GpsConfig().public(),
           'points': [point(NOW - (2 - i) * 60, offset) for i, offset in enumerate(offsets)]}
    result = model.analyze(job)
    assert result['target']['basis'] == 'plan'
    assert not codes(result) & {'stopped', 'moving_away', 'near_site'}

    # A fresh explicit departure restores the usual GPS journey checks.
    engineer['status']['time'] = 660
    result = model.analyze(job)
    assert result['target']['basis'] == 'confirmed'
    assert expected_alert in codes(result)


def test_http_absence_corrections_resync_gps_without_losing_position(secured, tmp_path):
    server, _, _ = secured
    employee, admin = Client(server), Client(server)
    assert employee.login('worker1')[0] == admin.login('admin')[0] == 200
    plan = admin.request('/api/state')[1]
    day = plan['date']
    now = datetime.fromisoformat(day).replace(hour=6, tzinfo=timezone.utc).timestamp()
    # Exercise HTTP publication with a deterministic GPS store; inference itself
    # is checked above, and no subprocess or network routing is needed here.
    previous = server.gps.service
    gps = StoreOnly(previous.context(), clock=lambda: now)
    previous.close()
    server.gps.service = gps
    gps.ingest(packet(point(now)))

    create = {'version': plan['version'], 'engineer_id': 'e1',
              'start_date': day, 'start_time': '10:00', 'end_date': day, 'end_time': '11:00',
              'reason': 'sick', 'comment': 'Test absence', 'location_id': 'office'}
    status, mobile, _ = employee.request('/api/engineer/unavailability', create)
    assert status == 200
    rid = mobile['scheduled_absences'][0]['id']
    projection = gps.context()['engineers']['e1']['availability']
    assert availability_at(projection, 599) == 'available'
    assert availability_at(projection, 600) == 'absent'

    update = {**create, 'version': mobile['version'], 'report_id': rid, 'start_mode': 'now'}
    status, mobile, _ = employee.request('/api/engineer/unavailability/update', update)
    assert status == 200 and mobile['active_absence']
    current = gps.context()
    assert current['version'] == mobile['version']
    row = next(row for row in gps.snapshot()['rows'] if row['engineer_id'] == 'e1')
    assert row['position'] == point(now)
    assert 'reported_absence' in codes(row) and row['assignment'] is None
    assert employee.request('/api/engineer/unavailability/update', update)[0] == 409
    assert gps.context() == current

    status, mobile, _ = employee.request('/api/engineer/unavailability/cancel', {
        'version': mobile['version'], 'engineer_id': 'e1', 'report_id': rid,
        'correction_reason': 'Mistaken report', 'location_id': 'office'})
    assert status == 200 and mobile['return_pending_replan']
    assert gps.context()['version'] == mobile['version']
    assert gps.context()['engineers']['e1']['tasks'] == []
    row = next(row for row in gps.snapshot()['rows'] if row['engineer_id'] == 'e1')
    assert row['position'] == point(now) and 'reported_absence' not in codes(row)

    status, batch, _ = admin.request('/api/preview', {
        'version': mobile['version'], 'event': {'id': 'after-correction', 'type': 'replan', 'time': 540}})
    assert status == 200
    status, approved, _ = admin.request('/api/approve', {
        'version': mobile['version'], 'batch_id': batch['id'], 'candidate_id': 'stable'})
    assert status == 200 and not approved['needs_replanning']
    restored = ScenarioSession.restore(json.loads((tmp_path / 'state.json').read_text(encoding='utf-8')))
    assert gps.context() == project_context(restored)
    assert gps.context()['version'] == approved['version']
    assert gps.snapshot()['rows'][0]['position'] == point(now)
