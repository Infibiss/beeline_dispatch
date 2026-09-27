"""Operational counters must not count invalidated assignments as covered."""
from copy import deepcopy
from pathlib import Path

from scripts.intake_demo import build_stream_baseline
from field_router.replanning import ScenarioSession
from test_engineer_unavailability import session_with_route, report, preview_replan


def test_report_reduces_coverage_without_rewriting_the_approved_routes(make_scenario):
    session = session_with_route(make_scenario)
    original_plan = session.plan.model_dump()
    assert session.assignment_summary()['assigned'] == 2
    report(session)
    summary = session.state()['assignment_summary']
    assert summary == {'total': 2, 'assigned': 0, 'requires_assignment': 2,
                       'awaiting_replan': 2, 'unassigned': 0, 'pending_request_ids': ['a', 'b']}
    assert session.plan.model_dump() == original_plan
    assert session.workbench()['operations']['assignment_summary'] == summary
    assert session.workbench(1)['operations']['assignment_summary']['assigned'] == 2
    batch = preview_replan(session)
    assert session.assignment_summary() == summary  # Preview is not approval.
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    applied = session.assignment_summary()
    assert applied['awaiting_replan'] == 0
    assert applied['assigned'] == session.plan.metrics.assigned
    assert applied['requires_assignment'] == session.plan.metrics.unassigned
    assert applied['assigned'] + applied['requires_assignment'] == applied['total']
    notice=session.dispatch_notifications[-1]
    assert notice['before_effective_assigned_count'] == 0
    assert notice['before_requires_assignment_count'] == 2


def test_real_demo_shows_52_valid_assignments_and_14_requiring_assignment():
    source = Path(__file__).resolve().parents[1] / 'data/demo-road/Восток.json'
    session = build_stream_baseline(source)
    summary = session.assignment_summary()
    assert (summary['assigned'], summary['requires_assignment'], summary['total']) == (52, 14, 66)
    assert session.plan.metrics.assigned == 66  # Keep the published plan as evidence.
    assert set(summary['pending_request_ids']) == {row['request_id'] for row in session.replanning_pool()}
    # Saved sessions written before this fix gain current metadata on read only.
    saved = deepcopy(session.dump())
    for view in saved['views'].values():
        view['operations'].pop('assignment_summary', None)
    restored = ScenarioSession.restore(saved)
    assert restored.workbench()['operations']['assignment_summary'] == summary
    assert 'assignment_summary' not in restored.views[restored.version]['operations']


def test_absence_and_solver_unassigned_are_counted_as_a_union(make_scenario):
    session = session_with_route(make_scenario)
    from field_router.planning import make_plan
    session.plan = make_plan(session.scenario, session._travel(session.scenario), {'e1': ['a']}, 'test', 'valid')
    report(session)
    summary = session.assignment_summary()
    assert summary['total'] == 2 and summary['requires_assignment'] == 2
    assert summary['awaiting_replan'] == 1 and summary['unassigned'] == 1
    assert summary['assigned'] == 0
