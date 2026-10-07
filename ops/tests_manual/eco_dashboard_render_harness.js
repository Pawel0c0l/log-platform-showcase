/* Node harness for the Driver Eco Dashboard frontend integration contract.
 *
 * WHAT CHANGED AND WHY. This harness used to `require()` the V1 renderer and
 * call its private export list (`dayGroups`, `coachingSentence`, `scoreRating`,
 * `liveCategories`, …), which made every one of those functions an accidental
 * integration contract: a replacement presentation layer could be perfectly
 * correct and still fail here. It now loads the shipped presentation files the
 * way a browser does — as plain scripts against a global object — and exercises
 * only the frozen boundary:
 *
 *     window.EcoApp.boot({ source })
 *     window.EcoRender.renderAccessState(code)
 *
 * plus the repo-owned `js/snapshot-source.js`, whose transport vocabulary is a
 * delivery contract rather than a design decision.
 *
 * Everything that needs a real DOM — how a snapshot is rendered, what a driver
 * can read, layout, interaction — is proved in a real browser by
 * `ops/tests_manual/eco_dashboard_browser.py`. This file deliberately stops at
 * the API surface.
 *
 * No dependencies, no bundler, no jsdom: the scripts run inside `node:vm` with
 * the smallest window/document stub that a string-returning renderer can need.
 * A presentation layer that cannot produce its access-state markup without a
 * live document is over-coupled to the DOM — `js/boot.js` assigns the result
 * straight to `innerHTML`, so the string must exist on its own.
 *
 * Usage:
 *     node ops/tests_manual/eco_dashboard_render_harness.js <scenario> [args…]
 * Prints one JSON object on stdout.
 */
"use strict";

const path = require("path");
const fs = require("fs");
const vm = require("vm");

const APP_ROOT = path.join(__dirname, "..", "..", "assets", "driver_eco_dashboard");

/* The presentation entrypoints, in the order index.html loads them. The design
 * owns their contents; the filenames are fixed by the Worker asset allowlist. */
const PRESENTATION_SCRIPTS = ["format.js", "render.js", "snapshot-source.js", "app.js"];

const ACCESS_CODES = ["INVALID_LINK", "LINK_EXPIRED", "SNAPSHOT_UNAVAILABLE", "SERVICE_UNAVAILABLE"];

function fixture(name) {
  return JSON.parse(fs.readFileSync(path.join(APP_ROOT, "fixtures", name + ".json"), "utf8"));
}

/* The minimum a classic script may touch while it is merely *loading*, plus the
 * few members every real element carries — a CSSStyleDeclaration answers
 * `setProperty`, and an element has a `classList`. A design that uses either is
 * writing ordinary DOM, not over-coupling to it, so the stub must not fail it. */
function stubElement() {
  const element = {
    style: {
      setProperty() {}, removeProperty() {}, getPropertyValue() { return ""; },
    },
    classList: {
      add() {}, remove() {}, toggle() {}, contains() { return false; },
    },
    dataset: {}, children: [], hidden: false, innerHTML: "", textContent: "",
    tagName: "DIV", className: "",
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    addEventListener() {}, removeEventListener() {}, appendChild() {}, focus() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, contains() { return false; },
  };
  return element;
}

function createRealm() {
  const documentStub = {
    readyState: "complete",
    title: "",
    addEventListener() {}, removeEventListener() {},
    getElementById() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    createElement() { return stubElement(); },
    body: stubElement(),
  };
  const sandbox = {
    console,
    document: documentStub,
    /* A window really does carry these, and a presentation layer is entitled to
     * use them: `hashchange` is how a routed dashboard follows the URL. Their
     * absence here would fail a correct design for a defect of the stub. */
    addEventListener() {}, removeEventListener() {},
    performance: { now: () => 0 },
    location: { hash: "", search: "", pathname: "/", href: "http://localhost/" },
    history: { replaceState() {}, pushState() {} },
    navigator: { userAgent: "node" },
    fetch: () => Promise.reject(new Error("no network in the API harness")),
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: (callback) => setTimeout(() => callback(0), 0),
    cancelAnimationFrame: clearTimeout,
    matchMedia: () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {} }),
    Promise, JSON, Math, Date, Object, Array, String, Number, Boolean, RegExp, Error, URL,
    URLSearchParams, Map, Set, Intl,
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  return sandbox;
}

/**
 * Load the presentation layer exactly as a browser does.
 *
 * `module` is deliberately absent from the realm, so a UMD wrapper takes its
 * global branch and a plain global script works unchanged. Neither style is
 * imposed on the design.
 */
function loadPresentation() {
  const realm = createRealm();
  const loaded = [];
  const failures = [];
  for (const name of PRESENTATION_SCRIPTS) {
    const file = path.join(APP_ROOT, "js", name);
    try {
      vm.runInContext(fs.readFileSync(file, "utf8"), realm, { filename: file });
      loaded.push(name);
    } catch (error) {
      failures.push({ file: name, message: String(error && error.message) });
    }
  }
  return { realm, loaded, failures };
}

function emit(value) {
  process.stdout.write(JSON.stringify(value));
}

const scenario = process.argv[2];

if (scenario === "api") {
  /* R1: the frozen frontend boundary. Presence and callability only — what the
   * functions render is the browser suite's business. */
  const { realm, loaded, failures } = loadPresentation();
  const app = realm.EcoApp;
  const render = realm.EcoRender;
  const source = realm.EcoSnapshotSource;
  emit({
    loaded,
    load_failures: failures,
    globals: {
      EcoApp: typeof app,
      EcoRender: typeof render,
      EcoSnapshotSource: typeof source,
    },
    eco_app_boot: typeof (app && app.boot),
    eco_render_access_state: typeof (render && render.renderAccessState),
    /* The design may export anything else it likes; nothing else is required. */
    eco_app_keys: app ? Object.keys(app).sort() : [],
    eco_render_keys: render ? Object.keys(render).sort() : [],
  });
} else if (scenario === "access") {
  /* renderAccessState must produce standalone markup for every transport
   * outcome, with no snapshot and no live document. */
  const { realm } = loadPresentation();
  const out = {};
  for (const code of ACCESS_CODES) {
    try {
      const html = realm.EcoRender.renderAccessState(code);
      out[code] = { ok: typeof html === "string", html: String(html) };
    } catch (error) {
      out[code] = { ok: false, html: "", error: String(error && error.message) };
    }
  }
  /* An unknown code must still fail closed rather than throw or leak. */
  try {
    out.__unknown = { ok: true, html: String(realm.EcoRender.renderAccessState("NOT_A_CODE")) };
  } catch (error) {
    out.__unknown = { ok: false, html: "", error: String(error && error.message) };
  }
  emit(out);
} else if (scenario === "boot_contract") {
  /* EcoApp.boot must take its snapshot from the supplied source and from
   * nowhere else: no fetch, no URL, no second transport. */
  const { realm } = loadPresentation();
  const document_ = fixture(process.argv[3] || "ranked_acceptable");
  let loadCalls = 0;
  const container = stubElement();
  realm.document.getElementById = () => container;
  const source = {
    kind: "test",
    load() {
      loadCalls += 1;
      return Promise.resolve({ ok: true, document: document_ });
    },
  };
  let threw = null;
  try {
    realm.EcoApp.boot({ source });
  } catch (error) {
    threw = String(error && error.message);
  }
  emit({ threw, load_calls_immediate: loadCalls, network_used: false });
} else if (scenario === "render_pair") {
  /* The two approved presentations from one pure renderer, for one snapshot.
   *
   * `default` is what every existing caller gets; `explicit_desktop` is the
   * same call with the flag spelled out. They must be the SAME BYTES: that is
   * what proves the desktop page is unchanged by the mobile branch existing.
   * `mobile` is the redesign, and must differ wherever there is a dashboard to
   * differ about (a fail-closed period is full-screen in both). */
  const { realm } = loadPresentation();
  const document_ = fixture(process.argv[3]);
  const period = process.argv[4];
  const slide = parseInt(process.argv[5], 10) || 0;
  const render = realm.EcoRender.renderDashboard;
  emit({
    default: String(render(document_, { period, slide })),
    explicit_desktop: String(render(document_, { period, slide, mobile: false })),
    mobile: String(render(document_, { period, slide, mobile: true })),
  });
} else if (scenario === "source_contract") {
  /* The repo-owned input boundary: structural gate and HTTP vocabulary. */
  const { realm } = loadPresentation();
  const sources = realm.EcoSnapshotSource;
  const good = fixture("ranked_acceptable");
  emit({
    valid: sources.validate(good),
    null_document: sources.validate(null),
    wrong_contract: sources.validate(Object.assign({}, good, { contract_id: "something_else" })),
    wrong_version: sources.validate(Object.assign({}, good, { schema_version: 2 })),
    no_periods: sources.validate(Object.assign({}, good, { periods: { weekly: null, monthly: null } })),
    status_401: sources.statusToAccessCode(401),
    status_403: sources.statusToAccessCode(403),
    status_410: sources.statusToAccessCode(410),
    status_404: sources.statusToAccessCode(404),
    status_429: sources.statusToAccessCode(429),
    status_500: sources.statusToAccessCode(500),
    contract_id: sources.CONTRACT_ID,
    schema_version: sources.SCHEMA_VERSION,
    factories: ["createWorkerSource", "createFixtureSource", "createStaticSource"]
      .filter((name) => typeof sources[name] === "function"),
  });
} else {
  process.stderr.write("unknown scenario: " + scenario + "\n");
  process.exit(2);
}
