"""Dispatcher confirmation of physical work reported by an engineer."""
from copy import deepcopy

ENDED = {'completed': 'Фактическое завершение',
         'completion_pending': 'Выполнение заявлено; ожидает подтверждения диспетчера',
         'interrupted': 'Фактическое прерывание'}


def refresh_frozen_report(session, fact):
    """Keep the current ledger consistent with a revision, leaving saved views intact."""
    previous = session.fact_audit[-1].get('previous') if session.fact_audit else None
    if not previous:
        return
    old_marker = f"fact:{previous['engineer_id']}:{previous['time']}:{previous['request_id']}:{previous['status']}"
    for route in session.frozen:
        for visit in list(route['visits']):
            if visit.get('fact_record_id') != fact['id'] and visit.get('fact_id') != old_marker:
                continue
            if fact['status'] not in ENDED:
                route['visits'].remove(visit)
                continue
            visit.update(request_id=fact['request_id'], arrival=fact['time'], start=fact['time'], end=fact['time'],
                status=fact['status'], explanation=ENDED[fact['status']], consumed_equipment=dict(fact['consumed']),
                fact_record_id=fact['id'],
                fact_id=f"fact:{fact['engineer_id']}:{fact['time']}:{fact['request_id']}:{fact['status']}")
    session.frozen=[route for route in session.frozen if route['visits']]


def pending_completions(session):
    return [{**deepcopy(fact), 'address': session.catalog[fact['request_id']].address,
             'engineer_name': session.initial_engineers[fact['engineer_id']].name}
            for fact in session.fact_log if fact['status'] == 'completion_pending']


def confirm_completion(session, payload):
    if set(payload) != {'version', 'fact_id'}:
        raise ValueError('Для подтверждения нужны версия и ID сообщения; уточнение вносится через журнал фактов')
    if type(payload['version']) is not int or payload['version'] != session.version:
        raise ValueError('Устаревшая версия: обновите сообщения')
    fact = next((f for f in session.fact_log if f['id'] == payload['fact_id']), None)
    if fact is None or fact['status'] != 'completion_pending':
        raise ValueError('Сообщение не найдено или уже подтверждено; нет ожидающего подтверждения')
    # Replay replaces the original report, preserving its effective time even if
    # the worker has already sent later statuses or the plan has been recalculated.
    return session.record_fact({**fact['input'], 'version': session.version,
        'fact_id': fact['id'], 'status': 'completed', 'source': 'dispatcher',
        'correction_reason': 'Диспетчер подтвердил сообщение инженера о завершении'})
