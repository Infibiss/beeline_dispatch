// Optional end-to-end check. Requires Playwright, Edge, and local test certificates.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const https = require('node:https');
const os = require('node:os');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {login,password} = require('./browser_auth_helpers.cjs');
const root = path.resolve(__dirname, '..');
const python = process.env.PYTHON_BIN || 'python';
const tlsDir = path.join(root, 'output/local/https');
const servers = [];
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function start(tls) {
  const args = ['-B', 'tests/gps_browser_server.py', 'data/demo-road/Восток.json', '--state', path.join(os.tmpdir(), 'gps-browser-' + Date.now() + '.json'), '--port', '0', '--host', '0.0.0.0', '--time-limit', '.1'];
  if (tls) args.push('--certfile', path.join(tlsDir, 'server.crt'), '--keyfile', path.join(tlsDir, 'server.key'));
  const stopFile = path.join(os.tmpdir(), 'gps-test-stop-' + Date.now() + '-' + Math.random().toString(36).slice(2));
  args.push('--accounts',stopFile+'.sqlite3');
  const child = spawn(python, args, {cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], env: {...process.env, PYTHONUTF8: '1', PYTHONPATH: path.join(root, 'src'), GPS_TEST_STOP_FILE: stopFile, FIELD_ROUTER_ADMIN_PASSWORD:password}});
  child.stopFile = stopFile;
  servers.push(child);
  return await new Promise((resolve, reject) => {
    let text = ''; const timer = setTimeout(() => reject(new Error('Server startup timeout')), 15000);
    child.stderr.on('data', chunk => process.stderr.write(chunk));
    child.on('error', reject);
    child.once('exit', code => { clearTimeout(timer); reject(new Error('Server exited: ' + code)); });
    child.stdout.on('data', chunk => {
      text += chunk; const match = text.match(/https?:\/\/127\.0\.0\.1:(\d+)\//);
      if (match) { clearTimeout(timer); resolve((tls ? 'https' : 'http') + '://127.0.0.1:' + match[1]); }
    });
  });
}
async function eventually(check, timeout = 12000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await check()) return; await delay(150); }
  throw new Error('Condition timed out');
}
(async () => {
  let browser;
  try {
    const httpUrl = await start(false), tlsUrl = await start(true);
    // Verify TLS chain/SAN without disabling certificate validation in the HTTP client.
    await new Promise((resolve, reject) => https.get(tlsUrl + '/api/gps', {ca: fs.readFileSync(path.join(tlsDir, 'local-ca.crt'))}, r => { assert.equal(r.statusCode, 401); r.resume(); r.on('end', resolve); }).on('error', reject));
    browser = await chromium.launch({executablePath: process.env.BROWSER_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
    const context = await browser.newContext({viewport: {width: 1280, height: 900}, ignoreHTTPSErrors: true, permissions: ['geolocation'], geolocation: {latitude: 55.72, longitude: 37.75, accuracy: 8}});
    await login(context,httpUrl);await login(context,tlsUrl);
    await context.route('https://tile.openstreetmap.org/**', route => route.fulfill({status: 204}));
    const page = await context.newPage(), errors = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', m => { if (m.type() === 'error' && /Content Security Policy|Refused to/.test(m.text())) errors.push(m.text()); });
    await page.goto(httpUrl); await page.locator('#tab-gps').click();
    await page.waitForSelector('.gps-person');
    const before = await (await context.request.get(httpUrl + '/api/state')).json();
    await page.selectOption('#gps-channel', 'demo');
    const cases = [['stopped', 'Остановка во время выезда'], ['moving_away', 'Расстояние до адреса увеличивается'], ['near_site', 'Инженер рядом с адресом'], ['late', 'Есть риск не попасть в окно'], ['low_accuracy', 'Координата требует проверки'], ['stale', 'Нет свежей координаты'], ['shift_unconfirmed', 'Выход на смену не подтверждён']];
    for (const [value, label] of cases) {
      await page.selectOption('#gps-demo-case', value); await page.click('#gps-demo-run');
      await page.getByRole('heading', {name: label, exact: true}).waitFor({timeout: 18000});
    }
    const after = await (await context.request.get(httpUrl + '/api/state')).json();
    assert.equal(after.version, before.version); assert.deepEqual(after.fact_journal, before.fact_journal);
    for (const width of [320, 360, 390, 430, 768, 1280]) {
      await page.setViewportSize({width, height: 844}); await delay(150);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'GPS dispatcher overflow ' + width);
    }
    await page.setViewportSize({width: 390, height: 844});
    await page.screenshot({path: path.join(os.tmpdir(), 'gps-dispatcher-390.png'), fullPage: true});
    const mobile = await context.newPage(); mobile.on('pageerror', e => errors.push(e.message));
    await mobile.goto(tlsUrl + '/engineer'); await mobile.click('#tab-shift');
    await mobile.waitForFunction(() => !document.getElementById('gps-start').disabled);
    const eid = await mobile.locator('#engineer-select').inputValue();
    await mobile.click('#gps-start');
    const row = async () => (await (await context.request.get(tlsUrl + '/api/gps')).json()).rows.find(r => r.engineer_id === eid);
    await eventually(async () => (await row()).position?.lat === 55.72);
    assert.equal(await mobile.evaluate(() => window.isSecureContext), true);
    for (const width of [320, 360, 390, 430, 768, 1280]) {
      await mobile.setViewportSize({width, height: 844});
      assert.ok(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'GPS engineer overflow ' + width);
    }
    await mobile.setViewportSize({width: 390, height: 844});
    await mobile.screenshot({path: path.join(os.tmpdir(), 'gps-engineer-390.png'), fullPage: true});
    await mobile.click('#gps-stop');
    await eventually(async () => (await row()).tracking_state === 'off');
    // Applying SourceCraft's new crew settings invalidates a removed mobile profile.
    await mobile.click('#gps-start');
    await eventually(async () => (await row()).tracking_state === 'active');
    const crew = await (await context.request.get(tlsUrl + '/api/crew')).json();
    const renamed = crew.engineers.find(e => e.id === eid); renamed.id = 'mobile-crew-test'; renamed.name = 'Новая бригада';
    const previewResponse = await context.request.post(tlsUrl + '/api/crew/preview', {data: {version: crew.version, engineers: crew.engineers}});
    assert.equal(previewResponse.status(), 200);
    const draft = await previewResponse.json();
    const applied = await context.request.post(tlsUrl + '/api/crew/apply', {data: {version: crew.version, id: draft.id}});
    assert.equal(applied.status(), 200);
    await mobile.click('#refresh');
    await eventually(async () => await mobile.locator('#engineer-select').inputValue() === 'mobile-crew-test');
    assert.equal(await mobile.locator('#gps-state').textContent(), 'Выключена');
    const gpsAfterCrew = await (await context.request.get(tlsUrl + '/api/gps')).json();
    assert.ok(!gpsAfterCrew.rows.some(r => r.engineer_id === eid));
    assert.ok(gpsAfterCrew.rows.some(r => r.engineer_id === 'mobile-crew-test'));
    // Main task views remain usable after stopping the optional tracker.
    await mobile.click('#tab-tasks'); await mobile.waitForSelector('#panel-tasks:not([hidden])');
    await eventually(async () => (await mobile.locator('#plan-version').textContent()) === 'Версия 2');
    const address = Object.values(os.networkInterfaces()).flat().find(a => a.family === 'IPv4' && !a.internal && a.address.startsWith('192.168.1.'))?.address;
    if (address) {
      const insecure = await context.newPage();
      await login(context,httpUrl.replace('127.0.0.1',address));
      await insecure.goto(httpUrl.replace('127.0.0.1', address) + '/engineer'); await insecure.click('#tab-shift');
      await insecure.waitForFunction(() => document.querySelector('#engineer-select').disabled === false);
      assert.equal(await insecure.evaluate(() => window.isSecureContext), false);
      assert.equal(await insecure.locator('#gps-start').isDisabled(), true);
      assert.match(await insecure.locator('#gps-explanation').textContent(), /HTTPS/);
      await insecure.goto(httpUrl.replace('127.0.0.1', address) + '/crew');
      insecure.on('pageerror', e => errors.push(e.message));
      await insecure.waitForSelector('.crew-row');
      const count = await insecure.locator('.crew-row').count();
      await insecure.click('#add');
      assert.equal(await insecure.locator('.crew-row').count(), count + 1);
      for (const width of [320, 390, 768]) {
        await insecure.setViewportSize({width, height: 844});
        assert.ok(await insecure.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Crew overflow ' + width);
      }
    }
    assert.deepEqual(errors, []);
    console.log('PASS: GPS demo/TLS/stop; crew application updates mobile and GPS; crew editor works on LAN HTTP; responsive screens; no JS/CSP errors.');
  } finally {
    if (browser) await browser.close();
    // Graceful shutdown also closes the model process; no process-name killing.
    for (const child of servers) {
      if (child.exitCode !== null) continue;
      const closed = new Promise(resolve => child.once('close', resolve));
      fs.writeFileSync(child.stopFile, 'stop');
      await Promise.race([closed, delay(5000).then(() => { if (child.exitCode === null) child.kill(); })]);
      fs.rmSync(child.stopFile, {force: true});
    }
  }
})().catch(e => {console.error(e); process.exitCode = 1;});
