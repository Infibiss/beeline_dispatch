/* Independent GPS dashboard. No plan mutations, HTML interpolation or model calls. */
(() => {
  'use strict';
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
      if (!response.ok) throw new Error(data.error || 'GPS временно недоступен');
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
    if(planning && data.version<planning.version)return;
    if(planning?.version!==data.version) {
      excludedSelection.clear();
      $('gps-exclusion-time').value=String(Math.floor(data.time/60)).padStart(2,'0')+':'+String(data.time%60).padStart(2,'0');
    }
    planning=data;syncExclusions();
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
    card.append(el('h3', row.name), el('p', states[row.tracking_state] || row.tracking_state, 'gps-subtle'));
    const pick=el('label',undefined,'gps-exclusion-pick');
    const checkbox=el('input');checkbox.type='checkbox';checkbox.dataset.excludeEngineer=row.engineer_id;
    checkbox.onchange=()=>{if(checkbox.checked)excludedSelection.add(row.engineer_id);else excludedSelection.delete(row.engineer_id);syncExclusions();};
    pick.append(checkbox,el('span','Не учитывать при пересчёте'));card.append(pick);
    const age = row.point_age_seconds;
    card.append(el('p', age === null ? 'Надёжная координата ещё не получена.' : 'Координата ' + age + ' сек назад · точность ±' + Math.round(row.position.accuracy_m) + ' м', 'gps-age'));
    const analysis = row.analysis;
    if (analysis?.target) card.append(el('p', 'К заявке № ' + analysis.target.id + ' · ' + analysis.target.address));
    if (analysis?.eta) card.append(el('p', 'Прибытие ориентировочно через ' + analysis.eta.low_minutes + '–' + analysis.eta.high_minutes + ' мин. Без пробок, по прямому расстоянию с поправкой; требует уточнения.', 'gps-subtle'));
    if (!row.date_matches) card.append(el('p', 'Дата плана отличается от текущей: выводы об опоздании и смене отключены.', 'gps-subtle'));
    for (const item of row.alerts) {
      const box = el('div', undefined, 'gps-alert ' + item.level); box.append(el('h4', item.title), el('p', item.detail));
      const advice = el('details'); advice.open = item.level === 'warning'; advice.append(el('summary', 'Что можно сделать'), el('p', item.action)); box.append(advice); card.append(box);
    }
    if (!row.alerts.length && fresh(row, config)) card.append(el('p', analysis ? 'По доступным данным сигналов для проверки нет.' : 'Свежая точка получена. Анализ ещё не готов.', 'gps-subtle'));
    if (row.alerts.length && channel === 'device') {
      const actions = el('div', undefined, 'gps-actions');
      for (const [label, action] of [['Уточнить статус', 'facts'], ['Проверить варианты плана', 'replan']]) {
        const button = el('button', label, 'secondary'); button.type = 'button'; button.onclick = () => intent(action, row.engineer_id); actions.append(button);
      }
      card.append(actions);
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
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {referrerPolicy:'strict-origin-when-cross-origin', attribution: '© OpenStreetMap'}).addTo(map);
      markers = L.layerGroup().addTo(map);
    }
    markers.clearLayers(); points = [];
    for (const row of rows) {
      if (!row.position) continue;
      const point = [row.position.lat, row.position.lon]; points.push(point);
      const popup = el('div'); popup.append(el('strong', row.name), el('p', 'Последняя надёжная точка · ' + row.point_age_seconds + ' сек назад'), el('p', states[row.tracking_state]));
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
      loadReplayTasks();
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
    $('gps-model-notice').hidden = h.enabled && !h.last_error;
    $('gps-model-notice').textContent = !h.enabled ? 'GPS-модуль выключен. Планирование и рабочие статусы доступны.'
      : 'GPS-модель временно недоступна: прогнозы приостановлены. Связь и последние координаты видны; планирование продолжает работать.';
    $('gps-health').textContent = !h.enabled ? 'Анализ GPS выключен.' : h.status === 'degraded'
      ? 'Модель временно недоступна. Проверки связи продолжаются; следующий запуск через ' + h.circuit_open_seconds + ' сек.'
      : h.status === 'idle' ? 'Модель готова к запуску при появлении координат.' : 'Анализ GPS работает. Последний успешный расчёт: ' + (h.last_success ? new Date(h.last_success * 1000).toLocaleTimeString('ru-RU') : 'ещё выполняется');
    $('gps-diagnostics').textContent = JSON.stringify({health: h, recent_events: data.recent_events, thresholds: data.config}, null, 2);
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
      if(!planning)request('/api/state').then(data=>setPlanning({version:data.version,time:data.time,engineers:data.engineers,excludedEngineerIds:data.excluded_engineer_ids || []})).catch(e=>exclusionError(e.message));
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
  const task = () => replayState?.tasks.find(t => t.id === $('gps-replay-task').value);
  function showTarget() {
    const selected = task();
    $('gps-replay-target').textContent = selected ? selected.address+' · координаты: '+selected.point.lat+', '+selected.point.lon : 'Нет назначенных заявок с координатами.';
    if (selected) {
      const minute = Math.max(0,(selected.visit?.start || replayState.engineer.shift.start)-10);
      $('gps-replay-time').value = String(Math.floor(minute/60)).padStart(2,'0')+':'+String(minute%60).padStart(2,'0');
    }
  }
  async function loadReplayTasks() {
    const id = $('gps-demo-engineer').value, token = ++taskLoad;
    replayState = null; $('gps-replay-run').disabled = true; $('gps-replay-task').replaceChildren();
    if (!id) return;
    try {
      const data = await request('/api/engineer?engineer_id='+encodeURIComponent(id));
      if (token!==taskLoad) return;
      replayState = data;
      for(const item of data.tasks.filter(t=>t.point && t.visit && t.status!=='completed')) {
        const option = el('option','№ '+item.id+' · '+item.address);option.value=item.id;$('gps-replay-task').append(option);
      }
      $('gps-replay-run').disabled=!$('gps-replay-task').options.length; showTarget();
    } catch(e) {if(token===taskLoad)replayStatus(e.message);}
  }
  $('gps-demo-engineer').addEventListener('change', loadReplayTasks);
  $('gps-replay-reload').onclick=loadReplayTasks;
  $('gps-replay-task').onchange=showTarget;
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
    try {
      if(!replayState || !task())throw Error('Обновите заявки и выберите одну из них.');
      const parts=$('gps-replay-time').value.split(':').map(Number);
      if(parts.length!==2)throw Error('Укажите время проверки.');
      const content=$('gps-track-text').value.trim();
      const result=await request('/api/gps/replay',{engineer_id:$('gps-demo-engineer').value,request_id:task().id,
        version:replayState.version,minute:parts[0]*60+parts[1],format:content.startsWith('[')?'json':'csv',text:content});
      replayStatus('Точек: '+result.points+'. Принято: '+result.accepted+', отброшено: '+result.rejected+'. '+result.note+' Вывод модели появится в карточке инженера ниже. Причины отброса — в журнале точек.');
      refresh();setTimeout(refresh,1000);
    } catch(e) {replayStatus(e.message);}
    finally {$('gps-replay-run').disabled=false;}
  };
  $('gps-fit').addEventListener('click', () => { if (map && points.length) map.fitBounds(points, {padding: [25, 25], maxZoom: 16}); });
  $('gps-trace-load').addEventListener('click', async () => {
    const token = generation;
    try { const data = await request('/api/gps/trace?channel=' + channel + '&engineer_id=' + encodeURIComponent($('gps-trace-engineer').value)); if (token === generation) $('gps-trace').textContent = JSON.stringify(data, null, 2); }
    catch (e) { $('gps-trace').textContent = e.message; }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  setInterval(refresh, 5000);
})();
