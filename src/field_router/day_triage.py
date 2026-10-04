"""Dispatcher review timing, not calibrated probability bands or auto-actions."""

NOW_MINUTES = 30
PREPARE_MINUTES = 120
TIERS = ('now', 'prepare', 'watch')


def clock(value):
    return f'{int(value) // 60:02d}:{int(value) % 60:02d}'


def prioritize(rows, now):
    """Explain and order each incident using deadlines and verified context.

    A large model percentage alone cannot promote an incident to "act now".
    These are transparent review rules, not a prediction of the dispatcher's
    required response time. Viewing the queue never changes the plan.
    """
    for row in rows:
        requests = sorted(row['requests'], key=lambda r: (
            r['planned_start'] if r['planned_start'] is not None else r['deadline'], r['request_id']))
        row['requests'] = requests
        timing_requests = requests
        if row['kind'] in ('late', 'window') and set(row['categories']) & {'deadline', 'tight'}:
            # A morning start-shift warning must not promote an unrelated
            # evening SLA warning merely because both belong to one route.
            timing_requests = [r for r in requests if r['request_id'] in row.get('deadline_request_ids', [])
                               or r.get('tight_window')] or requests
            row['deadline'] = min(r['deadline'] for r in timing_requests)
            row['minutes_to_deadline'] = row['deadline'] - now
        first = timing_requests[0] if timing_requests else None
        row['focus_request_id'] = first['request_id'] if first else None
        next_start = min((r['planned_start'] if r['planned_start'] is not None else
                          min((max(now, w['start']) for w in r['windows'] if w['end'] >= now),
                              default=r['deadline']) for r in timing_requests), default=None)
        until_start = next_start - now if next_start is not None else None
        left = row['minutes_to_deadline']
        kind = row['kind']
        imminent = left is not None and left <= NOW_MINUTES
        soon = until_start is not None and until_start <= PREPARE_MINUTES
        blocking = kind in ('unassigned', 'unavailable', 'equipment', 'vehicle')
        if kind in ('confirmation', 'delay'):
            tier, why = 'now', 'Есть сообщение о текущей работе, требующее проверки.'
        elif blocking:
            immediate = imminent or until_start is not None and until_start <= 60 or row['urgent']
            tier = 'now' if immediate else 'prepare'
            why = 'Без решения заявка остаётся без исполнителя или нужного ресурса.'
        elif kind in ('data', 'gps'):
            tier = 'now' if imminent or until_start is not None and until_start <= 60 else 'prepare'
            why = 'Нет надёжного подтверждения исполнения ближайших визитов.'
        elif kind == 'overtime':
            tier = 'prepare' if left is not None and left <= PREPARE_MINUTES else 'watch'
            why = 'Проверьте возможность разгрузить маршрут до конца смены; переработка пока модельная.'
        elif 'deadline' in row['categories'] or 'tight' in row['categories']:
            immediate = imminent or until_start is not None and until_start <= NOW_MINUTES
            tier = 'now' if immediate else 'prepare' if soon else 'watch'
            why = 'Близится визит с ограничением по сроку.' if tier != 'watch' else 'Визит ещё не скоро; фактическая задержка не подтверждена.'
        else:
            tier, why = 'watch', 'Модельный сдвиг начала сам по себе не подтверждает нарушение клиентского срока.'
        row.update(tier=tier, tier_reason=why, next_visit_at=next_start,
                   severity='high' if tier == 'now' else 'medium' if tier == 'prepare' else 'low')

        target = f"перед заявкой {first['request_id']}" if first else 'перед следующим выездом'
        if first and first['planned_start'] is not None:
            target += f" в {clock(first['planned_start'])}"
        row['impact'] = (f"Проверить в первую очередь — {first['request_id']}" +
                         (f" в {clock(first['planned_start'])}" if first['planned_start'] is not None else '') +
                         f"; срок {clock(first['deadline'])}." +
                         (f" Ещё затронуто: {len(requests) - 1}." if len(requests) > 1 else '')) if first else 'Затронутые заявки нужно уточнить.'
        reason = row['detail'].split('. ')[0].rstrip('.') + '.'
        if kind in ('late', 'window'):
            if row['evidence'] == 'forecast':
                reason = ('Модель даёт сигнал нарушения срока.' if 'deadline' in row['categories'] else
                          'Модель даёт сигнал сдвига начала; нарушение клиентского срока не установлено.')
                reason += ' Фактическая причина задержки пока неизвестна.'
            elif 'deadline' in row['categories']:
                reason = 'Плановое начало не укладывается в применимый срок заявки.'
            if first and first.get('previous_request_id'):
                gap = first.get('schedule_buffer_minutes')
                if gap is not None:
                    reason += f" После заявки {first['previous_request_id']} запас с учётом дороги — {gap} мин по плану."
            step = f'Уточните время освобождения инженера {target}. При подтверждённом сдвиге пересчитайте план.'
        elif kind in ('unassigned', 'unavailable'):
            step = 'Рассмотрите доступные назначения с учётом срока и ресурсов. Новый план потребует отдельного подтверждения.'
        elif kind == 'equipment':
            step = f'Уточните остаток у инженера и возможность передачи оборудования {target}.'
        elif kind == 'vehicle':
            step = 'Уточните доступность транспорта у инженера; при подтверждённом отказе сообщите недоступность и пересчитайте план.'
        elif kind == 'confirmation':
            step = 'Проверьте результат работы и подтвердите завершение либо уточните сообщение инженера.'
        elif kind == 'delay':
            step = 'Свяжитесь с инженером, уточните окончание текущей работы и сохраните фактический остаток времени.'
        elif kind in ('data', 'gps'):
            step = 'Свяжитесь с инженером; уточните текущую работу, местоположение и время освобождения. Отсутствие связи не доказывает опоздание.'
        elif kind == 'overtime':
            step = 'Уточните остаток работ у инженера, затем сравните варианты разгрузки маршрута.'
        else:
            step = 'Уточните сведения у инженера перед изменением плана.'
        row.update(reason=reason, next_step=step)
    rows.sort(key=lambda r: (TIERS.index(r['tier']),
                             r['deadline'] if r['deadline'] is not None else 10**9, r['priority_rank'],
                             r['evidence'] != 'fact', r['id']))
    return {tier: sum(r['tier'] == tier for r in rows) for tier in TIERS}
