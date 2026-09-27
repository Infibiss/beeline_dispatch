"""Run an isolated, prefilled dispatcher sandbox on loopback.

The default state file is deliberately different from the regular day state.
An existing demo state is reused, never silently replaced.
"""
import argparse
import json
from pathlib import Path

from field_router.models import Plan, Scenario
from field_router.replanning import ScenarioSession
from field_router.scenario_server import create_server


def build_demo(comparison, *, seed_requests=True):
    payload = json.loads(Path(comparison).read_text(encoding='utf-8'))
    scenario = Scenario.model_validate(payload['scenario'])
    plan = next((item for item in payload['plans'] if item['solver'] == 'ortools'), payload['plans'][0])
    session = ScenarioSession(scenario, Plan.model_validate(plan), geography=payload.get('geography'))
    if session.time != 540 or len(scenario.requests) < 60:
        raise ValueError('Этот учебный сценарий рассчитан на исходный демодень Востока с 09:00 и 60+ заявками')

    # Pick busy, on-duty engineers whose future visits will visibly enter the
    # replanning pool. The absence begins before either route's first visit.
    routes = sorted((route for route in session.plan.routes
                     if route.visits and not session.initial_engineers[route.engineer_id].off_duty
                     and session.initial_engineers[route.engineer_id].shift.start <= 545),
                    key=lambda route: (-len(route.visits), route.engineer_id))
    if len(routes) < 2 or any(route.visits[0].start <= 545 for route in routes[:2]):
        raise ValueError('Для демо не найдены два подходящих будущих маршрута')
    for route, reason, comment in zip(routes[:2], ('sick', 'transport'),
                                      ('Демо: инженер сообщил, что заболел.',
                                       'Демо: автомобиль неисправен, выезд невозможен.')):
        session.report_unavailability({
            'version': session.version, 'engineer_id': route.engineer_id,
            'start_date': scenario.date, 'start_time': '09:05',
            'end_date': scenario.date, 'end_time': '18:00',
            'reason': reason, 'comment': comment,
        })

    session.set_planning_settings({'version': session.version, 'mode': 'manual',
                                   'every_n_requests': 5, 'every_n_minutes': None})
    if seed_requests:
        session.receive_requests({'requests': demo_requests(session)})
    return session


def demo_requests(session, *, prefix='demo-inbox', first_minute=550):
    # Distinct already-routable addresses; no geocoder, network, or fabricated
    # travel times are needed to exercise the normal JSON intake path.
    scenario=session.scenario
    requests = sorted((request for request in scenario.requests
                       if not request.id.startswith(('request:demo-stream-', 'emergency:demo-stream-'))),
                      key=lambda request: request.id)
    anchors = [requests[index].id for index in (0, 10, 20, 30, 40, 50)]
    specs = [
        ('new_request', 'local', 30, '10:00', '12:00', 0, 0),
        ('new_request', 'connection', 70, '11:00', '13:00', 1, 0),
        ('new_request', 'addon', 20, '12:00', '14:00', 0, 1),
        ('new_request', 'local', 30, '13:00', '15:00', 0, 0),
        ('new_request', 'connection', 70, '14:00', '16:00', 1, 0),
        ('emergency', None, 80, None, None, 1, 0),
    ]
    incoming = []
    for index, (anchor, spec) in enumerate(zip(anchors, specs), start=1):
        kind, work, duration, start, end, router, tv_box = spec
        minute=first_minute+index-1
        row = {'id': f'{prefix}-{index:02}', 'type': kind,
               'time': f'{minute // 60:02}:{minute % 60:02}',
               'anchor_id': anchor, 'address': session.catalog[anchor].address,
               'duration_minutes': duration, 'router': router, 'tv_box': tv_box}
        if kind == 'new_request':
            row.update(work_kind=work, window_start=start, window_end=end)
        incoming.append(row)
    return incoming


def build_stream_baseline(comparison):
    """A reproducible start, not the event history used to prepare the demo.

    The two absences are initial conditions and their affected visits still need
    replanning. Only setup history is removed; no operational job is discarded.
    """
    session = build_demo(comparison, seed_requests=False)
    pending = set(session.pending_report_ids)
    session.pending_report_ids = []
    for period in session.unavailability:
        was_pending = period['id'] in pending
        period['id'] = 'demo-absence-' + period['engineer_id']
        period['recorded_at'] = session.scenario.date + 'T09:00:00+03:00'
        if was_pending:
            session.pending_report_ids.append(period['id'])
    session.version = 1
    session.history.clear()
    session.dispatch_notifications.clear()
    session.decision_log.clear()
    session.views.clear()
    session.batches.clear()
    session.planning_settings = {'mode': 'manual', 'every_n_requests': 3,
                                 'every_n_minutes': None}
    session._save_view('Начало симуляции')
    return session


def main(argv=None):
    parser = argparse.ArgumentParser(description='Изолированное демо очереди и недоступности инженеров')
    parser.add_argument('--comparison', type=Path, default=Path('data/demo-road/Восток.json'))
    parser.add_argument('--state', type=Path, default=Path('output/local/demo-inbox.json'))
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--seed-only', action='store_true')
    args = parser.parse_args(argv)
    if args.state.resolve() == args.comparison.resolve():
        parser.error('Демо-состояние не должно совпадать с исходными данными')
    if args.state.exists():
        from field_router.regions import load_session
        session = load_session(args.comparison, args.state)
        print('Продолжаем существующую песочницу; файл не перезаписан:', args.state, flush=True)
    else:
        session = build_demo(args.comparison)
        args.state.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.state.with_suffix(args.state.suffix + '.tmp')
        temporary.write_text(json.dumps(session.dump(), ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(args.state)
        print('Создана отдельная песочница:', args.state, flush=True)
    if args.seed_only:
        return
    from field_router.llm import configured
    server = create_server(session, args.state, port=args.port, host='127.0.0.1',
                           time_limit=3, solver='hybrid', disable_gps=True,
                           explainer=configured(), accounts=None)
    print(f'Учебный сценарий: http://127.0.0.1:{server.server_port}/', flush=True)
    print('Шесть JSON-заявок, два недоступных инженера; основной сервер не меняется.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
