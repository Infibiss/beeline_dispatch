"""Validated JSON intake for requests already structured by an upstream form/DB.

The upstream must identify a routable point. We never silently substitute a
nearby address or invent a road matrix for a newly geocoded customer.
"""
from datetime import datetime, timezone


def _minute(value, field):
    if type(value) is int and 0 <= value <= 1440:
        return value
    if isinstance(value, str) and len(value) == 5 and value[2] == ':' and (
            value[:2].isdigit() and value[3:].isdigit()):
        hour, minute = int(value[:2]), int(value[3:])
        if hour < 24 and minute < 60:
            return hour * 60 + minute
    raise ValueError(f'{field}: ожидается минута дня или время ЧЧ:ММ')


def _anchor(session, row):
    key = row.get('anchor_id', row.get('location_id'))
    known = set(session.geo_locations) if session.geography else set(session.scenario.points)
    known.update(session.catalog)
    if key is None:
        address = row.get('address')
        if not isinstance(address, str) or not address.strip():
            raise ValueError('Для заявки укажите anchor_id/location_id или адрес известной точки')
        matches = [item for item in known if (
            item in session.catalog and session.catalog[item].address.casefold() == address.strip().casefold()) or (
            item in session.geo_locations and (
                session.geo_locations[item].original_address or session.geo_locations[item].label
            ).casefold() == address.strip().casefold())]
        if len(matches) != 1:
            raise ValueError('Адрес не сопоставлен однозначно с дорожной точкой; сначала зарегистрируйте точку')
        key = matches[0]
    if not isinstance(key, str) or key not in known:
        raise ValueError('Неизвестная дорожная точка заявки; сначала зарегистрируйте адрес')
    return key


def normalize(session, raw, *, allow_past=False):
    from .replanning import Event

    if not isinstance(raw, dict):
        raise ValueError('Каждая заявка должна быть JSON-объектом')
    if raw.get('region', session.scenario.region) != session.scenario.region or (
            raw.get('date', session.scenario.date) != session.scenario.date):
        raise ValueError('Заявка относится к другому району или дате плана')
    allowed = {'id', 'type', 'time', 'anchor_id', 'location_id', 'address', 'region', 'date',
               'duration_minutes', 'router', 'tv_box', 'work_kind', 'window_start', 'window_end',
               'required_transport'}
    if set(raw) - allowed:
        raise ValueError('Неизвестные поля заявки: ' + ', '.join(sorted(set(raw) - allowed)))
    kind = raw.get('type', 'new_request')
    if kind not in ('new_request', 'emergency'):
        raise ValueError('Приём JSON поддерживает обычную или аварийную заявку')
    event = {key: raw[key] for key in ('id', 'duration_minutes', 'router', 'tv_box', 'work_kind',
                                     'required_transport') if key in raw}
    event.update(type=kind, time=_minute(raw.get('time', session.time), 'Время поступления'),
                 anchor_id=_anchor(session, raw))
    if kind == 'new_request':
        for key in ('window_start', 'window_end'):
            if key not in raw:
                raise ValueError('Для обычной заявки нужны обе границы временного окна')
            event[key] = _minute(raw[key], key)
    else:
        if 'work_kind' in raw:
            raise ValueError('Для аварийной заявки вид работ определяется автоматически')
        for key in ('window_start', 'window_end'):
            if key in raw and raw[key] is not None:
                event[key] = _minute(raw[key], key)
    parsed = Event.model_validate(event)
    if parsed.time < session.time and not allow_past:
        raise ValueError('Время поступления не может быть раньше текущего времени плана')
    address = raw.get('address')
    if address is not None and (not isinstance(address, str) or not 6 <= len(address.strip()) <= 300):
        raise ValueError('Адрес должен содержать от 6 до 300 символов')
    anchor = parsed.anchor_id
    valid_addresses = set()
    if anchor in session.catalog:
        valid_addresses.add(session.catalog[anchor].address.casefold().strip())
    if anchor in session.geo_locations:
        point = session.geo_locations[anchor]
        valid_addresses.update(value.casefold().strip() for value in
                               (point.original_address, point.label) if value)
    if anchor == 'office':
        valid_addresses.add(session.scenario.office_address.casefold().strip())
    if address is not None and valid_addresses and address.casefold().strip() not in valid_addresses:
        raise ValueError('Адрес не совпадает с выбранной дорожной точкой; проверьте привязку заявки')
    if address is None:
        if not valid_addresses:
            raise ValueError('Для точки без адреса в справочнике укажите адрес заявки')
        address = session.catalog[anchor].address if anchor in session.catalog else (
            session.geo_locations[anchor].original_address or session.geo_locations[anchor].label
            if anchor in session.geo_locations else session.scenario.office_address)
    serialized = parsed.model_dump(mode='json')
    # Keep legacy intake idempotency: absent constraints were not persisted.
    if parsed.required_transport is None:
        serialized.pop('required_transport', None)
    return {'id': parsed.id, 'event': serialized,
            'address': address.strip(), 'status': 'new',
            'received_at': datetime.now(timezone.utc).isoformat()}


def decision_entry(session, candidate, item, automatic):
    """Human-readable, fact-bound explanation for one incoming request."""
    event = item['event']
    key = ('emergency:' if event['type'] == 'emergency' else 'request:') + item['id']
    owner = next(((route.engineer_id, visit) for route in session.plan.routes
                  for visit in route.visits if visit.request_id == key), None)
    request = session.catalog[key]
    if owner:
        engineer = session.initial_engineers[owner[0]]
        visit = owner[1]
        time = f'{visit.start // 60:02}:{visit.start % 60:02}'
        window = next((w for w in request.windows if w.start <= visit.start <= w.end), request.windows[0])
        window_label = f'{window.start // 60:02}:{window.start % 60:02}–{window.end // 60:02}:{window.end % 60:02}'
        message = (f'Заявка назначена {engineer.name} на {time}: инженер допущен к этому виду работ, '
                   f'нужное оборудование есть, начало попадает в окно клиента {window_label}. '
                   'Расчёт также проверил дорогу и уже закреплённые визиты.')
        status = 'assigned'
    else:
        unassigned = next((row for row in session.plan.unassigned if row.request_id == key), None)
        message = ('Заявка пока без исполнителя: ' + (unassigned.reason.rstrip('. ') if unassigned else
                   'в рассчитанных вариантах не найдено допустимого назначения') +
                   '. Она остаётся в плане для следующего пересчёта и требует внимания диспетчера.')
        status = 'unassigned'
    return {'id': item['id'], 'request_id': key, 'address': item['address'],
            'kind': 'new', 'change_type': 'new_assignment' if owner else 'new_unassigned',
            'previous_engineer_id': None, 'previous_start': None,
            'status': status, 'engineer_id': owner[0] if owner else None,
            'start': owner[1].start if owner else None,
            'version': session.version, 'candidate_id': candidate['id'],
            'automatic': automatic, 'created_at': datetime.now(timezone.utc).isoformat(),
            'explanation': message, 'explanation_source': 'verified_rule',
            'plan_explanation': candidate.get('explanation', ''),
            'llm_notice': 'Проверенные факты доступны без YandexGPT; ИИ-пояснение добавляется только при успешном ответе модели.'}


def reassignment_entry(session, candidate, request_id, previous, automatic):
    """Audit a changed assignment in an already known request."""
    request = session.catalog[request_id]
    owner = next(((route.engineer_id, visit) for route in session.plan.routes
                  for visit in route.visits if visit.request_id == request_id), None)
    prior_name = (session.initial_engineers[previous[0]].name if previous and previous[0]
                  in session.initial_engineers else 'исполнителя не было')
    if previous:
        prior_name += f' с {previous[1] // 60:02}:{previous[1] % 60:02}'
    if owner:
        name = session.initial_engineers[owner[0]].name
        start = f'{owner[1].start // 60:02}:{owner[1].start % 60:02}'
        message = (f'После пересчёта заявка назначена {name} на {start}. '
                   f'Раньше: {prior_name}. Новый план прошёл проверку квалификации, '
                   'оборудования, времени клиента и уже закреплённых визитов.')
    else:
        unresolved = next((row for row in session.plan.unassigned if row.request_id == request_id), None)
        reason = unresolved.reason.rstrip('. ') if unresolved else 'допустимое назначение не найдено'
        message = (f'После пересчёта заявка без исполнителя: {reason}. '
                   f'Раньше: {prior_name}. Заявка остаётся в плане для следующего решения.')
    change_type = ('recovered' if previous is None and owner else
                   'lost_assignment' if previous and not owner else
                   'reassigned' if previous and owner and previous[0] != owner[0] else
                   'rescheduled')
    return {'id': request_id.split(':', 1)[-1], 'request_id': request_id,
            'kind': 'changed', 'change_type': change_type,
            'previous_engineer_id': previous[0] if previous else None,
            'previous_start': previous[1] if previous else None,
            'address': request.address, 'status': 'assigned' if owner else 'unassigned',
            'engineer_id': owner[0] if owner else None,
            'start': owner[1].start if owner else None,
            'version': session.version, 'candidate_id': candidate['id'],
            'automatic': automatic, 'created_at': datetime.now(timezone.utc).isoformat(),
            'explanation': message, 'explanation_source': 'verified_rule',
            'plan_explanation': candidate.get('explanation', ''),
            'llm_notice': 'Проверенные факты доступны без YandexGPT; ИИ-пояснение добавляется только при успешном ответе модели.'}


def cancellation_entry(session, candidate, request_id, previous, automatic):
    """Record an explicit cancellation without calling it an unassigned job."""
    request=session.catalog[request_id]
    prior=(session.initial_engineers[previous[0]].name if previous and previous[0]
           in session.initial_engineers else None)
    explanation=('Заявка отменена и исключена из действующего плана. '
                 'Она не считается заявкой без исполнителя и не участвует в следующих пересчётах.')
    if prior:
        explanation+=f' Ранее была назначена {prior} на {previous[1] // 60:02}:{previous[1] % 60:02}.'
    return {'id':request_id.split(':',1)[-1], 'request_id':request_id,
            'address':request.address, 'kind':'changed', 'change_type':'cancelled',
            'status':'cancelled', 'previous_engineer_id':previous[0] if previous else None,
            'previous_start':previous[1] if previous else None,
            'engineer_id':None, 'start':None,
            'version':session.version, 'candidate_id':candidate['id'],
            'automatic':automatic, 'created_at':datetime.now(timezone.utc).isoformat(),
            'explanation':explanation, 'explanation_source':'verified_rule',
            'plan_explanation':candidate.get('explanation',''),
            'llm_notice':'Проверенные факты доступны без YandexGPT; ИИ-пояснение добавляется только при успешном ответе модели.'}


def apply_autopilot(session, *, time_limit=1, solver='hybrid', gps_proposals=None):
    """Build one batch for all pending intake items and apply a verified candidate.

    This function mutates its argument. Call it on a deep copy and persist that
    copy atomically before publishing it in a live server.
    """
    from uuid import uuid4
    from .recommendation import eligible_candidates, recommendation_rank

    gps_proposals=gps_proposals or []
    if not session.autopilot_due() and not gps_proposals:
        return None
    if gps_proposals:
        if session.planning_settings.get('mode')!='autopilot':
            return None
        from .gps.autopilot import stage_estimates, record_applied
        stage_estimates(session,gps_proposals)
    event_time=max([session.time]+[p['time'] for p in gps_proposals])
    payload = {'version': session.version,
               'event': {'id': 'auto:' + uuid4().hex, 'type': 'replan', 'time': event_time},
               'allow_redirect': False, 'preserve_in_progress': True,
               'protect_approved_assignments': True}
    batch = session.preview(payload, time_limit=time_limit, solver=solver)
    chosen = min(eligible_candidates(batch), key=recommendation_rank)
    session.approve({'version': session.version, 'batch_id': batch['id'],
                     'candidate_id': chosen['id']}, automatic=True)
    if gps_proposals:
        record_applied(session,gps_proposals)
    return batch, chosen['id']
