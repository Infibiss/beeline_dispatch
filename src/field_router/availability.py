"""Date-aware engineer absence intervals for the single-day planning surface.

Intervals are half-open local wall-clock ranges. They remain in the journal
across reloads; a daily projection uses only its intersection with that day.
"""
from datetime import date, datetime, timezone
import re
from uuid import uuid4


REASONS = {
    'sick': 'Болезнь',
    'transport': 'Проблема с транспортом',
    'personal': 'Личные обстоятельства',
    'other': 'Другая причина',
}
_CLOCK = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')


def minute(value):
    match = _CLOCK.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError('Время укажите в формате ЧЧ:ММ')
    return int(match[1]) * 60 + int(match[2])


def stamp(day, clock):
    try:
        return date.fromisoformat(day).toordinal() * 1440 + minute(clock)
    except (TypeError, ValueError) as exc:
        raise ValueError('Укажите корректные даты и время периода') from exc


def make_period(payload, scenario_day, current_minute, engineer_ids, locations, existing):
    allowed = {'version', 'engineer_id', 'start_date', 'start_time', 'end_date',
               'end_time', 'reason', 'comment', 'location_id'}
    if set(payload) - allowed:
        raise ValueError('Неизвестные поля периода недоступности')
    engineer_id = payload.get('engineer_id')
    if not isinstance(engineer_id, str) or engineer_id not in engineer_ids:
        raise ValueError('Неизвестный инженер')
    start = stamp(payload.get('start_date'), payload.get('start_time'))
    end = stamp(payload.get('end_date'), payload.get('end_time'))
    today = date.fromisoformat(scenario_day).toordinal() * 1440
    if start < today + current_minute:
        raise ValueError('Начало периода не может быть раньше текущего времени плана')
    if end <= start or end - start > 31 * 1440:
        raise ValueError('Период должен длиться от минуты до 31 дня')
    reason = payload.get('reason')
    if reason not in REASONS:
        raise ValueError('Выберите причину недоступности')
    comment = payload.get('comment')
    if not isinstance(comment, str) or not 3 <= len(comment.strip()) <= 1000:
        raise ValueError('Опишите причину для диспетчера: от 3 до 1000 символов')
    location_id = payload.get('location_id')
    shift = engineer_ids[engineer_id].shift
    if start <= today + current_minute < end and shift.start <= current_minute < shift.end:
        if location_id not in locations:
            raise ValueError('Укажите подтверждённое текущее местоположение')
    elif location_id is not None and location_id not in locations:
        raise ValueError('Неизвестная текущая точка')
    if any(item['engineer_id'] == engineer_id and
           not item.get('revoked') and
           start < stamp(item['end_date'], item['end_time']) and
           stamp(item['start_date'], item['start_time']) < end for item in existing):
        raise ValueError('Период пересекается с уже сообщённой недоступностью')
    return {'id': uuid4().hex, 'engineer_id': engineer_id,
            'start_date': payload['start_date'], 'start_time': payload['start_time'],
            'end_date': payload['end_date'], 'end_time': payload['end_time'],
            'reason': reason, 'reason_label': REASONS[reason],
            'comment': comment.strip(), 'location_id': location_id,
            'recorded_at': datetime.now(timezone.utc).isoformat(), 'source': 'engineer'}


def day_interval(period, day):
    """Return [start, end) in minutes of *day*, or None when disjoint."""
    origin = date.fromisoformat(day).toordinal() * 1440
    start = max(0, stamp(period['start_date'], period['start_time']) - origin)
    end = min(1440, stamp(period['end_date'], period['end_time']) - origin)
    return (start, end) if start < end else None


def day_blocks(periods, engineer_id, day):
    return sorted(((block, item) for item in periods if item['engineer_id'] == engineer_id
                   if not item.get('revoked')
                   if (block := day_interval(item, day)) is not None), key=lambda entry: entry[0])


def shift_blocks(periods, engineer_id, day, shift):
    """Only absences intersecting a scheduled shift can disrupt its route."""
    return [(block, item) for block, item in day_blocks(periods, engineer_id, day)
            if block[0] < shift.end and shift.start < block[1]]
