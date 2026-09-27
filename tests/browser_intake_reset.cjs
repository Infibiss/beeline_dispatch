// Real browser, real 66-job source, isolated state: intake, autopilot, reset.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'intake-reset-browser-'));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(test, label, timeout=45000) {
  const end = Date.now()+timeout;
  while(Date.now()<end) {if(await test()) return; await delay(200);}
  throw Error('Timeout: '+label);
}
(async()=>{
  let server, browser;
  try {
    server=spawn(process.env.PYTHON_BIN || 'python', ['-B','tests/intake_browser_server.py',
      '--state',path.join(temporary,'state.json')], {cwd:root,windowsHide:true,
      env:{...process.env,PYTHONUTF8:'1',PYTHONPATH:path.join(root,'src')},stdio:['ignore','pipe','pipe']});
    const base=await new Promise((resolve,reject)=>{
      let output='';const timeout=setTimeout(()=>reject(Error('Server startup timeout')),20000);
      server.on('error',reject);server.stderr.on('data',chunk=>process.stderr.write(chunk));
      server.stdout.on('data',chunk=>{output+=chunk;const match=output.match(/http:\/\/127\.0\.0\.1:\d+/);
        if(match){clearTimeout(timeout);resolve(match[0]);}});
    });
    browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
    const context=await browser.newContext({viewport:{width:1440,height:1000}});
    await context.route('https://tile.openstreetmap.org/**',route=>route.fulfill({status:204}));
    const page=await context.newPage(), errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    const get=async route=>(await context.request.get(base+route)).json();
    const post=async(route,data)=>{const response=await context.request.post(base+route,{data});assert.equal(response.status(),200,await response.text());return response.json();};
    await page.goto(base);await page.click('#tab-events');
    await page.waitForSelector('#stream-demo-reset');
    await until(async()=>await page.locator('#stream-demo-start').isEnabled(),'stream ready');
    const initial=await get('/api/state');
    assert.equal(initial.version,1);assert.equal(initial.time,540);
    assert.deepEqual(initial.dispatch_notifications,[]);assert.deepEqual(initial.incoming_requests,[]);
    assert.equal(initial.assignment_summary.assigned,52);
    assert.equal(initial.assignment_summary.requires_assignment,14);
    assert.equal(await page.locator('#coverage').textContent(),'52 / 66');
    assert.equal(await page.locator('#coverage-pending').textContent(),'14');
    assert.equal(await page.locator('#replanning-pool-summary').textContent(),'Требуют назначения · 14');
    await page.click('#tab-plan');
    const dayPlan=page.frameLocator('#plan-frame');
    await until(async()=>await dayPlan.locator('#metrics .metric strong').count()>=2,'day plan counters');
    assert.equal(await dayPlan.locator('#metrics .metric strong').nth(0).textContent(),'52');
    assert.equal(await dayPlan.locator('#metrics .metric strong').nth(1).textContent(),'14');
    await dayPlan.locator('#engineer-select').selectOption('__unassigned__');
    assert.equal(await dayPlan.locator('#request-list .request-item').count(),14);
    assert.match(await dayPlan.locator('#request-list').textContent(),/Нужен новый инженер/);
    await page.click('#tab-events');
    assert.equal(await page.locator('#planning-mode').inputValue(),'manual');
    // A real ten-second interval, no accelerated timers.
    await page.click('#stream-demo-start');
    await until(async()=>(await get('/api/inbox')).incoming_requests.length===1,'first timed arrival');
    await page.click('#stream-demo-pause');
    assert.equal((await get('/api/inbox')).incoming_requests[0].status,'new');
    const current=await get('/api/state');
    const batch=await post('/api/preview',{demo_run_id:current.demo_run_id,version:current.version,
      event:{id:'browser-manual',type:'replan',time:current.time}});
    const applied=await post('/api/approve',{demo_run_id:current.demo_run_id,version:current.version,
      batch_id:batch.id,candidate_id:batch.recommended_candidate_id});
    // External manual updates require explicitly loading the published revision.
    await page.reload();await page.click('#tab-events');
    await until(async()=>(await page.locator('#application-visual-version').textContent()).includes(String(applied.version)),'manual applied panel');
    assert.equal((await get('/api/inbox')).incoming_requests.filter(item=>item.status==='new').length,0);
    await page.click('#stream-demo-reset');
    await until(async()=>(await get('/api/state')).version===1,'reset manual run');
    await until(async()=>await page.locator('#stream-demo-start').isEnabled(),'ready after reset');
    const fresh=await get('/api/state');
    assert.notEqual(fresh.demo_run_id,initial.demo_run_id);
    assert.deepEqual(fresh.plan,initial.plan);assert.deepEqual(fresh.replanning_pool,initial.replanning_pool);
    assert.deepEqual(fresh.dispatch_notifications,[]);assert.deepEqual(fresh.decision_log,[]);
    // Second tab must recognize reset even when the numerical version is 1 again.
    const second=await context.newPage();await second.goto(base);await second.click('#tab-events');
    await second.waitForSelector('#stream-demo-reset');
    await page.click('#stream-demo-reset');
    const next=await get('/api/state');
    await until(async()=>(await second.locator('#plan-frame').getAttribute('src')).includes(next.demo_run_id),'cross-tab new generation');
    await second.close();
    await page.selectOption('#planning-mode','autopilot');await page.fill('#auto-count','3');await page.click('#planning-save');
    await until(async()=>await page.locator('#stream-demo-start').isEnabled(),'autopilot saved');
    await page.click('#stream-demo-start');
    await until(async()=>(await get('/api/inbox')).incoming_requests.length>=3,'three timed arrivals');
    await page.click('#stream-demo-pause');
    await until(async()=>(await get('/api/inbox')).dispatch_notifications.some(item=>item.kind==='auto_applied'),'automatic approval');
    const auto=await get('/api/state');
    await until(async()=>(await page.locator('#application-visual-version').textContent()).includes(String(auto.version)),'autopilot UI refresh');
    const inbox=await get('/api/inbox');
    assert.equal(auto.assignment_summary.assigned,auto.plan.metrics.assigned);
    assert.equal(auto.assignment_summary.requires_assignment,auto.plan.metrics.unassigned);
    const assigned=new Set(auto.plan.routes.flatMap(route=>route.visits.map(visit=>visit.request_id)));
    const unassigned=new Set(auto.plan.unassigned.map(item=>item.request_id));
    assert.equal(assigned.size+unassigned.size,auto.requests.length);
    assert.deepEqual([...new Set(auto.replanning_pool.filter(item=>item.status==='unassigned').map(item=>item.request_id))].sort(),[...unassigned].sort());
    for(const item of inbox.incoming_requests) {
      const requestId=(item.event.type==='emergency'?'emergency:':'request:')+item.id;
      if(item.status==='assigned')assert.ok(assigned.has(requestId),'assigned inbox job in route');
      if(item.status==='unassigned')assert.ok(unassigned.has(requestId),'unassigned inbox job in pool');
    }
    assert.equal(inbox.incoming_requests.length,3);
    assert.equal(inbox.incoming_requests.filter(item=>item.status==='new').length,0);
    assert.equal(inbox.dispatch_notifications.filter(item=>item.kind==='auto_applied').length,1);
    assert.match(await page.locator('#replanning-pool-summary').textContent(),new RegExp(String(unassigned.size)));
    assert.match(await page.locator('#plan-frame').getAttribute('src'),new RegExp('version='+auto.version));
    await page.click('#tab-notifications');
    assert.match(await page.locator('#panel-notifications').textContent(),/Автопилот применил/);
    await page.screenshot({path:path.join(temporary,'notifications.png'),fullPage:true});
    await page.click('#tab-events');await page.click('#stream-demo-start');
    await until(async()=>(await get('/api/inbox')).incoming_requests.length===6,'remaining timed arrivals');
    await until(async()=>(await get('/api/inbox')).dispatch_notifications.filter(item=>item.kind==='auto_applied').length===2,'second automatic batch');
    await until(async()=>!(await page.locator('#stream-demo-start').isVisible()),'completed stream hides continue');
    assert.equal(await page.locator('#stream-demo-reset').isEnabled(),true);
    await page.click('#stream-demo-reset');
    await until(async()=>(await get('/api/state')).version===1,'final reset');
    const final=await get('/api/state');
    assert.deepEqual(final.dispatch_notifications,[]);assert.deepEqual(final.incoming_requests,[]);
    assert.deepEqual(final.plan,initial.plan);assert.deepEqual(final.history,[]);
    await until(async()=>await page.locator('#stream-demo-start').isEnabled(),'final ready');
    assert.equal(await page.locator('#application-visual').isVisible(),false);
    assert.equal(await page.locator('#plan-version option').count(),1);
    await page.screenshot({path:path.join(temporary,'fresh-start.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Mobile horizontal overflow');
    assert.equal(await page.locator('#stream-demo-reset').isVisible(),true);
    await page.screenshot({path:path.join(temporary,'fresh-mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,artifacts:temporary,assigned:assigned.size,unassigned:unassigned.size,
      checks:['10-second intake','manual apply','autopilot batch of 3','pool/status consistency','plan iframe refresh','repeat reset','cross-tab reset']}));
  } finally {await browser?.close();server?.kill();}
})().catch(error=>{console.error(error);process.exitCode=1;});
