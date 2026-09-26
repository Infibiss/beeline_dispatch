/* Fetch inside the signed-in page, then save locally. No second authenticated
   server connection is delegated to a phone's external download manager. */
(() => {
  'use strict';
  const link=document.getElementById('android-apk-download');
  const status=document.getElementById('android-apk-status');
  if(!link||!status)return;
  // Older installed APKs cannot save blob downloads from their WebView.
  // Keep the update entry visible, but direct them to the browser honestly.
  const installed=!!window.FieldRouterNative||/FieldRouterAndroid\//.test(navigator.userAgent);
  const browserHelp='Для обновления откройте этот же кабинет в браузере телефона, перейдите в «Смена» → «Приложение для Android» и скачайте APK. Затем установите файл поверх прежней версии.';
  if(installed) {
    link.textContent='Как скачать обновление';
    const help=document.getElementById('android-apk-help');
    if(help)help.textContent='Приложение уже установлено. Новую APK можно скачать через браузер телефона.';
  }
  let busy=false;
  link.addEventListener('click',async event=>{
    if(event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
    event.preventDefault();
    if(installed){status.textContent=browserHelp;return;}
    if(busy)return;busy=true;
    link.setAttribute('aria-disabled','true');
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),30000);
    status.textContent='Получаем приложение с сервера…';
    try {
      const response=await fetch(link.href,{credentials:'same-origin',cache:'no-store',signal:controller.signal});
      const kind=response.headers.get('Content-Type')||'';
      if(response.redirected||kind.includes('text/html'))
        throw Error('Войдите под своей учётной записью и повторите скачивание.');
      if(!response.ok) {
        let message='Сервер не смог передать приложение ('+response.status+').';
        if(kind.includes('application/json')) {
          const result=await response.json();if(result.error)message=result.error;
        }
        throw Error(message);
      }
      if(!kind.startsWith('application/vnd.android.package-archive'))
        throw Error('Сервер вернул другой файл. Перезапустите обновлённый сервер.');
      if(Number(response.headers.get('Content-Length'))>64*1024*1024)
        throw Error('Файл превышает допустимый размер загрузки.');
      const blob=await response.blob();
      if(blob.size<4||blob.size>64*1024*1024)throw Error('Получен неполный файл приложения.');
      const signature=new Uint8Array(await blob.slice(0,4).arrayBuffer());
      if(signature[0]!==80||signature[1]!==75||signature[2]!==3||signature[3]!==4)
        throw Error('Полученный файл не является APK. Обратитесь к диспетчеру.');
      const objectUrl=URL.createObjectURL(blob),save=document.createElement('a');
      save.href=objectUrl;save.download='field-engineer-debug.apk';save.hidden=true;
      document.body.append(save);save.click();save.remove();
      setTimeout(()=>URL.revokeObjectURL(objectUrl),60000);
      status.textContent='Файл получен: '+Math.ceil(blob.size/1024)+' КБ. Если браузер просит подтверждение, подтвердите сохранение. Затем откройте APK в загрузках телефона.';
    } catch(error) {
      status.textContent=error.name==='AbortError'
        ?'Сервер не ответил за 30 секунд. Проверьте соединение телефона с домашней сетью и повторите.'
        :error instanceof TypeError
          ?'Не удалось получить файл. Проверьте соединение и доверие к HTTPS-сертификату. Готовую APK также можно перенести с компьютера по USB.'
          :error.message;
    } finally {clearTimeout(timer);busy=false;link.removeAttribute('aria-disabled');}
  });
})();
