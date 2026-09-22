"""Independent region sessions selected by URL, never by a shared active flag."""
import json
import hashlib
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

REGIONS = {'east': 'Восток', 'south-east': 'Юго-восток', 'south-center': 'Югоцентр'}


def _legacy_key(comparison):
    return hashlib.sha256(Path(comparison).stem.encode()).hexdigest()[:12]


def discover_regions(comparison, state_path, directory=None):
    comparison, state_path = Path(comparison), Path(state_path)
    primary_label = json.loads(comparison.read_text(encoding='utf-8'))['scenario']['region']
    explicit_directory = directory is not None
    directory = Path(directory) if directory is not None else comparison.parent
    sources = {key: {'label': label, 'comparison': directory / (label + '.json'),
                  'state': state_path.with_name(state_path.stem + '-' + key + state_path.suffix)}
            for key, label in REGIONS.items() if (directory / (label + '.json')).is_file()}
    if directory.resolve() != comparison.parent.resolve():
        # Keep existing account region IDs and saved filenames when selecting a
        # different comparison directory. Additional regions get stable IDs.
        primary_key = next((key for key, label in REGIONS.items() if label == primary_label), 'current')
        sources[primary_key] = {'label': primary_label, 'comparison': comparison, 'state': state_path}
    if explicit_directory:
        known = {Path(spec['comparison']).resolve() for spec in sources.values()}
        for source in sorted(directory.glob('*.json')):
            if source.resolve() == comparison.resolve() or source.resolve() in known:continue
            label = json.loads(source.read_text(encoding='utf-8'))['scenario']['region']
            key = _legacy_key(source)
            sources[key] = {'label': label, 'comparison': source,
                            'state': state_path.parent / (state_path.stem + '-regions') / (key + '.json')}
    primary_key = next((key for key, spec in sources.items() if spec['label'] == primary_label), None)
    for key, spec in sources.items():
        if key == primary_key:
            spec['state'] = state_path
            continue
        legacy = state_path.parent / (state_path.stem + '-regions') / (_legacy_key(spec['comparison']) + '.json')
        canonical = spec['state']
        if canonical.resolve() == legacy.resolve():
            continue
        # Continue writing the existing workday in place. Never pick one of two
        # competing saves by filename or version and silently abandon the other.
        if canonical.exists() and legacy.exists():
            raise ValueError(f'Два файла состояния района {spec["label"]}: {canonical} и {legacy}. '
                             'Разберите конфликт сохранений перед запуском.')
        if legacy.exists():
            spec['state'] = legacy
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
        self.sources[self.default_key] = {**self.sources.get(self.default_key, {}),
                                         'label': session.scenario.region, 'state': Path(path)}
        self.aliases = {'current': self.default_key}
        if 'current' in self.sources and self.default_key != 'current':
            raise ValueError('ID current зарезервирован для основного района')
        for key, spec in self.sources.items():
            if key == self.default_key or 'comparison' not in spec:
                continue
            alias = _legacy_key(spec['comparison'])
            if (alias in self.sources and alias != key) or (alias in self.aliases and self.aliases[alias] != key):
                raise ValueError('Старый ID района неоднозначен: ' + alias)
            self.aliases[alias] = key
        paths = [Path(spec['state']).resolve() for spec in self.sources.values()]
        if len(paths) != len(set(paths)):
            raise ValueError('Регионам нужны отдельные состояния: разные файлы для каждого района')
        self.lock = threading.Lock()
        self.contexts = {self.default_key: self._context(session, Path(path), True)}

    def _context(self, session, path, primary):
        context = self.factory(session, path, primary)
        # Authorization must not wait for a preview's solver lock. Publish an
        # immutable roster separately from the mutable planning session.
        context['membership_lock'] = threading.Lock()
        context['members'] = frozenset(session.initial_engineers)
        return context

    def has_engineer(self, region_key, engineer_id):
        *_, context = self.resolve('/r/' + region_key + '/')
        with context['membership_lock']:
            return engineer_id in context['members']

    def canonical_key(self, key):
        if key in self.sources:
            return key
        if key in self.aliases:
            return self.aliases[key]
        raise UnknownRegion('Неизвестный район')

    def publish_session(self, region_key, session):
        """Publish a persisted session while the caller holds its planner lock."""
        context = self.contexts[self.canonical_key(region_key)]
        members = frozenset(session.initial_engineers)
        with context['membership_lock']:
            context['holder']['session'] = session
            context['members'] = members

    def resolve(self, raw_path):
        parsed = urlsplit(raw_path)
        key, prefix, relative = self.default_key, '', parsed.path
        if relative.startswith(('/r/', '/regions/')):
            parts = relative.split('/', 3)
            key = self.canonical_key(parts[2])
            prefix = '/' + parts[1] + '/' + parts[2]
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
                self.contexts[key] = self._context(session, state_path, False)
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
