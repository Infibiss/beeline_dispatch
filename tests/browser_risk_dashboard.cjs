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
    page.on('dialog',dialog=>{errors.push('Unexpected dialog: '+dialog.message());dialog.dismiss();});
    page.on('console',m=>{if(m.type()==='error' && /Content Security Policy|Refused to/.test(m.text()))errors.push(m.text());});
    page.on('request',r=>{if(r.method()==='POST' && r.url().startsWith(base+'/'))posts.push(r.url());});
    const get=async p=>(await context.request.get(base+p)).json();
    async function tab(name) {
      const menu=page.locator('#menu-toggle');
      if(await menu.isVisible() && await menu.getAttribute('aria-expanded')!=='true')await menu.click();
      await page.locator('#tab-'+name).click();
      await page.locator('#panel-'+name).waitFor({state:'visible'});
      if(name==='risk')await page.waitForFunction(()=>!document.querySelector('#risk-refresh').disabled);
    }
    const before=await get('/api/state');
    await page.goto(base);await tab('risk');
    await page.waitForSelector('[data-indicator="capacity"]');
    assert.equal(await page.locator('.risk-card').count(),3);
    assert.equal(await page.locator('.risk-card svg').count(),0);
    assert.ok(await page.locator('.risk-prediction').count()>=3);
    assert.doesNotMatch(await page.locator('#risk-model-status').textContent(),/risk-engine|600|Монте/i);
    assert.equal(await page.locator('#risk-engine-meta').isVisible(),false);
    await page.screenshot({path:path.join(tmp,'risk-desktop.png'),fullPage:true});
    await tab('demo');await page.click('#risk-demo');
    assert.equal(await page.locator('#risk-demo-notice').isVisible(),true);
    await tab('risk');
    assert.match(await page.locator('#risk-model-status').textContent(),/Учебный пример/);
    assert.equal(await page.locator('.risk-prediction').count(),3);
    assert.match(await page.locator('[data-risk="sla"] .risk-probability').textContent(),/31/);
    assert.equal(await page.locator('[data-risk="vehicle"]').count(),0);
    assert.equal(await page.locator('#risk-routes button').count(),0,'Fictitious demo visits cannot open fact actions');
    assert.equal(await page.locator('#risk-advisories button').count(),0);
    assert.doesNotMatch(await page.locator('#panel-risk').textContent(),/0[.,]62|условных ед|P50|P80|P90|Монте/i);
    await tab('technical');
    assert.equal(await page.locator('#risk-engine-meta').isVisible(),true);
    assert.match(await page.locator('#risk-engine-meta').textContent(),/условных ед/);
    await tab('risk');
    assert.deepEqual(await get('/api/state'),before);assert.deepEqual(posts,[]);
    await page.screenshot({path:path.join(tmp,'risk-example.png'),fullPage:true});
    for(const width of [320,390,768]){
      await page.setViewportSize({width,height:844});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Overflow at '+width);
    }
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(tmp,'risk-mobile.png'),fullPage:true});
    await tab('demo');await page.click('#risk-demo');await tab('risk');
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
    assert.equal(await page.locator('[data-risk="windows"] img').count(),0);
    forecast={...forecast,region:'Другой район'};await refresh();
    assert.equal(await page.locator('[data-risk="windows"]').count(),0);
    assert.match(await page.locator('#risk-model-status').textContent(),/устарел/);
    forecast={...forecast,region:snapshot.context.region,plan_version:snapshot.context.plan_version-1};await refresh();
    assert.equal(await page.locator('.risk-prediction').count(),0);
    assert.match(await page.locator('#risk-model-status').textContent(),/устарел/);
    forecast={...forecast,plan_version:snapshot.context.plan_version,risks:[{...forecast.risks[0],value:7}]};await refresh();
    assert.equal(await page.locator('[data-risk="windows"]').count(),0);
    // Real contract: compact estimates and actual visits; technical details stay on their own tab.
    const malicious='<img src=x onerror=alert(1)>';
    forecast={status:'ready',region:snapshot.context.region,plan_version:snapshot.context.plan_version,
      model_version:'risk-engine-v1.1',calibrated:false,iterations:600,loss_unit:'conditional_loss_units',
      duration_evidence:{status:'empirical_update',observation_count:3,group_count:1},
      loss_weights:{late_start:.25,sla_breach:10,overtime_minute:.05},expected_loss:4.2,
      expected_affected_customers:.7,p_sla_breach:.41,p_window_sla_breach:.41,
      p_emergency_response_sla_breach:null,p_late_start:.52,p_overtime:.18,expected_overtime_minutes:9.5,
      risks:[{id:'vehicle',measure:'heuristic_score',value:64,reasons:['ТО просрочено'],recommendation:'Проверить машину'}],
      routes:[{engineer_id:before.engineers[0].id,p_sla_breach:.41,p_late_start:.52,p_overtime:.18,expected_loss:4.2,
        expected_affected_customers:.7,visits:[{request_id:'R-1',request_name:malicious,planned_start:620,planned_sla_deadline:690,
          p_sla_breach:.41,p_late_start:.52,p_overtime:.18,expected_loss:4.2,
          service_duration:{p50_minutes:70,p80_minutes:88,p90_minutes:101,observation_count:3}}]}],
      assumptions:['synthetic priors; not calibrated'],advisories:[{id:'sla-1',risk_id:'sla',severity:'high',
        title:'Проверить SLA',summary:malicious,plan_version:snapshot.context.plan_version,engineer_id:before.engineers[0].id,request_id:'R-1'}]};
    await refresh();
    assert.equal(await page.locator('.risk-prediction').count(),4);
    assert.equal(await page.locator('[data-risk="sla"] .risk-probability').textContent(),'41%');
    assert.equal(await page.locator('[data-risk="vehicle"] .risk-probability').textContent(),'64 / 100');
    assert.doesNotMatch(await page.locator('#risk-model-status').textContent(),/калибр|risk-engine|600/i);
    assert.match(await page.locator('[data-risk="sla"] .risk-prediction-label').textContent(),/оценка/);
    assert.equal(await page.locator('#risk-engine-meta').isVisible(),false);
    assert.equal(await page.locator('#risk-routes').isVisible(),true);
    assert.match(await page.locator('#risk-routes').textContent(),/10:20/);
    assert.match(await page.locator('#risk-routes').textContent(),/11:30/);
    assert.doesNotMatch(await page.locator('#risk-routes').textContent(),/P50|P80|P90|потер|затронуто|0[.,]7/i);
    assert.equal(await page.locator('#risk-routes tbody tr').count(),1);
    assert.ok((await page.locator('#risk-routes').textContent()).includes(malicious),'Visit text must be escaped, not discarded');
    assert.ok((await page.locator('#risk-advisories').textContent()).includes(malicious),'Advisory text must be escaped, not discarded');
    assert.equal(await page.locator('#panel-risk img').count(),0);
    await tab('technical');
    assert.match(await page.locator('#risk-engine-meta').textContent(),/не подтверждена/);
    assert.match(await page.locator('#risk-engine-meta').textContent(),/3 подтверждённых фактов/);
    assert.match(await page.locator('#risk-engine-meta').textContent(),/4,2 условных ед/);
    await page.locator('#risk-assumption-details summary').click();
    assert.match(await page.locator('#risk-assumptions').textContent(),/synthetic priors/);
    await tab('risk');
    // A stale advisory remains visible but cannot trigger a request.
    forecast.advisories[0].plan_version=snapshot.context.plan_version-1;await refresh();
    assert.equal(await page.locator('[data-advisory="sla-1"]').isDisabled(),true);
    assert.deepEqual(posts,[]);
    forecast.advisories[0].plan_version=snapshot.context.plan_version;await refresh();
    const previewRequest=page.waitForRequest(r=>r.url().endsWith('/api/preview') && r.method()==='POST');
    const previewResponse=page.waitForResponse(r=>r.url().endsWith('/api/preview'));
    await page.locator('[data-advisory="sla-1"]').click();await previewResponse;
    const payload=(await previewRequest).postDataJSON();
    assert.equal(payload.version,before.version);assert.equal(payload.event.type,'replan');
    assert.equal(payload.preserve_in_progress,true);assert.equal(payload.allow_redirect,false);
    assert.deepEqual(payload.local_scope_engineer_ids,[before.engineers[0].id]);
    await page.waitForSelector('.candidate');
    assert.equal(await page.locator('#panel-events').isVisible(),true);
    assert.equal(await page.locator('#approve').isDisabled(),true);
    assert.equal(posts.filter(url=>url.endsWith('/api/preview')).length,1);
    assert.equal(posts.filter(url=>url.endsWith('/api/approve')).length,0);
    posts.length=0;
    await page.unroute('**/api/risk');
    await tab('risk');
    await page.route('**/api/risk',r=>r.fulfill({status:503,json:{error:'test failure'}}));
    await refresh();assert.equal(await page.locator('#risk-error').isVisible(),true);
    await tab('plan');assert.equal(await page.locator('#panel-plan').isVisible(),true);
    await page.unroute('**/api/risk');await tab('risk');await page.waitForSelector('.risk-card');
    await page.click('#risk-facts');assert.equal(await page.locator('#panel-facts').isVisible(),true);
    await tab('risk');await page.waitForSelector('.risk-card');
    await page.click('#risk-replan');assert.equal(await page.locator('#panel-events').isVisible(),true);
    assert.deepEqual(posts,[]);
    await page.selectOption('#region-select','south-east');await page.waitForURL(base+'/r/south-east/');
    await tab('risk');await page.waitForSelector('.risk-card');
    assert.match(await page.locator('#risk-context').textContent(),/^Юго-восток/);
    await page.goto(base+'/regions/east/');await tab('risk');await page.waitForSelector('.risk-card');
    assert.match(await page.locator('#risk-context').textContent(),/^Восток/);
    const engineer=before.engineers[0].id;
    const changed=await context.request.post(base+'/api/engineer/facts',{data:{version:before.version,
      engineer_id:engineer,status:'available',time:before.time,location_id:'office'}});
    assert.equal(changed.status(),200);
    await page.click('#reload');await page.waitForFunction(()=>document.querySelector('#version').textContent==='2');
    await page.waitForFunction(()=>!document.querySelector('#risk-stale').hidden);
    assert.equal(await page.locator('.risk-prediction').count(),0);
    assert.equal(await page.locator('#risk-route-details').isVisible(),false);
    assert.deepEqual(errors,[]);
    console.log('PASS risk dashboard: compact metrics, separate demo/technical tabs, escaped visits/advisories, preview payload, stale/invalid estimates, failure recovery, roles/regions, 320/390/768 px.');
    console.log('Screenshots: '+tmp);
  } finally {
    if(browser)await browser.close();
    if(server && server.exitCode===null){const closed=new Promise(resolve=>server.once('close',resolve));fs.writeFileSync(stop,'stop');await Promise.race([closed,delay(5000).then(()=>{if(server.exitCode===null)server.kill();})]);}
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
