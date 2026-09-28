// Optional UI integration check: Playwright + Edge; no credentials or TLS needed.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {login,password} = require('./browser_auth_helpers.cjs');
const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'crew-mobile-browser-'));
const stopFile = path.join(temporary, 'stop');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  let browser, server;
  try {
    server = spawn(process.env.PYTHON_BIN || 'python', ['-B', 'tests/gps_browser_server.py',
      'data/demo-road/Восток.json', '--state', path.join(temporary, 'state.json'),
      '--port', '0', '--time-limit', '.3', '--morning-time-limit', '.3', '--disable-gps'], {
      cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
      env: {...process.env, PYTHONUTF8:'1', PYTHONPATH:path.join(root, 'src'), GPS_TEST_STOP_FILE:stopFile, FIELD_ROUTER_ADMIN_PASSWORD:password}
    });
    const base = await new Promise((resolve, reject) => {
      let output = '';
      const timer = setTimeout(() => reject(Error('Server startup timeout')), 20000);
      server.on('error', error => {clearTimeout(timer); reject(error);});
      server.once('exit', code => {clearTimeout(timer); reject(Error('Server exited: ' + code));});
      server.stderr.on('data', chunk => process.stderr.write(chunk));
      server.stdout.on('data', chunk => {
        output += chunk;
        const match = output.match(/http:\/\/127\.0\.0\.1:\d+/);
        if (match) {clearTimeout(timer); resolve(match[0]);}
      });
    });
    browser = await chromium.launch({headless:true,
      executablePath:process.env.BROWSER_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
    const context = await browser.newContext({viewport:{width:390, height:844}});
    await login(context,base);
    await context.route('https://tile.openstreetmap.org/**', route => route.fulfill({status:204}));
    const errors = [];
    context.on('page', page => page.on('pageerror', error => errors.push(error.message)));
    const mobile = await context.newPage();
    await mobile.goto(base + '/engineer');
    await mobile.waitForFunction(() => document.querySelector('#plan-version').textContent === 'Версия 1');
    const dispatch = await context.newPage();
    async function workspaceTab(name) {
      const menu=dispatch.locator('#menu-toggle');
      if(await menu.isVisible() && await menu.getAttribute('aria-expanded')!=='true')await menu.click();
      await dispatch.locator('#tab-'+name).click();
      await dispatch.locator('#panel-'+name).waitFor({state:'visible'});
    }
    await dispatch.goto(base);
    await workspaceTab('crew');
    const editor=dispatch.frameLocator('#crew-frame');
    await editor.locator('.crew-row').first().waitFor();
    const original = await (await context.request.get(base + '/api/crew')).json();
    const oldIndex = await (await context.request.get(base + '/api/engineers')).json();
    const home = original.locations.find(point => point.id !== 'office' && !point.id.startsWith('home:'));
    assert.ok(home, 'Demo must contain an alternative starting point');
    // Retain the existing 15 engineers, but give the new one the only
    // connection skill, so its participation does not depend on solver ties.
    for (const existing of await editor.locator('.crew-row').all()) {
      await existing.locator('[data-skill="emergency"]').check();
      await existing.locator('[data-skill="local"]').uncheck();
      await existing.locator('[data-skill="connection"]').uncheck();
    }
    await editor.locator('#add').click();
    const row = editor.locator('.crew-row').last();
    const id = await row.getAttribute('data-id');
    await row.locator('[data-key="name"]').fill('Бригада — проверка связи');
    await row.locator('[data-key="transport"]').selectOption('car');
    await row.locator('[data-key="start_location"]').selectOption(home.id);
    await row.locator('[data-key="end"]').fill('22:00');
    for (const skill of ['local','connection','emergency']) await row.locator('[data-skill="' + skill + '"]').check();
    await editor.locator('#preview-button').click();
    try {
      await editor.locator('#preview').waitFor({state:'visible'});
    } catch (error) {
      console.error(await editor.locator('body').evaluate(() => ({
        status:document.querySelector('#status').textContent,
        error:document.querySelector('#error').textContent,
        invalid:[...document.querySelectorAll(':invalid')].map(n => ({key:n.dataset.key, value:n.value, message:n.validationMessage}))
      })), errors);
      throw error;
    }
    assert.equal((await (await context.request.get(base + '/api/engineers')).json()).version, 1);
    await editor.locator('#apply').click();
    await dispatch.locator('#panel-plan').waitFor({state:'visible'});
    await dispatch.goto(base+'/crew');
    await editor.locator('.crew-row').first().waitFor();
    assert.equal((await (await context.request.get(base+'/api/engineers')).json()).engineers.length, original.engineers.length+1);
    // Reproduce the race: old roster arrives with a newer personal plan.
    // A single plan-version comparison would then miss the new engineer forever.
    const racing = await context.newPage();
    let firstIndex = true;
    await racing.route('**/api/engineers', route => {
      if (firstIndex) {firstIndex=false; return route.fulfill({json:oldIndex});}
      return route.continue();
    });
    await racing.goto(base+'/engineer');
    await racing.waitForFunction(() => document.querySelector('#plan-version').textContent==='Версия 2');
    await racing.waitForFunction(id => [...document.querySelector('#engineer-select').options].some(o=>o.value===id), id, {timeout:20000});
    await racing.close();
    await mobile.bringToFront();
    // No manual refresh: the roster must arrive in the already-open application.
    await mobile.waitForFunction(id => [...document.querySelector('#engineer-select').options].some(o=>o.value===id), id, {timeout:20000});
    assert.equal(await mobile.locator('#engineer-select').inputValue(),original.engineers[0].id);
    await mobile.selectOption('#engineer-select',id);
    await mobile.waitForFunction(() => document.querySelector('#plan-version').textContent === 'Версия 2');
    assert.equal(await mobile.locator('#engineer-select option:checked').textContent(),'Бригада — проверка связи · Восток');
    const direct=editor.locator('.crew-row[data-id="'+id+'"] .crew-mobile-link');
    assert.equal(await direct.getAttribute('href'),'/engineer?engineer_id='+id);
    await mobile.click('#tab-shift');
    const summary = await mobile.locator('#shift-details').textContent();
    assert.match(summary, /Смена/);
    assert.match(summary, /Осталось заявок/);
    assert.match(summary, /Выполнено/);
    assert.doesNotMatch(summary, /Старт смены|Транспорт/);
    await mobile.click('#shift-start');
    assert.equal(await mobile.locator('#action-location').inputValue(), '');
    await mobile.selectOption('#action-location', 'home:' + id);
    assert.match(await mobile.locator('#action-location option:checked').textContent(), /^Старт: /);
    await mobile.click('#send-action');
    await mobile.waitForSelector('#task-dialog', {state:'hidden'});
    await mobile.click('#tab-tasks');
    await mobile.locator('#task-list .task-card').first().click();
    await mobile.getByRole('button', {name:'Начать работу', exact:true}).click();
    await mobile.click('#send-action');
    await mobile.waitForSelector('#task-dialog', {state:'hidden'});
    await mobile.locator('#task-list .task-card').first().click();
    await mobile.getByRole('button', {name:'Сообщить о выполнении', exact:true}).click();
    await mobile.locator('#action-comment').fill('Результат из мобильного приложения');
    await mobile.click('#send-action');
    await mobile.waitForSelector('#task-dialog', {state:'hidden'});
    const state = await (await context.request.get(base + '/api/state')).json();
    assert.equal(state.fact_journal.length, 3);
    assert.ok(state.fact_journal.every(f => f.engineer_id === id && f.source === 'engineer'));
    assert.equal(state.fact_journal.at(-1).status, 'completion_pending');
    assert.equal(state.pending_completions.length,1);
    assert.equal(state.fact_journal.at(-1).comment, 'Результат из мобильного приложения');
    await dispatch.bringToFront();
    await dispatch.goto(base);
    await dispatch.click('#reload');
    await workspaceTab('facts');
    await dispatch.getByText('Результат из мобильного приложения', {exact:false}).first().waitFor();
    await dispatch.getByRole('button',{name:'Подтвердить выполнение',exact:true}).click();
    await dispatch.waitForFunction(()=>document.querySelector('#completion-panel').hidden);
    const confirmed=await (await context.request.get(base+'/api/state')).json();
    assert.equal(confirmed.fact_journal.at(-1).status,'completed');
    assert.equal(confirmed.fact_journal.at(-1).source,'dispatcher');
    await dispatch.selectOption('#region-select','south-east');
    await dispatch.waitForURL(url=>url.origin===base && url.pathname==='/r/south-east/');
    await workspaceTab('crew');
    await editor.locator('.crew-row').first().waitFor();
    assert.equal(await dispatch.locator('#region-select').inputValue(),'south-east');
    assert.match(await dispatch.locator('#crew-frame').getAttribute('src'),/\/r\/south-east\/crew\?embedded=1/);
    const southRoster=await (await context.request.get(base+'/r/south-east/api/crew')).json();
    assert.ok(!southRoster.engineers.some(e=>e.id===id));
    assert.match(await editor.locator('#context').textContent(),new RegExp('^Бригад в составе: '+southRoster.engineers.length+' ·'));
    assert.match(await dispatch.locator('#region-select option:checked').textContent(),/^Юго-восток/);
    assert.equal(await editor.locator('.crew-row[data-id="'+id+'"]').count(),0);
    await editor.locator('#add').click();
    await editor.locator('.crew-row').last().locator('[data-key="name"]').fill('Только юго-восток');
    const southId=await editor.locator('.crew-row').last().getAttribute('data-id');
    await editor.locator('#preview-button').click();
    await editor.locator('#preview').waitFor({state:'visible'});
    assert.ok(!(await (await context.request.get(base+'/r/south-east/api/engineers')).json()).engineers.some(e=>e.id===southId));
    await editor.locator('#apply').click();
    await dispatch.waitForURL(url=>url.origin===base && url.pathname==='/r/south-east/');
    await dispatch.locator('#panel-plan').waitFor({state:'visible'});
    const southMobile=await context.newPage();
    await southMobile.goto(base+'/r/south-east/engineer?engineer_id='+southId);
    await southMobile.waitForFunction(id=>document.querySelector('#engineer-select').value===id,southId);
    assert.equal(await southMobile.locator('#engineer-select option:checked').textContent(),'Только юго-восток · Юго-восток');
    assert.ok(!(await (await context.request.get(base+'/api/engineers')).json()).engineers.some(e=>e.id===southId));
    for (const width of [320,390,768]) {
      await mobile.setViewportSize({width, height:844});
      await mobile.click('#tab-shift');
      assert.ok(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
        'Shift with a home address must fit: ' + width);
    }
    assert.deepEqual(errors, []);
    console.log('PASS: add 16th engineer → preview → explicit apply → automatic roster refresh (including version race) → pending completion → dispatcher confirmation; independent regional rosters and links; 320/390/768 px.');
  } finally {
    if (browser) await browser.close();
    if (server && server.exitCode === null) {
      const closed = new Promise(resolve => server.once('close', resolve));
      fs.writeFileSync(stopFile, 'stop');
      await Promise.race([closed, delay(5000).then(() => {if (server.exitCode === null) server.kill();})]);
    }
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
