"""Independent validator: never trusts solver cumul variables or reported totals."""
from collections import Counter


def validate_plan(scenario, travel, plan) -> list[str]:
    errors = []
    jobs = {r.id:r for r in scenario.requests}
    engineers = {e.id:e for e in scenario.engineers}
    assigned, route_ids = [], []
    distance_total = travel_total = service_total = 0
    if plan.scenario_id != scenario.id:
        errors.append("scenario_id mismatch")
    for route in plan.routes:
        route_ids.append(route.engineer_id)
        if route.engineer_id not in engineers:
            errors.append(f"unknown engineer: {route.engineer_id}")
            continue
        engineer = engineers[route.engineer_id]
        if not route.visits:
            errors.append(f"empty route: {engineer.id}")
        if engineer.off_duty:
            errors.append(f"off-duty engineer: {engineer.id}")
        now, location = engineer.shift.start, engineer.start_location
        distance = minutes = service = routers = boxes = 0
        for visit in route.visits:
            assigned.append(visit.request_id)
            if visit.request_id not in jobs:
                errors.append(f"unknown request: {visit.request_id}")
                continue
            request = jobs[visit.request_id]
            prefix = f"{engineer.id}/{request.id}: "
            if request.region != engineer.region or request.region != scenario.region:
                errors.append(prefix+"region")
            if request.skill not in engineer.skills:
                errors.append(prefix+"skill")
            if engineer.id in request.excluded_engineer_ids:
                errors.append(prefix+"engineer declined this request")
            if request.required_transport and request.required_transport != engineer.transport:
                errors.append(prefix+"transport")
            leg_time = travel.minutes(location,request.id,engineer.transport)
            leg_distance = travel.metres(location,request.id,engineer.transport)
            if visit.arrival != now+leg_time or visit.travel_minutes != leg_time or visit.distance_m != leg_distance:
                errors.append(prefix+"travel/arrival")
            if visit.start < visit.arrival or not any(w.start <= visit.start <= w.end for w in request.windows):
                errors.append(prefix+"window/wait")
            if visit.end != visit.start + request.duration_minutes:
                errors.append(prefix+"duration")
            if visit.start < engineer.shift.start or visit.end > engineer.shift.end:
                errors.append(prefix+"shift")
            distance += leg_distance
            minutes += leg_time
            service += request.duration_minutes
            routers += request.equipment.router
            boxes += request.equipment.tv_box
            now, location = visit.end, request.id
        if engineer.stock is not None and (routers > engineer.stock.router or boxes > engineer.stock.tv_box):
            errors.append(f"{engineer.id}: equipment stock")
        if (route.loadout.router,route.loadout.tv_box) != (routers,boxes):
            errors.append(f"{engineer.id}: loadout")
        if (route.distance_m,route.travel_minutes,route.service_minutes) != (distance,minutes,service):
            errors.append(f"{engineer.id}: route metrics")
        distance_total += distance
        travel_total += minutes
        service_total += service
    if len(set(route_ids)) != len(route_ids):
        errors.append("duplicate engineer routes")
    for id,count in Counter(assigned).items():
        if count > 1:
            errors.append(f"duplicate assignment: {id}")
    unassigned = [u.request_id for u in plan.unassigned]
    if len(set(unassigned)) != len(unassigned):
        errors.append("duplicate unassigned")
    if set(assigned) & set(unassigned):
        errors.append("assigned and unassigned overlap")
    if set(assigned) | set(unassigned) != set(jobs):
        errors.append("coverage: missing or unknown requests")
    if any(not u.reason.strip() or not u.code.strip() for u in plan.unassigned):
        errors.append("missing unassigned reason")
    expected = (len(assigned),len(unassigned),len(set(route_ids)),distance_total,travel_total,service_total)
    actual = (plan.metrics.assigned,plan.metrics.unassigned,plan.metrics.engineers_used,
              plan.metrics.distance_m,plan.metrics.travel_minutes,plan.metrics.service_minutes)
    if actual != expected:
        errors.append("plan metrics mismatch")
    return errors


def require_valid(scenario, travel, plan):
    errors = validate_plan(scenario,travel,plan)
    if errors:
        raise ValueError("Invalid plan: " + "; ".join(errors))
    return plan
