/* Optional tracker: latest fix only, bounded retry, no coupling to task requests. */
(() => {
  'use strict';
  const intervalFor = speed => Number.isFinite(speed) && speed > 1 ? 10000 : 30000;
  const retryFor = failures => Math.min(60000, 2000 * 2 ** Math.min(failures, 5));
  function nativeConfigUrl(origin, token, android) {
    const params = new URLSearchParams({url: new URL('/api/gps/native', origin).href, id: token,
      accuracy:'highest', interval:'15', distance:'0', heartbeat:'30', buffer:'true'});
    // Official Traccar Client configuration scheme, Android intent fallback.
    return android ? 'intent://config?'+params+'#Intent;scheme=org.traccar.client;package=org.traccar.client;end'
      : 'org.traccar.client://config?'+params;
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {intervalFor, retryFor, nativeConfigUrl};
  if (typeof document === 'undefined') return;
  const $ = id => document.getElementById(id);
  const appFetch = (...args) => window.FieldRouter ? window.FieldRouter.fetch(...args) : fetch(...args);
  const appUrl = path => window.FieldRouter ? window.FieldRouter.url(path) : path;
  if (!$('gps-start')) return;
  let engineerId = null, trackerId = null, sequence = 0, wanted = false, state = 'off';
  let watchId = null, latest = null, speed = null, sentTimestamp = 0, lastSent = 0;
  let nextTry = 0, failures = 0, inFlight = false, probeAt = 0, generation = 0;
  let wakeLock = null, nativePaired = false, nativeActive = false, nativeStage = 'unpaired';
  let nativeLoading = false, nativeWorking = false;
  const embedded = /FieldRouterAndroid\/1/.test(navigator.userAgent);
  const supported = window.isSecureContext && !!navigator.geolocation;
  const options = {enableHighAccuracy: true, maximumAge: 0, timeout: 30000};
  const labels = {off: 'Выключена', active: 'Включена', unavailable: 'Ищем координату', paused: 'На паузе', denied: 'Нет разрешения'};
  const message = text => { $('gps-feedback').textContent = text; };
  function render() {
    if(embedded)return;
    const pending = {awaiting:'Ожидаем телефон',low_accuracy:'Уточняем координату',waiting_fix:'Ожидаем точную точку',stale:'Нет свежей точки',off:'Фоновая передача остановлена'};
    $('gps-state').textContent = nativeActive ? 'Передача в фоне'
      : wanted ? labels[state]+' · браузер' : nativePaired ? (pending[nativeStage] || 'Ожидаем телефон') : labels[state];
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
    catch (_) { message('Удержание экрана недоступно. Для фоновой передачи используйте подключение телефона ниже.'); }
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
    if (c.accuracy > 100) message('Погрешность ±'+Math.round(c.accuracy)+' м: ждём более точную точку. Разрешите точное местоположение, включите GPS и выйдите на открытое место. Фильтр не может восстановить точный адрес из такой погрешности.');
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
          message(nativeActive ? 'Фоновое приложение передаёт координаты. Браузерный трекер остановлен.' : result.error);
          return;
        }
        throw new Error(result.error || 'Сервер GPS недоступен');
      }
      lastSent = Date.now(); failures = 0; nextTry = 0;
      if (point) sentTimestamp = point.measured_at_ms;
      $('gps-sent').textContent = new Date(lastSent).toLocaleTimeString('ru-RU', {hourCycle:'h23'}) + (point ? ' · координата' : ' · сигнал связи');
      message(result.reason === 'low_accuracy' || result.reason === 'jump'
        ? 'Точка получена, но не используется для выводов. '+(result.reason === 'low_accuracy' ? 'Погрешность слишком велика: разрешите точное местоположение и дождитесь GPS на открытом месте.' : 'Обнаружен неправдоподобный скачок; ждём следующего измерения.')
        : 'Связь работает. Координаты передаются каждые 10–30 секунд при наличии нового измерения.');
    } catch (error) {
      if (token !== generation) return;
      nextTry = Date.now() + retryFor(++failures) + Math.random() * 1000;
      message('Не удалось передать GPS. Повторим автоматически; задания и отправка статусов работают независимо.');
    } finally { clearTimeout(timer); inFlight = false; }
  }
  $('gps-start').addEventListener('click', () => {
    if (embedded || !supported || !engineerId || wanted || nativeActive) return;
    trackerId = crypto.randomUUID?.() || 'tracker-' + Date.now() + '-' + Math.random().toString(36).slice(2);
    sequence = 0; latest = null; sentTimestamp = 0; lastSent = 0; nextTry = 0; failures = 0;
    try { const previous=JSON.parse(sessionStorage.getItem('gps-session:'+location.pathname+':'+engineerId)); if(previous?.trackerId && Number.isSafeInteger(previous.sequence)) {trackerId=previous.trackerId;sequence=previous.sequence;} } catch (_) {}
    wanted = true; generation++; message('Разрешите браузеру определять точное местоположение. Первое точное измерение может занять 30–60 секунд.'); startWatch(); keepAwake();
  });
  $('gps-keep-awake')?.addEventListener('change', () => {if ($('gps-keep-awake').checked) keepAwake(); else wakeLock?.release().catch(() => {});});
  window.addEventListener('account-logout', event => {if(wanted) event.detail.tasks.push(stop());});
  $('gps-stop').addEventListener('click', async () => {
    try {
      if (wanted) await stop();
      if (nativePaired) await disconnectNative();
      message('Передача остановлена. Если включали Traccar Client, остановите передачу и в нём.');
    } catch(error) {message('Не удалось отключить фоновую передачу: '+error.message);}
  });
  $('engineer-select').addEventListener('change', () => {
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
  function clearNativeCredentials() {
    $('gps-native-id').value=''; $('gps-native-credentials').hidden=true;
    $('gps-native-manual').open=false; $('gps-native-open').removeAttribute('href');
    $('gps-native-reveal').textContent='Показать ключ';
  }
  async function nativeStatus() {
    if (embedded || !$('gps-native-status') || document.hidden || nativeLoading) return;
    nativeLoading=true;
    const currentEngineer=engineerId;
    try {
      const result=await nativeRequest();
      if(currentEngineer!==engineerId)return;
      nativePaired=result.paired;
      const connection=result.connection || {state:'awaiting'};
      nativeStage=connection.state; nativeActive=nativeStage==='active' && nativePaired && result.enabled;
      if(nativeActive && wanted)stop('off',false);
      $('gps-native-connect').disabled=nativeWorking || nativeActive || !result.enabled || !result.secure || !result.persistent || !!result.storage_error;
      $('gps-native-connect').textContent=nativePaired ? 'Подготовить новое подключение' : 'Подготовить подключение';
      $('gps-native-revoke').disabled=nativeWorking || !nativePaired;
      const descriptions={
        unpaired:'Фоновая передача не настроена. Установите приложение и подготовьте подключение.',
        awaiting:'Подключение подготовлено. Фоновое приложение ещё не прислало координату. Откройте его по кнопке ниже и включите передачу.',
        low_accuracy:'Телефон связался с сервером, но координата пока неточная. Разрешите точное местоположение и выйдите на открытое место.',
        waiting_fix:'Связь с приложением есть. Ждём свежую точную координату для фоновой передачи.',
        active:'Фоновая передача работает: сервер получил координату с этого телефона.',
        stale:'Свежих координат из фонового приложения нет. Проверьте интернет, разрешения и включённую передачу.',
        off:'Фоновая передача остановлена. Включите её в приложении или используйте GPS в браузере.'
      };
      let text=descriptions[nativeStage] || descriptions.unpaired;
      if(nativePaired && connection.point_age_seconds!=null)
        text+=' Последнее измерение '+connection.point_age_seconds+' сек назад, погрешность ±'+Math.round(connection.accuracy_m)+' м.';
      if(nativePaired)text+=' Доступ до '+new Date(result.expires_at*1000).toLocaleTimeString('ru-RU',{hourCycle:'h23'})+'.';
      if(!nativeActive && wanted)text+=' Пока координаты передаёт браузер.';
      if(!result.enabled)text='Приём GPS на сервере отключён. Сообщите диспетчеру.';
      else if(!result.secure)text='Для подключения телефона откройте кабинет по доверенному HTTPS.';
      else if(!result.persistent || result.storage_error)text='Хранилище GPS недоступно. Сообщите диспетчеру; задания остаются доступны.';
      $('gps-native-status').textContent=text; $('gps-native-status').dataset.state=nativeStage;
      if(nativeActive) {
        message('Координаты поступают из фонового приложения. Браузер можно закрыть; передачу останавливает кнопка «Выключить».');
        $('gps-fix').textContent=connection.point_age_seconds+' сек назад · точность ±'+Math.round(connection.accuracy_m)+' м';
        $('gps-sent').textContent='Получено сервером · фоновое приложение';
      }
      render();
    } catch(error) {
      $('gps-native-status').textContent='Не удалось проверить фоновую передачу. '+error.message;
      $('gps-native-connect').disabled=true;
    } finally {nativeLoading=false;}
  }
  async function disconnectNative() {
    await nativeRequest({action:'revoke'});
    nativePaired=false;nativeActive=false;nativeStage='unpaired';
    clearNativeCredentials();render();await nativeStatus();
  }
  $('gps-native-connect')?.addEventListener('click', async () => {
    if(nativeWorking)return;
    nativeWorking=true;$('gps-native-connect').disabled=true;
    try {
      const result=await nativeRequest({action:'issue'});
      // Issuing a key is not a GPS fix. Keep browser tracking until native reception.
      nativePaired=true;nativeStage='awaiting';
      $('gps-native-url').value=location.origin+'/api/gps/native';$('gps-native-id').value=result.device_identifier;
      $('gps-native-open').href=nativeConfigUrl(location.origin,result.device_identifier,/Android/i.test(navigator.userAgent));
      $('gps-native-id').type='password';$('gps-native-credentials').hidden=false;$('gps-native-manual').open=false;
      render();
    } catch(error) {$('gps-native-status').textContent=error.message;}
    finally {nativeWorking=false;await nativeStatus();}
  });
  $('gps-native-open')?.addEventListener('click',()=>{
    $('gps-native-status').textContent='В Traccar Client разрешите геолокацию и включите передачу. Получение данных проверяется автоматически после возвращения.';
  });
  $('gps-native-revoke')?.addEventListener('click', async () => {
    if(nativeWorking)return;nativeWorking=true;
    try {await disconnectNative();message('Фоновая передача отключена на сервере. Остановите также передачу в Traccar Client.');}
    catch(error) {$('gps-native-status').textContent=error.message;}
    finally {nativeWorking=false;await nativeStatus();}
  });
  $('gps-native-refresh')?.addEventListener('click',nativeStatus);
  $('gps-native-reveal')?.addEventListener('click', () => {$('gps-native-id').type=$('gps-native-id').type==='password'?'text':'password'; $('gps-native-reveal').textContent=$('gps-native-id').type==='password'?'Показать ключ':'Скрыть ключ';});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)nativeStatus();});
  setInterval(() => {if(nativePaired || $('gps-native-panel')?.open)nativeStatus();},5000);
  setInterval(tick, 2000);
  if (!supported) {
    $('gps-explanation').textContent = 'Для GPS на телефоне откройте приложение по доверенному HTTPS. Обычный http://IP позволяет работать с заданиями, но браузер не разрешает геолокацию.';
    message('На компьютере можно проверить GPS через http://localhost. Демонстрационные GPS-ситуации доступны диспетчеру по HTTP.');
  }
  render();
})();
