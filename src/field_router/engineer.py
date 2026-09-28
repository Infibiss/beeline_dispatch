"""Engineer-facing projections and commands over the shared, versioned fact journal.

HTTP authorization binds employees to their region and ID. Dispatchers retain
the preview picker. This projection never assigns jobs or approves a plan.
"""
from copy import deepcopy
from .availability import (day_blocks, stamp, shift_blocks, project_availability, availability_at,
                           trip_stopped_by_absence)


def engineer_index(session):
    return {
        'version': session.version,
        'date': session.scenario.date,
        'region': session.scenario.region,
        'engineers': [e.model_dump(mode='json') for e in session.initial_engineers.values()],
    }


def engineer_state(session, engineer_id):
    if engineer_id not in session.initial_engineers:
        raise ValueError('Неизвестный инженер')
    engineer = session.initial_engineers[engineer_id]
    current = next((item for item in session.scenario.engineers if item.id == engineer_id), None)
    if current is not None and current.off_duty != engineer.off_duty:
        engineer = engineer.model_copy(update={'off_duty': current.off_duty,
                                              'shift': session._availability_shift(engineer_id)})
    facts = [f for f in session.fact_log if f['engineer_id'] == engineer_id]
    latest = session.statuses.get(engineer_id)
    periods = [period for period in session.unavailability if period['engineer_id'] == engineer_id]
    blocks = shift_blocks(periods, engineer_id, session.scenario.date,
                          session._availability_shift(engineer_id))
    active_absence = next((period for (start, end), period in day_blocks(periods, engineer_id, session.scenario.date)
                           if start <= session.time < end), None)
    return_confirmation_required = availability_at(
        project_availability(blocks, facts, engineer_id), session.time) == 'awaiting_return'
    awaiting_period = next((period for (start, end), period in reversed(blocks)
                            if end <= session.time and not period.get('location_id')), None) if return_confirmation_required else None
    now_stamp = stamp(session.scenario.date, '00:00') + session.time
    scheduled_absences = sorted((period for period in periods if not period.get('revoked')
                                and stamp(period['start_date'], period['start_time']) > now_stamp),
                               key=lambda period: (period['start_date'], period['start_time']))
    stopped_trip = trip_stopped_by_absence(
        project_availability(blocks, facts, engineer_id), latest, session.time)
    pending_reassignment_ids = set(session.pending_reassignment_ids(engineer_id))
    visits = {}
    for route in session.frozen:
        if route['engineer_id'] == engineer_id:
            for visit in route['visits']:
                visits[visit['request_id']] = deepcopy(visit)
    route = next((r for r in session.plan.routes if r.engineer_id == engineer_id), None)
    if route:
        visits.update({v.request_id: v.model_dump() for v in route.visits})
    # Completed work stays in the engineer's archive after a replan removes it
    # from the remaining-day problem. Never infer completion from the clock.
    for fact in facts:
        if fact['status'] in ('completed', 'completion_pending'):
            visits.setdefault(fact['request_id'], None)
    tasks = []
    for key, visit in visits.items():
        job = session.catalog[key]
        history = [f for f in facts if f.get('request_id') == key]
        completed = next((f for f in reversed(history) if f['status'] == 'completed'), None)
        pending = next((f for f in reversed(history) if f['status'] == 'completion_pending'), None)
        status = 'planned'
        if completed:
            status = 'completed'
        elif pending:
            status = 'completion_pending'
        elif latest and not stopped_trip and latest.get('request_id') == key:
            status = latest['status']
        elif history and history[-1]['status'] == 'interrupted':
            status = 'interrupted'
        if key in pending_reassignment_ids and status == 'planned':
            status = 'pending_replan'
        point = session.geo_locations.get(key)
        spent = {device: sum(f['consumed'][device] for f in session.fact_log
                            if f.get('request_id') == key) for device in ('router', 'tv_box')}
        tasks.append({
            **job.model_dump(mode='json'),
            'visit': visit,
            'status': status,
            'completed_at': completed['time'] if completed else None,
            'reported_completed_at': pending['time'] if pending else None,
            'remaining_minutes': latest.get('remaining_minutes') if latest and latest.get('request_id') == key else job.duration_minutes,
            'equipment_remaining': {k: max(0, getattr(job.equipment, k) - spent[k]) for k in spent},
            'point': point.model_dump(mode='json') if point else None,
            'history': deepcopy(history),
        })
    tasks.sort(key=lambda t: ((t['visit'] or {}).get('start', t['completed_at'] or 1440), t['id']))
    shape = None
    if session.geography and session.version == 1:
        shape = session.geography.route_shapes.get(session.plan.solver, {}).get(engineer_id)

    def location_label(key):
        if key in session.catalog:
            return session.catalog[key].address
        if key == 'office':
            return 'Офис'
        if key == engineer.start_location:
            point = session.geo_locations.get(key)
            address = engineer.start_address or (point.original_address or point.label if point else None)
            return 'Старт: ' + address if address else 'Точка старта инженера'
        return key

    locations = [
        {'id': key, 'label': location_label(key)}
        for key in (session.geo_locations if session.geography else session.scenario.points)
        if key == engineer.start_location or key == 'office' or key in visits
    ]
    inventory = next((i for i in session.inventory if i['engineer_id'] == engineer_id), None)
    return {
        'version': session.version, 'date': session.scenario.date,
        'region': session.scenario.region, 'time': session.time,
        'plan_updates': [{'version': version, 'time': view['operations']['time'],
                          'label': view['operations']['label']}
                         for version, view in sorted(session.views.items())[-20:]],
        'needs_replanning': session.needs_replanning,
        'engineer': engineer.model_dump(mode='json'),
        'status': deepcopy(latest), 'tasks': tasks,
        'unavailability': deepcopy(periods),
        'active_absence': deepcopy(active_absence),
        'scheduled_absences': deepcopy(scheduled_absences),
        'indefinite_absence': deepcopy(latest) if latest and latest.get('status') == 'unavailable' else None,
        'return_report_id': (active_absence or awaiting_period or {}).get('id'),
        'return_confirmation_required': return_confirmation_required,
        'return_pending_replan': session.return_pending_replan(engineer_id),
        'declined_assignments': deepcopy([item for item in session.declined_assignments
                                          if item['engineer_id'] == engineer_id]),
        'pending_reassignment_ids': sorted(pending_reassignment_ids),
        'facts': deepcopy(facts), 'inventory': deepcopy(inventory),
        'locations': locations, 'route_shape': deepcopy(shape),
        'distance_m': route.distance_m if route else 0,
        'travel_minutes': route.travel_minutes if route else 0,
        'service_minutes': route.service_minutes if route else 0,
    }


def record_engineer_fact(session, payload):
    """Validate mobile commands before using the same persistence as dispatch."""
    allowed = {'version', 'engineer_id', 'time', 'status', 'request_id', 'location_id',
               'remaining_minutes', 'consumed_router', 'consumed_tv_box', 'comment'}
    if set(payload) - allowed:
        raise ValueError('Неизвестные поля сообщения инженера')
    if type(payload.get('version')) is not int or payload['version'] != session.version:
        raise ValueError('Устаревшая версия плана: обновите задания и повторите действие')
    eid = payload.get('engineer_id')
    if not isinstance(eid, str) or eid not in session.initial_engineers:
        raise ValueError('Неизвестный инженер')
    kind = payload.get('status')
    if kind not in ('available', 'traveling', 'working', 'completed', 'interrupted', 'unavailable'):
        raise ValueError('Неизвестный статус инженера')
    old = session.statuses.get(eid, {})
    key = payload.get('request_id')
    availability = availability_at(project_availability(
        shift_blocks(session.unavailability, eid, session.scenario.date, session._availability_shift(eid)),
        session.fact_log, eid), session.time)
    if kind in ('available', 'traveling', 'working') and (
            old.get('status') == 'unavailable' or availability != 'available'):
        raise ValueError('После недоступности подтвердите возвращение кнопкой «Я снова доступен».')
    if kind in ('traveling', 'working') and session.return_pending_replan(eid):
        raise ValueError('Возвращение передано на перепланирование; дождитесь новых назначений')
    if kind in ('traveling', 'working', 'completed', 'interrupted'):
        finishing_current = (kind in ('completed', 'interrupted') and old.get('status') == 'working'
                             and old.get('request_id') == key)
        # A scheduled absence may invalidate the old planned end, but must not
        # trap actual work: its completion or interruption still needs a fact.
        if key in session.pending_reassignment_ids(eid) and not finishing_current:
            raise ValueError('Заявка передана на перепланирование; обновите задания')
        # A fresh mobile action cannot act on another engineer's assignment or
        # a job removed since the last plan; historical corrections are dispatch-only.
        owned = {r['id'] for r in session.fact_requests() if r['engineer_id'] == eid}
        if not isinstance(key, str) or key not in owned:
            raise ValueError('Заявка больше не назначена вам или уже завершена; обновите задания')
    blocks = shift_blocks(session.unavailability, eid, session.scenario.date, session._availability_shift(eid))
    stopped_trip = trip_stopped_by_absence(
        project_availability(blocks, session.fact_log, eid), old, session.time)
    if not stopped_trip and old.get('status') == 'traveling' and kind in ('traveling', 'working') and old.get('request_id') != key:
        raise ValueError('Сначала сообщите диспетчеру об остановке текущего выезда')
    if kind == 'completed' and not (old.get('status') == 'working' and old.get('request_id') == key):
        raise ValueError('Сначала отметьте начало работы по этой заявке')
    comment = payload.get('comment', '')
    if not isinstance(comment, str) or len(comment) > 1000:
        raise ValueError('Комментарий должен быть текстом до 1000 символов')
    if kind in ('interrupted', 'unavailable') and len(comment.strip()) < 3:
        raise ValueError('Опишите проблему для диспетчера')
    session.record_fact({**payload, 'status': 'completion_pending' if kind == 'completed' else kind,
                         'source': 'engineer'})
    return engineer_state(session, eid)
