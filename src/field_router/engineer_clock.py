"""Operational engineer clock, independent from GPS and the saved plan clock.

Today's work uses server Moscow time. An archived or future scenario keeps its
explicit plan clock; reading a page never advances or rewrites the scenario.
"""
from datetime import datetime, timedelta, timezone


MOSCOW = timezone(timedelta(hours=3), 'Europe/Moscow')


def operational_clock(session, now=None):
    if now is None:
        now = datetime.now(timezone.utc)
    elif isinstance(now, (int, float)):
        now = datetime.fromtimestamp(now, timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError('Серверное время должно содержать часовой пояс')
    local = now.astimezone(MOSCOW)
    server_date = local.date().isoformat()
    server_time = local.hour * 60 + local.minute
    mode = 'live' if session.scenario.date == server_date else 'plan'
    minute = server_time if mode == 'live' else session.time
    return {'mode': mode, 'date': session.scenario.date, 'time': minute,
            'plan_time': session.time, 'server_date': server_date,
            'server_time': server_time, 'timezone': 'Europe/Moscow',
            'key': f'{mode}/{session.scenario.date}/{minute}'}


def require_current_write(session, payload, clock, *, expected_context=None):
    """Keep optimistic version validation before live-clock conflict checks."""
    if type(payload.get('version')) is not int or payload['version'] != session.version:
        raise ValueError('Устаревшая версия плана: обновите задания и повторите действие')
    context = f"{clock['mode']}/{clock['date']}/{clock['server_date']}"
    if expected_context is not None and expected_context != context:
        raise ValueError('Устаревшая версия рабочего дня: обновите задания и повторите действие')
    if clock['mode'] == 'live' and clock['time'] < session.time:
        raise ValueError('Время плана опережает текущее время. Обратитесь к диспетчеру; отметка не сохранена.')
