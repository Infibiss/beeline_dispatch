"""An explicit emergency call may open a bounded shift for a day-off worker."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from conftest import engineer, job
from field_router.engineer import engineer_state
from field_router.models import Point, Skill
from field_router.planning import make_plan
from field_router.replanning import ScenarioSession
from field_router.risk_dashboard import dashboard
from field_router.travel import synthetic_travel


TRAVEL_CONFIG = {
    'speed_kmh': {mode: 60 for mode in ('car', 'walk', 'bicycle', 'public')},
    'distance_factor': {mode: 1 for mode in ('car', 'walk', 'bicycle', 'public')},
    'boarding_minutes': {mode: 0 for mode in ('car', 'walk', 'bicycle', 'public')},
}


def session_with_day_off_worker(make_scenario):
    scenario = make_scenario(
        [job('a', start=600, end=700, duration=20)],
        [engineer('e1'), engineer('e2', off_duty=True,
                 skills=[Skill.LOCAL, Skill.EMERGENCY])],
    ).model_copy(update={'metadata': {'geography': 'synthetic_cartesian_km'},
                         'travel_config': TRAVEL_CONFIG,
                         'points': {'office': Point(x_km=0, y_km=0),
                                    'a': Point(x_km=1, y_km=0)}})
    plan = make_plan(scenario, synthetic_travel(scenario), {'e1': ['a']}, 'test', 'valid')
    return ScenarioSession(scenario, plan)


def emergency_payload(session, **overrides):
    payload = {'version': session.version,
               'event': {'id': 'late-emergency', 'type': 'emergency', 'time': 1100,
                         'anchor_id': 'a', 'duration_minutes': 20},
               'call_off_duty_engineer_ids': ['e2'],
               'call_off_duty_confirmed': True,
               'call_off_duty_reason': 'Инженер согласился помочь после смены',
               'call_off_duty_window_start': 1110,
               'call_off_duty_window_end': 1200}
    payload.update(overrides)
    return payload


def test_manual_emergency_window_after_original_shift_is_usable_and_persisted(make_scenario):
    session = session_with_day_off_worker(make_scenario)
    event = emergency_payload(session)['event']
    ordinary = session.preview({'version': session.version, 'event': event}, time_limit=.03)
    assert all(candidate['emergency']['assigned'] is False for candidate in ordinary['candidates'])
    with pytest.raises(ValueError, match='окно выхода'):
        no_window = emergency_payload(session)
        no_window.pop('call_off_duty_window_start')
        no_window.pop('call_off_duty_window_end')
        session.preview(no_window, time_limit=.03)
    batch = session.preview(emergency_payload(session), time_limit=.03)
    assert batch['off_duty_escalation']['window_start'] == 1110
    assert batch['off_duty_escalation']['window_end'] == 1200
    selected = next(candidate for candidate in batch['candidates']
                    if candidate['emergency']['engineer_id'] == 'e2')
    assert selected['emergency']['start'] >= 1110
    assert selected['emergency']['end'] <= 1200
    assert session.scenario.engineers[1].off_duty is True
    session.approve({'version': session.version, 'batch_id': batch['id'],
                     'candidate_id': selected['id']})
    activated = next(e for e in session.scenario.engineers if e.id == 'e2')
    assert activated.off_duty is False
    assert activated.shift.start == 1110
    assert activated.shift.end == 1200
    mobile = engineer_state(session, 'e2')
    assert mobile['engineer']['off_duty'] is False
    assert mobile['engineer']['shift'] == {'start': 1110, 'end': 1200}
    assert session.initial_engineers['e2'].off_duty is True
    restored = ScenarioSession.restore(session.dump())
    assert next(e for e in restored.scenario.engineers if e.id == 'e2').shift.end == 1200
    assert restored.history[-1]['policy']['off_duty_escalation']['activated_ids'] == ['e2']


@pytest.mark.parametrize('changes,match', [
    ({'call_off_duty_window_start': None}, 'обе границы'),
    ({'call_off_duty_window_start': True}, 'Некорректное окно'),
    ({'call_off_duty_window_start': 1090}, 'раньше времени аварии'),
    ({'call_off_duty_window_end': 1110}, 'Некорректное окно'),
])
def test_manual_window_must_be_valid(make_scenario, changes, match):
    session = session_with_day_off_worker(make_scenario)
    with pytest.raises(ValueError, match=match):
        session.preview(emergency_payload(session, **changes), time_limit=.03)


def test_reported_absence_blocks_the_custom_call_window(make_scenario):
    session = session_with_day_off_worker(make_scenario)
    session.report_unavailability({
        'version': session.version, 'engineer_id': 'e2',
        'start_date': session.scenario.date, 'start_time': '18:40',
        'end_date': session.scenario.date, 'end_time': '19:10',
        'reason': 'personal', 'comment': 'Выезд невозможен в это время'})
    with pytest.raises(ValueError, match='подтверждённой недоступности'):
        session.preview(emergency_payload(session), time_limit=.03)


def test_called_worker_can_report_absence_in_new_shift_and_release_visit(make_scenario):
    session = session_with_day_off_worker(make_scenario)
    batch = session.preview(emergency_payload(session), time_limit=.03)
    selected = next(candidate for candidate in batch['candidates']
                    if candidate['emergency']['engineer_id'] == 'e2')
    session.approve({'version': session.version, 'batch_id': batch['id'],
                     'candidate_id': selected['id']})
    assert dashboard(session)['summary']['engineers'] == 1
    session.report_unavailability({
        'version': session.version, 'engineer_id': 'e2',
        'start_date': session.scenario.date, 'start_time': '18:20',
        'end_date': session.scenario.date, 'end_time': '19:00',
        'reason': 'sick', 'comment': 'Выехать после согласования не смогу'})
    assert session.needs_replanning
    assert session.pending_reassignment_ids('e2') == ['emergency:late-emergency']
    assert engineer_state(session, 'e2')['tasks'][-1]['status'] == 'pending_replan'
    assert dashboard(session)['summary']['engineers'] == 0
    row = next(row for row in session.replanning_pool()
               if row['request_id'] == 'emergency:late-emergency')
    assert row['reason'] == 'Болезнь'
    replan = session.preview({'version': session.version,
        'event': {'id': 'after-absence', 'type': 'replan', 'time': session.time}}, time_limit=.03)
    for candidate in replan['candidates']:
        assert not any(v['request_id'] == 'emergency:late-emergency'
                       for route in candidate['plan']['routes'] if route['engineer_id'] == 'e2'
                       for v in route['visits'])


def test_approved_call_window_survives_a_temporary_route_cutoff(make_scenario):
    session = session_with_day_off_worker(make_scenario)
    batch = session.preview(emergency_payload(session), time_limit=.03)
    selected = next(candidate for candidate in batch['candidates']
                    if candidate['emergency']['engineer_id'] == 'e2')
    session.approve({'version': session.version, 'batch_id': batch['id'],
                     'candidate_id': selected['id']})
    session.report_unavailability({
        'version': session.version, 'engineer_id': 'e2',
        'start_date': session.scenario.date, 'start_time': '19:00',
        'end_date': session.scenario.date, 'end_time': '19:10',
        'reason': 'personal', 'comment': 'Короткий перерыв'})
    interruption = session.preview({'version': session.version,
        'event': {'id': 'temporary-cutoff', 'type': 'replan', 'time': session.time}}, time_limit=.03)
    session.approve({'version': session.version, 'batch_id': interruption['id'],
                     'candidate_id': 'stable'})
    report_id = session.unavailability[-1]['id']
    session.revoke_engineer_report({'version': session.version, 'report_id': report_id,
                                    'reason': 'Перерыв отменён'})
    restored = ScenarioSession.restore(session.dump())
    assert restored._availability_shift('e2').end == 1200
    new_batch = restored.preview({'version': restored.version,
        'event': {'id': 'after-revoke', 'type': 'replan', 'time': restored.time}},
        time_limit=.03)
    engineer = next(e for e in restored.batches[new_batch['id']]['scenario'].engineers
                    if e.id == 'e2')
    assert engineer.shift.end == 1200


def test_ordinary_request_cannot_use_manual_day_off_window(make_scenario):
    session = session_with_day_off_worker(make_scenario)
    payload = emergency_payload(session)
    payload['event'] = {'id': 'ordinary', 'type': 'new_request', 'time': 1100,
                        'anchor_id': 'a', 'duration_minutes': 20,
                        'work_kind': 'local', 'window_start': 1110, 'window_end': 1200}
    with pytest.raises(ValueError, match='только как ручная эскалация'):
        session.preview(payload, time_limit=.03)


def test_future_report_revoke_accepts_valid_optional_location(make_scenario):
    session = session_with_day_off_worker(make_scenario)
    session.report_unavailability({
        'version': session.version, 'engineer_id': 'e1',
        'start_date': session.scenario.date, 'start_time': '14:00',
        'end_date': session.scenario.date, 'end_time': '15:00',
        'reason': 'personal', 'comment': 'Визит к врачу'})
    report_id = session.unavailability[0]['id']
    with pytest.raises(ValueError, match='Неизвестная текущая точка'):
        session.revoke_engineer_report({'version': session.version,
            'report_id': report_id, 'reason': 'Встреча отменена',
            'location_id': 'not-a-location'})
    session.revoke_engineer_report({'version': session.version,
        'report_id': report_id, 'reason': 'Встреча отменена',
        'location_id': 'office'})
    assert session.unavailability[0]['revoked']


def test_pool_labels_the_period_that_reaches_the_visit(make_scenario):
    session = session_with_day_off_worker(make_scenario)
    first = {'version': session.version, 'engineer_id': 'e1',
             'start_date': session.scenario.date, 'end_date': session.scenario.date,
             'start_time': '12:00', 'end_time': '12:30',
             'reason': 'personal', 'comment': 'Поздняя встреча'}
    session.report_unavailability(first)
    second = dict(first, version=session.version, start_time='09:10',
                  end_time='09:30', comment='Ранний визит к врачу')
    session.report_unavailability(second)
    row = next(row for row in session.replanning_pool() if row['request_id'] == 'a')
    assert row['report_id'] == session.unavailability[1]['id']
    assert row['comment'] == 'Ранний визит к врачу'


def test_dispatcher_ui_emits_explicit_window_only_for_confirmed_emergency():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for client contract checks')
    script = Path(__file__).resolve().parents[1] / 'src/field_router/static/scenarios.js'
    js = "const p=core.eventPayload(1,{id:'e',type:'emergency',time:'18:20',target:'a',duration:'20',router:'0',offDutyEngineerId:'e2',offDutyConfirmed:true,offDutyReason:'Крупная авария',offDutyWindowStart:'18:30',offDutyWindowEnd:'20:00'});console.log(JSON.stringify(p));"
    result = subprocess.run([node, '-e', 'const core=require(' + json.dumps(str(script)) + ');' + js],
                            capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    assert payload['call_off_duty_window_start'] == 1110
    assert payload['call_off_duty_window_end'] == 1200
    html = script.with_name('scenarios.html').read_text(encoding='utf-8')
    assert 'id="call-off-duty-window-start"' in html
    assert 'id="call-off-duty-window-end"' in html
