"""Small HTTP boundary: identities are checked before planner/GPS endpoints."""
import hmac
from http.cookies import SimpleCookie, CookieError
import json
from urllib.parse import urlsplit, parse_qs

from .auth import AuthError


_BRAND_FILES = {
    '/brand.css': ('brand.css', 'text/css; charset=utf-8'),
    '/assets/beeline-logo.png': ('brand/beeline-logo.png', 'image/png'),
    '/assets/beeline-symbol.svg': ('brand/beeline-symbol.svg', 'image/svg+xml'),
}


class AuthHttp:
    def __init__(self, store, regions, static, secure):
        self.store, self.regions, self.static, self.secure = store, regions, static, secure

    def cookie_name(self, handler):
        return 'field_router_' + str(handler.server.server_port)

    def token(self, handler):
        try:
            cookie = SimpleCookie(handler.headers.get('Cookie', ''))
            item = cookie.get(self.cookie_name(handler))
            return item.value if item else ''
        except CookieError: return ''

    def set_cookie(self, handler, token, clear=False):
        handler.extra_headers = {'Set-Cookie':
            f'{self.cookie_name(handler)}={token}; Path=/; HttpOnly; SameSite=Strict; '
            + ('Max-Age=0' if clear else 'Max-Age=43200') + ('; Secure' if self.secure else '')}

    def destination(self, identity):
        return '/r/' + identity['region'] + '/engineer' if identity['role']=='engineer' else '/'

    def redirect(self, handler, destination):
        handler.extra_headers = {'Location':destination}
        handler.reply(303, b'', 'text/plain; charset=utf-8')

    def member(self, identity):
        if identity['role'] != 'engineer': return True
        try:
            return self.regions.has_engineer(identity['region'], identity['engineer_id'])
        except (ValueError, KeyError, OSError): return False

    def account_list(self):
        accounts = self.store.list()
        for account in accounts:
            if account['role'] != 'engineer': continue
            try:
                region, *_, context = self.regions.resolve('/r/'+account['region']+'/')
                account['region'] = region
                with context['lock']:
                    engineer = context['holder']['session'].initial_engineers.get(account['engineer_id'])
                    account['engineer_name'] = engineer.name if engineer else 'Удалён из состава'
            except (ValueError, KeyError, OSError):
                account['engineer_name'] = 'Район недоступен'
        return {'accounts': accounts}

    def before(self, handler):
        """True when response sent. Otherwise attach verified identity."""
        raw = urlsplit(handler.path)
        parts = raw.path.split('/')
        regional = len(parts)>3 and parts[1] in ('r','regions')
        region = parts[2] if regional else self.regions.default_key
        route = '/'+'/'.join(parts[3:]) if regional else raw.path
        handler.identity = None
        # Only these public brand resources are needed before login. No
        # directory traversal, arbitrary static files or API access is exposed.
        if handler.command == 'GET' and route in _BRAND_FILES:
            name, kind = _BRAND_FILES[route]
            handler.reply(200, (self.static/name).read_bytes(), kind)
            return True
        if not self.store:
            if route=='/api/auth/me':
                handler.reply(200, {'role':'demo', 'username':'Демонстрация', 'csrf':''})
                return True
            return False
        try:
            if handler.command=='GET' and route in ('/login', '/login.js', '/auth.css'):
                name = 'login.html' if route=='/login' else route[1:]
                kind = 'text/html' if name.endswith('.html') else 'text/javascript' if name.endswith('.js') else 'text/css'
                handler.reply(200, (self.static/name).read_bytes(), kind+'; charset=utf-8')
                return True
            if handler.command=='POST' and route=='/api/auth/login':
                payload = self.payload(handler)
                if set(payload) != {'username','password'}: raise AuthError('Укажите логин и пароль')
                token, identity = self.store.login(payload['username'], payload['password'], handler.client_address[0])
                if not self.member(identity):
                    self.store.logout(token)
                    raise AuthError('Сотрудник больше не входит в состав. Обратитесь к диспетчеру.', 403)
                if identity['role'] == 'engineer':
                    identity['region'] = self.regions.canonical_key(identity['region'])
                self.store.logout(self.token(handler))
                self.set_cookie(handler, token)
                handler.reply(200, {**identity, 'redirect':self.destination(identity)})
                return True
            identity = self.store.identify(self.token(handler))
            if not identity or not self.member(identity):
                if handler.command=='GET' and not route.startswith('/api/'):
                    self.redirect(handler, '/login')
                else: handler.reply(401, {'error':'Войдите в учётную запись'})
                return True
            if identity['role'] == 'engineer':
                identity['region'] = self.regions.canonical_key(identity['region'])
            handler.identity = identity
            if handler.command=='POST' and not hmac.compare_digest(handler.headers.get('X-CSRF-Token',''), identity['csrf']):
                raise AuthError('Сеанс запроса не подтверждён. Обновите страницу.', 403)
            if route=='/api/auth/me' and handler.command=='GET':
                handler.reply(200, {**identity, 'redirect':self.destination(identity)})
                return True
            if route=='/api/auth/logout' and handler.command=='POST':
                self.store.logout(self.token(handler)); self.set_cookie(handler, '', clear=True)
                handler.reply(200, {'ok':True}); return True
            if identity['role']=='engineer':
                if route=='/' and handler.command=='GET':
                    self.redirect(handler, self.destination(identity)); return True
                try:
                    region = self.regions.canonical_key(region)
                except ValueError:
                    raise AuthError('Этот район недоступен для вашей учётной записи', 403) from None
                if region != identity['region']:
                    raise AuthError('Этот район недоступен для вашей учётной записи', 403)
                allowed_get = {'/engineer','/api/engineers','/api/engineer','/api/revision','/api/regions',
                               '/engineer.js','/engineer.css','/gps-engineer.js','/gps-android.js','/apk-download.js','/gps.css',
                               '/app-context.js','/app-context.css','/time-input.js','/api/gps/mobile','/engineer-app.apk'}
                if handler.command=='GET' and route not in allowed_get and not route.startswith('/assets/'):
                    raise AuthError('Раздел доступен только диспетчеру', 403)
                if handler.command=='POST' and route not in {'/api/gps/telemetry','/api/engineer/facts',
                                                               '/api/engineer/unavailability','/api/engineer/decline',
                                                               '/api/engineer/unavailability/update','/api/engineer/unavailability/cancel',
                                                               '/api/engineer/return',
                                                               '/api/gps/mobile'}:
                    raise AuthError('Действие доступно только диспетчеру', 403)
                if route=='/api/engineer' and parse_qs(raw.query).get('engineer_id') != [identity['engineer_id']]:
                    raise AuthError('Доступны только ваши задания', 403)
            if identity['role']=='admin' and route in ('/accounts','/accounts.js','/auth.css'):
                handler.path = route  # Account management is global, not region-local.
                name = 'accounts.html' if route=='/accounts' else route[1:]
                kind = 'text/html' if name.endswith('.html') else 'text/javascript' if name.endswith('.js') else 'text/css'
                handler.reply(200, (self.static/name).read_bytes(), kind+'; charset=utf-8'); return True
            if identity['role']=='admin' and route=='/api/accounts':
                if handler.command=='GET':
                    handler.reply(200, self.account_list()); return True
                payload = self.payload(handler)
                action = payload.get('action')
                if action=='create' and set(payload)=={'action','username','password','region','engineer_id'}:
                    if not isinstance(payload['region'],str):
                        raise AuthError('Выберите район')
                    try:
                        payload['region'] = self.regions.canonical_key(payload['region'])
                    except ValueError:
                        raise AuthError('Выберите район') from None
                    *_, context = self.regions.resolve('/r/'+payload['region']+'/')
                    with context['lock']:
                        if payload['engineer_id'] not in context['holder']['session'].initial_engineers:
                            raise AuthError('Сначала сохраните инженера в составе выбранного района')
                    # SQLite's uniqueness key contains the stored region string.
                    # Also cover existing accounts that still store an old alias.
                    for account in self.store.list():
                        if account['role'] != 'engineer' or account['engineer_id'] != payload['engineer_id']:
                            continue
                        try:
                            existing_region = self.regions.canonical_key(account['region'])
                        except ValueError:
                            continue
                        if existing_region == payload['region']:
                            raise AuthError('Учётная запись этого инженера уже существует', 409)
                    self.store.create_engineer(payload['username'],payload['password'],payload['region'],payload['engineer_id'])
                elif action=='password' and set(payload)=={'action','username','password'}:
                    self.store.update(payload['username'], password=payload['password'])
                elif action=='enabled' and set(payload)=={'action','username','enabled'}:
                    self.store.update(payload['username'], enabled=payload['enabled'])
                else: raise AuthError('Неизвестная операция с учётной записью')
                handler.reply(200, self.account_list()); return True
            return False
        except (AuthError, ValueError, TypeError, KeyError) as exc:
            handler.reply(getattr(exc,'status',400), {'error':str(exc) if isinstance(exc,AuthError) else 'Некорректный запрос'})
            return True

    @staticmethod
    def payload(handler):
        if handler.headers.get('Content-Type','').split(';')[0]!='application/json':
            raise AuthError('Ожидается JSON', 415)
        length = int(handler.headers.get('Content-Length',0))
        if not 0<length<=4096: raise AuthError('Недопустимый размер запроса')
        payload = json.loads(handler.rfile.read(length))
        if not isinstance(payload,dict): raise AuthError('Ожидается объект')
        return payload
