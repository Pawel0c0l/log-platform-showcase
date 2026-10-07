/* DOM/history stub that executes the real api/static/js/data-grid.js.
 *
 * The density control is the one part of the data sheet whose contract lives in
 * JavaScript — URL, localStorage, DOM and browser history all have to agree —
 * so it is exercised by running the shipped file rather than by reading it.
 *
 * Invoked by ops/tests_manual/test_portal_database_table_first.py:
 *
 *     node ops/tests_manual/data_grid_density_harness.js <scenario> [json-args]
 *
 * Prints one JSON object describing the observed end state. Any real navigation
 * (assigning location.href, or a link click that was not prevented) is recorded
 * as `navigated: true`, because changing density must never requery.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const SCRIPT = path.join(__dirname, "..", "..", "api", "static", "js", "data-grid.js");

function makeHarness(options) {
  const opts = options || {};
  const store = {};
  if (opts.stored) {
    store["logplatform.database.density"] = opts.stored;
  }

  const result = {
    navigated: false,
    prevented: false,
    pushed: [],
    historyIndex: 0,
  };

  const sheetAttrs = {
    "data-density": opts.rendered || "compact",
    "data-density-default": opts.approvedDefault || "compact",
  };
  if (opts.locked) {
    sheetAttrs["data-density-locked"] = "";
  }

  const sheet = {
    hasAttribute: (n) => n in sheetAttrs,
    getAttribute: (n) => (n in sheetAttrs ? sheetAttrs[n] : null),
    setAttribute: (n, v) => { sheetAttrs[n] = v; },
    removeAttribute: (n) => { delete sheetAttrs[n]; },
  };

  /* Density options carry the server-built href for their own view, exactly as
     the rendered page does. */
  function option(mode) {
    const attrs = {
      "data-density-option": mode,
      href: (opts.hrefBase || "/user/database/datasets/D1?") + "density=" + mode,
    };
    const el = {
      _attrs: attrs,
      hasAttribute: (n) => n in attrs,
      getAttribute: (n) => (n in attrs ? attrs[n] : null),
      setAttribute: (n, v) => { attrs[n] = v; },
      parentNode: null,
    };
    return el;
  }

  const densityOptions = [option("compact"), option("comfortable")];
  const listeners = {};

  const location = {
    pathname: "/user/database/datasets/D1",
    search: opts.search || "",
    get href() { return this.pathname + this.search; },
    set href(v) { result.navigated = true; },
  };

  /* A history stack the popstate handler can move through, mirroring what a
     browser does with pushState entries. */
  const stack = [{ url: location.pathname + location.search }];
  let index = 0;

  const history = {
    pushState(state, title, url) {
      stack.length = index + 1;
      stack.push({ url: String(url), state });
      index = stack.length - 1;
      applyUrl(String(url));
      result.pushed.push(String(url));
    },
    replaceState(state, title, url) {
      stack[index] = { url: String(url), state };
      applyUrl(String(url));
    },
    go(delta) {
      const next = index + delta;
      if (next < 0 || next >= stack.length) {
        return;
      }
      index = next;
      applyUrl(stack[index].url);
      (listeners.popstate || []).forEach((fn) => fn({ state: stack[index].state }));
    },
  };

  function applyUrl(url) {
    const q = url.indexOf("?");
    location.pathname = q === -1 ? url : url.slice(0, q);
    location.search = q === -1 ? "" : url.slice(q);
  }

  global.document = {
    readyState: "complete",
    documentElement: {},
    addEventListener: (type, fn) => { (listeners[type] || (listeners[type] = [])).push(fn); },
    querySelector: (sel) => (sel.indexOf("db-sheet") !== -1 ? sheet : null),
    querySelectorAll: (sel) => (sel.indexOf("density-option") !== -1 ? densityOptions : []),
    createElement: () => ({ style: {}, setAttribute() {}, select() {}, parentNode: { removeChild() {} } }),
    body: { appendChild() {} },
    execCommand: () => true,
  };

  global.window = {
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = v; },
      removeItem: (k) => { delete store[k]; },
    },
    location,
    history,
    addEventListener: (type, fn) => { (listeners[type] || (listeners[type] = [])).push(fn); },
    setTimeout() {},
  };
  global.navigator = {};

  function click(mode) {
    const target = densityOptions[mode === "compact" ? 0 : 1];
    target.parentNode = global.document;
    (listeners.click || []).forEach((fn) =>
      fn({ target, preventDefault: () => { result.prevented = true; } })
    );
  }

  function snapshot() {
    return {
      dom: sheetAttrs["data-density"],
      stored: store["logplatform.database.density"] || null,
      url: location.pathname + location.search,
      pressed: densityOptions.map((o) => o.getAttribute("aria-pressed")),
      navigated: result.navigated,
      prevented: result.prevented,
      pushed: result.pushed.slice(),
    };
  }

  return { click, history, snapshot, load: () => eval(fs.readFileSync(SCRIPT, "utf8")) };
}

const scenario = process.argv[2];
const args = process.argv[3] ? JSON.parse(process.argv[3]) : {};
const out = {};

if (scenario === "url-sync") {
  /* Opened at ?density=comfortable with other view state; user picks Zwarta. */
  const h = makeHarness({
    rendered: "comfortable",
    locked: true,
    search: "?filter__driver_name=Ali&page=3&density=comfortable",
    hrefBase: "/user/database/datasets/D1?filter__driver_name=Ali&page=3&",
  });
  h.load();
  out.before = h.snapshot();
  h.click("compact");
  out.after = h.snapshot();
} else if (scenario === "history") {
  const h = makeHarness({
    rendered: "comfortable",
    locked: true,
    search: "?density=comfortable",
    hrefBase: "/user/database/datasets/D1?",
  });
  h.load();
  out.start = h.snapshot();
  h.click("compact");
  out.afterClick = h.snapshot();
  h.history.go(-1);
  out.afterBack = h.snapshot();
  h.history.go(1);
  out.afterForward = h.snapshot();
} else if (scenario === "precedence") {
  const h = makeHarness(args);
  h.load();
  out.state = h.snapshot();
} else {
  throw new Error("unknown scenario: " + scenario);
}

process.stdout.write(JSON.stringify(out));
