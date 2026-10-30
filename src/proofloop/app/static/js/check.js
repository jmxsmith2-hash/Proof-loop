// Check a loop file.
// Part of loop.html; the files load in order and share their top-level names.

// ---------- check ----------
dropZone($('#checkDrop'), $('#checkFile'), async (file) => {
  const out = $('#checkOut'); out.classList.remove('hidden'); out.innerHTML = 'Checking ' + esc(file.name) + '…';
  try { const fd = new FormData(); fd.append('file', file); const r = await json('/api/check', {method: 'POST', body: fd});
    const all = r.findings.concat(r.print_findings);
    const bad = all.filter(f => f.level === 'danger').length;
    const items = (list) => `<ul class="list">${list.map(f => `<li><span class="ic ${f.level}">${{danger: '✖', warning: '!', info: 'i', ok: '✓'}[f.level]}</span><div><b>${esc(f.title)}</b><div class="hint">${esc(f.detail)}</div></div></li>`).join('')}</ul>`;
    out.innerHTML = `<div class="spread"><h2>${esc(r.file)}</h2>${bad ? `<span class="chip bad">${bad} problem${bad > 1 ? 's' : ''}</span>` : '<span class="chip ok">looks good</span>'}</div><p class="muted">${esc(r.printer)}</p>
      ${items(r.findings)}
      <h3 style="margin-top:16px">The print in each loop</h3>
      ${items(r.print_findings)}`;
  } catch (e) { out.innerHTML = `<div class="banner bad"><span class="ic danger">✖</span><div>${esc(e.message)}</div></div>`; }
});
