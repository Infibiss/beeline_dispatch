"""ALNS must satisfy the application's independent contract, not its own validator alone."""
import math
import pytest

from conftest import engineer, job
from test_planning import travel
from field_router.models import Equipment, Skill, TimeWindow, Transport
from field_router.solvers.alns_solver import AlnsSolver
from field_router.solvers.ortools_solver import objective_key
from field_router.planning import make_plan
from field_router.travel import MatrixTravel
from field_router.validation import validate_plan


def solve(s, t=None, **kwargs):
    t = t or travel(s)
    p = AlnsSolver().solve(s, t, time_limit_seconds=.15, **kwargs)
    assert p.solver == 'alns'
    assert validate_plan(s, t, p) == []
    return p


def test_stock_skills_modes_and_no_return(make_scenario):
    s = make_scenario([job('a', equipment=Equipment(router=1)),
        job('b', equipment=Equipment(router=1)), job('skill',skill=Skill.EMERGENCY),
        job('mode',required_transport=Transport.WALK)], [engineer(stock=Equipment(router=1))])
    assert solve(s).metrics.assigned == 1
    s = make_scenario([job('a',start=600,end=600)], [engineer(end=630)])
    p = solve(s)
    assert p.metrics.assigned == 1 and p.metrics.distance_m == 1000
    assert solve(make_scenario(s.requests,[engineer(end=629)])).metrics.assigned == 0


def test_multiple_windows_and_empty_day(make_scenario):
    j = job('a').model_copy(update={'windows':[TimeWindow(start=600,end=610),TimeWindow(start=900,end=910)]})
    assert solve(make_scenario([j],[engineer(start=800)])).routes[0].visits[0].start == 900
    assert solve(make_scenario([])).metrics.assigned == 0


def test_emergency_beats_eleven_connections(make_scenario):
    s = make_scenario([job(str(i),start=600,end=610,duration=1,priority='high') for i in range(11)] +
        [job('urgent',start=600,end=600,duration=11,priority='urgent')], [engineer(start=600,end=611)])
    p = solve(s,travel(s,minutes=0,metres=0))
    assert [v.request_id for r in p.routes for v in r.visits] == ['urgent']


def test_high_beats_many_normal(make_scenario):
    s = make_scenario([job(str(i),start=600,end=610,duration=1) for i in range(11)] +
        [job('high',start=600,end=600,duration=11,priority='high')], [engineer(start=600,end=611)])
    assert solve(s,travel(s,minutes=0,metres=0)).routes[0].visits[0].request_id == 'high'


def test_nonmetric_matrix_keeps_indirectly_reachable_job(make_scenario):
    s=make_scenario([job('a',start=540,end=600,duration=10),job('b',start=540,end=600,duration=10)])
    t=MatrixTravel(['office','a','b'], {m:[[0,10,120],[10,0,10],[120,10,0]] for m in Transport},
        {m:[[0,1000,2000],[1000,0,1000],[2000,1000,0]] for m in Transport})
    p=solve(s,t)
    assert p.metrics.assigned==2
    assert not p.diagnostics.get('optimality_proven',False)


@pytest.mark.parametrize('limit',[0,-1,math.inf,math.nan])
def test_bad_budget_rejected(make_scenario,limit):
    s=make_scenario([job('a')])
    with pytest.raises(ValueError):AlnsSolver().solve(s,travel(s),time_limit_seconds=limit)


@pytest.mark.parametrize('profile',['sla','stable','cost'])
def test_profiles_do_not_regress_feasible_reference_seed(make_scenario,profile):
    s=make_scenario([job('late',start=800,end=820),job('early',start=600,end=620)],
        [engineer('one'),engineer('two')])
    t=travel(s)
    reference=make_plan(s,t,{'one':['early','late']},'reference','valid')
    p=solve(s,t,profile=profile,reference=reference)
    assert objective_key(p,s,profile=profile,reference=reference) <= objective_key(reference,s,profile=profile,reference=reference)


def test_plan_diagnostics_roundtrip_json_without_changes(make_scenario):
    from field_router.models import Plan
    from field_router._alns.solver import solve as _warm_import
    p=solve(make_scenario([job('a')]))
    assert 'search' in p.diagnostics
    assert Plan.model_validate_json(p.model_dump_json())==p
