"""Render a portable dispatcher workbench with safe, embedded scenario data."""
import json
from html import escape
from urllib.parse import urlsplit
from pathlib import Path


_STATIC = Path(__file__).with_name('static')



def _operation_context(payload: dict) -> tuple[str, str]:
    """Render server-confirmed context separately from the recalculable plan."""
    operations = payload.get('operations')
    if not isinstance(operations, dict):
        return '', ''

    def safe(value):
        return escape(str(value if value is not None else '—'), quote=True)

    def clock(value):
        if isinstance(value, (int, float)) and value >= 0:
            return f'{int(value) // 60:02d}:{int(value) % 60:02d}'
        return '—'

    labels = {'available': 'Свободен', 'traveling': 'В пути', 'working': 'Работает',
              'completion_pending': 'Ожидает подтверждения диспетчера',
              'completed': 'Завершена по факту', 'interrupted': 'Прервана по факту',
              'unavailable': 'Недоступен'}
    statuses = operations.get('statuses') or []
    if isinstance(statuses, dict):
        statuses = list(statuses.values())
    by_engineer = {fact.get('engineer_id'): fact for fact in statuses}
    engineers = {item.get('id'): item.get('name', item.get('id'))
                 for item in payload.get('scenario', {}).get('engineers', [])}
    historical = operations.get('historical', operations.get('is_historical', False))
    heading = 'Предпросмотр · не применён' if operations.get('preview') else 'История · только просмотр' if historical else 'Текущая версия'
    mode = operations.get('mode', 'simulation')
    mode_label = {'simulation': 'Симуляция', 'manual': 'Ручной учёт',
                  'mixed': 'Ручные статусы + симуляция остальных'}.get(mode, mode)
    context = '<section class="notice" aria-label="Состояние версии"><strong>' + heading
    context += ' · ' + safe(operations.get('version')) + ' · ' + clock(operations.get('time'))
    context += '</strong><p>' + safe(mode_label) + '. Местоположение без GPS.</p>'
    if operations.get('needs_replanning'):
        context += '<p role="status"><strong>Факты изменились; показан последний утверждённый план, требуется пересчёт.</strong></p>'
    context += '</section>'
    rows = []
    for route in (operations.get('frozen') or []) + (operations.get('history') or []):
        engineer_id = route.get('engineer_id')
        for visit in route.get('visits', [route]):
            fact = by_engineer.get(engineer_id)
            status = visit.get('status')
            if status in ('completed', 'completion_pending', 'interrupted'):
                status_text = labels[status]
            elif fact and fact.get('request_id') == visit.get('request_id'):
                status_text = labels.get(fact.get('status'), fact.get('status', 'Записанный факт'))
            elif fact or mode == 'manual':
                status_text = 'Закреплена · завершение не подтверждено'
            else:
                now = operations.get('time', 0)
                if visit.get('end', float('inf')) <= now:
                    status_text = 'Завершена · симуляция'
                elif visit.get('start', float('inf')) <= now:
                    status_text = 'В работе · симуляция'
                else:
                    status_text = 'Закреплена · симуляция'
            consumed = visit.get('consumed_equipment', visit.get('consumed'))
            usage = ', '.join(str(key) + ': ' + str(value) for key, value in consumed.items()) if isinstance(consumed, dict) else '—'
            if isinstance(consumed, dict):
                spent = status in ('completed', 'interrupted') or (status != 'completion_pending' and not fact and visit.get('end', float('inf')) <= operations.get('time', 0))
                usage = ('Списано: ' if spent else 'Резерв: ') + usage
            values = [engineers.get(engineer_id, engineer_id), payload.get('request_labels', {}).get(visit.get('request_id'), visit.get('request_id')),
                      clock(visit.get('start', visit.get('time'))), clock(visit.get('end', visit.get('time'))),
                      status_text, usage]
            rows.append('<tr>' + ''.join('<td>' + safe(value) + '</td>' for value in values) + '</tr>')
    if not rows:
        rows.append('<tr><td colspan="6">Закреплённых работ и записей истории нет.</td></tr>')
    history = '<details class="panel"><summary>Закреплённые работы и история выполнения</summary>'
    history += '<p class="muted">Эти работы показаны отдельно от пересчитываемого остатка дня. Для ручного учёта завершение подтверждается фактом.</p>'
    history += '<div class="scroll"><table><thead><tr><th>Инженер</th><th>Заявка</th><th>Начало</th><th>Завершение</th><th>Статус</th><th>Оборудование: списание / резерв</th></tr></thead><tbody>'
    history += ''.join(rows) + '</tbody></table></div></details>'
    return context, history


def render_dispatcher(payload: dict) -> str:
    """Return UTF-8 HTML; vendor Leaflet into assets beside the output file.

    Client code and styles are embedded. JSON is inert and escapes all characters
    that can terminate a script element; client views use DOM text nodes.
    """
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    for char, replacement in [('&', r'\u0026'), ('<', r'\u003c'), ('>', r'\u003e'),
                              ('\u2028', r'\u2028'), ('\u2029', r'\u2029')]:
        data = data.replace(char, replacement)
    links = ['<a href="report.html">Табличный отчёт ↗</a>']
    for dataset in payload.get('datasets', []):
        url = str(dataset.get('url', ''))
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if url and parsed.scheme in ('', 'https', 'http') and not url.startswith(('//', '\\')):
            links.append('<a href="' + escape(url, quote=True) + '">'
                         + escape(str(dataset.get('region', 'Регион'))) + '</a>')
    dataset_links = '<nav class="dataset-links" aria-label="Отчёт и регионы">' + ''.join(links) + '</nav>'
    operations_context, operations_history = _operation_context(payload)
    plan_heading = 'Пересчитываемый остаток дня' if isinstance(payload.get('operations'), dict) else 'Каждый выезд — на своём месте'
    css = (_STATIC / 'dispatcher.css').read_text(encoding='utf-8')
    js = (_STATIC / 'dispatcher.js').read_text(encoding='utf-8')
    return '''<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Диспетчер · Выездные инженеры</title>
<link rel="stylesheet" href="./assets/leaflet.css">
<style>''' + css + '''</style></head><body class="''' + ('embedded' if payload.get('operations') else 'standalone') + '''">
<a class="skip-link" href="#schedule">К расписанию</a>
<header class="topbar"><div class="brand"><span class="brand-icon" aria-hidden="true">↗</span>
<div><strong>ПУЛЬТ ВЫЕЗДОВ</strong><span>Рабочее место диспетчера</span></div></div>
<div class="day"><strong id="scenario-date">Рабочий день</strong><span id="scenario-region"></span></div></header>
<main>''' + dataset_links + operations_context + '''<section class="heading"><div><p class="eyebrow">ОПЕРАЦИОННЫЙ ПЛАН</p><h1>''' + plan_heading + '''</h1>
<p id="scenario-description" class="muted">Карта, расписание и заявки в одном рабочем окне.</p></div>
<div class="plan-control"><label for="plan-select">Вариант планирования</label><select id="plan-select"></select>
<span id="plan-status" class="muted"></span></div></section>
<div id="notice" class="notice" role="status">Проверка источников географии…</div>
<section id="metrics" class="metrics" aria-label="Показатели плана"></section>
<section class="toolbar" aria-label="Фильтры"><div><label for="engineer-select">Инженер</label><select id="engineer-select"><option value="">Все инженеры</option></select></div>
<div class="search"><label for="request-search">Поиск заявки</label><input id="request-search" type="search" placeholder="Адрес, номер заявки или инженер" autocomplete="off"></div>
<button id="reset-filters" class="quiet-button" type="button">Сбросить фильтры</button><span id="filter-summary" class="muted" aria-live="polite"></span></section>
<section class="workspace" aria-label="Заявки и карта">
<div class="panel request-panel"><div class="panel-heading"><h2>Заявки</h2><span id="request-count" class="badge">0</span></div><div id="request-list" class="request-list"></div></div>
<div class="panel map-panel"><div class="panel-heading"><h2 id="map-title">Карта выездов</h2><button type="button" id="fit-map" class="quiet-button">Показать все</button></div>
<div id="map" aria-label="Карта или схема расположения заявок"></div><div id="map-status" class="map-status" role="status"></div><div id="route-legend" class="route-legend"></div></div>
<aside class="panel detail-panel" aria-label="Карточка выбранной заявки"><div class="panel-heading"><h2>Карточка заявки</h2><span class="badge">ДЕТАЛИ</span></div><div id="request-card" aria-live="polite"></div></aside>
</section>
<section class="panel schedule-panel" aria-labelledby="schedule-title"><div class="panel-heading"><div><h2 id="schedule-title">Расписание дня</h2><p class="muted">Выберите участок работы, чтобы открыть заявку.</p></div><div class="timeline-legend"><span class="travel-key">Переезд</span><span class="wait-key">Ожидание</span><span class="work-key">Работа</span></div></div><div id="schedule" tabindex="-1"></div></section>
''' + operations_history + '''
<section class="bottom-grid"><details class="panel"><summary>Утренняя загрузка оборудования</summary><div id="equipment" class="scroll"></div></details>
<details class="panel"><summary>Источники, ограничения и проверка</summary><div id="provenance" class="provenance"></div></details></section>
<footer>Просмотр готового плана · Выбор на карте, в списке и в расписании синхронизирован. Для изменения назначений нужен новый расчёт.</footer>
</main><noscript><p class="notice">Для интерактивного рабочего окна включите JavaScript. Табличный отчёт report.html доступен отдельно.</p></noscript>
<script id="dispatcher-data" type="application/json">''' + data + '''</script>
<script src="./assets/leaflet.js"></script><script>''' + js + '''</script></body></html>'''
