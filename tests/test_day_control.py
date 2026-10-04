from copy import deepcopy

from field_router.day_control import attention
from field_router.fleet_risk import fleet_snapshot
from field_router.models import Equipment, TimeWindow, Unassigned, Vehicle
from field_router.replanning import ScenarioSession
from field_router.risk_dashboard import capture_dashboard_session, dashboard
from test_replanning import demo


def control(session, routes=(), gps=None):
    forecast = {'status': 'ready', 'plan_version': session.version,
                'region': session.scenario.region, 'routes': list(routes)}
    return attention(session, forecast, fleet_snapshot(session.scenario), gps)


def setup(make_scenario):
    return ScenarioSession(*demo(make_scenario))


def test_outage_is_one_group_and_resolved_report_does_not_linger(make_scenario):
    session = setup(make_scenario)
    session.report_unavailability({'version': 1, 'engineer_id': 'e1',
        'start_date': session.scenario.date, 'end_date': session.scenario.date,
        'start_time': '09:05', 'end_time': '18:00', 'reason': 'transport', 'comment': 'Сломалась машина'})
    before = deepcopy(session.dump())
    result = control(session)
    incidents = [r for r in result['items'] if 'unassigned' in r['categories']]
    assert len(incidents) == 1
    assert incidents[0]['request_ids'] == ['a', 'b']
    assert incidents[0]['evidence'] == 'fact'
    assert result['counts']['unassigned'] == session.assignment_summary()['requires_assignment'] == 2
    assert session.dump() == before
    # Once both jobs have valid assignments, old absence history is not an incident.
    session.plan = session.plan.model_copy(update={'routes': [session.plan.routes[0].model_copy(update={'engineer_id': 'e2'})]})
    assert control(session)['counts']['unassigned'] == 0
    assert not any(r['kind'] == 'unavailable' for r in control(session)['items'])


def test_unassigned_search_failure_is_not_called_impossible_and_deadline_counts_down(make_scenario):
    session = setup(make_scenario)
    session.plan = session.plan.model_copy(update={'routes': [], 'unassigned': [Unassigned(
        request_id='a', code='SEARCH_UNASSIGNED', reason='Technical search reason')]})
    a = next(r for r in control(session)['items'] if r['id'] == 'unassigned-a')
    assert 'не доказывает' in a['detail']
    session.time += 10
    updated = next(r for r in control(session)['items'] if r['id'] == 'unassigned-a')
    assert updated['minutes_to_deadline'] == a['minutes_to_deadline'] - 10
    assert control(session)['counts']['unassigned'] == 2  # includes catalog job omitted from reason list


def test_multiple_windows_are_not_combined_and_completed_jobs_leave_queue(make_scenario):
    session = setup(make_scenario)
    visit = session.plan.routes[0].visits[0]
    session.catalog['a'] = session.catalog['a'].model_copy(update={'windows': [
        TimeWindow(start=visit.start, end=visit.start + 15), TimeWindow(start=900, end=1000)]})
    result = control(session)
    row = next(r for r in result['items'] if r['id'] == 'visit-a')
    assert row['buffer_minutes'] == 15
    assert row['requests'][0]['deadline'] == visit.start + 15
    assert result['counts']['tight'] == 1
    session.fact_log.append({'status': 'completed', 'request_id': 'a'})
    assert control(session)['counts']['tight'] == 0
    assert not any('a' in r['request_ids'] for r in control(session)['items'])


def test_thresholds_and_stale_forecast_never_create_false_operational_risks(make_scenario):
    session = setup(make_scenario)
    route = {'engineer_id': 'e1', 'p_overtime': .7, 'expected_overtime_minutes': 35,
             'visits': [{'request_id': 'a', 'p_sla_breach': .4, 'expected_late_minutes': 12}]}
    result = control(session, [route])
    assert result['counts']['overtime'] == 1
    assert {'late', 'overtime'} <= {r['kind'] for r in result['items']}
    session.needs_replanning = True
    result = control(session, [route])
    assert result['counts']['overtime'] == 0
    assert all(r['kind'] not in ('late', 'overtime') for r in result['items'])
    session.needs_replanning = False
    route['expected_overtime_minutes'] = .5
    route['visits'][0]['p_sla_breach'] = .001
    route['visits'][0]['expected_late_minutes'] = 0
    assert not control(session, [route])['items']


def test_stock_is_engineer_specific_and_does_not_block_jobs_without_equipment(make_scenario):
    session = setup(make_scenario)
    session.inventory[0]['available'] = {'router': 0, 'tv_box': 0}
    session.inventory[0]['reserved'] = {'router': 0, 'tv_box': 0}
    session.inventory[1]['available'] = {'router': 10, 'tv_box': 0}
    row = next(r for r in control(session)['items'] if r['kind'] == 'equipment')
    assert row['engineer_id'] == 'e1'
    assert row['request_ids'] == ['a']
    assert 'не хватает 1' in row['detail']
    assert row['action'] == 'resources'


def test_delay_uses_fact_timestamp_groups_following_jobs_and_is_not_confirmed_lateness(make_scenario):
    session = setup(make_scenario)
    session.statuses['e1'] = {'status': 'working', 'request_id': 'a', 'time': 550, 'remaining_minutes': 100}
    session.time = 560
    row = next(r for r in control(session)['items'] if r['kind'] == 'delay')
    assert row['evidence'] == 'forecast'
    assert row['request_ids'] == ['a', 'b']
    assert '10:50' in row['detail']  # 550 + 100, not now + 100
    session.time = 651
    row = next(r for r in control(session)['items'] if r['kind'] == 'delay')
    assert row['evidence'] == 'unknown'
    assert 'подтверждения завершения нет' in row['detail']
    session.statuses['e1']['status'] = 'completed'
    session.fact_log.append({'status': 'completed', 'request_id': 'a'})
    assert not any(r['kind'] == 'delay' for r in control(session)['items'])


def test_fresh_gps_signals_are_grouped_and_stale_gps_is_unknown_not_prediction(make_scenario):
    session = setup(make_scenario)
    gps = {'channel': 'device', 'plan_version': 1, 'plan_date': session.scenario.date,
           'config': {'stale_seconds': 60}, 'rows': [{'engineer_id': 'e1', 'tracking_state': 'active',
           'date_matches': True, 'point_age_seconds': 5, 'analysis': {'target': {'id': 'a'}},
           'alerts': [{'code': 'late', 'detail': 'ETA позже окна'}, {'code': 'stopped', 'detail': 'Остановка'}]}]}
    result = control(session, gps=gps)
    assert result['counts']['verify'] == 1
    row = next(r for r in result['items'] if r['kind'] == 'gps')
    assert row['evidence'] == 'unknown' and 'Остановка' in row['detail'] and 'ETA' in row['detail']
    gps['rows'][0]['point_age_seconds'] = 120
    row = next(r for r in control(session, gps=gps)['items'] if r['kind'] == 'data')
    assert '2 мин назад' in row['detail'] and 'ETA' not in row['detail']
    gps['plan_version'] = 0
    assert not control(session, gps=gps)['items']


def test_fleet_data_gaps_do_not_become_breakdown_probabilities(make_scenario):
    session = setup(make_scenario)
    result = control(session)
    assert not any(r['kind'] == 'vehicle' for r in result['items'])
    assert any('Риск поломки без данных не оценивается' in n for n in result['notices'])
    session.scenario = session.scenario.model_copy(update={'vehicles': [Vehicle(id='v1', label='Van', active=False)],
        'engineers': [e.model_copy(update={'vehicle_id': 'v1'}) if e.id == 'e1' else e for e in session.scenario.engineers]})
    row = next(r for r in control(session)['items'] if r['kind'] == 'vehicle')
    assert row['evidence'] == 'fact' and row['request_ids'] == ['a', 'b']


def test_attention_survives_snapshot_and_has_no_side_effects(make_scenario):
    session = setup(make_scenario)
    before = deepcopy(session.dump())
    one = dashboard(session)['attention']
    two = dashboard(capture_dashboard_session(session))['attention']
    assert one == two
    assert before == session.dump()
    assert one['plan_version'] == session.version and one['time'] == session.time


def test_urgent_response_deadline_applies_even_without_assignment(make_scenario):
    from field_router.risk_engine import EMERGENCY_RESPONSE_TARGET_MINUTES
    session = setup(make_scenario)
    session.catalog['a'] = session.catalog['a'].model_copy(update={'priority': 'urgent', 'provenance': {'received_at': '540'}})
    session.plan = session.plan.model_copy(update={'routes': []})
    row = next(r for r in control(session)['items'] if r['id'] == 'unassigned-a')
    assert row['deadline'] == min(650, 540 + EMERGENCY_RESPONSE_TARGET_MINUTES)


def test_confirmation_action_does_not_prefill_a_travel_status(make_scenario):
    session = setup(make_scenario)
    session.statuses['e1'] = {'status': 'completion_pending', 'request_id': 'a', 'time': 580}
    row = next(r for r in control(session)['items'] if r['kind'] == 'confirmation')
    assert row['action'] == 'completions'
    session.fact_log.append({'engineer_id': 'e1', **session.statuses['e1']})
    session.statuses['e1'] = {'status': 'available', 'time': 580}
    assert any(r['kind'] == 'confirmation' for r in control(session)['items'])
    session.fact_log[-1]['status'] = 'completed'
    assert not any(r['kind'] == 'confirmation' for r in control(session)['items'])


def test_near_unassigned_job_precedes_evening_overtime_even_on_urgent_route(make_scenario):
    session = setup(make_scenario)
    session.catalog['a'] = session.catalog['a'].model_copy(update={'priority': 'urgent'})
    route = session.plan.routes[0]
    session.plan = session.plan.model_copy(update={'routes': [route.model_copy(update={'visits': [route.visits[0]]})]})
    risk = {'engineer_id': 'e1', 'p_overtime': .8, 'expected_overtime_minutes': 35,
            'visits': [{'request_id': 'a', 'p_sla_breach': 0}]}
    result = control(session, [risk])
    assert result['items'][0]['id'] == 'unassigned-b'
    assert next(r for r in result['items'] if r['kind'] == 'overtime')['tier'] == 'watch'


def test_route_deviations_group_but_keep_request_evidence(make_scenario):
    session = setup(make_scenario)
    risk = {'engineer_id': 'e1', 'p_overtime': 0, 'expected_overtime_minutes': 0,
            'visits': [{'request_id': rid, 'p_sla_breach': .5, 'expected_late_minutes': 15} for rid in ['a', 'b']]}
    rows = [r for r in control(session, [risk])['items'] if r['kind'] == 'late']
    assert len(rows) == 1 and rows[0]['request_ids'] == ['a', 'b']
    assert rows[0]['requests'][0]['expected_late_minutes'] == 15


def test_old_status_requires_verification_not_a_confirmed_delay(make_scenario):
    session = setup(make_scenario)
    session.statuses['e1'] = {'status': 'available', 'time': 540}
    session.time = 605
    row = next(r for r in control(session)['items'] if r['kind'] == 'data')
    assert row['evidence'] == 'unknown'
    assert '65 мин назад' in row['detail']
    session.statuses['e1']['time'] = 605
    assert not any(r['kind'] == 'data' for r in control(session)['items'])


def test_invalid_scheduled_window_remains_visible_without_a_model_estimate(make_scenario):
    session = setup(make_scenario)
    session.catalog['a'] = session.catalog['a'].model_copy(update={'windows': [TimeWindow(start=900, end=1000)]})
    row = next(r for r in control(session)['items'] if r['id'] == 'visit-a')
    assert row['evidence'] == 'plan'
    assert row['title'] == 'Начало вне клиентского окна'
