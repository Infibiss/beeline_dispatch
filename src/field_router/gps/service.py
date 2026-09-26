"""Bounded telemetry store and asynchronous model supervisor.

HTTP ingestion never waits for inference or routing. Device telemetry commits
to its own journal; failed storage/model never changes the planning subsystem.
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
from .quality import assess, describe


class GpsService:
    def __init__(self, context, config=None, model='field_router.gps.models:BaselineGpsModel', clock=time.time, enabled=True,
                 history_path=None, routing=None):
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
        self.routing = routing or {}
        self.history = None
        self._storage_error = None
        self._persistent = history_path is not None
        if history_path is not None:
            try:
                from .storage import GpsHistory
                self.history = GpsHistory(history_path, retention_seconds=self.config.retention_seconds,
                                          max_rows=self.config.max_history_rows)
                for eid, (metadata, samples) in self.history.restore(self.clock(), self.config.max_points).items():
                    if eid not in self._context['engineers']:
                        continue
                    stream = self._new_stream()
                    stream.update(metadata)
                    if metadata.get('continuity_version',0)<2:
                        from .continuity import repair_legacy_handoff
                        evidence = self.history.handoff_evidence(eid,stream['tracker_id'],self.config.stale_seconds)
                        if evidence:
                            repair_legacy_handoff(stream, evidence, self._context['engineers'][eid]['transport'], self.config)
                        # Remove the previous heuristic's assertion of certainty.
                        if stream.get('quality_detail',{}).get('ignored_alternate'):
                            stream['quality']='source_conflict'
                            stream['quality_detail'].pop('ignored_alternate',None)
                    stream['continuity_version']=2
                    stream['trace'].extend(samples)
                    latest_reset = max((s['point']['measured_at_ms'] for s in samples if s.get('reacquired')), default=0)
                    stream['points'].extend(s['point'] for s in samples if s['accepted'] and
                        s['point']['measured_at_ms']>=max(latest_reset,stream.get('points_since_ms',0)) and
                        self.clock()-s['point']['measured_at_ms']/1000 <= self.config.history_seconds)
                    good=stream['last_good']
                    if (good and 0 <= self.clock()-good['measured_at_ms']/1000 <= self.config.history_seconds
                        and not any(p['measured_at_ms']==good['measured_at_ms'] for p in stream['points'])):
                        stream['points'].append(good)
                    self._streams[('device', eid)] = stream
                    if stream['points']:
                        self._dirty[('device', eid)] = None
            except Exception as exc:
                self._storage_error = type(exc).__name__
                logging.warning('GPS storage unavailable: %s', self._storage_error)
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
                'last_good': None, 'quality': None, 'quality_detail': {}, 'quality_notice': None,
                'continuity_version': 2, 'points_since_ms': 0,
                'conflict_until': 0, 'conflict_detail': {}, 'recovery': [], 'seq': 0, 'result': None,
                'result_seq': None, 'result_version': None, 'analyzed_at': None, 'result_minute': None, 'demo_minute': None}

    def ingest(self, payload, *, channel='device', demo_minute=None, demo_engineer=None, native_takeover=False):
        if not self.enabled:
            raise GpsInputError('GPS выключен; заявки и статусы доступны как обычно', 503)
        now = self.clock()
        packet = validate_packet(payload, now, self.config)
        eid = packet['engineer_id']
        with self._lock:
            if eid not in self._context['engineers']:
                raise GpsInputError('GPS: неизвестный инженер')
            key = (channel, eid)
            if channel == 'device' and self._persistent and self.history is None:
                raise GpsInputError('Хранилище GPS недоступно. Точка не сохранена; повторите отправку.', 503)
            original = deepcopy(self._streams.get(key)) if channel == 'device' and self.history else None
            was_dirty = key in self._dirty
            stream = self._streams.setdefault(key, self._new_stream())
            received = stream['received_at']
            handoff = (native_takeover and channel == 'device' and packet['tracker_id'].startswith('native-')
                       and not str(stream['tracker_id']).startswith('native-'))
            if channel == 'device' and received is not None:
                if not handoff and stream['tracker_id'] != packet['tracker_id'] and stream['state'] in ('active', 'paused', 'unavailable') and now - received < self.config.lease_seconds:
                    raise GpsInputError('GPS этого инженера уже передаёт другое устройство. Остановите его трекер или дождитесь истечения связи.', 409)
                if stream['tracker_id'] == packet['tracker_id'] and now - received < .4 and packet['state'] == stream['state']:
                    raise GpsInputError('GPS: слишком частые сообщения', 429)
            if stream['tracker_id'] != packet['tracker_id']:
                # A new token is not evidence that the engineer moved. Keep the
                # employee's last position, but reset the device sequence/lease.
                previous_good = deepcopy(stream['last_good'])
                stream = self._new_stream()
                stream['last_good'] = previous_good
                if previous_good and 0 <= now-previous_good['measured_at_ms']/1000 <= self.config.history_seconds:
                    stream['points'].append(previous_good)
                self._streams[key] = stream
            if 'sequence' in packet:
                if packet['sequence'] <= stream['packet_sequence']:
                    return {'accepted': False, 'reason': 'old_message', 'state': stream['state']}
                stream['packet_sequence'] = packet['sequence']
            stream.update(tracker_id=packet['tracker_id'], received_at=now, state=packet['state'],
                          demo_minute=demo_minute, demo_engineer=deepcopy(demo_engineer))
            accepted = False
            sample = None
            point = packet['point']
            if point is not None:
                # A rejected fast source must not discard a slightly delayed
                # valid fix from another source. Accepted time remains monotonic.
                if ((stream['last_good'] and point['measured_at_ms'] <= stream['last_good']['measured_at_ms'])
                    or any(s['point']['measured_at_ms']==point['measured_at_ms'] and
                           s['point'].get('provider')==point.get('provider') for s in stream['trace'])):
                    return {'accepted': False, 'reason': 'duplicate_or_old', 'state': stream['state']}
                if not stream['last_raw'] or point['measured_at_ms'] > stream['last_raw']['measured_at_ms']:
                    stream['last_raw'] = point
                reason, recovered = assess(stream, point, self._context['engineers'][eid]['transport'], self.config, now)
                stream['quality'] = reason or ('source_conflict' if stream.get('conflict_until',0)>now else None)
                if recovered:
                    # Never draw/fit a movement between two incompatible clusters.
                    stream['points'].clear()
                sample = {'point': deepcopy(point), 'received_at': now, 'accepted': reason is None,
                          'reason': reason, 'reacquired': recovered, 'diagnostics': deepcopy(stream['quality_detail'])}
                stream['trace'].append(sample)
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
                    if stream['quality']:
                        stream['result'] = None
                    elif stream['result'] is None and stream['points']:
                        self._dirty[key] = None
                    self._audit.append({'time': now, 'engineer_id': eid, 'channel': channel, 'event': reason})
            if packet['state'] != 'active':
                stream['result'] = None
            if channel == 'device' and self.history:
                try:
                    self.history.save(eid, stream, sample, now)
                    self._storage_error = None
                except Exception as exc:
                    self._storage_error = type(exc).__name__
                    if original is None:
                        self._streams.pop(key, None)
                    else:
                        self._streams[key] = original
                    if not was_dirty:
                        self._dirty.pop(key, None)
                    raise GpsInputError('Точка GPS не сохранена. Повторим после восстановления хранилища.', 503) from None
            self._wake.set()
            rejection = sample.get('reason') if sample else stream['quality']
            return {'accepted': accepted, 'usable': accepted and not stream['quality'],
                    'reason': rejection or stream['quality'], 'state': stream['state'],
                    'detail': describe(rejection or stream['quality'], stream['quality_detail']) if rejection or stream['quality'] else
                              'Координата принята для расчёта.' + (' Позиция восстановлена по серии измерений.' if stream.get('quality_notice') else ''),
                    'buffered_points': len(stream['points'])}

    def clear_demo(self, engineer_id):
        with self._lock:
            self._streams.pop(('demo', engineer_id), None)
            self._dirty.pop(('demo', engineer_id), None)

    def replace_demo(self, engineer_id, packets, minute, engineer, version):
        """Replace a validated replay atomically; concurrent replans win."""
        with self._lock:
            if version != self._context['version']:
                raise GpsInputError(f'Проверка не запущена: выбрана версия № {version}, '
                                    f'а текущая версия — № {self._context["version"]}. Сравните изменения перед повтором.', 409)
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
                if stream:
                    for field in ('last_raw','last_good'):
                        if stream[field] and now-stream[field]['measured_at_ms']/1000 > self.config.retention_seconds:
                            stream[field] = None
                    while stream['trace'] and now-stream['trace'][0]['point']['measured_at_ms']/1000 > self.config.retention_seconds:
                        stream['trace'].popleft()
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
                    detail = describe(stream['quality'], stream.get('quality_detail',{}))
                    messages.append(alert('quality', 'Уточняем местоположение', detail + ' Оценка прибытия появится после уточнения.',
                                          'Подождите обновления. Если положение долго не уточняется, свяжитесь с инженером.'))
                notice = stream.get('quality_notice') if stream else None
                if notice and now-notice['time'] <= 300:
                    messages.append(alert('reacquired','Позиция восстановлена по серии измерений',
                        f"Новый участок трека подтверждён {notice['samples']} согласованными точками за {notice['span_seconds']} сек. Скачок между участками не считается движением.",
                        'Проверьте позицию на карте с инженером. Согласованность измерений не гарантирует их абсолютную точность.','info'))
                if state in ('active', 'unavailable', 'paused') and (point is None or point_age > self.config.stale_seconds):
                    messages.append(alert('stale', 'Нет свежей координаты',
                        'Надёжная точка ещё не получена.' if point is None else f"Последнему надёжному измерению {round(point_age)} сек. Сигнал связи не обновляет возраст GPS.",
                        'Проверьте геолокацию, фоновую работу и связь телефона. Откройте приложение для диагностики; отсутствие GPS не означает недоступность инженера.'))
                if clock['date_matches'] and not engineer.get('status') and engineer['shift']['start'] + self.config.shift_grace_minutes <= clock['minute'] < engineer['shift']['end']:
                    messages.append(alert('shift_unconfirmed', 'Выход на смену не подтверждён',
                        'В журнале нет отметки сотрудника после начала смены.', 'Свяжитесь с инженером. Наличие или отсутствие GPS не заменяет отметку о выходе.'))
                result = None
                if self.enabled and not self._metrics['last_error'] and stream and state == 'active' and not stream['quality'] and point_age is not None and point_age <= self.config.stale_seconds and stream['result_version'] == context['version'] and stream['result_seq'] == stream['seq'] and stream['result_minute'] == clock['minute']:
                    result = deepcopy(stream['result'])
                    if result:
                        if not clock['date_matches']:
                            result.update(alerts=[], eta=None, target=None)
                        messages.extend(result.get('alerts', []))
                from .models import choose_target
                status = engineer.get('status') or {}
                target = next((t for t in engineer['tasks'] if t['id']==status.get('request_id')),None)
                if target is None:target = choose_target(engineer,clock['minute'])[0]
                assignment = None
                if target:
                    assignment={'id':target['id'],'address':target['address'],
                                'distance_m':round(distance(point,target['point'])) if point and target.get('point') else None}
                rows.append({
                    'assignment': assignment,
                    'engineer_id': eid, 'name': original['name'], 'channel': channel, 'tracking_state': state,
                    'heartbeat_age_seconds': round(heartbeat_age) if heartbeat_age is not None else None,
                    'point_age_seconds': round(point_age) if point_age is not None else None,
                    'position': deepcopy(point), 'date_matches': clock['date_matches'],
                    'clock': clock, 'plan_date': context['date'],
                    'last_accuracy_m': stream['last_raw']['accuracy_m'] if stream and stream['last_raw'] else None,
                    'source': 'native' if stream and str(stream['tracker_id']).startswith('native-') else 'browser',
                    'alerts': messages, 'analysis': result, 'analyzed_at': stream['analyzed_at'] if stream else None,
                    'analysis_pending': bool(self.enabled and stream and stream['points'] and state == 'active' and not stream['quality'] and point_age is not None and point_age <= self.config.stale_seconds and result is None),
                    'quality_reason': stream.get('quality') if stream else None,
                    'quality_detail': deepcopy(stream.get('quality_detail',{})) if stream else {},
                    'measurement_provider': stream['last_raw'].get('provider','unknown') if stream and stream['last_raw'] else 'unknown',
                    'trace_points': len(stream['trace']) if stream else 0,
                })
            return {'channel': channel, 'generated_at': now, 'plan_version': context['version'], 'plan_date': context['date'],
                    'rows': rows, 'health': {**self._metrics, 'queue_size': len(self._dirty),
                        'model_entrypoint': self.model_entrypoint, 'circuit_open_seconds': max(0, round(self._open_until - time.monotonic())),
                        'enabled': self.enabled, 'storage': 'sqlite' if self.history else 'bounded_memory',
                        'storage_error': self._storage_error, 'persistent': bool(self.history)},
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

    def delay_draft(self, payload):
        if (not isinstance(payload,dict) or set(payload)!={'version','engineer_id'}
            or type(payload['version']) is not int or not isinstance(payload['engineer_id'],str)):
            raise GpsInputError('Укажите версию плана и инженера')
        with self._lock:
            if payload['version']!=self._context['version']:
                raise GpsInputError('Версия плана изменилась. Загрузите изменения.',409)
            from .delay import draft
            return draft(self._context, self.snapshot('device'), payload['engineer_id'])

    def history_page(self, engineer_id, **options):
        with self._lock:
            if engineer_id not in self._context['engineers']:
                raise GpsInputError('Неизвестный инженер')
        if not self.history:
            raise GpsInputError('Архив GPS недоступен', 503)
        try:
            return {'engineer_id': engineer_id, **self.history.history(engineer_id, self.clock(), **options)}
        except ValueError:
            raise GpsInputError('GPS: неверные параметры истории') from None

    def ingest_native(self, engineer_id, tracker_id, point):
        """Offline fixes are archived without refreshing live position/heartbeat."""
        now = self.clock()
        with self._lock:
            if not self.enabled or not self.history:
                raise GpsInputError('Приём GPS временно недоступен', 503)
            if engineer_id not in self._context['engineers']:
                raise GpsInputError('Инженер больше не входит в состав', 403)
            stream = self._streams.get(('device', engineer_id))
            last = stream['last_good'] if stream else None
            age = now-point['measured_at_ms']/1000
            if age > self.config.retention_seconds:
                return {'accepted': False, 'reason': 'expired_history'}
            if age > self.config.stale_seconds or (last and point['measured_at_ms'] <= last['measured_at_ms']):
                self.history.archive(engineer_id, tracker_id, {'point': point, 'received_at': now,
                    'accepted': False, 'reason': 'historical_only'}, now)
                return {'accepted': False, 'reason': 'historical_only'}
            # Preparing native access does not interrupt the browser. A verified
            # native device takes over only with a fresh, sufficiently precise fix.
            owns_stream = stream and stream['tracker_id'] == tracker_id
            if not owns_stream and (point['accuracy_m'] > self.config.max_accuracy_m or point.get('is_mock')):
                self.history.archive(engineer_id, tracker_id, {'point': point, 'received_at': now,
                                     'accepted': False, 'reason': 'mock' if point.get('is_mock') else 'low_accuracy'}, now)
                return {'accepted': False, 'usable': False, 'reason': 'mock' if point.get('is_mock') else 'low_accuracy'}
            return self.ingest({'engineer_id': engineer_id, 'tracker_id': tracker_id, 'state': 'active', 'point': point},
                               native_takeover=True)

    def tracker_status(self, engineer_id, tracker_id):
        """Confirm reception independently of merely issuing a device key."""
        with self._lock:
            now = self.clock()
            sample = self.history.latest(engineer_id, tracker_id, now) if self.history else None
            result = {'state': 'awaiting', 'point_age_seconds': None, 'accuracy_m': None}
            if not sample:
                return result
            age = max(0, now-sample['point']['measured_at_ms']/1000)
            result.update(point_age_seconds=round(age), accuracy_m=sample['point']['accuracy_m'],
                          provider=sample['point'].get('provider','unknown'), accepted=sample['accepted'],
                          reason=sample.get('reason'),
                          detail=describe(sample.get('reason'),sample.get('diagnostics',{})) if sample.get('reason') else '')
            stream = self._streams.get(('device', engineer_id))
            owns_stream = stream and stream['tracker_id'] == tracker_id
            if owns_stream and stream['state'] == 'off':
                result['state'] = 'off'
            elif age > self.config.stale_seconds:
                result['state'] = 'stale'
            elif (owns_stream and stream['state']=='active' and not stream.get('quality')
                  and stream.get('last_good') and 0 <= now-stream['last_good']['measured_at_ms']/1000 <= self.config.stale_seconds):
                good=stream['last_good']
                result.update(state='active',position_measured_at_ms=good['measured_at_ms'],
                              position_accuracy_m=good['accuracy_m'],
                              detail='Местоположение обновляется.' if sample['accepted'] else 'Неточное измерение пропущено. На карте используются свежие согласованные координаты.')
            elif owns_stream and stream.get('quality') == 'source_conflict':
                result.update(state='waiting_fix', reason='source_conflict',
                              detail=describe('source_conflict',stream.get('quality_detail',{})))
            elif not sample['accepted']:
                result['state'] = 'low_accuracy' if sample['reason'] == 'low_accuracy' else 'waiting_fix'
            elif owns_stream and stream['state'] == 'active':
                result['state'] = 'active'
            else:
                result['state'] = 'waiting_fix'
            return result

    def native_heartbeat(self, engineer_id, tracker_id):
        """Contact is not a fix; never take a browser's lease or refresh GPS age."""
        with self._lock:
            stream = self._streams.get(('device', engineer_id))
            if stream and stream['tracker_id'] != tracker_id and stream['state'] in ('active','paused','unavailable'):
                return {'accepted':False, 'reason':'awaiting_first_fix'}
            state = 'active' if stream and stream['tracker_id']==tracker_id and stream['last_good'] else 'unavailable'
            return self.ingest({'engineer_id':engineer_id,'tracker_id':tracker_id,'state':state,'point':None})

    def _queue_clock_updates(self):
        now = self.clock()
        for key, stream in self._streams.items():
            if (stream['state'] != 'active' or not stream['last_good'] or stream['quality']
                    or now-stream['last_good']['measured_at_ms']/1000 > self.config.stale_seconds):
                continue
            minute = plan_clock(now,self.config,self._context['date'],stream.get('demo_minute'))['minute']
            if stream['result_minute'] != minute:
                self._dirty.setdefault(key,None)

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
            if not isinstance(eta.get('description'),str) or len(eta['description'])>2000:
                raise ValueError('model_schema')
            if 'schedule' in eta:
                schedule=eta['schedule']
                fields={'now_minute','planned_start','arrival_low','arrival_high','leave_by','slack_minutes','remaining_work_minutes'}
                if (not isinstance(schedule,dict) or set(schedule)!=fields|{'source'}
                    or schedule['source'] not in ('server','demo')
                    or any(type(schedule[k]) is not int or not -100000<=schedule[k]<=100000 for k in fields)):
                    raise ValueError('model_schema')
            if 'route' in eta:
                route = eta['route']
                if (not isinstance(route,dict) or not isinstance(route.get('legs'),list) or
                    len(route['legs'])>100 or not isinstance(route.get('kind'),str)):
                    raise ValueError('model_schema')
                for leg in route['legs']:
                    if (not isinstance(leg,dict) or any(not isinstance(leg.get(k),str) or len(leg[k])>300 for k in ('mode','origin','destination')) or
                        any(type(leg.get(k)) is not int or not 0<=leg[k]<=10000000 for k in ('minutes','metres'))):
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
                self._queue_clock_updates()
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
                       'now': self.clock(), 'config': self.config.public(),
                       'routing': self.routing if key[0] == 'device' else {}}
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
                        current.update(result=result, result_seq=seq, result_version=version, analyzed_at=self.clock(),
                                       result_minute=plan_clock(job['now'],self.config,job['date'],job.get('demo_minute'))['minute'])
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
