// Read-only browser regression against an already running local dispatcher.
// Future responses are intercepted in this browser only; server data is never edited.
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.DAY_CONTROL_URL || 'http://127.0.0.1:8769';
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.BROWSER_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1080}});
    await context.route('https://tile.openstreetmap.org/**',r=>r.fulfill({status:204}));
    // This read-only suite does not spend provider calls when the initial card opens.
    await context.route('**/api/assignment-explanation',r=>{
      const data=r.request().postDataJSON();return r.fulfill({json:{source:'verified',paragraphs:[],notice:'Проверка интерфейса',
        version:data.version,request_id:data.request_id,demo_run_id:data.demo_run_id || null}});
    });
    const page = await context.newPage(), errors=[], posts=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('request',r=>{if(r.method()==='POST' && r.url().startsWith(base) && !r.url().endsWith('/api/assignment-explanation'))posts.push(r.url());});
    const before = await (await context.request.get(base+'/api/state')).json();
    await page.goto(base);
    await page.locator('#tab-risk').click();
    await page.waitForSelector('.risk-filter');
    await page.waitForFunction(()=>!document.querySelector('#risk-refresh').disabled);
    assert.equal(await page.locator('#risk-error').isVisible(),false);
    const source = await (await context.request.get(base+'/api/risk')).json();
    assert.equal(await page.locator('.risk-filter').count(),5);
    assert.equal(await page.locator('[data-filter="deadline"] strong').textContent(),String(source.attention.counts.deadline));
    assert.equal(await page.locator('[data-filter="unassigned"] strong').textContent(),String(source.attention.counts.unassigned));
    assert.equal(await page.locator('#risk-resources').getAttribute('open'),null);
    assert.equal(await page.locator('#risk-forecasts').isVisible(),false);
    assert.equal(await page.locator('.risk-tier').count(),3);
    for(const tier of ['now','prepare','watch']){
      assert.equal(await page.locator('.risk-tier[data-tier="'+tier+'"] .risk-tier-heading strong').textContent(),String(source.attention.tiers[tier]));
      assert.ok(await page.locator('.risk-tier[data-tier="'+tier+'"] .risk-issue').count()<=6);
    }
    assert.equal(await page.locator('.risk-tier[data-tier="watch"]').getAttribute('open'),null);
    await page.screenshot({path:path.resolve('output/local/day-control-desktop.png'),fullPage:true,animations:'disabled'});
    for(const width of [320,390,768]) {
      await page.setViewportSize({width,height:900});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Overflow at '+width);
    }
    await page.setViewportSize({width:390,height:900});
    await page.screenshot({path:path.resolve('output/local/day-control-mobile.png'),fullPage:true,animations:'disabled'});
    await page.setViewportSize({width:1440,height:1080});
    await page.locator('[data-filter="deadline"]').click();
    assert.ok(await page.locator('.risk-issue:visible').count()>0);
    await page.screenshot({path:path.resolve('output/local/day-control-deadlines.png'),fullPage:true,animations:'disabled'});
    await page.locator('[data-filter="deadline"]').click();
    await page.locator('[data-filter="tight"]').click();
    assert.equal(await page.locator('.risk-issue').count(),['now','prepare','watch'].reduce((n,tier)=>n+Math.min(6,source.attention.items.filter(i=>i.tier===tier && i.categories.includes('tight')).length),0));
    const factAction=page.locator('.risk-issue button').filter({hasText:'Уточнить факт'}).first();
    if(await factAction.count()) {
      await factAction.click();assert.equal(await page.locator('#panel-facts').isVisible(),true);
      await page.locator('#tab-risk').click();
      await page.waitForFunction(()=>!document.querySelector('#risk-refresh').disabled);
    }
    await page.locator('#risk-attention-all').click();
    const first=source.attention.items.find(i=>i.requests.length);
    if(first) {
      await page.locator('#risk-attention-search').fill(first.requests[0].request_id);
      assert.ok(await page.locator('.risk-issue').count()>0);
      await page.locator('#risk-attention-search').fill('__not_found__');
      assert.equal(await page.locator('.risk-issue').count(),0);
      await page.locator('#risk-attention-all').click();
    }
    // A received current snapshot removes resolved incidents and updates all counters.
    const resolved=structuredClone(source);
    resolved.attention.items=[];resolved.attention.counts={unassigned:0,deadline:0,tight:0,verify:0,overtime:0};
    let response=resolved;
    await page.route('**/api/risk',r=>r.fulfill({json:response}));
    await page.locator('#risk-refresh').click();
    await page.waitForFunction(()=>!document.querySelector('#risk-refresh').disabled);
    assert.equal(await page.locator('.risk-issue').count(),0);
    assert.equal(await page.locator('[data-filter="unassigned"] strong').textContent(),'0');
    assert.equal(await page.locator('[data-filter="deadline"] strong').textContent(),'0');
    // Old version is not rendered as current, even if its counters look plausible.
    response=structuredClone(source);response.context.plan_version=before.version+100;
    await page.locator('#risk-refresh').click();
    await page.waitForFunction(()=>!document.querySelector('#risk-refresh').disabled);
    assert.equal(await page.locator('#risk-error').isVisible(),true);
    assert.equal(await page.locator('.risk-issue').count(),0);
    assert.deepEqual(await (await context.request.get(base+'/api/state')).json(),before);
    assert.deepEqual(posts,[]);assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,counts:source.attention.counts,incidents:source.attention.items.length,posts,errors}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
