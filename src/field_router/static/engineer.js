(function () {
  'use strict';
  const labels = {planned:'Назначена', traveling:'В пути', working:'В работе', completion_pending:'Ожидает подтверждения', completed:'Выполнена', interrupted:'Приостановлена', available:'Свободен', unavailable:'Недоступен', pending_replan:'Ожидает перепланирования'};
  const actions = {traveling:'Выехать на заявку', working:'Начать работу', completed:'Сообщить о выполнении', interrupted:'Приостановить работу', available:'На смене / свободен', unavailable:'Сообщить о недоступности'};
  const absenceReasons = {sick:'Болезнь', transport:'Проблема с транспортом', personal:'Личные обстоятельства', other:'Другое'};
  const transport = {car:'Автомобиль', walk:'Пешком', bicycle:'Велосипед', public:'Общественный транспорт'};
  const skills = {local:'Локальный ремонт', connection:'Подключение', emergency:'Аварийные работы'};
  function time(value) {
    if (!Number.isInteger(value)) return '—';
    return String(Math.floor(value / 60)).padStart(2, '0') + ':' + String(value % 60).padStart(2, '0');
  }
  function minute(value) {
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) throw new Error('Укажите время в пределах рабочего дня.');
    return Number(value.slice(0, 2)) * 60 + Number(value.slice(3));
  }
  function actionPayload(version, engineerId, status, taskId, input) {
    const payload = {version, engineer_id:engineerId, status, time:minute(input.time), comment:input.comment.trim()};
    if (taskId && ['traveling','working','completed','interrupted'].includes(status)) payload.request_id = taskId;
    if (['available','unavailable','traveling'].includes(status)) {
      if (!input.location) throw new Error('Укажите ваше текущее местоположение.');
      payload.location_id = input.location;
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
  function unavailabilityPayload(version, engineerId, input, currentDate, currentMinute, shiftStart = 0, shiftEnd = 1440) {
    const startDate = input.startDate, endDate = input.endDate;
    if (!validDate(startDate) || !validDate(endDate)) throw new Error('Укажите корректные даты периода.');
    const startMinute = minute(input.startTime), endMinute = minute(input.endTime);
    if (endDate < startDate || (endDate === startDate && endMinute <= startMinute)) throw new Error('Конец периода должен быть позже начала.');
    if (currentDate && (startDate < currentDate || (startDate === currentDate && startMinute < currentMinute))) throw new Error('Начало периода не может быть раньше текущего времени плана.');
    if (!Object.hasOwn(absenceReasons, input.reason)) throw new Error('Выберите причину недоступности.');
    const comment = (input.comment || '').trim();
    if (comment.length < 3 || comment.length > 1000) throw new Error('Кратко поясните ситуацию для диспетчера.');
    const payload = {version, engineer_id:engineerId, start_date:startDate, start_time:input.startTime, end_date:endDate, end_time:input.endTime, reason:input.reason, comment};
    const started = currentDate && currentMinute >= shiftStart && currentMinute < shiftEnd && (startDate < currentDate || (startDate === currentDate && startMinute <= currentMinute));
    if (started && !input.location) throw new Error('Для начавшегося периода укажите ваше текущее местоположение.');
    if (started && input.location) payload.location_id = input.location;
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
  if (typeof module !== 'undefined' && module.exports) module.exports = {time, minute, actionPayload, unavailabilityPayload, declinePayload};
  if (typeof document === 'undefined') return;
  const $ = id => document.getElementById(id);
  const appFetch = (...args) => window.FieldRouter ? window.FieldRouter.fetch(...args) : fetch(...args);
  const appUrl = path => window.FieldRouter ? window.FieldRouter.url(path) : path;
  let rosterVersion = null;
  let data = null, engineerId = '', filter = 'pending', tab = 'tasks', selectedId = null;
  let action = null, decline = null, sending = false, connected = false, loading = false, generation = 0;
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
  function notice(text) { $('updates').textContent = text; $('updates').hidden = !text; }
  function error(text) { $('error').textContent = text; $('error').hidden = !text; }
  function currentTask() { return data && data.tasks.find(t => t.id === selectedId); }
  function pendingReplan(task) { return task.status === 'pending_replan' || (data.pending_reassignment_ids || []).includes(task.id); }
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
    $('shift-problem').disabled = sending || !connected || !data;
    $('unavailability-fields').disabled = sending || !connected || !data;
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
      const response = await appFetch(appUrl(path), {cache:'no-store', signal:controller.signal, ...(payload ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)} : {})});
      let result;
      try { result = await response.json(); } catch { throw new Error('Сервер вернул неполный ответ. Обновите задания.'); }
      if (!response.ok) { const failure = new Error(result.error || 'Не удалось выполнить запрос.'); failure.status = response.status; throw failure; }
      return result;
    } catch (failure) {
      if (!failure.status && (failure.name === 'AbortError' || failure instanceof TypeError)) {
        throw new Error(payload ? 'Нет подтверждения от сервера. Обновите задания и проверьте журнал перед повторной отправкой.' : 'Не удалось связаться с компьютером. Проверьте Wi-Fi и запущенный сервер.');
      }
      throw failure;
    } finally { clearTimeout(timeout); }
  }
  function taskCard(task) {
    const card = button('', 'task-card', () => openTask(task.id));
    const status = taskStatus(task);
    card.setAttribute('aria-label', 'Заявка ' + task.id + ', ' + task.address + ', ' + (labels[status] || status));
    const timing = el('div', time(task.visit && task.visit.start), 'task-time');
    timing.append(el('small', task.duration_minutes + ' мин'));
    const info = el('div', null, 'task-info'), top = el('div', null, 'task-top');
    top.append(el('span', '№ ' + task.id, 'task-id'), el('span', labels[status] || status, 'pill ' + status));
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
    const node = $('next-task'); node.replaceChildren(); node.hidden = !task;
    if (!task) return;
    const top = el('div', null, 'next-top');
    top.append(el('span', active ? 'ТЕКУЩАЯ ЗАДАЧА' : 'СЛЕДУЮЩИЙ ВЫЕЗД', 'eyebrow'), el('span', labels[task.status], 'pill'));
    node.append(top, el('h2', task.address), el('p', '№ ' + task.id + ' · ' + taskType(task)), el('p', 'Начало по плану ' + time(task.visit && task.visit.start) + ' · окно ' + windows(task)));
    node.append(button('Открыть заявку →', 'primary', () => openTask(task.id)));
  }
  function renderShift() {
    const e = data.engineer;
    $('engineer-name').textContent = e.name;
    if ($('personal-name')) $('personal-name').textContent = e.name;
    $('engineer-status').textContent = data.active_absence ? 'Недоступен: ' + (data.active_absence.reason_label || 'период отсутствия') : data.return_confirmation_required ? 'Подтвердите возвращение' : data.status ? labels[data.status.status] : e.off_duty ? 'Выходной по плану' : 'Выход не подтверждён';
    $('engineer-status').className = 'pill ' + (data.active_absence || data.return_confirmation_required ? 'unavailable' : data.status ? data.status.status : '');
    const dl = $('shift-details'); dl.replaceChildren();
    if (e.off_duty) factPair(dl, 'График', 'Выходной по плану');
    if (data.return_confirmation_required) factPair(dl, 'Возврат на смену', 'Укажите статус «На смене / свободен» и вашу фактическую точку, чтобы диспетчер мог вернуть вас в план.', true);
    factPair(dl, 'Смена', time(e.shift.start) + '–' + time(e.shift.end));
    const start = data.locations.find(p => p.id === e.start_location);
    factPair(dl, 'Старт смены', start ? start.label : (e.start_address || 'Точка старта не указана'), true);
    factPair(dl, 'Транспорт', transport[e.transport]);
    factPair(dl, 'Компетенции', e.skills.map(s => skills[s]).join(', '), true);
    const location = data.status && data.locations.find(p => p.id === data.status.location_id);
    factPair(dl, 'Последнее сообщённое место', location ? location.label : 'Ещё не сообщено', true);
    const inventory = data.inventory, stock = e.stock || {};
    $('equipment').replaceChildren(...[['router','Роутеры'],['tv_box','ТВ-приставки']].map(([key, title]) => {
      const box = el('div');
      box.append(el('div', title), el('strong', inventory ? inventory.available[key] + ' доступно' : '—'), el('p', 'Выдано: ' + (stock[key] ?? '—') + ' · использовано: ' + (inventory ? inventory.consumed[key] : '—') + ' · в резерве: ' + (inventory ? inventory.reserved[key] : '—')));
      return box;
    }));
    $('fact-list').replaceChildren(...data.facts.slice().reverse().map(f => {
      const row = el('div', null, 'fact');
      row.append(el('strong', time(f.time) + ' · ' + labels[f.status]), el('small', (f.request_id ? 'Заявка № ' + f.request_id + ' · ' : '') + (f.source === 'engineer' ? 'Отправлено инженером' : 'Записано диспетчером')));
      if (f.comment) row.append(el('p', f.comment));
      return row;
    }));
    if (!data.facts.length) $('fact-list').append(el('p', 'Отметок пока нет.', 'muted'));
    const periods = Array.isArray(data.unavailability) ? data.unavailability : [];
    $('unavailability-list').replaceChildren(...periods.map(period => {
      const row = el('div', null, 'unavailability-entry');
      const start = period.start_date + ' ' + (typeof period.start_time === 'number' ? time(period.start_time) : period.start_time);
      const end = period.end_date + ' ' + (typeof period.end_time === 'number' ? time(period.end_time) : period.end_time);
      row.append(el('strong', start + ' — ' + end), el('small', period.reason_label || absenceReasons[period.reason] || period.reason || 'Причина не указана'));
      if (period.comment) row.append(el('p', period.comment));
      return row;
    }));
    if (!periods.length) $('unavailability-list').append(el('p', 'Периодов недоступности пока нет.', 'muted'));
    if (!$('unavailability-start-date').value) $('unavailability-start-date').value = data.date;
    if (!$('unavailability-end-date').value) $('unavailability-end-date').value = data.date;
    if (!$('unavailability-start-time').value) $('unavailability-start-time').value = time(Math.min(data.time, 1439));
    if (!$('unavailability-end-time').value) $('unavailability-end-time').value = time(Math.min(1439, Math.max(data.time + 1, e.shift.end)));
    $('unavailability-start-date').min = data.date;
    $('unavailability-end-date').min = data.date;
    const previousLocation = $('unavailability-location').value;
    $('unavailability-location').replaceChildren(...data.locations.map(point => {
      const option = el('option', point.label); option.value = point.id; return option;
    }));
    $('unavailability-location').value = previousLocation || data.status?.location_id || e.start_location;
    updateUnavailabilityLocation();
  }
  function render(result) {
    data = result;
    window.dispatchEvent(new CustomEvent('engineer-state', {detail: {engineerId: data.engineer.id}}));
    $('plan-version').textContent = 'Версия ' + data.version;
    $('work-date').textContent = dateLabel(data.date).toUpperCase();
    $('region').textContent = data.region;
    $('replan-note').hidden = !data.needs_replanning && !(data.pending_reassignment_ids || []).length;
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
      if (quiet && oldVersion && oldVersion !== result.version) notice('Диспетчер обновил данные. Показана версия ' + result.version + '. Проверьте назначения и время.');
    } catch (failure) {
      if (token !== generation) return;
      syncStatus(false); error(failure.message);
    } finally { if (token === generation) loading = false; }
  }
  function renderDetail() {
    const task = currentTask(); if (!task) return;
    $('dialog-kicker').textContent = 'ЗАЯВКА № ' + task.id;
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
    if (task.visit && task.visit.explanation) {
      const details = el('details'); details.append(el('summary', 'Данные назначения'), el('p', task.visit.explanation, 'details-note')); content.append(details);
    }
    if (task.history.length) {
      const recent = task.history[task.history.length - 1];
      content.append(el('p', 'Последняя отметка: ' + time(recent.time) + ' · ' + labels[recent.status] + (recent.comment ? '. ' + recent.comment : ''), 'details-note'));
    }
    const controlsNode = $('task-actions'); controlsNode.replaceChildren();
    if (pendingReplan(task)) {
      controlsNode.append(el('p', 'Вы сообщили, что не можете выехать. Заявка ожидает перепланирования диспетчером.', 'hint'));
    } else if (task.status === 'completion_pending') {
      controlsNode.append(el('p', 'Выполнение заявлено в ' + time(task.reported_completed_at) + '. Диспетчер проверит сообщение. Можно продолжать следующие задания.', 'hint'));
    } else if (task.status !== 'completed') {
      if (task.status === 'working') {
        controlsNode.append(button('Сообщить о выполнении', 'primary', () => beginAction('completed', task)), button('Обновить остаток', 'secondary', () => beginAction('working', task)), button('Сообщить о проблеме', 'secondary', () => beginAction('interrupted', task)));
      } else {
        controlsNode.append(button(task.status === 'traveling' ? 'На месте · начать работу' : 'Начать работу', 'primary', () => beginAction('working', task)));
        if (task.status !== 'traveling') controlsNode.append(button('В пути', 'secondary', () => beginAction('traveling', task)));
        else controlsNode.append(button('Обновить время пути', 'secondary', () => beginAction('traveling', task)));
        if (task.visit && task.status === 'planned') controlsNode.append(button('Не могу выехать', 'secondary', () => beginDecline(task)));
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
    $('action-day').textContent = 'Рабочий день: ' + dateLabel(data.date);
    const last = data.status ? data.status.time + 1 : data.engineer.shift.start;
    const planned = task && task.visit ? (status === 'completed' ? task.visit.end : status === 'working' ? task.visit.start : task.visit.arrival - task.visit.travel_minutes) : data.time;
    $('action-time').value = time(Math.min(1439, Math.max(last, planned || data.time)));
    const locationNeeded = ['available','unavailable','traveling'].includes(status);
    $('location-field').hidden = !locationNeeded; $('action-location').required = locationNeeded;
    $('action-location').replaceChildren(...data.locations.map(point => {
      const option = el('option', point.label); option.value = point.id; return option;
    }));
    $('action-location').value = (data.status && data.status.location_id) || data.engineer.start_location;
    if (!$('action-location').value && data.locations.length) $('action-location').selectedIndex = 0;
    const remainingNeeded = ['working','interrupted','traveling'].includes(status);
    $('remaining-field').hidden = !remainingNeeded; $('action-remaining').required = remainingNeeded;
    $('remaining-label').textContent = status === 'traveling' ? 'Осталось в пути, мин' : 'Осталось работы, мин';
    $('action-remaining').value = task ? Math.max(1, status === 'traveling' ? ((task.status === 'traveling' && task.remaining_minutes) || (task.visit && task.visit.travel_minutes) || 1) : ((task.status === 'working' && task.remaining_minutes) || task.duration_minutes)) : '';
    const consumed = ['completed','interrupted'].includes(status);
    $('consumed-fields').hidden = !consumed;
    $('action-router').required = consumed; $('action-tv').required = consumed;
    $('action-router').value = task && status === 'completed' ? task.equipment_remaining.router : 0;
    $('action-tv').value = task && status === 'completed' ? task.equipment_remaining.tv_box : 0;
    $('action-comment').required = ['interrupted','unavailable'].includes(status);
    controls(); if (!dialog.open) dialog.showModal();
    $('action-form').scrollIntoView({block:'nearest'});
  }
  function updateUnavailabilityLocation() {
    if (!data) return;
    $('unavailability-end-date').min = $('unavailability-start-date').value || data.date;
    let started = false;
    try {
      const startDate = $('unavailability-start-date').value;
      started = data.time >= data.engineer.shift.start && data.time < data.engineer.shift.end && (startDate < data.date || (startDate === data.date && minute($('unavailability-start-time').value) <= data.time));
    } catch { /* Native required validation handles an unfinished time. */ }
    $('unavailability-location-group').hidden = !started;
    $('unavailability-location').required = started;
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
  for (const id of ['unavailability-start-date','unavailability-start-time']) $(id).addEventListener('input', updateUnavailabilityLocation);
  $('unavailability-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!data || sending || !connected) return;
    let payload;
    try {
      payload = unavailabilityPayload(data.version, engineerId, {
        startDate:$('unavailability-start-date').value, startTime:$('unavailability-start-time').value,
        endDate:$('unavailability-end-date').value, endTime:$('unavailability-end-time').value,
        reason:$('unavailability-reason').value, comment:$('unavailability-comment').value,
        location:$('unavailability-location').value
      }, data.date, data.time, data.engineer.shift.start, data.engineer.shift.end);
    } catch (failure) { $('unavailability-error').textContent = failure.message; $('unavailability-error').hidden = false; return; }
    sending = true; controls(); $('unavailability-error').hidden = true;
    try {
      const result = await request('/api/engineer/unavailability', payload);
      $('unavailability-form').reset(); render(result); syncStatus(true); error('');
      notice('Период недоступности передан диспетчеру. Затронутые назначения изменятся после предпросмотра и утверждения плана.');
    } catch (failure) {
      $('unavailability-error').textContent = failure.message; $('unavailability-error').hidden = false;
      if (failure.status === 409) $('unavailability-error').textContent += ' Обновите задания перед повторной отправкой.';
    } finally { sending = false; controls(); }
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
      notice('Заявка передана в пул перепланирования. Новый план применит диспетчер после предпросмотра.');
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
        time:$('action-time').value, location:$('action-location').value, remaining:$('action-remaining').value,
        router:$('action-router').value, tv:$('action-tv').value, comment:$('action-comment').value
      });
    } catch (failure) { $('action-error').textContent = failure.message; $('action-error').hidden = false; return; }
    sending = true; controls(); $('action-error').hidden = true;
    try {
      const result = await request('/api/engineer/facts', payload);
      action = null; dialog.close(); render(result); syncStatus(true); error('');
      window.dispatchEvent(new CustomEvent('engineer-action',{detail:{status:payload.status}}));
      notice(payload.status === 'completed' ? 'Сообщение о выполнении отправлено. Окончательное закрытие подтвердит диспетчер.' : 'Отметка сохранена: у диспетчера → «Бригады» → «Журнал статусов». Назначения изменятся после утверждения нового плана.');
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
  window.addEventListener('engineer-end-shift',()=>{beginAction('unavailable'); if(action)$('action-comment').value='Смена завершена';});
  $('shift-start').addEventListener('click', () => beginAction('available'));
  $('shift-problem').addEventListener('click', () => beginAction('unavailable'));
  $('search').addEventListener('input', renderTasks);
  document.querySelectorAll('[data-filter]').forEach(b => b.addEventListener('click', () => {
    filter = b.dataset.filter; document.querySelectorAll('[data-filter]').forEach(x => x.setAttribute('aria-pressed', String(x === b))); renderTasks();
  }));
  ['tasks','map','shift'].forEach(name => $('tab-' + name).addEventListener('click', () => activate(name)));
  $('fit-map').addEventListener('click', fitMap);
  $('engineer-select').addEventListener('change', () => {
    engineerId = $('engineer-select').value; data = null; syncStatus(false); dialog.close();
    $('unavailability-form').reset(); $('unavailability-list').replaceChildren(); $('unavailability-error').hidden = true;
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
  window.addEventListener('online', () => { if (!action) load(true); });
  window.addEventListener('offline', () => syncStatus(false));
  async function poll() {
    if (document.hidden || sending || loading || polling || !engineerId) return;
    polling = true;
    try {
      const revision = await request('/api/revision');
      if (!data || revision.version !== data.version || revision.version !== rosterVersion || !connected) {
        if (action) {
          if (revision.version !== action.version) {
            action.stale = true; controls();
            $('action-error').textContent = 'Данные изменились на другом устройстве. Закройте карточку, обновите задания и проверьте действие.';
            $('action-error').hidden = false;
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
