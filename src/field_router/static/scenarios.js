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
      if (input.requiredTransport) {
        if (!['car','walk','bicycle','public'].includes(input.requiredTransport)) throw new Error('Выберите допустимый тип транспорта.');
        event.required_transport = input.requiredTransport;
      }
      if (input.type === 'emergency' && input.emergencyWindowEnabled) {
        const start = minute(input.emergencyWindowStart), end = minute(input.emergencyWindowEnd);
        if (start > end || end < event.time) throw new Error('Проверьте клиентское окно аварийной заявки.');
        Object.assign(event, {window_start: start, window_end: end});
      }
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
    if (input.type === 'emergency' && input.offDutyEngineerId) {
      const reason = (input.offDutyReason || '').trim();
      if (input.offDutyConfirmed !== true || reason.length < 3 || reason.length > 1000) throw new Error('Подтвердите согласие сотрудника и укажите причину эскалации.');
      Object.assign(payload, {call_off_duty_engineer_ids:[input.offDutyEngineerId], call_off_duty_confirmed:true, call_off_duty_reason:reason});
      const startText = (input.offDutyWindowStart || '').trim(), endText = (input.offDutyWindowEnd || '').trim();
      if (!!startText !== !!endText) throw new Error('Укажите оба часа выхода сотрудника с выходного.');
      if (startText) {
        const start = minute(startText), end = minute(endText);
        if (start < event.time || end <= start) throw new Error('Окно выхода должно начинаться не раньше аварии и заканчиваться после начала.');
        payload.call_off_duty_window_start = start;
        payload.call_off_duty_window_end = end;
      }
    }
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
  const appUrl = path => {
    if(window.FieldRouter)return window.FieldRouter.url(path);
    if(typeof location!=='undefined' && /^\/(r|regions)\//.test(location.pathname || ''))throw Error('Не загрузилась навигация района. Обновите страницу; данные другого района не будут показаны.');
    return path;
  };
  let state = null, batch = null, selected = null, busy = false, recommendation = null, recommendationPending = false, localScopeEngineerIds = [];
  let inboxSnapshot = null, lastSeenNotificationId = null, notificationsInitialized = false;
  let streamDemoEnabled=false, streamRows=null, streamTimer=null, streamRunning=false;
  let demoEpoch=0, streamGeneration=0, demoResetting=false, streamLoading=false;
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
  function planningDraft() {
    const mode=$('planning-mode').value;
    if(mode==='manual'){
      const saved=inboxSnapshot?.planning_settings || {every_n_requests:1,every_n_minutes:null};
      return {mode,every_n_requests:saved.every_n_requests,every_n_minutes:saved.every_n_minutes};
    }
    const count=$('auto-count').value.trim(), minutes=$('auto-minutes').value.trim();
    return {mode,every_n_requests:count===''?null:Number(count),
      every_n_minutes:minutes===''?null:Number(minutes)};
  }
  function updatePlanningDraft() {
    const draft=planningDraft(), saved=inboxSnapshot?.planning_settings || {mode:'manual',every_n_requests:1,every_n_minutes:null};
    const changed=draft.mode!==saved.mode || draft.every_n_requests!==saved.every_n_requests ||
      draft.every_n_minutes!==saved.every_n_minutes;
    $('autopilot-thresholds').hidden=draft.mode!=='autopilot';
    const validCount=draft.every_n_requests===null || Number.isInteger(draft.every_n_requests) && draft.every_n_requests>=1 && draft.every_n_requests<=100;
    const validMinutes=draft.every_n_minutes===null || Number.isInteger(draft.every_n_minutes) && draft.every_n_minutes>=1 && draft.every_n_minutes<=240;
    const valid=validCount && validMinutes && (draft.mode!=='autopilot' || draft.every_n_requests!==null || draft.every_n_minutes!==null);
    $('planning-mode-feedback').textContent=!valid ? 'Укажите допустимый порог: 1–100 заявок или 1–240 минут.' :
      changed ? (draft.mode==='autopilot'
        ? 'Изменения не сохранены. После сохранения автопилот может сразу обработать уже ожидающие новые заявки.'
        : 'Изменения не сохранены. После сохранения автопилот будет выключен.') :
      draft.mode==='autopilot' ? 'Обрабатывает новые заявки при достижении порога; уже обработанные заявки без исполнителя автоматически не исчезнут.' :
      'Новые заявки ждут ручного расчёта и подтверждения плана.';
    $('planning-save').disabled=busy || demoResetting || !state || !changed || !valid;
    return valid;
  }
  function controls() {
    if(demoResetting)busy=true;
    window.dispatchEvent(new CustomEvent('planning-busy',{detail:{busy}}));
    $('event-fields').disabled = busy || !state;
    $('facts-fields').disabled = busy || !state;
    $('plan-version').disabled = busy || !state;
    $('quick-planning-mode').disabled = busy || !state;
    $('address-search').disabled = busy || !state || state.geography_mode !== 'road';
    $('address-confirm').disabled = busy || !state || !$('address-candidates').value;
    $('reload').disabled = busy; $('execution-mode').disabled = busy || !state; $('mode-apply').disabled = busy || !state || $('execution-mode').value === (state.execution_mode || 'simulation');
    for (const id of ['planning-mode','auto-count','auto-minutes','intake-json','intake-file','intake-submit']) $(id).disabled=busy || !state;
    updatePlanningDraft();
    $('replan-primary').disabled=busy || !state || !state.needs_replanning &&
      !(state.incoming_requests || []).some(item=>item.status==='new') && !(state.replanning_pool || []).length;
    for(const id of ['new-request-action','record-fact-action'])$(id).disabled=busy||!state;
    $('approve').disabled = busy || !state || !batch || !selected || batch.version !== state.version;
    $('ai-recommend').disabled = busy || !batch || recommendationPending;
    document.querySelectorAll('.candidate button').forEach(button => { button.disabled = busy; });
    document.querySelectorAll('#completion-list button').forEach(button => { button.disabled = busy || !state; });
    if(streamDemoEnabled)streamProgress();
  }
  function resetCandidates(message) {
    $('excluded-summary').hidden=true;
    batch = null; selected = null; recommendation = null; recommendationPending = false; $('ai-panel').hidden=true; $('comparison-tools').hidden = true; $('comparison-metrics').hidden = true; $('preview-coverage').hidden=true; $('observation-warnings').textContent = ''; $('variants-note').textContent = ''; $('replan-needed').hidden = true; $('diff-title').closest('section').hidden = true;
    $('candidates').dataset.count = '0';
    $('candidates').replaceChildren(element('div', message || 'Нажмите «Рассчитать варианты», чтобы увидеть предложение для текущей очереди.', 'empty'));
    $('diff-title').textContent = 'Изменения выбранного варианта';
    const row = element('tr'), cell = element('td', 'Выберите вариант для просмотра изменений.', 'muted');
    cell.colSpan = 3; row.append(cell); $('diff-body').replaceChildren(row);
    $('notice').textContent = '';
    renderCommitments(state, false);
    $('approval-note').textContent = 'План изменится только после вашего утверждения.';
    controls();
  }
  function failure(error) {
    if(error.staleDemoRun)return;
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
    const epoch=demoEpoch;
    if(payload && state?.demo_run_id)payload={demo_run_id:state.demo_run_id,...payload};
    const response = await appFetch(appUrl(path), payload ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)} : {cache: 'no-store'})
      .catch(error=>{if(epoch!==demoEpoch)error.staleDemoRun=true;throw error;});
    let data;
    try { data = await response.json(); } catch { const error=new Error('Сервер не подтвердил операцию. Нажмите «Обновить», чтобы проверить состояние.');if(epoch!==demoEpoch)error.staleDemoRun=true;throw error; }
    if(epoch!==demoEpoch){const error=new Error('Симуляция уже перезапущена.');error.staleDemoRun=true;throw error;}
    if (!response.ok) { const error = new Error((response.status === 409 ? 'Версия изменилась. ' : '') + (data.error || 'Ошибка сервера.')); error.httpStatus = response.status; throw error; }
    return data;
  }
  function streamProgress() {
    $('stream-demo-reset').hidden=!state?.demo_reset_enabled;
    $('stream-demo-reset').disabled=demoResetting;
    $('stream-demo-reset').textContent=demoResetting?'Возвращаемся к началу…':'Начать заново';
    if(!streamRows)return;
    const arrived=new Set((inboxSnapshot?.incoming_requests || []).map(item=>item.id));
    const count=streamRows.filter(row=>arrived.has(row.id)).length;
    $('stream-demo-status').textContent=streamRunning
      ? `Поступило ${count} из ${streamRows.length}. Следующая заявка через 10 секунд; распределение зависит от сохранённого режима.`
      : count===streamRows.length ? `Поток завершён: поступило ${count} из ${streamRows.length}. «Начать заново» вернёт исходный план и очистит историю.`
        : `Поступило ${count} из ${streamRows.length}. ${count?'Поток на паузе.':'Выберите режим и запустите поток.'}`;
    $('stream-demo-start').disabled=demoResetting || streamRunning || count===streamRows.length;
    $('stream-demo-start').hidden=count===streamRows.length;
    $('stream-demo-start').textContent=count?'Продолжить поток':'Запустить поток';
    $('stream-demo-pause').disabled=demoResetting || !streamRunning;
    $('stream-demo-pause').hidden=!streamRunning;
  }
  function stopStream() {
    streamRunning=false;streamGeneration++;
    if(streamTimer)clearTimeout(streamTimer);
    streamTimer=null;
  }
  function clearDemoWorkspace() {
    stopStream();streamRows=null;streamLoading=false;
    inboxSnapshot=null;lastSeenNotificationId=null;notificationsInitialized=false;
    localScopeEngineerIds=[];clearGpsDelay();resetCandidates();
    $('decision-search').value='';$('decision-filter').value='all';
    for(const id of ['event-form','facts-form'])$(id).reset?.();
    for(const id of ['intake-json','intake-file','new-address','call-off-duty-reason'])if($(id))$(id).value='';
    addressCandidates=[];populateSelect('address-candidates',[],item=>item.label,'Сначала найдите адрес');
    $('address-context').textContent='';$('address-map').hidden=true;$('address-map').src='about:blank';
    $('intake-feedback').textContent='';$('error').hidden=true;$('remote-update').hidden=true;
    document.querySelectorAll('#panel-events details').forEach(item=>{item.open=false;});
  }
  function loadStreamRows() {
    if(streamRows || streamLoading)return;
    streamLoading=true;
    const epoch=demoEpoch, runId=state?.demo_run_id;
    request('/api/demo-stream/requests').then(result=>{
      if(epoch!==demoEpoch || runId!==state?.demo_run_id)return;
      if(result.demo_run_id && result.demo_run_id!==runId){reload();return;}
      streamRows=result.requests;streamProgress();
    }).catch(error=>{if(!error.staleDemoRun)$('stream-demo-status').textContent='Не удалось загрузить заявки: '+error.message;})
      .finally(()=>{if(epoch===demoEpoch)streamLoading=false;});
  }
  async function restartDemo() {
    if(demoResetting || !state?.demo_reset_enabled)return;
    const previous={version:state.version,demo_run_id:state.demo_run_id};
    stopStream();demoEpoch++;demoResetting=true;busy=true;controls();
    $('stream-demo-status').textContent='Восстанавливаем исходный день и очищаем историю…';
    try {
      const fresh=await request('/api/demo-stream/reset',previous);
      clearDemoWorkspace();renderState(fresh);
      $('status').textContent='Новая симуляция: исходный день восстановлен, история очищена. Выберите режим и запустите поток.';
    } catch(error) {
      if(!error.staleDemoRun){$('error').textContent=error.message;$('error').hidden=false;$('stream-demo-status').textContent='Перезапуск не подтверждён. Обновите состояние и повторите.';}
    } finally {demoResetting=false;busy=false;controls();}
  }
  async function streamNext() {
    if(!streamRunning)return;
    const generation=streamGeneration, epoch=demoEpoch;
    const active=()=>streamRunning && generation===streamGeneration && epoch===demoEpoch;
    try {
      const live=await request('/api/state');
      if(!active())return;
      if(live.demo_run_id && live.demo_run_id!==state?.demo_run_id){stopStream();await reload();return;}
      const arrived=new Set((live.incoming_requests || []).map(item=>item.id));
      const row=streamRows.find(item=>!arrived.has(item.id));
      if(!row){streamRunning=false;streamProgress();return;}
      const copy={...row,time:Math.max(minute(row.time),live.time)};
      await request('/api/inbox/requests',{request:copy,demo_run_id:live.demo_run_id});
      if(!active())return;
      const latest=await request('/api/state');
      if(!active())return;
      if(!busy && !batch)renderLiveState(latest);
      else {
        if(latest.version===state?.version)renderInbox(latest,true);
        $('remote-update-text').textContent=`Поступила заявка ${row.id}. Загрузите изменения перед расчётом.`;
        $('remote-update').hidden=false;
      }
      if(!active())return;
      if(streamRows.every(item=>(latest.incoming_requests || []).some(incoming=>incoming.id===item.id)))
        streamRunning=false;
      streamProgress();
      if(streamRunning)streamTimer=setTimeout(streamNext,10000);
    } catch(error) {
      if(!active() || error.staleDemoRun)return;
      streamRunning=false;streamTimer=null;
      $('stream-demo-status').textContent='Поток остановлен: '+error.message+' Повторите после проверки состояния.';
      $('stream-demo-start').disabled=false;$('stream-demo-pause').disabled=true;
    }
  }
  function targets() {
    if (!state) return;
    const engineer = $('event-type').value === 'unavailable', emergency = $('event-type').value === 'emergency', ordinary = $('event-type').value === 'new_request', newJob = emergency || ordinary, replan = $('event-type').value === 'replan';
    $('event-address-field').hidden=replan || engineer; $('event-target').hidden = replan || engineer; $('target-label').hidden = replan || engineer;
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
    $('new-request-constraints').hidden = !newJob;
    $('required-transport').disabled = !newJob;
    $('emergency-window-panel').hidden = !emergency;
    $('emergency-window-enabled').disabled = !emergency;
    emergencyWindowFields();
    for (const id of ['work-kind','window-start','window-end','new-tv']) $(id).disabled = !ordinary;
    $('redirect').closest('label').hidden = !emergency; $('redirect').disabled = !emergency;
    const offDuty = state.engineers.filter(item => item.off_duty === true);
    const previousOffDuty = $('call-off-duty-engineer').value;
    const noCall = element('option', 'Никого не привлекать'); noCall.value = '';
    $('call-off-duty-engineer').replaceChildren(noCall, ...offDuty.map(item => {
      const option = element('option', item.name + ' · ' + item.id); option.value = item.id; return option;
    }));
    if (offDuty.some(item => item.id === previousOffDuty)) $('call-off-duty-engineer').value = previousOffDuty;
    $('call-off-duty-panel').hidden = !emergency || !offDuty.length;
    $('call-off-duty-engineer').disabled = !emergency || !offDuty.length;
    offDutyFields();
    $('event-policy').textContent = ordinary ? 'Добавим в свободный интервал без переноса прежних назначений. Если места нет, заявка останется без назначения.' : emergency ? 'Начатая работа завершается. Ориентир реакции на аварию — до 2 часов с поступления; превышение покажем в результате. Выходные сотрудники привлекаются только при отдельной подтверждённой эскалации.' : 'Начатые работы сохраняются до завершения.';
    $('target-label').textContent = engineer ? 'Недоступный инженер' : newJob ? 'Адрес новой заявки' : 'Отменяемая заявка';
    $('event-target').replaceChildren(...(engineer ? state.engineers : newJob ? (state.locations || []).map(item => ({id:item.id,address:item.label})) : state.requests).map(item => {
      const option = element('option', engineer ? item.name + ' · ' + item.id : newJob ? item.address : requestName(item.id) + ' · ' + item.address);
      option.value = item.id; return option;
    }));
  }
  function offDutyFields() {
    const selected = $('event-type').value === 'emergency' && !!$('call-off-duty-engineer').value;
    $('call-off-duty-confirmation').hidden = !selected;
    $('call-off-duty-confirmed').disabled = !selected;
    $('call-off-duty-confirmed').required = selected;
    $('call-off-duty-reason').disabled = !selected;
    $('call-off-duty-reason').required = selected;
    for (const id of ['call-off-duty-window-start','call-off-duty-window-end']) $(id).disabled = !selected;
    const engineer = (state?.engineers || []).find(item => item.id === $('call-off-duty-engineer').value);
    $('call-off-duty-window-hint').textContent = engineer ?
      'Исходный график: ' + time(engineer.shift_start) + '–' + time(engineer.shift_end) + '. Для выхода в другое время укажите обе границы.' : '';
  }
  function emergencyWindowFields() {
    const enabled=$('event-type').value==='emergency' && $('emergency-window-enabled').checked;
    $('emergency-window-inputs').hidden=!enabled;
    for(const id of ['emergency-window-start','emergency-window-end']) {
      $(id).disabled=!enabled;$(id).required=enabled;
    }
  }
  function renderReplanningPool() {
    const pool = Array.isArray(state?.replanning_pool) ? state.replanning_pool : [];
    const waiting = pool.filter(item => item.status === 'awaiting_replan');
    const unresolved = pool.filter(item => item.status === 'unassigned');
    $('replanning-pool-summary').textContent = `Требуют назначения · ${waiting.length+unresolved.length}`;
    $('replanning-pool-summary').classList.toggle('attention', waiting.length+unresolved.length>0);
    const card = item => {
      const row = element('article', null, 'pool-card');
      row.append(element('strong', requestName(item.request_id) + ' · ' + (item.address || 'Адрес не указан')));
      const context=[];
      if (item.engineer_id) context.push('Сообщил: ' + engineerName(item.engineer_id));
      else if (item.previous_engineer_id) context.push('Ранее: ' + engineerName(item.previous_engineer_id));
      if (item.windows?.length) context.push('Окно: ' + item.windows.map(window=>time(window.start)+'–'+time(window.end)).join(' или '));
      if (item.duration_minutes) context.push('Работа: '+item.duration_minutes+' мин');
      if(context.length)row.append(element('p',context.join(' · '),'hint'));
      if (item.reason) {
        if(item.status==='awaiting_replan')row.append(element('p',item.reason,'pool-reason'));
        else {const why=element('details');why.append(element('summary','Почему пока без исполнителя'),element('p',item.reason));row.append(why);}
      }
      if (item.start_date || item.comment) {
        const detail=element('details');detail.append(element('summary','Сообщение инженера'));
        if(item.start_date && item.end_date)detail.append(element('p','Недоступен: '+item.start_date+' '+item.start_time+' — '+item.end_date+' '+item.end_time));
        if(item.comment)detail.append(element('p',item.comment));row.append(detail);
      }
      return row;
    };
    const sections = [];
    if (waiting.length) {
      sections.push(element('h3', `Нужно заменить исполнителя · ${waiting.length}`));
      sections.push(element('p', 'Прежний инженер недоступен или отказался от выезда.', 'hint'));
      sections.push(...waiting.map(card));
    }
    if (unresolved.length) {
      sections.push(element('h3', `Без исполнителя после расчёта · ${unresolved.length}`));
      if(state.last_applied)sections.push(element('p',
        `Список по применённому плану №${state.last_applied.version}. Назначенные и отменённые заявки здесь больше не показываются.`, 'hint'));
      sections.push(element('p', unresolved.every(item=>item.code==='SEARCH_UNASSIGNED')
        ? 'Заявки не вошли в найденный план. Проверьте ресурсы и окна; повторный расчёт может дать тот же результат.'
        : 'Проверьте причины, доступных инженеров и окна клиентов.', 'hint'));
      sections.push(...unresolved.map(card));
    }
    $('replanning-pool').replaceChildren(...(sections.length ? sections : [element('p', 'Нет заявок, требующих внимания.', 'hint')]));
    const reports = [...(state.unavailability || []).filter(item => !item.revoked).map(item => ({...item, report_type:'unavailability'})),
      ...(state.declined_assignments || []).filter(item => !item.revoked).map(item => ({...item, report_type:'decline'}))];
    $('engineer-reports-title').textContent = `Сообщения инженеров · ${reports.length}`;
    $('engineer-reports').replaceChildren(...(reports.length ? reports.map(report => {
      const card = element('div', null, 'notice');
      const title = report.report_type === 'decline' ? 'Отказ от выезда · ' + requestName(report.request_id) : 'Период недоступности · ' + (report.reason_label || report.reason);
      card.append(element('strong', engineerName(report.engineer_id) + ' · ' + title));
      if (report.start_date) card.append(element('p', report.start_date + ' ' + report.start_time + ' — ' + report.end_date + ' ' + report.end_time));
      if (report.comment) card.append(element('p', report.comment));
      const reason = element('input'); reason.type = 'text'; reason.minLength = 3; reason.maxLength = 1000;
      reason.placeholder = 'Причина исправления'; reason.setAttribute('aria-label', 'Причина исправления сообщения');
      card.append(reason);
      let location = null;
      const started = report.report_type === 'unavailability' && report.start_date <= state.date &&
        (report.start_date < state.date || minute(report.start_time) <= state.time);
      if (started) {
        location = element('select'); location.setAttribute('aria-label', 'Подтверждённая точка возвращения');
        const blank = element('option', 'Выберите текущую точку'); blank.value = '';
        location.append(blank, ...(state.locations || []).map(point => {
          const option = element('option', point.label); option.value = point.id; return option;
        }));
        card.append(location);
      }
      const revoke = element('button', 'Отозвать сообщение', 'secondary'); revoke.type = 'button';
      revoke.addEventListener('click', async () => {
        if (busy || !state) return;
        if (reason.value.trim().length < 3) { $('error').textContent = 'Укажите причину исправления.'; $('error').hidden = false; return; }
        if (location && !location.value) { $('error').textContent = 'Укажите подтверждённую текущую точку инженера.'; $('error').hidden = false; return; }
        busy = true; controls(); $('error').hidden = true;
        try {
          const payload = {version:state.version, report_id:report.id, reason:reason.value.trim()};
          if (location) payload.location_id = location.value;
          renderState(await request('/api/engineer/revoke-report', payload));
          $('status').textContent = 'Сообщение исправлено. Рассчитайте и утвердите новый план.';
        } catch (error) { failure(error); }
        finally { busy = false; controls(); }
      });
      card.append(revoke); return card;
    }) : [element('p', 'Активных сообщений нет.', 'hint')]));
  }
  function openReplanningPool() {
    activateTab('events');
    const attention=(state?.replanning_pool || []).length>0;
    const target=attention ? $('replanning-pool-section') : $('incoming-section');
    target.open=true;target.scrollIntoView({block:'start'});target.querySelector('summary').focus();
  }
  function renderApplicationVisual(data) {
    const applied=data.last_applied, panel=$('application-visual');
    panel.hidden=!applied;
    if(!applied)return;
    const event=[...(data.dispatch_notifications || [])].reverse().find(item=>
      item.version===applied.version && ['auto_applied','manual_applied'].includes(item.kind));
    const rows=(data.decision_log || []).filter(item=>item.version===applied.version);
    const newIds=new Set(event?.request_ids || []);
    const isAuto=applied.automatic===true;
    $('application-visual-title').textContent=isAuto?'Автопилот обновил план':'Что изменилось после применения';
    $('application-visual-version').textContent=`План №${applied.version}`;
    const stale=data.version!==state?.version;
    $('application-visual-context').textContent=stale
      ? 'План уже применён на сервере. Нажмите «Загрузить изменения», чтобы увидеть его на карте и в плане дня.'
      : `${isAuto?'Применён автоматически':'Утверждён диспетчером'} · время плана ${time(state.time)}. Ниже только изменения этого применения, без полной ленты событий.`;
    const retried=event?.backlog_attempted_request_ids?.length;
    if(retried) $('application-visual-context').textContent+=
      ` Ранее оставшихся без исполнителя перепроверено ${retried}; назначено ${event.recovered_request_ids?.length || 0}.`;
    if(event?.lost_request_ids?.length) $('application-visual-context').textContent+=
      ` Внимание: ${event.lost_request_ids.length} ранее назначенных заявок теперь без исполнителя.`;
    if(rows.some(item=>!Object.prototype.hasOwnProperty.call(item,'previous_engineer_id')))
      $('application-visual-context').textContent+=' Для этого старого применения сведения «до» сохранены не полностью.';
    const metrics=[];
    const addMetric=(label,value,attention=false)=>{
      const card=element('div',null,attention?'application-metric attention':'application-metric');
      card.append(element('span',label),element('strong',value));metrics.push(card);
    };
    addMetric('Новых обработано',String(applied.incoming_count ?? newIds.size));
    addMetric('Изменений в плане',String(applied.changed_count ?? event?.changed_request_ids?.length ?? 0));
    if(event?.cancelled_request_ids?.length)addMetric('Отменено',String(event.cancelled_request_ids.length));
    const assignedBefore=event?.before_effective_assigned_count ?? event?.before_assigned_count,assignedAfter=event?.after_assigned_count;
    if(assignedBefore != null && assignedAfter != null)
      addMetric('Назначено',`${assignedBefore} → ${assignedAfter}`);
    const before=event?.before_requires_assignment_count ?? event?.before_unassigned_count,after=event?.after_unassigned_count;
    const unresolved=after ?? event?.unassigned_request_ids?.length ?? (data.version===state?.version ? state.plan?.unassigned?.length : null);
    addMetric('Требуют назначения',before != null && unresolved != null ? `${before} → ${unresolved}` : String(unresolved ?? '—'),unresolved>0);
    $('application-visual-metrics').replaceChildren(...metrics);
    const llm=event?.llm_status==='ready' && event.plan_explanation ? event :
      rows.find(item=>item.plan_explanation && (item.llm_status==='ready' || item.llm_notice?.startsWith('YandexGPT')));
    const ordinary=event?.plan_explanation ? event : rows.find(item=>item.plan_explanation);
    const explanation=llm || ordinary;
    const explainer=$('application-visual-explanation');
    explainer.replaceChildren();
    if(event?.explanation_card){
      renderExplanationCard(explainer,event.explanation_card,{applied:true,ai:!!llm,unavailable:event.llm_status==='unavailable'});
    }else if(explanation){
      explainer.append(element('strong',llm?'YandexGPT расставила акценты · проверенные показатели':'Почему выбран этот вариант · проверенные показатели'),
        element('p',explanation.plan_explanation));
      if(!llm)explainer.append(element('small',event?.llm_status==='unavailable' ?
        event.llm_notice || 'YandexGPT сейчас недоступна; решение и показатели проверены алгоритмом.' :
        'Пояснение YandexGPT для этого применения пока не сохранено; решение и показатели проверены алгоритмом.','hint'));
    }else explainer.append(element('p',event?.llm_status==='unavailable' ?
      event.llm_notice || 'YandexGPT сейчас недоступна; назначения видны в плане дня.' :
      'Подробное пояснение для этого применения пока не сохранено; назначения уже видны в плане дня.','hint'));
    const ordered=[...rows].sort((a,b)=>Number(newIds.has(b.id))-Number(newIds.has(a.id)) ||
      Number(a.status==='assigned')-Number(b.status==='assigned'));
    const changeCard=item=>{
      const isNew=newIds.has(item.id);
      const cancelled=item.status==='cancelled' || item.change_type==='cancelled';
      const beforeName=item.previous_engineer_id ? `${engineerName(item.previous_engineer_id)} · ${time(item.previous_start)}` :
        isNew ? 'Новая заявка' : item.previous_engineer_id===null ? 'Без назначения' : 'До применения: нет данных';
      const afterName=cancelled?'Удалена из плана':item.engineer_id ? `${engineerName(item.engineer_id)} · ${time(item.start)}` : 'Без исполнителя';
      const label=cancelled?'Отменена':item.change_type==='lost_assignment'?'Потеря назначения':
        item.change_type==='recovered'?'Снова назначена':
        item.status==='unassigned'?'Требует внимания':isNew?'Новая заявка':
        item.previous_engineer_id===item.engineer_id?'Сдвиг времени':'Переназначение';
      const card=element('details',null,!cancelled && item.status==='unassigned'?'application-change attention':'application-change');
      card.dataset.requestId=String(item.request_id || item.id);
      const summary=element('summary');
      const heading=element('span',null,'application-change-heading');
      heading.append(element('span',label,'application-change-kind'),
        element('strong',`${item.id} · ${item.address || requestName(item.request_id)}`));
      summary.append(heading,element('span',`${beforeName} → ${afterName}`,'application-change-route'));
      card.append(summary);
      const body=element('div',null,'application-change-body');
      body.append(element('p',item.llm_explanation ? `Подробности расчёта: ${item.llm_explanation}` :
        item.explanation || 'Проверенное объяснение пока не сохранено.',item.llm_explanation?'application-llm-note':null));
      card.append(body);return card;
    };
    const list=$('application-visual-changes');
    const expanded=new Set([...list.querySelectorAll('.application-change[open]')].map(card=>card.dataset.requestId));
    const moreExpanded=!!list.querySelector('.application-more[open]');
    if(!ordered.length){
      list.replaceChildren(element('p','В этой версии нет сохранённых изменений отдельных назначений.','hint'));
      return;
    }
    const visible=ordered.slice(0,4).map(changeCard);
    if(ordered.length>4){
      const extra=element('details',null,'application-more');
      extra.append(element('summary',`Ещё ${ordered.length-4} изменений`),
        ...ordered.slice(4).map(changeCard));
      extra.open=moreExpanded;
      visible.push(extra);
    }
    for(const card of visible){
      if(card.classList.contains('application-change') && expanded.has(card.dataset.requestId))card.open=true;
      for(const nested of card.querySelectorAll?.('.application-change') || [])
        if(expanded.has(nested.dataset.requestId))nested.open=true;
    }
    list.replaceChildren(...visible);
  }
  function renderNotificationCenter(data) {
    const notifications=data.dispatch_notifications || [];
    if (!notificationsInitialized) {
      lastSeenNotificationId=notifications[notifications.length-1]?.id || null;
      notificationsInitialized=true;
    }
    const seenIndex=notifications.findIndex(item=>item.id===lastSeenNotificationId);
    const unread=lastSeenNotificationId ? Math.max(0,notifications.length-seenIndex-1) : notifications.length;
    $('notification-count').textContent=unread ? `(${unread})` : '';
    const current=data.version===state?.version;
    const pending=(data.incoming_requests || []).filter(item=>item.status==='new').length;
    const unassigned=current ? (state.plan?.unassigned || []).length : 0;
    const waiting=current ? (state.replanning_pool || []).filter(item=>item.status==='awaiting_replan').length : 0;
    const applied=data.last_applied;
    $('notifications-overview').textContent=current
      ? `${applied ? `Последний применённый план №${applied.version}. ` : ''}Сейчас: без исполнителя ${unassigned}; новых заявок ${pending}; ждут пересчёта ${waiting}.`
      : 'План изменился в другом окне. Загрузите изменения, чтобы увидеть актуальные назначения.';
    $('notifications-replan').hidden=!current || !pending && !unassigned && !waiting;
    $('notifications-replan').textContent=unassigned || waiting ? `Разобрать заявки · ${unassigned+waiting}` : `Открыть новые заявки · ${pending}`;
    $('notification-total').textContent=`${notifications.length} событий`;
    const labels={incoming:'Поступление',auto_applied:'Автопилот',manual_applied:'План применён',planning_mode:'Режим изменён',
      explanation_ready:'Пояснение готово',
      auto_error:'Нужно вмешательство',replan_required:'Требуется пересчёт'};
    $('dispatch-notifications').replaceChildren(...(notifications.length ? [...notifications].reverse().map(item=>{
      const card=element('article',null,'notification-card');
      const head=element('div',null,'notification-card-head');
      head.append(element('span',labels[item.kind] || 'Событие','notification-kind'),
        element('small',`${item.time ? new Date(item.time).toLocaleString('ru-RU') : 'Время не указано'} · план №${item.version ?? '—'}`,'hint'));
      card.append(head,element('strong',item.title || 'Событие'),element('p',item.text || ''));
      if(['auto_applied','manual_applied'].includes(item.kind)){
        const before=item.before_effective_assigned_count ?? item.before_assigned_count,after=item.after_assigned_count;
        if(before!=null && after!=null)
          card.append(element('small',`В основном плане назначено: ${before} → ${after}.`,'hint'));
        if(item.backlog_attempted_request_ids?.length)
          card.append(element('small',`Перепроверено ранее нераспределённых: ${item.backlog_attempted_request_ids.length}; назначено: ${item.recovered_request_ids?.length || 0}.`,'hint'));
        if(item.plan_explanation){
          const detail=element('details',null,'notification-explanation');
          detail.append(element('summary',item.llm_status==='ready' ?
              'Почему применён этот план · YandexGPT выделила главное' :
              'Почему применён этот план · проверенные показатели'),
            element('p',item.plan_explanation),
            element('small',item.llm_status==='ready' ?
              'YandexGPT выбрала акценты; факты и числа подставлены из проверенного расчёта.' :
              'Показатели проверены расчётом; модель не формирует назначения.','hint'));
          card.append(detail);
        }
        if(item.llm_status==='unavailable')
          card.append(element('small',item.llm_notice || 'YandexGPT недоступна; назначения рассчитаны и применены без неё.','hint'));
        else if(item.llm_status==null && !item.plan_explanation)
          card.append(element('small','Пояснение YandexGPT для этого применения пока не сохранено.','hint'));
      }
      const left=(item.unassigned_request_ids || []).length;
      if(left)card.append(element('p',`После этого решения без исполнителя: ${left}`,'notification-attention'));
      if(item.cancelled_request_ids?.length)card.append(element('p',`Отменено и удалено из плана: ${item.cancelled_request_ids.length}.`,'hint'));
      if((item.request_ids || []).length)card.append(element('small',`Заявок в событии: ${item.request_ids.length}`,'hint'));
      if((item.changed_request_ids || []).length)card.append(element('small',`Изменены назначения: ${item.changed_request_ids.length}. Причины — в журнале справа.`,'hint'));
      if(['auto_error','replan_required'].includes(item.kind) || left) {
        const action=element('button','Открыть заявки','secondary');action.type='button';
        action.addEventListener('click',openReplanningPool);card.append(action);
      }
      return card;
    }) : [element('p','Событий пока нет. Новые заявки и перепланирования появятся здесь.','hint')]));
    const decisions=data.decision_log || [];
    const assignments=new Map(current ? (state.plan?.routes || []).flatMap(route=>(route.visits || []).map(visit=>[visit.request_id,{engineer_id:route.engineer_id,start:visit.start}])) : []);
    const unresolved=new Set(current ? (state.plan?.unassigned || []).map(item=>item.request_id) : []);
    const cancelled=new Set(current ? [
      ...decisions.filter(item=>item.status==='cancelled' || item.change_type==='cancelled').map(item=>item.request_id),
      ...notifications.flatMap(item=>item.cancelled_request_ids || []),
      ...(data.incoming_requests || []).filter(item=>item.status==='cancelled').map(item=>
        item.request_id || (item.event.type==='emergency'?'emergency:':'request:')+item.id),
    ] : []);
    const currentStatus=item=>assignments.has(item.request_id)?'assigned':unresolved.has(item.request_id)?'unassigned':cancelled.has(item.request_id)?'cancelled':null;
    const wanted=$('decision-filter').value || 'all', query=$('decision-search').value.trim().toLocaleLowerCase('ru-RU');
    const rows=[...decisions].reverse().filter(item=>(wanted==='all' || current && currentStatus(item)===wanted) &&
      (!query || `${item.id} ${item.address || ''} ${item.request_id || ''}`.toLocaleLowerCase('ru-RU').includes(query)));
    $('decision-count').textContent=`${rows.length} из ${decisions.length}`;
    $('decision-log').replaceChildren(...(rows.length ? rows.map(item=>{
      const actual=currentStatus(item), historical=item.status==='cancelled'?'отменена':item.status==='assigned'?'назначена':'без исполнителя';
      const label=actual==='cancelled'?'Отменена':actual==='assigned'?'Сейчас назначена':actual==='unassigned'?'Сейчас без исполнителя':'Историческое решение';
      const card=element('details',null,'decision-card');
      const summary=element('summary');
      summary.append(element('strong',item.id+' · '+(item.address || requestName(item.request_id))),
        element('span',label,actual==='unassigned'?'decision-status attention':'decision-status'));
      card.append(summary);
      const body=element('div',null,'decision-body');
      body.append(element('p',`${item.automatic?'Автопилот':'Диспетчер'} · решение плана №${item.version}: ${historical}.`,'hint'));
      if(current && actual){
        const assignment=assignments.get(item.request_id);
        body.append(element('p',actual==='cancelled'?`Текущий план №${state.version}: заявка отменена и удалена из плана.`:assignment ? `Текущий план №${state.version}: ${engineerName(assignment.engineer_id)}, начало ${time(assignment.start)}.` :
          `Текущий план №${state.version}: заявка без исполнителя.`,actual==='unassigned'?'notification-attention':'hint'));
        if(actual!==item.status)body.append(element('p',`В текущем плане статус изменился: ${label.toLowerCase()}. Историческое объяснение ниже относится к плану №${item.version}.`,actual==='cancelled'?'hint':'notification-attention'));
        else if(item.version!==state.version)body.append(element('p',`Объяснение ниже относится к прежнему плану №${item.version}; назначение и время могли измениться.`,'hint'));
      }
      body.append(element('p',item.llm_explanation ? `Подробности расчёта: ${item.llm_explanation}` :
        item.explanation || 'Объяснение не сохранено.',item.llm_explanation?'application-llm-note':null));
      if(item.plan_explanation){const detail=element('details');detail.append(element('summary','Компромисс плана №'+item.version),
        element('p',item.plan_explanation));if(item.llm_notice)detail.append(element('small',item.llm_notice,'hint'));body.append(detail);}
      card.append(body);return card;
    }) : [element('p',decisions.length?'По этому фильтру решений нет.':'Решений по заявкам пока нет.','hint')]));
  }
  function renderInbox(data, settingsOnly = false) {
    inboxSnapshot=data;
    const settings = data.planning_settings || {mode:'manual',every_n_requests:1,every_n_minutes:null};
    if (!settingsOnly) {
      $('planning-mode').value = settings.mode;
      $('auto-count').value = settings.every_n_requests ?? '';
      $('auto-minutes').value = settings.every_n_minutes ?? '';
    }
    const rows = data.incoming_requests || [];
    const pending = rows.filter(item => item.status === 'new');
    const processed = rows.filter(item => item.status !== 'new');
    const assigned=processed.filter(item=>item.status==='assigned').length;
    const unassigned=processed.filter(item=>item.status==='unassigned').length;
    const cancelled=processed.filter(item=>item.status==='cancelled').length;
    $('incoming-summary').textContent = `Ожидают распределения ${pending.length} · назначены ${assigned} · без исполнителя ${unassigned}`+
      (cancelled?` · отменены ${cancelled}`:'');
    $('incoming-history-summary').textContent = `История обработанных · ${processed.length}`;
    $('planning-mode-summary').textContent = settings.mode === 'autopilot'
      ? `Сейчас включён автопилот: ${[settings.every_n_requests && `после ${settings.every_n_requests} новых заявок`, settings.every_n_minutes && `после ${settings.every_n_minutes} минут ожидания`].filter(Boolean).join(' или ')}.`
      : 'Сейчас ручной режим: план считает и применяет диспетчер.';
    $('quick-planning-mode').value=settings.mode;
    $('quick-planning-mode').title=$('planning-mode-summary').textContent;
    updatePlanningDraft();
    const incomingCard = item => {
      const card = element('article', null, 'intake-card');
      const kind = item.event.type === 'emergency' ? 'Аварийная' : 'Обычная';
      const status = {new:'Ожидает',assigned:'Назначена',unassigned:'Без исполнителя',cancelled:'Отменена'}[item.status] || item.status;
      card.append(element('strong', `${kind} · ${item.id} · ${status}`));
      card.append(element('p', item.address));
      const window = item.event.window_start != null ? ` · окно ${time(item.event.window_start)}–${time(item.event.window_end)}` : '';
      const transport = item.event.required_transport ? ' · транспорт: '+({car:'автомобиль',walk:'пешком',bicycle:'велосипед',public:'общественный транспорт'}[item.event.required_transport] || item.event.required_transport) : '';
      card.append(element('p', `Поступила ${time(item.event.time)}${window} · работа ${item.event.duration_minutes} мин${transport}`,'hint'));
      return card;
    };
    $('incoming-list').replaceChildren(...(pending.length ? [...pending].reverse().map(incomingCard) : [element('p','Сейчас нет новых заявок.','hint')]));
    $('incoming-history-list').replaceChildren(...(processed.length ? [...processed].reverse().slice(0,100).map(incomingCard) : [element('p','Обработанных заявок пока нет.','hint')]));
    renderNotificationCenter(data);
    renderApplicationVisual(data);
    if(streamDemoEnabled)streamProgress();
  }
  function renderState(data) {
    if (!data || data.version == null || !Array.isArray(data.requests) || !Array.isArray(data.engineers)) throw new Error('Сервер вернул неполное состояние.');
    if(state && data.demo_run_id===state.demo_run_id && data.version<state.version)return;
    if(state?.demo_run_id && data.demo_run_id && state.demo_run_id!==data.demo_run_id){
      demoEpoch++;clearDemoWorkspace();
    }
    state = data;
    if(data.demo_stream_enabled) {
      streamDemoEnabled=true;$('stream-demo-panel').hidden=false;
      loadStreamRows();
    }
    renderReplanningPool();
    renderInbox(data);
    const incoming=(data.incoming_requests || []).filter(item=>item.status==='new').length;
    const waiting=(data.replanning_pool || []).filter(item=>item.status==='awaiting_replan').length;
    const unresolved=(data.replanning_pool || []).filter(item=>item.status==='unassigned').length;
    $('replan-overview').textContent = `Новых: ${incoming} · требуют назначения: ${waiting+unresolved}`+
      (data.needs_replanning && !incoming && !waiting ? ' · есть новые факты' : '');
    const applied=data.last_applied;
    $('replan-last-applied').textContent=applied
      ? `Последний план применён: №${applied.version}${applied.automatic===true?' автопилотом':applied.automatic===false?' диспетчером':''}. Новых обработано ${applied.incoming_count}, изменений назначений ${applied.changed_count}. `
      : 'План ещё не пересчитывался.';
    $('replan-guidance').textContent=unresolved && !incoming && !waiting
      ? 'Остались заявки без исполнителя. Проверьте ресурсы и пересчитайте план.'
      : incoming || waiting ? 'Рассчитайте варианты и выберите подходящий.' :
        'Все заявки текущего плана распределены.';
    $('replan-primary').textContent = unresolved && !incoming && !waiting && !data.needs_replanning ? 'Повторить расчёт' : 'Рассчитать варианты';
    $('remote-update').hidden = true;
    $('region').textContent = data.region || '—'; $('status').classList.toggle('attention', !!data.needs_replanning); $('version').textContent = data.version;
    $('clock').textContent = time(data.time); $('event-time').value = time(data.time); $('event-time').min = time(data.time);
    const count = (data.plan && data.plan.routes || []).reduce((sum, route) => sum + (route.visits || []).length, 0);
    const coverage=data.assignment_summary;
    $('coverage').textContent = (coverage?.assigned ?? Math.max(0,count-waiting)) + ' / ' + (coverage?.total ?? data.requests.length);
    $('coverage-pending').textContent=coverage?.requires_assignment ?? waiting+unresolved;
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
    $('replan-needed').hidden = true; $('go-replan').hidden = !data.needs_replanning;
    $('fact-next-step').textContent=data.needs_replanning ? 'План требует пересчёта по новым статусам.' : '';
    if(data.needs_replanning && data.planning_settings?.mode==='autopilot')$('fact-next-step').textContent+=' Автопилот обрабатывает новые заявки; текущие изменения можно рассчитать сейчас.';
    populateSelect('fact-location', data.locations || [], item => item.label || item.id, 'Без уточнения места');
    const versions = data.versions && data.versions.length ? data.versions : [{version: data.version, time: data.time, label: 'Текущий план'}];
    $('plan-version').replaceChildren(...versions.map(item => { const option = element('option', '№ ' + item.version + ' · ' + time(item.time) + ' · ' + (item.version === data.version ? 'Текущий план' : (item.label || 'План'))); option.title=item.label || 'План'; option.value = item.version; return option; }));
    $('plan-version').value = String(data.version); showPlan();
    renderFacts(); renderCompletions(); factFields(); targets(); resetCandidates();
    window.dispatchEvent(new CustomEvent('planning-state',{detail:{version:data.version,time:data.time,demoRunId:data.demo_run_id,
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
    $('plan-frame').src = appUrl('/dispatcher?version=' + encodeURIComponent(version) +
      (state.demo_run_id?'&demo_run_id='+encodeURIComponent(state.demo_run_id):''));
    const unresolved = (state.replanning_pool || []).filter(item => item.status === 'unassigned').length;
    $('plan-context').textContent = historical ? 'История · версия ' + version + '. Только просмотр; текущий план не изменён.' : state.needs_replanning ? 'Есть новые факты. Показан последний утверждённый план; пересчитайте его в разделе «Заявки и изменения».' : unresolved ? `Текущий план · версия ${version}. Без исполнителя: ${unresolved}; откройте «Заявки и изменения».` : '';
    $('plan-context').hidden=!$('plan-context').textContent;
    $('plan-context').classList.toggle('attention', !historical && (!!state.needs_replanning || unresolved > 0));
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
      [engineerName(fact.engineer_id), factLabels[fact.status] || fact.status, requestName(fact.request_id), time(fact.time), location ? location.label : fact.location_id || '—', fact.remaining_minutes ?? '—', fact.recorded_at && !fact.recorded_at.startsWith('unknown') ? new Date(fact.recorded_at).toLocaleString('ru-RU', {hourCycle:'h23'}) : '—'].forEach(value => row.append(element('td', value)));
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
    clearGpsDelay();
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
  function renderExplanationCard(target, card, {applied=false,ai=false,unavailable=false}={}) {
    target.replaceChildren();
    const box=element('div',null,'explanation-card');
    box.append(element('h4',(applied?'Применён вариант':'Рекомендуем')+' «'+card.label+'»'),element('p',card.summary,'explanation-summary'));
    for(const [key,label] of [['reasons','Почему'],['tradeoffs','Компромисс'],['attention',applied?'Требует внимания':'Перед применением']]) {
      if(!card[key]?.length)continue;
      const section=element('div',null,'explanation-section'+(key==='attention'?' explanation-attention':''));
      section.append(element('strong',label));
      const list=element('ul');
      for(const text of card[key])list.append(element('li',text));
      section.append(list);box.append(section);
    }
    if(card.note)box.append(element('small',card.note,'explanation-note'));
    box.append(element('small',ai?'Пояснение с ИИ · показатели проверены':unavailable?'ИИ недоступен · объяснение по расчёту':'Объяснение по расчёту','explanation-source'));
    target.append(box);
  }
  function renderRecommendation() {
    const choice = batch.candidates.find(c => c.id === recommendation?.candidate_id);
    const canonical = canonicalCandidate(batch, choice?.id);
    $('ai-panel').hidden = !batch.candidates.length || (!batch.llm_enabled && !recommendation);
    $('ai-recommend').hidden = !batch.llm_enabled;
    $('ai-result').textContent = recommendationPending ? 'Сравниваем варианты…' : choice
      ? 'Рекомендуем «' + strategyName(choice) + '». ' + [recommendation.notice, recommendation.text].filter(Boolean).join(' ')
      : recommendation?.error || 'Сравните варианты самостоятельно или запросите объяснение с ИИ.';
    if(!recommendationPending && choice && recommendation?.card)
      renderExplanationCard($('ai-result'),recommendation.card,{ai:recommendation.source==='yandexgpt',unavailable:recommendation.source!=='yandexgpt'});
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
      if (batch === current) recommendation = {error:'Не удалось получить объяснение. Попробуйте ещё раз или сравните варианты самостоятельно.'};
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
      recommendation = null;
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
    const currentAssigned=state.assignment_summary?.assigned ?? (state.plan?.routes || []).reduce((sum,route)=>sum+(route.visits || []).length,0);
    const best=ordered.reduce((winner,candidate)=>candidate.facts.assigned>(winner?.facts.assigned ?? -1)?candidate:winner,null);
    $('preview-coverage').hidden=!best;
    if(best)$('preview-coverage').textContent=`Действующих назначений сейчас: ${currentAssigned}/${state.requests.length}. Лучший рассчитанный охват: ${best.facts.assigned}/${best.facts.assigned+best.facts.unassigned}. Расчёт не меняет назначения до «Применить план».`;
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
    if (data.event?.type === 'replan' && !(data.incoming_ids || []).length &&
        ordered.length && ordered.every(candidate=>candidate.facts.unassigned >= (state.plan?.unassigned || []).length) &&
        (state.plan?.unassigned || []).length) {
      notes.push('Ни один вариант не уменьшил число заявок без исполнителя. Применение плана с тем же охватом не очистит этот список; проверьте доступность инженеров и окна клиентов.');
    }
    if (data.insertion_policy) notes.push('Новая заявка добавляется без изменения прежних назначений и времени начала.');
    if (merged) notes.push(ordered.length === 1 ? 'Один уникальный план: все стратегии нашли одинаковые маршруты и время.' : 'Совпавшие планы объединены в одну карточку.');
    if (!newEmergency && !responseTargets && merged) notes.push('Аварий в остатке дня нет: «Быстрее к аварии» совпадает с «Меньше изменений».');
    if (riskReady) notes.push('Прогнозные риски — оценка, а не гарантия.');
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
      card.append(element('p', aliases.length ? 'Совпадает с ' + aliases.map(id => '«' + strategyName({id}) + '»').join(', ') : '', 'duplicate'));
      const f = candidate.facts || {}, dl = element('dl');
      card.append(element('p', 'Назначено: аварии ' + f.emergency_assigned + '/' + f.emergency_total + ' · подключения ' + f.connection_assigned + '/' + f.connection_total, 'priority-coverage'));
      const metrics = [['Без назначения', f.unassigned], ['Смена исполнителя', f.reassigned], ['Дорога, мин', f.travel_minutes]];
      const extraMetrics = [['Из них предотвратимые', f.avoidable_reassigned], ['Сдвиги начала, мин', f.time_shift_minutes], ['Расстояние, км', f.distance_m == null ? '—' : (f.distance_m / 1000).toFixed(1)]];
      const risk = candidate.risk?.status === 'ready' && candidate.risk.summary ? candidate.risk : null;
      if (risk) metrics.unshift(
        ['Риск нарушения срока', (risk.summary.p_sla_breach * 100).toFixed(0) + '%'],
        ['Изменено обещаний', risk.affected_clients]);

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
      const more = element('details', null, 'candidate-details');
      const moreList = element('dl');
      extraMetrics.forEach(([label, value]) => { const row = element('div'); row.append(element('dt', label), element('dd', value ?? '—')); moreList.append(row); });
      more.append(element('summary', 'Ещё показатели'), moreList); card.append(more);
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
  $('emergency-window-enabled').addEventListener('change', emergencyWindowFields);
  $('call-off-duty-engineer').addEventListener('change', () => {
    $('call-off-duty-confirmed').checked = false; $('call-off-duty-reason').value = '';
    $('call-off-duty-window-start').value = ''; $('call-off-duty-window-end').value = '';
    offDutyFields();
  });
  $('work-kind').addEventListener('change', defaultDuration);
  $('event-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy || !state) return;
    let payload;
    try { payload = eventPayload(state.version, {id: uniqueEventId(), type: $('event-type').value, time: $('event-time').value, target: $('event-target').value,
      engineerIds:[...$('event-engineers').querySelectorAll('input:checked:not(:disabled)')].map(input=>input.value),
      duration: $('duration').value, router: $('router').value, tvBox:$('new-tv').value, workKind:$('work-kind').value, windowStart:$('window-start').value, windowEnd:$('window-end').value, redirect: $('redirect').checked, preserve: true,
      requiredTransport:$('required-transport').value, emergencyWindowEnabled:$('emergency-window-enabled').checked,
      emergencyWindowStart:$('emergency-window-start').value, emergencyWindowEnd:$('emergency-window-end').value,
      offDutyEngineerId:$('call-off-duty-engineer').value, offDutyConfirmed:$('call-off-duty-confirmed').checked, offDutyReason:$('call-off-duty-reason').value,
      offDutyWindowStart:$('call-off-duty-window-start').value, offDutyWindowEnd:$('call-off-duty-window-end').value,
      localScopeEngineerIds}); }
    catch (error) { $('error').textContent = error.message; $('error').hidden = false; return; }
    resetCandidates('Расчёт допустимых вариантов…'); busy = true; controls(); $('error').hidden = true; $('status').textContent = 'Расчёт может занять несколько секунд…';
    try { renderBatch(await request('/api/preview', payload)); $('status').textContent = 'Расчёт завершён. Выберите вариант и проверьте изменения.'; $('comparison-panel').scrollIntoView({block:'start'}); $('comparison-panel').focus(); }
    catch (error) { failure(error); $('error').scrollIntoView({block:'center'}); }
    finally { busy = false; controls(); }
  });
  $('approve').addEventListener('click', async () => {
    if (busy || !state || !batch || !selected || batch.version !== state.version) return;
    const payload = {version: state.version, batch_id: batch.id, candidate_id: selected.id};
    busy = true; controls(); $('error').hidden = true; $('status').textContent = 'Утверждение выбранного плана…';
    try { renderState(await request('/api/approve', payload)); const unresolved=(state.replanning_pool || []).filter(item=>item.status==='unassigned').length; $('status').textContent = unresolved ? `План утверждён. Без исполнителя осталось ${unresolved}; проверьте заявки, требующие внимания.` : 'План утверждён. Сохранена версия ' + state.version + '.'; }
    catch (error) { failure(error); }
    finally { busy = false; controls(); }
  });
  const tabs = ['plan','events','facts','gps','crew','risk','notifications','settings','demo'];
  const pageNames={plan:'План дня',events:'Заявки и изменения',facts:'Бригады',gps:'Карта и связь',crew:'Состав и оборудование',risk:'Контроль дня',notifications:'История',settings:'Настройки района',demo:'Демонстрация'};
  const mobileMenu=window.matchMedia?.('(max-width:900px)');
  function syncMenuAccess(){ $('sidebar').inert=!!mobileMenu?.matches&&!$('sidebar').classList.contains('is-open'); }
  mobileMenu?.addEventListener('change',syncMenuAccess);syncMenuAccess();
  function closeMenu() {
    $('sidebar').classList.toggle('is-open',false);$('menu-backdrop').hidden=true;
    $('menu-toggle').setAttribute('aria-expanded','false');syncMenuAccess();
  }
  $('menu-toggle').addEventListener('click',()=>{
    const open=!$('sidebar').classList.contains('is-open');
    $('sidebar').classList.toggle('is-open',open);$('menu-backdrop').hidden=!open;
    $('menu-toggle').setAttribute('aria-expanded',String(open));syncMenuAccess();
    if(open)$('tab-plan').focus();
  });
  $('menu-backdrop').addEventListener('click',closeMenu);
  window.addEventListener('keydown',event=>{if(event.key==='Escape'&&$('sidebar').classList.contains('is-open')){closeMenu();$('menu-toggle').focus();}});

  $('record-fact-action').addEventListener('click',()=>{activateTab('facts');$('fact-engineer').focus();});
  $('new-request-action').addEventListener('click',()=>{
    if(busy||!state)return;activateTab('events');$('event-type').value='new_request';
    targets();defaultDuration();resetCandidates();$('event-target').focus();
  });
  $('crew-settings-action').addEventListener('click',()=>activateTab('crew'));
  $('crew-map-action').addEventListener('click',()=>activateTab('gps'));
  $('demo-gps-action').addEventListener('click',()=>activateTab('gps'));
  $('risk-demo').addEventListener('click',()=>activateTab('risk'));
  let crewDirty=false,crewBusy=false,crewLoading=false;
  function loadCrew(){
    if(crewDirty || crewBusy)return;
    crewLoading=true;
    $('crew-frame').src=appUrl('/crew?embedded=1');
  }
  $('crew-discard').addEventListener('click',()=>{
    if(crewBusy)return;
    $('crew-frame').contentWindow?.postMessage({type:'crew-discard'},new URL(location.href).origin);
    $('error').hidden=true;
  });
  window.addEventListener('message',event=>{
    if(event.origin!==new URL(location.href).origin || event.source!==$('crew-frame').contentWindow)return;
    if(event.data?.type==='crew-editing'){
      crewDirty=event.data.dirty===true;crewBusy=event.data.busy===true;
      $('crew-discard').hidden=!crewDirty;$('crew-discard').disabled=crewBusy;
      if(crewLoading && !crewBusy){
        crewLoading=false;
        requestAnimationFrame(()=>{if(!$('panel-crew').hidden){$('crew-frame').resizePlan?.();window.scrollTo?.({top:0,behavior:'instant'});}});
      }
    }else if(event.data?.type==='crew-applied'){
      crewDirty=false;crewBusy=false;$('crew-discard').hidden=true;reload();activateTab('plan');
    }
  });
  window.addEventListener('region-change',event=>{
    if(!crewDirty&&!crewBusy)return;
    event.preventDefault();$('error').textContent='В составе есть несохранённые правки. Примените их или нажмите «Отменить правки состава».';$('error').hidden=false;
  });
  window.addEventListener('beforeunload',event=>{if(crewDirty){event.preventDefault();event.returnValue='';}});
  window.addEventListener('hashchange',()=>{const name=location.hash?.slice(1);if(tabs.includes(name))activateTab(name);});
  function activateTab(name, keepMenu=false) {
    if(!tabs.includes(name))return;
    window.history?.replaceState(null,'',appUrl('/#'+name));
    const fromDrawer=$('sidebar').classList.contains('is-open');
    if(!keepMenu){closeMenu();window.scrollTo?.({top:0,behavior:'instant'});}$('page-title').textContent=pageNames[name];
    for(const id of ['record-fact-action','new-request-action'])$(id).hidden=['crew','settings','demo'].includes(name);
    if(fromDrawer&&!keepMenu){$('page-title').tabIndex=-1;$('page-title').focus();}
    $('page-caption').textContent=['crew','settings','demo'].includes(name)?'УПРАВЛЕНИЕ':'РАБОЧИЙ ДЕНЬ';
    tabs.forEach(tab => { const active = tab === name; $('tab-' + tab).setAttribute('aria-selected', String(active)); $('tab-' + tab).tabIndex = active ? 0 : -1; $('panel-' + tab).hidden = !active; });
    if (name === 'notifications' && inboxSnapshot) {
      const notices=inboxSnapshot.dispatch_notifications || [];
      lastSeenNotificationId=notices[notices.length-1]?.id || null;
      renderInbox(inboxSnapshot,true);
    }
    if (name === 'crew'){renderCommitments(state,false);loadCrew();requestAnimationFrame(()=>$('crew-frame').resizePlan?.());}
    if (name === 'plan') requestAnimationFrame(() => $('plan-frame').resizePlan?.());
    window.dispatchEvent(new CustomEvent('workbench-tab', {detail: {name}}));
  }
  let gpsDelayDraft=null;
  function clearGpsDelay() {
    gpsDelayDraft=null; $('gps-delay-note').hidden=true;
    $('facts-form').querySelector('button[type="submit"]').textContent='Сохранить статус';
  }
  window.addEventListener('gps-intent', event => {
    if (!state || busy) return;
    localScopeEngineerIds=[];
    if(event.detail.action==='delay') {
      const draft=event.detail.draft;
      if(draft.version!==state.version) {
        $('error').textContent='Загрузите изменения плана и повторите оценку GPS.';$('error').hidden=false;return;
      }
      clearGpsDelay(); $('fact-edit').value='';$('fact-late').checked=false;
      $('fact-engineer').value=draft.engineer_id;factRequests();
      $('fact-status').value='traveling';$('fact-request').value=draft.request_id;
      $('fact-time').value=time(draft.time);$('fact-location').value=draft.location_id||'';
      $('fact-remaining').value=draft.remaining_minutes;factFields();
      gpsDelayDraft=draft;
      const note=$('gps-delay-note');note.replaceChildren(element('p',draft.explanation));
      note.append(element('p','Предварительное влияние при сохранении порядка заявок и нормативных переездов. Итог проверит оптимизатор:'));
      for(const item of draft.affected)note.append(element('p','№ '+item.request_id+': '+time(item.planned_start)+' → '+time(item.estimated_start)+
        (item.window_missed?' · окно клиента нарушено':item.shift_minutes?' · сдвиг '+item.shift_minutes+' мин':' · время сохранено')));
      const cancel=element('button','Отменить предложение','secondary');cancel.type='button';cancel.onclick=clearGpsDelay;note.append(cancel);
      note.hidden=false;
      $('facts-form').querySelector('button[type="submit"]').textContent='Подтвердить доезд и открыть пересчёт';
      activateTab('facts');$('fact-remaining').focus();
    } else if (event.detail.action === 'facts') {
      clearGpsDelay();
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
    if(event.detail?.planVersion != null && event.detail.planVersion!==state.version){
      $('error').textContent='Показатели относятся к другой версии плана. Обновите контроль дня.';$('error').hidden=false;return;
    }
    clearGpsDelay();
    if (event.detail?.action === 'completions') {
      activateTab('facts'); $('completion-panel').scrollIntoView({block:'center'}); return;
    }
    if (event.detail?.action === 'facts') {
      activateTab('facts');$('fact-edit').value='';$('fact-late').checked=false;$('correction-reason').value='';
      for(const id of ['fact-remaining','fact-router','fact-tv'])$(id).value='';
      $('fact-time').value=time(state.time);$('fact-request').value='';
      if(event.detail.engineerId && [...$('fact-engineer').options].some(o=>o.value===event.detail.engineerId))$('fact-engineer').value=event.detail.engineerId;
      const current=(state.statuses || []).find(f=>f.engineer_id===$('fact-engineer').value);
      $('fact-status').value=current?.status==='working'?'working':event.detail.requestId?'traveling':'available';
      factRequests();
      const requested=[...$('fact-request').options].find(o=>o.value===event.detail.requestId);
      if(requested && (current?.status!=='working'||current.request_id===event.detail.requestId))$('fact-request').value=requested.value;
      factFields();$('fact-status').focus();
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
      const offset = ['ArrowRight','ArrowDown'].includes(event.key) ? 1 : ['ArrowLeft','ArrowUp'].includes(event.key) ? -1 : 0;
      if (!offset && !['Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs[tabs.length - 1] : tabs[(index + offset + tabs.length) % tabs.length];
      activateTab(next,true); $('tab-' + next).focus();
    });
  });
  $('plan-version').addEventListener('change', showPlan);
  $('quick-planning-mode').addEventListener('change',async()=>{
    const saved=inboxSnapshot?.planning_settings || {mode:'manual',every_n_requests:1,every_n_minutes:null};
    const mode=$('quick-planning-mode').value;
    if(busy || !state || !['manual','autopilot'].includes(mode) || mode===saved.mode){$('quick-planning-mode').value=saved.mode;return;}
    // Switch the persisted mode only; an unsaved threshold draft must survive.
    const countDraft=$('auto-count').value,minutesDraft=$('auto-minutes').value;
    const thresholdsDirty=countDraft!==String(saved.every_n_requests??'') || minutesDraft!==String(saved.every_n_minutes??'');
    const every_n_requests=saved.every_n_requests??(saved.every_n_minutes?null:1);
    busy=true;controls();$('error').hidden=true;
    try{
      renderState(await request('/api/inbox/settings',{version:state.version,mode,every_n_requests,every_n_minutes:saved.every_n_minutes??null}));
      $('status').textContent=mode==='autopilot'?'Автопилот включён. Новые заявки обрабатываются по сохранённым порогам.':'Ручной режим включён. План применяет диспетчер.';
    }catch(error){failure(error);}
    finally{
      $('quick-planning-mode').value=inboxSnapshot?.planning_settings?.mode || saved.mode;
      if(thresholdsDirty){$('auto-count').value=countDraft;$('auto-minutes').value=minutesDraft;}
      busy=false;controls();
    }
  });
  $('planning-mode').addEventListener('change',updatePlanningDraft);
  $('auto-count').addEventListener('input',updatePlanningDraft);
  $('auto-minutes').addEventListener('input',updatePlanningDraft);
  $('planning-save').addEventListener('click', async () => {
    if (busy || !state || !updatePlanningDraft()) return;
    const payload={version:state.version,...planningDraft()};
    busy=true;controls();$('error').hidden=true;
    try {renderState(await request('/api/inbox/settings',payload));$('status').textContent='Режим планирования сохранён.';}
    catch(error){failure(error);}finally{busy=false;controls();}
  });
  $('stream-demo-start').addEventListener('click',()=>{
    if(!streamDemoEnabled || !streamRows || streamRunning || demoResetting)return;
    if(!$('planning-save').disabled){
      $('stream-demo-status').textContent='Сначала сохраните выбранный режим распределения.';return;
    }
    streamGeneration++;streamRunning=true;streamProgress();streamNext();
  });
  $('stream-demo-pause').addEventListener('click',()=>{
    stopStream();streamProgress();
  });
  $('stream-demo-reset').addEventListener('click',restartDemo);
  $('notifications-replan').addEventListener('click',openReplanningPool);
  for(const id of ['decision-search','decision-filter'])$(id).addEventListener(id==='decision-search'?'input':'change',()=>{
    if(inboxSnapshot && state)renderNotificationCenter(inboxSnapshot);
  });
  $('intake-file').addEventListener('change', async () => {
    const file=$('intake-file').files?.[0];if(!file)return;
    if(file.size>60000){$('intake-feedback').textContent='Файл слишком большой для одного пакета.';return;}
    $('intake-json').value=await file.text();$('intake-feedback').textContent='Файл загружен. Проверьте и нажмите «Принять заявки».';
  });
  $('intake-submit').addEventListener('click', async () => {
    if(busy||!state)return;
    let payload;
    try {
      const parsed=JSON.parse($('intake-json').value);
      payload=Array.isArray(parsed)?{requests:parsed}:parsed && parsed.id?{request:parsed}:parsed;
      if(!payload || typeof payload!=='object')throw new Error('Ожидается JSON заявки или массива requests.');
    } catch(error){$('intake-feedback').textContent='Проверьте JSON: '+error.message;return;}
    busy=true;controls();$('intake-feedback').textContent='Принимаем заявки…';$('error').hidden=true;
    try {
      const result=await request('/api/inbox/requests',payload);
      renderState(result.state);
      $('intake-feedback').textContent=`Принято: ${result.added}; повторов без изменений: ${result.duplicates}.`;
      $('intake-json').value='';$('intake-file').value='';
      $('status').textContent=result.added?'Заявки поступили в очередь.':'Все заявки уже были получены.';
    } catch(error){if(!error.staleDemoRun)$('intake-feedback').textContent=error.message;}
    finally{busy=false;controls();}
  });
  function runQueueReplan() {
    if(!state || busy)return;
    $('event-type').value='replan';targets();resetCandidates();
    $('event-time').value=time(state.time);
    $('event-form').requestSubmit();
  }
  function renderLiveState(data) {
    const draft=!$('planning-save').disabled?planningDraft():null;
    renderState(data);
    if(draft){
      $('planning-mode').value=draft.mode;$('auto-count').value=draft.every_n_requests ?? '';
      $('auto-minutes').value=draft.every_n_minutes ?? '';updatePlanningDraft();
    }
  }
  $('replan-primary').addEventListener('click', runQueueReplan);
  $('fact-status').addEventListener('change', () => { factRequests(); factFields(); });
  $('fact-engineer').addEventListener('change', () => { factRequests(); factFields(); });
  $('go-replan').addEventListener('click', () => {activateTab('events');runQueueReplan();});
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
  $('fact-remaining').addEventListener('input',()=>{
    if(gpsDelayDraft)$('gps-delay-note').textContent='Остаток изменён диспетчером. Предварительное сравнение скрыто: новые последствия рассчитает оптимизатор после подтверждения.';
  });
  $('facts-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy || !state) return;
    let payload;
    try { payload = factPayload(state.version, {engineer_id: $('fact-engineer').value, time: $('fact-time').value, status: $('fact-status').value, request_id: $('fact-request').value, location_id: $('fact-location').value, remaining_minutes: $('fact-remaining').value, consumed_router: $('fact-router').value, consumed_tv_box: $('fact-tv').value}); }
    catch (error) { $('error').textContent = error.message; $('error').hidden = false; return; }
    if($('fact-edit').value){payload.fact_id=$('fact-edit').value;payload.correction_reason=$('correction-reason').value;}
    const delay=gpsDelayDraft;
    if(delay) {
      if(state.version!==delay.version || payload.engineer_id!==delay.engineer_id || payload.request_id!==delay.request_id ||
          payload.status!=='traveling' || payload.fact_id) {
        clearGpsDelay();$('error').textContent='Параметры подтверждения изменились. Получите оценку GPS заново.';$('error').hidden=false;return;
      }
      if(Date.now()>delay.expires_at_ms) {
        clearGpsDelay();$('error').textContent='GPS-оценка устарела. Получите свежую перед подтверждением.';$('error').hidden=false;return;
      }
      payload.gps_point_measured_at_ms=delay.point_measured_at_ms;
      payload.comment=delay.comment+' Подтверждённый остаток: '+payload.remaining_minutes+' мин.';
    }
    busy = true; controls(); $('error').hidden = true; $('status').textContent = 'Запись фактического статуса…';
    let saved=false;
    try {
      renderState(await request(delay?'/api/gps/confirm-delay':'/api/facts', payload)); saved=true;clearGpsDelay();
      $('event-type').value='replan';targets();$('event-time').value=time(state.time);
      $('status').textContent=delay?'Время доезда подтверждено. Рассчитываем влияние на план.':'Статус сохранён.';
    } catch (error) { failure(error); }
    finally { busy = false; controls(); }
    if(saved && delay){activateTab('events');$('event-form').requestSubmit();}
  });
  $('reload').addEventListener('click', reload);
  $('load-remote').addEventListener('click', reload);
  let polling = false;
  setInterval(async () => {
    if (!state || busy || polling || document.hidden) return;
    polling = true;
    const epoch=demoEpoch;
    try {
      const revision = await request('/api/revision');
      if(epoch!==demoEpoch)return;
      if(revision.demo_run_id && revision.demo_run_id!==state.demo_run_id){
        stopStream();await reload();
        $('status').textContent='Симуляция начата заново. Загружен исходный день без прежней истории.';
        return;
      }
      const inbox=await request('/api/inbox');
      if(epoch!==demoEpoch)return;
      if(inbox.demo_run_id && inbox.demo_run_id!==state.demo_run_id){stopStream();await reload();return;}
      const serverVersion=inbox.version ?? revision.version;
      if (state && serverVersion !== state.version) {
        const autoApplied=inbox.last_applied?.automatic===true && inbox.last_applied.version>state.version;
        if(!batch && (autoApplied || streamDemoEnabled)) {
          renderLiveState(await request('/api/state'));
          $('status').textContent=autoApplied?'Автопилот применил план; расписание дня обновлено.':
            'Поступления и применения плана обновлены.';
        } else {
          $('remote-update-text').textContent = 'Есть новые факты или назначения с другого устройства (версия ' + serverVersion + '). Загрузите изменения перед продолжением.';
          $('remote-update').hidden = false;
        }
      } else renderInbox(inbox, true);
    } catch(error) {
      if(error.staleDemoRun || epoch!==demoEpoch)return;
      $('remote-update-text').textContent = 'Связь с сервером потеряна. Показанные данные могут быть устаревшими.';
      $('remote-update').hidden = false;
    } finally { polling = false; }
  }, 2000);
  reload();
  const initial=location.hash?.slice(1) || (location.pathname?.endsWith('/crew')?'crew':null);
  if(tabs.includes(initial))activateTab(initial);
})();

// The embedded plan scrolls with the workspace, not inside a second viewport.
if (typeof document !== 'undefined') for(const id of ['plan-frame','crew-frame']) document.getElementById(id)?.addEventListener('load', function () {
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
