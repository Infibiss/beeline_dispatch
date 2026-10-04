"""Read-only, actionable exceptions for the dispatcher, not a second event log.

Counts are distinct requests (unassigned/tight/deadline) or engineers (verify/overtime).
Categories overlap; their counts must never be added as a total of incidents.
Forecast thresholds are presentation policy, not calibrated risk classes.
"""
from .availability import shift_blocks
from .risk_engine import RiskEngine, application_risk_config
from .risk_signals import RULES, current_forecast, frequency_label, review_counts, signal
from .day_triage import NOW_MINUTES, PREPARE_MINUTES, prioritize

SLA_ATTENTION = RULES['sla']['frequency']
OVERTIME_ATTENTION = RULES['overtime']['frequency']
MIN_OVERTIME = RULES['overtime']['minutes']
TIGHT_MINUTES = 15
FACT_AGE_MINUTES = 60


def _clock(value):
    return f'{int(value) // 60:02d}:{int(value) % 60:02d}'


def attention(session, forecast, fleet, gps=None):
    now = session.time
    catalog = session.catalog
    done = {f.get('request_id') for f in session.fact_log
            if f.get('status') in ('completed', 'completion_pending')}
    done.update(s.get('request_id') for s in session.statuses.values()
                if s.get('status') in ('completed', 'completion_pending'))
    active = {r.id for r in session.scenario.requests} - done
    pending = set(session.pending_reassignment_ids()) & active
    planned = {v.request_id for r in session.plan.routes for v in r.visits}
    missing = (active - planned) | pending
    reasons = {u.request_id: u for u in session.plan.unassigned}
    all_visits = {}
    for route in session.frozen:
        for visit in route['visits']:
            all_visits[(route['engineer_id'], visit['request_id'])] = visit
    for route in session.plan.routes:
        for visit in route.visits:
            all_visits[(route.engineer_id, visit.request_id)] = visit.model_dump()
    remaining = {key: v for key, v in all_visits.items()
                 if key[1] not in done and key[1] in catalog and
                 (v['end'] > now or session.statuses.get(key[0], {}).get('request_id') == key[1])}
    usable = current_forecast(forecast, {'needs_replanning': session.needs_replanning,
        'plan_version': session.version, 'region': session.scenario.region, 'time': now})
    model_visits = {v['request_id']: v for r in forecast.get('routes', []) for v in r['visits']} if usable else {}
    predecessors = {}
    for eid in {key[0] for key in all_visits}:
        previous = None
        for visit in sorted((v for (owner, rid), v in all_visits.items() if owner == eid), key=lambda v: v['start']):
            if previous:
                predecessors[visit['request_id']] = (previous['request_id'],
                    visit['start'] - previous['end'] - visit['travel_minutes'])
            previous = visit
    rows, grouped, tight_ids = [], set(), set()
    deadline_engine = RiskEngine(application_risk_config(0))

    def name(eid):
        worker = session.initial_engineers.get(eid)
        return worker.name if worker else eid

    def request_info(rid, start=None):
        req = catalog[rid]
        windows = [w for w in req.windows if w.end >= now]
        suitable = [w for w in req.windows if start is not None and w.start <= start <= w.end]
        # A future alternative is not extra buffer for an already planned visit.
        deadline = max(w.end for w in suitable) if suitable else min((w.end for w in windows), default=max(w.end for w in req.windows))
        try:
            sla = deadline_engine._emergency_response_sla_deadline(req)
        except ValueError:
            sla = None  # Invalid provenance is not evidence for an invented deadline.
        if sla is not None:
            deadline = min(deadline, sla)
        return {'request_id': rid, 'address': req.address, 'priority': req.priority,
                'priority_label': 'Авария' if req.skill.value == 'emergency' else
                                  'Срочная' if req.priority == 'urgent' else
                                  'Высокий приоритет' if req.priority == 'high' else 'Обычная',
                'windows': [w.model_dump() for w in req.windows], 'deadline': deadline,
                'minutes_to_deadline': deadline - now, 'planned_start': start,
                'window_buffer_minutes': max(w.end for w in suitable) - start if suitable else None,
                'tight_window': bool(suitable) and max(w.end for w in suitable) - start <= TIGHT_MINUTES,
                'previous_request_id': predecessors.get(rid, (None, None))[0],
                'schedule_buffer_minutes': predecessors.get(rid, (None, None))[1],
                'expected_late_minutes': model_visits.get(rid, {}).get('expected_late_minutes'),
                'p_sla_breach': model_visits.get(rid, {}).get('p_sla_breach')}

    def add(key, kind, evidence, title, detail, ids=(), eid=None, categories=(), action='facts', deadline=None, extra=None):
        ids = sorted(set(ids) & set(catalog))
        requests = [request_info(rid, all_visits.get((eid, rid), {}).get('start')) for rid in ids]
        due = deadline if deadline is not None else min((r['deadline'] for r in requests), default=None)
        urgent = kind != 'overtime' and any(r['priority_label'] == 'Авария' or r['priority'] == 'urgent' for r in requests)
        priority_rank = min((0 if r['priority_label'] == 'Авария' or r['priority'] == 'urgent'
                             else 1 if r['priority'] == 'high' else 2 for r in requests), default=2)
        rows.append({'id': key, 'kind': kind, 'evidence': evidence, 'title': title, 'detail': detail,
                     'request_ids': ids, 'requests': requests, 'engineer_id': eid, 'engineer_name': name(eid),
                     'categories': list(categories), 'deadline': due,
                     'minutes_to_deadline': due - now if due is not None else None,
                     'urgent': urgent, 'priority_rank': priority_rank, 'action': action,
                     'plan_version': session.version, **(extra or {})})

    # One outage is one incident, even when it invalidates many assignments.
    for eid, engineer in session.initial_engineers.items():
        shift = session.activated_off_duty_windows.get(eid, engineer.shift)
        periods = [(block, p) for block, p in shift_blocks(session.unavailability, eid, session.scenario.date, shift)
                   if block[1] > now]
        impacted = {rid for (owner, rid), v in remaining.items() if owner == eid and rid in pending}
        status = session.statuses.get(eid, {})
        if not impacted or not (periods or status.get('status') == 'unavailable'):
            continue
        parts = [f"{p['start_date']} {p['start_time']} — {p['end_date']} {p['end_time']}: {p.get('reason_label', 'недоступность')}"
                 for block, p in periods]
        add('absence-' + eid, 'unavailable', 'fact', name(eid) + ' недоступен',
            '; '.join(parts) or 'Инженер сообщил о недоступности. Его оставшиеся выезды требуют назначения.',
            impacted, eid, ('unassigned',), 'preview',
            extra={'periods': [p for _, p in periods]})
        grouped |= impacted

    for rid in sorted(missing - grouped):
        reason = reasons.get(rid)
        explanation = ('Вариант назначения пока не найден. Это не доказывает, что выполнить заявку невозможно.'
                       if reason and reason.code == 'SEARCH_UNASSIGNED' else reason.reason if reason else
                       'Предыдущее назначение недействительно: инженер не может выполнить выезд.' if rid in pending else
                       'В утверждённом плане нет исполнителя.')
        add('unassigned-' + rid, 'unassigned', 'fact', 'Нет исполнителя', explanation,
            [rid], categories=('unassigned',), action='preview')

    # Individual equipment balances: do not treat district totals as transferable stock.
    stocks = {r['engineer_id']: r for r in session.inventory}
    for eid in sorted({eid for eid, rid in remaining if rid not in missing}):
        visits = sorted((v for (owner, rid), v in remaining.items() if owner == eid and rid not in missing), key=lambda v: v['start'])
        stock = stocks.get(eid)
        if not stock:
            continue
        balance = {k: stock['available'][k] + max(0, stock['reserved'][k] - sum(
            f.get('consumed', {}).get(k, 0) for f in session.fact_log
            if f.get('engineer_id') == eid and f.get('status') == 'completion_pending')) for k in ('router', 'tv_box')}
        deficits, blocked = {}, []
        for visit in visits:
            rid = visit['request_id']
            for key in balance:
                spent = sum(f.get('consumed', {}).get(key, 0) for f in session.fact_log if f.get('request_id') == rid)
                needed = max(0, getattr(catalog[rid].equipment, key) - spent)
                balance[key] -= needed
                if needed and balance[key] < 0:
                    deficits[key] = -balance[key]
                    blocked.append(rid)
        if blocked:
            detail = ', '.join(f"{'Роутеры' if k == 'router' else 'ТВ-приставки'}: не хватает {v}" for k, v in deficits.items())
            add('equipment-' + eid, 'equipment', 'fact', 'Не хватает оборудования · ' + name(eid),
                detail + '. По учётному остатку инженера; наличие у другой бригады не означает подтверждённую передачу.',
                blocked, eid, action='resources')

    for (eid, rid), visit in remaining.items():
        if rid in missing or visit['start'] < now:
            continue
        req = catalog[rid]
        windows = [w for w in req.windows if w.start <= visit['start'] <= w.end]
        buffer = max(w.end for w in windows) - visit['start'] if windows else None
        tight = buffer is not None and buffer <= TIGHT_MINUTES
        risk = model_visits.get(rid, {})
        sla = risk.get('p_sla_breach', 0)
        late = risk.get('expected_late_minutes', 0)
        model_attention = signal(risk, 'sla') or signal(risk, 'late')
        outside_window = not windows
        deadline_signal = signal(risk, 'sla') or outside_window or visit['start'] > request_info(rid, visit['start'])['deadline']
        if not tight and not model_attention and not deadline_signal:
            continue
        if tight:
            tight_ids.add(rid)
        detail = (f'От планового начала {_clock(visit["start"])} до конца подходящего окна — {buffer} мин. '
                  if buffer is not None else 'Начало по плану вне клиентских окон. ')
        if model_attention:
            detail += (f'Срок нарушен в {frequency_label(sla)} модельных сценариев. ' if signal(risk, 'sla') else '')
            if signal(risk, 'late'):
                detail += f'Средний сдвиг начала по сценариям: {round(late)} мин. '
            detail += 'В расчёте варьируются дорога и длительность работ. Уточните текущую работу у инженера.'
        else:
            detail += 'Это запас по расписанию, не фактическое опоздание.'
        add('visit-' + rid, 'window' if tight or outside_window else 'late', 'forecast' if model_attention else 'plan',
            'Начало вне клиентского окна' if outside_window else 'Малый запас до конца окна' if tight else 'Риск опоздания', detail, [rid], eid,
            [k for k, yes in [('tight', tight), ('deadline', deadline_signal)] if yes],
            extra={'buffer_minutes': buffer, 'deadline_request_ids': [rid] if deadline_signal else []})

    # A completion awaits approval even after the worker sent a later status.
    confirmations = {}
    for fact in [*session.fact_log, *({**status, 'engineer_id': eid} for eid, status in session.statuses.items())]:
        if fact.get('status') == 'completion_pending' and fact.get('request_id') in catalog:
            eid = fact.get('engineer_id')
            if eid:
                confirmations.setdefault(eid, set()).add(fact['request_id'])
    for eid, ids in confirmations.items():
        add('completion-' + eid, 'confirmation', 'unknown', 'Подтвердить результат · ' + name(eid),
            f'Ожидают подтверждения сообщения о завершении: {len(ids)}. Результат ещё не подтверждён диспетчером.',
            ids, eid, ('verify',), 'completions')

    # Current statuses, not every historical fact. An old "working" record must
    # not overwrite a later completion or make its remaining time roll forward.
    for eid, status in session.statuses.items():
        rid = status.get('request_id')
        if status.get('status') != 'working' or rid in done or rid not in catalog:
            continue
        visit = all_visits.get((eid, rid))
        stamp, left = status.get('time'), status.get('remaining_minutes')
        if not visit or not isinstance(stamp, (int, float)) or not isinstance(left, (int, float)):
            continue
        end = stamp + left
        if end <= now:
            add('delay-' + eid, 'delay', 'unknown', 'Нужно уточнить окончание · ' + name(eid),
                f'В {_clock(stamp)} инженер сообщил остаток {left} мин. Расчётное окончание {_clock(end)} уже прошло; подтверждения завершения нет.',
                [rid], eid, ('verify',), deadline=end)
        elif end > visit['end']:
            following, threatened, cursor = [], [], end
            for next_visit in sorted((v for (owner, key), v in remaining.items() if owner == eid and key != rid
                                      and key not in missing and v['start'] >= visit['start']), key=lambda v: v['start']):
                start = max(next_visit['start'], cursor + next_visit['travel_minutes'])
                if start > next_visit['start']:
                    following.append(next_visit['request_id'])
                if start > request_info(next_visit['request_id'], next_visit['start'])['deadline']:
                    threatened.append(next_visit['request_id'])
                cursor = start + next_visit['end'] - next_visit['start']
            add('delay-' + eid, 'delay', 'forecast', 'Работа дольше плана · ' + name(eid),
                f'По сообщению в {_clock(stamp)} окончание ожидается в {_clock(end)}, по плану — {_clock(visit["end"])}. '
                f'При прежнем порядке могут сдвинуться следующие визиты: {len(following)}. Уточните остаток работы.',
                [rid, *following], eid, ('verify', 'deadline') if threatened else ('verify',), deadline=visit['end'],
                extra={'deadline_request_ids': threatened})

    if usable:
        for route in forecast.get('routes', []):
            eid = route['engineer_id']
            ids = [v['request_id'] for v in route['visits'] if (eid, v['request_id']) in remaining and v['request_id'] not in missing]
            if not ids or not signal(route, 'overtime'):
                continue
            worker = next((e for e in session.scenario.engineers if e.id == eid), None)
            add('overtime-' + eid, 'overtime', 'forecast', 'Возможна переработка · ' + name(eid),
                f'Средняя переработка по модельным сценариям — около {round(route["expected_overtime_minutes"])} мин. '
                'Задержки дороги и работ могут сдвинуть окончание маршрута. Проверьте последние визиты.',
                ids, eid, ('overtime',), 'preview', deadline=worker.shift.end if worker else None,
                extra={'expected_overtime_minutes': route['expected_overtime_minutes']})

    for vehicle in fleet['rows']:
        eid = vehicle['engineer_id']
        ids = [rid for owner, rid in remaining if owner == eid and rid not in missing]
        if not ids or vehicle['status'] in ('not_linked', 'insufficient_data'):
            continue
        if vehicle['status'] == 'unavailable' or vehicle.get('band') in ('high', 'critical'):
            # If the engineer already reported an outage, don't repeat that incident.
            if any(r['id'] == 'absence-' + eid for r in rows):
                continue
            add('vehicle-' + eid, 'vehicle', 'fact', 'Проверить транспорт · ' + name(eid),
                ' '.join(vehicle['reasons']) + ' Это факты эксплуатации, не вероятность поломки.',
                ids, eid, ('verify',), 'resources')

    gps_current = (gps and gps.get('channel') == 'device' and gps.get('plan_version') == session.version
                   and gps.get('plan_date') == session.scenario.date)
    if gps_current:
        for point in gps.get('rows', []):
            eid = point.get('engineer_id')
            ids = [rid for owner, rid in remaining if owner == eid and rid not in missing]
            if not ids or point.get('tracking_state') != 'active':
                continue
            age = point.get('point_age_seconds')
            threshold = gps.get('config', {}).get('stale_seconds', 120)
            stale = not point.get('date_matches') or age is None or age > threshold
            signals = [a for a in point.get('alerts', []) if a.get('code') in ('late', 'tight_window', 'stopped', 'moving_away')]
            if not stale and not signals:
                continue
            detail = ('Нет актуальной координаты. ' + (f'Последняя точка получена {round(age / 60)} мин назад. ' if age is not None else '')
                      if stale else ' '.join(a.get('detail') or a.get('title', '') for a in signals))
            existing = next((r for r in rows if r['id'] == 'delay-' + str(eid)), None)
            if existing:
                existing['detail'] += ' ' + detail
                continue
            target = (point.get('analysis') or {}).get('target') or {}
            target_id = target.get('id')
            add('gps-' + str(eid), 'data' if stale else 'gps', 'unknown',
                ('Нет свежей связи · ' if stale else 'Проверить GPS-сигнал · ') + name(eid),
                detail + ' GPS не подтверждает факт задержки и не меняет назначения.',
                [target_id] if target_id in ids else ids, eid, ('verify',))

    if session.execution_mode == 'manual':
        for eid in sorted({eid for eid, rid in remaining if rid not in missing} - set(session.statuses)):
            ids = [rid for owner, rid in remaining if owner == eid and rid not in missing]
            existing = next((r for r in rows if r['engineer_id'] == eid and r['kind'] in ('gps', 'data')), None)
            if existing:
                existing['detail'] += ' Подтверждённого статуса инженера также нет.'
                continue
            add('status-' + eid, 'data', 'unknown', 'Нет подтверждённого статуса · ' + name(eid),
                'В ручном режиме нет факта о текущей доступности и местоположении инженера.', ids, eid, ('verify',))

    for eid, status in session.statuses.items():
        stamp = status.get('time')
        ids = [rid for owner, rid in remaining if owner == eid and rid not in missing]
        if (not ids or not isinstance(stamp, (int, float)) or now - stamp <= FACT_AGE_MINUTES
                or status.get('status') == 'unavailable'
                or any(r['engineer_id'] == eid and 'verify' in r['categories'] for r in rows)):
            continue
        add('status-age-' + eid, 'data', 'unknown', 'Давно не обновлялся статус · ' + name(eid),
            f'Последний факт в {_clock(stamp)}, {now - stamp} мин назад. Выполнение следующих визитов не подтверждено. '
            f'Порог проверки — {FACT_AGE_MINUTES} мин без нового статуса.', ids, eid, ('verify',))

    # Attach derivative visit alerts to a known obstruction instead of asking
    # the dispatcher to check the same work twice. Keep category evidence.
    for cause in [r for r in rows if r['kind'] in ('delay', 'equipment', 'vehicle')]:
        related = [r for r in rows if r['kind'] in ('late', 'window')
                   and r['engineer_id'] == cause['engineer_id']
                   and set(r['request_ids']) <= set(cause['request_ids'])]
        for row in related:
            cause['categories'] = sorted(set(cause['categories']) | set(row['categories']))
            cause['deadline_request_ids'] = sorted(set(cause.get('deadline_request_ids', [])) | set(row.get('deadline_request_ids', [])))
            cause['detail'] += ' ' + row['detail']
            rows.remove(row)

    # One route inspection; tight-window and deadline filters keep their own counts.
    for eid in sorted({r['engineer_id'] for r in rows if r['kind'] in ('late', 'window')}):
        similar = [r for r in rows if r['kind'] in ('late', 'window') and r['engineer_id'] == eid]
        if len(similar) < 2:
            continue
        ids = {rid for row in similar for rid in row['request_ids']}
        rows = [r for r in rows if r not in similar]
        categories = sorted({c for row in similar for c in row['categories']})
        add('route-late-' + eid, 'late', 'forecast' if any(r['evidence'] == 'forecast' for r in similar) else 'plan',
            ('Проверить сроки визитов · ' if 'deadline' in categories else 'Проверить начало визитов · ') + name(eid),
            f'Требуют проверки {len(ids)} визитов одного маршрута. ' + ' '.join(r['detail'] for r in similar),
            ids, eid, categories, extra={'deadline_request_ids': sorted({rid for r in similar for rid in r.get('deadline_request_ids', [])})})

    tiers = prioritize(rows, now)
    counts = {'unassigned': len(missing), 'tight': len(tight_ids),
              'deadline': len({rid for r in rows for rid in r.get('deadline_request_ids', [])}),
              **{category: len({r['engineer_id'] or r['id'] for r in rows if category in r['categories']})
                 for category in ('verify', 'overtime')}}
    notices = []
    if not gps_current:
        notices.append('Свежие GPS-сигналы для этого плана не подключены. Отсутствие сигнала не означает отсутствие задержек.')
    if fleet['covered'] < fleet['total']:
        notices.append(f"Данные эксплуатации транспорта: {fleet['covered']} из {fleet['total']} автомобильных бригад. Риск поломки без данных не оценивается.")
    forecast_review = review_counts(forecast.get('routes', []),
        {rid for (eid, rid), v in remaining.items() if rid not in missing and v['start'] >= now},
        {eid for eid, rid in remaining if rid not in missing}) if usable else None
    return {'plan_version': session.version, 'time': now, 'items': rows, 'counts': counts,
            'tiers': tiers,
            'incoming': {'new': sum(r.get('status') == 'new' for r in session.incoming_requests),
                         'emergency': sum(r.get('status') == 'new' and r['event']['type'] == 'emergency'
                                          for r in session.incoming_requests)},
            'forecast_review': forecast_review,
            'notices': notices, 'forecast_usable': usable, 'gps_available': bool(gps_current),
            'thresholds': {'tight_minutes': TIGHT_MINUTES, 'sla_probability': SLA_ATTENTION,
                           'act_now_minutes': NOW_MINUTES, 'prepare_minutes': PREPARE_MINUTES,
                           'overtime_probability': OVERTIME_ATTENTION, 'overtime_minutes': MIN_OVERTIME,
                           'fact_age_minutes': FACT_AGE_MINUTES}}
