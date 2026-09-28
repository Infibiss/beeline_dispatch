"""Crew screen behavior using its real client and a dependency-free DOM boundary."""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

import pytest

STATIC = Path(__file__).resolve().parents[1] / 'src/field_router/static'


class Markup(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tree = {'tag': 'document', 'attrs': {}, 'children': []}
        self.stack = [self.tree]
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        item = {'tag': tag, 'attrs': dict(attrs), 'children': []}
        self.stack[-1]['children'].append(item)
        if tag not in {'meta', 'link', 'input', 'br', 'hr'}:
            self.stack.append(item)

    def handle_endtag(self, tag):
        if self.stack[-1]['tag'] == tag:
            self.stack.pop()

    def handle_data(self, data):
        self.stack[-1]['children'].append(data)


HARNESS = r'''
const assert = require('node:assert/strict');
class Element {
  constructor(tag) { this.tagName=tag.toUpperCase(); this.children=[]; this.dataset={}; this.events={}; this.attributes={}; this._text=''; this._value=''; this.hidden=false; this.disabled=false; }
  append(...items) { for (let item of items) { if(typeof item==='string') item=Object.assign(new Element('#text'),{textContent:item}); item.parentNode=this; this.children.push(item); } }
  replaceChildren(...items) { this.children=[]; this._text=''; this.append(...items); }
  remove() { this.parentNode.children=this.parentNode.children.filter(x=>x!==this); }
  set textContent(value) { this._text=String(value); this.children=[]; }
  get textContent() { return this._text+this.children.map(x=>x.textContent).join(''); }
  set value(value) { this._value=String(value); }
  get value() { return this._value || (this.tagName==='SELECT' ? this.children[0]?.value || '' : ''); }
  get options() { return this.children; }
  get previousSibling() { return this.parentNode.children[this.parentNode.children.indexOf(this)-1]; }
  setAttribute(key,value) { this.attributes[key]=value; }
  removeAttribute(key) { delete this.attributes[key]; if(key==='href') delete this.href; }
  addEventListener(type,handler) { (this.events[type] ||= []).push(handler); }
  dispatch(type) { const e={target:this,preventDefault(){}}; for(let n=this;n;n=n.parentNode) for(const handler of n.events[type]||[]) handler(e); }
  matches(selector) {
    if(selector.startsWith('.')) return (this.className||'').split(' ').includes(selector.slice(1));
    if(selector.startsWith('#')) return this.id===selector.slice(1);
    const data=selector.match(/^\[data-(key|skill)(?:="([^"]+)")?\](:checked)?$/);
    if(data) return this.dataset[data[1]]!==undefined && (!data[2] || this.dataset[data[1]]===data[2]) && (!data[3] || this.checked);
    return this.tagName===selector.toUpperCase();
  }
  closest(selector) { for(let n=this;n;n=n.parentNode) if(n.matches(selector)) return n; return null; }
  contains(other) { for(let n=other;n;n=n.parentNode) if(n===this) return true; return false; }
  querySelectorAll(selector) { return this.children.flatMap(x=>[...(x.matches(selector)?[x]:[]),...x.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0]||null; }
}
const ids={};
function build(item) {
  if(typeof item==='string') return Object.assign(new Element('#text'),{textContent:item});
  const el=new Element(item.tag);
  for(const [key,value] of Object.entries(item.attrs)) { if(['hidden','disabled','required'].includes(key)) el[key]=true; else if(key==='class') el.className=value; else el[key]=value; }
  if(el.id) ids[el.id]=el;
  el.append(...item.children.map(build)); return el;
}
build(markup);
global.document={getElementById:id=>ids[id]||null,createElement:tag=>new Element(tag),createTextNode:text=>Object.assign(new Element('#text'),{textContent:text})};
global.window={events:{},addEventListener(type,handler){(this.events[type] ||= []).push(handler);},scrollTo(){},dispatch(type){const e={prevented:false,preventDefault(){this.prevented=true;}};for(const handler of this.events[type]||[])handler(e);return e;}};
global.location={href:'http://localhost/regions/south/crew'};
global.crypto=require('node:crypto').webcrypto;
const base={id:'E-1',name:'Анна',transport:'public',shift:{start:540,end:1080},skills:['local'],start_location:'office',stock_mode:'fixed',router:0,tv_box:0};
const changes=[{engineer_id:'E-1',name:'Анна',fields:{stock_mode:{before:'fixed',after:'plan'},router:{before:0,after:1}}}];
const presets=[
 {id:'current',label:'Текущий состав',description:'Без изменений',engineers:[base],changes:[],assumptions:[]},
 {id:'homes',label:'Домашние старты',description:'Старт ближе к заявкам',engineers:[{...base,start_location:'job'}],changes:[{engineer_id:'E-1',name:'Анна',fields:{start_location:{before:'office',after:'job'}}}],assumptions:['Модельная стартовая точка']},
 {id:'skills',label:'Навыки',description:'Расширение навыков',engineers:[{...base,skills:['local','emergency']}],changes:[],assumptions:[]},
 {id:'reserve',label:'Выдача + резерв',description:'Один запасной роутер',engineers:[{...base,stock_mode:'plan',router:1}],changes,assumptions:['Склад достаточен']},
 {id:'combined',label:'Всё вместе',description:'Все настройки',engineers:[{...base,stock_mode:'plan',router:1,start_location:'job'}],changes,assumptions:[]},
 {id:'control',label:'Контрольный состав',description:'Ориентир',engineers:[{...base,id:'model-01'}],changes:[],assumptions:[]}
];
const settings={version:7,region:'Юго-восток',editable:true,network:false,control_count:12,engineers:[base],recommended_engineers:[{...base,id:'old-model'}],presets,locations:[{id:'office',label:'Офис'},{id:'job',label:'Ступино'}],metrics:{}};
const preview={id:'draft-1',before:{assigned:4,unassigned:3,engineers_used:2,travel_minutes:120,distance_m:22500},after:{assigned:6,unassigned:1,engineers_used:3,travel_minutes:95,distance_m:18000},before_coverage:{urgent:{assigned:0,total:1},high:{assigned:1,total:2},normal:{assigned:3,total:4}},after_coverage:{urgent:{assigned:1,total:1},high:{assigned:2,total:2},normal:{assigned:3,total:4}},engineers:[{id:'E-1',name:'Анна',region:'Юго-восток',assigned:6}],changes,assumptions:['Склад достаточен'],issue:[{name:'Анна',router:3,tv_box:0}],unassigned:[{request_id:'R-7',reason:'Нет времени'}]};
const requests=[]; let holdPreview=false, releasePreview;
global.fetch=async(url,options={})=>{
  requests.push({url,payload:options.body?JSON.parse(options.body):null});
  if(url.endsWith('/preview') && holdPreview) await new Promise(resolve=>releasePreview=resolve);
  return {ok:true,json:async()=>url.endsWith('/preview')?preview:url.endsWith('/apply')?{version:8}:settings};
};
const $=id=>ids[id];
const input=key=>$('rows').querySelector('[data-key="'+key+'"]');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const submit=async()=>{await $('crew-form').onsubmit({preventDefault(){}}); await settle();};
const choose=id=>{$('preset-select').value=id; $('preset-select').onchange?.(); $('preset-select').dispatch('change');};
'''


def client(expression, before=''):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for client checks')
    markup = Markup((STATIC / 'crew.html').read_text()).tree
    script = 'const markup=' + json.dumps(markup) + ';' + HARNESS + before
    script += '\nrequire(' + json.dumps(str(STATIC / 'crew.js')) + ');'
    script += '\n(async()=>{await settle();' + expression + '})().catch(error=>{console.error(error);process.exitCode=1;});'
    result = subprocess.run([node, '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_preset_fills_form_only_and_uses_saved_roster_instead_of_unsaved_rows():
    client('''
assert.ok($('preset-select'),'Preset selection must be available');
assert.deepEqual($('preset-select').options.map(x=>x.value),presets.map(x=>x.id));
choose('homes');
assert.equal(input('start_location').value,'office');
assert.match($('preset-description').textContent,/ближе/);
$('recommend').onclick();
assert.equal(input('start_location').value,'job');
assert.equal($('rows').children[0].dataset.id,'E-1');
assert.equal(requests.length,1,'Filling the form must not publish or preview');
input('name').value='Несохранённая правка';
choose('reserve'); $('recommend').onclick();
assert.equal(input('name').value,'Анна');
assert.equal(input('start_location').value,'office');
assert.equal(input('router').value,'1');
await submit();
assert.equal(requests[1].url,'http://localhost/regions/south/api/crew/preview');
assert.equal(requests[1].payload.engineers[0].id,'E-1');
assert.equal(requests[1].payload.engineers[0].stock_mode,'plan');
assert.equal(requests.filter(x=>x.url.endsWith('/apply')).length,0);
''')


def test_preview_compares_priorities_and_editing_invalidates_approval():
    client('''
await submit();
assert.equal($('preview').hidden,false);
const cells=$('comparison').querySelectorAll('tr').map(row=>row.children.map(x=>x.textContent));
assert.ok(cells.some(row=>row.join('|')==='Аварии|0 / 1|1 / 1'));
assert.ok(cells.some(row=>row.join('|')==='Подключения|1 / 2|2 / 2'));
assert.ok(cells.some(row=>row.join('|')==='В пути, мин|120|95'));
assert.ok(cells.some(row=>row.join('|')==='Расстояние, км|22,5|18'));
assert.match($('changes').textContent,/Анна/);
assert.match($('issue').textContent,/роутеры 3/);
assert.equal($('apply').disabled,false);
choose('homes');
assert.equal($('preview').hidden,false,'Browsing presets does not change form or invalidate its draft');
input('name').value='Новое имя'; input('name').dispatch('input');
assert.equal($('preview').hidden,true);
assert.equal($('apply').disabled,true);
await $('apply').onclick();
assert.equal(requests.filter(x=>x.url.endsWith('/apply')).length,0);
await submit(); await $('apply').onclick();
assert.deepEqual(requests.at(-1).payload,{id:'draft-1',version:7});
assert.equal(location.href,'http://localhost/regions/south/');
''')


def test_started_day_is_read_only_even_for_direct_event_invocation():
    client('''
assert.equal($('fields').disabled,true);
$('recommend').onclick(); $('add').onclick(); await submit(); await $('apply').onclick();
assert.equal($('rows').children.length,1);
assert.equal(requests.length,1);
''', before='settings.editable=false;')


def test_calculation_blocks_duplicate_requests_and_preset_changes():
    client('''
choose('homes'); holdPreview=true;
const pending=submit(); await settle();
assert.equal($('fields').disabled,true);
$('recommend').onclick(); $('add').onclick(); await $('crew-form').onsubmit({preventDefault(){}});
assert.equal($('rows').children.length,1);
assert.equal(input('start_location').value,'office');
assert.equal(requests.length,2);
releasePreview(); await pending;
assert.equal($('fields').disabled,false);
assert.equal($('apply').disabled,false);
''')


def test_authenticated_regional_preview_keeps_comparison_membership_and_mobile_links():
    client('''
assert.equal($('rows').querySelector('.crew-mobile-link').href,'/r/south/engineer?engineer_id=E-1');
choose('reserve'); $('recommend').onclick(); await submit();
assert.ok(requests.every(request=>request.url.startsWith('/r/south/')));
assert.equal(secureCalls,requests.length,'Every request must use authenticated transport');
assert.match($('participation').textContent,/Анна.*Юго-восток.*6/);
assert.match($('comparison').textContent,/Аварии/);
assert.match($('changes').textContent,/Роутеры/);
assert.equal(requests.filter(x=>x.url.endsWith('/apply')).length,0);
await $('apply').onclick();
assert.equal(requests.filter(x=>x.url.endsWith('/apply')).length,1);
assert.equal(location.href,'/r/south/');
''', before='''let secureCalls=0; window.FieldRouter={url:path=>'/r/south'+path,fetch:(...args)=>{secureCalls++;return fetch(...args);}};''')


def test_dirty_roster_blocks_navigation_until_explicit_apply():
    client('''
assert.equal(window.dispatch('region-change').prevented,false);
assert.equal(window.dispatch('beforeunload').prevented,false);
input('name').value='Несохранённое имя'; input('name').dispatch('input');
assert.equal(window.dispatch('region-change').prevented,true);
assert.equal(window.dispatch('beforeunload').prevented,true);
await submit();
assert.equal(window.dispatch('beforeunload').prevented,true,'Preview does not save roster');
await $('apply').onclick();
assert.equal(window.dispatch('beforeunload').prevented,false,'Confirmed apply clears unsaved state');
''')


def test_crew_ids_work_on_lan_http_without_secure_context_crypto():
    client('''
$('add').onclick();
assert.equal($('rows').children.length,2);
assert.ok($('rows').children[1].dataset.id.startsWith('crew-'));
assert.equal(new Set($('rows').children.map(row=>row.dataset.id)).size,2);
''', before="Object.defineProperty(globalThis,'crypto',{value:undefined,configurable:true});")


def test_embedded_editor_reports_drafts_and_apply_to_same_origin_parent():
    client('''
input('name').value='Новое имя';input('name').dispatch('input');
assert.ok(messages.some(x=>x.data.type==='crew-editing' && x.data.dirty));
await submit();await $('apply').onclick();await settle();
assert.ok(messages.some(x=>x.data.type==='crew-applied' && !x.data.dirty));
assert.ok(messages.every(x=>x.origin==='http://localhost'));
assert.equal(location.href,'http://localhost/regions/south/crew?embedded=1');
assert.ok(requests.some(x=>x.url==='http://localhost/regions/south/api/crew/apply'));
''', before='''
const messages=[];
location.href='http://localhost/regions/south/crew?embedded=1';
document.body={classList:{add(){}}};
window.parent={postMessage(data,origin){messages.push({data,origin});}};
''')


def test_embedded_discard_restores_saved_rows_without_unload_confirmation():
    client('''
input('name').value='Черновик';input('name').dispatch('input');
const discard={origin:'http://localhost',source:window.parent,data:{type:'crew-discard'}};
for(const fn of window.events.message||[])fn({...discard,source:{}});
assert.equal(input('name').value,'Черновик');
for(const fn of window.events.message||[])fn(discard);
assert.equal(input('name').value,'Анна');
assert.equal(window.dispatch('beforeunload').prevented,false);
assert.ok(messages.some(x=>x.data.type==='crew-editing'&&!x.data.dirty));
''', before='''
const messages=[];
location.href='http://localhost/regions/south/crew?embedded=1';
document.body={classList:{add(){}}};
window.parent={postMessage(data,origin){messages.push({data,origin});}};
''')
