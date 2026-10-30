// Monitor: each printer, the plate check, print history and events.
// Part of loop.html; the files load in order and share their top-level names.

// ---------- monitor ----------
async function refreshMonitor() {
  try {
    STATE = await json('/api/state');
    const cards = STATE.monitor;
    $('#monitorEmpty').classList.toggle('hidden', cards.length > 0);
    $('#printerCards').innerHTML = cards.map(m => {
      const p = m.printer, n = encodeURIComponent(m.name);
      const chip = m.waiting_for_you ? '<span class="chip bad">waiting for you</span>' : p.online ? '<span class="chip ok">online</span>' : '<span class="chip">offline</span>';
      const lc = m.last_check ? `${m.last_check.clear ? '<span class="chip ok">plate empty</span>' : '<span class="chip bad">parts left</span>'} <span class="muted">${new Date(m.last_check_ts * 1000).toLocaleTimeString()}</span>` : '<span class="muted">no check yet</span>';
      return `<div class="card"><div class="spread"><h2>${esc(m.name)}</h2>${chip}</div>
        <p style="margin:0 0 8px">${esc(m.status)}</p>
        ${m.loop_no ? `<div class="phase">${['starting', 'printing', 'cooling, door open', 'bending the plate', 'safety check', 'door closing'].map(ph => `<span class="${(m.phase || '').startsWith(ph.split(' ')[0]) ? 'on' : ''}">${esc(ph.split(',')[0])}</span>`).join('')}</div>` : ''}
        ${p.ams && p.ams.trays.length ? `<div class="ams-strip">${p.ams.trays.map(t => t.empty ? `<span class="muted">${esc(t.label)} empty</span>` : `<span class="${p.ams.tray_now === t.tray ? 'on' : ''}" title="${esc((t.sub_brand || t.type) + (t.spool ? ' · ' + t.spool.label : ''))}">${swatch(t.color)}${esc(t.label)} ${esc(t.type)}${t.spool && t.spool.remaining_g != null ? ' · ' + Math.round(t.spool.remaining_g) + ' g' : t.remain_pct != null ? ' · ' + t.remain_pct + '%' : ''}</span>`).join('')}</div>` : ''}
        <dl class="kv">${m.loop_no ? `<dt>Loop</dt><dd><b>${m.loop_no}</b>${m.job_loops ? ' of ' + m.job_loops : ''} · ${esc(m.phase || '—')}</dd>` : ''}<dt>Job</dt><dd>${esc(p.job || '—')}</dd><dt>Progress</dt><dd>${p.percent}% · layer ${p.layer}/${p.total_layers}</dd><dt>Bed</dt><dd>${p.bed_temp.toFixed(0)} °C</dd><dt>Last check</dt><dd>${lc}</dd><dt>Empty-plate pictures</dt><dd>${m.references ? m.references : '<span class="chip warn">none yet</span>'}</dd><dt>This week</dt><dd>${m.stats.checks} loop checks, ${m.stats.resumed} resumed automatically</dd>
          <dt>Measured</dt><dd>${m.timings.cooldown ? `cooldown ${fmtDur(m.timings.cooldown.avg)} avg (${m.timings.cooldown.n})` : 'cooldown not timed yet'}${m.timings.loop ? ` · loop ${fmtDur(m.timings.loop.avg)}` : ''}</dd>
          <dt>Plate bends</dt><dd>${m.bends.value}${m.bends.since ? ` since ${new Date(m.bends.since * 1000).toLocaleDateString()}` : ''} <button class="linkbtn" data-act="bends" data-n="${n}">new plate</button></dd></dl>
        ${m.pending_resume ? '<p><span class="chip info">holding for quiet hours</span></p>' : ''}
        ${p.hms.length ? `<p class="hint" style="color:var(--bad)">HMS: ${p.hms.map(h => esc(h.code + ' (' + h.level + ')')).join(', ')}</p>` : ''}
        <img class="thumb hidden" alt="Camera picture" data-img="${n}">
        <div class="row" style="margin-top:10px">
          <button class="btn" data-act="snap" data-n="${n}">Camera</button>
          <button class="btn" data-act="ref" data-n="${n}" title="Take this with the plate empty">Save empty-plate picture</button>
          <button class="btn ${m.waiting_for_you ? 'primary' : ''}" data-act="check" data-n="${n}">Check plate${m.waiting_for_you ? ' & resume' : ''}</button>
          <button class="btn" data-act="diff" data-n="${n}">Last difference</button>
          <button class="btn" data-act="test" data-n="${n}" title="Log in the way a send does; nothing is printed">Test connection</button>
          ${m.waiting_for_you ? `<button class="btn danger" data-act="resume" data-n="${n}">Resume without check</button>` : ''}
          <button class="btn danger" data-act="remove" data-n="${n}">Remove</button></div></div>`;
    }).join('');
    const oc = await json('/api/outcomes?limit=25');
    const how = {finished: '<span class="chip ok">finished</span>', failed: '<span class="chip bad">failed</span>', cancelled: '<span class="chip">cancelled</span>', unknown: '<span class="chip">?</span>'};
    $('#outcomes').innerHTML = '<tr><th>Ended</th><th>Printer</th><th>File</th><th>Result</th></tr>' + (oc.length ? '' : '<tr><td colspan="4" class="muted">Nothing yet. Every print the Monitor sees is recorded here.</td></tr>') + oc.map(o => `<tr><td class="num muted">${new Date(o.ts * 1000).toLocaleString()}</td><td>${esc(o.printer)}</td><td>${esc(o.job || '')}${o.loop_file ? ' <span class="muted">loop ' + o.loop_no + '</span>' : ''}</td><td>${how[o.outcome] || esc(o.outcome)}${o.outcome !== 'finished' && o.layer ? ' <span class="muted">at layer ' + o.layer + (o.total_layers ? '/' + o.total_layers : '') + '</span>' : ''}${o.hms && o.hms.length ? ' <span class="muted">HMS ' + o.hms.map(h => esc(h.code)).join(', ') + '</span>' : ''}${o.eject === 'parts_left' ? ' <span class="chip warn">parts left</span>' : ''}${o.has_picture ? ` <button class="btn" data-act="outpic" data-id="${o.id}">Picture</button>` : ''}</td></tr>`).join('');
    const ev = await json('/api/events?limit=40');
    $('#events').innerHTML = '<tr><th>Time</th><th>Printer</th><th>What happened</th></tr>' + (ev.length ? '' : '<tr><td colspan="3" class="muted">Nothing yet. Events appear here as printers connect and loops run.</td></tr>') + ev.map(e => `<tr><td class="num muted">${new Date(e.ts * 1000).toLocaleString()}</td><td>${esc(e.printer || '')}</td><td>${e.level === 'error' ? '<span class="chip bad">!</span> ' : e.level === 'warning' ? '<span class="chip warn">!</span> ' : ''}${esc(e.message)}</td></tr>`).join('');
    if (document.activeElement !== $('#failureWatch')) $('#failureWatch').value = STATE.failure_watch || 'alert';
    const q = STATE.quiet_hours, qf = $('#quietForm');
    if (q && document.activeElement?.form !== qf) { qf.enabled.checked = q.enabled; qf.start.value = q.start; qf.end.value = q.end; }
    const nt = STATE.notifications[0];
    if (nt) { const f = $('#notifyForm'); f.kind.value = nt.kind; f.url.value = nt.url; f.min_level.value = nt.min_level; }
    renderSend();
  } catch (e) { toast(e.message); }
}
$('#outcomes').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-act="outpic"]'); if (!b) return;
  const r = await api(`/api/outcomes/${b.dataset.id}/picture`);
  if (r.ok) window.open(URL.createObjectURL(await r.blob()), '_blank'); else toast('No picture');
});
$('#printerCards').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-act]'); if (!b) return; const n = b.dataset.n, card = b.closest('.card');
  try {
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
    if (b.dataset.act === 'check') { await json(`/api/monitor/${n}/check`, {method: 'POST'}); toast('Checking the plate…'); setTimeout(refreshMonitor, 4000); }
    if (b.dataset.act === 'resume') { if (!confirm('Resume the next loop without a passing plate check?')) return; await json(`/api/monitor/${n}/resume`, {method: 'POST'}); }
    if (b.dataset.act === 'bends') { if (!confirm('Fitted a new build plate? This resets its bend counter.')) return; await json(`/api/monitor/${n}/bends/reset`, {method: 'POST'}); }
    if (b.dataset.act === 'remove') { if (!confirm('Stop monitoring this printer?')) return; await json(`/api/monitor/printers/${n}`, {method: 'DELETE'}); }
    refreshMonitor();
  } catch (err) { toast(err.message); }
});
$('#addPrinter').onsubmit = async (e) => { e.preventDefault(); const f = e.target;
  try { await send('/api/monitor/printers', {name: f.name.value, model_id: f.model_id.value, host: f.host.value.trim(), serial: f.serial.value.trim(), access_code: f.access_code.value.trim()});
    f.reset(); toast('Printer added. Save a picture of its empty plate next.'); refreshMonitor(); } catch (err) { toast(err.message); } };
$('#notifyForm').onsubmit = async (e) => { e.preventDefault(); const f = e.target;
  try { await send('/api/notifications', f.url.value ? [{kind: f.kind.value, url: f.url.value, min_level: f.min_level.value}] : [], 'PUT'); toast('Notifications saved'); } catch (err) { toast(err.message); } };
$('#notifyClear').onclick = async () => { await send('/api/notifications', [], 'PUT'); $('#notifyForm').reset(); toast('Notifications off'); };
setInterval(() => { if (!liveOn && !$('[data-panel=monitor]').classList.contains('hidden')) refreshMonitor(); }, 5000);  // without live updates
