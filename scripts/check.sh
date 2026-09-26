#!/bin/sh
# Run after installing requirements.lock and the project. No provider keys needed.
set -eu
python --version
node --version
node --check src/field_router/static/scenarios.js
node --check src/field_router/static/dispatcher.js
node --check src/field_router/static/crew.js
node --check src/field_router/static/app-context.js
node --check src/field_router/static/login.js
node --check src/field_router/static/accounts.js
node --check src/field_router/static/engineer.js
node --check src/field_router/static/gps-engineer.js
node --check src/field_router/static/gps-dispatcher.js
node --check src/field_router/static/risk-dashboard.js
python -m pytest -q --junitxml=test-results.xml
python -m pip wheel --no-deps . --wheel-dir dist
python - <<'PY'
from pathlib import Path
from zipfile import ZipFile
wheel = max(Path('dist').glob('field_router-*.whl'), key=lambda p: p.stat().st_mtime)
required = {'field_router/_alns/PROVENANCE.md', 'field_router/_alns/solver.py', 'field_router/_alns/scheduling.py', 'field_router/solvers/alns_solver.py', 'field_router/solvers/selection.py', 'field_router/regions.py', 'field_router/static/app-context.js', 'field_router/static/app-context.css', 'field_router/static/crew.html', 'field_router/static/crew.js', 'field_router/static/crew.css', 'field_router/data/metro.json', 'field_router/data/surface.json', 'field_router/data/SURFACE-SOURCE.txt', 'field_router/data/METRO-LICENSE.txt', 'field_router/static/scenarios.html', 'field_router/static/scenarios.js',
            'field_router/static/dispatcher.js', 'field_router/static/vendor/leaflet.js',
            'field_router/static/vendor/images/marker-icon.png'}
with ZipFile(wheel) as archive:
    required.update({'field_router/risk_dashboard.py', 'field_router/static/risk-dashboard.js',
                     'field_router/static/risk-dashboard.css'})
    required.update({'field_router/auth.py', 'field_router/auth_http.py', 'field_router/gps/replay.py',
                     'field_router/completion.py', 'field_router/ordinary_insertion.py',
                     'field_router/static/login.html', 'field_router/static/login.js', 'field_router/static/auth.css',
                     'field_router/static/accounts.html', 'field_router/static/accounts.js'})
    required.update({'field_router/gps/service.py', 'field_router/gps/models.py',
                     'field_router/gps/storage.py', 'field_router/gps/mobile.py', 'field_router/gps/eta.py',
                     'field_router/static/engineer.html', 'field_router/static/engineer.js', 'field_router/static/engineer.css',
                     'field_router/static/gps-engineer.js', 'field_router/static/gps-dispatcher.js', 'field_router/static/gps.css'})
    missing = required - set(archive.namelist())
    assert not missing, f'Missing runtime assets: {missing}'
print(f'Package verified: {wheel.name}')
PY
