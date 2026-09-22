// Real East UI: ordinary arrivals preserve promises at mobile and desktop widths.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {password} = require('./browser_auth_helpers.cjs');
const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'ordinary-arrival-browser-'));
const stateFile = path.join(temporary, 'state.json'), stopFile = path.join(temporary, 'stop');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function appointments(routes) {
  return Object.fromEntries(routes.flatMap(route => route.visits.map(visit =>
    [visit.request_id, {engineer:route.engineer_id, start:visit.start}])));
}

function assertPreserved(before, batch, expectAssigned) {
  const previous = appointments([...before.frozen, ...before.plan.routes]);
  const oldIds = new Set(Object.keys(previous));
  const requestId = 'request:' + batch.event.id;
  assert.equal(batch.insertion_policy, 'preserve_existing_assignments_and_starts');
  assert.equal(batch.candidates.length, 3);
  for (const candidate of batch.candidates) {
    const routes = [...batch.frozen, ...candidate.plan.routes];
    const after = appointments(routes);
    assert.deepEqual(Object.fromEntries(Object.entries(after).filter(([key]) => oldIds.has(key))), previous);
    for (const route of before.plan.routes) {
      const priorOrder = route.visits.map(visit => visit.request_id);
      const currentOrder = routes.filter(row => row.engineer_id === route.engineer_id)
        .flatMap(row => row.visits.map(visit => visit.request_id)).filter(key => priorOrder.includes(key));
      assert.deepEqual(currentOrder, priorOrder, 'Existing visit order must survive insertion');
    }
    assert.deepEqual(candidate.plan.unassigned.filter(item => item.request_id !== requestId), before.plan.unassigned);
    assert.equal(candidate.ordinary_request.assigned, expectAssigned);
    assert.equal(candidate.facts.reassigned, 0);
    assert.equal(candidate.facts.shifted, 0);
    if (expectAssigned) assert.ok(after[requestId], 'The one-minute local job must fit a real gap');
    else assert.equal(candidate.plan.unassigned.find(item => item.request_id === requestId)?.code, 'NO_FREE_GAP');
  }
  return requestId;
}

(async () => {
  let server, browser;
  try {
    server = spawn(process.env.PYTHON_BIN || 'python', ['-B', 'tests/gps_browser_server.py',
      'data/demo-road/Восток.json', '--state', stateFile, '--port', '0', '--time-limit', '.1', '--disable-gps'], {
      cwd:root, windowsHide:true, stdio:['ignore', 'pipe', 'pipe'],
      env:{...process.env, PYTHONUTF8:'1', PYTHONPATH:path.join(root, 'src'), GPS_TEST_STOP_FILE:stopFile,
        FIELD_ROUTER_ADMIN_PASSWORD:password}
    });
    const base = await new Promise((resolve, reject) => {
      let output = '';
      const timer = setTimeout(() => reject(Error('Server startup timeout')), 20000);
      server.on('error', error => {clearTimeout(timer); reject(error);});
      server.once('exit', code => {clearTimeout(timer); reject(Error('Server exited: ' + code));});
      server.stderr.on('data', chunk => process.stderr.write(chunk));
      server.stdout.on('data', chunk => {
        output += chunk;
        const match = output.match(/http:\/\/127\.0\.0\.1:\d+/);
        if (match) {clearTimeout(timer); resolve(match[0]);}
      });
    });
    browser = await chromium.launch({headless:true,
      executablePath:process.env.BROWSER_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
    const context = await browser.newContext({viewport:{width:390, height:844}});
    await context.route('https://tile.openstreetmap.org/**', route => route.fulfill({status:204}));
    const errors = [];
    context.on('page', page => {
      page.on('pageerror', error => errors.push(error.message));
      page.on('console', message => {
        if (message.type() === 'error' && /Content Security Policy|Refused to/.test(message.text())) errors.push(message.text());
      });
    });
    const page = await context.newPage();
    await page.goto(base);
    await page.waitForURL('**/login');
    await page.fill('#username', 'admin'); await page.fill('#password', password);
    await page.click('#login-submit'); await page.waitForSelector('#sign-out');
    await page.waitForFunction(() => document.querySelector('#version').textContent === '1');
    const readState = async () => {
      const response = await context.request.get(base + '/api/state');
      assert.equal(response.status(), 200); return response.json();
    };
    const before = await readState();
    const sample = JSON.parse(fs.readFileSync(path.join(root, 'data/demo-road/Восток.json'), 'utf8'));
    const engineer = sample.scenario.engineers.find(item => item.skills.includes('local') &&
      before.plan.routes.some(route => route.engineer_id === item.id && route.visits.at(-1).end < item.shift.end));
    assert.ok(engineer, 'East fixture needs a local engineer with time after the last appointment');
    const anchor = before.plan.routes.find(route => route.engineer_id === engineer.id).visits.at(-1).request_id;
    await page.click('#tab-events');

    async function ordinary(duration) {
      await page.selectOption('#event-type', 'new_request');
      await page.selectOption('#work-kind', 'local');
      await page.fill('#event-time', '09:00'); await page.selectOption('#event-target', anchor);
      await page.fill('#duration', String(duration)); await page.fill('#router', '0');
      await page.fill('#new-tv', '0'); await page.fill('#window-start', '09:00');
      await page.fill('#window-end', '22:00');
      assert.equal(await page.locator('#redirect').isVisible(), false);
      assert.match(await page.locator('#event-policy').textContent(), /без переноса прежних назначений/);
      const responsePromise = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/api/preview') && response.request().method() === 'POST');
      await page.click('#preview');
      const response = await responsePromise, batch = await response.json();
      assert.equal(response.status(), 200, JSON.stringify(batch));
      await page.waitForSelector('.candidate');
      assert.equal(await page.locator('#error').isVisible(), false);
      return batch;
    }
    async function layout(width, name) {
      await page.setViewportSize({width, height:844});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Horizontal overflow at ' + width);
      await page.screenshot({path:path.join(temporary, name + '.png'), fullPage:true});
    }

    const batch = await ordinary(1), newId = assertPreserved(before, batch, true);
    assert.deepEqual(await readState(), before, 'Preview must not change the published state');
    await layout(390, 'ordinary-mobile'); await layout(1280, 'ordinary-desktop');
    const chosenId = await page.locator('.candidate').first().getAttribute('data-id');
    await page.locator('.candidate').first().getByRole('button').first().click();
    const approval = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/api/approve'));
    await page.click('#approve');
    const approvedResponse = await approval, approved = await approvedResponse.json();
    assert.equal(approvedResponse.status(), 200, JSON.stringify(approved));
    assert.equal(approved.version, before.version + 1);
    assert.deepEqual(approved.plan, batch.candidates.find(candidate => candidate.id === chosenId).plan);
    assert.ok(appointments(approved.plan.routes)[newId]);
    const persisted = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
    assert.deepEqual(persisted.plan, approved.plan); assert.equal(persisted.version, approved.version);
    assert.equal(persisted.catalog[newId].provenance.work_kind, 'local');
    await page.reload();
    await page.waitForFunction(version => Number(document.querySelector('#version').textContent) === version, approved.version);
    assert.deepEqual((await readState()).plan, approved.plan);
    await page.click('#tab-events');
    const impossible = await ordinary(1440);
    assertPreserved(approved, impossible, false);
    assert.match(await page.locator('#candidates').textContent(), /не назначена|без назначения/i);
    await layout(1280, 'ordinary-no-gap');
    assert.deepEqual(await readState(), approved, 'An unapproved impossible arrival must not change the day');

    await page.selectOption('#event-type', 'emergency');
    assert.equal(await page.locator('#preserve').isVisible(), false);
    assert.equal(await page.locator('#preserve').isDisabled(), true);
    assert.equal(await page.locator('#preserve').isChecked(), true);
    assert.match(await page.locator('#event-policy').textContent(), /Начатая работа завершается.*Ориентир.*2 часов.*превышение/);
    await page.fill('#duration', '1'); await page.fill('#router', '0');
    const emergencyResponse = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/api/preview'));
    await page.click('#preview');
    const emergencyHttp = await emergencyResponse, emergency = await emergencyHttp.json();
    assert.equal(emergencyHttp.status(), 200, JSON.stringify(emergency));
    assert.match(emergency.notice, /120 минут.*ориентир.*не запрещает/);
    assert.ok(emergency.candidates.every(candidate => candidate.emergency.response_target_minutes === 120));
    await page.waitForSelector('.candidate');
    if (emergency.candidates.some(candidate => candidate.emergency.exceeds_response_target)) {
      assert.match(await page.locator('#candidates').textContent(), /ориентира реакции 2 часа/);
    }
    await layout(390, 'emergency-guidance-mobile');
    assert.deepEqual(await readState(), approved);
    assert.deepEqual(errors, []);
    console.log('PASS ordinary UI: feasible insertion, unchanged appointments/order, no-gap result, approval persistence, mobile/desktop, soft emergency target');
    console.log('Screenshots: ' + temporary);
  } finally {
    if (browser) await browser.close();
    if (server) {
      fs.writeFileSync(stopFile, 'stop');
      for (let n = 0; n < 60 && server.exitCode === null; n++) await delay(100);
      if (server.exitCode === null) server.kill();
    }
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
