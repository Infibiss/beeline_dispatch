"""Audit must not confuse failed direct travel or append with infeasibility."""
import importlib.util
from pathlib import Path

from conftest import engineer, job
from field_router.models import Transport
from field_router.planning import make_plan
from field_router.travel import MatrixTravel

spec = importlib.util.spec_from_file_location('audit_unassigned', Path(__file__).parents[1] / 'scripts/audit_unassigned.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def matrix(ids, values):
    return MatrixTravel(ids, {mode: values for mode in Transport}, {mode: values for mode in Transport})


def test_insertion_finds_middle_slot_when_append_fails(make_scenario):
    scenario = make_scenario([job('a',600,600),job('b',720,720),job('c',660,660)])
    travel = matrix(['office','a','b','c'], [[0]*4 for _ in range(4)])
    saved = make_plan(scenario,travel,{'e1':['a','b']},'ortools','test')
    result = audit.inspect_request(scenario.requests[2],scenario,saved,travel,
                                  {m:audit.shortest_times(travel,m) for m in Transport})
    assert [(x['engineer'],x['position']) for x in result['saved_route_insertions']] == [('e1',1)]
    assert result['saved_route_insertions'][0]['resulting_objective_key'][2] == 0


def test_nonmetric_direct_failure_is_not_global_impossibility(make_scenario):
    scenario = make_scenario([job('a',540,600,duration=1),job('b',540,560,duration=1)],
                             [engineer(start=540)])
    travel = matrix(['office','a','b'], [[0,5,100],[5,0,5],[100,5,0]])
    saved = make_plan(scenario,travel,{'e1':['a']},'ortools','test')
    result = audit.inspect_request(scenario.requests[1],scenario,saved,travel,
                                  {m:audit.shortest_times(travel,m) for m in Transport})
    assert result['singleton_feasible_engineers'] == []
    assert result['impossible_under_relaxed_travel'] is False
    assert result['eligible_engineers'][0]['relaxed_minutes'] == 10
    assert result['saved_route_insertions'][0]['position'] == 1
