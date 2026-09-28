from conftest import engineer, job
from test_planning import travel
from field_router.assignment_diagnostics import annotate_unassigned
from field_router.models import Equipment, Transport
from field_router.planning import make_plan


def diagnosis(scenario, sequences, key):
    provider=travel(scenario, minutes=0)
    original=make_plan(scenario,provider,sequences,'test','valid')
    result=annotate_unassigned(scenario,provider,original)
    assert result.routes == original.routes
    assert result.metrics == original.metrics
    assert result.diagnostics['unassigned_diagnostics'][key]['global_impossibility_proven'] is False
    return result,result.diagnostics['unassigned_diagnostics'][key]


def test_checks_middle_slot_without_mutating_plan(make_scenario):
    scenario=make_scenario([job('a',start=600,end=600),job('b',start=720,end=720),
                            job('new',start=650,end=650)])
    result,detail=diagnosis(scenario,{'e1':['a','b']},'new')
    assert detail['checked_positions']==3
    assert detail['engineers'][0]['fixed_positions']==[2]
    assert detail['fixed_slots']==1
    assert 'без изменения начала' in result.unassigned[0].reason


def test_distinguishes_required_retiming_from_preserved_promises(make_scenario):
    scenario=make_scenario([job('a',start=540,end=750),job('new',start=540,end=550)],
                           [engineer(start=540)])
    result,detail=diagnosis(scenario,{'e1':['a']},'new')
    assert detail['fixed_slots']==0
    assert detail['retimed_slots']==1
    assert 'изменением времени' in result.unassigned[0].reason


def test_respects_issued_stock_already_reserved_for_other_visits(make_scenario):
    scenario=make_scenario([job('a',equipment=Equipment(router=1)),job('new',equipment=Equipment(router=1))],
                           [engineer(stock=Equipment(router=1))])
    result,detail=diagnosis(scenario,{'e1':['a']},'new')
    assert detail['counts']['stock']==1
    assert detail['checked_positions']==0
    assert detail['engineers'][0]['remaining_stock']['router']==0
    assert 'свободного оборудования' in result.unassigned[0].reason


def test_reports_individual_resource_filters(make_scenario):
    scenario=make_scenario([job('new',required_transport=Transport.BICYCLE)],
                           [engineer('e1',transport=Transport.CAR),engineer('e2',transport=Transport.BICYCLE,off_duty=True)])
    result,detail=diagnosis(scenario,{},'new')
    assert detail['counts']['transport']==1
    assert detail['counts']['off_duty']==1
    assert result.unassigned[0].code=='OFF_DUTY'


def test_zero_capacity_worker_is_not_reported_as_available(make_scenario):
    scenario=make_scenario([job('new')],[engineer(start=540,end=540)])
    result,detail=diagnosis(scenario,{},'new')
    assert detail['counts']['shift_capacity']==1
    assert detail['checked_positions']==0


def test_checks_indirect_insertion_even_when_direct_trip_is_impossible(make_scenario):
    scenario=make_scenario([job('a',start=540,end=540,duration=10),job('new',start=550,end=550)])
    provider=travel(scenario,minutes=0)
    # Nonmetric public/road matrices need not satisfy the triangle inequality.
    for mode in Transport:
        rows=[list(row) for row in provider._minutes[mode]]
        rows[provider.index['office']][provider.index['new']]=200
        provider._minutes[mode]=tuple(tuple(row) for row in rows)
    plan=make_plan(scenario,provider,{'e1':['a']},'test','valid')
    result=annotate_unassigned(scenario,provider,plan)
    assert result.diagnostics['unassigned_diagnostics']['new']['fixed_slots']==1


def test_failed_slots_are_not_global_impossibility(make_scenario):
    scenario=make_scenario([job('a',start=600,end=600,duration=100),job('new',start=630,end=650)])
    result,detail=diagnosis(scenario,{'e1':['a']},'new')
    assert detail['fixed_slots']==detail['retimed_slots']==0
    assert detail['counts']['window']==2
    assert detail['engineers'][0]['obstacle_examples'][0]['arrival']>detail['engineers'][0]['obstacle_examples'][0]['latest_start']
    assert 'Проверено мест' in result.unassigned[0].reason
    assert 'не доказана' in result.unassigned[0].reason
