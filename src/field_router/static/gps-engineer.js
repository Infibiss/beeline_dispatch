/* Optional tracker: latest fix only, bounded retry, no coupling to task requests. */
(() => {
  'use strict';
  const intervalFor = speed => Number.isFinite(speed) && speed > 1 ? 10000 : 30000;
  const retryFor = failures => Math.min(60000, 2000 * 2 ** Math.min(failures, 5));
  if (typeof module !== 'undefined' && module.exports) module.exports = {intervalFor, retryFor};
  if (typeof document === 'undefined') return;
  const $ = id => document.getElementById(id);
  const appFetch = (...args) => window.FieldRouter ? window.FieldRouter.fetch(...args) : fetch(...args);
  const appUrl = path => window.FieldRouter ? window.FieldRouter.url(path) : path;
  if (!$('gps-start')) return;
  let engineerId = null, trackerId = null, sequence = 0, wanted = false, state = 'off';
  let watchId = null, latest = null, speed = null, sentTimestamp = 0, lastSent = 0;
  let nextTry = 0, failures = 0, inFlight = false, probeAt = 0, generation = 0;
  const supported = window.isSecureContext && !!navigator.geolocation;
  const options = {enableHighAccuracy: true, maximumAge: 5000, timeout: 12000};
  const labels = {off: 'Выключена', active: 'Включена', unavailable: 'Ищем координату', paused: 'На паузе', denied: 'Нет разрешения'};
  const message = text => { $('gps-feedback').textContent = text; };
  function render() {
    $('gps-state').textContent = labels[state];
    $('gps-start').disabled = !supported || !engineerId || wanted;
    $('gps-stop').disabled = !wanted;
  }
  function packet(forState, point = null) {
    return {engineer_id: engineerId, tracker_id: trackerId, sequence: ++sequence, state: forState, point};
  }
  function beacon(forState) {
    if (!engineerId || !trackerId) return;
    const body = JSON.stringify(packet(forState));
    // sendBeacon cannot carry the session CSRF header; keepalive fetch can.
    return appFetch(appUrl('/api/gps/telemetry'), {method: 'POST', headers: {'Content-Type': 'application/json'}, body, keepalive: true}).catch(() => {});
  }
  function clearWatch() {
    if (watchId !== null) navigator.geolocation.clearWatch(watchId);
    watchId = null;
  }
  function stop(forState = 'off') {
    wanted = false; generation++; clearWatch(); state = forState; const pending=beacon(forState); render(); return pending;
  }
  function fix(position, token) {
    if (!wanted || token !== generation || document.hidden) return;
    const c = position.coords;
    if (![c.latitude, c.longitude, c.accuracy, position.timestamp].every(Number.isFinite)) return;
    speed = c.speed;
    if (speed === null && latest) {
      const seconds = (position.timestamp - latest.measured_at_ms) / 1000;
      const metres = Math.hypot((c.latitude - latest.lat) * 111195,
        (c.longitude - latest.lon) * 111195 * Math.cos(c.latitude * Math.PI / 180));
      speed = seconds > 0 && metres > c.accuracy + latest.accuracy_m ? metres / seconds : 0;
    }
    latest = {lat: c.latitude, lon: c.longitude, accuracy_m: Math.max(.1, c.accuracy), measured_at_ms: position.timestamp};
    state = 'active';
    $('gps-fix').textContent = new Date(position.timestamp).toLocaleTimeString('ru-RU') + ' · точность ±' + Math.round(c.accuracy) + ' м';
    render(); tick();
  }
  function gpsError(error, token) {
    if (!wanted || token !== generation) return;
    if (error.code === 1) {
      stop('denied'); message('Разрешение отклонено. Разрешите геолокацию в настройках сайта и включите GPS снова.');
    } else {
      state = 'unavailable'; render();
      message('Не удалось получить свежую координату. Проверьте геолокацию телефона; повторим запрос автоматически.');
      tick();
    }
  }
  function startWatch() {
    if (!wanted || document.hidden || watchId !== null) return;
    const token = generation;
    state = 'unavailable'; probeAt = Date.now();
    watchId = navigator.geolocation.watchPosition(p => fix(p, token), e => gpsError(e, token), options);
    render();
  }
  async function tick() {
    if (!wanted || document.hidden) return;
    const now = Date.now();
    if (now - probeAt >= 60000 && (!latest || now - latest.measured_at_ms >= 45000)) {
      probeAt = now; const token = generation;
      navigator.geolocation.getCurrentPosition(p => fix(p, token), e => gpsError(e, token), options);
    }
    if (inFlight || now < nextTry) return;
    const fresh = latest && now - latest.measured_at_ms < 90000 && latest.measured_at_ms > sentTimestamp;
    const withPoint = fresh && state === 'active' && (now - lastSent >= intervalFor(speed));
    if (!withPoint && now - lastSent < 30000) return;
    const point = withPoint ? latest : null, token = generation;
    const body = JSON.stringify(packet(state, point));
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 5000);
    inFlight = true;
    try {
      const response = await appFetch(appUrl('/api/gps/telemetry'), {method: 'POST', headers: {'Content-Type': 'application/json'}, body, signal: controller.signal});
      const result = await response.json();
      if (token !== generation) return;
      if (!response.ok) {
        if (response.status === 409) { stop(); message(result.error); return; }
        throw new Error(result.error || 'Сервер GPS недоступен');
      }
      lastSent = Date.now(); failures = 0; nextTry = 0;
      if (point) sentTimestamp = point.measured_at_ms;
      $('gps-sent').textContent = new Date(lastSent).toLocaleTimeString('ru-RU') + (point ? ' · координата' : ' · сигнал связи');
      message(result.reason === 'low_accuracy' || result.reason === 'jump'
        ? 'Сервер получил точку, но не использует её для выводов: измерение недостаточно надёжно.'
        : 'Связь работает. Координаты передаются каждые 10–30 секунд при наличии нового измерения.');
    } catch (error) {
      if (token !== generation) return;
      nextTry = Date.now() + retryFor(++failures) + Math.random() * 1000;
      message('Не удалось передать GPS. Повторим автоматически; задания и отправка статусов работают независимо.');
    } finally { clearTimeout(timer); inFlight = false; }
  }
  $('gps-start').addEventListener('click', () => {
    if (!supported || !engineerId || wanted) return;
    trackerId = crypto.randomUUID?.() || 'tracker-' + Date.now() + '-' + Math.random().toString(36).slice(2);
    sequence = 0; latest = null; sentTimestamp = 0; lastSent = 0; nextTry = 0; failures = 0;
    wanted = true; generation++; message('Разрешите браузеру определять местоположение.'); startWatch();
  });
  window.addEventListener('account-logout', event => {if(wanted) event.detail.tasks.push(stop());});
  $('gps-stop').addEventListener('click', () => { stop(); message('Передача остановлена. Новые координаты не запрашиваются.'); });
  $('engineer-select').addEventListener('change', () => {
    if (wanted) stop(); engineerId = null; latest = null;
    $('gps-fix').textContent = 'Ещё нет'; $('gps-sent').textContent = 'Ещё нет';
    message('Для нового профиля включите GPS отдельно.'); render();
  }, true);
  window.addEventListener('engineer-state', event => { engineerId = event.detail.engineerId; render(); });
  document.addEventListener('visibilitychange', () => {
    if (!wanted) return;
    generation++; clearWatch();
    if (document.hidden) { state = 'paused'; beacon(state); render(); }
    else { lastSent = 0; nextTry = 0; startWatch(); tick(); }
  });
  window.addEventListener('pagehide', () => { if (wanted) { clearWatch(); beacon('paused'); } });
  window.addEventListener('pageshow', event => { if (event.persisted && wanted) startWatch(); });
  window.addEventListener('online', () => { nextTry = 0; tick(); });
  setInterval(tick, 2000);
  if (!supported) {
    $('gps-explanation').textContent = 'Для GPS на телефоне откройте приложение по доверенному HTTPS. Обычный http://IP позволяет работать с заданиями, но браузер не разрешает геолокацию.';
    message('На компьютере можно проверить GPS через http://localhost. Демонстрационные GPS-ситуации доступны диспетчеру по HTTP.');
  }
  render();
})();
