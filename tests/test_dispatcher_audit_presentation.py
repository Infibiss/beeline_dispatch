"""Saved, factual presentation contracts for dispatcher approvals."""
from copy import deepcopy
import pytest

from test_replanning import demo
from conftest import job, engineer
from field_router.replanning import ScenarioSession
from field_router.planning import make_plan
from field_router.travel import synthetic_travel
from field_router.solvers.baseline import BaselineSolver
from field_router.engineer import engineer_state


def test_initial_baseline_uses_pre_issue_stock_and_saves_compact_distances(make_scenario):
    scenario, _ = demo(make_scenario)
    plan = make_plan(scenario, synthetic_travel(scenario), {'e2': ['a', 'b']}, 'demo', 'valid')
    expected = BaselineSolver().solve(scenario, synthetic_travel(scenario))
    session = ScenarioSession(scenario, plan)
    comparison = session.workbench()['baseline_comparison']
    assert comparison['version'] == 1 and comparison['time'] == 540
    assert comparison['baseline']['assigned'] == expected.metrics.assigned
    assert comparison['engineers'][0] == {'engineer_id': 'e1',
        'baseline_distance_m': expected.routes[0].distance_m, 'plan_distance_m': 0,
        'baseline_assigned':2,'plan_assigned':0}
    assert 'routes' not in comparison['baseline'] and 'geography' not in comparison
    assert ScenarioSession.restore(session.dump()).workbench()['baseline_comparison'] == comparison


def test_comparison_is_saved_per_version_and_hidden_after_invalidating_fact(make_scenario):
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    original = deepcopy(session.workbench()['baseline_comparison'])
    batch = session.preview({'version': 1, 'event': {'id': 'replan', 'type': 'replan', 'time': 590}}, time_limit=.03)
    session.approve({'version': 1, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    current = session.workbench()['baseline_comparison']
    assert current['version'] == 2 and current['time'] == 590
    assert current['plan']['assigned'] == session.plan.metrics.assigned
    session.record_fact({'version': 2, 'engineer_id': 'e2', 'status': 'available', 'time': 600, 'location_id': 'office'})
    assert session.workbench()['baseline_comparison'] is None
    assert session.workbench(1)['baseline_comparison'] == original
    assert session.workbench(2)['baseline_comparison'] == current


def test_approval_compares_residual_counts_and_keeps_application_time(make_scenario):
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    batch = session.preview({'version': 1, 'event': {'id': 'replan', 'type': 'replan', 'time': 590}}, time_limit=.03)
    session.approve({'version': 1, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    comparison = session.dispatch_notifications[-1]['assignment_comparison']
    assert comparison == {'time': 590, 'before_assigned': 1, 'after_assigned': 1,
        'before_requires_assignment': 0, 'after_requires_assignment': 0, 'frozen_count': 1}
    session.time = 650
    assert session.state()['last_applied']['time'] == 590
    restored = ScenarioSession.restore(session.dump())
    restored.history[-1].pop('application_time', None)
    assert restored.state()['last_applied']['time'] == 590


def test_manual_new_ordinary_is_recorded_as_new_with_readable_engineer_label(make_scenario):
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    batch = session.preview({'version': 1, 'event': {'id': 'random-manual-id', 'type': 'new_request', 'time': 540,
        'anchor_id': 'a', 'duration_minutes': 20, 'work_kind': 'local', 'window_start': 700, 'window_end': 900}}, time_limit=.03)
    session.approve({'version': 1, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    key = 'request:random-manual-id'
    entry = next(row for row in session.decision_log if row['request_id'] == key)
    assert entry['kind'] == 'new' and entry['change_type'] == 'new_assignment'
    assert session.dispatch_notifications[-1]['created_request_ids'] == [key]
    assert session.state()['last_applied']['incoming_count'] == 1
    task = next(task for task in engineer_state(session, 'e1')['tasks'] if task['id'] == key)
    assert task['display_label'] == session.request_labels()[key] == 'Локальная проблема №1'


def test_crew_comparison_uses_resources_before_loadout_is_issued(make_scenario):
    from field_router.crew import CrewService
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    crew = CrewService()
    rows = crew.settings(session)['engineers']
    for row in rows:
        row.update(stock_mode='plan', router=0, tv_box=0)
    result = crew.preview(session, {'version': 1, 'engineers': rows}, time_limit=.03)
    candidate = crew.selected(session, {'version': 1, 'id': result['id']})
    pre_issue = candidate.scenario.model_copy(update={'engineers': [e.model_copy(update={'stock': None}) for e in candidate.scenario.engineers]})
    expected = BaselineSolver().solve(pre_issue, synthetic_travel(pre_issue))
    comparison = candidate.workbench()['baseline_comparison']
    assert comparison['version'] == 2
    assert comparison['baseline']['assigned'] == expected.metrics.assigned
    assert {row['engineer_id']: row['baseline_distance_m'] for row in comparison['engineers']} == {
        e.id: next((r.distance_m for r in expected.routes if r.engineer_id == e.id), 0) for e in pre_issue.engineers}


def test_plan_day_and_report_render_saved_comparison_and_display_labels(make_scenario):
    from field_router.dispatcher import render_dispatcher
    from field_router.report import render_report
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    comparison = session.workbench()['baseline_comparison']
    report = render_report(scenario, [plan], baseline_comparison=comparison, request_labels={'a': 'Локальная проблема №1'})
    dispatcher = render_dispatcher(session.workbench())
    for document in (report, dispatcher):
        assert 'Сравнение с базовым планом' in document
        assert 'Те же заявки, инженеры, старты и оборудование' in document
        assert 'Расстояние по инженерам' in document
        assert 'Базовый, км' in document and 'Применённый, км' in document
        assert 'Базовый, заявок' in document and 'Применённый, заявок' in document
    assert '1. Локальная проблема №1' in report


def test_inbox_returns_saved_application_time_and_readable_request_labels(make_scenario, tmp_path):
    import json
    import threading
    from urllib.request import build_opener, ProxyHandler
    from field_router.scenario_server import create_server
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    batch = session.preview({'version': 1, 'event': {'id': 'manual', 'type': 'new_request', 'time': 540,
        'anchor_id': 'a', 'duration_minutes': 20, 'work_kind': 'local', 'window_start': 700, 'window_end': 900}}, time_limit=.03)
    session.approve({'version': 1, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    session.time = 720
    server = create_server(session, tmp_path / 'state.json', port=0, time_limit=.03, disable_gps=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{server.server_port}/api/inbox') as response:
            inbox = json.load(response)
        assert inbox['last_applied']['time'] == 540
        assert inbox['request_labels']['request:manual'] == 'Локальная проблема №1'
    finally:
        server.shutdown(); server.server_close(); worker.join()


@pytest.mark.parametrize('release', ['emergency', 'absence'])
def test_released_frozen_job_keeps_previous_assignment_in_residual_comparison(make_scenario, release):
    from field_router.models import Point, Skill
    scenario, _ = demo(make_scenario)
    scenario = scenario.model_copy(update={
        'requests': [job('a', start=600, end=800), job('b', start=700, end=900)],
        'engineers': [engineer('e1', skills=[Skill.LOCAL, Skill.EMERGENCY])],
        'points': {'office': Point(x_km=0, y_km=0), 'a': Point(x_km=20, y_km=0), 'b': Point(x_km=21, y_km=0)}})
    plan = make_plan(scenario, synthetic_travel(scenario), {'e1': ['a', 'b']}, 'demo', 'valid')
    session = ScenarioSession(scenario, plan)
    first = session.preview({'version': 1, 'event': {'id': 'first', 'type': 'replan', 'time': 550}}, time_limit=.03)
    session.approve({'version': 1, 'batch_id': first['id'], 'candidate_id': 'stable'})
    assert {v['request_id'] for r in session.frozen for v in r['visits']} == {'a'}
    assert {v.request_id for r in session.plan.routes for v in r.visits} == {'b'}
    if release == 'absence':
        session.report_unavailability({'version': session.version, 'engineer_id': 'e1',
            'start_date': scenario.date, 'start_time': '09:11', 'end_date': scenario.date,
            'end_time': '18:00', 'reason': 'transport', 'comment': 'Поломка автомобиля'})
        assert session.pending_reassignment_ids() == ['a', 'b']
        event = {'id': 'after-absence', 'type': 'replan', 'time': 551}
    else:
        event = {'id': 'urgent', 'type': 'emergency', 'time': 551, 'anchor_id': 'a', 'duration_minutes': 20}
    second = session.preview({'version': session.version, 'event': event, 'allow_redirect': True}, time_limit=.03)
    session.approve({'version': session.version, 'batch_id': second['id'], 'candidate_id': 'sla'})
    notice = session.dispatch_notifications[-1]
    assert notice['assignment_comparison']['before_assigned'] == (0 if release == 'absence' else 2)
    assert notice['assignment_comparison']['before_requires_assignment'] == (2 if release == 'absence' else 0)
    assert notice['assignment_comparison']['frozen_count'] == 0
    after = next(((r.engineer_id, v.start) for r in session.plan.routes for v in r.visits if v.request_id == 'a'), None)
    entries = [row for row in session.decision_log if row['version'] == session.version and row['request_id'] == 'a']
    if after == ('e1', 600):
        assert not entries  # Releasing the trip alone did not change its promise.
    else:
        assert len(entries) == 1
        assert entries[0]['previous_engineer_id'] == 'e1' and entries[0]['previous_start'] == 600
        assert entries[0]['change_type'] in ('lost_assignment', 'reassigned', 'rescheduled')
    assert 'a' not in notice['created_request_ids']
    assert notice['assignment_comparison']['after_assigned'] == session.plan.metrics.assigned
