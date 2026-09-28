(function () {
  'use strict';

  const legacyKinds = [
    ['capacity', 'Нехватка бригад', 'Не хватит доступных исполнителей для оставшихся заявок.'],
    ['windows', 'Опоздания к клиентам', 'Хотя бы один визит начнётся после клиентского окна.'],
    ['equipment', 'Нехватка оборудования', 'Для выполнения заявки не окажется нужного устройства.']
  ];
  const engineKinds = [
    {id:'sla', aliases:['sla','sla_breach','window_sla'], field:'p_sla_breach', title:'Нарушение срока',
      definition:'Хотя бы один визит нарушит применимый SLA окна или реакции.'},
    {id:'late', aliases:['late','late_start'], field:'p_late_start', title:'Начало позже плана',
      definition:'Хотя бы один визит начнётся позже утверждённого времени.'},
    {id:'overtime', aliases:['overtime'], field:'p_overtime', title:'Переработка',
      definition:'Хотя бы один маршрут завершится после конца смены.'},
    {id:'vehicle', aliases:['vehicle','vehicle_failure'], field:null, title:'Транспорт',
      definition:'Риск недоступности машины по пробегу, ТО и истории отказов.'}
  ];
  const isFiniteNumber = value => typeof value === 'number' && Number.isFinite(value);
  const probability = value => isFiniteNumber(value) && value >= 0 && value <= 1;
  const boundedText = (value, maximum = 1000) => typeof value === 'string' && value.trim() && value.length <= maximum ? value.trim() : null;
  const boundedList = (value, maximum = 20) => Array.isArray(value)
    ? value.filter(item => boundedText(item, 1000)).slice(0, maximum).map(item => item.trim()) : [];

  function isEngineForecast(forecast) {
    if (!forecast || typeof forecast !== 'object') return false;
    if (engineKinds.some(kind => kind.field && isFiniteNumber(forecast[kind.field]))) return true;
    if (Array.isArray(forecast.routes)) return true;
    return Array.isArray(forecast.risks) && forecast.risks.some(item =>
      item && engineKinds.some(kind => kind.aliases.includes(item.id)));
  }

  function directVehicleRisk(forecast) {
    const vehicle = forecast.vehicle_risk || forecast.vehicle;
    return vehicle && typeof vehicle === 'object' ? vehicle : null;
  }

  function collectVisits(forecast) {
    if (Array.isArray(forecast?.visits)) return forecast.visits.filter(item => item && typeof item === 'object').slice(0, 500);
    if (!Array.isArray(forecast?.routes)) return [];
    return forecast.routes.flatMap(route => Array.isArray(route?.visits) ? route.visits : []).filter(item => item && typeof item === 'object').slice(0, 500);
  }

  // Review threshold, not an expected count or a confirmed late arrival.
  function forecastCounts(forecast) {
    const visits=collectVisits(forecast),routes=Array.isArray(forecast?.routes)?forecast.routes:[];
    const count=(rows,field)=>rows.length && rows.every(row=>probability(row?.[field]))
      ? {count:rows.filter(row=>row[field]>=.2).length,total:rows.length} : null;
    return {sla:count(visits,'p_sla_breach'),late:count(visits,'p_late_start'),overtime:count(routes,'p_overtime')};
  }

  function derivedReasons(kind, forecast) {
    const result = [];
    if (kind.id === 'sla') {
      if (probability(forecast.p_window_sla_breach)) result.push('SLA клиентского окна: ' + Math.round(forecast.p_window_sla_breach * 1000) / 10 + '%.');
      if (probability(forecast.p_emergency_response_sla_breach)) result.push('SLA реакции на аварию: ' + Math.round(forecast.p_emergency_response_sla_breach * 1000) / 10 + '%.');
    } else if (kind.id === 'late') {
      const visits = collectVisits(forecast).filter(visit => probability(visit.p_late_start) && visit.p_late_start > 0);
      if (visits.length) result.push('Визитов с ненулевой оценкой позднего начала: ' + visits.length + '.');
    } else if (kind.id === 'overtime' && isFiniteNumber(forecast.expected_overtime_minutes)) {
      result.push('Ожидаемая переработка по плану: ' + Math.round(forecast.expected_overtime_minutes * 10) / 10 + ' мин.');
    } else if (kind.id === 'vehicle') {
      result.push('Без пробега, истории ТО и отказов вероятность поломки не рассчитывается.');
    }
    return result;
  }

  function defaultRecommendation(id) {
    return {
      sla:'Проверьте визиты с риском нарушения срока и сравните варианты плана.',
      late:'Уточните текущий статус и остаток работ, затем рассчитайте варианты по подтверждённым фактам.',
      overtime:'Сравните перераспределение оставшихся визитов до окончания смен.',
      vehicle:'Проверьте назначенную машину, пробег, срок ТО и открытые дефекты перед изменением маршрута.'
    }[id];
  }

  function engineRisks(forecast) {
    const source = Array.isArray(forecast?.risks) ? forecast.risks : [];
    return engineKinds.map(kind => {
      const supplied = source.find(item => item && kind.aliases.includes(item.id)) || (kind.id === 'vehicle' ? directVehicleRisk(forecast) : null) || {};
      const direct = kind.field ? forecast?.[kind.field] : null;
      const value = isFiniteNumber(supplied.value) ? supplied.value : isFiniteNumber(supplied.score) ? supplied.score : direct;
      const measure = boundedText(supplied.measure, 40) || (kind.id === 'vehicle' ? (isFiniteNumber(value) ? 'score' : 'not_available') : 'probability');
      const reasons = boundedList(supplied.reasons, 5);
      return {...kind, value, measure, reasons:reasons.length ? reasons : derivedReasons(kind, forecast || {}),
        recommendation:boundedText(supplied.recommendation) || defaultRecommendation(kind.id),
        status:boundedText(supplied.status, 80), raw:supplied};
    });
  }

  function lossUnitLabel(forecast) {
    const units = forecast?.loss_units;
    const unit = boundedText(forecast?.loss_unit, 80) || boundedText(typeof units === 'object' ? units.unit : units, 80);
    if (!unit) return 'единицы не указаны';
    if (['conditional_units','conditional_loss_units','loss_units','units'].includes(unit)) return 'условных ед.';
    return unit.replaceAll('_', ' ');
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = {forecastCounts,isEngineForecast, engineRisks, collectVisits, lossUnitLabel};
  if (typeof document === 'undefined') return;

  const $ = id => document.getElementById(id);
  if (!$('panel-risk')) return;
  let snapshot = null, active = false, demo = false, version = null, demoRunId = null, sequence = 0, controller = null, planningBusy = false, planTime = null;
  let summarySequence=0,summaryController=null;
  let attentionFilter = 'all', attentionLimit = 6;
  const expandedIssues = new Set();
  const text = (tag, value, cls) => { const el = document.createElement(tag); if (value != null) el.textContent = value; if (cls) el.className = cls; return el; };
  const percent = value => value == null ? '—' : new Intl.NumberFormat('ru-RU', {maximumFractionDigits:1}).format(value) + '%';
  const number = value => isFiniteNumber(value) ? new Intl.NumberFormat('ru-RU', {maximumFractionDigits:2}).format(value) : '—';
  const clock = value => value == null || value === '' || !isFiniteNumber(Number(value)) ? '—'
    : String(Math.floor(Number(value) / 60)).padStart(2, '0') + ':' + String(Math.round(Number(value) % 60)).padStart(2, '0');

  function renderIndicators() {
    $('risk-indicators').replaceChildren(...snapshot.indicators.map(item => {
      const card = text('article', null, 'risk-card'); card.dataset.indicator = item.id;
      const facts = item.facts || {};
      let value = percent(item.value), label = item.caption, detail = item.detail;
      if (item.id === 'capacity') {
        label = 'времени смен занято';
      } else if (item.id === 'windows') {
        value = Number.isInteger(facts.tight_visits) && Number.isInteger(facts.checked_visits)
          ? facts.tight_visits + ' из ' + facts.checked_visits : '—';
        label = 'визитов с небольшим запасом';
        detail = 'До конца окна — ' + (isFiniteNumber(facts.threshold_minutes) ? number(facts.threshold_minutes) : '15') + ' мин или меньше';
      } else if (item.id === 'equipment') {
        const equipment = Array.isArray(facts.equipment) ? facts.equipment : [];
        const known = equipment.length && equipment.every(row => Number.isInteger(row.needed) && Number.isInteger(row.on_hand));
        const missing = known ? equipment.reduce((total, row) => total + Math.max(0, row.needed - row.on_hand), 0) : null;
        value = missing === null ? '—' : missing ? number(missing) : 'Есть запас';
        label = missing ? 'устройств не хватает по району' : 'суммарно по району';
        detail += '. Только назначенные визиты; передача между инженерами не подтверждена.';
      }
      card.append(text('h3', item.title), text('strong', value, 'risk-indicator-value'),
        text('span', label, 'risk-caption'), text('p', detail, 'risk-detail'));
      return card;
    }));
  }

  const attentionFilters = [
    ['unassigned','Без исполнителя','заявок'], ['tight','Малый запас окна','визитов'],
    ['verify','Проверить факты','инженеров'], ['overtime','Риск переработки','инженеров']
  ];
  const evidenceLabels = {fact:'Факт', plan:'По плану', forecast:'Прогноз', unknown:'Нужно уточнить'};
  function currentAttention() {
    const control = snapshot?.attention;
    return control && control.plan_version === snapshot.context.plan_version && control.time === snapshot.context.time
      && Array.isArray(control.items) && control.counts ? control : null;
  }
  function remainingLabel(minutes) {
    if (!isFiniteNumber(minutes)) return 'Срок не указан';
    if (minutes < 0) return 'Срок прошёл ' + Math.ceil(-minutes) + ' мин назад';
    if (minutes === 0) return 'Срок сейчас';
    return 'До срока ' + (minutes < 60 ? Math.floor(minutes) + ' мин' : Math.floor(minutes / 60) + ' ч ' + Math.floor(minutes % 60) + ' мин');
  }
  function attentionAction(item) {
    if (item.action === 'resources') {
      const link = text('a', 'Уточнить ресурсы', 'risk-resource-link');
      link.href = window.FieldRouter ? window.FieldRouter.url('/crew') : '/crew';
      return link;
    }
    const button = previewButton({id:item.id, action:['preview','completions'].includes(item.action) ? item.action : 'facts',
      planVersion:item.plan_version, riskId:item.kind, engineerId:item.engineer_id,
      requestId:item.requests?.[0]?.request_id || null});
    button.textContent = item.action === 'preview' ? 'Рассмотреть варианты' : item.action === 'completions' ? 'Проверить результат' : item.kind === 'data' ? 'Связаться / уточнить' : 'Уточнить факт';
    return button;
  }
  function renderAttention() {
    const control = currentAttention(), list = $('risk-attention-list');
    if (!list) return;
    $('risk-attention-filters').replaceChildren(...attentionFilters.map(([key,label,unit]) => {
      const count = control?.counts[key];
      const button = text('button', null, 'risk-filter'); button.type = 'button'; button.dataset.filter = key;
      button.setAttribute('aria-pressed', String(attentionFilter === key));
      button.disabled = !control;
      button.append(text('span', label), text('strong', Number.isInteger(count) ? count : '—'), text('small', unit));
      button.onclick = () => {attentionFilter = attentionFilter === key ? 'all' : key; attentionLimit = 6; renderAttention();};
      return button;
    }));
    $('risk-attention-all').setAttribute('aria-pressed', String(attentionFilter === 'all'));
    const query = ($('risk-attention-search').value || '').trim().toLocaleLowerCase('ru');
    const items = (control?.items || []).filter(item => (attentionFilter === 'all' || item.categories.includes(attentionFilter))
      && (!query || [item.title,item.engineer_name,item.engineer_id,...(item.requests || []).flatMap(r => [r.request_id,r.address])].join(' ').toLocaleLowerCase('ru').includes(query)));
    $('risk-attention-count').textContent = control ? 'Ситуаций: ' + items.length : '';
    $('risk-attention-more').hidden = items.length <= attentionLimit;
    $('risk-attention-more').textContent = 'Показать ещё · осталось ' + Math.max(0, items.length - attentionLimit);
    if (!items.length) {
      list.replaceChildren(text('p', !control ? 'Список ситуаций пока недоступен. Обновите данные; отсутствие списка не означает отсутствие рисков.'
        : query || attentionFilter !== 'all' ? 'По этому фильтру ситуаций нет. Посмотрите остальные категории.'
        : 'По доступным данным ситуаций для вмешательства не найдено. Это не гарантирует отсутствие рисков при неполных данных.', 'risk-empty'));
    } else list.replaceChildren(...items.slice(0, attentionLimit).map(item => {
      const card = text('article', null, 'risk-issue'); card.dataset.issue = item.id; card.dataset.severity = item.severity;
      const body = text('div', null, 'risk-issue-body'), top = text('div', null, 'risk-issue-heading');
      const badge = text('span', evidenceLabels[item.evidence] || 'Нужно уточнить', 'risk-evidence'); badge.dataset.evidence = item.evidence;
      top.append(badge, text('h3', item.title)); body.append(top);
      const requests = Array.isArray(item.requests) ? item.requests : [], single = requests.length === 1 ? requests[0] : null;
      if (single) body.append(text('p', single.priority_label + ' · ' + single.request_id + ' · ' + single.address, 'risk-issue-subject'));
      else if (requests.length) body.append(text('p', 'Затронуто заявок: ' + requests.length, 'risk-issue-subject'));
      const meta = text('p', null, 'risk-issue-meta');
      if (single && item.engineer_name) meta.append(text('span', item.engineer_name));
      if (single) meta.append(text('span', 'Окна: ' + single.windows.map(w => clock(w.start) + '–' + clock(w.end)).join(' / ')));
      if (isFiniteNumber(item.buffer_minutes)) meta.append(text('span', 'Запас по плану: ' + item.buffer_minutes + ' мин'));
      body.append(meta);
      body.append(text('p', item.detail.split('. ')[0] + (item.detail.includes('. ') ? '.' : ''), 'risk-issue-reason'));
      const details = text('details', null, 'risk-issue-details'); details.open = expandedIssues.has(item.id);
      details.append(text('summary', requests.length > 1 ? 'Причина и затронутые заявки' : 'Почему и что проверить'), text('p', item.detail));
      if (requests.length > 1) {
        const ul = text('ul');
        requests.forEach(request => ul.append(text('li', request.priority_label + ' · ' + request.request_id + ' · ' + request.address
          + (request.planned_start != null ? ' · по плану ' + clock(request.planned_start) : '') + ' · срок ' + clock(request.deadline)
          + (item.kind === 'late' && isFiniteNumber(request.expected_late_minutes) ? ' · ожидаемое отклонение ≈ ' + Math.round(request.expected_late_minutes) + ' мин' : '')
          + (item.kind === 'late' && probability(request.p_sla_breach) ? ' · оценка нарушения срока ' + percent(request.p_sla_breach * 100) : ''))));
        details.append(ul);
      }
      details.addEventListener('toggle', () => {if (details.open) expandedIssues.add(item.id); else expandedIssues.delete(item.id);});
      body.append(details);
      const aside = text('div', null, 'risk-issue-action');
      aside.append(text('strong', item.kind === 'overtime' ? 'К концу смены ' + clock(item.deadline) : remainingLabel(item.minutes_to_deadline)),
        text('span', item.kind === 'overtime' ? '≈ ' + Math.round(item.expected_overtime_minutes) + ' мин сверх смены' : item.deadline != null ? 'Срок ' + clock(item.deadline) : '', 'hint'), attentionAction(item));
      card.append(body, aside); return card;
    }));
    $('risk-data-notices').replaceChildren(...(control?.notices || []).map(notice => text('p', notice)));
  }

  function example() {
    const now = snapshot.context.time;
    return {status:'ready',region:snapshot.context.region,plan_version:snapshot.context.plan_version,
      model_version:'risk-engine-v1 · пример',horizon_minutes:180,calibrated:false,iterations:600,
      loss_unit:'conditional_loss_units',loss_weights:{late_start:.25,sla_breach:10,overtime_minute:.05},expected_loss:3.4,
      expected_affected_customers:.62,p_late_start:.46,p_sla_breach:.31,p_window_sla_breach:.31,
      p_emergency_response_sla_breach:null,p_overtime:.17,expected_overtime_minutes:8.4,risks:[
        {id:'sla',measure:'probability',value:.31,reasons:['В примере один визит имеет малый запас до конца окна.'],recommendation:'Сравнить варианты с меньшим риском нарушения окна.'},
        {id:'late',measure:'probability',value:.46,reasons:['Вариация первой работы переносится на следующий визит.'],recommendation:'Уточнить остаток текущей работы и рассчитать варианты.'},
        {id:'overtime',measure:'probability',value:.17,reasons:['Последний визит расположен близко к концу смены.'],recommendation:'Проверить перенос последнего визита другой бригаде.'},
        {id:'vehicle',measure:'not_available',value:null,reasons:['В примере нет истории пробега, ТО и отказов.'],recommendation:'Сначала заполнить данные автомобиля; вероятность поломки не имитируется.'}
      ],routes:[{engineer_id:'E-01',p_late_start:.46,p_sla_breach:.31,p_overtime:.17,expected_overtime_minutes:8.4,
        expected_loss:3.4,expected_affected_customers:.62,visits:[{request_id:'R-101',engineer_id:'E-01',planned_start:now+35,
          planned_sla_deadline:now+90,p_late_start:.24,p_sla_breach:.12,p_overtime:.02,expected_loss:.7,
          service_duration:{p50_minutes:70,p80_minutes:88,p90_minutes:101,source:'synthetic_prior'}}]}],
      assumptions:['Длительность дороги и работ варьируется синтетически.', 'Оценка не откалибрована на фактической истории.']};
  }

  function validRisk(result) {
    if (!result) return false;
    if (result.measure === 'probability') return probability(result.value);
    if (['score','heuristic_score'].includes(result.measure)) return isFiniteNumber(result.value) && result.value >= 0 && result.value <= 100;
    return false;
  }

  function riskDisplay(result) {
    if (!validRisk(result)) return '—';
    if (result.measure === 'probability') return percent(result.value * 100);
    const score = result.value <= 1 ? result.value * 100 : result.value;
    return number(score) + ' / 100';
  }

  function renderRiskCard(result, isDemo, counts) {
    const card = text('article', null, 'risk-prediction'); card.dataset.risk = result.id;
    const count=counts?.[result.id];
    const counted=['sla','late','overtime'].includes(result.id);
    const label=result.id==='overtime'?'бригад с риском переработки':result.id==='late'?'визитов с риском позднего начала':'визитов с риском нарушения срока';
    card.append(text('h4', result.title), text('strong', counted ? (count ? count.count+' из '+count.total : '—') : riskDisplay(result), 'risk-probability'),
      text('span', counted ? (count ? label : 'нет подробной оценки') : result.measure==='probability'?'оценка вероятности':'индекс · не вероятность', 'risk-prediction-label'));
    if(count)card.append(text('small','Оценка риска ≥ 20%', 'risk-count-threshold'));
    if (isDemo) card.append(text('span', 'Демонстрационная оценка', 'risk-demo-tag'));
    return card;
  }

  function legacyResults(forecast, usable) {
    return legacyKinds.map(([id, title, definition]) => {
      const item = usable && Array.isArray(forecast.risks) ? forecast.risks.find(value => value.id === id) : null;
      const valid = item && ['probability','score'].includes(item.measure) && isFiniteNumber(item.value)
        && item.value >= 0 && item.value <= 1 && boundedList(item.reasons, 5).length
        && boundedText(item.recommendation);
      return {id,title,definition,measure:valid ? item.measure : 'not_available',value:valid ? item.value : null,
        reasons:valid ? boundedList(item.reasons, 5) : [], recommendation:valid ? item.recommendation : 'Уточните данные и повторите расчёт.'};
    });
  }

  function metaItem(label, value, className) {
    const item = text('div', null, className); item.append(text('span', label), text('strong', value)); return item;
  }

  function renderMeta(forecast, usable, modern) {
    const box = $('risk-engine-meta');
    box.hidden = !usable || !modern;
    if (box.hidden) { box.replaceChildren(); return; }
    const calibrated = forecast.calibrated === true;
    const lossUnits = typeof forecast.loss_units === 'object' && forecast.loss_units || {};
    const expectedLoss = isFiniteNumber(forecast.expected_loss) ? forecast.expected_loss : lossUnits.expected_loss;
    const expectedCustomers = isFiniteNumber(forecast.expected_affected_customers) ? forecast.expected_affected_customers : null;
    const evidence = forecast.duration_evidence && typeof forecast.duration_evidence === 'object' ? forecast.duration_evidence : {};
    const observationCount = Number.isInteger(evidence.observation_count) && evidence.observation_count >= 0 ? evidence.observation_count : 0;
    const groupCount = Number.isInteger(evidence.group_count) && evidence.group_count >= 0 ? evidence.group_count : 0;
    const weights = forecast.loss_weights || lossUnits.weights || {};
    const weightLabel = ['late_start','sla_breach','overtime_minute'].every(key => isFiniteNumber(weights[key]))
      ? 'опоздание ' + number(weights.late_start) + ' · SLA ' + number(weights.sla_breach) + ' · переработка ' + number(weights.overtime_minute) + '/мин'
      : 'не указаны';
    box.replaceChildren(
      metaItem('Версия модели', boundedText(forecast.model_version, 200) || 'не указана'),
      metaItem('Калибровка', calibrated ? 'подтверждена на фактической истории' : 'не подтверждена · нужна проверка на исходах', calibrated ? '' : 'risk-uncalibrated'),
      metaItem('Данные длительности', observationCount
        ? observationCount + ' подтверждённых фактов · ' + groupCount + ' групп'
        : 'априорные значения · подтверждённых фактов пока нет'),
      metaItem('Монте-Карло', Number.isInteger(forecast.iterations) && forecast.iterations > 0 ? forecast.iterations + ' прогонов' : 'число прогонов не указано'),
      metaItem('Ожидаемые потери', isFiniteNumber(expectedLoss) ? number(expectedLoss) + ' ' + lossUnitLabel(forecast) : 'не рассчитаны'),
      metaItem('Ожидаемо затронуто', expectedCustomers == null ? 'не рассчитано' : number(expectedCustomers) + ' клиента'),
      metaItem('Веса loss units', weightLabel)
    );
  }

  function renderRoutes(forecast, usable, modern, isDemo) {
    const detailsNode = $('risk-route-details'), container = $('risk-routes');
    const routes = usable && modern && Array.isArray(forecast.routes)
      ? forecast.routes.filter(route => route && typeof route === 'object').slice(0, 100)
      : usable && modern && collectVisits(forecast).length ? [{visits:collectVisits(forecast)}] : [];
    detailsNode.hidden = !routes.length; container.replaceChildren();
    if (!routes.length) return;
    const visitCount = routes.reduce((total, route) => total + (Array.isArray(route.visits) ? route.visits.filter(visit => visit && typeof visit === 'object').slice(0, 200).length : 0), 0);
    detailsNode.querySelector('summary').textContent = 'Визиты в плане · ' + visitCount;
    container.replaceChildren(...routes.map(route => {
      const card = text('article', null, 'risk-route');
      card.append(text('h4', boundedText(route.engineer_name, 300) || boundedText(route.engineer_id, 300) || 'Визиты'));
      const visits = Array.isArray(route.visits) ? route.visits.filter(visit => visit && typeof visit === 'object').slice(0, 200) : [];
      if (visits.length) {
        const scroll = text('div', null, 'risk-visit-table'), table = text('table'), head = text('thead'), header = text('tr');
        ['Визит','Начало','Срок','Риск нарушения срока','Действие'].forEach(label => { const cell = text('th', label); cell.setAttribute('scope', 'col'); header.append(cell); });
        head.append(header); const body = text('tbody');
        visits.forEach(visit => {
          const deadline = isFiniteNumber(visit.emergency_response_sla_deadline) ? visit.emergency_response_sla_deadline : visit.planned_sla_deadline;
          const row = text('tr'), action = text('td');
          const requestId = boundedText(visit.request_id, 300);
          if (requestId && !isDemo) action.append(previewButton({id:'visit-' + requestId, action:'facts', planVersion:snapshot.context.plan_version,
            riskId:'sla', engineerId:boundedText(visit.engineer_id, 300) || boundedText(route.engineer_id, 300), requestId}));
          row.append(text('td', boundedText(visit.request_name, 300) || requestId || 'Без названия'),
            text('td', clock(visit.planned_start)), text('td', clock(deadline)),
            text('td', probability(visit.p_sla_breach) ? percent(visit.p_sla_breach * 100) + ' · оценка' : 'Нет оценки'), action);
          body.append(row);
        });
        table.append(head, body); scroll.append(table); card.append(scroll);
      }
      return card;
    }));
  }

  function renderAssumptions(forecast, usable, modern) {
    const box = $('risk-assumption-details'), assumptions = usable && modern ? boundedList(forecast.assumptions, 30) : [];
    box.hidden = !assumptions.length;
    $('risk-assumptions').replaceChildren(...assumptions.map(value => text('li', value)));
  }

  function severityFor(result) {
    const value = result.measure === 'probability' ? result.value : result.value > 1 ? result.value / 100 : result.value;
    return value >= .5 ? 'high' : value >= .2 ? 'medium' : 'low';
  }

  function normalizedAdvisories(forecast, risks) {
    const supplied = Array.isArray(forecast.advisories) ? forecast.advisories.filter(item => item && typeof item === 'object').slice(0, 20) : [];
    if (Array.isArray(forecast.advisories)) return supplied.map((item, index) => ({
      id:boundedText(item.id, 200) || 'advisory-' + index, riskId:boundedText(item.risk_id, 80),
      title:boundedText(item.title, 300) || 'Проверить рекомендацию',
      summary:boundedText(item.summary) || boundedText(item.detail) || boundedText(item.recommendation) || boundedText(item.reason) || 'Сравните варианты плана перед решением.',
      severity:['low','medium','high'].includes(item.severity) ? item.severity : 'medium',
      engineerId:boundedText(item.engineer_id, 300), requestId:boundedText(item.request_id, 300),
      planVersion:Number.isInteger(item.plan_version) ? item.plan_version : null,
      action:item.action === 'facts' ? 'facts' : 'preview'
    }));
    return risks.filter(result => validRisk(result) && result.value > 0).map(result => ({
      id:'risk-' + result.id, riskId:result.id, title:'Проверить · ' + result.title,
      summary:defaultRecommendation(result.id) || result.recommendation, severity:severityFor(result), engineerId:null, requestId:null, planVersion:null
    }));
  }

  function previewButton(advisory) {
    const stale = advisory.planVersion != null && advisory.planVersion !== snapshot.context.plan_version;
    const button = text('button', stale ? 'Рекомендация устарела' : advisory.action === 'facts' ? 'Уточнить факт' : 'Рассчитать варианты', 'secondary risk-preview'); button.type = 'button';
    button.disabled = planningBusy || stale; button.dataset.advisory = advisory.id; button.dataset.stale = String(stale);
    button.addEventListener('click', () => {
      if (!snapshot || planningBusy || stale || advisory.planVersion != null && advisory.planVersion !== snapshot.context.plan_version) return;
      window.dispatchEvent(new CustomEvent('risk-intent', {detail:{action:advisory.action || 'preview', planVersion:advisory.planVersion ?? snapshot.context.plan_version,
        advisoryId:advisory.id, riskId:advisory.riskId, engineerId:advisory.engineerId, requestId:advisory.requestId}}));
    });
    return button;
  }

  function renderAdvisories(forecast, risks, usable, modern, isDemo) {
    const section = $('risk-advisory-section'), advisories = usable && modern && !isDemo ? normalizedAdvisories(forecast, risks) : [];
    section.hidden = !advisories.length;
    $('risk-advisories').replaceChildren(...advisories.map(advisory => {
      const card = text('article', null, 'risk-advisory'); card.dataset.severity = advisory.severity;
      card.append(text('h4', advisory.title), text('p', advisory.summary));
      const context = text('div', null, 'risk-advisory-context');
      if (advisory.engineerId) context.append(text('span', 'Инженер: ' + advisory.engineerId));
      if (advisory.requestId) context.append(text('span', 'Заявка: ' + advisory.requestId));
      if (context.children.length) card.append(context);
      card.append(previewButton(advisory)); return card;
    }));
  }

  function clearForecastDetails() {
    if ($('risk-attention-list')) {
      $('risk-attention-list').replaceChildren(); $('risk-attention-filters').replaceChildren();
      $('risk-attention-count').textContent = ''; $('risk-attention-more').hidden = true;
      $('risk-data-notices').replaceChildren();
    }
    $('risk-raw-details').hidden=true;$('risk-raw-payload').textContent='';
    $('risk-engine-meta').hidden = true; $('risk-engine-meta').replaceChildren();
    $('risk-advisory-section').hidden = true; $('risk-advisories').replaceChildren();
    $('risk-route-details').hidden = true; $('risk-routes').replaceChildren();
    $('risk-assumption-details').hidden = true; $('risk-assumptions').replaceChildren();
  }

  function renderForecast() {
    $('risk-demo').setAttribute('aria-pressed', String(demo));
    $('risk-demo').textContent = demo ? 'Вернуться к прогнозу для плана' : 'Показать учебный пример';
    $('risk-demo-notice').hidden = !demo;
    $('risk-forecasts').classList.toggle('risk-example', demo);
    const forecast = demo ? example() : snapshot.forecast || {};
    const modern = isEngineForecast(forecast);
    const matches = forecast.region === snapshot.context.region && forecast.plan_version === snapshot.context.plan_version;
    const horizonValid = forecast.horizon_minutes == null ? modern : Number.isInteger(forecast.horizon_minutes) && forecast.horizon_minutes > 0 && forecast.horizon_minutes <= 1440;
    const usable = forecast.status === 'ready' && matches && (demo || !snapshot.context.needs_replanning) && horizonValid;
    const notices = {not_connected:'Оценка пока недоступна. Проверьте текущие показатели и статусы.',
      insufficient_data:'Для прогноза недостаточно данных. Показатели плана доступны выше.',
      error:'Прогноз временно недоступен. Планирование и показатели плана продолжают работать.',
      pending:'Обновляем оценку текущего плана…',
      stale:'Прогноз устарел. Нужна оценка текущей версии плана.'};
    $('risk-model-status').textContent = usable
      ? (demo ? 'Учебный пример — только в разделе «Ресурсы и расчёт». Список ситуаций относится к вашему плану. ' : '') + 'Прогнозы не откалиброваны на фактических исходах. Факты, расчёт по плану и прогнозы отмечены отдельно.'
      : forecast.status === 'ready' ? notices.stale : notices[forecast.status] || notices.error;
    if (currentAttention()?.gps_available === false) $('risk-model-status').textContent += ' GPS-сигналы для плана недоступны.';
    const risks = modern ? engineRisks(forecast) : legacyResults(forecast, usable);
    $('risk-forecasts').replaceChildren(...(usable ? risks.filter(validRisk).map(result => renderRiskCard(result, demo, forecastCounts(forecast))) : []));
    const advisoryForecast = Array.isArray(snapshot.advisories) && !Array.isArray(forecast.advisories)
      ? {...forecast, advisories:snapshot.advisories} : forecast;
    const explicitAdvisories = Array.isArray(advisoryForecast.advisories);
    const advisoryUsable = explicitAdvisories ? matches : usable;
    $('risk-raw-details').hidden=!usable;
    $('risk-raw-payload').textContent=usable?JSON.stringify({demo,context:snapshot.context,forecast,attention:currentAttention()},null,2):'';
    renderMeta(forecast, usable, modern); renderAdvisories(advisoryForecast, risks, advisoryUsable, modern, demo);
    // Operational incidents already include GPS and facts; avoid a duplicate feed.
    if (currentAttention()) {$('risk-advisory-section').hidden = true; $('risk-advisories').replaceChildren();}
    renderRoutes(forecast, usable, modern, demo); renderAssumptions(forecast, usable, modern);
  }

  function validSnapshot(data) {
    const c = data?.context;
    return data?.schema_version === '1.0' && c && Number.isInteger(c.plan_version) && c.plan_version > 0
      && typeof c.region === 'string' && typeof c.date === 'string' && Number.isFinite(c.time)
      && data.summary && Number.isInteger(data.summary.engineers) && Number.isInteger(data.summary.roster)
      && Number.isInteger(data.summary.unassigned) && Array.isArray(data.indicators) && data.indicators.length === 3
      && legacyKinds.every(([id]) => data.indicators.filter(item => item.id === id).length === 1)
      && data.indicators.every(item => (item.value === null || Number.isFinite(item.value) && item.value >= 0)
        && ['title','caption','detail','explanation','recommendation'].every(key => typeof item[key] === 'string'));
  }

  function clearSummary(){
    summarySequence++;summaryController?.abort();summaryController=null;
    $('risk-summary').hidden=true;$('risk-summary').replaceChildren();
    $('risk-summary-button').disabled=true;
  }
  function summaryControls(){
    const enabled=!!snapshot?.llm_enabled && !!snapshot?.snapshot_id && !demo;
    $('risk-summary-button').disabled=!enabled || !!summaryController;
    $('risk-summary-button').title=enabled?'':demo?'Вернитесь к показателям плана.':snapshot?'ИИ не подключён.':'Сначала обновите показатели.';
  }
  async function summarizeDay(){
    if(!snapshot || demo || summaryController || !snapshot.llm_enabled)return;
    const current=++summarySequence,id=snapshot.snapshot_id,planVersion=snapshot.context.plan_version;
    summaryController=new AbortController();const requestController=summaryController,signal=requestController.signal;
    const timer=setTimeout(()=>requestController.abort(),35000);
    const box=$('risk-summary');box.hidden=false;box.replaceChildren(text('p','Готовим короткую сводку…'));summaryControls();
    try{
      const fetcher=window.FieldRouter?window.FieldRouter.fetch:fetch;
      const url=window.FieldRouter?window.FieldRouter.url('/api/risk/summary'):'/api/risk/summary';
      const response=await fetcher(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({snapshot_id:id,version:planVersion,demo_run_id:demoRunId}),signal});
      const result=await response.json();
      if(current!==summarySequence || snapshot?.snapshot_id!==id)return;
      if(!response.ok)throw Error(result.error || 'Сводка временно недоступна.');
      if(result.snapshot_id!==id || !boundedText(result.title,100) || !boundedText(result.action,400)
          || !Array.isArray(result.items) || result.items.length<1 || result.items.length>3
          || result.items.some(item=>!boundedText(item.title,100)||!boundedText(item.detail,300)))throw Error('Не удалось прочитать сводку.');
      const list=text('ul');
      for(const item of result.items){const row=text('li');row.append(text('strong',item.title),text('span',item.detail));list.append(row);}
      const action=text('p',null,'risk-summary-action');action.append(text('strong','Следующий шаг: '),text('span',result.action));
      box.replaceChildren(text('h3',result.title),list,action);
      if(result.notice)box.append(text('p',boundedText(result.notice,300)||'','risk-summary-notice'));
    }catch(error){
      if(current===summarySequence)box.replaceChildren(text('p',signal.aborted?'ИИ не ответил вовремя. Можно повторить запрос.':error.message));
    }finally{
      clearTimeout(timer);
      if(current===summarySequence){summaryController=null;summaryControls();}
    }
  }

  const summaryContext = data => JSON.stringify(data, (key, value) => key === 'snapshot_id' ? undefined : value);
  async function load(background = false) {
    if (!background) clearSummary();
    const current = ++sequence; controller?.abort(); controller = new AbortController();
    const requestController = controller;
    const signal = controller.signal, timeout = setTimeout(() => requestController.abort(), 6000);
    $('risk-refresh').disabled = true; $('risk-demo').disabled = true;
    $('risk-error').hidden = true; $('risk-context').textContent = 'Обновляем показатели…';
    try {
      if(!window.FieldRouter && typeof location!=='undefined' && /^\/(r|regions)\//.test(location.pathname || ''))throw Error('Не загрузилась навигация района. Обновите страницу; данные другого района не будут показаны.');
      const url = window.FieldRouter ? window.FieldRouter.url('/api/risk') : '/api/risk';
      const fetcher = window.FieldRouter ? window.FieldRouter.fetch : fetch;
      const response = await fetcher(url, {cache:'no-store',signal});
      if (!response.ok) throw Error('Показатели временно недоступны. Можно продолжить работу с планом и повторить запрос.');
      const data = await response.json();
      if (current !== sequence) return;
      if (!validSnapshot(data)) throw Error('Не удалось прочитать показатели. Проверьте совместимость модуля риска.');
      if (version !== null && data.context.plan_version !== version) throw Error('Версия плана изменилась. Нажмите «Обновить» в шапке и откройте показатели снова.');
      if (planTime !== null && data.context.time !== planTime) throw Error('Время плана изменилось. Обновите данные в шапке; прежние ситуации скрыты.');
      // A background refresh gets a new snapshot ID even when the underlying
      // facts are unchanged. Keep its completed explanation until facts change.
      if (background && summaryContext(snapshot) !== summaryContext(data)) clearSummary();
      snapshot = data;
      const c = data.context, m = data.summary;
      $('risk-context').textContent = c.region + ' · ' + c.date + ' · на ' + clock(c.time)
        + ' · план №' + c.plan_version + (c.mode === 'simulation' ? ' · исполнение моделируется' : '');
      $('risk-stale').hidden = !c.needs_replanning;
      $('risk-stale').textContent = 'Есть новые факты. Уточните статусы и пересчитайте оставшиеся визиты.';
      renderIndicators(); renderForecast(); renderAttention();
    } catch (error) {
      if (current !== sequence) return;
      clearSummary();
      snapshot = null; demo = false;
      $('risk-demo').setAttribute('aria-pressed', 'false'); $('risk-demo').textContent = 'Показать учебный пример';
      $('risk-indicators').replaceChildren(); $('risk-forecasts').replaceChildren(); clearForecastDetails();
      $('risk-stale').hidden = true; $('risk-demo-notice').hidden = true;
      $('risk-model-status').textContent = 'Прогноз недоступен до загрузки текущего плана.';
      $('risk-error').textContent = signal.aborted ? 'Показатели не ответили вовремя. Повторите запрос; планирование доступно.' : error.message;
      $('risk-error').hidden = false; $('risk-context').textContent = 'Актуальность показателей не подтверждена.';
    } finally {
      clearTimeout(timeout);
      if (current === sequence) { $('risk-refresh').disabled = false; $('risk-demo').disabled = !snapshot; summaryControls(); }
    }
  }

  $('risk-refresh').onclick = () => load();
  $('risk-summary-button').onclick=summarizeDay;
  if ($('risk-attention-all')) {
    $('risk-attention-all').onclick = () => {attentionFilter = 'all'; attentionLimit = 6; $('risk-attention-search').value = ''; renderAttention();};
    $('risk-attention-search').addEventListener('input', () => {attentionLimit = 6; renderAttention();});
    $('risk-attention-more').onclick = () => {attentionLimit += 6; renderAttention();};
  }
  $('risk-demo').onclick = () => {if (snapshot) {
    demo = !demo; clearSummary(); summaryControls(); renderForecast();
    if (demo && $('risk-resources')) {
      $('risk-resources').open = true;
      $('risk-resources').scrollIntoView?.({block:'start'});
    }
  }};
  $('risk-facts').onclick = () => window.dispatchEvent(new CustomEvent('risk-intent', {detail:{action:'facts'}}));
  $('risk-replan').onclick = () => window.dispatchEvent(new CustomEvent('risk-intent', {detail:{action:'replan'}}));
  window.addEventListener('workbench-tab', event => {active = ['risk','demo'].includes(event.detail.name); if (active) load();});
  window.addEventListener('planning-busy', event => {
    planningBusy = !!event.detail?.busy;
    document.querySelectorAll('.risk-preview').forEach(button => {button.disabled = planningBusy || button.dataset.stale === 'true';});
  });
  window.addEventListener('planning-state', event => {
    const runChanged = demoRunId !== (event.detail.demoRunId || null);
    const timeChanged = Number.isFinite(event.detail.time) && planTime !== event.detail.time;
    const changed = version !== event.detail.version || runChanged || timeChanged;
    if (Number.isFinite(event.detail.time)) planTime = event.detail.time;
    version = event.detail.version; demoRunId = event.detail.demoRunId || null;
    if (changed) {
      clearSummary();
      // A reset can reuse version 1; invalidate outstanding responses as well
      // as the snapshot so a previous run cannot reappear on another tab.
      sequence++; controller?.abort(); snapshot = null; demo = false;
      if (runChanged) {attentionFilter = 'all'; attentionLimit = 6; expandedIssues.clear();}
      $('risk-indicators').replaceChildren(); $('risk-forecasts').replaceChildren(); clearForecastDetails();
      $('risk-demo').disabled = true; $('risk-refresh').disabled = false;
      $('risk-demo').setAttribute('aria-pressed', 'false');
      $('risk-demo').textContent = 'Показать учебный пример';
      $('risk-demo-notice').hidden = true; $('risk-stale').hidden = true; $('risk-error').hidden = true;
      $('risk-context').textContent = 'Показатели появятся при открытии вкладки.';
      if (active) load();
    }
  });
  window.addEventListener('focus', () => {if (active) load();});
  // GPS freshness may change without a new plan version. Never advance simulated time here.
  if (typeof window.setInterval === 'function') window.setInterval(() => {
    if (active && !planningBusy && !summaryController && !document.hidden && !$('risk-refresh').disabled) load(true);
  }, 30000);
})();
