// GPS -> one/group exclusion -> preview -> approval -> mobile + region isolation.
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {login,password}=require('./browser_auth_helpers.cjs');
const root=path.resolve(__dirname,'..'),tmp=fs.mkdtempSync(path.join(os.tmpdir(),'gps-exclusion-'));
const stop=path.join(tmp,'stop'),delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
  let server,browser;
  try {
    server=spawn(process.env.PYTHON_BIN || 'python',['-B','tests/gps_browser_server.py','data/demo-road/Восток.json',
      '--state',path.join(tmp,'state.json'),'--port','0','--time-limit','.1'],{
      cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env:{...process.env,PYTHONUTF8:'1',
      PYTHONPATH:path.join(root,'src'),GPS_TEST_STOP_FILE:stop,FIELD_ROUTER_ADMIN_PASSWORD:password}});
    const base=await new Promise((resolve,reject)=>{
      let out='';const timer=setTimeout(()=>reject(Error('Startup timeout')),20000);
      server.on('error',reject);server.stderr.on('data',chunk=>process.stderr.write(chunk));
      server.stdout.on('data',chunk=>{out+=chunk;const match=out.match(/http:\/\/127\.0\.0\.1:\d+/);if(match){clearTimeout(timer);resolve(match[0]);}});
    });
    browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
    const context=await browser.newContext({viewport:{width:390,height:844}});
    await login(context,base);
    await context.route('https://tile.openstreetmap.org/**',r=>r.fulfill({status:204}));
    const errors=[];context.on('page',page=>{
      page.on('pageerror',error=>errors.push(error.message));
      page.on('console',message=>{if(message.type()==='error' && /Content Security Policy|Refused to/.test(message.text()))errors.push(message.text());});
    });
    const page=await context.newPage();await page.goto(base);await page.click('#tab-gps');
    await page.waitForSelector('[data-exclude-engineer]');
    const get=async path=>(await context.request.get(base+path)).json();
    const before=await get('/api/state'),ids=before.engineers.slice(0,2).map(e=>e.id);
    await page.selectOption('#gps-channel','demo');
    await page.selectOption('#gps-demo-engineer',ids[0]);await page.selectOption('#gps-demo-case','stopped');
    await page.click('#gps-demo-run');
    await page.waitForFunction(()=>document.querySelector('#gps-demo-status').textContent.includes('Пример добавлен'));
    // GPS refresh replaces cards. Resolve a live control and use a real checked
    // action; evaluateAll().click() could target an already detached element.
    for(const id of ids)await page.locator('[data-exclude-engineer='+JSON.stringify(id)+']').check();
    assert.equal(await page.locator('#gps-exclusion-run').textContent(),'Пересчитать без выбранных (2)');
    // Force focus out of cards; two refreshes must preserve the checked group.
    await page.locator('#gps-exclusion-time').focus();await delay(5500);
    assert.equal(await page.locator('[data-exclude-engineer]:checked').count(),2);
    for(const width of [320,390,768]){
      await page.setViewportSize({width,height:844});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    }
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(tmp,'gps-selection.png'),fullPage:true});
    async function preview(action) {
      const pending=page.waitForResponse(response=>response.url().endsWith('/api/preview') && response.request().method()==='POST');
      await action();const response=await pending;
      const body=await response.json();assert.equal(response.status(),200,JSON.stringify(body));
      await page.waitForSelector('.candidate');return body;
    }
    const batch=await preview(()=>page.click('#gps-exclusion-run'));
    assert.deepEqual(new Set(batch.event.engineer_ids),new Set(ids));
    assert.equal(await page.locator('#panel-events').isVisible(),true);
    assert.equal(await page.locator('#event-engineers input:checked').count(),2);
    assert.equal(await page.locator('#excluded-summary').isVisible(),true);
    for(const candidate of batch.candidates)
      assert.ok(!candidate.plan.routes.some(route=>ids.includes(route.engineer_id) && route.visits.length));
    assert.deepEqual(await get('/api/state'),before,'Preview must not publish');
    await page.locator('.candidate[data-id="stable"]').getByRole('button',{name:'Посмотреть изменения',exact:true}).click();
    await page.click('#approve');await page.waitForFunction(()=>document.querySelector('#version').textContent==='2');
    const applied=await get('/api/state');
    assert.deepEqual(new Set(applied.excluded_engineer_ids),new Set(ids));
    assert.equal(applied.engineers.length,before.engineers.length);
    assert.equal((await get('/api/engineers')).engineers.length,before.engineers.length);
    for(const id of ids)assert.equal((await get('/api/engineer?engineer_id='+encodeURIComponent(id))).tasks.length,0);
    assert.equal((await get('/r/south-east/api/state')).excluded_engineer_ids.length,0);
    await page.click('#tab-gps');
    await page.waitForFunction(()=>document.querySelector('#gps-excluded-now').hidden===false);
    assert.equal(await page.locator('#gps-exclusion-run').textContent(),'Пересчитать без выбранных (0)');
    for(const id of ids)assert.equal(await page.locator('[data-exclude-one]').evaluateAll((buttons,id)=>buttons.find(b=>b.dataset.excludeOne===id).disabled,id),true);
    // Direct per-person action works in the devices channel as well.
    await page.selectOption('#gps-channel','device');
    const third=before.engineers[2].id;
    await page.waitForFunction(id=>!![...document.querySelectorAll('[data-exclude-one]')].find(b=>b.dataset.excludeOne===id && !b.disabled),third);
    const one=await preview(()=>page.locator('[data-exclude-one='+JSON.stringify(third)+']').click());
    assert.deepEqual(one.event.engineer_ids,[third]);
    assert.equal(one.excluded_engineers.length,3);
    assert.deepEqual(await get('/api/state'),applied,'Second preview must stay unpublished');
    for(const width of [320,390,768]){
      await page.setViewportSize({width,height:844});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Comparison overflow '+width);
    }
    await page.screenshot({path:path.join(tmp,'exclusion-preview.png'),fullPage:true});
    assert.deepEqual(errors,[]);console.log('PASS GPS one/group exclusion; durable roster and mobile; preview/approval; region isolation; 320/390/768 px');
    console.log('Screenshots: '+tmp);
  } finally {
    if(browser)await browser.close();
    if(server){fs.writeFileSync(stop,'stop');for(let n=0;n<60 && server.exitCode===null;n++)await delay(100);if(server.exitCode===null)server.kill();}
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
