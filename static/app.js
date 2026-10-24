/* MacroLog client. No build step, no dependencies. */
'use strict';

const $ = (id) => document.getElementById(id);
const CIRC = 2 * Math.PI * 86;
let state = null;
let pendingPhoto = null;   // { temp_photo, dataUrl }

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
async function api(path, opts = {}) {
  const res = await fetch('/api/' + path, opts);
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

function renderToday(s) {
  renderUserChip(s);
  const c = s.consumed, t = s.targets;
  const exercise = s.exercise || 0;
  const remaining = t.calories - c.calories + exercise;

  $('kcal-left').textContent = fmt(Math.abs(remaining));
  $('ring-label').textContent = remaining >= 0 ? 'kcal left' : 'kcal over';

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
  $('macro-row').innerHTML = macros.map(([, label, got, want, color]) => `
    <div class="macro">
      <div class="macro-name">${label}</div>
      <div class="macro-val">${fmt(got)}<span>/${fmt(want)}g</span></div>
      <div class="bar"><i style="width:${Math.min(100, want ? got / want * 100 : 0)}%;background:${color}"></i></div>
    </div>`).join('');

  renderStatus(s);
  renderMeals(s.meals);
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

function renderMeals(meals) {
  $('meals-empty').hidden = meals.length > 0;
  $('meal-list').innerHTML = meals.map(m => {
    const time = new Date(m.ts * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    const thumb = m.photo
      ? `<img class="meal-thumb" src="/api/photo/${encodeURIComponent(m.photo)}" alt="" loading="lazy">`
      : `<div class="meal-thumb"></div>`;
    return `<li class="meal" data-id="${m.id}">
      ${thumb}
      <div class="meal-info">
        <div class="meal-desc">${esc(m.description || m.kind || 'Meal')}</div>
        <div class="meal-meta"><span class="conf-dot" style="background:${confColor(m.confidence)}"></span>${time} · ${cap(m.kind)}${m.source === 'manual' ? ' · manual' : ''}</div>
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
    </li>`;
  }).join('');
}

const esc = (s) => String(s || '').replace(/[&<>"]/g, ch =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
const cap = (s) => s ? s[0].toUpperCase() + s.slice(1) : '';

// ------------------------------------------------------------- meal actions
$('meal-list').addEventListener('click', async (e) => {
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
        protein: meal.protein, carbs: meal.carbs, fat: meal.fat, items: [],
      },
      provider: meal.provider, model: meal.model, editingId: id,
      photoUrl: meal.photo ? `/api/photo/${encodeURIComponent(meal.photo)}` : null,
    });
  }
});

// ------------------------------------------------------------------- capture
$('btn-photo').addEventListener('click', () => $('file-input').click());
$('file-input').addEventListener('change', async (e) => {
  const file = e.target.files?.[0];
  if (!file) return;
  if (file.size > 12 * 1024 * 1024) { toast('Image too large (max 12MB)', true); return; }

  const dataUrl = await readAsDataURL(file);
  loading(true, 'Analyzing photo…');
  try {
    const form = new FormData();
    form.append('photo', file, file.name || 'meal.jpg');
    const res = await fetch('/api/estimate', { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Estimate failed');
    pendingPhoto = data.temp_photo;
    openReview({ ...data, previewUrl: dataUrl });
  } catch (err) {
    toast(err.message, true);
  } finally {
    loading(false);
    e.target.value = '';
  }
});

function readAsDataURL(file) {
  return new Promise((resolve) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.readAsDataURL(file);
  });
}

// ------------------------------------------------------------ review sheet
function openReview({ estimate, provider, model, calibrated, previewUrl, photoUrl, editingId }) {
  const e = estimate || {};
  $('review-sheet').dataset.editing = editingId || '';
  $('r-desc').value = e.description || '';
  $('r-cal').value = Math.round(e.calories || 0);
  $('r-pro').value = Math.round(e.protein || 0);
  $('r-car').value = Math.round(e.carbs || 0);
  $('r-fat').value = Math.round(e.fat || 0);

  const img = $('review-img');
  const src = previewUrl || photoUrl;
  img.src = src || '';
  img.style.display = src ? 'block' : 'none';
  const pa = document.querySelector('.photo-actions');
  if (pa) pa.style.display = editingId ? 'none' : 'block';

  $('r-items').innerHTML = (e.items || []).map(i =>
    `<div><span>${esc(i.name)}${i.grams ? ` · ${fmt(i.grams)}g` : ''}</span><span class="num">${fmt(i.calories)} kcal</span></div>`
  ).join('');

  const conf = Math.round((e.confidence ?? 0.5) * 100);
  $('r-prov').innerHTML = `${cap(provider || 'manual')}${model ? ' · ' + esc(model) : ''} · confidence <b style="color:${confColor(e.confidence ?? .5)}">${conf}%</b>${calibrated ? ' · calibrated by your history' : ''}`;

  $('review-sheet').hidden = false;
  document.body.style.overflow = 'hidden';
}

function closeSheets() {
  $('review-sheet').hidden = true;
  $('manual-sheet').hidden = true;
  document.body.style.overflow = '';
}

$('btn-cancel-review').addEventListener('click', closeSheets);
$('review-sheet').addEventListener('click', (e) => { if (e.target.id === 'review-sheet') closeSheets(); });

$('btn-save-review').addEventListener('click', async () => {
  const payload = {
    description: $('r-desc').value.trim(),
    calories: Number($('r-cal').value) || 0,
    protein: Number($('r-pro').value) || 0,
    carbs: Number($('r-car').value) || 0,
    fat: Number($('r-fat').value) || 0,
    kind: $('r-kind').value,
    source: 'photo',
  };
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
        body: JSON.stringify({ ...payload, photo_path: pendingPhoto }),
      });
      pendingPhoto = null;
      toast('Logged');
    }
    closeSheets();
    await refresh();
  } catch (err) { toast(err.message, true); }
});

// -------------------------------------------------------------- manual entry
$('btn-manual').addEventListener('click', () => {
  ['m-desc', 'm-cal', 'm-pro', 'm-car', 'm-fat'].forEach(id => { $(id).value = ''; });
  $('manual-sheet').hidden = false;
  document.body.style.overflow = 'hidden';
});
$('btn-cancel-manual').addEventListener('click', closeSheets);
$('manual-sheet').addEventListener('click', (e) => { if (e.target.id === 'manual-sheet') closeSheets(); });

$('btn-save-manual').addEventListener('click', async () => {
  const payload = {
    description: $('m-desc').value.trim(),
    calories: Number($('m-cal').value) || 0,
    protein: Number($('m-pro').value) || 0,
    carbs: Number($('m-car').value) || 0,
    fat: Number($('m-fat').value) || 0,
    kind: $('m-kind').value,
    source: 'manual',
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

// -------------------------------------------------------------------- weight
$('btn-weight').addEventListener('click', async () => {
  const value = Number($('w-value').value);
  if (!value) { toast('Enter a weight first', true); return; }
  try {
    // Send the raw entry plus its unit; server stores canonical kg.
    const res = await api('weight', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value, unit: U.imperial ? 'lb' : 'kg' }),
    });
    $('w-value').value = '';
    toast(res.adapted
      ? `Weight logged · target moved to ${fmt(res.target)} kcal (${res.delta > 0 ? '+' : ''}${res.delta})`
      : 'Weight logged');
    await refresh();
  } catch (err) { toast(err.message, true); }
});

// Keep the weight dock placeholder in sync with the active unit.
function syncWeightDock() {
  const el = $('w-value');
  if (el) el.placeholder = `weight (${U.wUnitLabel()})`;
}

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
  const cssH = Number(canvas.getAttribute('height')) || 170;
  const w = Math.max(200, rect.width || 320);
  canvas.width = w * dpr;
  canvas.height = cssH * dpr;
  canvas.style.height = cssH + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, cssH);
  return { ctx, w, h: cssH };
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
  $('s-rate-label').textContent = imp ? 'Weekly change (lb)' : 'Weekly change (kg)';
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

  $('provider-list').innerHTML = (s.providers || []).map((p, i) => {
    const st = (state.providers || [])[i] || {};
    return `<div class="prov">
      <div><div class="prov-name">${esc(p.kind)}</div><div class="prov-model">${esc(p.model || '')}</div></div>
      <span class="badge ${st.configured ? 'on' : ''}">${st.configured ? 'ready' : 'no key'}</span>
    </div>`;
  }).join('');
}

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
    state = await api('state');
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

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
