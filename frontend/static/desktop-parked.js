/* DownloadPulse parked desktop window (1.0.6).
 *
 * Windows and macOS keep the window's native shell while it is released (their
 * tray is the shell's own), so the application's page leaves for this one
 * instead (DPWindowState.park): nothing is drawn, nothing runs but one wait.
 * GET /api/desktop/parked answers once Open asks for the application again;
 * the page then goes back to it, and the application takes its kept state as a
 * relaunched window's page does. A wait that ends unanswered is asked again; a
 * failed one, after a pause. The mode the application showed comes in the
 * address (#light), so the window's background stays the same. */
(() => {
  'use strict';
  if (window.location.hash === '#light') document.documentElement.classList.add('light');
  async function wait() {
    for (;;) {
      try {
        const response = await fetch('/api/desktop/parked', {
          credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'},
        });
        const body = response.ok ? await response.json() : null;
        // Open, or nothing parked any more, or a refusal (signed out, the window
        // no longer the window): the application, which says what is needed.
        if (!response.ok || (body && body.open === true)) {
          window.location.replace('/');
          return;
        }
      } catch (_) {
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
    }
  }
  wait();
})();
