// Start: load the state and open the right tab.
// Part of loop.html; the files load in order and share their top-level names.

loadState().then(() => {
  const t = location.hash.slice(1);
  // ProofLoop opens on the printers' overview (a link with a #tab still goes to that tab).
  show(t && $(`[data-panel=${t}]`) ? t : DEFAULT_TAB);
  startLive();
}).catch(e => toast(e.message));
