/* Embedded Android GPS. The service keeps running independently of this view. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let ready=false, busy=false, resumeStart=false, account=null, engineerId=null, last={}, serverAt=0, localError=null;
  let serverConnection=null, serverConnectionAt=0, serverError=false, connectionEpoch=0;
  function call(action,data={}) {
    const result=window.FieldRouterNative.call(action,data);
    if(result.error)throw Error(result.error);
    return result;
  }
  const feedback=text=>{$('gps-feedback').textContent=text;};
  const clock=ms=>new Date(ms).toLocaleTimeString('ru-RU',{hourCycle:'h23'});
  const diagnosticFields=['enabled','permission','finePermission','serviceRunning','batteryExempt',
    'backgroundRestricted','powerSaveMode','notificationsEnabled','networkConnected','networkType',
    'measuredAt','accuracy','sentAt','serverMeasuredAt','serverAccepted','serverReason','serverDetail',
    'provider','gpsEnabled','appVersion','queued','pendingStop','heartbeatAt','attemptAt',
    'backgroundFixAt','backgroundSentAt','startedAt'];
  // Inspect from developer tools when needed; never expose keys or raw coordinates.
  window.FieldRouterGpsDiagnostics=Object.freeze({snapshot:()=>Object.fromEntries(
    diagnosticFields.filter(key=>Object.hasOwn(last,key)).map(key=>[key,last[key]]))});
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
    last={...status};
    const now=Date.now();
    const fixStale=status.enabled&&status.measuredAt&&now-status.measuredAt>60000;
    const sendStale=status.enabled&&status.queued>0&&now-(status.sentAt||status.startedAt||now)>60000;
    const usingPosition=status.enabled&&serverConnection?.state==='active'&&now-serverConnectionAt<20000;
    $('gps-state').textContent=!status.enabled?'Выключена':!status.serviceRunning?'GPS запускается':
      fixStale?'Нет свежей координаты':sendStale?'Точки ждут отправки':'Включена';
    $('gps-start').textContent='Включить';
    $('gps-start').hidden=!!status.enabled;
    $('gps-stop').hidden=!status.enabled;
    $('gps-start').disabled=busy||account?.role!=='engineer'||!engineerId||!!status.enabled;
    $('gps-stop').disabled=busy||!status.enabled;
    $('gps-fix').textContent=status.measuredAt?clock(status.measuredAt)+' · ±'+Math.round(status.accuracy)+' м':'Ещё нет';
    $('gps-sent').textContent=status.sentAt?clock(status.sentAt):'Ещё нет';
    if(usingPosition && serverConnection.position_measured_at_ms) {
      $('gps-fix').textContent=clock(serverConnection.position_measured_at_ms)+
        ' · ±'+Math.round(serverConnection.position_accuracy_m)+' м';
    }
    const reasons={jump:'Уточняем местоположение. Ждём следующую точку.',
      source_conflict:'Уточняем местоположение. Ждём следующую точку.',
      low_accuracy:'Низкая точность. Дождитесь обновления на открытом месте.',
      mock:'Отключите тестовое местоположение на телефоне.'};
    const action=$('gps-android-settings');
    action.hidden=true;
    let instruction='';
    if(status.permission===false || status.finePermission===false) {
      instruction=status.permission===false?'Разрешите геолокацию в настройках приложения.':'Включите точное местоположение в разрешениях приложения.';
      action.hidden=false;action.textContent='Разрешения геолокации';action.dataset.action='settings';
    } else if(status.enabled && status.gpsEnabled===false) {
      instruction='Включите геолокацию в настройках телефона.';
    } else if(status.enabled && (status.backgroundRestricted || status.batteryExempt===false)) {
      instruction='Разрешите фоновую работу без ограничений батареи.';
      action.hidden=false;action.textContent='Настроить фоновую работу';action.dataset.action='background-settings';
    } else if(status.enabled && status.networkConnected===false) {
      instruction='Нет интернета. Точки будут отправлены после подключения.';
    } else if(sendStale || status.enabled && serverError) {
      instruction='Нет связи с сервером. Проверьте подключение.';
    } else if(fixStale) {
      instruction='Нет свежей координаты. Проверьте GPS телефона.';
    } else if(status.enabled && status.powerSaveMode) {
      instruction='Отключите энергосбережение для передачи в фоне.';
    } else if(status.enabled && !usingPosition) {
      instruction=reasons[status.serverReason]||(!status.measuredAt?'Ищем координату…':'');
    } else if(status.pendingStop) {
      instruction='GPS остановлен. Ожидаем связи для подтверждения сервером.';
    }
    if(!busy)feedback(localError||instruction);
    if(account && account.role!=='engineer')feedback('Для передачи GPS войдите как инженер.');
    if(status.enabled && now-serverAt>15000) {
      serverAt=now;
      const epoch=connectionEpoch;
      api().then(data=>{
        if(!last.enabled || epoch!==connectionEpoch)return;
        serverConnection=data.connection||null;serverConnectionAt=Date.now();serverError=false;
        render(last);
      }).catch(()=>{if(last.enabled && epoch===connectionEpoch){serverError=true;render(last);}});
    }
  }
  async function start() {
    if(!ready||busy||!engineerId||account?.role!=='engineer')return;
    if(call('status').enabled)return;
    localError=null;
    try {
      const permission=call('permission');
      if(permission.requested||!permission.granted) {
        resumeStart=true;localError='Разрешите точное местоположение.';render(call('status'));return;
      }
      busy=true;render(last);feedback('Включаем GPS…');
      const access=await api('issue');
      // The key never enters a URL, localStorage, diagnostics or employee fields.
      serverConnection=null;serverConnectionAt=0;serverAt=0;serverError=false;connectionEpoch++;
      render(call('start',{token:access.device_identifier,expires:Math.round(access.expires_at*1000),
        engineer:engineerId,account:account.username+'@'+account.region}));
    }catch(error){localError=error.message;}
    finally{busy=false;render(call('status'));}
  }
  function stop() {
    resumeStart=false;localError=null;serverConnection=null;serverConnectionAt=0;serverError=false;connectionEpoch++;
    try{render(call('stop'));}
    catch(error){localError=error.message;feedback(localError);}
  }
  async function init() {
    if(ready||!window.FieldRouterNative||!$('gps-start'))return;ready=true;
    const keepAwake=$('gps-keep-awake')?.closest('label');if(keepAwake)keepAwake.hidden=true;
    if($('gps-explanation'))$('gps-explanation').textContent='Передача работает и при выключенном экране.';
    const action=document.createElement('button');
    action.id='gps-android-settings';action.type='button';action.className='secondary';action.hidden=true;
    $('gps-feedback').after(action);
    action.onclick=()=>{try{call(action.dataset.action||'settings');}catch(error){feedback(error.message);}};
    $('gps-start').addEventListener('click',event=>{event.stopImmediatePropagation();start();},true);
    $('gps-stop').addEventListener('click',event=>{event.stopImmediatePropagation();stop();},true);
    window.addEventListener('account-logout',()=>stop());
    try {
      account=await window.FieldRouter.ready;
      engineerId=engineerId||$('engineer-select')?.value;
      if(account.role==='engineer' && engineerId)render(call('bind',{engineer:engineerId,account:account.username+'@'+account.region}));
      else if(account.role==='engineer')render(call('status'));
      else{call('stop');render(call('status'));}
    }catch(error){feedback(error.message);}
  }
  window.addEventListener('field-router-native-ready',init);
  window.addEventListener('field-router-native-status',event=>{if(ready)render(event.detail);});
  window.addEventListener('engineer-state',event=>{
    if(engineerId!==event.detail.engineerId){serverConnection=null;serverConnectionAt=0;serverAt=0;connectionEpoch++;}
    engineerId=event.detail.engineerId;
    if(ready && account?.role==='engineer')render(call('bind',{engineer:engineerId,account:account.username+'@'+account.region}));
  });
  window.addEventListener('engineer-action',event=>{
    if(ready && event.detail.status==='available')start();
  });
  window.addEventListener('field-router-native-permission',()=>{
    if(!resumeStart)return;resumeStart=false;
    if(call('status').permission)start();
    else {localError='GPS не включён: нет разрешения.';render(call('status'));}
  });
  if(window.FieldRouterNative)init();
})();
