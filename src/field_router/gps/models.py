"""Baseline: constant-velocity Kalman filter + explicit operational rules.

No learned weights, probability calibration, live traffic, or network calls.
Replace BaselineGpsModel through the worker entrypoint, keeping analyze(job).
"""
import math

from .contracts import distance, plan_clock, alert
from .config import GpsConfig


def smooth_track(points):
    """Two independent position/velocity Kalman filters in local metre axes."""
    if not points:
        return None
    origin = points[0]
    scale_y = 6371000 * math.pi / 180
    scale_x = scale_y * max(.001, math.cos(math.radians(origin['lat'])))
    # x, velocity, P(position), P(cross), P(velocity)
    variance = max(9, (origin['accuracy_m'] / 2.45)**2)
    axes = [[0., 0., variance, 0., 100.] for _ in range(2)]
    previous = origin['measured_at_ms']
    for point in points[1:]:
        dt = max(.001, min(120, (point['measured_at_ms'] - previous) / 1000))
        previous = point['measured_at_ms']
        measurement = [(point['lon'] - origin['lon']) * scale_x, (point['lat'] - origin['lat']) * scale_y]
        r = max(9, (point['accuracy_m'] / 2.45)**2)
        for index, z in enumerate(measurement):
            x, velocity, p00, p01, p11 = axes[index]
            q = .25
            x += velocity * dt
            a = p00 + 2 * dt * p01 + dt * dt * p11 + q * dt**4 / 4
            b = p01 + dt * p11 + q * dt**3 / 2
            c = p11 + q * dt * dt
            k0, k1 = a / (a + r), b / (a + r)
            residual = z - x
            axes[index] = [x + k0 * residual, velocity + k1 * residual,
                           max(.001, (1 - k0) * a), (1 - k0) * b, max(.001, c - k1 * b)]
    return {
        'lat': origin['lat'] + axes[1][0] / scale_y,
        'lon': origin['lon'] + axes[0][0] / scale_x,
        'speed_mps': round(math.hypot(axes[0][1], axes[1][1]), 2),
    }


def choose_target(engineer):
    status = engineer.get('status') or {}
    tasks = engineer['tasks']
    if status.get('status') == 'traveling':
        return next((t for t in tasks if t['id'] == status.get('request_id')), None), 0
    if status.get('status') == 'working':
        current = next((t for t in tasks if t['id'] == status.get('request_id')), None)
        following = [t for t in tasks if current and t['id'] != current['id'] and t['start'] >= current['start']]
        return (following[0] if following else None), status.get('remaining_minutes') or 0
    return None, 0


class BaselineGpsModel:
    name = 'kalman-motion-v1'

    def analyze(self, job):
        config = GpsConfig(**job['config'])
        points = job['points'][-30:]
        filtered = smooth_track(points)
        result = {'model': self.name, 'filtered': filtered, 'alerts': [], 'eta': None, 'target': None}
        if not filtered:
            return result
        clock = plan_clock(job['now'], config, job['date'], job.get('demo_minute'))
        if not clock['date_matches']:
            return result
        engineer = job['engineer']
        target, work_remaining = choose_target(engineer)
        if not target or not target['point']:
            return result
        latest = points[-1]
        if job['now'] - latest['measured_at_ms'] / 1000 > config.stale_seconds:
            return result
        metres = distance(filtered, target['point'])
        result['target'] = {'id': target['id'], 'address': target['address'], 'point': target['point'],
                            'distance_m': round(metres)}
        # An explicitly approximate band, not confidence bounds or OSRM routing.
        slow, fast, boarding = {
            'car': (18, 40, 0), 'walk': (3, 5, 0),
            'bicycle': (10, 18, 0), 'public': (11, 24, 7),
        }[engineer['transport']]
        accuracy = latest['accuracy_m']
        low = math.ceil(max(0, metres - accuracy) * 1.1 / (fast * 1000 / 60) + boarding + work_remaining)
        high = math.ceil((metres + accuracy) * 1.6 / (slow * 1000 / 60) + boarding + work_remaining)
        minute = clock['minute']
        result['eta'] = {'low_minutes': low, 'high_minutes': high, 'approximate': True,
                         'description': 'Расстояние по прямой, поправка на дорогу и тип транспорта; без пробок и дорожного запроса.'}
        if all(minute + low > window['end'] for window in target['windows']):
            result['alerts'].append(alert(
                'late', 'Есть риск не попасть в окно',
                f"До заявки № {target['id']} ориентировочно {low}–{high} мин. Даже быстрая оценка выходит за все окна.",
                'Уточните время приезда у инженера; проверьте перенос или другого исполнителя. Это оценка, не подтверждённое опоздание.'))
        elif all(minute + high > window['end'] for window in target['windows']):
            result['alerts'].append(alert(
                'tight_window', 'Времени на дорогу может не хватить',
                f"Ориентировочная дорога и остаток работы: {low}–{high} мин; медленная оценка выходит за окна заявки № {target['id']}.",
                'Уточните обстановку и проверьте запас времени в плане.', 'info'))
        phase = (engineer.get('status') or {}).get('status')
        if phase != 'traveling':
            return result
        recent = [p for p in points if latest['measured_at_ms'] - p['measured_at_ms'] <= 180000]
        span = (recent[-1]['measured_at_ms'] - recent[0]['measured_at_ms']) / 1000
        if len(recent) >= 3 and span >= config.stationary_seconds:
            spread = max(distance(p, recent[-1]) for p in recent)
            uncertainty = max(p['accuracy_m'] for p in recent)
            if spread <= max(30, uncertainty) and metres > config.near_target_m + accuracy:
                result['alerts'].append(alert(
                    'stopped', 'Остановка во время выезда',
                    f"Несколько точек за {round(span / 60, 1)} мин остаются рядом; до адреса ещё около {round(metres)} м.",
                    'Уточните причину остановки: это может быть светофор, пробка или остановка по делу. При подтверждённой задержке пересчитайте план.'))
        if len(recent) >= 3 and span >= 30:
            first, middle, last = recent[0], recent[len(recent)//2], recent[-1]
            distances = [distance(p, target['point']) for p in (first, middle, last)]
            margin = config.moving_away_m + first['accuracy_m'] + last['accuracy_m']
            if distances[0] < distances[1] < distances[2] and distances[2] - distances[0] > margin:
                result['alerts'].append(alert(
                    'moving_away', 'Расстояние до адреса увеличивается',
                    f"За несколько измерений инженер удалился от заявки примерно на {round(distances[2] - distances[0])} м.",
                    'Проверьте направление и свяжитесь с инженером. Объезд по дороге тоже может временно увеличивать расстояние.'))
        nearby = points[-2:]
        if len(nearby) == 2 and (nearby[1]['measured_at_ms'] - nearby[0]['measured_at_ms']) >= 10000 and all(
                distance(p, target['point']) <= config.near_target_m + p['accuracy_m'] for p in nearby):
            result['alerts'].append(alert(
                'near_site', 'Инженер рядом с адресом',
                f"Две последовательные координаты находятся рядом с заявкой № {target['id']}.",
                'Попросите подтвердить прибытие и начало работы. GPS сам не меняет статус заявки.', 'info'))
        return result
