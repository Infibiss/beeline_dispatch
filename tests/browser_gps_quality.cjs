// Real API + browser, synthetic coordinates. Draft -> confirm -> preview -> approve.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {login,password}=require('./browser_auth_helpers.cjs');
const root=path.resolve(__dirname,'..'),tmp=fs.mkdtempSync(path.join(os.tmpdir(),'gps-quality-browser-')),stop=path.join(tmp,'stop');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 let child,browser;
 try{
  child=spawn(process.env.PYTHON_BIN||'python',['-B','tests/gps_quality_browser_server.py',tmp],
    {cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env:{...process.env,PYTHONUTF8:'1',PYTHONPATH:path.join(root,'src'),
      FIELD_ROUTER_ADMIN_PASSWORD:password,GPS_TEST_STOP_FILE:stop}});
  const base=await new Promise((resolve,reject)=>{
    let out='';const timer=setTimeout(()=>reject(Error('Server startup timeout')),15000);
    child.on('error',reject);child.stderr.on('data',x=>process.stderr.write(x));
    child.stdout.on('data',x=>{out+=x;const m=out.match(/http:\/\/127\.0\.0\.1:\d+/);if(m){clearTimeout(timer);resolve(m[0]);}});
  });
  browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_BIN||'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  const context=await browser.newContext({viewport:{width:390,height:844}});
  await context.route('https://tile.openstreetmap.org/**',r=>r.fulfill({status:204}));
  await login(context,base);
  const page=await context.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const get=async p=>(await context.request.get(base+p)).json();
  await page.goto(base);
  await page.evaluate(()=>{
    window.__gpsMarkers=[];
    const circle=L.circleMarker;
    L.circleMarker=function(latlng,options){window.__gpsMarkers.push(latlng);return circle.call(this,latlng,options);};
  });
  await page.click('#tab-gps');
  await page.waitForSelector('[data-gps-delay]');
  assert.match(await page.locator('#gps-people').textContent(),/До заявки № gps-test-001: около 1 км по прямой/);
  assert.doesNotMatch(await page.locator('#gps-people').textContent(),/Источник измерения|км\/ч|Спутники/);
  assert.match(await page.locator('#gps-diagnostics').textContent(),/incoming_provider/);
  const before=await get('/api/state');
  await page.click('[data-gps-delay]');
  await page.locator('#panel-facts').waitFor({state:'visible'});
  assert.equal(await page.locator('#panel-facts').isVisible(),true);
  assert.match(await page.locator('#gps-delay-note').textContent(),/Предварительное влияние/);
  assert.ok(Number(await page.inputValue('#fact-remaining'))>2);
  assert.deepEqual(await get('/api/state'),before,'A draft must never write facts');
  // A risk recommendation must not accidentally submit an earlier GPS confirmation.
  await page.evaluate(()=>window.dispatchEvent(new CustomEvent('risk-intent',{detail:{action:'facts',engineerId:'gps-browser-e1'}})));
  assert.equal(await page.locator('#gps-delay-note').isVisible(),false);
  assert.equal(await page.locator('#facts-form button[type=submit]').textContent(),'Сохранить статус');
  assert.deepEqual(await get('/api/state'),before);
  await page.click('#tab-gps');await page.click('[data-gps-delay]');
  await page.locator('#gps-delay-note').waitFor({state:'visible'});
  const pending=page.waitForResponse(r=>r.url().endsWith('/api/preview')&&r.request().method()==='POST');
  await page.locator('#facts-form button[type=submit]').click();
  const response=await pending,preview=await response.json();
  assert.equal(response.status(),200,JSON.stringify(preview));
  await page.waitForSelector('.candidate');
  const recorded=await get('/api/state');
  assert.equal(recorded.version,before.version+1);
  assert.match(recorded.fact_journal.at(-1).comment,/Диспетчер подтвердил/);
  assert.equal(recorded.fact_journal.at(-1).status,'traveling');
  assert.equal(await page.locator('#panel-events').isVisible(),true);
  // Reusing the original plan version is rejected, not applied twice.
  const stale=await context.request.post(base+'/api/gps/delay',{data:{version:before.version,engineer_id:'gps-browser-e1'}});
  assert.equal(stale.status(),409);
  for(const width of [320,390,768]){
    await page.setViewportSize({width,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Overflow '+width);
  }
  await page.locator('.candidate').first().getByRole('button',{name:'Посмотреть изменения',exact:true}).click();
  await page.click('#approve');
  await page.waitForFunction(v=>document.querySelector('#version').textContent===String(v),recorded.version+1);
  assert.equal((await get('/api/state')).version,recorded.version+1);
  // A renewed tracker sends a bad first fix; the actual map must stay at the
  // previously accepted employee position, independent of provider name.
  const telemetry=async data=>{
    const r=await context.request.post(base+'/api/gps/telemetry',{data});
    assert.equal(r.status(),202);return r.json();
  };
  const employee='gps-browser-e1';
  await telemetry({engineer_id:employee,tracker_id:'native-browser-quality',state:'off',point:null});
  const jump=await telemetry({engineer_id:employee,tracker_id:'native-browser-renewed',state:'active',
    point:{lat:56.21,lon:37.7,accuracy_m:8,measured_at_ms:Date.now(),provider:'network'}});
  assert.equal(jump.accepted,false);
  const gps=await get('/api/gps?channel=device');
  assert.equal(gps.rows[0].position.lat,55.7);
  await page.click('#tab-gps');
  await page.waitForFunction(()=>document.querySelector('#gps-people').textContent.includes('Уточняем местоположение'));
  assert.deepEqual(await page.evaluate(()=>window.__gpsMarkers.at(-1)),[55.7,37.7]);
  assert.deepEqual(errors,[]);
  await page.screenshot({path:path.join(tmp,'confirmed-delay.png'),fullPage:true});
  console.log('PASS GPS source + read-only proposal + explicit confirmation + solver preview + approval + stale version + 320–768px.');
  console.log('Screenshot: '+tmp);
 }finally{
  if(browser)await browser.close();
  if(child){fs.writeFileSync(stop,'stop');for(let i=0;i<60&&child.exitCode===null;i++)await sleep(100);if(child.exitCode===null)child.kill();}
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
