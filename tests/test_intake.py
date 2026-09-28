"""Incoming JSON, manual queue and automatic batch approval."""
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError

import pytest
from conftest import engineer, job
from field_router.intake import apply_autopilot
from field_router.models import Point, Skill
from field_router.models import Transport
from field_router.intake import normalize
from field_router.replanning import Event
from field_router.planning import make_plan
from field_router.replanning import ScenarioSession
from field_router.travel import synthetic_travel


TRAVEL_CONFIG = {
    'speed_kmh': {mode: 60 for mode in ('car', 'walk', 'bicycle', 'public')},
    'distance_factor': {mode: 1 for mode in ('car', 'walk', 'bicycle', 'public')},
    'boarding_minutes': {mode: 0 for mode in ('car', 'walk', 'bicycle', 'public')},
}


def session_with_plan(make_scenario):
    scenario = make_scenario([job('a', start=600, end=700, duration=20)],
        [engineer('e1'), engineer('e2')]).model_copy(update={
            'metadata': {'geography': 'synthetic_cartesian_km'},
            'travel_config': TRAVEL_CONFIG,
            'points': {'office': Point(x_km=0, y_km=0), 'a': Point(x_km=1, y_km=0)}})
    plan = make_plan(scenario, synthetic_travel(scenario), {'e1': ['a']}, 'test', 'valid')
    return ScenarioSession(scenario, plan)


def incoming(key):
    return {'id': key, 'type': 'new_request', 'anchor_id': 'a',
            'time': '09:00',
            'window_start': '12:00', 'window_end': '15:00',
            'work_kind': 'local', 'duration_minutes': 20, 'router': 0, 'tv_box': 0}


@pytest.mark.parametrize('kind', ['new_request','emergency'])
def test_new_request_constraints_survive_intake_autopilot_and_restore(make_scenario,kind):
    session=session_with_plan(make_scenario)
    if kind=='emergency':
        session.scenario=session.scenario.model_copy(update={'engineers':[
            e.model_copy(update={'skills':[Skill.LOCAL,Skill.EMERGENCY]}) for e in session.scenario.engineers]})
        session.initial_engineers={e.id:e for e in session.scenario.engineers}
    row={**incoming('constrained'),'type':kind,'required_transport':'car','window_start':'12:00','window_end':'12:30'}
    if kind=='emergency':row.pop('work_kind')
    session.set_planning_settings({'version':session.version,'mode':'autopilot','every_n_requests':1,'every_n_minutes':None})
    session.receive_requests({'requests':[row]})
    assert apply_autopilot(session,time_limit=.05) is not None
    key=('emergency:' if kind=='emergency' else 'request:')+'constrained'
    request=session.catalog[key]
    assert request.required_transport==Transport.CAR
    assert [(w.start,w.end) for w in request.windows]==[(720,750)]
    visit=next(v for route in session.plan.routes for v in route.visits if v.request_id==key)
    assert 720<=visit.start<=750
    restored=ScenarioSession.restore(session.dump())
    assert restored.catalog[key]==request
    assert restored.incoming_requests[0]['event']['required_transport']=='car'


def test_manual_emergency_uses_explicit_customer_window_and_transport(make_scenario):
    session=session_with_plan(make_scenario)
    batch=session.preview({'version':session.version,'event':{'id':'manual-window','type':'emergency',
        'time':540,'anchor_id':'a','duration_minutes':20,'window_start':800,'window_end':850,
        'required_transport':'bicycle'}},time_limit=.03)
    session.approve({'version':session.version,'batch_id':batch['id'],'candidate_id':batch['recommended_candidate_id']})
    request=session.catalog['emergency:manual-window']
    assert request.required_transport==Transport.BICYCLE
    assert [(w.start,w.end) for w in request.windows]==[(800,850)]


@pytest.mark.parametrize('fields', [
    {'window_start':'12:00'}, {'window_end':'12:00'},
    {'window_start':'12:00','window_end':'11:00'},
    {'window_start':'07:00','window_end':'08:00'}, {'required_transport':'helicopter'}])
def test_invalid_emergency_constraints_are_atomic(make_scenario,fields):
    session=session_with_plan(make_scenario)
    before=session.dump()
    with pytest.raises(ValueError):
        session.receive_requests({'requests':[{'id':'bad','type':'emergency','anchor_id':'a','time':'09:00',**fields}]})
    assert session.dump()==before


def test_legacy_emergency_default_window_and_idempotency(make_scenario):
    session=session_with_plan(make_scenario)
    row={'id':'legacy','type':'emergency','anchor_id':'a','time':'09:00'}
    item=normalize(session,row)
    assert 'required_transport' not in item['event']
    session.receive_requests({'requests':[row]})
    restored=ScenarioSession.restore(session.dump())
    assert restored.receive_requests({'requests':[row]})['duplicates']==1
    assert Event.model_validate(item['event']).window_start is None
    batch=restored.preview({'version':restored.version,'event':{'id':'legacy-preview','type':'replan','time':540}},time_limit=.03)
    restored.approve({'version':restored.version,'batch_id':batch['id'],'candidate_id':batch['recommended_candidate_id']})
    assert [(w.start,w.end) for w in restored.catalog['emergency:legacy'].windows]==[(540,1440)]


def test_transport_requirement_is_not_ignored_when_no_engineer_has_it(make_scenario):
    session=session_with_plan(make_scenario)
    session.receive_requests({'requests':[{**incoming('walk-only'),'required_transport':'walk'}]})
    batch=session.preview({'version':session.version,'event':{'id':'transport-preview','type':'replan','time':540}},time_limit=.03)
    session.approve({'version':session.version,'batch_id':batch['id'],'candidate_id':batch['recommended_candidate_id']})
    row=next(u for u in session.plan.unassigned if u.request_id=='request:walk-only')
    assert row.code=='NO_TRANSPORT'


def test_restore_enriches_legacy_reasons_without_mutating_input_or_history(make_scenario):
    from copy import deepcopy
    session=session_with_plan(make_scenario)
    scenario=session.scenario.model_copy(update={'requests':[*session.scenario.requests,
        job('not-chosen',start=720,end=800,duration=20)],'points':{
            **session.scenario.points,'not-chosen':Point(x_km=1,y_km=0)}})
    plan=make_plan(scenario,synthetic_travel(scenario),{'e1':['a']},'test','valid')
    session=ScenarioSession(scenario,plan)
    saved=session.dump()
    for view in saved['views'].values():
        view['plans'][0]['diagnostics'].pop('unassigned_diagnostics',None)
        view['plans'][0]['unassigned'][0]['reason']='Старая общая причина'
    saved['plan']['diagnostics'].pop('unassigned_diagnostics',None)
    saved['plan']['unassigned'][0]['reason']='Старая общая причина'
    saved['views'][0]=deepcopy(saved['views'][1])
    before=deepcopy(saved)
    restored=ScenarioSession.restore(saved)
    assert saved==before
    assert restored.views[0]==before['views'][0]
    assert restored.version==saved['version']
    assert 'допустимые места' in restored.plan.unassigned[0].reason


def test_json_intake_stays_visible_until_manual_approval(make_scenario):
    session = session_with_plan(make_scenario)
    result = session.receive_requests({'requests': [incoming('db-1')]})
    assert result['added'] == 1
    assert session.incoming_requests[0]['status'] == 'new'
    assert 'request:db-1' not in session.catalog
    assert session.planning_settings['mode'] == 'manual'
    assert session.receive_requests({'requests': [incoming('db-1')]})['duplicates'] == 1
    restored = ScenarioSession.restore(session.dump())
    assert restored.incoming_requests == session.incoming_requests
    batch = session.preview({'version': session.version,
        'event': {'id': 'manual-batch', 'type': 'replan', 'time': session.time}}, time_limit=.03)
    assert batch['incoming_ids'] == ['db-1']
    session.approve({'version': session.version, 'batch_id': batch['id'],
                     'candidate_id': batch['recommended_candidate_id']})
    assert 'request:db-1' in session.catalog
    assert session.incoming_requests[0]['status'] in ('assigned', 'unassigned')
    assert len(session.decision_log) == 1
    assert 'db-1' in session.decision_log[0]['request_id']


def test_autopilot_waits_for_count_then_applies_batch(make_scenario):
    session = session_with_plan(make_scenario)
    session.set_planning_settings({'version': session.version, 'mode': 'autopilot',
                                   'every_n_requests': 2, 'every_n_minutes': None})
    session.receive_requests({'requests': [incoming('db-1')]})
    assert not session.autopilot_due()
    assert apply_autopilot(session, time_limit=.03) is None
    session.receive_requests({'requests': [incoming('db-2')]})
    assert session.autopilot_due()
    applied = apply_autopilot(session, time_limit=.03)
    assert applied is not None
    assert {'request:db-1', 'request:db-2'} <= set(session.catalog)
    assert len(session.decision_log) == 2
    assert all(row['automatic'] for row in session.decision_log)
    assert session.dispatch_notifications[-1]['plan_explanation']
    assert not session.autopilot_due()
    assert ScenarioSession.restore(session.dump()).decision_log == session.decision_log


def test_autopilot_keeps_unassigned_request_actionable_after_batch(make_scenario):
    session = session_with_plan(make_scenario)
    session.set_planning_settings({'version': session.version, 'mode': 'autopilot',
                                   'every_n_requests': 1, 'every_n_minutes': None})
    impossible = {**incoming('needs-specialist'), 'work_kind': 'connection'}
    session.receive_requests({'request': impossible})
    assert apply_autopilot(session, time_limit=.03) is not None
    assert session.incoming_requests[0]['status'] == 'unassigned'
    assert session.replanning_pool()[0]['request_id'] == 'request:needs-specialist'
    assert session.replanning_pool()[0]['status'] == 'unassigned'
    assert not session.needs_replanning  # Applied plan is current, but coverage needs attention.
    assert 'без исполнителя' in session.dispatch_notifications[-1]['title']
    assert session.dispatch_notifications[-1]['unassigned_request_ids'] == ['request:needs-specialist']
    assert not session.autopilot_due()  # Do not loop forever over the same unresolved request.


def test_autopilot_retries_existing_backlog_with_new_batch(make_scenario):
    scenario = make_scenario([
        job('a', start=600, end=700, duration=20),
        job('backlog', start=720, end=900, duration=20)],
        [engineer('e1'), engineer('e2')]).model_copy(update={
            'metadata': {'geography': 'synthetic_cartesian_km'},
            'travel_config': TRAVEL_CONFIG,
            'points': {'office': Point(x_km=0, y_km=0),
                       'a': Point(x_km=1, y_km=0),
                       'backlog': Point(x_km=2, y_km=0)}})
    plan = make_plan(scenario, synthetic_travel(scenario), {'e1': ['a']}, 'test', 'valid')
    session = ScenarioSession(scenario, plan)
    assert {row.request_id for row in session.plan.unassigned} == {'backlog'}
    session.set_planning_settings({'version': session.version, 'mode': 'autopilot',
                                   'every_n_requests': 1, 'every_n_minutes': None})
    session.receive_requests({'request': {**incoming('unserviceable'), 'work_kind': 'connection'}})
    apply_autopilot(session, time_limit=.03)
    assigned = {visit.request_id for route in session.plan.routes for visit in route.visits}
    assert {'a', 'backlog'} <= assigned
    assert session.dispatch_notifications[-1]['recovered_request_ids'] == ['backlog']
    assert session.dispatch_notifications[-1]['backlog_attempted_request_ids'] == ['backlog']
    assert session.dispatch_notifications[-1]['backlog_remaining_request_ids'] == []
    assert session.dispatch_notifications[-1]['before_unassigned_count'] == 1
    assert session.dispatch_notifications[-1]['after_unassigned_count'] == 1  # One new impossible job.
    assert any(entry['request_id'] == 'backlog' and entry['change_type'] == 'recovered'
               for entry in session.decision_log)
    assert not session.autopilot_due()  # Unassigned work alone cannot create a retry loop.


def test_autopilot_does_not_evict_approved_visit_for_routine_arrival(make_scenario):
    scenario = make_scenario([job('a', start=600, end=610, duration=120)],
        [engineer('e1', skills=[Skill.LOCAL, Skill.CONNECTION])]).model_copy(update={
            'metadata': {'geography': 'synthetic_cartesian_km'},
            'travel_config': TRAVEL_CONFIG,
            'points': {'office': Point(x_km=0, y_km=0), 'a': Point(x_km=1, y_km=0)}})
    plan = make_plan(scenario, synthetic_travel(scenario), {'e1': ['a']}, 'test', 'valid')
    session = ScenarioSession(scenario, plan)
    session.set_planning_settings({'version': session.version, 'mode': 'autopilot',
                                   'every_n_requests': 1, 'every_n_minutes': None})
    session.receive_requests({'request': {**incoming('priority-new'),
        'work_kind': 'connection', 'window_start': '10:00',
        'window_end': '10:10', 'duration_minutes': 400}})
    apply_autopilot(session, time_limit=.03)
    assigned = {visit.request_id for route in session.plan.routes for visit in route.visits}
    assert 'a' in assigned
    assert session.dispatch_notifications[-1]['lost_request_ids'] == []
    assert session.incoming_requests[0]['status'] == 'unassigned'
    assert session.decision_log[-1]['previous_engineer_id'] is None


def test_existing_emergency_does_not_waive_routine_autopilot_protection(make_scenario):
    scenario = make_scenario([
        job('old-urgent', start=600, end=610, duration=60,
            skill=Skill.EMERGENCY, priority='urgent'),
        job('old-ordinary', start=720, end=730, duration=60)],
        [engineer('e1', skills=[Skill.LOCAL, Skill.EMERGENCY, Skill.CONNECTION])]).model_copy(update={
            'metadata': {'geography': 'synthetic_cartesian_km'},
            'travel_config': TRAVEL_CONFIG,
            'points': {'office': Point(x_km=0, y_km=0),
                       'old-urgent': Point(x_km=1, y_km=0),
                       'old-ordinary': Point(x_km=2, y_km=0)}})
    plan = make_plan(scenario, synthetic_travel(scenario),
                     {'e1': ['old-urgent', 'old-ordinary']}, 'test', 'valid')
    session = ScenarioSession(scenario, plan)
    session.set_planning_settings({'version': session.version, 'mode': 'autopilot',
                                   'every_n_requests': 1, 'every_n_minutes': None})
    session.receive_requests({'request': {**incoming('new-routine'),
        'anchor_id': 'old-ordinary', 'work_kind': 'connection',
        'window_start': '12:00', 'window_end': '12:10', 'duration_minutes': 300}})
    apply_autopilot(session, time_limit=.03)
    assigned = {visit.request_id for route in session.plan.routes for visit in route.visits}
    assert {'old-urgent', 'old-ordinary'} <= assigned
    assert session.incoming_requests[0]['status'] == 'unassigned'
    assert session.dispatch_notifications[-1]['lost_request_ids'] == []


def test_plain_manual_retry_recommends_no_loss_stable_plan(make_scenario):
    scenario = make_scenario([
        job('existing-urgent', start=600, end=610, duration=60,
            skill=Skill.EMERGENCY, priority='urgent'),
        job('promised-ordinary', start=720, end=730, duration=60),
        job('backlog-high', start=720, end=730, duration=60,
            skill=Skill.CONNECTION, priority='high')],
        [engineer('e1', skills=[Skill.LOCAL, Skill.CONNECTION, Skill.EMERGENCY])]).model_copy(update={
            'metadata': {'geography': 'synthetic_cartesian_km'},
            'travel_config': TRAVEL_CONFIG,
            'points': {'office': Point(x_km=0, y_km=0),
                       'existing-urgent': Point(x_km=1, y_km=0),
                       'promised-ordinary': Point(x_km=2, y_km=0),
                       'backlog-high': Point(x_km=2, y_km=0)}})
    plan = make_plan(scenario, synthetic_travel(scenario),
        {'e1': ['existing-urgent', 'promised-ordinary']}, 'test', 'valid')
    session = ScenarioSession(scenario, plan)
    batch = session.preview({'version': session.version,
        'event': {'id': 'plain-retry', 'type': 'replan', 'time': session.time}},
        time_limit=.03)
    stable = next(candidate for candidate in batch['candidates'] if candidate['id'] == 'stable')
    assert stable['facts']['previous_approved_lost'] == 0
    assert batch['recommended_candidate_id'] == 'stable'
    assert {visit['request_id'] for route in stable['plan']['routes']
            for visit in route['visits']} == {'existing-urgent', 'promised-ordinary'}
    session.approve({'version': session.version, 'batch_id': batch['id'],
                     'candidate_id': batch['recommended_candidate_id']})
    assert session.dispatch_notifications[-1]['lost_request_ids'] == []
    assert session.dispatch_notifications[-1]['backlog_remaining_request_ids'] == ['backlog-high']


def test_urgent_arrival_can_displace_only_with_explicit_audit(make_scenario):
    scenario = make_scenario([job('a', start=600, end=610, duration=120)],
        [engineer('e1', skills=[Skill.LOCAL, Skill.EMERGENCY])]).model_copy(update={
            'metadata': {'geography': 'synthetic_cartesian_km'},
            'travel_config': TRAVEL_CONFIG,
            'points': {'office': Point(x_km=0, y_km=0), 'a': Point(x_km=1, y_km=0)}})
    plan = make_plan(scenario, synthetic_travel(scenario), {'e1': ['a']}, 'test', 'valid')
    session = ScenarioSession(scenario, plan)
    session.set_planning_settings({'version': session.version, 'mode': 'autopilot',
                                   'every_n_requests': 1, 'every_n_minutes': None})
    session.receive_requests({'request': {'id': 'urgent', 'type': 'emergency',
        'anchor_id': 'a', 'time': '09:00', 'duration_minutes': 500}})
    apply_autopilot(session, time_limit=.03)
    assigned = {visit.request_id for route in session.plan.routes for visit in route.visits}
    assert 'emergency:urgent' in assigned
    assert 'a' not in assigned
    notice = session.dispatch_notifications[-1]
    assert notice['lost_request_ids'] == ['a']
    assert notice['before_assigned_count'] == 1
    assert notice['after_assigned_count'] == 1
    audit = next(entry for entry in session.decision_log if entry['request_id'] == 'a')
    assert audit['previous_engineer_id'] == 'e1'
    assert audit['status'] == 'unassigned'
    assert audit['change_type'] == 'lost_assignment'


def _assert_current_identity_consistency(session):
    assigned = {visit.request_id for route in session.plan.routes for visit in route.visits}
    frozen = {visit['request_id'] for route in session.frozen for visit in route['visits']}
    unassigned = {row.request_id for row in session.plan.unassigned}
    assert not (assigned & unassigned)
    assert {row['request_id'] for row in session.replanning_pool()
            if row['status'] == 'unassigned'} == unassigned
    assert session.plan.metrics.assigned == len(assigned)
    assert session.plan.metrics.unassigned == len(unassigned)
    for item in session.incoming_requests:
        key = ('emergency:' if item['event']['type'] == 'emergency' else 'request:') + item['id']
        if item['status'] == 'new':
            assert key not in assigned | frozen | unassigned
        elif item['status'] == 'assigned':
            assert key in assigned | frozen
        elif item['status'] == 'cancelled':
            assert key not in assigned | frozen | unassigned
        else:
            assert item['status'] == 'unassigned' and key in unassigned


@pytest.mark.parametrize('work_kind,initial_status',[
    ('local','assigned'), ('connection','unassigned')])
def test_cancelled_json_request_stays_cancelled_after_retry_and_autopilot(
        make_scenario, work_kind, initial_status):
    session=session_with_plan(make_scenario)
    original={**incoming('to-cancel'), 'work_kind':work_kind}
    session.receive_requests({'request':original})
    first=session.preview({'version':session.version,
        'event':{'id':'assign-before-cancel','type':'replan','time':session.time}},
        time_limit=.03)
    session.approve({'version':session.version,'batch_id':first['id'],
                     'candidate_id':first['recommended_candidate_id']})
    key='request:to-cancel'
    assert session.incoming_requests[0]['status']==initial_status
    _assert_current_identity_consistency(session)
    before_unassigned={row.request_id for row in session.plan.unassigned}
    cancelled=session.preview({'version':session.version,
        'event':{'id':'cancel-json','type':'cancel','time':session.time,'request_id':key}},
        time_limit=.03)
    session.approve({'version':session.version,'batch_id':cancelled['id'],
                     'candidate_id':cancelled['recommended_candidate_id']})
    assert session.incoming_requests[0]['status']=='cancelled'
    assert session.incoming_requests[0]['applied_version']==session.version
    assert key not in {request.id for request in session.scenario.requests}
    assert key not in {row['request_id'] for row in session.replanning_pool()}
    assert session.dispatch_notifications[-1]['cancelled_request_ids']==[key]
    assert session.dispatch_notifications[-1]['lost_request_ids']==[]
    cancellation_entry=next(entry for entry in session.decision_log
        if entry['request_id']==key and entry['version']==session.version)
    assert cancellation_entry['change_type']=='cancelled'
    assert cancellation_entry['status']=='cancelled'
    if key in before_unassigned:
        assert key not in session.dispatch_notifications[-1]['backlog_attempted_request_ids']
    _assert_current_identity_consistency(session)
    assert session.receive_requests({'request':original})['duplicates']==1
    session.set_planning_settings({'version':session.version,'mode':'autopilot',
        'every_n_requests':1,'every_n_minutes':None})
    session.receive_requests({'request':incoming('later')})
    apply_autopilot(session,time_limit=.03)
    assert session.incoming_requests[0]['status']=='cancelled'
    assert key not in {request.id for request in session.scenario.requests}
    assert key not in {row['request_id'] for row in session.replanning_pool()}
    _assert_current_identity_consistency(session)


def test_multiple_batches_and_retry_audit_identities_not_just_counts(make_scenario):
    session = session_with_plan(make_scenario)
    session.set_planning_settings({'version': session.version, 'mode': 'autopilot',
                                   'every_n_requests': 2, 'every_n_minutes': None})
    for key in ('first', 'second'):
        session.receive_requests({'request': incoming(key)})
    apply_autopilot(session, time_limit=.03)
    _assert_current_identity_consistency(session)
    before_assigned = {visit.request_id for route in session.plan.routes for visit in route.visits}
    before_unassigned = {row.request_id for row in session.plan.unassigned}
    before_counts = (session.plan.metrics.assigned, session.plan.metrics.unassigned)
    retry = session.preview({'version': session.version,
        'event': {'id': 'retry-without-new-data', 'type': 'replan', 'time': session.time}},
        time_limit=.03)
    session.approve({'version': session.version, 'batch_id': retry['id'],
                     'candidate_id': retry['recommended_candidate_id']})
    _assert_current_identity_consistency(session)
    after_assigned = {visit.request_id for route in session.plan.routes for visit in route.visits}
    after_unassigned = {row.request_id for row in session.plan.unassigned}
    notice = session.dispatch_notifications[-1]
    assert notice['request_ids'] == []
    assert notice['lost_request_ids'] == sorted(before_assigned & after_unassigned)
    assert notice['recovered_request_ids'] == sorted(before_unassigned & after_assigned)
    assert (notice['before_assigned_count'], notice['before_unassigned_count']) == before_counts
    assert (notice['after_assigned_count'], notice['after_unassigned_count']) == (
        session.plan.metrics.assigned, session.plan.metrics.unassigned)
    for key in notice['lost_request_ids'] + notice['recovered_request_ids']:
        assert any(entry['request_id'] == key and entry['version'] == session.version
                   for entry in session.decision_log)
    session.receive_requests({'request': {**incoming('third'), 'work_kind': 'connection'}})
    session.receive_requests({'request': incoming('fourth')})
    apply_autopilot(session, time_limit=.03)
    _assert_current_identity_consistency(session)


def test_assigned_inbox_item_records_latest_assignment_version(make_scenario):
    session = session_with_plan(make_scenario)
    session.receive_requests({'request': incoming('moving')})
    first = session.preview({'version': session.version,
        'event': {'id': 'first-assignment', 'type': 'replan', 'time': session.time}},
        time_limit=.03)
    session.approve({'version': session.version, 'batch_id': first['id'],
                     'candidate_id': first['recommended_candidate_id']})
    key = 'request:moving'
    owner = next(route.engineer_id for route in session.plan.routes
                 for visit in route.visits if visit.request_id == key)
    original_version = session.incoming_requests[0]['applied_version']
    session.report_unavailability({'version': session.version, 'engineer_id': owner,
        'start_date': session.scenario.date, 'start_time': '09:00',
        'end_date': session.scenario.date, 'end_time': '18:00',
        'reason': 'sick', 'comment': 'Нет выезда', 'location_id': 'office'})
    second = session.preview({'version': session.version,
        'event': {'id': 'owner-unavailable', 'type': 'replan', 'time': session.time}},
        time_limit=.03)
    session.approve({'version': session.version, 'batch_id': second['id'],
                     'candidate_id': second['recommended_candidate_id']})
    assert session.incoming_requests[0]['status'] == 'assigned'
    assert session.incoming_requests[0]['applied_version'] == session.version > original_version
    changed = [entry for entry in session.decision_log if entry['request_id'] == key]
    assert len(changed) == 2
    assert changed[-1]['previous_engineer_id'] == owner
    assert changed[-1]['engineer_id'] != owner
    _assert_current_identity_consistency(session)


def test_later_replan_updates_inbox_history_when_unassigned_request_recovers(make_scenario):
    session = session_with_plan(make_scenario)
    session.receive_requests({'request': incoming('retry-1')})
    reports = []
    for engineer_id in ('e1', 'e2'):
        session.report_unavailability({'version': session.version, 'engineer_id': engineer_id,
            'start_date': session.scenario.date, 'start_time': '09:00',
            'end_date': session.scenario.date, 'end_time': '18:00',
            'reason': 'sick', 'comment': 'Нет выезда', 'location_id': 'office'})
        reports.append(session.unavailability[-1]['id'])
    first = session.preview({'version': session.version,
        'event': {'id': 'no-engineers', 'type': 'replan', 'time': session.time}}, time_limit=.03)
    session.approve({'version': session.version, 'batch_id': first['id'], 'candidate_id': 'stable'})
    assert session.incoming_requests[0]['status'] == 'unassigned'
    session.revoke_engineer_report({'version': session.version, 'report_id': reports[-1],
        'reason': 'Инженер вернулся', 'location_id': 'office'})
    second = session.preview({'version': session.version,
        'event': {'id': 'engineer-returned', 'type': 'replan', 'time': session.time}}, time_limit=.03)
    session.approve({'version': session.version, 'batch_id': second['id'], 'candidate_id': 'stable'})
    assert session.incoming_requests[0]['status'] == 'assigned'
    assert session.incoming_requests[0]['applied_version'] == session.version
    history=[entry for entry in session.decision_log if entry['request_id']=='request:retry-1']
    assert [entry['status'] for entry in history]==['unassigned','assigned']
    assert history[0]['version']<history[1]['version']
    assert session.dispatch_notifications[-1]['changed_request_ids']


def test_autopilot_processes_successive_batches_and_notifies_each(make_scenario):
    session=session_with_plan(make_scenario)
    session.set_planning_settings({'version':session.version,'mode':'autopilot',
        'every_n_requests':2,'every_n_minutes':None})
    assert session.dispatch_notifications[-1]['kind']=='planning_mode'
    for key in ('first','second'):
        session.receive_requests({'request':incoming(key)})
    assert session.autopilot_due()
    apply_autopilot(session,time_limit=.03)
    first_version=session.version
    assert not session.autopilot_due()
    assert session.dispatch_notifications[-1]['request_ids']==['first','second']
    for key in ('third','fourth'):
        session.receive_requests({'request':incoming(key)})
    assert session.autopilot_due()
    apply_autopilot(session,time_limit=.03)
    assert session.version>first_version
    applied=[item for item in session.dispatch_notifications if item['kind']=='auto_applied']
    assert [item['request_ids'] for item in applied]==[['first','second'],['third','fourth']]
    for key in ('first','second','third','fourth'):
        assert any(entry['id']==key and entry['automatic'] for entry in session.decision_log)
    assert session.state()['last_applied']['incoming_count']==2
    assert not session.autopilot_due()


def test_every_applied_plan_has_notification_even_without_new_requests(make_scenario):
    session=session_with_plan(make_scenario)
    initial=len(session.dispatch_notifications)
    batch=session.preview({'version':session.version,
        'event':{'id':'recheck','type':'replan','time':session.time}},time_limit=.03)
    session.approve({'version':session.version,'batch_id':batch['id'],'candidate_id':'stable'})
    assert len(session.dispatch_notifications)==initial+1
    assert session.dispatch_notifications[-1]['kind']=='manual_applied'
    assert session.dispatch_notifications[-1]['version']==session.version


def test_time_threshold_and_invalid_batch_are_atomic(make_scenario):
    session = session_with_plan(make_scenario)
    session.set_planning_settings({'version': session.version, 'mode':'autopilot',
        'every_n_requests':None, 'every_n_minutes':5})
    session.receive_requests({'request':incoming('db-1')})
    assert not session.autopilot_due()
    received=datetime.fromisoformat(session.incoming_requests[0]['received_at'])
    assert not session.autopilot_due(received+timedelta(minutes=5)-timedelta(milliseconds=1))
    assert session.autopilot_due(received+timedelta(minutes=5))
    assert session.autopilot_due(received+timedelta(minutes=5,seconds=1))
    before=session.dump()
    with pytest.raises(ValueError):
        session.receive_requests({'requests':[incoming('db-2'),{**incoming('bad'),'anchor_id':'missing'}]})
    assert session.dump()==before
    with pytest.raises(ValueError,match='Адрес не совпадает'):
        session.receive_requests({'request':{**incoming('wrong-address'),
            'address':'Москва, чужой адрес 99'}})
    assert session.dump()==before


def test_autopilot_failure_keeps_queue_and_notifies(make_scenario):
    session=session_with_plan(make_scenario)
    session.set_planning_settings({'version':session.version,'mode':'autopilot',
        'every_n_requests':1,'every_n_minutes':None})
    session.receive_requests({'request':incoming('db-1')})
    session.record_autopilot_failure('Требуется ручная проверка.')
    assert session.incoming_requests[0]['status']=='new'
    assert session.dispatch_notifications[-1]['kind']=='auto_error'
    assert not session.autopilot_due()
    retry=datetime.fromisoformat(session.autopilot_last_failure_at)+timedelta(seconds=61)
    assert session.autopilot_due(retry)


def test_http_intake_autopilot_and_idempotent_retry(make_scenario,tmp_path):
    from field_router.scenario_server import create_server
    path=tmp_path/'state.json'
    server=create_server(session_with_plan(make_scenario),path,port=0,time_limit=.03,
                         intake_token='test-integration-token')
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    opener=build_opener(ProxyHandler({}))
    root=f'http://127.0.0.1:{server.server_port}'
    def post(route,data,token=None):
        headers={'Content-Type':'application/json'}
        if token:headers['X-Intake-Token']=token
        with opener.open(Request(root+route,data=json.dumps(data).encode(),headers=headers)) as response:
            return json.load(response)
    def get(route):
        with opener.open(root+route) as response:return json.load(response)
    try:
        current=get('/api/state')
        post('/api/inbox/settings',{'version':current['version'],'mode':'autopilot',
             'every_n_requests':2,'every_n_minutes':None})
        first=post('/api/inbox/requests',{'request':incoming('db-1')},'test-integration-token')
        assert first['added']==1
        assert get('/api/inbox')['incoming_requests'][0]['status']=='new'
        retry=post('/api/inbox/requests',{'request':incoming('db-1')},'test-integration-token')
        assert retry['duplicates']==1 and retry['added']==0
        with pytest.raises(HTTPError) as error:
            post('/api/inbox/requests',{'request':{**incoming('db-1'),'router':3}},'test-integration-token')
        assert error.value.code==400
        post('/api/inbox/requests',{'request':incoming('db-2')},'test-integration-token')
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            result=get('/api/inbox')
            if len(result['decision_log'])==2:break
            time.sleep(.1)
        assert len(result['decision_log'])==2
        assert all(row['automatic'] for row in result['decision_log'])
        assert all(row['status'] in ('assigned','unassigned') for row in result['incoming_requests'])
        assert json.loads(path.read_text(encoding='utf-8'))['version']==result['version']
    finally:
        server.shutdown();server.server_close();worker.join()


def test_http_autopilot_time_threshold_fires_without_new_arrival(make_scenario,tmp_path):
    from field_router.scenario_server import create_server
    session=session_with_plan(make_scenario)
    session.set_planning_settings({'version':session.version,'mode':'autopilot',
        'every_n_requests':None,'every_n_minutes':1})
    session.receive_requests({'request':incoming('timed')})
    session.incoming_requests[0]['received_at']=(datetime.now(timezone.utc)-
        timedelta(seconds=59)).isoformat()
    server=create_server(session,tmp_path/'timed.json',port=0,time_limit=.03,disable_gps=True)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    opener=build_opener(ProxyHandler({}))
    try:
        root=f'http://127.0.0.1:{server.server_port}'
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            with opener.open(root+'/api/inbox') as response:result=json.load(response)
            if result['incoming_requests'][0]['status']!='new':break
            time.sleep(.15)
        assert result['incoming_requests'][0]['status'] in ('assigned','unassigned')
        assert result['decision_log'][0]['automatic']
        assert result['dispatch_notifications'][-1]['kind']=='auto_applied'
        assert result['last_applied']['version']==result['version']
    finally:
        server.shutdown();server.server_close();worker.join()


def test_manual_approval_later_adds_llm_wording_to_verified_decision(make_scenario,tmp_path):
    from field_router.scenario_server import create_server
    class Explainer:
        def explain(self,batch,candidate_id):
            return {'source':'yandexgpt','text':'Компромисс плана по проверенным показателям.'}
    session=session_with_plan(make_scenario)
    session.receive_requests({'request':incoming('manual-llm')})
    path=tmp_path/'manual-llm.json'
    server=create_server(session,path,port=0,time_limit=.03,disable_gps=True,explainer=Explainer())
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    opener=build_opener(ProxyHandler({}))
    root=f'http://127.0.0.1:{server.server_port}'
    def post(route,data):
        with opener.open(Request(root+route,data=json.dumps(data).encode(),
                headers={'Content-Type':'application/json'})) as response:return json.load(response)
    try:
        batch=post('/api/preview',{'version':session.version,
            'event':{'id':'manual-llm-plan','type':'replan','time':session.time}})
        post('/api/approve',{'version':session.version,'batch_id':batch['id'],
            'candidate_id':batch['recommended_candidate_id']})
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with opener.open(root+'/api/inbox') as response:result=json.load(response)
            if result['decision_log'][0]['plan_explanation']=='Компромисс плана по проверенным показателям.':break
            time.sleep(.05)
        assert result['decision_log'][0]['explanation_source']=='verified_rule'
        assert result['decision_log'][0]['plan_explanation']=='Компромисс плана по проверенным показателям.'
        assert 'YandexGPT' in result['decision_log'][0]['llm_notice']
        assert result['dispatch_notifications'][-1]['kind']=='explanation_ready'
        assert json.loads(path.read_text(encoding='utf-8'))['decision_log'][0]['plan_explanation']==result['decision_log'][0]['plan_explanation']
    finally:
        server.shutdown();server.server_close();worker.join()


def test_machine_intake_token_does_not_open_dispatcher_api(make_scenario,tmp_path):
    from field_router.auth import AccountStore
    from field_router.scenario_server import create_server
    accounts=AccountStore(tmp_path/'accounts.sqlite3')
    accounts.bootstrap('test-password-1234')
    server=create_server(session_with_plan(make_scenario),tmp_path/'state.json',
        port=0,accounts=accounts,intake_token='exact-test-token',disable_gps=True)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    opener=build_opener(ProxyHandler({}))
    root=f'http://127.0.0.1:{server.server_port}'
    def call(route,token):
        headers={'Content-Type':'application/json'}
        if token:headers['X-Intake-Token']=token
        return opener.open(Request(root+route,data=json.dumps({'request':incoming('db-1')}).encode(),headers=headers))
    try:
        with pytest.raises(HTTPError) as missing:call('/api/inbox/requests',None)
        assert missing.value.code==401
        with pytest.raises(HTTPError) as wrong:call('/api/inbox/requests','wrong')
        assert wrong.value.code==401
        with call('/r/current/api/inbox/requests','exact-test-token') as response:
            assert json.load(response)['added']==1
        with pytest.raises(HTTPError) as other:
            call('/api/inbox/settings','exact-test-token')
        assert other.value.code==401
    finally:
        server.shutdown();server.server_close();worker.join()


def test_json_uses_real_road_point_and_rejects_wrong_address(tmp_path):
    from field_router.regions import load_session
    comparison=Path(__file__).resolve().parents[1]/'data'/'demo-road'/'Восток.json'
    session=load_session(comparison,tmp_path/'fresh.json')
    anchor=next(iter(session.catalog))
    address=session.catalog[anchor].address
    row={'id':'db-road-1','type':'new_request','anchor_id':anchor,'address':address,
         'time':session.time,'window_start':max(600,session.time),
         'window_end':min(1440,max(660,session.time+120)),
         'work_kind':'local','duration_minutes':30,'router':0,'tv_box':0}
    assert session.receive_requests({'request':row})['added']==1
    assert session.incoming_requests[0]['address']==address
    with pytest.raises(ValueError,match='Адрес не совпадает'):
        session.receive_requests({'request':{**row,'id':'db-road-2','address':'Москва, другой объект 99'}})
