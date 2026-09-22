(function () {
  'use strict';
  const match = location.pathname.match(/^\/(r|regions)\/([a-zA-Z0-9_-]+)(?:\/|$)/);
  const base = match ? '/' + match[1] + '/' + match[2] : '';
  const url = path => base + '/' + path.replace(/^\/+/, '');
  let identity = null;
  const ready = fetch('/api/auth/me', {cache:'no-store'}).then(async response => {
    if (!response.ok) {location.replace('/login');throw Error('Войдите в учётную запись');}
    identity = await response.json(); return identity;
  });
  ready.catch(() => {});
  async function authenticatedFetch(path, options = {}) {
    const account = await ready;
    const headers = new Headers(options.headers);
    if (options.method && !['GET','HEAD'].includes(options.method.toUpperCase())) headers.set('X-CSRF-Token', account.csrf);
    const response = await fetch(path, {...options, headers});
    if (response.status===401) location.replace('/login');
    return response;
  }
  window.FieldRouter = Object.freeze({url, base, ready, fetch:authenticatedFetch});
  document.addEventListener('DOMContentLoaded', async () => {
    const bar = document.getElementById('region-context') || document.createElement('div'); bar.className = 'region-context';
    const label = bar.querySelector('label') || document.createElement('label'); label.textContent = 'Район данных';
    const select = document.createElement('select'); select.id = 'region-select'; select.disabled = true;
    const caption = document.createElement('span'); caption.textContent = 'Сервер: ' + location.host;
    const hint = document.createElement('span'); hint.className = 'region-hint';
    hint.textContent = 'У каждого района свой состав и план.';
    label.append(select); bar.append(label, caption, hint);
    const main = document.querySelector('main'); if (!main) return;
    main.prepend(bar);
    try {
      const account = await ready;
      if(account.role!=='demo') {
        caption.textContent = account.username+' · '+(account.role==='engineer'?'Инженер':'Диспетчер');
        const logout=document.createElement('button');logout.type='button';logout.textContent='Выйти';logout.id='sign-out';
        logout.onclick=async()=>{logout.disabled=true;try {
          const detail={tasks:[]};window.dispatchEvent(new CustomEvent('account-logout',{detail}));
          await Promise.race([Promise.all(detail.tasks),new Promise(resolve=>setTimeout(resolve,1500))]);
          const response=await authenticatedFetch('/api/auth/logout',{method:'POST'});
          if(!response.ok)throw Error('Не удалось выйти. Повторите.');
          location.replace('/login');
        } catch(error){hint.textContent=error.message;logout.disabled=false;}};
        bar.append(logout);
        if(account.role==='admin') {
          const link=document.createElement('a');link.href='/accounts';link.textContent='Доступ сотрудников';bar.append(link);
        } else {
          label.hidden=true;hint.textContent='Личный рабочий профиль';
        }
      }
      const response = await authenticatedFetch(url('/api/regions'), {cache:'no-store'});
      if (!response.ok) throw Error('Районы недоступны');
      const data = await response.json();
      for (const region of data.regions) {
        const option = document.createElement('option'); option.value = region.id;
        option.textContent = region.label; select.append(option);
      }
      select.value = data.current; select.disabled = data.regions.length < 2;
      let planningBusy = false;
      window.addEventListener('planning-busy', event => {
        planningBusy = Boolean(event.detail?.busy);
        select.disabled = planningBusy || data.regions.length < 2;
      });
      select.addEventListener('change', () => {
        if (planningBusy) {select.value = data.current; return;}
        const event = new CustomEvent('region-change', {cancelable:true});
        if (!window.dispatchEvent(event)) {select.value = data.current; return;}
        const chosen = data.regions.find(region => region.id === select.value);
        const page = location.pathname.slice(base.length) || '/';
        location.href = chosen.path + page; // Do not carry another region's engineer ID.
      });
    } catch {hint.textContent = 'Не удалось загрузить районы. Обновите страницу.';}
  });
})();
