// Costs: what a plate costs and earns, from your own prices.
// Part of loop.html; the files load in order and share their top-level names.

// ---------- costs ----------
let COSTS = null, COST = null, costTimer = null, costSaveTimer = null, coolMin = null;
try { const v = localStorage.getItem('proofloop-cool-min'); coolMin = v === null ? null : +v; } catch (e) {}
const PALETTE = ['#1d6fe0', '#16834a', '#c77d00', '#7b4ddb', '#c0312b', '#0f8f9a', '#d64b8a', '#5d6774', '#8a6d3b', '#2f9e44', '#e8590c'];
const money = (v, dp = 2) => v == null ? '—' : (v < 0 ? '−' : '') + (COSTS?.currency || '') + (Math.abs(v) < 1 && v !== 0 && dp < 3 ? Math.abs(v).toFixed(3) : Math.abs(v).toFixed(dp));
function anyCostSet() { const c = COSTS || {}; return (STATE?.spools || []).length || c.electricity_per_kwh != null || c.printer_price != null || c.labor_rate != null || c.price_per_part != null; }

function bindCosts() {
  $$('[data-c]').forEach(inp => {
    const k = inp.dataset.c, v = COSTS[k];
    if (document.activeElement !== inp) inp.value = v == null ? '' : v;
    if (inp.dataset.bound) return; inp.dataset.bound = 1;
    inp.addEventListener('input', () => {
      const raw = inp.value.trim(), kind = inp.dataset.kind;
      COSTS[k] = kind === 'text' ? raw : raw === '' ? (kind === 'zero' ? 0 : kind === 'one' ? 1 : null) : +raw;
      saveCostsSoon();
    });
  });
  $$('.cur').forEach(e => e.textContent = COSTS.currency || '');
  renderExtras();
}
function renderExtras() {
  [['extras_per_part', '#extraPartRows'], ['extras_per_order', '#extraOrderRows']].forEach(([k, el]) => {
    $(el).innerHTML = (COSTS[k] || []).map((x, i) => `<div class="list-ed two">
      <input type="text" data-ex="${k}" data-i="${i}" data-f="name" value="${esc(x.name)}" placeholder="what" aria-label="Extra">
      <input type="number" data-ex="${k}" data-i="${i}" data-f="cost" value="${x.cost}" min="0" step="0.01" aria-label="Cost">
      <button class="linkbtn" data-rmex="${k}" data-i="${i}" aria-label="Remove">✕</button></div>`).join('');
  });
}
document.addEventListener('input', (e) => {
  const t = e.target;
  if (t.dataset.ex) { const x = COSTS[t.dataset.ex][+t.dataset.i]; x[t.dataset.f] = t.dataset.f === 'cost' ? +t.value : t.value; saveCostsSoon(); }
});
document.addEventListener('click', (e) => {
  const t = e.target;
  if (t.dataset.rmex) { COSTS[t.dataset.rmex].splice(+t.dataset.i, 1); renderExtras(); saveCostsSoon(); }
  if (t.dataset.addextra) { COSTS[t.dataset.addextra].push({name: '', cost: 0}); renderExtras(); }
});
function saveCostsSoon() {
  clearTimeout(costSaveTimer); $('#costSaved').textContent = '';
  costSaveTimer = setTimeout(async () => {
    try { const clean = {...COSTS, spools: [], slot_spool: {}, extras_per_part: COSTS.extras_per_part.filter(x => x.name), extras_per_order: COSTS.extras_per_order.filter(x => x.name)};
      await send('/api/costs', clean, 'PUT'); $('#costSaved').textContent = '✓ saved'; refreshCost(); }
    catch (e) { $('#costSaved').textContent = ''; toast(e.message); }
  }, 600);
}

function coolOptions() {
  const sel = $('#cCoolSrc'), prev = sel.value;
  const measured = (STATE.monitor || []).filter(m => m.timings && (m.timings.cooldown || m.timings.cycle));
  sel.innerHTML = measured.map(m => `<option value="p:${esc(m.name)}">Measured on ${esc(m.name)}</option>`).join('') +
    '<option value="est">My estimate</option><option value="none">Unknown</option>';
  sel.value = [...sel.options].some(o => o.value === prev) ? prev : measured.length ? sel.options[0].value : coolMin != null ? 'est' : 'none';
  $('#cCoolMinWrap').classList.toggle('hidden', sel.value !== 'est');
  if (coolMin != null) $('#cCoolMin').value = coolMin;
}
['#cLoops', '#cCoolSrc', '#cCoolMin'].forEach(id => $(id).addEventListener('input', () => {
  if (id === '#cCoolMin') { coolMin = $('#cCoolMin').value === '' ? null : +$('#cCoolMin').value; try { localStorage.setItem('proofloop-cool-min', coolMin ?? ''); } catch (e) {} }
  $('#cCoolMinWrap').classList.toggle('hidden', $('#cCoolSrc').value !== 'est'); refreshCost();
}));
$('#cPrice').addEventListener('input', () => { const v = $('#cPrice').value; COSTS.price_per_part = v === '' ? null : +v; $('#cPriceRange').value = v || 0; saveCostsSoon(); });
$('#cPriceRange').addEventListener('input', () => { $('#cPrice').value = $('#cPriceRange').value; COSTS.price_per_part = +$('#cPriceRange').value; saveCostsSoon(); });

function costRequest() {
  const src = $('#cCoolSrc').value || 'none';
  return {settings: currentSettings(), loops: +$('#cLoops').value || +$('#sLoops').value || 1, printer: $('#fPrinter').value || null,
    cooldown_printer: src.startsWith('p:') ? src.slice(2) : null, cooldown_min: src === 'est' ? coolMin : null};
}
function refreshCost() {
  clearTimeout(costTimer);
  costTimer = setTimeout(async () => {
    if (!PLATE || !COSTS) { $('#costEmpty').classList.remove('hidden'); ['#costPlate', '#costSelling', '#costCapacity'].forEach(id => $(id).classList.add('hidden')); return; }
    try { COST = await send(`/api/plates/${PLATE.token}/cost`, costRequest()); renderCost(); } catch (e) { toast(e.message); }
  }, 250);
}
function renderCost() {
  const r = COST, t = r.totals, sell = r.selling;
  $('#costEmpty').classList.add('hidden'); $('#costPlate').classList.remove('hidden'); $('#costSelling').classList.remove('hidden'); $('#costCapacity').classList.remove('hidden');
  $('#costTitle').textContent = PLATE.file;
  $('#costChip').innerHTML = t.complete ? '<span class="chip ok">complete</span>' : `<span class="chip warn">${r.missing.length} missing</span>`;
  const kp = [['Per part', money(t.per_part, 3)], ['Per loop', money(t.per_loop)], [`Whole run (${r.loops} loop${r.loops > 1 ? 's' : ''}, ${t.parts} parts)`, money(t.run)], ['Per printer hour', money(t.per_hour)]];
  if (sell) kp.push(['Profit per part', money(sell.profit, 3), sell.profit >= 0 ? 'good' : 'bad'], ['Margin', sell.margin_pct == null ? '—' : sell.margin_pct.toFixed(1) + '%', sell.profit >= 0 ? 'good' : 'bad']);
  $('#costKpis').innerHTML = kp.map(([k, v, c]) => `<div class="kpi ${c || ''}"><small>${esc(k)}</small><b>${esc(v)}</b></div>`).join('');
  const lines = r.lines.filter(l => l.per_loop), sum = lines.reduce((a, l) => a + l.per_loop, 0) || 1;
  $('#costStack').innerHTML = lines.map((l, i) => `<i style="width:${(l.per_loop / sum * 100).toFixed(2)}%;background:${PALETTE[i % PALETTE.length]}" title="${esc(l.label)}"></i>`).join('');
  $('#costLegend').innerHTML = lines.map((l, i) => `<span><i style="background:${PALETTE[i % PALETTE.length]}"></i>${esc(l.label)} ${(l.per_loop / sum * 100).toFixed(0)}%</span>`).join('');
  const per = r.parts_per_plate || 1;
  $('#costTable').innerHTML = '<tr><th>Cost</th><th>Per loop</th><th>Per part</th><th>Share</th><th>How</th></tr>' + r.lines.map(l => `<tr>
    <td>${esc(l.label)}</td><td class="num">${l.per_loop == null ? '<span class="chip warn">not set</span>' : money(l.per_loop, 3)}</td>
    <td class="num">${l.per_loop == null ? '—' : money(l.per_loop / per, 3)}</td><td class="num">${l.per_loop ? (l.per_loop / sum * 100).toFixed(1) + '%' : '—'}</td>
    <td class="hint">${esc(l.missing || l.how)}</td></tr>`).join('') +
    (r.setup ? `<tr><td>Setup, once per run</td><td class="num">—</td><td class="num">${money(r.setup / t.parts, 3)}</td><td></td><td class="hint">${money(r.setup)} spread over ${t.parts} parts</td></tr>` : '');
  $('#costMaterials').innerHTML = '<tr><th>Slot</th><th>Filament</th><th>Grams</th><th>Spool</th><th>Per kg</th><th>Cost</th></tr>' + r.materials.map(m => `<tr>
    <td>${esc(m.slot)}</td><td><span class="sw" style="background:${esc(m.color)}"></span>${esc(m.type)} <span class="hint">${esc(m.color)}</span></td><td class="num">${m.grams} g</td>
    <td><select data-slotkey="${esc(m.key)}" aria-label="Spool for slot ${esc(m.slot)}">${spoolOptions(m.type, m.spool, '— choose —')}</select>${!m.spool && m.spool_name ? `<div class="hint">priced from ${esc(m.spool_name)}</div>` : ''}</td>
    <td class="num">${m.per_kg != null ? money(m.per_kg) : '—'}</td><td class="num">${m.cost != null ? money(m.cost, 3) : '<span class="chip warn">not set</span>'}</td></tr>`).join('');
  $('#costMissing').innerHTML = r.missing.map(m => `<li><span class="ic warning">!</span><div>${esc(m)}</div></li>`).join('');
  const tm = r.times;
  $('#costTimes').textContent = `Loop time ${fmtDur(tm.loop_s)}: print ${fmtDur(tm.print_s)} (slicer estimate), FarmBoard waits and sweep ${fmtDur(tm.fixed_s)}, cooldown ${tm.cooldown_s == null ? 'unknown' : fmtDur(tm.cooldown_s) + ' (' + tm.cooldown_source + ')'}.`;
  // selling
  if (document.activeElement !== $('#cPrice')) $('#cPrice').value = COSTS.price_per_part ?? '';
  $('#cPriceRange').max = Math.max(20, Math.ceil(((sell?.price_for_target || t.per_part * 4) * 2) || 20));
  $('#cPriceRange').value = COSTS.price_per_part ?? 0;
  if (sell) {
    $('#sellKpis').innerHTML = [['Profit per part', money(sell.profit, 3), sell.profit >= 0 ? 'good' : 'bad'], ['Break-even price', money(sell.breakeven_price)],
      [`Price for ${sell.target_margin_pct}% margin`, money(sell.price_for_target)], ['Markup on cost', sell.markup_pct == null ? '—' : sell.markup_pct.toFixed(0) + '%']]
      .map(([k, v, c]) => `<div class="kpi ${c || ''}"><small>${esc(k)}</small><b>${esc(v)}</b></div>`).join('');
    $('#sellTable').innerHTML = [['Price', sell.price], ['Tax included in it', -sell.tax], ['Marketplace and payment fees (%)', -sell.percent_fees],
      ['Per-order costs, per part (fees, shipping, box)', -sell.per_order_costs], ['Cost to make', -sell.cost], ['Profit', sell.profit]]
      .map(([k, v], i, a) => `<tr${i === a.length - 1 ? ' style="font-weight:700"' : ''}><td>${esc(k)}</td><td class="num">${money(v, 3)}</td></tr>`).join('');
  } else { $('#sellKpis').innerHTML = '<p class="hint">Enter your selling price to see profit, margin, break-even and the price for your target margin.</p>'; $('#sellTable').innerHTML = ''; }
  // capacity
  const cap = r.capacity;
  $('#capKpis').innerHTML = cap ? [['Loops a day', cap.loops_per_day], ['Parts a day', cap.parts_per_day], ['Parts a month', cap.parts_per_month],
    ...(cap.revenue_per_month != null ? [['Revenue a month', money(cap.revenue_per_month, 0)], ['Profit a month', money(cap.profit_per_month, 0), cap.profit_per_month >= 0 ? 'good' : 'bad']] : [])]
    .map(([k, v, c]) => `<div class="kpi ${c || ''}"><small>${esc(k)}</small><b>${esc(v)}</b></div>`).join('')
    : '<p class="hint">Needs a cooldown time: measured by the Monitor, or your estimate above.</p>';
  // Make tab summary
  $('#makeCostOut').innerHTML = anyCostSet() ? `<div class="kpis" style="margin:0"><div class="kpi"><small>Per part</small><b>${money(t.per_part, 3)}</b></div>
    ${sell ? `<div class="kpi ${sell.profit >= 0 ? 'good' : 'bad'}"><small>Profit per part</small><b>${money(sell.profit, 3)}</b></div>` : ''}</div>
    ${t.complete ? '' : `<p class="hint" style="margin-top:6px">${r.missing.length} cost${r.missing.length > 1 ? 's' : ''} not set yet, so this is a minimum.</p>`}`
    : 'Enter your prices in the <a href="#" data-goto="costs">Costs</a> tab to see what each part costs and earns.';
}
document.addEventListener('change', (e) => {
  const t = e.target; if (t.dataset.slotkey === undefined) return;
  send('/api/filament/assign', {printer: '', key: t.dataset.slotkey, spool: t.value || null}, 'PUT').then(() => { refreshCost(); refreshPlateFil(); }).catch(err => toast(err.message));
});
