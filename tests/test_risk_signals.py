import pytest

from field_router.risk_signals import frequency_label, review_counts, signal
from field_router.risk_summary import findings
from test_day_control import setup, control


def test_frequency_rounding_matches_ui_and_preserves_nonzero_edge_cases():
    assert [frequency_label(p) for p in [0, 1/600, .125, 599/600, 1]] == [
        '0%', '<1%', '13%', '>99%', '100%']


def test_shared_review_rules_do_not_escalate_tiny_delays_or_malformed_scores():
    assert not signal({'p_sla_breach': .249}, 'sla')
    assert signal({'p_sla_breach': .25}, 'sla')
    assert not signal({'p_late_start': .99, 'expected_late_minutes': .5}, 'late')
    assert signal({'p_late_start': .5, 'expected_late_minutes': 10}, 'late')
    for value in [None, True, float('nan'), float('inf'), -1, 1.01]:
        assert not signal({'p_overtime': value, 'expected_overtime_minutes': 20}, 'overtime')


def test_missing_estimates_and_an_empty_scope_are_different():
    rows = [{'engineer_id': 'e', 'p_overtime': .8, 'expected_overtime_minutes': 20,
             'visits': [{'request_id': 'a', 'p_sla_breach': .25}]}]
    result = review_counts(rows, {'a', 'missing'}, {'e'})
    assert result['sla']['count'] is None and result['sla']['total'] == 2
    assert result['late']['count'] is None
    assert result['overtime']['count'] == 1
    empty = review_counts([], set(), set())
    assert empty['sla']['count'] == empty['sla']['total'] == 0


def test_panel_queue_and_summary_share_overtime_scope_and_threshold(make_scenario):
    session = setup(make_scenario)
    route = {'engineer_id': 'e1', 'p_overtime': .5, 'expected_overtime_minutes': 10,
             'visits': [{'request_id': rid, 'p_sla_breach': .25,
                         'p_late_start': .7, 'expected_late_minutes': 2} for rid in ['a', 'b']]}
    result = control(session, [route])
    review = result['forecast_review']
    assert result['counts']['overtime'] == review['overtime']['count'] == 1
    assert review['sla']['count'] == 2 and review['late']['count'] == 0
    snapshot = {'context': {'region': session.scenario.region, 'plan_version': session.version},
                'forecast': {'status': 'ready', 'region': session.scenario.region,
                             'plan_version': session.version, 'review': review}}
    assert {'sla', 'overtime'} <= {r['id'] for r in findings(snapshot)}
    assert 'late' not in {r['id'] for r in findings(snapshot)}
    session.time = 600
    assert control(session, [route])['forecast_review']['sla']['total'] == 1
    session.fact_log.append({'request_id': 'b', 'status': 'completed'})
    assert control(session, [route])['forecast_review']['sla']['total'] == 0


@pytest.mark.parametrize('changed', [{'needs_replanning': True}, {'time': 601}, {'plan_version': 8}])
def test_ai_summary_never_uses_outdated_model_counts(changed):
    context = {'region': 'test', 'plan_version': 7, 'time': 600, **changed}
    snapshot = {'context': context, 'forecast': {'status': 'ready', 'region': 'test',
        'plan_version': 7, 'evaluated_at': 600,
        'review': {k: {'count': 5, 'total': 5} for k in ['sla', 'late', 'overtime']}}}
    ids = {r['id'] for r in findings(snapshot)}
    assert 'forecast_missing' in ids
    assert not ids & {'sla', 'late', 'overtime', 'clear'}
