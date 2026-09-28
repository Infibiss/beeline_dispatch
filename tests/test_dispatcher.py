"""Rendering boundary and dependency-free dispatcher logic regressions."""
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


STATIC = Path(__file__).resolve().parents[1] / 'src/field_router/static'


def render(payload):
    assert importlib.util.find_spec('field_router.dispatcher') is not None, 'Dispatcher renderer is required'
    from field_router.dispatcher import render_dispatcher
    return render_dispatcher(payload)


def test_payload_cannot_break_out_of_json_script_and_roundtrips_unicode():
    payload = {'scenario': {'id': '</script><script>alert(1)</script>',
                           'region': 'Москва & область', 'requests': []},
               'plans': [], 'validation': {},
               'geography': {'locations': {'x': {'label': '<img onerror=alert(2)>'}}}}
    html = render(payload)
    assert '<html lang="ru">' in html
    assert '<script>alert(1)</script>' not in html
    assert '<img onerror=alert(2)>' not in html
    embedded = re.search(r'<script id="dispatcher-data" type="application/json">(.*?)</script>', html, re.S)
    assert embedded and json.loads(embedded.group(1)) == payload
    assert '<meta charset="utf-8">' in html


def test_local_assets_and_accessible_workbench_regions_exist_for_empty_data():
    html = render({'scenario': {}, 'plans': [], 'validation': {}})
    for marker in ['id="plan-select"', 'id="engineer-select"', 'id="request-search"',
                   'id="map"', 'id="schedule"', 'id="request-card"', 'id="request-list"',
                   'aria-live="polite"', '<noscript>', './assets/leaflet.js', './assets/leaflet.css']:
        assert marker in html
    assert 'https://unpkg.com' not in html
    assert 'innerHTML' not in html
    assert 'data-request-id' in html


def run_js(expression):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for client logic checks')
    script = STATIC / 'dispatcher.js'
    assert script.exists(), 'Dispatcher client is required'
    code = 'const core = require(' + json.dumps(str(script)) + ');' + expression
    return subprocess.run([node, '-e', code], capture_output=True, text=True, check=True)


def test_assignment_explanation_describes_verified_skill_and_route_order():
    result=run_js('''
const scenario={requests:[{id:'a',address:'Первый адрес',skill:'local',windows:[{start:600,end:650}]},
 {id:'b',address:'Второй адрес'}],engineers:[{id:'e1',name:'Иван',skills:['local'],transport:'car',shift:{start:540,end:1080}}]};
const plan={routes:[{engineer_id:'e1',visits:[{request_id:'a',arrival:590,start:600,end:630,travel_minutes:10,explanation:'Сохранённое <b>объяснение</b>'},
 {request_id:'b',arrival:640,start:650,end:680}]}]};
const record=core.buildRecords(scenario,plan)[0];
console.log(JSON.stringify({current:core.assignmentExplanation(record,scenario),
 stale:core.assignmentExplanation({...record,pendingReassignment:true},scenario)}));
''')
    payload=json.loads(result.stdout)
    assert payload['current']['stale'] is False
    assert payload['stale']['stale'] is True
    text=' '.join(payload['current']['lines'])
    assert 'Локальные работы' in text and 'Место 1 из 2' in text
    assert 'Второй адрес' in text and 'ожидание 10 мин' in text
    assert payload['current']['stored']=='Сохранённое <b>объяснение</b>'
    assert 'оптимального' in payload['current']['notice']


def test_assignment_explanation_does_not_invent_missing_qualifications():
    result=run_js("console.log(JSON.stringify(core.assignmentExplanation({request:{id:'a'},visit:{request_id:'a'},route:{visits:[{request_id:'a'}]}},{requests:[]}))); ")
    assert 'нужно подтвердить' in json.loads(result.stdout)['lines'][0]


def test_timeline_separates_travel_wait_and_service_in_order():
    result = run_js('console.log(JSON.stringify(core.timelineSegments({arrival:570,start:600,end:640,travel_minutes:20})));')
    assert json.loads(result.stdout) == [
        {'kind': 'travel', 'start': 550, 'end': 570},
        {'kind': 'wait', 'start': 570, 'end': 600},
        {'kind': 'work', 'start': 600, 'end': 640},
    ]


def test_request_index_retains_unassigned_and_filters_synchronously():
    result = run_js('''
const scenario={requests:[{id:'a',address:'Арбат'},{id:'b',address:'Тверская'}],engineers:[{id:'e',name:'Иван'}]};
const plan={routes:[{engineer_id:'e',visits:[{request_id:'a',start:600}]}],unassigned:[{request_id:'b',reason:'Нет времени'}]};
const records=core.buildRecords(scenario,plan);
console.log(JSON.stringify({assigned:records[0].engineer.id,reason:records[1].unassigned.reason,
 engineer:core.filterRecords(records,'e','').map(r=>r.request.id),
 search:core.filterRecords(records,'','ТВЕР').map(r=>r.request.id),
 unassigned:core.filterRecords(records,'__unassigned__','').map(r=>r.request.id)}));
''')
    assert json.loads(result.stdout) == {'assigned': 'e', 'reason': 'Нет времени',
                                         'engineer': ['a'], 'search': ['b'], 'unassigned': ['b']}


def test_only_finite_in_range_geo_coordinates_are_accepted():
    result = run_js('console.log(JSON.stringify([core.validLocation({lat:55.75,lon:37.62}),core.validLocation({lat:95,lon:37}),core.validLocation({lat:null,lon:0}),core.validLocation({lat:0,lon:180})]));')
    assert json.loads(result.stdout) == [True, False, False, True]


def test_pending_reassignment_is_actionable_but_retains_its_previous_visit():
    result = run_js(r'''
const scenario={requests:[{id:'a',priority:'urgent'},{id:'b'}],engineers:[{id:'e'}]};
const plan={routes:[{engineer_id:'e',visits:[{request_id:'a',start:600}]}],unassigned:[{request_id:'b'}]};
const ops={assignment_summary:{pending_request_ids:['a']}};
const records=core.buildRecords(scenario,plan,ops);
console.log(JSON.stringify({pending:records[0].pendingReassignment,oldStart:records[0].visit.start,
  attention:core.filterRecords(records,'__unassigned__','').map(r=>r.request.id),
  priority:core.prioritySummary(scenario,plan,ops)}));
''')
    assert json.loads(result.stdout) == {'pending': True, 'oldStart': 600,
        'attention': ['a', 'b'], 'priority': {'served': 0, 'total': 1}}


def test_report_and_region_links_escape_labels_and_reject_script_urls():
    html = render({'scenario': {}, 'plans': [], 'datasets': [
        {'region': '<img src=x>', 'url': '../north/dispatcher.html'},
        {'region': 'Unsafe', 'url': 'javascript:alert(1)'},
    ]})
    assert 'href="report.html"' in html
    assert 'href="../north/dispatcher.html"' in html
    assert '&lt;img src=x&gt;' in html
    assert 'href="javascript:' not in html


def test_malformed_route_geometry_falls_back_without_breaking_workbench():
    result = run_js('''
console.log(JSON.stringify([
 core.routeCoordinates({coordinates:{invalid:true}}),
 core.routeCoordinates({coordinates:[[37.6,55.7],null,[181,0],['37.6',55.7],[37.7,55.8]]}),
 core.routeCoordinates(null)
]));
''')
    assert json.loads(result.stdout) == [[], [[55.7, 37.6], [55.8, 37.7]], []]


def test_overlapping_map_points_select_nearest_center_not_paint_order():
    result = run_js('''
const points=[{id:'61540',x:431.1274,y:61.7635},{id:'77563',x:433.9274,y:67.9335}];
console.log(JSON.stringify([
 core.nearestPointId(points,431.1274,61.7635),
 core.nearestPointId([...points].reverse(),431.1274,61.7635),
 core.nearestPointId(points,434,68),
 core.nearestPointId([],0,0)
]));
''')
    assert json.loads(result.stdout) == ['61540', '61540', '77563', None]


def test_priority_labels_distinguish_emergency_and_connection():
    result = run_js('''
const scenario={requests:[{id:'emergency',priority:'urgent'},{id:'new',priority:'high'},{id:'normal',priority:'normal'}]};
const plan={routes:[{visits:[{request_id:'new'},{request_id:'normal'},{request_id:'new'}]}]};
console.log(JSON.stringify({labels:['urgent','high','normal'].map(core.priorityLabel),summary:core.prioritySummary(scenario,plan)}));
''')
    assert json.loads(result.stdout) == {
        'labels': ['Авария · максимальный приоритет', 'Подключение · после аварий', 'Обычный'],
        'summary': {'served': 1, 'total': 2},
    }
