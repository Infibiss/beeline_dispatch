// Real local TLS/native-protocol requests, mocked coordinates; NOT a field phone test.
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const https=require('node:https');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {login,password}=require('./browser_auth_helpers.cjs');
const root=path.resolve(__dirname,'..');
const directory=fs.mkdtempSync(path.join(os.tmpdir(),'gps-native-test-'));
const tls=path.join(root,'output/local/https');
const ca=fs.readFileSync(path.join(tls,'local-ca.crt'));
const servers=[];
async function start() {
  const stopFile=path.join(directory,'stop-'+servers.length);
  const args=['-B','tests/gps_browser_server.py','data/demo-road/Восток.json','--state',path.join(directory,'state.json'),
    '--accounts',path.join(directory,'accounts.sqlite3'),'--host','127.0.0.1','--port','0','--time-limit','.1',
    '--certfile',path.join(tls,'server.crt'),'--keyfile',path.join(tls,'server.key')];
  const child=spawn(process.env.PYTHON_BIN||'python',args,{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],
    env:{...process.env,PYTHONUTF8:'1',PYTHONPATH:path.join(root,'src'),GPS_TEST_STOP_FILE:stopFile,FIELD_ROUTER_ADMIN_PASSWORD:password}});
  child.stopFile=stopFile;servers.push(child);
  child.stderr.on('data',chunk=>process.stderr.write(chunk));
  return new Promise((resolve,reject)=>{
    let output='';const timer=setTimeout(()=>reject(new Error('Startup timeout')),20000);
    child.on('error',reject);child.once('exit',code=>{clearTimeout(timer);reject(new Error('Server exited '+code));});
    child.stdout.on('data',chunk=>{output+=chunk;const match=output.match(/https:\/\/127\.0\.0\.1:\d+/);if(match){clearTimeout(timer);resolve(match[0]);}});
  });
}
async function stop(child) {
  if(child.exitCode!==null)return;
  fs.writeFileSync(child.stopFile,'stop');
  await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('Shutdown timeout')),12000);child.once('exit',()=>{clearTimeout(timer);resolve();});});
}
function upload(url,params) {
  const body=new URLSearchParams(params).toString();
  return new Promise((resolve,reject)=>{
    const request=https.request(url+'/api/gps/native',{method:'POST',ca,headers:{'Content-Type':'application/x-www-form-urlencoded','Content-Length':Buffer.byteLength(body)}},res=>{
      let text='';res.on('data',c=>text+=c);res.on('end',()=>resolve({status:res.statusCode,body:JSON.parse(text)}));
    });request.on('error',reject);request.end(body);
  });
}
(async()=>{
  let browser;
  try {
    let url=await start();
    browser=await chromium.launch({executablePath:process.env.BROWSER_BIN||'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
    const admin=await browser.newContext({ignoreHTTPSErrors:true});await login(admin,url);
    const manifest=await(await admin.request.get(url+'/api/regions')).json();
    const engineers=await(await admin.request.get(url+'/api/engineers')).json();
    const eid=engineers.engineers[0].id;
    assert.equal((await admin.request.post(url+'/api/accounts',{data:{action:'create',username:'phone-test',password,region:manifest.current,engineer_id:eid}})).status(),200);
    const employee=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:390,height:844},permissions:['geolocation'],geolocation:{latitude:55.75,longitude:37.62,accuracy:8}});
    const signed=await employee.request.post(url+'/api/auth/login',{data:{username:'phone-test',password}});assert.equal(signed.status(),200);
    const identity=await signed.json();
    const page=await employee.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.route('https://tile.openstreetmap.org/**',route=>route.fulfill({status:204}));
    await page.goto(url+identity.redirect);await page.click('#tab-shift');
    assert.equal(await page.locator('#gps-stop').isVisible(),false);
    await page.click('#gps-start');
    assert.equal(await page.locator('#gps-start').isVisible(),false);
    assert.equal(await page.locator('#gps-stop').isVisible(),true);
    await page.waitForFunction(()=>document.querySelector('#gps-sent').textContent!=='Ещё нет');
    assert.equal(await page.locator('#gps-native-panel, #gps-native-credentials').count(),0);
    const apkUrl=url+'/engineer-app.apk';
    const head=await employee.request.head(apkUrl);
    assert.equal(head.status(),200);assert.equal(head.headers()['accept-ranges'],'bytes');
    assert.equal((await head.body()).length,0);
    const originalApk=fs.readFileSync(path.join(root,'output/android/field-engineer-debug.apk'));
    assert.equal(Number(head.headers()['content-length']),originalApk.length);
    const partial=await employee.request.get(apkUrl,{headers:{Range:'bytes=0-19'}});
    assert.equal(partial.status(),206);assert.deepEqual(await partial.body(),originalApk.subarray(0,20));
    await page.locator('#android-app > summary').click();
    const received=page.waitForEvent('download');
    await page.click('#android-apk-download');
    const download=await received;
    assert.equal(download.suggestedFilename(),'field-engineer-debug.apk');
    assert.deepEqual(fs.readFileSync(await download.path()),originalApk);
    await page.route('**/engineer-app.apk',route=>route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:'Тест: APK ещё не собрана'})}));
    await page.click('#android-apk-download');
    await page.waitForFunction(()=>document.querySelector('#android-apk-status').textContent.includes('APK ещё не собрана'));
    await page.unroute('**/engineer-app.apk');
    // The Android app obtains its scoped key internally; the engineer page never displays it.
    const issued=await employee.request.post(url+'/api/gps/mobile',{
      headers:{'X-CSRF-Token':identity.csrf},data:{action:'issue'}});
    assert.equal(issued.status(),200);
    const token=(await issued.json()).device_identifier;assert.ok(token.length>=40);
    assert.equal(await page.locator('#gps-start').isDisabled(),true); // Browser remains active.
    assert.equal(await page.locator('#gps-native-id, #gps-native-url, #gps-native-open').count(),0);
    let status=await(await employee.request.get(url+'/api/gps/mobile')).json();
    assert.equal(status.connection.state,'awaiting');
    assert.equal(status.tracking_state,'active');assert.equal(status.source,'browser');
    assert.match(await page.locator('#gps-state').textContent(),/браузер/);
    const stamp=Date.now()/1000;
    const bad=await upload(url,{id:token,lat:55.75,lon:37.62,accuracy:20000,timestamp:stamp});
    assert.equal(bad.status,200);assert.equal(bad.body.reason,'low_accuracy');
    status=await(await employee.request.get(url+'/api/gps/mobile')).json();
    assert.equal(status.connection.state,'low_accuracy');
    assert.equal(status.source,'browser');
    await new Promise(r=>setTimeout(r,500));
    const good=await upload(url,{id:token,lat:55.75,lon:37.62,accuracy:9,timestamp:Date.now()/1000});
    assert.equal(good.status,200);assert.equal(good.body.accepted,true);
    await page.waitForFunction(()=>document.querySelector('#gps-state').textContent==='Передача в фоне');
    assert.equal(await page.locator('#gps-start').isVisible(),false);
    assert.equal(await page.locator('#gps-stop').isVisible(),true);
    status=await(await employee.request.get(url+'/api/gps/mobile')).json();
    assert.equal(status.connection.state,'active');assert.equal(status.source,'native');
    assert.equal(status.tracking_state,'active');
    const offline=await upload(url,{id:token,lat:55.74,lon:37.61,accuracy:9,timestamp:stamp-3600});
    assert.equal(offline.body.reason,'historical_only');
    const history=await(await admin.request.get(url+'/api/gps/history?engineer_id='+encodeURIComponent(eid))).json();
    assert.equal(history.samples.length,4);
    for(const width of [320,360,390,430,768]) {
      await page.setViewportSize({width,height:844});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Native settings overflow '+width);
    }
    await page.screenshot({path:path.join(directory,'native-settings.png'),fullPage:true});
    await stop(servers[0]);url=await start();await login(admin,url);
    const restored=await(await admin.request.get(url+'/api/gps/history?engineer_id='+encodeURIComponent(eid))).json();
    assert.equal(restored.samples.length,4);
    const afterRestart=await upload(url,{id:token,lat:55.75001,lon:37.62,accuracy:8,timestamp:Date.now()/1000});
    assert.equal(afterRestart.status,200);assert.equal(afterRestart.body.accepted,true);
    const newLogin=await employee.request.post(url+'/api/auth/login',{data:{username:'phone-test',password}});
    const auth=await newLogin.json();
    const revoke=await employee.request.post(url+'/api/gps/mobile',{headers:{'X-CSRF-Token':auth.csrf},data:{action:'revoke'}});
    assert.equal(revoke.status(),200);
    assert.equal((await upload(url,{id:token,lat:55.75,lon:37.62,accuracy:8,timestamp:Date.now()/1000})).status,401);
    assert.deepEqual(errors,[]);
    console.log('PASS: compact browser GPS, no credential fields, browser-to-native handoff only after good fix, TLS, quality, offline archive, restart, revocation, 320–768px. Physical phone NOT tested.');
  } finally {if(browser)await browser.close();for(const child of servers)await stop(child);}
})().catch(error=>{console.error(error);process.exitCode=1;});
