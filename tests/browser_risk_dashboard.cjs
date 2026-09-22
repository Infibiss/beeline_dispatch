// Dispatcher risk UI, using a real local server and isolated state/accounts.
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {password,login}=require('./browser_auth_helpers.cjs');
const root=path.resolve(__dirname,'..'),tmp=fs.mkdtempSync(path.join(os.tmpdir(),'risk-dashboard-'));
const stop=path.join(tmp,'stop'),delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
  let server,browser;
  try {
    server=spawn(process.env.PYTHON_BIN || 'python',['-B','tests/gps_browser_server.py','data/demo-road/Восток.json',
      '--state',path.join(tmp,'state.json'),'--port','0','--time-limit','.05'],{
      cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],
      env:{...process.env,PYTHONUTF8:'1',PYTHONPATH:path.join(root,'src'),GPS_TEST_STOP_FILE:stop,FIELD_ROUTER_ADMIN_PASSWORD:password}});
    const base=await new Promise((resolve,reject)=>{
      let out='';const timer=setTimeout(()=>reject(Error('Startup timeout')),20000);
      server.on('error',reject);server.stderr.on('data',chunk=>process.stderr.write(chunk));
      server.stdout.on('data',chunk=>{out+=chunk;const match=out.match(/http:\/\/127\.0\.0\.1:\d+/);if(match){clearTimeout(timer);resolve(match[0]);}});
    });
    browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
    const context=await browser.newContext({viewport:{width:1440,height:1100}});
    await login(context,base);await context.route('https://tile.openstreetmap.org/**',r=>r.fulfill({status:204}));
    const page=await context.newPage(),errors=[],posts=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('console',m=>{if(m.type()==='error' && /Content Security Policy|Refused to/.test(m.text()))errors.push(m.text());});
    page.on('request',r=>{if(r.method()==='POST' && r.url().startsWith(base+'/'))posts.push(r.url());});
    const get=async p=>(await context.request.get(base+p)).json();
    const before=await get('/api/state');
    await page.goto(base);await page.click('#tab-risk');
    await page.waitForSelector('[data-indicator="capacity"]');
    assert.equal(await page.locator('.risk-card').count(),3);
    assert.equal(await page.locator('.risk-prediction').count(),3);
    assert.ok((await page.locator('#risk-model-status').textContent()).includes('не подключена'));
    await page.locator('.risk-card').first().locator('summary').click();
    assert.equal(await page.locator('.risk-card').first().locator('details').getAttribute('open'),'');
    await page.screenshot({path:path.join(tmp,'risk-desktop.png'),fullPage:true});
    await page.click('#risk-demo');
    assert.equal(await page.locator('#risk-demo-notice').isVisible(),true);
    assert.match(await page.locator('[data-risk="capacity"] .risk-probability').textContent(),/72/);
    assert.equal(await page.locator('[data-risk="equipment"] .risk-probability').textContent(),'15 / 100');
    await page.locator('[data-risk="capacity"] summary').click();
    assert.ok((await page.locator('[data-risk="capacity"]').textContent()).includes('резервного инженера'));
    assert.deepEqual(await get('/api/state'),before);assert.deepEqual(posts,[]);
    await page.screenshot({path:path.join(tmp,'risk-example.png'),fullPage:true});
    for(const width of [320,390,768]){
      await page.setViewportSize({width,height:844});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Overflow at '+width);
    }
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(tmp,'risk-mobile.png'),fullPage:true});
    await page.click('#risk-demo');
    // Future model payload: valid, stale, and invalid values must be distinguished.
    const snapshot=await get('/api/risk');
    let forecast={status:'ready',region:snapshot.context.region,plan_version:snapshot.context.plan_version,
      model_version:'test-model',horizon_minutes:120,risks:[{id:'windows',measure:'probability',value:.41,
        reasons:['<img src=x onerror=alert(1)>'],recommendation:'Проверить окно'}]};
    await page.route('**/api/risk',r=>r.fulfill({json:{...snapshot,forecast}}));
    async function refresh() {
      const wait=page.waitForResponse(r=>r.url().endsWith('/api/risk'));
      await page.click('#risk-refresh');await wait;await page.waitForFunction(()=>!document.querySelector('#risk-refresh').disabled);
    }
    await refresh();assert.equal(await page.locator('[data-risk="windows"] .risk-probability').textContent(),'41%');
    await page.locator('[data-risk="windows"] summary').click();
    assert.equal(await page.locator('[data-risk="windows"] img').count(),0);
    forecast={...forecast,region:'Другой район'};await refresh();
    assert.equal(await page.locator('[data-risk="windows"] .risk-probability').textContent(),'—');
    forecast={...forecast,region:snapshot.context.region,risks:[{...forecast.risks[0],value:7}]};await refresh();
    assert.equal(await page.locator('[data-risk="windows"] .risk-probability').textContent(),'—');
    await page.unroute('**/api/risk');
    await page.route('**/api/risk',r=>r.fulfill({status:503,json:{error:'test failure'}}));
    await refresh();assert.equal(await page.locator('#risk-error').isVisible(),true);
    await page.click('#tab-plan');assert.equal(await page.locator('#panel-plan').isVisible(),true);
    await page.unroute('**/api/risk');await page.click('#tab-risk');await page.waitForSelector('.risk-card');
    await page.click('#risk-facts');assert.equal(await page.locator('#panel-facts').isVisible(),true);
    await page.click('#tab-risk');await page.waitForSelector('.risk-card');
    await page.click('#risk-replan');assert.equal(await page.locator('#panel-events').isVisible(),true);
    assert.deepEqual(posts,[]);
    await page.selectOption('#region-select','south-east');await page.waitForURL(base+'/r/south-east/');
    await page.click('#tab-risk');await page.waitForSelector('.risk-card');
    assert.match(await page.locator('#risk-context').textContent(),/^Юго-восток/);
    await page.goto(base+'/regions/east/');await page.click('#tab-risk');await page.waitForSelector('.risk-card');
    assert.match(await page.locator('#risk-context').textContent(),/^Восток/);
    const engineer=before.engineers[0].id;
    const changed=await context.request.post(base+'/api/engineer/facts',{data:{version:before.version,
      engineer_id:engineer,status:'available',time:before.time,location_id:'office'}});
    assert.equal(changed.status(),200);
    await page.click('#reload');await page.waitForFunction(()=>document.querySelector('#version').textContent==='2');
    await page.waitForFunction(()=>!document.querySelector('#risk-stale').hidden);
    assert.deepEqual(errors,[]);
    console.log('PASS risk dashboard: live metrics, isolated demo, model contract/staleness, failure recovery, roles/regions, 320/390/768 px.');
    console.log('Screenshots: '+tmp);
  } finally {
    if(browser)await browser.close();
    if(server && server.exitCode===null){const closed=new Promise(resolve=>server.once('close',resolve));fs.writeFileSync(stop,'stop');await Promise.race([closed,delay(5000).then(()=>{if(server.exitCode===null)server.kill();})]);}
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
