"""Small immutable plan projections. Called only when the plan/facts change."""
from copy import deepcopy


def project_context(session):
    if not hasattr(session, 'initial_engineers'):
        return {'version': session.version, 'date': '', 'engineers': {}}
    done = {f['request_id'] for f in session.fact_log if f['status'] in ('completed', 'completion_pending')}
    result = {'version': session.version, 'date': session.scenario.date, 'time': session.time, 'engineers': {}}
    for engineer in session.initial_engineers.values():
        visits = {}
        for route in session.frozen:
            if route['engineer_id'] == engineer.id:
                visits.update({v['request_id']: deepcopy(v) for v in route['visits']})
        for route in session.plan.routes:
            if route.engineer_id == engineer.id:
                visits.update({v.request_id: v.model_dump() for v in route.visits})
        tasks = []
        for key, visit in visits.items():
            if key in done:
                continue
            job = session.catalog[key]
            point = session.geo_locations.get(key)
            tasks.append({
                'id': key, 'address': job.address,
                'point': {'lat': point.lat, 'lon': point.lon} if point else None,
                'windows': [w.model_dump() for w in job.windows],
                'start': visit['start'], 'end': visit['end'],
                'duration_minutes': job.duration_minutes, 'travel_minutes': visit.get('travel_minutes',0),
            })
        tasks.sort(key=lambda t: (t['start'], t['id']))
        result['engineers'][engineer.id] = {
            'id': engineer.id, 'name': engineer.name, 'transport': engineer.transport.value,
            'shift': engineer.shift.model_dump(), 'tasks': tasks,
            'status': deepcopy(session.statuses.get(engineer.id)),
        }
    return result
