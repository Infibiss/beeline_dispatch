#!/usr/bin/env python3
"""Offline, deterministic-input day stress experiment; never writes source demos.

Run: .venv/bin/python scripts/check_resilience.py
Wall-clock limited solver choices can vary between machines; no optimality claim.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from field_router.models import Plan, Scenario
from field_router.geo_models import GeographyBundle
from field_router.road_travel import bundle_travel
from field_router.replanning import ScenarioSession
from field_router.solvers.selection import get_solver
from field_router.validation import require_valid


class ScaledTravel:
    def __init__(self, base, factor):
        self.base, self.factor = base, factor

    def minutes(self, a, b, mode):
        return math.ceil(self.base.minutes(a, b, mode) * self.factor)

    def metres(self, a, b, mode):
        return self.base.metres(a, b, mode)


def execute_fixed(scenario, plan, travel, *, travel_factor=1.0, service_factor=1.0):
    """Execute published order, never advance appointments; propagate late finishes.

    Windows constrain service START (same contract as require_valid). If no later
    window exists, execute late and count the breach; do not drop the visit.
    Routes are open: no unmodelled office-return requirement is invented.
    """
    jobs = {r.id: r for r in scenario.requests}
    engineers = {e.id: e for e in scenario.engineers}
    travel = ScaledTravel(travel, travel_factor)
    window_breaches = shift_breaches = max_delay = max_overrun = 0
    affected = set()
    for route in plan.routes:
        engineer = engineers[route.engineer_id]
        now, location = engineer.shift.start, engineer.start_location
        for visit in route.visits:
            request = jobs[visit.request_id]
            arrival = now + travel.minutes(location, request.id, engineer.transport)
            earliest = max(arrival, visit.start)
            starts = [max(earliest, w.start) for w in request.windows if max(earliest, w.start) <= w.end]
            start = min(starts) if starts else earliest
            end = start + math.ceil(request.duration_minutes * service_factor)
            window_bad = not any(w.start <= start <= w.end for w in request.windows)
            shift_bad = end > engineer.shift.end
            window_breaches += window_bad
            shift_breaches += shift_bad
            if window_bad or shift_bad:
                affected.add(request.id)
            max_delay = max(max_delay, start - visit.start)
            max_overrun = max(max_overrun, end - engineer.shift.end)
            now, location = end, request.id
    return {'window_breaches': window_breaches, 'shift_breaches': shift_breaches,
            'breached_requests': len(affected), 'pending': len(plan.unassigned),
            'max_start_delay_minutes': max_delay, 'max_shift_overrun_minutes': max_overrun}


def assert_invariants(session, previous_frozen=(), mutable=frozenset()):
    """Independent of solver feasibility: whole-day coverage and issued stock."""
    require_valid(session.scenario, session._travel(session.scenario), session.plan)
    frozen = [v for r in session.frozen for v in r['visits']]
    frozen_ids = [v['request_id'] for v in frozen]
    pending = [r.id for r in session.scenario.requests]
    cancelled = {h['event']['request_id'] for h in session.history if h['event']['type'] == 'cancel'}
    assert len(frozen_ids) == len(set(frozen_ids)), 'duplicate frozen work'
    assert not set(frozen_ids) & set(pending), 'frozen work rescheduled'
    assert not cancelled & (set(frozen_ids) | set(pending)), 'cancelled request retained'
    assert set(session.catalog) == set(frozen_ids) | set(pending) | cancelled, 'lost request'
    frozen_map = {(r['engineer_id'], v['request_id']): v for r in session.frozen for v in r['visits']}
    for route in previous_frozen:
        for visit in route['visits']:
            if visit['request_id'] not in mutable:
                assert frozen_map[(route['engineer_id'], visit['request_id'])] == visit, 'frozen commitment changed'
    for row in session.inventory:
        initial = session.initial_stock[row['engineer_id']]
        for device in ('router', 'tv_box'):
            values = [row[k][device] for k in ('consumed', 'reserved', 'available')]
            assert all(v >= 0 for v in values), 'negative inventory'
            assert sum(values) == getattr(initial, device), 'inventory not conserved'
    return True


def dynamic_day(scenario, plan, geography, time_limit, solver='hybrid'):
    session = ScenarioSession(scenario.model_copy(deep=True), plan.model_copy(deep=True), deepcopy(geography))
    stages = []

    def apply(event, mutable=frozenset()):
        before = deepcopy(session.frozen)
        batch = session.preview({'version': session.version, 'event': event,
                                 'preserve_in_progress': True, 'allow_redirect': False}, time_limit=time_limit, solver=solver)
        residual = session.batches[batch['id']]['scenario']
        for candidate in batch['candidates']:
            require_valid(residual, session._travel(residual), Plan.model_validate(candidate['plan']))
        session.approve({'version': session.version, 'batch_id': batch['id'], 'candidate_id': 'stable'})
        assert_invariants(session, before, mutable)
        urgent = {r.id for r in session.scenario.requests if r.priority == 'urgent'}
        urgent_pending = sum(u.request_id in urgent for u in session.plan.unassigned)
        stages.append({'event': event, 'urgent_assigned_remaining': len(urgent) - urgent_pending,
                       'urgent_pending': urgent_pending, 'assigned_remaining': session.plan.metrics.assigned,
                       'pending': session.plan.metrics.unassigned,
                       'frozen': sum(len(r['visits']) for r in session.frozen),
                       'all_three_candidates_valid': True, 'invariants_valid': True})

    start = session.time
    anchors = [r.id for r in scenario.requests]
    for n, minute in enumerate((start + 5, start + 25), 1):
        apply({'id': f'emergency-{n}', 'type': 'emergency', 'time': minute,
               'anchor_id': anchors[n - 1], 'duration_minutes': 80, 'router': 0})
    # Cancel a genuinely future/unassigned request, never already-started work.
    protected = {v.request_id for r in session.plan.routes for v in r.visits if v.start <= session.time + 1}
    candidate = next(r.id for r in session.scenario.requests if r.id not in protected and not r.id.startswith('emergency:'))
    apply({'id': 'cancel', 'type': 'cancel', 'time': session.time + 1, 'request_id': candidate})

    # Select the earliest instant at which an engineer is not doing a service.
    visits = [(r.engineer_id, v.start, v.end) for r in session.plan.routes for v in r.visits]
    visits += [(r['engineer_id'], v['start'], v['end']) for r in session.frozen for v in r['visits']]
    ready = next((t, e.id) for t in range(session.time + 1, 1440) for e in session.scenario.engineers
                 if e.shift.start <= t < e.shift.end and not any(eid == e.id and a <= t < b for eid, a, b in visits))
    apply({'id': 'unavailable', 'type': 'unavailable', 'time': ready[0], 'engineer_id': ready[1]})

    # Explicitly observe one assigned job taking 20 minutes longer than its plan.
    choices = [(max(session.time + 1, v.start), r.engineer_id, v.request_id, v.end)
               for r in session.plan.routes for v in r.visits if v.end > session.time + 1]
    choices += [(max(session.time + 1, v['start']), r['engineer_id'], v['request_id'], v['end'])
                for r in session.frozen for v in r['visits'] if v['end'] > session.time + 1
                and r['engineer_id'] != ready[1]]
    when, eid, key, planned_end = min(choices)
    remaining = planned_end - when + 20
    session.record_fact({'version': session.version, 'time': when, 'engineer_id': eid,
                         'request_id': key, 'status': 'working', 'remaining_minutes': remaining})
    assert session.needs_replanning
    apply({'id': 'delay-fact-replan', 'type': 'replan', 'time': when}, {key})
    delay = {'engineer_id': eid, 'request_id': key, 'time': when,
             'extra_service_minutes': 20, 'remaining_minutes': remaining}
    # Completion is a second explicit fact, not inferred from the working clock.
    finish = when + remaining
    session.record_fact({'version': session.version, 'time': finish, 'engineer_id': eid,
                         'request_id': key, 'status': 'completed'})
    apply({'id': 'completion-replan', 'type': 'replan', 'time': finish}, {key})
    serialized = json.loads(json.dumps(session.dump()))
    restored = ScenarioSession.restore(serialized)
    assert restored.state() == session.state(), 'restore changed operational state'
    assert restored.workbench() == session.workbench(), 'restore changed view'
    session = restored
    apply({'id': 'post-restore', 'type': 'replan', 'time': finish + 1})
    apply({'id': 'end-of-shift', 'type': 'replan',
           'time': max(e.shift.end for e in session.initial_engineers.values())})
    return {'stages': stages, 'delay_fact': delay, 'restore_equal': True,
            'final_pending': session.plan.metrics.unassigned,
            'cancelled': candidate, 'unavailable_engineer': ready[1],
            'note': 'Frozen includes simulated execution and explicit facts; these are not observed field outcomes.'}


def evaluate(path, time_limit, solver=None):
    raw = path.read_bytes()
    payload = json.loads(raw)
    scenario = Scenario.model_validate(payload['scenario'])
    plan = next(Plan.model_validate(p) for p in payload['plans'] if p['solver'] == 'ortools')
    travel = bundle_travel(GeographyBundle.model_validate(payload['geography']))
    require_valid(scenario, travel, plan)
    rows = []
    # Morning issue is fixed to the published loadout, including idle engineers.
    stocked = ScenarioSession(scenario.model_copy(deep=True), plan, payload['geography']).scenario
    for name, tf, sf in [('baseline', 1, 1), ('travel+20%', 1.2, 1), ('travel+40%', 1.4, 1),
                         ('service+20%', 1, 1.2), ('travel+40% service+20%', 1.4, 1.2)]:
        row = {'stress': name, 'fixed_execution': execute_fixed(scenario, plan, travel, travel_factor=tf, service_factor=sf)}
        if name != 'baseline':
            stressed = stocked.model_copy(update={'requests': [r.model_copy(update={
                'duration_minutes': math.ceil(r.duration_minutes * sf)}) for r in stocked.requests]})
            stress_travel = ScaledTravel(travel, tf)
            replacement = get_solver(solver or 'ortools').solve(stressed, stress_travel, time_limit_seconds=time_limit)
            require_valid(stressed, stress_travel, replacement)
            row['morning_reoptimization'] = {'valid': True, 'assigned': replacement.metrics.assigned,
                                             'pending': replacement.metrics.unassigned}
        rows.append(row)
    day = dynamic_day(scenario, plan, payload['geography'], time_limit, solver=solver or 'hybrid')
    assert path.read_bytes() == raw, 'source demo modified'
    return {'region': scenario.region, 'source': str(path.relative_to(ROOT)),
            'sha256': hashlib.sha256(raw).hexdigest(), 'requests': len(scenario.requests),
            'initial_assigned': plan.metrics.assigned, 'stress_results': rows, 'dynamic_day': day}


def render_report(results):
    lines = ['# Устойчивость плана рабочего дня', '',
             'При увеличении времени пути или обслуживания исходные маршруты начинают нарушать ограничения. '
             'Перепланирование сохраняет допустимость, но часть заявок может остаться без назначения. '
             'Это проверка заданных сценариев, а не оценка вероятности успешного дня.', '',
             f"Reproduce offline: `.venv/bin/python scripts/check_resilience.py --time-limit {results['solver_limit_seconds']}"
             + (f" --solver {results['solver']}" if results.get('solver') else '') + '`.', '',
             f"Morning backend: {results.get('solver') or 'ortools'}; dynamic backend: {results.get('solver') or 'hybrid'}.", '',
             f"Solver limit: {results['solver_limit_seconds']} seconds per solve/profile (preparation and fallback add overhead). "
             'Inputs and event selection are reproducible; wall-clock solver choices can vary. '
             'All source demo SHA-256 hashes are recorded in `day-resilience.json` and verified unchanged.', '',
             'Fixed execution keeps the published engineer/order and never starts earlier than its appointment. '
             'Each leg and service is rounded up after scaling. Delays propagate through the route; waiting can absorb them. '
             'A later allowed window is used if reachable; otherwise the late visit remains in execution and counts as a breach. '
             'Appointment windows constrain service start, while service must finish before shift end. '
             'These are open routes: no return trip is required by the model.', '',
             'Replacement plans are separate morning counterfactuals with known stress and the original issued stock. '
             'They are independently checked with `require_valid`; their pending counts are not execution breaches. '
             'Valid plans may leave work pending. These experiments are sensitivity tests, not probabilities or observed service performance.', '',
             '| Region | Stress | Window breaches | Shift breaches | Distinct breached jobs | Initial pending | Max start delay | Replacement pending |',
             '|---|---|---:|---:|---:|---:|---:|---:|']
    for region in results['regions']:
        for row in region['stress_results']:
            f = row['fixed_execution']
            lines.append(f"| {region['region']} | {row['stress']} | {f['window_breaches']} | {f['shift_breaches']} | {f['breached_requests']} | {f['pending']} | {f['max_start_delay_minutes']} min | {row.get('morning_reoptimization', {}).get('pending', '—')} |")
    lines += ['', '## Dynamic day', '',
              'Each region receives two 80-minute emergencies, a future cancellation, an unavailable engineer, '
              'a working-status fact extending service by 20 minutes, an explicit completion fact, a JSON save/restore, '
              'another replan and an end-of-shift replan. Emergency anchors use existing known addresses. '
              'Travel redirection is disabled; in-progress work is preserved. The Stable candidate is approved. '
              'All three proposals at every stage are independently validated.', '',
              'Checks after every approval: every catalog job is exactly in remaining work, frozen work or cancellations; '
              'no duplicate frozen jobs or rescheduling of frozen work; frozen visit records stay unchanged except the '
              'job with a new explicit fact; per-engineer consumed + reserved + available equals issued stock with no '
              'negative amounts. Save/restore must preserve the complete state and current workbench.', '',
              '| Region | Event | Time | Remaining assigned | Pending | Urgent pending | Frozen |',
              '|---|---|---:|---:|---:|---:|---:|']
    for region in results['regions']:
        for stage in region['dynamic_day']['stages']:
            lines.append(f"| {region['region']} | {stage['event']['id']} | {stage['event']['time']//60:02}:{stage['event']['time']%60:02} | {stage['assigned_remaining']} | {stage['pending']} | {stage['urgent_pending']} | {stage['frozen']} |")
    lines += ['', 'После ручного статуса инженер не возвращается автоматически к симуляции исполнения: его последующие работы требуют фактов завершения. Поэтому остаток на 22:00 включает неподтверждённые работы и не является числом потерь от стресс-событий; смотреть качество назначения нужно также на промежуточные строки. Столбец аварий относится к оставшимся заявкам, а не к доказанным фактическим пропускам выезда.', '', 'All listed assertions passed in this run. Frozen work primarily reflects the session simulation; '
              'only the deliberately injected delay/completion is a dispatcher fact. End-of-shift pending is therefore '
              'a modeled residual, not a claim that jobs were actually completed. The stress execution and dynamic '
              'event experiments are separate: dynamic events use the saved travel matrix and ordinary durations '
              'except the explicit delay fact. Cached road/public-transport estimates and approximate coordinates '
              'remain limitations of these demos; there is no live traffic, stochastic outage model, '
              'emergency SLA guarantee or optimality proof.', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--time-limit', type=float, default=3)
    parser.add_argument('--solver', choices=['ortools', 'alns', 'hybrid'],
                        help='Use this backend for both morning stress and dynamic events')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'docs/planning')
    args = parser.parse_args()
    if not math.isfinite(args.time_limit) or args.time_limit <= 0:
        parser.error('--time-limit must be positive and finite')
    results = {'solver_limit_seconds': args.time_limit, 'solver': args.solver, 'regions': []}
    for path in sorted((ROOT / 'data/demo-road').glob('*.json')):
        results['regions'].append(evaluate(path, args.time_limit, solver=args.solver))
        print(f"Validated resilience: {results['regions'][-1]['region']}", flush=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / 'day-resilience.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    (args.output_dir / 'day-resilience.md').write_text(render_report(results))


if __name__ == '__main__':
    main()
