// Synthetic employee/account and APK bytes; no real GPS, certificate or APK files.
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {password}=require('./browser_auth_helpers.cjs');
const root=path.resolve(__dirname,'..');
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'engineer-apk-qa-'));
const bytes=Buffer.from([80,75,3,4,0,1,2,3]);
(async()=>{
  let server,browser;
  try {
    server=spawn(process.env.PYTHON_BIN||'python',['-B','tests/engineer_return_browser_server.py',tmp],{
      cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],
      env:{...process.env,PYTHONUTF8:'1',PYTHONPATH:path.join(root,'src'),FIELD_ROUTER_ADMIN_PASSWORD:password}});
    const base=await new Promise((resolve,reject)=>{
      let text='';const timeout=setTimeout(()=>reject(Error('Server startup timeout')),20000);
      server.once('error',e=>{clearTimeout(timeout);reject(e);});
      server.once('exit',code=>{clearTimeout(timeout);reject(Error('Server exited: '+code));});
      server.stderr.on('data',data=>process.stderr.write(data));
      server.stdout.on('data',data=>{text+=data;const match=text.match(/http:\/\/127\.0\.0\.1:\d+/);
        if(match){clearTimeout(timeout);resolve(match[0]);}});
    });
    browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_BIN||'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
    const errors=[];
    const openEmployee=async native=>{
      const context=await browser.newContext({viewport:{width:390,height:844},
        ...(native?{userAgent:'Mozilla/5.0 Android FieldRouterAndroid/1'}:{})});
      const auth=await context.request.post(base+'/api/auth/login',{data:{username:'worker',password}});
      assert.equal(auth.status(),200);
      const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
      await page.route('https://**',route=>route.abort());
      await page.goto(base+(await auth.json()).redirect);
      await page.click('#tab-shift');
      assert.equal(await page.locator('#android-app').isVisible(),true);
      assert.equal(await page.locator('#android-app').evaluate(node=>node.tagName),'DETAILS');
      assert.equal(await page.locator('#android-apk-download').isVisible(),false);
      await page.locator('#android-app > summary').click();
      assert.equal(await page.locator('#android-apk-download').isVisible(),true);
      return {context,page};
    };
    const {context,page}=await openEmployee(false);
    const href=await page.locator('#android-apk-download').getAttribute('href');
    assert.equal(href,'/r/east/engineer-app.apk');
    const anonymous=await browser.newContext();
    const denied=await anonymous.request.get(base+href,{maxRedirects:0});
    assert.equal(denied.status(),303);assert.equal(denied.headers().location,'/login');
    let mode='good',calls=0;
    await page.route('**/engineer-app.apk',async route=>{
      calls++;
      if(mode==='redirect')return route.fulfill({status:303,headers:{Location:'/login'}});
      if(mode==='network')return route.abort('failed');
      if(mode==='401')return route.fulfill({status:401,contentType:'application/json',body:JSON.stringify({error:'Expired'})});
      if(mode==='404')return route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:'Android-приложение ещё не собрано. Обратитесь к диспетчеру.'})});
      if(mode==='text')return route.fulfill({status:200,contentType:'text/plain',body:'Not an APK'});
      if(mode==='zip')return route.fulfill({status:200,contentType:'application/vnd.android.package-archive',body:'invalid-file'});
      if(mode==='slow')await new Promise(resolve=>setTimeout(resolve,300));
      return route.fulfill({status:200,contentType:'application/vnd.android.package-archive',body:bytes});
    });
    const download=async()=>{
      const pending=page.waitForEvent('download');await page.click('#android-apk-download');
      const result=await pending;
      assert.equal(result.suggestedFilename(),'field-engineer-debug.apk');
      assert.deepEqual(fs.readFileSync(await result.path()),bytes);
      await page.waitForFunction(()=>document.querySelector('#android-apk-status').textContent.startsWith('Скачано:'));
      assert.equal(await page.locator('#android-apk-download').getAttribute('aria-disabled'),null);
    };
    await download();
    for(const [failure,expected] of [['404','ещё не собрано'],['401','Войдите'],['redirect','Войдите'],
      ['text','Не удалось получить APK'],['zip','не является APK'],['network','Не удалось скачать APK']]) {
      mode=failure;await page.click('#android-apk-download');
      await page.waitForFunction(text=>document.querySelector('#android-apk-status').textContent.includes(text),expected);
      assert.equal(await page.locator('#android-apk-download').getAttribute('aria-disabled'),null);
      mode='good';await download(); // A failed request must never leave the action stuck.
    }
    mode='slow';const before=calls,pending=page.waitForEvent('download');
    await page.locator('#android-apk-download').evaluate(link=>{link.click();link.click();});
    await pending;assert.equal(calls,before+1);
    await page.waitForFunction(()=>!document.querySelector('#android-apk-download').hasAttribute('aria-disabled'));
    const native=await openEmployee(true);
    assert.equal(await native.page.locator('#android-apk-download').textContent(),'Скопировать ссылку на APK');
    assert.equal(await native.page.locator('#android-apk-download').getAttribute('download'),null);
    let nativeRequests=0;
    await native.page.route('**/engineer-app.apk',route=>{nativeRequests++;return route.abort();});
    await native.page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,
      value:{writeText:async value=>{window.__copiedApk=value;}}}));
    await native.page.click('#android-apk-download');
    await native.page.waitForFunction(()=>document.querySelector('#android-apk-status').textContent.startsWith('Ссылка скопирована'));
    assert.equal(await native.page.evaluate(()=>window.__copiedApk),base+href);
    assert.equal(new URL(await native.page.evaluate(()=>window.__copiedApk)).search,'');
    await native.page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,
      value:{writeText:async()=>{throw Error('Clipboard disabled');}}}));
    await native.page.click('#android-apk-download');
    await native.page.locator('#android-apk-copy-link').waitFor({state:'visible'});
    assert.equal(await native.page.locator('#android-apk-copy-link').inputValue(),base+href);
    assert.equal(await native.page.locator('#android-apk-copy-link').getAttribute('readonly'),'');
    assert.match(await native.page.locator('#android-apk-status').textContent(),/^Скопируйте ссылку/);
    for(const width of [320,390,768]) {
      await native.page.setViewportSize({width,height:844});
      assert.ok(await native.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    }
    await native.page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,
      value:{writeText:async value=>{window.__copiedApk=value;}}}));
    await native.page.click('#android-apk-download');
    await native.page.waitForFunction(()=>!document.querySelector('#android-apk-copy-link'));
    assert.equal(nativeRequests,0);
    assert.deepEqual(errors,[]);
    console.log('PASS: profile APK entry, authenticated browser download, 404/401/login/network/type/signature recovery, duplicate-click guard, installed APK copy/fallback, 320–768px. Synthetic file only; Android WebView not run.');
    await context.close();await native.context.close();await anonymous.close();
  } finally {
    if(browser)await browser.close();
    if(server&&server.exitCode===null) {
      fs.writeFileSync(path.join(tmp,'stop'),'stop');
      await new Promise(resolve=>{const timer=setTimeout(()=>{server.kill();resolve();},10000);
        server.once('exit',()=>{clearTimeout(timer);resolve();});});
    }
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
