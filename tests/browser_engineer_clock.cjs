// Deterministic live Moscow minute changes through real employee/server polling.
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {password}=require('./browser_auth_helpers.cjs');
const root=path.resolve(__dirname,'..');
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'engineer-clock-'));

(async()=>{
  let server,browser;
  try {
    server=spawn(process.env.PYTHON_BIN||'python',['-B','tests/engineer_clock_browser_server.py',tmp],{
      cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env:{...process.env,
        PYTHONUTF8:'1',PYTHONPATH:path.join(root,'src'),FIELD_ROUTER_ADMIN_PASSWORD:password}});
    const base=await new Promise((resolve,reject)=>{
      let output='';const timeout=setTimeout(()=>reject(Error('Server startup timeout')),20000);
      server.once('error',e=>{clearTimeout(timeout);reject(e);});
      server.once('exit',code=>{clearTimeout(timeout);reject(Error('Server exited: '+code));});
      server.stderr.on('data',data=>process.stderr.write(data));
      server.stdout.on('data',data=>{output+=data;const match=output.match(/http:\/\/127\.0\.0\.1:\d+/);
        if(match){clearTimeout(timeout);resolve(match[0]);}});
    });
    browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_BIN||
      'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
    const context=await browser.newContext({viewport:{width:390,height:844}});
    const auth=await context.request.post(base+'/api/auth/login',{data:{username:'worker',password}});
    assert.equal(auth.status(),200);
    const page=await context.newPage(),errors=[],posts=[];
    page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>{
      if(request.method()==='POST'&&request.url().includes('/api/engineer/'))posts.push(request);
    });
    const state=async()=>{
      const response=await context.request.get(base+'/r/east/api/engineer?engineer_id=e1');
      assert.equal(response.status(),200);return response.json();
    };
    await page.goto(base+(await auth.json()).redirect);
    await page.click('#tab-shift');
    const first=await state();
    assert.equal(first.clock.mode,'live');assert.equal(first.time,540);
    assert.equal(first.plan_time,540);
    assert.doesNotMatch(await page.locator('#shift-plan-context').textContent(),/Время сценария/);
    assert.equal(await page.locator('#availability-state').count(),0);
    assert.equal(await page.locator('#shift-start-location').count(),0);
    await page.click('#shift-problem');
    await page.selectOption('#unavailability-start-mode','later');
    await page.fill('#unavailability-start-date',first.date);
    await page.fill('#unavailability-start-time','09:01');
    await page.fill('#unavailability-end-date',first.date);
    await page.fill('#unavailability-end-time','09:03');
    await page.selectOption('#unavailability-reason','sick');
    assert.equal(await page.locator('label[for="unavailability-start-time"]').textContent(),'Со времени');
    assert.equal(await page.locator('label[for="unavailability-end-time"]').textContent(),'До времени');
    assert.equal(await page.locator('#unavailability-location').count(),0);
    const posted=page.waitForResponse(response=>response.url().endsWith('/api/engineer/unavailability')
      &&response.request().method()==='POST');
    await page.click('#unavailability-submit');
    const reported=await posted;assert.equal(reported.status(),200,await reported.text());
    const future=await reported.json();
    assert.equal(future.active_absence,null);assert.equal(future.scheduled_absences.length,1);
    await page.locator('#availability-dialog').waitFor({state:'hidden'});
    // A future period becomes current while its edit form is open. Preserve
    // the local draft, disable its stale submission, and never auto-submit it.
    const periodId=future.scheduled_absences[0].id;
    await page.locator('[data-absence-edit="'+periodId+'"]').click();
    const draftComment='Неотправленное пояснение инженера';
    await page.fill('#unavailability-comment',draftComment);
    const postsBeforeBoundary=posts.length;
    fs.writeFileSync(path.join(tmp,'clock-minute'),'541');
    await page.locator('#unavailability-error').waitFor({state:'visible',timeout:30000});
    assert.match(await page.locator('#unavailability-error').textContent(),/Период уже начался или закончился/);
    assert.match(await page.locator('#unavailability-error').textContent(),/Закройте форму/);
    assert.equal(await page.locator('#unavailability-submit').isDisabled(),true);
    assert.equal(await page.locator('#unavailability-comment').inputValue(),draftComment);
    assert.equal(posts.length,postsBeforeBoundary);
    const boundary=await state();
    assert.equal(boundary.version,future.version);
    assert.equal(boundary.active_absence.comment,future.scheduled_absences[0].comment);
    await page.screenshot({path:path.join(tmp,'live-open-form-boundary.png'),fullPage:true});
    await page.click('#close-availability');
    await page.reload();
    await page.click('#tab-shift');
    // No plan publication takes place: the revision clock drives status updates.
    await page.waitForFunction(()=>document.querySelector('#engineer-status').textContent==='Недоступен',null,{timeout:30000});
    const active=await state();
    assert.equal(active.version,future.version);assert.equal(active.plan_time,540);
    assert.equal(active.time,541);assert.equal(active.active_absence.start_time,'09:01');
    assert.equal(await page.locator('#shift-return').isVisible(),true);
    await page.screenshot({path:path.join(tmp,'live-absence-active.png'),fullPage:true});
    fs.writeFileSync(path.join(tmp,'clock-minute'),'543');
    await page.waitForFunction(()=>document.querySelector('#shift-plan-context').textContent.includes('09:03'),null,{timeout:30000});
    const ended=await state();
    assert.equal(ended.version,future.version);assert.equal(ended.active_absence,null);
    assert.equal(ended.return_confirmation_required,true);
    assert.equal(await page.locator('#shift-return').isVisible(),true);
    await page.click('#shift-return');
    assert.equal(await page.locator('#action-location').isVisible(),false);
    assert.equal(await page.locator('#action-time').inputValue(),'09:03');
    const returning=page.waitForResponse(response=>response.url().endsWith('/api/engineer/return')
      &&response.request().method()==='POST');
    await page.click('#send-action');
    const returned=await returning;assert.equal(returned.status(),200,await returned.text());
    const result=await returned.json();
    assert.equal(result.time,543);assert.equal(result.status.location_id,null);
    assert.equal(result.location_confirmation_pending,true);
    assert.equal(result.return_confirmation_required,false);
    await page.locator('#action-form').waitFor({state:'hidden'});
    await page.screenshot({path:path.join(tmp,'live-return-location-pending.png'),fullPage:true});
    // "Now" follows the live minute even when the saved plan still says 09:03.
    fs.writeFileSync(path.join(tmp,'clock-minute'),'544');
    await page.waitForFunction(()=>document.querySelector('#shift-plan-context').textContent.includes('09:04'),null,{timeout:30000});
    const beforeNow=await state();
    assert.equal(beforeNow.plan_time,543);assert.equal(beforeNow.time,544);
    await page.click('#shift-problem');
    assert.equal(await page.locator('#unavailability-start-mode').inputValue(),'now');
    assert.equal(await page.locator('#unavailability-start-mode option[value="now"]').textContent(),'Сейчас · 09:04');
    assert.match(await page.locator('#absence-day').textContent(),/09:04 МСК/);
    await page.fill('#unavailability-end-time','09:20');
    await page.selectOption('#unavailability-reason','personal');
    const nowPosted=page.waitForResponse(response=>response.url().endsWith('/api/engineer/unavailability')
      &&response.request().method()==='POST');
    await page.click('#unavailability-submit');
    const nowResponse=await nowPosted;assert.equal(nowResponse.status(),200,await nowResponse.text());
    const immediate=await nowResponse.json();
    assert.equal(immediate.active_absence.start_time,'09:04');
    assert.equal(immediate.active_absence.start_date,first.date);
    await page.locator('#availability-dialog').waitFor({state:'hidden'});
    assert.equal(await page.locator('#engineer-status').textContent(),'Недоступен');
    assert.ok(posts.length>=3);
    for(const request of posts)
      assert.equal(request.headers()['x-engineer-clock'],'live/'+first.date+'/'+first.date);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.deepEqual(errors,[]);
    console.log('PASS: live Moscow clock updates absence start/end without plan version changes; stale open edit keeps unsent draft; current-minute Now and clock headers; return has no invented location; '+tmp);
  } finally {
    if(browser)await browser.close();
    fs.writeFileSync(path.join(tmp,'stop'),'stop');
    if(server&&server.exitCode===null)await new Promise(resolve=>{
      const timeout=setTimeout(()=>{server.kill();resolve();},5000);
      server.once('exit',()=>{clearTimeout(timeout);resolve();});
    });
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
