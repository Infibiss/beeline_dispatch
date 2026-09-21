import pytest

from conftest import engineer, job
from field_router.models import Equipment, Skill, TimeWindow, Transport
from field_router.travel import MatrixTravel
from field_router.solvers.baseline import BaselineSolver
from field_router.validation import validate_plan


def travel(scenario, minutes=10, metres=1000):
    ids = ["office"] + [r.id for r in scenario.requests]
    n = len(ids)
    return MatrixTravel(ids, {t: [[0 if i == j else minutes for j in range(n)] for i in range(n)] for t in Transport},
                        {t: [[0 if i == j else metres for j in range(n)] for i in range(n)] for t in Transport})


def test_wait_window_boundary_service_can_finish_after_window(make_scenario):
    s = make_scenario([job("a", start=600, end=600, duration=30)], [engineer(end=630)])
    p = BaselineSolver().solve(s, travel(s))
    visit = p.routes[0].visits[0]
    assert (visit.arrival, visit.start, visit.end) == (550, 600, 630)
    assert p.metrics.distance_m == 1000  # No return to office.
    assert validate_plan(s, travel(s), p) == []


def test_baseline_preserves_input_order_not_sorted_by_window(make_scenario):
    s = make_scenario([job("late", start=800, end=820), job("early", start=600, end=620)],
                      [engineer("first"), engineer("second")])
    p = BaselineSolver().solve(s, travel(s))
    assert [(r.engineer_id, [v.request_id for v in r.visits]) for r in p.routes] == [
        ("first", ["late"]), ("second", ["early"])]


def test_skill_transport_and_stock_are_real_constraints(make_scenario):
    s = make_scenario([job("skill",skill=Skill.EMERGENCY),
                      job("mode",required_transport=Transport.WALK),
                      job("stock",equipment=Equipment(router=1))], [engineer(stock=Equipment())])
    p = BaselineSolver().solve(s, travel(s))
    assert p.metrics.assigned == 0
    assert {u.code for u in p.unassigned} == {"NO_SKILL", "NO_TRANSPORT", "NO_EQUIPMENT"}
    assert validate_plan(s, travel(s), p) == []


def test_stock_cannot_cover_two_jobs_and_morning_loadout_is_sum(make_scenario):
    jobs = [job("a", equipment=Equipment(router=1)), job("b", equipment=Equipment(router=1,tv_box=1))]
    s = make_scenario(jobs, [engineer(stock=Equipment(router=1,tv_box=1))])
    assert BaselineSolver().solve(s, travel(s)).metrics.assigned == 1
    s = make_scenario(jobs)
    p = BaselineSolver().solve(s, travel(s))
    assert p.routes[0].loadout == Equipment(router=2,tv_box=1)


def test_multiple_windows_and_shift_end(make_scenario):
    r = job("a").model_copy(update={"windows":[TimeWindow(start=600,end=610),TimeWindow(start=900,end=910)]})
    s = make_scenario([r], [engineer(start=800,end=920)])
    assert BaselineSolver().solve(s, travel(s)).metrics.assigned == 0  # end would be 930.
    s = make_scenario([r], [engineer(start=800,end=930)])
    assert BaselineSolver().solve(s, travel(s)).routes[0].visits[0].start == 900


def test_independent_validator_detects_tampered_schedule_metrics_and_coverage(make_scenario):
    s = make_scenario([job("a")])
    p = BaselineSolver().solve(s, travel(s))
    route = p.routes[0]
    visit = route.visits[0].model_copy(update={"start":599})
    broken = p.model_copy(update={"routes":[route.model_copy(update={"visits":[visit, visit]})],
                                "metrics":p.metrics.model_copy(update={"distance_m":0})})
    errors = validate_plan(s, travel(s), broken)
    assert any("window" in e for e in errors)
    assert any("duplicate" in e for e in errors)
    assert any("metrics" in e for e in errors)


def test_invalid_matrices_fail_closed():
    with pytest.raises(ValueError):
        MatrixTravel(["office","a"], {t:[[0,-1],[1,0]] for t in Transport},
                     {t:[[0,1],[1,0]] for t in Transport})


def test_unassigned_explanation_does_not_claim_impossibility_on_nonmetric_matrix(make_scenario):
    from field_router.planning import explain_unassigned
    s = make_scenario([job('a',start=540,end=600,duration=10),job('b',start=540,end=600,duration=10)])
    t = MatrixTravel(['office','a','b'], {m:[[0,10,120],[10,0,10],[120,10,0]] for m in Transport},
                     {m:[[0,1000,2000],[1000,0,1000],[2000,1000,0]] for m in Transport})
    explanation = explain_unassigned(s.requests[1],s,t)
    assert explanation.code == 'SEARCH_UNASSIGNED'
    assert 'Глобальная невозможность не доказана' in explanation.reason
