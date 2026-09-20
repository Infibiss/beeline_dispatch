import pytest

from conftest import engineer, job
from test_planning import travel
from field_router.models import Equipment, Skill, TimeWindow, Transport
from field_router.solvers.baseline import BaselineSolver
from field_router.solvers.ortools_solver import OrToolsSolver
from field_router.validation import validate_plan


def test_optimization_improves_known_counterexample(make_scenario):
    s = make_scenario([job("late", start=800, end=820),job("early", start=600,end=620)],
                      [engineer("first"),engineer("second")])
    t = travel(s)
    baseline = BaselineSolver().solve(s,t)
    optimized = OrToolsSolver().solve(s,t,time_limit_seconds=0.15)
    assert baseline.metrics.engineers_used == 2
    assert optimized.metrics.assigned == 2
    assert optimized.metrics.engineers_used == 1
    assert [v.request_id for v in optimized.routes[0].visits] == ["early","late"]
    assert optimized.solver == "ortools"
    assert not validate_plan(s,t,optimized)


def test_ortools_last_service_must_finish_in_shift_and_no_return(make_scenario):
    s = make_scenario([job("a",start=600,end=600,duration=30)], [engineer(end=630)])
    p = OrToolsSolver().solve(s,travel(s),time_limit_seconds=.1)
    assert p.metrics.assigned == 1
    assert p.metrics.distance_m == 1000
    s = make_scenario(s.requests,[engineer(end=629)])
    p = OrToolsSolver().solve(s,travel(s),time_limit_seconds=.1)
    assert p.metrics.assigned == 0


def test_ortools_handles_incompatible_and_equipment_capacity(make_scenario):
    s = make_scenario([job("no-skill",skill=Skill.EMERGENCY), job("no-mode",required_transport=Transport.WALK),
                      job("a",equipment=Equipment(router=1)),job("b",equipment=Equipment(router=1))],
                      [engineer(stock=Equipment(router=1))])
    t = travel(s)
    p = OrToolsSolver().solve(s,t,time_limit_seconds=.1)
    assert p.metrics.assigned == 1
    assert not validate_plan(s,t,p)


def test_ortools_multiple_disjoint_windows(make_scenario):
    r = job("a").model_copy(update={"windows":[TimeWindow(start=600,end=610),TimeWindow(start=900,end=910)]})
    s = make_scenario([r],[engineer(start=800)])
    p = OrToolsSolver().solve(s,travel(s),time_limit_seconds=.1)
    assert p.metrics.assigned == 1
    assert p.routes[0].visits[0].start == 900


def test_empty_scenario(make_scenario):
    s = make_scenario([])
    p = OrToolsSolver().solve(s,travel(s),time_limit_seconds=.1)
    assert p.metrics.assigned == 0 and p.metrics.engineers_used == 0


def test_urgent_breaks_equal_coverage_tie(make_scenario):
    s = make_scenario([job("ordinary",start=600,end=600,duration=60),
                      job("urgent",start=600,end=600,duration=60,priority="urgent")])
    p = OrToolsSolver().solve(s,travel(s),time_limit_seconds=.15)
    assert p.metrics.assigned == 1
    assert p.routes[0].visits[0].request_id == "urgent"


def test_time_limit_must_be_positive(make_scenario):
    s = make_scenario([job("a")])
    with pytest.raises(ValueError):
        OrToolsSolver().solve(s,travel(s),time_limit_seconds=0)


@pytest.mark.parametrize("priority", ["urgent", "high"])
def test_one_high_tier_job_takes_precedence_over_two_normal_jobs(make_scenario, priority):
    s = make_scenario([job("normal-1", start=600, end=600),
                       job("normal-2", start=640, end=640),
                       job("important", start=600, end=600, duration=90, priority=priority)],
                      [engineer(end=690)])
    t = travel(s)
    baseline = BaselineSolver().solve(s,t)
    assert [v.request_id for v in baseline.routes[0].visits] == ["normal-1", "normal-2"]
    optimized = OrToolsSolver().solve(s,t,time_limit_seconds=.3)
    assert [v.request_id for v in optimized.routes[0].visits] == ["important"]
    assert not validate_plan(s,t,optimized)


def test_emergency_precedes_connection_and_total_coverage(make_scenario):
    from field_router.solvers.ortools_solver import objective_key
    from field_router.planning import make_plan

    s = make_scenario([job("emergency",start=600,end=600,duration=90,priority="urgent"),
                       job("connection",start=600,end=600,priority="high"),
                       job("normal",start=640,end=640)], [engineer(end=690)])
    t = travel(s)
    emergency_only = make_plan(s,t,{"e1":["emergency"]},"test","test")
    connection_only = make_plan(s,t,{"e1":["connection"]},"test","test")
    assert objective_key(emergency_only,s) < objective_key(connection_only,s)
    p = OrToolsSolver().solve(s,t,time_limit_seconds=.3)
    assert [v.request_id for v in p.routes[0].visits] == ["emergency"]
    assert objective_key(p,s) == objective_key(emergency_only,s)


def test_coverage_takes_precedence_over_extra_engineer(make_scenario):
    s = make_scenario([job("a",start=600,end=600),job("b",start=600,end=600)],
                      [engineer("one"),engineer("two")])
    p = OrToolsSolver().solve(s,travel(s),time_limit_seconds=.1)
    assert p.metrics.assigned == 2 and p.metrics.engineers_used == 2


def test_transport_specific_matrix_changes_eligibility_by_time(make_scenario):
    from field_router.travel import MatrixTravel
    s = make_scenario([job("a",start=550,end=550)],
                      [engineer("walk",transport=Transport.WALK),engineer("car",transport=Transport.CAR)])
    t = MatrixTravel(["office","a"],
        {mode:[[0,30 if mode==Transport.WALK else 5],[30 if mode==Transport.WALK else 5,0]] for mode in Transport},
        {mode:[[0,1000],[1000,0]] for mode in Transport})
    for solver in (BaselineSolver(),OrToolsSolver()):
        p = solver.solve(s,t,time_limit_seconds=.1)
        assert p.routes[0].engineer_id == "car"
        assert not validate_plan(s,t,p)
