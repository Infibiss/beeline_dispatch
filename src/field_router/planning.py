"""Common schedule construction, metrics and bounded explanations."""
from heapq import heappop, heappush

from .models import Equipment, Metrics, Plan, Route, Unassigned, Visit


def compatible(request, engineer):
    return (request.region == engineer.region and request.skill in engineer.skills
            and (request.required_transport is None or request.required_transport == engineer.transport))


def build_route(engineer, requests, travel):
    """Earliest feasible start for a fixed order; None if this order is infeasible."""
    now, location = engineer.shift.start, engineer.start_location
    visits = []
    routers = boxes = 0
    for request in requests:
        if not compatible(request, engineer):
            return None
        routers += request.equipment.router
        boxes += request.equipment.tv_box
        if engineer.stock is not None and (routers > engineer.stock.router or boxes > engineer.stock.tv_box):
            return None
        minutes = travel.minutes(location,request.id,engineer.transport)
        distance = travel.metres(location,request.id,engineer.transport)
        arrival = now + minutes
        starts = [max(arrival,w.start) for w in request.windows if max(arrival,w.start) <= w.end]
        if not starts:
            return None
        start = min(starts)
        end = start + request.duration_minutes
        if end > engineer.shift.end:
            return None
        visits.append(Visit(request_id=request.id,arrival=arrival,start=start,end=end,
            travel_minutes=minutes,distance_m=distance,
            explanation=f"Навык и транспорт подходят. Дорога {minutes} мин, ожидание {start-arrival} мин. "
                        f"Работа {request.duration_minutes} мин, завершение в пределах смены. "
                        f"Роутеры: {request.equipment.router}, приставки: {request.equipment.tv_box}. "
                        "Это проверка допустимости выбранного назначения, не доказательство оптимальности."))
        now, location = end, request.id
    return Route(engineer_id=engineer.id,visits=visits,distance_m=sum(v.distance_m for v in visits),
                 travel_minutes=sum(v.travel_minutes for v in visits),
                 service_minutes=sum(v.end-v.start for v in visits),loadout=Equipment(router=routers,tv_box=boxes))


def _optimistically_reachable(request, engineer, scenario, travel):
    """A shortest-path relaxation is safe even for nonmetric transport matrices.

    Intermediate service, skills, stock and windows are deliberately ignored.
    Failure proves infeasibility; success only prevents an unsupported claim.
    """
    nodes = {r.id for r in scenario.requests} | {e.start_location for e in scenario.engineers}
    best = {engineer.start_location: 0}
    queue = [(0, engineer.start_location)]
    pending = set(nodes)
    while queue:
        duration, node = heappop(queue)
        if node not in pending:
            continue
        pending.remove(node)
        if node == request.id:
            arrival = engineer.shift.start + duration
            return any(max(arrival,w.start) <= min(w.end,engineer.shift.end-request.duration_minutes)
                       for w in request.windows)
        for dest in pending:
            value = duration + travel.minutes(node,dest,engineer.transport)
            if value < best.get(dest,float('inf')):
                best[dest] = value
                heappush(queue,(value,dest))
    return False


def explain_unassigned(request, scenario, travel, *, baseline=False):
    same_region = [e for e in scenario.engineers if e.region==request.region]
    skilled = [e for e in same_region if request.skill in e.skills]
    mode = [e for e in skilled if request.required_transport is None or e.transport==request.required_transport]
    stocked = [e for e in mode if e.stock is None or (
        e.stock.router>=request.equipment.router and e.stock.tv_box>=request.equipment.tv_box)]
    if not skilled:
        code, reason = "NO_SKILL", "Нет исполнителя с требуемым навыком в этом участке."
    elif not mode:
        code, reason = "NO_TRANSPORT", "Среди инженеров с нужным навыком нет требуемого транспорта."
    elif not stocked:
        code, reason = "NO_EQUIPMENT", "У подходящих инженеров недостаточно устройств для этой заявки."
    elif not any(build_route(e,[request],travel) is not None or _optimistically_reachable(request,e,scenario,travel) for e in stocked):
        code, reason = "NO_TIME_WINDOW", "При текущих стартах и сменах не успеть в окно даже с оптимистичной оценкой дороги."
    elif baseline:
        code, reason = "NO_APPEND_SLOT", "Нет допустимого добавления в конец текущих маршрутов. Baseline не меняет порядок уже назначенных работ."
    else:
        code, reason = "SEARCH_UNASSIGNED", "Не включена в найденный план при доступных ресурсах и лимите поиска. Глобальная невозможность не доказана."
    return Unassigned(request_id=request.id,code=code,reason=reason)


def make_plan(scenario, travel, sequences, solver, status, elapsed_seconds=0, diagnostics=None):
    jobs = {r.id:r for r in scenario.requests}
    routes, assigned = [], set()
    for e in scenario.engineers:
        ids = sequences.get(e.id, [])
        if not ids:
            continue
        route = build_route(e,[jobs[i] for i in ids],travel)
        if route is None:
            raise ValueError(f"Solver returned infeasible sequence for {e.id}")
        routes.append(route)
        assigned.update(ids)
    unassigned = [explain_unassigned(r,scenario,travel,baseline=solver=="baseline")
                  for r in scenario.requests if r.id not in assigned]
    return Plan(solver=solver,status=status,scenario_id=scenario.id,routes=routes,unassigned=unassigned,
        metrics=Metrics(assigned=sum(len(r.visits) for r in routes),unassigned=len(unassigned),
            engineers_used=len(routes),distance_m=sum(r.distance_m for r in routes),
            travel_minutes=sum(r.travel_minutes for r in routes),service_minutes=sum(r.service_minutes for r in routes)),
        elapsed_seconds=elapsed_seconds,diagnostics=diagnostics or {})
