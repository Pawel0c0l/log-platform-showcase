/* DOM stub that executes the real api/static/js/data-grid-states.js.
 *
 * The parts of the S9 contract that live in the browser — raising the pending
 * skeleton for a navigation the browser has actually accepted, never persisting
 * that flag, clearing it on `pageshow`, leaving editable controls alone, and
 * copying exactly the server-rendered error reference — are exercised by running
 * the shipped file rather than by reading it.
 *
 * Invoked by ops/tests_manual/test_portal_database_catalogue_and_states.py:
 *
 *     node ops/tests_manual/data_grid_states_harness.js <scenario> [json-args]
 *
 * Prints one JSON object describing the observed end state. Any storage write,
 * URL change or network attempt is recorded, because the pending state must make
 * none of them.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const SCRIPT = path.join(__dirname, "..", "..", "api", "static", "js", "data-grid-states.js");

/* ------------------------------------------------------------------ DOM stub */

function makeElement(tag, attrs, options) {
  const opts = options || {};
  const el = {
    tagName: tag,
    _attrs: Object.assign({}, attrs || {}),
    _children: [],
    _classes: (attrs && attrs.class ? String(attrs.class).split(/\s+/) : []),
    style: {},
    parentNode: null,
    disabled: !!opts.disabled,
    readOnly: !!opts.readOnly,
    hasAttribute(name) { return name in this._attrs; },
    getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; },
    setAttribute(name, value) { this._attrs[name] = String(value); },
    removeAttribute(name) { delete this._attrs[name]; },
    appendChild(child) { child.parentNode = this; this._children.push(child); return child; },
    removeChild(child) {
      const i = this._children.indexOf(child);
      if (i !== -1) { this._children.splice(i, 1); child.parentNode = null; }
      return child;
    },
    get className() { return this._classes.join(" "); },
    set className(value) { this._classes = String(value).split(/\s+/).filter(Boolean); },
    getBoundingClientRect() { return { width: 100, height: 20 }; },
    querySelector(selector) { return descendants(this).find((n) => matches(n, selector)) || null; },
    querySelectorAll(selector) { return descendants(this).filter((n) => matches(n, selector)); },
  };
  return el;
}

function descendants(node) {
  const out = [];
  (node._children || []).forEach((child) => {
    out.push(child);
    descendants(child).forEach((n) => out.push(n));
  });
  return out;
}

/* A deliberately tiny selector engine: only the forms the module actually uses
   (`.class`, `tag`, `[attr]`, and one descendant combinator). Anything wider
   would be a second implementation of the browser rather than a stub. */
function matchesSimple(node, token) {
  if (token.charAt(0) === ".") {
    return (node._classes || []).indexOf(token.slice(1)) !== -1;
  }
  if (token.charAt(0) === "[") {
    const name = token.slice(1, -1);
    return node.hasAttribute && node.hasAttribute(name);
  }
  return node.tagName === token;
}

function matches(node, selector) {
  const parts = String(selector).trim().split(/\s+/);
  if (parts.length === 1) {
    return matchesSimple(node, parts[0]);
  }
  if (!matchesSimple(node, parts[parts.length - 1])) {
    return false;
  }
  let current = node.parentNode;
  let index = parts.length - 2;
  while (current && index >= 0) {
    if (matchesSimple(current, parts[index])) {
      index -= 1;
    }
    current = current.parentNode;
  }
  return index < 0;
}

/* --------------------------------------------------------------- the page */

function buildPage(options) {
  const opts = options || {};
  const pageSize = opts.pageSize || 25;
  const renderedRows = opts.renderedRows === undefined ? 3 : opts.renderedRows;

  const sheet = makeElement("div", {
    "data-db-sheet": "",
    "data-db-page-size": String(pageSize),
    class: "db-browser db-sheet",
  });

  const viewport = makeElement("div", { class: "db-table-viewport" });
  const table = makeElement("table", { class: "db-table" });
  const colgroup = makeElement("colgroup", {});
  ["160px", "220px", "120px"].forEach((width) => {
    const col = makeElement("col", {});
    col.style.width = width;
    colgroup.appendChild(col);
  });
  table.appendChild(colgroup);
  const tbody = makeElement("tbody", {});
  for (let i = 0; i < renderedRows; i += 1) {
    tbody.appendChild(makeElement("tr", {}));
  }
  table.appendChild(tbody);
  viewport.appendChild(table);
  sheet.appendChild(viewport);

  const toolbar = makeElement("div", { class: "db-toolbar" });
  const counter = makeElement("span", { class: "db-counter" });
  toolbar.appendChild(counter);

  /* The filter Apply form, with the submit button the S3 duplicate-submit guard
     owns, plus an ordinary text input that must stay editable. */
  const form = makeElement("form", { method: "get", class: "db-panel-form" });
  const submit = makeElement("button", { type: "submit", class: "db-col-apply" });
  const input = makeElement("input", { type: "search", name: "search" });
  form.appendChild(submit);
  form.appendChild(input);
  toolbar.appendChild(form);

  const sortLink = makeElement("a", { href: "/user/database/datasets/D1?sort=trip_date&direction=asc" });
  const densityLink = makeElement("a", {
    href: "/user/database/datasets/D1?density=comfortable",
    "data-density-option": "comfortable",
  });
  toolbar.appendChild(sortLink);
  toolbar.appendChild(densityLink);
  sheet.appendChild(toolbar);

  const copyButton = makeElement("button", {
    type: "button",
    "data-db-error-copy": opts.reference || "7f0ca41b7f0ca41b",
  });
  sheet.appendChild(copyButton);

  return { sheet, viewport, table, tbody, counter, form, submit, input, sortLink, densityLink, copyButton };
}

function makeHarness(options) {
  const page = buildPage(options);
  const result = {
    storageWrites: [],
    urlChanges: [],
    fetches: [],
    copied: null,
    prevented: false,
  };

  const listeners = {};
  const windowListeners = {};

  const root = makeElement("html", {});
  root.appendChild(page.sheet);

  const document = {
    readyState: "complete",
    documentElement: root,
    _children: root._children,
    addEventListener(type, handler) {
      (listeners[type] = listeners[type] || []).push(handler);
    },
    createElement(tag) { return makeElement(tag, {}); },
    querySelector(selector) { return matches(page.sheet, selector) ? page.sheet : page.sheet.querySelector(selector); },
    querySelectorAll(selector) {
      const found = page.sheet.querySelectorAll(selector);
      return matches(page.sheet, selector) ? [page.sheet].concat(found) : found;
    },
  };

  const localStorage = {
    getItem() { return null; },
    setItem(key, value) { result.storageWrites.push([key, value]); },
  };

  const history = {
    pushState(state, title, url) { result.urlChanges.push(String(url)); },
    replaceState(state, title, url) { result.urlChanges.push(String(url)); },
  };

  const win = {
    document,
    localStorage,
    history,
    navigator: {
      clipboard: { writeText(text) { result.copied = String(text); return Promise.resolve(); } },
    },
    location: { pathname: "/user/database/datasets/D1", search: "" },
    setTimeout(fn) { fn(); return 0; },
    addEventListener(type, handler) {
      (windowListeners[type] = windowListeners[type] || []).push(handler);
    },
    fetch() { result.fetches.push("fetch"); return Promise.resolve({}); },
  };

  function dispatch(type, event) {
    (listeners[type] || []).forEach((handler) => handler(event));
  }

  function dispatchWindow(type, event) {
    (windowListeners[type] || []).forEach((handler) => handler(event));
  }

  function makeEvent(target, extra) {
    const event = Object.assign({
      target,
      defaultPrevented: false,
      button: 0,
      metaKey: false, ctrlKey: false, shiftKey: false, altKey: false,
      preventDefault() { this.defaultPrevented = true; result.prevented = true; },
    }, extra || {});
    return event;
  }

  return { page, result, win, document, dispatch, dispatchWindow, makeEvent };
}

function run(harness) {
  const source = fs.readFileSync(SCRIPT, "utf-8");
  const sandbox = {
    window: harness.win,
    document: harness.document,
    navigator: harness.win.navigator,
    console,
  };
  const fn = new Function("window", "document", "navigator", source);
  fn.call(sandbox, harness.win, harness.document, harness.win.navigator);
}

function snapshot(harness, extra) {
  const page = harness.page;
  const skeleton = page.viewport.querySelector(".db-skeleton");
  const rows = skeleton ? skeleton._children.length : 0;
  const cellText = skeleton
    ? skeleton._children
        .map((row) => row._children.map((cell) => cell.textContent || "").join(""))
        .join("")
    : "";
  return Object.assign({
    pending: page.sheet.hasAttribute("data-db-pending"),
    ariaBusy: page.viewport.getAttribute("aria-busy"),
    skeletonRows: rows,
    skeletonText: cellText,
    counterPending: page.counter.hasAttribute("data-db-pending-counter"),
    submitDisabled: !!page.submit.disabled,
    inputDisabled: !!page.input.disabled,
    inputReadOnly: !!page.input.readOnly,
    storageWrites: harness.result.storageWrites,
    urlChanges: harness.result.urlChanges,
    fetches: harness.result.fetches,
    copied: harness.result.copied,
    prevented: harness.result.prevented,
  }, extra || {});
}

/* ------------------------------------------------------------- scenarios */

const scenarios = {
  submit(args) {
    const harness = makeHarness(args);
    run(harness);
    harness.dispatch("submit", harness.makeEvent(harness.page.form));
    return snapshot(harness);
  },

  sort_link(args) {
    const harness = makeHarness(args);
    run(harness);
    const event = harness.makeEvent(harness.page.sortLink);
    harness.dispatch("click", event);
    return snapshot(harness, { prevented: event.defaultPrevented });
  },

  density_link(args) {
    const harness = makeHarness(args);
    run(harness);
    harness.dispatch("click", harness.makeEvent(harness.page.densityLink));
    return snapshot(harness);
  },

  bfcache(args) {
    const harness = makeHarness(args);
    run(harness);
    harness.dispatch("submit", harness.makeEvent(harness.page.form));
    /* The S3 guard disables the submit for the lifetime of the navigation; the
       browser then restores this exact DOM from the back-forward cache. */
    harness.page.submit.disabled = true;
    const pendingBefore = harness.page.sheet.hasAttribute("data-db-pending");
    harness.dispatchWindow("pageshow", { persisted: true });
    harness.page.submit.disabled = false;
    return snapshot(harness, { pendingBefore });
  },

  controls(args) {
    const harness = makeHarness(args);
    run(harness);
    harness.dispatch("submit", harness.makeEvent(harness.page.form));
    return snapshot(harness);
  },

  copy_reference(args) {
    const harness = makeHarness(args);
    run(harness);
    harness.dispatch("click", harness.makeEvent(harness.page.copyButton));
    return snapshot(harness);
  },
};

const name = process.argv[2];
const args = process.argv[3] ? JSON.parse(process.argv[3]) : {};
if (!scenarios[name]) {
  console.error(`unknown scenario: ${name}`);
  process.exit(2);
}
process.stdout.write(JSON.stringify(scenarios[name](args)));
