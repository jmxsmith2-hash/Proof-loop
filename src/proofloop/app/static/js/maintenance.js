// Maintenance: what each printer has used since each task was last done, and the tasks themselves.
// Part of loop.html; the files load in order and share their top-level names.

// ---------- maintenance ----------
const MAINT_UNITS = {hours: ['print hour', 'print hours'], prints: ['print', 'prints'], loops: ['loop', 'loops'], grams: ['g of filament', 'g of filament'], days: ['day', 'days']};
const MAINT_CHIP = {ok: 'ok', soon: 'info', due: 'warn', overdue: 'bad'};
let MAINT = null, maintOpen = null;  // maintOpen: "printer|task" while its Done form is open
const maintAmount = (unit, n) => `${n} ${MAINT_UNITS[unit][n === 1 ? 0 : 1]}`;
const maintDay = (ts) => new Date(ts * 1000).toLocaleDateString([], {month: 'short', day: 'numeric', year: 'numeric'});
const maintToday = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };

function maintTask(p, i) {
  const key = p.printer + '|' + i.task;
  const label = i.state === 'overdue' ? 'overdue' : i.state === 'due' ? 'due now' : maintAmount(i.unit, i.left) + ' left';
  const since = i.last_done ? `done ${maintDay(i.last_done)}${i.note ? ' · ' + esc(i.note) : ''}` : `not marked done yet · counting from ${maintDay(i.since)}`;
  const form = maintOpen === key ? `<form class="row" data-mform="${esc(key)}" style="margin-top:6px">
      <label class="field" style="width:150px"><span>Done on</span><input type="date" name="date" value="${maintToday()}" max="${maintToday()}" required></label>
      <label class="field" style="flex:1;min-width:150px"><span>Note</span><input type="text" name="note" maxlength="200" placeholder="optional"></label>
      <button class="btn primary">Save</button> <button type="button" class="linkbtn" data-mcancel>Cancel</button></form>` : '';
  return `<div class="run"><div class="spread"><b>${esc(i.name)}</b><span class="chip ${MAINT_CHIP[i.state]}">${label}</span></div>
    <div class="meter ${i.state === 'overdue' ? 'bad' : i.state === 'due' || i.state === 'soon' ? 'warn' : ''}"><i style="width:${Math.min(100, i.pct)}%"></i></div>
    <div class="spread"><span class="hint"><b>${maintAmount(i.unit, i.used)}</b> of ${maintAmount(i.unit, i.every)}</span><span class="hint">${since}</span></div>
    <div class="row" style="margin-top:6px">${maintOpen === key ? '' : `<button class="btn" data-mdone="${esc(key)}">Done</button>`}${i.hint ? `<span class="hint">${esc(i.hint)}</span>` : ''}</div>${form}</div>`;
}

function maintEditor(tasks) {
  return tasks.map(t => {
    const changed = t.default && (t.default.every !== t.every || t.default.unit !== t.unit);
    const units = Object.keys(MAINT_UNITS).map(u => `<option value="${u}" ${u === t.unit ? 'selected' : ''}>${MAINT_UNITS[u][1]}</option>`).join('');
    return `<div class="list-ed two" data-mtask="${esc(t.id)}" style="grid-template-columns:auto minmax(0,2fr) minmax(0,.7fr) minmax(0,1fr) auto">
      <input type="checkbox" data-mset="enabled" ${t.enabled ? 'checked' : ''} aria-label="Track ${esc(t.name)}">
      <span>${esc(t.name)}${t.source ? ' <span class="chip info" title="Bambu Lab\'s wiki (X1 series) gives this interval">Bambu wiki</span>' : ''}</span>
      <input type="number" data-mset="every" value="${t.every}" min="1" step="any" aria-label="Every">
      <select data-mset="unit" aria-label="Unit">${units}</select>
      ${t.builtin ? (changed ? '<button class="linkbtn" data-mreset>Reset</button>' : '<span></span>') : '<button class="linkbtn" data-mremove>Remove</button>'}</div>`;
  }).join('');
}

function maintDraw() {
  const m = MAINT;
  $('#maintRemind').checked = m.remind;
  $('#maintEdit').innerHTML = maintEditor(m.tasks);
  $('#maintOut').innerHTML = m.printers.length ? m.printers.map(p => `<div class="card">
      <div class="spread"><h2>${esc(p.printer)}</h2><span class="chip ${p.due ? 'warn' : 'ok'}">${p.due ? p.due + ' due' : 'all up to date'}</span></div>
      ${p.tasks.map(i => maintTask(p, i)).join('') || '<p class="hint">Every task is switched off.</p>'}</div>`).join('')
    : '<div class="card"><p class="hint">No printers yet. Add one under <a href="#" data-goto="monitor">Monitor</a>.</p></div>';
}
async function refreshMaintenance() {
  try { MAINT = await json('/api/maintenance'); } catch (e) { return; }
  if (!maintOpen) maintDraw();  // with a Done form open, leave what is being typed
}

$('#maintOut').addEventListener('click', (e) => {
  const d = e.target.closest('[data-mdone]');
  if (d) { maintOpen = d.dataset.mdone; maintDraw(); return; }
  if (e.target.closest('[data-mcancel]')) { maintOpen = null; maintDraw(); }
});
$('#maintOut').addEventListener('submit', async (e) => {
  e.preventDefault();
  const [printer, task] = e.target.dataset.mform.split('|'), f = e.target;
  try {
    await send('/api/maintenance/done', {printer, task, date: f.date.value, note: f.note.value});
    toast('Marked done');
  } catch (err) { toast(err.message); return; }
  maintOpen = null; refreshMaintenance();
});
$('#maintEdit').addEventListener('change', async (e) => {
  const el = e.target.closest('[data-mset]'), row = e.target.closest('[data-mtask]'); if (!el || !row) return;
  const field = el.dataset.mset;
  try { await send('/api/maintenance/tasks/' + row.dataset.mtask, {[field]: field === 'enabled' ? el.checked : field === 'every' ? +el.value : el.value}, 'PATCH'); }
  catch (err) { toast(err.message); }
  refreshMaintenance();
});
$('#maintEdit').addEventListener('click', async (e) => {
  const row = e.target.closest('[data-mtask]'); if (!row) return;
  if (e.target.closest('[data-mremove]') && !confirm('Remove this task?')) return;
  if (!e.target.closest('[data-mremove]') && !e.target.closest('[data-mreset]')) return;
  try { await json('/api/maintenance/tasks/' + row.dataset.mtask, {method: 'DELETE'}); } catch (err) { toast(err.message); }
  refreshMaintenance();
});
$('#maintAdd').onsubmit = async (e) => {
  e.preventDefault(); const f = e.target;
  try { await send('/api/maintenance/tasks', {name: f.name.value, every: +f.every.value, unit: f.unit.value}); f.reset(); }
  catch (err) { toast(err.message); }
  refreshMaintenance();
};
$('#maintRemind').addEventListener('change', async (e) => {
  try { await send('/api/maintenance', {remind: e.target.checked}, 'PATCH'); } catch (err) { toast(err.message); }
});
setInterval(() => {
  if (!$('[data-panel=maintenance]').classList.contains('hidden') && !$('#maintEdit').contains(document.activeElement)) refreshMaintenance();
}, 60000);
