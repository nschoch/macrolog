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
// IMPORTANT: give this suite its OWN server + MACROLOG_DATA directory. It
// writes meals and weights, which perturb api_test's consumed/trend assertions
// when both share one database (api_test then fails for reasons unrelated to
// the code under test). Suggested invocation:
//   MACROLOG_DATA=$(mktemp -d) python3 server.py --port 8790 --no-browser &
//   node tests/browser_check.mjs http://127.0.0.1:8790/
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
  // Reset the pinned target first: the toggle assertions below need the
  // "auto" starting state, and an earlier run may have left a locked value.
  await evalJson(`fetch('/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ calorie_target: null }) }).then(r => r.json()).then(d => d.settings.calorie_target ?? null)`);
  await evalJson(`refresh()`); await sleep(900);
  s0 = await btnState();
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

  // Pin the offline stub provider so estimate calls don't hit the live model
  // (60-200s when healthy, flaky otherwise). LOCAL TARGETS ONLY: `providers`
  // lives in the SHARED settings blob, so pinning against production silently
  // replaced the household's real vision chain with the stub -- twice. On a
  // remote target we accept slow estimates rather than corrupting config.
  const isLocalTarget = /^https?:\/\/(127\.|localhost|\[::1\])/.test(BASE);
  if (isLocalTarget) {
    const pinned = await evalJson(`fetch('/api/settings', { method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ providers: [{ kind: 'stub' }] }) }).then(r => r.status)`);
    if (pinned !== 200) console.log('[harness] warning: could not pin stub provider (' + pinned + ')');
  } else {
    console.log('[harness] remote target: not touching shared provider config');
  }

  // ---- Unified Add-meal sheet: photos + text composed into one estimate ----
  await evalJson(`(() => { document.querySelector('button[data-view="today"]').click();
    if (typeof closeSheets === 'function') closeSheets(); })()`);
  await sleep(800); drain();

  check('single + Add button replaces the old three', await evalJson(
    `!!document.getElementById('btn-add-meal') && !document.getElementById('btn-describe')
      && !document.getElementById('btn-manual') && !!document.getElementById('btn-add-meal-fab')`));

  const errsD = jsErrors.length;
  await evalJson(`document.getElementById('btn-add-meal').click()`);
  await sleep(400); drain();
  check('add sheet opens from + button', await evalJson(`!document.getElementById('add-sheet').hidden`));

  // Stage two images client-side (plate + label) without any file dialog:
  // synthesize File objects and drive the same staging path the UI uses.
  await evalJson(`(() => {
    const mk = (name) => new File([new Uint8Array([255,216,255,224].concat(Array(600).fill(7)))], name, {type:'image/jpeg'});
    stagedPhotos.length = 0;
    stageFile(mk('plate.jpg')); stageFile(mk('label.jpg'));
  })()`);
  await sleep(900);
  check('two photos staged in tray', await evalJson(`stagedPhotos.length === 2
    && document.querySelectorAll('#photo-tray .thumb-wrap').length === 2`));

  await evalJson(`document.getElementById('a-text').value = 'about half the box of pasta'`);
  await evalJson(`document.getElementById('btn-estimate-meal').click()`);
  // Remote targets hit the real model (can take minutes); poll for the spinner
  // to clear rather than sleeping a fixed amount that races later sections.
  for (let i = 0; i < 60; i++) {
    await sleep(1000);
    if (await evalJson(`document.getElementById('loading').hidden`)) break;
  }
  await watchdog(); drain();
  check('multi-input estimate produced no JS error', jsErrors.length === errsD,
        jsErrors.slice(errsD).join(' | ').slice(0, 300));

  const rev = await evalJson(`({open: !document.getElementById('review-sheet').hidden,
    desc: document.getElementById('r-desc').value,
    nItems: typeof compItems !== 'undefined' ? compItems.length : -1,
    staged: typeof stagedForSave !== 'undefined' ? stagedForSave.length : -1})`);
  check('review sheet opened with estimate', rev.open && rev.nItems >= 1, JSON.stringify(rev));
  check('both temp photos carried forward for save', rev.staged === 2, `staged=${rev.staged}`);

  // Save and confirm BOTH photos attach to the meal.
  const errsE = jsErrors.length;
  await evalJson(`document.getElementById('btn-save-review').click()`);
  await sleep(2800); await watchdog(); drain();
  check('saving multi-photo meal produced no JS error', jsErrors.length === errsE,
        jsErrors.slice(errsE).join(' | ').slice(0, 300));
  const twoShot = await evalJson(`(async () => {
    const r = await fetch('/api/state').then(x=>x.json());
    const m = r.meals.find(x => (x.photos||[]).length >= 2);
    return m ? {id:m.id, n:m.photos.length} : null })()`);
  check('meal stored with both photos', !!twoShot && twoShot.n === 2, JSON.stringify(twoShot));

  // ---- Auto grouping: two nearby logs should form one sitting -------------
  await evalJson(`(async () => {
    const post = (desc, mins) => { const d = new Date(Date.now() - mins*60000);
      const hh = String(d.getHours()).padStart(2,'0'), mm = String(d.getMinutes()).padStart(2,'0');
      return fetch('/api/meals', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({description: desc, calories: 150, protein: 8, carbs: 12, fat: 5,
          date: todayISO(), time: hh+':'+mm})}); };
    await post('grp-one', 4); await post('grp-two', 2);
  })()`);
  await sleep(1200);
  await evalJson(`refresh()`); await sleep(1800); await watchdog();
  const grouped = await evalJson(`(() => ({
    heads: [...document.querySelectorAll('.group-head h3')].map(h=>h.textContent),
    joined: (state.groups||[]).some(g => {
      const names = g.meal_ids.map(id => (state.meals.find(m=>m.id===id)||{}).description);
      return names.includes('grp-one') && names.includes('grp-two'); })}))()`);
  check('nearby meals render inside one sitting group', grouped.joined === true,
        JSON.stringify(grouped.heads));
  check('group headers show derived labels', grouped.heads.length >= 1, JSON.stringify(grouped.heads));

  // cleanup everything this section created
  await evalJson(`(async () => {
    const r = await fetch('/api/state').then(x=>x.json());
    for (const m of r.meals) {
      if ((m.photos||[]).length >= 2 || ['grp-one','grp-two'].includes(m.description))
        await fetch('/api/meals/'+m.id, {method:'DELETE'});
    } })()`);
  await sleep(1200);
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

  // ---- Weight dock: prefill from last weigh-in + stale (>7d) nudge --------
  await evalJson(`(() => { document.querySelector('button[data-view="today"]').click();
    if (typeof closeSheets === 'function') closeSheets(); })()`);
  await sleep(600); drain();

  // The suite logs weights earlier (trend/grouping checks), and a same-day row
  // legitimately suppresses both the prefill and the nudge -- so drive the
  // precondition explicitly instead of assuming a pristine database: point
  // state.last_weight at a stale date and re-render the dock.
  await evalJson(`(() => {
    const d = new Date(Date.now() - 12 * 864e5);
    const iso = d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
    state.last_weight = { date: iso, kg: 82.4, days_ago: 12 };
    $('w-value').value = ''; dockPrefill = false;
    syncWeightDock();
  })()`);
  await sleep(300);

  const dockStale = await evalJson(`(() => ({
    val: document.getElementById('w-value').value,
    warn: document.getElementById('btn-weight').classList.contains('warn'),
    stale: document.getElementById('weight-dock').classList.contains('stale'),
    prefilled: document.getElementById('w-value').classList.contains('prefilled'),
    title: document.getElementById('btn-weight').title }))()`);
  check('dock prefills last weight', dockStale.val !== '' && Number(dockStale.val) > 0,
        JSON.stringify(dockStale));
  check('stale >7d turns Log button amber', dockStale.warn === true && dockStale.stale === true,
        JSON.stringify(dockStale));
  check('prefill marked as suggestion not typed input', dockStale.prefilled === true);
  // Day count is derived from calendar dates, so assert it reports a plausible
  // stale age rather than an exact number (DST can make 12x864e5 read as 11).
  check('stale button explains itself via title',
        /Last weigh-in \d+ days ago/.test(dockStale.title || '') &&
          Number((dockStale.title.match(/\d+/) || [0])[0]) >= 7,
        dockStale.title);

  // Typing should clear the "suggestion" styling immediately.
  await evalJson(`(() => { const el=document.getElementById('w-value');
    el.value='80.1'; el.dispatchEvent(new Event('input',{bubbles:true})); })()`);
  await sleep(300);
  check('editing clears prefilled styling',
        await evalJson(`!document.getElementById('w-value').classList.contains('prefilled')`));

  // Now log for real: the POST plus refresh() must clear the nag and prefill.
  const errsW = jsErrors.length;
  await evalJson(`document.getElementById('btn-weight').click()`);
  await sleep(2500); await watchdog(); drain();
  check('weight log produced no JS error', jsErrors.length === errsW,
        jsErrors.slice(errsW).join(' | ').slice(0,300));
  const dockFresh = await evalJson(`(() => ({
    warn: document.getElementById('btn-weight').classList.contains('warn'),
    val: document.getElementById('w-value').value,
    ph: document.getElementById('w-value').placeholder,
    days: (state.last_weight||{}).days_ago }))()`);
  check('same-day log clears stale warning', dockFresh.warn === false, JSON.stringify(dockFresh));
  check('dock confirms today is done', /logged today/.test(dockFresh.ph), dockFresh.ph);
  check('no prefill once today is logged', dockFresh.val === '', `val=${dockFresh.val}`);

  // ---- Caret/DOM stability while editing estimated component values.
  // Regression: typing grams re-rendered the whole editor (innerHTML), which
  // destroys the focused input -- so the caret reset and iOS dismissed the
  // keyboard on every keystroke. ----
  const caretRaw = await evalJson(`(() => {
    try {
      openReview({ estimate: { description: 'x', calories: 500, protein: 30, carbs: 50,
                               fat: 15, items: [{ name: 'rice', grams: 100, calories: 130,
                               protein: 2.7, carbs: 28, fat: 0.3 }] } });
      const sel = '.item-row[data-i="0"] input[data-f="grams"]';
      const before = document.querySelector(sel);
      if (!before) return JSON.stringify({ fatal: 'no grams input' });
      before.focus();
      const focusedBefore = document.activeElement === before;
      before.value = '1005';
      before.dispatchEvent(new Event('input', { bubbles: true }));
      const after = document.querySelector(sel);
      return JSON.stringify({
        focusedBefore,
        sameNode: before === after,                 // element not replaced
        stillFocused: document.activeElement === before,
        valueKeepsTyping: after.value === '1005',   // not clobbered by re-render
        grams: compItems[0].grams,
        kcal: document.querySelector('.item-row[data-i="0"] input[data-f="calories"]').value,
        protein: document.querySelector('.item-row[data-i="0"] input[data-f="protein"]').value,
        rowsAfter: document.querySelectorAll('.item-row').length,
      });
    } catch (e) {
      return JSON.stringify({ fatal: String(e && e.message || e) });
    }
  })()`);
  const caretObj = JSON.parse(caretRaw);
  check('editing grams keeps the same input element',
        !caretObj.fatal && caretObj.sameNode && caretObj.stillFocused, caretRaw);
  check('typed text is not clobbered mid-edit', caretObj.valueKeepsTyping === true, caretRaw);
  check('derived macros update in place without re-render',
        caretObj.grams === 1005 && Number(caretObj.kcal) > 130
        && Number(caretObj.protein) > 2.7 && caretObj.rowsAfter === 1, caretRaw);
  await evalJson(`closeSheets()`);

  // ---- Each new entry starts blank (regression: the note from the previous
  // meal persisted into the next sheet, so it got estimated/saved again). ----
  const reset = await evalJson(`(() => {
    openAddSheet();
    document.getElementById('a-text').value = 'leftover note from last meal';
    stageFile(new File([new Uint8Array(400)], 'x.jpg', { type: 'image/jpeg' }));
    closeSheets();                 // user cancels
    openAddSheet();                // next entry
    return { text: document.getElementById('a-text').value,
             staged: stagedPhotos.length, forSave: stagedForSave.length };
  })()`);
  check('note does not persist to the next entry', reset.text === '', JSON.stringify(reset));
  check('staged photos do not persist either', reset.staged === 0 && reset.forSave === 0,
        JSON.stringify(reset));

  // ---- 24-hour clock option --------------------------------------------
  const clockFmt = await evalJson(`(async () => {
    // Earlier sections navigate days; make sure we are looking at today or the
    // seeded meal would not be rendered at all.
    viewDate = '';
    document.querySelector('button[data-view="today"]').click();
    await refresh();
    // Seed a meal so there is a time to render.
    const today = todayISO();
    await fetch('/api/meals', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ description: 'clock probe', calories: 100, protein: 5,
        carbs: 10, fat: 4, date: today, time: '13:30', source: 'manual' }) });
    await refresh();
    const sample = () => {
      const row = [...document.querySelectorAll('.meal-meta')].find(e => /clock probe/.test(
        (e.closest('.meal') || {}).textContent || ''));
      return { head: (document.querySelector('.group-meta') || {}).textContent || '',
               meta: row ? row.textContent : '' };
    };
    const as12 = sample();
    // Switch through the Settings UI, as a user would.
    document.querySelector('button[data-view="settings"]').click();
    document.getElementById('s-timefmt').value = '24h';
    document.getElementById('btn-save').click();
    await new Promise(r => setTimeout(r, 1800));
    document.querySelector('button[data-view="today"]').click();
    await refresh();
    const as24 = sample();
    const stored = (await fetch('/api/state').then(r => r.json())).settings.time_format;
    // restore
    document.querySelector('button[data-view="settings"]').click();
    document.getElementById('s-timefmt').value = '12h';
    document.getElementById('btn-save').click();
    await new Promise(r => setTimeout(r, 1500));
    document.querySelector('button[data-view="today"]').click();
    await refresh();
    const back = sample();
    const meals = await fetch('/api/state').then(r => r.json());
    for (const m of meals.meals.filter(x => x.description === 'clock probe'))
      await fetch('/api/meals/' + m.id, { method: 'DELETE' });
    return JSON.stringify({ as12, as24, back, stored });
  })()`);
  const cf = JSON.parse(clockFmt);
  check('12-hour is the default and shows AM/PM',
        /\d{1,2}:\d{2}\s?(AM|PM)/i.test(cf.as12.head + ' ' + cf.as12.meta), JSON.stringify(cf.as12));
  check('24-hour option renders HH:MM without AM/PM',
        /\b\d{2}:\d{2}\b/.test(cf.as24.head) && !/(AM|PM)/i.test(cf.as24.head), JSON.stringify(cf.as24));
  check('time format persists on the server', cf.stored === '24h' || cf.stored === '12h', String(cf.stored));
  check('switching back restores 12-hour display',
        /(AM|PM)/i.test(cf.back.head + ' ' + cf.back.meta), JSON.stringify(cf.back));

  // ---- The browser must tell the server its own timezone ----------------
  const tzHeader = await evalJson(`(async () => {
    const seen = [];
    const orig = window.fetch;
    window.fetch = function (u, o) {
      try { seen.push(new Headers((o && o.headers) || {}).get('X-Client-TZ')); } catch (e) {}
      return orig.apply(this, arguments);
    };
    try { await api('state'); } finally { window.fetch = orig; }
    return JSON.stringify({ browser: clientTz(), sent: seen[0] || null,
                            allSent: seen.every(v => !!v) });
  })()`);
  const tzh = JSON.parse(tzHeader);
  check('client reports an IANA timezone', /^[A-Za-z]+\/[A-Za-z_]+/.test(tzh.browser || ''), tzHeader);
  check('every API call carries X-Client-TZ', tzh.allSent === true && tzh.sent === tzh.browser, tzHeader);

  // ---- Consumed / Remaining toggle + fiber shown inside carbs ------------
  const modes = await evalJson(`(() => {
    document.querySelector('#macro-mode button[data-mode="remaining"]').click();
    const read = () => ({
      mode: [...document.querySelectorAll('#macro-mode button')].find(b => b.classList.contains('on')).dataset.mode,
      ring: document.getElementById('kcal-left').textContent,
      label: document.getElementById('ring-label').textContent,
      macros: [...document.querySelectorAll('.macro-val')].map(e => e.textContent),
      fiber: (document.querySelector('.macro-sub') || {}).textContent || null,
    });
    const remaining = read();
    document.querySelector('#macro-mode button[data-mode="consumed"]').click();
    const consumed = read();
    document.querySelector('#macro-mode button[data-mode="remaining"]').click();
    return JSON.stringify({ remaining, consumed, stored: localStorage.getItem('ml_macro_mode') });
  })()`);
  const md = JSON.parse(modes);
  check('toggle switches to consumed values',
        md.consumed.mode === 'consumed' && /eaten/i.test(md.consumed.label), modes);
  check('toggle switches back to remaining values',
        md.remaining.mode === 'remaining' && /left|over/i.test(md.remaining.label), modes);
  check('the two modes show different numbers for the same day',
        md.consumed.macros[0] !== md.remaining.macros[0], modes);
  check('mode choice is remembered', md.stored === 'remaining', String(md.stored));
  check('fiber is shown inside the carbs card',
        !!md.remaining.fiber && /fiber/i.test(md.remaining.fiber), String(md.remaining.fiber));

  // ---- Field labels spelled out, units inside the input, fiber correctable --
  const fib = await evalJson(`(() => {
    openReview({ estimate: { description: 'psyllium', calories: 10, protein: 0.1,
                             carbs: 5.5, fat: 0.1, fiber: 0,
                             items: [{ name: 'Psyllium husk', grams: 6, calories: 10,
                             protein: 0.1, carbs: 5.5, fat: 0.1, fiber: 0 }] } });
    const row = document.querySelector('.item-row');
    const out = {
      // No regex on purpose: inside a Node template literal a "\s" escape
      // collapses to "s", which turned "Grams" into "Gram" and made this
      // assertion look like a product bug.
      labels: [...row.querySelectorAll('.ir-lab')].map(e => e.textContent.trim()),
      units: [...row.querySelectorAll('.num i')].map(e => e.textContent),
      fiberEditable: !row.querySelector('[data-f="fiber"]').readOnly,
      macrosLocked: row.querySelector('[data-f="calories"]').readOnly,
      initialFiber: row.querySelector('[data-f="fiber"]').value,
    };
    // Fiber can be corrected in auto mode (the estimator often omits it, and
    // meals logged before fiber existed store 0) and then follows the portion.
    const f = row.querySelector('[data-f="fiber"]');
    f.value = '4.5'; f.dispatchEvent(new Event('input', { bubbles: true }));
    out.afterCorrect = { fiber: compItems[0].fiber, totals: document.getElementById('r-total-shown').textContent };
    const g = row.querySelector('[data-f="grams"]');
    g.value = '12'; g.dispatchEvent(new Event('input', { bubbles: true }));
    out.afterDouble = { grams: compItems[0].grams, fiber: compItems[0].fiber,
                        shown: row.querySelector('[data-f="fiber"]').value };
    // subset rule still enforced when correcting
    const f2 = row.querySelector('[data-f="fiber"]');
    f2.value = '99'; f2.dispatchEvent(new Event('input', { bubbles: true }));
    out.clamped = { fiber: compItems[0].fiber, carbs: compItems[0].carbs };
    return JSON.stringify(out);
  })()`);
  const fb = JSON.parse(fib);
  check('component fields are spelled out, not abbreviated',
        JSON.stringify(fb.labels) === JSON.stringify(['Grams', 'Calories', 'Protein', 'Carbs', 'Fat', 'Fiber of carbs']),
        JSON.stringify(fb.labels));
  check('units are shown inside each input',
        JSON.stringify(fb.units) === JSON.stringify(['g', 'kcal', 'g', 'g', 'g', 'g']),
        JSON.stringify(fb.units));
  check('fiber is editable while the calorie-bearing macros stay derived',
        fb.fiberEditable === true && fb.macrosLocked === true, fib);
  check('a missing fiber value can be corrected in auto mode',
        fb.initialFiber === '0.0' && fb.afterCorrect.fiber === 4.5
        && /fiber\s*5/.test(fb.afterCorrect.totals), fib);
  check('corrected fiber then scales with the portion',
        fb.afterDouble.grams === 12 && fb.afterDouble.fiber === 9 && fb.afterDouble.shown === '9', fib);
  check('fiber is still clamped to carbs', fb.clamped.fiber === fb.clamped.carbs, fib);
  await evalJson(`closeSheets()`);

  // ---- Portion scaling: editing grams rescales macros and persists -------
  const scale = await evalJson(`(() => {
    openReview({ estimate: { description: 'scale probe', calories: 380, protein: 28.7,
                             carbs: 28, fat: 15.3, items: [
      { name: 'rice', grams: 100, calories: 130, protein: 2.7, carbs: 28, fat: 0.3 },
      { name: 'beef', grams: 100, calories: 250, protein: 26, carbs: 0, fat: 15 }] } });
    const g = document.querySelector('.item-row[data-i="0"] input[data-f="grams"]');
    g.value = '200'; g.dispatchEvent(new Event('input', { bubbles: true }));
    const doubled = [compItems[0].grams, compItems[0].calories, compItems[0].protein,
                     compItems[0].carbs, compItems[0].fat];
    const otherUntouched = [compItems[1].grams, compItems[1].calories];
    const shownKcal = document.querySelector('.item-row[data-i="0"] input[data-f="calories"]').value;
    const totals = document.getElementById('r-total-shown').textContent.trim();
    g.value = '100'; g.dispatchEvent(new Event('input', { bubbles: true }));
    return JSON.stringify({ doubled, otherUntouched, shownKcal, totals,
                            reverted: [compItems[0].grams, compItems[0].calories] });
  })()`);
  const sc = JSON.parse(scale);
  check('editing grams rescales kcal/protein/carbs/fat',
        JSON.stringify(sc.doubled) === JSON.stringify([200, 260, 5.4, 56, 0.6]), scale);
  check('other items are left alone', JSON.stringify(sc.otherUntouched) === JSON.stringify([100, 250]), scale);
  check('rescaled value is written back into the row', sc.shownKcal === '260', scale);
  check('meal totals follow the new portion', /510 kcal/.test(sc.totals), sc.totals);
  check('scaling is reversible (density preserved)',
        JSON.stringify(sc.reverted) === JSON.stringify([100, 130]), scale);
  await evalJson(`closeSheets()`);

  // An item with no starting weight has no density to scale by: it must start in
  // manual mode (macros editable) and say why, rather than silently ignoring edits.
  const zg = await evalJson(`(() => {
    openReview({ estimate: { description: 'x', calories: 250, protein: 12.5, carbs: 28,
                             fat: 11, items: [{ name: 'mystery', grams: 0, calories: 250,
                             protein: 12.5, carbs: 28, fat: 11 }] } });
    const hintEl = document.getElementById('r-scale-hint');
    const before = { hinted: !hintEl.hidden,
                     mode: document.querySelector('[data-mode="0"]').textContent.trim(),
                     kcalReadonly: document.querySelector('.item-row[data-i="0"] input[data-f="calories"]').readOnly };
    const g = document.querySelector('.item-row[data-i="0"] input[data-f="grams"]');
    g.value = '150'; g.dispatchEvent(new Event('input', { bubbles: true }));
    return JSON.stringify({ before, grams: compItems[0].grams,
                            kcalKept: compItems[0].calories });
  })()`);
  const z = JSON.parse(zg);
  check('zero-weight item starts in manual mode with editable macros',
        z.before.mode === 'manual' && z.before.kcalReadonly === false, zg);
  check('zero-weight item explains why it cannot scale', z.before.hinted === true, zg);
  check('zero-weight item records grams without inventing macros',
        z.grams === 150 && z.kcalKept === 250, zg);
  await evalJson(`closeSheets()`);

  // ---- Automatic macro calculation (default) + manual override -----------
  const auto = await evalJson(`(() => {
    openReview({ estimate: { description: '8g of brazil nuts', calories: 52, protein: 1.1,
                             carbs: 1, fat: 5.3, items: [{ name: 'Brazil nuts', grams: 8,
                             calories: 52, protein: 1.1, carbs: 1, fat: 5.3 }] } });
    const rowSel = '.item-row[data-i="0"]';
    const out = {
      mode: document.querySelector('[data-mode="0"]').textContent.trim(),
      macrosReadOnly: document.querySelector(rowSel + ' input[data-f="calories"]').readOnly,
    };
    const g = document.querySelector(rowSel + ' input[data-f="grams"]');
    g.value = '10'; g.dispatchEvent(new Event('input', { bubbles: true }));
    out.after10 = { grams: compItems[0].grams, kcal: compItems[0].calories,
                    p: compItems[0].protein, c: compItems[0].carbs, f: compItems[0].fat,
                    shownKcal: document.querySelector(rowSel + ' input[data-f="calories"]').value,
                    totals: document.getElementById('r-total-shown').textContent.trim() };
    // manual mode: macros become editable and grams stops rescaling them
    document.querySelector('[data-mode="0"]').click();
    const kc = document.querySelector(rowSel + ' input[data-f="calories"]');
    out.manualEditable = !kc.readOnly;
    kc.value = '150'; kc.dispatchEvent(new Event('input', { bubbles: true }));
    const g2 = document.querySelector(rowSel + ' input[data-f="grams"]');
    g2.value = '20'; g2.dispatchEvent(new Event('input', { bubbles: true }));
    out.manualAfter = { kcal: compItems[0].calories, grams: compItems[0].grams,
                        mode: document.querySelector('[data-mode="0"]').textContent.trim() };
    return JSON.stringify(out);
  })()`);
  const au = JSON.parse(auto);
  check('macros are read-only in auto mode (grams is the input)',
        au.mode === 'auto' && au.macrosReadOnly === true, auto);
  check('editing grams recalculates macros to the new portion',
        Math.round(au.after10.kcal) === 65 && au.after10.shownKcal === '65'
        && au.after10.grams === 10, auto);
  check('recalculated macros reach the rendered row and totals',
        au.after10.p === 1.4 && /65 kcal/.test(au.after10.totals), auto);
  check('manual mode unlocks the macro fields', au.manualEditable === true, auto);
  check('manual macros are not overwritten by a gram change',
        au.manualAfter.kcal === 150 && au.manualAfter.grams === 20
        && au.manualAfter.mode === 'manual', auto);
  await evalJson(`closeSheets()`);

  // ---- Description naming a different portion offers a one-tap apply ----
  const desc = await evalJson(`(() => {
    openReview({ estimate: { description: 'brazil nuts', calories: 52, protein: 1.1,
                             carbs: 1, fat: 5.3, items: [{ name: 'Brazil nuts', grams: 8,
                             calories: 52, protein: 1.1, carbs: 1, fat: 5.3 }] } });
    const btn = document.getElementById('btn-apply-desc-grams');
    const hiddenAtOpen = btn.hidden;
    const d = document.getElementById('r-desc');
    d.value = '10g of brazil nuts'; d.dispatchEvent(new Event('input', { bubbles: true }));
    const offered = { hidden: btn.hidden, label: btn.textContent };
    btn.click();
    return JSON.stringify({ hiddenAtOpen, offered, grams: compItems[0].grams,
                            kcal: compItems[0].calories,
                            hiddenAfter: document.getElementById('btn-apply-desc-grams').hidden });
  })()`);
  const de = JSON.parse(desc);
  check('description portion mismatch is surfaced, not ignored',
        de.hiddenAtOpen === true && de.offered.hidden === false
        && /10\s*g/.test(de.offered.label), desc);
  check('applying the described portion rescales macros',
        de.grams === 10 && Math.round(de.kcal) === 65 && de.hiddenAfter === true, desc);
  await evalJson(`closeSheets()`);

  // ---- Scaled values survive save and reopen in auto mode ----------------
  const round = await evalJson(`(async () => {
    openReview({ estimate: { description: 'auto save probe', calories: 52, protein: 1.1,
                             carbs: 1, fat: 5.3, items: [{ name: 'Brazil nuts', grams: 8,
                             calories: 52, protein: 1.1, carbs: 1, fat: 5.3 }] } });
    const g = document.querySelector('.item-row[data-i="0"] input[data-f="grams"]');
    g.value = '10'; g.dispatchEvent(new Event('input', { bubbles: true }));
    // The sheet stamps whichever day is being viewed, which may not be today
    // after the day-nav checks -- query the date it will actually save under.
    const saveDate = document.getElementById('r-date').value || todayISO();
    document.getElementById('btn-save-review').click();
    await new Promise(r => setTimeout(r, 3000));
    const st = await fetch('/api/state?date=' + encodeURIComponent(saveDate)).then(r => r.json());
    const m = st.meals.find(x => x.description === 'auto save probe');
    const saved = m ? { kcal: m.calories, item: (m.items || [])[0] } : null;
    let reopen = null;
    if (m) { closeSheets(); openReview({ estimate: m, editingId: m.id });
      reopen = { mode: document.querySelector('[data-mode="0"]').textContent.trim(),
                 grams: document.querySelector('.item-row[data-i="0"] input[data-f="grams"]').value }; }
    if (m) await fetch('/api/meals/' + m.id, { method: 'DELETE' });
    return JSON.stringify({ saved, reopen });
  })()`);
  const rt = JSON.parse(round);
  check('scaled portion persists after save',
        rt.saved && rt.saved.kcal === 65 && rt.saved.item.grams === 10
        && rt.saved.item.calories === 65, round);
  check('reopening a saved meal returns to auto mode at the saved weight',
        rt.reopen && rt.reopen.mode === 'auto' && rt.reopen.grams === '10', round);
  await evalJson(`closeSheets()`);

  // ---- Provider editor (LOCAL ONLY) -------------------------------------
  // This section PUTs /api/settings {providers}, and providers are SHARED
  // across all users -- running it against production would replace the real
  // vision chain with the offline stub (it did once). Skip unless the target
  // looks local or ML_ALLOW_PROVIDER_EDITS=1 says otherwise.
  const mayEditProviders = isLocalTarget || process.env.ML_ALLOW_PROVIDER_EDITS === '1';
  if (!mayEditProviders) console.log('[harness] skipping provider-editor checks (shared config; set ML_ALLOW_PROVIDER_EDITS=1 to force)');
  if (mayEditProviders) {
  // edits target the visible row, disabled entries stop being used. ----
  await evalJson(`(() => { document.querySelector('button[data-view="settings"]').click(); })()`);
  await sleep(500);
  const prov = await evalJson(`(async () => {
    await fetch('/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ providers: [{ kind: 'stub', model: '' },
                                         { kind: 'qwenplan', model: 'M-A' },
                                         { kind: 'groq', model: 'M-B' }] }) });
    await refresh();
    $('btn-edit-providers').click();
    await new Promise(r => setTimeout(r, 200));
    const out = { start: provDraft.map(p => [p.kind, p.model]) };
    document.querySelector('.prov-row[data-i="1"] [data-down]').click();   // qwenplan -> last
    await new Promise(r => setTimeout(r, 200));
    out.afterMove = provDraft.map(p => [p.kind, p.model]);
    // edit whichever row is visually first after the move
    const inp = document.querySelector('.prov-row[data-i="0"] input[data-f="model"]');
    out.firstKind = document.querySelector('.prov-row[data-i="0"] .prov-name').textContent;
    inp.value = 'EDITED'; inp.dispatchEvent(new Event('input', { bubbles: true }));
    out.afterEdit = provDraft.map(p => [p.kind, p.model]);
    // disable stub so only the real-ish chain remains
    const cb = document.querySelector('.prov-row[data-i="0"] input[data-toggle]');
    cb.checked = false; cb.dispatchEvent(new Event('change', { bubbles: true }));
    $('btn-save-providers').click();
    await new Promise(r => setTimeout(r, 2200));
    const st = await fetch('/api/state').then(r => r.json());
    out.saved = st.settings.providers.map(p => [p.kind, p.model, p.enabled !== false]);
    return JSON.stringify(out);
  })()`);
  const po = JSON.parse(prov);
  check('reorder moves the whole entry (model travels with provider)',
        JSON.stringify(po.afterMove) === JSON.stringify([['stub', ''], ['groq', 'M-B'], ['qwenplan', 'M-A']]), prov);
  check('editing targets the visible row', po.firstKind === 'stub' && po.afterEdit[0][1] === 'EDITED', prov);
  check('provider edits persist to the server',
        Array.isArray(po.saved) && po.saved.length === 3 && po.saved[0][2] === false, prov);
  }

  // ---- Trend view must actually paint (regression: prepCanvas returned `h`
  // while callers destructured `hh`, leaving every y-coordinate NaN so both
  // canvases drew nothing and the screen looked like empty boxes). ----------
  await evalJson(`(async () => {
    for (let i = 20; i >= 0; i--) {
      const d = new Date(Date.now() - i * 864e5);
      await fetch('/api/weight', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: +(90 - i * 0.22).toFixed(2), date: d.toISOString().slice(0, 10), unit: 'kg' }) });
    }
    for (let i = 5; i >= 0; i--) {
      const d = new Date(Date.now() - i * 864e5);
      await fetch('/api/meals', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description: 'trend meal ' + i, calories: 600, protein: 40, carbs: 60, fat: 18, date: d.toISOString().slice(0, 10) }) });
    }
  })()`);
  await sleep(1200);
  await evalJson(`document.querySelector('button[data-view="trend"]').click()`);
  await sleep(2500); await watchdog(); drain();
  const painted = await evalJson(`(() => {
    const ink = (id) => { const c = document.getElementById(id);
      const x = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0;
      for (let i = 3; i < x.length; i += 4) if (x[i] > 0) n++;
      return { n, w: c.width, h: c.height }; };
    return { weight: ink('chart-weight'), intake: ink('chart-intake') };
  })()`);
  check('weight chart paints pixels', painted.weight.n > 500, JSON.stringify(painted.weight));
  check('intake chart paints pixels', painted.intake.n > 500, JSON.stringify(painted.intake));
  // Canvas backing store must stay stable across redraws (the compounding bug).
  const stab = await evalJson(`(async () => {
    const h = await api('history?days=90'); const out = [];
    for (let i = 0; i < 4; i++) { drawWeightChart(h); out.push(document.getElementById('chart-weight').height); }
    return out; })()`);
  check('canvas height stable across redraws', new Set(stab).size === 1 && stab[0] <= 400, JSON.stringify(stab));
  check('trend stats populated', await evalJson(
    `document.getElementById('trend-stats').textContent.includes('Current')`));

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
