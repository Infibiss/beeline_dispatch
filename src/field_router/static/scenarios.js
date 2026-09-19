(function () {
  'use strict';
  function minute(value) {
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) throw new Error('Укажите корректное время события.');
    const [h, m] = value.split(':').map(Number);
    return h * 60 + m;
  }
  function time(value) {
    if (value == null || !Number.isFinite(Number(value))) return '—';
    return String(Math.floor(value / 60)).padStart(2, '0') + ':' + String(value % 60).padStart(2, '0');
  }
  function eventPayload(version, input) {
    if (input.type !== 'replan' && !input.target) throw new Error('Выберите заявку или инженера.');
    const event = {id: input.id, type: input.type, time: minute(input.time)};
    if (input.type === 'emergency') {
      const duration = Number(input.duration), router = Number(input.router);
      if (!Number.isInteger(duration) || duration < 1 || duration > 1440 || !Number.isInteger(router) || router < 0) throw new Error('Проверьте длительность и количество роутеров.');
      Object.assign(event, {anchor_id: input.target, duration_minutes: duration, router});
    } else if (input.type === 'cancel') event.request_id = input.target;
    else if (input.type === 'unavailable') event.engineer_id = input.target;
    else if (input.type !== 'replan') throw new Error('Неизвестный тип события.');
    return {version, event, allow_redirect: input.type === 'emergency' && !!input.redirect, preserve_in_progress: !!input.preserve};
  }
  const factLabels = {available: 'Свободен', traveling: 'В пути', working: 'Работает', completed: 'Завершил заявку', interrupted: 'Работа прервана', unavailable: 'Недоступен'};
  function factPayload(version, input) {
    if (!input.engineer_id || !factLabels[input.status]) throw new Error('Выберите инженера и статус.');
    const result = {version, mode: 'manual', time: minute(input.time), engineer_id: input.engineer_id, status: input.status};
    const requiresRequest = ['working', 'completed', 'interrupted', 'traveling'].includes(input.status);
    if (requiresRequest && !input.request_id) throw new Error('Для этого статуса выберите заявку.');
    if (requiresRequest && input.request_id) result.request_id = input.request_id;
    if (input.location_id && !['working', 'completed', 'interrupted'].includes(input.status)) result.location_id = input.location_id;
    for (const name of ['remaining_minutes', 'consumed_router', 'consumed_tv_box']) {
      const applies = name === 'remaining_minutes' ? ['working', 'interrupted', 'traveling'].includes(input.status) : ['completed', 'interrupted'].includes(input.status);
      if (!applies) continue;
      const raw = input[name];
      if (raw == null || raw === '') {
        if (name === 'remaining_minutes' && input.status === 'traveling') throw new Error('Укажите оставшееся время пути.');
        if (input.status === 'interrupted') throw new Error('При прерывании укажите остаток работы и оба количества списанного оборудования, включая нули.');
        continue;
      }
      const value = Number(raw), minimum = name === 'remaining_minutes' ? 1 : 0;
      if (!Number.isInteger(value) || value < minimum) throw new Error('Проверьте остаток работы и списание оборудования.');
      result[name] = value;
    }
    return result;
  }
  function requestsForEngineer(data, id) { return (data.fact_requests || []).filter(r => r.engineer_id === id); }
  function frozenStatus(visit, now) {
    if (visit.end <= now) return 'Выполнена';
    if (visit.start <= now) return 'В работе';
    return visit.arrival > now ? 'В пути · работа закреплена' : 'Ожидание · работа закреплена';
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {requestsForEngineer, eventPayload, factPayload, minute, time, frozenStatus};
  if (typeof document === 'undefined') return;
  const $ = id => document.getElementById(id);
  let state = null, batch = null, selected = null, busy = false;
  const strategyNames = {sla:'Раньше приоритетные', stable:'Меньше изменений', cost:'Меньше ресурсов'};
  function strategyName(c) { return strategyNames[c.id] || c.label; }
  function requestName(id) { return ((batch && batch.request_labels) || (state && state.request_labels) || {})[id] || id || '—'; }
  const strategies = ['Раннее начало приоритетных работ', 'Меньше изменений инженера и времени', 'Меньше инженеров и километров'];
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function controls() {
    $('event-fields').disabled = busy || !state;
    $('facts-fields').disabled = busy || !state;
    $('plan-version').disabled = busy || !state;
    $('address-search').disabled = busy || !state || state.geography_mode !== 'road';
    $('address-confirm').disabled = busy || !state || !$('address-candidates').value;
    $('reload').disabled = busy; $('mode-apply').disabled = busy || !state;
    $('approve').disabled = busy || !state || !batch || !selected || batch.version !== state.version;
    document.querySelectorAll('.candidate button').forEach(button => { button.disabled = busy; });
  }
  function resetCandidates(message) {
    batch = null; selected = null; $('comparison-tools').hidden = true; $('replan-needed').hidden = !state?.needs_replanning; $('diff-title').closest('section').hidden = true;
    $('candidates').replaceChildren(element('div', message || 'Задайте событие и рассчитайте варианты.', 'empty'));
    $('diff-title').textContent = 'Изменения выбранного варианта';
    const row = element('tr'), cell = element('td', 'Выберите вариант для просмотра изменений.', 'muted');
    cell.colSpan = 3; row.append(cell); $('diff-body').replaceChildren(row);
    $('notice').textContent = '';
    renderCommitments(state, false);
    $('approval-note').textContent = 'План изменится только после вашего утверждения.';
    controls();
  }
  function failure(error) {
    if (error.httpStatus === 400 && state) { resetCandidates('Исправьте введённые данные и повторите расчёт.'); $('error').textContent = error.message; $('error').hidden = false; $('status').textContent = 'Изменения не сохранены.'; controls(); return; }
    state = null;
    $('plan-context').textContent = 'Состояние не подтверждено сервером. Показанный план может быть устаревшим; обновите состояние.';
    resetCandidates('Варианты сброшены. Обновите состояние перед новым расчётом.');
    $('error').textContent = error.message || 'Не удалось выполнить запрос.';
    $('error').hidden = false;
    $('status').textContent = 'Состояние не подтверждено сервером. Нажмите «Обновить состояние».';
    controls();
  }
  async function request(path, payload) {
    const response = await fetch(path, payload ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)} : {cache: 'no-store'});
    let data;
    try { data = await response.json(); } catch { throw new Error('Сервер вернул некорректный ответ. Откройте страницу через локальный сервер.'); }
    if (!response.ok) { const error = new Error((response.status === 409 ? 'Версия изменилась. ' : '') + (data.error || 'Ошибка сервера.')); error.httpStatus = response.status; throw error; }
    return data;
  }
  function targets() {
    if (!state) return;
    const engineer = $('event-type').value === 'unavailable', emergency = $('event-type').value === 'emergency', replan = $('event-type').value === 'replan';
    $('event-target').hidden = replan; $('target-label').hidden = replan;
    $('event-target').disabled = replan; $('event-target').required = !replan;
    $('emergency-fields').hidden = !emergency;
    $('duration').disabled = !emergency; $('router').disabled = !emergency;
    $('target-label').textContent = engineer ? 'Недоступный инженер' : emergency ? 'Адрес по существующей заявке' : 'Отменяемая заявка';
    $('event-target').replaceChildren(...(engineer ? state.engineers : emergency ? (state.locations || []).map(item => ({id:item.id,address:item.label})) : state.requests).map(item => {
      const option = element('option', engineer ? item.name + ' · ' + item.id : requestName(item.id) + ' · ' + item.address);
      option.value = item.id; return option;
    }));
  }
  function renderState(data) {
    if (!data || data.version == null || !Array.isArray(data.requests) || !Array.isArray(data.engineers)) throw new Error('Сервер вернул неполное состояние.');
    state = data;
    $('region').textContent = data.region || '—'; $('status').classList.toggle('attention', !!data.needs_replanning); $('version').textContent = data.version;
    $('clock').textContent = time(data.time); $('event-time').value = time(data.time);
    const count = (data.plan && data.plan.routes || []).reduce((sum, route) => sum + (route.visits || []).length, 0);
    $('coverage').textContent = count + ' / ' + data.requests.length;
    const mode = data.mode || 'simulation';
    $('mode-badge').textContent = mode === 'mixed' ? 'РУЧНЫЕ СТАТУСЫ + СИМУЛЯЦИЯ' : mode === 'manual' ? 'РУЧНОЙ УЧЁТ' : 'СИМУЛЯЦИЯ';
    $('clock-label').textContent = mode === 'simulation' ? 'Время симуляции' : 'Время состояния';
    $('mode-notice').textContent = mode === 'simulation' ? (data.geography_mode === 'road' ? 'Дорожные матрицы; исполнение по времени плана моделируется, без GPS.' : 'Условная география и время по утверждённому плану. Это симуляция, без GPS и реальных дорожных маршрутов.') : mode === 'mixed' ? 'Ручные статусы + симуляция остальных. Для инженеров с записанными фактами время само по себе не подтверждает завершение. Местоположение вводится вручную, без GPS.' : 'Ручной учёт: завершение подтверждается записанным фактом. Местоположение вводится вручную, без GPS.';
    if (data.approximate_locations) $('mode-notice').textContent += ' Приблизительных точек: ' + data.approximate_locations + '. Исходный и выбранный адрес сохранены в карточке.';
    $('execution-mode').value=data.execution_mode || 'simulation';
    populateSelect('fact-edit',data.fact_journal || [],f=>engineerName(f.engineer_id)+' · '+time(f.time)+' · '+factLabels[f.status],'Новый факт');
    $('fact-edit').value=''; $('correction-reason').value=''; $('correction-fields').hidden=true;
    $('fact-time').value = time(data.time);
    const previousEngineer = $('fact-engineer').value;
    populateSelect('fact-engineer', data.engineers, item => item.name + ' · ' + item.id);
    if (data.engineers.some(e => e.id === previousEngineer)) $('fact-engineer').value = previousEngineer;
    factRequests();
    $('replan-needed').hidden = !data.needs_replanning; $('go-replan').hidden = !data.needs_replanning;
    populateSelect('fact-location', data.locations || [], item => item.label || item.id, 'Без уточнения места');
    const versions = data.versions && data.versions.length ? data.versions : [{version: data.version, time: data.time, label: 'Текущий план'}];
    $('plan-version').replaceChildren(...versions.map(item => { const option = element('option', 'Версия ' + item.version + ' · ' + time(item.time) + ' · ' + (item.label || '') + (item.version === data.version ? ' · текущая' : '')); option.value = item.version; return option; }));
    $('plan-version').value = String(data.version); showPlan();
    renderFacts(); factFields(); targets(); resetCandidates();
  }
  function populateSelect(id, values, label, empty) {
    const options = values.map(item => { const option = element('option', label(item)); option.value = item.id; return option; });
    if (empty) { const option = element('option', empty); option.value = ''; options.unshift(option); }
    $(id).replaceChildren(...options);
  }
  function showPlan() {
    if (!state) return;
    const version = $('plan-version').value, historical = String(state.version) !== version;
    $('plan-frame').src = '/dispatcher?version=' + encodeURIComponent(version);
    $('plan-context').textContent = historical ? 'История · версия ' + version + '. Только просмотр; текущий план не изменён.' : state.needs_replanning ? 'Есть новые факты. Показан последний утверждённый план; пересчитайте его на вкладке «Перепланирование».' : 'Текущий утверждённый план · версия ' + version + '.';
  }
  function factRequests() {
    const previous = $('fact-request').value;
    const historical=$('fact-late').checked || !!$('fact-edit').value;
    const requests = requestsForEngineer(historical ? {fact_requests:state.historical_requests || []} : state, $('fact-engineer').value);
    populateSelect('fact-request', requests, item => requestName(item.id) + ' · ' + item.address, requests.length ? 'Выберите заявку инженера' : 'Нет незавершённых назначений');
    if (requests.some(r => r.id === previous)) $('fact-request').value = previous;
    const active = (state.statuses || []).find(f => f.engineer_id === $('fact-engineer').value && f.status === 'working');
    if (!historical && active && requests.some(r => r.id === active.request_id)) $('fact-request').value = active.request_id;
  }
  function factFields() {
    const previous = (state?.statuses || []).find(f => f.engineer_id === $('fact-engineer').value);
    const historical=$('fact-late').checked || !!$('fact-edit').value;
    const workingNow = !historical && previous?.status === 'working';
    for (const option of $('fact-status').options) option.disabled = (workingNow && ['available','traveling','unavailable'].includes(option.value)) || (option.value === 'interrupted' && !workingNow && !historical);
    if ($('fact-status').selectedOptions[0]?.disabled) $('fact-status').value = workingNow ? 'working' : 'available';
    $('engineer-fact-context').textContent = previous ? 'Последний факт: ' + factLabels[previous.status] + ' · ' + time(previous.time) + (workingNow ? '. Сначала завершите или прервите эту работу.' : '') : 'Фактов нет: выполнение пока моделируется по расписанию.';
    const earliest = historical ? 0 : Math.max(state?.time || 0, previous ? previous.time + 1 : 0);
    $('fact-engineer').disabled=!!$('fact-edit').value;
    $('correction-fields').hidden=!$('fact-edit').value; $('correction-reason').required=!!$('fact-edit').value;
    $('correction-reason').disabled=!$('fact-edit').value;
    $('fact-time').min = time(earliest);
    if (earliest < 1440 && $('fact-time').value && minute($('fact-time').value) < earliest) $('fact-time').value = time(earliest);
    const status = $('fact-status').value, interrupted = status === 'interrupted', working = status === 'working', traveling = status === 'traveling';
    $('fact-request').required = ['working', 'completed', 'interrupted', 'traveling'].includes(status);
    $('fact-request').disabled = !$('fact-request').required;
    if (workingNow) { for (const option of $('fact-request').options) option.disabled = option.value !== previous.request_id; }
    $('fact-location').required = ['available','traveling','unavailable'].includes(status);
    const atRequest = ['working', 'completed', 'interrupted'].includes(status);
    $('fact-location').disabled = atRequest;
    $('fact-location-note').textContent = atRequest ? 'Местоположение определяется адресом выбранной заявки.' : 'Выберите известное место вручную.';
    $('fact-remaining-group').hidden = !working && !interrupted && !traveling;
    $('fact-remaining').disabled = $('fact-remaining-group').hidden;
    document.querySelector('label[for="fact-remaining"]').textContent = traveling ? 'Осталось пути, мин' : 'Осталось работы, мин';
    $('fact-remaining').required = interrupted || working || traveling;
    $('fact-consumed-group').hidden = !interrupted && status !== 'completed';
    $('fact-router').disabled = $('fact-consumed-group').hidden; $('fact-tv').disabled = $('fact-consumed-group').hidden;
    $('fact-router').required = interrupted; $('fact-tv').required = interrupted;
    $('fact-consumed-note').textContent = interrupted ? 'Укажите фактически потраченное оборудование, включая нули. Остаток работы обязателен.' : 'Пустые поля означают списание потребности заявки. Можно указать фактическое количество, включая нули.';
  }
  function renderFacts() {
    const rows = (state.fact_journal || state.statuses || []).map(fact => {
      const row = element('tr'), location = (state.locations || []).find(item => item.id === fact.location_id);
      [engineerName(fact.engineer_id), factLabels[fact.status] || fact.status, requestName(fact.request_id), time(fact.time), location ? location.label : fact.location_id || '—', fact.remaining_minutes ?? '—', fact.recorded_at && !fact.recorded_at.startsWith('unknown') ? new Date(fact.recorded_at).toLocaleString('ru-RU') : '—'].forEach(value => row.append(element('td', value)));
      return row;
    });
    fillTable('facts-body', rows, 7, 'Фактические статусы ещё не записаны.');
    const revisions=(state.fact_audit || []).filter(r=>r.replaces);
    $('fact-audit').replaceChildren(...revisions.map(r=>element('p',engineerName(r.fact.engineer_id)+' · '+time(r.previous.time)+' '+factLabels[r.previous.status]+' → '+time(r.fact.time)+' '+factLabels[r.fact.status]+'. '+r.reason)));
    if(!revisions.length)$('fact-audit').textContent='Исправлений нет.';
  }
  async function reload() {
    busy = true; controls(); $('error').hidden = true; $('status').textContent = 'Загрузка состояния…';
    try { renderState(await request('/api/state')); $('status').textContent = state.needs_replanning ? 'Факты изменились — требуется перепланирование.' : ''; }
    catch (error) { failure(error); }
    finally { busy = false; controls(); }
  }
  function engineerName(id) { return id == null ? 'Не назначен' : ((state && state.engineers || []).find(item => item.id === id) || {}).name || id; }
  function fillTable(id, rows, columns, emptyText) {
    if (!rows.length) {
      const row = element('tr'), cell = element('td', emptyText, 'muted');
      cell.colSpan = columns; row.append(cell); rows.push(row);
    }
    $(id).replaceChildren(...rows);
  }
  function commitmentStatus(engineerId, visit, now) {
    const fact = (state && state.statuses || []).find(item => item.engineer_id === engineerId);
    if (fact && fact.request_id === visit.request_id) return (factLabels[fact.status] || fact.status) + ' · записанный факт';
    if (fact || state && state.mode === 'manual') return 'Закреплена · завершение не подтверждено';
    return frozenStatus(visit, now) + ' · симуляция';
  }
  function renderCommitments(data, preview) {
    const now = data ? (preview ? data.event.time : data.time) : null;
    $('commitments-context').textContent = data ? (preview ? 'Предпросмотр события' : 'Утверждённое состояние') + ' на ' + time(now) + (preview ? '. До утверждения состояние не изменено.' : '.') : 'Состояние не подтверждено. Обновите его с сервера.';
    const rows = [];
    for (const route of data && data.frozen || []) {
      for (const visit of route.visits || []) {
        const row = element('tr');
        row.append(element('td', engineerName(route.engineer_id)), element('td', requestName(visit.request_id)), element('td', commitmentStatus(route.engineer_id, visit, now)), element('td', time(visit.start) + ' → ' + time(visit.end)));
        rows.push(row);
      }
    }
    fillTable('frozen-body', rows, 4, data ? 'Закреплённых работ нет.' : 'Данные недоступны.');
    const stockRows = [];
    for (const stock of data && data.inventory || []) {
      for (const [device, label] of [['router', 'Роутеры'], ['tv_box', 'ТВ-приставки']]) {
        const row = element('tr');
        row.append(element('td', engineerName(stock.engineer_id)), element('td', label));
        for (const kind of ['consumed', 'reserved', 'available']) row.append(element('td', stock[kind] && stock[kind][device] != null ? stock[kind][device] : '—'));
        row.append(element('td', stock.dispatchable === false ? 'Нет · запас недоступен' : stock.dispatchable === true ? 'Да' : 'Неизвестно'));
        stockRows.push(row);
      }
    }
    fillTable('inventory-body', stockRows, 6, 'Сервер не передал данные об остатках оборудования.');
  }
  function select(candidate) {
    selected = candidate; $('diff-title').closest('section').hidden = false;
    document.querySelectorAll('.candidate').forEach(card => {
      const active = card.dataset.id === candidate.id;
      card.classList.toggle('selected', active);
      const button = card.querySelector('button'); button.setAttribute('aria-pressed', String(active));
      button.textContent = active ? 'Выбран для сравнения' : 'Выбрать вариант';
    });
    $('diff-title').textContent = 'Изменения · ' + strategyName(candidate);
    const rows = (candidate.diff || []).map(diff => {
      const row = element('tr');
      const after = batch.event.type === 'cancel' && batch.event.request_id === diff.request_id ? 'Отменена' : engineerName(diff.after_engineer);
      row.append(element('td', requestName(diff.request_id)), element('td', engineerName(diff.before_engineer) + ' → ' + after), element('td', time(diff.before_start) + ' → ' + time(diff.after_start)));
      return row;
    });
    if (!rows.length) { const row = element('tr'), cell = element('td', 'Назначения и время начала не изменились.'); cell.colSpan = 3; row.append(cell); rows.push(row); }
    $('diff-body').replaceChildren(...rows);
    for (const item of candidate.unassigned_details || []) { const row = element('tr'); row.className = item.lost_assignment ? 'lost-assignment' : ''; const reason = element('td', (item.lost_assignment ? 'Потеря назначения. ' : 'Без назначения. ') + item.reason); reason.colSpan = 2; row.append(element('td', requestName(item.request_id) + ' · ' + item.address), reason); $('diff-body').append(row); }
    $('approval-note').textContent = 'К применению: ' + strategyName(candidate) + '. Потеряно назначений: ' + (candidate.facts.lost_assignments || 0) + '. Проверьте список выше.';
    controls();
  }
  function renderBatch(data) {
    if (!data || !Array.isArray(data.candidates) || data.version !== state.version) throw new Error('Варианты не соответствуют текущей версии.');
    batch = data; selected = null;
    $('comparison-tools').hidden = false;
    $('replan-needed').hidden = true;
    $('observation-warnings').textContent = (data.observation_warnings || []).join(' ');
    const order = $('candidate-sort').value;
    const score = c => order === 'coverage' ? c.facts.unassigned : order === 'response' ? (c.emergency?.start ?? Infinity) : order === 'stable' ? c.facts.reassigned * 100000 + c.facts.time_shift_minutes : order === 'distance' ? c.facts.distance_m : ['sla','stable','cost'].indexOf(c.id);
    const ordered = [...data.candidates].sort((a,b) => score(a)-score(b));
    const table = element('table'), head = element('tr');
    ['Показатель', ...ordered.map(strategyName)].forEach(label => head.append(element('th', label))); table.append(head);
    const metricsRows = [
      ['Приоритетные назначены', c => c.facts.high_assigned + ' / ' + c.facts.high_total],
      ['Без назначения', c => c.facts.unassigned],
      ['Потеряно назначений', c => c.facts.lost_assignments],
      ['Начало новой аварии', c => c.emergency ? (c.emergency.assigned ? time(c.emergency.start) : 'Не назначена') : '—'],
      ['Смена исполнителя', c => c.facts.reassigned],
      ['Переносы по времени', c => c.facts.shifted],
      ['Дорога, мин', c => c.facts.travel_minutes],
      ['Расстояние, км', c => (c.facts.distance_m/1000).toFixed(1)],
      ['Инженеров', c => c.facts.engineers_used]];
    metricsRows.forEach(([label,value]) => { const row=element('tr');row.append(element('th',label));ordered.forEach(c => row.append(element('td',value(c))));table.append(row); });
    $('comparison-table').replaceChildren(table);
    const cards = ordered.map(candidate => {
      const index = ['sla','stable','cost'].indexOf(candidate.id);
      const card = element('article', null, 'candidate'); card.dataset.id = candidate.id;
      card.append(element('h3', strategyName(candidate)), element('p', strategies[index] || '', 'strategy'));
      if (candidate.duplicate_of) {
        const duplicate = data.candidates.find(item => item.id === candidate.duplicate_of);
        card.append(element('p', 'Совпадает с ' + (duplicate ? strategyName(duplicate) : candidate.duplicate_of), 'duplicate'));
      }
      const f = candidate.facts || {}, dl = element('dl');
      const metrics = [['Потеряно назначений', f.lost_assignments], ['Сдвиги начала, мин', f.time_shift_minutes]];
      if (candidate.emergency) { const e = candidate.emergency; card.append(element('p', e.assigned ? 'Авария: ' + engineerName(e.engineer_id) + ' · ' + time(e.start) + '–' + time(e.end) + '. До начала: ' + e.response_minutes + ' мин.' : 'Авария не назначена', 'emergency-summary')); }
      metrics.forEach(([label, value]) => { const row = element('div'); row.append(element('dt', label), element('dd', value ?? '—')); dl.append(row); });
      card.append(dl); const explanation = element('details', null, 'candidate-details'); explanation.append(element('summary', 'Почему этот вариант'), element('p', candidate.explanation || 'Пояснение недоступно.', 'explanation')); card.append(explanation);
      const button = element('button', 'Выбрать вариант', 'secondary'); button.type = 'button'; button.setAttribute('aria-pressed', 'false'); button.addEventListener('click', () => select(candidate)); card.append(button);
      return card;
    });
    $('candidates').replaceChildren(...cards);
    if (!cards.length) $('candidates').append(element('div', 'Нет допустимых вариантов для этого события.', 'empty'));
    $('notice').textContent = data.notice || '';
    renderCommitments(data, true);
    controls();
  }
  let addressCandidates = [];
  function addressSelection() {
    const point=addressCandidates.find(p=>p.id===$('address-candidates').value);
    $('address-map').hidden = !point;
    $('address-context').textContent = point ? (point.quality==='approximate' ? 'Приблизительное совпадение — проверьте точку. ' : 'Совпадение здания. ') + point.lat + ', ' + point.lon : '';
    if(point) $('address-map').src='/address-preview?version='+state.version+'&candidate_id='+encodeURIComponent(point.id);
    controls();
  }
  $('address-candidates').addEventListener('change',addressSelection);
  $('new-address').addEventListener('input',()=>{addressCandidates=[];$('address-candidates').replaceChildren(element('option','Повторите поиск'));$('address-candidates').options[0].value='';addressSelection();});
  $('address-search').addEventListener('click',async()=>{
    if(busy||!state)return;
    busy=true;controls();$('error').hidden=true;$('address-context').textContent='Поиск адреса…';
    try{const result=await request('/api/address/search',{version:state.version,address:$('new-address').value});addressCandidates=result.candidates;populateSelect('address-candidates',addressCandidates,p=>p.label,'Выберите точку на карте');$('address-context').textContent=addressCandidates.length?'Проверьте совпадение дома и корпуса.':'Ничего не найдено. Уточните город, улицу и дом.';}
    catch(error){failure(error);}finally{busy=false;controls();}
  });
  $('address-confirm').addEventListener('click',async()=>{
    if(busy||!state||!$('address-candidates').value)return;
    busy=true;controls();$('error').hidden=true;$('address-context').textContent='Расчёт дорожных матриц…';
    try{const result=await request('/api/address/confirm',{version:state.version,candidate_id:$('address-candidates').value});renderState(result);$('event-type').value='emergency';targets();$('event-target').value=result.added_location_id;$('new-address-panel').open=false;$('address-candidates').value='';$('status').textContent='Адрес добавлен. Задайте время и параметры аварии, затем рассчитайте варианты.';}
    catch(error){failure(error);}finally{busy=false;controls();}
  });
  $('candidate-sort').addEventListener('change', () => { if (batch) { const choice=selected; renderBatch(batch); if (choice) select(choice); } });
  $('event-form').addEventListener('input', () => { resetCandidates('Событие изменено. Рассчитайте варианты заново.'); });
  $('event-type').addEventListener('change', targets);
  $('event-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy || !state) return;
    let payload;
    try { payload = eventPayload(state.version, {id: crypto.randomUUID(), type: $('event-type').value, time: $('event-time').value, target: $('event-target').value, duration: $('duration').value, router: $('router').value, redirect: $('redirect').checked, preserve: $('preserve').checked}); }
    catch (error) { $('error').textContent = error.message; $('error').hidden = false; return; }
    resetCandidates('Расчёт допустимых вариантов…'); busy = true; controls(); $('error').hidden = true; $('status').textContent = 'Расчёт может занять несколько секунд…';
    try { renderBatch(await request('/api/preview', payload)); $('status').textContent = 'Расчёт завершён. Выберите вариант и проверьте изменения.'; }
    catch (error) { failure(error); }
    finally { busy = false; controls(); }
  });
  $('approve').addEventListener('click', async () => {
    if (busy || !state || !batch || !selected || batch.version !== state.version) return;
    const payload = {version: state.version, batch_id: batch.id, candidate_id: selected.id};
    busy = true; controls(); $('error').hidden = true; $('status').textContent = 'Утверждение выбранного плана…';
    try { renderState(await request('/api/approve', payload)); $('status').textContent = 'План утверждён. Сохранена версия ' + state.version + '.'; }
    catch (error) { failure(error); }
    finally { busy = false; controls(); }
  });
  const tabs = ['plan', 'events', 'facts'];
  function activateTab(name) {
    tabs.forEach(tab => { const active = tab === name; $('tab-' + tab).setAttribute('aria-selected', String(active)); $('tab-' + tab).tabIndex = active ? 0 : -1; $('panel-' + tab).hidden = !active; });
  }
  tabs.forEach((name, index) => {
    $('tab-' + name).addEventListener('click', () => activateTab(name));
    $('tab-' + name).addEventListener('keydown', event => {
      const offset = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
      if (!offset && !['Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs[tabs.length - 1] : tabs[(index + offset + tabs.length) % tabs.length];
      activateTab(next); $('tab-' + next).focus();
    });
  });
  $('plan-version').addEventListener('change', showPlan);
  $('fact-status').addEventListener('change', () => { factRequests(); factFields(); });
  $('fact-engineer').addEventListener('change', () => { factRequests(); factFields(); });
  $('go-replan').addEventListener('click', () => { $('event-type').value='replan'; targets(); resetCandidates(); activateTab('events'); $('preview').focus(); });
  $('fact-late').addEventListener('change',()=>{factRequests();factFields();});
  $('fact-edit').addEventListener('change',()=>{
    const f=(state.fact_journal || []).find(f=>f.id===$('fact-edit').value);
    if(f){$('fact-engineer').value=f.engineer_id;$('fact-late').checked=true;factRequests();$('fact-status').value=f.status;$('fact-request').value=f.request_id || '';$('fact-location').value=f.location_id;$('fact-time').value=time(f.time);$('fact-remaining').value=f.remaining_minutes || '';$('fact-router').value=f.consumed.router;$('fact-tv').value=f.consumed.tv_box;}
    else{ $('fact-time').value=time(state.time);$('correction-reason').value=''; }
    factFields();
  });
  $('mode-apply').addEventListener('click',async()=>{
    if(busy||!state)return;
    busy=true;controls();$('error').hidden=true;
    try{renderState(await request('/api/mode',{version:state.version,mode:$('execution-mode').value}));$('status').textContent='Режим изменён. Пересчитайте план по фактам.';}
    catch(error){failure(error);}finally{busy=false;controls();}
  });
  $('facts-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy || !state) return;
    let payload;
    try { payload = factPayload(state.version, {engineer_id: $('fact-engineer').value, time: $('fact-time').value, status: $('fact-status').value, request_id: $('fact-request').value, location_id: $('fact-location').value, remaining_minutes: $('fact-remaining').value, consumed_router: $('fact-router').value, consumed_tv_box: $('fact-tv').value}); }
    catch (error) { $('error').textContent = error.message; $('error').hidden = false; return; }
    if($('fact-edit').value){payload.fact_id=$('fact-edit').value;payload.correction_reason=$('correction-reason').value;}
    busy = true; controls(); $('error').hidden = true; $('status').textContent = 'Запись фактического статуса…';
    try { renderState(await request('/api/facts', payload)); $('event-type').value='replan'; targets(); $('status').textContent = 'Факт записан. Нажмите «Перейти к пересчёту», затем «Рассчитать варианты».'; }
    catch (error) { failure(error); }
    finally { busy = false; controls(); }
  });
  $('reload').addEventListener('click', reload);
  reload();
})();

// The embedded plan scrolls with the workspace, not inside a second viewport.
if (typeof document !== 'undefined') document.getElementById('plan-frame')?.addEventListener('load', function () {
  const frame = this, body = frame.contentDocument?.body;
  if (!body) return;
  if (frame.planObserver) frame.planObserver.disconnect();
  const resize = () => { frame.style.height = Math.ceil(body.getBoundingClientRect().height + 24) + 'px'; };
  frame.planObserver = new ResizeObserver(resize); frame.planObserver.observe(body); resize();
});
