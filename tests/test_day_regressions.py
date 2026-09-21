"""Regressions at the boundary between recorded facts and published day plans."""
import json

import pytest

from test_replanning import demo
from test_real_replanning import real_session
from field_router.models import TimeWindow
from field_router.replanning import ScenarioSession


def approve_event(session, event):
    batch = session.preview(
        {'version': session.version, 'event': event, 'preserve_in_progress': True},
        time_limit=.03,
    )
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    return batch


@pytest.mark.parametrize('late_completion', [False, True])
def test_cancelled_interrupted_job_stays_cancelled_after_restart_and_replan(make_scenario, late_completion):
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    session.record_fact({'version': 1, 'time': 550, 'engineer_id': 'e1', 'request_id': 'a',
                         'status': 'working', 'remaining_minutes': 30})
    session.record_fact({'version': 2, 'time': 560, 'engineer_id': 'e1', 'request_id': 'a',
                         'status': 'interrupted', 'remaining_minutes': 20,
                         'consumed_router': 1, 'consumed_tv_box': 0})
    approve_event(session, {'id': 'cancel-a', 'type': 'cancel', 'time': 570, 'request_id': 'a'})
    assert 'a' not in {request.id for request in session.scenario.requests}
    if late_completion:
        session.record_fact({'version': session.version, 'time': 565, 'engineer_id': 'e1',
                             'request_id': 'a', 'status': 'completed'})
        assert session.fact_audit[-1]['fact']['status'] == 'completed'
        assert session.fact_audit[-1]['fact']['consumed']['router'] == 0

    session = ScenarioSession.restore(json.loads(json.dumps(session.dump())))
    batch = approve_event(session, {'id': 'after-cancel', 'type': 'replan', 'time': 580})

    assert 'a' not in {request.id for request in session.scenario.requests}
    for candidate in batch['candidates']:
        assert 'a' not in {visit['request_id'] for route in candidate['plan']['routes']
                           for visit in route['visits']}
        assert 'a' not in {request['request_id'] for request in candidate['plan']['unassigned']}
    inventory = next(row for row in session.inventory if row['engineer_id'] == 'e1')
    assert inventory['consumed']['router'] == 1
    assert inventory['reserved']['router'] == 0
    assert inventory['available']['router'] == 0
    assert sum(visit['consumed_equipment']['router'] for route in session.frozen
               for visit in route['visits'] if visit['request_id'] == 'a') == 1
    for row in session.inventory:
        for device in ('router', 'tv_box'):
            quantities = [row[category][device] for category in ('consumed', 'reserved', 'available')]
            assert all(quantity >= 0 for quantity in quantities)
            assert sum(quantities) == getattr(session.initial_stock[row['engineer_id']], device)


@pytest.mark.parametrize(('now', 'shift_end'), [(695, 1080), (690, 710)])
def test_fresh_road_redirect_can_release_a_trip_that_no_longer_fits(real_session, now, shift_end):
    scenario = real_session.scenario.model_copy(update={'engineers': [
        engineer.model_copy(update={'shift': TimeWindow(start=540, end=shift_end)})
        for engineer in real_session.scenario.engineers
    ]})
    session = ScenarioSession(scenario, real_session.plan, real_session.geography)
    session.record_fact({'version': 1, 'time': now, 'engineer_id': 'e1',
                         'status': 'traveling', 'request_id': 'a',
                         'location_id': 'b', 'remaining_minutes': 10})
    payload = {'version': 2, 'event': {'id': 'redirect', 'type': 'emergency', 'time': now,
                                      'anchor_id': 'b', 'duration_minutes': 5}}

    with pytest.raises(ValueError, match='окно|смену'):
        session.preview(payload, time_limit=.03)
    batch = session.preview({**payload, 'allow_redirect': True}, time_limit=.03)

    residual = session.batches[batch['id']]['scenario']
    worker = next(engineer for engineer in residual.engineers if engineer.id == 'e1')
    assert worker.shift.start == now
    assert session.aliases[worker.start_location] == 'b'
    assert not any(visit['request_id'] == 'a' for route in batch['frozen'] for visit in route['visits'])
    assert all(candidate['emergency']['assigned'] for candidate in batch['candidates'])
    assert all(candidate['emergency']['start'] >= now for candidate in batch['candidates'])


@pytest.mark.parametrize('geography', ['synthetic', 'road'])
def test_advertised_office_emergency_uses_scenario_address(make_scenario, real_session, geography):
    if geography == 'synthetic':
        scenario, plan = demo(make_scenario)
        session = ScenarioSession(scenario, plan)
    else:
        session = real_session
        assert session.geo_locations['office'].original_address == ''
        assert session.geo_locations['office'].label == ''
    assert 'office' in {location['id'] for location in session.state()['locations']}

    approve_event(session, {'id': 'at-office', 'type': 'emergency', 'time': 540,
                            'anchor_id': 'office', 'duration_minutes': 5})

    emergency = session.catalog['emergency:at-office']
    assert emergency.address == session.scenario.office_address
    if geography == 'synthetic':
        assert session.scenario.points[emergency.id] == session.scenario.points['office']
    else:
        assert session.aliases[emergency.id] == 'office'
    restored = ScenarioSession.restore(json.loads(json.dumps(session.dump())))
    assert restored.catalog[emergency.id].address == session.scenario.office_address
