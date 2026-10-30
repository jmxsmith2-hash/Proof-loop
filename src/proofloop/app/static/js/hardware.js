// Calibration prints and hardware profiles.
// Part of loop.html; the files load in order and share their top-level names.

// ---------- calibrate ----------
$$('[data-test]').forEach(b => b.onclick = async () => {
  const file = $('#calFile').files[0]; if (!file) { toast('Choose a small sliced plate first'); $('#calFile').focus(); return; }
  const fd = new FormData(); fd.append('file', file); fd.append('test', b.dataset.test); fd.append('profile_id', $('#calProfile').value); fd.append('release_temp', $('#calRelease').value || 30);
  try { const r = await api('/api/calibration', {method: 'POST', body: fd}); toast('Saved ' + await saveBlob(r, 'test.gcode.3mf')); } catch (e) { toast(e.message); }
});
$('#saveFloor').onclick = async () => { const v = +$('#floorResult').value; if (!v) { toast('Enter the lowest height that didn\'t touch the plate'); return; }
  try { STATE.defaults = await send('/api/defaults', {floor_z: v, floor_calibrated: true}, 'PUT'); $('#calBanner').classList.add('hidden'); toast(`Sweep floor saved: Z${v}`); if (PLATE) { $('#sFloor').value = v; reanalyze(); } } catch (e) { toast(e.message); } };

// ---------- profiles ----------
function renderProfiles() {
  $('#profileCards').innerHTML = STATE.profiles.map(p => `<div class="card"><div class="spread"><h2>${esc(p.name)}</h2><span class="chip ${p.builtin ? 'info' : ''}">${p.builtin ? 'built in' : 'imported'}</span></div>
    <p class="muted" style="margin:0 0 8px">${esc(p.printer)} · Stage ${p.stage}${p.stage === 2 ? ' (FarmBoard)' : ' (no electronics)'}</p><p>${esc(p.description)}</p>
    <details data-pid="${esc(p.id)}"><summary>Moves and sources</summary><div class="pbody muted">Loading…</div></details>
    ${p.builtin ? '' : `<div class="row" style="margin-top:10px"><button class="btn danger" data-del="${esc(p.id)}">Delete</button></div>`}</div>`).join('');
}
$('#profileCards').addEventListener('toggle', async (e) => {
  const d = e.target; if (!d.open || d.dataset.loaded) return; d.dataset.loaded = 1;
  const p = await json('/api/profiles/' + encodeURIComponent(d.dataset.pid));
  const blk = (t, g) => g ? `<h3>${t}</h3><pre class="code mono">${esc(g)}</pre>` : '';
  const src = Object.entries(p.source).map(([k, v]) => `<dt>${esc(k)}</dt><dd class="mono" style="word-break:break-all">${/^https?:/.test(v) ? `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a>` : esc(v)}</dd>`).join('');
  d.querySelector('.pbody').innerHTML = `<dl class="kv">${src}</dl>${(p.notes || []).map(n => `<p class="hint">• ${esc(n)}</p>`).join('')}
    <h3>Sweep path</h3><p class="hint">Pushes from Y${p.sweep.y_rear} to Y${p.sweep.y_front}; lanes ${p.sweep.passes.map(x => 'F' + x.feed + ': X' + x.lanes.join(', ')).join(' · ')}; park: ${esc(p.sweep.park.join('; '))}</p>
    ${blk('Start step', p.start_block)}${blk('Door open (T1)', p.door_open)}${blk('Bend', p.bend)}${blk('Safety check', p.safety)}${blk('Door close (T3)', p.door_close)}`;
}, true);
$('#profileCards').addEventListener('click', async (e) => { const b = e.target.closest('[data-del]'); if (!b) return;
  if (!confirm('Delete this imported profile?')) return; await json('/api/profiles/' + encodeURIComponent(b.dataset.del), {method: 'DELETE'}); await loadState(); });
$('#importForm').onsubmit = async (e) => { e.preventDefault(); const out = $('#importOut'); out.innerHTML = '<p>Comparing the files…</p>';
  try { const r = await json('/api/profiles/import', {method: 'POST', body: new FormData(e.target)});
    out.innerHTML = `<div class="banner ok"><span class="ic ok">✓</span><div><b>Saved "${esc(r.profile.name)}".</b> You can now make loop files for this printer.</div></div>
      <h3>Found</h3><ul class="list">${r.found.map(f => `<li><span class="ic ok">✓</span><div>${esc(f)}</div></li>`).join('')}</ul>
      ${r.dropped.length ? `<h3>Left out on purpose</h3><ul class="list">${r.dropped.map(f => `<li><span class="ic info">i</span><div>${esc(f)}</div></li>`).join('')}</ul>` : ''}`;
    e.target.reset(); await loadState(); }
  catch (err) { out.innerHTML = `<div class="banner bad"><span class="ic danger">✖</span><div>${esc(err.message)}</div></div>`; } };
