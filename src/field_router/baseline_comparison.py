"""Compact comparison on the exact inputs used to calculate a published plan."""
from .solvers.baseline import BaselineSolver
from .validation import require_valid


def compare_baseline(scenario, travel, plan, *, version, time):
    baseline = BaselineSolver().solve(scenario, travel)
    require_valid(scenario, travel, baseline)
    def metrics(item):
        return {key: getattr(item.metrics, key) for key in
                ('assigned', 'unassigned', 'distance_m', 'travel_minutes')}
    distances = {r.engineer_id: r.distance_m for r in plan.routes}
    baseline_distances = {r.engineer_id: r.distance_m for r in baseline.routes}
    counts = {r.engineer_id: len(r.visits) for r in plan.routes}
    baseline_counts = {r.engineer_id: len(r.visits) for r in baseline.routes}
    return {'version': version, 'time': time, 'scope': 'remaining_day',
            'baseline': metrics(baseline), 'plan': metrics(plan),
            'engineers': [{'engineer_id': e.id, 'baseline_distance_m': baseline_distances.get(e.id, 0),
                           'plan_distance_m': distances.get(e.id, 0),
                           'baseline_assigned': baseline_counts.get(e.id, 0), 'plan_assigned': counts.get(e.id, 0)} for e in scenario.engineers]}


def render_baseline_comparison(comparison, engineers):
    """Render only saved aggregates; no recalculation or historical mutation."""
    if not comparison:
        return ''
    from html import escape
    baseline, plan = comparison['baseline'], comparison['plan']
    names = {e.id: e.name for e in engineers}
    parts = ['<section class="panel card pad baseline-comparison" aria-label="Сравнение с базовым планом">',
             '<h2>Сравнение с базовым планом</h2>',
             '<p class="muted">Те же заявки, инженеры, старты и оборудование. Базовый алгоритм берёт заявки по порядку и назначает первому подходящему инженеру. Сравнение остатка дня на момент расчёта.</p>',
             '<div class="scroll"><table><thead><tr><th>Показатель</th><th>Базовый</th><th>Применённый</th></tr></thead><tbody>']
    for label, key, scale in [('Назначено', 'assigned', 1), ('Без исполнителя', 'unassigned', 1),
                               ('Расстояние, км', 'distance_m', 1000), ('В пути, мин', 'travel_minutes', 1)]:
        values = [f'{item[key]/scale:.2f}' if scale == 1000 else str(item[key]) for item in (baseline, plan)]
        parts.append('<tr><td>' + label + '</td>' + ''.join('<td>' + value + '</td>' for value in values) + '</tr>')
    parts.append('</tbody></table></div><details><summary>Расстояние по инженерам</summary><div class="scroll"><table><thead><tr><th>Инженер</th><th>Базовый, заявок</th><th>Применённый, заявок</th><th>Базовый, км</th><th>Применённый, км</th></tr></thead><tbody>')
    for row in comparison['engineers']:
        parts.append('<tr><td>' + escape(names.get(row['engineer_id'], row['engineer_id'])) + '</td>'
                     + f'<td>{row["baseline_assigned"]}</td><td>{row["plan_assigned"]}</td><td>{row["baseline_distance_m"]/1000:.2f}</td><td>{row["plan_distance_m"]/1000:.2f}</td></tr>')
    return ''.join(parts) + '</tbody></table></div></details></section>'
