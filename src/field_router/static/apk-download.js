/* Fetch inside the signed-in page, then save locally. No second authenticated
   server connection is delegated to a phone's external download manager. */
(() => {
  'use strict';
  const link=document.getElementById('android-apk-download');
  const status=document.getElementById('android-apk-status');
  if(!link||!status)return;
  const installed=!!window.FieldRouterNative||/FieldRouterAndroid\//.test(navigator.userAgent);
  if(installed) {
    const card=document.getElementById('android-app');
    if(card)card.hidden=true;
    return;
  }
  let busy=false;
  link.addEventListener('click',async event=>{
    if(event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
    event.preventDefault();
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
