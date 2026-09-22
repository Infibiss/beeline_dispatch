"""Explicit synthetic telemetry, held in a separate 'demo' stream."""
from copy import deepcopy
from uuid import uuid4
from .contracts import GpsInputError


CASES = ('near_site', 'stopped', 'moving_away', 'late', 'low_accuracy', 'stale', 'shift_unconfirmed', 'clear')


def run_demo(service, payload):
    if not isinstance(payload, dict) or set(payload) != {'engineer_id', 'case'}:
        raise GpsInputError('Выберите инженера и демонстрационный сценарий')
    eid, case = payload['engineer_id'], payload['case']
    context = service.context()
    if not isinstance(eid, str) or eid not in context['engineers'] or case not in CASES:
        raise GpsInputError('Неизвестный инженер или GPS-сценарий')
    service.clear_demo(eid)
    if case == 'clear':
        return {'channel': 'demo', 'cleared': eid}
    engineer = deepcopy(context['engineers'][eid])
    task = next((t for t in engineer['tasks'] if t['point']), None)
    if task is None:
        raise GpsInputError('У этого инженера нет назначенной заявки с координатами; выберите другого')
    engineer['status'] = {'status': 'traveling', 'request_id': task['id']}
    minute = max(engineer['shift']['start'], task['start'] - 10)
    if case == 'late':
        minute = max(w['end'] for w in task['windows']) - 1
    if case == 'shift_unconfirmed':
        engineer['status'] = None
        minute = engineer['shift']['start'] + service.config.shift_grace_minutes + 1
    now, tracker = service.clock(), 'demo-' + uuid4().hex
    offsets = {'near_site': [.0002]*3, 'stopped': [.01]*3,
               'moving_away': [.001, .003, .005], 'late': [.04]*3,
               'low_accuracy': [.01]*3, 'stale': [.01]*3, 'shift_unconfirmed': [.01]*3}[case]
    times = [now - 120, now - 60, now]
    if case == 'moving_away':
        times = [now - 60, now - 30, now]
    if case == 'stale':
        times = [now - 300, now - 240, now - 180]
    for delta, stamp in zip(offsets, times):
        point = {'lat': task['point']['lat'] + delta, 'lon': task['point']['lon'],
                 'accuracy_m': 1500 if case == 'low_accuracy' else 10, 'measured_at_ms': stamp * 1000}
        service.ingest({'engineer_id': eid, 'tracker_id': tracker, 'state': 'active', 'point': point},
                       channel='demo', demo_minute=minute, demo_engineer=engineer)
    return {'channel': 'demo', 'engineer_id': eid, 'case': case, 'plan_unchanged': True}
