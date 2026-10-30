// Finding printers on the network, and quiet hours.
// Part of loop.html; the files load in order and share their top-level names.

// ---------- monitor: discovery and quiet hours ----------
$('#findPrinters').onclick = async () => {
  const out = $('#foundPrinters'); out.innerHTML = '<p class="hint">Listening for printers on your network (about 6 s)…</p>';
  try {
    const found = await json('/api/discover');
    out.innerHTML = found.length ? `<ul class="list">${found.map((f, i) => `<li><div style="flex:1"><b>${esc(f.name)}</b><div class="hint">${esc(f.model || '?')} · ${esc(f.host)} · ${esc(f.serial)}</div></div>${f.added ? '<span class="chip ok">added</span>' : `<button class="btn" data-use="${i}">Use</button>`}</li>`).join('')}</ul>`
      : '<p class="hint">No printers answered. Check that this computer is on the same network, or type the details below.</p>';
    out.querySelectorAll('[data-use]').forEach(b => b.onclick = () => { const f = found[+b.dataset.use], form = $('#addPrinter');
      form.name.value = f.name; form.host.value = f.host; form.serial.value = f.serial; if (STATE.printers[f.model_id]) form.model_id.value = f.model_id;
      form.access_code.focus(); toast('Now enter the access code from the printer screen'); });
  } catch (e) { out.innerHTML = `<p class="hint" style="color:var(--bad)">${esc(e.message)}</p>`; }
};
$('#failureWatch').addEventListener('change', async (e) => {
  try { const r = await send('/api/failure-watch', {mode: e.target.value}, 'PUT');
    toast({alert: 'The failure watch will tell you', pause: 'The failure watch will pause the printer and tell you', off: 'Failure watch off'}[r.mode]); }
  catch (err) { toast(err.message); }
});
$('#quietForm').onsubmit = async (e) => { e.preventDefault(); const f = e.target;
  try { await send('/api/quiet-hours', {enabled: f.enabled.checked, start: f.start.value || '22:00', end: f.end.value || '07:00'}, 'PUT'); toast('Quiet hours saved'); } catch (err) { toast(err.message); } };
