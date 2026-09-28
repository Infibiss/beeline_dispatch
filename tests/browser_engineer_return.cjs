// Employee availability -> dispatcher notification -> approval; synthetic data only.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {login, password} = require('./browser_auth_helpers.cjs');
const root = path.resolve(__dirname, '..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'engineer-return-'));

(async () => {
  let server, browser;
  try {
    server = spawn(process.env.PYTHON_BIN || 'python',
      ['-B', 'tests/engineer_return_browser_server.py', tmp], {
        cwd:root, windowsHide:true, stdio:['ignore','pipe','pipe'],
        env:{...process.env, PYTHONUTF8:'1', PYTHONPATH:path.join(root,'src'),
             FIELD_ROUTER_ADMIN_PASSWORD:password}
      });
    const base = await new Promise((resolve,reject) => {
      let output = '';
      const timer = setTimeout(() => reject(Error('Server startup timeout')), 20000);
      server.once('error', err => {clearTimeout(timer); reject(err);});
      server.once('exit', code => {clearTimeout(timer); reject(Error('Server exited: '+code));});
      server.stderr.on('data', data => process.stderr.write(data));
      server.stdout.on('data', data => {
        output += data;
        const match = output.match(/http:\/\/127\.0\.0\.1:\d+/);
        if (match) {clearTimeout(timer); resolve(match[0]);}
      });
    });
    browser = await chromium.launch({headless:true,
      executablePath:process.env.BROWSER_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
    const mobileContext = await browser.newContext({viewport:{width:390,height:844}});
    const adminContext = await browser.newContext({viewport:{width:1365,height:900}});
    const mobile = await mobileContext.newPage();
    const errors = [];
    mobile.on('pageerror', error => errors.push(error.message));
    const waitVersion = version => mobile.waitForFunction(expected =>
      document.querySelector('#plan-version')?.textContent.trim().endsWith(String(expected)),version);
    const employeeState = async () => {
      const response = await mobileContext.request.get(base+'/r/east/api/engineer?engineer_id=e1');
      assert.equal(response.status(),200);
      return response.json();
    };
    const submit = async (button, endpoint) => {
      const pending = mobile.waitForResponse(response => response.url().endsWith(endpoint)
        && response.request().method() === 'POST');
      await mobile.click(button);
      const response = await pending;
      const result = await response.json();
      assert.equal(response.status(),200,JSON.stringify(result));
      return result;
    };
    const noOverflow = async () => assert.equal(await mobile.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth),true);
    const openAbsence = async (duration,start,end,later=false,send=true) => {
      await mobile.click('#shift-problem');
      await mobile.locator('#availability-dialog').waitFor({state:'visible'});
      assert.equal(await mobile.locator('#unavailability-duration').inputValue(),'fixed');
      await mobile.selectOption('#unavailability-start-mode',later ? 'later' : 'now');
      await mobile.selectOption('#unavailability-duration',duration);
      if (later) {
        await mobile.fill('#unavailability-start-date','2026-08-17');
        await mobile.fill('#unavailability-start-time',start);
      }
      if (duration === 'fixed') {
        await mobile.fill('#unavailability-end-date','2026-08-17');
        await mobile.fill('#unavailability-end-time',end);
      }
      await mobile.selectOption('#unavailability-reason','transport');
      await mobile.fill('#unavailability-comment','Ремонт автомобиля');
      if (await mobile.locator('#unavailability-location').isVisible()) {
        await mobile.selectOption('#unavailability-location','office');
      }
      await noOverflow();
      await mobile.screenshot({path:path.join(tmp,duration+'-absence-form.png'),fullPage:true});
      if (!send) return;
      const result = await submit('#unavailability-submit',duration === 'fixed'
        ? '/api/engineer/unavailability' : '/api/engineer/facts');
      await mobile.locator('#availability-dialog').waitFor({state:'hidden'});
      return result;
    };
    const returnToWork = async time => {
      await mobile.locator('#shift-return').waitFor({state:'visible'});
      assert.equal(await mobile.locator('#shift-start').isVisible(),false);
      await mobile.screenshot({path:path.join(tmp,'active-absence-'+time.replace(':','')+'.png'),fullPage:true});
      await mobile.click('#shift-return');
      assert.equal(await mobile.locator('#action-title').textContent(),'Я снова доступен');
      assert.equal(await mobile.locator('#action-comment').isVisible(),false);
      await mobile.fill('#action-time',time);
      await mobile.selectOption('#action-location','office');
      await noOverflow();
      const result = await submit('#send-action','/api/engineer/return');
      await mobile.locator('#action-form').waitFor({state:'hidden'});
      assert.equal(await mobile.locator('#shift-return').isVisible(),false);
      assert.equal(result.return_pending_replan,true);
      assert.ok(result.pending_reassignment_ids.length);
      await mobile.reload();
      await waitVersion(result.version);
      await mobile.click('#tab-shift');
      assert.equal((await employeeState()).return_pending_replan,true);
      assert.equal(await mobile.locator('#shift-return').isVisible(),false);
      await noOverflow();
      return result;
    };
    await mobile.goto(base+'/login');
    await mobile.fill('#username','worker');
    await mobile.fill('#password',password);
    await mobile.click('#login-submit');
    await mobile.waitForURL('**/engineer');
    await waitVersion(2);
    await mobile.click('#tab-shift');
    assert.equal(await mobile.locator('#gps-native-panel').count(),0);
    assert.equal(await mobile.locator('#shift-return').isVisible(),false);
    assert.equal(await mobile.locator('#unavailability-form').isVisible(),false);

    // An unknown end must use exactly the same return control as a fixed period.
    const indefinite = await openAbsence('unknown','09:00');
    assert.equal(indefinite.status.status,'unavailable');
    await returnToWork('09:15');

    await login(adminContext,base);
    const dispatch = await adminContext.newPage();
    dispatch.on('pageerror', error => errors.push(error.message));
    const approve = async (id,time) => {
      await dispatch.goto(base+'/r/east/');
      await dispatch.click('#tab-notifications');
      await dispatch.waitForFunction(() => document.querySelector('#dispatch-notifications')
        .textContent.includes('снова доступен'));
      const state = await (await adminContext.request.get(base+'/r/east/api/state')).json();
      const response = await adminContext.request.post(base+'/r/east/api/preview', {data:{
        version:state.version, event:{id,type:'replan',time}}});
      assert.equal(response.status(),200);
      const batch = await response.json();
      const applied = await adminContext.request.post(base+'/r/east/api/approve',{data:{
        version:state.version,batch_id:batch.id,candidate_id:'stable'}});
      assert.equal(applied.status(),200);
      const refreshed = mobile.waitForResponse(response => response.url().includes('/api/engineer?'));
      await mobile.click('#refresh');
      await refreshed;
      const after = await employeeState();
      await waitVersion(after.version);
      assert.equal(after.return_pending_replan,false);
      assert.equal(after.pending_reassignment_ids.length,0);
      assert.ok(after.tasks.length);
    };
    await approve('returned-unknown',555);

    const fixed = await openAbsence('fixed','09:15','12:00');
    assert.equal(fixed.active_absence.end_time,'12:00');
    const returned = await returnToWork('09:30');
    const completedPeriod = returned.unavailability.find(period => period.id === fixed.active_absence.id);
    assert.ok(completedPeriod.early_return);
    assert.equal(completedPeriod.start_time,'09:15');
    assert.equal(completedPeriod.end_time,'09:30');
    const persisted = await employeeState();
    assert.equal(persisted.unavailability.find(period => period.id === completedPeriod.id).end_time,'09:30');
    await approve('returned-fixed',570);

    // A future period is visible, but must not call the engineer currently unavailable.
    const future = await openAbsence('fixed','11:00','12:00',true);
    assert.equal(future.active_absence,null);
    assert.equal(future.status.status,'available');
    assert.equal(await mobile.locator('#shift-return').isVisible(),false);
    assert.match(await mobile.locator('#availability-summary').textContent(),/11:00/);
    assert.doesNotMatch(await mobile.locator('#engineer-status').textContent(),/^Недоступен/);
    await mobile.reload();
    await waitVersion(future.version);
    await mobile.click('#tab-shift');
    assert.match(await mobile.locator('#availability-summary').textContent(),/11:00/);
    await noOverflow();
    await mobile.screenshot({path:path.join(tmp,'shift-mobile.png'),fullPage:true});
    await mobile.setViewportSize({width:1365,height:900});
    await noOverflow();
    await mobile.screenshot({path:path.join(tmp,'shift-desktop.png'),fullPage:true});
    assert.equal(await mobile.locator('nav [aria-current="page"]').count(),1);
    assert.equal(await mobile.locator('nav [aria-current="page"]').getAttribute('id'),'tab-shift');

    // Announcing a future absence must not interrupt work already in progress.
    await mobile.setViewportSize({width:390,height:844});
    await mobile.click('#tab-tasks');
    assert.equal(await mobile.locator('nav [aria-current="page"]').count(),1);
    assert.equal(await mobile.locator('nav [aria-current="page"]').getAttribute('id'),'tab-tasks');
    await mobile.click('button[aria-label^="Заявка a,"]');
    await mobile.locator('#task-actions').getByRole('button',{name:'Начать работу',exact:true}).click();
    await mobile.fill('#action-time','09:40');
    await mobile.fill('#action-remaining','30');
    const working = await submit('#send-action','/api/engineer/facts');
    assert.equal(working.status.status,'working');
    await mobile.locator('#action-form').waitFor({state:'hidden'});
    await mobile.click('#tab-shift');
    const laterWhileWorking = await openAbsence('fixed','13:00','14:00',true);
    assert.equal(laterWhileWorking.status.status,'working');
    assert.equal(laterWhileWorking.facts.filter(fact => fact.status === 'interrupted').length,0);
    assert.ok(laterWhileWorking.unavailability.some(period => period.start_time === '13:00'));

    // For an immediate absence, interruption is explicit and the form survives it.
    const absenceRequests = [];
    mobile.on('request',request => {
      if (request.method() === 'POST' && request.url().endsWith('/api/engineer/unavailability')) {
        absenceRequests.push(request);
      }
    });
    await openAbsence('fixed',null,'10:30',false,false);
    await mobile.click('#unavailability-submit');
    await mobile.locator('#action-form').waitFor({state:'visible'});
    assert.match(await mobile.locator('#action-title').textContent(),/Приостановить/);
    assert.equal(absenceRequests.length,0);
    assert.equal((await employeeState()).status.status,'working');
    await mobile.fill('#action-time','09:41');
    await mobile.fill('#action-remaining','20');
    await mobile.fill('#action-comment','Приостанавливаю работу: ремонт автомобиля');
    const interrupted = await submit('#send-action','/api/engineer/facts');
    assert.equal(interrupted.status.status,'interrupted');
    await mobile.locator('#availability-dialog').waitFor({state:'visible'});
    assert.equal(await mobile.locator('#unavailability-duration').inputValue(),'fixed');
    assert.equal(await mobile.locator('#unavailability-end-time').inputValue(),'10:30');
    assert.equal(await mobile.locator('#unavailability-reason').inputValue(),'transport');
    assert.equal(await mobile.locator('#unavailability-comment').inputValue(),'Ремонт автомобиля');
    assert.equal(await mobile.locator('#unavailability-location').inputValue(),'office');
    assert.equal(absenceRequests.length,0);
    await noOverflow();
    const nowAbsent = await submit('#unavailability-submit','/api/engineer/unavailability');
    await mobile.locator('#availability-dialog').waitFor({state:'hidden'});
    assert.equal(absenceRequests.length,1);
    assert.equal(nowAbsent.active_absence.start_time,'09:41');
    assert.equal(nowAbsent.active_absence.end_time,'10:30');
    assert.equal(nowAbsent.active_absence.comment,'Ремонт автомобиля');
    assert.equal(await mobile.locator('#shift-return').isVisible(),true);
    assert.deepEqual(errors,[]);
    console.log('PASS: unified absence, early return, dispatcher approval, future/current absence during work, persisted form, mobile/desktop; '+tmp);
  } finally {
    if (browser) await browser.close();
    fs.writeFileSync(path.join(tmp,'stop'),'stop');
    if (server && server.exitCode === null) {
      await new Promise(resolve => {
        const timer = setTimeout(() => {server.kill(); resolve();},5000);
        server.once('exit',() => {clearTimeout(timer); resolve();});
      });
    }
  }
})().catch(error => {console.error(error); process.exitCode=1;});
