"""Ordinary arrivals preserve promises; emergency response never interrupts work."""
import pytest

from conftest import engineer, job
from field_router.models import Equipment, Point, Skill
from field_router.planning import make_plan
from field_router.replanning import ScenarioSession
from field_router.travel import synthetic_travel
from field_router.validation import require_valid


def session_for(make_scenario, jobs=None, engineers=None, sequences=None):
    jobs = jobs if jobs is not None else [
        job('a', start=600, end=600, duration=30, equipment=Equipment(router=1)),
        job('b', start=720, end=720, duration=30)]
    engineers = engineers or [engineer(skills=list(Skill), stock=Equipment(router=2, tv_box=1))]
    scenario = make_scenario(jobs, engineers).model_copy(update={
        'points': {key: Point(x_km=0, y_km=0) for key in ['office', *(r.id for r in jobs)]},
        'metadata': {'geography': 'synthetic_cartesian_km'},
        'travel_config': {key: {m: value for m in ('car', 'walk', 'bicycle', 'public')}
                          for key, value in [('speed_kmh', 60), ('distance_factor', 1), ('boarding_minutes', 0)]}})
    plan = make_plan(scenario, synthetic_travel(scenario),
                     sequences if sequences is not None else {engineers[0].id: [r.id for r in jobs]}, 'fixture', 'valid')
    return ScenarioSession(scenario, plan)


def ordinary(session, **changes):
    event = {'id': 'new', 'type': 'new_request', 'time': 640, 'anchor_id': 'a',
             'work_kind': 'connection', 'duration_minutes': 30, 'router': 1, 'tv_box': 1,
             'window_start': 640, 'window_end': 700}
    event.update(changes)
    return session.preview({'version': session.version, 'event': event}, time_limit=.02)


def assignments(plan):
    return {v['request_id']: (r['engineer_id'], v['start']) for r in plan['routes'] for v in r['visits']}


def test_ordinary_insertion_preserves_old_owners_order_starts_and_carried_stock(make_scenario, monkeypatch):
    session = session_for(make_scenario, engineers=[
        engineer('e1', skills=list(Skill), stock=Equipment(router=2, tv_box=1)),
        engineer('e2', skills=list(Skill), stock=Equipment())])
    def forbidden_solver(*args, **kwargs):
        pytest.fail('Ordinary arrival must not invoke a route optimizer')
    monkeypatch.setattr('field_router.replanning.get_solver', forbidden_solver)
    batch = ordinary(session)
    assert batch['insertion_policy'] == 'preserve_existing_assignments_and_starts'
    assert batch['request_labels']['request:new'] == 'Подключение №1'
    assert session.version == 1 and 'request:new' not in session.catalog
    for candidate in batch['candidates']:
        assert assignments(candidate['plan']) == {'request:new': ('e1', 640), 'b': ('e1', 720)}
        assert candidate['ordinary_request']['assigned']
        assert candidate['facts']['reassigned'] == candidate['facts']['shifted'] == 0
        assert candidate['plan']['routes'][0]['loadout'] == {'router': 1, 'tv_box': 1}
        assert batch['frozen'][0]['visits'][0]['request_id'] == 'a'
    session.approve({'version': 1, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    request = session.catalog['request:new']
    assert request.priority == 'high' and request.skill == Skill.CONNECTION
    assert request.region == session.scenario.region
    require_valid(session.scenario, session._travel(session.scenario), session.plan)
    restored = ScenarioSession.restore(session.dump())
    assert restored.request_labels()['request:new'] == 'Подключение №1'


@pytest.mark.parametrize('changes', [
    {'duration_minutes': 90, 'window_start': 650, 'window_end': 670},
    {'router': 2}, {'tv_box': 2},
])
def test_ordinary_no_gap_or_equipment_leaves_only_new_job_unassigned(make_scenario, changes):
    session = session_for(make_scenario)
    batch = ordinary(session, **changes)
    for candidate in batch['candidates']:
        assert assignments(candidate['plan']) == {'b': ('e1', 720)}
        assert [u['request_id'] for u in candidate['plan']['unassigned']] == ['request:new']
        assert not candidate['ordinary_request']['assigned']
        assert candidate['facts']['lost_assignments'] == 0


@pytest.mark.parametrize('work_kind,skill,priority,label', [
    ('connection', Skill.CONNECTION, 'high', 'Подключение'),
    ('local', Skill.LOCAL, 'normal', 'Локальная проблема'),
    ('addon', Skill.CONNECTION, 'normal', 'Доп. заказ'),
])
def test_ordinary_kind_mapping_and_arbitrary_ordered_window(make_scenario, work_kind, skill, priority, label):
    session = session_for(make_scenario)
    batch = ordinary(session, work_kind=work_kind, window_start=637, window_end=693)
    stored = session.batches[batch['id']]['scenario']
    request = next(r for r in stored.requests if r.id == 'request:new')
    assert (request.skill, request.priority) == (skill, priority)
    assert (request.windows[0].start, request.windows[0].end) == (637, 693)
    assert batch['request_labels']['request:new'] == label + ' №1'
    assert all(c['ordinary_request']['start'] >= 640 for c in batch['candidates'])


@pytest.mark.parametrize('changes', [
    {'work_kind': 'emergency'}, {'work_kind': None}, {'window_start': None},
    {'window_start': 750, 'window_end': 700}, {'window_end': 1500},
    {'duration_minutes': 0}, {'router': -1}, {'tv_box': True}, {'anchor_id': 'unknown'},
])
def test_ordinary_rejects_invalid_input_without_publishing(make_scenario, changes):
    session = session_for(make_scenario)
    before = session.dump()
    with pytest.raises(ValueError):
        ordinary(session, **changes)
    assert session.dump() == before


def test_ordinary_requires_competence_and_cannot_load_equipment_from_warehouse(make_scenario):
    session = session_for(make_scenario, engineers=[engineer(stock=None)])
    batch = ordinary(session, router=0, tv_box=0)
    assert all(not c['ordinary_request']['assigned'] for c in batch['candidates'])
    batch = ordinary(session, work_kind='local', router=1, tv_box=0)
    assert all(not c['ordinary_request']['assigned'] for c in batch['candidates'])


def test_ordinary_rejects_preservation_when_observed_state_misses_old_promise(make_scenario):
    session = session_for(make_scenario)
    session.record_fact({'version': 1, 'time': 640, 'engineer_id': 'e1', 'status': 'working',
                         'request_id': 'a', 'remaining_minutes': 120})
    with pytest.raises(ValueError, match='[Пп]ересч'):
        ordinary(session, time=641)


@pytest.mark.parametrize('preserve', [None, False, True])
def test_emergency_protects_started_work_for_every_flag_value(make_scenario, preserve):
    session = session_for(make_scenario, jobs=[job('a', start=600, end=600, duration=180)])
    payload = {'version': 1, 'event': {'id': 'urgent', 'type': 'emergency', 'time': 610,
                                      'anchor_id': 'a', 'duration_minutes': 20}}
    if preserve is not None:
        payload['preserve_in_progress'] = preserve
    batch = session.preview(payload, time_limit=.03)
    assert batch['frozen'][0]['visits'][0]['end'] == 780
    for candidate in batch['candidates']:
        response = candidate['emergency']
        assert response['assigned'] and response['start'] >= 780
        assert response['received_at'] == 610
        assert response['response_target_minutes'] == 120
        assert response['response_target_at'] == 730
        assert response['response_minutes'] >= 170
        assert response['exceeds_response_target'] is True
    assert session.batches[batch['id']]['policy']['preserve_in_progress'] is True


def test_same_region_home_city_does_not_restrict_ordinary_assignment(make_scenario):
    session = session_for(make_scenario, jobs=[job('a', start=720, end=720,
        locality='Красногорск').model_copy(update={'address': 'Московская область, Красногорск'})],
        engineers=[engineer(start_address='Москва', skills=list(Skill), stock=Equipment())])
    batch = ordinary(session, time=600, window_start=600, router=0, tv_box=0)
    assert all(c['ordinary_request']['assigned'] for c in batch['candidates'])


def test_ordinary_does_not_schedule_previously_unassigned_jobs(make_scenario):
    jobs = [job('a', start=720, end=720), job('leftover', start=600, end=700)]
    session = session_for(make_scenario, jobs=jobs, sequences={'e1': ['a']})
    batch = ordinary(session, anchor_id='a', time=600, router=0, tv_box=0)
    for candidate in batch['candidates']:
        assert [u['request_id'] for u in candidate['plan']['unassigned']] == ['leftover']
        assert assignments(candidate['plan'])['a'] == ('e1', 720)


def test_ordinary_preserves_multiple_old_owners_order_and_flexible_window_starts(make_scenario):
    session = session_for(make_scenario, jobs=[job('a', start=700, end=800),
        job('b', start=750, end=850), job('c', start=700, end=900)],
        engineers=[engineer('e1', skills=list(Skill), stock=Equipment()),
                   engineer('e2', skills=list(Skill), stock=Equipment())],
        sequences={'e1': ['a', 'b'], 'e2': ['c']})
    before = assignments(session.plan.model_dump())
    batch = ordinary(session, time=640, router=0, tv_box=0)
    for candidate in batch['candidates']:
        after = assignments(candidate['plan'])
        assert {key: after[key] for key in before} == before
        assert [v['request_id'] for r in candidate['plan']['routes'] if r['engineer_id']=='e1'
                for v in r['visits'] if v['request_id'] in before] == ['a', 'b']


def test_ordinary_rejects_trip_that_would_delay_the_old_service_start(make_scenario):
    session = session_for(make_scenario, jobs=[job('a', start=700, end=800)])
    session.record_fact({'version': 1, 'time': 640, 'engineer_id': 'e1', 'status': 'traveling',
                         'request_id': 'a', 'location_id': 'office', 'remaining_minutes': 90})
    with pytest.raises(ValueError, match='[Пп]ересч'):
        ordinary(session, time=641, router=0, tv_box=0)


def test_ordinary_preserves_promised_start_of_confirmed_early_trip(make_scenario):
    session = session_for(make_scenario, jobs=[job('a', start=640, end=800)])
    # A published later start inside the customer's flexible window.
    route = session.plan.routes[0]
    visit = route.visits[0].model_copy(update={'start': 700, 'end': 730})
    session.plan = session.plan.model_copy(update={'routes': [route.model_copy(update={'visits': [visit]})]})
    session.record_fact({'version': 1, 'time': 640, 'engineer_id': 'e1', 'status': 'traveling',
                         'request_id': 'a', 'location_id': 'office', 'remaining_minutes': 10})
    batch = ordinary(session, time=641, router=0, tv_box=0, window_end=800)
    frozen = [v for r in batch['frozen'] for v in r['visits'] if v['request_id']=='a']
    assert frozen[0]['start'] == 700


def test_ordinary_counts_travel_both_before_and_after_insertion(make_scenario):
    session = session_for(make_scenario, jobs=[job('a', start=600, end=600),
        job('b', start=680, end=680), job('anchor', start=900, end=900)],
        sequences={'e1': ['a', 'b']})
    # The unused anchor is 20 minutes each way, so a 30-minute job cannot fit
    # between current time 640 and the promise at 680.
    scenario = session.scenario.model_copy(update={'points': {
        **session.scenario.points, 'anchor': Point(x_km=20, y_km=0)}})
    session = ScenarioSession(scenario, session.plan)
    batch = ordinary(session, anchor_id='anchor', time=640, window_end=680, router=0, tv_box=0)
    for candidate in batch['candidates']:
        assert not candidate['ordinary_request']['assigned']
        assert assignments(candidate['plan'])['b'] == ('e1', 680)


def test_ordinary_after_explicit_interruption_and_replan_uses_only_live_promises(make_scenario):
    session = session_for(make_scenario, jobs=[job('a', start=600, end=900)])
    session.record_fact({'version': 1, 'time': 600, 'engineer_id': 'e1', 'status': 'working',
                         'request_id': 'a', 'remaining_minutes': 30})
    session.record_fact({'version': 2, 'time': 610, 'engineer_id': 'e1', 'status': 'interrupted',
                         'request_id': 'a', 'remaining_minutes': 20,
                         'consumed_router': 0, 'consumed_tv_box': 0})
    replan = session.preview({'version': 3, 'event': {'id': 'repair', 'type': 'replan', 'time': 611}}, time_limit=.03)
    session.approve({'version': 3, 'batch_id': replan['id'], 'candidate_id': 'stable'})
    batch = ordinary(session, time=611, router=0, tv_box=0, window_start=640, window_end=900)
    assert all(c['ordinary_request']['assigned'] for c in batch['candidates'])
