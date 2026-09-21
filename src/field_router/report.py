"""Self-contained Russian HTML report; all scenario content is escaped."""
from html import escape
import json

from .models import Plan, Scenario


def _e(value) -> str:
    return escape(str(value), quote=True)


def _time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _json(value) -> str:
    return _e(json.dumps(value, ensure_ascii=False, indent=2, default=str))


_PRIORITY = {"urgent": "Авария · максимальный приоритет", "high": "Подключение · после аварий", "normal": "Обычный"}
_TRANSPORT = {"car": "Автомобиль", "walk": "Пешком", "bicycle": "Велосипед", "public": "Общественный транспорт"}
_STATUS = {
    "FEASIBLE": "Допустимое решение", "OPTIMAL": "Оптимальное решение",
    "INFEASIBLE": "Допустимое решение не найдено", "UNKNOWN": "Результат не определён",
    "empty": "Нет заявок", "sequential_complete": "Последовательное назначение завершено",
    "feasible_search_completed": "Поиск завершён: допустимый план",
    "fallback_baseline_no_solution": "Резервный базовый план: поиск не дал решения",
    "fallback_baseline_better": "Базовый план оказался лучше",
}


_STYLE = """
:root{color-scheme:light;--ink:#18332f;--muted:#62716d;--line:#dae4de;--green:#17694f}
*{box-sizing:border-box}body{margin:0;background:#f2f5f0;color:var(--ink);font:15px/1.55 system-ui,sans-serif}
main{max-width:1220px;margin:auto;padding:42px 24px}h1{font-size:clamp(30px,4vw,46px);line-height:1.12;letter-spacing:-1.5px;margin:10px 0 16px}
h2{font-size:24px;margin:30px 0 14px}h3{font-size:18px}.eyebrow{font-size:12px;letter-spacing:2px;color:var(--green);font-weight:700}
p{margin:8px 0}.muted,small{color:var(--muted)}.notice{background:#fff3d8;border-left:4px solid #bc8a29;padding:16px 20px;margin:24px 0;border-radius:6px}
.card,details.route{background:white;border:1px solid var(--line);border-radius:12px;margin:14px 0;overflow:hidden}.pad{padding:20px}.scroll{overflow:auto}
table{width:100%;border-collapse:collapse;text-align:left;font-size:14px}th{background:#edf3ee;font-weight:600;white-space:nowrap}td,th{padding:12px 15px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}
summary{cursor:pointer;padding:18px 20px;font-weight:650}summary:hover{background:#f8faf7}.pill{display:inline-block;background:#e6f1ea;color:var(--green);border-radius:100px;padding:3px 10px;font-size:12px;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.stat{padding:18px;background:#fff;border:1px solid var(--line);border-radius:12px}.stat b{display:block;font-size:28px}.tabs{display:flex;flex-wrap:wrap;gap:8px;margin:24px 0}
a{color:var(--green)}.tabs a{padding:9px 16px;background:white;border:1px solid var(--line);border-radius:8px;text-decoration:none}.tabs a[aria-current=true]{background:var(--green);color:white}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f8f5;padding:16px;border-radius:8px;font-size:12px}ul{padding-left:22px}footer{margin-top:36px;border-top:1px solid var(--line);padding-top:18px;color:var(--muted);font-size:13px}
@media(max-width:600px){main{padding:24px 14px}td,th{padding:10px}.pad{padding:14px}}@media print{body{background:white}main{padding:0}.tabs{display:none}[hidden]{display:block!important}details{break-inside:avoid}}
"""

# Script contains static code only; data and IDs are generated as escaped HTML.
_SCRIPT = """
const links=[...document.querySelectorAll('.tabs a')];
const panels=[...document.querySelectorAll('.plan')];
function selectPlan(id){panels.forEach(p=>p.hidden=p.id!==id);links.forEach(a=>a.setAttribute('aria-current',String(a.hash==='#'+id)));}
links.forEach(a=>a.addEventListener('click',e=>{e.preventDefault();selectPlan(a.hash.slice(1));}));
if(panels.length)selectPlan(panels[0].id);
window.addEventListener('beforeprint',()=>document.querySelectorAll('details').forEach(d=>{d.dataset.wasOpen=String(d.open);d.open=true;}));
window.addEventListener('afterprint',()=>document.querySelectorAll('details').forEach(d=>d.open=d.dataset.wasOpen==='true'));
"""


def render_report(scenario: Scenario, plans: list[Plan]) -> str:
    """Return an autonomous UTF-8 HTML document without network dependencies."""
    real = scenario.metadata.get("geography") == "wgs84"
    geo_notice = ('<aside class="notice"><strong>Реальные координаты; время поездок рассчитано моделью.</strong><p>Дорожные профили без актуальных пробок. Общественный транспорт — явно обозначенная оценка без расписаний. Источники и параметры сохранены ниже.</p></aside>' if real else
        '<aside class="notice"><strong>Синтетические координаты. Время и расстояния приблизительные.</strong><p>Демонстрационная модель передвижения: это не реальные дорожные маршруты Москвы. Пробки, расписания общественного транспорта и актуальная дорожная сеть не подтверждены. Параметры расчёта приведены в разделе допущений.</p></aside>')
    requests = {r.id: r for r in scenario.requests}
    engineers = {e.id: e for e in scenario.engineers}
    parts = ['<!doctype html><html lang="ru"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             '<title>План выездов — ' + _e(scenario.id) + '</title><style>' + _STYLE + '</style></head><body><main>',
             '<header><div class="eyebrow">ПЛАНИРОВАНИЕ ВЫЕЗДОВ · ДЕМОНСТРАЦИОННЫЙ ОТЧЁТ</div>',
             '<h1>Рабочий день под контролем</h1><p>' + _e(scenario.region) + ' · ' + _e(scenario.date) + '</p>',
             '<p class="muted">Сценарий ' + _e(scenario.id) + ' · Офис: ' + _e(scenario.office_address) + '</p></header>',
             geo_notice,
             f'<div class="grid"><div class="stat"><b>{len(scenario.requests)}</b>заявок в сценарии</div>'
             f'<div class="stat"><b>{len(scenario.engineers)}</b>доступных инженеров</div>'
             f'<div class="stat"><b>{len(plans)}</b>вариантов плана</div></div>',
             '<h2>Сравнение вариантов</h2><p class="muted">Приоритет: аварии, затем подключения, затем ремонт и дозаказы.</p>']
    if not plans:
        parts.append('<p>Планы отсутствуют. Выполните расчёт для этого сценария.</p>')
    else:
        parts.append('<div class="card scroll"><table><thead><tr><th>Алгоритм / статус</th>'
                     '<th>Назначено</th><th>Аварии + подключения, назначено / всего</th><th>Не назначено</th><th>Инженеров</th><th>Путь, км</th>'
                     '<th>В пути, мин</th><th>Работа, мин</th><th>Расчёт, с</th></tr></thead><tbody>')
        for plan in plans:
            m = plan.metrics
            top_ids = {r.id for r in scenario.requests if r.priority in {"urgent", "high"}}
            assigned_ids = {v.request_id for route in plan.routes for v in route.visits}
            priority_coverage = f"{len(top_ids & assigned_ids)} / {len(top_ids)}"
            parts.append(f'<tr><td><b>{_e(plan.solver)}</b><br><span class="pill">{_e(_STATUS.get(plan.status, plan.status))}</span></td>'
                         f'<td>{m.assigned}</td><td>{priority_coverage}</td><td>{m.unassigned}</td><td>{m.engineers_used}</td>'
                         f'<td>{m.distance_m / 1000:.2f}</td><td>{m.travel_minutes}</td>'
                         f'<td>{m.service_minutes}</td><td>{plan.elapsed_seconds:.3f}</td></tr>')
        parts.append('</tbody></table></div><nav class="tabs" aria-label="Варианты плана">')
        for index, plan in enumerate(plans):
            parts.append(f'<a href="#plan-{index}">{_e(plan.solver)}</a>')
        parts.append('</nav>')
    for index, plan in enumerate(plans):
        parts.append(f'<section class="plan" id="plan-{index}"><h2>Маршруты · {_e(plan.solver)}</h2>')
        if plan.solver.lower() in ("ortools","alns","hybrid"):
            parts.append('<p class="notice">Математическая оптимальность не гарантируется. '
                         'Показан допустимый результат ограниченного поиска или резервный базовый план.</p>')
        parts.append('<h3>Утренняя загрузка</h3><div class="card scroll"><table><thead><tr>'
                     '<th>Инженер</th><th>Роутеры</th><th>ТВ-приставки</th>'
                     '</tr></thead><tbody>')
        for route in plan.routes:
            engineer = engineers.get(route.engineer_id)
            parts.append(f'<tr><td>{_e(engineer.name if engineer else route.engineer_id)}</td>'
                         f'<td>{route.loadout.router}</td><td>{route.loadout.tv_box}</td></tr>')
        parts.append('<tr><td><strong>Итого</strong></td>'
                     f'<td>{sum(r.loadout.router for r in plan.routes)}</td>'
                     f'<td>{sum(r.loadout.tv_box for r in plan.routes)}</td></tr></tbody></table></div>')
        if not plan.routes:
            parts.append('<p class="muted">Назначенных маршрутов нет.</p>')
        for route_index, route in enumerate(plan.routes):
            engineer = engineers.get(route.engineer_id)
            name = engineer.name if engineer else route.engineer_id
            parts.append(f'<details class="route"{" open" if route_index == 0 else ""}><summary>{_e(name)} '
                         f'<span class="pill">{len(route.visits)} заявок · {route.distance_m / 1000:.2f} км</span></summary><div class="pad">')
            if engineer:
                parts.append(f'<p>Смена: {_time(engineer.shift.start)}–{_time(engineer.shift.end)} · '
                             f'Транспорт: {_e(_TRANSPORT.get(engineer.transport.value, engineer.transport.value))} · Старт: {_e(scenario.office_address if engineer.start_location == "office" else engineer.start_location)}</p>')
            parts.append(f'<p><strong>Загрузка оборудования:</strong> роутеры — {route.loadout.router}; '
                         f'ТВ-приставки — {route.loadout.tv_box}.</p>'
                         f'<p class="muted">В пути: {route.travel_minutes} мин · Обслуживание: {route.service_minutes} мин</p></div>'
                         '<div class="scroll"><table><thead><tr><th>№ / заявка</th><th>Адрес и окно</th>'
                         '<th>Переезд</th><th>Прибытие</th><th>Начало–конец</th><th>Работа</th></tr></thead><tbody>')
            for number, visit in enumerate(route.visits, 1):
                req = requests.get(visit.request_id)
                windows = ', '.join(f'{_time(w.start)}–{_time(w.end)}' for w in req.windows) if req else '—'
                address = req.address if req else 'Заявка отсутствует в сценарии'
                duration = f'{req.duration_minutes} мин' if req else '—'
                parts.append(f'<tr><td>{number}. {_e(visit.request_id)}</td><td>{_e(address)}<br><small>{windows}</small>'
                             f'<br><small>{_e(visit.explanation)}</small></td><td>{visit.travel_minutes} мин<br>'
                             f'<small>{visit.distance_m / 1000:.2f} км</small></td><td>{_time(visit.arrival)}</td>'
                             f'<td>{_time(visit.start)}–{_time(visit.end)}</td><td>{duration}</td></tr>')
            parts.append('</tbody></table></div></details>')
        parts.append('<h3>Неназначенные заявки</h3>')
        if not plan.unassigned:
            parts.append('<p class="muted">Неназначенных заявок нет.</p>')
        else:
            parts.append('<div class="card scroll"><table><thead><tr><th>Заявка / адрес</th><th>Причина</th></tr></thead><tbody>')
            for item in plan.unassigned:
                req = requests.get(item.request_id)
                parts.append(f'<tr><td>{_e(item.request_id)}<br>{_e(req.address if req else "—")}</td>'
                             f'<td>{_e(item.reason)}<br><small>{_e(item.code)}</small></td></tr>')
            parts.append('</tbody></table></div>')
        parts.append('<details class="card"><summary>Диагностика алгоритма</summary><div class="pad"><pre>'
                     + _json({"solver": plan.solver, "status": plan.status, "diagnostics": plan.diagnostics}) + '</pre></div></details></section>')
    parts.append('<h2>Допущения и происхождение данных</h2><div class="card pad"><ul>')
    for assumption in scenario.assumptions:
        parts.append('<li>' + _e(assumption) + '</li>')
    parts.append('</ul><h3>Параметры передвижения</h3><pre>' + _json(scenario.travel_config)
                 + '</pre><h3>Метаданные сценария</h3><pre>' + _json(scenario.metadata) + '</pre></div>')
    parts.append('<details class="card"><summary>Источники и параметры заявок</summary><div class="pad">')
    for req in scenario.requests:
        parts.append(f'<h3>{_e(req.id)} · {_e(req.address)}</h3><p>Исходная строка: {_e(req.source_line if req.source_line is not None else "не указана")} · '
                     f'Навык: {_e(req.skill.value)} · Приоритет: {_e(_PRIORITY.get(req.priority, req.priority))}</p><pre>{_json(req.provenance)}</pre>')
    parts.append('</div></details><footer>Автономный отчёт · Все времена указаны в пределах рабочего дня. '
                 'Возвращение в офис не включено в маршруты MVP. Путь приведён только по рассчитанным участкам до заявок.</footer>'
                 '</main><script>' + _SCRIPT + '</script></body></html>')
    return ''.join(parts)
