"""Absence reports never require or establish a physical route origin."""
import json
import pytest

from test_engineer_clock import running, instant
from test_engineer_unavailability import session_with_route


@pytest.mark.parametrize('location', [None, 'office', 'not-in-route', {'lat': 0, 'lon': 0}])
@pytest.mark.parametrize('start_mode', ['now', 'later'])
def test_report_and_edit_ignore_legacy_location_fields(make_scenario, tmp_path, location, start_mode):
    session = session_with_route(make_scenario)
    with running(session, tmp_path, [instant(9, 1)]) as (client, path):
        payload = {'version': session.version, 'engineer_id': 'e1',
            'start_mode': start_mode, 'start_date': session.scenario.date,
            'start_time': '09:01' if start_mode == 'now' else '09:20',
            'end_date': session.scenario.date, 'end_time': '10:01',
            'reason': 'sick', 'comment': 'Болезнь'}
        if location is not None:
            payload['location_id'] = location
        status, data, _ = client.request('/api/engineer/unavailability', payload)
        assert status == 200, data
        period = data['unavailability'][0]
        assert period['location_id'] is None
        assert data['facts'] == [] and data['status'] is None
        assert data['needs_replanning'] and data['pending_reassignment_ids']
        if start_mode == 'now':
            assert data['active_absence']['id'] == period['id']
        else:
            assert data['active_absence'] is None
        status, updated, _ = client.request('/api/engineer/unavailability/update', {
            **payload, 'version': data['version'], 'report_id': period['id'],
            'start_mode': 'later', 'end_time': '10:31',
            'location_id': 'stale-client-location'})
        assert status == 200, updated
        assert updated['unavailability'][0]['location_id'] is None
        saved = json.loads(path.read_text(encoding='utf-8'))
        assert saved['unavailability'][0]['location_id'] is None
        assert saved['fact_log'] == []
        dispatch = client.request('/api/state')[1]
        assert any(note.get('report_id') == period['id'] for note in dispatch['dispatch_notifications'])


def test_open_ended_unavailability_does_not_use_location(make_scenario, tmp_path):
    session = session_with_route(make_scenario)
    with running(session, tmp_path, [instant(9, 1)]) as (client, path):
        status, data, _ = client.request('/api/engineer/facts', {
            'version': session.version, 'engineer_id': 'e1', 'time': 541,
            'status': 'unavailable', 'comment': 'Болезнь', 'location_id': 'not-in-route'})
        assert status == 200, data
        assert data['indefinite_absence']['status'] == 'unavailable'
        assert data['indefinite_absence']['location_id'] is None
        assert 'location_id' not in data['indefinite_absence']['input']
