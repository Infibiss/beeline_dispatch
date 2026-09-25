from field_router.recommendation import eligible_candidates, recommendation_rank


EQUAL_COVERAGE = {
    'emergency_assigned': 1,
    'connection_assigned': 2,
    'unassigned': 0,
    'avoidable_reassigned': 0,
    'time_shift_minutes': 0,
    'travel_minutes': 30,
    'distance_m': 1_000,
    'engineers_used': 1,
}


def _candidate(candidate_id, *, p_sla, expected_loss, affected_clients, p_overtime):
    return {
        'id': candidate_id,
        'duplicate_of': None,
        'facts': dict(EQUAL_COVERAGE),
        'risk': {
            'status': 'ready',
            'summary': {
                'p_sla_breach': p_sla,
                'expected_loss': expected_loss,
                'expected_affected_customers': affected_clients,
                'p_overtime': p_overtime,
            },
        },
    }


def test_risk_rank_breaks_equal_coverage_by_sla_loss_then_affected_clients():
    comparisons = [
        # P(SLA) is first even when every later risk metric is worse.
        (
            _candidate('stable', p_sla=.20, expected_loss=.1, affected_clients=.1, p_overtime=0),
            _candidate('cost', p_sla=.10, expected_loss=9, affected_clients=2, p_overtime=1),
        ),
        # With equal P(SLA), expected loss is the next discriminator.
        (
            _candidate('stable', p_sla=.10, expected_loss=2, affected_clients=.1, p_overtime=0),
            _candidate('cost', p_sla=.10, expected_loss=1, affected_clients=2, p_overtime=1),
        ),
        # With equal P(SLA) and loss, expected affected customers decides.
        (
            _candidate('stable', p_sla=.10, expected_loss=1, affected_clients=1.2, p_overtime=0),
            _candidate('cost', p_sla=.10, expected_loss=1, affected_clients=.8, p_overtime=1),
        ),
    ]

    for stable, lower_risk in comparisons:
        assert recommendation_rank(lower_risk)<recommendation_rank(stable)
        assert [candidate['id'] for candidate in eligible_candidates(
            {'candidates': [stable, lower_risk]}
        )]==['cost']
