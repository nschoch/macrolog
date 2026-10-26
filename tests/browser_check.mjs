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
  // Fresh browser context: start from a clean SW slate so the app registers
  // exactly once (see app.js note about Chrome's wedged-SW failure mode).
  await evalJson(`navigator.serviceWorker.getRegistrations ? navigator.serviceWorker.getRegistrations().then(rs => Promise.all(rs.map(r => r.unregister())).then(() => 'sw cleared') : 'n/a')`).catch(() => {});

  // Disable service-worker registration for this run. Root cause of the
  // endless hangs we chased: headless Chrome ends up with an ACTIVE but
  // non-responsive SW for this origin (proven — postMessage gets no reply and
  // handler-triggered POSTs never reach the server, while identical fetches
  // from the console succeed). The SW only provides offline caching; it is not
  // what these checks exercise, and a wedged worker makes every POST hang.
  // Stubbing registration before any document script runs removes the whole
  // failure class and makes runs deterministic.
  await send('Page.addScriptToEvaluateOnNewDocument', {
    source: `try {
      Object.defineProperty(navigator, 'serviceWorker', {
        configurable: true,
        value: { register: () => Promise.reject(new Error('sw disabled for test')),
                 getRegistration: () => Promise.resolve(undefined),
                 getRegistrations: () => Promise.resolve([]),
                 addEventListener: () => {}, removeEventListener: () => {},
                 controller: null, ready: new Promise(() => {}) },
      });
    } catch (_) {}`,
  });

  // Stall detector: wrap window.fetch to track in-flight requests. If one has
  // been outstanding >5s, the watchdog recovers the page (belt and braces —
  // with the SW stubbed out this should never trigger).
  const installWatchdog = () => evalJson(`(() => {
    if (window.__wd) return 'already';
    window.__wd = { inflight: new Map(), released: 0 };
    const of = window.fetch.bind(window);
    window.fetch = function(u, o) {
      const key = String(u) + '#x';
      window.__wd.inflight.set(key, Date.now());
      return of(u, o).finally(() => window.__wd.inflight.delete(key));
    };
    return 'installed';
  })()`);
  const waitForBoot = async (ms = 20000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      await sleep(400);
      const ok = await evalJson(`typeof state !== 'undefined' && !!state`).catch(() => false);
      if (ok) return true;
    }
    return false;
  };
  const watchdog = async () => {
    let stuckUrls = [];
    try {
      stuckUrls = await evalJson(`[...window.__wd.inflight.entries()].filter(([,t])=>Date.now()-t>5000).map(([k])=>k.split('#')[0])`);
    } catch { return; }
    if (!stuckUrls.length) return;
    console.log('[harness] watchdog: stalled ' + stuckUrls.join(',') + ' — aborting & reloading');
    // Headless-Chrome wedge: an aborted-then-retried POST to the same URL can
    // hang forever in the renderer. Unregistering the SW releases (aborts) the
    // in-flight fetches so handlers unwind; a reload resets per-page UI state.
    await evalJson(`(async()=>{const rs=await navigator.serviceWorker.getRegistrations();await Promise.all(rs.map(r=>r.unregister()))})()`).catch(() => {});
    await sleep(800);
    await send('Page.navigate', { url: BASE });
    if (!await waitForBoot()) throw new Error('page failed to re-boot after watchdog reload');
    await installWatchdog();
    await sleep(300); drain();
  };
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
  await sleep(1000);
  await installWatchdog();
  await sleep(200); drain();   // drop any events from setup before baseline
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
  await sleep(2500); await watchdog(); drain();
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
  await sleep(2500); await watchdog(); drain();
  check('click #2 produced no JS error', jsErrors.length === errsBefore2, jsErrors.slice(errsBefore2).join(' | ').slice(0, 300));
  let s2 = await btnState();
  check('after click #2 button shows "auto"', s2.label === 'auto', `label=${s2.label}`);

  const st2 = await serverTarget().catch(() => 'ERR');
  check('server calorie_target cleared after unlock', st2 == null, `calorie_target=${st2}`);

  // ---- Day browsing: prev-day navigation + past-day meal editor ----------
  // Seed a meal on yesterday via the API (in-page fetch carries the cookie).
  const ySeed = await evalJson(`(async () => {
    const d = new Date(); d.setDate(d.getDate() - 1);
    const iso = d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
    const r = await fetch('/api/meals', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ description: 'browser-check yesterday', calories: 123, protein: 9, carbs: 12, fat: 4, date: iso }) });
    return { status: r.status, iso };
  })()`);
  check('seeded meal on yesterday', ySeed.status >= 200 && ySeed.status < 300, JSON.stringify(ySeed));

  await evalJson(`document.querySelector('button[data-view="today"]').click()`);
  await sleep(800);
  const titleToday = await evalJson(`document.getElementById('day-title').textContent`);
  check('today header says "Today"', titleToday === 'Today', `title=${titleToday}`);

  const errsB = jsErrors.length;
  await evalJson(`document.getElementById('btn-day-prev').click()`);
  await sleep(2500); await watchdog(); drain();
  check('prev-day click produced no JS error', jsErrors.length === errsB, jsErrors.slice(errsB).join(' | ').slice(0, 300));
  const yView = await evalJson(`({date: state.date, hasMeal: state.meals.some(m => m.description === 'browser-check yesterday'),
    nextDisabled: document.getElementById('btn-day-next').disabled})`);
  check('viewing yesterday shows seeded meal', yView.hasMeal, JSON.stringify(yView));
  check('next-arrow enabled on yesterday', yView.nextDisabled === false);

  // Tap the meal row -> should open the review sheet in edit mode.
  const errsC = jsErrors.length;
  await evalJson(`(() => { const row = [...document.querySelectorAll('.meal-row')].find(r => r.textContent.includes('browser-check yesterday')); row.click(); })()`);
  await sleep(1500); await watchdog(); drain();
  check('past-day tap produced no JS error', jsErrors.length === errsC, jsErrors.slice(errsC).join(' | ').slice(0, 300));
  const sheet = await evalJson(`({open: !document.getElementById('review-sheet').hidden,
    desc: document.getElementById('r-desc').value, kcal: document.getElementById('r-cal').value})`);
  check('review sheet opened with the meal', sheet.open && sheet.desc === 'browser-check yesterday' && sheet.kcal === '123', JSON.stringify(sheet));
  await evalJson(`document.getElementById('btn-cancel-review').click()`);

  // Back to today via next arrow.
  await evalJson(`document.getElementById('btn-day-next').click()`);
  await sleep(2500); await watchdog();
  const backToday = await evalJson(`({title: document.getElementById('day-title').textContent, gone: !state.meals.some(m => m.description === 'browser-check yesterday')})`);
  check('next-arrow returns to Today without the old meal', backToday.title === 'Today' && backToday.gone, JSON.stringify(backToday));

  // Cleanup: delete the seeded meal so repeated runs stay idempotent.
  await evalJson(`(async () => {
    const h = await fetch('/api/history?days=7').then(r=>r.json());
    const y = h.intake.find(d => d.meals > 0 && d.date !== new Date().toISOString().slice(0,10));
    if (!y) return null;
    const st = await fetch('/api/state?date=' + y.date).then(r=>r.json());
    const m = st.meals.find(mm => mm.description === 'browser-check yesterday');
    if (m) await fetch('/api/meals/' + m.id, { method: 'DELETE' });
    return m ? m.id : null;
  })()`);

  // ---- Describe-a-meal: text -> AI estimate -> editable review sheet ------
  await evalJson(`(() => { document.querySelector('button[data-view="today"]').click();
    if (typeof closeSheets === 'function') closeSheets(); })()`);
  await sleep(800); drain();
  const errsD = jsErrors.length;
  await evalJson(`document.getElementById('btn-describe').click()`);
  await sleep(400); drain();
  check('describe sheet opens', await evalJson(`!document.getElementById('describe-sheet').hidden`));
  await evalJson(`(() => { const t = document.getElementById('d-text');
    t.value = '100g of rice and 100g of cooked 85% lean ground beef'; })()`);
  await evalJson(`document.getElementById('btn-estimate-text').click()`);
  await sleep(3500); await watchdog(); drain();
  check('estimate produced no JS error', jsErrors.length === errsD, jsErrors.slice(errsD).join(' | ').slice(0, 300));
  const readRev = () => evalJson(`({open: !document.getElementById('review-sheet').hidden,
    desc: document.getElementById('r-desc').value,
    nItems: typeof compItems !== 'undefined' ? compItems.length : -1,
    src: document.getElementById('review-sheet').dataset.source})`);
  let rev = await readRev();
  for (let attempt = 1; !rev.open && attempt <= 3; attempt++) {
    // After a watchdog reload the sheets are closed and the textarea empty:
    // redo the whole describe interaction.
    console.log(`[harness] review not open (attempt ${attempt}) — repeating describe interaction`);
    await evalJson(`(() => { document.querySelector('button[data-view="today"]').click();
      document.getElementById('btn-describe').click(); })()`);
    await sleep(400);
    await evalJson(`document.getElementById('d-text').value = '100g of rice and 100g of cooked 85% lean ground beef'`);
    await evalJson(`document.getElementById('btn-estimate-text').click()`);
    await sleep(3500); await watchdog(); drain();
    rev = await readRev();
  }
  check('review sheet shows text estimate with components', rev.open && rev.nItems >= 2 && rev.src === 'text', JSON.stringify(rev));

  // Save it and confirm it appears in today's list tagged text.
  const errsE = jsErrors.length;
  await evalJson(`document.getElementById('btn-save-review').click()`);
  await sleep(2500); await watchdog(); drain();
  check('saving text meal produced no JS error', jsErrors.length === errsE, jsErrors.slice(errsE).join(' | ').slice(0, 300));
  const saved = await evalJson(`(async () => {
    const r = await fetch('/api/state').then(x=>x.json());
    return r.meals.filter(m=>m.source==='text').map(m=>({id:m.id,n:(m.items||[]).length}))})()`);
  check('text meal visible on Today with components', Array.isArray(saved) && saved.length >= 1 && saved[0].n >= 2, JSON.stringify(saved));
  if (Array.isArray(saved)) for (const m of saved) await evalJson(`fetch('/api/meals/'+${m.id},{method:'DELETE'})`);
  // ---- Day-nav: arrows must not raise a native date picker (Safari bug) ----
  const pickerRaised = await evalJson(`(() => {
    let raised = false;
    const pick = document.getElementById('day-pick');
    const origFocus = pick.focus.bind(pick);
    pick.focus = () => { raised = true; };
    document.getElementById('btn-day-prev').click();
    pick.focus = origFocus;
    return raised; })()`);
  check('prev-arrow does not trigger date picker', pickerRaised === false);

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
