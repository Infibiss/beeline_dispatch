"""Bounded telemetry store and asynchronous model supervisor.

HTTP ingestion never waits for inference. No solver lock, file writes or
network calls here. A failed/hung model is killed; raw telemetry remains usable.
"""
from collections import OrderedDict, deque
from copy import deepcopy
import json
import logging
import multiprocessing
import threading
import time

from .config import GpsConfig
from .contracts import GpsInputError, validate_packet, distance, plan_clock, alert
from .worker import worker_main


class GpsService:
    def __init__(self, context, config=None, model='field_router.gps.models:BaselineGpsModel', clock=time.time, enabled=True):
        self.config = config or GpsConfig()
        self.clock, self.model_entrypoint, self.enabled = clock, model, enabled
        self._lock = threading.RLock()
        self._process_lock = threading.Lock()
        self._context = deepcopy(context)
        if len(context['engineers']) > self.config.max_engineers:
            raise ValueError('GPS: превышен лимит инженеров')
        self._streams = {}
        self._dirty = OrderedDict()
        self._audit = deque(maxlen=200)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._process = self._connection = None
        self._metrics = {'status': 'idle' if enabled else 'disabled', 'processed': 0, 'failures': 0,
                         'timeouts': 0, 'restarts': 0, 'last_error': None, 'last_success': None}
        self._streak = 0
        self._open_until = 0
        self._thread = threading.Thread(target=self._run, name='gps-supervisor', daemon=True)
        self._thread.start()

    def update_context(self, context):
        if len(context['engineers']) > self.config.max_engineers:
            raise ValueError('GPS: превышен лимит инженеров')
        with self._lock:
            changed = context['version'] != self._context['version']
            self._context = deepcopy(context)
            for key, stream in list(self._streams.items()):
                if key[1] not in context['engineers'] or (changed and key[0] == 'demo'):
                    self._streams.pop(key)
                    self._dirty.pop(key, None)
                    continue
                if key[1] in context['engineers'] and stream['points']:
                    stream['result'] = None
                    self._dirty[key] = None
        self._wake.set()

    def _new_stream(self):
        return {'points': deque(maxlen=self.config.max_points), 'trace': deque(maxlen=self.config.max_points),
                'tracker_id': None, 'received_at': None, 'state': 'off', 'last_raw': None, 'packet_sequence': -1,
                'last_good': None, 'quality': None, 'seq': 0, 'result': None,
                'result_seq': None, 'result_version': None, 'analyzed_at': None, 'demo_minute': None}

    def ingest(self, payload, *, channel='device', demo_minute=None, demo_engineer=None):
        if not self.enabled:
            raise GpsInputError('GPS выключен; заявки и статусы доступны как обычно', 503)
        now = self.clock()
        packet = validate_packet(payload, now, self.config)
        eid = packet['engineer_id']
        with self._lock:
            if eid not in self._context['engineers']:
                raise GpsInputError('GPS: неизвестный инженер')
            key = (channel, eid)
            stream = self._streams.setdefault(key, self._new_stream())
            received = stream['received_at']
            if channel == 'device' and received is not None:
                if stream['tracker_id'] != packet['tracker_id'] and stream['state'] in ('active', 'paused', 'unavailable') and now - received < self.config.lease_seconds:
                    raise GpsInputError('GPS этого инженера уже передаёт другое устройство. Остановите его трекер или дождитесь истечения связи.', 409)
                if stream['tracker_id'] == packet['tracker_id'] and now - received < .4 and packet['state'] == stream['state']:
                    raise GpsInputError('GPS: слишком частые сообщения', 429)
            if stream['tracker_id'] != packet['tracker_id']:
                # Never interpret a device change as employee movement.
                stream = self._new_stream()
                self._streams[key] = stream
            if 'sequence' in packet:
                if packet['sequence'] <= stream['packet_sequence']:
                    return {'accepted': False, 'reason': 'old_message', 'state': stream['state']}
                stream['packet_sequence'] = packet['sequence']
            stream.update(tracker_id=packet['tracker_id'], received_at=now, state=packet['state'],
                          demo_minute=demo_minute, demo_engineer=deepcopy(demo_engineer))
            accepted = False
            point = packet['point']
            if point is not None:
                previous = stream['last_good']
                reason = None
                if stream['last_raw'] and point['measured_at_ms'] <= stream['last_raw']['measured_at_ms']:
                    return {'accepted': False, 'reason': 'duplicate_or_old', 'state': stream['state']}
                stream['last_raw'] = point
                if point['accuracy_m'] > self.config.max_accuracy_m:
                    reason = 'low_accuracy'
                elif previous:
                    dt = (point['measured_at_ms'] - previous['measured_at_ms']) / 1000
                    speed_limit = {'car': 60, 'walk': 8, 'bicycle': 20, 'public': 65}[self._context['engineers'][eid]['transport']]
                    displacement = max(0, distance(previous, point) - previous['accuracy_m'] - point['accuracy_m'])
                    if dt <= 0 or displacement / dt > speed_limit:
                        reason = 'jump'
                stream['quality'] = reason
                stream['trace'].append({'point': deepcopy(point), 'received_at': now, 'accepted': reason is None, 'reason': reason})
                if reason is None:
                    stream['last_good'] = point
                    stream['points'].append(point)
                    while stream['points'] and now - stream['points'][0]['measured_at_ms'] / 1000 > self.config.history_seconds:
                        stream['points'].popleft()
                    stream['seq'] += 1
                    stream['result'] = None
                    self._dirty[key] = None
                    accepted = True
                else:
                    stream['result'] = None
                    self._audit.append({'time': now, 'engineer_id': eid, 'channel': channel, 'event': reason})
            if packet['state'] != 'active':
                stream['result'] = None
            self._wake.set()
            return {'accepted': accepted, 'reason': stream['quality'], 'state': stream['state'],
                    'buffered_points': len(stream['points'])}

    def clear_demo(self, engineer_id):
        with self._lock:
            self._streams.pop(('demo', engineer_id), None)
            self._dirty.pop(('demo', engineer_id), None)

    def replace_demo(self, engineer_id, packets, minute, engineer, version):
        """Replace a validated replay atomically; concurrent replans win."""
        with self._lock:
            if version != self._context['version']:
                raise GpsInputError('План изменился. Обновите тестовые параметры.', 409)
            key = ('demo', engineer_id)
            previous = self._streams.get(key)
            was_dirty = key in self._dirty
            self.clear_demo(engineer_id)
            try:
                return [self.ingest(packet, channel='demo', demo_minute=minute, demo_engineer=engineer)
                        for packet in packets]
            except Exception:
                self.clear_demo(engineer_id)
                if previous is not None: self._streams[key] = previous
                if was_dirty: self._dirty[key] = None
                raise

    def context(self):
        with self._lock:
            return deepcopy(self._context)

    def snapshot(self, channel='device'):
        if channel not in ('device', 'demo'):
            raise GpsInputError('Неизвестный источник GPS')
        now = self.clock()
        with self._lock:
            context = self._context
            rows = []
            for eid, original in context['engineers'].items():
                stream = self._streams.get((channel, eid))
                engineer = (stream and stream.get('demo_engineer')) or original
                clock = plan_clock(now, self.config, context['date'], stream.get('demo_minute') if stream else None)
                messages = []
                point = stream['last_good'] if stream else None
                point_age = max(0, now - point['measured_at_ms'] / 1000) if point else None
                heartbeat_age = max(0, now - stream['received_at']) if stream and stream['received_at'] is not None else None
                state = stream['state'] if stream else 'not_started'
                if heartbeat_age is not None and heartbeat_age > self.config.offline_seconds and state in ('active', 'unavailable'):
                    messages.append(alert('no_contact', 'Приложение давно не выходило на связь',
                        f"Последнее сообщение {round(heartbeat_age)} сек назад. Это не доказательство отсутствия сотрудника.",
                        'Свяжитесь с инженером и проверьте интернет или закрытое приложение.'))
                if state == 'paused':
                    messages.append(alert('paused', 'Передача GPS на паузе', 'Страница скрыта или телефон заблокирован.',
                                          'Попросите открыть страницу инженера. Фоновый трекинг браузером не гарантируется.', 'info'))
                elif state in ('off', 'denied'):
                    messages.append(alert('tracking_off', 'Геолокация выключена' if state == 'off' else 'Нет разрешения на геолокацию',
                        'Телефон не передаёт новые координаты.', 'При необходимости инженер может включить GPS в своей странице.', 'info'))
                if stream and stream['quality']:
                    detail = 'Погрешность последнего измерения слишком велика.' if stream['quality'] == 'low_accuracy' else 'Последняя точка означает неправдоподобный скачок.'
                    messages.append(alert('quality', 'Координата требует проверки', detail + ' Она не используется для оценки маршрута.',
                                          'Дождитесь точного измерения; уточните место у инженера.'))
                if state in ('active', 'unavailable') and (point is None or point_age > self.config.stale_seconds):
                    messages.append(alert('stale', 'Нет свежей координаты',
                        'Надёжная точка ещё не получена.' if point is None else f"Последнему надёжному измерению {round(point_age)} сек. Сигнал связи не обновляет возраст GPS.",
                        'Проверьте разрешение геолокации и попросите открыть приложение; не объявляйте инженера недоступным автоматически.'))
                if clock['date_matches'] and not engineer.get('status') and engineer['shift']['start'] + self.config.shift_grace_minutes <= clock['minute'] < engineer['shift']['end']:
                    messages.append(alert('shift_unconfirmed', 'Выход на смену не подтверждён',
                        'В журнале нет отметки сотрудника после начала смены.', 'Свяжитесь с инженером. Наличие или отсутствие GPS не заменяет отметку о выходе.'))
                result = None
                if self.enabled and not self._metrics['last_error'] and stream and state == 'active' and not stream['quality'] and point_age is not None and point_age <= self.config.stale_seconds and stream['result_version'] == context['version'] and stream['result_seq'] == stream['seq']:
                    result = deepcopy(stream['result'])
                    if result:
                        if not clock['date_matches']:
                            result.update(alerts=[], eta=None, target=None)
                        messages.extend(result.get('alerts', []))
                rows.append({
                    'engineer_id': eid, 'name': original['name'], 'channel': channel, 'tracking_state': state,
                    'heartbeat_age_seconds': round(heartbeat_age) if heartbeat_age is not None else None,
                    'point_age_seconds': round(point_age) if point_age is not None else None,
                    'position': deepcopy(point), 'date_matches': clock['date_matches'],
                    'alerts': messages, 'analysis': result, 'analyzed_at': stream['analyzed_at'] if stream else None,
                    'analysis_pending': bool(self.enabled and stream and stream['points'] and state == 'active' and not stream['quality'] and point_age <= self.config.stale_seconds and result is None),
                    'trace_points': len(stream['trace']) if stream else 0,
                })
            return {'channel': channel, 'generated_at': now, 'plan_version': context['version'], 'plan_date': context['date'],
                    'rows': rows, 'health': {**self._metrics, 'queue_size': len(self._dirty),
                        'model_entrypoint': self.model_entrypoint, 'circuit_open_seconds': max(0, round(self._open_until - time.monotonic())),
                        'enabled': self.enabled, 'storage': 'bounded_memory'},
                    'config': self.config.public(), 'recent_events': list(self._audit)[-20:]}

    def trace(self, engineer_id, channel):
        if channel not in ('device', 'demo'):
            raise GpsInputError('Неизвестный источник GPS')
        with self._lock:
            if engineer_id not in self._context['engineers']:
                raise GpsInputError('Неизвестный инженер')
            stream = self._streams.get((channel, engineer_id))
            return {'engineer_id': engineer_id, 'channel': channel,
                    'samples': deepcopy(list(stream['trace'])) if stream else []}

    def _start_worker(self):
        with self._process_lock:
            if self._process and self._process.is_alive():
                return self._connection
            context = multiprocessing.get_context('spawn')
            parent, child = context.Pipe()
            process = context.Process(target=worker_main, args=(child, self.model_entrypoint), daemon=True, name='gps-model')
            process.start()
            child.close()
            self._process, self._connection = process, parent
        if not parent.poll(self.config.startup_timeout_seconds):
            raise TimeoutError('startup_timeout')
        ready = json.loads(parent.recv_bytes(32768))
        if not ready.get('ready'):
            raise RuntimeError('startup_error')
        return parent

    def _stop_worker(self):
        with self._process_lock:
            process, connection = self._process, self._connection
            self._process = self._connection = None
            if process:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=.5)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=.5)
                if not process.is_alive():
                    process.close()
            if connection:
                connection.close()

    @staticmethod
    def _validate_result(result):
        if not isinstance(result, dict) or set(result) != {'model', 'filtered', 'alerts', 'eta', 'target'}:
            raise ValueError('model_schema')
        if not isinstance(result['model'], str) or len(result['model']) > 100:
            raise ValueError('model_schema')
        if not isinstance(result['alerts'], list) or len(result['alerts']) > 12:
            raise ValueError('model_schema')
        for item in result['alerts']:
            if not isinstance(item, dict) or set(item) != {'code', 'title', 'detail', 'action', 'level'}:
                raise ValueError('model_schema')
            if any(not isinstance(value, str) or len(value) > 1500 for value in item.values()) or item['level'] not in ('info', 'warning'):
                raise ValueError('model_schema')
        if result['filtered'] is not None:
            from .contracts import number
            number(result['filtered'].get('lat'), 'lat', -90, 90)
            number(result['filtered'].get('lon'), 'lon', -180, 180)
            number(result['filtered'].get('speed_mps'), 'speed', 0, 200)
        if result['eta'] is not None:
            eta = result['eta']
            if not isinstance(eta, dict) or any(type(eta.get(k)) is not int or not 0 <= eta[k] <= 100000 for k in ('low_minutes', 'high_minutes')) or eta['low_minutes'] > eta['high_minutes'] or eta.get('approximate') is not True:
                raise ValueError('model_schema')
        if result['target'] is not None:
            target = result['target']
            if not isinstance(target, dict) or not isinstance(target.get('id'), str) or not isinstance(target.get('address'), str):
                raise ValueError('model_schema')
        # Reject NaN/Infinity even from a replacement model.
        json.dumps(result, allow_nan=False)
        return result

    def _run(self):
        while not self._stop.is_set():
            if time.monotonic() < self._open_until or not self.enabled:
                self._stop.wait(.25)
                continue
            self._wake.wait(.25)
            self._wake.clear()
            with self._lock:
                if not self._dirty:
                    continue
                key, _ = self._dirty.popitem(last=False)
                stream = self._streams.get(key)
                if not stream or not stream['points'] or stream['state'] != 'active':
                    continue
                version, seq, tracker = self._context['version'], stream['seq'], stream['tracker_id']
                engineer = stream.get('demo_engineer') or self._context['engineers'][key[1]]
                job = {'points': list(stream['points']), 'engineer': deepcopy(engineer),
                       'date': self._context['date'], 'demo_minute': stream.get('demo_minute'),
                       'now': self.clock(), 'config': self.config.public()}
                self._metrics['status'] = 'running'
            try:
                connection = self._start_worker()
                encoded = json.dumps(job, allow_nan=False).encode('utf-8')
                if len(encoded) > 262144:
                    raise ValueError('job_size')
                connection.send_bytes(encoded)
                if not connection.poll(self.config.model_timeout_seconds):
                    raise TimeoutError('inference_timeout')
                response = json.loads(connection.recv_bytes(32768))
                if response.get('error'):
                    raise RuntimeError('model_error')
                result = self._validate_result(response['result'])
                with self._lock:
                    current = self._streams.get(key)
                    if current and current['tracker_id'] == tracker and current['seq'] == seq and self._context['version'] == version:
                        current.update(result=result, result_seq=seq, result_version=version, analyzed_at=self.clock())
                    self._metrics.update(status='ok', last_success=self.clock(), last_error=None)
                    self._metrics['processed'] += 1
                    self._streak = 0
            except Exception as exc:
                if self._stop.is_set():
                    break
                error_code = str(exc) if isinstance(exc, TimeoutError) else type(exc).__name__
                logging.warning('GPS isolated worker failed: %s', error_code)
                self._stop_worker()
                with self._lock:
                    self._metrics['failures'] += 1
                    self._metrics['restarts'] += 1
                    if isinstance(exc, TimeoutError):
                        self._metrics['timeouts'] += 1
                    self._streak += 1
                    self._metrics.update(status='degraded', last_error=error_code)
                    self._audit.append({'time': self.clock(), 'event': 'model_unavailable'})
                    self._dirty[key] = None
                    if self._streak >= self.config.failure_threshold:
                        self._open_until = time.monotonic() + self.config.cooldown_seconds
                        self._streak = 0
                self._stop.wait(.1)
            with self._lock:
                if self._dirty:
                    self._wake.set()
        self._stop_worker()

    def close(self):
        self._stop.set()
        self._wake.set()
        self._stop_worker()
        self._thread.join(timeout=1)
