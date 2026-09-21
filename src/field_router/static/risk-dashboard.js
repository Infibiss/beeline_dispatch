(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  if (!$('panel-risk')) return;
  const kinds = [
    ['capacity', 'Нехватка бригад', 'Не хватит доступных исполнителей для оставшихся заявок.'],
    ['windows', 'Опоздания к клиентам', 'Хотя бы один визит начнётся после клиентского окна.'],
    ['equipment', 'Нехватка оборудования', 'Для выполнения заявки не окажется нужного устройства.']
  ];
  let snapshot = null, active = false, demo = false, version = null, sequence = 0, controller = null;
  const text = (tag, value, cls) => { const el = document.createElement(tag); if (value != null) el.textContent = value; if (cls) el.className = cls; return el; };
  const percent = value => value == null ? '—' : new Intl.NumberFormat('ru-RU', {maximumFractionDigits:1}).format(value) + '%';
  const clock = value => String(Math.floor(value / 60)).padStart(2, '0') + ':' + String(value % 60).padStart(2, '0');
  function ring(value, caption) {
    const wrap = text('div', null, 'risk-ring');
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 120 120'); svg.setAttribute('aria-hidden', 'true');
    for (const cls of ['risk-track', 'risk-arc']) {
      const circle = document.createElementNS(svg.namespaceURI, 'circle');
      for (const [key, val] of Object.entries({cx:60,cy:60,r:51,fill:'none','stroke-width':9,pathLength:100,class:cls})) circle.setAttribute(key, val);
      if (cls === 'risk-arc') circle.setAttribute('stroke-dasharray', Math.min(100, Math.max(0, value || 0)) + ' 100');
      svg.append(circle);
    }
    const label = text('div', null, 'risk-ring-label');
    label.append(text('strong', percent(value)), text('span', value == null ? 'нет оценки' : 'по плану'));
    wrap.setAttribute('aria-label', percent(value) + ' · ' + caption); wrap.append(svg, label); return wrap;
  }
  function details(label, explanation, recommendation) {
    const box = text('details', null, 'risk-details');
    const summary = text('summary', label); summary.title = recommendation;
    box.append(summary, text('p', explanation), text('strong', 'Что проверить'), text('p', recommendation)); return box;
  }
  function renderIndicators() {
    $('risk-indicators').replaceChildren(...snapshot.indicators.map(item => {
      const card = text('article', null, 'risk-card'); card.dataset.indicator = item.id;
      card.append(text('h3', item.title), ring(item.value, item.caption), text('p', item.caption, 'risk-caption'),
        text('p', item.detail, 'risk-detail'), details('Как читать и что проверить', item.explanation, item.recommendation));
      return card;
    }));
  }
  function example() {
    return {status:'ready',region:snapshot.context.region,plan_version:snapshot.context.plan_version,
      model_version:'Пример интерфейса',horizon_minutes:120,risks:[
        {id:'capacity',measure:'probability',value:.72,
          reasons:['В примере один из двух инженеров с нужным навыком недоступен.', 'Три заявки требуют этого навыка в ближайшие два часа.'],
          recommendation:'Уточнить выход резервного инженера с нужным навыком и сравнить план после изменения статуса.'},
        {id:'windows',measure:'probability',value:.38,
          reasons:['В примере два визита идут подряд с запасом менее 10 минут.', 'Задержка первой работы может сдвинуть следующий визит.'],
          recommendation:'Уточнить окончание текущей работы. При задержке предложить диспетчеру пересчёт или согласование другого окна.'},
        {id:'equipment',measure:'score',value:.15,
          reasons:['В примере есть один запасной роутер у доступной бригады.', 'Передача устройства другой бригаде ещё не подтверждена.'],
          recommendation:'Проверить комплект конкретного исполнителя перед новой заявкой. Значение является индексом модели, а не вероятностью.'}
      ]};
  }
  function renderForecast() {
    $('risk-demo').setAttribute('aria-pressed', String(demo));
    $('risk-demo').textContent = demo ? 'Вернуться к прогнозу для плана' : 'Показать пример ML-прогноза';
    $('risk-demo-notice').hidden = !demo;
    $('risk-forecasts').classList.toggle('risk-example', demo);
    const forecast = demo ? example() : snapshot.forecast || {};
    const matches = forecast.region === snapshot.context.region && forecast.plan_version === snapshot.context.plan_version;
    const usable = forecast.status === 'ready' && matches && (demo || !snapshot.context.needs_replanning)
      && Number.isInteger(forecast.horizon_minutes) && forecast.horizon_minutes > 0 && forecast.horizon_minutes <= 1440;
    const notices = {not_connected:'ML-модель ещё не подключена. Вероятности пока не рассчитываются.',
      insufficient_data:'Для прогноза недостаточно данных. Показатели плана доступны выше.',
      error:'Прогноз временно недоступен. Планирование и показатели плана продолжают работать.',
      pending:'Модель рассчитывает прогноз. Показатели плана доступны выше.',
      stale:'Прогноз устарел. Нужна оценка текущей версии плана.'};
    $('risk-model-status').textContent = usable
      ? (demo ? 'Пример формата ответа · ' : 'Модель: ' + (forecast.model_version || 'версия не указана') + ' · ')
        + 'горизонт ' + forecast.horizon_minutes + ' мин от времени состояния'
      : forecast.status === 'ready' ? notices.stale : notices[forecast.status] || notices.error;
    $('risk-forecasts').replaceChildren(...kinds.map(([id, title, definition]) => {
      const card = text('article', null, 'risk-prediction'); card.dataset.risk = id;
      const result = usable && Array.isArray(forecast.risks) && forecast.risks.find(item => item.id === id);
      const valid = result && ['probability','score'].includes(result.measure) && Number.isFinite(result.value)
        && result.value >= 0 && result.value <= 1 && Array.isArray(result.reasons)
        && result.reasons.length > 0 && result.reasons.length <= 5 && result.reasons.every(x => typeof x === 'string' && x.length <= 500)
        && typeof result.recommendation === 'string' && result.recommendation.length > 0 && result.recommendation.length <= 1000;
      card.append(text('h4', title), text('p', definition, 'risk-definition'));
      const value = text('strong', valid ? (result.measure === 'probability' ? percent(result.value * 100) : Math.round(result.value * 100) + ' / 100') : '—', 'risk-probability');
      const label = text('span', valid ? (result.measure === 'probability' ? 'вероятность события' : 'индекс риска · не вероятность') : 'Нет прогноза', 'risk-prediction-label');
      card.append(value, label);
      if (demo) card.append(text('span', 'Демонстрационная оценка', 'risk-demo-tag'));
      if (valid) {
        const explanation = text('details', null, 'risk-details');
        explanation.append(text('summary', 'Почему такая оценка и что сделать'));
        const list = text('ul'); result.reasons.forEach(reason => list.append(text('li', reason)));
        explanation.append(list, text('strong', 'Рекомендация'), text('p', result.recommendation)); card.append(explanation);
      } else card.append(text('p', 'Здесь появятся причины оценки и рекомендация для диспетчера.', 'risk-placeholder'));
      return card;
    }));
  }
  function validSnapshot(data) {
    const c = data?.context;
    return data?.schema_version === '1.0' && c && Number.isInteger(c.plan_version) && c.plan_version > 0
      && typeof c.region === 'string' && typeof c.date === 'string' && Number.isFinite(c.time)
      && data.summary && Number.isInteger(data.summary.engineers) && Number.isInteger(data.summary.roster)
      && Number.isInteger(data.summary.unassigned) && Array.isArray(data.indicators) && data.indicators.length === 3
      && kinds.every(([id]) => data.indicators.filter(item => item.id === id).length === 1)
      && data.indicators.every(item => (item.value === null || Number.isFinite(item.value) && item.value >= 0)
        && ['title','caption','detail','explanation','recommendation'].every(key => typeof item[key] === 'string'));
  }
  async function load() {
    const current = ++sequence; controller?.abort(); controller = new AbortController();
    const requestController = controller;
    const signal = controller.signal, timeout = setTimeout(() => requestController.abort(), 6000);
    $('risk-refresh').disabled = true; $('risk-demo').disabled = true;
    $('risk-error').hidden = true; $('risk-context').textContent = 'Обновляем показатели…';
    try {
      const url = window.FieldRouter ? window.FieldRouter.url('/api/risk') : '/api/risk';
      const fetcher = window.FieldRouter ? window.FieldRouter.fetch : fetch;
      const response = await fetcher(url, {cache:'no-store',signal});
      if (!response.ok) throw Error('Показатели временно недоступны. Можно продолжить работу с планом и повторить запрос.');
      const data = await response.json();
      if (current !== sequence) return;
      if (!validSnapshot(data)) throw Error('Не удалось прочитать показатели. Проверьте совместимость модуля риска.');
      if (version !== null && data.context.plan_version !== version) throw Error('Версия плана изменилась. Нажмите «Обновить» в шапке и откройте показатели снова.');
      snapshot = data;
      const c = data.context, m = data.summary;
      $('risk-context').textContent = c.region + ' · ' + c.date + ' · версия ' + c.plan_version + ' · на ' + clock(c.time)
        + ' · бригад в расчёте ' + m.engineers + ' из ' + m.roster + ' · без назначения ' + m.unassigned;
      $('risk-stale').hidden = !c.needs_replanning;
      $('risk-stale').textContent = 'Есть новые факты: утверждённый план требует пересчёта. Показатели справочные; прежний ML-прогноз не показывается.';
      renderIndicators(); renderForecast();
    } catch (error) {
      if (current !== sequence) return;
      snapshot = null; demo = false;
      $('risk-demo').setAttribute('aria-pressed', 'false'); $('risk-demo').textContent = 'Показать пример ML-прогноза';
      $('risk-indicators').replaceChildren(); $('risk-forecasts').replaceChildren();
      $('risk-stale').hidden = true; $('risk-demo-notice').hidden = true;
      $('risk-model-status').textContent = 'Прогноз недоступен до загрузки текущего плана.';
      $('risk-error').textContent = signal.aborted ? 'Показатели не ответили вовремя. Повторите запрос; планирование доступно.' : error.message;
      $('risk-error').hidden = false; $('risk-context').textContent = 'Актуальность показателей не подтверждена.';
    } finally {
      clearTimeout(timeout);
      if (current === sequence) { $('risk-refresh').disabled = false; $('risk-demo').disabled = !snapshot; }
    }
  }
  $('risk-refresh').onclick = load;
  $('risk-demo').onclick = () => {if (snapshot) {demo = !demo; renderForecast();}};
  $('risk-facts').onclick = () => window.dispatchEvent(new CustomEvent('risk-intent', {detail:{action:'facts'}}));
  $('risk-replan').onclick = () => window.dispatchEvent(new CustomEvent('risk-intent', {detail:{action:'replan'}}));
  window.addEventListener('workbench-tab', event => {active = event.detail.name === 'risk'; if (active) load();});
  window.addEventListener('planning-state', event => {
    const changed = version !== event.detail.version; version = event.detail.version;
    if (changed) {demo = false; if (active) load();}
  });
  window.addEventListener('focus', () => {if (active) load();});
})();
