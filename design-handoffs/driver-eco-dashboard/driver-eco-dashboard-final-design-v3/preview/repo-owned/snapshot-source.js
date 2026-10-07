/* Driver Eco Dashboard V1 — snapshot input boundary.
 *
 * The UI depends on exactly one thing: a schema-v1
 * "driver_eco_dashboard_snapshot" document for ONE driver. This module is the
 * only place that knows where that document came from, so the next milestone
 * can swap the fixture source for the Cloudflare Worker without touching a
 * single renderer.
 *
 * It deliberately does NOT implement tokens or capability URLs: the bootstrap
 * exchange lives in js/boot.js and the credential is an HttpOnly cookie this
 * module never sees. It only maps a transport outcome onto the access states
 * the design already specifies.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.EcoSnapshotSource = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var CONTRACT_ID = "driver_eco_dashboard_snapshot";
  var SCHEMA_VERSION = 1;

  /* Structural validation only. Business validation happened on the host; the
   * browser never re-decides whether a snapshot is publishable. */
  function validate(doc) {
    if (!doc || typeof doc !== "object") return { ok: false, code: "SERVICE_UNAVAILABLE" };
    if (doc.contract_id !== CONTRACT_ID) return { ok: false, code: "SERVICE_UNAVAILABLE" };
    if (doc.schema_version !== SCHEMA_VERSION) return { ok: false, code: "SERVICE_UNAVAILABLE" };
    if (!doc.periods || typeof doc.periods !== "object") return { ok: false, code: "SERVICE_UNAVAILABLE" };
    var hasPeriod = !!(doc.periods.weekly || doc.periods.monthly);
    if (!hasPeriod) return { ok: false, code: "SNAPSHOT_UNAVAILABLE" };
    return { ok: true };
  }

  function statusToAccessCode(status) {
    if (status === 401 || status === 403) return "INVALID_LINK";
    if (status === 410) return "LINK_EXPIRED";
    if (status === 404) return "SNAPSHOT_UNAVAILABLE";
    return "SERVICE_UNAVAILABLE";
  }

  /* Production path. The capability credential is whatever the Worker expects
   * and is never echoed into the page, the DOM or a log. */
  function createWorkerSource(options) {
    var settings = options || {};
    return {
      kind: "worker",
      load: function () {
        var url = settings.url;
        if (!url) return Promise.resolve({ ok: false, code: "INVALID_LINK" });
        /* same-origin so the HttpOnly session cookie is sent; the capability
         * itself is never a parameter here — the Worker resolves the grant. */
        return fetch(url, {
          credentials: "same-origin",
          cache: "no-store",
          headers: { "Accept": "application/json" }
        })
          .then(function (response) {
            if (!response.ok) return { ok: false, code: statusToAccessCode(response.status) };
            return response.json().then(function (doc) {
              var result = validate(doc);
              return result.ok ? { ok: true, document: doc } : result;
            }, function () { return { ok: false, code: "SERVICE_UNAVAILABLE" }; });
          }, function () { return { ok: false, code: "SERVICE_UNAVAILABLE" }; });
      }
    };
  }

  /* Development / verification path. Reads a synthetic fixture from the same
   * origin. Never wired into index.html. */
  function createFixtureSource(options) {
    var settings = options || {};
    var basePath = settings.basePath || "./fixtures/";
    return {
      kind: "fixture",
      load: function () {
        var name = String(settings.name || "ranked_acceptable");
        if (!/^[a-z0-9_]+$/.test(name)) return Promise.resolve({ ok: false, code: "SNAPSHOT_UNAVAILABLE" });
        return fetch(basePath + name + ".json", { cache: "no-store" })
          .then(function (response) {
            if (!response.ok) return { ok: false, code: "SNAPSHOT_UNAVAILABLE" };
            return response.json().then(function (doc) {
              var result = validate(doc);
              return result.ok ? { ok: true, document: doc } : result;
            });
          }, function () { return { ok: false, code: "SERVICE_UNAVAILABLE" }; });
      }
    };
  }

  /* Already-parsed document (used by the preview harness and by tests). */
  function createStaticSource(doc) {
    return {
      kind: "static",
      load: function () {
        var result = validate(doc);
        return Promise.resolve(result.ok ? { ok: true, document: doc } : result);
      }
    };
  }

  return {
    CONTRACT_ID: CONTRACT_ID,
    SCHEMA_VERSION: SCHEMA_VERSION,
    validate: validate,
    statusToAccessCode: statusToAccessCode,
    createWorkerSource: createWorkerSource,
    createFixtureSource: createFixtureSource,
    createStaticSource: createStaticSource
  };
});
