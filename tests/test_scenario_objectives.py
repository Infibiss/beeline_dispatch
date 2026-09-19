import pytest

from conftest import engineer, job
from test_planning import travel
from field_router.planning import make_plan
from field_router.solvers.ortools_solver import OrToolsSolver, objective_key
from field_router.validation import validate_plan


def test_stable_preserves_engineer_and_non_earliest_start(make_scenario):
    s = make_scenario([job('a')], [engineer('first'), engineer('second')])
    t = travel(s)
    reference = make_plan(s,t,{'second':['a']},'test','test')
    route = reference.routes[0]
    visit = route.visits[0].model_copy(update={'start':680,'end':710})
    reference = reference.model_copy(update={'routes':[route.model_copy(update={'visits':[visit]})]})
    stable = OrToolsSolver().solve(s,t,profile='stable',reference=reference,time_limit_seconds=.1)
    assert stable.routes[0].engineer_id == 'second'
    assert stable.routes[0].visits[0].start == 680
    assert validate_plan(s,t,stable) == []


def test_sla_prefers_earlier_high_tier_even_with_extra_engineer(make_scenario):
    s = make_scenario([job('a',priority='high'),job('b',priority='urgent')],
                      [engineer('first'),engineer('second')])
    t = travel(s)
    cost = OrToolsSolver().solve(s,t,profile='cost',time_limit_seconds=.1)
    sla = OrToolsSolver().solve(s,t,profile='sla',time_limit_seconds=.1)
    assert cost.metrics.engineers_used == 1
    assert sla.metrics.engineers_used == 2
    assert [v.start for r in sla.routes for v in r.visits] == [600,600]


@pytest.mark.parametrize('profile',['stable','sla','cost'])
def test_profiles_preserve_coverage_priority(make_scenario,profile):
    s = make_scenario([job('n1',start=600,end=600),job('n2',start=640,end=640),
                      job('high',start=600,end=600,duration=90,priority='high')], [engineer(end=690)])
    p = OrToolsSolver().solve(s,travel(s),profile=profile,time_limit_seconds=.1)
    assert [v.request_id for r in p.routes for v in r.visits] == ['high']


def test_reference_to_unavailable_engineer_is_ignored(make_scenario):
    old = make_scenario([job('a')],[engineer('gone')])
    reference = make_plan(old,travel(old),{'gone':['a']},'test','test')
    s = make_scenario(old.requests)
    p = OrToolsSolver().solve(s,travel(s),profile='stable',reference=reference,time_limit_seconds=.1)
    assert objective_key(p,s,profile='stable',reference=reference)[2:4] == (0,0)


def test_bad_profile_rejected(make_scenario):
    s = make_scenario([])
    with pytest.raises(ValueError,match='profile'):
        OrToolsSolver().solve(s,travel(s),profile='invented')


def test_profile_weights_fail_closed_on_overflow(make_scenario):
    s = make_scenario([job('a')])
    with pytest.raises(ValueError,match='safe integer'):
        OrToolsSolver().solve(s,travel(s,metres=10**15),profile='stable',time_limit_seconds=.1)


def test_stable_reconstructs_waiting_and_arrivals_for_multiple_visits(make_scenario):
    s = make_scenario([job('a',end=900),job('b',end=900)])
    t = travel(s)
    reference = make_plan(s,t,{'e1':['a','b']},'test','test')
    route = reference.routes[0]
    visits = [route.visits[0].model_copy(update={'start':700,'end':730}),
              route.visits[1].model_copy(update={'arrival':740,'start':800,'end':830})]
    reference = reference.model_copy(update={'routes':[route.model_copy(update={'visits':visits})]})
    p = OrToolsSolver().solve(s,t,profile='stable',reference=reference,time_limit_seconds=.1)
    assert [(v.arrival,v.start,v.end) for v in p.routes[0].visits] == [(550,700,730),(740,800,830)]
    assert not validate_plan(s,t,p)


def test_default_and_cost_keep_same_objective_order(make_scenario):
    s = make_scenario([job('a'),job('b')],[engineer('first'),engineer('second')])
    t = travel(s)
    default = OrToolsSolver().solve(s,t,time_limit_seconds=.1)
    cost = OrToolsSolver().solve(s,t,profile='cost',time_limit_seconds=.1)
    assert objective_key(default,s) == objective_key(cost,s,profile='cost')
    assert default.metrics.engineers_used == 1
