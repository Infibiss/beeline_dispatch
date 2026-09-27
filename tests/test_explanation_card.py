from copy import deepcopy
from field_router.llm import Explainer


def example():
    def candidate(key, loss, changed, travel):
        return {'id':key, 'facts':{'assigned':66,'unassigned':0,
            'emergency_assigned':8,'connection_assigned':31,'lost_assignments':0,
            'reassigned':changed,'avoidable_reassigned':changed,
            'time_shift_minutes':333 if changed else 0,'travel_minutes':travel},
            'risk':{'status':'ready','affected_clients':5 if changed else 0,
                'summary':{'p_sla_breach':1.0,'expected_loss':loss,'p_overtime':.3}},
            'explanation':'old text'}
    return {'id':'b','version':1,'event':{'type':'replan'},'candidates':[
        candidate('sla',79.45,1,1624),candidate('stable',86.31,0,1532)]}


def test_card_exposes_actual_risk_tradeoff_without_claiming_faster_emergency():
    from field_router.explanation_card import build_card
    b=example(); card=build_card(b,b['candidates'][0])
    text=str(card)
    assert '79.45' in text and '86.31' in text
    assert '92' in text and '333' in text and '5' in text
    assert 'не откалиброван' in text
    assert 'раньше' not in text
    assert len(card['reasons'])<=2


def test_equal_metrics_and_critical_warnings_are_not_hidden():
    from field_router.explanation_card import build_card
    b=example(); a=b['candidates'][0]
    a['facts'].update(unassigned=2,lost_assignments=1)
    a['emergency']={'assigned':True,'response_minutes':145,'exceeds_response_target':True}
    other=deepcopy(a);other['id']='stable';b['candidates']=[a,other]
    card=build_card(b,a)
    assert 'такой же' in str(card['reasons'])
    assert 'Без исполнителя: 2' in str(card['attention'])
    assert 'Потеря' in str(card['attention'])
    assert '145' in str(card['attention'])


def test_provider_failure_still_returns_structured_card():
    result=Explainer(send=lambda _: {},max_calls=0).recommend(example())
    assert result['card']['candidate_id']==result['candidate_id']
    assert result['card']['summary']


def test_model_emphasis_cannot_hide_primary_reason_or_warnings():
    from field_router.explanation_card import build_card
    b=example(); a=b['candidates'][0]
    a['facts'].update(unassigned=0,travel_minutes=1400,avoidable_reassigned=0,time_shift_minutes=0)
    b['candidates'][1]['facts']['avoidable_reassigned']=2
    card=build_card(b,a,emphasis=['coverage','travel','changes'])
    assert 'Потери по модели' in card['reasons'][0]
    assert 'Дороги' in card['reasons'][1]
    assert 'визит' in str(card['attention'])


def test_retry_explains_preservation_before_coverage_tradeoff():
    from field_router.explanation_card import build_card
    b=example(); b['recommendation_policy']='preserve_approved'
    sla,stable=b['candidates']
    sla['facts']['previous_approved_lost']=1
    stable['facts'].update(previous_approved_lost=0,unassigned=1)
    card=build_card(b,stable)
    assert 'Потеря обещанных визитов: 0 против 1' in card['reasons'][0]
    assert 'Без исполнителя' in str(card['tradeoffs'])


def test_overtime_difference_is_not_presented_as_identical_plans():
    from field_router.explanation_card import build_card
    b=example(); a=b['candidates'][0]; other=deepcopy(a);other['id']='stable'
    b['candidates']=[a,other]
    a['risk']['summary']['p_overtime']=.1
    other['risk']['summary']['p_overtime']=.9
    card=build_card(b,a)
    assert 'Риск переработки, %: 10 против 90' in card['reasons'][0]
