"""Real minute precision must support fast actions without losing ledger rows."""
from copy import deepcopy

import pytest

from field_router.completion import confirm_completion
from field_router.engineer import record_engineer_fact
from field_router.models import Equipment
from field_router.planning import make_plan
from field_router.replanning import ScenarioSession
from field_router.travel import synthetic_travel
from test_engineer_unavailability import session_with_route, report, preview_replan


def make_session(make_scenario):
    original = session_with_route(make_scenario).scenario
    scenario = original.model_copy(update={
        'requests': [job.model_copy(update={'equipment': Equipment(router=2)})
                     if job.id == 'a' else job for job in original.requests],
        'engineers': [engineer.model_copy(update={'stock': Equipment(router=3)})
                      for engineer in original.engineers]})
    plan = make_plan(scenario, synthetic_travel(scenario), {'e1': ['a', 'b']}, 'test', 'valid')
    return ScenarioSession(scenario, plan)


def fact(session, status, **overrides):
    payload = {'version': session.version, 'engineer_id': 'e1', 'request_id': 'a',
               'status': status, 'time': 550}
    payload.update(overrides)
    return record_engineer_fact(session, payload)


def test_arrival_interruption_absence_and_return_can_share_actual_minute(make_scenario):
    session = make_session(make_scenario)
    fact(session, 'traveling', location_id='office', remaining_minutes=1)
    fact(session, 'working', remaining_minutes=30)
    with pytest.raises(ValueError, match='прервите|завершите'):
        report(session, start_mode='now', location_id=None)
    fact(session, 'interrupted', remaining_minutes=25,
         consumed_router=1, consumed_tv_box=0, comment='Плохо себя чувствую')
    report(session, start_mode='now', location_id=None)
    assert session.unavailability[0]['start_time'] == '09:10'
    assert session.time == 550
    assert [row['status'] for row in session.fact_log] == ['traveling', 'working', 'interrupted']
    batch = preview_replan(session, 'same-minute-absence')
    assert next(row for row in batch['inventory'] if row['engineer_id'] == 'e1')['consumed']['router'] == 1
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    session.return_from_absence({'version': session.version, 'engineer_id': 'e1',
        'report_id': session.unavailability[0]['id'], 'time': 550})
    restored = ScenarioSession.restore(session.dump())
    assert restored.time == 550 and restored.location_confirmation_pending('e1')
    assert restored.fact_log == session.fact_log
    # A later employee action replays the entire accepted same-minute chain.
    record_engineer_fact(restored, {'version': restored.version, 'engineer_id': 'e2',
        'status': 'available', 'time': 550})
    assert restored.fact_log[:4] == session.fact_log


def test_two_interruptions_same_minute_keep_distinct_consumption_and_corrections(make_scenario):
    session = make_session(make_scenario)
    for remaining in (20, 10):
        fact(session, 'working', remaining_minutes=remaining + 10)
        fact(session, 'interrupted', remaining_minutes=remaining,
             consumed_router=1, consumed_tv_box=0, comment='Нужно прервать работу')
    interruptions = [row for row in session.fact_log if row['status'] == 'interrupted']
    batch = preview_replan(session, 'two-interruptions')
    ledger = [visit for route in batch['frozen'] for visit in route['visits']
              if visit.get('status') == 'interrupted']
    assert len(ledger) == len({row['fact_id'] for row in ledger}) == 2
    assert {row['fact_record_id'] for row in ledger} == {row['id'] for row in interruptions}
    assert sum(row['consumed_equipment']['router'] for row in ledger) == 2
    residual = session.batches[batch['id']]['scenario']
    job = next(job for job in residual.requests if job.id == 'a')
    assert job.duration_minutes == 10 and job.equipment.router == 0
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    # Correct only the first of two reports sharing worker, minute, job and status.
    first = interruptions[0]
    session.record_fact({**first['input'], 'version': session.version,
        'fact_id': first['id'], 'correction_reason': 'Первое устройство не использовал',
        'consumed_router': 0})
    ledger = [visit for route in session.frozen for visit in route['visits']
              if visit.get('status') == 'interrupted']
    amounts = {row['fact_record_id']: row['consumed_equipment']['router'] for row in ledger}
    assert amounts == {interruptions[0]['id']: 0, interruptions[1]['id']: 1}
    restored = ScenarioSession.restore(session.dump())
    again = preview_replan(restored, 'after-correction')
    assert next(row for row in again['inventory'] if row['engineer_id'] == 'e1')['consumed']['router'] == 1
    job = next(job for job in restored.batches[again['id']]['scenario'].requests if job.id == 'a')
    assert job.duration_minutes == 10 and job.equipment.router == 1


def test_same_minute_completion_is_confirmed_once_and_replays_with_consumption(make_scenario):
    session = make_session(make_scenario)
    fact(session, 'working', remaining_minutes=30)
    fact(session, 'completed', consumed_router=2, consumed_tv_box=0)
    pending = deepcopy(session.fact_log[-1])
    batch = preview_replan(session, 'pending-completion')
    session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    before = session.dump()
    with pytest.raises(ValueError):
        session.record_fact({'version': session.version, 'engineer_id': 'e1',
            'request_id': 'a', 'status': 'completed', 'time': 550, 'consumed_router': 2})
    assert session.dump() == before
    confirm_completion(session, {'version': session.version, 'fact_id': pending['id']})
    assert session.fact_log[-1]['sequence'] == pending['sequence']
    ledger = [visit for route in session.frozen for visit in route['visits']
              if visit.get('fact_record_id') == pending['id']]
    assert len(ledger) == 1 and ledger[0]['status'] == 'completed'
    restored = ScenarioSession.restore(session.dump())
    again = preview_replan(restored, 'confirmed-completion')
    row = next(row for row in again['inventory'] if row['engineer_id'] == 'e1')
    assert row['consumed']['router'] == 2 and row['reserved']['router'] == 0
    before = restored.dump()
    with pytest.raises(ValueError):
        confirm_completion(restored, {'version': restored.version, 'fact_id': pending['id']})
    assert restored.dump() == before


def test_employee_cannot_go_backwards_or_change_confirmed_place_in_same_minute(make_scenario):
    session = make_session(make_scenario)
    fact(session, 'available', location_id='office')
    before = session.dump()
    for changes in ({'time': 549, 'location_id': 'office'}, {'time': 550, 'location_id': 'a'}):
        with pytest.raises(ValueError):
            fact(session, 'available', **changes)
        assert session.dump() == before
