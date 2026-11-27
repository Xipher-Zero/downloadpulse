/* DownloadPulse parked desktop window (1.0.6; 1.0.8).
 *
 * Windows and macOS keep the window's native shell while it is released (their
 * tray is the shell's own), so the application's page leaves for this one
 * instead (DPWindowState.park): nothing is drawn, nothing runs but one wait.
 * GET /api/desktop/parked answers once Open asks for the application again;
 * the page then goes back to it, and the application takes its kept state as a
 * relaunched window's page does. A wait that ends unanswered is asked again; a
 * failed one, after a pause.
 *
 * The whole window is the mode's background, set on the root itself with its
 * matching color-scheme (desktop-parked.html starts dark): first the mode the
 * application showed, which comes in the address (#light, #dark) and is set
 * here, before the body is parsed; then the mode in force, which each
 * unanswered wait carries (``mode``). On Open the supervisor shows the window
 * first, with this page still in it (bring_forward), so what is shown is this
 * page, in the mode the application will show. */
(() => {
  'use strict';
  const BACKGROUNDS = Object.freeze({dark: '#000000', light: '#ffffff'});
  function show(mode) {
    if (!Object.prototype.hasOwnProperty.call(BACKGROUNDS, mode)) return;
    const root = document.documentElement;
    root.style.background = BACKGROUNDS[mode];
    root.style.colorScheme = mode;
  }
  show(window.location.hash.slice(1));
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
        if (body) show(body.mode);
      } catch (_) {
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
    }
  }
  wait();
})();
