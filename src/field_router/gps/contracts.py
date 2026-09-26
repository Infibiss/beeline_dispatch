"""Input validation and pure geometry, shared by ingestion and model."""
import math
from datetime import datetime, timezone, timedelta


class GpsInputError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def number(value, name, minimum, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise GpsInputError('GPS: некорректное поле ' + name)
    return value


def validate_packet(payload, now, config):
    allowed = {'engineer_id', 'tracker_id', 'state', 'point', 'sequence'}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise GpsInputError('GPS: неизвестные поля сообщения')
    for key in ('engineer_id', 'tracker_id'):
        if not isinstance(payload.get(key), str) or not 1 <= len(payload[key]) <= 100:
            raise GpsInputError('GPS: укажите ' + key)
    if payload.get('state') not in ('active', 'paused', 'off', 'denied', 'unavailable'):
        raise GpsInputError('GPS: неизвестное состояние трекера')
    if 'sequence' in payload and (type(payload['sequence']) is not int or not 0 <= payload['sequence'] <= 2**53-1):
        raise GpsInputError('GPS: некорректный номер сообщения')
    point = payload.get('point')
    if point is not None:
        if payload['state'] != 'active':
            raise GpsInputError('Координаты принимаются только от включённого трекера')
        required = {'lat', 'lon', 'accuracy_m', 'measured_at_ms'}
        if not isinstance(point, dict) or not required <= set(point) <= required | {'provider','is_mock'}:
            raise GpsInputError('GPS: нужна координата, точность и время измерения')
        metadata = {}
        if 'provider' in point:
            if point['provider'] not in ('gps','network','fused','browser','unknown'):
                raise GpsInputError('GPS: неизвестный источник координаты')
            metadata['provider'] = point['provider']
        if 'is_mock' in point:
            if type(point['is_mock']) is not bool:
                raise GpsInputError('GPS: неверная отметка тестовой координаты')
            metadata['is_mock'] = point['is_mock']
        point = {
            **metadata,
            'lat': number(point.get('lat'), 'lat', -90, 90),
            'lon': number(point.get('lon'), 'lon', -180, 180),
            'accuracy_m': number(point.get('accuracy_m'), 'accuracy_m', .1, 100000),
            'measured_at_ms': number(point.get('measured_at_ms'), 'measured_at_ms',
                                     (now - config.max_sample_age_seconds) * 1000, (now + 30) * 1000),
        }
    return {**payload, 'point': point}


def distance(a, b):
    lat1, lat2 = math.radians(a['lat']), math.radians(b['lat'])
    dlat = lat2 - lat1
    dlon = math.radians(b['lon'] - a['lon'])
    h = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    return 6371000 * 2 * math.asin(math.sqrt(min(1, max(0, h))))


def plan_clock(now, config, date, demo_minute=None):
    local = datetime.fromtimestamp(now, timezone(timedelta(minutes=config.utc_offset_minutes)))
    return {
        'date_matches': demo_minute is not None or local.date().isoformat() == date,
        'minute': demo_minute if demo_minute is not None else local.hour * 60 + local.minute,
        'local_date': local.date().isoformat(),
    }


def alert(code, title, detail, action, level='warning'):
    return {'code': code, 'title': title, 'detail': detail, 'action': action, 'level': level}
