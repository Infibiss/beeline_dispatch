"""Read-only plan indicators and the hand-off contract for a future risk model.

No inference, optimizer invocation or persistence belongs in this projection.
Percentages describe the plan, never a probability of failure.
"""
SCHEMA_VERSION = '1.0'
WINDOW_BUFFER_MINUTES = 15


def ratio(numerator, denominator):
    return round(100 * numerator / denominator, 1) if denominator else None


def _occupied(intervals, lower, upper):
    total, end = 0, lower
    for start, stop in sorted(intervals):
        start, stop = max(start, lower, end), min(stop, upper)
        if stop > start:
            total += stop - start
            end = stop
    return total


def forecast_placeholder(context):
    """Replace with a validated cached worker result; do not run ML under a lock."""
    return {'status': 'not_connected', 'region': context['region'],
            'plan_version': context['plan_version'], 'model_version': None,
            'horizon_minutes': None, 'risks': []}


def dashboard(session):
    now = session.time
    excluded = session.excluded_engineers()
    eligible = {key: e for key, e in session.initial_engineers.items()
                if key not in excluded and e.shift.end > now
                and session.statuses.get(key, {}).get('status') != 'unavailable'
                and (session.execution_mode != 'manual' or key in session.statuses)}
    completed = {f['request_id'] for f in session.fact_log if f['status'] in ('completed', 'completion_pending')}
    visits = {}
    for route in session.frozen:
        for visit in route['visits']:
            visits[(route['engineer_id'], visit['request_id'])] = visit
    for route in session.plan.routes:
        for visit in route.visits:
            visits[(route.engineer_id, visit.request_id)] = visit.model_dump()
    visits = {(eid, rid): v for (eid, rid), v in visits.items()
              if eid in eligible and v['end'] > now and rid not in completed}
    capacity = sum(e.shift.end - max(now, e.shift.start) for e in eligible.values())
    occupied = 0
    for eid, engineer in eligible.items():
        intervals = []
        for (owner, _), visit in visits.items():
            if owner != eid:continue
            intervals += [(visit['arrival'] - visit['travel_minutes'], visit['arrival']),
                          (visit['start'], visit['end'])]
        occupied += _occupied(intervals, max(now, engineer.shift.start), engineer.shift.end)

    tight, checked, min_buffer = 0, 0, None
    for (_, rid), visit in visits.items():
        if visit['start'] < now:continue
        windows = [w for w in session.catalog[rid].windows if w.start <= visit['start'] <= w.end]
        if not windows:continue
        buffer = max(w.end for w in windows) - visit['start']
        checked += 1
        tight += buffer <= WINDOW_BUFFER_MINUTES
        min_buffer = buffer if min_buffer is None else min(min_buffer, buffer)

    supply = {'router': 0, 'tv_box': 0}
    demand = dict(supply)
    for stock in session.inventory:
        if stock['engineer_id'] not in eligible:continue
        for key in supply:
            reported=sum(f['consumed'][key] for f in session.fact_log
                         if f['engineer_id']==stock['engineer_id'] and f['status']=='completion_pending')
            supply[key] += stock['available'][key] + max(0,stock['reserved'][key]-reported)
    for (_, rid), _visit in visits.items():
        spent = {key: sum(f['consumed'][key] for f in session.fact_log if f.get('request_id') == rid)
                 for key in demand}
        for key in demand:demand[key] += max(0, getattr(session.catalog[rid].equipment, key) - spent[key])
    labels = {'router': 'Роутеры', 'tv_box': 'ТВ-приставки'}
    equipment = [{'id': key, 'label': labels[key], 'needed': demand[key], 'on_hand': supply[key],
                  'spare': max(0, supply[key] - demand[key]), 'percent': ratio(demand[key], supply[key])}
                 for key in supply]
    limiting = max([x for x in equipment if x['on_hand'] or x['needed']] or equipment,
                   key=lambda x: (x['needed'] > x['on_hand'], x['percent'] or 0))
    equipment_value = limiting['percent'] if any(supply.values()) or any(demand.values()) else None
    context = {'region': session.scenario.region, 'date': session.scenario.date, 'plan_version': session.version,
               'time': now, 'needs_replanning': session.needs_replanning, 'mode': session.mode()}
    return {'schema_version': SCHEMA_VERSION, 'context': context,
            'summary': {'engineers': len(eligible), 'roster': len(session.initial_engineers),
                        'unassigned': len(session.plan.unassigned)},
            'indicators': [
                {'id': 'capacity', 'title': 'Загрузка бригад', 'value': ratio(occupied, capacity),
                 'caption': 'оставшегося времени смен занято',
                 'detail': f'{occupied} из {capacity} мин · дорога и работа',
                 'explanation': 'Доля дороги и работы в оставшихся сменах по утверждённому плану. Ожидание клиента не считается занятостью. Свободные минуты могут быть разбросаны между визитами и не гарантируют место для новой заявки.',
                 'recommendation': 'Проверьте загрузку отдельных инженеров и неназначенные заявки. Для изменений сравните варианты перепланирования.',
                 'facts': {'occupied_minutes': occupied, 'capacity_minutes': capacity}},
                {'id': 'windows', 'title': 'Плотные клиентские окна', 'value': ratio(tight, checked),
                 'caption': 'будущих визитов с запасом ≤ 15 мин',
                 'detail': f'{tight} из {checked} визитов · минимум '+(f'{min_buffer} мин' if min_buffer is not None else 'нет данных'),
                 'explanation': 'Запас — время от планового начала до конца подходящего клиентского окна. Для нескольких окон берётся подходящее с наиболее поздним концом. Это локальный запас визита, а не допустимая задержка всего маршрута и не вероятность опоздания.',
                 'recommendation': 'Уточните время у клиента и инженера. Если появилась задержка, запишите факт и сравните новые варианты плана.',
                 'facts': {'tight_visits': tight, 'checked_visits': checked, 'threshold_minutes': WINDOW_BUFFER_MINUTES}},
                {'id': 'equipment', 'title': 'Оборудование под план', 'value': equipment_value,
                 'caption': 'остатка наиболее занятого типа требуется',
                 'detail': ' · '.join(f"{x['label']}: нужно {x['needed']}, на руках {x['on_hand']}" for x in equipment),
                 'explanation': 'Потребность оставшихся визитов относительно физического остатка у доступных бригад. Показан наиболее занятый тип устройств. Сумма по району не означает, что оборудование можно передать между инженерами.',
                 'recommendation': 'Проверьте запас нужного типа у конкретного исполнителя. Пополнение или передача требуют отдельного подтверждения; пересчёт сам не создаёт оборудование.',
                 'facts': {'equipment': equipment}}],
            'forecast': forecast_placeholder(context)}
