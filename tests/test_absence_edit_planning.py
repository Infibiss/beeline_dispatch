"""Editing an approved absence rebuilds capacity from the agreed working hours."""
from datetime import date, timedelta

from field_router.models import TimeWindow
from field_router.replanning import Event, ScenarioSession
from test_engineer_unavailability import session_with_route, report, preview_replan
from test_off_duty_emergency_window import session_with_day_off_worker, emergency_payload


def approve(session):
    batch = preview_replan(session, event_id=f'replan-v{session.version}')
    session.approve({'version': session.version, 'batch_id': batch['id'],
                     'candidate_id': 'stable'})


def edit(session, **changes):
    period = session.unavailability[0]
    payload = {key: period[key] for key in (
        'engineer_id', 'start_date', 'start_time', 'end_date', 'end_time',
        'reason', 'comment', 'location_id')}
    payload.update(version=session.version, report_id=period['id'])
    payload.update(changes)
    session.update_unavailability(payload)


def residual_shift(session, engineer_id='e1'):
    residual, _, _ = session._residual(
        Event(id='edited-absence', type='replan', time=session.time), False)
    return next(worker.shift for worker in residual.engineers if worker.id == engineer_id)


def test_shorter_active_absence_restores_earlier_capacity_after_approval(make_scenario):
    session = session_with_route(make_scenario)
    report(session, end_time='12:00')
    approve(session)
    assert next(worker.shift.start for worker in session.scenario.engineers
                if worker.id == 'e1') == 720

    edit(session, end_time='10:00')

    assert residual_shift(session) == TimeWindow(start=600, end=1080)
    restored = ScenarioSession.restore(session.dump())
    assert residual_shift(restored) == TimeWindow(start=600, end=1080)
    approve(restored)
    assert next(worker.shift for worker in restored.scenario.engineers
                if worker.id == 'e1') == TimeWindow(start=600, end=1080)


def test_postponed_future_absence_releases_the_old_cutoff(make_scenario):
    session = session_with_route(make_scenario)
    report(session, start_time='10:00', end_time='12:00')
    approve(session)
    assert next(worker.shift.end for worker in session.scenario.engineers
                if worker.id == 'e1') == 600

    edit(session, start_time='13:00', end_time='14:00')

    assert residual_shift(session) == TimeWindow(start=540, end=780)
    approve(session)
    assert next(worker.shift for worker in session.scenario.engineers
                if worker.id == 'e1') == TimeWindow(start=540, end=780)


def test_moving_absence_to_another_day_releases_the_current_day(make_scenario):
    session = session_with_route(make_scenario)
    report(session, start_time='10:00', end_time='12:00')
    approve(session)
    tomorrow = (date.fromisoformat(session.scenario.date) + timedelta(days=1)).isoformat()

    edit(session, start_date=tomorrow, end_date=tomorrow)

    assert residual_shift(session) == TimeWindow(start=540, end=1080)
    assert residual_shift(ScenarioSession.restore(session.dump())) == TimeWindow(start=540, end=1080)


def test_edit_keeps_the_approved_day_off_call_window(make_scenario):
    session = session_with_day_off_worker(make_scenario)
    batch = session.preview(emergency_payload(session), time_limit=.03)
    selected = next(candidate for candidate in batch['candidates']
                    if candidate['emergency']['engineer_id'] == 'e2')
    session.approve({'version': session.version, 'batch_id': batch['id'],
                     'candidate_id': selected['id']})
    report(session, engineer_id='e2', start_time='19:00', end_time='19:10')
    approve(session)

    edit(session, start_time='19:30', end_time='19:45')
    assert residual_shift(session, 'e2') == TimeWindow(start=1110, end=1170)

    tomorrow = (date.fromisoformat(session.scenario.date) + timedelta(days=1)).isoformat()
    edit(session, start_date=tomorrow, end_date=tomorrow)
    assert residual_shift(session, 'e2') == TimeWindow(start=1110, end=1200)
    assert residual_shift(ScenarioSession.restore(session.dump()), 'e2') == TimeWindow(start=1110, end=1200)
