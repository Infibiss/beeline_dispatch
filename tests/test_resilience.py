"""Stress evaluator must expose lateness rather than silently repair routes."""
import importlib.util
from pathlib import Path
import pytest

from conftest import engineer, job
from field_router.planning import make_plan

spec = importlib.util.spec_from_file_location('check_resilience', Path(__file__).resolve().parents[1] / 'scripts/check_resilience.py')
resilience = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resilience)


class Travel:
    def minutes(self, a, b, mode):
        return 10

    def metres(self, a, b, mode):
        return 1000


def test_fixed_execution_propagates_service_delay_and_shift_breach(make_scenario):
    scenario = make_scenario([job('a', start=550, end=550, duration=30),
                              job('b', start=590, end=590, duration=30)],
                             [engineer(end=620)])
    plan = make_plan(scenario, Travel(), {'e1': ['a', 'b']}, 'test', 'ok')
    result = resilience.execute_fixed(scenario, plan, Travel(), service_factor=1.2)
    assert result['window_breaches'] == 1
    assert result['shift_breaches'] == 1
    assert result['max_start_delay_minutes'] == 6
    assert result['max_shift_overrun_minutes'] == 12
    assert result['pending'] == 0


def test_fixed_execution_waits_and_accepts_window_endpoint(make_scenario):
    scenario = make_scenario([job('a', start=600, end=600)], [engineer()])
    plan = make_plan(scenario, Travel(), {'e1': ['a']}, 'test', 'ok')
    result = resilience.execute_fixed(scenario, plan, Travel(), travel_factor=1.4)
    assert result['window_breaches'] == 0
    assert result['max_start_delay_minutes'] == 0


def test_fixed_execution_uses_later_window_without_hiding_delay(make_scenario):
    from field_router.models import TimeWindow
    scenario = make_scenario([job('a', start=550, end=552)])
    scenario.requests[0].windows.append(TimeWindow(start=570, end=575))
    plan = make_plan(scenario, Travel(), {'e1': ['a']}, 'test', 'ok')
    result = resilience.execute_fixed(scenario, plan, Travel(), travel_factor=1.4)
    assert result['window_breaches'] == 0
    assert result['max_start_delay_minutes'] == 20


def test_independent_inventory_invariant_rejects_created_stock(make_scenario):
    import pytest
    from field_router.replanning import ScenarioSession
    from test_replanning import demo
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    resilience.assert_invariants(session)
    session.inventory[0]['available']['router'] += 1
    with pytest.raises(AssertionError, match='inventory not conserved'):
        resilience.assert_invariants(session)


@pytest.mark.parametrize('solver', ['hybrid', 'alns'])
def test_real_demo_dynamic_day_has_every_event_and_restores(solver):
    import json
    from field_router.models import Plan, Scenario
    payload = json.loads((resilience.ROOT / 'data/demo-road/Восток.json').read_text())
    result = resilience.dynamic_day(Scenario.model_validate(payload['scenario']),
                                  next(Plan.model_validate(p) for p in payload['plans'] if p['solver'] == 'ortools'),
                                  payload['geography'], .03, solver=solver)
    assert result['restore_equal']
    assert result['delay_fact']['extra_service_minutes'] == 20
    stages = result['stages']
    assert [s['event']['type'] for s in stages] == ['emergency', 'emergency', 'cancel', 'unavailable',
                                                    'replan', 'replan', 'replan', 'replan']
    assert all(s['all_three_candidates_valid'] and s['invariants_valid'] for s in stages)
    assert stages[-1]['assigned_remaining'] == 0


def test_unavailable_road_engineer_releases_trip_without_gps():
    import json
    from field_router.models import Plan,Scenario
    from field_router.replanning import ScenarioSession
    payload=json.loads((resilience.ROOT/'data/demo-road/Восток.json').read_text())
    for freeze_first in (False,True):
        session=ScenarioSession(Scenario.model_validate(payload['scenario']),
            next(Plan.model_validate(p) for p in payload['plans'] if p['solver']=='ortools'),payload['geography'])
        e=next(e for e in session.scenario.engineers if e.shift.start==540 and any(r.engineer_id==e.id and r.visits[0].arrival>542 for r in session.plan.routes))
        trip=next(r.visits[0].request_id for r in session.plan.routes if r.engineer_id==e.id)
        if freeze_first:
            b=session.preview({'version':session.version,'event':{'id':'freeze','type':'replan','time':541},'preserve_in_progress':True},time_limit=.03)
            session.approve({'version':session.version,'batch_id':b['id'],'candidate_id':'stable'})
        b=session.preview({'version':session.version,'event':{'id':'failed','type':'unavailable','time':542,'engineer_id':e.id},'preserve_in_progress':True},time_limit=.03)
        session.approve({'version':session.version,'batch_id':b['id'],'candidate_id':'stable'})
        assert e.id not in {x.id for x in session.scenario.engineers}
        assert trip in {r.id for r in session.scenario.requests}
        assert trip not in {v['request_id'] for r in session.frozen for v in r['visits']}
        resilience.assert_invariants(session)
