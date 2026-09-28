// Real employee/dispatcher HTTP flows; generated accounts and coordinates only.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {login,password} = require('./browser_auth_helpers.cjs');
const root = path.resolve(__dirname,'..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(),'engineer-absence-edit-'));

(async () => {
  let server,browser;
  try {
    server = spawn(process.env.PYTHON_BIN || 'python',
      ['-B','tests/engineer_return_browser_server.py',tmp],{
        cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],
        env:{...process.env,PYTHONUTF8:'1',PYTHONPATH:path.join(root,'src'),
          FIELD_ROUTER_ADMIN_PASSWORD:password}});
    const base = await new Promise((resolve,reject) => {
      let output = '';
      const timer = setTimeout(() => reject(Error('Server startup timeout')),20000);
      server.once('error',error => {clearTimeout(timer);reject(error);});
      server.once('exit',code => {clearTimeout(timer);reject(Error('Server exited: '+code));});
      server.stderr.on('data',data => process.stderr.write(data));
      server.stdout.on('data',data => {
        output += data;
        const match = output.match(/http:\/\/127\.0\.0\.1:\d+/);
        if (match) {clearTimeout(timer);resolve(match[0]);}
      });
    });
    browser = await chromium.launch({headless:true,
      executablePath:process.env.BROWSER_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
    const employee = await browser.newContext({viewport:{width:390,height:844}});
    const admin = await browser.newContext({viewport:{width:1365,height:900}});
    const page = await employee.newPage();
    const errors = [];
    page.on('pageerror',error => errors.push(error.message));
    const state = async () => {
      const response = await employee.request.get(base+'/r/east/api/engineer?engineer_id=e1');
      assert.equal(response.status(),200);
      return response.json();
    };
    const dispatchState = async () => {
      const response = await admin.request.get(base+'/r/east/api/state');
      assert.equal(response.status(),200);
      return response.json();
    };
    const waitVersion = version => page.waitForFunction(expected =>
      document.querySelector('#plan-version')?.textContent.trim().endsWith(String(expected)),version);
    const noOverflow = async () => assert.equal(await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth),true);
    const post = async (button,endpoint) => {
      const pending = page.waitForResponse(response => response.url().endsWith(endpoint)
        && response.request().method() === 'POST');
      await page.click(button);
      const response = await pending;
      const result = await response.json();
      assert.equal(response.status(),200,JSON.stringify(result));
      return result;
    };
    const refresh = async () => {
      const pending = page.waitForResponse(response => response.url().includes('/api/engineer?'));
      await page.click('#refresh');
      await pending;
      await waitVersion((await state()).version);
    };
    const preview = async (id,eventTime) => {
      const current = await dispatchState();
      const response = await admin.request.post(base+'/r/east/api/preview',{data:{
        version:current.version,event:{id,type:'replan',time:eventTime ?? current.time}}});
      assert.equal(response.status(),200,await response.text());
      return {version:current.version,batch:await response.json()};
    };
    const approve = async proposal => {
      const response = await admin.request.post(base+'/r/east/api/approve',{data:{
        version:proposal.version,batch_id:proposal.batch.id,candidate_id:'stable'}});
      assert.equal(response.status(),200,await response.text());
      await refresh();
      return state();
    };
    const report = async (later,start,end,reason='sick') => {
      await page.click('#shift-problem');
      await page.selectOption('#unavailability-start-mode',later?'later':'now');
      if (later) {
        await page.fill('#unavailability-start-date','2026-08-17');
        await page.fill('#unavailability-start-time',start);
      }
      await page.fill('#unavailability-end-date','2026-08-17');
      await page.fill('#unavailability-end-time',end);
      await page.selectOption('#unavailability-reason',reason);
      assert.equal(await page.locator('#unavailability-location').count(),0);
      await noOverflow();
      const result = await post('#unavailability-submit','/api/engineer/unavailability');
      await page.locator('#availability-dialog').waitFor({state:'hidden'});
      return result;
    };
    const openCancel = async id => {
      await page.locator('[data-absence-cancel="'+id+'"]').click();
      await page.locator('#absence-cancel-dialog').waitFor({state:'visible'});
    };

    await page.goto(base+'/login');
    await page.fill('#username','worker');
    await page.fill('#password',password);
    await page.click('#login-submit');
    await page.waitForURL('**/engineer');
    await waitVersion(2);
    await page.click('#tab-shift');
    await login(admin,base);
    assert.equal(await page.locator('#shift-history').count(),0);
    assert.equal(await page.locator('#history-dialog').count(),0);
    assert.equal(await page.locator('#availability-state').count(),0);
    assert.equal(await page.locator('#shift-start-location').count(),0);

    // Use the displayed plan clock. "Now" must never silently become a future period.
    await page.click('#shift-start');
    await page.fill('#action-time','09:01');
    await page.selectOption('#action-location','office');
    await post('#send-action','/api/engineer/facts');
    await page.locator('#action-form').waitFor({state:'hidden'});
    assert.match(await page.locator('#shift-plan-context').textContent(),/09:01/);
    assert.match(await page.locator('#shift-plan-context').textContent(),/[Вв]ремя сценария/);
    const immediate = await report(false,null,'12:00');
    assert.equal(immediate.time,541);
    assert.equal(immediate.active_absence.start_time,'09:01');
    assert.equal(await page.locator('#engineer-status').textContent(),'Недоступен');
    assert.equal(await page.locator('#shift-return').isVisible(),true);
    const immediateId = immediate.active_absence.id;
    assert.equal(await page.locator('[data-absence-cancel="'+immediateId+'"]').count(),1);
    await page.locator('[data-absence-edit="'+immediateId+'"]').click();
    assert.equal(await page.locator('#unavailability-start-mode').isDisabled(),true);
    await page.fill('#unavailability-end-time','12:30');
    const extended = await post('#unavailability-submit','/api/engineer/unavailability/update');
    await page.locator('#availability-dialog').waitFor({state:'hidden'});
    assert.equal(extended.active_absence.id,immediateId);
    assert.equal(extended.active_absence.start_time,'09:01');
    assert.equal(extended.active_absence.end_time,'12:30');
    assert.ok(extended.active_absence.revisions.length);
    await page.screenshot({path:path.join(tmp,'absence-now-mobile.png'),fullPage:true});
    const beforeCancel = await state();
    await openCancel(immediateId);
    await page.click('#close-absence-cancel');
    assert.deepEqual(await state(),beforeCancel);
    await openCancel(immediateId);
    await page.keyboard.press('Escape');
    assert.deepEqual(await state(),beforeCancel);

    // The same employee report is delivered to dispatch and constrains the solver.
    const absentDispatch = await dispatchState();
    assert.ok(absentDispatch.dispatch_notifications.length);
    assert.ok(absentDispatch.unavailability.some(period => period.id === immediateId
      && period.engineer_id === 'e1' && period.end_time === '12:30'));
    const blockedProposal = await preview('test-current-absence');
    for (const candidate of blockedProposal.batch.candidates) {
      for (const route of candidate.plan.routes.filter(route => route.engineer_id === 'e1'))
        assert.ok(route.visits.every(visit => visit.start >= 750));
    }
    await approve(blockedProposal);
    assert.equal(await page.locator('#engineer-status').textContent(),'Недоступен');
    const afterAbsenceApproved = await state();
    assert.equal(afterAbsenceApproved.active_absence.id,immediateId);

    // A mistaken report can be cancelled after approval, but assignments still
    // have to pass through dispatcher preview/approval before the engineer works.
    await openCancel(immediateId);
    await page.selectOption('#absence-cancel-reason','Отправлено по ошибке');
    assert.equal(await page.locator('#absence-cancel-location').count(),0);
    await noOverflow();
    await page.screenshot({path:path.join(tmp,'cancel-absence-mobile.png'),fullPage:true});
    const cancelled = await post('#confirm-absence-cancel','/api/engineer/unavailability/cancel');
    await page.locator('#absence-cancel-dialog').waitFor({state:'hidden'});
    assert.equal(cancelled.active_absence,null);
    assert.ok(cancelled.unavailability.find(period => period.id === immediateId).revoked);
    assert.equal(cancelled.return_pending_replan,true);
    assert.equal(cancelled.location_confirmation_pending,true);
    assert.equal(cancelled.status.location_id,null);
    assert.match(await page.locator('#engineer-status').textContent(),/Ожидаем уточнения точки/);
    assert.equal(await page.locator('#shift-return').isVisible(),false);
    const cancelledDispatch = await dispatchState();
    assert.ok(cancelledDispatch.dispatch_notifications.length > absentDispatch.dispatch_notifications.length);
    assert.ok(cancelledDispatch.dispatch_notifications.some(note => note.report_id === immediateId
      && note.title.includes('отменил')));
    const recoveredProposal = await preview('test-cancelled-absence');
    const recoveredStable = recoveredProposal.batch.candidates.find(candidate => candidate.id === 'stable');
    assert.ok(recoveredStable.plan.routes.filter(route=>route.engineer_id==='e1')
      .every(route=>route.visits.length===0));
    const stillHeld = await approve(recoveredProposal);
    assert.equal(stillHeld.location_confirmation_pending,true);
    const confirmedLocation = await admin.request.post(base+'/r/east/api/facts',{data:{
      version:stillHeld.version,engineer_id:'e1',status:'available',time:stillHeld.time+1,
      location_id:'office',comment:'Местоположение подтверждено диспетчером'}});
    assert.equal(confirmedLocation.status(),200,await confirmedLocation.text());
    const recovered = await approve(await preview('test-location-confirmed'));
    assert.equal(recovered.return_pending_replan,false);
    assert.equal(recovered.location_confirmation_pending,false);
    assert.equal(recovered.tasks.length,2);

    // Future absence is distinguished from being unavailable now. Editing an
    // existing period must replace that period, rather than add another block.
    const future = await report(true,'13:00','14:00','transport');
    const futureId = future.scheduled_absences.find(period => period.start_time === '13:00').id;
    assert.equal(future.active_absence,null);
    assert.equal(await page.locator('#engineer-status').textContent(),'На смене');
    await page.locator('[data-absence-edit="'+futureId+'"]').click();
    assert.equal(await page.locator('#unavailability-start-time').inputValue(),'13:00');
    assert.equal(await page.locator('#unavailability-end-time').inputValue(),'14:00');
    assert.equal(await page.locator('#unavailability-reason').inputValue(),'transport');
    await page.setViewportSize({width:320,height:740});
    await noOverflow();
    await page.screenshot({path:path.join(tmp,'edit-absence-small-mobile.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    const beforeEdit = await state();
    await page.fill('#unavailability-end-time','12:00');
    await page.click('#unavailability-submit');
    await page.locator('#unavailability-error').waitFor({state:'visible'});
    assert.deepEqual(await state(),beforeEdit);
    await page.fill('#unavailability-start-time','14:00');
    await page.fill('#unavailability-end-time','15:00');
    await page.selectOption('#unavailability-reason','personal');
    const edited = await post('#unavailability-submit','/api/engineer/unavailability/update');
    await page.locator('#availability-dialog').waitFor({state:'hidden'});
    assert.equal(edited.scheduled_absences.length,1);
    const amended = edited.scheduled_absences[0];
    assert.equal(amended.start_time,'14:00');
    assert.equal(amended.end_time,'15:00');
    assert.equal(amended.reason,'personal');
    assert.equal(await page.locator('#engineer-status').textContent(),'На смене');
    await page.reload();
    await waitVersion(edited.version);
    await page.click('#tab-shift');
    assert.match(await page.locator('#availability-summary').textContent(),/14:00.*15:00/);
    assert.match(await page.locator('#availability-summary').textContent(),/Личные обстоятельства/);
    for (const [width,height] of [[320,740],[1365,900]]) {
      await page.setViewportSize({width,height});
      await noOverflow();
      await page.screenshot({path:path.join(tmp,'edited-absence-'+width+'.png'),fullPage:true});
    }
    await page.setViewportSize({width:390,height:844});
    await openCancel(amended.id);
    assert.equal(await page.locator('#absence-cancel-location').count(),0);
    await page.selectOption('#absence-cancel-reason','Планы изменились');
    const noFuture = await post('#confirm-absence-cancel','/api/engineer/unavailability/cancel');
    await page.locator('#absence-cancel-dialog').waitFor({state:'hidden'});
    assert.equal(noFuture.scheduled_absences.length,0);
    assert.equal(noFuture.active_absence,null);
    assert.equal(await page.locator('[data-absence-edit]').count(),0);
    assert.equal(await page.locator('[data-absence-cancel]').count(),0);
    assert.ok(noFuture.unavailability.some(period => period.revoked));
    assert.equal(await page.locator('#history-dialog').count(),0);

    // Moving a future absence to "Now" uses the plan clock on the server and
    // updates the visible availability immediately, without dispatcher approval.
    const initiallyLater = await report(true,'16:00','17:00','personal');
    const movedId = initiallyLater.scheduled_absences[0].id;
    await page.locator('[data-absence-edit="'+movedId+'"]').click();
    await page.selectOption('#unavailability-start-mode','now');
    await page.fill('#unavailability-end-time','10:30');
    assert.equal(await page.locator('#unavailability-location').count(),0);
    const movedToNow = await post('#unavailability-submit','/api/engineer/unavailability/update');
    await page.locator('#availability-dialog').waitFor({state:'hidden'});
    assert.equal(movedToNow.active_absence.id,movedId);
    assert.equal(movedToNow.active_absence.start_time,'09:02');
    assert.equal(await page.locator('#engineer-status').textContent(),'Недоступен');
    assert.equal(await page.locator('#shift-return').isVisible(),true);
    await page.reload();
    await waitVersion(movedToNow.version);
    await page.click('#tab-shift');
    assert.equal(await page.locator('#engineer-status').textContent(),'Недоступен');

    // A future report does not know the engineer's later location. Once its
    // start arrives, editing the end/reason must preserve that unknown location.
    await openCancel(movedId);
    await page.selectOption('#absence-cancel-reason','Отправлено по ошибке');
    assert.equal(await page.locator('#absence-cancel-location').count(),0);
    await post('#confirm-absence-cancel','/api/engineer/unavailability/cancel');
    await page.locator('#absence-cancel-dialog').waitFor({state:'hidden'});
    const pendingLater = await report(true,'10:00','11:00','transport');
    const laterId = pendingLater.scheduled_absences[0].id;
    assert.equal(pendingLater.scheduled_absences[0].location_id,null);
    const clockAdvanced = await approve(await preview('test-future-absence-starts',600));
    assert.equal(clockAdvanced.active_absence.id,laterId);
    assert.equal(clockAdvanced.active_absence.location_id,null);
    assert.equal(await page.locator('#engineer-status').textContent(),'Недоступен');
    await page.locator('[data-absence-edit="'+laterId+'"]').click();
    assert.equal(await page.locator('#unavailability-start-mode').isDisabled(),true);
    assert.equal(await page.locator('#unavailability-location').count(),0);
    await page.fill('#unavailability-end-time','11:30');
    await page.selectOption('#unavailability-reason','personal');
    const editedActive = await post('#unavailability-submit','/api/engineer/unavailability/update');
    await page.locator('#availability-dialog').waitFor({state:'hidden'});
    assert.equal(editedActive.active_absence.start_time,'10:00');
    assert.equal(editedActive.active_absence.end_time,'11:30');
    assert.equal(editedActive.active_absence.reason,'personal');
    assert.equal(editedActive.active_absence.location_id,null);

    // A concurrent update must not be overwritten by an already-open cancel
    // dialog. Hold polling at the old version to exercise the HTTP 409 path.
    await openCancel(laterId);
    await page.selectOption('#absence-cancel-reason','Планы изменились');
    assert.equal(await page.locator('#absence-cancel-location').count(),0);
    await employee.route('**/api/revision',route => route.fulfill({json:{version:editedActive.version}}));
    const concurrent = await admin.request.post(base+'/r/east/api/engineer/unavailability/update',{data:{
      version:editedActive.version,engineer_id:'e1',report_id:laterId,
      start_date:'2026-08-17',start_time:'10:00',end_date:'2026-08-17',end_time:'11:45',
      reason:'personal',comment:'Время изменилось'}});
    assert.equal(concurrent.status(),200,await concurrent.text());
    const stateAfterConcurrent = await state();
    const staleResponse = page.waitForResponse(response => response.url().endsWith('/api/engineer/unavailability/cancel')
      && response.request().method() === 'POST');
    await page.click('#confirm-absence-cancel');
    assert.equal((await staleResponse).status(),409);
    await page.locator('#absence-cancel-error').waitFor({state:'visible'});
    assert.equal(await page.locator('#confirm-absence-cancel').isDisabled(),true);
    assert.match(await page.locator('#absence-cancel-error').textContent(),/Закройте форму/);
    assert.deepEqual(await state(),stateAfterConcurrent);
    await page.screenshot({path:path.join(tmp,'cancel-stale-absence-mobile.png'),fullPage:true});
    await page.click('#close-absence-cancel');
    await employee.unroute('**/api/revision');
    await refresh();
    assert.equal((await state()).active_absence.end_time,'11:45');
    await noOverflow();
    assert.deepEqual(errors,[]);
    console.log('PASS: now/future labels, editing unknown-location active absence, cancel confirmation and stale-version protection, dispatcher constraints and recovery, 320/390px and desktop; '+tmp);
  } finally {
    if (browser) await browser.close();
    fs.writeFileSync(path.join(tmp,'stop'),'stop');
    if (server && server.exitCode === null) {
      await new Promise(resolve => {
        const timer = setTimeout(() => {server.kill();resolve();},5000);
        server.once('exit',() => {clearTimeout(timer);resolve();});
      });
    }
  }
})().catch(error => {console.error(error);process.exitCode=1;});
