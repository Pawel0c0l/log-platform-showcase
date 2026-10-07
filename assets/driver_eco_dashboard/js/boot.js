/* Capability exchange.
 *
 * The driver's link carries the capability in the URL *fragment*
 * (`https://<host>/#k=<capability>`). A fragment is never transmitted in the
 * HTTP request, so the raw secret cannot reach an origin access log, a CDN log,
 * a Referer header or an analytics beacon.
 *
 * Capture and URL cleanup already happened in js/capability-bootstrap.js, which
 * runs as the first script in <head> so that cleanup does not depend on this
 * bundle loading. This file only consumes the one-shot handover, exchanges the
 * value for an HttpOnly session cookie over a same-origin POST, and drops it.
 *
 * After this file has run, the capability exists nowhere in the browser: not in
 * the URL, not in history, not in storage, not in a retained variable.
 */
(function () {
  "use strict";

  var SESSION_ENDPOINT = "/api/session";

  function container() {
    return document.getElementById("eco-root");
  }

  function startDashboard() {
    var root = container();
    if (!root || !window.EcoApp || !window.EcoSnapshotSource) return;
    var url = root.getAttribute("data-snapshot-url") || "/api/snapshot";
    window.EcoApp.boot({ source: window.EcoSnapshotSource.createWorkerSource({ url: url }) });
  }

  function showAccessState(code) {
    var root = container();
    if (root && window.EcoRender) root.innerHTML = window.EcoRender.renderAccessState(code);
  }

  /* One-shot handover from the head script. Returns null on the second call,
   * so nothing keeps the value reachable after this module has used it. */
  function takeCapability() {
    if (typeof window.__ecoTakeCapability !== "function") return null;
    return window.__ecoTakeCapability();
  }

  function exchange(capability) {
    return fetch(SESSION_ENDPOINT, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ capability: capability })
    });
  }

  function run() {
    var capability = takeCapability();
    if (!capability) {
      /* No bootstrap in this navigation: an existing session (or none) decides. */
      startDashboard();
      return;
    }

    /* The URL was already cleaned by js/capability-bootstrap.js before this
     * script existed, so a failed or slow exchange cannot leave it dirty. */
    exchange(capability).then(function (response) {
      capability = null;
      if (response && (response.status === 204 || response.ok)) {
        startDashboard();
        return;
      }
      var code = window.EcoSnapshotSource.statusToAccessCode(response ? response.status : 0);
      showAccessState(code);
    }, function () {
      capability = null;
      showAccessState("SERVICE_UNAVAILABLE");
    });
    capability = null;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
