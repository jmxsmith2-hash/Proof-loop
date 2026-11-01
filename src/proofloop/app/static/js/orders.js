// Orders: taking them, how each is going, invoices, and the spreadsheet downloads.
// Part of loop.html; the files load in order and share their top-level names.

// ---------- orders ----------
let ORDER_TOKEN = null, ORDER_MODEL = null, orderSliceTimer = null;
const ORDER_HINT = 'A model (.stl, .obj, .step) or project is sliced for the order; a sliced .gcode.3mf is used as it is';
function orderPlateReady(token, text) {
  ORDER_TOKEN = token; $('#orderPlate').textContent = text; $('#orderAdd').disabled = !token;
}
dropZone($('#orderDrop'), $('#orderFile'), async (file) => {
  $('#orderPlate').textContent = 'Reading ' + file.name + '…'; ORDER_MODEL = null;
  try { const fd = new FormData(); fd.append('file', file); const r = await json('/api/plates', {method: 'POST', body: fd});
    if (r.model) { ORDER_MODEL = r.model; await sliceForOrder(); return; }
    if (r.no_profile) throw new Error(`No hardware profile for the ${r.printer} yet`);
    orderPlateReady(r.token, `${file.name} · ${(r.parts || []).length} parts per plate`); quoteForOrder();
  } catch (e) { orderPlateReady(null, e.message); }
});
// A model is sliced with your usual presets: full plates for a big order, just the parts for a small one.
async function sliceForOrder() {
  const qty = +$('#orderForm').quantity.value;
  if (!ORDER_MODEL || !(qty >= 1)) return;
  orderPlateReady(null, `Slicing ${ORDER_MODEL.name} for ${qty} parts…`);
  try {
    const r = await send(`/api/models/${encodeURIComponent(ORDER_MODEL.token)}/slice-for-order`, {quantity: qty});
    if (r.no_profile) throw new Error(`No hardware profile for the ${r.printer} yet`);
    orderPlateReady(r.token, `${r.file} · ${(r.slice_notes || []).join('; ')}`); quoteForOrder();
  } catch (e) { orderPlateReady(null, e.message); }
}
$('#orderForm').quantity.addEventListener('input', () => {
  clearTimeout(orderSliceTimer);
  if (ORDER_MODEL) { $('#orderAdd').disabled = true; orderSliceTimer = setTimeout(sliceForOrder, 900); }
});
async function quoteForOrder() {
  const f = $('#orderForm'), token = ORDER_TOKEN;
  f.price_each.placeholder = 'optional';
  try {
    const c = await send(`/api/plates/${encodeURIComponent(token)}/cost`, {settings: {}, loops: 1});
    if (token === ORDER_TOKEN && c.quote?.price) f.price_each.placeholder = `quote ${money(c.quote.price)}`;
  } catch (e) { /* no rates or no profile: no quote */ }
}
// A plate open in Make a loop file, handed over with its quoted price.
function orderFromPlate(token, file, parts, price) {
  ORDER_TOKEN = token; ORDER_MODEL = null; $('#orderAdd').disabled = false;
  $('#orderPlate').textContent = `${file} · ${parts} parts per plate`;
  const f = $('#orderForm');
  if (price != null) f.price_each.value = price.toFixed(2);
  show('orders');
  setTimeout(() => f.customer.focus(), 50);  // once the tab is on screen
}
$('#orderForm').onsubmit = async (e) => {
  e.preventDefault(); const f = e.target; if (!ORDER_TOKEN) return;
  $('#orderAdd').disabled = true;
  try {
    const o = await send('/api/orders', {token: ORDER_TOKEN, customer: f.customer.value, quantity: +f.quantity.value, due: f.due.value || null, price_each: f.price_each.value || null, notes: f.notes.value});
    toast(`Order for ${o.customer} taken: ${o.on_time === false ? 'it will be late' : o.eta ? 'ready ' + at(o.eta) : 'queued'}`);
    f.reset(); ORDER_MODEL = null; orderPlateReady(null, ORDER_HINT); f.price_each.placeholder = 'optional'; refreshOrders();
  } catch (err) { toast(err.message); $('#orderAdd').disabled = false; }
};
// A proof photo: a loop's finished plate, before the sweep. A click opens it full size.
const proofThumb = (ph) => {
  const src = esc(withToken('/api/proofs/' + encodeURIComponent(ph.id)));
  const what = `${ph.printer} · loop ${ph.loop} · ${new Date(ph.taken * 1000).toLocaleString()}`;
  return `<a class="proof" href="${src}" target="_blank" rel="noopener" title="${esc(what)}"><img src="${src}" alt="${esc('Finished plate: ' + what)}" loading="lazy"><span>${ph.loop}</span></a>`;
};
const galleryOpen = new Set();  // orders whose gallery you opened, kept open as the list is drawn again
const at = (ts) => new Date(ts * 1000).toLocaleString([], {weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
function csvLinks() {
  const range = [['from', $('#csvFrom').value], ['to', $('#csvTo').value]].filter(([, v]) => v).map(([k, v]) => `${k}=${v}`);
  $('#csvOrders').href = withToken('/api/orders/export.csv');
  $('#csvHistory').href = withToken('/api/outcomes/export.csv');
  $('#csvCosts').href = withToken('/api/costs/export.csv?' + [...range, 'by=' + $('#csvBy').value].join('&'));
}
['#csvFrom', '#csvTo', '#csvBy'].forEach(id => $(id).addEventListener('input', csvLinks));
csvLinks();

async function loadBusiness() {
  const f = $('#businessForm'); if (f.contains(document.activeElement)) return;  // not while you're typing
  try { const b = await json('/api/business'); for (const k of Object.keys(b)) if (f[k]) f[k].value = b[k]; } catch (e) {}
}
$('#businessForm').onsubmit = async (e) => {
  e.preventDefault(); const f = e.target;
  try {
    const b = {}; for (const k of ['name', 'address', 'email', 'tax_id', 'prefix', 'footer']) b[k] = f[k].value;
    b.next_number = +f.next_number.value || 1;
    await send('/api/business', b, 'PUT'); $('#businessSaved').textContent = '✓ saved'; setTimeout(() => { $('#businessSaved').textContent = ''; }, 3000);
  } catch (err) { toast(err.message); }
};

function renderCustomers(rows) {
  const cur = STATE?.costs?.currency || '', amt = (v) => v ? cur + v.toFixed(2) : '—';
  $('#customersOut').innerHTML = rows.length ? '<tr><th>Customer</th><th class="num">Orders</th><th class="num">Parts</th><th class="num">Earned</th><th class="num">Unpaid</th></tr>' +
    rows.map(c => `<tr><td>${esc(c.customer)}</td><td class="num">${c.orders}</td><td class="num">${c.parts}</td><td class="num">${amt(c.earned)}</td><td class="num">${c.unpaid ? `<b>${amt(c.unpaid)}</b>` : '—'}</td></tr>`).join('')
    : '<tr><td class="muted">No customers yet.</td></tr>';
}
async function refreshOrders() {
  let orders;
  try { orders = await json('/api/orders'); } catch (e) { return; }
  csvLinks(); loadBusiness();
  json('/api/orders/customers').then(renderCustomers).catch(() => {});
  if (!$('#ordersAll').checked) orders = orders.filter(o => o.status === 'open');
  const cur = STATE?.costs?.currency || '';
  $('#ordersOut').innerHTML = orders.length ? orders.map(o => {
    const chip = o.status === 'cancelled' ? '<span class="chip">cancelled</span>' : o.complete ? '<span class="chip ok">made</span>'
      : o.on_time === false ? `<span class="chip bad">late by ${o.late_by_days} day${o.late_by_days === 1 ? '' : 's'}</span>` : o.on_time ? '<span class="chip ok">on time</span>' : '<span class="chip info">in the queue</span>';
    const m = o.money, paid = m.revenue == null ? '' : o.paid ? '<span class="chip ok">paid</span>' : '<span class="chip warn">unpaid</span>';
    const money = [m.revenue != null ? `${cur}${m.revenue.toFixed(2)} in` : '', m.cost != null ? `${cur}${m.cost.toFixed(2)} cost` : '', m.profit != null ? `<b>${cur}${m.profit.toFixed(2)} profit</b>` : ''].filter(Boolean).join(' · ');
    return `<div class="run"><div class="spread"><b>${esc(o.customer)}</b><span class="row" style="gap:6px">${paid}${chip}</span></div>
      <div class="hint">${o.quantity} × ${esc(o.plate)}${o.due ? ' · due ' + esc(new Date(o.due + 'T12:00').toLocaleDateString([], {weekday: 'short', month: 'short', day: 'numeric'})) : ''}${o.notes ? ' · ' + esc(o.notes) : ''}</div>
      <div class="meter"><i style="width:${o.pct}%"></i></div>
      <div class="spread"><span><b>${o.made}</b> of ${o.quantity} made</span><span class="hint">${o.complete ? 'all made' : o.eta ? 'ready around <b>' + at(o.eta) + '</b>' : o.queued ? '' : 'its queue was removed'}</span></div>
      ${money ? `<div class="hint">${money}</div>` : ''}
      ${o.proofs?.length ? `<details class="gallery" data-gallery="${esc(o.id)}"${galleryOpen.has(o.id) ? ' open' : ''}><summary class="hint">${o.proofs.length} finished plate${o.proofs.length === 1 ? '' : 's'}</summary><div class="proofs">${o.proofs.map(proofThumb).join('')}</div></details>` : ''}
      <div class="row" style="margin-top:6px"><button class="linkbtn" data-invoice="${esc(o.id)}">Invoice${o.invoice ? ' ' + esc(o.invoice.label) : ''}</button>${m.revenue != null ? ` <button class="linkbtn" data-order="${esc(o.id)}" data-set="${o.paid ? 'unpaid' : 'paid'}">Mark ${o.paid ? 'unpaid' : 'paid'}</button>` : ''} ${o.status === 'open' ? `<button class="linkbtn" data-ordergo="overview">Queue</button> <button class="linkbtn" data-order="${esc(o.id)}" data-set="done">Mark made</button> <button class="linkbtn" data-order="${esc(o.id)}" data-set="cancelled">Cancel</button>` : ''} <button class="linkbtn" data-order="${esc(o.id)}" data-set="remove">Remove</button></div></div>`;
  }).join('') : '<p class="hint">No orders yet. Take one below: drop the model or sliced plate, say how many and by when.</p>';
}
$('#ordersAll').addEventListener('change', refreshOrders);
$('#ordersOut').addEventListener('toggle', (e) => {  // toggle doesn't bubble: caught on the way down
  const g = e.target.closest?.('[data-gallery]'); if (!g) return;
  if (g.open) galleryOpen.add(g.dataset.gallery); else galleryOpen.delete(g.dataset.gallery);
}, true);
$('#ordersOut').addEventListener('click', async (e) => {
  const g = e.target.closest('[data-ordergo]'); if (g) { show(g.dataset.ordergo); return; }
  const inv = e.target.closest('[data-invoice]');
  if (inv) {
    try {
      const w = window.open(URL.createObjectURL(await (await api('/api/orders/' + inv.dataset.invoice + '/invoice')).blob()), '_blank');
      if (!w) toast('Allow pop-ups to see the invoice');
    } catch (err) { toast(err.message); }
    refreshOrders(); return;
  }
  const b = e.target.closest('[data-order]'); if (!b) return;
  try {
    if (b.dataset.set === 'paid' || b.dataset.set === 'unpaid') await send('/api/orders/' + b.dataset.order, {paid: b.dataset.set === 'paid'}, 'PATCH');
    else if (b.dataset.set === 'remove') { if (!confirm('Remove this order, and its jobs from the queue?')) return; await json('/api/orders/' + b.dataset.order, {method: 'DELETE'}); }
    else await send('/api/orders/' + b.dataset.order, {status: b.dataset.set}, 'PATCH');
  } catch (err) { toast(err.message); }
  refreshOrders();
});
setInterval(() => { if (!liveOn && !$('[data-panel=orders]').classList.contains('hidden')) refreshOrders(); }, 10000);  // without live updates
