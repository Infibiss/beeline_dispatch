"""Real intake/approval/fact lifecycles through the risk HTTP projection."""
import threading

import pytest

from field_router.scenario_server import create_server
from test_auth import Client
from test_day_control import setup


@pytest.fixture
def client(make_scenario, tmp_path):
    server = create_server(setup(make_scenario), tmp_path/'state.json', port=0,
                           accounts=None, disable_gps=True, time_limit=.03)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield Client(server)
    finally:
        server.shutdown(); server.server_close(); worker.join()


def call(client, path, payload=None):
    status, result, _ = client.request(path, payload)
    assert status == 200, result
    return result


def risk(client):
    result = call(client, '/api/risk')
    state = call(client, '/api/state')
    attention = result['attention']
    assert attention['plan_version'] == state['version']
    assert attention['time'] == state['time']
    assert len({r['id'] for r in attention['items']}) == len(attention['items'])
    assert sum(attention['tiers'].values()) == len(attention['items'])
    assert attention['counts']['deadline'] == len({rid for r in attention['items'] for rid in r.get('deadline_request_ids', [])})
    return result


def apply(client, event):
    state = call(client, '/api/state')
    batch = call(client, '/api/preview', {'version': state['version'], 'event': event, 'preserve_in_progress': True})
    call(client, '/api/approve', {'version': state['version'], 'batch_id': batch['id'],
                                  'candidate_id': batch['recommended_candidate_id']})


def test_urgent_intake_preview_approval_and_cancellation_leave_no_ghosts(client):
    initial = risk(client)
    call(client, '/api/inbox/requests', {'request': {'id': 'urgent-risk', 'type': 'emergency',
        'anchor_id': 'a', 'time': 540, 'duration_minutes': 20, 'router': 0, 'tv_box': 0,
        'window_start': 700, 'window_end': 800}})
    queued = risk(client)
    assert queued['attention']['incoming'] == {'new': 1, 'emergency': 1}
    assert queued['attention']['counts']['unassigned'] == initial['attention']['counts']['unassigned']
    apply(client, {'id': 'assign-risk', 'type': 'replan', 'time': 540})
    assigned = risk(client)
    assert assigned['attention']['incoming']['new'] == 0
    apply(client, {'id': 'cancel-risk', 'type': 'cancel', 'time': 540, 'request_id': 'emergency:urgent-risk'})
    final = risk(client)
    assert not any('emergency:urgent-risk' in r['request_ids'] for r in final['attention']['items'])
    assert final['attention']['incoming']['new'] == 0


def test_outage_reassignment_updates_counts_and_removes_outage(client):
    call(client, '/api/engineer/unavailability', {'version': 1, 'engineer_id': 'e1',
        'start_date': '2026-08-17', 'end_date': '2026-08-17',
        'start_time': '09:00', 'end_time': '18:00', 'reason': 'transport', 'comment': 'Машина недоступна'})
    result = risk(client)
    assert result['attention']['counts']['unassigned'] == 2
    assert len([r for r in result['attention']['items'] if r['kind'] == 'unavailable']) == 1
    assert result['forecast']['status'] == 'stale'
    apply(client, {'id': 'outage-risk', 'type': 'replan', 'time': 540})
    result = risk(client)
    assert not any(r['kind'] == 'unavailable' for r in result['attention']['items'])
    # e2 can take b, but has no router for a; the real resource gap must remain.
    assert result['attention']['counts']['unassigned'] == 1
    assert any(r['id'] == 'unassigned-a' for r in result['attention']['items'])


def test_reported_delay_completion_and_confirmation_clear_old_incidents(client):
    call(client, '/api/engineer/facts', {'version': 1, 'engineer_id': 'e1', 'status': 'working',
        'time': 550, 'request_id': 'a', 'remaining_minutes': 200})
    result = risk(client)
    assert any(r['kind'] == 'delay' and r['tier'] == 'now' for r in result['attention']['items'])
    state = call(client, '/api/state')
    call(client, '/api/engineer/facts', {'version': state['version'], 'engineer_id': 'e1',
        'status': 'completed', 'time': 580, 'request_id': 'a', 'consumed_router': 1, 'consumed_tv_box': 0})
    result = risk(client)
    assert not any(r['kind'] == 'delay' for r in result['attention']['items'])
    assert len([r for r in result['attention']['items'] if r['kind'] == 'confirmation']) == 1
    state = call(client, '/api/state')
    call(client, '/api/completions/confirm', {'version': state['version'],
        'fact_id': state['pending_completions'][0]['id']})
    result = risk(client)
    assert not any('a' in r['request_ids'] for r in result['attention']['items'])
