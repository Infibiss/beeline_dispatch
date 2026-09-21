"""Exact waiting optimization for an already feasible set of fixed routes."""
from types import SimpleNamespace

from ._alns.scheduling import stable_starts
from .objectives import reference_visits, response_targets


def optimize_waiting(scenario, plan, *, profile=None, reference=None, focus_request_id=None):
    """Optimize response then promised times without changing routes or travel.

    The shared minute scheduler needs only each route's predecessor arcs. Its
    lightweight adapter takes O(route length) space and never builds an ALNS
    Instance, all-pairs matrices, or shortest-path closure. Morning schedules
    remain unchanged. The caller independently validates the resulting plan.
    """
    if profile not in (None, 'stable', 'cost', 'sla'):
        raise ValueError('Unknown objective profile')
    targets = response_targets(scenario, focus_request_id)
    if profile is None:
        return plan
    prior = reference_visits(scenario, reference)
    requests = {request.id: request for request in scenario.requests}
    engineers = {engineer.id: engineer for engineer in scenario.engineers}
    routes = []
    for route in plan.routes:
        engineer = engineers[route.engineer_id]
        tasks = [SimpleNamespace(service=requests[visit.request_id].duration_minutes,
                                 service_windows=tuple((w.start, w.end) for w in requests[visit.request_id].windows))
                 for visit in route.visits]
        n = len(tasks)
        # Local node 0 is the engineer start; task k is node k+1. No other
        # arcs are needed because this operation never changes the route order.
        minutes = {k: {k + 1: visit.travel_minutes} for k, visit in enumerate(route.visits)}
        adapter = SimpleNamespace(
            engineers=[SimpleNamespace(idx=0, shift_start=engineer.shift.start, mode='route')],
            tasks=tasks, ttl={'route': minutes}, node=lambda index: index + 1)
        earliest, latest = [], [0] * n
        ready = engineer.shift.start
        for task, visit in zip(tasks, route.visits):
            arrival = ready + visit.travel_minutes
            start = min(max(arrival, a) for a, b in task.service_windows if max(arrival, a) <= b)
            earliest.append(start)
            ready = start + task.service
        for k in range(n - 1, -1, -1):
            task = tasks[k]
            limit = engineer.shift.end - task.service
            if k + 1 < n:
                limit = min(limit, latest[k + 1] - task.service - route.visits[k + 1].travel_minutes)
            latest[k] = max(min(limit, b) for a, b in task.service_windows if min(limit, b) >= a)
        wanted = tuple(prior[visit.request_id][1] if visit.request_id in prior else None
                       for visit in route.visits)
        focused = tuple(visit.request_id in targets for visit in route.visits) if profile == 'sla' else ()
        starts = stable_starts(adapter, 0, list(range(n)), earliest, latest, wanted, focused)
        ready, visits = engineer.shift.start, []
        for visit, task, start in zip(route.visits, tasks, starts):
            arrival, end = ready + visit.travel_minutes, start + task.service
            visits.append(visit.model_copy(update={'arrival': arrival, 'start': start, 'end': end,
                'explanation': f'Дорога {visit.travel_minutes} мин, ожидание {start - arrival} мин. '
                               'Время выбрано целью пересчёта; допустимость проверена.'}))
            ready = end
        routes.append(route.model_copy(update={'visits': visits}))
    return plan.model_copy(update={'routes': routes})
