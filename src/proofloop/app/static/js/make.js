// Make a loop file: the plate, its settings, sending, the sweep, planning a run, batches.
// Part of loop.html; the files load in order and share their top-level names.

// ---------- make ----------
dropZone($('#makeDrop'), $('#makeFile'), async (file) => {
  $('#makeError').classList.add('hidden'); $('#noProfile').classList.add('hidden');
  $('#makeDrop strong').textContent = 'Reading ' + file.name + '…';
  try {
    const fd = new FormData(); fd.append('file', file);
    showOpened(await json('/api/plates', {method: 'POST', body: fd}));
  } catch (e) { makeError(e); }
  $('#makeDrop strong').textContent = 'Drop another plate, or click to choose';
});
function makeError(e) { $('#makeError').innerHTML = `<span class="ic danger">✖</span><div>${esc(e.message)}</div>`; $('#makeError').classList.remove('hidden'); $('#makeResult').classList.add('hidden'); }
function showOpened(res) {
  if (res.no_profile) { showNoProfile(res); $('#makeResult').classList.add('hidden'); return; }
  const kept = PLATE && BATCH.find(x => x.token === PLATE.token);
  if (kept) kept.part_push = {...partPush};  // the plate leaving the screen keeps its own heights
  partPush = {}; PLATE = res; renderPlate(res);
}

// ---------- inside the ProofLoop window ----------
// The window shows this page as its Loop files and Printers tabs, and hands plates to and from Check.
const IN_PROOFLOOP = window.parent !== window && new URLSearchParams(location.search).has('proofloop');
if (IN_PROOFLOOP) {
  document.documentElement.classList.add('in-proofloop');
  window.addEventListener('message', async (e) => {
    if (e.origin !== location.origin || e.source !== window.parent || !e.data) return;
    if (e.data.proofloop === 'part' && PARTS[e.data.part]) {
      setPart(e.data.part, e.data.tab);
      const el = typeof e.data.anchor === 'string' && document.getElementById(e.data.anchor);
      if (el) {  // the first-run guide points at the exact spot
        el.scrollIntoView({block: 'center', behavior: 'smooth'});
        el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 1800);
      }
      return;
    }
    if (e.data.proofloop !== 'open-plate') return;
    show('make'); $('#makeError').classList.add('hidden'); $('#noProfile').classList.add('hidden');
    try { showOpened(await send(`/api/plates/${encodeURIComponent(e.data.token)}/open`, {})); toast('Opened ' + (PLATE ? PLATE.file : 'the plate') + ' from Check'); }
    catch (err) { makeError(err); }
  });
  $('#toCheck').classList.remove('hidden');
  $('#toCheck').onclick = () => { if (PLATE) window.parent.postMessage({proofloop: 'check-plate', token: PLATE.token}, location.origin); };
}

function showNoProfile(res) {
  const el = $('#noProfile');
  el.innerHTML = `<h2>No hardware profile for the ${esc(res.printer)} yet</h2>
    <p>ProofLoop ships with a verified profile for the P2S. For other printers it learns your kit's exact moves from a FarmLoop file rather than guessing them. That takes a minute and works with the free FarmLoop Lite plan (Stage 1).</p>
    <button class="btn primary" data-goto="profiles">Add my kit from a FarmLoop file</button>`;
  el.classList.remove('hidden');
}

function currentSettings() {
  return {
    profile_id: $('#sProfile').value, loops: +$('#sLoops').value || 1, release_temp: +$('#sRelease').value,
    floor_z: +$('#sFloor').value, push_offset: +$('#sDepth').value, natural_cooldown_min: +$('#sNatural').value || 0, bender_fitted: $('#sBender').checked,
    cooldown_fan: $('#sFan').value, wait_for_monitor: $('#sMonitor').checked, pause_first_sweep: $('#sFirstPause').dataset.mode || 'auto', part_push: partPush,
  };
}
function fillSettings(s, a) {
  const profiles = STATE.profiles.filter(p => p.printer_model_id === a.printer_model_id);
  $('#sProfile').innerHTML = profiles.map(p => `<option value="${esc(p.id)}">${esc(p.name)}${p.builtin ? '' : ' (imported)'}</option>`).join('');
  $('#sProfile').value = s.profile_id;
  $('#sLoops').value = s.loops; $('#sRelease').value = s.release_temp; $('#sFloor').value = s.floor_z; $('#sDepth').value = s.push_offset;
  syncSliders();
  $('#sNatural').value = s.natural_cooldown_min; $('#sBender').checked = s.bender_fitted; $('#sFan').value = s.cooldown_fan; $('#sMonitor').checked = s.wait_for_monitor;
  const fp = $('#sFirstPause'); fp.dataset.mode = s.pause_first_sweep || 'auto'; fp.checked = !!a.first_pause;
  $('#firstPauseHint').dataset.auto = fp.dataset.mode === 'auto' ? (a.first_pause ? 'on' : 'off') : '';
  const prof = STATE.profiles.find(p => p.id === s.profile_id) || {};
  $('#profileHint').textContent = (prof.description || '') + (prof.builtin ? ' Built in, from public FarmLoop output.' : '');
  $('#benderRow').classList.toggle('hidden', !(prof.stage === 2 && prof.has_bender_steps));
  $('#sFan').querySelector('[value=exhaust]').disabled = a.printer_model_id !== 'N7';
  $('#floorChip').innerHTML = STATE.defaults.floor_calibrated && STATE.defaults.floor_z === s.floor_z ? '<span class="chip ok">calibrated</span>' : '<span class="chip warn">not calibrated</span>';
  const warp = (a.filaments || []).filter(f => /^(ASA|ABS|PC)/i.test(f));
  $('#naturalHint').innerHTML = warp.length && !s.natural_cooldown_min ? `<b style="color:var(--warn)">${esc(warp.join(', '))} on this plate:</b> consider a natural cooldown so parts don't warp when the door opens.` : 'Door shut and fans off before the door opens. Helps ASA, ABS and PC keep their shape.';
}
const schedule = () => { clearTimeout(pending); pending = setTimeout(reanalyze, 350); };
['#sProfile', '#sLoops', '#sRelease', '#sFloor', '#sDepth', '#sNatural', '#sBender', '#sFan', '#sMonitor', '#sFirstPause'].forEach(id => $(id).addEventListener('input', schedule));
$('#sFirstPause').addEventListener('input', (e) => { e.target.dataset.mode = e.target.checked ? 'on' : 'off'; $('#firstPauseHint').dataset.auto = ''; });
// Every slider is paired with its number box; either one can be used.
function syncSliders() { $$('input[type=range][data-num]').forEach(r => { r.value = $('#' + r.dataset.num).value; }); }
$$('input[type=range][data-num]').forEach(r => r.addEventListener('input', () => { $('#' + r.dataset.num).value = r.value; schedule(); }));
$$('input[type=range][data-num]').forEach(r => $('#' + r.dataset.num).addEventListener('input', () => { r.value = $('#' + r.dataset.num).value; }));
async function reanalyze() {
  if (!PLATE) return;
  try { PLATE = await send(`/api/plates/${PLATE.token}/analyze`, currentSettings()); renderPlate(PLATE, true); }
  catch (e) { toast(e.message); }
}

function renderPlate(a, keepSettings) {
  $('#makeResult').classList.remove('hidden');
  $('#plateTitle').textContent = a.file;
  $('#plateChip').innerHTML = a.ok ? '<span class="chip ok">ready</span>' : '<span class="chip bad">needs attention</span>';
  $('#plateFacts').innerHTML = [
    ['Printer', a.printer], ['Filament', (a.filaments || []).join(', ') || '—'], ['Plate', a.bed_type || '—'],
    ['Print time', fmtDur(a.print_seconds)], ['Parts', a.parts.length], ['Sliced with', a.slicer || '—'],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  if (!keepSettings) fillSettings(a.settings, a); else fillSettings(currentSettings(), a);
  stopAnim(); drawTop(a); drawSide(a); planPrinters(); renderPlan();
  if (document.activeElement !== $('#cLoops')) $('#cLoops').value = +$('#sLoops').value || 1;
  refreshCost(); refreshPlateFil();
  $('#changesOut').innerHTML = '';
  $('#addToBatch').textContent = BATCH.some(b => b.token === a.token) ? '✓ In the batch' : '+ Add this plate';
  const floor = +$('#sFloor').value || 0;
  $('#partTable').innerHTML = '<tr><th>Part</th><th>Height</th><th>Push at</th><th></th></tr>' + a.parts.map(p => {
    const z = a.plan.part_push[p.label], reached = z != null && z < p.height;
    const target = a.plan.part_target[p.label], mine = (a.plan.part_custom || []).includes(p.label);
    const max = Math.max(floor, Math.floor(p.height * 2) / 2);
    return `<tr><td>${esc(p.name)}<div class="hint">label ${esc(p.label)}</div></td><td class="num">${p.height.toFixed(1)} mm</td>
      <td><div class="pslide"><input type="range" data-part="${esc(p.label)}" min="${floor}" max="${max}" step="0.5" value="${target}" aria-label="Push height for ${esc(p.name)}">
        <span class="num" data-partval="${esc(p.label)}">Z${target}</span>
        ${mine ? `<span><span class="chip info">yours</span> <button class="linkbtn" data-reset="${esc(p.label)}" title="Back to part height minus push depth">reset</button></span>` : '<span class="hint" title="part height minus push depth">auto</span>'}</div></td>
      <td>${reached ? '<span class="chip ok">swept</span>' : '<span class="chip bad">not reached</span>'}</td></tr>`;
  }).join('');
  // timeline
  let known = 0;
  $('#timeline').innerHTML = a.steps.map(s => { if (s.seconds) known += s.seconds; return `<div class="step"><div class="dot"></div><div><b>${esc(s.name)}</b><small>${esc(s.detail)}</small></div><div class="num muted">${s.seconds == null ? 'varies' : fmtDur(s.seconds)}</div></div>`; }).join('');
  $('#loopTotal').textContent = `About ${fmtDur(known)} per loop, plus the cooldown (it depends on your room and plate).`;
  // checks
  const items = [];
  a.problems.forEach(t => items.push(['problem', '✖', t]));
  a.warnings.forEach(t => items.push(['warning', '!', t]));
  a.notes.forEach(t => items.push(['note', 'i', t]));
  const allSwept = a.parts.every(p => a.plan.part_push[p.label] != null && a.plan.part_push[p.label] < p.height);
  if (allSwept) items.push(['ok', '✓', `Every part is reached by the sweep (${a.parts.length} checked).`]);
  items.push(['ok', '✓', 'Cycle runs inside the printer\'s executable block, after its own end G-code, with steppers switched back on.']);
  items.push(['ok', '✓', 'Bed wait uses M190 S, never M190 R (that faults on a P2S).']);
  if (a.profile.stage === 2) items.push(['ok', '✓', 'FarmBoard triggers and switch gestures are copied exactly from FarmLoop\'s own output for this kit.']);
  $('#checks').innerHTML = items.map(([k, i, t]) => `<li><span class="ic ${k}">${i}</span><div>${esc(t)}</div></li>`).join('');
  $('#download').disabled = !a.ok; $('#sendBtn').disabled = !a.ok;
  $('#download').textContent = a.ok ? 'Download loop file' : 'Fix the problems above first';
}

function heightColor(h, max) { const t = max ? h / max : 0; return `hsl(${215 - t * 30} 75% ${62 - t * 22}%)`; }
let TOPMAP = null, SIDEMAP = null, ANIM = null;
function drawTop(a) {
  const [W, H] = a.bed, pad = 14, s = 360 / Math.max(W, H);
  const w = W * s, h = H * s, max = Math.max(...a.parts.map(p => p.height), 1);
  const X = x => pad + x * s, Y = y => pad + (H - y) * s;
  TOPMAP = {X, Y};
  const pass = a.plan.passes[0] || {lanes: []};
  const uniq = [...new Map(pass.lanes.map(l => [l.x, l])).values()].sort((p, q) => p.x - q.x);
  let lanes = '', lastX = -1e9, row = 0;
  uniq.forEach(l => {
    // Lanes closer than ~26 px get their labels on alternating rows so they never overlap.
    row = X(l.x) - lastX < 26 ? (row + 1) % 2 : 0; lastX = X(l.x);
    const top = Y(a.profile.sweep.y_rear) - 4 - row * 11;
    lanes += `<line x1="${X(l.x)}" x2="${X(l.x)}" y1="${Y(a.profile.sweep.y_rear)}" y2="${Y(a.profile.sweep.y_front)}" stroke="var(--ok)" stroke-width="1.5" stroke-dasharray="4 3" opacity=".8"><title>lane X${l.x}: push at Z${l.tiers.join(', Z')}</title></line>
      <text x="${X(l.x)}" y="${top}" font-size="9" text-anchor="middle" fill="var(--ok)">${esc(l.tiers.map(t => +t.toFixed(1)).join('/'))}</text>`; });
  const parts = a.parts.map(p => `<g><rect x="${X(p.xmin)}" y="${Y(p.ymax)}" width="${(p.xmax - p.xmin) * s}" height="${(p.ymax - p.ymin) * s}" rx="2" fill="${heightColor(p.height, max)}" fill-opacity=".55" stroke="var(--accent)"/>
      <title>${esc(p.name)} · ${p.height} mm</title><text x="${X((p.xmin + p.xmax) / 2)}" y="${Y((p.ymin + p.ymax) / 2) + 3}" font-size="10" text-anchor="middle" fill="var(--ink)">${p.height.toFixed(0)} mm</text></g>`).join('');
  $('#topView').innerHTML = `<svg viewBox="0 ${-14} ${w + pad * 2} ${h + pad * 2 + 30}" role="img" aria-label="Top view of the plate">
    <rect x="${pad}" y="${pad}" width="${w}" height="${h}" rx="6" fill="var(--soft)" stroke="var(--line)"/>${lanes}${parts}<g id="animTop"></g>
    <text x="${pad + w / 2}" y="${h + pad * 2 + 10}" font-size="11" text-anchor="middle" fill="var(--muted)">front · door · parts leave this way ↓</text></svg>`;
}
function drawSide(a) {
  const W = a.bed[0], Hmax = Math.max(...a.parts.map(p => p.height), 20) * 1.15, pw = 380, ph = 150, pad = 26;
  const X = x => pad + x / W * pw, Y = z => 8 + ph - z / Hmax * ph;
  SIDEMAP = {X, Y, Hmax};
  const bars = a.parts.map(p => `<rect x="${X(p.xmin)}" y="${Y(p.height)}" width="${Math.max(2, X(p.xmax) - X(p.xmin))}" height="${Y(0) - Y(p.height)}" fill="var(--accent)" fill-opacity=".3" stroke="var(--accent)"><title>${esc(p.name)} · ${p.height} mm</title></rect>`).join('');
  const seen = new Set(); let dots = '';
  (a.plan.passes[0] || {lanes: []}).lanes.forEach(l => { if (seen.has(l.x)) return; seen.add(l.x);
    l.tiers.forEach(t => { if (t <= Hmax) dots += `<circle cx="${X(l.x)}" cy="${Y(t)}" r="3.5" fill="var(--ok)"><title>push at Z${t} in lane X${l.x}</title></circle>`; }); });
  $('#sideView').innerHTML = `<svg viewBox="0 0 ${pw + pad * 2} ${ph + 30}" role="img" aria-label="Side view">
    <line x1="${pad}" x2="${pad + pw}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--muted)"/>
    <line x1="${pad}" x2="${pad + pw}" y1="${Y(+$('#sFloor').value || 0)}" y2="${Y(+$('#sFloor').value || 0)}" stroke="var(--warn)" stroke-dasharray="3 3"/>
    <text x="${pad + pw}" y="${Y(+$('#sFloor').value || 0) - 3}" font-size="9" text-anchor="end" fill="var(--warn)">sweep floor</text>
    ${bars}${dots}<g id="animSide"></g><text x="${pad}" y="${ph + 26}" font-size="10" fill="var(--muted)">X 0</text><text x="${pad + pw}" y="${ph + 26}" font-size="10" text-anchor="end" fill="var(--muted)">X ${W}</text></svg>`;
}

$('#partTable').addEventListener('input', (e) => {
  const r = e.target.closest('[data-part]'); if (!r) return;
  $(`[data-partval="${CSS.escape(r.dataset.part)}"]`).textContent = 'Z' + r.value;
});
$('#partTable').addEventListener('change', (e) => {
  const r = e.target.closest('[data-part]'); if (!r) return;
  partPush[r.dataset.part] = +r.value; reanalyze();
});
$('#partTable').addEventListener('click', (e) => {
  const b = e.target.closest('[data-reset]'); if (!b) return;
  delete partPush[b.dataset.reset]; reanalyze();
});
$('#download').onclick = async () => {
  try { const r = await api(`/api/plates/${PLATE.token}/download`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(currentSettings())});
    const name = await saveBlob(r, 'loop.gcode.3mf'); toast('Saved ' + name); }
  catch (e) { toast(e.message); }
};
// ---------- send to a printer (only when turned on, never from a phone) ----------
function renderSend() {
  const on = !!(STATE && STATE.send_to_printer), mons = (STATE && STATE.monitor) || [];
  $('#sendCard').classList.toggle('hidden', !(STATE && STATE.can_send));
  $('#restoreBtn').classList.toggle('hidden', !(STATE && STATE.can_send));  // a phone can't restore
  $('#sendToggle').checked = on;
  $('#sendBox').classList.toggle('hidden', !on || !mons.length);
  $('#spreadBtn').classList.toggle('hidden', !mons.length);
  const keep = $('#sendPrinter').value;
  $('#sendPrinter').innerHTML = mons.map(m => {
    const st = (m.printer.gcode_state || '').toLowerCase(), online = m.printer.online;
    return `<option value="${esc(m.name)}">${esc(m.name)} · ${online ? (st || 'connected') : 'not connected'}</option>`;
  }).join('');
  if (keep) $('#sendPrinter').value = keep;
}
$('#sendToggle').onchange = async () => {
  try { const r = await send('/api/send-to-printer', {on: $('#sendToggle').checked}, 'PUT'); STATE.send_to_printer = r.send_to_printer; renderSend();
    toast(r.send_to_printer ? 'ProofLoop can send loop files to your printers now' : 'Sending to printers is off'); }
  catch (e) { $('#sendToggle').checked = !$('#sendToggle').checked; toast(e.message); }
};
$('#sendBtn').onclick = async () => {
  const printer = $('#sendPrinter').value, s = currentSettings();
  if (!PLATE || !printer) return;
  if (!confirm(`Start ${PLATE.file} (${s.loops} loop${s.loops > 1 ? 's' : ''}) on ${printer}?\n\nThe plate must be empty. The printer starts as soon as the file arrives.`)) return;
  $('#sendBtn').disabled = true;
  try { const r = await send(`/api/plates/${PLATE.token}/send`, {...s, printer, confirm: printer}); toast(`Started ${r.file} on ${r.printer}`); }
  catch (e) { toast(e.message); }
  $('#sendBtn').disabled = false;
};

$('#saveDefaults').onclick = async () => { try { STATE.defaults = await send('/api/defaults', currentSettings(), 'PUT'); toast('Saved as your defaults'); } catch (e) { toast(e.message); } };


// ---------- sweep animation: replays the exact moves in the file ----------
function stopAnim() { if (ANIM) { cancelAnimationFrame(ANIM.raf); ANIM = null; } $('#playSweep').textContent = '▶ Play the sweep'; }
$('#playSweep').onclick = () => {
  if (ANIM) { stopAnim(); return; }
  if (!PLATE || !PLATE.path || PLATE.path.length < 2) return;
  const pts = PLATE.path, speed = +$('#animSpeed').value;
  // time of each segment from its distance and feed (mm/min), so slow and fast passes look right
  const segs = []; let total = 0;
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1], b = pts[i];
    const d = Math.hypot(b.x - a.x, b.y - a.y, b.z - a.z); const t = d / Math.max(1, b.f / 60);
    segs.push({a, b, t0: total, t}); total += t;
  }
  const gTop = $('#animTop'), gSide = $('#animSide');
  gTop.innerHTML = '<path id="trail" fill="none" stroke="var(--ok)" stroke-width="2.5" stroke-opacity=".55" stroke-linecap="round" d=""/><circle id="head" r="5" fill="var(--warn)" stroke="#fff" stroke-width="1.5"/>';
  gSide.innerHTML = '<circle id="headSide" r="4.5" fill="var(--warn)" stroke="#fff" stroke-width="1.5"/>';
  let trail = '', lastSeg = -1;
  const start = performance.now();
  $('#playSweep').textContent = '■ Stop';
  const frame = (now) => {
    const t = (now - start) / 1000 * speed;
    let i = segs.findIndex(sg => t < sg.t0 + sg.t); if (i < 0) i = segs.length - 1;
    const sg = segs[i], k = sg.t ? Math.min(1, Math.max(0, (t - sg.t0) / sg.t)) : 1;
    const x = sg.a.x + (sg.b.x - sg.a.x) * k, y = sg.a.y + (sg.b.y - sg.a.y) * k, z = sg.a.z + (sg.b.z - sg.a.z) * k;
    const hx = TOPMAP.X(x), hy = TOPMAP.Y(y);
    $('#head').setAttribute('cx', hx); $('#head').setAttribute('cy', hy);
    if (sg.b.push) {
      // each push is its own stroke, so lanes never get joined by diagonal lines
      if (i !== lastSeg) { trail += ` M${TOPMAP.X(sg.a.x).toFixed(1)} ${TOPMAP.Y(sg.a.y).toFixed(1)}`; lastSeg = i; }
      trail += ` L${hx.toFixed(1)} ${hy.toFixed(1)}`; $('#trail').setAttribute('d', trail);
    }
    if (z <= SIDEMAP.Hmax) { $('#headSide').setAttribute('cx', SIDEMAP.X(x)); $('#headSide').setAttribute('cy', SIDEMAP.Y(z)); }
    $('#animInfo').textContent = `X${x.toFixed(0)} Y${y.toFixed(0)} Z${z.toFixed(1)} · ${sg.b.push ? 'pushing' : 'moving'} · ${fmtDur(Math.min(t, total))} of ${fmtDur(total)}`;
    if (t < total) ANIM.raf = requestAnimationFrame(frame); else { stopAnim(); $('#animInfo').textContent = `Done: ${pts.filter(p => p.push).length} pushes in about ${fmtDur(total)}.`; }
  };
  ANIM = {raf: requestAnimationFrame(frame)};
};

// ---------- production planner ----------
function planPrinters() {
  const sel = $('#planPrinter'), prev = sel.value;
  const timed = (STATE.monitor || []).filter(m => m.timings && m.timings.cooldown);
  sel.innerHTML = (timed.length ? timed.map(m => `<option value="${esc(m.name)}">${esc(m.name)}: cooldown ${fmtDur(m.timings.cooldown.avg)} avg</option>`).join('') : '') + '<option value="">Not measured yet</option>';
  if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
}
function renderPlan() {
  if (!PLATE) return;
  const qty = +$('#planQty').value, per = PLATE.parts.length;
  if (!qty || !per) { $('#planApply').disabled = true; return; }
  const loops = Math.ceil(qty / per), extra = loops * per - qty;
  const m = (STATE.monitor || []).find(x => x.name === $('#planPrinter').value);
  const known = PLATE.steps.reduce((t, s) => t + (s.seconds || 0), 0);
  let eta = `About ${fmtDur(known * loops)} of printing and fixed steps`;
  if (m && m.timings.cooldown) {
    const total = (known + m.timings.cooldown.avg) * loops, done = new Date(Date.now() + total * 1000);
    eta = `About <b>${fmtDur(total)}</b> in total, using ${esc(m.name)}'s measured cooldown. Started now, it would finish around <b>${done.toLocaleString([], {weekday: 'short', hour: '2-digit', minute: '2-digit'})}</b>`;
  } else eta += ', plus the cooldown after each loop (the Monitor times it on your printer)';
  let fil = '';
  if (PLATEFIL && PLATEFIL.slots.length) {
    const need = PLATEFIL.slots.map(x => `${(x.grams_per_loop * loops).toFixed(0)} g ${esc(x.material)}`).join(' + ');
    const cap = PLATEFIL.max_loops;
    fil = ` Filament: ${need}.` + (cap != null ? (cap < loops ? ` <b style="color:var(--bad)">Your spools cover ${cap} loop${cap === 1 ? '' : 's'}</b>; load more before you start.` : ` Your spools cover it (${cap} loops' worth).`) : '');
  }
  $('#planOut').innerHTML = `<b>${loops} loop${loops > 1 ? 's' : ''}</b> of ${per} part${per > 1 ? 's' : ''} = ${loops * per} parts${extra ? ` (${extra} spare)` : ''}. ${eta}.${fil}`;
  $('#planApply').disabled = false; $('#planApply').dataset.loops = loops;
}
['#planQty', '#planPrinter'].forEach(id => $(id).addEventListener('input', renderPlan));
$('#planApply').onclick = () => { $('#sLoops').value = $('#planApply').dataset.loops; syncSliders(); reanalyze(); toast('Loops set to ' + $('#planApply').dataset.loops); };

// ---------- spread a run over your printers, and the queue ----------
let SPREAD = null;
const when = (s) => s == null ? '—' : new Date(Date.now() + s * 1000).toLocaleString([], {weekday: 'short', hour: '2-digit', minute: '2-digit'});
$('#spreadBtn').onclick = async () => {
  const qty = +$('#planQty').value;
  if (!PLATE || !qty) { toast('Enter how many parts you need first'); return; }
  try { SPREAD = await send(`/api/plates/${PLATE.token}/spread`, {parts: qty, settings: currentSettings()}); renderSpread(); }
  catch (e) { toast(e.message); }
};
function renderSpread() {
  const s = SPREAD;
  if (!s) { $('#spreadOut').innerHTML = ''; return; }
  const out = s.left_out.length ? `<p class="hint">Left out: ${s.left_out.map(x => `${esc(x.printer)} (${esc(x.why)})`).join(', ')}.</p>` : '';
  if (!s.rows.length) { $('#spreadOut').innerHTML = `<div class="banner warn" style="margin-top:10px"><span class="ic warning">!</span><div>No printer can print this plate right now.${out}</div></div>`; return; }
  $('#spreadOut').innerHTML = `<div class="tw" style="margin-top:10px"><table><tr><th>Printer</th><th class="n">Loops</th><th class="n">Parts</th><th>Starts</th><th>Finishes</th><th>Cooldown</th></tr>
    ${s.rows.map(r => `<tr><td>${esc(r.printer)}</td><td class="n">${r.loops}${r.files > 1 ? ` <span class="hint">(${r.files} files)</span>` : ''}</td><td class="n">${r.parts}</td>
      <td>${r.starts_in_s ? 'in ' + fmtDur(r.starts_in_s) : 'now'}</td><td>${when(r.finish_s)}</td><td class="hint">${esc(r.cooldown)}</td></tr>`).join('')}</table></div>
    <p class="hint">${s.loops} loops, ${s.parts} parts, done around <b>${when(s.finish_s)}</b>${s.cooldown_known ? '' : ' plus the cooldowns, which no printer has timed yet'}.</p>${out}
    <button class="btn primary" id="keepQueue">Keep it as a queue</button>
    <p class="hint">ProofLoop tells you when each printer is free for its next job. You send or download each one; nothing starts by itself.</p>`;
  $('#keepQueue').onclick = async () => {
    try { await send('/api/queue', {token: PLATE.token, parts: +$('#planQty').value, settings: currentSettings()}); toast('Queued: see Printers › Overview'); SPREAD = null; renderSpread(); refreshQueue(); }
    catch (e) { toast(e.message); }
  };
}
async function refreshQueue() {
  let runs = [];
  try { runs = await json('/api/queue'); } catch (e) { return; }
  const sendOn = !!(STATE && STATE.send_to_printer);
  const label = (j) => ({waiting: '<span class="chip">waiting</span>', sent: '<span class="chip info">sent, not started yet</span>',
    downloaded: '<span class="chip info">downloaded</span>', printing: `<span class="chip info">printing loop ${j.loop || 1} of ${j.loops}</span>`,
    done: '<span class="chip ok">done</span>', stopped: `<span class="chip bad">stopped: ${esc(j.why || '')}</span>`})[j.status] || '';
  const again = (j) => j.status === 'waiting' || j.status === 'stopped';
  $('#queueOut').innerHTML = runs.length ? runs.map(run => `<div style="padding:8px 0;border-top:1px solid var(--line)">
      <div class="spread"><b>${esc(run.plate)}</b><span><span class="hint">${run.parts} parts</span> <button class="linkbtn" data-qremove="${esc(run.id)}">Remove</button></span></div>
      ${run.jobs.map(j => `<div class="spread" style="margin-top:4px"><span>${esc(j.printer)} · ${j.loops} loop${j.loops > 1 ? 's' : ''} ${label(j)}</span>
        <span class="row">${again(j) && sendOn ? `<button class="btn" data-qsend="${esc(run.id)}/${esc(j.id)}" data-printer="${esc(j.printer)}">Send and start</button>` : ''}
        ${again(j) ? `<button class="btn" data-qget="${esc(run.id)}/${esc(j.id)}">Download</button>` : ''}</span></div>`).join('')}
    </div>`).join('') : '<p class="hint">Nothing queued. Under <a href="#" data-goto="make">Make a loop file</a>, plan a run and spread it over your printers.</p>';
}
$('#queueOut').addEventListener('click', async (e) => {
  const b = e.target.closest('button'); if (!b) return;
  try {
    if (b.dataset.qremove) { if (!confirm('Remove this run from the queue?')) return; await json('/api/queue/' + b.dataset.qremove, {method: 'DELETE'}); }
    if (b.dataset.qget) { const name = await saveBlob(await api(`/api/queue/${b.dataset.qget}/download`, {method: 'POST'}), 'loop.gcode.3mf'); toast('Saved ' + name); }
    if (b.dataset.qsend) {
      if (!confirm(`Start this job on ${b.dataset.printer}?\n\nThe plate must be empty. The printer starts as soon as the file arrives.`)) return;
      const r = await send(`/api/queue/${b.dataset.qsend}/send`, {confirm: b.dataset.printer}); toast(`Started ${r.file} on ${r.printer}`);
    }
  } catch (err) { toast(err.message); }
  refreshQueue();
});

// ---------- what ProofLoop adds ----------
$('#showChanges').onclick = async () => {
  try {
    const c = await send(`/api/plates/${PLATE.token}/changes`, currentSettings());
    $('#changesOut').innerHTML = `<p class="hint">${c.lines_added} lines added per loop${c.loops > 1 ? `, repeated for all ${c.loops} loops` : ''}.</p>` +
      c.blocks.map((b, i) => `<h3>${b.name === 'START' ? 'At the start, after the start template line' : 'After the print, before the end of the executable block'} <button class="linkbtn" data-copy="${i}">copy</button></h3><pre class="code mono">${esc(b.gcode)}</pre>`).join('');
    $('#changesOut').querySelectorAll('[data-copy]').forEach(btn => btn.onclick = async () => { try { await navigator.clipboard.writeText(c.blocks[+btn.dataset.copy].gcode); toast('Copied'); } catch (e) { toast('Copy not allowed here; select the text instead'); } });
  } catch (e) { toast(e.message); }
};

// ---------- batch ----------
let BATCH = [];
$('#addToBatch').onclick = () => {
  if (!PLATE) return;
  if (BATCH.some(b => b.token === PLATE.token)) { toast('This plate is already in the batch'); return; }
  BATCH.push({token: PLATE.token, file: PLATE.file, parts: PLATE.parts.length, count: +$('#sLoops').value || 1, part_push: {...partPush}});
  $('#addToBatch').textContent = '✓ In the batch';
  renderBatch(); toast('Added. Drop another plate to add it too.');
};
function renderBatch() {
  $('#batchRows').innerHTML = BATCH.map((b, i) => `<div class="batch-row"><div><b>${esc(b.file)}</b><div class="hint">${b.parts} part${b.parts > 1 ? 's' : ''} per plate${Object.keys(pushOf(b)).length ? ` · ${Object.keys(pushOf(b)).length} push height${Object.keys(pushOf(b)).length > 1 ? 's' : ''} set by you` : ''}</div></div>
    <div class="stepper"><button data-b="${i}" data-d="-1" aria-label="fewer">−</button><span>${b.count}×</span><button data-b="${i}" data-d="1" aria-label="more">+</button></div>
    <div class="row"><button class="linkbtn" data-up="${i}" ${i ? '' : 'disabled'}>↑</button><button class="linkbtn" data-rm="${i}">remove</button></div></div>`).join('');
  $('#batchDownload').classList.toggle('hidden', !BATCH.length);
  if (BATCH.length) previewBatch(); else $('#batchOut').innerHTML = '';
}
$('#batchRows').addEventListener('click', (e) => {
  const t = e.target;
  if (t.dataset.d) { const b = BATCH[+t.dataset.b]; b.count = Math.max(1, Math.min(50, b.count + +t.dataset.d)); }
  else if (t.dataset.up) { const i = +t.dataset.up; [BATCH[i - 1], BATCH[i]] = [BATCH[i], BATCH[i - 1]]; }
  else if (t.dataset.rm) { BATCH.splice(+t.dataset.rm, 1); }
  else return;
  renderBatch();
});
// Each plate keeps its own per-part push heights; the plate on screen uses its live sliders.
const pushOf = (b) => (PLATE && b.token === PLATE.token ? partPush : b.part_push) || {};
const batchBody = () => ({items: BATCH.map(b => ({token: b.token, count: b.count, part_push: pushOf(b)})), settings: {...currentSettings(), part_push: {}}});
async function previewBatch() {
  try {
    const r = await send('/api/batch/preview', batchBody());
    const probs = r.items.flatMap(it => it.problems);
    $('#batchOut').innerHTML = `<p class="hint"><b>${r.loops} loops</b>, ${r.parts} parts, about ${fmtDur(r.print_seconds)} of printing plus cooldowns.</p>` +
      (probs.length || r.notes.length ? `<ul class="list">${probs.map(p => `<li><span class="ic danger">✖</span><div>${esc(p)}</div></li>`).join('')}${r.notes.map(n => `<li><span class="ic info">i</span><div class="hint">${esc(n)}</div></li>`).join('')}</ul>` : '');
    $('#batchDownload').disabled = !r.ok;
  } catch (e) { $('#batchOut').innerHTML = `<p class="hint" style="color:var(--bad)">${esc(e.message)}</p>`; $('#batchDownload').disabled = true; }
}
$('#batchDownload').onclick = async () => {
  try { const r = await api('/api/batch/download', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(batchBody())}); toast('Saved ' + await saveBlob(r, 'batch.gcode.3mf')); }
  catch (e) { toast(e.message); }
};
