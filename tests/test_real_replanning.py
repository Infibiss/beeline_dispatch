"""Road replanning uses cached matrices and explicit locations, never a network."""
import json

import pytest

from conftest import engineer, job
from field_router.geo_models import GeoLocation, GeographyBundle
from field_router.geography import attach_geography, scenario_signature
from field_router.models import Skill
from field_router.planning import make_plan
from field_router.replanning import ScenarioSession
from field_router.road_travel import build_matrices, bundle_travel


@pytest.fixture
def real_session(make_scenario):
    scenario = make_scenario(
        [job('a', start=550, end=700), job('b', start=610, end=800)],
        [engineer('e1', skills=[Skill.LOCAL, Skill.EMERGENCY])],
    )
    locations = {
        key: GeoLocation(lat=55.7 + index / 100, lon=37.7 + index / 100,
                         quality='manual', source='Local test fixture')
        for index, key in enumerate(('office', 'a', 'b'))
    }

    class CachedRoadClient:
        def get(self, url):
            # Deliberately asymmetric road times; no network implementation.
            return {'code': 'Ok',
                    'durations': [[0, 600, 1200], [900, 0, 420], [1500, 660, 0]],
                    'distances': [[0, 4000, 8000], [5000, 0, 2500], [9000, 3500, 0]]}

    geography = build_matrices(
        GeographyBundle(scenario_signature=scenario_signature(scenario), locations=locations),
        CachedRoadClient(), allow_public_estimate=True,
    )
    scenario = attach_geography(scenario, geography)
    plan = make_plan(scenario, bundle_travel(geography), {'e1': ['a', 'b']}, 'cached-road', 'valid')
    return ScenarioSession(scenario, plan, geography=geography.model_dump(mode='json'))


def preview(session, event, **options):
    return session.preview({'version': session.version, 'event': event, **options}, time_limit=.03)


def approve(session, batch):
    return session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})


def test_cached_geography_initialization_uses_road_times(real_session):
    session = real_session
    assert session.scenario.points == {}
    assert session.scenario.metadata['geography'] == 'wgs84'
    assert session.plan.routes[0].visits[0].travel_minutes == 10
    assert session._travel(session.scenario).minutes('a', 'office', 'car') == 15
    assert session.workbench()['geography']['locations']['a']['lat'] == 55.71


def test_road_aliases_survive_emergency_approval_and_restore(real_session):
    session = real_session
    batch = preview(session, {'id': 'urgent', 'type': 'emergency', 'time': 540,
                              'anchor_id': 'b', 'duration_minutes': 20})
    residual = session.batches[batch['id']]['scenario']
    assert residual.points == {}
    start = residual.engineers[0].start_location
    key = 'emergency:urgent'
    assert session.aliases[start] == 'office'
    assert session.aliases[key] == 'b'
    assert session.geo_locations[key] == session.geo_locations['b']
    assert session._travel(residual).minutes(start, key, 'car') == 20
    assert session._travel(residual).minutes(key, 'a', 'car') == 11
    approve(session, batch)
    restored = ScenarioSession.restore(json.loads(json.dumps(session.dump())))
    assert restored.workbench(1) == session.workbench(1)
    assert restored.workbench() == session.workbench()
    assert restored._travel(restored.scenario).minutes(start, key, 'car') == 20
    again = preview(restored, {'id': 'again', 'type': 'replan', 'time': 540})
    approve(restored, again)
    assert restored.scenario.points == {}
    assert restored.version == 3


def test_completed_road_visit_becomes_aliased_start(real_session):
    session = real_session
    batch = preview(session, {'id': 'after-a', 'type': 'replan', 'time': 580})
    scenario = session.batches[batch['id']]['scenario']
    assert [request.id for request in scenario.requests] == ['b']
    assert session.aliases[scenario.engineers[0].start_location] == 'a'
    assert session._travel(scenario).minutes(scenario.engineers[0].start_location, 'b', 'car') == 7
    approve(session, batch)
    restored = ScenarioSession.restore(json.loads(json.dumps(session.dump())))
    assert restored.frozen == session.frozen


def test_mid_trip_road_redirect_requires_explicit_location(real_session):
    session = real_session
    with pytest.raises(ValueError, match='ручной статус'):
        preview(session, {'id': 'redirect', 'type': 'emergency', 'time': 545,
                          'anchor_id': 'b', 'duration_minutes': 20}, allow_redirect=True)
    assert session.version == 1
    assert not any(key.startswith('position:') for key in session.geo_locations)
    session.record_fact({'version': 1, 'time': 545, 'engineer_id': 'e1',
                         'status': 'available', 'location_id': 'office'})
    batch = preview(session, {'id': 'observed-redirect', 'type': 'emergency', 'time': 545,
                              'anchor_id': 'b', 'duration_minutes': 20}, allow_redirect=True)
    scenario = session.batches[batch['id']]['scenario']
    assert session.aliases[scenario.engineers[0].start_location] == 'office'


def test_mismatched_geography_signature_is_rejected(real_session):
    session = real_session
    wrong = session.geography.model_copy(update={'scenario_signature': 'different-addresses'})
    with pytest.raises(ValueError, match='География'):
        ScenarioSession(session.scenario, session.plan, geography=wrong.model_dump(mode='json'))


def test_fresh_travel_fact_allows_road_redirect(real_session):
    session = real_session
    session.record_fact({'version': 1, 'time': 545, 'engineer_id': 'e1',
                         'status': 'traveling', 'request_id': 'a',
                         'location_id': 'office', 'remaining_minutes': 5})
    batch = preview(session, {'id': 'fresh', 'type': 'emergency', 'time': 545,
                             'anchor_id': 'b', 'duration_minutes': 20}, allow_redirect=True)
    scenario = session.batches[batch['id']]['scenario']
    assert session.aliases[scenario.engineers[0].start_location] == 'office'
    with pytest.raises(ValueError, match='ручной статус'):
        preview(session, {'id': 'stale', 'type': 'emergency', 'time': 546,
                          'anchor_id': 'b', 'duration_minutes': 20}, allow_redirect=True)
