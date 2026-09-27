/* Independent GPS dashboard. No plan mutations, HTML interpolation or model calls. */
(() => {
  'use strict';
  const hhmm = value => {if(!Number.isFinite(value))return '—';const day=Math.floor(value/1440),m=((value%1440)+1440)%1440;return String(Math.floor(m/60)).padStart(2,'0')+':'+String(m%60).padStart(2,'0')+(day?' ('+(day>0?'+':'')+day+' дн.)':'');};
  const availableTask = item => item.point && item.visit && !['completed','completion_pending'].includes(item.status);
  function planComparison(before, after, requestId) {
    const oldTask=before.tasks.find(t=>t.id===requestId),newTask=after.tasks.find(t=>t.id===requestId);
    const labels={available:'Свободен',traveling:'В пути',working:'Работает',unavailable:'Недоступен',completed:'Завершил работу',completion_pending:'Ждёт подтверждения',interrupted:'Работа прервана'};
    const status = data => {
      const s=data.status;
      return s ? (labels[s.status] || s.status)+(s.request_id ? ' · № '+s.request_id : '')+' · '+hhmm(s.time)
        +(s.remaining_minutes ? ' · осталось '+s.remaining_minutes+' мин' : '') : 'Нет отметки';
    };
    const assignment = task => !task ? 'Нет у этого инженера' : task.status==='completed' ? 'Завершена'
      : task.status==='completion_pending' ? 'Ожидает подтверждения выполнения' : 'Назначена';
    const visit = task => task?.visit ? hhmm(task.visit.start)+'–'+hhmm(task.visit.end) : '—';
    const windows = task => task ? task.windows.map(w=>hhmm(w.start)+'–'+hhmm(w.end)).join(' или ') : '—';
    const route = data => data.tasks.filter(availableTask).map(t=>'№ '+t.id).join(' → ') || 'Нет заявок';
    const shift = data => hhmm(data.engineer.shift.start)+'–'+hhmm(data.engineer.shift.end);
    const all=[
      ['Версия данных',String(before.version),String(after.version)],
      ['Дата плана',before.date,after.date],
      ['Назначение № '+requestId,assignment(oldTask),assignment(newTask)],
      ['Работа по плану',visit(oldTask),visit(newTask)],
      ['Окна клиента',windows(oldTask),windows(newTask)],
      ['Длительность работы, мин',String(oldTask?.duration_minutes ?? '—'),String(newTask?.duration_minutes ?? '—')],
      ['Статус инженера',status(before),status(after)],
      ['Смена',shift(before),shift(after)],
      ['Порядок заявок',route(before),route(after)]
    ];
    const selectedUnchanged=JSON.stringify(oldTask && [assignment(oldTask),oldTask.visit,oldTask.windows,oldTask.duration_minutes,oldTask.point])
      ===JSON.stringify(newTask && [assignment(newTask),newTask.visit,newTask.windows,newTask.duration_minutes,newTask.point]);
    return {rows:all.filter(row=>row[1]!==row[2]), selectedUnchanged,
      canReplay:!!newTask && !!availableTask(newTask),
      updates:(after.plan_updates || []).filter(v=>v.version>before.version && v.version<=after.version)};
  }
  if(typeof module!=='undefined' && module.exports)module.exports={planComparison};
  if(typeof document==='undefined')return;
  const $ = id => document.getElementById(id);
  const appFetch = (...args) => window.FieldRouter ? window.FieldRouter.fetch(...args) : fetch(...args);
  const appUrl = path => window.FieldRouter ? window.FieldRouter.url(path) : path;
  if (!$('panel-gps')) return;
  let visible = false, busy = false, channel = 'device', generation = 0, map = null, markers = null, points = [];
  let snapshot = null, optionsKey = '';
  let planning = null, planningBusy = false;
  const excludedSelection = new Set();
  const states = {not_started: 'GPS не включён', active: 'Передаёт координаты', paused: 'Передача на паузе', off: 'GPS выключен', denied: 'Нет разрешения', unavailable: 'Ищет координату'};
  function el(tag, text, className) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; }
  function error(text) { $('gps-dashboard-error').textContent = text; $('gps-dashboard-error').hidden = !text; }
  async function request(path, body) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await appFetch(appUrl(path), {signal: controller.signal, ...(body ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {})});
      const data = await response.json();
      if (!response.ok) {
        const failure=new Error(data.error || 'GPS временно недоступен');failure.status=response.status;throw failure;
      }
      return data;
    } finally { clearTimeout(timer); }
  }
  const fresh = (row, config) => row.tracking_state === 'active' && row.point_age_seconds !== null && row.point_age_seconds <= config.stale_seconds && !row.alerts.some(a => a.code === 'quality');
  function metric(title, count, total, detail, className) {
    const card = el('article', undefined, 'gps-metric ' + (className || ''));
    const percent = total ? Math.round(count / total * 100) : 0;
    card.title = detail; card.append(el('h3', title), el('strong', percent + '%'), el('p', count + ' из ' + total + ' инженеров'));
    const progress = el('progress'); progress.max = total || 1; progress.value = count; progress.setAttribute('aria-label', title + ': ' + count + ' из ' + total);
    const help = el('details'); help.append(el('summary', 'Что это значит'), el('p', detail)); card.append(progress, help); return card;
  }
  function intent(action, eid) { window.dispatchEvent(new CustomEvent('gps-intent', {detail: {action, engineerId: eid}})); }
  function exclusionError(message) {$('gps-exclusion-error').textContent=message;$('gps-exclusion-error').hidden=!message;}
  function syncExclusions() {
    const excluded=new Set(planning?.excludedEngineerIds || []);
    const roster=planning?.engineers || [];
    for(const id of excludedSelection)if(excluded.has(id) || !roster.some(e=>e.id===id))excludedSelection.delete(id);
    $('gps-exclusion-run').disabled=planningBusy || !planning || !excludedSelection.size;
    $('gps-exclusion-run').textContent='Пересчитать без выбранных ('+excludedSelection.size+')';
    $('gps-exclusion-selection').textContent=excludedSelection.size
      ? 'Выбраны: '+roster.filter(e=>excludedSelection.has(e.id)).map(e=>e.name).join(', ')
      : 'Выберите сотрудников в карточках ниже.';
    $('gps-excluded-now').hidden=!excluded.size;
    $('gps-excluded-now').textContent='Уже исключены из планирования: '+roster.filter(e=>excluded.has(e.id)).map(e=>e.name).join(', ')+'. Для возвращения запишите новый факт «Свободен» и пересчитайте план.';
    $('gps-people').querySelectorAll('[data-exclude-engineer]').forEach(input=>{
      input.checked=excludedSelection.has(input.dataset.excludeEngineer);
      input.disabled=planningBusy || !planning || excluded.has(input.dataset.excludeEngineer);
    });
    $('gps-people').querySelectorAll('[data-exclude-one]').forEach(button=>{
      const blocked=excluded.has(button.dataset.excludeOne);
      button.disabled=planningBusy || !planning || blocked;
      button.textContent=blocked?'Уже исключён из планирования':'Пересчитать без него';
    });
  }
  function setPlanning(data) {
    const runChanged = (planning?.demoRunId || null) !== (data.demoRunId || null);
    if(planning && !runChanged && data.version<planning.version)return;
    if(runChanged) {
      generation++; snapshot = null; optionsKey = ''; replayState = null; taskLoad++;
      historyCursor = null; historyEngineer = null;
      $('gps-people').replaceChildren(); $('gps-metrics').replaceChildren();
      $('gps-trace').textContent = ''; $('gps-diagnostics').textContent = '';
      $('gps-replay-changes').hidden = true; $('gps-replay-task').replaceChildren();
      $('gps-history-next').hidden = true;
      markers?.clearLayers(); excludedSelection.clear(); replayControls();
    }
    if(runChanged || planning?.version!==data.version) {
      excludedSelection.clear();
      $('gps-exclusion-time').value=String(Math.floor(data.time/60)).padStart(2,'0')+':'+String(data.time%60).padStart(2,'0');
    }
    planning=data;syncExclusions();
    if(runChanged && visible) refresh();
  }
  window.addEventListener('planning-state',event=>setPlanning(event.detail));
  window.addEventListener('planning-busy',event=>{planningBusy=event.detail.busy;syncExclusions();});
  function recalculateWithout(ids) {
    exclusionError('');
    if(!planning || planningBusy)return;
    if(snapshot && snapshot.plan_version!==planning.version) {
      exclusionError('План изменился. Обновите страницу перед расчётом.');return;
    }
    const value=$('gps-exclusion-time').value;
    if(!/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) {exclusionError('Укажите время пересчёта.');return;}
    const [hour,minute]=value.split(':').map(Number);
    if(hour*60+minute<planning.time) {exclusionError('Время пересчёта не может быть раньше времени текущего плана.');return;}
    window.dispatchEvent(new CustomEvent('gps-intent',{detail:{action:'exclude',engineerIds:ids,time:value,version:planning.version}}));
  }
  $('gps-exclusion-run').onclick=()=>recalculateWithout([...excludedSelection]);
  $('gps-exclusion-clear').onclick=()=>{excludedSelection.clear();syncExclusions();exclusionError('');};
  function person(row, config) {
    const warn = row.alerts.some(a => a.level === 'warning');
    const card = el('article', undefined, 'gps-person' + (warn ? ' gps-warning' : ''));
    card.dataset.engineerId = row.engineer_id;
    const native=row.native_connection;
    const waiting=native && native.state==='awaiting' && ['off','not_started'].includes(row.tracking_state);
    card.append(el('h3', row.name), el('p', waiting ? 'Ожидаем подключение телефона' : states[row.tracking_state] || row.tracking_state, 'gps-subtle'));
    if(native) {
      const notes={awaiting:'Фоновое подключение подготовлено, но приложение ещё не прислало точку. Сотруднику нужно открыть приложение инженера и включить GPS.',
        low_accuracy:'Фоновое приложение связалось с сервером, но точность пока недостаточна. Работающий браузерный GPS сохраняется.',
        waiting_fix:'От фонового приложения ожидается свежая пригодная координата.',stale:'Фоновое приложение не присылает свежих координат. Проверьте связь и разрешение работы в фоне.',
        active:'Фоновая передача подтверждена полученной координатой.',off:'Фоновая передача остановлена.'};
      card.append(el('p',notes[native.state] || 'Проверяем фоновую передачу.','gps-subtle'));
    }
    const pick=el('label',undefined,'gps-exclusion-pick');
    const checkbox=el('input');checkbox.type='checkbox';checkbox.dataset.excludeEngineer=row.engineer_id;
    checkbox.onchange=()=>{if(checkbox.checked)excludedSelection.add(row.engineer_id);else excludedSelection.delete(row.engineer_id);syncExclusions();};
    pick.append(checkbox,el('span','Не учитывать при пересчёте'));card.append(pick);
    const age = row.point_age_seconds;
    card.append(el('p', age === null ? 'Надёжная координата ещё не получена.' : 'Координата ' + age + ' сек назад · точность ±' + Math.round(row.position.accuracy_m) + ' м', 'gps-age'));
    const analysis = row.analysis;
    if(row.assignment?.distance_m!=null) {
      const metres=row.assignment.distance_m;
      const value=metres>=1000?(metres/1000).toLocaleString('ru-RU',{maximumFractionDigits:1})+' км':Math.round(metres)+' м';
      card.append(el('p',(fresh(row,config)?'До заявки № ':'От показанной точки до заявки № ')+row.assignment.id+
        ': около '+value+' по прямой','gps-age'));
    }
    if (analysis?.target) card.append(el('p', (analysis.target.basis==='plan'?'Следующая по плану: № ':'К заявке № ') + analysis.target.id + ' · ' + analysis.target.address));
    if(row.clock)card.append(el('p',(channel==='demo'?'Тестовое время: ':'Время сервера: ')+hhmm(row.clock.minute)+(channel==='device'?' · '+row.clock.local_date:'')+' · план '+row.plan_date,'gps-subtle'));
    if (analysis?.eta) {
      card.append(el('p', 'Прибытие ориентировочно через ' + analysis.eta.low_minutes + '–' + analysis.eta.high_minutes + ' мин. '+analysis.eta.description, 'gps-subtle'));
      const schedule=analysis.eta.schedule;
      if(schedule)card.append(el('p','Начало по плану '+hhmm(schedule.planned_start)+
        ' · оценка прибытия '+hhmm(schedule.arrival_low)+'–'+hhmm(schedule.arrival_high)+
        ' · '+(schedule.slack_minutes>=0?'запас '+schedule.slack_minutes+' мин':'медленная оценка позже плана на '+(-schedule.slack_minutes)+' мин'),'gps-age'));
      const legs=analysis.eta.route?.legs;
      if(legs?.length) {
        const modes={walk:'Пешком',wait:'Ожидание',transfer:'Пересадка',metro:'Метро',rail:'Поезд',bus:'Автобус',tram:'Трамвай',car:'Автомобиль',bicycle:'Велосипед',public:'Транспорт'};
        const list=el('ol');
        for(const leg of legs) list.append(el('li',(modes[leg.mode]||leg.mode)+': '+leg.origin+' → '+leg.destination+' · '+leg.minutes+' мин'));
        card.append(list);
      }
    }
    if(row.last_accuracy_m>config.max_accuracy_m) card.append(el('p','Последнее измерение: погрешность ±'+Math.round(row.last_accuracy_m)+' м. Это не расстояние до заявки. В оценке маршрута оно не участвует.','gps-subtle'));
    if(row.source==='native') card.append(el('p','Данные с телефона инженера.','gps-subtle'));
    if (!row.date_matches) card.append(el('p', 'Дата плана отличается от сегодняшней: координаты и потеря связи проверяются, опоздания по старому плану не оцениваются. Для рабочего теста нужен план на сегодня; для исторического — тестовый режим с выбранным временем.', 'gps-subtle'));
    for (const item of row.alerts) {
      const box = el('div', undefined, 'gps-alert ' + item.level); box.append(el('h4', item.title), el('p', item.detail));
      const advice = el('details'); advice.open = item.level === 'warning'; advice.append(el('summary', 'Что можно сделать'), el('p', item.action)); box.append(advice); card.append(box);
    }
    if (!row.alerts.length && fresh(row, config) && row.date_matches) card.append(el('p',
      analysis ? analysis.target ? 'По текущей точке и выбранной заявке отклонений не обнаружено.'
        : 'Нет подходящей заявки для сравнения сейчас: проверьте назначение, смену и рабочий статус инженера.'
      : 'Свежая точка получена. Анализ ещё не готов.', 'gps-subtle'));
    if (row.alerts.length && channel === 'device') {
      const actions = el('div', undefined, 'gps-actions');
      for (const [label, action] of [['Уточнить статус', 'facts'], ['Проверить варианты плана', 'replan']]) {
        const button = el('button', label, 'secondary'); button.type = 'button'; button.onclick = () => intent(action, row.engineer_id); actions.append(button);
      }
      card.append(actions);
    }
    if(channel==='device' && fresh(row,config) && analysis?.eta && analysis?.target?.basis==='confirmed') {
      const delay=el('button','Уточнить доезд и пересчитать','secondary'); delay.type='button';
      delay.dataset.gpsDelay=row.engineer_id;
      delay.onclick=async()=>{
        delay.disabled=true;
        try {
          const draft=await request('/api/gps/delay',{engineer_id:row.engineer_id,version:snapshot.plan_version});
          window.dispatchEvent(new CustomEvent('gps-intent',{detail:{action:'delay',draft}}));
        }catch(e){error(e.message);}finally{delay.disabled=false;}
      };
      card.append(delay);
    }
    const recalculate=el('button','Пересчитать без него','secondary');
    recalculate.type='button';recalculate.dataset.excludeOne=row.engineer_id;
    recalculate.onclick=()=>{excludedSelection.clear();excludedSelection.add(row.engineer_id);syncExclusions();recalculateWithout([row.engineer_id]);};
    card.append(recalculate);
    return card;
  }
  function renderMap(rows, config) {
    if (!window.L) return;
    if (!map) {
      map = L.map('gps-map').setView([55.75, 37.62], 10);
      map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {referrerPolicy:'strict-origin-when-cross-origin', attribution: '© OpenStreetMap'}).addTo(map);
      markers = L.layerGroup().addTo(map);
    }
    markers.clearLayers(); points = [];
    for (const row of rows) {
      if (!row.position) continue;
      const point = [row.position.lat, row.position.lon]; points.push(point);
      const popup = el('div'); popup.append(el('strong', row.name), el('p', (fresh(row,config)?'Последняя принятая точка · ':'Показана последняя принятая точка, текущее положение требует проверки · ') + row.point_age_seconds + ' сек назад'), el('p', states[row.tracking_state]));
      const color = fresh(row, config) ? '#168365' : '#818998';
      L.circleMarker(point, {radius: 8, color, fillColor: color, fillOpacity: .85}).bindPopup(popup).addTo(markers);
      L.circle(point, {radius: row.position.accuracy_m, color, weight: 1, fillOpacity: .07}).addTo(markers);
    }
    map.invalidateSize();
  }
  function render(data) {
    snapshot = data;
    const rows = data.rows, total = rows.length, good = rows.filter(row => fresh(row, data.config)).length;
    const attention = rows.filter(row => row.alerts.some(a => a.level === 'warning')).length;
    const metrics = $('gps-metrics');
    // Preserve open explanations during background refresh.
    const opened = [...metrics.querySelectorAll('details')].map(d => d.open);
    metrics.replaceChildren(
      metric('Свежие координаты', good, total, 'Точка не старше ' + data.config.stale_seconds + ' сек, передача активна, точность достаточна. Это показатель видимости, а не подтверждение выполнения работ.'),
      metric('Нужна проверка', attention, total, 'У инженера есть хотя бы один сигнал: связь, качество координат или ситуация на маршруте. Откройте карточку ниже и уточните причину.', 'warning'),
      metric('Без свежей точки', total - good, total, 'Сюда входят выключенный GPS, пауза, неточность и устаревшие точки. Проверьте разрешение, интернет и открытое приложение. Это не означает отсутствие сотрудника.', 'muted'));
    metrics.querySelectorAll('details').forEach((d, i) => { d.open = !!opened[i]; });
    $('gps-context').textContent = channel === 'demo'
      ? 'ДЕМО · Искусственные ситуации на дате плана ' + data.plan_date + '. Реальные GPS-данные и факты не изменяются.'
      : 'Устройства · План от ' + data.plan_date + '. ' + (rows.some(r => !r.date_matches) ? 'Дата не совпадает с сегодняшней: проверяем связь и качество, не сравниваем GPS с прошлыми окнами.' : 'Сигналы сверяются с подтверждёнными статусами инженеров.');
    const key = rows.map(r => r.engineer_id + ':' + r.name).join('|');
    if (key !== optionsKey) {
      for (const id of ['gps-demo-engineer', 'gps-trace-engineer']) {
        const selected = $(id).value; $(id).replaceChildren(...rows.map(row => { const option = el('option', row.name + ' · ' + row.engineer_id); option.value = row.engineer_id; return option; }));
        if (rows.some(r => r.engineer_id === selected)) $(id).value = selected;
      }
      optionsKey = key;
      loadReplayTasks({preserve:true});
    }
    const list = $('gps-people');
    // Avoid replacing focused controls while the dispatcher interacts with a card.
    if (!list.contains(document.activeElement)) {
      const openedCards = new Map([...list.querySelectorAll('details')].map(d => [d.closest('[data-engineer-id]').dataset.engineerId + '|' + d.parentElement.querySelector('h4').textContent, d.open]));
      list.replaceChildren(...[...rows].sort((a, b) => Number(b.alerts.some(x => x.level === 'warning')) - Number(a.alerts.some(x => x.level === 'warning'))).map(row => person(row, data.config)));
      list.querySelectorAll('details').forEach(d => { const key = d.closest('[data-engineer-id]').dataset.engineerId + '|' + d.parentElement.querySelector('h4').textContent; if (openedCards.has(key)) d.open = openedCards.get(key); });
    }
    renderMap(rows, data.config);
    syncExclusions();
    const h = data.health;
    $('gps-model-notice').hidden = h.enabled && !h.last_error && !h.storage_error;
    $('gps-model-notice').textContent = !h.enabled ? 'GPS-модуль выключен. Планирование и рабочие статусы доступны.'
      : 'GPS-модель временно недоступна: прогнозы приостановлены. Связь и последние координаты видны; планирование продолжает работать.';
    if(h.storage_error) $('gps-model-notice').textContent='История GPS временно недоступна. Сервер просит устройства повторить отправку; планирование продолжает работать.';
    $('gps-health').textContent = !h.enabled ? 'Анализ GPS выключен.' : h.status === 'degraded'
      ? 'Модель временно недоступна. Проверки связи продолжаются; следующий запуск через ' + h.circuit_open_seconds + ' сек.'
      : h.status === 'idle' ? 'Модель готова к запуску при появлении координат.' : 'Анализ GPS работает. Последний успешный расчёт: ' + (h.last_success ? new Date(h.last_success * 1000).toLocaleTimeString('ru-RU', {hourCycle:'h23'}) : 'ещё выполняется');
    $('gps-diagnostics').textContent = JSON.stringify({health: h, measurements: rows.map(row=>({engineer:row.name,
      incoming_provider:row.measurement_provider, used_provider:row.position?.provider||'unknown',
      accepted_position_age_seconds:row.point_age_seconds, quality_reason:row.quality_reason,
      comparison:row.quality_detail,
      explanation:'distance_m в comparison — разница между измерениями телефона, не расстояние до заявки; minimum_speed_kmh — проверочный расчёт, не измеренная скорость сотрудника.'})),
      recent_events: data.recent_events, thresholds: data.config}, null, 2);
    $('gps-health').textContent += h.persistent ? ' История устройств сохраняется до '+Math.round(data.config.retention_seconds/86400*10)/10+' суток; демо хранится только в памяти.' : ' История хранится только в памяти.';
  }
  async function refresh() {
    if (!visible || document.hidden || busy) return;
    busy = true; const token = generation;
    try { const data = await request('/api/gps?channel=' + channel); if (token === generation) { error(''); render(data); } }
    catch (e) { if (token === generation) error('GPS: ' + (e.name === 'AbortError' ? 'сервер не ответил вовремя.' : e.message) + ' Остальные разделы работают независимо. Показанные GPS-данные могут быть устаревшими.'); }
    finally { busy = false; if (token !== generation && visible) setTimeout(refresh, 0); }
  }
  window.addEventListener('workbench-tab', event => {
    visible = event.detail.name === 'gps';
    if (visible) {
      refresh();setTimeout(() => map?.invalidateSize(),0);
      // A fast initial state request may have completed before this deferred script.
      if(!planning) {
        const token = generation;
        request('/api/state').then(data=>{
          if(token===generation)setPlanning({version:data.version,demoRunId:data.demo_run_id || null,time:data.time,engineers:data.engineers,excludedEngineerIds:data.excluded_engineer_ids || []});
        }).catch(e=>{if(token===generation)exclusionError(e.message);});
      }
    }
  });
  $('gps-channel').addEventListener('change', () => {
    channel = $('gps-channel').value; generation++; $('gps-demo-tools').hidden = channel !== 'demo';
    $('gps-people').replaceChildren(); $('gps-trace').textContent = ''; markers?.clearLayers(); refresh();
  });
  $('gps-demo-run').addEventListener('click', async () => {
    $('gps-demo-run').disabled = true; $('gps-demo-status').textContent = 'Готовим пример…';
    try {
      await request('/api/gps/demo', {engineer_id: $('gps-demo-engineer').value, case: $('gps-demo-case').value});
      $('gps-demo-status').textContent = 'Пример добавлен. Вывод модели появится после расчёта; первый запуск может занять несколько секунд.';
      refresh(); setTimeout(refresh, 1000);
    } catch (e) { $('gps-demo-status').textContent = e.message; }
    finally { $('gps-demo-run').disabled = false; }
  });
  let replayState = null, taskLoad = 0;
  const replayStatus = text => {$('gps-replay-status').textContent = text;};
  const task = () => replayState?.tasks.find(t => t.id === $('gps-replay-task').value && availableTask(t));
  function replayControls() {
    $('gps-replay-run').disabled=!task();
  }
  function showTarget(resetTime=true) {
    const selected = task();
    $('gps-replay-target').textContent = selected ? 'План № '+replayState.version+' · '+selected.address+
      ' · работа '+hhmm(selected.visit.start)+'–'+hhmm(selected.visit.end)+
      ' · окна '+selected.windows.map(w=>hhmm(w.start)+'–'+hhmm(w.end)).join(' или ')+
      ' · координаты: '+selected.point.lat+', '+selected.point.lon : 'Выберите назначенную заявку с координатами.';
    if (selected && resetTime) $('gps-replay-time').value=hhmm(Math.max(0,selected.visit.start-10));
    replayControls();
  }
  function showPlanComparison(before, after, requestId) {
    const result=planComparison(before,after,requestId),box=$('gps-replay-changes');
    box.replaceChildren(el('h3','Что обновилось: версия № '+before.version+' → № '+after.version));
    box.append(el('p','Это сравнение рабочих данных. GPS-проверка на устаревшей версии не запускалась.'));
    const table=el('table'),head=el('thead'),heading=el('tr'),body=el('tbody');
    for(const title of ['Параметр','Было','Стало'])heading.append(el('th',title));
    head.append(heading);
    for(const values of result.rows){const row=el('tr');values.forEach(value=>row.append(el('td',value)));body.append(row);}
    table.append(head,body);box.append(table);
    if(result.updates.length){
      box.append(el('p','Причины обновления из журнала:'));
      const list=el('ul');for(const update of result.updates)list.append(el('li','Версия № '+update.version+': '+update.label));box.append(list);
    } else box.append(el('p','Причина обновления отсутствует в доступной части журнала.'));
    box.append(el('p',result.selectedUnchanged
      ? 'Назначение и расписание выбранной заявки не изменились. Обновились другие рабочие данные.'
      : 'Параметры выбранной заявки изменились. Следующая проверка будет учитывать актуальные данные.'));
    box.append(el('p',result.canReplay
      ? 'Трек и тестовое время сохранены. Нажмите «Проверить на плане № '+after.version+'», чтобы получить вывод модели.'
      : 'Эта заявка больше не доступна для выезда этого инженера. Выберите другую; трек и тестовое время сохранены.'));
    box.hidden=false;
    $('gps-replay-run').textContent='Проверить на плане № '+after.version;
  }
  async function loadReplayTasks(options={}) {
    const id = $('gps-demo-engineer').value, token = ++taskLoad;
    const before=options.before || replayState;
    const preserve=!!options.preserve && before?.engineer.id===id;
    const selectedId=options.requestId || $('gps-replay-task').value;
    $('gps-replay-run').disabled=true;
    if (!id) return;
    try {
      const data=await request('/api/engineer?engineer_id='+encodeURIComponent(id));
      if(token!==taskLoad || id!==$('gps-demo-engineer').value)return;
      replayState=data;$('gps-replay-task').replaceChildren();
      const tasks=data.tasks.filter(availableTask);
      const removed=preserve && selectedId && !tasks.some(t=>t.id===selectedId);
      if(removed){const empty=el('option','Выберите заявку заново');empty.value='';$('gps-replay-task').append(empty);}
      for(const item of tasks){
        const option=el('option','№ '+item.id+' · '+item.address);option.value=item.id;$('gps-replay-task').append(option);
      }
      if(preserve && !removed && selectedId)$('gps-replay-task').value=selectedId;
      showTarget(!preserve);
      if(preserve && before.version!==data.version)showPlanComparison(before,data,selectedId);
      return data;
    } catch(e) {
      if(token===taskLoad){replayState=null;replayStatus('Не удалось обновить параметры: '+e.message+'. Трек сохранён. Нажмите «Обновить заявки».');}
    }
  }
  $('gps-demo-engineer').addEventListener('change',()=>{
    replayState=null;$('gps-replay-changes').hidden=true;$('gps-replay-run').textContent='Проверить трек';loadReplayTasks();
  });
  $('gps-replay-reload').onclick=()=>loadReplayTasks({preserve:true});
  $('gps-replay-task').onchange=()=>{showTarget();$('gps-replay-changes').hidden=true;};
  $('gps-track-file').onchange=async()=>{
    const file=$('gps-track-file').files[0];if(!file)return;
    if(file.size>48000){replayStatus('Файл больше 48 КБ. Возьмите короткий фрагмент трека.');return;}
    try {$('gps-track-text').value=await file.text();replayStatus('Файл прочитан. Проверьте инженера, заявку и время, затем нажмите «Проверить трек».');}
    catch {replayStatus('Не удалось прочитать файл.');}
  };
  $('gps-single-point').onclick=()=>{
    const lat=$('gps-lat'),lon=$('gps-lon'),accuracy=$('gps-accuracy');
    if(!lat.value || !lon.value || !accuracy.value || ![lat,lon,accuracy].every(input=>input.reportValidity())){replayStatus('Введите широту, долготу и точность.');return;}
    $('gps-track-text').value='seconds,lat,lon,accuracy_m\n0,'+lat.value+','+lon.value+','+accuracy.value;
  };
  $('gps-own-position').onclick=()=>{
    if(!window.isSecureContext || !navigator.geolocation){replayStatus('Координаты устройства доступны на localhost или по доверенному HTTPS. Можно ввести их вручную.');return;}
    replayStatus('Запрашиваем геолокацию по вашему разрешению…');
    navigator.geolocation.getCurrentPosition(position=>{
      $('gps-lat').value=position.coords.latitude;$('gps-lon').value=position.coords.longitude;$('gps-accuracy').value=position.coords.accuracy;
      $('gps-single-point').click();replayStatus('Координата подставлена. Она будет тестовой точкой выбранного инженера после проверки трека.');
    },()=>replayStatus('Координата недоступна. Проверьте разрешение или введите точку вручную.'),{enableHighAccuracy:true,timeout:10000,maximumAge:0});
  };
  $('gps-track-sample').onclick=()=>{
    const selected=task();if(!selected){replayStatus('Выберите заявку с координатами.');return;}
    $('gps-track-text').value='seconds,lat,lon,accuracy_m\n'+[0,60,120].map(seconds=>[seconds,selected.point.lat,selected.point.lon,10].join(',')).join('\n');
    replayStatus('Пример подставлен: ожидается сигнал «Инженер рядом с адресом».');
  };
  $('gps-replay-run').onclick=async()=>{
    $('gps-replay-run').disabled=true;
    const submitted=replayState,requestId=$('gps-replay-task').value,engineerId=$('gps-demo-engineer').value;
    try {
      if(!replayState || !task())throw Error('Обновите заявки и выберите одну из них.');
      if(!$('gps-replay-time').reportValidity())return;
      const parts=$('gps-replay-time').value.split(':').map(Number);
      if(parts.length!==2)throw Error('Укажите время проверки.');
      const content=$('gps-track-text').value.trim();
      const result=await request('/api/gps/replay',{engineer_id:$('gps-demo-engineer').value,request_id:task().id,
        version:replayState.version,minute:parts[0]*60+parts[1],format:content.startsWith('[')?'json':'csv',text:content});
      $('gps-replay-changes').hidden=true;$('gps-replay-run').textContent='Проверить трек';
      replayStatus('Проверка по плану № '+submitted.version+'. Точек: '+result.points+'. Принято: '+result.accepted+', отброшено: '+result.rejected+'. '+result.note+' Вывод модели появится в карточке инженера ниже. Причины отброса — в журнале точек.');
      refresh();setTimeout(refresh,1000);
    } catch(e) {
      if(e.status===409 && submitted && engineerId===$('gps-demo-engineer').value) {
        replayStatus('Проверка не запускалась: рабочие данные обновились. Загружаем сравнение; введённый трек сохранён.');
        const current=await loadReplayTasks({preserve:true,before:submitted,requestId});
        if(current)replayStatus('Данные обновлены с версии № '+submitted.version+' до № '+current.version+'. Сравнение ниже. Подтвердите повторную проверку.');
      } else replayStatus(e.message);
    }
    finally {replayControls();}
  };
  $('gps-fit').addEventListener('click', () => { if (map && points.length) map.fitBounds(points, {padding: [25, 25], maxZoom: 16}); });
  let historyCursor=null, historyEngineer=null;
  async function loadHistory(next=false) {
    const eid=$('gps-trace-engineer').value, token=generation;
    if(channel!=='device') { $('gps-trace').textContent='Демонстрации не сохраняются. Выберите источник «Устройства инженеров».'; return; }
    if(eid!==historyEngineer || !next) historyCursor=null;
    historyEngineer=eid;
    try {
      const data=await request('/api/gps/history?engineer_id='+encodeURIComponent(eid)+'&limit=500'+(historyCursor?'&before='+encodeURIComponent(historyCursor):''));
      if(token!==generation || eid!==$('gps-trace-engineer').value) return;
      historyCursor=data.next_cursor; $('gps-history-next').hidden=!historyCursor;
      $('gps-trace').textContent=JSON.stringify(data,null,2);
    } catch(error) {if(token===generation)$('gps-trace').textContent=error.message;}
  }
  $('gps-history-load').addEventListener('click',()=>loadHistory());
  $('gps-history-next').addEventListener('click',()=>loadHistory(true));
  $('gps-trace-load').addEventListener('click', async () => {
    const token = generation;
    try { const data = await request('/api/gps/trace?channel=' + channel + '&engineer_id=' + encodeURIComponent($('gps-trace-engineer').value)); if (token === generation) $('gps-trace').textContent = JSON.stringify(data, null, 2); }
    catch (e) { if(token===generation)$('gps-trace').textContent = e.message; }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  setInterval(refresh, 5000);
})();
