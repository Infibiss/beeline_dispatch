from copy import deepcopy

from field_router.day_triage import prioritize
from test_day_control import control, setup


def test_percentage_alone_does_not_make_an_incident_immediate(make_scenario):
    session = setup(make_scenario)
    risk = {'engineer_id': 'e1', 'p_overtime': .99, 'expected_overtime_minutes': 30,
            'visits': [{'request_id': 'a', 'p_sla_breach': 0,
                        'p_late_start': .99, 'expected_late_minutes': 40}]}
    result = control(session, [risk])
    assert {r['tier'] for r in result['items']} == {'watch'}
    assert result['tiers'] == {'now': 0, 'prepare': 0, 'watch': 2}
    assert result['counts']['deadline'] == 0
    assert 'неизвестна' in result['items'][0]['reason']


def test_deadline_filter_is_distinct_from_tight_windows_and_keeps_group_members(make_scenario):
    session = setup(make_scenario)
    risk = {'engineer_id': 'e1', 'visits': [
        {'request_id': rid, 'p_sla_breach': .6, 'expected_late_minutes': 15} for rid in ['a', 'b']]}
    result = control(session, [risk])
    assert result['counts']['deadline'] == 2 and result['counts']['tight'] == 0
    assert len(result['items']) == 1
    row = result['items'][0]
    assert row['categories'] == ['deadline']
    assert row['deadline_request_ids'] == ['a', 'b']
    assert row['tier'] == 'now'  # first visit in ten minutes, not because of .6
    assert 'a' in row['impact'] and '09:10' in row['impact']
    assert 'время освобождения' in row['next_step']
    b = row['requests'][1]
    assert b['previous_request_id'] == 'a' and b['schedule_buffer_minutes'] == 29
    session.fact_log.append({'status': 'completed', 'request_id': 'a'})
    updated = control(session, [risk])
    assert updated['counts']['deadline'] == 1
    assert updated['items'][0]['tier'] == 'prepare'


def test_confirmed_obstruction_owns_related_model_alert_not_a_duplicate(make_scenario):
    session = setup(make_scenario)
    session.inventory[0]['available'] = {'router': 0, 'tv_box': 0}
    session.inventory[0]['reserved'] = {'router': 0, 'tv_box': 0}
    risk = {'engineer_id': 'e1', 'visits': [{'request_id': 'a', 'p_sla_breach': .8}]}
    before = deepcopy(session.dump())
    result = control(session, [risk])
    rows = [r for r in result['items'] if 'a' in r['request_ids']]
    assert len(rows) == 1 and rows[0]['kind'] == 'equipment'
    assert rows[0]['tier'] == 'now' and 'deadline' in rows[0]['categories']
    assert result['counts']['deadline'] == 1
    assert session.dump() == before
    session.inventory[0]['available']['router'] = 1
    assert all(r['kind'] != 'equipment' for r in control(session, [risk])['items'])
    assert control(session, [risk])['counts']['deadline'] == 1  # independent model signal remains


def test_delayed_work_adds_deadline_signal_without_stale_model(make_scenario):
    session = setup(make_scenario)
    session.statuses['e1'] = {'status': 'working', 'request_id': 'a', 'time': 550, 'remaining_minutes': 200}
    session.time = 560
    session.needs_replanning = True
    result = control(session)
    assert result['counts']['deadline'] == 1
    row = next(r for r in result['items'] if r['kind'] == 'delay')
    assert row['deadline_request_ids'] == ['b'] and row['tier'] == 'now'
    assert row['evidence'] == 'forecast'  # projected propagation, not confirmed lateness
    session.statuses['e1']['remaining_minutes'] = 25
    assert control(session)['counts']['deadline'] == 0


def test_missing_status_and_stale_gps_are_one_contact_action(make_scenario):
    session = setup(make_scenario)
    session.execution_mode = 'manual'
    gps = {'channel': 'device', 'plan_version': 1, 'plan_date': session.scenario.date,
           'rows': [{'engineer_id': 'e1', 'tracking_state': 'active', 'date_matches': True,
                     'point_age_seconds': 180, 'alerts': []}]}
    result = control(session, gps=gps)
    rows = [r for r in result['items'] if r['engineer_id'] == 'e1']
    assert len(rows) == 1 and rows[0]['tier'] == 'now'
    assert 'статуса' in rows[0]['detail']
    session.statuses['e1'] = {'status': 'available', 'time': 540}
    gps['rows'][0]['point_age_seconds'] = 5
    assert not control(session, gps=gps)['items']


def test_triage_changes_with_time_not_with_probability():
    row = {'id': 'x', 'kind': 'late', 'categories': ['deadline'], 'evidence': 'forecast',
           'requests': [{'request_id': 'a', 'planned_start': 900, 'deadline': 1000,
                         'windows': [{'start': 900, 'end': 1000}]}],
           'deadline': 1000, 'minutes_to_deadline': 460, 'urgent': False,
           'priority_rank': 2, 'engineer_name': 'Анна', 'detail': 'Модельный сигнал.'}
    for now, expected in [(540, 'watch'), (780, 'prepare'), (870, 'now')]:
        row['minutes_to_deadline'] = 1000 - now
        prioritize([row], now)
        assert row['tier'] == expected


def test_grouping_does_not_promote_evening_deadline_because_of_morning_start_shift():
    row = {'id': 'group', 'kind': 'late', 'categories': ['deadline'], 'evidence': 'forecast',
           'requests': [{'request_id': 'morning', 'planned_start': 550, 'deadline': 650, 'windows': []},
                        {'request_id': 'evening', 'planned_start': 960, 'deadline': 1000, 'windows': []}],
           'deadline_request_ids': ['evening'], 'deadline': 650, 'minutes_to_deadline': 110,
           'urgent': False, 'priority_rank': 2, 'engineer_name': 'Анна', 'detail': 'Модельный сигнал.'}
    prioritize([row], 540)
    assert row['tier'] == 'watch' and row['focus_request_id'] == 'evening'
    assert row['deadline'] == 1000
    assert 'evening' in row['next_step'] and 'morning' not in row['next_step']
