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
