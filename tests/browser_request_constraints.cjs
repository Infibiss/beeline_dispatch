const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'..'),temporary=fs.mkdtempSync(path.join(os.tmpdir(),'request-constraints-'));
const stop=path.join(temporary,'stop'),delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
  let server,browser;
  try {
    server=spawn(process.env.PYTHON_BIN || 'python',['-B','tests/request_browser_server.py',
      '--state',path.join(temporary,'state.json'),'--stop',stop,'--model-fixture'],{cwd:root,windowsHide:true,
      env:{...process.env,PYTHONUTF8:'1',PYTHONPATH:path.join(root,'src')},stdio:['ignore','pipe','pipe']});
    const base=await new Promise((resolve,reject)=>{
      let output='';const timer=setTimeout(()=>reject(Error('Startup timeout')),20000);
      server.on('error',reject);server.stderr.on('data',chunk=>process.stderr.write(chunk));
      server.stdout.on('data',chunk=>{output+=chunk;const match=output.match(/http:\/\/127\.0\.0\.1:\d+/);
        if(match){clearTimeout(timer);resolve(match[0]);}});
    });
    browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_BIN});
    const context=await browser.newContext({viewport:{width:1440,height:1000}});
    await context.route('https://tile.openstreetmap.org/**',r=>r.fulfill({status:204}));
    const page=await context.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>{errors.push(d.message());d.dismiss();});
    const get=async route=>(await context.request.get(base+route)).json();
    const post=async(route,data)=>{const response=await context.request.post(base+route,{data});assert.equal(response.status(),200,await response.text());return response.json();};
    async function tab(name) {
      const menu=page.locator('#menu-toggle');
      if(await menu.isVisible() && await menu.getAttribute('aria-expanded')!=='true')await menu.click();
      await page.locator('#tab-'+name).click();await page.locator('#panel-'+name).waitFor({state:'visible'});
    }
    await page.goto(base);
    const frame=page.frameLocator('#plan-frame');
    await frame.locator('[data-request-id="a"]').first().click();
    await frame.locator('.assignment-ai-note').waitFor();
    assert.match(await frame.locator('.assignment-ai').textContent(),/Инженер автомобиль.*10:00/);
    assert.equal(await frame.locator('#request-card > details').getAttribute('open'),null);
    let explanationPosts=0;
    page.on('request',r=>{if(r.url().endsWith('/api/assignment-explanation'))explanationPosts++;});
    await frame.locator('[data-request-id="a"]').first().click();
    assert.equal(explanationPosts,0,'Repeated card uses browser cache');
    await frame.locator('#request-card > details > summary').click();
    await frame.locator('#request-card details summary').filter({hasText:'Сохранённое объяснение расчёта'}).click();
    assert.match(await frame.locator('#request-card').textContent(),/Сохранённое объяснение <img/);
    assert.equal(await frame.locator('#request-card img').count(),0);
    await frame.locator('[data-request-id="unassigned"]').first().click();
    await frame.locator('.assignment-ai-note').waitFor();
    assert.match(await frame.locator('.assignment-ai').textContent(),/пока без исполнителя/);
    await frame.locator('#request-card details summary').filter({hasText:'Проверка инженеров'}).click();
    assert.match(await frame.locator('#request-card').textContent(),/проверено мест: 3/);
    await tab('events');await page.selectOption('#event-type','emergency');
    assert.equal(await page.locator('#required-transport').isEnabled(),true);
    assert.equal(await page.locator('#emergency-window-start').isEnabled(),false);
    await page.selectOption('#required-transport','car');await page.fill('#duration','20');
    await page.check('#emergency-window-enabled');await page.fill('#emergency-window-start','12:00');await page.fill('#emergency-window-end','12:40');
    const submitted=page.waitForRequest(r=>r.url()===base+'/api/preview' && r.method()==='POST');
    await page.click('#preview');const payload=(await submitted).postDataJSON();
    assert.equal(payload.event.required_transport,'car');assert.equal(payload.event.window_start,720);assert.equal(payload.event.window_end,760);
    await page.locator('#candidates button').filter({hasText:'Посмотреть изменения'}).first().click();
    await page.waitForFunction(()=>!document.querySelector('#approve').disabled);
    await page.click('#approve');await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('План утверждён'));
    let state=await get('/api/state');const newId='emergency:'+payload.event.id;
    const saved=JSON.parse(fs.readFileSync(path.join(temporary,'state.json'),'utf8'));
    const request=saved.catalog[newId];
    assert.equal(request.required_transport,'car');assert.deepEqual(request.windows,[{start:720,end:760}]);
    const visit=state.plan.routes.flatMap(r=>r.visits).find(v=>v.request_id===newId);assert.ok(visit.start>=720 && visit.start<=760);
    // JSON follows the same path; wrong transport remains explicitly unassigned.
    await post('/api/inbox/requests',{requests:[{id:'json-walk',type:'new_request',anchor_id:'a',time:state.time,
      duration_minutes:20,work_kind:'local',window_start:800,window_end:850,required_transport:'walk'}]});
    state=await get('/api/state');
    const batch=await post('/api/preview',{version:state.version,event:{id:'json-replan',type:'replan',time:state.time}});
    const applied=await post('/api/approve',{version:state.version,batch_id:batch.id,candidate_id:batch.recommended_candidate_id});
    assert.equal(applied.plan.unassigned.find(u=>u.request_id==='request:json-walk').code,'NO_TRANSPORT');
    await page.reload();await tab('events');await page.selectOption('#event-type','new_request');
    assert.equal(await page.locator('#emergency-window-start').isEnabled(),false);
    assert.equal(await page.locator('#window-start').isEnabled(),true);
    await page.selectOption('#event-type','replan');assert.equal(await page.locator('#required-transport').isEnabled(),false);
    await page.selectOption('#event-type','emergency');await page.check('#emergency-window-enabled');
    await page.fill('#emergency-window-start','13:00');await page.fill('#emergency-window-end','14:00');
    for(const width of [320,390,768]) {await page.setViewportSize({width,height:900});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'overflow '+width);}
    await page.screenshot({path:path.join(temporary,'request-mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);console.log(JSON.stringify({ok:true,artifacts:temporary,checks:['initial explanation','safe saved text','slot diagnosis','manual emergency window/transport','JSON transport rejection','hidden fields','mobile']}));
  } finally {await browser?.close();fs.writeFileSync(stop,'stop');await delay(400);server?.kill();}
})().catch(e=>{console.error(e);process.exitCode=1;});
