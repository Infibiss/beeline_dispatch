// Real TLS server + employee UI. Android bridge and GPS are simulated, not a physical phone test.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {spawn}=require('node:child_process'),https=require('node:https');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {login,password}=require('./browser_auth_helpers.cjs');
const root=path.resolve(__dirname,'..'),directory=fs.mkdtempSync(path.join(os.tmpdir(),'android-gps-browser-'));
const tls=path.join(root,'output/local/https'),ca=fs.readFileSync(path.join(tls,'local-ca.crt')),stopFile=path.join(directory,'stop');
function upload(url,params) {
  const data=new URLSearchParams(params).toString();
  return new Promise((resolve,reject)=>{
    const req=https.request(url+'/api/gps/native',{method:'POST',ca,headers:{'Content-Type':'application/x-www-form-urlencoded','Content-Length':Buffer.byteLength(data)}},res=>{
      let text='';res.on('data',c=>text+=c);res.on('end',()=>resolve({status:res.statusCode,data:JSON.parse(text)}));
    });req.on('error',reject);req.end(data);
  });
}
(async()=>{
  let browser,child;
  try {
    child=spawn(process.env.PYTHON_BIN||'python',['-B','tests/gps_browser_server.py','data/demo-road/Восток.json',
      '--state',path.join(directory,'state.json'),'--accounts',path.join(directory,'accounts.sqlite3'),
      '--host','127.0.0.1','--port','0','--time-limit','.1','--certfile',path.join(tls,'server.crt'),'--keyfile',path.join(tls,'server.key')],
      {cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env:{...process.env,PYTHONUTF8:'1',PYTHONPATH:path.join(root,'src'),GPS_TEST_STOP_FILE:stopFile,FIELD_ROUTER_ADMIN_PASSWORD:password}});
    child.stderr.on('data',d=>process.stderr.write(d));
    const url=await new Promise((resolve,reject)=>{
      let out='';const timer=setTimeout(()=>reject(Error('Server timeout')),30000);
      child.on('error',reject);child.stdout.on('data',d=>{out+=d;const m=out.match(/https:\/\/127\.0\.0\.1:\d+/);if(m){clearTimeout(timer);resolve(m[0]);}});
    });
    browser=await chromium.launch({executablePath:process.env.BROWSER_BIN||'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
    const admin=await browser.newContext({ignoreHTTPSErrors:true});await login(admin,url);
    const region=(await(await admin.request.get(url+'/api/regions')).json()).current;
    const eid=(await(await admin.request.get(url+'/api/engineers')).json()).engineers[0].id;
    assert.equal((await admin.request.post(url+'/api/accounts',{data:{action:'create',username:'android-worker',password,region,engineer_id:eid}})).status(),200);
    const employee=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:390,height:844},userAgent:'Mozilla/5.0 Android FieldRouterAndroid/1'});
    const identity=await(await employee.request.post(url+'/api/auth/login',{data:{username:'android-worker',password}})).json();
    const page=await employee.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.route('https://tile.openstreetmap.org/**',r=>r.fulfill({status:204}));
    await page.addInitScript(()=>{
      let state={enabled:false,permission:true,serviceRunning:false,queued:0};const calls=[];
      window.__androidTest={calls,get state(){return state;},update(value){Object.assign(state,value);window.dispatchEvent(new CustomEvent('field-router-native-status',{detail:state}));}};
      window.FieldRouterNative={call(action,data={}){
        calls.push({action,data});
        if(action==='permission')return{granted:state.permission,requested:!state.permission};
        if(action==='start')state={...state,...data,enabled:true,serviceRunning:true,message:'Ищем координату'};
        if(action==='stop')state={...state,enabled:false,serviceRunning:false,queued:0,message:'Передача остановлена'};
        return {...state};
      }};
    });
    await page.goto(url+identity.redirect);await page.click('#tab-shift');
    const apk=await employee.request.get(url+'/engineer-app.apk');
    assert.equal(apk.status(),200);
    assert.equal(apk.headers()['content-type'],'application/vnd.android.package-archive');
    assert.equal((await apk.body()).subarray(0,2).toString(),'PK');
    await page.waitForFunction(()=>!document.querySelector('#gps-start').disabled);
    assert.equal(await page.locator('#gps-native-panel, #gps-native-url, #gps-end-shift').count(),0);
    assert.equal(await page.locator('#android-app').isVisible(),false);
    assert.equal(await page.locator('#gps-stop').isVisible(),false);
    await page.click('#gps-start');
    await page.waitForFunction(()=>window.__androidTest.state.enabled);
    assert.equal(await page.locator('#gps-start').isVisible(),false);
    assert.equal(await page.locator('#gps-stop').isVisible(),true);
    let token=await page.evaluate(()=>window.__androidTest.state.token);
    assert.ok(token.length>=40);
    assert.equal(await page.evaluate(()=>window.__androidTest.calls.find(c=>c.action==='start').data.engineer),eid);
    assert.equal(await page.evaluate(()=>window.__androidTest.calls.filter(c=>c.action==='start').length),1);
    const params={id:token,lat:55.75,lon:37.62,accuracy:8,timestamp:Date.now()};
    assert.equal((await upload(url,params)).status,200);
    await page.evaluate(()=>window.__androidTest.update({measuredAt:Date.now(),accuracy:8,sentAt:Date.now(),message:'Координата доставлена серверу.'}));
    assert.match(await page.locator('#gps-state').textContent(),/Включена/);
    assert.match(await page.locator('#gps-fix').textContent(),/±8/);
    await page.evaluate(()=>window.__androidTest.update({serverMeasuredAt:Date.now(),serverAccepted:false,serverReason:'jump',serverDetail:'Скачок 58000 м за 10 сек.'}));
    assert.doesNotMatch(await page.locator('#gps-feedback').textContent(),/58000/);
    assert.match(await page.evaluate(()=>window.FieldRouterGpsDiagnostics.snapshot().serverDetail),/58000/);
    assert.equal(await page.evaluate(()=>Object.hasOwn(window.FieldRouterGpsDiagnostics.snapshot(),'token')),false);
    assert.equal(await page.locator('#gps-android-diagnostics, #gps-android-quality, #gps-android-background').count(),0);
    await page.evaluate(()=>window.__androidTest.update({serverAccepted:true,serverReason:'',serverDetail:'Координата принята для расчёта.'}));
    await page.evaluate(()=>window.__androidTest.update({finePermission:false,gpsEnabled:true,provider:'network',accuracy:100,appVersion:'0.1.2'}));
    assert.match(await page.locator('#gps-feedback').textContent(),/точное местоположение/);
    assert.equal(await page.evaluate(()=>window.FieldRouterGpsDiagnostics.snapshot().provider),'network');
    assert.match(await page.locator('#gps-fix').textContent(),/±100/);
    await page.click('#gps-android-settings');
    assert.ok(await page.evaluate(()=>window.__androidTest.calls.some(c=>c.action==='settings')));
    await page.evaluate(()=>window.__androidTest.update({finePermission:true,provider:'gps',accuracy:8}));
    assert.equal(await page.evaluate(()=>window.FieldRouterGpsDiagnostics.snapshot().provider),'gps');
    assert.equal(await page.locator('#gps-android-settings').isVisible(),false);
    await page.evaluate(()=>window.__androidTest.update({batteryExempt:false,backgroundRestricted:true,powerSaveMode:true,notificationsEnabled:false}));
    assert.match(await page.locator('#gps-feedback').textContent(),/без ограничений батареи/);
    await page.click('#gps-android-settings');
    assert.ok(await page.evaluate(()=>window.__androidTest.calls.some(c=>c.action==='background-settings')));
    await page.evaluate(()=>window.__androidTest.update({batteryExempt:true,backgroundRestricted:false,powerSaveMode:false,notificationsEnabled:true,networkType:'mobile'}));
    assert.equal(await page.locator('#gps-android-settings').isVisible(),false);
    await page.evaluate(()=>window.__androidTest.update({networkType:'wifi',backgroundFixAt:Date.now(),backgroundSentAt:Date.now(),attemptAt:Date.now()}));
    assert.ok(await page.evaluate(()=>window.FieldRouterGpsDiagnostics.snapshot().backgroundFixAt));
    assert.ok(await page.evaluate(()=>window.FieldRouterGpsDiagnostics.snapshot().backgroundSentAt));
    await page.evaluate(()=>window.__androidTest.update({queued:12,sentAt:Date.now()-90000,measuredAt:Date.now(),message:'Нет связи с сервером. Точки сохранены.'}));
    assert.match(await page.locator('#gps-state').textContent(),/Точки ждут отправки/);
    await page.evaluate(()=>window.__androidTest.update({measuredAt:Date.now()-90000}));
    assert.match(await page.locator('#gps-state').textContent(),/Нет свежей координаты/);
    await page.evaluate(()=>window.__androidTest.update({measuredAt:Date.now()}));
    assert.equal(await page.evaluate(()=>window.FieldRouterGpsDiagnostics.snapshot().queued),12);
    for(const width of [320,390,768]){await page.setViewportSize({width,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));}
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(directory,'android-gps.png'),fullPage:true});
    await page.click('#gps-stop');
    assert.equal(await page.evaluate(()=>window.__androidTest.state.enabled),false);
    assert.equal(await page.locator('#gps-start').isVisible(),true);
    assert.equal(await page.locator('#gps-stop').isVisible(),false);
    assert.equal(await page.locator('#task-dialog').isVisible(),false);
    assert.equal((await upload(url,{id:token,event:'stop'})).status,200);
    assert.equal((await upload(url,params)).status,401);
    assert.equal(await page.locator('#task-dialog').isVisible(),false);
    // Errors must remain understandable instead of being overwritten by periodic native status.
    await page.route('**/api/gps/mobile',route=>route.request().method()==='POST'
      ?route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Тест: сервер временно недоступен'})}):route.continue());
    await page.click('#gps-start');
    await page.waitForFunction(()=>document.querySelector('#gps-feedback').textContent.includes('Тест: сервер'));
    await page.evaluate(()=>window.__androidTest.update({enabled:false}));
    assert.match(await page.locator('#gps-feedback').textContent(),/Тест: сервер/);
    await page.unroute('**/api/gps/mobile');
    await page.evaluate(()=>window.__androidTest.update({permission:false}));
    await page.click('#gps-start');
    await page.evaluate(()=>window.dispatchEvent(new Event('field-router-native-permission')));
    assert.match(await page.locator('#gps-feedback').textContent(),/нет разрешения/);
    assert.equal(await page.evaluate(()=>window.__androidTest.state.enabled),false);
    await page.evaluate(()=>window.__androidTest.update({permission:true}));
    await page.click('#gps-start');await page.waitForFunction(()=>window.__androidTest.state.enabled);
    token=await page.evaluate(()=>window.__androidTest.state.token);
    await page.locator('#account-menu > summary').click();await page.click('#sign-out');await page.waitForURL('**/login');
    assert.equal((await upload(url,{id:token,event:'stop'})).status,200);
    assert.deepEqual(errors,[]);
    console.log('PASS: Android UI, employee binding, scoped key, server GPS, compact status, permission actions, GPS stop, logout, revocation, 320–768px. Android bridge simulated.');
    console.log('Screenshot: '+path.join(directory,'android-gps.png'));
  } finally {
    if(browser)await browser.close();
    if(child&&child.exitCode===null){fs.writeFileSync(stopFile,'stop');await new Promise(resolve=>{const t=setTimeout(()=>{child.kill();resolve();},10000);child.once('exit',()=>{clearTimeout(t);resolve();});});}
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
