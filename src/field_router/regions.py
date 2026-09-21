"""Independent region sessions selected by URL, never by a shared active flag."""
import json
import hashlib
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

REGIONS = {'east': 'Восток', 'south-east': 'Юго-восток', 'south-center': 'Югоцентр'}


def discover_regions(comparison, state_path, directory=None):
    comparison, state_path = Path(comparison), Path(state_path)
    explicit_directory = directory is not None
    directory = Path(directory) if directory is not None else comparison.parent
    sources = {key: {'label': label, 'comparison': directory / (label + '.json'),
                  'state': state_path.with_name(state_path.stem + '-' + key + state_path.suffix)}
            for key, label in REGIONS.items() if (directory / (label + '.json')).is_file()}
    if directory.resolve() != comparison.parent.resolve():
        # Keep existing account region IDs and saved filenames when selecting a
        # different comparison directory. Additional regions get stable IDs.
        primary_label = json.loads(comparison.read_text(encoding='utf-8'))['scenario']['region']
        primary_key = next((key for key, label in REGIONS.items() if label == primary_label), 'current')
        sources[primary_key] = {'label': primary_label, 'comparison': comparison, 'state': state_path}
    if explicit_directory:
        known = {Path(spec['comparison']).resolve() for spec in sources.values()}
        for source in sorted(directory.glob('*.json')):
            if source.resolve() == comparison.resolve() or source.resolve() in known:continue
            label = json.loads(source.read_text(encoding='utf-8'))['scenario']['region']
            key = hashlib.sha256(source.stem.encode()).hexdigest()[:12]
            sources[key] = {'label': label, 'comparison': source,
                            'state': state_path.parent / (state_path.stem + '-regions') / (key + '.json')}
    return sources


class UnknownRegion(ValueError):
    pass


def load_session(comparison, state_path):
    from .models import Scenario, Plan
    from .replanning import ScenarioSession
    payload = json.loads(Path(comparison).read_text(encoding='utf-8'))
    scenario = Scenario.model_validate(payload['scenario'])
    if Path(state_path).exists():
        session = ScenarioSession.restore(json.loads(Path(state_path).read_text(encoding='utf-8')))
        if session.scenario.region != scenario.region:
            raise ValueError('Файл состояния принадлежит другому региону')
        return session
    plan = next((p for p in payload['plans'] if p['solver'] == 'ortools'), payload['plans'][0])
    return ScenarioSession(scenario, Plan.model_validate(plan), geography=payload.get('geography'))


class RegionRegistry:
    def __init__(self, session, path, factory, sources=None):
        self.factory = factory
        self.sources = dict(sources or {})
        if any(not re.fullmatch(r'[a-zA-Z0-9_-]+', key) for key in self.sources):
            raise ValueError('Некорректный ID региона')
        self.default_key = next((key for key, spec in self.sources.items()
                                 if spec['label'] == session.scenario.region), 'default')
        self.sources[self.default_key] = {'label': session.scenario.region, 'state': Path(path)}
        paths = [Path(spec['state']).resolve() for spec in self.sources.values()]
        if len(paths) != len(set(paths)):
            raise ValueError('Регионам нужны отдельные состояния: разные файлы для каждого района')
        self.lock = threading.Lock()
        self.contexts = {self.default_key: factory(session, Path(path), True)}

    def resolve(self, raw_path):
        parsed = urlsplit(raw_path)
        key, prefix, relative = self.default_key, '', parsed.path
        if relative.startswith(('/r/', '/regions/')):
            parts = relative.split('/', 3)
            key = parts[2]
            if key not in self.sources:
                raise UnknownRegion('Неизвестный район')
            prefix = '/' + parts[1] + '/' + key
            relative = '/' + (parts[3] if len(parts) == 4 else '')
        with self.lock:
            if key not in self.contexts:
                from .replanning import ScenarioSession
                spec = self.sources[key]
                state_path = Path(spec['state'])
                if 'session' in spec:
                    session = spec['session']
                elif state_path.exists():
                    session = ScenarioSession.restore(json.loads(state_path.read_text(encoding='utf-8')))
                else:
                    session = load_session(spec['comparison'], state_path)
                if session.scenario.region != spec['label']:
                    raise ValueError('Файл состояния принадлежит другому району')
                self.contexts[key] = self.factory(session, state_path, False)
            context = self.contexts[key]
        return key, prefix, relative + ('?' + parsed.query if parsed.query else ''), context

    def manifest(self, current):
        return {'current': current, 'regions': [
            {'id': key, 'label': spec['label'], 'path': '/r/' + key,
             'name': spec['label'], 'url': '/regions/' + key + '/'}
            for key, spec in self.sources.items()]}

    def close(self):
        for context in self.contexts.values():
            context['gps'].close()
