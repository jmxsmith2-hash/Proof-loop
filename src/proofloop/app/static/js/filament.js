// Filament: spools, AMS trays, usage, weigh-ins, Spoolman, backup.
// Part of loop.html; the files load in order and share their top-level names.

// ---------- filament ----------
let FIL = null, EDIT = null, PLATEFIL = null;
const G = (v, d = 0) => v == null ? '—' : (+v).toFixed(d) + ' g';
const ago = (ts) => { if (!ts) return null; const d = (Date.now() / 1000 - ts) / 86400; return d < 1 ? 'today' : d < 2 ? 'yesterday' : Math.floor(d) + ' days ago'; };
const swatch = (c, big) => `<span class="sw${big ? ' big' : ''}" style="background:${esc(c || 'transparent')}"></span>`;
const withToken = (u) => token ? u + (u.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token) : u;
const statusChip = (s) => ({empty: '<span class="chip bad">used up</span>', low: '<span class="chip warn">low</span>', unknown: '<span class="chip" title="Weigh it or type what\'s left">amount unknown</span>', archived: '<span class="chip">archived</span>'}[s] || '');
function spoolOptions(material, selected, blank = '— none —') {
  const sp = (FIL ? FIL.spools : STATE.spools || []).filter(s => !s.archived);
  const same = sp.filter(s => !material || s.material === (material || '').toUpperCase()), other = sp.filter(s => !same.includes(s));
  const o = (s) => `<option value="${esc(s.id)}" ${s.id === selected ? 'selected' : ''}>${esc(s.label)}${s.remaining_g != null ? ' · ' + Math.round(s.remaining_g) + ' g' : ''}</option>`;
  return `<option value="">${esc(blank)}</option>` + same.map(o).join('') + (other.length && material ? `<optgroup label="Other materials">${other.map(o).join('')}</optgroup>` : other.map(o).join(''));
}

async function loadFil() {
  try { FIL = await json('/api/filament'); STATE.spools = FIL.spools; renderFil(); loadUsage(); }
  catch (e) { toast(e.message); }
}
function renderFil() {
  const s = FIL.summary, cur = STATE.costs?.currency || '';
  $('#filKpis').innerHTML = [['Spools', s.spools], ['Filament on hand', s.on_hand_g >= 1000 ? (s.on_hand_g / 1000).toFixed(2) + ' kg' : Math.round(s.on_hand_g) + ' g'],
    ['Worth', s.value ? cur + s.value.toFixed(2) : '—'], ['Used today', G(s.used_g['1'])], ['Used 7 days', G(s.used_g['7'])],
    ['Used 30 days', G(s.used_g['30']) + (s.used_cost['30'] ? ` · ${cur}${s.used_cost['30'].toFixed(2)}` : '')]]
    .map(([k, v]) => `<div class="kpi"><small>${esc(k)}</small><b>${esc(v)}</b></div>`).join('');
  const alerts = [];
  if (s.unmatched) alerts.push(`<div class="banner warn"><span class="ic warning">!</span><div><b>${s.unmatched} loop${s.unmatched > 1 ? 's' : ''} used filament that isn't matched to a spool.</b> Pick the spool below so the counts stay right.</div></div>`);
  if (s.unknown_remaining) alerts.push(`<div class="banner info"><span class="ic info">i</span><div>${s.unknown_remaining} spool${s.unknown_remaining > 1 ? 's have' : ' has'} no known amount left. Weigh ${s.unknown_remaining > 1 ? 'them' : 'it'} (enter the empty-spool weight once) or type what's left, so ProofLoop can warn you before a run comes up short.</div></div>`);
  $('#filAlerts').innerHTML = alerts.join('');
  // lists for the editor
  const mats = new Set([...FIL.materials, ...(PLATE?.filaments || []), ...FIL.printers.flatMap(p => p.trays.filter(t => !t.empty).map(t => t.type))]);
  $('#matList').innerHTML = [...mats].map(m => `<option value="${esc(m)}">`).join('');
  const presets = new Set([...FIL.spools.map(x => x.preset).filter(Boolean), ...Object.values(PLATE?.presets || {}).map(p => p.preset).filter(Boolean)]);
  $('#presetList').innerHTML = [...presets].map(m => `<option value="${esc(m)}">`).join('');
  const sel = $('#filMat'), prev = sel.value;
  sel.innerHTML = '<option value="">All materials</option>' + FIL.materials.map(m => `<option>${esc(m)}</option>`).join('');
  sel.value = [...sel.options].some(o => o.value === prev) ? prev : '';
  renderAms(); renderSpoolList(); renderUnmatched(); renderShopping(); fillFilSettings();
  $('#csvSpools').href = withToken('/api/filament/export.csv?what=spools'); $('#csvUsage').href = withToken('/api/filament/export.csv?what=usage'); $('#backupBtn').href = withToken('/api/backup');
  if (!EDIT) resetEditor();
}

function renderAms() {
  const ps = FIL.printers;
  if (!ps.length) { $('#amsOut').innerHTML = '<p class="hint">Add a printer in <a href="#" data-goto="monitor">Monitor</a> and its AMS trays show up here: type, colour, how full the AMS thinks each spool is, humidity, and which of your spools is in which slot. Bambu spools link themselves from their RFID tag.</p>'; return; }
  const known = new Set(FIL.spools.map(s => s.tag).filter(Boolean));
  $('#amsOut').innerHTML = ps.map(p => {
    const units = p.units.map(u => `<span class="chip">${esc(u.name)}${u.humidity_pct != null ? ` · ${u.humidity_pct.toFixed(0)}% RH` : ''}${u.temp != null ? ` · ${u.temp.toFixed(0)} °C` : ''}${u.drying_min_left ? ` · drying, ${fmtDur(u.drying_min_left * 60)} left` : ''}</span>`).join(' ');
    const trays = p.trays.length ? p.trays.map(t => {
      if (t.empty) return `<div class="tray empty"><b>${esc(t.label)}</b> <span class="hint">empty</span></div>`;
      const sp = FIL.spools.find(s => s.printer === p.name && s.tray === t.tray);
      const feeding = p.tray_now === t.tray;
      return `<div class="tray${feeding ? ' feeding' : ''}"><i style="background:${esc(t.color || 'var(--soft)')}"></i>
        <div class="spread"><b>${esc(t.label)}</b>${feeding ? '<span class="chip info">feeding</span>' : ''}</div>
        <div>${esc(t.sub_brand || t.type)} <span class="hint">${esc(t.color)}</span></div>
        <div class="hint">AMS says ${t.remain_pct == null ? 'unknown' : t.remain_pct + '%'}${t.nozzle_temp_min ? ` · ${t.nozzle_temp_min}–${t.nozzle_temp_max} °C` : ''}</div>
        ${sp ? `<div style="margin-top:4px"><b>${esc(sp.label)}</b> <span class="num">${G(sp.remaining_g)}</span></div>` : ''}
        <select data-link="${esc(p.name)}" data-tray="${t.tray}" aria-label="Spool in ${esc(t.label)}">${spoolOptions(t.type, sp?.id, sp ? '— unlink —' : 'Link a spool…')}</select>
        ${!sp ? `<button class="linkbtn" data-addtray="${esc(p.name)}|${t.tray}">+ add as a new spool${t.tag && !known.has(t.tag) ? ' (RFID)' : ''}</button>` : ''}</div>`;
    }).join('') : '<p class="hint">No AMS data yet. It arrives with the printer\'s next status report.</p>';
    return `<div class="ams-printer"><div class="spread"><h3 style="margin:6px 0">${esc(p.name)}</h3><span>${p.online ? '' : '<span class="chip">offline</span>'} ${units}</span></div><div class="trays">${trays}</div></div>`;
  }).join('');
}

function renderSpoolList() {
  const q = $('#filSearch').value.trim().toLowerCase(), mat = $('#filMat').value, all = $('#filArchived').checked, sort = $('#filSort').value;
  let list = FIL.spools.filter(s => (all || !['archived', 'empty'].includes(s.status)) && (!mat || s.material === mat) &&
    (!q || [s.label, s.brand, s.color, s.location, s.preset, s.notes, s.material].join(' ').toLowerCase().includes(q)));
  const key = {remaining: s => s.remaining_g ?? 1e9, recent: s => -(s.last_used || 0), name: s => s.label.toLowerCase(), added: s => -s.added}[sort];
  list = list.sort((a, b) => key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0);
  if (!FIL.spools.length) { $('#spoolList').innerHTML = '<p class="hint">No spools yet. Add the ones on your shelf with <b>+ Add spool</b>, or link the ones in your AMS above. With spools added, ProofLoop counts what every loop uses, warns you before a run comes up short, and prices each part from what you really paid.</p>'; return; }
  $('#spoolList').innerHTML = list.map(s => {
    const pct = s.remaining_pct, where = s.printer ? `in <b>${esc(s.printer)}</b> ${esc(trayLabel(s.tray))}` : esc(s.location || '');
    const meta = [where, s.opened ? 'opened ' + ago(s.opened) : (s.remaining_g != null && s.net_g && s.remaining_g >= s.net_g ? 'sealed' : ''), s.dried ? 'dried ' + ago(s.dried) : '',
      s.forecast.days_left != null ? `about ${s.forecast.days_left < 1 ? 'a day' : Math.round(s.forecast.days_left) + ' days'} left at your last-14-day rate` : '',
      s.tag ? 'RFID' : '', s.spoolman_id != null ? 'Spoolman #' + s.spoolman_id : ''].filter(Boolean).join(' · ');
    return `<div class="spool" data-sid="${esc(s.id)}">
      <div class="spool-top">${swatch(s.color, true)}<div style="flex:1;min-width:0"><div class="spread"><b>${esc(s.label)}</b><span>${statusChip(s.status)}</span></div>
        <div class="hint">${esc([s.brand, s.material, s.preset].filter(Boolean).join(' · '))}${s.per_kg != null ? ` · ${esc(money(s.per_kg))}/kg` : ''}</div></div></div>
      <div class="meter" title="${pct == null ? '' : pct + '%'}"><i style="width:${Math.max(0, Math.min(100, pct ?? 0))}%;background:${esc(s.color || 'var(--accent)')}"></i></div>
      <div class="spread"><span class="num"><b>${G(s.remaining_g)}</b>${s.net_g ? ` of ${G(s.net_g)}` : ''}${pct != null ? ` · ${pct}%` : ''}${s.value != null ? ` · worth ${esc(money(s.value))}` : ''}</span>
        <span class="row spool-acts"><button class="linkbtn" data-sa="weigh">Weigh</button><button class="linkbtn" data-sa="set">Set left</button><button class="linkbtn" data-sa="use">Used…</button><button class="linkbtn" data-sa="dried">Dried today</button><button class="linkbtn" data-sa="edit">Edit</button><button class="linkbtn" data-sa="again">Buy again</button><button class="linkbtn" data-sa="label">Label</button><button class="linkbtn" data-sa="history">History</button></span></div>
      ${meta ? `<div class="hint">${meta}</div>` : ''}
      <div class="spool-form hidden"></div></div>`;
  }).join('') || '<p class="hint">No spools match.</p>';
}
const trayLabel = (t) => { if (t == null) return ''; if (t === 254) return 'Ext'; if (t === 255) return 'Ext 2'; if (t >= 128) return 'HT-' + String.fromCharCode(65 + t - 128); if (t >= 24 && t <= 27) return 'Lite ' + (t - 23); return String.fromCharCode(65 + Math.floor(t / 4)) + (t % 4 + 1); };

$('#spoolList').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-sa]'); if (!b) return;
  const box = b.closest('.spool'), sid = box.dataset.sid, sp = FIL.spools.find(s => s.id === sid), form = box.querySelector('.spool-form');
  const act = b.dataset.sa;
  const inline = (label, ph, hint, go) => {
    form.classList.remove('hidden');
    form.innerHTML = `<div class="row" style="margin-top:8px"><label class="inline"><span class="hint">${esc(label)}</span><input type="number" min="0" step="0.1" placeholder="${esc(ph)}"></label><button class="btn primary">Save</button><button class="btn" data-x>Cancel</button></div>${hint ? `<p class="hint">${hint}</p>` : ''}`;
    const inp = form.querySelector('input'); inp.focus();
    form.querySelector('[data-x]').onclick = () => form.classList.add('hidden');
    const run = async () => { const v = parseFloat(inp.value); if (!(v >= 0)) { toast('Enter a number'); return; } try { await go(v); await loadFil(); } catch (err) { toast(err.message); } };
    form.querySelector('.primary').onclick = run; inp.onkeydown = (ev) => { if (ev.key === 'Enter') run(); };
  };
  try {
    if (act === 'weigh') {
      if (sp.tare_g == null) { inline('Empty spool weighs', 'g', 'Weigh an empty spool of the same kind once. ProofLoop takes it off every weigh-in after that.', async (v) => { await send(`/api/filament/spools/${sid}`, {tare_g: v}, 'PUT'); toast('Saved. Now weigh the spool.'); }); return; }
      inline('Spool on the scale', 'g', `Filament left = scale reading − ${sp.tare_g} g empty spool.`, async (v) => { const r = await send(`/api/filament/spools/${sid}/weigh`, {gross_g: v}); toast(`${Math.round(r.remaining_g)} g left on ${r.label}`); });
    }
    if (act === 'set') inline('Filament left', 'g', '', async (v) => send(`/api/filament/spools/${sid}/remaining`, {grams: v}));
    if (act === 'use') inline('Used outside ProofLoop', 'g', 'For prints the monitor didn\'t count, test prints, or filament you threw away.', async (v) => send(`/api/filament/spools/${sid}/use`, {grams: v}));
    if (act === 'dried') { await send(`/api/filament/spools/${sid}/dried`, {}); toast('Marked as dried today'); await loadFil(); }
    if (act === 'edit') { editSpool(sp); }
    if (act === 'again') { editSpool({...sp, id: '', remaining_g: null, opened: null, dried: null, tag: '', printer: '', tray: null, spoolman_id: null, archived: false}, true); }
    if (act === 'label') printLabel(sp);
    if (act === 'history') {
      const u = await json(`/api/filament/usage?days=366&spool=${encodeURIComponent(sid)}`);
      form.classList.remove('hidden');
      form.innerHTML = `<div class="tw" style="margin-top:8px"><table>${usageRows(u.rows, false) || '<tr><td class="hint">Nothing recorded yet.</td></tr>'}</table></div><button class="linkbtn" data-x>close</button>`;
      form.querySelector('[data-x]').onclick = () => form.classList.add('hidden');
    }
  } catch (err) { toast(err.message); }
});
['#filSearch', '#filMat', '#filSort', '#filArchived'].forEach(id => $(id).addEventListener('input', () => FIL && renderSpoolList()));

function printLabel(s) {
  const w = window.open('', '_blank', 'width=420,height=320'); if (!w) { toast('Allow pop-ups to print labels'); return; }
  w.document.write(`<!doctype html><title>${esc(s.label)}</title><style>body{font:14px system-ui;margin:16px}.l{border:2px solid #000;border-radius:10px;padding:12px;width:300px}.c{width:40px;height:40px;border-radius:6px;border:1px solid #000;float:right}b{font-size:18px}small{color:#444}</style>
    <div class="l"><div class="c" style="background:${esc(s.color || '#fff')}"></div><b>${esc(s.label)}</b><br>${esc([s.brand, s.material].filter(Boolean).join(' · '))}<br>
    <small>${esc(s.color)}${s.net_g ? ` · ${s.net_g} g new` : ''}${s.tare_g != null ? ` · empty spool ${s.tare_g} g` : ''}</small><br>
    <small>${s.drying_temp ? `Dry ${s.drying_temp} °C${s.drying_hours ? ' for ' + s.drying_hours + ' h' : ''} · ` : ''}added ${new Date(s.added * 1000).toLocaleDateString()}</small><br><small>ProofLoop spool ${esc(s.id)}</small></div>
    <script>setTimeout(()=>print(),200)<\/script>`);
  w.document.close();
}

// ---- spool editor ----
const F = () => $('#spoolForm');
const NUMF = ['price', 'net_g', 'tare_g', 'remaining_g', 'low_g', 'diameter', 'density', 'drying_temp', 'drying_hours'];
function resetEditor() { EDIT = null; editSpool(null); }
function editSpool(sp, asNew) {
  const f = F(); f.reset();
  EDIT = sp && sp.id && !asNew ? sp.id : null;
  const v = sp || {};
  ['material', 'color', 'name', 'brand', 'location', 'preset', 'filament_id', 'tag', 'lot', 'notes'].forEach(k => f[k].value = v[k] || '');
  NUMF.forEach(k => f[k].value = v[k] ?? '');
  f.archived.checked = !!v.archived;
  $('#edColorPick').value = /^#[0-9A-F]{6}$/i.test(v.color || '') ? v.color.toLowerCase() : '#ffffff';
  $('#edTitle').textContent = EDIT ? 'Edit spool' : asNew ? 'Buy another' : 'Add a spool';
  $('#edSave').textContent = EDIT ? 'Save' : 'Add spool';
  $('#edDelete').classList.toggle('hidden', !EDIT);
  $('#edSealedRow').classList.toggle('hidden', !!EDIT); $('#edCopiesWrap').classList.toggle('hidden', !!EDIT);
  $('#edRemainWrap').classList.toggle('hidden', !EDIT && f.sealed.checked);
  $('#edChip').innerHTML = EDIT ? `<span class="chip">${esc(sp.id)}</span>` : '';
  const pre = [];
  if (PLATE) Object.entries(PLATE.presets || {}).forEach(([sid, p]) => { const slot = (PLATE.filament_slots || []).find(x => x[0] === sid); pre.push(`<button type="button" class="btn" data-prefill="${esc(sid)}">${swatch(slot?.[2])}From this plate: ${esc(p.preset || slot?.[1] || 'slot ' + sid)}</button>`); });
  $('#edPrefill').innerHTML = !EDIT && pre.length ? pre.join('') : '';
  if (sp && (sp.id || asNew)) $('#spoolEditor').scrollIntoView({behavior: 'smooth', block: 'start'});
}
F().sealed.addEventListener('change', () => $('#edRemainWrap').classList.toggle('hidden', !EDIT && F().sealed.checked));
$('#edColorPick').addEventListener('input', (e) => { F().color.value = e.target.value.toUpperCase(); });
F().color.addEventListener('input', (e) => { if (/^#?[0-9A-F]{6}$/i.test(e.target.value.trim())) $('#edColorPick').value = '#' + e.target.value.trim().replace('#', '').toLowerCase(); });
$('#edPrefill').addEventListener('click', (e) => {
  const b = e.target.closest('[data-prefill]'); if (!b) return;
  const sid = b.dataset.prefill, p = PLATE.presets[sid] || {}, slot = (PLATE.filament_slots || []).find(x => x[0] === sid) || [];
  const f = F(); f.material.value = p.type || slot[1] || ''; f.color.value = slot[2] || ''; $('#edColorPick').value = (slot[2] || '#ffffff').toLowerCase();
  f.preset.value = p.preset || ''; f.brand.value = p.vendor || ''; f.filament_id.value = p.filament_id || ''; f.diameter.value = p.diameter ?? ''; f.density.value = p.density ?? '';
  f.drying_temp.value = p.ams_drying_temp ?? ''; f.drying_hours.value = p.ams_drying_hours ?? '';
  if (!f.name.value) f.name.value = (p.preset || '').split('@')[0].trim();
  toast(p.slicer_cost_per_kg ? `Filled from your slicer preset. Your slicer prices it at ${p.slicer_cost_per_kg}/kg; enter what you paid.` : 'Filled from your slicer preset.');
});
$('#amsOut').addEventListener('click', (e) => {
  const b = e.target.closest('[data-addtray]'); if (!b) return;
  const [pn, tid] = b.dataset.addtray.split('|'), p = FIL.printers.find(x => x.name === pn), t = p.trays.find(x => x.tray === +tid);
  editSpool({material: t.type, color: t.color, name: t.sub_brand || '', filament_id: t.filament_id, tag: t.tag, drying_temp: t.drying_temp, drying_hours: t.drying_hours}, true);
  PENDING_LINK = [pn, +tid];
  toast(t.tag ? 'Read from the spool\'s RFID tag. Add the weight and price from its label.' : 'Filled from the AMS. Add the weight and price.');
});
let PENDING_LINK = null;
$('#amsOut').addEventListener('change', async (e) => {
  const s = e.target.closest('[data-link]'); if (!s) return;
  try { await send('/api/filament/link', {printer: s.dataset.link, tray: +s.dataset.tray, spool: s.value || null}); await loadFil(); } catch (err) { toast(err.message); }
});
$('#edCancel').onclick = () => { PENDING_LINK = null; resetEditor(); };
$('#filAdd').onclick = () => { PENDING_LINK = null; resetEditor(); $('#spoolEditor').scrollIntoView({behavior: 'smooth'}); F().material.focus(); };
$('#edDelete').onclick = async () => { if (!EDIT || !confirm('Delete this spool? Its usage history stays, unassigned.')) return;
  try { await json(`/api/filament/spools/${EDIT}`, {method: 'DELETE'}); resetEditor(); await loadFil(); toast('Spool deleted'); } catch (err) { toast(err.message); } };
F().onsubmit = async (e) => {
  e.preventDefault(); const f = F(), body = {};
  ['material', 'color', 'name', 'brand', 'location', 'preset', 'filament_id', 'tag', 'lot', 'notes'].forEach(k => body[k] = f[k].value.trim());
  NUMF.forEach(k => body[k] = f[k].value === '' ? null : +f[k].value);
  body.archived = f.archived.checked;
  try {
    if (EDIT) { await send(`/api/filament/spools/${EDIT}`, body, 'PUT'); toast('Saved'); }
    else {
      body.sealed = f.sealed.checked; body.copies = +f.copies.value || 1;
      if (body.sealed && !body.net_g) { toast('Enter how much filament a new spool holds (it\'s on the label)'); f.net_g.focus(); return; }
      if (body.sealed) body.remaining_g = null;
      const made = await send('/api/filament/spools', body);
      if (PENDING_LINK) { await send('/api/filament/link', {printer: PENDING_LINK[0], tray: PENDING_LINK[1], spool: made[0].id}); PENDING_LINK = null; }
      toast(made.length > 1 ? `Added ${made.length} spools` : 'Spool added');
    }
    resetEditor(); await loadFil(); if (PLATE) { refreshPlateFil(); refreshCost(); }
  } catch (err) { toast(err.message); }
};

// ---- unmatched usage, usage log, shopping list, settings ----
function renderUnmatched() {
  const rows = FIL.unmatched;
  $('#unmatchedCard').classList.toggle('hidden', !rows.length);
  $('#unmatchedOut').innerHTML = rows.map(r => `<div class="spread" style="padding:6px 0;border-top:1px solid var(--line)"><span>${swatch(r.color)}<b>${G(r.grams, 1)}</b> ${esc(r.material)} · ${esc(r.printer || '')} loop ${r.loop_no ?? '?'} <span class="hint">${new Date(r.ts * 1000).toLocaleString()} · ${esc(r.note || '')}</span></span>
    <span class="inline"><select data-um="${r.id}" style="max-width:220px">${spoolOptions(r.material, null, 'Pick the spool…')}</select></span></div>`).join('');
}
$('#unmatchedOut').addEventListener('change', async (e) => { const s = e.target.closest('[data-um]'); if (!s || !s.value) return;
  try { await send(`/api/filament/usage/${s.dataset.um}/assign`, {spool: s.value}); toast('Charged to the spool'); await loadFil(); } catch (err) { toast(err.message); } });

const KIND = {loop: 'loop', partial: 'stopped loop', manual: 'by hand', weigh: 'weighed', set: 'set'};
function usageRows(rows, withSpool = true) {
  if (!rows.length) return '';
  const byId = Object.fromEntries(FIL.spools.map(s => [s.id, s]));
  return '<tr><th>When</th>' + (withSpool ? '<th>Spool</th>' : '') + '<th>Grams</th><th>What</th><th></th></tr>' + rows.map(r => {
    const sp = byId[r.spool_id];
    const what = r.kind === 'loop' || r.kind === 'partial' ? `${KIND[r.kind]} ${r.loop_no ?? ''} on ${esc(r.printer || '?')}<div class="hint">${esc(r.job || '')} · ${esc(r.note || '')}</div>` : `${KIND[r.kind] || esc(r.kind)}<div class="hint">${esc(r.note || '')}</div>`;
    return `<tr><td class="num muted">${new Date(r.ts * 1000).toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'})}</td>` +
      (withSpool ? `<td>${sp ? swatch(sp.color) + esc(sp.label) : r.spool_id ? '<span class="hint">deleted spool</span>' : '<span class="chip warn">not matched</span>'}</td>` : '') +
      `<td class="num">${r.kind === 'weigh' || r.kind === 'set' ? (r.grams > 0 ? '−' : '+') + Math.abs(r.grams).toFixed(1) : r.grams.toFixed(1)}</td><td>${what}</td>
      <td>${['loop', 'partial', 'manual'].includes(r.kind) ? `<button class="linkbtn" data-undo="${r.id}" title="Remove this entry and give the grams back">undo</button>` : ''}</td></tr>`;
  }).join('');
}
async function loadUsage() {
  try {
    const days = +$('#usageDays').value, u = await json('/api/filament/usage?days=' + days);
    $('#usageTable').innerHTML = usageRows(u.rows) || '<tr><td class="hint">No filament used yet. Finished loops of files made by this ProofLoop are counted here automatically.</td></tr>';
    const mats = [...new Set(u.daily.flatMap(d => Object.keys(d.by_material)))];
    const max = Math.max(1, ...u.daily.map(d => Object.values(d.by_material).reduce((a, b) => a + b, 0)));
    const W = 600, H = 150, bw = W / u.daily.length;
    const bars = u.daily.map((d, i) => { let y = H - 18; return mats.map((m, j) => { const g = d.by_material[m] || 0, h = g / max * (H - 30); y -= h; return g ? `<rect x="${(i * bw + 1).toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(1, bw - 2).toFixed(1)}" height="${h.toFixed(1)}" fill="${PALETTE[j % PALETTE.length]}"><title>${new Date(d.day * 1000).toLocaleDateString()}: ${m} ${g.toFixed(1)} g</title></rect>` : ''; }).join(''); }).join('');
    const ticks = u.daily.map((d, i) => i % Math.ceil(u.daily.length / 6) === 0 ? `<text x="${(i * bw + bw / 2).toFixed(1)}" y="${H - 4}" font-size="10" text-anchor="${i ? 'middle' : 'start'}" fill="currentColor" opacity=".6">${new Date(d.day * 1000).toLocaleDateString([], {month: 'short', day: 'numeric'})}</text>` : '').join('');
    $('#usageChart').innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Filament used per day"><line x1="0" x2="${W}" y1="${H - 18}" y2="${H - 18}" stroke="currentColor" opacity=".15"/>${bars}${ticks}<text x="2" y="10" font-size="10" fill="currentColor" opacity=".6">${max.toFixed(0)} g</text></svg>`;
    $('#usageLegend').innerHTML = mats.map((m, j) => `<span><i style="background:${PALETTE[j % PALETTE.length]}"></i>${esc(m)}</span>`).join('');
  } catch (e) { toast(e.message); }
}
$('#usageDays').addEventListener('input', loadUsage);
$('[data-panel=filament]').addEventListener('click', async (e) => { const b = e.target.closest('[data-undo]'); if (!b) return;
  if (!confirm('Remove this entry and give its grams back to the spool?')) return;
  try { await json(`/api/filament/usage/${b.dataset.undo}`, {method: 'DELETE'}); await loadFil(); } catch (err) { toast(err.message); } });

function renderShopping() {
  const low = FIL.spools.filter(s => !s.archived && ['low', 'empty'].includes(s.status));
  const soon = FIL.spools.filter(s => !s.archived && s.status === 'ok' && s.forecast.days_left != null && s.forecast.days_left < 14);
  const lines = [...low.map(s => [s, s.status === 'empty' ? 'used up' : `${Math.round(s.remaining_g)} g left`]), ...soon.map(s => [s, `about ${Math.round(s.forecast.days_left)} days left`])];
  $('#shopOut').innerHTML = lines.length ? '<ul class="list">' + lines.map(([s, why]) => `<li>${swatch(s.color)}<div><b>${esc(s.label)}</b> <span class="hint">${esc(why)}${s.per_kg != null && s.net_g ? ` · last paid ${esc(money(s.price))}` : ''}</span></div></li>`).join('') + '</ul>'
    : '<p class="hint">Nothing to buy. Spools appear here when they drop below their warning weight, or when your last 14 days of use would empty them within two weeks.</p>';
  $('#shopCopy').onclick = () => { const t = lines.map(([s, why]) => `- ${s.label}${s.brand ? ' (' + s.brand + ')' : ''}: ${why}`).join('\n') || 'Nothing to buy'; navigator.clipboard?.writeText(t).then(() => toast('Copied'), () => toast(t)); };
}
function fillFilSettings() {
  const f = $('#filSettings'), s = FIL.settings; if (document.activeElement?.form === f) return;
  f.track_usage.checked = s.track_usage; f.hold_when_short.checked = s.hold_when_short; f.low_g.value = s.low_g ?? ''; f.spoolman_url.value = s.spoolman_url || ''; f.spoolman_report.checked = s.spoolman_report;
}
$('#filSettings').onsubmit = async (e) => { e.preventDefault(); const f = e.target;
  try { await send('/api/filament/settings', {track_usage: f.track_usage.checked, hold_when_short: f.hold_when_short.checked, low_g: f.low_g.value === '' ? null : +f.low_g.value, spoolman_url: f.spoolman_url.value.trim() || null, spoolman_report: f.spoolman_report.checked}, 'PUT'); toast('Saved'); await loadFil(); } catch (err) { toast(err.message); } };
$('#smImport').onclick = async () => { try { const r = await json('/api/filament/spoolman/import', {method: 'POST'}); toast(`Spoolman: ${r.added} added, ${r.updated} updated`); await loadFil(); } catch (err) { toast(err.message); } };
$('#restoreFile').addEventListener('change', async (e) => { const file = e.target.files[0]; e.target.value = ''; if (!file) return;
  if (!confirm('Replace everything in this ProofLoop (settings, printers, spools, history, design checks, the queue and the empty-plate pictures) with the backup?')) return;
  try { const fd = new FormData(); fd.append('file', file); const r = await json('/api/restore', {method: 'POST', body: fd}); toast(`Restored: ${r.printers} printers, ${r.spools} spools, ${r.plate_pictures} plate pictures, ${r.queued} queued runs`); await loadState(); await loadFil(); } catch (err) { toast(err.message); } });

// ---- the plate's filament (Make tab) ----
let plateFilTimer = null;
function filPrinters() {
  const sel = $('#fPrinter'), prev = sel.value;
  sel.innerHTML = '<option value="">Any printer (no AMS match)</option>' + (STATE.monitor || []).map(m => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join('');
  sel.value = sel.dataset.chosen && [...sel.options].some(o => o.value === prev) ? prev : (STATE.monitor?.[0]?.name || '');
}
function refreshPlateFil() {
  clearTimeout(plateFilTimer);
  plateFilTimer = setTimeout(async () => {
    if (!PLATE) return;
    try { PLATEFIL = await send(`/api/plates/${PLATE.token}/filament`, {loops: +$('#sLoops').value || 1, printer: $('#fPrinter').value || null}); renderPlateFil(); renderPlan(); }
    catch (e) { $('#makeFilOut').textContent = e.message; }
  }, 200);
}
function renderPlateFil() {
  const r = PLATEFIL; if (!r) return;
  const rows = r.slots.map(s => {
    const p = s.preset || {}, info = [p.preset, p.nozzle_temp_low ? `${p.nozzle_temp_low}–${p.nozzle_temp_high} °C nozzle` : '', p.ams_drying_temp ? `dry ${p.ams_drying_temp} °C${p.ams_drying_hours ? ' · ' + p.ams_drying_hours + ' h' : ''}` : ''].filter(Boolean).join(' · ');
    const tr = s.tray != null ? ` · ${trayLabel(s.tray)}` : s.trays.length ? ` · trays ${s.trays.map(trayLabel).join(', ')}` : '';
    let verdict = '<span class="hint">pick a spool to see how many loops it covers</span>';
    if (s.available_g != null) verdict = s.short ? `<span class="chip bad">enough for ${s.loops_possible} loop${s.loops_possible === 1 ? '' : 's'}</span> <button class="linkbtn" data-setloops="${s.loops_possible}">set loops to ${s.loops_possible}</button>`
      : `<span class="chip ok">enough for ${s.loops_possible} loops</span>`;
    else if (s.spool) verdict = '<span class="chip">amount on the spool unknown</span> <a href="#" data-goto="filament" class="linkbtn">weigh it</a>';
    return `<div class="filrow"><div class="spread"><span>${swatch(s.color)}<b>${esc(s.material)}</b> <span class="hint">${esc(s.color)} · slot ${esc(s.slot)}${tr}</span></span><span class="num">${s.grams_per_loop} g a loop · <b>${s.grams_total} g</b> for ${r.loops}</span></div>
      <div class="inline" style="margin:6px 0"><select data-pick="${esc(s.key)}" aria-label="Spool for slot ${esc(s.slot)}">${spoolOptions(s.material, s.spool, !s.spool && s.spools_counted.length ? 'Whichever the AMS feeds (' + s.trays.map(trayLabel).join(', ') + ')' : 'No spool')}</select></div>
      <div>${verdict}${s.available_g != null ? ` <span class="hint">${G(s.available_g)} available${s.spools_counted.length > 1 ? ' across ' + s.spools_counted.length + ' spools in the AMS' : ''}</span>` : ''}</div>
      <div class="hint">${esc(s.how)}${info ? ' · ' + esc(info) : ''}</div></div>`;
  }).join('');
  $('#makeFilOut').innerHTML = rows || '<p class="hint">The slicer listed no filament for this plate.</p>';
  $('#makeFilOut').classList.remove('hint');
}
$('#fPrinter').addEventListener('input', () => { $('#fPrinter').dataset.chosen = 1; refreshPlateFil(); refreshCost(); });
$('#makeFilOut').addEventListener('change', async (e) => { const s = e.target.closest('[data-pick]'); if (!s) return;
  try { await send('/api/filament/assign', {printer: $('#fPrinter').value || '', key: s.dataset.pick, spool: s.value || null}, 'PUT'); refreshPlateFil(); refreshCost(); } catch (err) { toast(err.message); } });
$('#makeFilOut').addEventListener('click', (e) => { const b = e.target.closest('[data-setloops]'); if (!b) return;
  $('#sLoops').value = b.dataset.setloops; syncSliders(); reanalyze(); toast('Loops set to ' + b.dataset.setloops); });
