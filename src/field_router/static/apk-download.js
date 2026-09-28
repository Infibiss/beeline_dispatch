/* Fetch inside the signed-in page, then save locally. No second authenticated
   server connection is delegated to a phone's external download manager. */
(() => {
  'use strict';
  const link=document.getElementById('android-apk-download');
  const status=document.getElementById('android-apk-status');
  if(!link||!status)return;
  const installed=()=>!!window.FieldRouterNative||/FieldRouterAndroid\//.test(navigator.userAgent);
  function updateEntry() {
    if(!installed())return;
    link.textContent='Скопировать ссылку на APK';
    link.removeAttribute('download');
    const help=document.getElementById('android-apk-help');
    if(help)help.textContent='Для обновления откройте ссылку в браузере и войдите в рабочий профиль.';
  }
  updateEntry();
  window.addEventListener('field-router-native-ready',updateEntry);
  let busy=false;
  link.addEventListener('click',async event=>{
    if(!installed()&&(event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey))return;
    event.preventDefault();
    if(busy)return;busy=true;
    link.setAttribute('aria-disabled','true');
    if(installed()) {
      // WebView has no blob download handler. Keep the authenticated browser
      // path available without passing account cookies or GPS tokens in a URL.
      try {
        await navigator.clipboard.writeText(link.href);
        document.getElementById('android-apk-copy-link')?.remove();
        status.textContent='Ссылка скопирована. Откройте её в браузере.';
      } catch(error) {
        let input=document.getElementById('android-apk-copy-link');
        if(!input) {
          input=document.createElement('input');input.id='android-apk-copy-link';
          input.type='text';input.readOnly=true;input.setAttribute('aria-label','Ссылка на APK');
          status.before(input);
        }
        input.value=link.href;input.focus();input.select();
        status.textContent='Скопируйте ссылку и откройте её в браузере.';
      } finally {busy=false;link.removeAttribute('aria-disabled');}
      return;
    }
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),30000);
    status.textContent='Получаем приложение с сервера…';
    try {
      const response=await fetch(link.href,{credentials:'same-origin',cache:'no-store',signal:controller.signal});
      const kind=(response.headers.get('Content-Type')||'').toLowerCase();
      if(response.status===401||response.redirected||kind.includes('text/html'))
        throw Error('Войдите под своей учётной записью и повторите скачивание.');
      if(!response.ok) {
        let message='Сервер не смог передать приложение ('+response.status+').';
        if(kind.includes('application/json')) {
          const result=await response.json();if(result.error)message=result.error;
        }
        throw Error(message);
      }
      if(!kind.startsWith('application/vnd.android.package-archive'))
        throw Error('Не удалось получить APK. Обратитесь к диспетчеру.');
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
      status.textContent='Скачано: '+Math.ceil(blob.size/1024)+' КБ. Откройте APK в загрузках телефона.';
    } catch(error) {
      status.textContent=error.name==='AbortError'
        ?'Сервер не отвечает. Проверьте подключение и повторите.'
        :error instanceof TypeError
          ?'Не удалось скачать APK. Проверьте подключение и HTTPS-сертификат.'
          :error.message;
    } finally {clearTimeout(timer);busy=false;link.removeAttribute('aria-disabled');}
  });
})();
