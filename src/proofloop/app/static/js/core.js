// Helpers shared by every part of the page: the API, toasts, and switching tabs.
// Part of loop.html; the files load in order and share their top-level names.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let token = ''; try { token = localStorage.getItem('proofloop-token') || ''; } catch (e) {}
let STATE = null, PLATE = null, pending = null, partPush = {};

async function api(path, opts = {}) {
  opts.headers = Object.assign({}, opts.headers || {}, token ? {Authorization: 'Bearer ' + token} : {});
  const r = await fetch(path, opts);
  if (r.status === 401) { const t = prompt('This ProofLoop needs its access token'); if (t) { token = t; try { localStorage.setItem('proofloop-token', t); } catch (e) {} return api(path, opts); } }
  if (!r.ok) { let d = r.statusText; try { d = (await r.json()).detail || d; } catch (e) {} throw new Error(typeof d === 'string' ? d : JSON.stringify(d)); }
  return r;
}
const json = async (path, opts) => (await api(path, opts)).json();
const send = (path, body, method = 'POST') => json(path, {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
function toast(msg) { const t = document.createElement('div'); t.className = 'toast'; t.textContent = msg; document.body.appendChild(t); setTimeout(() => t.remove(), 3800); }
function fmtDur(s) { if (s == null) return '—'; s = Math.round(s); if (s < 90) return s + ' s'; const m = Math.round(s / 60); return m < 90 ? m + ' min' : (Math.floor(m / 60) + ' h ' + (m % 60) + ' min'); }
async function saveBlob(resp, fallback) { const name = resp.headers.get('X-ProofLoop-Filename') || fallback; const b = await resp.blob(); const a = document.createElement('a'); a.href = URL.createObjectURL(b); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 5000); return name; }

// ---------- tabs ----------
// The sections belong to two tabs of the ProofLoop window: Loop files and Printers. On their own
// (a phone, proofloop printers serve) both groups are shown.
const PARTS = {loopfiles: ['make', 'check', 'costs', 'calibrate', 'profiles'], printers: ['overview', 'monitor', 'filament', 'orders']};
const partOf = (tab) => Object.keys(PARTS).find(p => PARTS[p].includes(tab));
let PART = null;  // inside the window: the tab chosen there
const lastIn = {};
function setPart(part, tab) {
  PART = part;
  $$('nav [data-part]').forEach(el => el.classList.toggle('hidden', el.dataset.part !== part));
  show(tab && partOf(tab) === part ? tab : (lastIn[part] || PARTS[part][0]));
}
function show(tab) {
  if (PART && partOf(tab) !== PART) {  // a link to the other tab: the window switches to it
    window.parent.postMessage({proofloop: 'show', part: partOf(tab), tab}, location.origin);
    return;
  }
  lastIn[partOf(tab)] = tab;
  $$('nav button').forEach(b => b.setAttribute('aria-selected', b.dataset.tab === tab));
  $$('[data-panel]').forEach(p => p.classList.toggle('hidden', p.dataset.panel !== tab));
  try { history.replaceState(null, '', '#' + tab); } catch (e) {}
  if (tab === 'monitor') refreshMonitor();
  if (tab === 'costs') { coolOptions(); bindCosts(); refreshCost(); }
  if (tab === 'filament') loadFil();
  if (tab === 'overview') refreshOverview();
  if (tab === 'orders') refreshOrders();
}
$$('nav button').forEach(b => b.onclick = () => show(b.dataset.tab));
document.addEventListener('click', (e) => { const g = e.target.closest('[data-goto]'); if (g) { e.preventDefault(); show(g.dataset.goto); } });

function dropZone(zone, input, onFile) {
  zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('over'));
  zone.addEventListener('drop', (e) => { e.preventDefault(); zone.classList.remove('over'); if (e.dataTransfer.files[0]) onFile(e.dataTransfer.files[0]); });
  input.addEventListener('change', () => { if (input.files[0]) onFile(input.files[0]); input.value = ''; });
}

async function loadState() {
  STATE = await json('/api/state');
  COSTS = JSON.parse(JSON.stringify(STATE.costs)); bindCosts(); coolOptions(); filPrinters();
  $('#ver').textContent = 'v' + STATE.version;
  $('#calBanner').classList.toggle('hidden', !!STATE.defaults.floor_calibrated);
  $('#modelSelect').innerHTML = Object.entries(STATE.printers).map(([id, n]) => `<option value="${esc(id)}" ${id === 'N7' ? 'selected' : ''}>${esc(n)}</option>`).join('');
  const opts = STATE.profiles.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  $('#calProfile').innerHTML = opts; $('#calProfile').value = STATE.defaults.profile_id;
  renderProfiles();
  renderSend();
}

// ---------- live updates: what's on screen refreshes when it changes, not on a timer ----------
const LIVE = {overview: ['printers', 'events', 'queue', 'orders', 'filament'], monitor: ['printers', 'events'],
  orders: ['printers', 'queue', 'orders']};
let liveOn = false, liveTimer = null, lastLive = 0;
const showing = () => $('[data-panel]:not(.hidden)')?.dataset.panel;
function refreshShowing() {
  const tab = showing();
  if (tab === 'overview') refreshOverview(); else if (tab === 'monitor') refreshMonitor(); else if (tab === 'orders') refreshOrders();
}
function startLive() {
  if (!window.EventSource) return;  // the timers below keep the page fresh instead
  const es = new EventSource('/api/live' + (token ? '?token=' + encodeURIComponent(token) : ''));
  es.addEventListener('change', (e) => {
    const first = !liveOn; liveOn = true;
    const what = JSON.parse(e.data), tab = showing();
    if (first || !(LIVE[tab] || []).some(k => what.includes(k))) return;
    if (liveTimer) return;  // a refresh is already coming: it will show this change too
    const wait = Math.max(200, lastLive + 2000 - Date.now());  // at most one refresh every 2 s
    liveTimer = setTimeout(() => { liveTimer = null; lastLive = Date.now(); refreshShowing(); }, wait);
  });
  es.onerror = () => { liveOn = false; };  // the browser reconnects by itself
}
