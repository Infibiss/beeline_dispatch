"""Resettable single-process demo for Render's ephemeral Free service."""
import os
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory

from . import scenario_server
from .auth import validate_password
from .auth_http import PUBLIC_DEMO_PASSWORD


def run(*, root=Path('.')):
    password = os.environ.get('FIELD_ROUTER_ADMIN_PASSWORD', '')
    if not password:
        raise ValueError('Задайте секрет FIELD_ROUTER_ADMIN_PASSWORD в настройках сервиса')
    validate_password(password)
    origin = os.environ.get('FIELD_ROUTER_PUBLIC_ORIGIN') or os.environ.get('RENDER_EXTERNAL_URL')
    if not origin:
        raise ValueError('Укажите public origin: FIELD_ROUTER_PUBLIC_ORIGIN или RENDER_EXTERNAL_URL')
    scenario_server.validate_public_origin(origin)
    port = int(os.environ.get('PORT', '10000'))
    if not 1 <= port <= 65535:
        raise ValueError('PORT должен быть от 1 до 65535')
    source = Path(root) / 'data' / 'demo-road'
    for region in ('Восток', 'Юго-восток', 'Югоцентр'):
        if not (source / (region + '.json')).is_file():
            raise ValueError('Не найден исходный демо-регион: ' + region)
    # A mounted persistent volume is opt-in; the public demo keeps its reset
    # behavior. Never delete or reset an operator-supplied directory.
    persistent = os.environ.get('FIELD_ROUTER_DATA_DIR')
    if persistent:
        Path(persistent).mkdir(parents=True, exist_ok=True)
    with (nullcontext(persistent) if persistent else TemporaryDirectory(prefix='field-router-demo-')) as directory:
        state = Path(directory)
        print('Постоянное хранилище: план, аккаунты и GPS сохраняются.' if persistent else
              'Демо: общий день для всех пользователей; перезапуск восстанавливает исходные сценарии.', flush=True)
        arguments = [
            str(source / 'Восток.json'), '--regions-dir', str(source),
            '--state', str(state / 'state.json'), '--accounts', str(state / 'accounts.sqlite3'),
            '--cache', str(state / 'geo-cache'), '--host', '0.0.0.0', '--port', str(port),
            '--public-origin', origin, '--network', '--solver', 'hybrid',
            '--time-limit', str(scenario_server.DAY_TIME_LIMIT),
            '--morning-time-limit', str(scenario_server.MORNING_TIME_LIMIT),
        ]
        # Never reveal an arbitrary deployment password. This credential was
        # explicitly designated public by the demo owner.
        if password == PUBLIC_DEMO_PASSWORD:
            arguments += ['--public-demo-login']
        routing = os.environ.get('FIELD_ROUTER_GPS_ROUTING')
        if routing:
            arguments += ['--gps-routing', routing]
        scenario_server.main(arguments)


if __name__ == '__main__':
    run()
