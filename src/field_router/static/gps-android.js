/* Embedded Android GPS. Web-only tracking remains in gps-engineer.js. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let ready=false, busy=false, resumeStart=false, account=null, engineerId=null, last={}, serverAt=0, localError=null;
  let serverConnection=null, serverConnectionAt=0;
  function call(action,data={}) {
    const result=window.FieldRouterNative.call(action,data);
    if(result.error)throw Error(result.error);
    return result;
  }
  const feedback=text=>{$('gps-feedback').textContent=text;};
  const clock=ms=>new Date(ms).toLocaleTimeString('ru-RU',{hourCycle:'h23'});
  async function api(action) {
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),8000);
    try {
    const response=await window.FieldRouter.fetch(window.FieldRouter.url('/api/gps/mobile'), {
      ...(action?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})}:{}),
      signal:controller.signal
    });
    const data=await response.json();if(!response.ok)throw Error(data.error||'Сервер GPS недоступен');return data;
    }finally{clearTimeout(timer);}
  }
  function render(status) {
    last=status;
    const now=Date.now();
    const fixStale=status.enabled&&status.measuredAt&&now-status.measuredAt>60000;
    const sendStale=status.enabled&&status.queued>0&&now-(status.sentAt||status.startedAt||now)>60000;
    $('gps-state').textContent=!status.enabled?'Выключена':!status.serviceRunning?'GPS-служба не подтверждена':
      fixStale?'Нет свежей координаты':sendStale?'Точки ждут отправки':'GPS-служба включена';
    $('gps-start').textContent='Включить GPS на время смены';
    $('gps-start').disabled=busy||!account||account.role!=='engineer'||!engineerId||!!status.enabled;
    $('gps-stop').disabled=busy||!status.enabled;
    $('gps-fix').textContent=status.measuredAt?clock(status.measuredAt)+' · точность ±'+Math.round(status.accuracy)+' м':'Ждём координату';
    $('gps-sent').textContent=status.sentAt?clock(status.sentAt)+' · получен ответ сервера':'Ещё нет подтверждения';
    if(status.serverMeasuredAt) $('gps-sent').textContent+=' · измерение '+clock(status.serverMeasuredAt)+
      (status.serverAccepted?' принято для расчёта':' не используется для прогноза');
    const usingPosition=serverConnection?.state==='active' && now-serverConnectionAt<20000;
    const reasons={jump:'Уточняем местоположение. Неточная точка не переместит инженера на карте.',
      source_conflict:'Уточняем местоположение. Телефон прислал разные позиции.',
      low_accuracy:'Пока не хватает точности. Дождитесь обновления.',
      mock:'Отключите тестовое местоположение на телефоне.',historical_only:'Предыдущие измерения сохранены в истории.'};
    $('gps-android-verdict').textContent=usingPosition?'Местоположение обновляется.':
      status.serverMeasuredAt?(status.serverAccepted?'Координата принята.':reasons[status.serverReason]||'Уточняем местоположение.'):'Ждём первую координату.';
    if(usingPosition && serverConnection.position_measured_at_ms) {
      $('gps-fix').textContent=clock(serverConnection.position_measured_at_ms)+' · точность ±'+Math.round(serverConnection.position_accuracy_m)+' м · на карте';
      $('gps-sent').textContent=status.sentAt?clock(status.sentAt)+' · данные доставлены':'Ожидаем передачу';
    }
    const quality=[];
    if(status.permission===false)quality.push('Доступ к местоположению не разрешён. Откройте «Разрешения телефона».');
    else if(status.finePermission===false)quality.push('Разрешено только приблизительное местоположение. Нажмите «Разрешения телефона» и включите точное.');
    else if(status.finePermission===true)quality.push('Точное местоположение разрешено.');

    if(status.measuredAt)quality.push('Измерение '+Math.max(0,Math.floor((Date.now()-status.measuredAt)/1000))+' сек назад.');

    $('gps-android-quality').textContent=quality.join(' ');
    const background=[];
    if(status.batteryExempt===false)background.push('Android может задерживать GPS и сеть при выключенном экране. Нажмите «Фоновая работа» и разрешите работу без ограничений.');
    if(status.batteryExempt===true)background.push('Исключение из оптимизации батареи включено.');
    if(status.backgroundRestricted)background.push('Android ограничивает фоновую работу приложения. Откройте «Разрешения телефона» → «Батарея» и снимите ограничение.');
    if(status.powerSaveMode)background.push('На телефоне включено энергосбережение; оно может ограничивать GPS.');
    if(status.notificationsEnabled===false)background.push('Уведомления выключены. Разрешите их, чтобы видеть передачу и останавливать её из шторки.');
    if(status.networkConnected===false)background.push('Нет подключения к сети. Полученные точки остаются в очереди.');
    if(status.networkType==='mobile')background.push('Сейчас используется мобильная сеть. Локальный сервер компьютера обычно доступен только через общий Wi-Fi.');
    if(fixStale)background.push('Измерение устарело. Проверьте геолокацию и ограничения батареи; включённая служба сама по себе не подтверждает свежий GPS.');
    if(sendStale)background.push('Точки накопились без подтверждения сервера. Проверьте связь с компьютером и фоновую работу.');
    $('gps-android-background').textContent=background.join(' ');
    $('gps-android-background-settings').disabled=typeof status.batteryExempt!=='boolean';
    const diagnostics=[];
    if(status.appVersion)diagnostics.push('Приложение '+status.appVersion+'.');
    if(status.provider)diagnostics.push('Источник последнего измерения: '+({gps:'спутники',network:'Wi-Fi/сотовая сеть'}[status.provider]||status.provider)+'.');
    if(status.gpsEnabled===false)diagnostics.push('Спутниковая геолокация выключена или недоступна.');
    if(status.serverDetail)diagnostics.push('Ответ сервера на последнее измерение: '+status.serverDetail);
    diagnostics.push(status.heartbeatAt?'Сигнал связи без координаты: '+clock(status.heartbeatAt)+'. Он не обновляет возраст GPS.':'');
    diagnostics.push(status.attemptAt?'Последняя попытка отправки: '+clock(status.attemptAt)+'.':'Попыток отправки пока нет.');
    diagnostics.push(status.backgroundFixAt?'Координата получена с выключенным экраном: '+clock(status.backgroundFixAt)+'.':'Получение координат с выключенным экраном пока не зафиксировано.');
    diagnostics.push(status.backgroundSentAt?'Ответ сервера с выключенным экраном: '+clock(status.backgroundSentAt)+'.':'Ответов сервера с выключенным экраном пока нет.');
    $('gps-android-diagnostics').textContent=diagnostics.join(' ');
    $('gps-android-queue').textContent='Ожидают отправки: '+(status.queued||0)
      +(status.pendingStop?' · GPS остановлен на телефоне; остановка на сервере ожидает связи.':'');
    $('gps-end-shift').disabled=busy||!account||account.role!=='engineer'||!engineerId;
    if(!busy)feedback(localError||(usingPosition?'Диспетчер видит ваше местоположение.':reasons[status.serverReason])||status.message||'Передача начинается после вашего разрешения. Можно свернуть приложение.');
    if(account && account.role!=='engineer')feedback('Войдите под учётной записью инженера, чтобы передавать его координаты.');
    if(status.enabled && Date.now()-serverAt>15000) {
      serverAt=Date.now();
      api().then(data=>{
        if(!last.enabled)return;
        serverConnection=data.connection||null;serverConnectionAt=Date.now();
        const descriptions={active:'Сервер получает свежие координаты.',awaiting:'Сервер пока не получил координату.',
          low_accuracy:'Точка получена, но точность недостаточна для выводов.',
          stale:'Свежих координат на сервере нет.',waiting_fix:'Ждём свежую точную координату.',off:'Передача на сервере остановлена.'};
        $('gps-android-server').textContent=(descriptions[data.connection?.state]||'Подключение проверяется.')+
          (data.connection?.detail?' '+data.connection.detail:'');
        render(last);
      }).catch(()=>{$('gps-android-server').textContent='Сервер недоступен. Сбор координат на телефоне работает независимо.';});
    }else if(!status.enabled)$('gps-android-server').textContent='';
  }
  async function start() {
    if(!ready||busy||!engineerId||account?.role!=='engineer')return;
    if(call('status').enabled)return;
    localError=null;
    try {
      const permission=call('permission');
      if(permission.requested||!permission.granted) {
        resumeStart=true;localError='Разрешите точное местоположение. GPS включится после подтверждения.';feedback(localError);return;
      }
      busy=true;render(last);feedback('Подключаем GPS к вашему рабочему профилю…');
      const access=await api('issue');
      // The key never enters a URL, localStorage, logging or visible employee fields.
      const status=call('start',{token:access.device_identifier,expires:Math.round(access.expires_at*1000),
        engineer:engineerId,account:account.username+'@'+account.region});
      render(status);
    }catch(error){localError=error.message;}
    finally{busy=false;render(call('status'));}
  }
  function stop() {
    resumeStart=false;localError=null;serverConnection=null;serverConnectionAt=0;
    try{render(call('stop'));feedback('GPS остановлен. Неотправленные координаты удалены с телефона.');}
    catch(error){feedback(error.message);}
  }
  async function init() {
    if(ready||!window.FieldRouterNative||!$('gps-start'))return;ready=true;
    $('gps-native-panel').hidden=true;
    $('gps-keep-awake').closest('label').hidden=true;
    $('gps-explanation').textContent='GPS-служба предназначена для передачи во время смены, в том числе с выключенным экраном. Проверьте фоновую работу ниже. Без связи с сервером точки временно сохраняются на телефоне. Сервер хранит историю до 7 дней. Кнопка «Выключить» останавливает сбор и удаляет неотправленные точки.';
    $('gps-native-panel').previousElementSibling.textContent='± метры — погрешность измерения. Разрешите точное местоположение. В помещении точность может быть хуже.';
    const box=document.createElement('div');
    box.innerHTML='<p id="gps-android-verdict" class="gps-message" role="status"></p><p id="gps-android-quality" class="gps-message" role="status"></p><p id="gps-android-background" class="gps-message" role="status"></p><p id="gps-android-queue" class="muted"></p><details><summary>Проверка фоновой передачи</summary><p id="gps-android-diagnostics" class="muted"></p></details><p id="gps-android-server" class="gps-message"></p><div class="button-row"><button id="gps-android-settings" type="button" class="secondary">Разрешения телефона</button><button id="gps-android-background-settings" type="button" class="secondary">Фоновая работа</button><button id="gps-end-shift" type="button" class="secondary">Завершить смену</button></div><p class="muted">Android показывает уведомление о GPS. При завершении смены сбор останавливается; отметку недоступности подтвердите в форме. Непрерывный GPS расходует батарею: выключайте его в конце смены. Один запуск передачи действует не более 12 часов.</p>';
    $('gps-native-panel').before(box);
    document.querySelector('#panel-shift .demo-note').textContent='Рабочие отметки относятся к дате плана. GPS использует фактическое время телефона.';
    $('gps-start').addEventListener('click',event=>{event.stopImmediatePropagation();start();},true);
    $('gps-stop').addEventListener('click',event=>{event.stopImmediatePropagation();stop();},true);
    $('gps-android-settings').onclick=()=>call('settings');
    $('gps-android-background-settings').onclick=()=>{
      try{call('background-settings');}catch(error){feedback(error.message);}
    };
    $('gps-end-shift').onclick=()=>{stop();window.dispatchEvent(new Event('engineer-end-shift'));};
    window.addEventListener('account-logout',()=>stop());
    try {
      account=await window.FieldRouter.ready;
      engineerId=engineerId||$('engineer-select').value;
      if(account.role==='engineer' && engineerId)render(call('bind',{engineer:engineerId,account:account.username+'@'+account.region}));
      else if(account.role==='engineer')render(call('status'));
      else{call('stop');render(call('status'));}
    }catch(error){feedback(error.message);}
  }
  window.addEventListener('field-router-native-ready',init);
  window.addEventListener('field-router-native-status',event=>{if(ready)render(event.detail);});
  window.addEventListener('engineer-state',event=>{
    if(engineerId!==event.detail.engineerId){serverConnection=null;serverConnectionAt=0;}
    engineerId=event.detail.engineerId;
    if(ready && account?.role==='engineer')render(call('bind',{engineer:engineerId,account:account.username+'@'+account.region}));
  });
  window.addEventListener('engineer-action',event=>{
    if(ready && event.detail.status==='available')start();
  });
  window.addEventListener('field-router-native-permission',()=>{
    if(!resumeStart)return;resumeStart=false;
    if(call('status').permission)start();else {localError='GPS не включён: нет разрешения. Его можно выдать в настройках телефона.';feedback(localError);}
  });
  if(window.FieldRouterNative)init();
})();
