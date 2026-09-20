"""Shipped demo plans must remain valid and start without external services."""
import json
from pathlib import Path
import pytest
from field_router.models import Scenario, Plan
from field_router.geo_models import GeographyBundle
from field_router.road_travel import bundle_travel, current_shapes
from field_router.replanning import ScenarioSession
from field_router.validation import require_valid

DATA = Path(__file__).resolve().parents[1] / 'data/demo-road'

@pytest.mark.parametrize('region', ['Восток', 'Юго-восток', 'Югоцентр'])
def test_shipped_road_demo_is_valid_and_restorable(region):
    payload = json.loads((DATA / f'{region}.json').read_text())
    scenario = Scenario.model_validate(payload['scenario'])
    geo = GeographyBundle.model_validate(payload['geography'])
    plans = [Plan.model_validate(p) for p in payload['plans']]
    assert not geo.unresolved
    assert any(p.quality == 'approximate' for p in geo.locations.values())
    for plan in plans:
        require_valid(scenario, bundle_travel(geo), plan)
    assert current_shapes(geo, scenario, plans).route_shapes == geo.route_shapes
    selected = next(p for p in plans if p.solver == 'ortools')
    session = ScenarioSession(scenario, selected, payload['geography'])
    restored = ScenarioSession.restore(session.dump())
    assert restored.plan == session.plan
    assert restored.geography == session.geography
    assert restored.state()['version'] == 1

@pytest.mark.parametrize('region', ['Восток', 'Юго-восток', 'Югоцентр'])
def test_saved_multimodal_details_match_planning_matrices(region):
    payload=json.loads((DATA / f'{region}.json').read_text())
    geo=payload['geography'];m=geo['matrices'];counts={}
    assert geo['modes']['public']['model']=='walk-metro-walk-v2'
    for i,a in enumerate(m['locations']):
        for j,b in enumerate(m['locations']):
            trip=m['public_journeys'][a][b]
            counts[trip['kind']]=counts.get(trip['kind'],0)+1
            assert trip['minutes']==m['minutes']['public'][i][j]==sum(x['minutes'] for x in trip['legs'])
            assert trip['metres']==m['metres']['public'][i][j]==sum(x['metres'] for x in trip['legs'])
            if trip['kind']=='multimodal':
                assert trip['legs'][0]['mode']==trip['legs'][-1]['mode']=='walk'
                assert any(x['mode']=='metro' for x in trip['legs'])
    assert counts==geo['modes']['public']['journey_counts']
    assert counts['multimodal']>0
