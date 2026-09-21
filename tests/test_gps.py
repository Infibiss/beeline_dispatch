"""GPS behavior, bounded storage, and real subprocess failure isolation."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from field_router.gps.config import GpsConfig
from field_router.gps.contracts import GpsInputError, distance
from field_router.gps.demo import run_demo
from field_router.gps.models import BaselineGpsModel, smooth_track
from field_router.gps.service import GpsService


NOW = datetime(2026, 9, 20, 6, tzinfo=timezone.utc).timestamp()  # 09:00 Moscow


def context():
    return {'version': 1, 'date': '2026-09-20', 'engineers': {'e1': {
        'id': 'e1', 'name': 'Инженер 1', 'transport': 'car',
        'shift': {'start': 540, 'end': 1080},
        'status': {'status': 'traveling', 'request_id': 'a'},
        'tasks': [{'id': 'a', 'address': 'Адрес A', 'point': {'lat': 55.7, 'lon': 37.7},
                   'windows': [{'start': 550, 'end': 650}], 'start': 550, 'end': 580, 'duration_minutes': 30}],
    }}}


def point(now=NOW, offset=.01, accuracy=10):
    return {'lat': 55.7 + offset, 'lon': 37.7, 'accuracy_m': accuracy, 'measured_at_ms': now * 1000}


def packet(p=None, tracker='phone', state='active', **extra):
    return {'engineer_id': 'e1', 'tracker_id': tracker, 'state': state, 'point': p, **extra}


class StoreOnly(GpsService):
    """No inference needed for deterministic storage/clock validation tests."""
    def _run(self):
        self._stop.wait()


@contextmanager
def service(cls=StoreOnly, **kwargs):
    instance = cls(context(), **kwargs)
    try:
        yield instance
    finally:
        instance.close()


def wait_for(check, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.03)
    raise AssertionError('Timed out waiting for GPS state')


def codes(row):
    return {a['code'] for a in row['alerts']}


def analyze(offsets, *, age=0, engineer=None, minute=540, date='2026-09-20'):
    return BaselineGpsModel().analyze({
        'points': [point(NOW - age - (len(offsets) - 1 - i) * 60, delta) for i, delta in enumerate(offsets)],
        'engineer': engineer or context()['engineers']['e1'], 'config': GpsConfig().public(),
        'date': date, 'now': NOW, 'demo_minute': minute,
    })


def test_kalman_reduces_noise_on_known_synthetic_motion():
    rng = random.Random(42)
    points, filtered_error, raw_error = [], [], []
    for i in range(100):
        truth = point(NOW - 100 + i, offset=i * .00002)
        noisy = {**truth, 'lat': truth['lat'] + rng.gauss(0, 15 / 111195),
                 'lon': truth['lon'] + rng.gauss(0, 15 / 62680), 'accuracy_m': 50}
        points.append(noisy)
        if i >= 20:
            filtered_error.append(distance(smooth_track(points), truth)**2)
            raw_error.append(distance(noisy, truth)**2)
    assert sum(filtered_error) < sum(raw_error) * .8


def test_motion_rules_require_multiple_fixes_and_confirmed_travel():
    assert 'stopped' not in codes(analyze([.01]))
    assert 'stopped' in codes(analyze([.01, .01, .01]))
    working = context()['engineers']['e1']
    working['status'] = {'status': 'working', 'request_id': 'a', 'remaining_minutes': 30}
    assert 'stopped' not in codes(analyze([.01, .01, .01], engineer=working))
    assert 'moving_away' in codes(analyze([.001, .003, .006]))
    assert 'moving_away' not in codes(analyze([.006, .003, .001]))
    assert 'near_site' in codes(analyze([.0001, .0001]))


def test_later_time_window_prevents_false_lateness_and_old_plan_is_not_today():
    assert 'late' in codes(analyze([.04, .04, .04], minute=649))
    engineer = context()['engineers']['e1']
    engineer['tasks'][0]['windows'].append({'start': 900, 'end': 1000})
    assert 'late' not in codes(analyze([.04, .04, .04], engineer=engineer, minute=649))
    old = analyze([.04, .04, .04], minute=None, date='2026-08-17')
    assert not old['alerts'] and old['eta'] is None and old['filtered']
    assert not analyze([.04, .04, .04], age=180)['alerts']


def test_heartbeat_does_not_refresh_fix_or_prove_attendance():
    clock = [NOW]
    with service(clock=lambda: clock[0]) as gps:
        gps.ingest(packet(point()))
        clock[0] += 95
        gps.ingest(packet())
        row = gps.snapshot()['rows'][0]
        assert row['point_age_seconds'] == 95 and row['heartbeat_age_seconds'] == 0
        assert 'stale' in codes(row) and 'no_contact' not in codes(row)
        clock[0] += 121
        assert 'no_contact' in codes(gps.snapshot()['rows'][0])
        assert gps.context()['engineers']['e1']['status']['status'] == 'traveling'


def test_bad_accuracy_jump_duplicate_and_clock_validation():
    clock = [NOW]
    with service(clock=lambda: clock[0]) as gps:
        assert gps.ingest(packet(point()))['accepted']
        clock[0] += 1
        assert not gps.ingest(packet(point()))['accepted']
        clock[0] += 1
        assert gps.ingest(packet(point(clock[0], accuracy=500)))['reason'] == 'low_accuracy'
        clock[0] += 1
        assert gps.ingest(packet(point(clock[0], offset=2)))['reason'] == 'jump'
        assert gps.snapshot()['rows'][0]['position'] == point()
        assert len(gps.trace('e1', 'device')['samples']) == 3
        for invalid in (point(clock[0] + 100), point(clock[0] - 700), {**point(), 'lat': float('nan')}):
            with pytest.raises(GpsInputError):
                gps.ingest(packet(invalid))


def test_tracker_lease_release_and_out_of_order_control_messages():
    clock = [NOW]
    with service(clock=lambda: clock[0]) as gps:
        gps.ingest(packet(point(), sequence=1))
        with pytest.raises(GpsInputError) as exc:
            gps.ingest(packet(point(), tracker='phone-2'))
        assert exc.value.status == 409
        gps.ingest(packet(state='off', sequence=3))
        assert gps.ingest(packet(sequence=2))['reason'] == 'old_message'
        assert gps.snapshot()['rows'][0]['tracking_state'] == 'off'
        gps.ingest(packet(point(), tracker='phone-2'))
        assert gps.snapshot()['rows'][0]['tracking_state'] == 'active'


def test_queue_and_histories_are_bounded_and_channels_separated():
    clock = [NOW]
    with service(clock=lambda: clock[0], config=GpsConfig(max_points=5)) as gps:
        for i in range(50):
            clock[0] += 1
            gps.ingest(packet(point(clock[0])))
        assert gps.snapshot()['health']['queue_size'] == 1
        assert len(gps.trace('e1', 'device')['samples']) == 5
        before = gps.snapshot()['rows'][0]
        run_demo(gps, {'engineer_id': 'e1', 'case': 'stopped'})
        assert gps.snapshot()['rows'][0] == before
        assert len(gps.trace('e1', 'demo')['samples']) == 3
        run_demo(gps, {'engineer_id': 'e1', 'case': 'clear'})
        assert not gps.trace('e1', 'demo')['samples']


def test_shift_check_uses_facts_and_date_not_gps_presence():
    with service(clock=lambda: NOW + 11 * 60) as gps:
        c = context(); c['engineers']['e1']['status'] = None
        gps.update_context(c)
        assert 'shift_unconfirmed' in codes(gps.snapshot()['rows'][0])
        c['date'] = '2026-08-17'; gps.update_context(c)
        assert 'shift_unconfirmed' not in codes(gps.snapshot()['rows'][0])


def test_real_worker_runs_demo_and_context_change_invalidates_results():
    with service(GpsService, clock=lambda: NOW) as gps:
        for case, expected in [('stopped', 'stopped'), ('moving_away', 'moving_away'), ('near_site', 'near_site'), ('late', 'late')]:
            run_demo(gps, {'engineer_id': 'e1', 'case': case})
            wait_for(lambda: expected in codes(gps.snapshot('demo')['rows'][0]))
        c = context(); c['version'] = 2; c['engineers']['e1']['status'] = None
        # Device inference must stop predicting a route once its confirmed target is gone.
        gps.ingest(packet(point()))
        wait_for(lambda: gps.snapshot()['rows'][0]['analysis'])
        gps.update_context(c)
        wait_for(lambda: gps.snapshot()['rows'][0]['analysis'] is not None)
        assert gps.snapshot()['rows'][0]['analysis']['target'] is None


@pytest.mark.parametrize('model', ['FailingDemoModel', 'HangingDemoModel', 'CrashingDemoModel'])
def test_model_exception_hang_and_process_exit_are_isolated_and_recover(model):
    config = GpsConfig(model_timeout_seconds=.15, failure_threshold=1, cooldown_seconds=.2)
    with service(GpsService, model='field_router.gps.testing_models:' + model, config=config) as gps:
        gps.ingest(packet(point(time.time())))
        wait_for(lambda: gps.snapshot()['health']['failures'] >= 1)
        start = time.monotonic()
        snapshot = gps.snapshot()
        assert time.monotonic() - start < .3
        assert snapshot['rows'][0]['position'] is not None
        if model == 'HangingDemoModel':
            assert snapshot['health']['timeouts'] >= 1
        # A future healthy replacement can recover after the cooldown.
        gps.model_entrypoint = 'field_router.gps.models:BaselineGpsModel'
        wait_for(lambda: gps.snapshot()['health']['processed'] > 0)


def test_invalid_config_fails_only_gps(make_scenario, tmp_path):
    from field_router.scenario_server import create_server
    from field_router.replanning import ScenarioSession
    from test_replanning import demo
    s, p = demo(make_scenario)
    bad = tmp_path / 'invalid-gps.json'; bad.write_text('{"max_points":999999}', encoding='utf-8')
    server = create_server(ScenarioSession(s, p), tmp_path / 'state.json', port=0, gps_config=bad)
    try:
        assert server.gps.service is None
        assert server.gps.request('GET', '/api/gps')[0] == 503
    finally:
        server.server_close()


def test_gps_api_remains_responsive_during_solver_and_does_not_write_plan(make_scenario, tmp_path):
    from field_router.scenario_server import create_server
    from field_router.replanning import ScenarioSession
    from test_replanning import demo
    s, p = demo(make_scenario)
    entered, release = threading.Event(), threading.Event()

    class SlowSession(ScenarioSession):
        def preview(self, payload, **kwargs):
            entered.set(); release.wait(5)
            return {'still_works': True}

    server = create_server(SlowSession(s, p), tmp_path / 'state.json', port=0,
                           gps_model='field_router.gps.testing_models:CrashingDemoModel')
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    url = f'http://127.0.0.1:{server.server_port}'

    def call(path, data=None):
        request = Request(url + path, data=json.dumps(data).encode() if data is not None else None,
                          headers={'Content-Type': 'application/json', 'Origin': url})
        with urlopen(request, timeout=2) as response:
            return json.load(response)

    planner = threading.Thread(target=lambda: call('/api/preview', {}), daemon=True)
    try:
        planner.start(); assert entered.wait(2)
        started = time.monotonic()
        assert call('/api/gps/telemetry', packet(point(time.time())))['accepted']
        assert call('/api/gps')['rows'][0]['position']
        assert time.monotonic() - started < 1
        assert not (tmp_path / 'state.json').exists()
        release.set(); planner.join(2)
        wait_for(lambda: call('/api/gps')['health']['failures'] >= 1)
        assert call('/api/state')['version'] == 1
        assert call('/api/facts', {'version': 1, 'engineer_id': 'e1', 'status': 'available',
                                 'time': 540, 'location_id': 'office'})['version'] == 2
        persisted = json.loads((tmp_path / 'state.json').read_text(encoding='utf-8'))
        assert persisted['version'] == 2 and 'gps' not in persisted
        for data in ({'engineer_id': 'missing', 'tracker_id': 't', 'state': 'active'},
                     packet(point(time.time() + 3600))):
            with pytest.raises(HTTPError) as exc:
                call('/api/gps/telemetry', data)
            assert exc.value.code == 400
    finally:
        release.set(); planner.join(2); server.shutdown(); server.server_close(); thread.join(2)


def test_config_limits_and_malformed_model_output():
    for config in ({'max_points': 100000}, {'model_timeout_seconds': 30}, {'stale_seconds': float('nan')}):
        with pytest.raises(ValueError):
            GpsConfig(**config)
    with pytest.raises(ValueError):
        GpsService._validate_result({'model': 'bad', 'filtered': None, 'alerts': [], 'eta': {'probability': .9}, 'target': None})
