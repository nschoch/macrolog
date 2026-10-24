/* Headless-Chrome smoke test for the settings "auto"/"locked" toggle.
 * Uses only Chrome's DevTools Protocol over plain WebSocket (no npm deps).
 * Usage: node tests/browser_check.mjs <baseURL> [sessionCookie]
 * Exits non-zero if any uncaught JS error / console.error appears, or if the
 * toggle does not flip state as expected.
 */
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BASE = process.argv[2] || 'http://127.0.0.1:8790/';
const COOKIE = process.argv[3] || '';
// Optional: route the real hostname to a local port-forward (e.g. an SSH
// tunnel) so headless Chrome can reach deployments that its network stack
// cannot touch directly. TLS is still validated against the REAL hostname.
const TUNNEL = process.env.ML_TUNNEL_MAP || '';   // e.g. "host:443=127.0.0.1:1443"
const HOST = (BASE.match(/^https?:\/\/([^/:]+)/) || [])[1];
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

const profile = mkdtempSync(join(tmpdir(), 'ml-chrome-'));
const port = 9557;
const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`, '--no-first-run', '--disable-gpu', '--no-sandbox',
  '--disable-crash-reporter', `--crash-pads-dir=${profile}/crashpad`,
  '--window-size=430,900',
  ...(TUNNEL ? [`--host-resolver-rules=MAP ${HOST} ${TUNNEL.split('=')[1]}`] : []),
  'about:blank',
], { stdio: 'ignore' });

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function waitForJson(url, tries = 60) {
  for (let i = 0; i < tries; i++) {
    try { const r = await fetch(url); if (r.ok) return await r.json(); } catch {}
    await sleep(250);
  }
  throw new Error(`CDP endpoint not ready: ${url}`);
}

function cdp(wsUrl) {
  return new Promise((resolve) => {
    const ws = new WebSocket(wsUrl);
    let id = 0; const pending = new Map(); const events = [];
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
      else events.push(m);
    };
    ws.onopen = () => resolve({
      send(method, params = {}) {
        return new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
      },
      events, ws,
    });
  });
}

let failures = 0;
const check = (name, ok, detail = '') => {
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures++;
};

try {
  const list = await waitForJson(`http://127.0.0.1:${port}/json/list`);
  const target = list.find(t => t.type === 'page');
  if (!target?.webSocketDebuggerUrl) throw new Error('no CDP page target: ' + JSON.stringify(target));
  const client = await cdp(target.webSocketDebuggerUrl);
  const { send } = client;

  // Helper: evaluate JS in the page and return the JSON value (throws on error).
  // send() resolves the raw CDP envelope: { id, result: { result: RemoteObject }, exceptionDetails? }.
  const evalJson = async (expr, tries = 3) => {
    for (let i = 0; i < tries; i++) {
      const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw new Error('page threw: ' + JSON.stringify(r.exceptionDetails).slice(0, 300));
      const res = r.result?.result || {};
      if (res.type === 'null') return null;
      if (res.type === 'undefined') return undefined;   // fire-and-forget expressions
      if ('value' in res && res.value !== undefined) return res.value;
      await sleep(500);
    }
    throw new Error('evaluate returned no value: ' + expr.slice(0, 80));
  };

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Log.enable');
  await send('Network.enable');
  if (COOKIE) {
    await send('Network.setCookie', { name: 'macrolog_session', value: COOKIE, url: BASE, path: '/' });
  }

  const jsErrors = [];
  client.events; // collected below via polling helper
  const drain = () => {
    for (const e of client.events.splice(0)) {
      if (e.method === 'Runtime.exceptionThrown')
        jsErrors.push(e.params.exceptionDetails?.exception?.description || JSON.stringify(e.params.exceptionDetails));
      if (e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error')
        jsErrors.push('console.error: ' + (e.params.args || []).map(a => a.value ?? a.description).join(' '));
      if (e.method === 'Log.entryAdded' && e.params.entry.level === 'error' && e.params.entry.source !== 'network')
        jsErrors.push('log: ' + e.params.entry.text);
    }
  };

  await send('Page.navigate', { url: BASE });
  // Wait for the app to boot (window state populated by first refresh()).
  let booted = false;
  for (let i = 0; i < 40 && !booted; i++) {
    await sleep(500);
    // typeof-guard: evaluating a bare `state` throws ReferenceError if app.js
    // never ran, which would mask the real diagnosis.
    booted = await evalJson(`typeof state !== 'undefined' && !!state`).catch((e) => { console.log('probe err:', String(e.message).slice(0,140)); return false; });
    if (i === 5) console.log('mid-boot location:', await evalJson(`location.href`).catch(() => '?'));
  }
  if (!booted) throw new Error('app never booted (state stayed null) — check auth/redirect');
  await sleep(1000); drain();
  check('initial load has no JS errors', jsErrors.length === 0, jsErrors.join(' | ').slice(0, 300));

  // Switch to Settings view.
  await evalJson(`document.querySelector('button[data-view="settings"]').click()`);
  await sleep(1200); drain();

  const btnState = () => evalJson(`(({label: document.getElementById('btn-unlock').textContent.trim(),
      targetVal: document.getElementById('s-target').value,
      viewActive: document.getElementById('view-settings').classList.contains('active')}))`);

  let s0 = await btnState();
  check('settings view active', s0.viewActive);
  check('button shows "auto" on fresh account (calorie_target null)', s0.label === 'auto', `label=${s0.label}`);

  // Click 1: auto -> lock (server pins 2200 fallback when target was null? see code: locks at s.calorie_target || 2200)
  const errsBefore = jsErrors.length;
  await evalJson(`document.getElementById('btn-unlock').click()`);
  await sleep(2500); drain();
  check('click #1 produced no JS error', jsErrors.length === errsBefore, jsErrors.slice(errsBefore).join(' | ').slice(0, 300));
  let s1 = await btnState();
  check('after click #1 button shows "locked"', s1.label === 'locked', `label=${s1.label} targetVal=${s1.targetVal}`);

  // Verify server-side (in-page fetch so the session cookie rides along).
  const serverTarget = () => evalJson(`fetch('/api/state').then(r=>r.json()).then(d=>({v: d.settings.calorie_target ?? null}))`).then(o => o.v);
  const st = await serverTarget().catch(() => 'ERR');
  check('server calorie_target pinned after lock', st != null && st !== 'ERR', `calorie_target=${st}`);

  // Click 2: locked -> auto.
  const errsBefore2 = jsErrors.length;
  await evalJson(`document.getElementById('btn-unlock').click()`);
  await sleep(2500); drain();
  check('click #2 produced no JS error', jsErrors.length === errsBefore2, jsErrors.slice(errsBefore2).join(' | ').slice(0, 300));
  let s2 = await btnState();
  check('after click #2 button shows "auto"', s2.label === 'auto', `label=${s2.label}`);

  const st2 = await serverTarget().catch(() => 'ERR');
  check('server calorie_target cleared after unlock', st2 == null, `calorie_target=${st2}`);

  // Screenshot for the record.
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  const out = join(process.env.ML_SHOT_DIR || '.', 'settings-auto-button.png');
  writeFileSync(out, Buffer.from(shot.result.data, 'base64'));
  console.log(`  screenshot -> ${out}`);

  console.log(jsErrors.length ? `\n${jsErrors.length} JS error(s) total:\n` + jsErrors.join('\n') : '\nNo JS errors across full sequence.');
} finally {
  chrome.kill();
  await sleep(300);
  try { rmSync(profile, { recursive: true, force: true }); } catch {}
}
process.exit(failures ? 1 : 0);
