/* Earliest-possible capability capture and URL cleanup.
 *
 * This is the FIRST script in the document and it is deliberately tiny: it runs
 * as a render-blocking classic script in <head>, before the stylesheet, before
 * the renderer and before anything that could fail. Its only job is to take the
 * capability out of the address bar and out of session history at the earliest
 * point the browser gives us.
 *
 * Why it is separate from js/boot.js: boot.js runs at the end of <body> and
 * waits for the rest of the bundle. If any later asset were blocked, slow or
 * broken, the capability would still be sitting in the URL — and in history —
 * for as long as the page stayed open. Cleanup must not depend on the
 * application booting successfully.
 *
 * It is an external file, not an inline <script>, so the delivery CSP stays
 * `script-src 'self'` with no inline exception.
 *
 * The captured value is handed on through a one-shot accessor that nulls itself
 * on first read. It is never written to localStorage, sessionStorage,
 * IndexedDB, a cookie, a DOM attribute, or a log.
 */
(function () {
  "use strict";

  var FRAGMENT_PREFIX = "#k=";
  var captured = null;

  try {
    var hash = window.location.hash || "";
    if (hash.indexOf(FRAGMENT_PREFIX) === 0) {
      var value = hash.slice(FRAGMENT_PREFIX.length);
      if (value.length) captured = value;

      /* replaceState, not pushState: the bootstrap URL is overwritten rather
       * than stacked, so Back/Forward cannot restore the secret. */
      var clean = window.location.pathname + window.location.search;
      if (window.history && window.history.replaceState) {
        window.history.replaceState(null, document.title, clean);
      } else {
        window.location.hash = "";
      }
    }
  } catch (error) {
    /* A cleanup failure must never surface the value. Drop it and continue: the
     * dashboard will simply have no session and fail closed. */
    captured = null;
  }

  /* One-shot handover. The second call always returns null, so the value does
   * not stay reachable after boot.js has consumed it. */
  Object.defineProperty(window, "__ecoTakeCapability", {
    value: function () {
      var value = captured;
      captured = null;
      return value;
    },
    writable: false,
    enumerable: false,
    configurable: false
  });
})();
