/* DownloadPulse desktop window state (DPWindowState): what a released window
 * comes back to.
 *
 * The installed desktop application may release its window after it has been
 * hidden in the tray for a while -- the native shell ended to free its memory
 * (Linux) -- but only when this page says nothing would be lost, and only to
 * come back exactly as it was. The supervisor asks this page alone (event
 * desktop_release_prompt, a single-use nonce); the page answers with whether
 * it is releasable and the state it would come back to (PUT
 * /api/desktop/window-state); the relaunched page takes that state once (GET),
 * before any page shows. The backend keeps it in its own process memory only.
 *
 * One owner of the answer and the restore. Each page owner keeps its own state
 * and says it here (register: capture, releasable); a relaunched owner reads
 * its part as an input to its own entry (take) -- this owner never writes a
 * page's DOM. Inert anywhere but the desktop window (window.downloadPulseDesktop),
 * and nothing is restored on an ordinary reload: the backend hands a state
 * back only after a release. */
const DPWindowState = (() => {
  const active = window.downloadPulseDesktop === true;
  const owners = new Map();                 // name -> {capture?, releasable?}
  let restored = null;                       // {state: {route, owners}, since} after a release
  const taken = new Set();

  // The relaunched page's state, asked once, before any page shows.
  const ready = !active ? Promise.resolve(null) : fetch('/api/desktop/window-state', {
    credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'},
  }).then(response => (response.ok ? response.json() : null))
    .then(body => {
      const restore = body && body.restore;
      restored = restore && restore.state && typeof restore.state === 'object' ? restore : null;
      return restored;
    })
    .catch(() => null);

  function register(name, hooks) {
    if (typeof name === 'string' && name && hooks && typeof hooks === 'object') owners.set(name, hooks);
  }

  // An owner's part of the restored state, given once (null otherwise).
  function take(name) {
    if (!restored || taken.has(name)) return null;
    taken.add(name);
    const part = restored.state.owners && restored.state.owners[name];
    return part === undefined ? null : part;
  }

  // An owner's restore still under way (a page it fetches): the relaunched
  // interface is not shown before every one has settled, or RESTORE_HOLD_MS.
  const held = [];
  const RESTORE_HOLD_MS = 8000;
  function hold(work) {
    if (restored && work && typeof work.then === 'function') held.push(Promise.resolve(work).catch(() => {}));
    return work;
  }
  const settled = () => Promise.race([Promise.allSettled(held), new Promise(resolve => setTimeout(resolve, RESTORE_HOLD_MS))]);

  // The route the released window showed, and where the journal stood at the release.
  const route = () => (restored && typeof restored.state.route === 'string' ? restored.state.route : null);
  const since = () => (restored && Number.isInteger(restored.since) ? restored.since : null);
  // What happened while it was released, counted by the backend from that position (1.0.5 WS8g).
  const away = () => {
    const counts = restored && restored.away;
    if (!counts || typeof counts !== 'object') return null;
    const count = value => (Number.isInteger(value) && value > 0 ? value : 0);
    return {completed: count(counts.completed), failed: count(counts.failed), attention: count(counts.attention)};
  };

  // Whether nothing would be lost: no change in flight, nothing open that the
  // state does not bring back, and every owner agrees.
  function releasable() {
    if ((window.downloadPulseAuth?.pendingMutations?.() || 0) > 0) return false;
    if (window.DPSettingsModal?.showing?.()) return false;
    const mode = window.DPModal?.mode;
    if (mode && mode !== 'details') return false;
    // A tooltip holds nothing; a popover the state brings back is not in the way.
    const open = [...document.querySelectorAll(':popover-open')]
      .filter(element => element.getAttribute('role') !== 'tooltip' && !element.hasAttribute('data-dp-window-state-restorable'));
    if (open.length) return false;
    for (const [, hooks] of owners) {
      try {
        if (typeof hooks.releasable === 'function' && hooks.releasable() === false) return false;
      } catch (_) {
        return false;                        // an owner that cannot say is not releasable
      }
    }
    return true;
  }

  // The state to come back to; null when any owner cannot say its part (a
  // capture that throws, or a part that is not JSON): then nothing is released.
  function capture() {
    const parts = {};
    try {
      for (const [name, hooks] of owners) {
        if (typeof hooks.capture !== 'function') continue;
        const part = hooks.capture();
        if (part !== undefined) parts[name] = JSON.parse(JSON.stringify(part));
      }
    } catch (_) {
      return null;
    }
    return {route: window.location.pathname, owners: parts};
  }

  // The answer to the supervisor's release prompt. A page may answer again
  // with the same nonce; the latest answer counts.
  async function answer(nonce) {
    if (!active || typeof nonce !== 'string' || !nonce) return;
    const state = releasable() ? capture() : null;
    const body = {nonce, releasable: state !== null, state: state || {}};
    try {
      await window.downloadPulseAuth.fetch('/api/desktop/window-state', {
        method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
      });
    } catch (_) {}
  }

  return Object.freeze({active, ready, register, take, hold, settled, route, since, away, releasable, answer});
})();
window.DPWindowState = DPWindowState;
