from importlib.util import find_spec

from field_router.models import (Scenario, Engineer, Request, TimeWindow, Skill, Transport,
    Equipment, Visit, Route, Metrics, Plan, Unassigned)


def fixture():
    request = Request(id='req1', region='Центр', address='<img src=x onerror=alert(1)>', date='2026-09-18',
                      windows=[TimeWindow(start=540, end=720)], duration_minutes=40,
                      skill=Skill.CONNECTION, provenance={'address': 'CSV: строка 2'})
    scenario = Scenario(id='demo', region='Центр', date='2026-09-18', office_address='Офис',
        requests=[request], engineers=[Engineer(id='eng1', name='Иван', region='Центр',
        shift=TimeWindow(start=540, end=1080), skills=[Skill.CONNECTION], transport=Transport.CAR)],
        assumptions=['Синтетические координаты'], travel_config={'speed_kmh': 25},
        metadata={'source': '<script>alert(2)</script>'})
    route = Route(engineer_id='eng1', visits=[Visit(request_id='req1', arrival=550, start=550,
        end=590, travel_minutes=10, distance_m=2500, explanation='Назначено')],
        distance_m=2500, travel_minutes=10, service_minutes=40, loadout=Equipment(router=1, tv_box=2))
    plan = Plan(solver='greedy', status='FEASIBLE', scenario_id='demo', routes=[route], unassigned=[],
        metrics=Metrics(assigned=1, unassigned=0, engineers_used=1, distance_m=2500,
        travel_minutes=10, service_minutes=40), elapsed_seconds=0.125)
    return scenario, plan


def render(scenario, plans):
    assert find_spec('field_router.report') is not None, 'HTML report renderer must exist'
    from field_router.report import render_report
    return render_report(scenario, plans)


def test_report_is_self_contained_and_escapes_external_text():
    scenario, plan = fixture()
    html = render(scenario, [plan])
    assert '<html lang="ru">' in html
    assert '<meta charset="utf-8">' in html
    assert '<img src=x' not in html
    assert '<script>alert(2)</script>' not in html
    assert '&lt;img src=x onerror=alert(1)&gt;' in html
    assert '&lt;script&gt;alert(2)&lt;/script&gt;' in html
    assert 'https://' not in html and 'http://' not in html


def test_report_covers_schedule_metrics_equipment_and_provenance():
    scenario, plan = fixture()
    other = plan.model_copy(update={'solver': 'ortools', 'unassigned': [Unassigned(
        request_id='req1', code='NO_SKILL', reason='Нет квалификации')], 'routes': []})
    html = render(scenario, [plan, other])
    for value in ['greedy', 'ortools', 'FEASIBLE', '2.50', '0.125', '09:10', '09:50',
                  '09:00–12:00', 'Роутеры', 'ТВ-приставки', 'Нет квалификации', 'NO_SKILL',
                  'CSV: строка 2', 'speed_kmh', 'Синтетические координаты', 'приблизительн']:
        assert value in html
    assert '<details' in html


def test_report_handles_no_plans_and_no_requests():
    scenario, _ = fixture()
    html = render(scenario.model_copy(update={'requests': []}), [])
    assert 'Планы отсутствуют' in html


def test_dispatcher_loadout_totals_russian_labels_and_optimality_limit():
    scenario, plan = fixture()
    second = plan.routes[0].model_copy(update={'loadout': Equipment(router=3, tv_box=5)})
    plan = plan.model_copy(update={'solver': 'ortools', 'routes': [plan.routes[0], second]})
    html = render(scenario, [plan])
    for text in ['Автомобиль', 'Допустимое решение', 'Утренняя загрузка',
                 '<td>4</td><td>7</td>', 'Математическая оптимальность не гарантируется',
                 'Возвращение в офис не включено', 'FEASIBLE']:
        assert text in html
    assert html.count('<details class="route" open>') == 1
    assert html.index('Утренняя загрузка') < html.index('<details class="route"')


def test_report_counts_emergency_and_new_connection_as_equal_top_priority():
    scenario, plan = fixture()
    urgent = scenario.requests[0].model_copy(update={'id': 'emergency', 'priority': 'urgent'})
    high = scenario.requests[0].model_copy(update={'priority': 'high'})
    scenario = scenario.model_copy(update={'requests': [urgent, high]})
    html = render(scenario, [plan])
    assert 'Высший уровень, выполнено / всего' in html
    assert '<td>1 / 2</td>' in html
    assert 'Аварийные заявки и новые подключения имеют одинаковый высший приоритет' in html
    assert 'Высокий · высший уровень' in html
    assert 'Срочный · высший уровень' in html
