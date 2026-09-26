"""Opt-in local test day: synthetic assignment, real device telemetry.

Never rewrites the source plan or injects its reference fix into the live stream.
Coordinates are only written under the ignored output/local directory.
"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import socket
import sqlite3
import uuid

from ..geo_models import GeoLocation, GeographyBundle
from ..geography import scenario_signature
from ..models import Engineer, Equipment, Request, Scenario, TimeWindow, Transport, Vehicle
from ..planning import make_plan
from ..regions import REGIONS
from ..replanning import ScenarioSession
from ..road_travel import bundle_travel, coordinates_signature
from .config import GpsConfig


def recent_reference(history, engineers, now, *, engineer_id=None, max_accuracy=100):
    """Read the existing WAL journal without changing retention or stream state."""
    history = Path(history).resolve()
    if not history.is_file():
        raise ValueError('Нет GPS-журнала. Сначала включите GPS телефона на обычном сервере.')
    ids = {e.id for e in engineers}
    if engineer_id is not None and engineer_id not in ids:
        raise ValueError('Инженер отсутствует в исходном плане.')
    with closing(sqlite3.connect(history.as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT engineer,data FROM streams').fetchall()
    candidates = []
    for key, raw in rows:
        if key not in ids or (engineer_id is not None and key != engineer_id):
            continue
        point = json.loads(raw).get('last_good')
        if not point:
            continue
        age = now.timestamp() - point['measured_at_ms'] / 1000
        if (0 <= age <= 600 and 0 < point['accuracy_m'] <= max_accuracy
                and all(math.isfinite(point[k]) for k in ('lat','lon','accuracy_m','measured_at_ms'))
                and -90 <= point['lat'] <= 90 and -180 <= point['lon'] <= 180):
            candidates.append((point['measured_at_ms'], key, point))
    if not candidates:
        raise ValueError('Нет надёжной точки за последние 10 минут. На обычном сервере включите GPS телефона, дождитесь точности до 100 м и повторите запуск.')
    _, key, point = max(candidates, key=lambda item: (item[0], item[1]))
    return next(e for e in engineers if e.id == key), point


def make_test_day(engineer, reference, now, *, lead_minutes=5, vehicles=()):
    if now.tzinfo is None:
        raise ValueError('Нужно время с часовым поясом.')
    if type(lead_minutes) is not int or not 1 <= lead_minutes <= 60:
        raise ValueError('Начало заявки должно быть через 1–60 минут.')
    minute = now.hour * 60 + now.minute
    start = minute + lead_minutes
    if start + 35 > 1440 or minute < 60:
        raise ValueError('Для этого теста нужно время между 01:00 и 23:20; план должен целиком оставаться в сегодняшнем дне.')
    date = now.date().isoformat()
    job_id = 'gps-test-001'
    engineer = engineer.model_copy(update={
        'start_location': 'office', 'start_address': None,
        'shift': TimeWindow(start=minute-60, end=min(1440, start+90)),
    })
    label = 'ТЕСТ GPS · условная заявка в 1 км от точки телефона; выезжать не нужно'
    request = Request(id=job_id, region=engineer.region, date=date, address=label,
                      windows=[TimeWindow(start=start, end=start+20)], duration_minutes=10,
                      skill=engineer.skills[0], equipment=Equipment(),
                      provenance={'test': 'Условное задание для проверки реального GPS. Не адрес клиента.'})
    scenario = Scenario(id='gps-test-'+date, region=engineer.region, date=date,
                        office_address='ТЕСТ GPS · исходная точка телефона',
                        engineers=[engineer], requests=[request],
                        vehicles=[v for v in vehicles if v.id==engineer.vehicle_id],
                        metadata={'gps_test': True, 'reference_measured_at_ms': reference['measured_at_ms']},
                        assumptions=['ТЕСТ: заявка условная, GPS поступает с устройства; начало смены задано раньше текущего времени для допустимости плана.',
                                     'Матрица теста оценочная, без дорог и пробок. Выезжать к условной точке не требуется.'])
    # One kilometre along a great circle, bearing east. Valid even near the poles.
    lat, lon, angular = math.radians(reference['lat']), math.radians(reference['lon']), 1000/6371000
    dest_lat = math.asin(math.sin(lat)*math.cos(angular))
    dest_lon = lon + math.atan2(math.sin(angular)*math.cos(lat), math.cos(angular)-math.sin(lat)*math.sin(dest_lat))
    locations = {
        'office': GeoLocation(lat=reference['lat'], lon=reference['lon'], quality='manual',
                              source='Опорная GPS-точка для теста; не новое измерение', label=scenario.office_address),
        job_id: GeoLocation(lat=math.degrees(dest_lat), lon=(math.degrees(dest_lon)+180)%360-180,
                            quality='manual', source='Условная тестовая точка, 1000 м от опорной', label=label),
    }
    speeds = {'car': 25, 'walk': 4.5, 'bicycle': 14, 'public': 4.5}
    minutes = {mode: [[0, math.ceil(1.3/speed*60)], [math.ceil(1.3/speed*60), 0]] for mode, speed in speeds.items()}
    geo = GeographyBundle(scenario_signature=scenario_signature(scenario), locations=locations,
        modes={mode: {'kind': 'estimate', 'source': 'Локальная тестовая оценка по расстоянию; public — пеший подход'} for mode in speeds},
        matrices={'locations': list(locations), 'minutes': minutes,
                  'metres': {mode: [[0,1300],[1300,0]] for mode in speeds},
                  'coordinates_signature': coordinates_signature(locations)},
        metadata={'gps_test': True})
    plan = make_plan(scenario, bundle_travel(geo), {engineer.id: [job_id]}, 'gps-test', 'feasible')
    session = ScenarioSession(scenario, plan, geography=geo.model_dump(mode='json'))
    session.time = minute
    session.execution_mode = 'manual'
    session._save_view('ТЕСТ GPS · сегодняшний план с условной заявкой')
    return session


def prepare(repository, source_state, *, engineer_id=None, history=None, lead_minutes=5, now=None):
    repository, source_state = Path(repository).resolve(), Path(source_state).resolve()
    config = GpsConfig.load(repository/'config/gps.json')
    now = (now or datetime.now(timezone.utc)).astimezone(timezone(timedelta(minutes=config.utc_offset_minutes)))
    source = json.loads(source_state.read_text(encoding='utf-8'))
    engineers = [Engineer.model_validate(e) for e in source['scenario']['engineers']]
    histories=[Path(history)] if history else [source_state.with_suffix('.gps.sqlite3')]
    if history is None:
        # Retests keep their own GPS journal: do not force a detour through the old workday.
        root=repository/'output/local/gps-test'
        if root.is_dir():
            for directory in sorted(root.iterdir(),key=lambda p:p.name,reverse=True)[:20]:
                manifest=directory/'manifest.json'
                try:
                    data=json.loads(manifest.read_text(encoding='utf-8'))
                    if Path(data['source_state']).resolve()==source_state:
                        histories.append(directory/'state.gps.sqlite3')
                except (OSError,ValueError,KeyError):
                    continue
    references=[]
    for candidate in histories:
        try:
            engineer, point=recent_reference(candidate,engineers,now,engineer_id=engineer_id,max_accuracy=config.max_accuracy_m)
            # Do not seed another test from a currently disputed track.
            with closing(sqlite3.connect(candidate.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                row=db.execute('SELECT data FROM streams WHERE engineer=?',(engineer.id,)).fetchone()
            if json.loads(row[0]).get('quality') in ('jump','source_conflict','mock'):
                continue
            references.append((engineer,point))
        except (ValueError,OSError,sqlite3.Error):
            continue
    if not references:
        raise ValueError('Нет свежей пригодной точки за последние 10 минут. Включите GPS телефона на работающем обычном или тестовом сервере и дождитесь принятого измерения без конфликта.')
    chosen,point=max(references,key=lambda pair:pair[1]['measured_at_ms'])
    if chosen.region not in REGIONS.values():
        raise ValueError('Тестовый запуск поддерживает районы Восток, Юго-восток и Югоцентр.')
    vehicles=[Vehicle.model_validate(v) for v in source['scenario'].get('vehicles',[])]
    session = make_test_day(chosen, point, now, lead_minutes=lead_minutes, vehicles=vehicles)
    directory = repository/'output/local/gps-test'/f'{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}'
    directory.mkdir(parents=True, exist_ok=False)
    comparison, state = directory/(chosen.region+'.json'), directory/'state.json'
    def write(path, data):
        with path.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
    write(comparison, {'scenario':session.scenario.model_dump(mode='json'),
                       'plans':[session.plan.model_dump(mode='json')],
                       'geography':session.geography.model_dump(mode='json')})
    write(state, session.dump())
    manifest = {'comparison':str(comparison), 'state':str(state), 'source_state':str(source_state),
                'engineer_id':chosen.id, 'engineer_name':chosen.name, 'date':session.scenario.date,
                'planned_start':session.plan.routes[0].visits[0].start,
                'reference_age_seconds':round(now.timestamp()-point['measured_at_ms']/1000),
                'reference_accuracy_m':round(point['accuracy_m'],1),
                'gps_history':str(state.with_suffix('.gps.sqlite3'))}
    write(directory/'manifest.json', manifest)
    return manifest



def resume_latest(repository, source_state, *, engineer_id=None, now=None):
    """Reuse today's isolated test verbatim, including its existing GPS journal."""
    repository, source_state = Path(repository).resolve(), Path(source_state).resolve()
    config = GpsConfig.load(repository/'config/gps.json')
    now = (now or datetime.now(timezone.utc)).astimezone(timezone(timedelta(minutes=config.utc_offset_minutes)))
    root = repository/'output/local/gps-test'
    directories = sorted(root.iterdir(), key=lambda p:p.name, reverse=True) if root.is_dir() else []
    for directory in directories:
        try:
            data = json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
            if Path(data['source_state']).resolve()!=source_state or data['date']!=now.date().isoformat():
                continue
            if engineer_id and data['engineer_id']!=engineer_id:
                continue
            state, comparison = (Path(data[k]).resolve() for k in ('state','comparison'))
            if any(p.parent!=directory.resolve() or not p.is_file() for p in (state,comparison)):
                continue
            saved = json.loads(state.read_text(encoding='utf-8'))['scenario']
            if saved['date']!=data['date'] or not saved.get('metadata',{}).get('gps_test'):
                continue
            return data
        except (OSError, ValueError, KeyError, TypeError):
            continue
    raise ValueError('Нет сохранённого тестового плана на сегодня. Запустите без -Resume, чтобы создать новый.')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Отдельный план на сегодня для реального GPS телефона')
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--source-state', type=Path, required=True)
    parser.add_argument('--engineer-id')
    parser.add_argument('--gps-history', type=Path)
    parser.add_argument('--lead-minutes', type=int, default=5)
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    repository = args.repository.resolve()
    accounts = args.source_state.resolve().with_name('accounts.sqlite3')
    cert, key = (repository/'output/local/https'/name for name in ('server.crt','server.key'))
    if not all(p.is_file() for p in (accounts,cert,key)):
        parser.error('Нужны существующие учётные записи и HTTPS-сертификат обычного сервера.')
    if not 1 <= args.port <= 65535:
        parser.error('Порт должен быть от 1 до 65535.')
    if not args.prepare_only:
        try:
            with socket.socket() as probe:
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(('0.0.0.0',args.port))
        except OSError:
            parser.error('Порт занят. Остановите обычный сервер через Ctrl+C и повторите команду.')
    try:
        if args.resume:
            manifest = resume_latest(repository, args.source_state, engineer_id=args.engineer_id)
        else:
            manifest = prepare(repository, args.source_state, engineer_id=args.engineer_id,
                               history=args.gps_history, lead_minutes=args.lead_minutes)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    start = manifest['planned_start']
    print(f"ТЕСТ GPS · {manifest['date']} · {manifest['engineer_name']} ({manifest['engineer_id']})",flush=True)
    print(f'Начало условной заявки: {start//60:02d}:{start%60:02d}. Окно: ещё 20 минут. Выезжать к ней не нужно.',flush=True)
    if args.resume:
        print('Продолжаем сохранённый тест. Время плана, статусы и GPS-история сохранены; ждём новые точки телефона.',flush=True)
    else:
        print(f"Опорной точке {manifest['reference_age_seconds']} сек, точность ±{manifest['reference_accuracy_m']} м. Ждём новые точки телефона.",flush=True)
    print('План и GPS-журнал теста: '+str(Path(manifest['state']).parent),flush=True)
    print('Рабочий план сохранён. Вход и настройки учётных записей общие с обычным сервером.',flush=True)
    if args.prepare_only:
        return
    from ..scenario_server import main as serve
    serve([manifest['comparison'], '--state',manifest['state'], '--accounts',str(accounts),
           '--host','0.0.0.0','--port',str(args.port), '--certfile',str(cert),'--keyfile',str(key),
           '--gps-config',str(repository/'config/gps.json')])


if __name__ == '__main__':
    main()
