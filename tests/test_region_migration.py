import hashlib
import json
import threading

import pytest

from field_router.auth import AccountStore
from field_router.regions import RegionRegistry, discover_regions, load_session
from field_router.replanning import ScenarioSession
from field_router.scenario_server import create_server
from test_auth import Client, PASSWORD
from test_replanning import demo


@pytest.fixture
def legacy_regions(make_scenario, tmp_path):
    scenario, plan = demo(make_scenario)
    comparisons = tmp_path / 'comparisons'
    comparisons.mkdir()
    sessions = {}
    for label in ('Восток', 'Юго-восток'):
        regional = scenario.model_copy(update={
            'region': label,
            'engineers': [e.model_copy(update={'region': label}) for e in scenario.engineers],
            'requests': [r.model_copy(update={'region': label}) for r in scenario.requests],
        })
        (comparisons / (label + '.json')).write_text(json.dumps({
            'scenario': regional.model_dump(mode='json'),
            'plans': [plan.model_dump(mode='json')],
        }), encoding='utf-8')
        session = ScenarioSession(regional, plan)
        session.set_mode({'version': 1, 'mode': 'manual'})
        sessions[label] = session
    primary = comparisons / 'Восток.json'
    state = tmp_path / 'workday.json'
    state.write_text(json.dumps(sessions['Восток'].dump()), encoding='utf-8')
    old_key = hashlib.sha256('Юго-восток'.encode()).hexdigest()[:12]
    old_path = tmp_path / 'workday-regions' / (old_key + '.json')
    old_path.parent.mkdir()
    south = sessions['Юго-восток']
    south.record_fact({'version': 2, 'engineer_id': 'e1', 'status': 'available',
                       'time': 540, 'location_id': 'office', 'comment': 'Saved before upgrade'})
    old_path.write_text(json.dumps(south.dump()), encoding='utf-8')
    return primary, state, old_key, old_path


@pytest.mark.parametrize('explicit', [False, True])
def test_legacy_urls_share_canonical_context_and_preserve_saved_work(legacy_regions, explicit):
    primary, state, old_key, old_path = legacy_regions
    sources = discover_regions(primary, state, primary.parent if explicit else None)
    assert sources['south-east']['state'] == old_path
    registry = RegionRegistry(load_session(primary, state), state,
                              lambda session, path, primary: {'holder': {'session': session}, 'path': path},
                              sources)
    current = registry.resolve('/regions/current/api/state')
    assert current[0] == 'east'
    assert current[-1] is registry.resolve('/r/east/api/state')[-1]
    legacy = registry.resolve('/regions/' + old_key + '/api/state')
    assert legacy[0] == 'south-east'
    assert legacy[1] == '/regions/' + old_key
    assert legacy[-1] is registry.resolve('/r/south-east/api/state')[-1]
    assert len(registry.contexts) == 2
    assert {region['id'] for region in registry.manifest('east')['regions']} == {'east', 'south-east'}
    restored = legacy[-1]['holder']['session'].state()
    assert restored['version'] == 3 and restored['execution_mode'] == 'manual'
    assert restored['fact_journal'][0]['comment'] == 'Saved before upgrade'


def test_current_url_without_region_catalog_uses_default_context(make_scenario, tmp_path):
    scenario, plan = demo(make_scenario)
    registry = RegionRegistry(ScenarioSession(scenario, plan), tmp_path / 'state.json',
                              lambda session, path, primary: {'holder': {'session': session}})
    assert registry.resolve('/regions/current/api/state')[-1] is registry.resolve('/api/state')[-1]
    assert registry.resolve('/regions/current/api/state')[0] == 'default'


def test_competing_legacy_and_canonical_saves_require_explicit_resolution(legacy_regions):
    primary, state, _, old_path = legacy_regions
    canonical = state.with_name('workday-south-east.json')
    data = json.loads(old_path.read_text(encoding='utf-8'))
    data['version'] += 1
    canonical.write_text(json.dumps(data), encoding='utf-8')
    before = old_path.read_bytes(), canonical.read_bytes()
    with pytest.raises(ValueError) as error:
        discover_regions(primary, state, primary.parent)
    assert str(old_path) in str(error.value) and str(canonical) in str(error.value)
    assert before == (old_path.read_bytes(), canonical.read_bytes())


def test_existing_canonical_save_keeps_its_path_and_work(legacy_regions):
    primary, state, _, old_path = legacy_regions
    canonical = state.with_name('workday-south-east.json')
    old_path.replace(canonical)
    sources = discover_regions(primary, state, primary.parent)
    assert sources['south-east']['state'] == canonical
    restored = load_session(sources['south-east']['comparison'], canonical)
    assert restored.version == 3 and restored.mode() == 'manual'
    assert restored.fact_log[0]['comment'] == 'Saved before upgrade'


@pytest.mark.parametrize('legacy_accounts', [False, True])
def test_authenticated_legacy_urls_remain_isolated_and_save_to_legacy_file(
        legacy_regions, legacy_accounts):
    primary, state, old_key, old_path = legacy_regions
    sources = discover_regions(primary, state, primary.parent)
    accounts = AccountStore(state.with_suffix('.sqlite3'))
    accounts.bootstrap(PASSWORD)
    accounts.create_engineer('east-worker', PASSWORD, 'current' if legacy_accounts else 'east', 'e1')
    accounts.create_engineer('south-worker', PASSWORD, old_key if legacy_accounts else 'south-east', 'e1')
    server = create_server(load_session(primary, state), state, port=0, disable_gps=True,
                           accounts=accounts, region_sources=sources)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        admin, east, south = Client(server), Client(server), Client(server)
        assert admin.login('admin')[0] == 200
        status, identity, _ = east.login('east-worker')
        assert status == 200 and identity['redirect'] == '/r/east/engineer'
        assert identity['region'] == 'east'
        status, identity, _ = south.login('south-worker')
        assert status == 200 and identity['redirect'] == '/r/south-east/engineer'
        assert identity['region'] == 'south-east'
        if legacy_accounts:
            assert next(account for account in accounts.list() if account['username'] == 'south-worker')['region'] == old_key
        listed = admin.request('/api/accounts')[1]['accounts']
        assert next(account for account in listed if account['username'] == 'south-worker')['region'] == 'south-east'
        east_urls = ['/regions/current', '/r/east']
        south_urls = ['/regions/' + old_key, '/r/south-east']
        for client, allowed, forbidden in ((east, east_urls, south_urls), (south, south_urls, east_urls)):
            for base in allowed:
                assert client.request(base + '/api/engineer?engineer_id=e1')[0] == 200
            for base in forbidden:
                assert client.request(base + '/api/engineer?engineer_id=e1')[0] == 403
                assert client.request(base + '/api/gps/telemetry', {
                    'engineer_id': 'e1', 'tracker_id': 'cross-region', 'state': 'active',
                })[0] == 403
        for base in south_urls:
            status, restored, _ = admin.request(base + '/api/state')
            assert status == 200 and restored['version'] == 3
            assert restored['execution_mode'] == 'manual'
            assert restored['fact_journal'][0]['comment'] == 'Saved before upgrade'
        assert south.request(south_urls[0] + '/api/engineer/facts', {
            'version': 3, 'engineer_id': 'e1', 'status': 'available', 'time': 541,
            'location_id': 'office', 'comment': 'Saved after upgrade',
        })[0] == 200
        assert admin.request('/r/south-east/api/state')[1]['version'] == 4
        saved = ScenarioSession.restore(json.loads(old_path.read_text(encoding='utf-8')))
        assert saved.version == 4 and saved.fact_log[-1]['comment'] == 'Saved after upgrade'
        assert admin.request('/regions/current/api/state')[1]['version'] == 2
        assert not state.with_name('workday-south-east.json').exists()
        # An old browser may still submit an alias, but new accounts must store
        # the canonical region and must not duplicate an existing alias account.
        create = {'action': 'create', 'username': 'duplicate-worker', 'password': PASSWORD,
                  'region': 'south-east', 'engineer_id': 'e1'}
        assert admin.request('/api/accounts', create)[0] == 409
        assert admin.request('/api/accounts', {
            **create, 'username': 'new-worker', 'region': old_key, 'engineer_id': 'e2',
        })[0] == 200
        assert next(account for account in accounts.list() if account['username'] == 'new-worker')['region'] == 'south-east'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
