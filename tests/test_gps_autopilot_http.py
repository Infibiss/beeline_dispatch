"""GPS replanning uses the normal durable, concurrent HTTP publication boundary.

The policy's evidence timer is replaced here; its real signal/quality decisions
are exercised separately. The planner, publication and HTTP requests are real.
"""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import json
import threading
import time

import pytest

from field_router.gps.autopilot import GpsAutopilot, trip_key
from field_router.replanning import ScenarioSession
from field_router.scenario_server import create_server
from test_auth import Client
from test_gps_quality_workflow import traveling_session
from test_gps_test_plan import NOW
from test_intake import incoming, session_with_plan


def approved_traveler():
    session = traveling_session()
    batch = session.preview({'version': session.version, 'event': {
        'id': 'approve-departure', 'type': 'replan', 'time': session.time}}, time_limit=.03)
    session.approve({'version': session.version, 'batch_id': batch['id'],
                    'candidate_id': batch['recommended_candidate_id']})
    session.set_planning_settings({'version': session.version, 'mode': 'autopilot',
                                  'every_n_requests': 10, 'every_n_minutes': None})
    assert not session.needs_replanning and not session.autopilot_due()
    return session


def proposal_for(session):
    fact = session.statuses['test-e1']
    minute = fact['time'] + 2
    measured = NOW.timestamp() + 120
    return {'version': session.version, 'date': session.scenario.date,
            'engineer_id': 'test-e1', 'request_id': 'gps-test-001',
            'trip_key': trip_key(fact), 'time': minute, 'remaining_minutes': 15,
            'low_minutes': 12, 'planned_start': 905, 'delay_minutes': 9,
            'point_measured_at_ms': measured * 1000, 'evaluated_at': measured,
            'expires_at_ms': (measured + 90) * 1000,
            'valid_until_minute': minute + 2, 'route_kind': 'road',
            'source': 'gps_estimate'}


def ready_policy(monkeypatch, *, gate=None):
    """Represent a validated proposal; do not replace planner or persistence."""
    def collect(self, session, gps):
        if (gate is not None and not gate.is_set()
                or session.planning_settings['mode'] != 'autopilot'
                or session.pending_report_ids or session.gps_autopilot_history
                or self._failures
                or session.statuses.get('test-e1', {}).get('status') != 'traveling'):
            return []
        return [proposal_for(session)]
    monkeypatch.setattr(GpsAutopilot, 'collect', collect)
    monkeypatch.setattr(GpsAutopilot, 'revalidate', lambda self, session, gps, proposals:
        bool(proposals) and proposals[0]['version'] == session.version
        and proposals[0]['trip_key'] == trip_key(session.statuses.get('test-e1', {})))


@contextmanager
def running(session, path, **kwargs):
    path.write_text(json.dumps(session.dump()), encoding='utf-8')
    server = create_server(session, path, port=0, time_limit=.03,
                           disable_gps=True, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, Client(server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def eventually(check, *, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.04)
    raise AssertionError('Timed out waiting for the background transaction')


def saved(path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_gps_only_replan_is_persisted_and_reaches_engineer_without_rewriting_facts(
        monkeypatch, tmp_path):
    session = approved_traveler()
    before_facts = deepcopy(session.fact_log)
    path = tmp_path / 'gps-plan.json'
    ready_policy(monkeypatch)
    with running(session, path) as (server, client):
        result = eventually(lambda: saved(path) if saved(path)['gps_autopilot_history'] else None)
        assert result['fact_log'] == before_facts
        assert result['version'] > session.version
        assert len(result['gps_autopilot_history']) == 1
        assert result['history'][-1]['automatic'] is True
        notification = next(row for row in reversed(result['dispatch_notifications'])
                            if row['kind'] == 'auto_applied')
        assert notification['gps_estimates'][0]['request_id'] == 'gps-test-001'
        assert notification['request_ids'] == []
        employee = client.request('/api/engineer?engineer_id=test-e1')[1]
        assert employee['version'] == result['version']
        assert server.gps.service.context()['version'] == result['version']
        assert employee['status']['status'] == 'traveling'
    # A recovered state must not publish the same acknowledged trigger again.
    restored = ScenarioSession.restore(saved(path))
    with running(restored, path) as (_, client):
        observed = threading.Event()
        original = GpsAutopilot.collect
        def observe(self, current, gps):
            observed.set()
            return original(self, current, gps)
        monkeypatch.setattr(GpsAutopilot, 'collect', observe)
        assert observed.wait(5)
        assert client.request('/api/state')[1]['version'] == restored.version
        assert len(saved(path)['gps_autopilot_history']) == 1


def test_failed_save_does_not_publish_or_acknowledge_gps_and_can_retry(monkeypatch, tmp_path):
    session = approved_traveler()
    path = tmp_path / 'atomic.json'
    ready_policy(monkeypatch)
    attempted, release = threading.Event(), threading.Event()
    replace = Path.replace
    def failing_replace(source, target):
        if Path(target) == path and not release.is_set():
            attempted.set()
            raise OSError('Test state disk unavailable')
        return replace(source, target)
    monkeypatch.setattr(Path, 'replace', failing_replace)
    with running(session, path) as (server, client):
        try:
            assert attempted.wait(6)
            assert client.request('/api/state')[1]['version'] == session.version
            assert server.gps.service.context()['version'] == session.version
            assert saved(path)['gps_autopilot_history'] == []
            assert saved(path)['fact_log'] == session.fact_log
        finally:
            release.set()
        result = eventually(lambda: saved(path) if saved(path)['gps_autopilot_history'] else None)
        assert len(result['gps_autopilot_history']) == 1
        assert result['version'] > session.version


def test_human_fact_and_telemetry_remain_responsive_and_invalidate_running_gps_solve(
        monkeypatch, tmp_path):
    session = approved_traveler()
    path = tmp_path / 'concurrent.json'
    ready_policy(monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    preview = ScenarioSession.preview
    def slow_preview(self, payload, **kwargs):
        automatic = payload.get('event', {}).get('id', '').startswith('auto:')
        if automatic:
            entered.set()
            assert release.wait(8), 'Test did not release the GPS calculation'
        try:
            return preview(self, payload, **kwargs)
        finally:
            if automatic:
                finished.set()
    monkeypatch.setattr(ScenarioSession, 'preview', slow_preview)
    with running(session, path) as (server, client):
        try:
            assert entered.wait(6)
            server.gps.service.enabled = True
            assert client.request('/api/gps/telemetry', {
                'engineer_id': 'test-e1', 'tracker_id': 'test-http', 'state': 'active'})[0] == 202
            status, changed, _ = client.request('/api/engineer/facts', {
                'version': session.version, 'engineer_id': 'test-e1', 'status': 'available',
                'time': session.time + 3, 'location_id': 'office'})
            assert status == 200
            assert not finished.is_set()
        finally:
            release.set()
        assert finished.wait(5)
        # Wait until the policy has observed the post-fact state too.
        observed = threading.Event()
        collect = GpsAutopilot.collect
        def observe(self, current, gps):
            if current.version == changed['version']:
                observed.set()
            return collect(self, current, gps)
        monkeypatch.setattr(GpsAutopilot, 'collect', observe)
        assert observed.wait(5)
        result = saved(path)
        assert result['version'] == changed['version']
        assert result['statuses']['test-e1']['status'] == 'available'
        assert result['gps_autopilot_history'] == []


def test_model_failure_does_not_stop_normal_intake_autopilot(monkeypatch, make_scenario, tmp_path):
    session = session_with_plan(make_scenario)
    session.set_planning_settings({'version': session.version, 'mode': 'autopilot',
                                  'every_n_requests': 1, 'every_n_minutes': None})
    session.receive_requests({'request': incoming('despite-gps-failure')})
    failed = threading.Event()
    def broken(self, current, gps):
        failed.set()
        raise RuntimeError('Synthetic GPS policy unavailable')
    monkeypatch.setattr(GpsAutopilot, 'collect', broken)
    path = tmp_path / 'intake.json'
    with running(session, path) as (server, client):
        server.gps.service.enabled = True
        result = eventually(lambda: saved(path) if saved(path)['decision_log'] else None)
        assert failed.is_set()
        assert result['incoming_requests'][0]['status'] == 'assigned'
        assert result['decision_log'][0]['automatic'] is True
        assert client.request('/api/state')[0] == 200


def traveler_with_due_request():
    session = approved_traveler()
    session.set_planning_settings({'version': session.version, 'mode': 'autopilot',
                                  'every_n_requests': 1, 'every_n_minutes': None})
    session.receive_requests({'request': {
        **incoming('during-gps'), 'anchor_id': 'gps-test-001', 'time': session.time,
        'window_start': '16:00', 'window_end': '17:00', 'duration_minutes': 10}})
    return session


def test_failed_gps_calculation_falls_back_to_normal_request_intake(monkeypatch, tmp_path):
    from field_router import intake
    session = traveler_with_due_request()
    ready_policy(monkeypatch)
    apply = intake.apply_autopilot
    attempted = threading.Event()
    def fail_gps(current, **kwargs):
        if kwargs.get('gps_proposals'):
            attempted.set()
            raise RuntimeError('Synthetic failed GPS proposal solve')
        return apply(current, **kwargs)
    monkeypatch.setattr(intake, 'apply_autopilot', fail_gps)
    path = tmp_path / 'solve-fallback.json'
    with running(session, path) as (_, client):
        result = eventually(lambda: saved(path) if saved(path)['decision_log'] else None)
        assert attempted.is_set()
        assert result['incoming_requests'][0]['status'] == 'assigned'
        assert result['gps_autopilot_history'] == []
        assert result['fact_log'] == session.fact_log
        assert any(row['kind'] == 'auto_gps_error' for row in result['dispatch_notifications'])
        assert result['autopilot_last_failure_at'] is None
        assert client.request('/api/state')[0] == 200


@pytest.mark.parametrize('failure', ['changed_estimate', 'model_exception'])
def test_gps_revalidation_failure_does_not_starve_pending_intake(monkeypatch, tmp_path, failure):
    session = traveler_with_due_request()
    ready_policy(monkeypatch)
    attempted = threading.Event()
    def reject(self, current, gps, proposals):
        attempted.set()
        if failure == 'model_exception':
            raise RuntimeError('Synthetic GPS unavailable after solve')
        return False
    monkeypatch.setattr(GpsAutopilot, 'revalidate', reject)
    path = tmp_path / 'revalidate.json'
    with running(session, path) as (_, client):
        result = eventually(lambda: saved(path) if saved(path)['decision_log'] else None)
        assert attempted.is_set()
        assert result['incoming_requests'][0]['status'] == 'assigned'
        assert result['gps_autopilot_history'] == []
        assert result['fact_log'] == session.fact_log
        assert result['autopilot_last_failure_at'] is None
        assert client.request('/api/state')[0] == 200


def test_demo_reset_invalidates_inflight_gps_plan_even_at_same_version(monkeypatch, tmp_path):
    session = approved_traveler()
    baseline = ScenarioSession.restore(session.dump())
    path = tmp_path / 'reset.json'
    gate = threading.Event()
    gate.set()
    ready_policy(monkeypatch, gate=gate)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    preview = ScenarioSession.preview
    def slow_preview(self, payload, **kwargs):
        if payload.get('event', {}).get('id', '').startswith('auto:'):
            entered.set()
            assert release.wait(8)
            try:
                return preview(self, payload, **kwargs)
            finally:
                finished.set()
        return preview(self, payload, **kwargs)
    monkeypatch.setattr(ScenarioSession, 'preview', slow_preview)
    with running(session, path, demo_reset_session=baseline) as (_, client):
        old = client.request('/api/state')[1]
        try:
            assert entered.wait(6)
            gate.clear()
            status, reset, _ = client.request('/api/demo-stream/reset', {
                'version': old['version'], 'demo_run_id': old['demo_run_id']})
            assert status == 200
            assert reset['version'] == old['version']
            assert reset['demo_run_id'] != old['demo_run_id']
        finally:
            release.set()
        assert finished.wait(5)
        # Observe a subsequent iteration, after the old result was discarded.
        observed = threading.Event()
        collect = GpsAutopilot.collect
        def observe(self, current, gps):
            if not gate.is_set():
                observed.set()
            return collect(self, current, gps)
        monkeypatch.setattr(GpsAutopilot, 'collect', observe)
        assert observed.wait(5)
        assert client.request('/api/state')[1]['demo_run_id'] == reset['demo_run_id']
        assert saved(path)['gps_autopilot_history'] == []
        assert saved(path)['version'] == reset['version']
