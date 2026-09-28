"""Engineer absence/resume projections retain facts from the interrupted job."""
import pytest

from field_router.engineer import engineer_state, record_engineer_fact
from field_router.replanning import ScenarioSession
from test_engineer_unavailability import session_with_route, report, preview_replan


def test_returned_engineer_resumes_only_recorded_remainder(make_scenario):
    session = session_with_route(make_scenario)
    report(session, engineer_id='e2', end_time='18:00')
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'working', 'request_id': 'a', 'time': 550, 'remaining_minutes': 30})
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'interrupted', 'request_id': 'a', 'time': 555, 'remaining_minutes': 10,
        'consumed_router': 0, 'consumed_tv_box': 0, 'comment': 'Личные обстоятельства'})
    report(session, start_time='09:15', end_time='12:00', reason='personal')
    period = session.unavailability[-1]
    session.return_from_absence({'version': session.version, 'engineer_id': 'e1',
        'report_id': period['id'], 'time': 570, 'location_id': 'a'})
    batch = preview_replan(session)
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    task = next(task for task in engineer_state(session, 'e1')['tasks'] if task['id'] == 'a')
    assert task['status'] == 'interrupted'
    assert task['visit']['end'] - task['visit']['start'] == 10
    assert task['remaining_minutes'] == 10
    assert task['work_remaining_minutes'] == 10


def test_reassigned_interrupted_work_keeps_remainder_separate_from_trip(make_scenario):
    session = session_with_route(make_scenario)
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'working', 'request_id': 'a', 'time': 550, 'remaining_minutes': 30})
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'interrupted', 'request_id': 'a', 'time': 555, 'remaining_minutes': 10,
        'consumed_router': 0, 'consumed_tv_box': 0, 'comment': 'Личные обстоятельства'})
    report(session, start_time='09:15', end_time='18:00', reason='personal')
    batch = preview_replan(session)
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    assigned = next(task for task in engineer_state(session, 'e2')['tasks'] if task['id'] == 'a')
    assert assigned['status'] == 'planned'
    assert assigned['work_remaining_minutes'] == 10
    assert assigned['remaining_minutes'] == 10
    session = ScenarioSession.restore(session.dump())
    before = session.dump()
    with pytest.raises(ValueError, match='не назначена'):
        session.record_fact({'version': session.version, 'engineer_id': 'e1',
            'status': 'traveling', 'request_id': 'a', 'time': 556,
            'location_id': 'office', 'remaining_minutes': 2})
    assert session.dump() == before
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e2',
        'status': 'traveling', 'request_id': 'a', 'time': 556,
        'location_id': 'office', 'remaining_minutes': 2})
    traveling = next(task for task in engineer_state(session, 'e2')['tasks'] if task['id'] == 'a')
    assert traveling['remaining_minutes'] == 2
    assert traveling['work_remaining_minutes'] == 10
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e2',
        'status': 'working', 'request_id': 'a', 'time': 558, 'remaining_minutes': 8})
    working = next(task for task in engineer_state(session, 'e2')['tasks'] if task['id'] == 'a')
    assert working['work_remaining_minutes'] == working['remaining_minutes'] == 8
