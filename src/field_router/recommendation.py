"""Deterministic recommendation over already validated plan alternatives."""

LABELS={'stable':'Меньше изменений','sla':'Быстрее к аварии','cost':'Меньше дороги'}
STRATEGY_ORDER=('stable','sla','cost')


def recommendation_rank(candidate):
    facts=candidate['facts'];emergency=candidate.get('emergency')
    response=0
    if emergency:
        response=emergency.get('response_minutes')
        if response is None:response=1441
    # Feasibility/priority coverage stays lexicographically first.  Once two
    # plans cover the same work, compare their simulated operational risk.
    # Older/imported batches without a risk block retain the former ordering.
    risk=candidate.get('risk')
    risk_rank=()
    if isinstance(risk,dict) and risk.get('status','ready')=='ready':
        summary=risk.get('summary',risk)
        risk_rank=(summary.get('p_sla_breach',1),summary.get('expected_loss',float('inf')),
                   risk.get('affected_clients',summary.get('expected_affected_customers',float('inf'))),
                   summary.get('p_overtime',1))
    return (-facts.get('emergency_assigned',0),-facts.get('connection_assigned',0),
            facts.get('unassigned',0),*risk_rank,facts.get('outside_scope_changes',0),response,
            facts.get('avoidable_reassigned',facts.get('reassigned',0)),facts.get('time_shift_minutes',0),
            facts.get('travel_minutes',0),facts.get('distance_m',0),facts.get('engineers_used',0),
            STRATEGY_ORDER.index(candidate['id']) if candidate['id'] in STRATEGY_ORDER else 3)


def eligible_candidates(batch):
    """LLM explains the server rule; it cannot substitute another trade-off."""
    candidates=[c for c in batch['candidates'] if not c.get('duplicate_of')]
    if not candidates:raise ValueError('Нет вариантов для рекомендации')
    if batch.get('recommendation_policy')=='preserve_approved':
        # A retry with no new facts must not exchange promised customers just
        # to improve a secondary metric. Keep this policy common to the solver
        # recommendation and optional LLM wording.
        def retry_rank(candidate):
            facts=candidate['facts']
            return (facts.get('previous_approved_lost',facts.get('lost_assignments',0)),
                    facts.get('unassigned',0),
                    facts.get('avoidable_reassigned',facts.get('reassigned',0)),
                    facts.get('time_shift_minutes',0),recommendation_rank(candidate))
        best=min(retry_rank(candidate) for candidate in candidates)
        return [candidate for candidate in candidates if retry_rank(candidate)==best]
    best=min(recommendation_rank(c) for c in candidates)
    return [c for c in candidates if recommendation_rank(c)==best]
