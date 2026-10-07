/* DOM stub that executes the real api/static/js/theme.js.
 *
 * The parts of `D-011` / `SH-8` that live in JavaScript — which authority
 * decides the mode, whether the browser mirror may ever act as account state,
 * and what happens when a user changes theme faster than the server answers —
 * are exercised by running the shipped file rather than by reading it.
 *
 * Invoked by ops/tests_manual/test_portal_server_preferences_and_saved_views.py:
 *
 *     node ops/tests_manual/theme_engine_harness.js <scenario> [script-path]
 *
 * The optional script path exists so the SAME scenarios can be replayed against
 * an earlier revision of `theme.js` to show a corrected defect actually failing
 * there. It defaults to the shipped file.
 *
 * Prints one JSON object describing the observed end state. There is no real
 * network: `fetch` is a recorder whose responses are resolved by an explicit
 * plan, so a response arriving out of order is a scenario rather than a race.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const DEFAULT_SCRIPT = path.join(__dirname, "..", "..", "api", "static", "js", "theme.js");

/* ------------------------------------------------------------------ DOM ---- */

function makeNode(tag, attrs) {
  const node = {
    _tag: tag,
    _attrs: Object.assign({}, attrs || {}),
    nodeType: 1,
    childNodes: [],
    parentNode: null,
    textContent: "",
    value: "",
    hasAttribute(name) { return name in this._attrs; },
    getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; },
    setAttribute(name, value) { this._attrs[name] = String(value); },
    removeAttribute(name) { delete this._attrs[name]; },
    addEventListener() {},
    querySelector(selector) { return query(this, selector)[0] || null; },
    querySelectorAll(selector) { return query(this, selector); },
  };
  return node;
}

function append(parent, child) {
  child.parentNode = parent;
  parent.childNodes.push(child);
  return child;
}

function walk(root, visit) {
  visit(root);
  root.childNodes.forEach((child) => walk(child, visit));
}

/* A deliberately small selector engine: only the shapes theme.js uses. An
   unsupported selector throws rather than silently matching nothing, so the
   harness cannot pass by accident. */
function matches(node, selector) {
  const sel = selector.trim();
  if (sel.charAt(0) === "[" && sel.charAt(sel.length - 1) === "]") {
    const inner = sel.slice(1, -1);
    const eq = inner.indexOf("=");
    if (eq === -1) {
      return node.hasAttribute(inner);
    }
    const name = inner.slice(0, eq);
    const wanted = inner.slice(eq + 1).replace(/^["']|["']$/g, "");
    return node.getAttribute(name) === wanted;
  }
  if (/^[a-z]+\[[^\]]+\]$/.test(sel)) {
    const tag = sel.slice(0, sel.indexOf("["));
    return node._tag === tag && matches(node, sel.slice(sel.indexOf("[")));
  }
  throw new Error("unsupported selector: " + selector);
}

function query(root, selector) {
  const found = [];
  walk(root, (node) => {
    if (node !== root && matches(node, selector)) {
      found.push(node);
    }
  });
  return found;
}

/* --------------------------------------------------------------- build ---- */

function buildDocument(options) {
  const root = makeNode("html", options.rootAttrs || {});
  const body = append(root, makeNode("body", {}));

  let switcher;
  if (options.durable) {
    switcher = append(body, makeNode("form", {
      "data-theme-switcher": "",
      "data-theme-form": "",
      action: "/user/preferences/theme",
      "data-theme-failed-message": "NOT_STORED",
    }));
    const next = append(switcher, makeNode("input", { name: "next" }));
    next.value = "/user/database";
  } else {
    const attrs = { "data-theme-switcher": "", hidden: "" };
    if (options.unavailableMessage) {
      attrs["data-theme-failed-message"] = "NOT_STORED";
    }
    switcher = append(body, makeNode("div", attrs));
  }
  ["auto", "light", "dark"].forEach((mode) => {
    append(switcher, makeNode("button", { "data-theme-option": mode, "aria-pressed": "false" }));
  });
  append(switcher, makeNode("p", { "data-theme-status": "" }));

  const document = {
    documentElement: root,
    readyState: "complete",
    _listeners: {},
    addEventListener(type, fn) { (this._listeners[type] || (this._listeners[type] = [])).push(fn); },
    querySelector(selector) { return query(root, selector)[0] || null; },
    querySelectorAll(selector) { return query(root, selector); },
  };
  return { document, root, switcher };
}

/* ------------------------------------------------------------- runtime ---- */

function run(scenario, scriptPath) {
  const plan = SCENARIOS[scenario];
  if (!plan) {
    throw new Error("unknown scenario: " + scenario);
  }
  const built = buildDocument(plan);
  const store = Object.assign({}, plan.storage || {});
  const requests = [];
  const pending = [];
  let inFlight = 0;
  let maxConcurrent = 0;
  /* The account row, as the server would hold it: written when the request is
     DELIVERED, which for this harness is the moment its response resolves. That
     is what makes an out-of-order completion able to leave the server on an
     earlier choice than the user's last one. */
  let serverValue = plan.serverValue || "auto";

  const window = {
    localStorage: {
      getItem(key) { return key in store ? store[key] : null; },
      setItem(key, value) { store[key] = String(value); },
      removeItem(key) { delete store[key]; },
    },
    fetch(url, init) {
      const body = String((init && init.body) || "");
      const mode = decodeURIComponent((/(?:^|&)theme=([^&]*)/.exec(body) || [])[1] || "");
      const index = requests.length;
      requests.push({ index: index, mode: mode, url: url });
      inFlight += 1;
      maxConcurrent = Math.max(maxConcurrent, inFlight);
      return new Promise((resolve) => {
        pending.push({
          index: index,
          mode: mode,
          order: (plan.completionOrder && plan.completionOrder[index] !== undefined)
            ? plan.completionOrder[index]
            : index,
          settle: () => {
            inFlight -= 1;
            const stored = !(plan.failures || []).includes(index);
            /* `storedAs` lets a plan say the server durably stored something
               other than what was sent, so "the client adopts the mode the
               SERVER reports" is testable rather than indistinguishable from
               "the client echoes what it sent". */
            const persisted = (plan.storedAs && plan.storedAs[index]) || mode;
            if (stored) {
              serverValue = persisted;
            }
            resolve({
              ok: stored,
              json: () => Promise.resolve({ stored: stored, theme: stored ? persisted : mode }),
            });
          },
        });
      });
    },
  };

  if (plan.noFetch) {
    /* No transport at all: the durable form is present but the choice cannot
       reach the account row. */
    delete window.fetch;
  }

  const sandbox = { window: window, document: built.document, URLSearchParams: URLSearchParams,
                    Promise: Promise, console: console };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(scriptPath, "utf-8"), sandbox, { filename: scriptPath });

  const api = window.LogPlatformTheme;

  function tick() {
    /* Let every queued microtask and promise continuation run. */
    return new Promise((resolve) => setImmediate(resolve));
  }

  function settleOne() {
    if (!pending.length) {
      return Promise.resolve(false);
    }
    /* Lowest `order` first: a plan can therefore deliver a LATER request's
       response before an EARLIER one's, which is the reordering the review
       described. Code that serializes its writes never has two to reorder,
       and that is exactly what the scenario proves. */
    pending.sort((a, b) => a.order - b.order);
    pending.shift().settle();
    return tick().then(() => true);
  }

  function drain() {
    return settleOne().then((did) => (did ? drain() : undefined));
  }

  /* A step list runs clicks and responses in an exact interleaving. Plain
     `clicks` is the burst case: every click happens before the network can
     answer, which is the reviewed scenario. */
  const steps = plan.steps
    ? plan.steps.slice()
    : (plan.clicks || []).map((mode) => ["click", mode]);

  let chain = Promise.resolve();
  steps.forEach((step) => {
    chain = chain.then(() => {
      if (step[0] === "click") {
        api.set(step[1]);
        return undefined;
      }
      if (step[0] === "settle") {
        return settleOne();
      }
      throw new Error("unknown step: " + step[0]);
    });
  });

  return chain.then(drain).then(function () {
    const pressed = built.document.querySelectorAll("[data-theme-option]")
      .filter((node) => node.getAttribute("aria-pressed") === "true")
      .map((node) => node.getAttribute("data-theme-option"));
    const status = built.document.querySelectorAll("[data-theme-status]")
      .map((node) => node.textContent || "");
    return {
      scenario: scenario,
      current: api.get(),
      dataTheme: built.root.getAttribute("data-theme"),
      mirror: store["logplatform.theme"] === undefined ? null : store["logplatform.theme"],
      pressed: pressed,
      status: status,
      serverValue: serverValue,
      requestModes: requests.map((r) => r.mode),
      maxConcurrent: maxConcurrent,
      switcherHidden: built.switcher.hasAttribute("hidden"),
    };
  });
}

/* ----------------------------------------------------------- scenarios ---- */

const SERVER = { durable: true, rootAttrs: { "data-theme-scope": "server", "data-theme-mode": "auto" } };

const SCENARIOS = {
  /* Account A left "dark" in this browser; account B's row says light. */
  "server-authority-overwrites-mirror": Object.assign({}, SERVER, {
    rootAttrs: { "data-theme-scope": "server", "data-theme-mode": "light" },
    storage: { "logplatform.theme": "dark" },
  }),
  /* The same browser, an account whose row says AUTO. */
  "server-authority-auto-clears-mirror": Object.assign({}, SERVER, {
    rootAttrs: { "data-theme-scope": "server", "data-theme-mode": "auto" },
    storage: { "logplatform.theme": "dark" },
  }),
  /* Account B signs in while the platform database is unhealthy. */
  "unavailable-authority-ignores-mirror": {
    durable: false,
    unavailableMessage: true,
    rootAttrs: { "data-theme-scope": "unavailable", "data-theme-mode": "auto" },
    storage: { "logplatform.theme": "dark" },
  },
  /* A choice made while the preference is unreadable applies to this document
     and must not be written to the mirror as if it were durable. */
  "unavailable-authority-choice-is-not-durable": {
    durable: false,
    unavailableMessage: true,
    rootAttrs: { "data-theme-scope": "unavailable", "data-theme-mode": "auto" },
    storage: { "logplatform.theme": "dark" },
    clicks: ["light"],
  },
  /* CONFIRMED pre-S13: the documented browser-local tolerance is unchanged. */
  "local-authority-uses-mirror": {
    durable: false,
    rootAttrs: {},
    storage: { "logplatform.theme": "dark" },
  },
  "local-authority-writes-mirror": {
    durable: false,
    rootAttrs: {},
    storage: {},
    clicks: ["dark"],
  },

  /* dark, then light, with the responses arriving in the WRONG order. */
  "race-two-clicks-reordered-responses": Object.assign({}, SERVER, {
    clicks: ["dark", "light"],
    completionOrder: [1, 0],
  }),
  /* A -> B -> C, every response reordered against its request. */
  "race-three-clicks-reordered-responses": Object.assign({}, SERVER, {
    clicks: ["dark", "auto", "light"],
    completionOrder: [2, 1, 0],
  }),
  /* Three clicks with no chance to answer in between: the intermediate intent
     is coalesced away and the LAST one is what the server is asked for. */
  "race-burst-coalesces-to-the-last-choice": Object.assign({}, SERVER, {
    clicks: ["dark", "auto", "light"],
    completionOrder: [2, 1, 0],
  }),
  /* Three real writes, and it is the MIDDLE one the server refuses. The
     failure must not survive into the settled state. */
  "race-middle-write-fails": Object.assign({}, SERVER, {
    steps: [["click", "dark"], ["settle"], ["click", "auto"], ["settle"], ["click", "light"], ["settle"]],
    failures: [1],
  }),
  /* Three real writes where the LAST is refused: the refusal is stated, the
     mirror is not written, and the earlier success does not resurface. */
  "race-three-writes-last-fails": Object.assign({}, SERVER, {
    steps: [["click", "dark"], ["settle"], ["click", "auto"], ["settle"], ["click", "light"], ["settle"]],
    failures: [2],
  }),
  /* The LAST write fails: it must be stated and must not touch the mirror. */
  "race-last-write-fails": Object.assign({}, SERVER, {
    clicks: ["dark", "light"],
    completionOrder: [1, 0],
    failures: [1],
  }),
  /* An earlier write fails and the user then chooses again successfully: the
     stale failure must not keep claiming the surface. */
  "race-recovers-after-failure": Object.assign({}, SERVER, {
    clicks: ["dark", "light"],
    failures: [0],
  }),

  /* ---- S13 final closure: durable ordering after a latest-intent failure ---- */

  /* One write, accepted. The simplest settled state there is. */
  "single-write-succeeds": Object.assign({}, SERVER, {
    steps: [["click", "dark"], ["settle"]],
  }),
  /* THE REVIEWED SEQUENCE. The page was rendered from an account row saying
     AUTO. `dark` is chosen and DURABLY STORED. `light` is chosen while that is
     still in flight, and its write FAILS. The reviewed engine settled on
     UI=light, mirror=auto, server=dark — three different answers to one
     question. The surface must end on the last confirmed durable value. */
  "superseded-success-then-latest-failure": Object.assign({}, SERVER, {
    clicks: ["dark", "light"],
    failures: [1],
  }),
  /* The same shape written as an explicit interleaving: the superseded success
     is DELIVERED before the latest request is even issued. */
  "superseded-success-delivered-before-latest-request": Object.assign({}, SERVER, {
    steps: [["click", "dark"], ["click", "light"], ["settle"], ["settle"]],
    failures: [1],
  }),
  /* Nothing was ever confirmed in this session: the page's own rendered account
     value is the last confirmed durable one. */
  "latest-failure-with-no-confirmed-write": Object.assign({}, SERVER, {
    rootAttrs: { "data-theme-scope": "server", "data-theme-mode": "dark" },
    storage: { "logplatform.theme": "dark" },
    serverValue: "dark",
    steps: [["click", "light"], ["settle"]],
    failures: [0],
  }),
  /* The server is the authority on what it stored, including when that is not
     what this client sent. */
  "server-reports-the-mode-it-stored": Object.assign({}, SERVER, {
    steps: [["click", "light"], ["settle"]],
    storedAs: { 0: "dark" },
  }),
  /* A durable form with no transport: the choice was never persisted and must
     not be left looking as though it was. */
  "no-transport-cannot-persist": Object.assign({}, SERVER, {
    rootAttrs: { "data-theme-scope": "server", "data-theme-mode": "dark" },
    storage: { "logplatform.theme": "dark" },
    serverValue: "dark",
    noFetch: true,
    clicks: ["light"],
  }),
};

const scenario = process.argv[2];
const scriptPath = process.argv[3] ? path.resolve(process.argv[3]) : DEFAULT_SCRIPT;
run(scenario, scriptPath).then(
  (result) => { process.stdout.write(JSON.stringify(result)); },
  (error) => { process.stdout.write(JSON.stringify({ error: String((error && error.stack) || error) })); process.exitCode = 1; }
);
