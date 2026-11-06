// Monitor: each printer, the plate check, print history and events.
// Part of loop.html; the files load in order and share their top-level names.

// ---------- monitor ----------
// The camera view: live video while it is open and on screen. Printer name -> its view, which is kept
// across the cards being redrawn (so the picture never blinks), with the stream that feeds it.
const cameraOn = {};
const cameraShown = () => !document.hidden && document.body.getClientRects().length > 0 && !$('[data-panel=monitor]').classList.contains('hidden');
function cameraView(name) {
  const el = document.createElement('div');
  el.className = 'cam-live';
  el.innerHTML = `<img alt="Live camera: ${esc(name)}"><span class="cam-badge">connecting…</span>
    <button class="cam-big" type="button" title="Bigger (Esc to close)" aria-label="Show the camera bigger">⤢</button><p class="cam-note hidden"></p>`;
  el.querySelector('.cam-big').onclick = el.querySelector('img').onclick = () => el.classList.toggle('expanded');
  return {el, img: el.querySelector('img'), badge: el.querySelector('.cam-badge'), note: el.querySelector('.cam-note'), url: '', ctl: null, times: []};
}
function cameraToggle(name) {
  const v = cameraOn[name];
  if (v) { v.ctl?.abort(); if (v.url) URL.revokeObjectURL(v.url); delete cameraOn[name]; } else { cameraOn[name] = cameraView(name); cameraStart(); }
}
// One part of the multipart stream from the front of buf: {type, body, rest}, or null until it has all come.
const CRLF2 = [13, 10, 13, 10];
function takePart(buf) {
  let h = -1;
  for (let i = 0; i + 3 < buf.length; i++) if (buf[i] === 13 && buf[i + 1] === 10 && buf[i + 2] === 13 && buf[i + 3] === 10) { h = i; break; }
  if (h < 0) return null;
  const head = new TextDecoder().decode(buf.subarray(0, h));
  const len = +(/content-length:\s*(\d+)/i.exec(head) || [])[1], type = (/content-type:\s*([^\r\n]+)/i.exec(head) || [])[1] || '';
  if (!(len >= 0) || buf.length < h + CRLF2.length + len) return null;
  const start = h + CRLF2.length;
  return {type, body: buf.slice(start, start + len), rest: buf.slice(start + len + 2)};
}
function cameraFrame(v, part) {
  if (!part.type.startsWith('image/')) { v.note.textContent = new TextDecoder().decode(part.body); v.note.classList.remove('hidden'); v.badge.textContent = 'reconnecting…'; v.badge.classList.remove('on'); return; }
  const url = URL.createObjectURL(new Blob([part.body], {type: part.type}));
  v.img.onload = () => { if (v.old) URL.revokeObjectURL(v.old); v.old = null; };
  v.old = v.url; v.url = url; v.img.src = url;
  const now = performance.now(); v.times = v.times.filter(t => now - t < 2000).concat(now);
  v.badge.textContent = `LIVE · ${Math.round(v.times.length / 2)} fps`; v.badge.classList.add('on'); v.note.classList.add('hidden');
}
async function cameraStream(name) {
  const v = cameraOn[name]; if (!v || v.ctl) return;
  const ctl = v.ctl = new AbortController();
  try {
    const r = await api(`/api/monitor/${encodeURIComponent(name)}/live`, {signal: ctl.signal});
    const reader = r.body.getReader();
    let buf = new Uint8Array(0);
    for (;;) {
      const {value, done} = await reader.read(); if (done) break;
      const more = new Uint8Array(buf.length + value.length); more.set(buf); more.set(value, buf.length); buf = more;
      for (let p; (p = takePart(buf));) { buf = p.rest; cameraFrame(v, p); }
    }
  } catch (e) {
    if (e.name !== 'AbortError') { v.note.textContent = e.message; v.note.classList.remove('hidden'); }
  } finally { if (v.ctl === ctl) v.ctl = null; }
}
// Every open view streams while the Monitor is on screen, and stops (the camera closes soon after) while it isn't.
function cameraStart() {
  for (const [name, v] of Object.entries(cameraOn)) {
    if (cameraShown()) { if (!v.ctl) cameraStream(name); } else if (v.ctl) { v.ctl.abort(); v.ctl = null; v.badge.textContent = 'paused'; v.badge.classList.remove('on'); }
  }
}
setInterval(cameraStart, 2000);  // also restarts a stream that dropped
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $$('.cam-live.expanded').forEach(el => el.classList.remove('expanded')); });
// What's in each AMS, as the printer reports it: colour, type, how full, which tray is feeding.
function amsStrip(p) {
  const a = p.ams || {units: [], trays: []};
  if (!a.trays.length) return p.online ? '<p class="hint">No AMS report from this printer yet. If it stays empty, check its serial number below.</p>' : '';
  const units = (a.units || []).filter(u => u.humidity_pct != null).map(u => `${esc(u.name)} ${u.humidity_pct.toFixed(0)}% RH`).join(' · ');
  return `<div class="ams-strip" aria-label="AMS trays">${a.trays.map(t => t.empty ? `<span class="muted">${esc(t.label)} empty</span>`
    : `<span class="${a.tray_now === t.tray ? 'on' : ''}" title="${esc((t.sub_brand || t.type) + (t.spool ? ' · ' + t.spool.label : '') + (a.tray_now === t.tray ? ' · feeding now' : ''))}">${swatch(t.color)}<b>${esc(t.label)}</b> ${esc(t.sub_brand || t.type)}${t.spool && t.spool.remaining_g != null ? ' · ' + Math.round(t.spool.remaining_g) + ' g' : t.remain_pct != null ? ' · ' + t.remain_pct + '%' : ''}</span>`).join('')}</div>${units ? `<div class="hint">${units}</div>` : ''}`;
}
async function refreshMonitor() {
  try {
    STATE = await json('/api/state');
    const cards = STATE.monitor;
    $('#monitorEmpty').classList.toggle('hidden', cards.length > 0);
    $('#printerCards').innerHTML = cards.map(m => {
      const p = m.printer, n = encodeURIComponent(m.name);
      const chip = m.waiting_for_you ? '<span class="chip bad">waiting for you</span>' : p.online ? '<span class="chip ok">online</span>' : '<span class="chip">offline</span>';
      const lc = m.last_check ? `${m.last_check.clear ? '<span class="chip ok">plate empty</span>' : '<span class="chip bad">parts left</span>'} <span class="muted">${new Date(m.last_check_ts * 1000).toLocaleTimeString()}</span>` : '<span class="muted">no check yet</span>';
      const cam = cameraOn[m.name];
      const lapses = (m.timelapses || []).map(t => `<li><div style="flex:1">${esc(t.job || 'A print')} <span class="muted">${new Date(t.started * 1000).toLocaleString()} · ${t.frames} pictures · ${(t.size / 1e6).toFixed(1)} MB${t.outcome === 'failed' ? ' · failed' : ''}</span></div><button class="btn" data-act="lapse" data-n="${n}" data-id="${esc(t.id)}">View</button><button class="btn" data-act="lapsesave" data-n="${n}" data-id="${esc(t.id)}" data-kind="${esc(t.kind)}">Save</button></li>`).join('');
      const proofs = (m.proofs || []).map(proofThumb).join('');
      return `<div class="card"><div class="spread"><h2>${esc(m.name)}</h2>${chip}</div>
        <p style="margin:0 0 8px">${esc(m.status)}</p>
        ${m.loop_no ? `<div class="phase">${['starting', 'printing', 'cooling, door open', 'bending the plate', 'safety check', 'door closing'].map(ph => `<span class="${(m.phase || '').startsWith(ph.split(' ')[0]) ? 'on' : ''}">${esc(ph.split(',')[0])}</span>`).join('')}</div>` : ''}
        ${amsStrip(p)}
        <dl class="kv">${m.loop_no ? `<dt>Loop</dt><dd><b>${m.loop_no}</b>${m.job_loops ? ' of ' + m.job_loops : ''} · ${esc(m.phase || '—')}</dd>` : ''}${m.spool_swap ? `<dt>Spools</dt><dd><span class="chip warn" title="${esc(m.spool_swap.detail)}">${esc(m.spool_swap.text)}</span></dd>` : ''}<dt>Job</dt><dd>${esc(p.job || '—')}</dd><dt>Progress</dt><dd>${p.percent}% · layer ${p.layer}/${p.total_layers}</dd><dt>Bed</dt><dd>${p.bed_temp.toFixed(0)} °C</dd><dt>Last check</dt><dd>${lc}</dd><dt>Empty-plate pictures</dt><dd>${m.references ? m.references : '<span class="chip warn">none yet</span>'}</dd><dt>This week</dt><dd>${m.stats.checks} loop checks, ${m.stats.resumed} resumed automatically</dd>
          <dt>Measured</dt><dd>${m.timings.cooldown ? `cooldown ${fmtDur(m.timings.cooldown.avg)} avg (${m.timings.cooldown.n})` : 'cooldown not timed yet'}${m.timings.loop ? ` · loop ${fmtDur(m.timings.loop.avg)}` : ''}</dd>
          <dt>Plate bends</dt><dd>${m.bends.value}${m.bends.since ? ` since ${new Date(m.bends.since * 1000).toLocaleDateString()}` : ''} <button class="linkbtn" data-act="bends" data-n="${n}">new plate</button></dd></dl>
        ${m.pending_resume ? '<p><span class="chip info">holding for quiet hours</span></p>' : ''}
        ${p.hms.length ? `<p class="hint" style="color:var(--bad)">HMS: ${p.hms.map(h => esc(h.code + ' (' + h.level + ')')).join(', ')}</p>` : ''}
        <img class="thumb hidden" alt="Camera picture" data-img="${n}">
        ${cam ? `<div data-livecam="${n}"></div>` : ''}
        ${lapses ? `<div class="hint" style="margin-top:10px">Timelapses</div><ul class="list">${lapses}</ul>` : ''}
        ${proofs ? `<div class="hint" style="margin-top:10px">Finished plates</div><div class="proofs">${proofs}</div>` : ''}
        <div class="row" style="margin-top:10px">
          <button class="btn ${cam ? 'primary' : ''}" data-act="cam" data-n="${n}" title="Live video from the printer's camera while it is open">${cam ? 'Hide camera' : 'Live camera'}</button>
          <button class="btn" data-act="snap" data-n="${n}">Snapshot</button>
          <button class="btn" data-act="ref" data-n="${n}" title="Take this with the plate empty">Save empty-plate picture</button>
          <button class="btn ${m.waiting_for_you ? 'primary' : ''}" data-act="check" data-n="${n}">Check plate${m.waiting_for_you ? ' & resume' : ''}</button>
          <button class="btn" data-act="diff" data-n="${n}">Last difference</button>
          <button class="btn" data-act="test" data-n="${n}" title="Log in the way a send does; nothing is printed">Test connection</button>
          ${WATCH_ONLY() ? '' : `<button class="btn ${m.hands_off ? 'primary' : ''}" data-act="handsoff" data-n="${n}" aria-pressed="${!!m.hands_off}" title="Send this printer its next queued job by itself once the plate checks empty">Hands-off: ${m.hands_off ? 'on' : 'off'}</button>`}
          ${m.waiting_for_you ? `<button class="btn danger" data-act="resume" data-n="${n}">Resume without check</button>` : ''}
          <button class="btn danger" data-act="remove" data-n="${n}">Remove</button></div></div>`;
    }).join('');
    for (const [name, v] of Object.entries(cameraOn)) $(`[data-livecam="${encodeURIComponent(name)}"]`)?.replaceWith(v.el);
    const oc = await json('/api/outcomes?limit=25');
    const how = {finished: '<span class="chip ok">finished</span>', failed: '<span class="chip bad">failed</span>', cancelled: '<span class="chip">cancelled</span>', unknown: '<span class="chip">?</span>'};
    $('#outcomes').innerHTML = '<tr><th>Ended</th><th>Printer</th><th>File</th><th>Result</th></tr>' + (oc.length ? '' : '<tr><td colspan="4" class="muted">Nothing yet. Every print the Monitor sees is recorded here.</td></tr>') + oc.map(o => `<tr><td class="num muted">${new Date(o.ts * 1000).toLocaleString()}</td><td>${esc(o.printer)}</td><td>${esc(o.job || '')}${o.loop_file ? ' <span class="muted">loop ' + o.loop_no + '</span>' : ''}</td><td>${how[o.outcome] || esc(o.outcome)}${o.outcome !== 'finished' && o.layer ? ' <span class="muted">at layer ' + o.layer + (o.total_layers ? '/' + o.total_layers : '') + '</span>' : ''}${o.hms && o.hms.length ? ' <span class="muted">HMS ' + o.hms.map(h => esc(h.code)).join(', ') + '</span>' : ''}${o.eject === 'parts_left' ? ' <span class="chip warn">parts left</span>' : ''}${o.has_picture ? ` <button class="btn" data-act="outpic" data-id="${o.id}">Picture</button>` : ''}</td></tr>`).join('');
    await refreshMade().catch(() => {});  // a list that can't load never hides the rest of the page
    const ev = await json('/api/events?limit=40');
    $('#events').innerHTML = '<tr><th>Time</th><th>Printer</th><th>What happened</th></tr>' + (ev.length ? '' : '<tr><td colspan="3" class="muted">Nothing yet. Events appear here as printers connect and loops run.</td></tr>') + ev.map(e => `<tr><td class="num muted">${new Date(e.ts * 1000).toLocaleString()}</td><td>${esc(e.printer || '')}</td><td>${e.level === 'error' ? '<span class="chip bad">!</span> ' : e.level === 'warning' ? '<span class="chip warn">!</span> ' : ''}${esc(e.message)}</td></tr>`).join('');
    const cs = STATE.camera, cf = $('#cameraForm');
    if (cs && !cf.contains(document.activeElement)) { cf.look_every.value = cs.look_every_s ? cs.look_every_s + 's' : cs.look_every_pct; cf.timelapse.checked = cs.timelapse; cf.timelapse_every_layers.value = cs.timelapse_every_layers; cf.timelapse_keep.value = cs.timelapse_keep; cf.proof_photos.checked = cs.proof_photos; cf.proof_keep.value = cs.proof_keep; }
    cameraStart();
    if (document.activeElement !== $('#failureWatch')) $('#failureWatch').value = STATE.failure_watch || 'alert';
    const q = STATE.quiet_hours, qf = $('#quietForm');
    if (q && document.activeElement?.form !== qf) { qf.enabled.checked = q.enabled; qf.start.value = q.start; qf.end.value = q.end; }
    const pa = STATE.print_alerts, af = $('#alertForm');
    if (pa && af && !af.contains(document.activeElement)) { af.finished.checked = pa.finished; af.failed.checked = pa.failed; }
    renderNotify();
    await refreshHa().catch(() => {});  // the export is optional: never let it hide the rest of the page
    renderSend();
  } catch (e) { toast(e.message); }
}
$('#outcomes').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-act="outpic"]'); if (!b) return;
  const r = await api(`/api/outcomes/${b.dataset.id}/picture`);
  if (r.ok) window.open(URL.createObjectURL(await r.blob()), '_blank'); else toast('No picture');
});
// ---------- print again: loop files ProofLoop kept, sent again to a printer you pick (never from a phone) ----------
async function refreshMade() {
  const can = !!(STATE && STATE.can_send) && !WATCH_ONLY();
  $('#histCard').classList.toggle('hidden', !can);
  if (!can) return;
  const rows = await json('/api/made?limit=20');
  $('#history').innerHTML = '<tr><th>File</th><th>Made</th><th>Last printer</th><th>Loops</th><th></th></tr>' +
    (rows.length ? '' : '<tr><td colspan="5" class="muted">Nothing made yet. Loop files you make appear here.</td></tr>') +
    rows.map(h => `<tr><td>${esc(h.file)}</td><td class="num muted">${new Date(h.made * 1000).toLocaleString()}</td>
      <td>${h.printer ? esc(h.printer) : '<span class="muted">not sent</span>'}</td><td class="num">${h.loops ?? ''}</td>
      <td>${h.kept ? `<button class="btn" data-hist="${esc(h.key)}" data-file="${esc(h.file)}" ${STATE.send_to_printer ? '' : 'disabled title="Turn sending on first"'}>Print again</button>`
        : '<span class="muted">file no longer kept</span>'}</td></tr>`).join('');
}
$('#history').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-hist]'); if (!b) return;
  const pick = $('#histPick');
  pick.innerHTML = '<p class="hint">Checking the printers…</p>';
  try {
    const r = await send(`/api/made/${encodeURIComponent(b.dataset.hist)}/printers`, {});
    pick.innerHTML = `<div class="hint" style="margin-top:10px">Print ${esc(b.dataset.file)} again on:</div>` +
      (r.printers.length ? r.printers.map(t => `<div class="send-target${t.ready ? '' : ' off'}" style="margin-top:6px"><div class="spread"><b>${esc(t.printer)}</b>
        ${t.ready ? `<button class="btn primary" data-histgo="${esc(b.dataset.hist)}" data-printer="${esc(t.printer)}" data-file="${esc(b.dataset.file)}">Send and start on ${esc(t.printer)}</button>` : `<span class="chip">${esc(t.state || 'not ready')}</span>`}</div>
        ${t.slots.map(sl => `<span class="chip${sl.tray == null ? ' bad' : ''}">${swatch(sl.color)} ${esc(sl.type)} → ${sl.tray_label ? esc(sl.tray_label) : (t.use_ams ? 'not loaded' : 'external spool')}</span>`).join(' ')}
        ${t.reasons.length ? `<div class="hint">${t.reasons.map(esc).join('<br>')}</div>` : ''}
        ${t.notes.length ? `<div class="hint" style="color:var(--warn)">${t.notes.map(esc).join('<br>')}</div>` : ''}</div>`).join('')
        : '<p class="hint">No printer is set up yet.</p>');
  } catch (err) { pick.innerHTML = ''; toast(err.message); }
});
$('#histPick').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-histgo]'); if (!b) return;
  const printer = b.dataset.printer;
  if (WATCH_ONLY() || !STATE.can_send) return;
  if (STATE.send_confirm !== false && !confirm(`Print ${b.dataset.file} again on ${printer}?\n\nThe plate must be empty. The printer starts as soon as the file arrives.`)) return;
  b.disabled = true; b.textContent = `Sending to ${printer}…`;
  try { const r = await send(`/api/made/${encodeURIComponent(b.dataset.histgo)}/send`, {printer, confirm: printer}); toast(`Started ${r.file} on ${r.printer}`); }
  catch (err) { toast(err.message); }
  $('#histPick').innerHTML = '';
  refreshMade().catch(() => {});
});
$('#printerCards').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-act]'); if (!b) return; const n = b.dataset.n, card = b.closest('.card');
  try {
    if (b.dataset.act === 'cam') { cameraToggle(decodeURIComponent(n)); refreshMonitor(); return; }
    if (b.dataset.act === 'lapse' || b.dataset.act === 'lapsesave') {
      const r = await api(`/api/monitor/${n}/timelapses/${encodeURIComponent(b.dataset.id)}`);
      if (b.dataset.act === 'lapse') window.open(URL.createObjectURL(await r.blob()), '_blank');
      else await saveBlob(r, `${decodeURIComponent(n).replace(/[^\w.-]+/g, '_')}-${b.dataset.id}.${b.dataset.kind}`);
      return;
    }
    if (b.dataset.act === 'snap' || b.dataset.act === 'diff') {
      const r = await api(b.dataset.act === 'snap' ? `/api/monitor/${n}/snapshot` : `/api/monitor/${n}/diff`);
      const img = card.querySelector('[data-img]'); img.src = URL.createObjectURL(await r.blob()); img.classList.remove('hidden'); return;
    }
    if (b.dataset.act === 'ref') { if (!confirm('Is the plate empty right now? This picture is what "empty" looks like.')) return; await json(`/api/monitor/${n}/references`, {method: 'POST'}); toast('Saved. Add a few, with the door open and closed and the light on and off.'); }
    if (b.dataset.act === 'test') {
      b.disabled = true; b.textContent = 'Testing…';
      try { const r = await json(`/api/monitor/${n}/test`, {method: 'POST'});
        const out = card.querySelector('[data-test]') || card.insertAdjacentElement('beforeend', Object.assign(document.createElement('ul'), {className: 'list'}));
        out.dataset.test = '1';
        out.innerHTML = r.steps.map(s => `<li><span class="ic ${s.ok ? 'ok' : 'danger'}">${s.ok ? '✓' : '✖'}</span><div>${esc(s.detail)}</div></li>`).join('');
        toast(r.ok ? 'Connection works: ready to send' : 'Connection test found a problem');
      } finally { b.disabled = false; b.textContent = 'Test connection'; }
      return;
    }
    if (b.dataset.act === 'handsoff') {
      const on = b.getAttribute('aria-pressed') !== 'true', name = decodeURIComponent(n);
      if (on && !confirm(`Let ${name} take its next queued job by itself?\n\nWhen it's free and the camera says the plate is empty, ProofLoop sends the next job in the queue and it starts.` +
        (STATE.send_confirm ? ' You get a notice 2 minutes before, with Hold to stop it.' : ' It starts without asking first (Ask before each send is off).'))) return;
      const r = await send(`/api/monitor/${n}/hands-off`, {on}, 'PUT');
      toast(!r.hands_off ? `${name}: hands-off is off` : r.send_to_printer ? `${name} is hands-off` : `${name} is hands-off, once Sending to printers is turned on`);
    }
    if (b.dataset.act === 'check') { await json(`/api/monitor/${n}/check`, {method: 'POST'}); toast('Checking the plate…'); setTimeout(refreshMonitor, 4000); }
    if (b.dataset.act === 'resume') { if (!confirm('Resume the next loop without a passing plate check?')) return; await json(`/api/monitor/${n}/resume`, {method: 'POST'}); }
    if (b.dataset.act === 'bends') { if (!confirm('Fitted a new build plate? This resets its bend counter.')) return; await json(`/api/monitor/${n}/bends/reset`, {method: 'POST'}); }
    if (b.dataset.act === 'remove') { if (!confirm('Stop monitoring this printer?')) return; await json(`/api/monitor/printers/${n}`, {method: 'DELETE'}); }
    refreshMonitor();
  } catch (err) { toast(err.message); }
});
$('#cameraForm').onsubmit = async (e) => { e.preventDefault(); const f = e.target;
  const look = f.look_every.value, byTime = look.endsWith('s');  // "30s": by time; "10": by layers
  try { await send('/api/camera-settings', {...(byTime ? {look_every_s: parseInt(look)} : {look_every_s: 0, look_every_pct: +look}), timelapse: f.timelapse.checked, timelapse_every_layers: +f.timelapse_every_layers.value || 5, timelapse_keep: +f.timelapse_keep.value || 5, proof_photos: f.proof_photos.checked, proof_keep: +f.proof_keep.value || 500}, 'PUT'); toast('Camera settings saved'); } catch (err) { toast(err.message); } };
document.addEventListener('visibilitychange', () => { if (!document.hidden) cameraStart(); });
$('#addPrinter').onsubmit = async (e) => { e.preventDefault(); const f = e.target;
  try { await send('/api/monitor/printers', {name: f.name.value, model_id: f.model_id.value, host: f.host.value.trim(), serial: f.serial.value.trim(), access_code: f.access_code.value.trim()});
    f.reset(); toast('Printer added. Save a picture of its empty plate next.'); refreshMonitor(); } catch (err) { toast(err.message); } };
// ---------- notifications ----------
let notifyEditing = null;  // the position of the target being changed, or null when adding one
const NOTIFY_NAME = {ntfy: 'ntfy', discord: 'Discord', telegram: 'Telegram', email: 'Email', webhook: 'Webhook'};
const NOTIFY_LEVEL = {error: 'only problems', warning: 'problems and warnings', info: 'everything'};
function notifyShowFields(kind) {
  $$('#notifyForm [data-for]').forEach(d => d.classList.toggle('hidden', !d.dataset.for.split(' ').includes(kind)));
}
function notifyWhere(n) {
  if (n.kind === 'email') return (n.mail_to && n.mail_to.length ? 'to ' + n.mail_to.join(', ') + ' via ' : 'via ') + (n.smtp_host || n.label) + (n.has_smtp_password ? ' · password saved' : '');
  if (n.kind === 'telegram') return 'chat ' + (n.chat_id || '') + (n.has_bot_token ? ' · token saved' : '');
  return (n.url || n.label || '') + (n.has_token ? ' · access token saved' : '');
}
function renderNotify() {
  const list = STATE.notifications || [], canEdit = !!STATE.can_send;
  $('#notifyList').innerHTML = list.length ? list.map((n, i) => `<li><div style="flex:1;min-width:0"><b>${esc(NOTIFY_NAME[n.kind] || n.kind)}</b> <span class="chip">${esc(NOTIFY_LEVEL[n.min_level] || n.min_level)}</span>
      <div class="hint" style="word-break:break-all;margin:2px 0 0">${esc(notifyWhere(n))}</div></div>
      ${canEdit ? `<div class="row" style="flex:none;align-self:center"><button class="btn" data-nact="test" data-i="${i}">Send test</button><button class="btn" data-nact="edit" data-i="${i}">Change</button><button class="btn danger" data-nact="remove" data-i="${i}">Remove</button></div>` : ''}</li>`).join('')
    : '<li class="muted">Nothing set up yet. You won\'t hear about failures when you\'re away.</li>';
  $('#notifyForm').classList.toggle('hidden', !canEdit);
}
function notifyReset() {
  const f = $('#notifyForm'); f.reset(); notifyEditing = null; notifyShowFields(f.kind.value);
  f.bot_token.placeholder = '123456:ABC-DEF...'; f.smtp_password.placeholder = '';
  $('#notifyHead').textContent = 'Add a place to send notices'; $('#notifySave').textContent = 'Add'; $('#notifyCancel').classList.add('hidden');
}
function notifyEdit(i) {
  const n = STATE.notifications[i], f = $('#notifyForm'); f.reset();
  notifyEditing = i; f.kind.value = n.kind; notifyShowFields(n.kind); f.min_level.value = n.min_level;
  for (const name of ['url', 'chat_id', 'smtp_host', 'smtp_user', 'mail_from']) f[name].value = n[name] || '';
  f.smtp_port.value = n.smtp_port || ''; f.smtp_security.value = n.smtp_security || 'starttls'; f.mail_to.value = (n.mail_to || []).join(', ');
  f.bot_token.placeholder = n.has_bot_token ? 'saved: leave empty to keep it' : '123456:ABC-DEF...';
  f.smtp_password.placeholder = n.has_smtp_password ? 'saved: leave empty to keep it' : '';
  $('#notifyHead').textContent = 'Change this place'; $('#notifySave').textContent = 'Save'; $('#notifyCancel').classList.remove('hidden');
  f.scrollIntoView({block: 'nearest'});
}
$('#notifyForm').kind.onchange = (e) => notifyShowFields(e.target.value);
$('#notifyCancel').onclick = notifyReset;
$('#notifyList').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-nact]'); if (!b) return; const i = Number(b.dataset.i);
  try {
    if (b.dataset.nact === 'edit') return notifyEdit(i);
    if (b.dataset.nact === 'remove') {
      if (!confirm('Stop sending notices there?')) return;
      await json('/api/notifications/' + i, {method: 'DELETE'}); notifyReset(); toast('Removed'); refreshMonitor(); return;
    }
    b.disabled = true; b.textContent = 'Sending…';
    try { const r = await json('/api/notifications/' + i + '/test', {method: 'POST'}); toast(r.ok ? r.detail : 'Test failed: ' + r.detail); }
    finally { b.disabled = false; b.textContent = 'Send test'; }
  } catch (err) { toast(err.message); }
});
$('#notifyForm').onsubmit = async (e) => {
  e.preventDefault(); const f = e.target, kind = f.kind.value, body = {kind, min_level: f.min_level.value};
  if (kind === 'telegram') Object.assign(body, {bot_token: f.bot_token.value.trim(), chat_id: f.chat_id.value.trim()});
  else if (kind === 'email') Object.assign(body, {smtp_host: f.smtp_host.value, smtp_port: f.smtp_port.value ? Number(f.smtp_port.value) : null, smtp_security: f.smtp_security.value, smtp_user: f.smtp_user.value, smtp_password: f.smtp_password.value, mail_from: f.mail_from.value, mail_to: f.mail_to.value});
  else body.url = f.url.value.trim();
  try {
    if (notifyEditing === null) await send('/api/notifications', body); else await send('/api/notifications/' + notifyEditing, body, 'PUT');
    toast('Saved. Press Send test to check it.'); notifyReset(); refreshMonitor();
  } catch (err) { toast(err.message); }
};

// ---------- Home Assistant ----------
function haBody(f) {
  return {enabled: f.enabled.checked, host: f.host.value, port: f.port.value ? Number(f.port.value) : null, username: f.username.value, password: f.password.value, tls: f.tls.checked, tls_verify: f.tls_verify.checked, base_topic: f.base_topic.value || 'proofloop', discovery_prefix: f.discovery_prefix.value || 'homeassistant'};
}
async function refreshHa() {
  const h = await json('/api/home-assistant'), f = $('#haForm'), st = h.status || {}, canEdit = !!STATE.can_send;
  f.classList.toggle('hidden', !canEdit);
  $('#haStatus').textContent = st.state === 'connected' ? `Connected. ${st.printers} printer${st.printers === 1 ? '' : 's'} shared.`
    : st.state === 'connecting' ? 'Connecting to the broker…' + (st.error ? ' ' + st.error + '.' : '') : st.state === 'paused' ? '' : 'Off.';
  if (!canEdit || document.activeElement?.form === f) return;
  f.enabled.checked = h.enabled; f.host.value = h.host || ''; f.port.value = h.port || ''; f.username.value = h.username || '';
  f.password.value = ''; f.password.placeholder = h.has_password ? 'saved: leave empty to keep it' : '';
  f.tls.checked = h.tls; f.tls_verify.checked = h.tls_verify; f.base_topic.value = h.base_topic; f.discovery_prefix.value = h.discovery_prefix;
  $('#haVerifyRow').classList.toggle('hidden', !h.tls);
}
$('#haForm').tls.onchange = (e) => $('#haVerifyRow').classList.toggle('hidden', !e.target.checked);
$('#haForm').onsubmit = async (e) => {
  e.preventDefault();
  try { await send('/api/home-assistant', haBody(e.target), 'PUT'); toast('Saved'); e.target.password.value = ''; refreshMonitor(); } catch (err) { toast(err.message); }
};
$('#haTest').onclick = async (e) => {
  const b = e.target; b.disabled = true; b.textContent = 'Testing…';
  try { const r = await send('/api/home-assistant/test', haBody($('#haForm'))); toast(r.detail); } catch (err) { toast(err.message); }
  finally { b.disabled = false; b.textContent = 'Test connection'; }
};
setInterval(() => { if (!liveOn && !$('[data-panel=monitor]').classList.contains('hidden')) refreshMonitor(); }, 5000);  // without live updates

$('#alertForm').addEventListener('change', async () => {
  const f = $('#alertForm');
  try { STATE.print_alerts = await send('/api/print-alerts', {finished: f.finished.checked, failed: f.failed.checked}, 'PUT'); toast('Saved'); }
  catch (e) { toast(e.message); refreshMonitor(); }
});
