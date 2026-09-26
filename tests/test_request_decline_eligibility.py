"""A declined visit remains a request but cannot return to that engineer."""
import pytest

from conftest import engineer, job
from test_planning import travel
from test_replanning import demo
from field_router.models import Request, Scenario, Skill
from field_router.planning import make_plan
from field_router.replanning import ScenarioSession
from field_router.solvers.baseline import BaselineSolver
from field_router.solvers.selection import get_solver
from field_router.travel import synthetic_travel
from field_router.validation import require_valid, validate_plan


def declined_scenario(make_scenario, *, spare=True):
    engineers = [engineer('declined', skills=[Skill.EMERGENCY])]
    if spare:
        engineers.append(engineer('available', skills=[Skill.EMERGENCY]))
    return make_scenario([job('emergency', skill=Skill.EMERGENCY, priority='urgent',
                              excluded_engineer_ids=['declined'])], engineers)


def test_request_exclusion_roundtrip_and_old_payload_default(make_scenario):
    scenario = declined_scenario(make_scenario)
    dumped = scenario.model_dump(mode='json')
    assert dumped['requests'][0]['excluded_engineer_ids'] == ['declined']
    assert Scenario.model_validate(dumped).requests[0].excluded_engineer_ids == ['declined']
    legacy = dumped['requests'][0].copy()
    del legacy['excluded_engineer_ids']
    assert Request.model_validate(legacy).excluded_engineer_ids == []
    for invalid in (['declined', 'declined'], ['']):
        with pytest.raises(ValueError):
            Request.model_validate({**legacy, 'excluded_engineer_ids': invalid})


def test_declined_request_survives_session_save_restore(make_scenario):
    scenario, _ = demo(make_scenario)
    scenario = scenario.model_copy(update={'requests': [
        request.model_copy(update={'excluded_engineer_ids': ['e1']}) if request.id == 'b'
        else request for request in scenario.requests]})
    plan = make_plan(scenario, synthetic_travel(scenario), {'e1': ['a'], 'e2': ['b']},
                     'manual', 'valid')
    restored = ScenarioSession.restore(ScenarioSession(scenario, plan).dump())
    assert restored.catalog['b'].excluded_engineer_ids == ['e1']
    assert next(request for request in restored.scenario.requests if request.id == 'b').excluded_engineer_ids == ['e1']


@pytest.mark.parametrize('solver', ['ortools', 'alns', 'hybrid'])
@pytest.mark.parametrize('profile', [None, 'stable', 'cost', 'sla'])
def test_all_solver_profiles_respect_request_specific_decline(make_scenario, solver, profile):
    scenario = declined_scenario(make_scenario)
    matrix = travel(scenario)
    plan = get_solver(solver).solve(scenario, matrix, time_limit_seconds=.12, profile=profile)
    assert [(route.engineer_id, [visit.request_id for visit in route.visits]) for route in plan.routes] == [
        ('available', ['emergency'])]
    assert validate_plan(scenario, matrix, plan) == []


def test_declined_emergency_remains_in_replanning_pool_when_no_spare(make_scenario):
    scenario = declined_scenario(make_scenario, spare=False)
    matrix = travel(scenario)
    for solver in (BaselineSolver(), get_solver('ortools'), get_solver('alns')):
        plan = solver.solve(scenario, matrix, time_limit_seconds=.12)
        assert plan.routes == []
        assert [(item.request_id, item.code) for item in plan.unassigned] == [
            ('emergency', 'ENGINEER_DECLINED')]
        assert [request.id for request in scenario.requests] == ['emergency']


def test_independent_validator_rejects_forged_declined_assignment(make_scenario):
    allowed = make_scenario([job('emergency', skill=Skill.EMERGENCY, priority='urgent')],
                            [engineer('declined', skills=[Skill.EMERGENCY])])
    matrix = travel(allowed)
    plan = make_plan(allowed, matrix, {'declined': ['emergency']}, 'manual', 'valid')
    blocked = allowed.model_copy(update={'requests': [allowed.requests[0].model_copy(
        update={'excluded_engineer_ids': ['declined']})]})
    assert any('engineer declined this request' in error for error in validate_plan(blocked, matrix, plan))
    with pytest.raises(ValueError, match='engineer declined this request'):
        require_valid(blocked, matrix, plan)
    with pytest.raises(ValueError, match='infeasible'):
        make_plan(blocked, matrix, {'declined': ['emergency']}, 'manual', 'invalid')
