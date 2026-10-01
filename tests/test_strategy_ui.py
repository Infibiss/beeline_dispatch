"""Day-strategy behavior through the real page and a small DOM boundary."""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

import pytest


STATIC = Path(__file__).resolve().parents[1] / 'src/field_router/static'


class Markup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tree = {'tag': 'document', 'attrs': {}, 'children': []}
        self.stack = [self.tree]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = {'tag': tag, 'attrs': dict(attrs), 'children': []}
        self.stack[-1]['children'].append(node)
        if tag not in {'meta', 'link', 'input', 'br', 'hr'}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if self.stack[-1]['tag'] == tag:
            self.stack.pop()

    def handle_data(self, data):
        self.stack[-1]['children'].append(data)


HARNESS = r'''
const assert = require('node:assert/strict');
class Element {
  constructor(tag) {
    this.tagName=tag.toUpperCase(); this.children=[]; this.dataset={}; this.events={};
    this.attributes={}; this._text=''; this._value=null; this.hidden=false; this.disabled=false; this.open=false;
    this.classList={
      contains:name=>(this.className||'').split(' ').includes(name),
      toggle:(name,on)=>{const names=new Set((this.className||'').split(' ').filter(Boolean)); if(on) names.add(name); else names.delete(name); this.className=[...names].join(' ');}
    };
  }
  append(...items) { for(const item of items) {item.parentNode=this; this.children.push(item);} }
  replaceChildren(...items) {this.children=[]; this._text=''; this.append(...items);}
  set textContent(value) {this._text=String(value); this.children=[];}
  get textContent() {return this._text+this.children.map(x=>x.textContent).join('');}
  set value(value) {this._value=String(value);}
  get value() {return this._value ?? (this.tagName==='SELECT' ? this.options.find(x=>x.selected)?.value ?? this.options[0]?.value ?? '' : '');}
  get options() {return this.children.filter(x=>x.tagName==='OPTION');}
  get selectedOptions() {return this.options.filter(x=>x.value===this.value);}
  setAttribute(key,value) {this.attributes[key]=String(value);}
  getAttribute(key) {return this.attributes[key] ?? null;}
  addEventListener(type,handler) {(this.events[type] ||= []).push(handler);}
  async dispatch(type) {const event={target:this,preventDefault(){}}; for(let n=this;n;n=n.parentNode) for(const handler of n.events[type]||[]) await handler(event);}
  matches(selector) {
    if(selector.includes(' ')) {const parts=selector.split(' '); return this.matches(parts.pop()) && !!this.parentNode?.closest(parts.join(' '));}
    if(selector.startsWith('.')) return this.classList.contains(selector.slice(1));
    if(selector.startsWith('#')) return this.id===selector.slice(1);
    const attr=selector.match(/^(\w+)\[([^=]+)="([^"]+)"\]$/);
    if(attr) return this.tagName===attr[1].toUpperCase() && this[attr[2]]===attr[3];
    return this.tagName===selector.toUpperCase();
  }
  closest(selector) {for(let n=this;n;n=n.parentNode) if(n.matches(selector)) return n; return null;}
  querySelectorAll(selector) {return this.children.flatMap(x=>[...(x.matches(selector)?[x]:[]),...x.querySelectorAll(selector)]);}
  querySelector(selector) {return this.querySelectorAll(selector)[0]||null;}
  focus() {}
  scrollIntoView() {}
  requestSubmit() {return this.dispatch('submit');}
}
const ids={};
function build(item) {
  if(typeof item==='string') return Object.assign(new Element('#text'),{textContent:item});
  const el=new Element(item.tag);
  for(const [key,value] of Object.entries(item.attrs)) {
    if(['hidden','disabled','required','selected','checked'].includes(key)) el[key]=true;
    else if(key==='class') el.className=value;
    else el[key]=value;
  }
  if(el.id) ids[el.id]=el;
  el.append(...item.children.map(build)); return el;
}
const root=build(markup);
global.document={getElementById:id=>ids[id]||null,createElement:tag=>new Element(tag),querySelector:s=>root.querySelector(s),querySelectorAll:s=>root.querySelectorAll(s)};
global.location={href:'http://localhost/regions/south/'};
const windowEvents=new EventTarget();
// Browser API boundary, including CI Node versions without global CustomEvent.
global.CustomEvent=class extends Event {constructor(type,options={}){super(type,options);this.detail=options.detail;}};
global.window={addEventListener:windowEvents.addEventListener.bind(windowEvents),dispatchEvent:windowEvents.dispatchEvent.bind(windowEvents)};
global.requestAnimationFrame=fn=>fn();
const nativeInterval=global.setInterval;
global.setInterval=(...args)=>nativeInterval(...args).unref();
global.crypto=require('node:crypto').webcrypto;
const state={version:7,time:600,region:'Юг',requests:[],engineers:[],locations:[],plan:{routes:[]},execution_mode:'simulation'};
function candidate(id, facts={}) {
  return {id,duplicate_of:null,strategy_ids:[id],facts:{assigned:4,unassigned:0,emergency_assigned:1,emergency_total:1,connection_assigned:2,connection_total:2,lost_assignments:0,reassigned:1,avoidable_reassigned:1,shifted:1,time_shift_minutes:20,travel_minutes:120,distance_m:10000,engineers_used:2,priority_start_sum:660,response_target_count:1,response_unassigned:0,...facts},diff:[],explanation:'Проверенное объяснение',emergency:{assigned:true,engineer_id:'e1',arrival:650,start:660,end:720,response_minutes:60}};
}
const preview={id:'batch-1',version:7,event:{type:'emergency',time:600},llm_enabled:true,recommended_candidate_id:'stable',candidates:[candidate('sla',{reassigned:3,avoidable_reassigned:2,time_shift_minutes:80,travel_minutes:150,distance_m:9000}),candidate('stable'),candidate('cost',{reassigned:2,time_shift_minutes:50,travel_minutes:90,distance_m:12000})]};
preview.candidates[0].emergency={...preview.candidates[0].emergency,arrival:620,start:630,response_minutes:30};
const ai={candidate_id:'sla',notice:'Показатели проверены.',text:'Авария начнётся раньше.'};
const requests=[];
global.fetch=async(url,options={})=>{
  requests.push({url,payload:options.body?JSON.parse(options.body):null});
  return {ok:true,json:async()=>url.endsWith('/regions')?{regions:[],current:'south'}:url.endsWith('/preview')?preview:url.endsWith('/recommend')?ai:state};
};
const $=id=>ids[id];
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const submit=async()=>{await $('event-form').dispatch('submit'); await settle(); assert.equal($('error').hidden,true,$('error').textContent);};
const cards=()=>$('candidates').querySelectorAll('.candidate');
const row=label=>$('comparison-table').querySelectorAll('tr').find(r=>r.children[0].textContent===label)?.children.slice(1).map(c=>c.textContent);
'''


def client(expression, before=''):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for client checks')
    markup = Markup((STATIC / 'scenarios.html').read_text(encoding='utf-8')).tree
    script = 'const markup=' + json.dumps(markup) + ';' + HARNESS + before
    script += '\nrequire(' + json.dumps(str(STATIC / 'scenarios.js')) + ');'
    script += '\n(async()=>{await settle();' + expression + '})().catch(error=>{console.error(error);process.exitCode=1;});'
    # Keep the full-page DOM harness off the Windows command line (32K limit).
    result = subprocess.run([node, '-'], input=script, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_unique_plans_show_stable_first_and_comparison_deltas_from_facts():
    client('''
await submit();
assert.deepEqual(cards().map(c=>c.dataset.id),['stable','sla','cost']);
assert.deepEqual(cards().map(c=>c.querySelector('h3').textContent),['Меньше изменений','Быстрее к аварии','Меньше дороги']);
assert.deepEqual(row('До начала новой аварии, мин'),['60','30 (−30)','60 (0)']);
assert.deepEqual(row('Прибытие к новой аварии'),['10:50','10:20','10:50']);
assert.deepEqual(row('Дорога, мин'),['120','150 (+30)','90 (−30)']);
assert.deepEqual(row('Расстояние, км'),['10.0','9.0 (−1.0)','12.0 (+2.0)']);
assert.deepEqual(row('Смена исполнителя'),['1','3 (+2)','2 (+1)']);
assert.deepEqual(row('Сдвиги начала, мин'),['20','80 (+60)','50 (+30)']);
assert.deepEqual(row('Из них предотвратимые'),['1','2','1']);
assert.ok($('preview-coverage').textContent.includes('Действующих назначений сейчас: 0/0. Лучший рассчитанный охват: 4/4'));
assert.match($('comparison-metrics').textContent,/относительно «Меньше изменений»/);
assert.equal($('approve').disabled,true,'Recommendation does not approve or select');
''')


def test_candidate_cards_show_decision_metrics_before_secondary_details():
    client('''
await submit();
const card=cards()[0];
assert.deepEqual(card.querySelectorAll('dl')[0].querySelectorAll('dt').map(item=>item.textContent),
  ['Без назначения','Смена исполнителя','Дорога, мин']);
const extra=card.querySelectorAll('details').find(item=>item.querySelector('summary')?.textContent==='Ещё показатели');
assert.ok(extra);
assert.equal(extra.open,false);
assert.deepEqual(extra.querySelectorAll('dt').map(item=>item.textContent),
  ['Из них предотвратимые','Сдвиги начала, мин','Расстояние, км']);
''')


def test_three_identical_profiles_render_one_card_and_one_metric_column():
    client('''
await submit();
assert.deepEqual(cards().map(c=>c.dataset.id),['stable']);
assert.equal($('candidates').dataset.count,'1');
assert.match(cards()[0].textContent,/Совпадает с «Быстрее к аварии», «Меньше дороги»/);
assert.match($('variants-note').textContent,/Один уникальный план/);
assert.equal($('comparison-table').querySelectorAll('tr')[0].children.length,2);
assert.equal($('comparison-tools').hidden,true);
assert.equal($('ai-panel').hidden,true,'No recommendation before an explicit AI request');
assert.equal($('ai-recommend').hidden,true);
assert.equal(cards()[0].classList.contains('recommended'),false);
''', before='''
preview.llm_enabled=false;
preview.candidates[1].strategy_ids=['stable','sla','cost'];
for(const c of preview.candidates) if(c.id!=='stable') c.duplicate_of='stable';
''')


def test_structured_ai_card_is_opt_in_and_renders_safe_sections():
    client('''
await submit();
assert.equal(cards().some(c=>c.classList.contains('recommended')),false);
await $('ai-recommend').dispatch('click');
const result=$('ai-result');
assert.equal(result.querySelectorAll('h4').length,1);
assert.match(result.textContent,/Компромисс/);
assert.match(result.textContent,/<script>bad/);
assert.equal(result.querySelectorAll('script').length,0);
assert.equal(result.querySelectorAll('details').length,0);
''', before='''
ai.card={label:'Быстрее к аварии',summary:'Все заявки назначены.',reasons:['<script>bad</script>'],tradeoffs:['Дороги больше.'],attention:['Потеря назначения.'],note:'Оценка модели.'};
''')


def test_autopilot_card_is_visible_without_llm_or_decision_rows():
    client('''
const result=$('application-visual-explanation');
assert.match(result.textContent,/Применён вариант «Меньше изменений»/);
assert.match(result.textContent,/Останутся без исполнителя/);
assert.equal(result.querySelectorAll('details').length,0);
''', before='''
state.last_applied={version:7,automatic:true,incoming_count:0,changed_count:0};
state.dispatch_notifications=[{id:'a',kind:'auto_applied',version:7,explanation_card:{label:'Меньше изменений',summary:'План сохранён.',reasons:[],tradeoffs:[],attention:['Останутся без исполнителя: 2.']}}];
''')


def test_alias_recommendation_highlights_canonical_card_and_preserves_apply_guard():
    client('''
await submit();
assert.deepEqual(cards().map(c=>c.dataset.id),['stable','cost']);
assert.equal($('candidates').dataset.count,'2');
assert.equal(cards()[1].classList.contains('recommended'),false);
await $('ai-recommend').dispatch('click');
assert.match($('ai-result').textContent,/Рекомендуем «Быстрее к аварии»/);
assert.equal(cards()[0].classList.contains('recommended'),true);
assert.equal(cards()[1].classList.contains('recommended'),false);
assert.equal($('approve').disabled,true);
await $('approve').dispatch('click');
assert.equal(requests.filter(r=>r.url.endsWith('/approve')).length,0);
await cards()[0].querySelector('button').dispatch('click');
assert.equal($('approve').disabled,false);
$('candidate-sort').value='distance'; await $('candidate-sort').dispatch('change');
assert.match($('ai-result').textContent,/Рекомендуем «Быстрее к аварии»/);
assert.equal(cards().find(c=>c.dataset.id==='stable').classList.contains('recommended'),true);
await $('approve').dispatch('click');
assert.deepEqual(requests.find(r=>r.url.endsWith('/approve')).payload,{version:7,batch_id:'batch-1',candidate_id:'stable'});
''', before="preview.recommended_candidate_id='cost'; preview.candidates[0].duplicate_of='stable'; preview.candidates[1].strategy_ids=['stable','sla'];")


def test_road_sort_uses_minutes_before_distance():
    client('''
await submit();
$('candidate-sort').value='distance'; await $('candidate-sort').dispatch('change');
assert.deepEqual(cards().map(c=>c.dataset.id),['cost','stable','sla']);
assert.equal($('candidate-sort').options.find(o=>o.value==='distance').textContent,'Дорога');
''')


def test_risk_sort_keeps_diagnostics_out_of_operational_cards():
    client('''
await submit();
$('candidate-sort').value='risk'; await $('candidate-sort').dispatch('change');
assert.deepEqual(cards().map(c=>c.dataset.id),['cost','sla','stable']);
assert.deepEqual(row('P(SLA), %'),['8.0 (−12.0)','12.0 (−8.0)','20.0']);
assert.deepEqual(row('Ожидаемые потери, усл. ед.'),['1.20 (−1.80)','1.80 (−1.20)','3.00']);
assert.deepEqual(row('Клиентов с изменённым обещанием'),['1 (−2)','2 (−1)','3']);
assert.doesNotMatch(cards()[0].textContent,/Risk Engine|усл. ед.|Ожидаемо под SLA/);
assert.doesNotMatch(cards()[0].textContent,/калибр/);
assert.doesNotMatch($('variants-note').textContent,/Монте-Карло/);
assert.doesNotMatch($('variants-note').textContent,/оценка, а не гарантия/);
''', before='''
preview.risk_model={version:'risk-engine-v1.1',iterations:600,calibrated:false};
const values={stable:[.20,3,3,.4,.25],sla:[.12,1.8,2,.3,.18],cost:[.08,1.2,1,.2,.11]};
for(const c of preview.candidates){const [p,loss,affected,late,overtime]=values[c.id];c.risk={status:'ready',iterations:600,affected_clients:affected,summary:{p_sla_breach:p,expected_loss:loss,expected_affected_customers:affected/2,p_late_start:late,p_overtime:overtime}};}
''')


def test_no_remaining_emergencies_explains_matching_response_profile():
    client('''
await submit();
assert.deepEqual(cards().map(c=>c.dataset.id),['stable','cost']);
assert.match($('variants-note').textContent,/Аварий в остатке дня нет/);
assert.match(cards()[0].textContent,/Быстрее к аварии/);
assert.equal(row('До начала новой аварии, мин'),undefined);
assert.equal(row('Среднее начало оставшихся аварий'),undefined);
''', before='''
preview.event.type='replan'; preview.candidates[0].duplicate_of='stable';
preview.candidates[1].strategy_ids=['stable','sla'];
for(const c of preview.candidates) {c.emergency=null;c.facts.emergency_assigned=0;c.facts.emergency_total=0;c.facts.response_target_count=0;c.facts.response_unassigned=0;c.facts.priority_start_sum=0;}
''')


def test_ordinary_event_displays_average_start_of_assigned_remaining_emergencies():
    client('''
await submit();
assert.deepEqual(row('Среднее начало оставшихся аварий'),['11:00','10:21','—']);
assert.equal(row('До начала новой аварии, мин'),undefined);
assert.equal(row('Сумма начал аварий, мин от 00:00'),undefined);
''', before='''
preview.event.type='replan';
for(const c of preview.candidates) {c.emergency=null;c.facts.response_target_count=3;c.facts.response_unassigned=1;}
preview.candidates[0].facts.priority_start_sum=1241;
preview.candidates[1].facts.priority_start_sum=1320;
preview.candidates[2].facts.priority_start_sum=0;
preview.candidates[2].facts.response_unassigned=3;
''')


def test_unassigned_emergency_does_not_invent_response_delta():
    client('''
await submit();
assert.deepEqual(row('До начала новой аварии, мин'),['Не назначена','30','60']);
assert.deepEqual(row('Прибытие к новой аварии'),['Не назначена','10:20','10:50']);
''', before='''
preview.candidates[1].emergency={assigned:false,arrival:null,start:null,response_minutes:null};
preview.candidates[1].facts.response_unassigned=1;
''')


def test_editing_event_clears_recommendation_and_selected_plan():
    client('''
await submit(); await cards()[0].querySelector('button').dispatch('click');
assert.equal($('approve').disabled,false);
await $('event-time').dispatch('input');
assert.equal($('ai-panel').hidden,true);
assert.equal($('approve').disabled,true);
assert.equal($('candidates').dataset.count,'0');
assert.equal($('variants-note').textContent,'');
await $('approve').dispatch('click');
assert.equal(requests.filter(r=>r.url.endsWith('/approve')).length,0);
''')


def test_risk_advisory_runs_preview_only_and_rejects_stale_version():
    client('''
const previewCount=()=>requests.filter(r=>r.url.endsWith('/preview')).length;
const before=previewCount();
window.dispatchEvent(new CustomEvent('risk-intent',{detail:{action:'preview',planVersion:7,riskId:'sla',advisoryId:'risk-sla'}}));
await settle(); await settle();
assert.equal(previewCount(),before+1,'Explicit advisory click starts one preview');
assert.equal(requests.filter(r=>r.url.endsWith('/approve')).length,0,'Advisory must never approve');
assert.equal($('panel-events').hidden,false);
assert.equal($('event-type').value,'replan');
assert.equal($('approve').disabled,true,'Dispatcher must select a candidate before approval');
window.dispatchEvent(new CustomEvent('risk-intent',{detail:{action:'preview',planVersion:6,riskId:'late'}}));
await settle();
assert.equal(previewCount(),before+1,'Stale advisory must not start another preview');
assert.equal($('error').hidden,false);
''')


def test_gps_replan_does_not_inherit_previous_risk_local_scope():
    client("""
window.dispatchEvent(new CustomEvent('risk-intent',{detail:{action:'preview',planVersion:7,engineerId:'e1'}}));
await settle();await settle();
assert.deepEqual(requests.filter(r=>r.url.endsWith('/preview')).at(-1).payload.local_scope_engineer_ids,['e1']);
window.dispatchEvent(new CustomEvent('gps-intent',{detail:{action:'replan'}}));
await submit();
assert.equal(requests.filter(r=>r.url.endsWith('/preview')).at(-1).payload.local_scope_engineer_ids,undefined);
""")


def test_incoming_history_and_unassigned_pool_are_separate_and_actionable():
    client('''
assert.match($('incoming-summary').textContent,/Ожидают распределения 1 · назначены 1 · без исполнителя 0/);
assert.match($('incoming-list').textContent,/new-1/);
assert.doesNotMatch($('incoming-list').textContent,/done-1/);
assert.match($('incoming-history-list').textContent,/done-1/);
assert.ok($('incoming-section').getAttribute('open')!==undefined);
assert.equal($('manual-event-panel').open,false);
assert.equal($('replan-primary').disabled,false);
assert.match($('replanning-pool-summary').textContent,/Требуют назначения · 2/);
assert.match($('replanning-pool').textContent,/old-1/);
assert.match($('replanning-pool').textContent,/old-2/);
assert.match($('replan-overview').textContent,/Новых: 1 · требуют назначения: 2/);
await $('replan-primary').dispatch('click'); await settle();
assert.equal(requests.filter(r=>r.url.endsWith('/preview')).at(-1).payload.event.type,'replan');
''', before='''
Object.assign(state,{
  request_labels:{'old-1':'old-1','old-2':'old-2'},
  incoming_requests:[
    {id:'new-1',status:'new',address:'Москва, дом 1',event:{type:'new_request',time:600,window_start:660,window_end:720,duration_minutes:20}},
    {id:'done-1',status:'assigned',address:'Москва, дом 2',event:{type:'new_request',time:590,window_start:660,window_end:720,duration_minutes:20}}
  ],
  replanning_pool:[
    {request_id:'old-1',address:'Москва, дом 3',status:'awaiting_replan',reason:'Инженер недоступен'},
    {request_id:'old-2',address:'Москва, дом 4',status:'unassigned',reason:'Не включена в найденный план'}
  ]
});
''')


def test_unassigned_only_still_offers_manual_retry():
    client('''
assert.match($('incoming-summary').textContent,/Ожидают распределения 0/);
assert.equal($('replan-primary').disabled,false);
assert.equal($('replan-primary').textContent,'Повторить расчёт');
assert.match($('plan-context').textContent,/Без исполнителя: 1/);
''', before='''
state.replanning_pool=[{request_id:'old-2',address:'Москва, дом 4',status:'unassigned',reason:'Не включена в найденный план'}];
''')


def test_current_header_and_pool_count_invalidated_assignments_as_uncovered():
    client('''
assert.equal($('coverage').textContent,'52 / 66');
assert.equal($('coverage-pending').textContent,'14');
assert.equal($('replanning-pool-summary').textContent,'Требуют назначения · 14');
assert.match($('replanning-pool').textContent,/Нужно заменить исполнителя · 14/);
assert.doesNotMatch($('replanning-pool-summary').textContent,/исполнителя · 0/);
''', before='''
state.requests=Array.from({length:66},(_,i)=>({id:'job-'+i,address:'Адрес '+i}));
state.plan={routes:[{engineer_id:'e1',visits:state.requests.map(item=>({request_id:item.id}))}]};
state.replanning_pool=state.requests.slice(0,14).map(item=>({request_id:item.id,status:'awaiting_replan',reason:'Инженер недоступен'}));
state.assignment_summary={assigned:52,total:66,requires_assignment:14};
''')


def test_repeat_preview_does_not_claim_unassigned_jobs_disappear():
    client('''
await $('replan-primary').dispatch('click');await settle();
assert.ok($('preview-coverage').textContent.includes('Лучший рассчитанный охват: 54/67'));
assert.match($('variants-note').textContent,/Ни один вариант не уменьшил число заявок без исполнителя/);
assert.match($('replan-guidance').textContent,/Остались заявки без исполнителя/);
''', before='''
state.plan.unassigned=Array.from({length:13},(_,i)=>({request_id:'old-'+i}));
state.replanning_pool=state.plan.unassigned.map(item=>({request_id:item.request_id,status:'unassigned',reason:'Не включена в найденный план'}));
for(const c of preview.candidates)Object.assign(c.facts,{assigned:54,unassigned:13});
preview.event={type:'replan',time:600};preview.incoming_ids=[];
''')


def test_planning_mode_is_visible_and_can_be_switched_both_ways():
    client('''
assert.equal($('planning-mode').value,'autopilot');
assert.equal($('autopilot-thresholds').hidden,false);
assert.match($('planning-mode-summary').textContent,/Автопилот: новые заявки/);
assert.match($('planning-mode-summary').textContent,/задержки по GPS/);
$('planning-mode').value='manual';await $('planning-mode').dispatch('change');
assert.equal($('autopilot-thresholds').hidden,true);
assert.equal($('planning-save').disabled,false);
await $('planning-save').dispatch('click');await settle();
assert.deepEqual(requests.filter(r=>r.url.endsWith('/inbox/settings')).at(-1).payload,
  {version:7,mode:'manual',every_n_requests:3,every_n_minutes:null});
''', before="state.planning_settings={mode:'autopilot',every_n_requests:3,every_n_minutes:null};")
    client('''
$('planning-mode').value='autopilot';await $('planning-mode').dispatch('change');
assert.equal($('autopilot-thresholds').hidden,false);
assert.equal($('planning-save').disabled,true,'Autopilot needs a threshold');
$('auto-count').value='5';await $('auto-count').dispatch('input');
assert.equal($('planning-save').disabled,false);
await $('planning-save').dispatch('click');await settle();
assert.deepEqual(requests.filter(r=>r.url.endsWith('/inbox/settings')).at(-1).payload,
  {version:7,mode:'autopilot',every_n_requests:5,every_n_minutes:null});
''', before="state.planning_settings={mode:'manual',every_n_requests:null,every_n_minutes:null};")


def test_stream_demo_is_isolated_and_arrival_uses_json_intake():
    client('''
assert.equal($('stream-demo-panel').hidden,false);
assert.equal($('stream-demo-start').disabled,false);
await $('stream-demo-start').dispatch('click');await settle();await settle();
const posted=requests.filter(item=>item.url.endsWith('/api/inbox/requests'));
assert.equal(posted.length,1);
assert.equal(posted[0].payload.request.id,'demo-stream-01');
assert.match($('stream-demo-status').textContent,/Поток завершён: поступило 1 из 1/);
''', before='''
state.demo_stream_enabled=true;state.incoming_requests=[];
const originalFetch=global.fetch;
global.fetch=async(url,options={})=>{
  if(url.endsWith('/api/demo-stream/requests'))return {ok:true,json:async()=>({requests:[{id:'demo-stream-01',time:'10:00'}]})};
  if(url.endsWith('/api/inbox/requests')){
    const row=JSON.parse(options.body).request;
    state.version++;state.incoming_requests.push({id:row.id,status:'new',address:'Москва, дом 1',event:{type:'new_request',time:600,duration_minutes:20,window_start:660,window_end:720}});
    requests.push({url,payload:JSON.parse(options.body)});
    return {ok:true,json:async()=>({added:1,state})};
  }
  if(url.endsWith('/api/inbox'))return {ok:true,json:async()=>state};
  return originalFetch(url,options);
};
''')


def test_stream_demo_sends_one_request_per_ten_second_tick():
    client('''
await $('stream-demo-start').dispatch('click');await settle();await settle();
for(let count=1;count<=6;count++){
  assert.equal(requests.filter(item=>item.url.endsWith('/api/inbox/requests')).length,count);
  if(count===6)break;
  const timer=scheduled.shift();assert.equal(timer.ms,10000);
  await timer.fn();await settle();
}
assert.equal(scheduled.length,0);
assert.equal($('stream-demo-start').disabled,true);
assert.match($('stream-demo-status').textContent,/Поток завершён: поступило 6 из 6/);
''', before='''
state.demo_stream_enabled=true;state.time=540;state.incoming_requests=[];
const scheduled=[];
global.setTimeout=(fn,ms)=>{scheduled.push({fn,ms});return scheduled.length;};
const originalFetch=global.fetch;
global.fetch=async(url,options={})=>{
  if(url.endsWith('/api/demo-stream/requests'))return {ok:true,json:async()=>({requests:Array.from({length:6},(_,i)=>({id:'demo-stream-'+String(i+1).padStart(2,'0'),time:'09:10'}))})};
  if(url.endsWith('/api/inbox/requests')){
    const row=JSON.parse(options.body).request;
    state.version++;state.incoming_requests.push({id:row.id,status:'new',address:'Москва, дом 1',event:{type:'new_request',time:550,duration_minutes:20,window_start:660,window_end:720}});
    requests.push({url,payload:JSON.parse(options.body)});
    return {ok:true,json:async()=>({added:1,state})};
  }
  if(url.endsWith('/api/inbox'))return {ok:true,json:async()=>state};
  return originalFetch(url,options);
};
''')


DEMO_RESET_FIXTURE = '''
const fixtureRows=[{id:'demo-stream-01',time:'09:10'},{id:'demo-stream-02',time:'09:10'}];
const firstIncoming={id:'demo-stream-01',status:'assigned',address:'Москва',event:{type:'new_request',time:550,duration_minutes:20,window_start:660,window_end:720}};
Object.assign(state,{demo_stream_enabled:true,demo_reset_enabled:true,demo_run_id:'run-a',time:550,
  incoming_requests:[firstIncoming,{...firstIncoming,id:'demo-stream-02'}],
  dispatch_notifications:[{id:'old-notice',kind:'auto_applied',version:7,title:'Старое событие',text:'Старый план',time:'2026-09-27T00:10:00Z'}],
  planning_settings:{mode:'autopilot',every_n_requests:3,every_n_minutes:null}});
let live=structuredClone(state), pendingIntake=null, deferIntake=false;
const cleanState=()=>({...structuredClone(state),version:1,time:540,demo_run_id:'run-b',incoming_requests:[],
  dispatch_notifications:[],decision_log:[],last_applied:null,versions:[{version:1,time:540,label:'Исходный план'}],
  planning_settings:{mode:'manual',every_n_requests:3,every_n_minutes:null}});
const scheduled=[];let poll;
global.setTimeout=(fn,ms)=>{const timer={fn,ms};scheduled.push(timer);return timer;};
global.clearTimeout=timer=>{const index=scheduled.indexOf(timer);if(index>=0)scheduled.splice(index,1);};
global.setInterval=fn=>{poll=fn;return 1;};
const originalFetch=global.fetch;
const response=data=>({ok:true,json:async()=>structuredClone(data)});
global.fetch=async(url,options={})=>{
  const payload=options.body?JSON.parse(options.body):null;
  if(url.endsWith('/api/demo-stream/requests'))return response({requests:fixtureRows,demo_run_id:live.demo_run_id});
  if(url.endsWith('/api/demo-stream/reset')){requests.push({url,payload});live=cleanState();return response(live);}
  if(url.endsWith('/api/inbox/requests')){
    requests.push({url,payload});
    if(deferIntake)return new Promise(resolve=>{pendingIntake=()=>resolve(response({added:1,state}));});
    live.incoming_requests.push({...firstIncoming,id:payload.request.id,status:'new'});live.version++;
    return response({added:1,state:live});
  }
  if(['/api/state','/api/inbox','/api/revision'].some(path=>url.endsWith(path)))return response(live);
  return originalFetch(url,options);
};
'''


def test_completed_demo_can_reset_to_clean_day_and_start_same_requests_again():
    client('''
assert.equal($('stream-demo-start').hidden,true);
assert.equal($('stream-demo-reset').hidden,false);
assert.match($('dispatch-notifications').textContent,/Старое событие/);
$('decision-search').value='старое';$('decision-filter').value='unassigned';
await $('stream-demo-reset').dispatch('click');await settle();
assert.deepEqual(requests.find(r=>r.url.endsWith('/reset')).payload,{demo_run_id:'run-a',version:7});
assert.equal($('version').textContent,'1');assert.equal($('clock').textContent,'09:00');
assert.equal($('planning-mode').value,'manual');
assert.equal($('decision-search').value,'');assert.equal($('decision-filter').value,'all');
assert.doesNotMatch($('dispatch-notifications').textContent,/Старое событие/);
assert.equal($('plan-version').options.length,1);
assert.equal($('stream-demo-start').hidden,false);
assert.match($('stream-demo-status').textContent,/Поступило 0 из 2/);
assert.match($('plan-frame').src,/demo_run_id=run-b/);
await $('stream-demo-start').dispatch('click');await settle();await settle();
const intake=requests.find(r=>r.url.endsWith('/api/inbox/requests'));
assert.equal(intake.payload.request.id,'demo-stream-01');assert.equal(intake.payload.demo_run_id,'run-b');
assert.match($('stream-demo-status').textContent,/Поступило 1 из 2/);
''', before=DEMO_RESET_FIXTURE)


def test_reset_discards_inflight_arrival_and_does_not_resurrect_old_timer_or_history():
    client('''
await $('stream-demo-start').dispatch('click');await settle();
assert.ok(pendingIntake,'The old intake request is in flight');
await $('stream-demo-reset').dispatch('click');await settle();
pendingIntake();await settle();await settle();
assert.equal($('version').textContent,'1');
assert.match($('stream-demo-status').textContent,/Поступило 0 из 2/);
assert.doesNotMatch($('dispatch-notifications').textContent,/Старое событие/);
assert.equal(scheduled.length,0,'An old callback cannot schedule another arrival');
assert.equal($('stream-demo-start').disabled,false);
''', before=DEMO_RESET_FIXTURE+'''
live.incoming_requests=[];deferIntake=true;
''')


def test_poll_detects_new_demo_generation_when_version_number_is_reused():
    client('''
assert.equal($('version').textContent,'7');
live={...cleanState(),version:7};
await poll();await settle();
assert.equal($('version').textContent,'7');
assert.equal($('clock').textContent,'09:00');
assert.doesNotMatch($('dispatch-notifications').textContent,/Старое событие/);
assert.match($('plan-frame').src,/demo_run_id=run-b/);
assert.match($('stream-demo-status').textContent,/Поступило 0 из 2/);
''', before=DEMO_RESET_FIXTURE)


def test_autopilot_poll_updates_whole_plan_while_preserving_unsaved_mode_choice():
    client('''
$('planning-mode').value='manual';await $('planning-mode').dispatch('change');
assert.equal($('planning-save').disabled,false);
live.version=8;live.last_applied={automatic:true,version:8,incoming_count:2,changed_count:1};
live.dispatch_notifications=[{id:'new-notice',kind:'auto_applied',version:8,title:'Новый план применён',text:'Текущий результат',time:'2026-09-27T00:11:00Z'}];
await poll();await settle();
assert.equal($('version').textContent,'8');
assert.match($('plan-frame').src,/version=8/);
assert.match($('dispatch-notifications').textContent,/Новый план применён/);
assert.doesNotMatch($('dispatch-notifications').textContent,/Старое событие/);
assert.equal($('planning-mode').value,'manual','Unsaved mode choice survives applied plan refresh');
assert.equal($('planning-save').disabled,false);
live.dispatch_notifications[0].text='Дополнено объяснением';
await poll();await settle();
assert.match($('dispatch-notifications').textContent,/Дополнено объяснением/);
assert.equal($('version').textContent,'8');
''', before=DEMO_RESET_FIXTURE)


def test_cancelled_incoming_job_is_history_not_unassigned_or_lost_assignment():
    client('''
assert.match($('incoming-summary').textContent,/назначены 0 · без исполнителя 0 · отменены 1/);
assert.match($('incoming-history-summary').textContent,/История обработанных · 1/);
assert.match($('incoming-history-list').textContent,/Отменена/);
const card=$('application-visual-changes').querySelector('.application-change');
assert.match(card.textContent,/Отменена/);assert.match(card.textContent,/Удалена из плана/);
assert.doesNotMatch(card.textContent,/Без исполнителя|Потеря назначения/);
assert.equal(card.classList.contains('attention'),false);
assert.match($('dispatch-notifications').textContent,/Отменено и удалено из плана: 1/);
assert.match($('decision-log').textContent,/решение плана №7: отменена/);
$('decision-filter').value='unassigned';await $('decision-filter').dispatch('change');
assert.equal($('decision-log').querySelectorAll('.decision-card').length,0);
$('decision-filter').value='cancelled';await $('decision-filter').dispatch('change');
assert.equal($('decision-log').querySelectorAll('.decision-card').length,1);
assert.match($('decision-log').textContent,/заявка отменена и удалена из плана/);
''', before='''
state.incoming_requests=[{id:'client-1',status:'cancelled',address:'Москва',event:{type:'new_request',time:550,duration_minutes:20,window_start:660,window_end:720}}];
state.last_applied={version:7,automatic:false,incoming_count:0,changed_count:1};
state.dispatch_notifications=[{id:'cancelled-notice',kind:'manual_applied',version:7,title:'Заявка отменена',text:'Удалена из плана',time:'2026-09-27T00:10:00Z',cancelled_request_ids:['request:client-1'],unassigned_request_ids:[]}];
state.decision_log=[{id:'client-1',request_id:'request:client-1',version:7,status:'cancelled',change_type:'cancelled',previous_engineer_id:'e1',previous_start:660,engineer_id:null,start:null,explanation:'Отмена подтверждена диспетчером.'}];
''')


def test_notifications_distinguish_historical_decision_from_current_plan():
    client('''
assert.match($('notifications-overview').textContent,/без исполнителя 1/);
assert.match($('dispatch-notifications').textContent,/После этого решения без исполнителя: 1/);
assert.match($('decision-log').textContent,/В текущем плане статус изменился/);
$('decision-filter').value='assigned';await $('decision-filter').dispatch('change');
assert.match($('decision-log').textContent,/По этому фильтру решений нет/);
$('decision-filter').value='unassigned';await $('decision-filter').dispatch('change');
assert.match($('decision-log').textContent,/client-1/);
await $('notifications-replan').dispatch('click');
assert.equal($('replanning-pool-section').open,true);
assert.equal($('tab-events').attributes['aria-selected'],'true');
''', before='''
state.plan={routes:[],unassigned:[{request_id:'request:client-1',reason:'Не включена в план'}]};
state.replanning_pool=[{request_id:'request:client-1',address:'Москва, дом 1',status:'unassigned',reason:'Не включена в план'}];
state.dispatch_notifications=[{id:'n1',kind:'manual_applied',version:7,time:'2026-09-27T00:10:00Z',title:'План пересчитан',text:'Часть заявок без исполнителя',unassigned_request_ids:['request:client-1']}];
state.decision_log=[{id:'client-1',request_id:'request:client-1',address:'Москва, дом 1',status:'assigned',automatic:true,version:6,explanation:'Тогда была назначена.'}];
''')
    client('''
assert.match($('decision-log').textContent,/Текущий план №7: e2, начало 11:00/);
assert.match($('decision-log').textContent,/Объяснение ниже относится к прежнему плану №6/);
''', before='''
state.plan={routes:[{engineer_id:'e2',visits:[{request_id:'request:client-1',start:660}]}],unassigned:[]};
state.decision_log=[{id:'client-1',request_id:'request:client-1',address:'Москва, дом 1',status:'assigned',automatic:true,version:6,explanation:'Тогда была назначена.'}];
''')


def test_latest_application_shows_compact_before_after_and_job_reason():
    client('''
assert.equal($('application-visual').hidden,false);
assert.match($('application-visual-title').textContent,/Автопилот обновил план/);
assert.match($('application-visual-metrics').textContent,/12 → 13/);
assert.match($('application-visual-changes').textContent,/e1 · 10:00 → e2 · 11:00/);
assert.match($('application-visual-changes').textContent,/Подробности расчёта: Мастер ближе к следующей точке/);
assert.match($('application-visual-explanation').textContent,/Почему выбран этот вариант/);
''', before='''
state.last_applied={version:7,automatic:true,incoming_count:3,changed_count:1};
state.dispatch_notifications=[{id:'applied',kind:'auto_applied',version:7,
  request_ids:['client-1'],changed_request_ids:['request:client-2'],
  assignment_comparison:{before_requires_assignment:12,after_requires_assignment:13},before_unassigned_count:12,after_unassigned_count:13,unassigned_request_ids:Array(13).fill('x')}];
state.decision_log=[{id:'client-2',request_id:'request:client-2',address:'Москва, дом 2',status:'assigned',automatic:true,version:7,
  previous_engineer_id:'e1',previous_start:600,engineer_id:'e2',start:660,
  explanation:'Проверены окно и квалификация.',llm_explanation:'Мастер ближе к следующей точке',
  plan_explanation:'Выбран компромисс по SLA.',llm_notice:'YandexGPT проверила факты.'}];
''')


def test_application_explanation_can_arrive_without_changed_decisions():
    client('''
assert.equal($('application-visual').hidden,false);
assert.match($('application-visual-explanation').textContent,/Почему выбран этот вариант/);
assert.match($('application-visual-explanation').textContent,/Применён стабильный план/);
assert.match($('dispatch-notifications').textContent,/Почему применён этот план/);
''', before='''
state.last_applied={version:8,automatic:true,incoming_count:0,changed_count:0};
state.dispatch_notifications=[{id:'applied-8',kind:'auto_applied',version:8,
  request_ids:[],changed_request_ids:[],before_unassigned_count:2,after_unassigned_count:2,
  llm_status:'ready',plan_explanation:'Применён стабильный план.'}];
state.decision_log=[];
''')


def test_history_polling_keeps_explanation_nodes_and_open_state():
    client('''
const original=$('dispatch-notifications').querySelector('.notification-explanation');
original.open=true;
await pollNow();
assert.equal($('dispatch-notifications').querySelector('.notification-explanation')===original,true);
assert.equal(original.open,true);
state.dispatch_notifications[0].plan_explanation='Обновлённое пояснение.';
await pollNow();
const updated=$('dispatch-notifications').querySelector('.notification-explanation');
assert.equal(updated.open,true);
assert.match(updated.textContent,/Обновлённое пояснение/);
state.dispatch_notifications.unshift({id:'another',kind:'manual_applied',version:7,
    plan_explanation:'Другое пояснение.'});
await pollNow();
const details=$('dispatch-notifications').querySelectorAll('.notification-explanation');
assert.equal(details.find(d=>d.textContent.includes('Обновлённое')).open,true);
assert.ok(!details.find(d=>d.textContent.includes('Другое')).open);
state.dispatch_notifications=[];
await pollNow();
assert.equal($('dispatch-notifications').querySelectorAll('.notification-explanation').length,0);
''', before='''
let pollNow;
global.setInterval=fn=>{pollNow=fn;return {unref(){}};};
state.dispatch_notifications=[{id:'applied',kind:'manual_applied',version:7,
    plan_explanation:'Исходное пояснение.'}];
''')


def test_history_uses_structured_facts_instead_of_dense_model_prose():
    client('''
const detail=$('dispatch-notifications').querySelector('.notification-explanation');
assert.ok(detail.querySelector('.explanation-card'));
assert.match(detail.textContent,/Добавлено новых заявок: 1/);
assert.match(detail.textContent,/Прежние исполнители и время визитов сохранены/);
assert.match(detail.textContent,/Все рассчитанные варианты дали одинаковые назначения/);
assert.doesNotMatch(detail.textContent,/P\\(SLA\\)|Получен один уникальный план|Сплошной технический текст/);
assert.ok(detail.querySelector('ul'));
''', before='''
state.dispatch_notifications=[{id:'applied',kind:'manual_applied',version:7,
  plan_explanation:'Сплошной технический текст P(SLA).',
  created_request_ids:['request:new'],changed_request_ids:[],lost_request_ids:[],
  explanation_card:{label:'Меньше изменений',summary:'Назначено 67 из 67 заявок.',
    reasons:['Получен один уникальный план; отдельной альтернативы для сравнения нет.'],
    tradeoffs:[],attention:[]}}];
''')


def test_history_lists_cancellation_separately_from_changed_assignments():
    client('''
const detail=$('dispatch-notifications').querySelector('.notification-explanation');
assert.match(detail.textContent,/Отменено заявок: 1/);
assert.doesNotMatch(detail.textContent,/Изменены назначения|Прежние исполнители/);
''', before='''
state.dispatch_notifications=[{id:'cancel',kind:'manual_applied',version:8,
  changed_request_ids:['request:cancel'],cancelled_request_ids:['request:cancel'],
  lost_request_ids:[],explanation_card:{label:'Меньше изменений',summary:'Назначено 66 из 66 заявок.',
    reasons:[],tradeoffs:[],attention:[]}}];
''')


def test_application_keeps_verified_explanation_when_model_unavailable():
    client('''
assert.match($('application-visual-explanation').textContent,/Почему выбран этот вариант/);
assert.match($('application-visual-explanation').textContent,/Охват проверен/);
assert.match($('dispatch-notifications').textContent,/Почему применён этот план/);
''', before='''
state.last_applied={version:9,automatic:true,incoming_count:0,changed_count:0};
state.dispatch_notifications=[{id:'applied-9',kind:'auto_applied',version:9,
  request_ids:[],changed_request_ids:[],llm_status:'unavailable',
  llm_notice:'YandexGPT недоступна',plan_explanation:'Охват проверен.'}];
state.decision_log=[];
''')


def test_recommendation_network_failure_stays_neutral_and_reports_error():
    client('''
      await submit();
      await $('ai-recommend').dispatch('click');
      assert.match($('ai-result').textContent,/Не удалось получить объяснение/);
      assert.ok(cards().every(c=>!c.classList.contains('recommended')));
    ''', before='''
      const savedFetch=global.fetch;
      global.fetch=async(url,options)=>{if(url.endsWith('/recommend'))throw new Error('offline');return savedFetch(url,options);};
    ''')


def test_default_sort_does_not_imply_risk_recommendation():
    client("""
      await submit();
      assert.equal($('candidate-sort').value,'default');
      assert.deepEqual(cards().map(c=>c.dataset.id),['stable','sla','cost']);
      assert.ok(cards().every(c=>!c.classList.contains('recommended')));
    """, before="""
      for(const c of preview.candidates)c.risk={status:'ready',affected_clients:0,summary:{p_sla_breach:c.id==='cost'?.1:.9,expected_loss:1,expected_affected_customers:1,p_late_start:0,p_overtime:0}};
    """)


def test_application_uses_residual_comparison_immutable_time_and_manual_new_labels():
    client(r'''
state.time=720;
state.request_labels={'request:uuid-manual':'Локальная проблема №1'};
state.last_applied={version:7,time:590,automatic:false,incoming_count:1,changed_count:0};
state.dispatch_notifications=[{kind:'manual_applied',version:7,request_ids:[],created_request_ids:['request:uuid-manual'],
  before_assigned_count:67,after_assigned_count:52,
  assignment_comparison:{time:590,before_assigned:53,after_assigned:52,before_requires_assignment:0,after_requires_assignment:1,frozen_count:14}}];
state.decision_log=[{id:'uuid-manual',request_id:'request:uuid-manual',kind:'new',change_type:'new_assignment',
  status:'assigned',previous_engineer_id:null,engineer_id:'e1',start:700,address:'Новый адрес',version:7}];
state.decision_log.unshift({id:'old',request_id:'old',kind:'changed',status:'assigned',previous_engineer_id:'e1',engineer_id:'e2',version:7});
await $('reload').dispatch('click');await settle();
assert.match($('application-visual-context').textContent,/09:50/);
assert.equal($('application-visual-changes').querySelector('.application-change').dataset.requestId,'request:uuid-manual');
assert.doesNotMatch($('application-visual-context').textContent,/12:00/);
assert.match($('application-visual-metrics').textContent,/53 → 52/);
assert.doesNotMatch($('application-visual-metrics').textContent,/67 → 52/);
assert.match($('application-visual-changes').textContent,/Новая заявка/);
assert.match($('application-visual-changes').textContent,/Локальная проблема №1/);
assert.doesNotMatch($('application-visual-changes').textContent,/uuid-manual|Снова назначена/);
assert.match($('decision-log').textContent,/Локальная проблема №1/);
assert.doesNotMatch($('decision-log').textContent,/uuid-manual/);
state.dispatch_notifications[0].assignment_comparison=undefined;
await $('reload').dispatch('click');await settle();
assert.doesNotMatch($('application-visual-metrics').textContent,/67 → 52/);
assert.doesNotMatch($('dispatch-notifications').textContent,/67 → 52/);
''')
