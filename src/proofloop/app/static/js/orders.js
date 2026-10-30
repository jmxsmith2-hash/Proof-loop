// Orders: taking them, and how each is going.
// Part of loop.html; the files load in order and share their top-level names.

// ---------- orders ----------
let ORDER_TOKEN = null;
dropZone($('#orderDrop'), $('#orderFile'), async (file) => {
  $('#orderPlate').textContent = 'Reading ' + file.name + '…';
  try { const fd = new FormData(); fd.append('file', file); const r = await json('/api/plates', {method: 'POST', body: fd});
    ORDER_TOKEN = r.token; $('#orderPlate').textContent = `${file.name} · ${(r.parts || []).length} parts per plate`; $('#orderAdd').disabled = false;
  } catch (e) { $('#orderPlate').textContent = e.message; ORDER_TOKEN = null; $('#orderAdd').disabled = true; }
});
$('#orderForm').onsubmit = async (e) => {
  e.preventDefault(); const f = e.target; if (!ORDER_TOKEN) return;
  $('#orderAdd').disabled = true;
  try {
    const o = await send('/api/orders', {token: ORDER_TOKEN, customer: f.customer.value, quantity: +f.quantity.value, due: f.due.value || null, price_each: f.price_each.value || null, notes: f.notes.value});
    toast(`Order for ${o.customer} taken: ${o.on_time === false ? 'it will be late' : o.eta ? 'ready ' + at(o.eta) : 'queued'}`);
    f.reset(); ORDER_TOKEN = null; $('#orderPlate').textContent = '.gcode.3mf: the plain slice of the part'; refreshOrders();
  } catch (err) { toast(err.message); $('#orderAdd').disabled = false; }
};
const at = (ts) => new Date(ts * 1000).toLocaleString([], {weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
async function refreshOrders() {
  let orders = [];
  try { orders = await json('/api/orders'); } catch (e) { return; }
  if (!$('#ordersAll').checked) orders = orders.filter(o => o.status === 'open');
  const cur = STATE?.costs?.currency || '';
  $('#ordersOut').innerHTML = orders.length ? orders.map(o => {
    const chip = o.status === 'cancelled' ? '<span class="chip">cancelled</span>' : o.complete ? '<span class="chip ok">made</span>'
      : o.on_time === false ? `<span class="chip bad">late by ${o.late_by_days} day${o.late_by_days === 1 ? '' : 's'}</span>` : o.on_time ? '<span class="chip ok">on time</span>' : '<span class="chip info">in the queue</span>';
    const m = o.money, money = [m.revenue != null ? `${cur}${m.revenue.toFixed(2)} in` : '', m.cost != null ? `${cur}${m.cost.toFixed(2)} cost` : '', m.profit != null ? `<b>${cur}${m.profit.toFixed(2)} profit</b>` : ''].filter(Boolean).join(' · ');
    return `<div class="run"><div class="spread"><b>${esc(o.customer)}</b>${chip}</div>
      <div class="hint">${o.quantity} × ${esc(o.plate)}${o.due ? ' · due ' + esc(new Date(o.due + 'T12:00').toLocaleDateString([], {weekday: 'short', month: 'short', day: 'numeric'})) : ''}${o.notes ? ' · ' + esc(o.notes) : ''}</div>
      <div class="meter"><i style="width:${o.pct}%"></i></div>
      <div class="spread"><span><b>${o.made}</b> of ${o.quantity} made</span><span class="hint">${o.complete ? 'all made' : o.eta ? 'ready around <b>' + at(o.eta) + '</b>' : o.queued ? '' : 'its queue was removed'}</span></div>
      ${money ? `<div class="hint">${money}</div>` : ''}
      <div class="row" style="margin-top:6px">${o.status === 'open' ? `<button class="linkbtn" data-ordergo="overview">Queue</button> <button class="linkbtn" data-order="${esc(o.id)}" data-set="done">Mark made</button> <button class="linkbtn" data-order="${esc(o.id)}" data-set="cancelled">Cancel</button>` : ''} <button class="linkbtn" data-order="${esc(o.id)}" data-set="remove">Remove</button></div></div>`;
  }).join('') : '<p class="hint">No orders yet. Take one on the right: drop the sliced plate, say how many and by when.</p>';
}
$('#ordersAll').addEventListener('change', refreshOrders);
$('#ordersOut').addEventListener('click', async (e) => {
  const g = e.target.closest('[data-ordergo]'); if (g) { show(g.dataset.ordergo); return; }
  const b = e.target.closest('[data-order]'); if (!b) return;
  try {
    if (b.dataset.set === 'remove') { if (!confirm('Remove this order, and its jobs from the queue?')) return; await json('/api/orders/' + b.dataset.order, {method: 'DELETE'}); }
    else await send('/api/orders/' + b.dataset.order, {status: b.dataset.set}, 'PATCH');
  } catch (err) { toast(err.message); }
  refreshOrders();
});
setInterval(() => { if (!liveOn && !$('[data-panel=orders]').classList.contains('hidden')) refreshOrders(); }, 10000);  // without live updates
