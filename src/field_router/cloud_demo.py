"""Resettable single-process demo for Render's ephemeral Free service."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from . import scenario_server
from .auth import validate_password


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
    # Every process gets a clean day. Never remove any user-supplied state path.
    with TemporaryDirectory(prefix='field-router-demo-') as directory:
        state = Path(directory)
        print('Демо: общий день для всех пользователей; перезапуск восстанавливает исходные сценарии.', flush=True)
        scenario_server.main([
            str(source / 'Восток.json'), '--regions-dir', str(source),
            '--state', str(state / 'state.json'), '--accounts', str(state / 'accounts.sqlite3'),
            '--cache', str(state / 'geo-cache'), '--host', '0.0.0.0', '--port', str(port),
            '--public-origin', origin, '--network', '--solver', 'hybrid',
            '--time-limit', '3', '--morning-time-limit', '10',
        ])


if __name__ == '__main__':
    run()
