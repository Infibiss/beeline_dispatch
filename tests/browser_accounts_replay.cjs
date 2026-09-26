// Real UI workflow: provision employee, isolate sessions, replay own GPS, revoke access.
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {password}=require('./browser_auth_helpers.cjs');
const root=path.resolve(__dirname,'..'),tmp=fs.mkdtempSync(path.join(os.tmpdir(),'accounts-replay-'));
const stop=path.join(tmp,'stop'),delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
  let server,browser;
  try {
    server=spawn(process.env.PYTHON_BIN || 'python',['-B','tests/gps_browser_server.py','data/demo-road/Восток.json',
      '--state',path.join(tmp,'state.json'),'--port','0','--time-limit','.1'],{
      cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],
      env:{...process.env,PYTHONUTF8:'1',PYTHONPATH:path.join(root,'src'),GPS_TEST_STOP_FILE:stop,FIELD_ROUTER_ADMIN_PASSWORD:password}});
    const base=await new Promise((resolve,reject)=>{
      let out='';const timer=setTimeout(()=>reject(Error('Startup timeout')),20000);
      server.on('error',reject);server.stderr.on('data',chunk=>process.stderr.write(chunk));
      server.stdout.on('data',chunk=>{out+=chunk;const match=out.match(/http:\/\/127\.0\.0\.1:\d+/);if(match){clearTimeout(timer);resolve(match[0]);}});
    });
    browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
    const admin=await browser.newContext({viewport:{width:390,height:844}});
    const employee=await browser.newContext({viewport:{width:390,height:844},permissions:['geolocation'],
      geolocation:{latitude:55.75,longitude:37.62,accuracy:10}});
    const errors=[];
    for(const context of [admin,employee]){
      await context.route('https://tile.openstreetmap.org/**',r=>r.fulfill({status:204}));
      context.on('page',page=>{
        page.on('pageerror',e=>errors.push(e.message));
        page.on('console',m=>{if(m.type()==='error' && /Content Security Policy|Refused to/.test(m.text()))errors.push(m.text());});
      });
    }
    const page=await admin.newPage();await page.goto(base+'/accounts');
    await page.waitForURL('**/login');
    await page.fill('#username','admin');await page.fill('#password',password);await page.click('#login-submit');
    await page.waitForSelector('#sign-out');
    await page.getByRole('link',{name:'Доступ сотрудников'}).click();
    await page.waitForURL('**/accounts');
    await page.waitForFunction(()=>document.querySelector('#account-engineer').options.length>0);
    const eid=await page.locator('#account-engineer').inputValue();
    await page.fill('#account-login','worker-test');await page.fill('#account-password','employee-test-password-2026');
    await page.click('#account-create');
    await page.waitForFunction(()=>document.querySelector('#account-message').textContent.includes('создана'));
    const mobile=await employee.newPage();await mobile.goto(base+'/login');
    await mobile.fill('#username','worker-test');await mobile.fill('#password','employee-test-password-2026');await mobile.click('#login-submit');
    await mobile.waitForURL('**/r/east/engineer');
    await mobile.waitForFunction(()=>document.querySelector('#personal-name')?.textContent.length>0);
    assert.equal(await mobile.locator('#engineer-select').isVisible(),false);
    assert.equal(await mobile.locator('.dispatcher-link').isVisible(),false);
    assert.equal(await mobile.getByRole('link',{name:'Доступ сотрудников'}).count(),0);
    assert.equal(await mobile.locator('#engineer-select option').count(),1);
    assert.equal((await employee.request.get(base+'/api/state')).status(),403);
    assert.equal((await employee.request.get(base+'/r/south-east/api/engineers')).status(),403);
    await mobile.click('#tab-shift');await mobile.click('#gps-start');
    await mobile.waitForFunction(()=>document.querySelector('#gps-sent').textContent!=='Ещё нет');
    const before=await (await admin.request.get(base+'/api/state')).json();
    await page.goto(base);await page.click('#tab-gps');
    await page.selectOption('#gps-channel','demo');
    await page.locator('#gps-own-data summary').click();
    await page.waitForFunction(()=>document.querySelector('#gps-replay-task').options.length>0);
    await page.click('#gps-track-sample');
    const sample=await page.locator('#gps-track-text').inputValue();
    // Exercise file picker with exactly the documented CSV format.
    await page.setInputFiles('#gps-track-file',{name:'my-track.csv',mimeType:'text/csv',buffer:Buffer.from(sample)});
    await page.click('#gps-replay-run');
    await page.waitForFunction(()=>document.querySelector('#gps-replay-status').textContent.includes('Принято: 3'));
    await page.waitForFunction(id=>[...document.querySelectorAll('[data-engineer-id]')].find(e=>e.dataset.engineerId===id)?.textContent.includes('Инженер рядом с адресом'),eid,{timeout:20000});
    const after=await (await admin.request.get(base+'/api/state')).json();assert.deepEqual(after,before);
    const real=await (await admin.request.get(base+'/api/gps?channel=device')).json();
    assert.equal(real.rows.find(r=>r.engineer_id===eid).tracking_state,'active');
    // A manual fact increments the data version, without rerouting the selected job.
    const own=await(await employee.request.get(base+'/r/east/api/engineer?engineer_id='+encodeURIComponent(eid))).json();
    const identity=await(await employee.request.get(base+'/api/auth/me')).json();
    const typedTime=await page.inputValue('#gps-replay-time');
    const target=await page.inputValue('#gps-replay-task');
    const fact=await employee.request.post(base+'/r/east/api/engineer/facts',{
      headers:{'X-CSRF-Token':identity.csrf},
      data:{version:own.version,engineer_id:eid,status:'available',time:Math.max(own.time,own.engineer.shift.start),
        location_id:own.engineer.start_location,comment:'Browser test: confirmed shift'}
    });
    assert.equal(fact.status(),200,await fact.text());
    const withFact=await(await admin.request.get(base+'/api/state')).json();
    await page.click('#gps-replay-run');
    await page.locator('#gps-replay-changes').waitFor({state:'visible'});
    assert.match(await page.locator('#gps-replay-changes').textContent(),/Фактический статус/);
    assert.match(await page.locator('#gps-replay-changes').textContent(),/не изменились/);
    for(const width of [320,390,768]){
      await page.setViewportSize({width,height:844});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Plan comparison overflow '+width);
    }
    await page.locator('#gps-replay-changes').screenshot({path:path.join(tmp,'plan-comparison.png')});
    assert.equal(await page.inputValue('#gps-track-text'),sample);
    assert.equal(await page.inputValue('#gps-replay-time'),typedTime);
    assert.equal(await page.inputValue('#gps-replay-task'),target);
    await page.click('#gps-replay-run');
    await page.waitForFunction(()=>document.querySelector('#gps-replay-status').textContent.includes('Принято: 3'));
    assert.equal(await page.locator('#gps-replay-changes').isVisible(),false);
    assert.deepEqual(await(await admin.request.get(base+'/api/state')).json(),withFact);
    await mobile.click('#sign-out');await mobile.waitForURL('**/login');
    const stopped=await (await admin.request.get(base+'/api/gps?channel=device')).json();
    assert.equal(stopped.rows.find(r=>r.engineer_id===eid).tracking_state,'off');
    await mobile.fill('#username','worker-test');await mobile.fill('#password','employee-test-password-2026');await mobile.click('#login-submit');
    await mobile.waitForURL('**/r/east/engineer');await mobile.waitForSelector('#personal-name');
    await page.screenshot({path:path.join(tmp,'gps-test.png'),fullPage:true});
    for(const width of [320,390,768]){
      for(const active of [page,mobile]){
        await active.setViewportSize({width,height:844});
        assert.ok(await active.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Horizontal overflow at '+width);
      }
    }
    await mobile.screenshot({path:path.join(tmp,'employee.png'),fullPage:true});
    await page.goto(base+'/accounts');
    const card=page.locator('.account-card').filter({hasText:'worker-test'});
    await card.getByRole('button',{name:'Заблокировать',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#account-message').textContent.includes('обновлён'));
    assert.equal((await employee.request.get(base+'/api/auth/me')).status(),401);
    await mobile.goto(base+'/engineer');await mobile.waitForURL('**/login');
    for(const width of [320,390,768]){
      await mobile.setViewportSize({width,height:844});await page.setViewportSize({width,height:844});
      assert.ok(await mobile.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    }
    await mobile.screenshot({path:path.join(tmp,'login.png'),fullPage:true});
    assert.deepEqual(errors,[]);console.log('PASS accounts + personal mobile + GPS replay + explained version conflict preserving input + revocation + responsive UI');
    console.log('Screenshots: '+tmp);
  } finally {
    if(browser)await browser.close();
    if(server){fs.writeFileSync(stop,'stop');for(let n=0;n<60 && server.exitCode===null;n++)await delay(100);if(server.exitCode===null)server.kill();}
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
