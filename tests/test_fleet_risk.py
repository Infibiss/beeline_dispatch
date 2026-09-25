import pytest

from conftest import engineer, job
from field_router.fleet_risk import fleet_snapshot, score_vehicle
from field_router.models import Scenario, Vehicle, VehicleFailure, VehicleMaintenance


def scenario_with(engineers, vehicles):
    return Scenario(id="fleet", region="test", date="2026-08-17", office_address="Office",
                    requests=[job("a")], engineers=engineers, vehicles=vehicles)


def test_old_scenario_without_fleet_remains_valid(make_scenario):
    scenario = make_scenario([job("a")])
    assert scenario.vehicles == [] and scenario.engineers[0].vehicle_id is None
    snapshot = fleet_snapshot(scenario)
    assert snapshot["covered"] == 0 and snapshot["total"] == 1
    assert snapshot["rows"][0]["status"] == "not_linked"


def test_vehicle_link_is_strict_and_unique():
    car = Vehicle(id="v1", label="Фургон 1")
    with pytest.raises(ValueError, match="unknown vehicle"):
        scenario_with([engineer(vehicle_id="missing")], [car])
    with pytest.raises(ValueError, match="multiple engineers"):
        scenario_with([engineer("e1", vehicle_id="v1"), engineer("e2", vehicle_id="v1")], [car])


def test_fleet_score_is_explainable_score_not_probability():
    vehicle = Vehicle(id="v1", label="Фургон 1", odometer_km=105_000,
        odometer_observed_on="2026-08-16", next_service_km=100_000,
        next_service_date="2026-07-01", open_defects=1,
        maintenance=[VehicleMaintenance(performed_on="2026-01-10", odometer_km=90_000, kind="ТО-2")],
        failures=[VehicleFailure(occurred_on="2026-08-01", kind="генератор", severity="major")])
    result = score_vehicle(vehicle, "2026-08-17")
    assert result["status"] == "ready" and result["measure"] == "score"
    assert result["score"] >= .75 and result["band"] == "critical"
    assert any("просрочено" in reason.lower() for reason in result["reasons"])


def test_no_fleet_evidence_never_invents_breakdown_probability():
    result = score_vehicle(Vehicle(id="v1", label="Без телеметрии"), "2026-08-17")
    assert result["status"] == "insufficient_data"
    assert result["score"] is None
    assert "вероятность" in result["recommendation"].lower()


def test_future_vehicle_history_is_rejected():
    vehicle = Vehicle(id="v1", label="Фургон 1",
        failures=[VehicleFailure(occurred_on="2026-08-18", kind="двигатель")])
    with pytest.raises(ValueError, match="future"):
        scenario_with([engineer(vehicle_id="v1")], [vehicle])
