#!/bin/sh
# Run after installing requirements.lock and the project. No provider keys needed.
set -eu
python --version
node --version
node --check src/field_router/static/scenarios.js
node --check src/field_router/static/dispatcher.js
node --check src/field_router/static/crew.js
python -m pytest -q --junitxml=test-results.xml
python -m pip wheel --no-deps . --wheel-dir dist
python - <<'PY'
from pathlib import Path
from zipfile import ZipFile
wheel = max(Path('dist').glob('field_router-*.whl'), key=lambda p: p.stat().st_mtime)
required = {'field_router/static/crew.html', 'field_router/static/crew.js', 'field_router/static/crew.css', 'field_router/data/metro.json', 'field_router/data/surface.json', 'field_router/data/SURFACE-SOURCE.txt', 'field_router/data/METRO-LICENSE.txt', 'field_router/static/scenarios.html', 'field_router/static/scenarios.js',
            'field_router/static/dispatcher.js', 'field_router/static/vendor/leaflet.js',
            'field_router/static/vendor/images/marker-icon.png'}
with ZipFile(wheel) as archive:
    missing = required - set(archive.namelist())
    assert not missing, f'Missing runtime assets: {missing}'
print(f'Package verified: {wheel.name}')
PY
