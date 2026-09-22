"""Insert one ordinary arrival without changing existing appointment promises."""
from .models import Equipment, Metrics, Plan, Route, Unassigned, Visit
from .planning import compatible
from .validation import require_valid


PRESERVATION_ERROR = 'Прежний план нельзя сохранить по текущим фактам. Сначала выполните обычный пересчёт и утвердите его.'


def _fixed_route(engineer, ids, jobs, starts, travel):
    """Old service starts are fixed; only the new request starts at its earliest slot."""
    now, location = engineer.shift.start, engineer.start_location
    visits = []
    routers = boxes = 0
    for key in ids:
        request = jobs[key]
        if not compatible(request, engineer):
            return None
        routers += request.equipment.router
        boxes += request.equipment.tv_box
        if engineer.stock is not None and (routers > engineer.stock.router or boxes > engineer.stock.tv_box):
            return None
        minutes = travel.minutes(location, key, engineer.transport)
        distance = travel.metres(location, key, engineer.transport)
        arrival = now + minutes
        if key in starts:
            start = starts[key]
        else:
            feasible = [max(arrival, w.start) for w in request.windows if max(arrival, w.start) <= w.end]
            if not feasible:
                return None
            start = min(feasible)
        end = start + request.duration_minutes
        if (start < arrival or not any(w.start <= start <= w.end for w in request.windows)
                or end > engineer.shift.end):
            return None
        visits.append(Visit(request_id=key, arrival=arrival, start=start, end=end,
            travel_minutes=minutes, distance_m=distance,
            explanation=f'Вставка в свободный интервал. Дорога {minutes} мин; ожидание {start-arrival} мин. '
                        'Владельцы, порядок и начало прежних работ сохранены.'))
        now, location = end, key
    return Route(engineer_id=engineer.id, visits=visits,
        distance_m=sum(v.distance_m for v in visits), travel_minutes=sum(v.travel_minutes for v in visits),
        service_minutes=sum(v.end-v.start for v in visits), loadout=Equipment(router=routers, tv_box=boxes))


def _plan(scenario, routes, old_unassigned, new_id, *, assigned):
    unassigned = list(old_unassigned)
    if not assigned:
        unassigned.append(Unassigned(request_id=new_id, code='NO_FREE_GAP',
            reason='Нет допустимого свободного интервала с нужным навыком и выданными устройствами '
                   'при сохранении владельцев, порядка и начала прежних работ.'))
    return Plan(solver='ordinary_gap_insertion', status='feasible', scenario_id=scenario.id,
        routes=routes, unassigned=unassigned,
        metrics=Metrics(assigned=sum(len(r.visits) for r in routes), unassigned=len(unassigned),
            engineers_used=len(routes), distance_m=sum(r.distance_m for r in routes),
            travel_minutes=sum(r.travel_minutes for r in routes), service_minutes=sum(r.service_minutes for r in routes)),
        diagnostics={'insertion_policy': 'preserve_existing_assignments_and_starts'})


def insertion_candidates(scenario, travel, reference, new_id):
    """Enumerate every engineer/gap, preserving the old assigned and unassigned sets.

    Validate the unchanged residual schedule before attempting insertion: observed
    delays, unavailable crews or changed stock require a separate normal replan.
    """
    jobs = {r.id: r for r in scenario.requests}
    engineers = {e.id: e for e in scenario.engineers}
    starts = {v.request_id: v.start for r in reference.routes for v in r.visits if v.request_id in jobs}
    sequences, routes = {}, {}
    for route in reference.routes:
        ids = [v.request_id for v in route.visits if v.request_id in jobs]
        if not ids:
            continue
        engineer = engineers.get(route.engineer_id)
        if engineer is None:
            raise ValueError(PRESERVATION_ERROR)
        fixed = _fixed_route(engineer, ids, jobs, starts, travel)
        if fixed is None:
            raise ValueError(PRESERVATION_ERROR)
        sequences[engineer.id], routes[engineer.id] = ids, fixed
    old_unassigned = [u for u in reference.unassigned if u.request_id in jobs]
    baseline = _plan(scenario, list(routes.values()), old_unassigned, new_id, assigned=False)
    try:
        require_valid(scenario, travel, baseline)
    except ValueError as exc:
        raise ValueError(PRESERVATION_ERROR) from exc
    candidates = [baseline]
    for engineer in scenario.engineers:
        ids = sequences.get(engineer.id, [])
        for gap in range(len(ids) + 1):
            inserted = _fixed_route(engineer, ids[:gap] + [new_id] + ids[gap:], jobs, starts, travel)
            if inserted is None:
                continue
            changed = dict(routes)
            changed[engineer.id] = inserted
            plan = _plan(scenario, list(changed.values()), old_unassigned, new_id, assigned=True)
            candidates.append(require_valid(scenario, travel, plan))
    return candidates
