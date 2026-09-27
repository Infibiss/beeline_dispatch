const assert = require('node:assert/strict');
class Element {
  constructor(tag='div') { this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.events={};this.attributes={};this._text='';this.hidden=false;this.disabled=false;this.className='';this.classList={toggle:()=>{}}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children=[];this._text='';this.append(...items); }
  set textContent(value) { this._text=String(value);this.children=[]; }
  get textContent() { return this._text+this.children.map(x=>x.textContent).join(' '); }
  setAttribute(key,value) { this.attributes[key]=String(value); }
  addEventListener(type,handler) { (this.events[type] ||= []).push(handler); }
  click() { if(this.disabled)return;this.onclick?.();for(const handler of this.events.click||[])handler({target:this}); }
  matches(selector) { return selector.startsWith('.') ? this.className.split(' ').includes(selector.slice(1)) : this.tagName===selector.toUpperCase(); }
  querySelectorAll(selector) { return this.children.flatMap(x=>[...(x.matches(selector)?[x]:[]),...x.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0]||null; }
}
const ids={};
for(const id of ['risk-raw-details','risk-raw-payload','panel-risk','risk-indicators','risk-engine-meta','risk-route-details','risk-routes','risk-assumption-details','risk-assumptions','risk-advisory-section','risk-advisories','risk-demo','risk-demo-notice','risk-forecasts','risk-model-status','risk-refresh','risk-error','risk-context','risk-stale','risk-facts','risk-replan'])ids[id]=new Element();
ids['risk-route-details'].append(new Element('summary'));
global.document={getElementById:id=>ids[id]||null,createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag),querySelectorAll:selector=>Object.values(ids).flatMap(el=>el.querySelectorAll(selector))};
global.CustomEvent=class { constructor(type,options){this.type=type;this.detail=options.detail;} };
const intents=[];
global.window={events:{},addEventListener(type,handler){(this.events[type] ||= []).push(handler);},dispatchEvent(event){if(event.type==='risk-intent')intents.push(event.detail);for(const handler of this.events[event.type]||[])handler(event);}};
const snapshot={schema_version:'1.0',context:{region:'Восток',date:'2026-09-28',time:600,plan_version:7,needs_replanning:false},summary:{engineers:3,roster:4,unassigned:2},indicators:[
 {id:'capacity',title:'Загрузка бригад',value:70,caption:'времени смен',detail:'420 из 600 мин',explanation:'explanation',recommendation:'recommendation',facts:{occupied_minutes:420,capacity_minutes:600}},
 {id:'windows',title:'Плотные клиентские окна',value:25,caption:'визитов',detail:'1 из 4 визитов',explanation:'explanation',recommendation:'recommendation',facts:{tight_visits:1,checked_visits:4,threshold_minutes:15}},
 {id:'equipment',title:'Оборудование под план',value:50,caption:'остатка',detail:'Роутеры: нужно 2, на руках 4',explanation:'explanation',recommendation:'recommendation',facts:{equipment:[{label:'Роутеры',needed:2,on_hand:4}]}}
],forecast:{status:'ready',region:'Восток',plan_version:7,model_version:'risk-engine-v1.1',iterations:600,calibrated:false,p_sla_breach:.31,p_late_start:.46,p_overtime:.17,expected_loss:4.2,expected_affected_customers:.62,loss_unit:'conditional_loss_units',routes:[{engineer_id:'E-1',engineer_name:'Анна',p_sla_breach:.31,p_late_start:.46,p_overtime:.17,expected_loss:4.2,expected_affected_customers:.62,visits:[{request_id:'R-1',request_name:'Лесная, 10',planned_start:620,planned_sla_deadline:690,p_sla_breach:.31,p_late_start:.46,expected_loss:4.2,service_duration:{p50_minutes:70,p80_minutes:88,p90_minutes:101}}]}],assumptions:['synthetic priors'],advisories:[{id:'gps-1',action:'facts',title:'Проверьте задержку',detail:'Уточните положение инженера',plan_version:7,engineer_id:'E-1',request_id:'R-1'}]}};
let response=snapshot;
global.fetch=async()=>({ok:true,json:async()=>response});
const $=id=>ids[id];
const fire=(type,detail)=>window.dispatchEvent(new CustomEvent(type,{detail}));
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const refresh=async()=>{await $('risk-refresh').onclick();await settle();};
module.exports={assert,ids,$,snapshot,intents,fire,settle,refresh,setResponse:value=>response=value};
