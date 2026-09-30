"""Contracts for HTTPS termination by the hosting platform."""
import http.client
import json
import threading

import pytest

from field_router.auth import AccountStore
from field_router.replanning import ScenarioSession
from field_router.scenario_server import create_server
from test_replanning import demo


@pytest.fixture
def public_server(make_scenario, tmp_path, request):
    scenario, plan = demo(make_scenario)
    accounts = AccountStore(tmp_path / 'accounts.sqlite3')
    accounts.bootstrap('test-cloud-password')
    server = create_server(ScenarioSession(scenario, plan), tmp_path / 'state.json',
                           port=0, disable_gps=True, accounts=accounts,
                           public_origin='https://field-demo.onrender.com',
                           public_demo_login=getattr(request, 'param', False))
    server.public_demo_login = getattr(request, 'param', False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def request(server, path, payload=None, headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
    values = {'Host': 'field-demo.onrender.com', 'Origin': 'https://field-demo.onrender.com',
              'Content-Type': 'application/json', **(headers or {})}
    connection.request('GET' if payload is None else 'POST', path,
                       None if payload is None else json.dumps(payload), values)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_public_login_uses_secure_cookie_and_keeps_csrf(public_server):
    status, headers, raw = request(public_server, '/api/auth/login',
                                  {'username': 'admin', 'password': 'test-cloud-password'})
    assert status == 200
    assert 'Secure' in headers['Set-Cookie']
    assert 'HttpOnly' in headers['Set-Cookie']
    cookie = headers['Set-Cookie'].split(';')[0]
    assert request(public_server, '/api/state', headers={'Cookie': cookie})[0] == 200
    assert request(public_server, '/api/auth/logout', {}, {'Cookie': cookie})[0] == 403
    assert request(public_server, '/api/auth/logout', {},
                   {'Cookie': cookie, 'X-CSRF-Token': json.loads(raw)['csrf']})[0] == 200


@pytest.mark.parametrize('headers', [
    {'Host': 'evil.example'},
    {'Origin': 'https://evil.example'},
    {'Origin': 'http://field-demo.onrender.com'},
    {'Host': 'evil.example', 'X-Forwarded-Host': 'field-demo.onrender.com',
     'X-Forwarded-Proto': 'https'},
])
def test_public_server_rejects_foreign_requests(public_server, headers):
    assert request(public_server, '/login', headers=headers)[0] == 403


def test_anonymous_health_has_no_application_data(public_server):
    status, _, body = request(public_server, '/healthz')
    assert status == 200
    assert json.loads(body) == {'status': 'ok'}
    assert request(public_server, '/api/state')[0] == 401


@pytest.mark.parametrize('origin', ['http://demo.example', 'https://demo.example/path',
                                   'https://user:pass@demo.example', 'https://demo.example?x=1',
                                   'https://demo.example#x', 'https://demo.example/',
                                   'https://demo.example:bad', 'https://'])
def test_invalid_public_origin_fails_before_server_start(make_scenario, tmp_path, origin):
    scenario, plan = demo(make_scenario)
    with pytest.raises(ValueError, match='origin'):
        create_server(ScenarioSession(scenario, plan), tmp_path / 'state.json', port=0,
                      public_origin=origin)


def test_cloud_start_requires_credentials_and_https(tmp_path, monkeypatch):
    from field_router.cloud_demo import run
    for key in ('FIELD_ROUTER_ADMIN_PASSWORD', 'RENDER_EXTERNAL_URL', 'FIELD_ROUTER_PUBLIC_ORIGIN'):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match='FIELD_ROUTER_ADMIN_PASSWORD'):
        run(root=tmp_path)
    monkeypatch.setenv('FIELD_ROUTER_ADMIN_PASSWORD', 'test-cloud-password')
    with pytest.raises(ValueError, match='origin'):
        run(root=tmp_path)


def test_cloud_restart_uses_fresh_state_and_preserves_inputs(tmp_path, monkeypatch, capsys):
    from field_router import cloud_demo, scenario_server
    source = tmp_path / 'data' / 'demo-road'
    source.mkdir(parents=True)
    for region in ('Восток', 'Юго-восток', 'Югоцентр'):
        (source / (region + '.json')).write_text('fixture')
    calls = []
    def fake_main(args):
        from pathlib import Path
        state = Path(args[args.index('--state') + 1])
        assert not state.exists()
        assert args[args.index('--time-limit') + 1] == '30'
        assert args[args.index('--morning-time-limit') + 1] == '120'
        assert args[args.index('--regions-dir') + 1] == str(source)
        assert '--disable-gps' not in args
        assert args[args.index('--public-origin') + 1] == 'https://demo.onrender.com'
        state.write_text('changed day')
        calls.append(state)
    monkeypatch.setattr(scenario_server, 'main', fake_main)
    env = {'FIELD_ROUTER_ADMIN_PASSWORD': 'test-cloud-password',
           'RENDER_EXTERNAL_URL': 'https://demo.onrender.com', 'PORT': '10000'}
    monkeypatch.delenv('FIELD_ROUTER_PUBLIC_ORIGIN', raising=False)
    for key, value in env.items():monkeypatch.setenv(key, value)
    cloud_demo.run(root=tmp_path)
    cloud_demo.run(root=tmp_path)
    assert len(calls) == 2 and calls[0] != calls[1]
    assert all(not path.exists() for path in calls)
    assert all(path.read_text() == 'fixture' for path in source.glob('*.json'))
    assert env['FIELD_ROUTER_ADMIN_PASSWORD'] not in capsys.readouterr().out


@pytest.mark.parametrize('public_server', [False, True], indirect=True)
def test_demo_credentials_only_on_explicit_public_demo(public_server):
    status, _, body = request(public_server, '/login')
    html = body.decode()
    assert status == 200
    enabled = public_server.public_demo_login
    assert ('1f8ee261de8e35b09d38269b2bf34474' in html) == enabled
    assert ('Демо-доступ' in html) == enabled
    assert 'test-cloud-password' not in html
    if enabled:
        assert html.index('demo-access') > html.index('</form>')


@pytest.mark.parametrize('password, exposed', [
    ('1f8ee261de8e35b09d38269b2bf34474', True),
    ('some-other-private-password', False),
])
def test_cloud_only_advertises_known_public_password(tmp_path, monkeypatch, password, exposed):
    from field_router import cloud_demo, scenario_server
    source = tmp_path / 'data' / 'demo-road'
    source.mkdir(parents=True)
    for region in ('Восток', 'Юго-восток', 'Югоцентр'):
        (source / (region + '.json')).write_text('fixture')
    calls = []
    monkeypatch.setattr(scenario_server, 'main', calls.append)
    monkeypatch.setenv('FIELD_ROUTER_ADMIN_PASSWORD', password)
    monkeypatch.setenv('FIELD_ROUTER_PUBLIC_ORIGIN', 'https://demo.onrender.com')
    cloud_demo.run(root=tmp_path)
    assert ('--public-demo-login' in calls[0]) == exposed
