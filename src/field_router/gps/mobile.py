"""Revocable, employee-scoped native tracker access (Traccar/OsmAnd HTTP).

Device credentials can ONLY submit GPS; they cannot log in, read assignments,
read other tracks or change plans. Only their SHA-256 digests are persisted.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import secrets
import sqlite3
import time
import threading
import ssl
from urllib.parse import parse_qs, urlsplit

from .contracts import GpsInputError, number


class DeviceRegistry:
    def __init__(self, path, accounts, clock=time.time):
        self.path, self.accounts, self.clock = path, accounts, clock
        self.lock = threading.RLock()
        with closing(self.connect()) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS devices (
                username TEXT PRIMARY KEY, digest TEXT UNIQUE, tracker TEXT,
                region TEXT, engineer TEXT, revision INTEGER, expires REAL)''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=.2)
        db.row_factory = sqlite3.Row
        return db

    def account(self, username):
        with closing(self.accounts.connect()) as db:
            return db.execute('SELECT * FROM accounts WHERE username=?', (username,)).fetchone()

    def issue(self, identity):
        row = self.account(identity['username'])
        if not row or not row['enabled'] or row['role']!='engineer':
            raise GpsInputError('Войдите под учётной записью инженера', 403)
        token = secrets.token_urlsafe(32)
        record = {'username': row['username'], 'digest': hashlib.sha256(token.encode()).hexdigest(),
                  'tracker': 'native-'+secrets.token_hex(12), 'region': identity['region'],
                  'engineer': row['engineer_id'], 'revision': row['revision'], 'expires': self.clock()+43200}
        with closing(self.connect()) as db, db:
            db.execute('DELETE FROM devices WHERE expires<?', (self.clock(),))
            db.execute('INSERT OR REPLACE INTO devices VALUES (:username,:digest,:tracker,:region,:engineer,:revision,:expires)', record)
        return {'device_identifier': token, 'expires_at': record['expires']}

    def status(self, username):
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM devices WHERE username=? AND expires>?', (username,self.clock())).fetchone()
        account = self.account(username)
        return dict(row) if row and account and account['enabled'] and account['revision']==row['revision'] else None

    def identify(self, token):
        if not isinstance(token,str) or not 40 <= len(token) <= 100:
            raise GpsInputError('Недействительный ключ устройства', 401)
        with closing(self.connect()) as db:
            row = db.execute('SELECT username FROM devices WHERE digest=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        record = self.status(row['username']) if row else None
        if not record:
            raise GpsInputError('Ключ отозван или истёк; подключите телефон заново', 401)
        return record

    def revoke(self, username):
        with closing(self.connect()) as db, db:
            db.execute('DELETE FROM devices WHERE username=?', (username,))

    def active_in_region(self, region):
        with closing(self.connect()) as db:
            users = [r[0] for r in db.execute('SELECT username FROM devices WHERE region=? AND expires>?',
                                             (region, self.clock()))]
        return [record for user in users if (record := self.status(user))]


def parse_position(params, now):
    """Do not invent precision from HDOP, Wi-Fi or a missing accuracy field."""
    if params.get('valid', 'true').lower() not in ('true','1'):
        return None
    raw = params.get('timestamp', '')
    try:
        stamp = float(raw)
        if stamp < 100000000000:
            stamp *= 1000
    except ValueError:
        try:
            value = datetime.fromisoformat(raw.replace('Z','+00:00'))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            stamp = value.timestamp()*1000
        except (ValueError, OverflowError):
            raise GpsInputError('Некорректное время GPS') from None
    try:
        lat,lon = params.get('lat'),params.get('lon')
        if lat is None and params.get('location'):
            lat,lon = params['location'].split(',')
        metadata = {}
        if 'provider' in params:
            if params['provider'] not in ('gps','network','fused','browser','unknown'):
                raise ValueError('provider')
            metadata['provider'] = params['provider']
        if 'mock' in params:
            if params['mock'] not in ('true','false','1','0'):
                raise ValueError('mock')
            metadata['is_mock'] = params['mock'] in ('true','1')
        return {**metadata, 'lat': number(float(lat),'lat',-90,90), 'lon': number(float(lon),'lon',-180,180),
                'accuracy_m': number(float(params.get('accuracy',100000)),'accuracy',.1,100000),
                'measured_at_ms': int(number(stamp,'timestamp',0,(now+30)*1000))}
    except (TypeError, ValueError):
        raise GpsInputError('Некорректная координата, точность или время GPS') from None


class NativeGpsApi:
    def __init__(self, accounts, regions, secure, public_origin=None):
        self.regions, self.secure, self.registry = regions, secure, None
        self.public_origin = public_origin
        if accounts:
            try:
                self.registry = DeviceRegistry(accounts.path.with_name('gps-devices.sqlite3'), accounts)
            except (OSError, sqlite3.Error):
                pass  # This optional subsystem cannot prevent server startup.

    def secure_request(self, handler):
        return isinstance(handler.connection,ssl.SSLSocket) or bool(self.public_origin and
            handler.headers.get('Host','').lower()==urlsplit(self.public_origin).netloc)

    def service(self, record):
        if not self.regions.has_engineer(record['region'],record['engineer']):
            raise GpsInputError('Инженер больше не входит в состав', 403)
        *_, context = self.regions.resolve('/r/'+record['region']+'/')
        gps = context['gps'].service
        if not gps:
            raise GpsInputError('GPS временно недоступен',503)
        return gps

    def receive(self, handler):
        if urlsplit(handler.path).path != '/api/gps/native':
            return False
        try:
            if not self.secure_request(handler):
                raise GpsInputError('Для фонового трекера нужен доверенный HTTPS', 403)
            if not self.registry:
                raise GpsInputError('Подключение устройств временно недоступно',503)
            # Protocol allows GET or POST parameters. Reject browser cross-site
            # requests; this endpoint uses a device secret, never session cookies.
            if handler.headers.get('Sec-Fetch-Site') or handler.headers.get('Origin'):
                raise GpsInputError('Используйте нативный трекер',403)
            raw = urlsplit(handler.path).query
            if len(raw)>8192:
                raise GpsInputError('Слишком большой GPS-запрос',413)
            if handler.command=='POST':
                size = int(handler.headers.get('Content-Length',0))
                if not 0 <= size <= 8192:
                    raise GpsInputError('Слишком большой GPS-запрос',413)
                if size:
                    if handler.headers.get('Content-Type','').split(';')[0] != 'application/x-www-form-urlencoded':
                        raise GpsInputError('Ожидаются параметры OsmAnd',415)
                    raw += ('&' if raw else '')+handler.rfile.read(size).decode('utf-8')
            values = parse_qs(raw,keep_blank_values=True,max_num_fields=50)
            if any(len(v)!=1 for v in values.values()) or ('id' in values and 'deviceid' in values):
                raise GpsInputError('Повторяющиеся параметры GPS')
            params = {k:v[0] for k,v in values.items()}
            with self.registry.lock:
                record = self.registry.identify(params.get('id',params.get('deviceid')))
                gps = self.service(record)
                if params.get('event') == 'stop':
                    if handler.command != 'POST':
                        raise GpsInputError('Остановка GPS требует POST',405)
                    if set(params) not in ({'id','event'}, {'deviceid','event'}):
                        raise GpsInputError('Остановка не должна содержать координаты')
                    try:
                        gps.ingest({'engineer_id':record['engineer'],'tracker_id':record['tracker'],'state':'off','point':None})
                    except GpsInputError as exc:
                        if exc.status != 409:
                            raise
                    self.registry.revoke(record['username'])
                    result = {'ok':True,'stopped':True}
                elif params.get('event') == 'heartbeat':
                    if handler.command != 'POST':
                        raise GpsInputError('Сигнал связи требует POST',405)
                    if set(params) not in ({'id','event'}, {'deviceid','event'}):
                        raise GpsInputError('Сигнал связи не должен содержать координаты')
                    result = gps.native_heartbeat(record['engineer'],record['tracker'])
                elif 'event' in params:
                    raise GpsInputError('Неизвестное событие GPS')
                else:
                    point = parse_position(params,gps.clock())
                    result = gps.ingest_native(record['engineer'],record['tracker'],point) if point else {'accepted':False,'reason':'invalid_fix'}
            handler.reply(200,result)  # Traccar removes a buffered sample only after successful delivery.
        except GpsInputError as exc:
            handler.reply(exc.status,{'error':str(exc)})
        except (ValueError,UnicodeError):
            handler.reply(400,{'error':'Некорректный GPS-запрос'})
        except Exception:
            handler.reply(503,{'error':'GPS временно недоступен; повторите отправку'})
        return True

    def describe_connections(self, region, data):
        """Optional dispatcher status; never expose device keys or stop planning."""
        if not self.registry or data.get('channel') != 'device':
            return data
        try:
            key = self.regions.canonical_key(region)
            records = self.registry.active_in_region(key)
            rows = {row['engineer_id']: row for row in data['rows']}
            for record in records:
                if record['engineer'] in rows:
                    rows[record['engineer']]['native_connection'] = self.service(record).tracker_status(
                        record['engineer'], record['tracker'])
        except Exception:
            data['health']['native_status_error'] = True
        return data

    def authenticated(self, handler):
        raw = urlsplit(handler.path)
        # AuthHttp already checked the canonical region and employee role.
        route = '/'+'/'.join(raw.path.split('/')[3:]) if raw.path.startswith(('/r/','/regions/')) else raw.path
        if route != '/api/gps/mobile':
            return False
        try:
            identity = handler.identity
            if not identity or identity['role']!='engineer':
                raise GpsInputError('Подключайте телефон из личного кабинета инженера',403)
            if not self.registry:
                raise GpsInputError('Подключение устройств временно недоступно',503)
            record = self.registry.status(identity['username'])
            gps = self.service({'region':identity['region'],'engineer':identity['engineer_id']})
            if handler.command=='GET':
                row = next(r for r in gps.snapshot()['rows'] if r['engineer_id']==identity['engineer_id'])
                handler.reply(200,{'paired':bool(record),'expires_at':record['expires'] if record else None,
                    'enabled':gps.enabled,
                    'connection':gps.tracker_status(record['engineer'],record['tracker']) if record else {'state':'unpaired'},
                    'secure':self.secure_request(handler),'persistent':bool(gps.history),'storage_error':gps._storage_error,
                    'source':row['source'],'tracking_state':row['tracking_state'],
                    'point_age_seconds':row['point_age_seconds'],'last_accuracy_m':row['last_accuracy_m']})
                return True
            from ..auth_http import AuthHttp
            payload = AuthHttp.payload(handler)
            if set(payload)!={'action'} or payload['action'] not in ('issue','revoke'):
                raise GpsInputError('Выберите подключение или отключение телефона')
            if payload['action']=='issue' and (not self.secure_request(handler) or not gps.history or gps._storage_error or not gps.enabled):
                raise GpsInputError('Нужны HTTPS и работающее хранилище GPS',503)
            with self.registry.lock:
                record = self.registry.status(identity['username'])
                # Revocation must work even if telemetry storage is unavailable
                # or a browser has taken the live-device lease.
                self.registry.revoke(identity['username'])
                if record:
                    try:
                        gps.ingest({'engineer_id':record['engineer'],'tracker_id':record['tracker'],'state':'off','point':None})
                    except GpsInputError:
                        pass
                result = self.registry.issue(identity) if payload['action']=='issue' else {'ok':True}
            handler.reply(200,result)
        except GpsInputError as exc:
            handler.reply(exc.status,{'error':str(exc)})
        except Exception:
            handler.reply(503,{'error':'Подключение GPS временно недоступно'})
        return True
