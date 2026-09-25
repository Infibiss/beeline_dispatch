(function () {
  'use strict';
  function uniqueEventId() {
    // randomUUID is unavailable on http://LAN-IP in mobile browsers.
    const source = globalThis.crypto;
    if (source && typeof source.randomUUID === 'function') return source.randomUUID();
    if (source && typeof source.getRandomValues === 'function') {
      return 'event-' + Array.from(source.getRandomValues(new Uint32Array(4)), n => n.toString(16).padStart(8, '0')).join('');
    }
    return 'event-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
  }
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
    if (!['replan','unavailable'].includes(input.type) && !input.target) throw new Error('Выберите заявку или инженера.');
    const event = {id: input.id, type: input.type, time: minute(input.time)};
    if (['emergency','new_request'].includes(input.type)) {
      const duration = Number(input.duration), router = Number(input.router);
      if (!Number.isInteger(duration) || duration < 1 || duration > 1440 || !Number.isInteger(router) || router < 0) throw new Error('Проверьте длительность и количество роутеров.');
      Object.assign(event, {anchor_id: input.target, duration_minutes: duration, router});
      if (input.type === 'new_request') {
        const tv = Number(input.tvBox || 0), start = minute(input.windowStart), end = minute(input.windowEnd);
        if (!['connection','local','addon'].includes(input.workKind) || !Number.isInteger(tv) || tv < 0 || tv > 100 || start > end || end < event.time) throw new Error('Проверьте тип, окно клиента и оборудование новой заявки.');
        Object.assign(event, {work_kind: input.workKind, tv_box: tv, window_start: start, window_end: end});
      }
    } else if (input.type === 'cancel') event.request_id = input.target;
    else if (input.type === 'unavailable') {
      if (input.engineerIds !== undefined) {
        if (!Array.isArray(input.engineerIds) || !input.engineerIds.length ||
            input.engineerIds.some(id=>typeof id!=='string' || !id) || new Set(input.engineerIds).size!==input.engineerIds.length)
          throw new Error('Выберите одного или нескольких инженеров.');
        event.engineer_ids = [...input.engineerIds];
      } else {
        if (!input.target) throw new Error('Выберите инженера.');
        event.engineer_id = input.target;
      }
    }
    else if (input.type !== 'replan') throw new Error('Неизвестный тип события.');
    const payload = {version, event, allow_redirect: input.type === 'emergency' && !!input.redirect, preserve_in_progress: true};
    if (input.type === 'replan' && Array.isArray(input.localScopeEngineerIds) && input.localScopeEngineerIds.length) {
      payload.local_scope_engineer_ids = [...new Set(input.localScopeEngineerIds)];
    }
    return payload;
  }
  const factLabels = {available: 'Свободен', traveling: 'В пути', working: 'Работает', completion_pending:'Ожидает подтверждения', completed: 'Завершил заявку', interrupted: 'Работа прервана', unavailable: 'Недоступен'};
  function factPayload(version, input) {
    if (!input.engineer_id || !factLabels[input.status]) throw new Error('Выберите инженера и статус.');
    const result = {version, mode: 'manual', time: minute(input.time), engineer_id: input.engineer_id, status: input.status};
    const requiresRequest = ['working', 'completed', 'completion_pending', 'interrupted', 'traveling'].includes(input.status);
    if (requiresRequest && !input.request_id) throw new Error('Для этого статуса выберите заявку.');
    if (['available', 'traveling', 'unavailable'].includes(input.status) && !input.location_id) throw new Error('Укажите текущее местоположение инженера.');
    if (requiresRequest && input.request_id) result.request_id = input.request_id;
    if (input.location_id && !['working', 'completed', 'completion_pending', 'interrupted'].includes(input.status)) result.location_id = input.location_id;
    for (const name of ['remaining_minutes', 'consumed_router', 'consumed_tv_box']) {
      const applies = name === 'remaining_minutes' ? ['working', 'interrupted', 'traveling'].includes(input.status) : ['completed', 'completion_pending', 'interrupted'].includes(input.status);
      if (!applies) continue;
      const raw = input[name];
      if (raw == null || raw === '') {
        if (name === 'remaining_minutes' && ['working', 'traveling'].includes(input.status)) throw new Error(input.status === 'traveling' ? 'Укажите оставшееся время пути.' : 'Укажите оставшееся время работы.');
        if (input.status === 'interrupted') throw new Error('При прерывании укажите остаток работы и оба количества списанного оборудования, включая нули.');
        continue;
      }
      const value = Number(raw), minimum = name === 'remaining_minutes' ? 1 : 0;
      if (!Number.isInteger(value) || value < minimum || (name === 'remaining_minutes' && value > 1440)) throw new Error('Проверьте остаток работы и списание оборудования.');
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
  if (typeof module !== 'undefined' && module.exports) module.exports = {requestsForEngineer, eventPayload, factPayload, minute, time, frozenStatus, uniqueEventId};
  if (typeof document === 'undefined') return;
  const $ = id => document.getElementById(id);
  const appFetch = (...args) => window.FieldRouter ? window.FieldRouter.fetch(...args) : fetch(...args);
  const appUrl = path => window.FieldRouter ? window.FieldRouter.url(path) : path;
  let state = null, batch = null, selected = null, busy = false, recommendation = null, recommendationPending = false, localScopeEngineerIds = [];
  const strategyNames = {sla:'Быстрее к аварии', stable:'Меньше изменений', cost:'Меньше дороги'};
  function strategyName(c) { return strategyNames[c.id] || c.label; }
  function requestName(id) { return ((batch && batch.request_labels) || (state && state.request_labels) || {})[id] || id || '—'; }
  const strategies = {sla:'Раньше начать аварийные работы', stable:'Сохранить назначения и клиентское время', cost:'Сократить оставшиеся минуты дороги'};
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function controls() {
    window.dispatchEvent(new CustomEvent('planning-busy',{detail:{busy}}));
    $('event-fields').disabled = busy || !state;
    $('facts-fields').disabled = busy || !state;
    $('plan-version').disabled = busy || !state;
    $('address-search').disabled = busy || !state || state.geography_mode !== 'road';
    $('address-confirm').disabled = busy || !state || !$('address-candidates').value;
    $('reload').disabled = busy; $('execution-mode').disabled = busy || !state; $('mode-apply').disabled = busy || !state || $('execution-mode').value === (state.execution_mode || 'simulation');
    $('approve').disabled = busy || !state || !batch || !selected || batch.version !== state.version;
    $('ai-recommend').disabled = busy || !batch || recommendationPending;
    document.querySelectorAll('.candidate button').forEach(button => { button.disabled = busy; });
    document.querySelectorAll('#completion-list button').forEach(button => { button.disabled = busy || !state; });
  }
  function resetCandidates(message) {
    $('excluded-summary').hidden=true;
    batch = null; selected = null; recommendation = null; recommendationPending = false; $('ai-panel').hidden=true; $('comparison-tools').hidden = true; $('comparison-metrics').hidden = true; $('observation-warnings').textContent = ''; $('variants-note').textContent = ''; $('replan-needed').hidden = !state?.needs_replanning; $('diff-title').closest('section').hidden = true;
    $('candidates').dataset.count = '0';
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
    $('status').textContent = 'Состояние не подтверждено. Нажмите «Обновить».';
    controls();
  }
  async function request(path, payload) {
    const response = await appFetch(appUrl(path), payload ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)} : {cache: 'no-store'});
    let data;
    try { data = await response.json(); } catch { throw new Error('Сервер не подтвердил операцию. Нажмите «Обновить», чтобы проверить состояние.'); }
    if (!response.ok) { const error = new Error((response.status === 409 ? 'Версия изменилась. ' : '') + (data.error || 'Ошибка сервера.')); error.httpStatus = response.status; throw error; }
    return data;
  }
  function targets() {
    if (!state) return;
    const engineer = $('event-type').value === 'unavailable', emergency = $('event-type').value === 'emergency', ordinary = $('event-type').value === 'new_request', newJob = emergency || ordinary, replan = $('event-type').value === 'replan';
    $('event-target').hidden = replan || engineer; $('target-label').hidden = replan || engineer;
    $('event-target').disabled = replan || engineer; $('event-target').required = !replan && !engineer;
    $('event-engineers-group').hidden = !engineer;
    const chosen = new Set([...$('event-engineers').querySelectorAll('input:checked')].map(input=>input.value));
    $('event-engineers').replaceChildren(...state.engineers.map(item=>{
      const label=element('label');label.className='check';
      const checkbox=element('input');checkbox.type='checkbox';checkbox.value=item.id;
      checkbox.disabled=(state.excluded_engineer_ids || []).includes(item.id);
      checkbox.checked=!checkbox.disabled && chosen.has(item.id);
      label.append(checkbox,element('span',item.name+(checkbox.disabled?' · уже исключён':'')));return label;
    }));
    $('emergency-fields').hidden = !newJob;
    $('duration').disabled = !newJob; $('router').disabled = !newJob;
    $('ordinary-fields').hidden = !ordinary;
    for (const id of ['work-kind','window-start','window-end','new-tv']) $(id).disabled = !ordinary;
    $('redirect').closest('label').hidden = !emergency; $('redirect').disabled = !emergency;
    $('event-policy').textContent = ordinary ? 'Добавим в свободный интервал без переноса прежних назначений. Если места нет, заявка останется без назначения.' : emergency ? 'Начатая работа завершается. Ориентир реакции на аварию — до 2 часов с поступления; превышение покажем в результате.' : 'Начатые работы сохраняются до завершения.';
    $('target-label').textContent = engineer ? 'Недоступный инженер' : newJob ? 'Адрес новой заявки' : 'Отменяемая заявка';
    $('event-target').replaceChildren(...(engineer ? state.engineers : newJob ? (state.locations || []).map(item => ({id:item.id,address:item.label})) : state.requests).map(item => {
      const option = element('option', engineer ? item.name + ' · ' + item.id : newJob ? item.address : requestName(item.id) + ' · ' + item.address);
      option.value = item.id; return option;
    }));
  }
  function renderState(data) {
    if (!data || data.version == null || !Array.isArray(data.requests) || !Array.isArray(data.engineers)) throw new Error('Сервер вернул неполное состояние.');
    state = data;
    $('remote-update').hidden = true;
    $('region').textContent = data.region || '—'; $('status').classList.toggle('attention', !!data.needs_replanning); $('version').textContent = data.version;
    $('clock').textContent = time(data.time); $('event-time').value = time(data.time); $('event-time').min = time(data.time);
    const count = (data.plan && data.plan.routes || []).reduce((sum, route) => sum + (route.visits || []).length, 0);
    $('coverage').textContent = count + ' / ' + data.requests.length;
    const mode = data.mode || 'simulation';
    $('mode-badge').textContent = mode === 'mixed' ? 'Смешанный учёт' : mode === 'manual' ? 'Ручной учёт' : 'Симуляция';
    $('clock-label').textContent = mode === 'simulation' ? 'Время симуляции' : 'Время состояния';
    $('mode-notice').textContent = mode === 'simulation' ? (data.geography_mode === 'road' ? 'Дорожные матрицы; исполнение по времени плана моделируется.' : 'Условная география и исполнение по времени плана.') : mode === 'mixed' ? 'Факты инженеров и диспетчера + симуляция остальных. Время само по себе не закрывает заявку инженера с записанными фактами.' : 'Ручной учёт: выполнение подтверждает диспетчер.';
    $('mode-notice').textContent += ' GPS используется для наблюдения; координаты не меняют план и не подтверждают выполнение автоматически.';
    if (data.approximate_locations) $('mode-notice').textContent += ' Приблизительных точек: ' + data.approximate_locations + '. Исходный и выбранный адрес сохранены в карточке.';
    $('execution-mode').value=data.execution_mode || 'simulation';
    populateSelect('fact-edit',data.fact_journal || [],f=>engineerName(f.engineer_id)+' · '+time(f.time)+' · '+factLabels[f.status],'Новый факт');
    $('fact-edit').value=''; $('fact-late').checked=false; $('correction-reason').value=''; $('correction-fields').hidden=true;
    $('fact-remaining').value=''; $('fact-router').value=''; $('fact-tv').value='';
    $('fact-time').value = time(data.time);
    const previousEngineer = $('fact-engineer').value;
    populateSelect('fact-engineer', data.engineers, item => item.name + ' · ' + item.id);
    if (data.engineers.some(e => e.id === previousEngineer)) $('fact-engineer').value = previousEngineer;
    factRequests();
    $('replan-needed').hidden = !data.needs_replanning; $('go-replan').hidden = !data.needs_replanning;
    populateSelect('fact-location', data.locations || [], item => item.label || item.id, 'Без уточнения места');
    const versions = data.versions && data.versions.length ? data.versions : [{version: data.version, time: data.time, label: 'Текущий план'}];
    $('plan-version').replaceChildren(...versions.map(item => { const option = element('option', '№ ' + item.version + ' · ' + time(item.time) + ' · ' + (item.label || 'План') + (item.version === data.version ? ' · текущий' : '')); option.value = item.version; return option; }));
    $('plan-version').value = String(data.version); showPlan();
    renderFacts(); renderCompletions(); factFields(); targets(); resetCandidates();
    window.dispatchEvent(new CustomEvent('planning-state',{detail:{version:data.version,time:data.time,
      engineers:data.engineers,excludedEngineerIds:data.excluded_engineer_ids || []}}));
  }
  function populateSelect(id, values, label, empty) {
    const options = values.map(item => { const option = element('option', label(item)); option.value = item.id; return option; });
    if (empty) { const option = element('option', empty); option.value = ''; options.unshift(option); }
    $(id).replaceChildren(...options);
  }
  function showPlan() {
    if (!state) return;
    const version = $('plan-version').value, historical = String(state.version) !== version;
    $('plan-frame').src = appUrl('/dispatcher?version=' + encodeURIComponent(version));
    $('plan-context').textContent = historical ? 'История · версия ' + version + '. Только просмотр; текущий план не изменён.' : state.needs_replanning ? 'Есть новые факты. Показан последний утверждённый план; пересчитайте его на вкладке «Перепланирование».' : 'Текущий утверждённый план · версия ' + version + '.';
    $('plan-context').classList.toggle('attention', !historical && !!state.needs_replanning);
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
    for (const option of $('fact-status').options) option.disabled = (workingNow && ['available','traveling','unavailable'].includes(option.value)) || (option.value === 'interrupted' && !workingNow && !historical) || (option.value === 'completion_pending' && !$('fact-edit').value);
    if ($('fact-status').selectedOptions[0]?.disabled) $('fact-status').value = workingNow ? 'working' : 'available';
    $('engineer-fact-context').textContent = previous ? 'Последний статус: ' + factLabels[previous.status] + ' · ' + time(previous.time) + (workingNow ? '. Сначала завершите или прервите работу.' : '') : state?.execution_mode === 'manual' ? 'Нет статуса — инженер недоступен для назначений.' : 'Статус определяется по плану.';
    const earliest = historical ? 0 : Math.max(state?.time || 0, previous ? previous.time + 1 : 0);
    $('fact-engineer').disabled=!!$('fact-edit').value;
    $('correction-fields').hidden=!$('fact-edit').value; $('correction-reason').required=!!$('fact-edit').value;
    $('correction-reason').disabled=!$('fact-edit').value;
    $('fact-time').min = time(earliest);
    if (earliest < 1440 && $('fact-time').value && minute($('fact-time').value) < earliest) $('fact-time').value = time(earliest);
    const status = $('fact-status').value, interrupted = status === 'interrupted', working = status === 'working', traveling = status === 'traveling';
    $('fact-request').required = ['working', 'completed', 'completion_pending', 'interrupted', 'traveling'].includes(status);
    $('fact-request').disabled = !$('fact-request').required;
    $('fact-request').hidden = !$('fact-request').required;
    document.querySelector('label[for="fact-request"]').hidden = !$('fact-request').required;
    if (workingNow) { for (const option of $('fact-request').options) option.disabled = option.value !== previous.request_id; }
    $('fact-location').required = ['available','traveling','unavailable'].includes(status);
    const atRequest = ['working', 'completed', 'completion_pending', 'interrupted'].includes(status);
    $('fact-location').disabled = atRequest;
    $('fact-location').hidden = atRequest;
    document.querySelector('label[for="fact-location"]').hidden = atRequest;
    $('fact-location-note').textContent = atRequest ? 'Местоположение — адрес выбранной заявки.' : '';
    $('fact-remaining-group').hidden = !working && !interrupted && !traveling;
    $('fact-remaining').disabled = $('fact-remaining-group').hidden;
    document.querySelector('label[for="fact-remaining"]').textContent = traveling ? 'Осталось пути, мин' : 'Осталось работы, мин';
    $('fact-remaining').required = interrupted || working || traveling;
    $('fact-consumed-group').hidden = !interrupted && !['completed','completion_pending'].includes(status);
    $('fact-router').disabled = $('fact-consumed-group').hidden; $('fact-tv').disabled = $('fact-consumed-group').hidden;
    $('fact-router').required = interrupted; $('fact-tv').required = interrupted;
    $('fact-consumed-note').textContent = interrupted ? 'Укажите фактически потраченное оборудование, включая нули. Остаток работы обязателен.' : 'Пустые поля означают списание потребности заявки. Можно указать фактическое количество, включая нули.';
  }
  function renderFacts() {
    const rows = (state.fact_journal || state.statuses || []).map(fact => {
      const row = element('tr'), location = (state.locations || []).find(item => item.id === fact.location_id);
      [engineerName(fact.engineer_id), factLabels[fact.status] || fact.status, requestName(fact.request_id), time(fact.time), location ? location.label : fact.location_id || '—', fact.remaining_minutes ?? '—', fact.recorded_at && !fact.recorded_at.startsWith('unknown') ? new Date(fact.recorded_at).toLocaleString('ru-RU') : '—'].forEach(value => row.append(element('td', value)));
      const report = element('td', (fact.source === 'engineer' ? 'Инженер' : 'Диспетчер') + (fact.comment ? ': ' + fact.comment : ''));
      report.className = 'fact-comment'; row.append(report);
      return row;
    });
    fillTable('facts-body', rows, 8, 'Фактические статусы ещё не записаны.');
    const revisions=(state.fact_audit || []).filter(r=>r.replaces);
    $('fact-audit').replaceChildren(...revisions.map(r=>element('p',engineerName(r.fact.engineer_id)+' · '+time(r.previous.time)+' '+factLabels[r.previous.status]+' → '+time(r.fact.time)+' '+factLabels[r.fact.status]+'. '+r.reason)));
    if(!revisions.length)$('fact-audit').textContent='Исправлений нет.';
  }
  function renderCompletions() {
    const reports = state.pending_completions || [];
    $('completion-panel').hidden = !reports.length;
    $('completion-count').textContent = reports.length;
    $('completion-list').replaceChildren(...reports.map(report => {
      const card = element('article', null, 'completion-card');
      card.append(element('strong', report.engineer_name + ' · ' + requestName(report.request_id)),
        element('p', report.address), element('p', 'Выполнение заявлено в ' + time(report.time) + '. Роутеры: ' + report.consumed.router + ', ТВ-приставки: ' + report.consumed.tv_box + '.'));
      if (report.comment) card.append(element('p', report.comment, 'hint'));
      const confirm = element('button', 'Подтвердить выполнение', 'primary'); confirm.type = 'button';
      confirm.addEventListener('click', async () => {
        if (busy || !state) return;
        busy = true; controls(); $('error').hidden = true;
        try { renderState(await request('/api/completions/confirm', {version:state.version, fact_id:report.id})); $('status').textContent = 'Выполнение подтверждено. Расход оборудования списан один раз.'; }
        catch (error) { failure(error); }
        finally { busy = false; controls(); }
      });
      const edit = element('button', 'Уточнить время или расход', 'secondary'); edit.type = 'button';
      edit.addEventListener('click', () => { $('fact-edit').value = report.id; $('fact-edit').dispatchEvent(new Event('change')); $('facts-form').scrollIntoView({block:'center'}); $('fact-time').focus(); });
      card.append(confirm, edit); return card;
    }));
  }
  async function reload() {
    busy = true; controls(); $('error').hidden = true; $('status').textContent = 'Загрузка состояния…';
    try { renderState(await request('/api/state')); $('status').textContent = ''; }
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
    const pending = (state?.pending_completions || []).some(f => f.request_id === visit.request_id);
    if (pending || visit.status === 'completion_pending') return 'Ожидает подтверждения диспетчера';
    if (['completed','interrupted'].includes(visit.status)) return factLabels[visit.status] + ' · записанный факт';
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
      button.textContent = active ? 'Выбран' : 'Посмотреть изменения';
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
    $('approval-note').textContent = strategyName(candidate) + (candidate.facts.lost_assignments ? ' · Потеряно назначений: ' + candidate.facts.lost_assignments : '') + '. План изменится после применения.';
    controls();
  }
  function canonicalCandidate(data, id) {
    const candidate = data.candidates.find(c => c.id === id);
    return candidate?.duplicate_of ? data.candidates.find(c => c.id === candidate.duplicate_of) : candidate;
  }
  function renderRecommendation() {
    const choice = batch.candidates.find(c => c.id === recommendation?.candidate_id);
    const canonical = canonicalCandidate(batch, choice?.id);
    $('ai-panel').hidden = !batch.candidates.length;
    $('ai-recommend').hidden = !batch.llm_enabled;
    $('ai-result').textContent = recommendationPending ? 'Сравниваем варианты…' : choice
      ? 'Рекомендуем «' + strategyName(choice) + '». ' + [recommendation.notice, recommendation.text].filter(Boolean).join(' ')
      : 'Сравните показатели и проверьте изменения перед применением.';
    document.querySelectorAll('.candidate').forEach(card => {
      const recommended = card.dataset.id === canonical?.id;
      card.classList.toggle('recommended', recommended);
      card.querySelector('.recommendation-badge').hidden = !recommended;
    });
  }
  $('ai-recommend').addEventListener('click', async () => {
    const current = batch;
    if (!current?.llm_enabled || busy || recommendationPending) return;
    recommendationPending = true; controls(); renderRecommendation();
    try {
      const result = await request('/api/recommend', {version:current.version, batch_id:current.id});
      if (batch !== current || state?.version !== current.version) return;
      if (!canonicalCandidate(current, result.candidate_id)) throw new Error('Неизвестный вариант');
      recommendation = result;
    } catch (_) {
      if (batch === current) recommendation = {candidate_id:current.recommended_candidate_id, text:'ИИ недоступен. Сравните показатели и проверьте изменения перед применением.'};
    } finally {
      if (batch === current) { recommendationPending = false; renderRecommendation(); controls(); }
    }
  });
  function candidateOrder(candidate, order) {
    const f = candidate.facts, preference = ['stable', 'sla', 'cost'].indexOf(candidate.id);
    const stability = [f.lost_assignments ?? 0, f.avoidable_reassigned ?? f.reassigned, f.time_shift_minutes, f.travel_minutes, f.distance_m];
    const risk = candidate.risk?.status === 'ready' && candidate.risk.summary ? candidate.risk : null;
    const r = risk?.summary || {};
    if (order === 'risk') return risk
      ? [f.emergency_total - f.emergency_assigned, f.connection_total - f.connection_assigned, f.unassigned,
          r.p_sla_breach ?? Infinity, r.expected_loss ?? Infinity,
          risk.affected_clients ?? r.expected_affected_customers ?? Infinity, r.p_overtime ?? Infinity,
          candidate.emergency ? (candidate.emergency.response_minutes ?? Infinity) : 0, ...stability, preference]
      : [preference];
    if (order === 'coverage') return [f.emergency_total - f.emergency_assigned, f.connection_total - f.connection_assigned, f.unassigned, preference];
    if (order === 'response') return [f.response_unassigned ?? (candidate.emergency?.assigned === false ? 1 : 0), candidate.emergency ? (candidate.emergency.response_minutes ?? Infinity) : (f.priority_start_sum ?? Infinity), ...stability, preference];
    if (order === 'stable') return [...stability, preference];
    if (order === 'distance') return [f.travel_minutes, f.distance_m, ...stability, preference];
    return [preference];
  }
  function compareCandidates(a, b, order) {
    const left = candidateOrder(a, order), right = candidateOrder(b, order);
    for (let i = 0; i < left.length; i++) {
      if (left[i] < right[i]) return -1;
      if (left[i] > right[i]) return 1;
    }
    return 0;
  }
  function metricCell(candidate, baseline, value, delta, format = String) {
    const cell = element('td', value(candidate) ?? '—');
    if (!delta || !baseline || candidate.id === baseline.id) return cell;
    const current = delta(candidate), original = delta(baseline);
    if (current == null || original == null || !Number.isFinite(current) || !Number.isFinite(original)) return cell;
    const change = current - original;
    const label = (change > 0 ? '+' : change < 0 ? '−' : '') + format(Math.abs(change));
    cell.append(element('span', ' (' + label + ')', 'metric-delta'));
    return cell;
  }
  function renderBatch(data) {
    if (!data || !Array.isArray(data.candidates) || data.version !== state.version) throw new Error('Варианты не соответствуют текущей версии.');
    if (batch !== data) {
      recommendation = {candidate_id:data.recommended_candidate_id, text:'Проверьте изменения перед применением.'};
      recommendationPending = false;
    }
    batch = data; selected = null;
    const excluded=data.excluded_engineers || [];
    $('excluded-summary').hidden=!excluded.length;
    $('excluded-summary').textContent=excluded.length
      ? 'Расчёт с '+time(data.event.time)+' без: '+excluded.map(item=>item.name).join(', ')+'. Проверьте переназначения и заявки без исполнителя. Изменения вступят в силу после «Применить план».'
      : '';
    $('replan-needed').hidden = true;
    $('observation-warnings').textContent = (data.observation_warnings || []).join(' ');
    const order = $('candidate-sort').value;
    const ordered = data.candidates.filter(c => !c.duplicate_of).sort((a,b) => compareCandidates(a, b, order));
    const baseline = data.candidates.find(c => c.id === 'stable');
    const newEmergency = data.candidates.some(c => c.emergency);
    const responseTargets = data.candidates.some(c => c.facts.response_target_count > 0);
    $('comparison-tools').hidden = ordered.length < 2;
    $('comparison-metrics').hidden = !ordered.length;
    const merged = ordered.length < data.candidates.length;
    const riskReady = ordered.some(c => c.risk?.status === 'ready' && c.risk.summary);
    const durationEvidence = data.risk_model?.duration_evidence || {};
    const durationFacts = Number.isInteger(durationEvidence.observation_count) && durationEvidence.observation_count > 0
      ? durationEvidence.observation_count : 0;
    const notes = [];
    if (data.insertion_policy) notes.push('Новая заявка добавляется без изменения прежних назначений и времени начала.');
    if (merged) notes.push(ordered.length === 1 ? 'Один уникальный план: все стратегии нашли одинаковые маршруты и время.' : 'Совпавшие планы объединены в одну карточку.');
    if (!newEmergency && !responseTargets && merged) notes.push('Аварий в остатке дня нет: «Быстрее к аварии» совпадает с «Меньше изменений».');
    if (riskReady) notes.push('Риски: Монте-Карло · ' + (data.risk_model?.iterations || '—') + ' прогонов'
      + (durationFacts ? ' · доступно ' + durationFacts + ' подтверждённых фактов длительности' : ' · априорные длительности')
      + ' · калибровка вероятностей не подтверждена · потери в условных единицах.');
    if (data.local_scope?.engineer_ids?.length) notes.push('Локальный приоритет: сначала минимизируются изменения вокруг ' + data.local_scope.engineer_ids.map(engineerName).join(', ') + '; изменения вне контура явно посчитаны.');
    $('variants-note').textContent = notes.join(' ');
    const table = element('table'), head = element('tr');
    ['Показатель', ...ordered.map(strategyName)].forEach(label => head.append(element('th', label))); table.append(head);
    const metricsRows = [
      ['Аварии назначены', c => c.facts.emergency_assigned + ' / ' + c.facts.emergency_total],
      ['Подключения назначены', c => c.facts.connection_assigned + ' / ' + c.facts.connection_total],
      ['Без назначения', c => c.facts.unassigned],
      ['Потеряно назначений', c => c.facts.lost_assignments]];
    if (newEmergency) metricsRows.push(
      ['Прибытие к новой аварии', c => c.emergency?.assigned ? time(c.emergency.arrival) : 'Не назначена'],
      ['Начало новой аварии', c => c.emergency?.assigned ? time(c.emergency.start) : 'Не назначена'],
      ['До начала новой аварии, мин', c => c.emergency?.assigned ? c.emergency.response_minutes : 'Не назначена', c => c.emergency?.assigned ? c.emergency.response_minutes : null]);
    else if (responseTargets) metricsRows.push(
      ['Аварий для ускорения', c => c.facts.response_target_count],
      ['Из них без назначения', c => c.facts.response_unassigned],
      ['Среднее начало оставшихся аварий', c => {
        const assigned = c.facts.response_target_count - c.facts.response_unassigned;
        return assigned > 0 ? time(Math.round(c.facts.priority_start_sum / assigned)) : '—';
      }]);
    if (riskReady) metricsRows.push(
      ['P(SLA), %', c => c.risk?.status === 'ready' ? (100 * c.risk.summary.p_sla_breach).toFixed(1) : '—', c => c.risk?.status === 'ready' ? 100 * c.risk.summary.p_sla_breach : null, n => n.toFixed(1)],
      ['P(позднего начала), %', c => c.risk?.status === 'ready' ? (100 * c.risk.summary.p_late_start).toFixed(1) : '—', c => c.risk?.status === 'ready' ? 100 * c.risk.summary.p_late_start : null, n => n.toFixed(1)],
      ['P(переработки), %', c => c.risk?.status === 'ready' ? (100 * c.risk.summary.p_overtime).toFixed(1) : '—', c => c.risk?.status === 'ready' ? 100 * c.risk.summary.p_overtime : null, n => n.toFixed(1)],
      ['Ожидаемые потери, усл. ед.', c => c.risk?.status === 'ready' ? c.risk.summary.expected_loss.toFixed(2) : '—', c => c.risk?.status === 'ready' ? c.risk.summary.expected_loss : null, n => n.toFixed(2)],
      ['Клиентов с изменённым обещанием', c => c.risk?.status === 'ready' ? c.risk.affected_clients : '—', c => c.risk?.status === 'ready' ? c.risk.affected_clients : null],
      ['Ожидаемо клиентов под SLA-риском', c => c.risk?.status === 'ready' ? c.risk.summary.expected_affected_customers.toFixed(2) : '—', c => c.risk?.status === 'ready' ? c.risk.summary.expected_affected_customers : null, n => n.toFixed(2)]);
    if (data.local_scope?.engineer_ids?.length) metricsRows.push(
      ['Изменений вне локального контура', c => c.facts.outside_scope_changes ?? '—', c => c.facts.outside_scope_changes ?? null]);
    metricsRows.push(
      ['Смена исполнителя', c => c.facts.reassigned, c => c.facts.reassigned],
      ['Из них предотвратимые', c => c.facts.avoidable_reassigned],
      ['Переносы по времени', c => c.facts.shifted],
      ['Сдвиги начала, мин', c => c.facts.time_shift_minutes, c => c.facts.time_shift_minutes],
      ['Дорога, мин', c => c.facts.travel_minutes, c => c.facts.travel_minutes],
      ['Расстояние, км', c => c.facts.distance_m == null ? '—' : (c.facts.distance_m/1000).toFixed(1), c => c.facts.distance_m == null ? null : c.facts.distance_m/1000, n => n.toFixed(1)],
      ['Инженеров', c => c.facts.engineers_used]);
    metricsRows.forEach(([label, value, delta, format]) => {
      const row = element('tr'); row.append(element('th', label));
      ordered.forEach(c => row.append(metricCell(c, baseline, value, delta, format)));
      table.append(row);
    });
    $('comparison-table').replaceChildren(table);
    const cards = ordered.map(candidate => {
      const card = element('article', null, 'candidate'); card.dataset.id = candidate.id;
      const badge = element('span', 'Рекомендуем', 'recommendation-badge'); badge.hidden = true;
      card.append(badge, element('h3', strategyName(candidate)), element('p', strategies[candidate.id] || '', 'strategy'));
      const aliases = (candidate.strategy_ids || data.candidates.filter(c => c.duplicate_of === candidate.id).map(c => c.id)).filter(id => id !== candidate.id);
      if (aliases.length) card.append(element('p', 'Совпадает с ' + aliases.map(id => '«' + strategyName({id}) + '»').join(', '), 'duplicate'));
      const f = candidate.facts || {}, dl = element('dl');
      card.append(element('p', 'Назначено: аварии ' + f.emergency_assigned + '/' + f.emergency_total + ' · подключения ' + f.connection_assigned + '/' + f.connection_total, 'priority-coverage'));
      const metrics = [['Без назначения', f.unassigned], ['Смена исполнителя', f.reassigned], ['Из них предотвратимые', f.avoidable_reassigned], ['Сдвиги начала, мин', f.time_shift_minutes], ['Дорога, мин', f.travel_minutes], ['Расстояние, км', f.distance_m == null ? '—' : (f.distance_m / 1000).toFixed(1)]];
      const risk = candidate.risk?.status === 'ready' && candidate.risk.summary ? candidate.risk : null;
      if (risk) metrics.unshift(
        ['P(SLA)', (risk.summary.p_sla_breach * 100).toFixed(1) + '%'],
        ['Ожидаемые потери', risk.summary.expected_loss.toFixed(2) + ' усл. ед.'],
        ['Изменено обещаний', risk.affected_clients],
        ['Ожидаемо под SLA-риском', risk.summary.expected_affected_customers.toFixed(2)]);
      if (f.lost_assignments) metrics.unshift(['Потеряно назначений', f.lost_assignments]);
      if (candidate.emergency) {
        const e = candidate.emergency;
        card.append(element('p', e.assigned ? 'Авария: ' + engineerName(e.engineer_id) + '. Прибытие ' + time(e.arrival) + ', начало ' + time(e.start) + '. До начала: ' + e.response_minutes + ' мин.' : 'Авария не назначена', 'emergency-summary'));
        if (e.exceeds_response_target) card.append(element('p', 'Начало позже ориентира реакции 2 часа. Проверьте другие варианты или дополнительные ресурсы.', 'notice'));
      }
      if (candidate.ordinary_request) {
        const added = candidate.ordinary_request;
        card.append(element('p', added.assigned ? 'Новая заявка: ' + engineerName(added.engineer_id) + ', начало ' + time(added.start) + '. Прежние назначения сохранены.' : 'Для новой заявки нет свободного интервала с нужными навыками и оборудованием. Прежние назначения сохранены.', 'emergency-summary'));
      }
      metrics.forEach(([label, value]) => { const row = element('div'); row.append(element('dt', label), element('dd', value ?? '—')); dl.append(row); });
      card.append(dl);
      if (risk) card.append(element('p', 'Risk Engine · ' + risk.iterations + ' прогонов'
        + (durationFacts ? ' · доступно фактов: ' + durationFacts : ''), 'risk-candidate-note'));
      const explanation = element('details', null, 'candidate-details'); explanation.append(element('summary', 'Почему этот вариант'), element('p', candidate.explanation || 'Пояснение недоступно.', 'explanation')); card.append(explanation);

      const button = element('button', 'Посмотреть изменения', 'secondary'); button.type = 'button'; button.setAttribute('aria-pressed', 'false'); button.addEventListener('click', () => select(candidate)); card.append(button);
      return card;
    });
    $('candidates').replaceChildren(...cards);
    $('candidates').dataset.count = String(cards.length);
    if (!cards.length) $('candidates').append(element('div', 'Нет допустимых вариантов для этого события.', 'empty'));
    renderRecommendation();
    $('notice').textContent = data.notice || '';
    renderCommitments(data, true);
    controls();
  }
  let addressCandidates = [];
  function addressSelection() {
    const point=addressCandidates.find(p=>p.id===$('address-candidates').value);
    $('address-map').hidden = !point;
    $('address-context').textContent = point ? (point.quality==='approximate' ? 'Приблизительное совпадение — проверьте точку. ' : 'Совпадение здания. ') + point.lat + ', ' + point.lon : '';
    if(point) $('address-map').src=appUrl('/address-preview?version='+state.version+'&candidate_id='+encodeURIComponent(point.id));
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
    try{const kind=$('event-type').value;const result=await request('/api/address/confirm',{version:state.version,candidate_id:$('address-candidates').value});renderState(result);$('event-type').value=kind;targets();$('event-target').value=result.added_location_id;$('new-address-panel').open=false;$('address-candidates').value='';$('status').textContent='Адрес добавлен. Задайте время и параметры заявки, затем рассчитайте варианты.';}
    catch(error){failure(error);}finally{busy=false;controls();}
  });
  $('candidate-sort').addEventListener('change', () => { if (batch) { const choice=selected; renderBatch(batch); if (choice) select(choice); } });
  $('event-form').addEventListener('input', () => { localScopeEngineerIds=[]; const hadBatch = !!batch; resetCandidates('Параметры изменены. Рассчитайте варианты.'); if (hadBatch) $('status').textContent = 'Параметры изменены — предыдущий расчёт сброшен.'; });
  function defaultDuration() { $('duration').value = $('event-type').value === 'new_request' ? ({connection:70,local:30,addon:20}[$('work-kind').value]) : 80; }
  $('event-type').addEventListener('change', () => { targets(); defaultDuration(); });
  $('work-kind').addEventListener('change', defaultDuration);
  $('event-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy || !state) return;
    let payload;
    try { payload = eventPayload(state.version, {id: uniqueEventId(), type: $('event-type').value, time: $('event-time').value, target: $('event-target').value,
      engineerIds:[...$('event-engineers').querySelectorAll('input:checked:not(:disabled)')].map(input=>input.value),
      duration: $('duration').value, router: $('router').value, tvBox:$('new-tv').value, workKind:$('work-kind').value, windowStart:$('window-start').value, windowEnd:$('window-end').value, redirect: $('redirect').checked, preserve: true,
      localScopeEngineerIds}); }
    catch (error) { $('error').textContent = error.message; $('error').hidden = false; return; }
    resetCandidates('Расчёт допустимых вариантов…'); busy = true; controls(); $('error').hidden = true; $('status').textContent = 'Расчёт может занять несколько секунд…';
    try { renderBatch(await request('/api/preview', payload)); $('status').textContent = 'Расчёт завершён. Выберите вариант и проверьте изменения.'; }
    catch (error) { failure(error); $('error').scrollIntoView({block:'center'}); }
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
  const tabs = ['plan', 'risk', 'events', 'facts', 'gps'];
  function activateTab(name) {
    tabs.forEach(tab => { const active = tab === name; $('tab-' + tab).setAttribute('aria-selected', String(active)); $('tab-' + tab).tabIndex = active ? 0 : -1; $('panel-' + tab).hidden = !active; });
    if (name === 'plan') requestAnimationFrame(() => $('plan-frame').resizePlan?.());
    window.dispatchEvent(new CustomEvent('workbench-tab', {detail: {name}}));
  }
  window.addEventListener('gps-intent', event => {
    if (!state || busy) return;
    if (event.detail.action === 'facts') {
      activateTab('facts');
      if ([...$('fact-engineer').options].some(o => o.value === event.detail.engineerId)) $('fact-engineer').value = event.detail.engineerId;
      factRequests(); factFields(); $('fact-engineer').focus();
    } else if (event.detail.action === 'replan') {
      $('event-type').value = 'replan'; targets(); resetCandidates(); activateTab('events'); $('preview').focus();
    } else if (event.detail.action === 'exclude') {
      const ids=event.detail.engineerIds || [];
      if (event.detail.version!==state.version) {
        $('error').textContent='Версия плана изменилась. Обновите страницу и выберите инженеров заново.';
        $('error').hidden=false;$('error').scrollIntoView({block:'center'});return;
      }
      if(!ids.length || ids.some(id=>!state.engineers.some(e=>e.id===id) || (state.excluded_engineer_ids || []).includes(id)))return;
      $('event-type').value='unavailable';targets();
      $('event-engineers').querySelectorAll('input').forEach(input=>{input.checked=ids.includes(input.value);});
      $('event-time').value=event.detail.time;
      $('preserve').checked=true;resetCandidates();activateTab('events');
      $('panel-events').scrollIntoView({block:'start'});
      $('event-form').requestSubmit();
    }
  });
  window.addEventListener('risk-intent', event => {
    if (!state || busy) return;
    if (event.detail?.action === 'facts') {
      activateTab('facts');
      if (event.detail.engineerId && [...$('fact-engineer').options].some(option=>option.value===event.detail.engineerId)) {
        $('fact-engineer').value=event.detail.engineerId; factRequests(); factFields();
      }
      $('fact-engineer').focus();
    }
    if (event.detail?.action === 'replan') {activateTab('events');$('event-type').focus();}
    if (event.detail?.action === 'preview') {
      if (event.detail.planVersion !== state.version) {
        $('error').textContent = 'Рекомендация относится к другой версии плана. Обновите показатели риска.';
        $('error').hidden = false; $('error').scrollIntoView({block:'center'}); return;
      }
      localScopeEngineerIds = event.detail.engineerId ? [event.detail.engineerId] : [];
      $('event-type').value = 'replan'; $('event-time').value = time(state.time); targets();
      resetCandidates('Рассчитываем варианты по рекомендации Risk Engine…');
      $('status').textContent = 'Рекомендация открыта как безопасный предпросмотр. Применение потребует отдельного выбора и подтверждения.';
      activateTab('events'); $('panel-events').scrollIntoView({block:'start'});
      $('event-form').requestSubmit();
    }
  });
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
  $('go-replan').addEventListener('click', () => { $('event-type').value='replan'; targets(); resetCandidates(); $('status').textContent=''; activateTab('events'); $('preview').focus(); });
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
  $('execution-mode').addEventListener('change', controls);
  $('facts-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy || !state) return;
    let payload;
    try { payload = factPayload(state.version, {engineer_id: $('fact-engineer').value, time: $('fact-time').value, status: $('fact-status').value, request_id: $('fact-request').value, location_id: $('fact-location').value, remaining_minutes: $('fact-remaining').value, consumed_router: $('fact-router').value, consumed_tv_box: $('fact-tv').value}); }
    catch (error) { $('error').textContent = error.message; $('error').hidden = false; return; }
    if($('fact-edit').value){payload.fact_id=$('fact-edit').value;payload.correction_reason=$('correction-reason').value;}
    busy = true; controls(); $('error').hidden = true; $('status').textContent = 'Запись фактического статуса…';
    try { renderState(await request('/api/facts', payload)); $('event-type').value='replan'; targets(); $('status').textContent = 'Статус сохранён.'; }
    catch (error) { failure(error); }
    finally { busy = false; controls(); }
  });
  $('reload').addEventListener('click', reload);
  $('load-remote').addEventListener('click', reload);
  let polling = false;
  setInterval(async () => {
    if (!state || busy || polling || document.hidden) return;
    polling = true;
    try {
      const revision = await request('/api/revision');
      if (state && revision.version !== state.version) {
        $('remote-update-text').textContent = 'Есть новые факты или назначения с другого устройства (версия ' + revision.version + '). Загрузите изменения перед продолжением.';
        $('remote-update').hidden = false;
      }
    } catch {
      $('remote-update-text').textContent = 'Связь с сервером потеряна. Показанные данные могут быть устаревшими.';
      $('remote-update').hidden = false;
    } finally { polling = false; }
  }, 5000);
  reload();
})();

// The embedded plan scrolls with the workspace, not inside a second viewport.
if (typeof document !== 'undefined') document.getElementById('plan-frame')?.addEventListener('load', function () {
  const frame = this, body = frame.contentDocument?.body;
  if (!body) return;
  if (frame.planObserver) frame.planObserver.disconnect();
  const resize = () => {
    // Hidden tabs report zero height; do not collapse a working plan to 24px.
    if (!frame.getClientRects().length) return;
    const height = Math.ceil(body.getBoundingClientRect().height + 24);
    if (height > 24 && frame.style.height !== height + 'px') frame.style.height = height + 'px';
  };
  frame.resizePlan = resize;
  frame.planObserver = new ResizeObserver(resize); frame.planObserver.observe(body); resize();
});
