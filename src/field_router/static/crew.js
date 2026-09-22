(function(){
'use strict';
const regionURL=path=>new URL(path.replace(/^\//,''),new URL('.',location.href)).href;
const $=id=>document.getElementById(id);let state,draft,busy=false,dirty=false;
const appFetch=(...args)=>window.FieldRouter?window.FieldRouter.fetch(...args):fetch(...args);
const appUrl=path=>window.FieldRouter?window.FieldRouter.url(path):regionURL(path);
// Local HTTP on an engineer phone may not expose secure-context UUIDs.
const uniqueId=()=>globalThis.crypto?.randomUUID?.()||Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
const labelTransport={public:'Общественный транспорт',car:'Авто',walk:'Пешком',bicycle:'Велосипед'};
const labelSkill={local:'Ремонт',connection:'Подключение',emergency:'Аварии'};
const labelField={name:'Имя',transport:'Транспорт',shift:'Смена',skills:'Навыки',start_location:'Старт',start_candidate_id:'Другой адрес',stock_mode:'Выдача',router:'Роутеры, шт.',tv_box:'ТВ-приставки, шт.'};
function node(tag,text){const n=document.createElement(tag);if(text!=null)n.textContent=text;return n;}
function canEdit(){return !busy&&state?.editable;}
function reset(){draft=null;$('preview').hidden=true;$('plan-link').removeAttribute('href');controls();}
function updateContext(){if(state)$('context').textContent=state.region+' · в системе '+state.engineers.length+' · в форме '+$('rows').children.length+' · версия '+state.version+(state.control_count?' · контрольная численность '+state.control_count:'')+'. Ориентир, не обязательный штат.';}
function markDirty(){if(!canEdit())return;dirty=true;reset();updateContext();status('Есть несохранённые изменения. Рассчитайте план и сравните его перед применением.');}
function status(message){$('status').textContent=message;$('error').hidden=true;}
function controls(){ $('fields').disabled=busy||!state?.editable;$('apply').disabled=busy||!state?.editable||!draft; }
async function api(path,payload){const r=await appFetch(appUrl(path),payload?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}:{cache:'no-store'});const d=await r.json();if(!r.ok)throw Error((d.error||'Ошибка сервера')+(r.status===409?' Обновите страницу и повторите настройку.':''));return d;}
async function run(action){if(busy)return;busy=true;controls();$('error').hidden=true;try{await action();}catch(e){reset();$('error').textContent=e.message;$('error').hidden=false;$('status').textContent='Изменения не применены.';}finally{busy=false;controls();}}
function clock(n){return String(Math.floor(n/60)).padStart(2,'0')+':'+String(n%60).padStart(2,'0');}
function minutes(s){if(!/^([01]\d|2[0-3]):[0-5]\d$/.test(s))throw Error('Проверьте время смены');return Number(s.slice(0,2))*60+Number(s.slice(3));}
function changeValue(key,value){
if(value==null||value==='')return '—';
if(key==='skills')return value.map(x=>labelSkill[x]||x).join(', ');
if(key==='shift')return clock(value.start)+'–'+clock(value.end);
if(key==='transport')return labelTransport[value]||value;
if(key==='stock_mode')return value==='plan'?'Под план + резерв':'Фиксированный запас';
if(key==='start_location')return state.locations.find(x=>x.id===value)?.label||value;
if(key==='start_candidate_id')return 'Выбранный адрес';
return String(value);
}
function showChanges(target,changes){
target.replaceChildren();
if(!changes?.length){target.append(node('p','Состав и параметры без изменений.'));return;}
for(const change of changes){
const p=node('p'),name=node('strong',change.name+': ');
const text=Object.entries(change.fields).map(([key,values])=>key==='crew'?(values.after?'Добавлена бригада':'Бригада убрана'):(labelField[key]||key)+': '+changeValue(key,values.before)+' → '+changeValue(key,values.after)).join('; ');
p.append(name,document.createTextNode(text));target.append(p);
}
}
function showAssumptions(target,assumptions){target.replaceChildren(...(assumptions||[]).map(text=>node('li',text)));}
function selectedPreset(){return state?.presets?.find(p=>p.id===$('preset-select').value);}
function describePreset(){const preset=selectedPreset();$('preset-description').textContent=preset?.description||'';showChanges($('preset-changes'),preset?.changes);showAssumptions($('preset-assumptions'),preset?.assumptions);}
function showComparison(){
const table=node('table'),head=node('thead'),heading=node('tr'),body=node('tbody');
for(const title of ['Показатель','Сейчас','Новый план']){const th=node('th',title);th.scope='col';heading.append(th);}head.append(heading);
const coverage=(side,priority)=>{const value=draft[side+'_coverage']?.[priority];return value?value.assigned+' / '+value.total:'—';};
const number=value=>Number.isFinite(value)?value.toLocaleString('ru-RU',{maximumFractionDigits:1}):'—';
const rows=[['Назначено заявок',draft.before.assigned,draft.after.assigned],['Аварии',coverage('before','urgent'),coverage('after','urgent')],['Подключения',coverage('before','high'),coverage('after','high')],['Бригад на линии',draft.before.engineers_used,draft.after.engineers_used],['В пути, мин',number(draft.before.travel_minutes),number(draft.after.travel_minutes)],['Расстояние, км',number(draft.before.distance_m/1000),number(draft.after.distance_m/1000)]];
for(const [label,before,after] of rows){const tr=node('tr'),th=node('th',label);th.scope='row';tr.append(th,node('td',before),node('td',after));body.append(tr);}
table.append(node('caption','Аварии и подключения: назначено / всего'),head,body);$('comparison').replaceChildren(table);
}
function field(parent,label,key,value,type='text',options){const box=node('div');const id='f-'+uniqueId();const l=node('label',label);l.htmlFor=id;const input=node(options?'select':'input');input.id=id;input.dataset.key=key;
if(options){for(const [value,text] of options){const o=node('option',text);o.value=value;input.append(o);}}else{input.type=type;if(type==='number'){input.min=0;input.max=100;input.step=1;}input.required=true;}
input.value=value;box.append(l,input);parent.append(box);return input;}
function row(e){const card=node('section');card.className='crew-row';card.dataset.id=e.id;const grid=node('div');grid.className='crew-grid';
const saved=state.engineers.some(person=>person.id===e.id);
const membership=node('p',state.region+' · '+(saved?'В системе · заявок в текущем плане: '+(state.assignments?.[e.id]||0):'Новая бригада · ещё не сохранена'));membership.className='crew-membership';card.append(membership,grid);
field(grid,'Имя','name',e.name).maxLength=100;field(grid,'Транспорт','transport',e.transport,'text',Object.entries(labelTransport));
field(grid,'Начало смены','start',clock(e.shift.start),'time');field(grid,'Конец смены','end',clock(e.shift.end),'time');
const starts=state.locations.map(x=>[x.id,x.label]);const start=field(grid,'Старт дня','start_location',e.start_location,'text',starts);
const policy=field(grid,'Выдача','stock_mode',e.stock_mode,'text',[['plan','Под план + резерв'],['fixed','Фиксированный запас']]);
const router=field(grid,'Роутеры','router',e.router,'number'),tv=field(grid,'ТВ-приставки','tv_box',e.tv_box,'number');
const stockLabels=()=>{const label=policy.value==='plan'?'Резерв':'Запас';router.previousSibling.textContent=label+' роутеров, шт.';tv.previousSibling.textContent=label+' ТВ-приставок, шт.';};policy.onchange=stockLabels;stockLabels();
const skills=node('div');skills.className='skills';for(const [key,title] of [['local','Ремонт'],['connection','Подключение'],['emergency','Аварии']]){const l=node('label');const c=node('input');c.type='checkbox';c.value=key;c.checked=e.skills.includes(key);c.dataset.skill=key;l.append(c,document.createTextNode(title));skills.append(l);}card.append(skills);
const details=node('details'),summary=node('summary','Другой стартовый адрес');details.append(summary);const query=field(details,'Город, улица, дом','query','','text');query.required=false;query.maxLength=300;
const search=node('button','Найти адрес');search.type='button';search.disabled=!state.network;const results=node('select');results.className='address-results';results.setAttribute('aria-label','Найденные стартовые адреса');results.append(node('option','Выберите результат'));results.options[0].value='';const hint=node('p');
search.onclick=()=>{if(!canEdit()||!state.network)return;return run(async()=>{if(!query.value.trim())throw Error('Введите адрес');reset();status('Поиск стартовой точки…');const data=await api('/api/address/search',{version:state.version,address:query.value});results.replaceChildren();const empty=node('option','Выберите результат');empty.value='';results.append(empty);for(const c of data.candidates){const o=node('option',c.label+(c.quality==='approximate'?' · приблизительно':''));o.value=c.id;o.dataset.label=c.original_address||c.label;results.append(o);}status('Выберите найденную точку.');});};
results.onchange=()=>{if(!canEdit())return;card.dataset.candidate=results.value;hint.textContent=results.value?'Старт будет заменён найденной точкой при расчёте. Проверьте адрес; координаты могут быть приблизительными.':'';markDirty();};start.onchange=()=>{if(!canEdit())return;delete card.dataset.candidate;results.value='';hint.textContent='';};
details.append(search,results,hint);card.append(details);const remove=node('button','Убрать бригаду');remove.type='button';remove.className='secondary';remove.onclick=()=>{if(!canEdit())return;card.remove();markDirty();};card.append(remove);
if(saved){const mobile=node('a','Открыть приложение инженера ↗');mobile.className='crew-mobile-link';mobile.href=appUrl('/engineer?engineer_id='+encodeURIComponent(e.id));mobile.target='_blank';mobile.rel='noopener';card.append(mobile);}
$('rows').append(card);
}
function collect(){return [...$('rows').children].map(card=>{const get=k=>card.querySelector('[data-key="'+k+'"]').value;return {id:card.dataset.id,name:get('name').trim(),transport:get('transport'),shift:{start:minutes(get('start')),end:minutes(get('end'))},skills:[...card.querySelectorAll('[data-skill]:checked')].map(x=>x.value),start_location:get('start_location'),start_candidate_id:card.dataset.candidate||null,stock_mode:get('stock_mode'),router:Number(get('router')),tv_box:Number(get('tv_box'))};});}
$('rows').addEventListener('input',markDirty);$('rows').addEventListener('change',markDirty);
$('preset-select').onchange=()=>{if(canEdit())describePreset();};
$('recommend').onclick=()=>{if(!canEdit())return;const preset=selectedPreset();if(!preset)return;$('rows').replaceChildren();preset.engineers.forEach(row);markDirty();status('Форма заполнена: «'+preset.label+'». Рассчитайте и сравните план перед применением.');};
$('add').onclick=()=>{if(!canEdit())return;row({id:'crew-'+uniqueId(),name:'Новая бригада',transport:'public',shift:{start:540,end:1080},skills:['local'],start_location:'office',stock_mode:'plan',router:1,tv_box:0});markDirty();};
$('crew-form').onsubmit=e=>{e.preventDefault();if(!canEdit())return;return run(async()=>{reset();status('Расчёт состава, дорог и нового плана…');draft=await api('/api/crew/preview',{version:state.version,engineers:collect()});showComparison();showChanges($('changes'),draft.changes);showAssumptions($('assumptions'),draft.assumptions);$('participation').replaceChildren(...(draft.engineers||[]).map(e=>node('p',e.name+' · '+e.region+' · заявок: '+e.assigned+(e.assigned?'':' — участвовал в расчёте, назначений в найденном плане нет'))));$('unassigned-summary').textContent='Без назначения: '+draft.before.unassigned+' → '+draft.after.unassigned;$('unassigned').replaceChildren(...draft.unassigned.map(x=>node('p',x.request_id+': '+x.reason)));$('issue').replaceChildren(...draft.issue.map(x=>node('p',x.name+': роутеры '+x.router+', ТВ '+x.tv_box)));$('plan-link').href=appUrl('/crew-plan?id='+encodeURIComponent(draft.id)+'&version='+state.version);$('preview').hidden=false;status('Проверьте план и выдачу. Состав пока не изменён.');});};
$('apply').onclick=()=>{if(!canEdit()||!draft)return;return run(async()=>{await api('/api/crew/apply',{id:draft.id,version:state.version});dirty=false;location.href=appUrl('/');});};
window.addEventListener('region-change',event=>{if(busy||dirty){event.preventDefault();$('error').textContent='Примените состав после предпросмотра или обновите страницу, чтобы отменить изменения.';$('error').hidden=false;window.scrollTo({top:0});}});
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
run(async()=>{state=await api('/api/crew');$('back-link').href=appUrl('/');state.engineers.forEach(row);updateContext();$('preset-select').replaceChildren(...(state.presets||[]).map(p=>{const option=node('option',p.label);option.value=p.id;return option;}));describePreset();status(state.editable?'Настройка до начала дня. Сначала расчёт, затем применение.':'День уже начат: состав доступен только для просмотра.');});
})();
