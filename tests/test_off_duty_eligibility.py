"""A planned day off is a hard routing constraint, including emergencies."""
import pytest

from conftest import engineer, job
from test_planning import travel
from test_replanning import demo
from field_router.crew import CrewService
from field_router.models import Equipment, Skill
from field_router.planning import make_plan
from field_router.replanning import ScenarioSession
from field_router.solvers.baseline import BaselineSolver
from field_router.solvers.selection import get_solver
from field_router.travel import synthetic_travel
from field_router.validation import require_valid, validate_plan


def emergency_scenario(make_scenario, *, only_off_duty=False):
    workers = [engineer('off', skills=[Skill.EMERGENCY], off_duty=True)]
    if not only_off_duty:
        workers.append(engineer('working', skills=[Skill.EMERGENCY]))
    return make_scenario([job('emergency', skill=Skill.EMERGENCY, priority='urgent')], workers)


@pytest.mark.parametrize('solver', ['ortools', 'alns', 'hybrid'])
@pytest.mark.parametrize('profile', [None, 'stable', 'cost', 'sla'])
def test_all_solver_profiles_keep_off_duty_engineer_at_home(make_scenario, solver, profile):
    scenario = emergency_scenario(make_scenario)
    plan = get_solver(solver).solve(scenario, travel(scenario), time_limit_seconds=.12, profile=profile)
    assert [(route.engineer_id, [visit.request_id for visit in route.visits]) for route in plan.routes] == [
        ('working', ['emergency'])]
    assert validate_plan(scenario, travel(scenario), plan) == []


def test_emergency_stays_unassigned_when_only_engineer_is_off_duty(make_scenario):
    scenario = emergency_scenario(make_scenario, only_off_duty=True)
    for solver in (BaselineSolver(), get_solver('ortools'), get_solver('alns')):
        plan = solver.solve(scenario, travel(scenario), time_limit_seconds=.12)
        assert plan.routes == []
        assert [(item.request_id, item.code) for item in plan.unassigned] == [
            ('emergency', 'OFF_DUTY')]


def test_independent_validator_rejects_forged_off_duty_assignment(make_scenario):
    scenario = make_scenario([job('emergency', skill=Skill.EMERGENCY, priority='urgent')],
                             [engineer('off', skills=[Skill.EMERGENCY])])
    matrix = travel(scenario)
    valid = make_plan(scenario, matrix, {'off': ['emergency']}, 'manual', 'valid')
    off_duty = scenario.model_copy(update={'engineers': [scenario.engineers[0].model_copy(
        update={'off_duty': True})]})
    assert 'off-duty engineer: off' in validate_plan(off_duty, matrix, valid)
    with pytest.raises(ValueError, match='off-duty engineer'):
        require_valid(off_duty, matrix, valid)
    with pytest.raises(ValueError, match='infeasible'):
        make_plan(off_duty, matrix, {'off': ['emergency']}, 'manual', 'invalid')


def test_morning_crew_preview_carries_off_duty_flag_and_reassigns_emergency(make_scenario):
    scenario, _ = demo(make_scenario)
    scenario = scenario.model_copy(update={
        'requests': [request.model_copy(update={'skill': Skill.EMERGENCY, 'priority': 'urgent'})
                     if request.id == 'a' else request for request in scenario.requests],
        'engineers': [worker.model_copy(update={'skills': [Skill.LOCAL, Skill.EMERGENCY]})
                      for worker in scenario.engineers]})
    plan = make_plan(scenario, synthetic_travel(scenario), {'e1': ['a', 'b']}, 'initial', 'valid')
    session = ScenarioSession(scenario, plan)
    service = CrewService()
    rows = service.settings(session)['engineers']
    assert all(row['off_duty'] is False for row in rows)
    rows[0]['off_duty'] = True
    rows[1].update(stock_mode='fixed', router=1, tv_box=0)
    preview = service.preview(session, {'version': 1, 'engineers': rows}, time_limit=.15)
    assert 'off_duty' in preview['changes'][0]['fields']
    selected = service.selected(session, {'version': 1, 'id': preview['id']})
    assert selected.initial_engineers['e1'].off_duty is True
    assert all(route.engineer_id != 'e1' for route in selected.plan.routes)
    assert any(visit.request_id == 'a' for route in selected.plan.routes for visit in route.visits)
    assert service.settings(selected)['engineers'][0]['off_duty'] is True
    assert ScenarioSession.restore(selected.dump()).initial_engineers['e1'].off_duty is True


def test_existing_morning_rows_without_flag_remain_available(make_scenario):
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    service = CrewService()
    legacy_rows = service.settings(session)['engineers']
    for row in legacy_rows:
        row.pop('off_duty')
    session.scenario = session.scenario.model_copy(update={
        'metadata': {**session.scenario.metadata, 'crew_inputs': legacy_rows}})
    rows = service.settings(session)['engineers']
    assert all(row['off_duty'] is False for row in rows)
    preview = service.preview(session, {'version': 1, 'engineers': rows}, time_limit=.05)
    assert preview['changes'] == []
