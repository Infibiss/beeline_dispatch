(() => {
  const $ = id => document.getElementById(id);
  const node = (tag,text) => {const el=document.createElement(tag);el.textContent=text;return el;};
  let regions=[];
  async function api(path, payload) {
    const response=await FieldRouter.fetch(path,payload?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}:{});
    const data=await response.json();if(!response.ok)throw Error(data.error || 'Ошибка сервера');return data;
  }
  function message(text) {$('account-message').textContent=text;}
  async function roster() {
    $('account-engineer').disabled=true; $('account-engineer').replaceChildren(); $('account-create').disabled=true;
    const key=$('account-region').value;
    const data=await api('/r/'+key+'/api/engineers');
    if(key!==$('account-region').value)return;
    for(const engineer of data.engineers) {const option=node('option',engineer.name+' · '+data.region);option.value=engineer.id;$('account-engineer').append(option);}
    $('account-engineer').disabled=false; $('account-create').disabled=!data.engineers.length;
  }
  function render(data) {
    $('account-list').replaceChildren();
    for(const account of data.accounts) {
      const card=node('article','');card.className='account-card';card.append(node('strong',account.username));
      const label=regions.find(r=>r.id===account.region)?.label || account.region;
      card.append(node('p',account.role==='admin'?'Диспетчер · управление службой':
        (account.engineer_name || account.engineer_id)+' · '+label+' · '+(account.enabled?'Доступ открыт':'Заблокирован')));
      if(account.role!=='admin') {
        const actions=node('div','');actions.className='account-actions';
        const password=document.createElement('input');password.type='password';password.minLength=12;password.maxLength=128;
        password.autocomplete='new-password';password.placeholder='Новый пароль, 12+ символов';password.setAttribute('aria-label','Новый пароль для '+account.username);
        const reset=node('button','Сменить пароль'),toggle=node('button',account.enabled?'Заблокировать':'Разблокировать');
        const change=async(payload,button)=>{button.disabled=true;try{render(await api('/api/accounts',{...payload,username:account.username}));message('Доступ обновлён.');}catch(e){message(e.message);}finally{button.disabled=false;}};
        reset.onclick=()=>change({action:'password',password:password.value},reset);
        toggle.onclick=()=>change({action:'enabled',enabled:!account.enabled},toggle);
        actions.append(password,reset,toggle);card.append(actions);
      }
      $('account-list').append(card);
    }
  }
  $('account-region').onchange=()=>roster().catch(e=>message(e.message));
  $('account-form').onsubmit=async event=>{
    event.preventDefault();$('account-create').disabled=true;
    try {render(await api('/api/accounts',{action:'create',username:$('account-login').value.trim(),password:$('account-password').value,
      region:$('account-region').value,engineer_id:$('account-engineer').value}));
      $('account-password').value='';message('Учётная запись создана. Передайте сотруднику логин, пароль и адрес /login.');}
    catch(e){message(e.message);}finally{$('account-create').disabled=false;}
  };
  (async()=>{
    const manifest=await api('/api/regions');regions=manifest.regions;
    for(const region of regions){const option=node('option',region.label);option.value=region.id;$('account-region').append(option);}
    await roster();render(await api('/api/accounts'));
  })().catch(e=>message(e.message));
})();
