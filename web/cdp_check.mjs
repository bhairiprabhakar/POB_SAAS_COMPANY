/* CDP driver: load the /app admin dashboard in headless Chrome with a real
 * session seeded into localStorage, then report console errors, failed API
 * calls and the final rendered text. */
import { readFileSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9222;
let raw = readFileSync(process.env.POB_SESS || path.join(tmpdir(), 'pob_sess.json'), 'utf8');
if (raw.charCodeAt(0) === 0xFEFF) raw = raw.slice(1);
const session = JSON.parse(raw);

const userData = mkdtempSync(path.join(tmpdir(), 'pob-cdp-'));
const chrome = spawn(CHROME, [
  '--headless=new', '--disable-gpu', '--no-sandbox',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${userData}`,
  '--window-size=1600,1000', 'about:blank',
], { stdio: 'ignore' });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const wait = async (fn, timeout = 15000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    try { const v = await fn(); if (v) return v; } catch { /* retry */ }
    await sleep(200);
  }
  throw new Error('timeout');
};

async function main() {
  await sleep(2000);
  const version = await wait(() => fetch(`http://127.0.0.1:${PORT}/json/version`).then((r) => r.json()), 20000);
  const browser = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    browser.addEventListener('open', res);
    browser.addEventListener('error', rej);
  });
  console.log('connected to chrome', version.Browser);

  let id = 0;
  const pending = new Map();
  const sendRaw = (msg) => new Promise((resolve, reject) => {
    const mid = msg.id;
    pending.set(mid, { resolve, reject });
    browser.send(JSON.stringify(msg));
  });
  const send = (method, params = {}, sess) =>
    sendRaw(sess ? { id: ++id, method, params, sessionId: sess } : { id: ++id, method, params });

  browser.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const p = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? p.reject(new Error(msg.error.message)) : p.resolve(msg.result);
    }
  });

  // browser-level target
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  await sleep(500);

  const errors = [];
  const badResponses = [];

  // attach to the new target (flat protocol: route via sessionId)
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });

  // helper to send within the session
  const ssend = (method, params = {}) => send(method, params, sessionId);

  // forward session replies
  browser.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.sessionId !== sessionId || !msg.id) return;
    if (pending.has(msg.id)) {
      const p = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? p.reject(new Error(msg.error.message)) : p.resolve(msg.result);
    }
  });

  const seed = `localStorage.setItem('pob_saas_session', ${JSON.stringify(JSON.stringify(session))});`;
  await ssend('Page.enable');
  await ssend('Runtime.enable');
  await ssend('Log.enable');
  await ssend('Network.enable');
  await ssend('Page.addScriptToEvaluateOnNewDocument', { source: seed });

  // listeners for console/errors/network
  browser.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.sessionId !== sessionId) return;
    if (msg.method === 'Runtime.exceptionThrown') {
      const d = msg.params.exceptionDetails;
      errors.push('EXCEPTION: ' + (d.exception?.description || d.text || '').split('\n')[0]);
    }
    if (msg.method === 'Log.entryAdded') {
      const e = msg.params.entry;
      if (e.level === 'error') errors.push('LOG: ' + e.text);
    }
    if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
      errors.push('CONSOLE: ' + msg.params.args.map((a) => a.value ?? a.description ?? '').join(' '));
    }
    if (msg.method === 'Network.responseReceived') {
      const r = msg.params.response;
      if (r.url.includes('/api/') && r.status >= 400) {
        badResponses.push(`${r.status} ${r.url}`);
      }
    }
  });

  await ssend('Page.navigate', { url: 'http://127.0.0.1:8000/app' });
  console.log('navigated, settling...');
  await sleep(8000);

  const evalRes = await ssend('Runtime.evaluate', {
    expression: 'document.body ? document.body.innerText : "(no body)"',
    returnByValue: true,
  });
  const text = evalRes.result?.value || '';

  console.log('--- RENDERED /app TEXT (first 1200 chars) ---');
  console.log(text.slice(0, 1200));
  console.log('\n--- API FAILURES ---');
  console.log(badResponses.length ? badResponses.join('\n') : '(none)');
  console.log('\n--- CONSOLE/EXCEPTIONS ---');
  const realErrors = errors.filter((e) => !e.includes('favicon'));
  console.log(realErrors.length ? realErrors.join('\n') : '(none)');

  // also walk the other admin routes and surface any render exceptions
  const extraRoutes = process.env.POB_ROUTES || '';
  if (extraRoutes) {
    for (const route of extraRoutes.split(',')) {
      errors.length = 0; badResponses.length = 0;
      await ssend('Page.navigate', { url: `http://127.0.0.1:8000${route}` });
      await sleep(4500);
      const r2 = await ssend('Runtime.evaluate', {
        expression: `document.body ? document.body.innerText.length : -1`, returnByValue: true,
      });
      const ex = errors.filter((e) => !e.includes('favicon'));
      const bad = badResponses.filter((b) => !b.includes('favicon'));
      console.log(`\n=== ${route} => bodyLen=${r2.result?.value} errors=${ex.length} apiFailures=${bad.length} ===`);
      if (ex.length) console.log(ex.join('\n'));
    }
  }

  if (process.env.POB_ANALYTICS) {
    await checkAnalyticsTabs({ ssend, errors, badResponses });
  }

  await send('Target.closeTarget', { targetId });
  browser.close();
  chrome.kill();
  process.exit(realErrors.length || badResponses.length ? 2 : 0);
}

// Walk every Analytics tab by clicking, and validate the /roi endpoint shape.
async function checkAnalyticsTabs({ ssend, errors, badResponses }) {
  await ssend('Page.navigate', { url: 'http://127.0.0.1:8000/app/analytics' });
  await sleep(5000);
  const tabLabels = ['Overview', 'Campaign ROI', 'Team performance', 'Top 10'];
  for (const label of tabLabels) {
    errors.length = 0; badResponses.length = 0;
    const clicked = await ssend('Runtime.evaluate', {
      expression: `(() => { const b = Array.from(document.querySelectorAll('.tab')).find(x => x.textContent.trim() === ${JSON.stringify(label)}); if (!b) return false; b.click(); return true; })()`,
      returnByValue: true,
    });
    await sleep(3500);
    const body = await ssend('Runtime.evaluate', {
      expression: `document.body ? document.body.innerText.length : -1`, returnByValue: true,
    });
    const ex = errors.filter((e) => !e.includes('favicon'));
    const bad = badResponses.filter((b) => !b.includes('favicon'));
    console.log(`\n=== analytics tab "${label}" clicked=${clicked.result?.value} bodyLen=${body.result?.value} errors=${ex.length} apiFailures=${bad.length} ===`);
    if (ex.length) console.log(ex.join('\n'));
    if (bad.length) console.log(bad.join('\n'));
    if (!clicked.result?.value) console.log('WARN: tab button not found');
    if (process.env.POB_DUMP_TAB === label) {
      const txt = await ssend('Runtime.evaluate', {
        expression: `document.body ? document.body.innerText.slice(0, 1600) : ''`, returnByValue: true,
      });
      console.log('--- rendered text ---');
      console.log(txt.result?.value || '');
    }
  }
  const roiShape = await ssend('Runtime.evaluate', {
    expression: `(async () => {
      const t = JSON.parse(localStorage.getItem('pob_saas_session'));
      const r = await fetch('/api/v1/analytics/roi?days=0', { headers: { Authorization: 'Bearer ' + t.access } });
      const j = await r.json().catch(() => ({}));
      return { status: r.status, keys: Object.keys(j),
        totalsKeys: j.totals ? Object.keys(j.totals) : [],
        campaigns: Array.isArray(j.campaigns) ? j.campaigns.length : null,
        members: Array.isArray(j.members) ? j.members.length : null,
        chemists: Array.isArray(j.chemists) ? j.chemists.length : null,
        brands: Array.isArray(j.brands) ? j.brands.length : null,
        products: Array.isArray(j.products) ? j.products.length : null,
        divisions: Array.isArray(j.divisions) ? j.divisions.length : null,
        sample: Array.isArray(j.campaigns) && j.campaigns.length ? {
          roi: j.campaigns[0].roi, basis: j.campaigns[0].roi_basis,
          rewards_paid: j.campaigns[0].rewards_paid, verified_value: j.campaigns[0].verified_value,
          net: j.campaigns[0].net } : null };
    })()`,
    awaitPromise: true, returnByValue: true,
  });
  console.log('\n--- /api/v1/analytics/roi response shape ---');
  console.log(JSON.stringify(roiShape.result?.value, null, 2));
  if (!roiShape.result?.value || roiShape.result.value.status !== 200) {
    console.log('WARN: roi endpoint check failed');
  }
}

main().catch((e) => { console.error('CDP FAILED:', e.message); chrome.kill(); process.exit(1); });
