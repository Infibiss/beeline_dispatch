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
  let wakeLock = null, nativePaired = false, nativeActive = false, nativeStage = 'unpaired';
  let nativeLoading = false, nativeEpoch = 0;
  const embedded = !!window.FieldRouterNative || /FieldRouterAndroid\/1/.test(navigator.userAgent);
  const supported = window.isSecureContext && !!navigator.geolocation;
  const options = {enableHighAccuracy: true, maximumAge: 0, timeout: 30000};
  const labels = {off: 'Выключена', active: 'Включена', unavailable: 'Ищем координату', paused: 'На паузе', denied: 'Нет разрешения'};
  const message = text => { $('gps-feedback').textContent = text; };
  function render() {
    if(embedded)return;
    const pending = {awaiting:'Ожидаем телефон',low_accuracy:'Уточняем координату',waiting_fix:'Ожидаем точную точку',stale:'Нет свежей точки',off:'Фоновая передача остановлена'};
    $('gps-state').textContent = nativeActive ? 'Передача в фоне'
      : wanted ? labels[state]+' · браузер' : nativePaired ? (pending[nativeStage] || 'Ожидаем телефон') : labels[state];
    $('gps-start').hidden = wanted || nativeActive;
    $('gps-stop').hidden = !wanted && !nativeActive;
    $('gps-start').disabled = !supported || !engineerId || wanted || nativeActive;
    $('gps-stop').disabled = !wanted && !nativePaired;
  }
  function packet(forState, point = null) {
    sequence++;
    try { sessionStorage.setItem('gps-session:'+location.pathname+':'+engineerId, JSON.stringify({trackerId,sequence})); } catch (_) {}
    return {engineer_id: engineerId, tracker_id: trackerId, sequence, state: forState, point};
  }
  async function keepAwake() {
    if (!wanted || document.hidden || !$('gps-keep-awake')?.checked || !navigator.wakeLock || wakeLock) return;
    try { wakeLock = await navigator.wakeLock.request('screen'); wakeLock.addEventListener('release', () => {wakeLock = null;}); }
    catch (_) { message('Не удалось удержать экран включённым. Для передачи в фоне откройте Android-приложение.'); }
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
  function stop(forState = 'off', notifyServer = true) {
    wanted = false; generation++; clearWatch(); wakeLock?.release().catch(() => {}); state = forState;
    const pending = notifyServer ? beacon(forState) : undefined; render(); return pending;
  }
  function fix(position, token) {
    if (!wanted || token !== generation) return;
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
    $('gps-fix').textContent = new Date(position.timestamp).toLocaleTimeString('ru-RU', {hourCycle:'h23'}) + ' · точность ±' + Math.round(c.accuracy) + ' м';
    if (c.accuracy > 100) message('Погрешность ±'+Math.round(c.accuracy)+' м. Разрешите точное местоположение и выйдите на открытое место.');
    render(); tick();
  }
  function gpsError(error, token) {
    if (!wanted || token !== generation) return;
    if (error.code === 1) {
      stop('denied'); message('Разрешение отклонено. Разрешите геолокацию в настройках сайта и включите GPS снова.');
    } else {
      state = 'unavailable'; render();
      message('Ищем координату. Проверьте геолокацию телефона.');
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
    if (now - probeAt >= (latest?.accuracy_m > 100 ? 15000 : 60000) && (!latest || latest.accuracy_m > 100 || now - latest.measured_at_ms >= 45000)) {
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
        if (response.status === 409) {
          stop('off', false); await nativeStatus();
          message(nativeActive ? 'GPS передаёт Android-приложение.' : result.error);
          return;
        }
        throw new Error(result.error || 'Сервер GPS недоступен');
      }
      lastSent = Date.now(); failures = 0; nextTry = 0;
      if (point) sentTimestamp = point.measured_at_ms;
      $('gps-sent').textContent = new Date(lastSent).toLocaleTimeString('ru-RU', {hourCycle:'h23'}) + (point ? ' · координата' : ' · сигнал связи');
      message(result.reason === 'low_accuracy' || result.reason === 'jump'
        ? (result.reason === 'low_accuracy' ? 'Низкая точность. Разрешите точное местоположение.' : 'Уточняем местоположение. Ждём следующую точку.')
        : '');
    } catch (error) {
      if (token !== generation) return;
      nextTry = Date.now() + retryFor(++failures) + Math.random() * 1000;
      message('Нет связи с сервером. Повторим отправку автоматически.');
    } finally { clearTimeout(timer); inFlight = false; }
  }
  $('gps-start').addEventListener('click', () => {
    if (embedded || !supported || !engineerId || wanted || nativeActive) return;
    trackerId = crypto.randomUUID?.() || 'tracker-' + Date.now() + '-' + Math.random().toString(36).slice(2);
    sequence = 0; latest = null; sentTimestamp = 0; lastSent = 0; nextTry = 0; failures = 0;
    try { const previous=JSON.parse(sessionStorage.getItem('gps-session:'+location.pathname+':'+engineerId)); if(previous?.trackerId && Number.isSafeInteger(previous.sequence)) {trackerId=previous.trackerId;sequence=previous.sequence;} } catch (_) {}
    wanted = true; generation++; message('Разрешите точное местоположение. Ищем координату…'); startWatch(); keepAwake();
  });
  $('gps-keep-awake')?.addEventListener('change', () => {if ($('gps-keep-awake').checked) keepAwake(); else wakeLock?.release().catch(() => {});});
  window.addEventListener('account-logout', event => {if(wanted) event.detail.tasks.push(stop());});
  $('gps-stop').addEventListener('click', async () => {
    try {
      if (wanted) await stop();
      if (nativePaired) await disconnectNative();
      message('Передача остановлена.');
    } catch(error) {message('Не удалось отключить фоновую передачу: '+error.message);}
  });
  $('engineer-select')?.addEventListener('change', () => {
    if (wanted) stop(); engineerId = null; latest = null; nativePaired = false; nativeActive = false; nativeStage = 'unpaired';
    $('gps-fix').textContent = 'Ещё нет'; $('gps-sent').textContent = 'Ещё нет';
    message('Для нового профиля включите GPS отдельно.'); render();
  }, true);
  window.addEventListener('engineer-state', event => { const changed=engineerId!==event.detail.engineerId; engineerId = event.detail.engineerId; render(); if(changed) nativeStatus(); });
  document.addEventListener('visibilitychange', () => {
    if (!wanted) return;
    if (document.hidden) { state = 'paused'; beacon(state); render(); }
    else { generation++; clearWatch(); if(latest && Date.now()-latest.measured_at_ms>45000) latest=null; lastSent = 0; nextTry = 0; startWatch(); tick(); keepAwake(); }
  });
  window.addEventListener('pagehide', () => { if (wanted) { clearWatch(); beacon('paused'); } });
  window.addEventListener('pageshow', event => { if (event.persisted && wanted) startWatch(); });
  window.addEventListener('online', () => { nextTry = 0; tick(); });
  async function nativeRequest(payload) {
    const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),8000);
    try {
      const response=await appFetch(appUrl('/api/gps/mobile'), {signal:controller.signal,
        ...(payload ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)} : {})});
      const result=await response.json(); if(!response.ok) throw new Error(result.error || 'Подключение недоступно'); return result;
    } finally {clearTimeout(timer);}
  }
  async function nativeStatus() {
    if (embedded || !engineerId || document.hidden || nativeLoading) return;
    nativeLoading=true;
    const currentEngineer=engineerId, epoch=nativeEpoch;
    try {
      const result=await nativeRequest();
      if(currentEngineer!==engineerId || epoch!==nativeEpoch)return;
      nativePaired=!!result.paired;
      const connection=result.connection || {state:'awaiting'};
      nativeStage=connection.state;
      nativeActive=nativeStage==='active' && nativePaired && result.enabled;
      // The native lease remains authoritative even without native setup controls.
      if(nativeActive && wanted)stop('off',false);
      if(nativeActive) {
        message('GPS передаёт Android-приложение.');
        const age=connection.point_age_seconds, accuracy=connection.accuracy_m;
        $('gps-fix').textContent=(age==null?'Получена':age+' сек назад')+
          (Number.isFinite(accuracy)?' · точность ±'+Math.round(accuracy)+' м':'');
        $('gps-sent').textContent='Получено сервером';
      } else if(nativePaired && !wanted) {
        const descriptions={awaiting:'Откройте приложение и включите GPS.',
          low_accuracy:'Низкая точность. Разрешите точное местоположение.',
          waiting_fix:'Ждём свежую координату.',stale:'Нет свежей координаты. Проверьте GPS и связь телефона.',
          off:'Передача остановлена.'};
        message(descriptions[nativeStage] || 'Проверьте GPS в приложении.');
      }
      if(!result.enabled)message('Приём GPS отключён. Сообщите диспетчеру.');
      else if(result.storage_error)message('GPS временно недоступен. Сообщите диспетчеру.');
      render();
    } catch (_) {
      if(nativePaired && !wanted)message('Не удалось проверить передачу. Проверьте связь.');
    } finally {nativeLoading=false;}
  }
  async function disconnectNative() {
    await nativeRequest({action:'revoke'});
    nativeEpoch++;
    nativePaired=false;nativeActive=false;nativeStage='unpaired';
    render();
  }
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)nativeStatus();});
  setInterval(nativeStatus,5000);
  setInterval(tick, 2000);
  if (!supported) {
    $('gps-explanation').textContent = 'Для геолокации откройте кабинет по HTTPS.';
    message('Геолокация недоступна в этом браузере.');
  }
  render();
})();
