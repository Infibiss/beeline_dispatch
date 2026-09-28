"""Conservative GPS estimates for the existing planner; never employee facts.

The observer does no routing or optimization. It consumes already verified
device inference and requires repeated evidence before proposing a replan.
Only the caller's atomic plan transaction persists estimates/acknowledgements.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from uuid import uuid4

from ..availability import availability_at, project_availability, shift_blocks, trip_stopped_by_absence
from .contracts import plan_clock


def trip_key(fact):
    fields = ('id', 'time', 'status', 'request_id', 'location_id', 'remaining_minutes', 'recorded_at')
    return hashlib.sha256(json.dumps({k: fact.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()


def _visit(session, eid, request_id):
    for route in session.plan.routes:
        if route.engineer_id == eid:
            for visit in route.visits:
                if visit.request_id == request_id:
                    return visit.model_dump()
    for route in session.frozen:
        if route['engineer_id'] == eid:
            for visit in route['visits']:
                if visit['request_id'] == request_id:
                    return visit
    return None


def _available(session, eid, fact, minute):
    if eid not in session.initial_engineers or eid in session.excluded_engineers():
        return False
    blocks = shift_blocks(session.unavailability, eid, session.scenario.date, session._availability_shift(eid))
    availability = project_availability(blocks, session.fact_log, eid)
    return (availability_at(availability, minute) == 'available'
            and not trip_stopped_by_absence(availability, fact, minute)
            and not session.pending_reassignment_ids(eid))


def travel_estimate(session, eid, fact, minute):
    """Return an unexpired estimate for this exact still-confirmed journey.

The expiry is expressed in plan minutes so replaying a historical plan remains
deterministic. Wall-clock freshness is checked before the live commit.
"""
    value = getattr(session, 'gps_travel_estimates', {}).get(eid)
    if (not value or fact.get('status') != 'traveling'
            or value.get('date') != session.scenario.date
            or value.get('trip_key') != trip_key(fact)
            or not value['time'] <= minute <= value['valid_until_minute']
            or not _available(session, eid, fact, minute)
            or not _visit(session, eid, fact.get('request_id'))):
        return None
    # Once approved, a previous estimate cannot imply arrival/completion merely
    # because the clock advanced. Keep its conservative remaining duration.
    return value


class GpsAutopilot:
    minimum_delay_minutes = 5
    evidence_seconds = 30
    cooldown_seconds = 300
    failure_seconds = 60

    def __init__(self):
        self._pending = {}
        self._failures = {}

    def _eligible(self, session, gps):
        if (not gps or not gps.enabled or session.planning_settings.get('mode') != 'autopilot'
                or session.pending_report_ids):
            return []
        snapshot = gps.snapshot('device')
        context = gps.context()
        if (snapshot.get('channel') != 'device' or snapshot.get('plan_version') != session.version
                or context.get('version') != session.version
                or context.get('date') != session.scenario.date):
            return []
        now = gps.clock()
        freshness = min(60, gps.config.stale_seconds)
        result = []
        for row in snapshot['rows']:
            eid = row['engineer_id']
            fact = session.statuses.get(eid, {})
            analysis = row.get('analysis') or {}
            eta = analysis.get('eta') or {}
            target = analysis.get('target') or {}
            point = row.get('position') or {}
            minute = row.get('clock', {}).get('minute', -1)
            route = eta.get('route') or {}
            if (row.get('channel') != 'device' or row.get('tracking_state') != 'active'
                    or not row.get('date_matches') or row.get('quality_reason')
                    or fact.get('status') != 'traveling' or target.get('basis') != 'confirmed'
                    or target.get('id') != fact.get('request_id')
                    or minute < session.time or minute <= fact.get('time', -1)
                    or not _available(session, eid, fact, minute)
                    or route.get('kind') not in ('road', 'multimodal')
                    or any(a.get('code') in ('quality', 'reacquired') for a in row.get('alerts', []))):
                continue
            age = now - point.get('measured_at_ms', 0) / 1000
            if (not 0 <= age <= freshness or point.get('is_mock')
                    or not 0 < point.get('accuracy_m', float('inf')) <= min(50, gps.config.max_accuracy_m)):
                continue
            low, high = eta.get('low_minutes'), eta.get('high_minutes')
            if (type(low) is not int or type(high) is not int or not 0 <= low <= high <= 1440):
                continue
            visit = _visit(session, eid, fact.get('request_id'))
            if not visit or minute + low - visit['start'] < self.minimum_delay_minutes:
                continue
            shift = session._availability_shift(eid)
            if not shift.start <= minute < shift.end or minute + high >= shift.end:
                continue
            samples = gps.trace(eid, 'device')['samples']
            # Stationary browsers send every 30 seconds. Keep the observation
            # series within the model's horizon; only its latest fix has the
            # stricter 60-second publication limit (otherwise jitter drops the
            # third point at the exact 60-second boundary).
            recent = [s for s in samples if 0 <= now - s['point']['measured_at_ms'] / 1000 <= gps.config.stale_seconds]
            if any(not s.get('accepted') or s.get('reacquired') or s.get('reason') for s in recent):
                continue
            good = sorted((s['point'] for s in recent
                           if not s['point'].get('is_mock')
                           and 0 < s['point'].get('accuracy_m', float('inf')) <= min(50, gps.config.max_accuracy_m)
                           and plan_clock(s['point']['measured_at_ms']/1000, gps.config, session.scenario.date)['minute'] >= fact['time']),
                          key=lambda p: p['measured_at_ms'])
            if (len({p['measured_at_ms'] for p in good}) < 3
                    or good[-1]['measured_at_ms'] - good[0]['measured_at_ms'] < self.evidence_seconds * 1000):
                continue
            key = trip_key(fact)
            prior = next((item for item in reversed(getattr(session, 'gps_autopilot_history', []))
                          if item['engineer_id'] == eid and item['trip_key'] == key), None)
            if prior and (point['measured_at_ms'] <= prior['point_measured_at_ms']
                          or now - prior['applied_at'] < self.cooldown_seconds):
                continue
            if now < self._failures.get((eid, key), 0):
                continue
            result.append({'version': session.version, 'date': session.scenario.date,
                'engineer_id': eid, 'request_id': fact['request_id'], 'trip_key': key,
                'time': minute, 'remaining_minutes': max(1, high), 'low_minutes': low,
                'planned_start': visit['start'], 'delay_minutes': minute + low - visit['start'],
                'point_measured_at_ms': point['measured_at_ms'], 'evaluated_at': now,
                'expires_at_ms': point['measured_at_ms'] + freshness * 1000,
                'valid_until_minute': min(1439, minute + math.ceil(freshness / 60)),
                'route_kind': route['kind'], 'source': 'gps_estimate'})
        return result

    def collect(self, session, gps):
        proposals = self._eligible(session, gps)
        if not gps or not gps.enabled or session.planning_settings.get('mode') != 'autopilot':
            self._pending.clear()
            return []
        active = set()
        ready = []
        for proposal in proposals:
            key = (proposal['engineer_id'], proposal['trip_key'], proposal['version'])
            active.add(key)
            first = self._pending.setdefault(key, proposal)
            if (proposal['point_measured_at_ms'] - first['point_measured_at_ms'] >= self.evidence_seconds * 1000
                    and proposal['evaluated_at'] - first['evaluated_at'] >= self.evidence_seconds):
                ready.append(proposal)
        # Inference is absent briefly after every point. Preserve evidence in
        # that short gap, but never carry it over a different plan or journey.
        now = gps.clock() if gps else 0
        self._failures = {key: until for key, until in self._failures.items() if until > now}
        snapshot = gps.snapshot('device')
        rows = {row['engineer_id']: row for row in snapshot['rows']}
        def pending_evidence(key):
            row = rows.get(key[0], {})
            # A fresh but non-late analysis resets the streak. Only a brief
            # inference gap after the next good point may retain prior evidence.
            return (key in active or (row.get('analysis_pending')
                    and row.get('tracking_state') == 'active' and row.get('date_matches')
                    and not row.get('quality_reason')
                    and _available(session, key[0], session.statuses.get(key[0], {}), row['clock']['minute'])))
        self._pending = {key: value for key, value in self._pending.items()
                         if key[2] == session.version and now - value['evaluated_at'] <= 180
                         and trip_key(session.statuses.get(key[0], {})) == key[1]
                         and pending_evidence(key)}
        return ready

    def revalidate(self, session, gps, proposals):
        current = {(p['engineer_id'], p['trip_key']): p for p in self._eligible(session, gps)}
        now = gps.clock() if gps else 0
        for original in proposals:
            fresh = current.get((original['engineer_id'], original['trip_key']))
            if (not fresh or original['version'] != session.version
                    or now * 1000 > original['expires_at_ms']
                    or fresh['time'] != original['time']
                    or fresh['point_measured_at_ms'] < original['point_measured_at_ms']
                    or fresh['remaining_minutes'] > original['remaining_minutes']):
                return False
        return bool(proposals)

    def failed(self, proposals, now=None):
        for proposal in proposals:
            self._failures[(proposal['engineer_id'], proposal['trip_key'])] = (
                proposal['evaluated_at'] if now is None else now) + self.failure_seconds


def stage_estimates(session, proposals):
    """Apply only to an isolated candidate, never the live session."""
    for proposal in proposals:
        eid = proposal['engineer_id']
        if (proposal['version'] != session.version or proposal['date'] != session.scenario.date
                or proposal['trip_key'] != trip_key(session.statuses.get(eid, {}))
                or not _available(session, eid, session.statuses[eid], proposal['time'])):
            raise ValueError('GPS-оценка устарела; план не изменён')
        session.gps_travel_estimates[eid] = deepcopy(proposal)


def record_applied(session, proposals):
    for proposal in proposals:
        session.gps_autopilot_history.append({**deepcopy(proposal),
            'applied_version': session.version, 'applied_at': proposal['evaluated_at']})
    session.gps_autopilot_history = session.gps_autopilot_history[-200:]
    if session.history:
        session.history[-1]['gps_estimates'] = deepcopy(proposals)
    notice = next((item for item in reversed(session.dispatch_notifications)
                   if item.get('kind') == 'auto_applied' and item.get('version') == session.version), None)
    if notice is not None:
        notice['gps_estimates'] = deepcopy(proposals)
        notice['text'] = ('Учтена GPS-оценка задержки: ' + '; '.join(
            f"{session.initial_engineers[p['engineer_id']].name}, заявка № {p['request_id']}, "
            f"доезд {p['low_minutes']}–{p['remaining_minutes']} мин" for p in proposals)
            + '. Статусы сотрудников не изменены. ' + notice['text'])


def record_failure(session, proposals):
    """Diagnostic only: preserve plan version, previews and intake retry policy."""
    session.dispatch_notifications.append({'id': uuid4().hex, 'kind': 'auto_gps_error',
        'time': datetime.now(timezone.utc).isoformat(), 'version': session.version,
        'title': 'GPS-пересчёт требует проверки',
        'text': 'GPS-оценка задержки не применена к плану. '
                'Уточните время прибытия у инженера и проверьте варианты вручную.',
        'request_ids': sorted({p['request_id'] for p in proposals}),
        'gps_estimates': deepcopy(proposals)})
