"""Deterministic recommendation over already validated plan alternatives."""

LABELS={'stable':'Меньше изменений','sla':'Быстрее к аварии','cost':'Меньше дороги'}
STRATEGY_ORDER=('stable','sla','cost')


def recommendation_rank(candidate):
    facts=candidate['facts'];emergency=candidate.get('emergency')
    response=0
    if emergency:
        response=emergency.get('response_minutes')
        if response is None:response=1441
    return (-facts.get('emergency_assigned',0),-facts.get('connection_assigned',0),
            facts.get('unassigned',0),response,
            facts.get('avoidable_reassigned',facts.get('reassigned',0)),facts.get('time_shift_minutes',0),
            facts.get('travel_minutes',0),facts.get('distance_m',0),facts.get('engineers_used',0),
            STRATEGY_ORDER.index(candidate['id']) if candidate['id'] in STRATEGY_ORDER else 3)


def eligible_candidates(batch):
    """LLM explains the server rule; it cannot substitute another trade-off."""
    candidates=[c for c in batch['candidates'] if not c.get('duplicate_of')]
    if not candidates:raise ValueError('Нет вариантов для рекомендации')
    best=min(recommendation_rank(c) for c in candidates)
    return [c for c in candidates if recommendation_rank(c)==best]
