// Overview: the summary, KPIs, every printer's run, the queue.
// Part of loop.html; the files load in order and share their top-level names.

// ---------- overview ----------
async function refreshSummary() {
  try {
    const s = await json('/api/summary?hours=' + $('#sumHours').value);
    $('#sumHeadline').textContent = s.headline;
    const parts = [];
    if (s.failures.length) parts.push('<ul class="list">' + s.failures.slice(0, 5).map(f => `<li><span class="ic danger">✖</span><div><b>${esc(f.printer)}</b> ${esc(f.outcome)} ${esc(f.job || '')}<div class="hint">${f.layer && f.total_layers ? `layer ${f.layer} of ${f.total_layers}` : ''}${f.hms.length ? ' · HMS ' + esc(f.hms.join(', ')) : ''} · ${new Date(f.when * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}</div></div></li>`).join('') + '</ul>');
    const fil = s.filament, mats = Object.entries(fil.by_material);
    if (mats.length) parts.push(`<p class="hint">Filament used: ${mats.map(([m, g]) => esc(G(g) + ' ' + m)).join(' · ')}</p>`);
    $('#sumBody').innerHTML = parts.join('');
    $('#sumDaily').checked = !!s.at; if (s.at) $('#sumAt').value = s.at;
  } catch (e) { $('#sumHeadline').textContent = e.message; }
}
$('#sumHours').addEventListener('change', refreshSummary);
async function saveSummaryAt() {
  try { const r = await send('/api/summary-at', {at: $('#sumDaily').checked ? $('#sumAt').value : null}, 'PUT');
    toast(r.at ? `The summary will arrive every day at ${r.at}` : 'No daily summary'); } catch (e) { toast(e.message); }
}
$('#sumDaily').addEventListener('change', saveSummaryAt);
$('#sumAt').addEventListener('change', () => { if ($('#sumDaily').checked) saveSummaryAt(); });

async function refreshOverview() {
  refreshQueue();
  refreshSummary();
  try {
    const o = await json('/api/overview'), cur = STATE.costs?.currency || '';
    const att = [];
    o.runs.filter(r => r.waiting_for_you).forEach(r => att.push(`<div class="banner bad"><span class="ic danger">!</span><div><b>${esc(r.printer)} is waiting for you.</b> ${esc(r.status)} <a href="#" data-goto="monitor">Open Monitor</a></div></div>`));
    if (o.filament.unmatched) att.push(`<div class="banner warn"><span class="ic warning">!</span><div>${o.filament.unmatched} loop${o.filament.unmatched > 1 ? 's' : ''} used filament that isn't matched to a spool. <a href="#" data-goto="filament">Match it</a></div></div>`);
    $('#ovAttention').innerHTML = att.join('');
    $('#ovKpis').innerHTML = [['Parts today', o.today.parts], ['Loops today', o.today.loops], ['Parts, 7 days', o.week.parts], ['Parts, 30 days', o.month.parts],
      ['Stopped, 7 days', o.week.failed, o.week.failed ? 'bad' : ''], ['Filament, 7 days', G(o.filament.used_g['7']), '', o.filament.used_cost['7'] ? cur + o.filament.used_cost['7'].toFixed(2) + ' worth' : '']]
      .map(([k, v, c, sub]) => `<div class="kpi ${c || ''}"><small>${esc(k)}</small><b>${esc(v)}</b>${sub ? `<span class="kpi-sub">${esc(sub)}</span>` : ''}</div>`).join('');
    $('#ovRuns').innerHTML = o.runs.length ? o.runs.map(r => {
      const pct = r.loops ? Math.min(100, (Math.max(0, r.loop_no - 1) / r.loops) * 100) : 0;
      return `<div class="run"><div class="spread"><b>${esc(r.printer)}</b>${r.waiting_for_you ? '<span class="chip bad">waiting for you</span>' : r.online ? '<span class="chip ok">online</span>' : '<span class="chip">offline</span>'}</div>
        <div class="hint">${esc(r.status)}${r.phase ? ' · ' + esc(r.phase) : ''}</div>
        ${r.loops ? `<div class="meter"><i style="width:${pct.toFixed(1)}%"></i></div><div class="spread"><span>Loop <b>${r.loop_no}</b> of ${r.loops}</span><span class="hint">${r.eta ? 'done around <b>' + new Date(r.eta * 1000).toLocaleString([], {weekday: 'short', hour: '2-digit', minute: '2-digit'}) + '</b> (' + esc(r.eta_basis) + ')' : 'finish time once a loop has been timed'}</span></div>` : r.job ? `<div class="hint">${esc(r.job)}</div>` : ''}</div>`;
    }).join('') : '<p class="hint">No printers yet. Add one in <a href="#" data-goto="monitor">Monitor</a> to see live runs here.</p>';
    const W = 600, H = 140, bw = W / o.daily.length, max = Math.max(1, ...o.daily.map(d => d.parts));
    $('#ovChart').innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Parts made per day"><line x1="0" x2="${W}" y1="${H - 18}" y2="${H - 18}" stroke="currentColor" opacity=".15"/>` +
      o.daily.map((d, i) => { const h = d.parts / max * (H - 32); return `<rect x="${(i * bw + 2).toFixed(1)}" y="${(H - 18 - h).toFixed(1)}" width="${(bw - 4).toFixed(1)}" height="${h.toFixed(1)}" rx="2" fill="var(--accent)"><title>${new Date(d.day * 1000).toLocaleDateString()}: ${d.parts} parts, ${d.loops} loops</title></rect>${d.parts ? `<text x="${(i * bw + bw / 2).toFixed(1)}" y="${(H - 22 - h).toFixed(1)}" font-size="10" text-anchor="middle" fill="currentColor">${d.parts}</text>` : ''}`; }).join('') +
      o.daily.map((d, i) => i % 2 === 0 ? `<text x="${(i * bw + bw / 2).toFixed(1)}" y="${H - 4}" font-size="10" text-anchor="middle" fill="currentColor" opacity=".6">${new Date(d.day * 1000).toLocaleDateString([], {month: 'short', day: 'numeric'})}</text>` : '').join('') + '</svg>';
    const pp = Object.entries(o.parts_per_printer_7d);
    $('#ovPerPrinter').textContent = pp.length ? 'Last 7 days: ' + pp.map(([k, v]) => `${k} ${v}`).join(' · ') : 'Parts are counted from finished loops of files made by this ProofLoop.';
    $('#ovLow').innerHTML = o.low_spools.length ? '<ul class="list">' + o.low_spools.map(s => `<li>${swatch(s.color)}<div><b>${esc(s.label)}</b> <span class="hint">${s.status === 'empty' ? 'used up' : G(s.remaining_g) + ' left'}${s.printer ? ' · in ' + esc(s.printer) + ' ' + trayLabel(s.tray) : ''}</span></div></li>`).join('') + '</ul>' : '<p class="hint">None. You\'ll see spools here when they drop below their warning weight.</p>';
    $('#ovEvents').innerHTML = o.events.map(e => `<li><span class="ic ${e.level === 'error' ? 'danger' : e.level === 'warning' ? 'warning' : 'info'}">${e.level === 'info' ? 'i' : '!'}</span><div>${esc(e.message)}<div class="hint">${esc(e.printer || '')} · ${new Date(e.ts * 1000).toLocaleString()}</div></div></li>`).join('') || '<li class="hint">Nothing yet.</li>';
  } catch (e) { toast(e.message); }
}
setInterval(() => { if (!liveOn && !$('[data-panel=overview]').classList.contains('hidden')) refreshOverview(); }, 10000);  // without live updates
