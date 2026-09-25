from copy import deepcopy

from field_router.advisory import build_advisories
from field_router.replanning import ScenarioSession
from test_replanning import demo


def test_fact_advisory_is_readonly_and_requires_explicit_preview(make_scenario):
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    session.record_fact({"version": 1, "engineer_id": "e1", "status": "available", "time": 540,
                         "location_id": "office"})
    before = deepcopy(session.dump())
    rows = build_advisories(session)
    assert len(rows) == 1
    assert rows[0]["source"] == "fact"
    assert rows[0]["action"] == "preview_replan"
    assert rows[0]["requires_dispatcher_approval"] is True
    assert session.dump() == before


def test_gps_advisory_accepts_only_fresh_device_signal_for_current_plan(make_scenario):
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    base = {"channel": "device", "plan_version": 1, "plan_date": scenario.date,
            "config": {"stale_seconds": 60}, "rows": [{
                "engineer_id": "e1", "name": "Инженер", "tracking_state": "active",
                "date_matches": True, "point_age_seconds": 5,
                "analysis": {"target": {"id": "a"}},
                "alerts": [{"code": "late", "title": "Риск окна", "detail": "ETA вышла за окно"},
                           {"code": "near_site", "title": "Рядом", "detail": "У адреса"}],
            }]}
    rows = build_advisories(session, base)
    assert len(rows) == 1 and rows[0]["source"] == "gps"
    assert rows[0]["status"] == "needs_confirmation"
    assert rows[0]["action"] == "facts"
    assert rows[0]["request_id"] == "a"
    assert build_advisories(session, {**base, "channel": "demo"}) == []
    assert build_advisories(session, {**base, "plan_version": 2}) == []


def test_gps_without_fresh_coordinate_never_creates_replan_advisory(make_scenario):
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    snapshot = {"channel": "device", "plan_version": 1, "plan_date": scenario.date,
                "config": {"stale_seconds": 60}, "rows": [{
                    "engineer_id": "e1", "tracking_state": "active", "date_matches": True,
                    "point_age_seconds": 61, "analysis": {"target": {"id": "a"}},
                    "alerts": [{"code": "late", "title": "late", "detail": "late"}],
                }]}
    assert build_advisories(session, snapshot) == []
