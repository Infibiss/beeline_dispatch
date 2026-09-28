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
      assert.match(await mobile.locator('#unavailability-title').textContent(),/Период отсутствия/);
      assert.deepEqual(await mobile.locator('#unavailability-reason option').evaluateAll(
        options => options.map(option => option.value)),['','sick','transport','personal','other']);
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
      assert.equal(await mobile.locator('#unavailability-location').count(),0);
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
      assert.equal(await mobile.locator('#action-location').isVisible(),false);
      await noOverflow();
      const result = await submit('#send-action','/api/engineer/return');
      await mobile.locator('#action-form').waitFor({state:'hidden'});
      assert.equal(await mobile.locator('#shift-return').isVisible(),false);
      assert.equal(result.return_pending_replan,true);
      assert.equal(result.location_confirmation_pending,true);
      assert.equal(result.status.location_id,null);
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
    assert.equal(await mobile.locator('#panel-shift h1').textContent(),'Профиль');
    assert.equal(await mobile.locator('#availability-title').textContent(),'Доступность для заявок');
    assert.match(await mobile.locator('#shift-plan-context').textContent(),/17 августа 2026/);
    assert.match(await mobile.locator('#shift-plan-context').textContent(),/09:00/);
    await mobile.locator('#android-app summary').click();
    assert.equal(await mobile.locator('#android-apk-download').isVisible(),true);
    await mobile.locator('#android-app summary').click();
    assert.equal(await mobile.locator('#gps-native-panel').count(),0);
    assert.equal(await mobile.locator('#availability-state').count(),0);
    assert.equal(await mobile.locator('#shift-start-location').count(),0);
    assert.equal(await mobile.locator('#shift-return').isVisible(),false);
    assert.equal(await mobile.locator('#unavailability-form').isVisible(),false);

    // A scheduled day off requires dispatcher's call-in flow. A stale available
    // status must not label it a working day or reveal a start-shift bypass.
    // Project responses in this browser only; the synthetic server is untouched.
    const originalDay = await employeeState();
    const employeeUrl = /\/api\/engineer\?/;
    for (const [offDuty,status,expectedLabel,startVisible] of [
      [true,null,'Выходной',false],
      [true,{id:'off-day-ui-only',status:'available',time:540,location_id:'office'},'Выходной',false],
      [false,null,'Не на смене',true]
    ]) {
      const projected = {...originalDay,engineer:{...originalDay.engineer,off_duty:offDuty},status};
      await mobileContext.route(employeeUrl,route => route.fulfill({json:projected}));
      await mobile.reload();
      await waitVersion(originalDay.version);
      await mobile.click('#tab-shift');
      assert.equal(await mobile.locator('#engineer-status').textContent(),expectedLabel);
      assert.equal(await mobile.locator('#shift-start').isVisible(),startVisible);
      await mobileContext.unroute(employeeUrl);
    }
    await mobile.reload();
    await waitVersion(originalDay.version);
    await mobile.click('#tab-shift');
    assert.deepEqual(await employeeState(),originalDay);

    // Opening and cancelling a form must not produce an absence or a fact.
    const beforeCancel = await employeeState();
    await openAbsence('fixed',null,'12:00',false,false);
    await mobile.click('#close-availability');
    await mobile.locator('#availability-dialog').waitFor({state:'hidden'});
    assert.deepEqual(await employeeState(),beforeCancel);
    await mobile.click('#shift-problem');
    await mobile.keyboard.press('Escape');
    assert.deepEqual(await employeeState(),beforeCancel);
    await openAbsence('fixed','11:00','10:00',true,false);
    await mobile.click('#unavailability-submit');
    await mobile.locator('#unavailability-error').waitFor({state:'visible'});
    assert.deepEqual(await employeeState(),beforeCancel);
    await mobile.click('#close-availability');

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
      const apply = async (suffix,when,expectHeld=false) => {
        const state = await (await adminContext.request.get(base+'/r/east/api/state')).json();
        const response = await adminContext.request.post(base+'/r/east/api/preview', {data:{
          version:state.version, event:{id:id+suffix,type:'replan',time:when}}});
        assert.equal(response.status(),200,await response.text());
        const batch = await response.json();
        if (expectHeld) for (const option of batch.candidates)
          assert.ok(option.plan.routes.filter(route=>route.engineer_id==='e1')
            .every(route=>route.visits.length===0));
        const applied = await adminContext.request.post(base+'/r/east/api/approve',{data:{
          version:state.version,batch_id:batch.id,candidate_id:'stable'}});
        assert.equal(applied.status(),200,await applied.text());
      };
      // Availability does not invent a location. Even an approved replan must
      // keep assignments held until dispatch explicitly confirms the point.
      await apply('-unknown',time,true);
      const held = await employeeState();
      assert.equal(held.location_confirmation_pending,true);
      assert.equal(held.status.location_id,null);
      const known = await adminContext.request.post(base+'/r/east/api/facts',{data:{
        version:held.version,engineer_id:'e1',status:'available',time:time+1,
        location_id:'office',comment:'Место подтверждено диспетчером'}});
      assert.equal(known.status(),200,await known.text());
      await apply('-known',time+1);
      const refreshed = mobile.waitForResponse(response => response.url().includes('/api/engineer?'));
      await mobile.click('#refresh');
      await refreshed;
      const after = await employeeState();
      await waitVersion(after.version);
      assert.equal(after.return_pending_replan,false);
      assert.equal(after.location_confirmation_pending,false);
      assert.equal(after.pending_reassignment_ids.length,0);
      assert.ok(after.tasks.length);
    };
    await approve('returned-unknown',555);

    const fixed = await openAbsence('fixed','09:15','12:00');
    assert.equal(fixed.active_absence.end_time,'12:00');
    const returned = await returnToWork('09:30');
    const completedPeriod = returned.unavailability.find(period => period.id === fixed.active_absence.id);
    assert.ok(completedPeriod.early_return);
    assert.equal(completedPeriod.start_time,'09:16');
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
    assert.match(await mobile.locator('#availability-summary').textContent(),/Предстоящие отсутствия/);
    assert.match(await mobile.locator('#availability-summary').textContent(),/[Тт]ранспорт/);
    assert.doesNotMatch(await mobile.locator('#availability-summary').textContent(),/Запланировано/);
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
    await mobile.setViewportSize({width:320,height:740});
    await noOverflow();
    await mobile.screenshot({path:path.join(tmp,'shift-small-mobile.png'),fullPage:true});
    assert.equal(await mobile.locator('#shift-history').count(),0);
    assert.equal(await mobile.locator('#history-dialog').count(),0);
    const preservedHistory = await employeeState();
    assert.ok(preservedHistory.facts.some(fact => fact.status === 'unavailable'));
    assert.ok(preservedHistory.unavailability.some(period => period.start_time === '11:00'));
    assert.ok(preservedHistory.unavailability.some(period => period.end_time === '09:30'));

    // A failed refresh retains the last plan and blocks sending changes until
    // the connection is restored. No successful action is implied while offline.
    const stateBeforeOffline = await employeeState();
    await mobileContext.route('**/api/**',route => route.abort('internetdisconnected'));
    await mobile.click('#refresh');
    await mobile.waitForFunction(() => document.querySelector('#connection').classList.contains('offline'));
    assert.equal(await mobile.locator('#shift-problem').isDisabled(),true);
    assert.match(await mobile.locator('#availability-summary').textContent(),/11:00/);
    await mobileContext.unroute('**/api/**');
    await mobile.click('#refresh');
    await mobile.waitForFunction(() => !document.querySelector('#connection').classList.contains('offline'));
    assert.equal(await mobile.locator('#shift-problem').isDisabled(),false);
    assert.deepEqual(await employeeState(),stateBeforeOffline);

    // Announcing a future absence must not interrupt work already in progress.
    await mobile.setViewportSize({width:390,height:844});
    await mobile.click('#tab-tasks');
    assert.equal(await mobile.locator('nav [aria-current="page"]').count(),1);
    assert.equal(await mobile.locator('nav [aria-current="page"]').getAttribute('id'),'tab-tasks');
    await mobile.click('button[aria-label^="Заявка a,"]');
    await mobile.locator('#task-actions').getByRole('button',{name:'В пути',exact:true}).click();
    await mobile.fill('#action-time','09:35');
    await mobile.selectOption('#action-location','office');
    await mobile.fill('#action-remaining','5');
    const traveling = await submit('#send-action','/api/engineer/facts');
    assert.equal(traveling.status.status,'traveling');
    await mobile.locator('#action-form').waitFor({state:'hidden'});
    await mobile.click('button[aria-label^="Заявка a,"]');
    await mobile.locator('#task-actions').getByRole('button',{name:'На месте · начать работу',exact:true}).click();
    await mobile.fill('#action-time','09:40');
    await mobile.fill('#action-remaining','30');
    const working = await submit('#send-action','/api/engineer/facts');
    assert.equal(working.status.status,'working');
    await mobile.locator('#action-form').waitFor({state:'hidden'});
    await mobile.click('#tab-shift');
    assert.match(await mobile.locator('#engineer-status').textContent(),/Работает/);
    assert.match(await mobile.locator('#shift-activity').textContent(),/В работе · № a/);
    await mobile.locator('#shift-activity').getByRole('button',{name:'Открыть заявку'}).click();
    assert.match(await mobile.locator('#dialog-title').textContent(),/Address a/);
    await mobile.click('#close-dialog');
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
    assert.equal(await mobile.locator('#unavailability-location').count(),0);
    assert.equal(absenceRequests.length,0);
    await noOverflow();
    const nowAbsent = await submit('#unavailability-submit','/api/engineer/unavailability');
    await mobile.locator('#availability-dialog').waitFor({state:'hidden'});
    assert.equal(absenceRequests.length,1);
    assert.equal(nowAbsent.active_absence.start_time,'09:41');
    assert.equal(nowAbsent.active_absence.end_time,'10:30');
    assert.equal(nowAbsent.active_absence.comment,'Ремонт автомобиля');
    assert.equal(await mobile.locator('#shift-return').isVisible(),true);
    await returnToWork('09:50');
    await approve('returned-interrupted',590);

    // The engineer can resume and finish work; completion is visible only after
    // dispatcher confirmation, while the unconfirmed result stays in history.
    await mobile.click('#tab-tasks');
    await mobile.click('button[aria-label^="Заявка a,"]');
    await mobile.locator('#task-actions').getByRole('button',{name:'Начать работу',exact:true}).click();
    assert.equal(await mobile.locator('#action-remaining').inputValue(),'20');
    await mobile.fill('#action-time','09:55');
    await mobile.fill('#action-remaining','20');
    const resumed = await submit('#send-action','/api/engineer/facts');
    assert.equal(resumed.status.status,'working');
    await mobile.locator('#action-form').waitFor({state:'hidden'});
    await mobile.click('button[aria-label^="Заявка a,"]');
    await mobile.locator('#task-actions').getByRole('button',{name:'Сообщить о выполнении',exact:true}).click();
    await mobile.fill('#action-time','10:15');
    await mobile.fill('#action-router','0');
    await mobile.fill('#action-tv','0');
    await mobile.fill('#action-comment','Работа завершена');
    const reported = await submit('#send-action','/api/engineer/facts');
    assert.equal(reported.tasks.find(task => task.id === 'a').status,'completion_pending');
    assert.equal(await mobile.locator('#done-count').textContent(),'0');
    const state = await (await adminContext.request.get(base+'/r/east/api/state')).json();
    const confirmation = await adminContext.request.post(base+'/r/east/api/completions/confirm',{
      data:{version:state.version,fact_id:reported.status.id}});
    assert.equal(confirmation.status(),200,await confirmation.text());
    await mobile.reload();
    await waitVersion((await employeeState()).version);
    await mobile.click('[data-filter="completed"]');
    assert.equal(await mobile.locator('#done-count').textContent(),'1');
    assert.equal(await mobile.locator('button[aria-label^="Заявка a,"]').count(),1);
    await mobile.click('button[aria-label^="Заявка a,"]');
    assert.match(await mobile.locator('#task-detail').textContent(),/Выполнена/);
    assert.equal(await mobile.locator('#task-actions button').count(),0);
    await noOverflow();
    assert.deepEqual(errors,[]);
    console.log('PASS: absence cancellation/validation, offline recovery, early return, dispatcher approval, travel/work/interruption/completion, persisted audit, 320/390px and desktop; '+tmp);
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
