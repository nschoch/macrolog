/* MacroLog client. No build step, no dependencies. */
'use strict';

const $ = (id) => document.getElementById(id);
const CIRC = 2 * Math.PI * 86;
let state = null;
let pendingPhoto = null;   // { temp_photo, dataUrl }
// Which day the Today view is showing. '' = today (server default).
// All writes (meals, weights) stamp this date while browsing a past day.
let viewDate = '';   // '' = today, else YYYY-MM-DD being browsed

// --------------------------------------------------------------- units layer
// Canonical storage is metric (kg / cm). Imperial is a pure display + input
// conversion at the edges, so toggling never rewrites history or the DB.
const LB_PER_KG = 2.2046226;
const CM_PER_IN = 2.54;

const U = {
  get imperial() { return state?.settings?.units === 'imperial'; },
  kgToLb(kg) { return kg * LB_PER_KG; },
  lbToKg(lb) { return lb / LB_PER_KG; },
  // Weight as a formatted string in the active unit ("176.4 lb" / "80.0 kg").
  weight(kg, withUnit = true) {
    if (kg == null || isNaN(kg)) return '–';
    const v = this.imperial ? this.kgToLb(kg) : kg;
    return v.toFixed(1) + (withUnit ? (this.imperial ? ' lb' : ' kg') : '');
  },
  // Weight without decimals for compact stat cards.
  weightInt(kg) {
    if (kg == null || isNaN(kg)) return '–';
    return String(Math.round(this.imperial ? this.kgToLb(kg) : kg));
  },
  // Rate of change per week, converted from kg/wk.
  rate(kgPerWk, withUnit = true) {
    if (kgPerWk == null || isNaN(kgPerWk)) return '–';
    const sign = kgPerWk > 0 ? '+' : '';
    const v = this.imperial ? this.kgToLb(kgPerWk) : kgPerWk;
    return sign + v.toFixed(this.imperial ? 1 : 2) +
      (withUnit ? (this.imperial ? ' lb/wk' : ' kg/wk') : '');
  },
  // Height: metric shows "178 cm"; imperial shows feet+inches like 5'10".
  height(cm) {
    if (cm == null || isNaN(cm)) return '';
    if (!this.imperial) return Math.round(cm) + ' cm';
    const totalIn = cm / CM_PER_IN;
    let ft = Math.floor(totalIn / 12), inch = Math.round(totalIn % 12);
    if (inch === 12) { ft += 1; inch = 0; }
    return `${ft}'${inch}"`;
  },
  // Parse a height entered in the active unit back to canonical cm.
  heightToCm(value, inches) {
    if (this.imperial) {
      const ft = Number(value) || 0;
      const ins = Number(inches) || 0;
      return (ft * 12 + ins) * CM_PER_IN;
    }
    return Number(value) || 0;
  },
  // Parse a weight entered in the active unit back to canonical kg.
  weightToKg(value) { return this.imperial ? this.lbToKg(Number(value)) : Number(value); },
  wUnitLabel() { return this.imperial ? 'lb' : 'kg'; },
};

// ------------------------------------------------------------------ helpers
// The browser's IANA zone. Sent on every request so the server interprets the
// wall-clock times we post (and "today") in the user's timezone rather than the
// server's. Recomputed per call so a device that changes zone self-corrects.
function clientTz() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; }
  catch (_) { return ''; }
}

async function api(path, opts = {}) {
  const res = await fetchWithTimeout('/api/' + path,
    { ...opts, headers: { ...(opts.headers || {}), 'X-Client-TZ': clientTz() } });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (res.status === 401 && data.login) {
    // Session missing/expired: bounce to OIDC login, preserving current view.
    window.location.assign(data.login + '?next=' + encodeURIComponent(location.pathname));
    throw new Error('signing in…');
  }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// Hard deadline for every API call: a wedged service worker (seen with
// headless Chrome + SW-controlled pages) can hang fetches forever; without a
// timeout the UI just sits on the spinner and never recovers.
async function fetchWithTimeout(url, opts = {}, ms = 25000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try {
    return await fetch(url, { ...opts, signal: ctl.signal });
  } catch (err) {
    if (err && err.name === 'AbortError') throw new Error('Server not responding — check it is running, then retry.');
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

function toast(msg, isErr = false) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast' + (isErr ? ' err' : '');
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, 3400);
}

function loading(on, text = 'Analyzing photo…') {
  $('loading-text').textContent = text;
  $('loading').hidden = !on;
}

const fmt = (n) => Math.round(Number(n) || 0).toLocaleString();

// --------------------------------------------------------------------- tabs
$('tabs').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-view]');
  if (!btn) return;
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('active', b === btn));
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  $('view-' + btn.dataset.view).classList.add('active');
  if (btn.dataset.view === 'trend') loadTrend();
  if (btn.dataset.view === 'settings') renderSettings();
});

// --------------------------------------------------------------- today view
function renderUserChip(s) {
  const chip = $('user-chip');
  if (!chip) return;
  if (s.auth_enabled && s.user) {
    chip.hidden = false;
    chip.textContent = (s.user.name || s.user.email || 'account').split(' ')[0].slice(0, 12);
    chip.title = `${s.user.email || s.user.name} · tap to sign out`;
  } else {
    chip.hidden = true;
  }
}

const DAY_FMT = { weekday: 'short', month: 'short', day: 'numeric' };
function isoShift(iso, days) {
  const d = new Date(iso + 'T12:00:00');   // noon avoids DST edge shifts
  d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function renderDayHeader(day) {
  const el = $('day-title');
  if (!el) return;
  const d = new Date(day + 'T12:00:00');
  const label = d.toLocaleDateString(undefined, DAY_FMT);
  el.textContent = !viewDate ? 'Today' : day === todayISO() ? 'Today' : label;
  $('btn-day-next').disabled = isoShift(day, 1) > todayISO();
  const pick = $('day-pick');
  pick.value = day; pick.max = todayISO();
  // Keep the (hidden) input parked under the title so when Safari anchors its
  // native picker to the input's rect, it appears there — not bottom-left.
  const r = el.getBoundingClientRect();
  pick.style.top = Math.round(r.top + window.scrollY - 6) + 'px';
}

$('btn-day-prev').addEventListener('click', () => {
  const d = isoShift(state?.date || todayISO(), -1);
  viewDate = d === todayISO() ? '' : d;
  refresh();
});
$('btn-day-next').addEventListener('click', () => {
  const d = isoShift(state?.date || todayISO(), 1);
  if (d >= todayISO()) { viewDate = ''; refresh(); return; }
  viewDate = d; refresh();
});
// Tapping the date title opens the native picker. showPicker() (Chrome 99+,
// Safari 16+) anchors at the input's rect, which we park under the title.
// Fallback for older Safari: unhide briefly and focus — iOS still needs a tap
// inside the input itself to raise the wheel, so the title doubles as a hint.
$('day-title').addEventListener('click', () => {
  const pick = $('day-pick');
  const r = $('day-title').getBoundingClientRect();
  pick.style.top = Math.round(r.top + window.scrollY - 6) + 'px';
  if (typeof pick.showPicker === 'function') {
    try { pick.showPicker(); return; } catch (_) { /* outside a gesture or unsupported */ }
  }
  pick.style.opacity = '0.999'; pick.style.pointerEvents = 'auto';
  pick.focus();
  setTimeout(() => { pick.style.opacity = ''; pick.style.pointerEvents = ''; }, 400);
});
$('day-pick').addEventListener('change', (e) => {
  const d = e.target.value;
  if (!d) return;
  viewDate = d === todayISO() ? '' : d;
  refresh();
});

// Consumed vs remaining is a display preference, so it lives on the device
// rather than in the per-user settings blob.
function macroMode() {
  try { return localStorage.getItem('ml_macro_mode') === 'consumed' ? 'consumed' : 'remaining'; }
  catch (_) { return 'remaining'; }
}

function renderMacroMode() {
  const host = $('macro-mode');
  if (!host) return;
  const mode = macroMode();
  host.querySelectorAll('button[data-mode]').forEach(b =>
    b.classList.toggle('on', b.dataset.mode === mode));
}

$('macro-mode').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-mode]');
  if (!b) return;
  try { localStorage.setItem('ml_macro_mode', b.dataset.mode); } catch (_) { /* private mode */ }
  if (state) renderToday(state);
});

function renderToday(s) {
  renderUserChip(s);
  renderDayHeader(s.date);
  renderMacroMode();
  const c = s.consumed, t = s.targets;
  const exercise = s.exercise || 0;
  const remaining = t.calories - c.calories + exercise;
  const left = macroMode() === 'remaining';

  $('kcal-left').textContent = fmt(left ? Math.abs(remaining) : c.calories);
  $('ring-label').textContent = left
    ? (remaining >= 0 ? 'kcal left' : 'kcal over')
    : 'kcal eaten';

  const frac = Math.max(0, Math.min(1, (c.calories - exercise) / (t.calories || 1)));
  const ring = $('ring-cal');
  ring.style.strokeDashoffset = CIRC * (1 - frac);
  ring.style.stroke = remaining < -50 ? 'var(--danger)'
    : remaining < 250 ? 'var(--carbs)' : 'var(--accent)';

  const macros = [
    ['protein', 'Protein', c.protein, t.protein, 'var(--protein)'],
    ['carbs', 'Carbs', c.carbs, t.carbs, 'var(--carbs)'],
    ['fat', 'Fat', c.fat, t.fat, 'var(--fat)'],
  ];
  const fiberGot = c.fiber || 0, fiberWant = t.fiber || 0;
  $('macro-row').innerHTML = macros.map(([key, label, got, want, color]) => {
    const shown = left ? Math.max(0, want - got) : got;
    // Fiber rides along with carbs as a subset, so it is a quiet sub-line there
    // rather than a fourth bar implying another thing to hit.
    const sub = key === 'carbs'
      ? `<div class="macro-sub"><span>fiber</span><b>${fmt(fiberGot)} / ${fmt(fiberWant)}g</b></div>`
      : '';
    return `
    <div class="macro">
      <div class="macro-name">${label}</div>
      <div class="macro-val">${fmt(shown)}<span>of ${fmt(want)}g</span></div>
      <div class="bar"><i style="width:${Math.min(100, want ? got / want * 100 : 0)}%;background:${color}"></i></div>${sub}
    </div>`;
  }).join('');

  renderStatus(s);
  renderGroups(s.groups, s.meals);
}

function renderStatus(s) {
  const bits = [];
  const ready = (s.providers || []).filter(p => p.configured && p.kind !== 'stub');
  if (!ready.length) {
    bits.push(['warn', 'No vision provider configured — estimates use the offline placeholder. Add a key in Settings.']);
  }
  const tr = s.trend || {};
  if (tr.reliable && tr.slope_kg_per_week != null) {
    const rate = tr.slope_kg_per_week;
    bits.push(['ok', `Weight trend: <b>${U.rate(rate)}</b> · smoothed <b>${U.weight(tr.current_kg)}</b>`]);
  } else if ((s.weights_count || 0) < 4) {
    bits.push(['', `Log your weight daily — the target adapts after ~${4 - (s.weights_count || 0)} more weigh-ins.`]);
  }
  if (s.needs_setup) bits.push(['warn', 'Tell us your height and goal in Settings to seed your calorie target.']);
  if (s.exercise) bits.push(['', `<b>+${fmt(s.exercise)}</b> kcal credited from activity today`]);

  $('status-strip').innerHTML = bits.map(([cls, html]) =>
    `<div class="pill ${cls}">${html}</div>`).join('');
}

function confColor(c) {
  return c >= 0.75 ? 'var(--accent)' : c >= 0.5 ? 'var(--carbs)' : 'var(--danger)';
}

function mealRowHtml(m) {
    const time = fmtTime(m.ts);
    // A meal may carry several photos (plate + nutrition label); show them all.
    const pics = (m.photos && m.photos.length ? m.photos : (m.photo ? [m.photo] : []));
    const thumb = pics.length
      ? pics.slice(0, 2).map(p => `<img class="meal-thumb" src="/api/photo/${encodeURIComponent(p)}" alt="" loading="lazy">`).join('')
      : `<div class="meal-thumb"></div>`;
    const comps = (m.items || []);
    const compHtml = comps.length ? `
      <ul class="comp-list">${comps.map(c =>
        `<li><span class="c-name">${esc(c.name || 'item')}</span><span class="c-g">${fmt(c.grams)}g</span><span class="c-k">${fmt(c.calories)}</span></li>`
      ).join('')}</ul>` : '';
  return `<li class="meal ${comps.length ? 'has-comps' : ''}" data-id="${m.id}">
      <div class="meal-row" data-toggle="${m.id}">
        ${thumb}
        <div class="meal-info">
          <div class="meal-desc">${esc(m.description || m.kind || 'Meal')}</div>
          <div class="meal-meta"><span class="conf-dot" style="background:${confColor(m.confidence)}"></span>${time}${m.kind && m.kind !== 'other' ? ' · ' + cap(m.kind) : ''}${m.source === 'manual' ? ' · manual' : ''}${comps.length ? ` · ${comps.length} items` : ''}</div>
        </div>
        <div class="meal-kcal">${fmt(m.calories)}<small>kcal</small></div>
        <div class="meal-actions">
          <button class="icon-btn" data-act="edit" aria-label="Edit meal">
            <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 20h4L20 8l-4-4L4 16v4z"/></svg>
          </button>
          <button class="icon-btn" data-act="del" aria-label="Delete meal">
            <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 7h14M9 7V5h6v2M7 7l1 13h8l1-13"/></svg>
          </button>
        </div>
      </div>
      ${compHtml}
    </li>`;
}

// Grouped day view: server clusters records into eatings by time proximity, so
// logging a second dish does not create a competing "meal" and the label comes
// from when you ate rather than a dropdown you have to answer every time.
// Clock-time rendering honours the per-user preference. Epochs are the storage
// format, so switching format never rewrites data -- it is display only.
function is24h() {
  return (state?.settings?.time_format || '12h') === '24h';
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  if (is24h()) {
    return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }
  return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

function renderGroups(groups, meals) {
  const host = $('meal-groups');
  const byId = Object.fromEntries((meals || []).map(m => [m.id, m]));
  $('meals-empty').hidden = (meals || []).length > 0;
  if (!groups || !groups.length) { host.innerHTML = ''; return; }
  host.innerHTML = groups.map(g => {
    const rows = g.meal_ids.map(id => byId[id]).filter(Boolean).map(mealRowHtml).join('');
    const t = fmtTime(g.start_ts);
    const extra = g.count > 1 ? ` · ${g.count} items` : '';
    return `<section class="meal-group">
      <header class="group-head">
        <h3>${esc(g.label)}</h3>
        <span class="group-meta">${t}${extra}</span>
        <span class="group-kcal">${fmt(g.calories)}<small>kcal</small></span>
      </header>
      <ul class="meal-list">${rows}</ul>
    </section>`;
  }).join('');
}

const esc = (s) => String(s || '').replace(/[&<>"]/g, ch =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
const cap = (s) => s ? s[0].toUpperCase() + s.slice(1) : '';

// ------------------------------------------------------------- meal actions
$('meal-groups').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-act]');
  if (!btn) return;
  const li = btn.closest('.meal');
  const id = Number(li.dataset.id);
  const meal = (state?.meals || []).find(m => m.id === id);
  if (!meal) return;

  if (btn.dataset.act === 'del') {
    if (!confirm(`Delete "${meal.description || 'this meal'}"?`)) return;
    try {
      state = await api(`meals/${id}`, { method: 'DELETE' }).then(refresh);
      toast('Meal deleted');
    } catch (err) { toast(err.message, true); }
    return;
  }

  if (btn.dataset.act === 'edit') {
    openReview({
      estimate: {
        description: meal.description, calories: meal.calories,
        protein: meal.protein, carbs: meal.carbs, fat: meal.fat,
        items: meal.items || [], ts: meal.ts, date: meal.date,
      },
      provider: meal.provider, model: meal.model, editingId: id,
      photoUrl: meal.photo ? `/api/photo/${encodeURIComponent(meal.photo)}` : null,
    });
  }
});

// Tap a meal row (not its action buttons): on today, expand/collapse the
// breakdown; on a past day, open it for editing — that's how you fix an old log.
$('meal-groups').addEventListener('click', async (e) => {
  if (e.target.closest('[data-act]')) return;
  const head = e.target.closest('.meal-row'); if (!head) return;
  if (!viewDate) { head.parentElement.classList.toggle('open'); return; }
  try {
    const m = await api(`meals/${head.dataset.toggle}`);
    openReview({ estimate: m, provider: m.provider, model: m.model,
      editingId: m.id, photoUrl: m.photo ? `/api/photo/${encodeURIComponent(m.photo)}` : null });
  } catch (err) { toast(err.message, true); }
});

// ------------------------------------------------------------ review sheet
let compItems = [];        // live editable components for the open meal

function nowHM() { const d = new Date(); return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`; }
function todayISO() { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }

// Recompute a component's macros from grams, preserving per-gram density.
// ---------------------------------------------------------- auto vs manual
// Default is AUTO: grams is the single input and kcal/P/C/F are derived from the
// item's per-gram density, so any portion change always updates the macros.
// MANUAL hands the macro fields to the user, for cases where the numbers come
// from a printed label and must not be scaled by an estimated weight.
//
// Density is anchored once (_unit = macros per gram) rather than recomputed from
// rounded values on every keystroke -- otherwise repeated edits drift.
function densityOf(it) {
  if (!(+it.grams > 0)) return null;
  const g = +it.grams;
  return { calories: (+it.calories || 0) / g, protein: (+it.protein || 0) / g,
           carbs: (+it.carbs || 0) / g, fat: (+it.fat || 0) / g,
           fiber: (+it.fiber || 0) / g };
}

function ensureDensity(it) {
  if (it._unit) return it._unit;
  const d = densityOf(it);
  if (d) it._unit = d;
  return it._unit || null;
}

function isManual(it) { return it._manual === true; }

function applyDensity(it, grams) {
  it.grams = grams;
  const u = it._unit;
  if (!u || !(grams >= 0)) return it;
  it.calories = +(u.calories * grams).toFixed(1);
  it.protein = +(u.protein * grams).toFixed(1);
  it.carbs = +(u.carbs * grams).toFixed(1);
  it.fat = +(u.fat * grams).toFixed(1);
  it.fiber = +((u.fiber || 0) * grams).toFixed(1);
  return it;
}

// Portion change. Auto rows recompute macros from density; manual rows only
// record the weight (the user owns those numbers).
function rescaleItem(it, newGrams) {
  if (isManual(it)) { it.grams = newGrams; return it; }
  if (!(+it.grams > 0)) {
    // No baseline weight: nothing to derive from (e.g. a model that returned
    // macros but no grams). Record the weight; the hint explains the rest.
    it.grams = newGrams;
    return it;
  }
  applyDensity(it, newGrams);
  return it;
}

function sumComps() {
  return compItems.reduce((a, i) => ({
    calories: a.calories + (+i.calories || 0), protein: a.protein + (+i.protein || 0),
    carbs: a.carbs + (+i.carbs || 0), fat: a.fat + (+i.fat || 0),
    fiber: a.fiber + (+i.fiber || 0),
  }), { calories: 0, protein: 0, carbs: 0, fat: 0, fiber: 0 });
}

function refreshTotals() {
  const hasComps = compItems.length > 0;
  const t = sumComps();
  $('r-total-shown').textContent = `${fmt(t.calories)} kcal · P${Math.round(t.protein)} `
    + `C${Math.round(t.carbs)} F${Math.round(t.fat)}`
    + (t.fiber ? ` · fiber ${Math.round(t.fiber)}` : '');
  // When components exist they are the source of truth; reflect them into the
  // (collapsible) manual total fields so either view is consistent.
  if (hasComps) {
    $('r-cal').value = Math.round(t.calories); $('r-pro').value = Math.round(t.protein);
    $('r-car').value = Math.round(t.carbs); $('r-fat').value = Math.round(t.fat);
    if ($('r-fib')) $('r-fib').value = +(t.fiber || 0).toFixed(1);
  }
  $('r-nocomp-hint').hidden = hasComps;
  $('r-totals-wrap').style.display = hasComps ? 'block' : 'none';

  // Explain the asterisk on the g label: with no starting weight there is no
  // per-gram density to scale by. Evaluated last so it tracks the typed value.
  const hint = $('r-scale-hint');
  if (hint) {
    // Show it for ANY row without a weight, regardless of mode: with grams=0
    // there is no density, so scaling cannot work until one is supplied.
    const needsHint = compItems.some(i => !(+i.grams > 0));
    hint.hidden = !needsHint || !hasComps;
    if (!hint.hidden) hint.textContent = '* no portion weight yet \u2014 enter grams to scale automatically, or edit the macros directly.';
  }
  syncDescGrams();
}

// The description is a label, not a portion input, so typing "10g of nuts" there
// cannot rescale macros on its own. Rather than silently ignoring it, offer a
// one-tap apply whenever a single item's weight disagrees with the text.
function descGrams() {
  const t = $('r-desc') ? $('r-desc').value : '';
  const m = t.match(/(\d+(?:[.,]\d+)?)\s*(?:g|grams?)\b/i);
  return m ? parseFloat(m[1].replace(',', '.')) : null;
}

function syncDescGrams() {
  const btn = $('btn-apply-desc-grams');
  if (!btn) return;
  const want = descGrams();
  if (want == null || !(want > 0) || compItems.length !== 1) { btn.hidden = true; return; }
  const cur = +compItems[0].grams || 0;
  if (Math.abs(want - cur) < 0.05) { btn.hidden = true; return; }
  btn.hidden = false;
  btn.dataset.grams = String(want);
  btn.textContent = `Use ${want} g from description`;
}

$('r-desc').addEventListener('input', syncDescGrams);
$('btn-apply-desc-grams').addEventListener('click', () => {
  const want = Number($('btn-apply-desc-grams').dataset.grams);
  if (!(want > 0) || compItems.length !== 1) return;
  const it = compItems[0];
  if (!isManual(it) && !it._unit) { const d = densityOf(it); if (d) it._unit = d; }
  rescaleItem(it, want);
  renderCompEditor();
  toast(`Portion set to ${want} g`);
});

// Write an item's recomputed macros into its EXISTING row inputs. The focused
// element is skipped deliberately: assigning .value to the field being typed in
// resets the caret even without a full re-render (and drops the iOS keyboard).
function syncRowFromItem(row, it) {
  for (const inp of row.querySelectorAll('input[data-f]')) {
    const f = inp.dataset.f;
    if (f === 'grams' || f === 'name') continue;   // never touch what's typed
    if (inp === document.activeElement) continue;
    const shown = (f === 'protein' || f === 'fat' || f === 'carbs' || f === 'fiber')
      ? String(+(+it[f]).toFixed(1)) : String(Math.round(+it[f] || 0));
    if (inp.value !== shown) inp.value = shown;
  }
}

function renderCompEditor() {
  const wrap = $('r-items');
  if (!compItems.length) { wrap.innerHTML = ''; refreshTotals(); return; }
  wrap.innerHTML = compItems.map((it, idx) => {
    const manual = isManual(it);
    const ro = manual ? '' : 'readonly';
    const weightKnown = +it.grams > 0;
    // Labels are spelled out (P g / F g were unreadable) and the unit lives
    // INSIDE the field so "6" reads as "6 g" without an extra column.
    const field = (label, key, unit, opts = '') => `
      <label class="ir-cell"><span class="ir-lab">${label}</span>
        <span class="num"><input type="number" inputmode="numeric"
          value="${opts.raw ? opts.raw : (+it[key] || 0)}" data-f="${key}" ${opts.ro || ''}><i>${unit}</i></span>
      </label>`;
    return `
    <div class="item-row" data-i="${idx}">
      <div class="ir-top">
        <input class="ir-name" value="${esc(it.name)}" placeholder="food" data-f="name">
        <button class="ir-mode ${manual ? 'on' : ''}" data-mode="${idx}"
          title="${manual ? 'Macros are yours to edit; grams does not rescale them'
                          : 'Macros are calculated from grams — tap to edit them yourself'}">
          ${manual ? 'manual' : 'auto'}</button>
        <button class="ir-del" title="Remove" data-del="${idx}">×</button>
      </div>
      <div class="ir-fields">
        ${field(weightKnown || manual ? 'Grams' : 'Grams *', 'grams', 'g',
                 { raw: +it.grams || '' })}
        ${field('Calories', 'calories', 'kcal', { ro, raw: Math.round(it.calories) || '' })}
        ${field('Protein', 'protein', 'g', { ro, raw: (+it.protein).toFixed(1) })}
        ${field('Carbs', 'carbs', 'g', { ro, raw: (+it.carbs).toFixed(1) })}
        ${field('Fat', 'fat', 'g', { ro, raw: (+it.fat).toFixed(1) })}
        ${field('Fiber <em>of carbs</em>', 'fiber', 'g', { raw: (+(it.fiber || 0)).toFixed(1) })}
      </div>
    </div>`;
  }).join('');
  refreshTotals();
}

function openReview({ estimate, provider, model, calibrated, previewUrl, photoUrl, editingId, warning }) {
  const e = estimate || {};
  $('review-sheet').dataset.editing = editingId || '';
  // 'photo' covers camera shots; typed descriptions have no image.
  $('review-sheet').dataset.source = previewUrl || photoUrl ? 'photo' : 'text';
  $('r-desc').value = e.description || '';
  compItems = (e.items || []).map(i => {
    const copy = { ...i };
    // Anchor density from the estimate so later gram edits scale exactly rather
    // than compounding rounding. No weight -> cannot derive, so start that row in
    // manual mode (the macro fields are editable straight away).
    if (+copy.grams > 0) copy._unit = densityOf(copy);
    else copy._manual = true;
    return copy;
  });
  // Manual totals as fallback when there are no components.
  $('r-cal').value = Math.round(e.calories || 0);
  $('r-pro').value = Math.round(e.protein || 0);
  $('r-car').value = Math.round(e.carbs || 0);
  $('r-fat').value = Math.round(e.fat || 0);

  // Date/time default to now (or the meal's stored time when editing).
  const ts = e.ts ? new Date(e.ts * 1000) : new Date();
  $('r-date').value = editingId && e.date ? e.date : (viewDate || todayISO());
  $('r-time').value = editingId && e.ts ? `${String(ts.getHours()).padStart(2,'0')}:${String(ts.getMinutes()).padStart(2,'0')}` : nowHM();

  const img = $('review-img');
  const src = previewUrl || photoUrl;
  img.src = src || '';
  img.style.display = src ? 'block' : 'none';
  // "Choose different photo" only makes sense when a photo is involved — hide
  // it when editing an existing meal or for text-described meals (no image).
  const pa = document.querySelector('.photo-actions');
  if (pa) pa.style.display = (editingId || !src) ? 'none' : 'block';

  renderCompEditor();

  const conf = Math.round((e.confidence ?? 0.5) * 100);
  const warn = $('r-warn');
  if (warn) {
    warn.textContent = warning || '';
    warn.hidden = !warning;
  }

  $('r-prov').innerHTML = `${cap(provider || 'manual')}${model ? ' · ' + esc(model) : ''} · confidence <b style="color:${confColor(e.confidence ?? .5)}">${conf}%</b>${calibrated ? ' · calibrated by your history' : ''}`;

  $('review-sheet').hidden = false;
  document.body.style.overflow = 'hidden';
}

// Component editor event delegation (lives on the container, survives re-render).
$('r-items').addEventListener('input', (ev) => {
  const row = ev.target.closest('.item-row'); if (!row) return;
  const idx = +row.dataset.i, f = ev.target.dataset.f, raw = ev.target.value;
  const it = compItems[idx]; if (!it) return;
  if (f === 'name') { it.name = raw; return; }
  const val = Number(raw) || 0;
  if (f === 'fiber') {
    // Fiber is editable even in auto mode: the estimator sometimes omits it and
    // meals logged before it existed store 0, but it never affects the calorie
    // budget. Clamp to carbs (the subset rule) and re-anchor per gram so later
    // portion changes keep the correction.
    const cap = +it.carbs || 0;
    const v = cap > 0 ? Math.min(val, cap) : val;
    if (val > cap && cap > 0) toast(`Fiber can't exceed carbs (${cap} g)`, true);
    it.fiber = v;
    if (+it.grams > 0) {
      it._unit = Object.assign({}, it._unit || densityOf(it) || {}, { fiber: v / (+it.grams) });
    }
    syncRowFromItem(row, it);
    refreshTotals();
    return;
  }
  if (f === 'grams') {
    rescaleItem(it, val);
    // Never re-render here: replacing innerHTML destroys the input being typed
    // into, resetting the caret and dismissing the iOS keyboard.
    syncRowFromItem(row, it);
    refreshTotals();
    return;
  }
  if (isManual(it)) {
    it[f] = val;
    if (f === 'carbs' && (+it.fiber || 0) > val) it.fiber = val;
    // A manual edit re-anchors density from what is on screen, so flipping back
    // to auto scales from the numbers the user just confirmed.
    if (+it.grams > 0) {
      const d = densityOf(it);
      if (d) it._unit = d;
    }
    refreshTotals();
  }
  // Auto rows keep their macro inputs readonly, so this branch is unreachable
  // there; nothing to do if an event still arrives.
});

$('r-items').addEventListener('click', (ev) => {
  const mode = ev.target.closest('[data-mode]');
  if (mode) {
    const it = compItems[+mode.dataset.mode]; if (!it) return;
    if (isManual(it)) {
      // manual -> auto: re-derive from the values on screen so nothing jumps.
      if (!(+it.grams > 0)) {
        toast('Give it a weight first, then macros can be calculated', true);
        return;
      }
      const d = densityOf(it);
      if (d) it._unit = d;
      it._manual = false;
      toast('Macros now follow grams');
    } else {
      it._manual = true;      // auto -> manual: keep current numbers as the base
      toast('Macros are yours to edit now');
    }
    renderCompEditor();
    return;
  }
  const del = ev.target.closest('[data-del]'); if (!del) return;
  compItems.splice(+del.dataset.del, 1); renderCompEditor();
});

$('btn-add-item').addEventListener('click', () => {
  compItems.push({ name: '', grams: 0, calories: 0, protein: 0, carbs: 0, fat: 0 });
  renderCompEditor();
  const rows = $('r-items').querySelectorAll('.item-row');
  if (rows.length) rows[rows.length - 1].querySelector('.ir-name').focus();
});

function closeSheets() {
  $('review-sheet').hidden = true;
  $('review-sheet').dataset.editing = '';
  $('review-sheet').dataset.source = '';
  $('manual-sheet').hidden = true;
  $('add-sheet').hidden = true;
  document.body.style.overflow = '';
}

$('btn-cancel-review').addEventListener('click', closeSheets);
$('review-sheet').addEventListener('click', (e) => { if (e.target.id === 'review-sheet') closeSheets(); });

$('btn-save-review').addEventListener('click', async () => {
  const hasComps = compItems.length > 0;
  const cleanComps = compItems
    .filter(i => (i.name || +i.grams || +i.calories))
    // _unit/_manual are view state; the server only stores real nutrition fields.
    .map(({ _unit, _manual, _status, ...rest }) => rest);
  const payload = {
    description: $('r-desc').value.trim(),
    kind: $('r-kind').value,
    date: $('r-date').value || viewDate || undefined,
    time: $('r-time').value || undefined,
    source: $('review-sheet').dataset.source === 'text' ? 'text' : 'photo',
  };
  if (cleanComps.length) {
    payload.items = cleanComps;
    const t = sumComps();
    payload.calories = t.calories; payload.protein = t.protein;
    payload.carbs = t.carbs; payload.fat = t.fat; payload.fiber = t.fiber;
  } else {
    payload.calories = Number($('r-cal').value) || 0;
    payload.protein = Number($('r-pro').value) || 0;
    payload.carbs = Number($('r-car').value) || 0;
    payload.fat = Number($('r-fat').value) || 0;
    payload.fiber = Number($('r-fib') ? $('r-fib').value : 0) || 0;
  }
  if (payload.calories <= 0) { toast('Calories must be above zero', true); return; }

  const editingId = $('review-sheet').dataset.editing;
  try {
    if (editingId) {
      await api(`meals/${editingId}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      toast('Updated — correction recorded');
    } else {
      await api('meals', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign({}, payload,
          stagedForSave.length ? { photos: stagedForSave }
                               : { photo_path: pendingPhoto })),
      });
      stagedForSave = [];
      clearStaged();
      pendingPhoto = null;
      toast('Logged');
    }
    closeSheets();
    await refresh();
  } catch (err) { toast(err.message, true); }
});

// -------------------------------------------------------------- manual entry
// (Manual entry is opened from the Add-meal sheet's 'Enter values'.)
$('btn-cancel-manual').addEventListener('click', closeSheets);
$('manual-sheet').addEventListener('click', (e) => { if (e.target.id === 'manual-sheet') closeSheets(); });

$('btn-save-manual').addEventListener('click', async () => {
  const payload = {
    description: $('m-desc').value.trim(),
    calories: Number($('m-cal').value) || 0,
    protein: Number($('m-pro').value) || 0,
    carbs: Number($('m-car').value) || 0,
    fat: Number($('m-fat').value) || 0,
    fiber: Number($('m-fib') ? $('m-fib').value : 0) || 0,
    kind: $('m-kind').value,
    source: 'manual',
    date: viewDate || undefined,
  };
  if (payload.calories <= 0) { toast('Calories must be above zero', true); return; }
  try {
    await api('meals', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    closeSheets();
    toast('Logged');
    await refresh();
  } catch (err) { toast(err.message, true); }
});

// ------------------------------------------------------- unified add-meal
// One entry point that composes inputs: N photos (plate and/or Nutrition Facts
// label) + optional free text. Sending them together lets the model use the
// label's measured values and the note's stated portion instead of guessing.
let stagedPhotos = [];   // {blob, url} before upload
let stagedForSave = [];  // server temp paths returned by estimate-meal

function renderTray() {
  const tray = $('photo-tray');
  if (!tray) return;
  tray.innerHTML = stagedPhotos.map((ph, i) => `
    <div class="thumb-wrap">
      <img src="${ph.url}" alt="photo ${i + 1}">
      <button class="thumb-x" data-drop="${i}" aria-label="Remove photo">&times;</button>
    </div>`).join('') +
    (stagedPhotos.length ? '' :
      '<p class="hint tray-empty">No photos yet — the food itself or its nutrition label both work.</p>');
}

function clearStaged() {
  for (const ph of stagedPhotos) {
    if (ph.url.startsWith('blob:') && URL.revokeObjectURL) URL.revokeObjectURL(ph.url);
  }
  stagedPhotos = [];
}

function openAddSheet() {
  // Start every entry from a clean slate. Clearing on OPEN rather than on each
  // exit path matters because saves close via inline code (not closeSheets)
  // and cancels come from several buttons -- any one of them forgetting a reset
  // used to leave the previous meal's note sitting in the box, ready to be
  // estimated again as if it were the new one.
  $('a-text').value = '';
  clearStaged();
  stagedForSave = [];
  $('add-sheet').hidden = false;
  document.body.style.overflow = 'hidden';
  renderTray();
}

$('btn-add-meal').addEventListener('click', openAddSheet);
$('btn-add-meal-fab').addEventListener('click', openAddSheet);
$('btn-cancel-add').addEventListener('click', () => { clearStaged(); closeSheets(); });
$('add-sheet').addEventListener('click', (e) => {
  if (e.target.id === 'add-sheet') { clearStaged(); closeSheets(); }
});

function stageFile(file) {
  if (!file) return;
  if (file.size > 12 * 1024 * 1024) { toast('Image too large (max 12MB)', true); return; }
  if (stagedPhotos.length >= 4) { toast('Up to 4 photos per meal', true); return; }
  const reader = new FileReader();
  reader.onload = () => {
    stagedPhotos.push({ blob: file, url: reader.result });
    renderTray();
  };
  reader.readAsDataURL(file);
}

$('btn-take-photo').addEventListener('click', () => $('file-input-cam').click());
$('btn-pick-photo').addEventListener('click', () => $('file-input').click());
$('file-input').addEventListener('change', (e) => {
  [...(e.target.files || [])].forEach(stageFile);
  e.target.value = '';
});
$('file-input-cam').addEventListener('change', (e) => {
  stageFile(e.target.files && e.target.files[0]);
  e.target.value = '';
});
$('photo-tray').addEventListener('click', (e) => {
  const b = e.target.closest('[data-drop]'); if (!b) return;
  const [ph] = stagedPhotos.splice(+b.dataset.drop, 1);
  if (ph && ph.url.startsWith('blob:')) URL.revokeObjectURL(ph.url);
  renderTray();
});

$('btn-estimate-meal').addEventListener('click', async () => {
  const text = $('a-text').value.trim();
  if (!stagedPhotos.length && !text) {
    toast('Add a photo or describe what you ate', true); return;
  }
  loading(true, 'Estimating\u2026');
  try {
    const form = new FormData();
    for (const ph of stagedPhotos) {
      form.append('photos', ph.blob, (ph.blob && ph.blob.name) || 'meal.jpg');
    }
    if (text) form.append('text', text);
    const res = await fetchWithTimeout('/api/estimate-meal',
      { method: 'POST', body: form, headers: { 'X-Client-TZ': clientTz() } }, 75000);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || 'Estimate failed');
    $('add-sheet').hidden = true;
    stagedForSave = data.temp_photos || [];
    openReview(Object.assign({}, data, {
      previewUrl: stagedPhotos.length ? stagedPhotos[0].url : null,
    }));
  } catch (err) {
    toast(err.message, true);
  } finally {
    loading(false);
  }
});

// Exact macros read off a label by the user \u2014 skips the model entirely,
// which is the most accurate path available.
$('btn-enter-values').addEventListener('click', () => {
  $('add-sheet').hidden = true;
  ['m-desc', 'm-cal', 'm-pro', 'm-car', 'm-fat'].forEach(id => { $(id).value = ''; });
  $('m-kind').value = 'auto';   // don't inherit the previous entry's meal type
  $('manual-sheet').hidden = false;
  document.body.style.overflow = 'hidden';
});

// -------------------------------------------------------------------- weight
$('btn-weight').addEventListener('click', async () => {
  const value = Number($('w-value').value);
  if (!value) { toast('Enter a weight first', true); return; }
  try {
    // Send the raw entry plus its unit; server stores canonical kg.
    const res = await api('weight', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value, unit: U.imperial ? 'lb' : 'kg',
        date: viewDate || undefined }),
    });
    $('w-value').value = '';
    dockPrefill = false;
    toast(res.adapted
      ? `Weight logged · target moved to ${fmt(res.target)} kcal (${res.delta > 0 ? '+' : ''}${res.delta})`
      : 'Weight logged');
    await refresh();
  } catch (err) { toast(err.message, true); }
});

// ------------------------------------------------------------- weight dock
// Prefill the last weigh-in so logging daily is one tap, and turn the Log
// button amber when it has been >7 days -- the adaptive target degrades fast
// without fresh data, so this nudge matters more than a badge would.
const STALE_WEIGHT_DAYS = 7;
let dockPrefill = false;      // true while the field shows an untouched prefill

function syncWeightDock() {
  const el = $('w-value');
  if (!el) return;
  el.placeholder = `weight (${U.wUnitLabel()})`;

  const lw = state?.last_weight;
  // Only prefill when the user hasn't started typing, so we never clobber an
  // entry mid-edit (refresh() runs on every save and view change).
  if (!dockPrefill && !el.value && lw && lw.days_ago >= 1) {
    el.value = U.weightInt(lw.kg);
    dockPrefill = true;
  } else if (dockPrefill && el.value && !lw) {
    el.value = ''; dockPrefill = false;
  }
  // Once today's weight exists there is nothing to prefill or nag about.
  if (lw && lw.days_ago === 0) {
    if (dockPrefill) { el.value = ''; dockPrefill = false; }
    el.placeholder = `${U.wUnitLabel()} logged today ✓`;
  }

  el.classList.toggle('prefilled', dockPrefill);

  const btn = $('btn-weight');
  const stale = !!lw && lw.days_ago > STALE_WEIGHT_DAYS;
  if (btn) {
    btn.classList.toggle('warn', stale);
    btn.title = stale
      ? `Last weigh-in ${lw.days_ago} days ago — your target adapts best with daily weights`
      : 'Log today’s weight';
  }
  const dock = $('weight-dock');
  if (dock) dock.classList.toggle('stale', stale);
}

// Editing away from the prefill hands control back to the user.
$('w-value').addEventListener('input', () => {
  dockPrefill = false;
  $('w-value').classList.remove('prefilled');
});
// Tapping a prefilled field selects it for quick overwrite rather than making
// the user delete the old number first.
$('w-value').addEventListener('focus', () => {
  if (dockPrefill) setTimeout(() => $('w-value').select(), 30);
});

// --------------------------------------------------------------------- trend
async function loadTrend() {
  try {
    const h = await api('history?days=90');
    renderTrendStats(h);
    drawWeightChart(h);
    drawIntakeChart(h);
    renderAccuracy(h.accuracy);
  } catch (err) { toast(err.message, true); }
}

function renderTrendStats(h) {
  const w = h.weights || [];
  const last = w[w.length - 1]?.kg;
  const first = w[0]?.kg;
  const change = last != null && first != null ? last - first : null;
  const days = h.intake?.length || 0;
  const avgIn = days ? Math.round(h.intake.reduce((a, d) => a + d.calories, 0) / days) : 0;
  const avgP = days ? Math.round(h.intake.reduce((a, d) => a + d.protein, 0) / days) : 0;

  $('trend-stats').innerHTML = `
    <div class="stat"><div class="stat-k">Current</div><div class="stat-v">${last != null ? U.weightInt(last) : '–'}</div><div class="stat-s">${U.wUnitLabel()} (latest)</div></div>
    <div class="stat"><div class="stat-k">Change</div><div class="stat-v">${change == null ? '–' : (change > 0 ? '+' : '') + (U.imperial ? U.kgToLb(change).toFixed(1) : change.toFixed(1))}</div><div class="stat-s">${U.wUnitLabel()} over window</div></div>
    <div class="stat"><div class="stat-k">Avg intake</div><div class="stat-v">${avgIn || '–'}</div><div class="stat-s">kcal on ${days} logged days</div></div>
    <div class="stat"><div class="stat-k">Avg protein</div><div class="stat-v">${avgP || '–'}</div><div class="stat-s">g per day</div></div>
    <div class="stat"><div class="stat-k">Target</div><div class="stat-v">${fmt(h.target)}</div><div class="stat-s">kcal/day</div></div>`;
}

function renderAccuracy(acc) {
  const rows = Object.entries(acc || {});
  const any = rows.some(([, v]) => v.n > 0);
  $('accuracy').innerHTML = !any
    ? `<p class="hint">No corrections yet. Edit an estimate when it's off and this shows how accurate each field is — plus the bias the calibrator is compensating for.</p>`
    : rows.map(([field, v]) => v.n === 0 ? '' : `
      <div class="acc-row">
        <span>${cap(field)} <span class="muted">(${v.n} edits)</span></span>
        <b>±${v.mape}% <span class="muted">bias ${v.bias > 0 ? '+' : ''}${v.bias}%</span></b>
      </div>`).join('');
}

// ---------------------------------------------------------------- tiny charts
function prepCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  // The authored height lives in the HTML attribute, but setting canvas.height
  // below *overwrites that same attribute* -- reading it again on the next call
  // returned an already-scaled number, doubling the backing store on every
  // redraw (340 -> 680 -> ... -> 5440px after five tab switches) and pushing all
  // drawing, including the empty-state text, far outside the visible box. Cache
  // the intended CSS height on the element instead of re-reading the attribute.
  if (!canvas._cssH) canvas._cssH = Number(canvas.getAttribute('height')) || 170;
  const cssH = canvas._cssH;
  const w = Math.max(200, rect.width || 320);
  canvas.width = w * dpr;
  canvas.height = cssH * dpr;
  canvas.style.height = cssH + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, cssH);
  // Callers destructure `hh`; returning it as `h` silently shadowed nothing and
  // left every y-coordinate NaN, so both charts drew off-canvas and the Trend
  // view looked like an empty box (broken since the initial commit -- the two
  // names were never reconciled). Keep one name on both sides of this boundary.
  return { ctx, w, hh: cssH };
}

function drawWeightChart(h) {
  // Plot in the active display unit; data stays canonical kg on the server.
  const toDisp = (kg) => U.imperial ? U.kgToLb(kg) : kg;
  const pts = (h.weights || []).map(r => ({ x: new Date(r.date).getTime(), y: toDisp(r.kg) }));
  const { ctx, w, hh } = prepCanvas($('chart-weight'));
  if (pts.length < 2) {
    ctx.fillStyle = '#8b93a5'; ctx.font = '13px sans-serif';
    ctx.fillText('Log at least two weights to see the trend', 12, hh / 2);
    $('weight-sub').textContent = '';
    return;
  }
  const pad = { l: 34, r: 8, t: 10, b: 20 };
  const ys = pts.map(p => p.y);
  const lo = Math.min(...ys) - 0.4, hi = Math.max(...ys) + 0.4;
  const x0 = pts[0].x, x1 = pts[pts.length - 1].x || x0 + 1;
  const sx = (x) => pad.l + (x - x0) / (x1 - x0 || 1) * (w - pad.l - pad.r);
  const sy = (y) => hh - pad.b - (y - lo) / (hi - lo || 1) * (hh - pad.t - pad.b);

  ctx.strokeStyle = '#2a2f3a'; ctx.lineWidth = 1; ctx.fillStyle = '#8b93a5';
  ctx.font = '10px sans-serif';
  for (let i = 0; i <= 3; i++) {
    const val = lo + (hi - lo) * i / 3, y = sy(val);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.fillText(U.imperial ? String(Math.round(val)) : val.toFixed(1), 2, y + 3);
  }

  ctx.beginPath();
  pts.forEach((p, i) => i ? ctx.lineTo(sx(p.x), sy(p.y)) : ctx.moveTo(sx(p.x), sy(p.y)));
  ctx.strokeStyle = '#4ade80'; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.stroke();

  ctx.fillStyle = '#4ade80';
  pts.forEach(p => { ctx.beginPath(); ctx.arc(sx(p.x), sy(p.y), 2.4, 0, 7); ctx.fill(); });

  const slope = h.trend_slope ?? fitSlope(pts);
  if (slope != null) {
    ctx.setLineDash([5, 4]); ctx.strokeStyle = '#fbbf24'; ctx.lineWidth = 1.6;
    ctx.beginPath();
    ctx.moveTo(sx(x0), sy(interceptAt(pts, x0)));
    ctx.lineTo(sx(x1), sy(interceptAt(pts, x1)));
    ctx.stroke(); ctx.setLineDash([]);
    // slope is already in display-units/week because pts were converted above.
    const dec = U.imperial ? 1 : 2;
    $('weight-sub').textContent = `${slope > 0 ? '+' : ''}${slope.toFixed(dec)} ${U.wUnitLabel()}/wk`;
  }
}

function fitSlope(pts) {
  const n = pts.length;
  const mx = pts.reduce((a, p) => a + p.x, 0) / n;
  const my = pts.reduce((a, p) => a + p.y, 0) / n;
  let sxx = 0, sxy = 0;
  pts.forEach(p => { sxx += (p.x - mx) ** 2; sxy += (p.x - mx) * (p.y - my); });
  if (!sxx) return null;
  return sxy / sxx * (7 * 86400 * 1000);
}
function interceptAt(pts, x) {
  const n = pts.length;
  const mx = pts.reduce((a, p) => a + p.x, 0) / n;
  const my = pts.reduce((a, p) => a + p.y, 0) / n;
  let sxx = 0, sxy = 0;
  pts.forEach(p => { sxx += (p.x - mx) ** 2; sxy += (p.x - mx) * (p.y - my); });
  const m = sxx ? sxy / sxx : 0;
  return my + m * (x - mx);
}

function drawIntakeChart(h) {
  const days = h.intake || [];
  const { ctx, w, hh } = prepCanvas($('chart-intake'));
  if (!days.length) {
    ctx.fillStyle = '#8b93a5'; ctx.font = '13px sans-serif';
    ctx.fillText('No logged days yet', 12, hh / 2);
    return;
  }
  const pad = { l: 34, r: 6, t: 10, b: 18 };
  const max = Math.max(h.target, ...days.map(d => d.calories)) * 1.1;
  const bw = Math.max(3, (w - pad.l - pad.r) / 30 - 3);

  ctx.fillStyle = '#8b93a5'; ctx.font = '10px sans-serif'; ctx.strokeStyle = '#2a2f3a';
  for (let i = 0; i <= 2; i++) {
    const val = max * i / 2, y = hh - pad.b - (val / max) * (hh - pad.t - pad.b);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.fillText(fmt(val), 1, y + 3);
  }

  const ty = hh - pad.b - (h.target / max) * (hh - pad.t - pad.b);
  ctx.setLineDash([4, 4]); ctx.strokeStyle = '#fbbf24'; ctx.lineWidth = 1.4;
  ctx.beginPath(); ctx.moveTo(pad.l, ty); ctx.lineTo(w - pad.r, ty); ctx.stroke();
  ctx.setLineDash([]);

  days.slice(-30).forEach((d, i) => {
    const x = pad.l + i * ((w - pad.l - pad.r) / 30);
    const bh = (d.calories / max) * (hh - pad.t - pad.b);
    ctx.fillStyle = d.calories > h.target * 1.05 ? '#f87171'
      : d.calories < h.target * 0.85 ? '#60a5fa' : '#4ade80';
    ctx.fillRect(x, hh - pad.b - bh, bw, bh);
  });
}

// ------------------------------------------------------------------ settings
function renderSettings() {
  const s = state.settings || {};
  const imp = U.imperial;
  $('s-units').value = imp ? 'imperial' : 'metric';
  if ($('s-timefmt')) $('s-timefmt').value = s.time_format === '24h' ? '24h' : '12h';
  $('s-sex').value = s.sex || 'male';
  $('s-age').value = s.age ?? 35;

  // Height: metric = one cm field; imperial = feet + inches fields.
  const cm = s.height_cm ?? 178;
  if (imp) {
    const totalIn = Math.round(cm / CM_PER_IN);
    $('s-height-ft').value = Math.floor(totalIn / 12);
    $('s-height-in').value = totalIn % 12;
  } else {
    $('s-height-cm').value = Math.round(cm);
  }
  $('height-metric-field').hidden = imp;
  $('height-imperial-field').hidden = !imp;

  $('s-activity').value = String(s.activity_factor ?? 1.4);

  // Weekly goal: stored kg/wk; show lb when imperial (with a finer step).
  const rateKg = s.weekly_rate_kg ?? -0.5;
  $('s-rate').value = imp ? +(U.kgToLb(rateKg)).toFixed(1) : rateKg;
  // Update only the label's text node — assigning .textContent on the <label>
  // would wipe out the nested <input id="s-rate"> it wraps.
  $('s-rate-label').firstChild.textContent = imp ? 'Weekly change (lb)' : 'Weekly change (kg)';
  $('s-rate').step = imp ? '0.2' : '0.05';
  $('s-rate').min = imp ? '-3.3' : '-1.5';
  $('s-rate').max = imp ? '2.2' : '1';

  $('s-protein').value = s.protein_g_per_kg ?? 1.8;
  $('s-fatpct').value = s.fat_calorie_pct ?? 0.25;
  $('s-target').value = s.calorie_target ?? '';
  $('s-autoadapt').checked = s.auto_adapt !== false;
  $('s-calibrate').checked = s.calibrate_estimates !== false;
  $('btn-unlock').textContent = s.calorie_target ? 'locked' : 'auto';
  syncWeightDock();

  renderProviders();
}

// Editable working copy of the provider chain. Kept separate from `state` so a
// half-finished edit cannot leak into other renders, and so Cancel/reload
// simply re-reads the saved list.
let provDraft = null;

function renderProviders() {
  const host = $('provider-list');
  if (!host) return;
  const list = provDraft || (state?.settings?.providers || []);
  if (provDraft) provDraft.forEach((p, i) => { p._status = (state?.providers || [])[i] || {}; });
  host.innerHTML = list.map((p, i) => {
    const st = p._status || {};
    const known = ['qwenplan', 'groq', 'openai', 'gemini', 'ollama'].includes(p.kind);
    return `<div class="prov-row" data-i="${i}">
      <div class="prov-top">
        <span class="prov-name">${esc(p.kind)}</span>
        <span class="badge ${st.configured ? 'on' : ''}">${
          p.enabled === false ? 'off' : st.configured ? 'ready' : known ? 'no key' : 'offline'}</span>
        <label class="prov-en"><input type="checkbox" data-toggle="${i}" ${p.enabled === false ? '' : 'checked'}>use</label>
        <button class="ghost tiny" data-up="${i}" ${i === 0 ? 'disabled' : ''} aria-label="Move up">↑</button>
        <button class="ghost tiny" data-down="${i}" ${i === list.length - 1 ? 'disabled' : ''} aria-label="Move down">↓</button>
        <button class="ghost tiny prov-del" data-del="${i}" ${known ? '' : 'disabled'} aria-label="Remove">✕</button>
      </div>
      <div class="prov-fields">
        <label class="fld">Model<input data-f="model" data-i="${i}" value="${esc(p.model || '')}"
          placeholder="${known ? 'model id' : '—'}" ${known ? '' : 'disabled'}></label>
        <label class="fld">Base URL<input data-f="base_url" data-i="${i}" value="${esc(p.base_url || '')}"
          placeholder="from server env" ${known ? '' : 'disabled'}></label>
      </div>
    </div>`;
  }).join('') || '<p class="hint">No providers — add one below.</p>';
}

$('provider-list').addEventListener('input', (e) => {
  const inp = e.target.closest('input[data-f]'); if (!inp || !provDraft) return;
  provDraft[+inp.dataset.i][inp.dataset.f] = inp.value.trim();
});
$('provider-list').addEventListener('click', (e) => {
  if (!provDraft) return;
  const mv = (from, to) => { const [x] = provDraft.splice(from, 1); provDraft.splice(to, 0, x); renderProviders(); };
  const up = e.target.closest('[data-up]'), dn = e.target.closest('[data-down]'), rm = e.target.closest('[data-del]');
  if (up) mv(+up.dataset.up, +up.dataset.up - 1);
  else if (dn) mv(+dn.dataset.down, +dn.dataset.down + 1);
  else if (rm && !rm.disabled) { provDraft.splice(+rm.dataset.del, 1); renderProviders(); }
});
$('provider-list').addEventListener('change', (e) => {
  const t = e.target.closest('[data-toggle]'); if (!t || !provDraft) return;
  provDraft[+t.dataset.toggle].enabled = t.checked;
  renderProviders();
});

// Editing starts on demand: the list is read-only until "Edit" is pressed so an
// accidental tap can't reorder the chain that decides which model answers.
$('btn-edit-providers')?.addEventListener('click', () => {
  // Providers are stored in the SHARED settings blob, not per person: changing
  // the chain here changes it for every family member. Say so once, before the
  // first edit, because "my estimates stopped working" is otherwise baffling.
  if (!provDraft) toast('Providers are shared by everyone using this server');
  provDraft = JSON.parse(JSON.stringify(state.settings.providers || []));
  renderProviders();
  $('btn-save-providers').hidden = false;
  $('prov-add-wrap').hidden = false;
});

$('prov-add-kind')?.addEventListener('change', (e) => {
  const kind = e.target.value; if (!kind || !provDraft) return;
  if (provDraft.some(p => p.kind === kind)) { toast(kind + ' is already in the list', true); return; }
  provDraft.push({ kind, model: '', base_url: '', enabled: true });
  e.target.value = '';
  renderProviders();
});

$('btn-save-providers')?.addEventListener('click', async () => {
  if (!provDraft) return;
  try {
    const clean = provDraft.map(p => ({ kind: p.kind, model: p.model || undefined,
                                        base_url: p.base_url || undefined,
                                        enabled: p.enabled !== false }));
    await api('settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ providers: clean }) });
    provDraft = null;
    $('btn-save-providers').hidden = true;
    $('prov-add-wrap').hidden = true;
    renderProviders();   // back to read-only view of the saved list
    toast('Providers saved');
    await refresh();
  } catch (err) { toast(err.message, true); }
});

$('btn-save').addEventListener('click', async () => {
  const imp = U.imperial;
  // Convert display-unit inputs back to canonical metric before sending.
  const height_cm = imp
    ? U.heightToCm($('s-height-ft').value, $('s-height-in').value)
    : Number($('s-height-cm').value);
  const rateShown = Number($('s-rate').value);
  const payload = {
    units: $('s-units').value,
    sex: $('s-sex').value,
    age: Number($('s-age').value),
    height_cm: Math.round(height_cm),
    activity_factor: Number($('s-activity').value),
    weekly_rate_kg: +(imp ? U.lbToKg(rateShown) : rateShown).toFixed(3),
    protein_g_per_kg: Number($('s-protein').value),
    fat_calorie_pct: Number($('s-fatpct').value),
    auto_adapt: $('s-autoadapt').checked,
    calibrate_estimates: $('s-calibrate').checked,
    time_format: $('s-timefmt') ? $('s-timefmt').value : undefined,
  };
  const tgt = Number($('s-target').value);
  if (tgt) payload.calorie_target = tgt;
  try {
    await api('settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    toast('Saved');
    await refresh();
    renderSettings();   // re-render so fields reflect any unit change immediately
  } catch (err) { toast(err.message, true); }
});

// Switching the units dropdown re-renders the form live (before saving) so the
// user sees ft/in vs cm and lb vs kg without a round-trip.
$('s-units').addEventListener('change', () => {
  state.settings.units = $('s-units').value;
  renderSettings();
});

// "auto"/"locked" button toggles whether the calorie target is user-pinned or
// left for the adaptive engine to manage. One PUT, no round-trips.
$('btn-unlock').addEventListener('click', async () => {
  const s = state.settings || {};
  const currentlyLocked = !!s.calorie_target;
  try {
    await api('settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      // Unlock -> clear the pin (server recomputes); lock -> pin current value.
      body: JSON.stringify({ calorie_target: currentlyLocked ? null : (s.calorie_target || 2200) }),
    });
    toast(currentlyLocked ? 'Target set to auto' : 'Target locked');
    await refresh();
    if ($('view-settings').classList.contains('active')) renderSettings();
  } catch (err) { toast(err.message, true); }
});

$('btn-test').addEventListener('click', async () => {
  const out = $('test-out');
  out.hidden = false;
  out.textContent = 'Testing…';
  try {
    const res = await api('test-provider', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    out.textContent = res.results.map(r =>
      r.ok ? `✓ ${r.kind} (${r.model}) — ${r.ms}ms`
           : `✗ ${r.kind} (${r.model}) — ${r.error}`).join('\n');
  } catch (err) { out.textContent = err.message; }
});

// -------------------------------------------------------------------- refresh
async function refresh() {
  try {
    state = await api('state' + (viewDate ? '?date=' + encodeURIComponent(viewDate) : ''));
    renderToday(state);
    syncWeightDock();
    if ($('view-settings').classList.contains('active')) renderSettings();
  } catch (err) {
    toast(err.message, true);
  }
}

window.addEventListener('resize', () => {
  if ($('view-trend').classList.contains('active')) loadTrend();
});

refresh();

// One-shot SW install: register only when nothing exists yet. A page reload
// re-registers the same script, which can leave Chrome's headless SW with a
// wedged fetch dispatcher (observed: every POST hangs forever).
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.getRegistration().then((reg) => {
    if (!reg) { navigator.serviceWorker.register('/sw.js').catch(() => {}); return; }
    // If an update is pending or the controlling worker predates this build,
    // drop it and register fresh — a half-dead worker wedges all fetches.
    if (reg.waiting || reg.installing) {
      reg.unregister().then(() => navigator.serviceWorker.register('/sw.js')).catch(() => {});
    }
  });
}
