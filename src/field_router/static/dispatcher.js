/* Portable dispatcher: no backend, all supplied values enter the DOM as text. */
(function () {
  'use strict';
  const COLORS = ['#25765c','#506fbb','#a069b6','#bb7642','#318a99','#a45667','#637d32','#7768ac'];
  const TRANSPORT = {car:'Автомобиль',walk:'Пешком',bicycle:'Велосипед',public:'Общественный транспорт'};
  const SKILL = {local:'Локальные работы',connection:'Подключение',emergency:'Аварийные работы'};
  const STATUS = {FEASIBLE:'Допустимое решение',OPTIMAL:'Оптимальное решение',INFEASIBLE:'Решение не найдено',
    empty:'Нет заявок',sequential_complete:'Последовательное назначение завершено',
    feasible_search_completed:'Допустимый план после поиска',fallback_baseline_no_solution:'Резервный базовый план',
    fallback_baseline_better:'Выбран лучший базовый план'};
  const SOLVERS = {baseline:'Базовый алгоритм',greedy:'Жадный алгоритм',ortools:'OR-Tools · поиск улучшений'};
  const KINDS = {travel:'Переезд',wait:'Ожидание',work:'Работа'};
  function timelineSegments(visit) {
    return [{kind:'travel',start:visit.arrival-visit.travel_minutes,end:visit.arrival},
      {kind:'wait',start:visit.arrival,end:visit.start},{kind:'work',start:visit.start,end:visit.end}]
      .filter(part => Number.isFinite(part.start) && Number.isFinite(part.end) && part.end > part.start);
  }
  function buildRecords(scenario, plan) {
    const engineers = new Map((scenario.engineers || []).map(e => [e.id,e]));
    const visits = new Map();
    (plan.routes || []).forEach((route,routeIndex) => (route.visits || []).forEach((visit,stopIndex) => {
      visits.set(visit.request_id,{route,visit,engineer:engineers.get(route.engineer_id),routeIndex,stopIndex});
    }));
    const unassigned = new Map((plan.unassigned || []).map(item => [item.request_id,item]));
    return (scenario.requests || []).map(request => ({request,...(visits.get(request.id) || {}),
      unassigned:unassigned.get(request.id)}));
  }
  function filterRecords(records, engineer, query) {
    const needle = String(query || '').trim().toLocaleLowerCase('ru');
    return records.filter(record => {
      if (engineer === '__unassigned__' && record.visit) return false;
      if (engineer && engineer !== '__unassigned__' && record.route?.engineer_id !== engineer) return false;
      const haystack = [record.request.id,record.request.address,record.request.locality,record.engineer?.name]
        .filter(Boolean).join(' ').toLocaleLowerCase('ru');
      return !needle || haystack.includes(needle);
    });
  }
  function validLocation(location) {
    return Boolean(location && typeof location.lat === 'number' && Number.isFinite(location.lat) &&
      typeof location.lon === 'number' && Number.isFinite(location.lon) &&
      Math.abs(location.lat) <= 90 && Math.abs(location.lon) <= 180);
  }
  function routeCoordinates(shape) {
    const coordinates = Array.isArray(shape?.coordinates) ? shape.coordinates : [];
    return coordinates.filter(pair => Array.isArray(pair) && validLocation({lat:pair[1],lon:pair[0]}))
      .map(pair => [pair[1],pair[0]]);
  }
  function nearestPointId(points, x, y) {
    let nearest = null, distance = Infinity;
    points.forEach(point => {
      const candidate = (point.x-x)**2 + (point.y-y)**2;
      if (Number.isFinite(candidate) && candidate < distance) { nearest=point.id;distance=candidate; }
    });
    return nearest;
  }
  function priorityLabel(priority) {
    return {urgent:'Авария · максимальный приоритет',high:'Подключение · после аварий'}[priority] || 'Обычный';
  }
  function prioritySummary(scenario, plan) {
    const top=(scenario.requests || []).filter(request=>['urgent','high'].includes(request.priority));
    const assigned=new Set((plan.routes || []).flatMap(route=>(route.visits || []).map(visit=>visit.request_id)));
    return {served:top.filter(request=>assigned.has(request.id)).length,total:top.length};
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {timelineSegments,buildRecords,filterRecords,validLocation,routeCoordinates,nearestPointId,priorityLabel,prioritySummary};
    return;
  }
  const $ = id => document.getElementById(id);
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  }
  function time(value) {
    if (!Number.isFinite(value)) return '—';
    return String(Math.floor(value / 60)).padStart(2,'0') + ':' + String(value % 60).padStart(2,'0');
  }
  const number = value => Number.isFinite(value) ? value : 0;
  const km = value => (number(value) / 1000).toLocaleString('ru-RU',{minimumFractionDigits:1,maximumFractionDigits:1});
  const windows = request => (request.windows || []).map(w => time(w.start)+'–'+time(w.end)).join(', ') || 'Не указаны';
  const color = record => record.route ? COLORS[record.routeIndex % COLORS.length] : '#ac7b38';
  let payload;
  try { payload = JSON.parse($('dispatcher-data').textContent); }
  catch (_) { $('notice').textContent = 'Не удалось прочитать данные отчёта. Сформируйте файл повторно.'; return; }
  const requestName = id => payload.request_labels?.[id] || (String(id).startsWith('emergency:') ? 'Авария' : id);
  const scenario = payload.scenario || {};
  const plans = payload.plans || [];
  const geo = payload.geography || {};
  const locations = geo.locations || {};
  const realGeography = Object.values(locations).some(validLocation);
  let plan = plans[0] || {routes:[],unassigned:[],metrics:{}};
  let records = [], filtered = [], selected = null;
  let map = null, mapLayer = null, mapBounds = null;
  const markers = new Map();
  let tileState = 'Подложка OpenStreetMap загружается через интернет.';
  let geoStatus = '';
  const usingLeaflet = realGeography && Boolean(window.L);

  $('scenario-date').textContent = scenario.date || 'Дата не указана';
  $('scenario-region').textContent = scenario.region || 'Регион не указан';
  $('scenario-description').textContent = 'Офис: '+(scenario.office_address || 'не указан')+' · Сценарий '+(scenario.id || 'без номера');
  const notice = $('notice');
  notice.classList.toggle('geo',realGeography);
  notice.textContent = realGeography
    ? 'Географические координаты из указанных источников. Сплошные линии — дорожная геометрия; пунктир — оценка. Время передвижения зависит от модели и не гарантирует актуальные пробки или расписание транспорта.'
    : 'Демо: условные координаты и время в пути. Реальные дороги и пробки не учтены.';
  if (!plans.length) notice.textContent += ' Планы отсутствуют: доступны исходные заявки.';
  if (plans.length) plans.forEach((item,index) => {
    const option = node('option',SOLVERS[item.solver] || item.solver || 'План '+(index+1));
    option.value = String(index); $('plan-select').append(option);
  });
  else { const option=node('option','Планы отсутствуют'); option.value='0'; $('plan-select').append(option); $('plan-select').disabled=true; }
  (scenario.engineers || []).forEach(engineer => {
    const option=node('option',engineer.name || engineer.id); option.value=engineer.id; $('engineer-select').append(option);
  });
  const unassignedOption=node('option','Без назначения'); unassignedOption.value='__unassigned__'; $('engineer-select').append(unassignedOption);

  if (usingLeaflet) {
    map = window.L.map('map',{scrollWheelZoom:false}).setView([55.75,37.62],10);
    mapLayer = window.L.layerGroup().addTo(map);
    const tiles = window.L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{
      maxZoom:19,attribution:'© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors · <a href="https://www.openstreetmap.org/fixthemap">Исправить карту</a>'}).addTo(map);
    tiles.on('tileerror',()=>{ tileState='Подложка недоступна. Точки и маршруты сохранены; для карты нужен интернет.'; updateMapStatus(); });
    tiles.on('tileload',()=>{ tileState='Подложка © OpenStreetMap · нужна сеть.'; updateMapStatus(); });
  }
  function updateMapStatus() { $('map-status').textContent = geoStatus + (usingLeaflet ? ' '+tileState : ''); }
  function empty(target,text) { target.replaceChildren(node('p',text,'empty')); }
  function stat(value,label,warn) { const item=node('div',null,'metric'+(warn?' warn':'')); item.append(node('strong',value),node('span',label)); return item; }
  function renderMetrics() {
    const m=plan.metrics || {};
    $('metrics').replaceChildren(stat(m.assigned ?? 0,'Заявок назначено'),stat(m.unassigned ?? records.filter(r=>!r.visit).length,'Без назначения',number(m.unassigned)>0),
      stat(m.engineers_used ?? 0,'Инженеров на линии'),stat(km(m.distance_m),'Километров по модели'),
      stat(number(m.travel_minutes)+' мин','Время в пути'),stat(number(plan.elapsed_seconds).toFixed(2)+' с','Расчёт плана'));
    $('plan-status').textContent = STATUS[plan.status] || plan.status || 'Расчёт не выполнен';
    if (plan.solver === 'ortools') $('plan-status').textContent += ' · Оптимальность не гарантируется';
    const top=prioritySummary(scenario,plan);
    $('plan-status').textContent += ' · Аварии и подключения: '+top.served+' / '+top.total+'. Приоритет: авария → подключение → ремонт / дозаказ.';
  }
  function renderList() {
    $('request-count').textContent=String(filtered.length);
    const container=$('request-list'); container.replaceChildren();
    if (!filtered.length) { empty(container,records.length?'По фильтрам ничего не найдено.':'В сценарии нет заявок.'); return; }
    filtered.forEach(record => {
      const request=record.request;
      const button=node('button',null,'request-item'); button.type='button'; button.dataset.requestId=request.id;
      button.style.setProperty('--route-color',color(record)); button.setAttribute('aria-pressed',String(selected===request.id));
      const row=node('span',null,'row'); row.append(node('span',requestName(request.id),'request-id'),
        node('span',record.visit?time(record.visit.start):'Не назначена','request-state'+(record.visit?'':' unassigned')));
      button.append(row,node('strong',request.address || 'Адрес не указан'),
        node('small',windows(request)+' · '+number(request.duration_minutes)+' мин'),
        node('small',record.engineer?.name || (record.visit?record.route.engineer_id:'Нет инженера')));
      button.addEventListener('click',()=>select(request.id,true)); container.append(button);
    });
  }
  function fact(list,label,value,className) {
    const item=node('div'); item.append(node('dt',label),node('dd',value,className)); list.append(item);
  }
  function renderCard() {
    const record=records.find(item=>item.request.id===selected);
    const container=$('request-card'); container.replaceChildren();
    if (!record) { empty(container,'Выберите заявку в списке, на карте или в расписании.'); return; }
    const request=record.request, visit=record.visit;
    container.append(node('div',requestName(request.id),'card-id'),node('h3',request.address || 'Адрес не указан','card-address'),
      node('div',visit?'Назначена · '+(record.engineer?.name || record.route.engineer_id):'Без назначения','card-state'+(visit?'':' unassigned')));
    const list=node('dl',null,'facts');
    fact(list,'Окно клиента',windows(request));
    fact(list,'Продолжительность / тип работ',number(request.duration_minutes)+' мин · '+(SKILL[request.skill] || request.skill || '—'));
    if (visit) {
      fact(list,'Прибытие → начало → окончание',time(visit.arrival)+' → '+time(visit.start)+' → '+time(visit.end));
      fact(list,'Предыдущий переезд / ожидание',number(visit.travel_minutes)+' мин · '+km(visit.distance_m)+' км / '+Math.max(0,visit.start-visit.arrival)+' мин');
      fact(list,'Инженер / транспорт',(record.engineer?.name || record.route.engineer_id)+' · '+(TRANSPORT[record.engineer?.transport] || record.engineer?.transport || '—'));
      if (record.engineer?.transport === 'public') {
        const index=record.route.visits.findIndex(v=>v.request_id===request.id);
        const origin=index>0?record.route.visits[index-1].request_id:record.engineer.start_location;
        const aliases=geo.location_aliases || {};
        const trip=geo.matrices?.public_journeys?.[aliases[origin] || origin]?.[aliases[request.id] || request.id];
        if (trip && trip.minutes === visit.travel_minutes && trip.metres === visit.distance_m) {
          const labels={walk:'Пешком',wait:'Ожидание',metro:'Метро',transfer:'Пересадка',estimate:'Средняя оценка транспорта'};
          fact(list,'Состав переезда',trip.legs.map(x=>(labels[x.mode] || x.mode)+' '+x.minutes+' мин: '+x.origin+' → '+x.destination).join('; ') || 'В той же точке');
          fact(list,'Точность транспорта',trip.kind==='walk'?'Пеший путь по дорожной матрице':trip.kind==='fallback'?'Вне покрытия метро: усреднённая оценка без известного маршрута':'Неполная сеть метро; время, подходы и пересадки — оценки без расписания');
        } else fact(list,'Состав переезда','Для этого участка детализация отсутствует; время указано по модели плана.');
      }
      fact(list,'Обоснование назначения',visit.explanation || 'Допустимо по ограничениям модели');
    } else fact(list,'Причина',record.unassigned?.reason || (plans.length?'В плане нет назначения этой заявки.':'План ещё не рассчитан.'),'reason');
    const equipment=request.equipment || {};
    fact(list,'Оборудование для заявки','Роутеры: '+number(equipment.router)+' · ТВ-приставки: '+number(equipment.tv_box));
    fact(list,'Приоритет / обязательный транспорт',priorityLabel(request.priority)+' / '+(TRANSPORT[request.required_transport] || request.required_transport || 'Любой'));
    const location=locations[request.id];
    if (validLocation(location)) fact(list,'Координаты / качество',location.lat.toFixed(5)+', '+location.lon.toFixed(5)+' · '+({approximate:'Приблизительное расположение',manual:'Ручной выбор',building_match:'Совпадение здания'}[location.quality] || 'не указано'));
    if (location?.quality === 'approximate') { fact(list,'Использованная точка',location.label); fact(list,'Допущение',location.approximation_reason,'reason'); }
    fact(list,'Источник координат',location?.source || (realGeography?'Координаты этой заявки отсутствуют':'Синтетические точки сценария'));
    if (request.source_line != null) fact(list,'Строка исходных данных',request.source_line);
    container.append(list);
    if (request.provenance && Object.keys(request.provenance).length) {
      const details=node('details',null,'provenance');details.append(node('summary','Происхождение полей'),node('pre',JSON.stringify(request.provenance,null,2)));container.append(details);
    }
  }
  function renderSchedule() {
    const target=$('schedule');target.replaceChildren();
    const relevant=(plan.routes || []).filter(route=>filtered.some(r=>r.route===route));
    if (!relevant.length) {empty(target,'В выбранном наборе нет назначенных выездов.');return;}
    const finite=values=>values.filter(Number.isFinite);
    const starts=finite(relevant.flatMap(route=>{
      const engineer=(scenario.engineers || []).find(e=>e.id===route.engineer_id);
      return [engineer?.shift?.start,...(route.visits || []).map(v=>v.arrival-v.travel_minutes)];
    }));
    const ends=finite(relevant.flatMap(route=>{
      const engineer=(scenario.engineers || []).find(e=>e.id===route.engineer_id);
      return [engineer?.shift?.end,...(route.visits || []).map(v=>v.end)];
    }));
    const start=Math.floor(Math.min(...starts,540)/60)*60;
    const end=Math.max(start+60,Math.ceil(Math.max(...ends,1080)/60)*60);
    const span=end-start;
    const timeline=node('div',null,'timeline'), axis=node('div',null,'time-axis'), ticks=node('div',null,'ticks');
    axis.append(node('span','Инженер / транспорт'),ticks);
    const step=Math.max(60,Math.ceil(span/600)*60);
    for(let minute=start;minute<=end;minute+=step){const tick=node('span',time(minute),'tick');tick.style.left=((minute-start)/span*100)+'%';ticks.append(tick);}
    timeline.append(axis);
    relevant.forEach(route=>{
      const engineer=(scenario.engineers || []).find(e=>e.id===route.engineer_id);
      const row=node('div',null,'engineer-row'), label=node('div',engineer?.name || route.engineer_id,'engineer-label');
      label.append(node('small',(TRANSPORT[engineer?.transport] || engineer?.transport || '—')+' · '+km(route.distance_m)+' км'));
      const track=node('div',null,'track');
      filtered.filter(r=>r.route===route).forEach(record=>timelineSegments(record.visit).forEach(segment=>{
        const button=node('button',segment.kind==='work'?requestName(record.request.id):'','segment '+segment.kind);
        button.type='button';button.dataset.requestId=record.request.id;
        button.style.left=((segment.start-start)/span*100)+'%';button.style.width=((segment.end-segment.start)/span*100)+'%';
        button.style.setProperty('--route-color',color(record));
        const description=KINDS[segment.kind]+' · '+requestName(record.request.id)+' · '+time(segment.start)+'–'+time(segment.end)+' · '+record.request.address;
        button.title=description;button.setAttribute('aria-label',description);button.setAttribute('aria-pressed',String(record.request.id===selected));
        button.addEventListener('click',()=>select(record.request.id,true));track.append(button);
      }));
      row.append(label,track);timeline.append(row);
    });
    target.append(timeline);
  }
  function renderEquipment() {
    const target=$('equipment');target.replaceChildren();
    const table=node('table'),head=node('thead'),header=node('tr');
    ['Инженер','Роутеры','ТВ-приставки'].forEach(text=>header.append(node('th',text)));head.append(header);table.append(head);
    const body=node('tbody');let routers=0,boxes=0;
    (plan.routes || []).forEach(route=>{
      const engineer=(scenario.engineers || []).find(e=>e.id===route.engineer_id);
      const row=node('tr'),eq=route.loadout || {};routers+=number(eq.router);boxes+=number(eq.tv_box);
      [engineer?.name || route.engineer_id,number(eq.router),number(eq.tv_box)].forEach(value=>row.append(node('td',value)));body.append(row);
    });
    const total=node('tr');['Итого по всему плану',routers,boxes].forEach(value=>total.append(node('td',value)));body.append(total);table.append(body);target.append(table);
  }
  function renderProvenance() {
    const target=$('provenance');target.replaceChildren();
    target.append(node('p','Возвращение в офис не включено в маршруты MVP. Загрузка показана для всего выбранного плана и не меняется при поиске заявки.'));
    if(plan.solver==='ortools')target.append(node('p','Математическая оптимальность не гарантируется: поиск ограничен по времени.'));
    const assumptions=node('ul');(scenario.assumptions || []).forEach(value=>assumptions.append(node('li',value)));target.append(assumptions);
    for(const [label,value] of [['Проверка плана',payload.validation || {}],['Источники передвижения',geo.modes || {}],
      ['Параметры передвижения',scenario.travel_config || {}],['Метаданные',scenario.metadata || {}],
      ['Диагностика алгоритма',{solver:plan.solver,status:plan.status,...(plan.diagnostics || {})}]]){
      const details=node('details');details.append(node('summary',label),node('pre',JSON.stringify(value,null,2)));target.append(details);
    }
  }
  function activeRoutes() {return (plan.routes || []).filter(route=>filtered.some(record=>record.route===route));}
  function renderLegend() {
    const target=$('route-legend');target.replaceChildren();
    activeRoutes().forEach(route=>{
      const record=records.find(item=>item.route===route),item=node('span',null,'legend-item'),swatch=node('span',null,'swatch');
      swatch.style.background=color(record);item.append(swatch,node('span',record.engineer?.name || route.engineer_id));target.append(item);
    });
    const item=node('span','Пунктир — оценка, не дорожный маршрут','muted');target.append(item);
  }
  function shapeFor(route) { return geo.route_shapes?.[plan.solver]?.[route.engineer_id]; }
  function routeLocationIds(route) {
    const engineer=(scenario.engineers || []).find(e=>e.id===route.engineer_id);
    return [engineer?.start_location || 'office',...(route.visits || []).map(v=>v.request_id)];
  }
  function renderMap() {
    markers.clear();renderLegend();
    const located=filtered.filter(record=>realGeography?validLocation(locations[record.request.id]):
      Number.isFinite(scenario.points?.[record.request.id]?.x_km)&&Number.isFinite(scenario.points?.[record.request.id]?.y_km));
    geoStatus='На '+(realGeography?'карте':'схеме')+': '+located.length+' из '+filtered.length+' заявок.';
    if(located.length<filtered.length)geoStatus+=' У остальных нет координат; они доступны в списке.';
    if(usingLeaflet) {
      $('map-title').textContent='Карта выездов';mapLayer.clearLayers();const bounds=[];
      activeRoutes().forEach(route=>{
        const record=records.find(r=>r.route===route),shape=shapeFor(route);
        const coords=routeCoordinates(shape);
        const road=shape?.kind==='road'&&coords.length>1;
        const line=coords.length>1?coords:routeLocationIds(route).filter(id=>validLocation(locations[id])).map(id=>[locations[id].lat,locations[id].lon]);
        if(line.length>1){const layer=window.L.polyline(line,{color:color(record),weight:3,opacity:.7,dashArray:road?null:'7 7'}).addTo(mapLayer);
          layer.bindTooltip(node('span',(road?'Дорожная геометрия':'Оценочная линия')+' · '+(shape?.source || 'соединение координат')));bounds.push(...line);}
      });
      const starts=new Set(activeRoutes().map(route=>(scenario.engineers || []).find(e=>e.id===route.engineer_id)?.start_location || 'office'));
      starts.forEach(id=>{const location=locations[id];if(validLocation(location)){const marker=window.L.circleMarker([location.lat,location.lon],{radius:6,color:'#fff',weight:2,fillColor:'#243d32',fillOpacity:1}).addTo(mapLayer);
        marker.bindTooltip(node('span','Старт: '+(location.label || scenario.office_address || id)));bounds.push([location.lat,location.lon]);}});
      located.forEach(record=>{
        const location=locations[record.request.id],marker=window.L.circleMarker([location.lat,location.lon],{
          radius:record.request.id===selected?9:6,color:record.request.id===selected?'#18352c':'#fff',weight:2,fillColor:color(record),fillOpacity:1}).addTo(mapLayer);
        marker.bindTooltip(node('span',requestName(record.request.id)+' · '+record.request.address));
        marker.on('click',()=>select(record.request.id,false));
        const element=marker.getElement();if(element){element.setAttribute('tabindex','0');element.setAttribute('role','button');element.setAttribute('aria-label','Открыть заявку '+requestName(record.request.id)+' · '+record.request.address);
          element.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();select(record.request.id,false);}});}
        markers.set(record.request.id,marker);bounds.push([location.lat,location.lon]);
      });
      mapBounds=bounds.length?window.L.latLngBounds(bounds):null;fitMap();
    } else {
      $('map-title').textContent=realGeography?'Схема координат · карта недоступна':'Синтетическая схема';
      if(realGeography)geoStatus+=' Leaflet не загружен: показана схема координат без подложки.';
      else geoStatus+=' Условные точки, не географическая карта.';
      geoStatus+=' Совпадающие точки выбирайте через список или поиск.';
      renderSchematic(located);
    }
    updateMapStatus();
  }
  function svgNode(tag,attributes,text) {
    const element=document.createElementNS('http://www.w3.org/2000/svg',tag);
    Object.entries(attributes || {}).forEach(([key,value])=>element.setAttribute(key,String(value)));
    if(text!=null)element.textContent=String(text);return element;
  }
  function renderSchematic(located) {
    const target=$('map');target.replaceChildren();
    if(!located.length){target.append(node('div','Нет координат для отображения выбранных заявок.','map-empty'));return;}
    const points=new Map();
    const get=id=>{
      if(realGeography){const value=locations[id];return validLocation(value)?[value.lon*Math.cos(value.lat*Math.PI/180),-value.lat]:null;}
      const value=scenario.points?.[id];return value&&Number.isFinite(value.x_km)&&Number.isFinite(value.y_km)?[value.x_km,-value.y_km]:null;
    };
    located.forEach(r=>points.set(r.request.id,get(r.request.id)));
    activeRoutes().forEach(route=>routeLocationIds(route).forEach(id=>{const point=get(id);if(point)points.set(id,point);}));
    const xs=[...points.values()].map(p=>p[0]),ys=[...points.values()].map(p=>p[1]);
    const minX=Math.min(...xs),minY=Math.min(...ys),dx=Math.max(...xs)-minX,dy=Math.max(...ys)-minY;
    const scale=Math.min(540/(dx||1),300/(dy||1));
    const project=point=>[50+(540-dx*scale)/2+(point[0]-minX)*scale,40+(300-dy*scale)/2+(point[1]-minY)*scale];
    const svg=svgNode('svg',{viewBox:'0 0 640 380',class:'schematic','aria-label':realGeography?'Схема географических координат':'Условная синтетическая схема'});
    for(let x=40;x<640;x+=40)svg.append(svgNode('line',{x1:x,y1:0,x2:x,y2:380,stroke:'#dce4d8','stroke-width':.5}));
    for(let y=20;y<380;y+=40)svg.append(svgNode('line',{x1:0,y1:y,x2:640,y2:y,stroke:'#dce4d8','stroke-width':.5}));
    activeRoutes().forEach(route=>{
      const record=records.find(r=>r.route===route),line=routeLocationIds(route).map(id=>points.get(id)).filter(Boolean).map(project);
      if(line.length>1)svg.append(svgNode('polyline',{points:line.map(p=>p.join(',')).join(' '),fill:'none',stroke:color(record),'stroke-width':2,'stroke-dasharray':'6 5',opacity:.7}));
    });
    const starts=new Set(activeRoutes().map(route=>(scenario.engineers || []).find(e=>e.id===route.engineer_id)?.start_location || 'office'));
    starts.forEach(id=>{const point=points.get(id);if(point){const[x,y]=project(point);svg.append(svgNode('rect',{x:x-5,y:y-5,width:10,height:10,fill:'#274438',rx:2}),svgNode('text',{x:x+10,y:y+3},'Старт'));}});
    const projectedPoints=located.map(record=>{
      const [x,y]=project(points.get(record.request.id));return {id:record.request.id,x,y};
    });
    function selectPointer(event, fallbackId) {
      // Circle hit order is unrelated to nearest centre when markers overlap.
      // Transform the actual pointer into viewBox coordinates before choosing.
      const matrix=svg.getScreenCTM();
      if(event.detail===0 || !matrix){select(fallbackId,false);return;}
      const point=svg.createSVGPoint();point.x=event.clientX;point.y=event.clientY;
      const local=point.matrixTransform(matrix.inverse());
      select(nearestPointId(projectedPoints,local.x,local.y) ?? fallbackId,false);
    }
    located.forEach(record=>{
      const[x,y]=project(points.get(record.request.id));
      const group=svgNode('g',{class:'point','data-request-id':record.request.id});
      const hotspot=svgNode('circle',{cx:x,cy:y,r:8,fill:color(record),stroke:'#fff','stroke-width':2,
        tabindex:0,role:'button','aria-label':'Открыть заявку '+requestName(record.request.id)+' · '+record.request.address});
      group.append(hotspot,svgNode('text',{x:x+12,y:y+4},requestName(record.request.id)));
      // A circle is the only interactive element: labels never enlarge its hitbox.
      hotspot.addEventListener('click',event=>selectPointer(event,record.request.id));
      hotspot.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();select(record.request.id,false);}});
      svg.append(group);markers.set(record.request.id,group);
    });
    target.append(svg);
  }
  function fitMap() { if(map&&mapBounds){map.invalidateSize();map.fitBounds(mapBounds,{padding:[32,32],maxZoom:14});} }
  function select(id,pan) {
    selected=id;renderCard();
    document.querySelectorAll('[data-request-id]').forEach(element=>{
      const active=element.dataset.requestId===id;element.classList.toggle('selected',active);
      if(element.tagName==='BUTTON')element.setAttribute('aria-pressed',String(active));
    });
    if(map)markers.forEach((marker,key)=>marker.setStyle({radius:key===id?9:6,color:key===id?'#18352c':'#fff',weight:key===id?3:2}));
    const marker=markers.get(id);if(map&&marker&&pan){map.panTo(marker.getLatLng());marker.openTooltip();}
    const listItem=[...$('request-list').children].find(element=>element.dataset.requestId===id);
    if(listItem)listItem.scrollIntoView({block:'nearest',inline:'nearest'});
  }
  function refresh(resetPlan) {
    if(resetPlan){plan=plans[Number($('plan-select').value)] || {routes:[],unassigned:[],metrics:{}};records=buildRecords(scenario,plan);renderMetrics();renderEquipment();renderProvenance();}
    filtered=filterRecords(records,$('engineer-select').value,$('request-search').value);
    if(!filtered.some(record=>record.request.id===selected))selected=filtered[0]?.request.id || null;
    $('filter-summary').textContent='Показано '+filtered.length+' из '+records.length+' заявок';
    renderList();renderSchedule();renderMap();select(selected,false);
  }
  $('plan-select').addEventListener('change',()=>refresh(true));
  $('engineer-select').addEventListener('change',()=>refresh(false));
  $('request-search').addEventListener('input',()=>refresh(false));
  $('reset-filters').addEventListener('click',()=>{$('engineer-select').value='';$('request-search').value='';refresh(false);});
  $('fit-map').addEventListener('click',fitMap);
  refresh(true);
})();
