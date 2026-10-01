(function () {
  'use strict';
  const labels = {planned:'Назначена', traveling:'В пути', working:'В работе', completion_pending:'Ожидает подтверждения', completed:'Выполнена', interrupted:'Приостановлена', available:'На смене', unavailable:'Недоступен', pending_replan:'Ожидает перепланирования'};
  const actions = {traveling:'Выехать на заявку', working:'Начать работу', completed:'Сообщить о выполнении', interrupted:'Приостановить работу', available:'Начать смену', unavailable:'Не могу работать'};
  const absenceReasons = {sick:'Болезнь', transport:'Проблема с транспортом', personal:'Личные обстоятельства', other:'Другое'};
  const skills = {local:'Локальный ремонт', connection:'Подключение', emergency:'Аварийные работы'};
  function time(value) {
    if (!Number.isInteger(value)) return '—';
    return String(Math.floor(value / 60)).padStart(2, '0') + ':' + String(value % 60).padStart(2, '0');
  }
  function taskLabel(task) { return task.display_label || ('№ ' + task.id); }
  function minute(value) {
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) throw new Error('Укажите время в пределах рабочего дня.');
    return Number(value.slice(0, 2)) * 60 + Number(value.slice(3));
  }
  function actionPayload(version, engineerId, status, taskId, input) {
    const payload = {version, engineer_id:engineerId, status, time:minute(input.time), comment:input.comment.trim()};
    if (taskId && ['traveling','working','completed','interrupted'].includes(status)) payload.request_id = taskId;
    if (['available','traveling'].includes(status)) {
      if (status === 'traveling' && !input.location) throw new Error('Укажите точку начала выезда.');
      if (input.location) payload.location_id = input.location;
    }
    if (['working','interrupted','traveling'].includes(status)) {
      const remaining = Number(input.remaining);
      if (!Number.isInteger(remaining) || remaining < 1 || remaining > 1440) throw new Error('Укажите положительное оставшееся время.');
      payload.remaining_minutes = remaining;
    }
    if (['completed','interrupted'].includes(status)) {
      for (const [field, value] of [['consumed_router', input.router], ['consumed_tv_box', input.tv]]) {
        const count = Number(value);
        if (value === '' || !Number.isInteger(count) || count < 0) throw new Error('Укажите фактический расход оборудования, включая нули.');
        payload[field] = count;
      }
    }
    if (['interrupted','unavailable'].includes(status) && payload.comment.length < 3) throw new Error('Кратко опишите проблему для диспетчера.');
    return payload;
  }
  function validDate(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return false;
    const [year, month, day] = value.split('-').map(Number);
    const parsed = new Date(Date.UTC(year, month - 1, day));
    return parsed.getUTCFullYear() === year && parsed.getUTCMonth() === month - 1 && parsed.getUTCDate() === day;
  }
  function unavailabilityPayload(version, engineerId, input, currentDate, currentMinute, shiftStart = 0, shiftEnd = 1440, existingPeriod = null) {
    const startDate = input.startDate, endDate = input.endDate;
    if (!validDate(startDate) || !validDate(endDate)) throw new Error('Укажите корректные даты периода.');
    const startMinute = minute(input.startTime), endMinute = minute(input.endTime);
    if (endDate < startDate || (endDate === startDate && endMinute <= startMinute)) throw new Error('Конец периода должен быть позже начала.');
    const editingActive = !!existingPeriod?.is_active;
    if (editingActive && (startDate !== existingPeriod.start_date || input.startTime !== existingPeriod.start_time)) throw new Error('Начало действующего отсутствия сохраняется.');
    if (!editingActive && currentDate && (startDate < currentDate || (startDate === currentDate && startMinute < currentMinute))) throw new Error('Начало периода не может быть раньше текущего времени плана.');
    if (editingActive && currentDate && (endDate < currentDate || (endDate === currentDate && endMinute <= currentMinute))) throw new Error('Для возвращения сейчас нажмите «Я снова доступен».');
    if (!Object.hasOwn(absenceReasons, input.reason)) throw new Error('Выберите причину недоступности.');
    const comment = (input.comment || '').trim();
    if (comment.length < 3 || comment.length > 1000) throw new Error('Кратко поясните ситуацию для диспетчера.');
    const payload = {version, engineer_id:engineerId, start_date:startDate, start_time:input.startTime, end_date:endDate, end_time:input.endTime, reason:input.reason, comment};
    return payload;
  }
  function declinePayload(version, engineerId, requestId, input, currentMinute) {
    if (!requestId) throw new Error('Выберите заявку.');
    const comment = (input.comment || '').trim();
    if (comment.length < 3 || comment.length > 1000) throw new Error('Опишите причину, по которой не можете выехать.');
    const reportTime = minute(input.time);
    if (currentMinute != null && reportTime < currentMinute) throw new Error('Время сообщения не может быть раньше текущего времени плана.');
    return {version, engineer_id:engineerId, request_id:requestId, time:reportTime, comment};
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {time, minute, taskLabel, actionPayload, unavailabilityPayload, declinePayload};
  if (typeof document === 'undefined') return;
  const $ = id => document.getElementById(id);
  const appFetch = (...args) => window.FieldRouter ? window.FieldRouter.fetch(...args) : fetch(...args);
  const appUrl = path => window.FieldRouter ? window.FieldRouter.url(path) : path;
  let rosterVersion = null, currentClock = null;
  let data = null, engineerId = '', filter = 'pending', tab = 'tasks', selectedId = null;
  let action = null, decline = null, absence = null, cancelAbsence = null, sending = false, connected = false, loading = false, generation = 0;
  let map = null, mapLayer = null, mapBounds = [], polling = false;
  const dialog = $('task-dialog');
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function button(text, className, handler) {
    const node = el('button', text, className); node.type = 'button';
    node.addEventListener('click', handler); return node;
  }
  function notice(text) {
    $('updates').textContent = text;
    $('updates').hidden = !text || (!$('replan-note').hidden && ['Диспетчер уведомлён.', 'Вы снова доступны. Ожидаем задания.'].includes(text));
  }
  function error(text) { $('error').textContent = text; $('error').hidden = !text; }
  function liveClock() { return currentClock?.mode === 'live'; }
  function currentMinute() { return currentClock?.time ?? data.time; }
  function clockIdentity(clock) { return clock ? [clock.mode, clock.date, clock.server_date].join('/') : null; }
  function clockContext() {
    return dateLabel(data.date) + (liveClock() ? ' · ' + time(currentMinute()) + ' МСК' : ' · время сценария ' + time(currentMinute()));
  }
  function currentTask() { return data && data.tasks.find(t => t.id === selectedId); }
  function unavailableNow() { return !!(data && (data.active_absence || data.indefinite_absence || data.status?.status === 'unavailable' || data.return_confirmation_required)); }
  function pendingReplan(task) { return !['completed','completion_pending'].includes(task.status) && (!!data.return_pending_replan || !!data.location_confirmation_pending || task.status === 'pending_replan' || (data.pending_reassignment_ids || []).includes(task.id)); }
  function taskStatus(task) { return pendingReplan(task) ? 'pending_replan' : task.status; }
  function factPair(dl, name, value, wide) {
    const row = el('div', null, wide ? 'wide' : '');
    row.append(el('dt', name), el('dd', value)); dl.append(row);
  }
  function dateLabel(value) {
    return new Date(value + 'T12:00:00').toLocaleDateString('ru-RU', {day:'numeric', month:'long', year:'numeric'});
  }
  function windows(task) { return task.windows.map(w => time(w.start) + '–' + time(w.end)).join(' или '); }
  function taskType(task) { return task.hd_type || task.bk_type || skills[task.skill] || 'Выездная работа'; }
  function syncStatus(ok) {
    connected = ok; $('connection').classList.toggle('offline', !ok);
    $('connection').textContent = ok ? 'Обновлено ' + new Date().toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit', hourCycle:'h23'}) : 'Нет связи · показаны последние полученные данные';
    controls();
  }
  function controls() {
    $('engineer-select').disabled = sending || !$('engineer-select').options.length;
    $('refresh').disabled = sending;
    $('shift-start').disabled = sending || !connected || !data;
    $('shift-return').disabled = sending || !connected || !unavailableNow();
    $('shift-problem').disabled = sending || !connected || !data;
    $('unavailability-fields').disabled = sending || !connected || !data;
    $('unavailability-submit').disabled = sending || !connected || !!absence?.stale;
    $('close-availability').disabled = sending;
    $('absence-cancel-fields').disabled = sending || !connected;
    $('confirm-absence-cancel').disabled = sending || !connected || !!cancelAbsence?.stale;
    $('close-absence-cancel').disabled = sending;
    document.querySelectorAll('[data-absence-edit], [data-absence-cancel]').forEach(b => {b.disabled = sending || !connected;});
    $('action-fields').disabled = sending;
    $('send-action').disabled = sending || !connected || !!(action && action.stale);
    $('decline-fields').disabled = sending;
    $('send-decline').disabled = sending || !connected || !!(decline && decline.stale);
    $('close-dialog').disabled = sending;
    document.querySelectorAll('#task-actions button').forEach(b => { b.disabled = sending || !connected; });
  }
  async function request(path, payload) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await appFetch(appUrl(path), {cache:'no-store', signal:controller.signal, ...(payload ? {method:'POST', headers:{'Content-Type':'application/json', ...(path.startsWith('/api/engineer/') && currentClock ? {'X-Engineer-Clock':clockIdentity(currentClock)} : {})}, body:JSON.stringify(payload)} : {})});
      let result;
      try { result = await response.json(); } catch { throw new Error('Сервер вернул неполный ответ. Обновите задания.'); }
      if (!response.ok) { const failure = new Error(result.error || 'Не удалось выполнить запрос.'); failure.status = response.status; throw failure; }
      return result;
    } catch (failure) {
      if (!failure.status && (failure.name === 'AbortError' || failure instanceof TypeError)) {
        throw new Error(payload ? 'Нет подтверждения от сервера. Обновите страницу и проверьте результат перед повторной отправкой.' : 'Не удалось связаться с компьютером. Проверьте Wi-Fi и запущенный сервер.');
      }
      throw failure;
    } finally { clearTimeout(timeout); }
  }
  function taskCard(task) {
    const card = button('', 'task-card', () => openTask(task.id));
    const status = taskStatus(task);
    card.setAttribute('aria-label', taskLabel(task) + ', ' + task.address + ', ' + (labels[status] || status));
    const timing = el('div', time(task.visit && task.visit.start), 'task-time');
    timing.append(el('small', task.duration_minutes + ' мин'));
    const info = el('div', null, 'task-info'), top = el('div', null, 'task-top');
    top.append(el('span', taskLabel(task), 'task-id'), el('span', labels[status] || status, 'pill ' + status));
    if (task.priority !== 'normal') top.append(el('span', task.priority === 'urgent' ? 'Авария' : 'Приоритет', 'priority'));
    info.append(top, el('strong', task.address), el('div', taskType(task), 'task-meta'), el('div', 'Окно: ' + windows(task), 'task-meta'));
    card.append(timing, info, el('span', '›', 'chevron')); return card;
  }
  function renderTasks() {
    if (!data) return;
    const query = $('search').value.trim().toLocaleLowerCase('ru');
    const tasks = data.tasks.filter(t => (filter === 'all' || (t.status === 'completed') === (filter === 'completed')) && (t.id + ' ' + t.address + ' ' + taskType(t)).toLocaleLowerCase('ru').includes(query));
    $('task-list').replaceChildren(...tasks.map(taskCard));
    if (!tasks.length) $('task-list').append(el('p', query ? 'По вашему запросу ничего не найдено.' : filter === 'completed' ? 'Завершённые задания появятся здесь после подтверждения диспетчером.' : 'В этой группе пока нет заданий.', 'empty'));
  }
  function renderNext() {
    const active = data.tasks.find(t => !pendingReplan(t) && ['working','traveling'].includes(t.status));
    const task = active || data.tasks.find(t => !pendingReplan(t) && !['completed','completion_pending'].includes(t.status));
    const node = $('next-task'); node.replaceChildren(); node.hidden = !task || unavailableNow();
    if (!task || unavailableNow()) return;
    const top = el('div', null, 'next-top');
    top.append(el('span', active ? 'ТЕКУЩАЯ ЗАДАЧА' : 'СЛЕДУЮЩИЙ ВЫЕЗД', 'eyebrow'), el('span', labels[task.status], 'pill'));
    node.append(top, el('h2', task.address), el('p', taskLabel(task) + ' · ' + taskType(task)), el('p', 'Начало по плану ' + time(task.visit && task.visit.start) + ' · окно ' + windows(task)));
    node.append(button('Открыть заявку →', 'primary', () => openTask(task.id)));
  }
  function renderShift() {
    $('shift-return').hidden = !unavailableNow();
    $('shift-start').hidden = unavailableNow() || !!data.status || data.engineer.off_duty;
    $('shift-problem').hidden = unavailableNow();
    const e = data.engineer;
    const future = data.scheduled_absences || [];
    if ($('personal-name')) $('personal-name').textContent = e.name;
    const shiftStatus = {available:'На смене',traveling:'В пути',working:'Работает',completed:'На смене',completion_pending:'На смене',interrupted:'На смене'};
    $('engineer-status').textContent = unavailableNow() ? 'Недоступен' : data.location_confirmation_pending ? 'Ожидаем уточнения точки' : data.return_pending_replan ? 'Ожидает назначений' : e.off_duty ? 'Выходной' : data.status ? (shiftStatus[data.status.status] || 'На смене') : 'Не на смене';
    $('engineer-status').className = 'pill ' + (unavailableNow() ? 'unavailable' : data.return_pending_replan || data.location_confirmation_pending ? 'pending_replan' : ['working','traveling'].includes(data.status?.status) ? data.status.status : data.status && !e.off_duty ? 'available' : '');
    $('shift-plan-context').textContent = clockContext();
    const dl = $('shift-details'); dl.replaceChildren();
    const remainingTasks = data.tasks.filter(task => !pendingReplan(task) && !['completed','completion_pending'].includes(task.status)).length;
    factPair(dl, 'Смена', e.off_duty ? 'Выходной' : time(e.shift.start) + '–' + time(e.shift.end));
    factPair(dl, 'Осталось заявок', String(remainingTasks));
    factPair(dl, 'Выполнено', String(data.tasks.filter(task => task.status === 'completed').length));
    const activity = $('shift-activity'); activity.replaceChildren();
    const current = data.tasks.find(task => task.id === data.status?.request_id);
    if (current && !unavailableNow() && !pendingReplan(current) && ['working','traveling'].includes(data.status?.status)) {
      activity.append(el('span', (data.status.status === 'working' ? 'В работе' : 'В пути к заявке') + ' · № '+current.id),
        button('Открыть заявку', 'text-button', () => openTask(current.id)));
    }
    activity.hidden = !activity.childElementCount;
    const summary = $('availability-summary'); summary.replaceChildren();
    const periodTime = (day, clock) => (day === data.date ? '' : dateLabel(day) + ', ') + (typeof clock === 'number' ? time(clock) : clock);
    const reason = period => period.reason_label || absenceReasons[period.reason] || 'Отсутствие';
    const addPeriodActions = (row, period) => {
      const actions = el('div', null, 'absence-actions');
      const periodLabel = periodTime(period.start_date, period.start_time) + ' — ' + periodTime(period.end_date, period.end_time);
      if (period.can_edit) {
        const edit = button('Изменить', 'text-button', () => beginAbsence(null, period));
        edit.dataset.absenceEdit = period.id; edit.setAttribute('aria-label', 'Изменить отсутствие: ' + periodLabel); actions.append(edit);
      }
      if (period.can_cancel) {
        const cancel = button('Отменить', 'text-button', () => beginAbsenceCancel(period));
        cancel.dataset.absenceCancel = period.id; cancel.setAttribute('aria-label', 'Отменить отсутствие: ' + periodLabel); actions.append(cancel);
      }
      if (actions.childElementCount) row.append(actions);
    };
    const absenceRow = (period, current) => {
      const row = el('div', null, 'absence-period ' + (current ? 'absence-current' : 'absence-future'));
      const details = el('div', null, 'absence-description');
      if (current) details.append(el('span', liveClock() ? 'Сейчас' : 'В это время сценария', 'absence-label'));
      details.append(el('strong', current ? 'До ' + periodTime(period.end_date, period.end_time) : periodTime(period.start_date, period.start_time) + ' — ' + periodTime(period.end_date, period.end_time)), el('p', reason(period), 'absence-reason'));
      row.append(details); addPeriodActions(row, period); return row;
    };
    if (data.active_absence) summary.append(absenceRow(data.active_absence, true));
    else if (data.return_confirmation_required) summary.append(el('p', 'Период закончился. Подтвердите возвращение.', 'absence-note'));
    else if (data.indefinite_absence || data.status?.status === 'unavailable') summary.append(el('p', 'Без даты возвращения.', 'absence-note'));
    else if (data.location_confirmation_pending) summary.append(el('p', 'Диспетчер получил отметку. Для новых выездов нужно уточнить точку старта.', 'availability-note'));
    else if (data.return_pending_replan) summary.append(el('p', 'Диспетчер обновляет задания.', 'availability-note'));
    if (future.length) {
      summary.append(el('h3', 'Предстоящие отсутствия'));
      const periods = el('div', null, 'absence-list');
      for (const period of future) periods.append(absenceRow(period, false));
      summary.append(periods);
    }
    summary.hidden = !summary.childElementCount;
    const inventory = data.inventory, stock = e.stock || {};
    $('equipment').replaceChildren(...[['router','Роутеры'],['tv_box','ТВ-приставки']].map(([key, title]) => {
      const box = el('div');
      box.append(el('div', title), el('strong', inventory ? inventory.available[key] : stock[key] ?? '—'));
      return box;
    }));
    if (!$('unavailability-start-date').value) $('unavailability-start-date').value = data.date;
    if (!$('unavailability-end-date').value) $('unavailability-end-date').value = data.date;
    if (!$('unavailability-start-time').value) $('unavailability-start-time').value = time(Math.min(data.time, 1439));
    if (!$('unavailability-end-time').value) $('unavailability-end-time').value = time(Math.min(1439, Math.max(data.time + 1, e.shift.end)));
    $('unavailability-start-date').min = data.date;
    $('unavailability-end-date').min = data.date;
    updateUnavailabilityLocation();
  }
  function render(result) {
    data = result; currentClock = data.clock || null;
    window.dispatchEvent(new CustomEvent('engineer-state', {detail: {engineerId: data.engineer.id}}));
    $('plan-version').textContent = 'Версия ' + data.version;
    $('work-date').textContent = dateLabel(data.date).toUpperCase();
    $('region').textContent = data.region;
    $('replan-note').hidden = !data.return_pending_replan && !data.location_confirmation_pending && !(data.pending_reassignment_ids || []).length;
    $('pending-count').textContent = data.tasks.filter(t => t.status !== 'completed' && !pendingReplan(t)).length;
    $('done-count').textContent = data.tasks.filter(t => t.status === 'completed').length;
    $('distance').textContent = (data.distance_m / 1000).toLocaleString('ru-RU', {maximumFractionDigits:1});
    renderTasks(); renderNext(); renderShift();
    if (tab === 'map') renderMap();
    if (dialog.open && !action && !decline && selectedId) {
      if (currentTask()) renderDetail();
      else { dialog.close(); notice('Назначение изменилось. Проверьте обновлённый список заданий.'); }
    }
    controls();
  }
  async function load(quiet) {
    if (!engineerId || sending) return;
    const expected = engineerId, token = ++generation;
    loading = true;
    try {
      const result = await request('/api/engineer?engineer_id=' + encodeURIComponent(expected));
      if (token !== generation || engineerId !== expected) return;
      const oldVersion = data && data.version;
      render(result); syncStatus(true); error('');
      if (quiet && oldVersion && oldVersion !== result.version) notice('Задания обновлены.');
    } catch (failure) {
      if (token !== generation) return;
      syncStatus(false); error(failure.message);
    } finally { if (token === generation) loading = false; }
  }
  function renderDetail() {
    const task = currentTask(); if (!task) return;
    $('dialog-kicker').textContent = taskLabel(task);
    $('dialog-title').textContent = task.address;
    const content = $('task-detail'); content.replaceChildren();
    const status = taskStatus(task);
    content.append(el('span', labels[status] || status, 'pill ' + status));
    const dl = el('dl', null, 'detail-grid');
    factPair(dl, 'Работа', taskType(task), true);
    factPair(dl, 'Окно клиента', windows(task));
    factPair(dl, 'Длительность по нормативу', task.duration_minutes + ' мин');
    if (task.visit) {
      factPair(dl, 'Начало / конец по плану', time(task.visit.start) + '–' + time(task.visit.end));
      factPair(dl, 'Переезд по плану', task.visit.travel_minutes + ' мин · ' + (task.visit.distance_m / 1000).toFixed(1) + ' км');
    }
    factPair(dl, 'Оборудование по заявке', task.equipment.router + ' роутер(ов), ' + task.equipment.tv_box + ' ТВ-приставок', true);
    if (task.completed_at != null) factPair(dl, 'Фактическое завершение', time(task.completed_at));
    content.append(dl);
    if (task.point) {
      const link = el('a', 'Открыть маршрут в картах ↗', 'secondary navigation-link');
      const mode = {car:'driving', walk:'walking', bicycle:'bicycling', public:'transit'}[data.engineer.transport];
      link.href = 'https://www.google.com/maps/dir/?api=1&destination=' + encodeURIComponent(task.point.lat + ',' + task.point.lon) + '&travelmode=' + mode;
      link.target = '_blank'; link.rel = 'noopener noreferrer'; content.append(link);
      if (task.point.quality === 'approximate') content.append(el('p', 'Приблизительная точка: ' + task.point.label + '. ' + task.point.approximation_reason, 'notice'));
    } else content.append(el('p', 'Для этого набора нет географических координат. Используйте адрес заявки.', 'muted'));
    if (task.history.length) {
      const recent = task.history[task.history.length - 1];
      content.append(el('p', 'Последняя отметка: ' + time(recent.time) + ' · ' + labels[recent.status] + (recent.comment ? '. ' + recent.comment : ''), 'details-note'));
    }
    const controlsNode = $('task-actions'); controlsNode.replaceChildren();
    if (unavailableNow() && task.status === 'working') {
      controlsNode.append(el('p', 'Сообщите результат начатой работы.', 'hint'), button('Сообщить о выполнении', 'primary', () => beginAction('completed', task)), button('Приостановить работу', 'secondary', () => beginAction('interrupted', task)));
    } else if (unavailableNow()) {
      controlsNode.append(el('p', 'Вы недоступны.', 'hint'), button('К доступности', 'secondary', () => {dialog.close(); activate('shift');}));
    } else if (pendingReplan(task)) {
      controlsNode.append(el('p', 'Ожидаем новое назначение.', 'hint'));
    } else if (task.status === 'completion_pending') {
      controlsNode.append(el('p', 'Выполнение заявлено в ' + time(task.reported_completed_at) + '. Диспетчер проверит сообщение. Можно продолжать следующие задания.', 'hint'));
    } else if (task.status !== 'completed') {
      if (task.status === 'working') {
        controlsNode.append(button('Сообщить о выполнении', 'primary', () => beginAction('completed', task)), button('Обновить остаток', 'secondary', () => beginAction('working', task)), button('Сообщить о проблеме', 'secondary', () => beginAction('interrupted', task)));
      } else {
        controlsNode.append(button(task.status === 'traveling' ? 'На месте · начать работу' : 'Начать работу', 'primary', () => beginAction('working', task)));
        if (task.status !== 'traveling') controlsNode.append(button('В пути', 'secondary', () => beginAction('traveling', task)));
        else controlsNode.append(button('Обновить время пути', 'secondary', () => beginAction('traveling', task)));
        if (task.visit && task.status === 'planned') controlsNode.append(button('Проблема с заявкой', 'text-button', () => beginDecline(task)));
      }
    }
    controls();
  }
  function openTask(id) {
    selectedId = id; action = null; decline = null; $('action-form').hidden = true; $('decline-form').hidden = true;
    $('task-actions').hidden = false; $('task-detail').hidden = false;
    renderDetail(); if (!dialog.open) dialog.showModal(); dialog.scrollTop = 0;
  }
  function beginAction(status, task) {
    if (!data || !connected || sending) return;
    if (['available','unavailable'].includes(status) && data.status && data.status.status === 'working') {
      openTask(data.status.request_id);
      notice('Сначала завершите или приостановите текущую работу в карточке заявки.');
      return;
    }
    if (!task) {
      selectedId = null; $('dialog-kicker').textContent = 'МОЯ СМЕНА';
      $('dialog-title').textContent = data.engineer.name; $('task-detail').replaceChildren();
    }
    decline = null; $('decline-form').hidden = true;
    action = {version:data.version, status, taskId:task ? task.id : null, stale:false};
    $('action-form').reset(); $('action-form').hidden = false; $('task-actions').hidden = true;
    $('action-error').hidden = true; $('action-title').textContent = actions[status];
    $('action-day').textContent = clockContext();
    const last = data.status ? data.status.time + 1 : data.engineer.shift.start;
    const planned = task && task.visit ? (status === 'completed' ? task.visit.end : status === 'working' ? task.visit.start : task.visit.arrival - task.visit.travel_minutes) : data.time;
    $('action-time').value = time(liveClock() ? currentMinute() : Math.min(1439, Math.max(data.time, last, planned || data.time)));
    $('action-time').readOnly = liveClock();
    const locationNeeded = ['available','unavailable','traveling'].includes(status);
    $('location-field').hidden = !locationNeeded; $('action-location').required = status === 'traveling';
    document.querySelector('label[for="action-location"]').textContent = status === 'traveling' ? 'Точка начала выезда' : 'Местоположение · необязательно';
    $('action-location').replaceChildren(new Option('Не указано', ''), ...data.locations.map(point => {
      const option = el('option', point.label); option.value = point.id; return option;
    }));
    $('action-location').value = '';
    const remainingNeeded = ['working','interrupted','traveling'].includes(status);
    $('remaining-field').hidden = !remainingNeeded; $('action-remaining').required = remainingNeeded;
    $('remaining-label').textContent = status === 'traveling' ? 'Осталось в пути, мин' : 'Осталось работы, мин';
    $('action-remaining').value = task ? Math.max(1, status === 'traveling' ? ((task.status === 'traveling' && task.remaining_minutes) || (task.visit && task.visit.travel_minutes) || 1) : (task.work_remaining_minutes ?? task.duration_minutes)) : '';
    const consumed = ['completed','interrupted'].includes(status);
    $('consumed-fields').hidden = !consumed;
    $('action-router').required = consumed; $('action-tv').required = consumed;
    $('action-router').value = task && status === 'completed' ? task.equipment_remaining.router : 0;
    $('action-tv').value = task && status === 'completed' ? task.equipment_remaining.tv_box : 0;
    $('action-comment').required = ['interrupted','unavailable'].includes(status);
    $('action-comment').hidden = false;
    document.querySelector('label[for="action-comment"]').hidden = false;
    $('send-action').textContent = 'Отправить диспетчеру';
    controls(); if (!dialog.open) dialog.showModal();
    $('action-form').scrollIntoView({block:'nearest'});
  }
  function updateUnavailabilityLocation() {
    if (!data) return;
    const editingActive = !!absence?.period?.is_active;
    const unknown = $('unavailability-duration').value === 'unknown';
    if (unknown) $('unavailability-start-mode').value = 'now';
    $('unavailability-start-mode').disabled = unknown || editingActive;
    $('unavailability-duration').disabled = !!absence?.period;
    $('unavailability-start-mode').querySelector('[value="now"]').textContent = (liveClock() ? 'Сейчас · ' : 'С времени сценария · ') + time(currentMinute());
    $('absence-day').textContent = clockContext();
    const later = $('unavailability-start-mode').value === 'later';
    $('unavailability-start-fields').hidden = !later && !editingActive;
    for (const id of ['unavailability-start-date','unavailability-start-time']) $(id).disabled = !later || editingActive;
    $('unavailability-start-date').min = editingActive ? absence.period.start_date : data.date;
    $('unavailability-end-fields').hidden = unknown;
    for (const id of ['unavailability-end-date','unavailability-end-time']) $(id).disabled = unknown;
    $('unavailability-comment').required = $('unavailability-reason').value === 'other';
    document.querySelector('label[for="unavailability-comment"] .optional').hidden = $('unavailability-comment').required;
    $('unavailability-end-date').min = later ? $('unavailability-start-date').value || data.date : data.date;
  }
  function beginAbsence(draft, period = null) {
    if (!data || !connected || sending || (unavailableNow() && !period)) return;
    if (dialog.open) dialog.close();
    absence = {version:data.version, stale:false, period};
    $('unavailability-form').reset();
    $('unavailability-start-date').value = data.date;
    $('unavailability-start-time').value = time(currentMinute());
    const end = currentMinute() + 60, day = new Date(data.date+'T12:00:00Z');
    if (end >= 1440) day.setUTCDate(day.getUTCDate()+1);
    $('unavailability-end-date').value = day.toISOString().slice(0,10);
    $('unavailability-end-time').value = time(end%1440);
    if (period) {
      $('unavailability-start-mode').value = 'later';
      for (const [field,key] of [['start-date','start_date'],['start-time','start_time'],['end-date','end_date'],['end-time','end_time'],['reason','reason'],['comment','comment']]) $('unavailability-'+field).value = period[key] || '';
    }
    if (draft && !(draft instanceof Event)) {
      for (const [field, value] of Object.entries(draft)) $('unavailability-'+field).value = value;
    }
    $('unavailability-title').textContent = period ? 'Изменить отсутствие' : 'Период отсутствия';
    $('unavailability-submit').textContent = period ? 'Сохранить изменения' : 'Сообщить диспетчеру';
    $('absence-day').textContent = clockContext();
    $('unavailability-error').hidden = true;
    updateUnavailabilityLocation(); controls();
    $('availability-dialog').showModal();
  }
  function beginAbsenceCancel(period) {
    if (!data || !connected || sending || !period.can_cancel) return;
    cancelAbsence = {version:data.version, period, stale:false};
    $('absence-cancel-form').reset();
    $('absence-cancel-period').textContent = period.start_date + ' ' + period.start_time + ' — ' + period.end_date + ' ' + period.end_time;
    $('absence-cancel-error').hidden = true;
    controls(); $('absence-cancel-dialog').showModal();
  }
  function beginDecline(task) {
    if (!data || !connected || sending || !task.visit || task.status !== 'planned' || pendingReplan(task)) return;
    decline = {version:data.version, taskId:task.id, stale:false};
    action = null; $('action-form').hidden = true;
    $('decline-form').reset(); $('decline-form').hidden = false; $('decline-error').hidden = true;
    $('task-actions').hidden = true;
    $('decline-time').value = time(Math.min(1439, data.time));
    controls();
    $('decline-form').scrollIntoView({block:'nearest'});
    $('decline-comment').focus();
  }
  function renderMap() {
    if (!data) return;
    const tasks = data.tasks.filter(t => t.point);
    $('map-tasks').replaceChildren(...data.tasks.map((t, index) => button((index + 1) + '. ' + time(t.visit && t.visit.start) + ' · ' + t.address, '', () => openTask(t.id))));
    if (!window.L) { $('map-note').textContent = 'Не удалось загрузить карту. Адреса доступны в списке ниже.'; return; }
    if (!map) {
      map = L.map('engineer-map', {scrollWheelZoom:false});
      map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {referrerPolicy:'strict-origin-when-cross-origin', attribution:'© OpenStreetMap', maxZoom:19}).addTo(map).on('tileerror', () => {
        $('map-note').textContent = 'Подложка карты недоступна. Точки заявок сохранены; список адресов работает без интернета.';
      });
    }
    if (mapLayer) mapLayer.remove();
    mapLayer = L.layerGroup().addTo(map); mapBounds = tasks.map(t => [t.point.lat, t.point.lon]);
    tasks.forEach(task => {
      const number = data.tasks.indexOf(task) + 1;
      const marker = L.marker([task.point.lat, task.point.lon], {icon:L.divIcon({className:'map-number', html:String(number), iconSize:[28,28], iconAnchor:[14,14]}), title:task.address}).addTo(mapLayer);
      const popup = el('div'); popup.append(el('strong', task.address), el('p', time(task.visit && task.visit.start) + ' · ' + labels[taskStatus(task)]), button('Открыть заявку', 'primary', () => openTask(task.id)));
      marker.bindPopup(popup);
    });
    const shape = data.route_shape;
    if (shape && shape.coordinates) L.polyline(shape.coordinates.map(p => [p[1], p[0]]), {color:'#7b6500', weight:4, dashArray:shape.kind === 'road' ? null : '6 7'}).addTo(mapLayer);
    else if (mapBounds.length > 1) L.polyline(mapBounds, {color:'#9b8500', weight:3, dashArray:'6 7'}).addTo(mapLayer);
    $('map-note').textContent = !tasks.length ? 'В этом наборе нет координат заявок.' : shape && shape.kind === 'road' ? 'Сохранённая дорожная геометрия утверждённого плана.' : 'Пунктир показывает порядок посещения адресов, а не дорогу. Для навигации откройте маршрут из карточки.';
    requestAnimationFrame(() => { map.invalidateSize(); fitMap(); });
  }
  function fitMap() {
    if (!map) return;
    if (mapBounds.length) map.fitBounds(mapBounds, {padding:[30,30], maxZoom:15});
    else map.setView([55.75,37.62],10);
  }
  function activate(name) {
    tab = name;
    ['tasks','map','shift'].forEach(key => {
      $('panel-' + key).hidden = key !== name;
      if (key === name) $('tab-' + key).setAttribute('aria-current','page');
      else $('tab-' + key).removeAttribute('aria-current');
    });
    if (name === 'map') renderMap();
    window.scrollTo({top:0});
  }
  for (const id of ['unavailability-start-date','unavailability-start-time','unavailability-start-mode','unavailability-duration','unavailability-reason']) $(id).addEventListener('input', updateUnavailabilityLocation);
  $('unavailability-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!data || !absence || absence.stale || sending || !connected) return;
    let payload;
    const unknown = $('unavailability-duration').value === 'unknown';
    try {
      const reason = $('unavailability-reason').value, comment = $('unavailability-comment').value.trim();
      if (!absenceReasons[reason]) throw new Error('Выберите причину.');
      if (reason === 'other' && comment.length < 3) throw new Error('Кратко опишите причину.');
      if (comment && comment.length < 3) throw new Error('Комментарий: не менее трёх символов.');
      const later = $('unavailability-start-mode').value === 'later';
      payload = unknown ? actionPayload(absence.version, engineerId, 'unavailable', null, {
        time:time(liveClock() ? currentMinute() : Math.max(data.time, data.status ? data.status.time+1 : data.time)),
        comment:comment || absenceReasons[reason]
      }) : unavailabilityPayload(absence.version, engineerId, {
        startDate:later ? $('unavailability-start-date').value : data.date,
        startTime:later ? $('unavailability-start-time').value : time(currentMinute()),
        endDate:$('unavailability-end-date').value, endTime:$('unavailability-end-time').value,
        reason, comment:comment || absenceReasons[reason]
      }, data.date, currentMinute(), data.engineer.shift.start, data.engineer.shift.end, absence.period);
      if (!unknown && !later) payload.start_mode = 'now';
      if (absence.period) payload.report_id = absence.period.id;
    } catch (failure) { $('unavailability-error').textContent = failure.message; $('unavailability-error').hidden = false; return; }
    const startsNow = unknown || (payload.start_date === data.date && minute(payload.start_time) <= currentMinute());
    if (startsNow && data.status?.status === 'working') {
      const task = data.tasks.find(t => t.id === data.status.request_id);
      if (!task) {error('Обновите задания: текущая работа не найдена.'); return;}
      const draft = Object.fromEntries(['start-mode','start-date','start-time','duration','end-date','end-time','reason','comment'].map(field => [field,$('unavailability-'+field).value]));
      const period = absence.period;
      $('availability-dialog').close();
      openTask(task.id); beginAction('interrupted',task); action.thenAbsence = {draft, period};
      $('action-comment').value = payload.comment;
      $('action-title').textContent = 'Приостановить текущую работу';
      $('send-action').textContent = 'Сохранить и продолжить';
      return;
    }
    sending = true; controls(); $('unavailability-error').hidden = true;
    try {
      const result = await request(absence.period ? '/api/engineer/unavailability/update' : unknown ? '/api/engineer/facts' : '/api/engineer/unavailability', payload);
      $('availability-dialog').close();
      $('unavailability-form').reset(); render(result); syncStatus(true); error('');
      notice('Диспетчер уведомлён.');
    } catch (failure) {
      $('unavailability-error').textContent = failure.message; $('unavailability-error').hidden = false;
      if (failure.status === 409) {absence.stale = true; $('unavailability-error').textContent += ' Закройте форму и обновите задания.';}
    } finally { sending = false; controls(); }
  });
  $('absence-cancel-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!cancelAbsence || sending || !connected || cancelAbsence.stale) return;
    const pending = cancelAbsence;
    const payload = {version:pending.version,engineer_id:engineerId,report_id:pending.period.id,correction_reason:$('absence-cancel-reason').value};
    sending = true; controls(); $('absence-cancel-error').hidden = true;
    try {
      const result = await request('/api/engineer/unavailability/cancel',payload);
      $('absence-cancel-dialog').close(); render(result); syncStatus(true); error('');
      notice('Отсутствие отменено. Диспетчер уведомлён.');
    } catch (failure) {
      $('absence-cancel-error').textContent = failure.message; $('absence-cancel-error').hidden = false;
      if (failure.status === 409) {pending.stale = true; $('absence-cancel-error').textContent += '. Закройте форму и обновите страницу.';}
    } finally {sending = false; controls();}
  });
  $('decline-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!decline || sending || decline.stale || !connected) return;
    let payload;
    try { payload = declinePayload(decline.version, engineerId, decline.taskId, {time:$('decline-time').value, comment:$('decline-comment').value}, data.time); }
    catch (failure) { $('decline-error').textContent = failure.message; $('decline-error').hidden = false; return; }
    sending = true; controls(); $('decline-error').hidden = true;
    try {
      const result = await request('/api/engineer/decline', payload);
      decline = null; dialog.close(); render(result); syncStatus(true); error('');
      notice('Проблема по заявке отправлена диспетчеру.');
    } catch (failure) {
      $('decline-error').textContent = failure.message; $('decline-error').hidden = false;
      if (failure.status === 409) {
        decline.stale = true;
        $('decline-error').textContent += ' Обновите задания перед повторной отправкой.';
      }
    } finally { sending = false; controls(); }
  });
  $('cancel-decline').addEventListener('click', () => {
    decline = null; $('decline-form').hidden = true; $('task-actions').hidden = false;
    if (currentTask()) renderDetail(); else dialog.close();
  });
  $('action-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!action || sending || action.stale || !connected) return;
    let payload;
    try {
      payload = actionPayload(action.version, engineerId, action.status, action.taskId, {
        time:liveClock() ? time(currentMinute()) : $('action-time').value, location:action.returning ? null : $('action-location').value, remaining:$('action-remaining').value,
        router:$('action-router').value, tv:$('action-tv').value, comment:$('action-comment').value
      });
    } catch (failure) { $('action-error').textContent = failure.message; $('action-error').hidden = false; return; }
    const returning = !!action.returning, thenAbsence = action.thenAbsence;
    if (returning) payload = {version:payload.version, engineer_id:payload.engineer_id,
      ...(action.returnReportId ? {report_id:action.returnReportId} : {}),
      ...(action.unavailableFactId ? {unavailable_fact_id:action.unavailableFactId} : {}),
      time:payload.time, location_id:payload.location_id};
    sending = true; controls(); $('action-error').hidden = true;
    try {
      const result = await request(returning ? '/api/engineer/return' : '/api/engineer/facts', payload);
      action = null; dialog.close(); render(result); syncStatus(true); error('');
      window.dispatchEvent(new CustomEvent('engineer-action',{detail:{status:returning ? 'available' : payload.status}}));
      notice(returning ? 'Вы снова доступны. Ожидаем задания.' : payload.status === 'completed' ? 'Результат отправлен диспетчеру.' : 'Отметка сохранена.');
      if (thenAbsence) {sending = false; beginAbsence(thenAbsence.draft, thenAbsence.period);}
    } catch (failure) {
      $('action-error').textContent = failure.message; $('action-error').hidden = false;
      if (failure.status === 409) {
        action.stale = true;
        $('action-error').textContent += ' Закройте карточку и обновите задания. Введённые данные не отправлены повторно.';
      } else if (!failure.status) syncStatus(false);
    } finally { sending = false; controls(); }
  });
  $('cancel-action').addEventListener('click', () => {
    action = null; $('action-form').hidden = true; $('task-actions').hidden = false;
    if (currentTask()) renderDetail(); else dialog.close();
  });
  $('close-dialog').addEventListener('click', () => dialog.close());
  dialog.addEventListener('cancel', event => { if (sending) event.preventDefault(); });
  dialog.addEventListener('close', () => { action = null; decline = null; selectedId = null; });
  $('shift-start').addEventListener('click', () => beginAction('available'));
  $('shift-return').addEventListener('click', () => {
    if (!unavailableNow() || !connected || sending) return;
    beginAction('available');
    if (!action || action.status !== 'available') return;
    action.returning = true;
    action.returnReportId = data.return_report_id || data.active_absence?.id;
    if (!action.returnReportId) action.unavailableFactId = data.indefinite_absence?.id || data.status?.id;
    $('action-title').textContent = 'Я снова доступен';
    $('location-field').hidden = true; $('action-location').required = false;
    $('action-time').value = time(currentMinute());
    $('action-comment').hidden = true;
    document.querySelector('label[for="action-comment"]').hidden = true;
    $('send-action').textContent = 'Подтвердить возвращение';
  });
  $('shift-problem').addEventListener('click', beginAbsence);
  $('close-availability').addEventListener('click', () => $('availability-dialog').close());
  $('availability-dialog').addEventListener('cancel', event => {if (sending) event.preventDefault();});
  $('availability-dialog').addEventListener('close', () => {absence = null;});
  $('close-absence-cancel').addEventListener('click', () => $('absence-cancel-dialog').close());
  $('absence-cancel-dialog').addEventListener('cancel', event => {if (sending) event.preventDefault();});
  $('absence-cancel-dialog').addEventListener('close', () => {cancelAbsence = null;});
  $('search').addEventListener('input', renderTasks);
  document.querySelectorAll('[data-filter]').forEach(b => b.addEventListener('click', () => {
    filter = b.dataset.filter; document.querySelectorAll('[data-filter]').forEach(x => x.setAttribute('aria-pressed', String(x === b))); renderTasks();
  }));
  ['tasks','map','shift'].forEach(name => $('tab-' + name).addEventListener('click', () => activate(name)));
  $('fit-map').addEventListener('click', fitMap);
  $('engineer-select').addEventListener('change', () => {
    engineerId = $('engineer-select').value; data = null; syncStatus(false); dialog.close();
    $('availability-dialog').close(); $('absence-cancel-dialog').close();
    $('unavailability-form').reset(); $('unavailability-error').hidden = true;
    try { localStorage.setItem('field-router-engineer', engineerId); } catch {}
    $('task-list').replaceChildren(el('p','Загрузка заданий…','empty')); $('next-task').hidden = true; notice('');
    load(false);
  });
  async function refreshRoster() {
    const expected = engineerId;
    const index = await request('/api/engineers');
    if (engineerId !== expected) return true;
    const retained = index.engineers.some(e => e.id === expected);
    rosterVersion = index.version;
    $('engineer-select').replaceChildren(...index.engineers.map(e => {
      const option = el('option', e.name + ' · ' + e.region); option.value = e.id; return option;
    }));
    $('engineer-select').value = retained ? expected : index.engineers[0].id;
    if (!retained) {
      // The normal profile-change event also stops the independent GPS tracker.
      $('engineer-select').dispatchEvent(new Event('change', {bubbles: true}));
      notice('Состав бригад изменён. Проверьте выбранного инженера; для нового профиля GPS включается отдельно.');
    }
    return !retained;
  }
  $('refresh').addEventListener('click', async () => {
    try {
      if (!engineerId) return await init();
      if (!await refreshRoster()) await load(false);
    } catch (failure) { syncStatus(false); error(failure.message); }
  });
  window.addEventListener('online', () => { if (!action && !decline && !absence && !cancelAbsence) load(true); });
  window.addEventListener('offline', () => syncStatus(false));
  async function poll() {
    if (document.hidden || sending || loading || polling || !engineerId) return;
    polling = true;
    try {
      const revision = await request('/api/revision');
      if (!data || revision.version !== data.version || revision.version !== rosterVersion || revision.clock?.key !== data.clock?.key || clockIdentity(revision.clock) !== clockIdentity(data.clock) || !connected) {
        if (action || decline || absence || cancelAbsence) {
          const clockChanged = currentClock && revision.clock && clockIdentity(currentClock) !== clockIdentity(revision.clock);
          if (revision.clock) {
            currentClock = revision.clock;
            if (absence) updateUnavailabilityLocation();
            if (action && liveClock()) { $('action-time').value = time(currentMinute()); $('action-day').textContent = clockContext(); }
          }
          const form = action || decline || absence || cancelAbsence;
          const period = absence?.period || cancelAbsence?.period;
          const now = currentClock ? currentClock.date + 'T' + time(currentMinute()) : null;
          const periodStarted = period && now && period.start_date + 'T' + period.start_time <= now;
          const periodExpired = period && now && period.end_date + 'T' + period.end_time <= now;
          const periodChanged = !!period && !!now && (periodExpired || periodStarted !== !!period.is_active);
          const errorId = action ? 'action-error' : decline ? 'decline-error' : absence ? 'unavailability-error' : 'absence-cancel-error';
          if (revision.version !== form.version || clockChanged || periodChanged) {
            form.stale = true; controls();
            $(errorId).textContent = clockChanged ? 'Дата рабочего дня изменилась. Закройте форму и обновите задания.' : periodChanged ? 'Период уже начался или закончился. Закройте форму и обновите задания.' : 'Данные изменились. Закройте форму и обновите задания.';
            $(errorId).hidden = false;
          }
        } else {
          if (revision.version !== rosterVersion && await refreshRoster()) return;
          await load(true);
        }
      } else syncStatus(true);
    } catch (failure) { syncStatus(false); error(failure.message); }
    finally { polling = false; }
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  async function init() {
    try {
      const index = await request('/api/engineers');
      rosterVersion = index.version;
      $('engineer-select').replaceChildren(...index.engineers.map(e => {
        const option = el('option', e.name + ' · ' + e.region); option.value = e.id; return option;
      }));
      let saved = new URLSearchParams(location.search).get('engineer_id');
      if (!saved) { try { saved = localStorage.getItem('field-router-engineer'); } catch {} }
      engineerId = index.engineers.some(e => e.id === saved) ? saved : index.engineers[0].id;
      $('engineer-select').value = engineerId; controls(); await load(false);
    } catch (failure) { syncStatus(false); error(failure.message); }
  }
  init(); setInterval(poll, 10000);
})();
