"""Read-only plan indicators, Monte Carlo forecast and fleet data quality."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .advisory import build_advisories
from .fleet_risk import fleet_snapshot
from .risk_engine import (RISK_MODEL_VERSION, RiskEngine, application_risk_config,
                          application_risk_seed)
from .service_observations import confirmed_service_observations
from .availability import shift_blocks
from .day_control import attention
from .risk_signals import frequency_label

SCHEMA_VERSION = '1.0'
WINDOW_BUFFER_MINUTES = 15


@dataclass(frozen=True, slots=True)
class DashboardSessionSnapshot:
    """Small coherent view used outside the dispatcher mutation lock.

    Road matrices, saved workbench views and candidate batches are deliberately
    excluded: the dashboard never reads them, while copying them can exhaust a
    small deployment container.
    """

    scenario: Any
    plan: Any
    version: int
    time: int
    initial_engineers: dict[str, Any]
    statuses: dict[str, Any]
    fact_log: list[dict[str, Any]]
    frozen: list[dict[str, Any]]
    inventory: list[dict[str, Any]]
    catalog: dict[str, Any]
    execution_mode: str
    needs_replanning: bool
    excluded_engineer_ids: frozenset[str]
    unavailability: tuple[dict[str, Any], ...]
    activated_off_duty_windows: dict[str, Any]
    pending_reassignment_request_ids: frozenset[str]
    incoming_requests: list[dict[str, Any]]

    def excluded_engineers(self):
        return set(self.excluded_engineer_ids)

    def pending_reassignment_ids(self):
        return sorted(self.pending_reassignment_request_ids)

    def mode(self):
        return 'manual' if self.execution_mode == 'manual' else 'mixed' if self.statuses else 'simulation'


def capture_dashboard_session(session):
    """Capture only the immutable models and mutable journals used by dashboard()."""
    return DashboardSessionSnapshot(
        scenario=session.scenario,
        plan=session.plan,
        version=session.version,
        time=session.time,
        initial_engineers=dict(session.initial_engineers),
        statuses=deepcopy(session.statuses),
        fact_log=deepcopy(session.fact_log),
        frozen=deepcopy(session.frozen),
        inventory=deepcopy(session.inventory),
        catalog=dict(session.catalog),
        execution_mode=session.execution_mode,
        needs_replanning=session.needs_replanning,
        excluded_engineer_ids=frozenset(session.excluded_engineers()),
        unavailability=tuple(deepcopy(session.unavailability)),
        activated_off_duty_windows=dict(session.activated_off_duty_windows),
        pending_reassignment_request_ids=frozenset(session.pending_reassignment_ids()),
        incoming_requests=deepcopy(session.incoming_requests),
    )


def ratio(numerator, denominator):
    return round(100 * numerator / denominator, 1) if denominator else None


def _occupied(intervals, lower, upper):
    total, end = 0, lower
    for start, stop in sorted(intervals):
        start, stop = max(start, lower, end), min(stop, upper)
        if stop > start:
            total += stop - start
            end = stop
    return total


def forecast_placeholder(context, status='not_connected'):
    """Stable failure envelope: the deterministic indicators remain available."""
    return {'status': status, 'region': context['region'], 'evaluated_at': context['time'],
            'plan_version': context['plan_version'], 'model_version': None,
            'horizon_minutes': None, 'risks': []}


def _top_visit_reason(session, risk, field, title):
    visits = sorted(risk.visits, key=lambda row: getattr(row, field), reverse=True)
    if not visits or getattr(visits[0], field) <= 0:
        return f'{title}: в симуляции не выявлено.'
    visit = visits[0]
    request = session.catalog.get(visit.request_id)
    label = request.address if request else visit.request_id
    return f'{title}: {label} — в {frequency_label(getattr(visit, field))} модельных сценариев.'


def _forecast(session, context, fleet):
    """Calculate a reproducible prior-based forecast for the approved plan."""
    if context['needs_replanning']:
        return forecast_placeholder(context, 'stale')
    evidence = confirmed_service_observations(session)
    try:
        seed = application_risk_seed(
            session.plan,
            session.scenario,
            context={"surface": "approved-plan"},
        )
        engine = RiskEngine(
            application_risk_config(seed),
            service_observations=evidence.observations,
            service_group_key=evidence.group_key(session),
        )
        result = engine.analyze(session.plan, session.scenario)
    except Exception:
        return forecast_placeholder(context, 'error')
    horizon = max((engineer.shift.end for engineer in session.scenario.engineers), default=context['time']) - context['time']
    horizon = max(1, min(1440, horizon))
    vehicle_value = fleet['max_score']
    if vehicle_value is None:
        vehicle_reasons = [f"Данные связаны с 0 из {fleet['total']} автомобильных бригад.",
                           'Без vehicle_id, пробега, сроков ТО и истории отказов вероятность поломки не рассчитывается.']
        vehicle_status = 'insufficient_data'
    else:
        worst = max((row for row in fleet['rows'] if row['score'] is not None), key=lambda row: row['score'])
        vehicle_reasons = [f"Максимальный индекс готовности: {worst['engineer_name']} — {worst['score'] * 100:.0f}/100.",
                           *worst['reasons'][:3]]
        vehicle_status = 'ready'
    risks = [
        {'id': 'sla', 'measure': 'probability', 'value': result.p_sla_breach, 'status': 'ready',
         'reasons': [_top_visit_reason(session, result, 'p_sla_breach', 'Наибольший риск нарушения окна/SLA'),
                     f'Ожидаемо затронуто клиентов: {result.expected_affected_customers:.2f}.'],
         'recommendation': 'Сравните Stable/SLA/Cost по вероятности нарушения и ожидаемому числу затронутых клиентов; применение остаётся за диспетчером.'},
        {'id': 'late', 'measure': 'probability', 'value': result.p_late_start, 'status': 'ready',
         'reasons': [_top_visit_reason(session, result, 'p_late_start', 'Наибольший риск начать позже плана'),
                     'Задержки распространяются по маршруту с учётом ожидания начала клиентского окна.'],
         'recommendation': 'Уточните фактическое окончание текущей работы и GPS. При подтверждённом отклонении рассчитайте варианты заново.'},
        {'id': 'overtime', 'measure': 'probability', 'value': result.p_overtime, 'status': 'ready',
         'reasons': [_top_visit_reason(session, result, 'p_overtime', 'Наибольший риск переработки'),
                     f'Ожидаемая переработка по маршрутам: {result.expected_overtime_minutes:.1f} мин.'],
         'recommendation': 'Проверьте конец смены и последний визит рискованного маршрута; при необходимости выберите более устойчивый вариант.'},
        {'id': 'vehicle', 'measure': 'score', 'value': vehicle_value, 'status': vehicle_status,
         'reasons': vehicle_reasons[:5],
         'recommendation': ('Проверьте ТО и подготовьте резервный автомобиль.' if vehicle_value is not None
                            else 'Загрузите фактические автомобили, показания одометра, ТО и историю отказов.')},
    ]
    payload = result.to_dict()
    engineers = {e.id: e.name for e in session.scenario.engineers}
    for route in payload['routes']:
        route['engineer_name'] = engineers.get(route['engineer_id'], route['engineer_id'])
        for visit in route['visits']:
            request = session.catalog.get(visit['request_id'])
            visit['request_name'] = request.address if request else visit['request_id']
    if evidence.observation_count:
        evidence_notice = (
            f"Доступно {evidence.observation_count} подтверждённых наблюдений "
            f"в {evidence.group_count} группах; они уточняют только совместимые будущие работы. "
        )
    else:
        evidence_notice = "Длительности пока основаны на прозрачных априорных распределениях. "
    return {**payload, 'status': 'ready', 'region': context['region'],
            'plan_version': context['plan_version'], 'evaluated_at': context['time'],
            'scope': 'approved_plan', 'measure_semantics': 'simulation_frequency',
            'model_version': RISK_MODEL_VERSION,
            'method': 'monte_carlo', 'calibrated': False, 'horizon_minutes': horizon,
            'loss_unit': 'условные единицы', 'risks': risks,
            'summary': {key: payload[key] for key in ('p_late_start', 'p_sla_breach', 'p_overtime',
                        'expected_overtime_minutes', 'expected_loss', 'expected_affected_customers',
                        'affected_customer_count')},
            'assumptions': list(result.assumptions),
            'duration_evidence': evidence.metadata(),
            'fleet': fleet,
            'notice': ('Оценка получена методом Монте-Карло. ' + evidence_notice
                       + 'Калибровка вероятностей на независимой истории исходов пока не подтверждена; '
                       'потери указаны в условных единицах.')}


def dashboard(session, gps_snapshot=None):
    now = session.time
    excluded = session.excluded_engineers()
    current = {e.id: e for e in session.scenario.engineers}
    dispatchable = {row['engineer_id'] for row in session.inventory if row['dispatchable']}
    eligible = {}
    for key, initial in session.initial_engineers.items():
        worker = current.get(key)
        if (key in excluded or worker is None or worker.off_duty or key not in dispatchable or
                worker.shift.end <= now or session.statuses.get(key, {}).get('status') == 'unavailable' or
                (session.execution_mode == 'manual' and key not in session.statuses)):
            continue
        effective_shift = session.activated_off_duty_windows.get(key, initial.shift)
        intervals = shift_blocks(session.unavailability, key, session.scenario.date, effective_shift)
        if any(start <= now < end for (start, end), _ in intervals):
            continue
        unknown_ends = [end for (start, end), period in intervals
                        if end <= now and not period.get('location_id')]
        if unknown_ends and not any(
                fact.get('engineer_id') == key and fact.get('status') == 'available' and
                fact.get('time', -1) >= max(unknown_ends) and fact.get('location_id')
                for fact in session.fact_log):
            continue
        next_start = min((start for (start, end), _ in intervals if start > now),
                         default=worker.shift.end)
        end = min(worker.shift.end, next_start)
        if end > max(now, worker.shift.start):
            eligible[key] = worker.model_copy(update={'shift': worker.shift.model_copy(update={'end': end})})
    completed = {f['request_id'] for f in session.fact_log if f['status'] in ('completed', 'completion_pending')}
    pending = set(session.pending_reassignment_ids())
    visits = {}
    for route in session.frozen:
        for visit in route['visits']:
            visits[(route['engineer_id'], visit['request_id'])] = visit
    for route in session.plan.routes:
        for visit in route.visits:
            visits[(route.engineer_id, visit.request_id)] = visit.model_dump()
    visits = {(eid, rid): v for (eid, rid), v in visits.items()
              if eid in eligible and v['end'] > now and rid not in completed and rid not in pending}
    capacity = sum(e.shift.end - max(now, e.shift.start) for e in eligible.values())
    occupied = 0
    for eid, engineer in eligible.items():
        intervals = []
        for (owner, _), visit in visits.items():
            if owner != eid:continue
            intervals += [(visit['arrival'] - visit['travel_minutes'], visit['arrival']),
                          (visit['start'], visit['end'])]
        occupied += _occupied(intervals, max(now, engineer.shift.start), engineer.shift.end)

    tight, checked, min_buffer = 0, 0, None
    for (_, rid), visit in visits.items():
        if visit['start'] < now:continue
        windows = [w for w in session.catalog[rid].windows if w.start <= visit['start'] <= w.end]
        if not windows:continue
        buffer = max(w.end for w in windows) - visit['start']
        checked += 1
        tight += buffer <= WINDOW_BUFFER_MINUTES
        min_buffer = buffer if min_buffer is None else min(min_buffer, buffer)

    supply = {'router': 0, 'tv_box': 0}
    demand = dict(supply)
    for stock in session.inventory:
        if stock['engineer_id'] not in eligible:continue
        for key in supply:
            reported=sum(f['consumed'][key] for f in session.fact_log
                         if f['engineer_id']==stock['engineer_id'] and f['status']=='completion_pending')
            supply[key] += stock['available'][key] + max(0,stock['reserved'][key]-reported)
    for (_, rid), _visit in visits.items():
        spent = {key: sum(f['consumed'][key] for f in session.fact_log if f.get('request_id') == rid)
                 for key in demand}
        for key in demand:demand[key] += max(0, getattr(session.catalog[rid].equipment, key) - spent[key])
    labels = {'router': 'Роутеры', 'tv_box': 'ТВ-приставки'}
    equipment = [{'id': key, 'label': labels[key], 'needed': demand[key], 'on_hand': supply[key],
                  'spare': max(0, supply[key] - demand[key]), 'percent': ratio(demand[key], supply[key])}
                 for key in supply]
    limiting = max([x for x in equipment if x['on_hand'] or x['needed']] or equipment,
                   key=lambda x: (x['needed'] > x['on_hand'], x['percent'] or 0))
    equipment_value = limiting['percent'] if any(supply.values()) or any(demand.values()) else None
    context = {'region': session.scenario.region, 'date': session.scenario.date, 'plan_version': session.version,
               'time': now, 'needs_replanning': session.needs_replanning, 'mode': session.mode()}
    fleet = fleet_snapshot(session.scenario)
    forecast = _forecast(session, context, fleet)
    control = attention(session, forecast, fleet, gps_snapshot)
    forecast['review'] = control['forecast_review']
    return {'schema_version': SCHEMA_VERSION, 'context': context,
            'summary': {'engineers': len(eligible), 'roster': len(session.initial_engineers),
                        'unassigned': control['counts']['unassigned']},
            'indicators': [
                {'id': 'capacity', 'title': 'Загрузка бригад', 'value': ratio(occupied, capacity),
                 'caption': 'оставшегося времени смен занято',
                 'detail': f'{occupied} из {capacity} мин · дорога и работа',
                 'explanation': 'Доля дороги и работы в оставшихся сменах по утверждённому плану. Ожидание клиента не считается занятостью. Свободные минуты могут быть разбросаны между визитами и не гарантируют место для новой заявки.',
                 'recommendation': 'Проверьте загрузку отдельных инженеров и неназначенные заявки. Для изменений сравните варианты перепланирования.',
                 'facts': {'occupied_minutes': occupied, 'capacity_minutes': capacity}},
                {'id': 'windows', 'title': 'Плотные клиентские окна', 'value': ratio(tight, checked),
                 'caption': 'будущих визитов с запасом ≤ 15 мин',
                 'detail': f'{tight} из {checked} визитов · минимум '+(f'{min_buffer} мин' if min_buffer is not None else 'нет данных'),
                 'explanation': 'Запас — время от планового начала до конца подходящего клиентского окна. Для нескольких окон берётся подходящее с наиболее поздним концом. Это локальный запас визита, а не допустимая задержка всего маршрута и не вероятность опоздания.',
                 'recommendation': 'Уточните время у клиента и инженера. Если появилась задержка, запишите факт и сравните новые варианты плана.',
                 'facts': {'tight_visits': tight, 'checked_visits': checked, 'threshold_minutes': WINDOW_BUFFER_MINUTES}},
                {'id': 'equipment', 'title': 'Оборудование под план', 'value': equipment_value,
                 'caption': 'остатка наиболее занятого типа требуется',
                 'detail': ' · '.join(f"{x['label']}: нужно {x['needed']}, на руках {x['on_hand']}" for x in equipment),
                 'explanation': 'Потребность оставшихся визитов относительно физического остатка у доступных бригад. Показан наиболее занятый тип устройств. Сумма по району не означает, что оборудование можно передать между инженерами.',
                 'recommendation': 'Проверьте запас нужного типа у конкретного исполнителя. Пополнение или передача требуют отдельного подтверждения; пересчёт сам не создаёт оборудование.',
                 'facts': {'equipment': equipment}}],
            'forecast': forecast, 'attention': control,
            'fleet': fleet,
            'advisories': build_advisories(session, gps_snapshot)}
