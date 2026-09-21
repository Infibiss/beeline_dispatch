// Real Leaflet and browser request headers; ALL tiles are intercepted locally.
// Automated pan/zoom must never hit the public OpenStreetMap tile service.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {password,login}=require('./browser_auth_helpers.cjs');
const root=path.resolve(__dirname,'..'),output=fs.mkdtempSync(path.join(os.tmpdir(),'map-regression-'));
const stop=path.join(output,'stop');
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{let server,browser;try{
  const state=path.join(output,'state.json');
  if(process.env.TEST_STATE_SOURCE)fs.copyFileSync(process.env.TEST_STATE_SOURCE,state);
  server=spawn(process.env.PYTHON_BIN || 'python',['-B','tests/gps_browser_server.py','data/demo-road/Восток.json',
    '--state',state,'--port','0','--time-limit','.05'],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],
    env:{...process.env,PYTHONPATH:path.join(root,'src'),PYTHONUTF8:'1',FIELD_ROUTER_ADMIN_PASSWORD:password,GPS_TEST_STOP_FILE:stop}});
  const base=await new Promise((resolve,reject)=>{
    let out='';const timer=setTimeout(()=>reject(Error('Server startup timeout')),20000);
    server.on('error',reject);server.stderr.on('data',d=>process.stderr.write(d));
    server.stdout.on('data',d=>{out+=d;const m=out.match(/http:\/\/127\.0\.0\.1:\d+/);if(m){clearTimeout(timer);resolve(m[0]);}});
  });
  browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  const context=await browser.newContext({viewport:{width:1440,height:1000}});await login(context,base);
  const tileRequests=[];
  await context.route('https://tile.openstreetmap.org/**',async route=>{
    const request=route.request();tileRequests.push({page:request.frame().url(),headers:await request.allHeaders()});
    await route.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jE4sAAAAASUVORK5CYII=','base64')});
  });
  function assertReferrers() {
    assert.ok(tileRequests.length>0);
    for(const {page,headers} of tileRequests)assert.equal(headers.referer,base+'/',
      'OSM must receive the real application origin, without private paths/query parameters: '+page);
  }
  const page=await context.newPage(),errors=[],badAssets=[];
  context.on('page',p=>p.on('pageerror',e=>errors.push(e.message)));
  page.on('pageerror',e=>errors.push(e.message));
  page.on('console',m=>{if(m.type()==='error' && /Content Security Policy|Refused to/.test(m.text()))errors.push(m.text());});
  page.on('response',r=>{if(r.url().startsWith(base+'/') && /\.(?:js|css)(?:\?|$)/.test(r.url()) && r.status()>=400)badAssets.push([r.url(),r.status()]);});
  const get=async p=>(await context.request.get(base+p)).json();
  const before=await get('/api/state');
  async function mapReady(frame,id,paths=true) {
    await frame.waitForFunction(({id,paths})=>{
      const el=document.getElementById(id);if(!el)return false;
      const tiles=[...el.querySelectorAll('img.leaflet-tile')];
      return el.clientWidth>200 && el.clientHeight>200 && tiles.length>0
        && tiles.every(t=>t.complete && t.naturalWidth>0)
        && (!paths || el.querySelectorAll('.leaflet-overlay-pane path').length>0);
    },{id,paths},{timeout:25000});
  }
  async function planFrame(){await page.frameLocator('#plan-frame').locator('#map').waitFor();return page.frames().find(f=>/\/dispatcher\?/.test(f.url()));}
  await page.goto(base);let frame=await planFrame();await mapReady(frame,'map');
  assertReferrers();
  await page.screenshot({path:path.join(output,'plan.png'),fullPage:true});
  const total=await frame.locator('#request-list [data-request-id]').count();
  assert.ok(total>0);
  await frame.selectOption('#engineer-select',before.engineers[0].id);
  await mapReady(frame,'map');
  assert.ok(await frame.locator('#request-list [data-request-id]').count()<total);
  await frame.locator('#request-list [data-request-id]').first().click();
  assert.ok((await frame.locator('#request-card').textContent()).length>50);
  await page.click('#tab-risk');await page.waitForSelector('.risk-card');
  for(const width of [390,768,1440]){
    await page.setViewportSize({width,height:844});await page.click('#tab-plan');await mapReady(frame,'map');
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.click('#tab-risk');
  }
  await page.click('#tab-gps');await page.waitForSelector('.gps-person');await mapReady(page,'gps-map',false);
  await page.selectOption('#gps-channel','demo');await page.selectOption('#gps-demo-case','near_site');await page.click('#gps-demo-run');
  await mapReady(page,'gps-map');
  await page.click('#gps-fit');
  await page.screenshot({path:path.join(output,'gps.png'),fullPage:true});
  const mobile=await context.newPage();await mobile.goto(base+'/engineer');
  await mobile.waitForFunction(()=>document.querySelector('#engineer-select').options.length>1);
  await mobile.click('#tab-map');await mapReady(mobile,'engineer-map');
  assert.ok(await mobile.locator('#engineer-map .leaflet-marker-icon').count()>0);
  await mobile.setViewportSize({width:390,height:844});await mapReady(mobile,'engineer-map');
  await mobile.screenshot({path:path.join(output,'engineer.png'),fullPage:true});
  for(const prefix of ['/r/south-east','/regions/south-center']){
    await page.goto(base+prefix+'/');frame=await planFrame();await mapReady(frame,'map');
    assert.ok(frame.url().startsWith(base+prefix+'/'));
  }
  assertReferrers();
  assert.ok(tileRequests.some(r=>r.page.endsWith('/engineer')));
  assert.ok(tileRequests.some(r=>r.page===base+'/'));
  assert.ok(tileRequests.some(r=>r.page.includes('/regions/south-center/dispatcher')));
  // A failing basemap must leave task points and the list usable.
  await context.route('https://tile.openstreetmap.org/**',r=>r.abort('failed'));
  await page.goto(base);frame=await planFrame();
  await frame.waitForFunction(()=>document.querySelector('#map-status').textContent.includes('Подложка недоступна'));
  assert.ok(await frame.locator('#map .leaflet-overlay-pane path').count()>0);
  assert.equal(await frame.locator('#request-list [data-request-id]').count(),total);
  assert.deepEqual(await get('/api/state'),before,'Map/risk navigation must not mutate the plan');
  assert.deepEqual(badAssets,[]);assert.deepEqual(errors,[]);
  console.log('PASS maps: real origin Referer; decoded local tiles; plan filters/cards; GPS points; engineer; regions; risk-tab return/resizing; offline fallback; zero public tile requests.');
  console.log('Screenshots: '+output);
}finally{
  if(browser)await browser.close();
  if(server&&server.exitCode===null){const closed=new Promise(resolve=>server.once('close',resolve));fs.writeFileSync(stop,'stop');await Promise.race([closed,delay(5000).then(()=>{if(server.exitCode===null)server.kill();})]);}
}})().catch(e=>{console.error(e);process.exitCode=1;});
