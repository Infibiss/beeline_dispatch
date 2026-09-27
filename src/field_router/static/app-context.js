(function () {
  'use strict';
  const match = location.pathname.match(/^\/(r|regions)\/([a-zA-Z0-9_-]+)(?:\/|$)/);
  const base = match ? '/' + match[1] + '/' + match[2] : '';
  const url = path => base + '/' + path.replace(/^\/+/, '');
  const ready = fetch('/api/auth/me', {cache:'no-store'}).then(async response => {
    if (!response.ok) {location.replace('/login');throw Error('Войдите в учётную запись');}
    return response.json();
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
  const el=(tag,text,className)=>{const n=document.createElement(tag);if(text)n.textContent=text;if(className)n.className=className;return n;};
  document.addEventListener('DOMContentLoaded', async () => {
    let header=document.querySelector('header');
    if(!header){header=el('header');document.body.prepend(header);}
    header.classList.add('product-header');
    if(!header.querySelector('.brand')){
      const brand=el('a',null,'brand');brand.href=url('/');
      const mark=el('span',null,'brand-mark');mark.setAttribute('aria-hidden','true');brand.append(mark,el('strong','билайн'));header.prepend(brand);
    }
    const bar=document.getElementById('region-context') || el('div',null,'header-region');
    bar.id='region-context';bar.className='header-region';
    const label=el('label');const labelText=el('span','Район');
    const select=document.getElementById('region-select') || el('select');
    select.id='region-select';select.setAttribute('aria-label','Район');select.disabled=true;select.replaceChildren();
    label.append(labelText,select);bar.replaceChildren(label);
    if(!header.contains(bar))header.append(bar);
    let slot=document.getElementById('account-slot');
    if(!slot){slot=el('div',null,'account-slot');slot.id='account-slot';header.append(slot);}
    const profile=el('details',null,'account-menu');profile.id='account-menu';
    const trigger=el('summary','Профиль');trigger.setAttribute('aria-label','Профиль и доступ');
    const menu=el('div',null,'account-dropdown');
    const feedback=el('p',null,'account-feedback');feedback.setAttribute('role','status');
    profile.append(trigger,menu);slot.replaceChildren(profile);
    const close=()=>{profile.open=false;};
    document.addEventListener('click',event=>{if(!profile.contains(event.target))close();});
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&profile.open){close();trigger.focus();}});
    try {
      const account=await ready;
      trigger.replaceChildren(el('span',account.username || 'Демо','account-name'));
      trigger.title=account.username || 'Демо';
      menu.append(el('strong',account.username || 'Демонстрация'),el('span',account.role==='engineer'?'Инженер':account.role==='demo'?'Демо-доступ':'Диспетчер','account-role'));
      if(account.role==='admin'){
        const accounts=el('a','Доступ сотрудников');accounts.href='/accounts';menu.append(accounts);
        const crews=el('a','Состав и оборудование');crews.href=url('/crew');menu.append(crews);
      }
      if(account.role==='engineer')bar.hidden=true;
      if(account.role!=='demo'){
        const logout=el('button','Выйти');logout.type='button';logout.id='sign-out';
        logout.addEventListener('click',async()=>{
          logout.disabled=true;feedback.textContent='';
          try{
            const detail={tasks:[]};window.dispatchEvent(new CustomEvent('account-logout',{detail}));
            await Promise.race([Promise.all(detail.tasks),new Promise(resolve=>setTimeout(resolve,1500))]);
            const response=await authenticatedFetch('/api/auth/logout',{method:'POST'});
            if(!response.ok)throw Error('Не удалось выйти. Повторите.');
            location.replace('/login');
          }catch(error){feedback.textContent=error.message;logout.disabled=false;}
        });
        menu.append(logout);
      }
      menu.append(feedback);
      const response=await authenticatedFetch(url('/api/regions'),{cache:'no-store'});
      if(!response.ok)throw Error('Районы недоступны');
      const data=await response.json();
      for(const region of data.regions){const option=el('option',region.label);option.value=region.id;select.append(option);}
      select.value=data.current;select.disabled=data.regions.length<2;
      let planningBusy=false;
      window.addEventListener('planning-busy',event=>{planningBusy=Boolean(event.detail?.busy);select.disabled=planningBusy||data.regions.length<2;});
      select.addEventListener('change',()=>{
        if(planningBusy){select.value=data.current;return;}
        const event=new CustomEvent('region-change',{cancelable:true});
        if(!window.dispatchEvent(event)){select.value=data.current;return;}
        const chosen=data.regions.find(region=>region.id===select.value);
        if(!chosen)return;
        const page=location.pathname.slice(base.length)||'/';
        location.href=chosen.path+page;
      });
    }catch{
      const failure=el('span','Не удалось загрузить профиль или районы. Обновите страницу.','header-error');
      failure.setAttribute('role','alert');bar.append(failure);
    }
  });
})();
