"""Engineer-facing projections and commands over the shared, versioned fact journal.

HTTP authorization binds employees to their region and ID. Dispatchers retain
the preview picker. This projection never assigns jobs or approves a plan.
"""
from copy import deepcopy


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
    facts = [f for f in session.fact_log if f['engineer_id'] == engineer_id]
    latest = session.statuses.get(engineer_id)
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
        elif latest and latest.get('request_id') == key:
            status = latest['status']
        elif history and history[-1]['status'] == 'interrupted':
            status = 'interrupted'
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
    if kind in ('traveling', 'working', 'completed', 'interrupted'):
        # A fresh mobile action cannot act on another engineer's assignment or
        # a job removed since the last plan; historical corrections are dispatch-only.
        owned = {r['id'] for r in session.fact_requests() if r['engineer_id'] == eid}
        if not isinstance(key, str) or key not in owned:
            raise ValueError('Заявка больше не назначена вам или уже завершена; обновите задания')
    if old.get('status') == 'traveling' and kind in ('traveling', 'working') and old.get('request_id') != key:
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
