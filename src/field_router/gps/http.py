"""Optional HTTP adapter. GPS errors never cross into planning handlers."""
import logging
from urllib.parse import parse_qs

from .config import GpsConfig
from .context import project_context
from .contracts import GpsInputError
from .demo import run_demo
from .replay import replay
from .service import GpsService


class GpsApi:
    def __init__(self, session, *, config=None, model=None, enabled=True):
        self.service = None
        try:
            options = {'model': model} if model else {}
            self.service = GpsService(project_context(session), GpsConfig.load(config), enabled=enabled, **options)
        except Exception:
            logging.exception('GPS initialization failed; planning stays available')

    def update(self, session):
        if self.service:
            try:
                self.service.update_context(project_context(session))
            except Exception:
                # Disable inference against an obsolete plan, leave core commit intact.
                self.service.enabled = False
                logging.exception('GPS context failed; planning commit preserved')

    def request(self, method, path, query='', payload=None):
        try:
            if not self.service:
                return 503, {'error': 'GPS временно недоступен. Планирование и статусы продолжают работать.'}
            params = parse_qs(query, keep_blank_values=True)
            if any(len(v) != 1 for v in params.values()):
                raise GpsInputError('GPS: повторяющиеся параметры')
            if method == 'GET' and path == '/api/gps':
                if set(params) - {'channel'}:
                    raise GpsInputError('GPS: неизвестные параметры')
                return 200, self.service.snapshot(params.get('channel', ['device'])[0])
            if method == 'GET' and path == '/api/gps/trace':
                if set(params) != {'engineer_id', 'channel'}:
                    raise GpsInputError('Укажите engineer_id и channel')
                return 200, self.service.trace(params['engineer_id'][0], params['channel'][0])
            if params:
                raise GpsInputError('GPS: параметры запроса не поддерживаются')
            if method == 'POST' and path == '/api/gps/telemetry':
                return 202, self.service.ingest(payload)
            if method == 'POST' and path == '/api/gps/demo':
                return 202, run_demo(self.service, payload)
            if method == 'POST' and path == '/api/gps/replay':
                return 202, replay(self.service, payload)
            return 404, {'error': 'GPS: endpoint не найден'}
        except GpsInputError as exc:
            return exc.status, {'error': str(exc)}
        except Exception:
            logging.exception('GPS request failed in optional subsystem')
            return 503, {'error': 'GPS временно недоступен. Планирование и статусы продолжают работать.'}

    def close(self):
        if self.service:
            self.service.close()
