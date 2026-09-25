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
    this.attributes={}; this._text=''; this._value=null; this.hidden=false; this.disabled=false;
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
    markup = Markup((STATIC / 'scenarios.html').read_text()).tree
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
assert.match($('comparison-metrics').textContent,/относительно «Меньше изменений»/);
assert.equal($('approve').disabled,true,'Recommendation does not approve or select');
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
assert.equal($('ai-panel').hidden,false,'Server recommendation is available without AI');
assert.equal($('ai-recommend').hidden,true);
assert.equal(cards()[0].classList.contains('recommended'),true);
''', before='''
preview.llm_enabled=false;
preview.candidates[1].strategy_ids=['stable','sla','cost'];
for(const c of preview.candidates) if(c.id!=='stable') c.duplicate_of='stable';
''')


def test_alias_recommendation_highlights_canonical_card_and_preserves_apply_guard():
    client('''
await submit();
assert.deepEqual(cards().map(c=>c.dataset.id),['stable','cost']);
assert.equal($('candidates').dataset.count,'2');
assert.equal(cards()[1].classList.contains('recommended'),true);
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


def test_risk_sort_and_comparison_expose_monte_carlo_tradeoffs():
    client('''
await submit();
assert.deepEqual(cards().map(c=>c.dataset.id),['cost','sla','stable']);
assert.deepEqual(row('P(SLA), %'),['8.0 (−12.0)','12.0 (−8.0)','20.0']);
assert.deepEqual(row('Ожидаемые потери, усл. ед.'),['1.20 (−1.80)','1.80 (−1.20)','3.00']);
assert.deepEqual(row('Клиентов с изменённым обещанием'),['1 (−2)','2 (−1)','3']);
assert.match(cards()[0].textContent,/Risk Engine · 600 прогонов/);
assert.doesNotMatch(cards()[0].textContent,/калибр/);
assert.match($('variants-note').textContent,/Монте-Карло/);
assert.match($('variants-note').textContent,/калибровка вероятностей не подтверждена/);
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
