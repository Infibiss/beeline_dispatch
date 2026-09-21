"""Exact dispatcher objectives shared by every solver and candidate selector."""


def reference_visits(scenario, reference):
    """Keep surviving jobs' promised times, even when their owner has left."""
    if reference is None:
        return {}
    jobs = {request.id for request in scenario.requests}
    return {visit.request_id: (route.engineer_id, visit.start)
            for route in reference.routes for visit in route.visits
            if visit.request_id in jobs and 0 <= visit.start <= 1440}


def response_targets(scenario, focus_request_id=None):
    """An explicit emergency, or every urgent request when there is no focus."""
    urgent = {request.id for request in scenario.requests if request.priority == 'urgent'}
    if focus_request_id is not None:
        if focus_request_id not in urgent:
            raise ValueError('focus_request_id must identify an urgent request')
        return {focus_request_id}
    return urgent


def objective_key(plan, scenario, *, profile=None, reference=None, focus_request_id=None):
    """Lexicographic coverage, then morning resources or operational priorities.

    Missing response targets score 1441 (outside the planning day). Lost previous
    assignments remain visible to the UI, but are not a separate objective tier.
    """
    if profile not in (None, 'stable', 'cost', 'sla'):
        raise ValueError('Unknown objective profile')
    targets = response_targets(scenario, focus_request_id)
    urgent = {request.id for request in scenario.requests if request.priority == 'urgent'}
    high = {request.id for request in scenario.requests if request.priority in ('urgent', 'high')}
    coverage = (sum(item.request_id in urgent for item in plan.unassigned),
                sum(item.request_id in high for item in plan.unassigned), plan.metrics.unassigned)
    if profile is None:
        return coverage + (plan.metrics.engineers_used, plan.metrics.distance_m)
    prior = reference_visits(scenario, reference)
    available = {engineer.id for engineer in scenario.engineers}
    visits = {visit.request_id: (route.engineer_id, visit.start)
              for route in plan.routes for visit in route.visits}
    compared = [(visits[key], old) for key, old in prior.items() if key in visits]
    stability = (sum(old[0] in available and current[0] != old[0] for current, old in compared),
                 sum(abs(current[1] - old[1]) for current, old in compared))
    travel = (plan.metrics.travel_minutes, plan.metrics.distance_m)
    if profile == 'cost':
        return coverage + travel + stability + (plan.metrics.engineers_used,)
    if profile == 'sla':
        response = sum(visits[key][1] if key in visits else 1441 for key in targets)
        return coverage + (response,) + stability + travel + (plan.metrics.engineers_used,)
    return coverage + stability + travel + (plan.metrics.engineers_used,)
