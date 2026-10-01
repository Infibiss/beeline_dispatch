"""Ordinary JSON intake has the same promise contract as the manual form."""
from copy import deepcopy
from pathlib import Path

import pytest

from conftest import engineer, job
from field_router.intake import apply_autopilot
from field_router.models import Equipment, Skill
from field_router.replanning import ScenarioSession
from field_router.validation import require_valid
from test_organizer_day_rules import assignments, ordinary, session_for


def row(key, **changes):
    item = {'id': key, 'type': 'new_request', 'time': 540, 'anchor_id': 'a',
            'work_kind': 'local', 'duration_minutes': 20, 'window_start': 600,
            'window_end': 900, 'router': 0, 'tv_box': 0}
    item.update(changes)
    return item


def promises(session):
    return assignments(session.plan.model_dump())


@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('count', [1, 3])
def test_ordinary_batch_never_uses_unconstrained_optimizer_or_moves_promises(make_scenario, monkeypatch, automatic, count):
    session = session_for(make_scenario, jobs=[job('a', start=700, end=900), job('b', start=800, end=1000)],
        engineers=[engineer('e1', stock=Equipment()), engineer('e2', stock=Equipment())],
        sequences={'e2': ['a', 'b']})
    before = promises(session)
    if automatic:
        session.set_planning_settings({'version': session.version, 'mode': 'autopilot',
            'every_n_requests': count, 'every_n_minutes': None})
    session.receive_requests({'requests': [row(f'new-{i}') for i in range(count)] +
        [row('impossible', duration_minutes=1440)]})
    def forbidden(*args, **kwargs):
        pytest.fail('Ordinary intake must use only promise-preserving gap insertion')
    monkeypatch.setattr('field_router.replanning.get_solver', forbidden)
    if automatic:
        batch, _ = apply_autopilot(session, time_limit=.02)
    else:
        batch = session.preview({'version': session.version,
            'event': {'id': 'batch', 'type': 'replan', 'time': session.time}}, time_limit=.02)
    assert batch['insertion_policy'] == 'preserve_existing_assignments_and_starts'
    for candidate in batch['candidates']:
        after = assignments(candidate['plan'])
        assert {key: after[key] for key in before} == before
        assert [v['request_id'] for r in candidate['plan']['routes'] if r['engineer_id'] == 'e2'
            for v in r['visits'] if v['request_id'] in before] == ['a', 'b']
        assert all('request:new-' + str(i) in after for i in range(count))
        assert any(u['request_id'] == 'request:impossible' for u in candidate['plan']['unassigned'])
        assert candidate['facts']['lost_assignments'] == candidate['facts']['reassigned'] == candidate['facts']['shifted'] == 0
    if not automatic:
        session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    require_valid(session.scenario, session._travel(session.scenario), session.plan)
    restored = ScenarioSession.restore(session.dump())
    assert promises(restored) == promises(session)
    assert {key: promises(restored)[key] for key in before} == before


def test_manual_form_can_include_pending_ordinary_batch(make_scenario):
    session = session_for(make_scenario, jobs=[job('a', start=800, end=900)])
    session.receive_requests({'request': row('queued')})
    batch = ordinary(session, id='manual', time=540, work_kind='local', duration_minutes=20,
        window_start=600, window_end=900, router=0, tv_box=0)
    for candidate in batch['candidates']:
        assert {'a', 'request:queued', 'request:manual'} <= set(assignments(candidate['plan']))
        assert assignments(candidate['plan'])['a'] == ('e1', 800)


def test_pending_emergency_keeps_full_replanning_even_from_ordinary_form(make_scenario):
    session = session_for(make_scenario, jobs=[job('a', start=600, end=900)],
        engineers=[engineer('e1', skills=list(Skill), stock=Equipment())])
    session.receive_requests({'request': {'id': 'urgent', 'type': 'emergency', 'time': 540,
        'anchor_id': 'a', 'duration_minutes': 80, 'router': 0}})
    batch = ordinary(session, id='manual', time=540, work_kind='local', router=0, tv_box=0,
        window_start=900, window_end=1000)
    assert 'insertion_policy' not in batch
    assert all('emergency:urgent' in assignments(c['plan']) for c in batch['candidates'])


def test_ordinary_autopilot_cannot_hide_infeasible_promises_after_new_fact(make_scenario):
    session = session_for(make_scenario, jobs=[job('a', start=700, end=900), job('b', start=750, end=900)])
    session.set_planning_settings({'version': session.version, 'mode': 'autopilot',
        'every_n_requests': 1, 'every_n_minutes': None})
    session.record_fact({'version': session.version, 'engineer_id': 'e1', 'status': 'working',
        'request_id': 'a', 'time': 700, 'remaining_minutes': 100})
    session.receive_requests({'request': row('new', time=700, window_start=850)})
    before = deepcopy(session.dump())
    with pytest.raises(ValueError, match='[Пп]ересч'):
        apply_autopilot(session, time_limit=.02)
    assert session.dump() == before
    # A dispatcher explicitly recalculating changed facts can repair the day.
    batch = session.preview({'version': session.version,
        'event': {'id': 'by-facts', 'type': 'replan', 'time': 700}}, time_limit=.02)
    assert 'insertion_policy' not in batch
    assert all(any(v['request_id'] == 'a' and v['end'] == 800 for r in batch['frozen'] for v in r['visits'])
        for _ in batch['candidates'])


def test_queued_ordinary_preserves_confirmed_early_trip_promise(make_scenario):
    session = session_for(make_scenario, jobs=[job('a', start=640, end=800)])
    route = session.plan.routes[0]
    session.plan = session.plan.model_copy(update={'routes': [route.model_copy(update={'visits': [
        route.visits[0].model_copy(update={'start': 700, 'end': 730})]})]})
    session.record_fact({'version': session.version, 'engineer_id': 'e1', 'status': 'traveling',
        'request_id': 'a', 'time': 640, 'location_id': 'office', 'remaining_minutes': 10})
    first = session.preview({'version': session.version, 'event': {'id': 'ack', 'type': 'new_request',
        'time': 641, 'anchor_id': 'a', 'work_kind': 'local', 'duration_minutes': 20,
        'window_start': 750, 'window_end': 900}}, time_limit=.02)
    session.approve({'version': session.version, 'batch_id': first['id'], 'candidate_id': 'stable'})
    assert session.frozen[0]['visits'][0]['start'] == 700
    session.receive_requests({'request': row('queued', time=642, window_start=800)})
    batch = session.preview({'version': session.version, 'event': {'id': 'queued', 'type': 'replan', 'time': 642}}, time_limit=.02)
    assert next(v['start'] for r in batch['frozen'] for v in r['visits'] if v['request_id'] == 'a') == 700


@pytest.mark.parametrize('region', ['Восток', 'Юго-восток', 'Югоцентр'])
def test_real_road_json_batch_preserves_every_old_promise(region, tmp_path):
    from field_router.regions import load_session
    session = load_session(Path(__file__).resolve().parents[1] / 'data/demo-road' / (region + '.json'), tmp_path / 'state.json')
    before = promises(session)
    anchor = '35675' if region == 'Восток' else next(iter(session.catalog))
    session.receive_requests({'requests': [row('audit-json', anchor_id=anchor,
        duration_minutes=30, window_start=1080, window_end=1200)]})
    batch = session.preview({'version': session.version,
        'event': {'id': 'audit', 'type': 'replan', 'time': 540}}, time_limit=.15)
    scenario = session.batches[batch['id']]['scenario']
    travel = session._travel(scenario)
    for candidate in batch['candidates']:
        after = assignments(candidate['plan'])
        assert {key: after[key] for key in before} == before
        assert candidate['facts']['lost_assignments'] == candidate['facts']['reassigned'] == candidate['facts']['shifted'] == 0
        from field_router.models import Plan
        require_valid(scenario, travel, Plan.model_validate(candidate['plan']))


def test_batch_gap_search_is_bounded_and_preserves_promises(make_scenario):
    from field_router.ordinary_insertion import batch_insertion_candidates
    session = session_for(make_scenario, jobs=[job('a', start=800, end=900)],
        engineers=[engineer('e1', stock=Equipment()), engineer('e2', stock=Equipment())])
    session.receive_requests({'requests': [row('one'), row('two')]})
    from field_router.replanning import Event
    scenario, _, _ = session._residual(Event(id='batch', type='replan', time=540), False)
    plans = batch_insertion_candidates(scenario, session._travel(scenario), session.plan,
        {'request:one', 'request:two'}, beam_width=6)
    assert 1 <= len(plans) <= 6
    assert all(promises(session)['a'] == assignments(plan.model_dump())['a'] for plan in plans)
    assert len({tuple(sorted(assignments(plan.model_dump()).items())) for plan in plans}) == len(plans)


def test_road_queue_can_be_previewed_restored_and_then_applied_by_autopilot(tmp_path):
    from field_router.regions import load_session
    session = load_session(Path(__file__).resolve().parents[1] / 'data/demo-road/Восток.json', tmp_path / 'state.json')
    before = promises(session)
    session.receive_requests({'request': row('repeat-preview', anchor_id='35675',
        duration_minutes=30, window_start=1080, window_end=1200)})
    session.preview({'version': session.version,
        'event': {'id': 'manual-preview', 'type': 'replan', 'time': 540}}, time_limit=.15)
    session = ScenarioSession.restore(session.dump())
    session.set_planning_settings({'version': session.version, 'mode': 'autopilot',
        'every_n_requests': 1, 'every_n_minutes': None})
    batch, _ = apply_autopilot(session, time_limit=.15)
    assert batch['insertion_policy'] == 'preserve_existing_assignments_and_starts'
    assert 'request:repeat-preview' in promises(session)
    assert {key: promises(session)[key] for key in before} == before
    require_valid(session.scenario, session._travel(session.scenario), session.plan)


def test_road_intake_still_rejects_a_real_location_id_collision(tmp_path):
    from field_router.regions import load_session
    session = load_session(Path(__file__).resolve().parents[1] / 'data/demo-road/Восток.json', tmp_path / 'state.json')
    session.receive_requests({'request': row('collision', anchor_id='35675')})
    # A registered road point is not a preview-generated alias, even at the same address.
    key = 'request:collision'
    location = session.geo_locations['35675'].model_copy(update={'id': key})
    session.geography.locations[key] = location
    session.geo_locations[key] = location
    session.aliases[key] = key
    with pytest.raises(ValueError, match='пересекается с дорожной точкой'):
        session.preview({'version': session.version,
            'event': {'id': 'collision-preview', 'type': 'replan', 'time': 540}}, time_limit=.15)


def test_ordinary_insertion_targets_do_not_replace_emergency_response_metrics(make_scenario):
    session = session_for(make_scenario, jobs=[job('a', start=800, end=900, priority='urgent', skill=Skill.EMERGENCY)],
        engineers=[engineer('e1', skills=list(Skill), stock=Equipment())])
    session.receive_requests({'requests': [row('one'), row('two')]})
    batch = session.preview({'version': session.version, 'event': {'id': 'batch', 'type': 'replan', 'time': 540}}, time_limit=.02)
    for candidate in batch['candidates']:
        assert candidate['facts']['response_target_count'] == 1
        assert candidate['facts']['response_unassigned'] == 0
        assert candidate['facts']['priority_start_sum'] == 800
