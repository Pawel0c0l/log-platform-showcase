/* DOM stub that executes the real api/static/js/data-grid-row-detail.js.
 *
 * The parts of the S6 contract that live in JavaScript — opening a row by click
 * or Enter, Esc closing, focus returning to the row that opened the panel, table
 * scroll preservation and ↑/↓ traversal — are exercised by running the shipped
 * file rather than by reading it.
 *
 * Invoked by ops/tests_manual/test_portal_database_hidden_row_identity.py:
 *
 *     node ops/tests_manual/data_grid_row_detail_harness.js <scenario> [json-args]
 *
 * Prints one JSON object describing the observed end state. Every navigation is
 * recorded with its target so the tests can assert that the URL carries the
 * opaque reference and never a raw identifier, and that the script issues no
 * fetch of its own.
 */
"use strict";

const path = require("path");

const SCRIPT = path.join(__dirname, "..", "..", "api", "static", "js", "data-grid-row-detail.js");

/* Opaque references as the server would emit them: base64url-ish blobs bearing
 * no relation to the identity they stand for. The harness never holds the raw
 * identity at all, which is the point of the contract. */
const TOKENS = ["tok-AAAAref0000", "tok-BBBBref1111", "tok-CCCCref2222"];
const ROUTE = "/user/database/datasets/ds-1";
const VIEW = "sort=trip_start&direction=desc&limit=50&colorder=driver_name";

/* --------------------------------------------------------------------- DOM */

let seq = 0;
const navigations = [];
let fetches = 0;

function makeNode(tag, attrs) {
  const node = {
    _tag: String(tag).toLowerCase(),
    _attrs: Object.assign({}, attrs || {}),
    _id: (seq += 1),
    _listeners: {},
    nodeType: 1,
    childNodes: [],
    parentNode: null,
    style: {},
    scrollLeft: 0,
    scrollTop: 0,
    get tagName() { return this._tag.toUpperCase(); },
    hasAttribute(name) { return name in this._attrs; },
    getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; },
    setAttribute(name, value) { this._attrs[name] = String(value); },
    removeAttribute(name) { delete this._attrs[name]; },
    addEventListener(type, fn) { (this._listeners[type] || (this._listeners[type] = [])).push(fn); },
    focus() { global.__focused = this; },
    appendChild(child) { child.parentNode = this; this.childNodes.push(child); return child; },
    querySelector(sel) { return query(this, sel, true)[0] || null; },
    querySelectorAll(sel) { return query(this, sel, false); },
    closest(sel) {
      let cursor = this;
      while (cursor) {
        if (matches(cursor, sel)) { return cursor; }
        cursor = cursor.parentNode;
      }
      return null;
    },
    dispatch(type, event) {
      /* Bubble like a real listener chain so delegated handlers on the table and
       * on document both see the event. */
      const ev = Object.assign({ type: type, target: this, preventDefault() { this.defaultPrevented = true; }, defaultPrevented: false }, event || {});
      let cursor = this;
      while (cursor) {
        const list = cursor._listeners[type] || [];
        for (let i = 0; i < list.length; i += 1) { list[i].call(cursor, ev); }
        cursor = cursor.parentNode;
      }
      const docList = document._listeners[type] || [];
      for (let i = 0; i < docList.length; i += 1) { docList[i].call(document, ev); }
      return ev;
    }
  };
  return node;
}

function descendants(root) {
  const out = [];
  (function walk(node) {
    for (let i = 0; i < node.childNodes.length; i += 1) {
      out.push(node.childNodes[i]);
      walk(node.childNodes[i]);
    }
  })(root);
  return out;
}

/* A deliberately small selector engine: only the forms the shipped script uses. */
function matchesOne(node, sel) {
  sel = sel.trim();
  if (!sel) { return false; }
  if (sel.indexOf(",") !== -1) {
    return sel.split(",").some(function (part) { return matchesOne(node, part); });
  }
  let rest = sel;
  let tag = null;
  const tagMatch = /^([a-zA-Z]+)/.exec(rest);
  if (tagMatch) { tag = tagMatch[1].toLowerCase(); rest = rest.slice(tagMatch[1].length); }
  if (tag && node._tag !== tag) { return false; }
  const attrRe = /\[([^\]=]+)(?:=(?:'([^']*)'|"([^"]*)"))?\]/g;
  let m;
  while ((m = attrRe.exec(rest))) {
    const name = m[1];
    const want = m[2] !== undefined ? m[2] : m[3];
    if (!(name in node._attrs)) { return false; }
    if (want !== undefined && String(node._attrs[name]) !== want) { return false; }
  }
  const classRe = /\.([a-zA-Z0-9_-]+)/g;
  while ((m = classRe.exec(rest))) {
    const classes = String(node._attrs["class"] || "").split(/\s+/);
    if (classes.indexOf(m[1]) === -1) { return false; }
  }
  return true;
}

function matches(node, sel) {
  return sel.split(",").some(function (part) { return matchesOne(node, part.trim()); });
}

function query(root, sel, first) {
  const parts = sel.split(",").map(function (s) { return s.trim(); });
  const all = descendants(root);
  const out = [];
  for (let i = 0; i < all.length; i += 1) {
    /* Support the one descendant selector the script uses: "tbody tr[...]". */
    for (let p = 0; p < parts.length; p += 1) {
      const chunks = parts[p].split(/\s+/);
      const leaf = chunks[chunks.length - 1];
      if (!matchesOne(all[i], leaf)) { continue; }
      if (chunks.length > 1) {
        let ok = false;
        let cursor = all[i].parentNode;
        while (cursor) { if (matchesOne(cursor, chunks[0])) { ok = true; break; } cursor = cursor.parentNode; }
        if (!ok) { continue; }
      }
      out.push(all[i]);
      break;
    }
    if (first && out.length) { break; }
  }
  return out;
}

const document = {
  _listeners: {},
  readyState: "complete",
  addEventListener(type, fn) { (this._listeners[type] || (this._listeners[type] = [])).push(fn); },
  querySelector(sel) { return query(document.body, sel, true)[0] || null; },
  querySelectorAll(sel) { return query(document.body, sel, false); }
};

const storage = {
  _data: {},
  getItem(k) { return k in this._data ? this._data[k] : null; },
  setItem(k, v) { this._data[k] = String(v); },
  removeItem(k) { delete this._data[k]; }
};

global.document = document;
global.window = {
  sessionStorage: storage,
  CSS: { escape: function (v) { return String(v); } },
  location: {
    href: ROUTE + "?" + VIEW,
    assign(href) { navigations.push(String(href)); }
  },
  addEventListener() {}
};
global.fetch = function () { fetches += 1; return Promise.resolve({ ok: true }); };

/* ------------------------------------------------------------------ fixture */

function buildSheet(options) {
  options = options || {};
  /* Explicit undefined check: row 0 is a legitimate open row, and `|| null`
   * would silently treat it as "nothing open". */
  const open = options.open === undefined || options.open === null ? null : options.open;

  document.body = makeNode("body");
  const scroll = makeNode("div", { "data-db-scroll": "" });
  scroll.scrollLeft = 640;
  scroll.scrollTop = 320;
  document.body.appendChild(scroll);

  const table = makeNode("table", { "class": "db-table" });
  scroll.appendChild(table);
  const tbody = makeNode("tbody", {});
  table.appendChild(tbody);

  const rows = TOKENS.map(function (token, index) {
    const tr = makeNode("tr", { "data-db-row": token });
    if (open === index) { tr.setAttribute("class", "db-row db-row-selected"); }
    tbody.appendChild(tr);
    const cell = makeNode("td", {});
    tr.appendChild(cell);
    const link = makeNode("a", {
      "data-db-row-open": "",
      href: ROUTE + "?" + VIEW + "&row=" + token
    });
    cell.appendChild(link);
    return tr;
  });

  let panel = null;
  if (open !== null) {
    panel = makeNode("aside", { "data-db-row-detail": "", "data-db-row-current": TOKENS[open], "class": "db-row-detail" });
    document.body.appendChild(panel);
    const heading = makeNode("h3", { "data-db-row-heading": "" });
    panel.appendChild(heading);
    const close = makeNode("a", { "data-db-row-close": "", href: ROUTE + "?" + VIEW });
    panel.appendChild(close);
    if (open > 0) {
      panel.appendChild(makeNode("a", {
        "data-db-row-traverse": "previous",
        href: ROUTE + "?" + VIEW + "&row=" + TOKENS[open - 1]
      }));
    }
    if (open < TOKENS.length - 1) {
      panel.appendChild(makeNode("a", {
        "data-db-row-traverse": "next",
        href: ROUTE + "?" + VIEW + "&row=" + TOKENS[open + 1]
      }));
    }
  }
  return { scroll: scroll, table: table, rows: rows, panel: panel };
}

function run() {
  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
}

function result(extra) {
  return Object.assign({
    navigations: navigations.slice(),
    fetches: fetches,
    focused: global.__focused ? (global.__focused.getAttribute("data-db-row") || global.__focused._tag) : null,
    stored: Object.assign({}, storage._data)
  }, extra || {});
}

/* ---------------------------------------------------------------- scenarios */

const scenarios = {
  /* Clicking a row opens it, and the URL carries the opaque reference. */
  "open-by-click": function () {
    const sheet = buildSheet({});
    run();
    sheet.rows[1].dispatch("click");
    return result({ tabindex: sheet.rows.map(function (r) { return r.getAttribute("tabindex"); }) });
  },

  /* Enter on a focused row does the same, so a mouse is not required. */
  "open-by-enter": function () {
    const sheet = buildSheet({});
    run();
    sheet.rows[2].dispatch("keydown", { key: "Enter" });
    return result({});
  },

  /* A click on a header menu, a resize handle or a link keeps its own meaning. */
  "interactive-targets-do-not-open": function () {
    const sheet = buildSheet({});
    run();
    const cell = sheet.rows[0].childNodes[0];
    const menu = makeNode("button", { "data-db-col-menu": "trip_start" });
    cell.appendChild(menu);
    menu.dispatch("click");
    const handle = makeNode("span", { "data-db-resize": "trip_start" });
    cell.appendChild(handle);
    handle.dispatch("click");
    return result({});
  },

  /* Esc closes the panel by following its close link. */
  "escape-closes": function () {
    buildSheet({ open: 1 });
    run();
    const event = document.querySelector("[data-db-row-detail]").dispatch("keydown", { key: "Escape" });
    return result({ prevented: !!event.defaultPrevented });
  },

  /* Arrow keys traverse to the adjacent row's reference. */
  "traverse-next": function () {
    buildSheet({ open: 1 });
    run();
    document.querySelector("[data-db-row-detail]").dispatch("keydown", { key: "ArrowDown" });
    return result({});
  },

  "traverse-previous": function () {
    buildSheet({ open: 1 });
    run();
    document.querySelector("[data-db-row-detail]").dispatch("keydown", { key: "ArrowUp" });
    return result({});
  },

  /* At the first row there is no previous target, so nothing navigates. */
  "traverse-at-edge": function () {
    buildSheet({ open: 0 });
    run();
    document.querySelector("[data-db-row-detail]").dispatch("keydown", { key: "ArrowUp" });
    return result({});
  },

  /* Opening records the table scroll so the next render can restore it. */
  "scroll-is-remembered": function () {
    const sheet = buildSheet({});
    run();
    sheet.rows[0].dispatch("click");
    return result({ saved: storage.getItem("db-row-scroll") });
  },

  /* On the next render the offsets come back and the key is consumed. */
  "scroll-is-restored": function () {
    storage.setItem("db-row-scroll", JSON.stringify({ left: 640, top: 320 }));
    const sheet = buildSheet({ open: 1 });
    sheet.scroll.scrollLeft = 0;
    sheet.scroll.scrollTop = 0;
    run();
    return result({ left: sheet.scroll.scrollLeft, top: sheet.scroll.scrollTop, leftover: storage.getItem("db-row-scroll") });
  },

  /* Opening a panel moves focus to its heading (INTERACTION_SPEC §2.3). */
  "focus-enters-panel": function () {
    buildSheet({ open: 1 });
    run();
    return result({ focusedTag: global.__focused ? global.__focused._tag : null });
  },

  /* Closing returns focus to the row that opened it (DB-38, AC-4). */
  "focus-returns-to-row": function () {
    storage.setItem("db-row-trigger", TOKENS[2]);
    buildSheet({});
    run();
    return result({});
  },

  /* A malformed stored scroll value must not throw during load. */
  "malformed-scroll-is-ignored": function () {
    storage.setItem("db-row-scroll", "{not json");
    const sheet = buildSheet({ open: 0 });
    sheet.scroll.scrollLeft = 11;
    run();
    return result({ left: sheet.scroll.scrollLeft });
  },

  /* Arrow keys inside a field belong to the field, not to traversal. */
  "arrows-in-a-field-are-not-traversal": function () {
    buildSheet({ open: 1 });
    run();
    const input = makeNode("input", {});
    document.querySelector("[data-db-row-detail]").appendChild(input);
    input.dispatch("keydown", { key: "ArrowDown" });
    return result({});
  }
};

const name = process.argv[2];
if (!scenarios[name]) {
  console.error("unknown scenario: " + name);
  process.exit(2);
}
global.__focused = null;
process.stdout.write(JSON.stringify(scenarios[name]()));
