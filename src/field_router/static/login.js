(() => {
  const $ = id => document.getElementById(id);
  $('show-password').onchange = () => {$('password').type = $('show-password').checked ? 'text' : 'password';};
  $('login-form').onsubmit = async event => {
    event.preventDefault(); $('login-submit').disabled = true; $('login-error').hidden = true;
    try {
      const response = await fetch('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({username:$('username').value.trim(), password:$('password').value})});
      const data = await response.json();
      if (!response.ok) throw Error(data.error || 'Не удалось войти');
      $('password').value = ''; location.replace(data.redirect);
    } catch (error) {$('login-error').textContent = error.message; $('login-error').hidden = false;}
    finally {$('login-submit').disabled = false;}
  };
})();
