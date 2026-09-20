(function(){
'use strict';
const regionURL=path=>new URL(path.replace(/^\//,''),new URL('.',location.href)).href;
const $=id=>document.getElementById(id);let state,draft,busy=false;
const labelTransport={public:'Общественный транспорт',car:'Авто',walk:'Пешком',bicycle:'Велосипед'};
function node(tag,text){const n=document.createElement(tag);if(text!=null)n.textContent=text;return n;}
function reset(){draft=null;$('preview').hidden=true;}
function status(message){$('status').textContent=message;$('error').hidden=true;}
function controls(){ $('fields').disabled=busy||!state?.editable;$('apply').disabled=busy||!draft; }
async function api(path,payload){const r=await fetch(regionURL(path),payload?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}:{cache:'no-store'});const d=await r.json();if(!r.ok)throw Error((d.error||'Ошибка сервера')+(r.status===409?' Обновите страницу и повторите настройку.':''));return d;}
async function run(action){busy=true;controls();$('error').hidden=true;try{await action();}catch(e){reset();$('error').textContent=e.message;$('error').hidden=false;$('status').textContent='Изменения не применены.';}finally{busy=false;controls();}}
function clock(n){return String(Math.floor(n/60)).padStart(2,'0')+':'+String(n%60).padStart(2,'0');}
function minutes(s){if(!/^([01]\d|2[0-3]):[0-5]\d$/.test(s))throw Error('Проверьте время смены');return Number(s.slice(0,2))*60+Number(s.slice(3));}
function field(parent,label,key,value,type='text',options){const box=node('div');const id='f-'+crypto.randomUUID();const l=node('label',label);l.htmlFor=id;const input=node(options?'select':'input');input.id=id;input.dataset.key=key;
if(options){for(const [value,text] of options){const o=node('option',text);o.value=value;input.append(o);}}else{input.type=type;if(type==='number'){input.min=0;input.max=100;input.step=1;}input.required=true;}
input.value=value;box.append(l,input);parent.append(box);return input;}
function row(e){const card=node('section');card.className='crew-row';card.dataset.id=e.id;const grid=node('div');grid.className='crew-grid';card.append(grid);
field(grid,'Имя','name',e.name).maxLength=100;field(grid,'Транспорт','transport',e.transport,'text',Object.entries(labelTransport));
field(grid,'Начало смены','start',clock(e.shift.start),'time');field(grid,'Конец смены','end',clock(e.shift.end),'time');
const starts=state.locations.map(x=>[x.id,x.label]);const start=field(grid,'Старт дня','start_location',e.start_location,'text',starts);
const policy=field(grid,'Выдача','stock_mode',e.stock_mode,'text',[['plan','Под план + резерв'],['fixed','Фиксированный запас']]);
const router=field(grid,'Роутеры','router',e.router,'number'),tv=field(grid,'ТВ-приставки','tv_box',e.tv_box,'number');
const stockLabels=()=>{const label=policy.value==='plan'?'Резерв':'Запас';router.previousSibling.textContent=label+' роутеров, шт.';tv.previousSibling.textContent=label+' ТВ-приставок, шт.';};policy.onchange=stockLabels;stockLabels();
const skills=node('div');skills.className='skills';for(const [key,title] of [['local','Ремонт'],['connection','Подключение'],['emergency','Аварии']]){const l=node('label');const c=node('input');c.type='checkbox';c.value=key;c.checked=e.skills.includes(key);c.dataset.skill=key;l.append(c,document.createTextNode(title));skills.append(l);}card.append(skills);
const details=node('details'),summary=node('summary','Другой стартовый адрес');details.append(summary);const query=field(details,'Город, улица, дом','query','','text');query.required=false;query.maxLength=300;
const search=node('button','Найти адрес');search.type='button';search.disabled=!state.network;const results=node('select');results.className='address-results';results.setAttribute('aria-label','Найденные стартовые адреса');results.append(node('option','Выберите результат'));results.options[0].value='';const hint=node('p');
search.onclick=()=>run(async()=>{if(!query.value.trim())throw Error('Введите адрес');reset();status('Поиск стартовой точки…');const data=await api('/api/address/search',{version:state.version,address:query.value});results.replaceChildren();const empty=node('option','Выберите результат');empty.value='';results.append(empty);for(const c of data.candidates){const o=node('option',c.label+(c.quality==='approximate'?' · приблизительно':''));o.value=c.id;o.dataset.label=c.original_address||c.label;results.append(o);}status('Выберите найденную точку.');});
results.onchange=()=>{card.dataset.candidate=results.value;hint.textContent=results.value?'Старт будет заменён найденной точкой при расчёте. Проверьте адрес; координаты могут быть приблизительными.':'';reset();};start.onchange=()=>{delete card.dataset.candidate;results.value='';hint.textContent='';};
details.append(search,results,hint);card.append(details);const remove=node('button','Убрать бригаду');remove.type='button';remove.className='secondary';remove.onclick=()=>{card.remove();reset();};card.append(remove);$('rows').append(card);
}
function collect(){return [...$('rows').children].map(card=>{const get=k=>card.querySelector('[data-key="'+k+'"]').value;return {id:card.dataset.id,name:get('name').trim(),transport:get('transport'),shift:{start:minutes(get('start')),end:minutes(get('end'))},skills:[...card.querySelectorAll('[data-skill]:checked')].map(x=>x.value),start_location:get('start_location'),start_candidate_id:card.dataset.candidate||null,stock_mode:get('stock_mode'),router:Number(get('router')),tv_box:Number(get('tv_box'))};});}
$('fields').addEventListener('input',reset);$('fields').addEventListener('change',reset);
$('recommend').onclick=()=>{$('rows').replaceChildren();state.recommended_engineers.forEach(row);reset();status('Форма заполнена моделью участка. Рассчитайте и сравните план перед применением.');};
$('add').onclick=()=>{row({id:'crew-'+crypto.randomUUID(),name:'Новая бригада',transport:'public',shift:{start:540,end:1080},skills:['local'],start_location:'office',stock_mode:'plan',router:1,tv_box:0});reset();};
$('crew-form').onsubmit=e=>{e.preventDefault();run(async()=>{reset();status('Расчёт состава, дорог и нового плана…');draft=await api('/api/crew/preview',{version:state.version,engineers:collect()});$('comparison').textContent='Назначено: '+draft.before.assigned+' → '+draft.after.assigned+'. Без назначения: '+draft.before.unassigned+' → '+draft.after.unassigned+'. Инженеров на линии: '+draft.after.engineers_used+'.';$('unassigned').textContent=draft.unassigned.map(x=>x.request_id+': '+x.reason).join(' ');$('issue').replaceChildren(...draft.issue.map(x=>node('p',x.name+': роутеры '+x.router+', ТВ '+x.tv_box)));$('plan-link').href=regionURL('/crew-plan?id='+encodeURIComponent(draft.id)+'&version='+state.version);$('preview').hidden=false;status('Проверьте план и выдачу. Состав пока не изменён.');});};
$('apply').onclick=()=>run(async()=>{await api('/api/crew/apply',{id:draft.id,version:state.version});location.href=regionURL('/');});
run(async()=>{state=await api('/api/crew');$('context').textContent=state.region+' · бригад сейчас '+state.engineers.length+(state.control_count?' · в контрольном дне '+state.control_count:'')+'. Ориентир, не обязательный штат.';state.engineers.forEach(row);status(state.editable?'Настройка до начала дня. Сначала расчёт, затем применение.':'День уже начат: состав доступен только для просмотра.');});
})();
