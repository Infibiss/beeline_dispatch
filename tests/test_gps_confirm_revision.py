"""A dispatcher can edit travel time, but must confirm the current GPS fix."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from field_router.gps.contracts import GpsInputError
from field_router.gps.delay import confirm
from test_gps_quality_workflow import traveling_session
from test_gps_test_plan import NOW


def confirmation(session, *, elapsed_seconds=120):
    now = NOW.timestamp() + elapsed_seconds
    proposal = {
        'version': session.version, 'engineer_id': 'test-e1',
        'request_id': 'gps-test-001', 'time': session.time + elapsed_seconds // 60,
        'location_id': 'office', 'remaining_minutes': 15,
        'point_measured_at_ms': now * 1000,
    }
    gps = SimpleNamespace(
        delay_draft=lambda payload: deepcopy(proposal), clock=lambda: now,
        config=SimpleNamespace(stale_seconds=90),
    )
    payload = {key: value for key, value in proposal.items()
               if key != 'point_measured_at_ms'}
    payload.update(status='traveling', gps_point_measured_at_ms=now * 1000,
                   comment='Уточнено диспетчером')
    return gps, proposal, payload


def test_recent_replaced_fix_cannot_confirm_old_draft():
    session = traveling_session()
    gps, proposal, payload = confirmation(session)
    # Both fixes are fresh and belong to the same operational minute. Only
    # checking age would allow the obsolete draft to overwrite travel time.
    payload['gps_point_measured_at_ms'] -= 10_000
    before = session.dump()
    with pytest.raises(GpsInputError, match='устарела'):
        confirm(session, gps, payload)
    assert session.dump() == before
    assert payload['time'] == proposal['time']


def test_current_fix_allows_dispatcher_to_correct_estimated_duration():
    session = traveling_session()
    gps, proposal, payload = confirmation(session)
    payload['remaining_minutes'] = 8
    before_plan = session.plan.model_dump()
    confirm(session, gps, payload)
    assert session.statuses['test-e1']['remaining_minutes'] == 8
    assert session.statuses['test-e1']['remaining_minutes'] != proposal['remaining_minutes']
    assert session.statuses['test-e1']['location_id'] == 'office'
    assert session.plan.model_dump() == before_plan
    assert session.needs_replanning
