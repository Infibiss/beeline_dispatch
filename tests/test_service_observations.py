import json

from field_router.completion import confirm_completion
from field_router.engineer import record_engineer_fact
from field_router.replanning import ScenarioSession
from field_router.risk_dashboard import dashboard
from field_router.service_observations import confirmed_service_observations
from test_replanning import demo


def raw_fact(identity, engineer, request, status, when):
    return {
        'id': identity,
        'recorded_at': identity,
        'engineer_id': engineer,
        'request_id': request,
        'status': status,
        'time': when,
    }


def test_extractor_sums_explicit_segments_and_ignores_repeated_progress(make_scenario):
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    session.fact_log = [
        raw_fact('1', 'e1', 'a', 'working', 550),
        raw_fact('2', 'e1', 'a', 'working', 555),
        raw_fact('3', 'e1', 'a', 'interrupted', 560),
        raw_fact('4', 'e2', 'a', 'working', 565),
        raw_fact('5', 'e2', 'a', 'completed', 585),
    ]
    evidence = confirmed_service_observations(session)
    assert evidence.observation_count == 1
    assert next(iter(evidence.observations.values())) == (30.0,)
    assert evidence.request_ids == {'a'}
    assert not evidence.residual_request_ids
    assert evidence.latest_observation_time == 585


def test_extractor_fails_closed_for_pending_direct_and_conflicting_records(make_scenario):
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    session.fact_log = [
        raw_fact('1', 'e1', 'a', 'working', 550),
        raw_fact('2', 'e1', 'a', 'completion_pending', 580),
        raw_fact('3', 'e2', 'b', 'completed', 600),
    ]
    evidence = confirmed_service_observations(session)
    assert evidence.observation_count == 0
    assert evidence.pending_confirmation_count == 1
    assert evidence.ignored_completion_count == 1

    session.fact_log = [
        raw_fact('1', 'e1', 'a', 'working', 550),
        raw_fact('2', 'e1', 'b', 'working', 555),
        raw_fact('3', 'e1', 'b', 'completed', 580),
    ]
    assert confirmed_service_observations(session).observation_count == 0


def test_confirmation_correction_and_restore_keep_one_effective_observation(make_scenario):
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'working', 'time': 550, 'request_id': 'a', 'remaining_minutes': 30})
    record_engineer_fact(session, {'version': session.version, 'engineer_id': 'e1',
        'status': 'completed', 'time': 580, 'request_id': 'a'})
    pending = confirmed_service_observations(session)
    assert pending.observation_count == 0 and pending.pending_confirmation_count == 1

    identity = session.state()['pending_completions'][0]['id']
    confirm_completion(session, {'version': session.version, 'fact_id': identity})
    evidence = confirmed_service_observations(session)
    assert evidence.observation_count == 1
    assert next(iter(evidence.observations.values())) == (30.0,)

    completed = next(fact for fact in session.fact_log if fact['id'] == identity)
    session.record_fact({**completed['input'], 'version': session.version,
        'fact_id': identity, 'time': 590, 'status': 'completed',
        'correction_reason': 'Уточнено фактическое время завершения'})
    corrected = confirmed_service_observations(session)
    assert corrected.observation_count == 1
    assert next(iter(corrected.observations.values())) == (40.0,)
    restored = ScenarioSession.restore(json.loads(json.dumps(session.dump())))
    assert confirmed_service_observations(restored) == corrected


def test_dashboard_uses_peer_history_without_self_leakage(make_scenario):
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    session.record_fact({'version': session.version, 'time': 550, 'engineer_id': 'e1',
        'request_id': 'a', 'status': 'working', 'remaining_minutes': 90})
    session.record_fact({'version': session.version, 'time': 640, 'engineer_id': 'e1',
        'request_id': 'a', 'status': 'completed'})

    forecast = dashboard(session)['forecast']
    assert forecast['duration_evidence']['observation_count'] == 1
    visits = {visit['request_id']: visit for route in forecast['routes'] for visit in route['visits']}
    assert visits['a']['service_duration']['observation_count'] == 0
    assert visits['a']['service_duration']['source'] == 'prior'
    assert visits['b']['service_duration']['observation_count'] == 1
    assert visits['b']['service_duration']['source'] == 'prior+empirical'
    assert visits['b']['service_duration']['p50_minutes'] > session.catalog['b'].duration_minutes


def test_residual_request_gets_request_specific_prior_key(make_scenario):
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    session.fact_log = [
        raw_fact('1', 'e1', 'a', 'working', 550),
        raw_fact('2', 'e1', 'a', 'completed', 610),
        raw_fact('3', 'e1', 'b', 'working', 620),
        raw_fact('4', 'e1', 'b', 'interrupted', 630),
    ]
    evidence = confirmed_service_observations(session)
    key = evidence.group_key(session)
    residual = session.catalog['b'].model_copy(update={'duration_minutes': 20})
    peer = session.catalog['b'].model_copy(update={'id': 'peer'})
    assert key(residual) not in evidence.observations
    assert key(peer) in evidence.observations
    assert evidence.residual_request_ids == {'b'}


def test_candidate_risk_uses_confirmed_peer_evidence_but_not_residual_fragment(make_scenario):
    scenario, plan = demo(make_scenario)
    session = ScenarioSession(scenario, plan)
    session.record_fact({'version': session.version, 'time': 550, 'engineer_id': 'e1',
        'request_id': 'a', 'status': 'working', 'remaining_minutes': 30})
    session.record_fact({'version': session.version, 'time': 580, 'engineer_id': 'e1',
        'request_id': 'a', 'status': 'completed'})

    learned = session.preview({'version': session.version,
        'event': {'id': 'learned-peer', 'type': 'replan', 'time': 580}}, time_limit=.03)
    assert learned['risk_model']['duration_evidence']['observation_count'] == 1
    learned_b = [visit for candidate in learned['candidates'] for route in candidate['risk']['routes']
                 for visit in route['visits'] if visit['request_id'] == 'b']
    assert learned_b and all(visit['service_duration']['observation_count'] == 1 for visit in learned_b)

    session.record_fact({'version': session.version, 'time': 610, 'engineer_id': 'e1',
        'request_id': 'b', 'status': 'working', 'remaining_minutes': 30})
    session.record_fact({'version': session.version, 'time': 620, 'engineer_id': 'e1',
        'request_id': 'b', 'status': 'interrupted', 'remaining_minutes': 20,
        'consumed_router': 0, 'consumed_tv_box': 0})
    residual = session.preview({'version': session.version,
        'event': {'id': 'residual-peer', 'type': 'replan', 'time': 620}}, time_limit=.03)
    residual_b = [visit for candidate in residual['candidates'] for route in candidate['risk']['routes']
                  for visit in route['visits'] if visit['request_id'] == 'b']
    assert residual_b and all(visit['service_duration']['observation_count'] == 0 for visit in residual_b)
