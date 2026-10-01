"""Synthetic evidence only: GPS assumptions never impersonate employee facts."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from field_router.gps.autopilot import GpsAutopilot, record_failure, stage_estimates, travel_estimate, trip_key
from field_router.gps.context import project_context
from field_router.intake import apply_autopilot
from field_router.replanning import ScenarioSession
from test_intake import session_with_plan, incoming


def ready_session(make_scenario):
    session = session_with_plan(make_scenario)
    session.record_fact({'version':session.version,'engineer_id':'e1','status':'traveling',
                         'request_id':'a','time':595,'location_id':'office','remaining_minutes':5})
    session.set_planning_settings({'version':session.version,'mode':'autopilot','every_n_requests':3,
                                   'every_n_minutes':None})
    batch=session.preview({'version':session.version,'event':{'id':'start','type':'replan','time':595}},time_limit=.02)
    session.approve({'version':session.version,'batch_id':batch['id'],'candidate_id':batch['recommended_candidate_id']})
    return session


class Evidence:
    enabled=True
    config=SimpleNamespace(stale_seconds=90,max_accuracy_m=100,utc_offset_minutes=180)

    def __init__(self, session):
        self.session=session
        self.now=datetime(2026,8,17,7,0,tzinfo=timezone.utc).timestamp()
        self.row={'engineer_id':'e1','channel':'device','tracking_state':'active','date_matches':True,
            'quality_reason':None,'clock':{'minute':600},'alerts':[],
            'analysis':{'target':{'id':'a','basis':'confirmed'},
                        'eta':{'low_minutes':8,'high_minutes':12,'route':{'kind':'road'}}}}
        self.point={'lat':55.7,'lon':37.7,'accuracy_m':8,'is_mock':False}
        self.refresh()

    def refresh(self, seconds=0):
        self.now+=seconds
        self.row['position']={**self.point,'measured_at_ms':self.now*1000}
        self.samples=[{'accepted':True,'point':{**self.point,'measured_at_ms':(self.now-offset)*1000}}
                      for offset in (40,20,0)]

    def clock(self):return self.now
    def context(self):return project_context(self.session)
    def snapshot(self, channel='device'):
        return {'channel':channel,'plan_version':self.session.version,'rows':[deepcopy(self.row)]}
    def trace(self,eid,channel):return {'samples':self.samples}


def collect_ready(session, gps=None, policy=None):
    gps=gps or Evidence(session);policy=policy or GpsAutopilot()
    assert policy.collect(session,gps)==[]
    gps.refresh(31)
    proposals=policy.collect(session,gps)
    assert len(proposals)==1
    return gps,policy,proposals


def test_repeated_precise_road_delay_requires_distinct_analyses(make_scenario):
    session=ready_session(make_scenario);before=deepcopy(session.dump())
    gps=Evidence(session);policy=GpsAutopilot()
    assert policy.collect(session,gps)==[]
    gps.now+=31
    assert policy.collect(session,gps)==[]  # same point is not new evidence
    gps.refresh()
    proposals=policy.collect(session,gps)
    assert len(proposals)==1
    assert policy.revalidate(session,gps,proposals)
    assert session.dump()==before
    assert not any(k in proposals[0] for k in ('lat','lon','position'))


@pytest.mark.parametrize('case', ['manual','pending_absence','unavailable','working','stale','imprecise','mock',
    'demo','quality','date','fallback','target','reacquired','single','rejected','small_delay'])
def test_bad_or_unconfirmed_evidence_never_replans(make_scenario,case):
    session=ready_session(make_scenario);gps=Evidence(session)
    if case=='manual':session.planning_settings['mode']='manual'
    if case=='pending_absence':session.pending_report_ids=['absence-needs-review']
    if case in ('unavailable','working'):session.statuses['e1']['status']=case
    if case=='stale':gps.row['position']['measured_at_ms']-=91000
    if case=='imprecise':gps.row['position']['accuracy_m']=80
    if case=='mock':gps.row['position']['is_mock']=True
    if case=='demo':gps.row['channel']='demo'
    if case=='quality':gps.row['quality_reason']='source_conflict'
    if case=='date':gps.row['date_matches']=False
    if case=='fallback':gps.row['analysis']['eta']['route']['kind']='distance_estimate'
    if case=='target':gps.row['analysis']['target']['basis']='plan'
    if case=='reacquired':gps.row['alerts']=[{'code':'reacquired'}]
    if case=='single':gps.samples=gps.samples[-1:]
    if case=='rejected':gps.samples[0]['accepted']=False
    if case=='small_delay':gps.row['analysis']['eta']['low_minutes']=4
    policy=GpsAutopilot()
    assert policy.collect(session,gps)==[]
    gps.now+=31
    assert policy.collect(session,gps)==[]


def test_overlay_changes_trip_without_changing_human_facts(make_scenario):
    session=ready_session(make_scenario);gps,policy,proposals=collect_ready(session)
    original_facts=deepcopy(session.fact_log);original_statuses=deepcopy(session.statuses)
    candidate=ScenarioSession.restore(session.dump())
    applied=apply_autopilot(candidate,time_limit=.02,gps_proposals=proposals)
    assert applied is not None
    assert applied[0]['gps_estimates'][0]['source']=='gps_estimate'
    assert candidate.fact_log==original_facts and candidate.statuses==original_statuses
    frozen=next(v for r in candidate.frozen for v in r['visits'] if v['request_id']=='a')
    assert frozen['arrival']==612 and frozen['travel_minutes']==12
    assert 'GPS' in frozen['explanation']
    assert candidate.gps_autopilot_history[-1]['applied_version']==candidate.version
    assert 'GPS' in candidate.dispatch_notifications[-1]['text']
    restored=ScenarioSession.restore(candidate.dump())
    assert restored.gps_travel_estimates==candidate.gps_travel_estimates
    assert restored.gps_autopilot_history==candidate.gps_autopilot_history
    gps.session=restored;gps.row['analysis']['eta']['low_minutes']=30
    assert GpsAutopilot().collect(restored,gps)==[]  # durable cooldown across restart


def test_overlay_invalidated_by_human_fact_absence_and_time(make_scenario):
    session=ready_session(make_scenario);gps,policy,proposals=collect_ready(session)
    stage_estimates(session,proposals)
    fact=session.statuses['e1']
    assert travel_estimate(session,'e1',fact,600)
    assert travel_estimate(session,'e1',fact,603) is None
    changed={**fact,'id':'new-human-report'}
    assert travel_estimate(session,'e1',changed,600) is None
    session.unavailability.append({'id':'absence','engineer_id':'e1','start_date':session.scenario.date,
        'end_date':session.scenario.date,'start_time':'10:00','end_time':'10:30','reason':'illness',
        'source':'engineer','location_id':'office'})
    assert travel_estimate(session,'e1',fact,600) is None


def test_revalidate_rejects_newer_human_action_and_worsening_estimate(make_scenario):
    session=ready_session(make_scenario);gps,policy,proposals=collect_ready(session)
    gps.row['analysis']['eta']['high_minutes']=20
    assert not policy.revalidate(session,gps,proposals)
    gps.row['analysis']['eta']['high_minutes']=12
    session.version+=1
    assert not policy.revalidate(session,gps,proposals)


def test_gps_solver_failure_keeps_original_session_and_intake_still_works(make_scenario):
    session=ready_session(make_scenario);gps,policy,proposals=collect_ready(session)
    before=deepcopy(session.dump());candidate=ScenarioSession.restore(session.dump())
    proposals[0]['remaining_minutes']=200
    with pytest.raises(ValueError):apply_autopilot(candidate,time_limit=.02,gps_proposals=proposals)
    assert session.dump()==before
    policy.failed(proposals)
    assert policy.collect(session,gps)==[]
    session.receive_requests({'requests':[{**incoming(key),'time':'10:00'} for key in ('one','two','three')]})
    # The old human trip still says five minutes remain. At 10:00 it already
    # misses the published 10:00 start: intake cannot silently move that promise
    # after a rejected GPS estimate. The dispatcher must resolve the facts.
    before=deepcopy(session.dump())
    with pytest.raises(ValueError,match='[Пп]ересч'):
        apply_autopilot(session,time_limit=.02)
    assert session.dump()==before
    assert all(item['status']=='new' for item in session.incoming_requests)
    batch=session.preview({'version':session.version,'event':{'id':'manual-repair','type':'replan','time':600}},time_limit=.02)
    session.approve({'version':session.version,'batch_id':batch['id'],'candidate_id':'stable'})
    assert session.gps_travel_estimates=={}


def test_non_late_or_bad_measurement_resets_confirmation_streak(make_scenario):
    session=ready_session(make_scenario);gps=Evidence(session);policy=GpsAutopilot()
    assert policy.collect(session,gps)==[]
    gps.refresh(31);gps.row['analysis']['eta']['low_minutes']=2
    assert policy.collect(session,gps)==[]
    gps.refresh(31);gps.row['analysis']['eta']['low_minutes']=8
    assert policy.collect(session,gps)==[]
    gps.refresh(31)
    assert policy.collect(session,gps)
    gps.row['quality_reason']='jump'
    assert policy.collect(session,gps)==[]
    gps.refresh(31);gps.row['quality_reason']=None
    assert policy.collect(session,gps)==[]


def test_expired_or_off_shift_proposal_cannot_commit(make_scenario):
    session=ready_session(make_scenario);gps,policy,proposals=collect_ready(session)
    gps.refresh(91)
    assert not policy.revalidate(session,gps,proposals)
    gps.row['clock']['minute']=1080
    assert policy.collect(session,gps)==[]


def test_forecast_does_not_explicitly_confirm_arrival_or_work(make_scenario):
    session=ready_session(make_scenario);gps,policy,proposals=collect_ready(session)
    apply_autopilot(session,time_limit=.02,gps_proposals=proposals)
    fact=session.statuses['e1']
    assert fact['status']=='traveling'
    assert fact['location_id']=='office'
    assert all(f['status'] not in ('working','completed') for f in session.fact_log)
    # A new human report supersedes the bound estimate, even on the same trip.
    session.record_fact({'version':session.version,'engineer_id':'e1','status':'traveling',
        'time':601,'request_id':'a','location_id':'office','remaining_minutes':4})
    assert travel_estimate(session,'e1',session.statuses['e1'],601) is None


def test_fresh_human_travel_fact_does_not_require_manual_preview_before_gps(make_scenario):
    session=session_with_plan(make_scenario)
    session.set_planning_settings({'version':session.version,'mode':'autopilot','every_n_requests':3,
                                   'every_n_minutes':None})
    session.record_fact({'version':session.version,'engineer_id':'e1','status':'traveling',
                         'request_id':'a','time':595,'location_id':'office','remaining_minutes':5})
    assert session.needs_replanning
    gps,policy,proposals=collect_ready(session)
    assert apply_autopilot(session,time_limit=.02,gps_proposals=proposals)
    assert not session.needs_replanning


def test_gps_batch_includes_new_requests_below_intake_threshold(make_scenario):
    session=ready_session(make_scenario)
    session.receive_requests({'request':{**incoming('new-job'),'time':'10:00'}})
    assert not session.autopilot_due()
    gps,policy,proposals=collect_ready(session)
    batch,_=apply_autopilot(session,time_limit=.02,gps_proposals=proposals)
    assert batch['incoming_ids']==['new-job']
    assert session.incoming_requests[0]['status']!='new'


def test_previously_applied_gps_estimate_does_not_waive_ordinary_intake_preservation(make_scenario, monkeypatch):
    session=ready_session(make_scenario)
    gps,policy,proposals=collect_ready(session)
    apply_autopilot(session,time_limit=.02,gps_proposals=proposals)
    assert session.gps_travel_estimates
    session.set_planning_settings({'version':session.version,'mode':'autopilot',
        'every_n_requests':1,'every_n_minutes':None})
    session.receive_requests({'request':{**incoming('ordinary-after-gps'),'time':600}})
    def forbidden(*args,**kwargs):
        pytest.fail('An already applied GPS overlay is not a new operational trigger')
    monkeypatch.setattr('field_router.replanning.get_solver',forbidden)
    batch,_=apply_autopilot(session,time_limit=.02)
    assert batch['insertion_policy']=='preserve_existing_assignments_and_starts'
    assert next(v['start'] for r in batch['frozen'] for v in r['visits'] if v['request_id']=='a')==612


def test_failure_diagnostic_preserves_plan_previews_and_intake_retry(make_scenario):
    session=ready_session(make_scenario);gps,policy,proposals=collect_ready(session)
    batch=session.preview({'version':session.version,'event':{'id':'review','type':'replan','time':595}},time_limit=.02)
    version=session.version;plan=deepcopy(session.plan)
    record_failure(session,proposals)
    assert session.version==version and session.plan==plan
    assert batch['id'] in session.batches
    assert session.autopilot_last_failure_at is None
    assert session.gps_autopilot_history==[] and session.gps_travel_estimates=={}
    assert session.dispatch_notifications[-1]['kind']=='auto_gps_error'


def test_delayed_repeated_samples_do_not_replace_thirty_seconds_of_new_evidence(make_scenario):
    session=ready_session(make_scenario);gps=Evidence(session);policy=GpsAutopilot()
    assert policy.collect(session,gps)==[]
    gps.refresh(31)
    gps.row['position']['measured_at_ms']-=30000
    assert policy.collect(session,gps)==[]


def test_stationary_browser_thirty_second_polling_with_jitter_is_usable(make_scenario):
    session=ready_session(make_scenario);gps=Evidence(session);policy=GpsAutopilot()
    def browser_series():
        gps.samples=[{'accepted':True,'point':{**gps.point,'measured_at_ms':(gps.now-offset)*1000}}
                     for offset in (63,32,1)]
        gps.row['position']=gps.samples[-1]['point']
    browser_series()
    assert policy.collect(session,gps)==[]
    gps.refresh(32);browser_series()
    assert policy.collect(session,gps)
