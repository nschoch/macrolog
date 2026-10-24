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
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

const profile = mkdtempSync(join(tmpdir(), 'ml-chrome-'));
const port = 9557;
const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`, '--no-first-run', '--disable-gpu', '--no-sandbox',
  '--disable-crash-reporter', `--crash-pads-dir=${profile}/crashpad`,
  '--window-size=430,900', 'about:blank',
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
  const client = await cdp(target.webSocketDebuggerUrl);
  const { send } = client;

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Log.enable');
  await send('Network.enable');
  if (COOKIE) {
    const m = BASE.match(/^https?:\/\/([^/:]+)/);
    await send('Network.setCookie', { name: 'macrolog_session', value: COOKIE, domain: m[1], path: '/' });
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
  await sleep(3500); drain();
  check('initial load has no JS errors', jsErrors.length === 0, jsErrors.join(' | ').slice(0, 300));

  // Switch to Settings view.
  await send('Runtime.evaluate', { expression: `
    document.querySelector('button[data-view="settings"]').click(); 'ok'` });
  await sleep(1200); drain();

  const btnState = async () => (await send('Runtime.evaluate', {
    expression: `JSON.stringify({label: document.getElementById('btn-unlock').textContent.trim(),
      targetVal: document.getElementById('s-target').value,
      viewActive: document.getElementById('view-settings').classList.contains('active')})`,
    returnByValue: true })).result.result.value;

  let s0 = JSON.parse(await btnState());
  check('settings view active', s0.viewActive);
  check('button shows "auto" on fresh account (calorie_target null)', s0.label === 'auto', `label=${s0.label}`);

  // Click 1: auto -> lock (server pins 2200 fallback when target was null? see code: locks at s.calorie_target || 2200)
  const errsBefore = jsErrors.length;
  await send('Runtime.evaluate', { expression: `document.getElementById('btn-unlock').click(); 'clicked'` });
  await sleep(2500); drain();
  check('click #1 produced no JS error', jsErrors.length === errsBefore, jsErrors.slice(errsBefore).join(' | ').slice(0, 300));
  let s1 = JSON.parse(await btnState());
  check('after click #1 button shows "locked"', s1.label === 'locked', `label=${s1.label} targetVal=${s1.targetVal}`);

  // Verify server-side.
  const st = await (await fetch(BASE.replace(/\/$/, '') + '/api/state')).json().catch(() => null);
  check('server calorie_target pinned after lock', st && st.settings.calorie_target != null,
    st ? `calorie_target=${st.settings.calorie_target}` : 'state fetch failed (auth?)');

  // Click 2: locked -> auto.
  const errsBefore2 = jsErrors.length;
  await send('Runtime.evaluate', { expression: `document.getElementById('btn-unlock').click(); 'clicked'` });
  await sleep(2500); drain();
  check('click #2 produced no JS error', jsErrors.length === errsBefore2, jsErrors.slice(errsBefore2).join(' | ').slice(0, 300));
  let s2 = JSON.parse(await btnState());
  check('after click #2 button shows "auto"', s2.label === 'auto', `label=${s2.label}`);

  const st2 = await (await fetch(BASE.replace(/\/$/, '') + '/api/state')).json().catch(() => null);
  check('server calorie_target cleared after unlock', st2 && st2.settings.calorie_target == null,
    st2 ? `calorie_target=${st2.settings.calorie_target}` : '');

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
