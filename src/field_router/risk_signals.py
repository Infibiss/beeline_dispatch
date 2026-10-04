"""Shared review rules for the day panel; these are not calibrated risk bands."""
import math


RULES = {
    'sla': {'field': 'p_sla_breach', 'frequency': .25,
            'description': 'Нарушение срока в ≥ 25% сценариев'},
    'late': {'field': 'p_late_start', 'frequency': .5,
             'minutes_field': 'expected_late_minutes', 'minutes': 10,
             'description': 'Позднее начало в ≥ 50% сценариев и средний сдвиг ≥ 10 мин'},
    'overtime': {'field': 'p_overtime', 'frequency': .5,
                 'minutes_field': 'expected_overtime_minutes', 'minutes': 10,
                 'description': 'Переработка в ≥ 50% сценариев и в среднем ≥ 10 мин'},
}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def frequency_label(value):
    """Do not round a sampled near-zero/near-one frequency to certainty."""
    rounded = math.floor(value * 100 + .5)
    if value > 0 and rounded == 0:
        return '<1%'
    if value < 1 and rounded == 100:
        return '>99%'
    return f'{rounded}%'


def known(row, kind):
    rule = RULES[kind]
    value = row.get(rule['field'])
    return (finite(value) and 0 <= value <= 1 and
            ('minutes_field' not in rule or
             finite(row.get(rule['minutes_field'])) and row[rule['minutes_field']] >= 0))


def signal(row, kind):
    rule = RULES[kind]
    return (known(row, kind) and row[rule['field']] >= rule['frequency'] and
            ('minutes_field' not in rule or row[rule['minutes_field']] >= rule['minutes']))


def current_forecast(forecast, context):
    return (forecast.get('status') == 'ready' and not context.get('needs_replanning')
            and forecast.get('plan_version') == context.get('plan_version')
            and forecast.get('region') == context.get('region')
            and ('evaluated_at' not in forecast or forecast['evaluated_at'] == context.get('time')))


def review_counts(routes, visit_ids, engineer_ids):
    """Use the same future visits and active routes as the operational queue.

    Missing or invalid values cannot turn into a reassuring zero. A visit may
    appear under both SLA and late start; these counts must not be added.
    """
    visits = {v['request_id']: v for r in routes for v in r.get('visits', [])
              if v.get('request_id') in visit_ids}
    by_engineer = {r['engineer_id']: r for r in routes if r.get('engineer_id') in engineer_ids}
    result = {'policy': 'day-review-v1', 'visit_ids': sorted(visit_ids),
              'engineer_ids': sorted(engineer_ids)}
    for kind, rule in RULES.items():
        ids, rows = (engineer_ids, by_engineer) if kind == 'overtime' else (visit_ids, visits)
        complete = all(key in rows and known(rows[key], kind) for key in ids)
        result[kind] = {'count': sum(signal(rows[key], kind) for key in ids) if complete else None,
                        'total': len(ids), 'description': rule['description']}
    return result
