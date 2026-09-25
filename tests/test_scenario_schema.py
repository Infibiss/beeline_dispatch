import json
from pathlib import Path

from field_router.models import Scenario


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs" / "contracts" / "scenario.schema.json"
LEGACY_SCENARIO_PATH = ROOT / "data" / "demo-road" / "Восток.json"


def test_checked_in_schema_matches_pydantic_scenario_contract():
    checked_in = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert checked_in == Scenario.model_json_schema()
    assert checked_in["properties"]["vehicles"]["items"] == {"$ref": "#/$defs/Vehicle"}
    assert checked_in["$defs"]["Engineer"]["properties"]["vehicle_id"]["default"] is None
    assert "vehicles" not in checked_in["required"]
    assert "vehicle_id" not in checked_in["$defs"]["Engineer"]["required"]


def test_legacy_scenario_without_fleet_remains_valid():
    payload = json.loads(LEGACY_SCENARIO_PATH.read_text(encoding="utf-8"))["scenario"]

    assert "vehicles" not in payload
    assert all("vehicle_id" not in engineer for engineer in payload["engineers"])

    scenario = Scenario.model_validate(payload)

    assert scenario.vehicles == []
    assert all(engineer.vehicle_id is None for engineer in scenario.engineers)
