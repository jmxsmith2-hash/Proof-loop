// Start: load the state and open the right tab.
// Part of loop.html; the files load in order and share their top-level names.

loadState().then(() => {
  const t = location.hash.slice(1);
  // A farm that's running opens on its overview; a first visit opens on making a loop file.
  show(t && $(`[data-panel=${t}]`) ? t : (STATE.monitor.length || STATE.spools.length) ? 'overview' : 'make');
  startLive();
}).catch(e => toast(e.message));
