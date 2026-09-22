from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from field_router.replanning import ScenarioSession
from test_auth import Client, PASSWORD, secured


@pytest.mark.parametrize(('path', 'engineer_id', 'csrf', 'status'), [
    ('/r/east/api/gps/telemetry', 'e1', True, 202),
    ('/regions/east/api/gps/telemetry', 'e1', True, 202),
    ('/r/east/api/gps/telemetry', 'e2', True, 403),
    ('/r/south-east/api/gps/telemetry', 'e1', True, 403),
    ('/r/east/api/gps/telemetry', 'e1', False, 403),
])
def test_authenticated_telemetry_does_not_wait_for_preview(
        secured, monkeypatch, path, engineer_id, csrf, status):
    server, _, _ = secured
    server.gps.service.enabled = True
    employee, admin = Client(server), Client(server)
    assert employee.login('worker1')[0] == 200
    assert admin.login('admin')[0] == 200
    entered, release = threading.Event(), threading.Event()

    def slow_preview(self, payload, **kwargs):
        entered.set()
        assert release.wait(5), 'Test did not release the planner'
        return {}

    monkeypatch.setattr(ScenarioSession, 'preview', slow_preview)
    with ThreadPoolExecutor(max_workers=2) as pool:
        preview = pool.submit(admin.request, '/r/east/api/preview', {})
        try:
            assert entered.wait(2), 'Preview did not acquire the planner lock'
            telemetry = pool.submit(employee.request, path, {
                'engineer_id': engineer_id, 'tracker_id': 'concurrent-device',
                'state': 'active',
            }, csrf=csrf)
            # A real HTTP response must arrive before the blocked preview is
            # released. No solver duration or server scheduling guess is needed.
            response = telemetry.result(timeout=1)
            assert response[0] == status
            assert not preview.done()
        finally:
            release.set()
        assert preview.result(timeout=2)[0] == 200


@pytest.mark.parametrize('region', ['east', 'south-east'])
def test_roster_access_changes_only_after_successful_save(
        secured, monkeypatch, region):
    server, accounts, _ = secured
    accounts.create_engineer('south-worker', PASSWORD, 'south-east', 'e1')
    accounts.create_engineer('new-worker', PASSWORD, region, 'e3')
    employee, other_employee, admin = Client(server), Client(server), Client(server)
    new_employee = Client(server)
    username = 'worker1' if region == 'east' else 'south-worker'
    other_username = 'south-worker' if region == 'east' else 'worker1'
    assert employee.login(username)[0] == 200
    assert other_employee.login(other_username)[0] == 200
    assert admin.login('admin')[0] == 200
    assert new_employee.login('new-worker')[0] == 403
    base = '/r/' + region
    status, crew, _ = admin.request(base + '/api/crew')
    assert status == 200
    status, draft, _ = admin.request(base + '/api/crew/preview', {
        'version': crew['version'],
        'engineers': [row for row in crew['engineers'] if row['id'] != 'e1']
                     + [{**crew['engineers'][0], 'id': 'e3', 'name': 'New engineer'}],
    })
    assert status == 200
    assert employee.request('/api/auth/me')[0] == 200
    apply = {'version': crew['version'], 'id': draft['id']}

    def fail_save(self, target):
        raise OSError('Simulated unavailable state file')

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'replace', fail_save)
        assert admin.request(base + '/api/crew/apply', apply)[0] == 500
    assert employee.request('/api/auth/me')[0] == 200
    assert new_employee.login('new-worker')[0] == 403
    assert admin.request(base + '/api/crew/apply', apply)[0] == 200
    assert employee.request('/api/auth/me')[0] == 401
    assert employee.request(base + '/api/gps/telemetry', {
        'engineer_id': 'e1', 'tracker_id': 'removed-device', 'state': 'active',
    })[0] == 401
    assert employee.login(username)[0] == 403
    assert other_employee.request('/api/auth/me')[0] == 200
    assert new_employee.login('new-worker')[0] == 200
