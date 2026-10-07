/* DOM stub that executes the real api/static/js/data-grid-selection.js.
 *
 * S7 is an interaction contract, so reading the source proves nothing: the
 * rectangle mathematics, the keyboard bounds, the TSV grammar, the clipboard
 * fallbacks and the ≤768 px disable are all exercised by running the shipped
 * file against a fixture sheet shaped exactly like the server's markup.
 *
 * Invoked by ops/tests_manual/test_portal_database_grid_selection.py:
 *
 *     node ops/tests_manual/data_grid_selection_harness.js <scenario> [json-args]
 *
 * Prints one JSON object describing the observed end state. Nothing here can
 * reach a network or a database; the harness records any attempt so the tests
 * can assert the module issues none.
 */
"use strict";

const path = require("path");

const SCRIPT = path.join(__dirname, "..", "..", "api", "static", "js", "data-grid-selection.js");
/* The S6 module is loaded alongside it in the coexistence scenarios: the two
 * scripts share the same table, and only running both proves that a press on a
 * data cell anchors a range instead of opening the row drawer. */
const ROW_DETAIL_SCRIPT = path.join(__dirname, "..", "..", "api", "static", "js", "data-grid-row-detail.js");

/* The vocabulary the server hands down on the sheet, verbatim from the
 * translation catalogue. The module must hold no Polish of its own. */
const STRINGS = {
  summary: "zaznaczono {rows} × {columns} · {cells}",
  copied: "Skopiowano {cells} do schowka.",
  copyFailed: "Nie udało się skopiować zaznaczenia do schowka.",
  row: ["wiersz", "wiersze", "wierszy"],
  column: ["kolumna", "kolumny", "kolumn"],
  cell: ["komórka", "komórki", "komórek"]
};

/* A value that appears nowhere else, so any trace of the technical row identity
 * in a payload, a footer or the module's own state is unmistakable. */
const RAW_IDENTITY = "QQZX-RECORDID-NEVER-SHOWN-773311";
const ROW_REFERENCE_PREFIX = "tok-OPAQUE-REF-";

let seq = 0;
let fetches = 0;
const navigations = [];
const clipboardWrites = [];

/* Which vocabulary the CURRENT fixture speaks. The module serves two tables now
 * (Database Explorer and the Eco Driving trip table); the readers below must ask
 * the same attribute the fixture wrote, or an eco scenario reports an empty
 * selection and passes for the wrong reason. */
let COL_ATTR = "data-db-column";
const execCopies = [];

/* --------------------------------------------------------------------- DOM */

function makeNode(tag, attrs) {
  const node = {
    _tag: String(tag).toLowerCase(),
    _attrs: Object.assign({}, attrs || {}),
    _id: (seq += 1),
    _listeners: {},
    _text: "",
    nodeType: 1,
    childNodes: [],
    parentNode: null,
    style: {},
    value: "",
    get tagName() { return this._tag.toUpperCase(); },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
    get classList() {
      const self = this;
      return {
        add(name) {
          const list = String(self._attrs["class"] || "").split(/\s+/).filter(Boolean);
          if (list.indexOf(name) === -1) { list.push(name); }
          self._attrs["class"] = list.join(" ");
        },
        remove(name) {
          const list = String(self._attrs["class"] || "").split(/\s+/).filter(Boolean);
          self._attrs["class"] = list.filter(function (c) { return c !== name; }).join(" ");
        },
        contains(name) {
          return String(self._attrs["class"] || "").split(/\s+/).indexOf(name) !== -1;
        }
      };
    },
    hasAttribute(name) { return name in this._attrs; },
    getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; },
    setAttribute(name, value) { this._attrs[name] = String(value); },
    removeAttribute(name) { delete this._attrs[name]; },
    addEventListener(type, fn) { (this._listeners[type] || (this._listeners[type] = [])).push(fn); },
    focus() { global.__focused = this; },
    select() { this._selected = true; },
    appendChild(child) { child.parentNode = this; this.childNodes.push(child); return child; },
    removeChild(child) {
      this.childNodes = this.childNodes.filter(function (c) { return c !== child; });
      child.parentNode = null;
      return child;
    },
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
    dispatchEvent(ev) {
      /* Custom events published by the module, recorded for the S8 boundary. */
      global.__published = (global.__published || []).concat([{ type: ev.type, detail: ev.detail }]);
      const list = this._listeners[ev.type] || [];
      for (let i = 0; i < list.length; i += 1) { list[i].call(this, ev); }
      return true;
    },
    dispatch(type, event) {
      const ev = Object.assign({
        type: type,
        target: this,
        button: 0,
        detail: 1,
        preventDefault() { this.defaultPrevented = true; },
        defaultPrevented: false
      }, event || {});
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

/* A deliberately small selector engine: only the forms the shipped script uses
 * (tag, class, attribute with or without a value, and one descendant step). */
function matchesOne(node, sel) {
  sel = sel.trim();
  if (!sel) { return false; }
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
  return sel.split(",").some(function (part) {
    const chunks = part.trim().split(/\s+/);
    if (!matchesOne(node, chunks[chunks.length - 1])) { return false; }
    if (chunks.length === 1) { return true; }
    let cursor = node.parentNode;
    while (cursor) {
      if (matchesOne(cursor, chunks[0])) { return true; }
      cursor = cursor.parentNode;
    }
    return false;
  });
}

function query(root, sel, first) {
  const all = descendants(root);
  const out = [];
  for (let i = 0; i < all.length; i += 1) {
    if (!matches(all[i], sel)) { continue; }
    out.push(all[i]);
    if (first) { break; }
  }
  return out;
}

const document = {
  _listeners: {},
  readyState: "complete",
  body: null,
  addEventListener(type, fn) { (this._listeners[type] || (this._listeners[type] = [])).push(fn); },
  createElement(tag) { return makeNode(tag, {}); },
  execCommand(command) { execCopies.push(String(command)); return global.__execCopyWorks !== false; },
  querySelector(sel) { return query(document.body, sel, true)[0] || null; },
  querySelectorAll(sel) { return query(document.body, sel, false); }
};

global.document = document;
global.fetch = function () { fetches += 1; return Promise.resolve({ ok: true }); };

/* ------------------------------------------------------------------ fixture */

/* Columns as the approved catalogue orders them. `record_id` is deliberately
 * NOT here: the server never renders it, and the harness proves the module has
 * no way to reach it even when a row carries one in the fixture data. */
const DEFAULT_COLUMNS = ["trip_start", "driver_name", "distance_km", "is_billable"];

/* Rendered text and canonical copy value differ on purpose in every family, so
 * a payload built from display strings fails loudly. */
function defaultRows(count) {
  const out = [];
  for (let i = 0; i < count; i += 1) {
    out.push({
      record_id: RAW_IDENTITY + "-" + i,
      trip_start: { text: "2026-05-28 07:3" + i, copy: "2026-05-28T07:3" + i + ":45.512+00:00" },
      driver_name: { text: "Kowalsk" + String.fromCharCode(105), copy: "Kowalski " + i },
      distance_km: { text: "1 284,4" + i, copy: "1284.4" + i },
      is_billable: { text: i % 2 ? "NIE" : "TAK", copy: i % 2 ? "False" : "True" }
    });
  }
  return out;
}

let media = null;

function buildSheet(options) {
  options = options || {};
  const columns = options.columns || DEFAULT_COLUMNS.slice();
  const rows = options.rows || defaultRows(options.rowCount || 4);
  const withDetail = options.detailColumn !== false;

  /* `eco: true` builds the Eco Driving trip table instead: same module, same
   * painted classes, different vocabulary and none of the Database Explorer's
   * furniture -- no column menus, no resize handles, no detail column, and the
   * copy value on the cell itself rather than on an inner span. */
  const eco = !!options.eco;
  COL_ATTR = eco ? "data-eco-column" : "data-db-column";
  const COPY_ATTR = eco ? "data-eco-copy" : "data-db-copy";

  document.body = makeNode("body", {});
  const sheetAttrs = {
    "class": eco ? "eco-select-sheet" : "db-browser db-sheet",
    "data-db-sheet": "",
    "data-db-select-min-width": String(options.minWidth || 769),
    "data-db-select-strings": JSON.stringify(options.strings || STRINGS)
  };
  if (eco) {
    sheetAttrs["data-grid-table"] = "table.eco-table";
    sheetAttrs["data-grid-column-attr"] = "data-eco-column";
    sheetAttrs["data-grid-copy-attr"] = "data-eco-copy";
  }
  const sheet = makeNode("div", sheetAttrs);
  document.body.appendChild(sheet);

  const scroll = makeNode("div", { "data-db-scroll": "" });
  sheet.appendChild(scroll);
  const table = makeNode("table", { "class": eco ? "eco-table" : "db-table" });
  scroll.appendChild(table);

  const thead = makeNode("thead", {});
  table.appendChild(thead);
  const headRow = makeNode("tr", {});
  thead.appendChild(headRow);
  columns.forEach(function (name) {
    const thAttrs = { scope: "col" };
    thAttrs[COL_ATTR] = name;
    const th = makeNode("th", thAttrs);
    headRow.appendChild(th);
    if (!eco) {
      /* Every header carries its column menu and resize handle, exactly as the
       * server renders them, so the collision surfaces are real. */
      th.appendChild(makeNode("details", { "data-db-col-menu": name }));
      th.appendChild(makeNode("a", { "data-db-resize": name, href: "#" }));
    }
  });
  if (withDetail) {
    headRow.appendChild(makeNode("th", { scope: "col", "class": "db-detail-col" }));
  }

  const tbody = makeNode("tbody", {});
  table.appendChild(tbody);
  const cellIndex = {};
  rows.forEach(function (row, r) {
    const tr = makeNode("tr", {
      "class": "db-row",
      tabindex: "0",
      /* The opaque S6 reference, as the server emits it. It is row plumbing,
       * never cell data. */
      "data-db-row": ROW_REFERENCE_PREFIX + r
    });
    tbody.appendChild(tr);
    columns.forEach(function (name) {
      const tdAttrs = {};
      tdAttrs[COL_ATTR] = name;
      const td = makeNode("td", tdAttrs);
      tr.appendChild(td);
      const value = row[name];
      const blank = value === null || value === undefined;
      if (eco) {
        /* The server writes the machine value onto the cell. An empty cell still
         * carries the attribute, so a rectangle never shifts its columns. */
        td.setAttribute(COPY_ATTR, blank ? "" : value.copy);
        td.textContent = blank ? "" : value.text;
      } else if (blank) {
        /* NULL and empty string render their markers and carry no copy value —
         * the S2 contract, which S7 must not quietly change. */
        td.setAttribute("class", "db-cell-empty");
        const marker = makeNode("span", { "class": "db-v db-v-null" });
        marker.textContent = "brak wartości";
        td.appendChild(marker);
      } else {
        const span = makeNode("span", { "class": "db-v db-copyable", "data-db-copy": value.copy });
        span.textContent = value.text;
        td.appendChild(span);
      }
      cellIndex[r + ":" + name] = td;
    });
    if (withDetail) {
      const detail = makeNode("td", { "class": "db-detail-col" });
      tr.appendChild(detail);
      detail.appendChild(makeNode("a", {
        "class": "portal-link db-row-open",
        "data-db-row-open": "",
        href: "/user/database/datasets/ds-1?row=" + ROW_REFERENCE_PREFIX + r
      }));
    }
  });

  const footer = makeNode("div", { "class": "db-footer" });
  sheet.appendChild(footer);
  const count = makeNode("span", { "class": "db-select-count", "data-db-select-count": "", hidden: "" });
  footer.appendChild(count);
  const hint = makeNode("span", { "class": "db-select-hint" });
  hint.textContent = "zaznacz zakres i ⌘C, aby skopiować do arkusza";
  footer.appendChild(hint);
  const status = makeNode("span", {
    "class": "lp-visually-hidden", role: "status", "aria-live": "polite", "data-db-select-status": ""
  });
  footer.appendChild(status);

  /* A filter field lives in the toolbar, outside the table: typing in it must
   * keep ordinary text editing, including Shift+Arrow and Ctrl+C. */
  const search = makeNode("input", { type: "search", "data-db-search": "" });
  sheet.appendChild(search);

  media = {
    matches: options.wide === undefined ? true : !!options.wide,
    _handlers: [],
    addEventListener(type, fn) { this._handlers.push(fn); },
    _change(matches) {
      this.matches = !!matches;
      this._handlers.forEach(function (fn) { fn({ matches: matches }); });
    }
  };

  global.window = {
    matchMedia(queryText) { media.query = queryText; return media; },
    navigator: {
      clipboard: options.clipboard === "absent" ? undefined : {
        writeText(text) {
          clipboardWrites.push(String(text));
          if (options.clipboard === "reject") { return Promise.reject(new Error("denied")); }
          if (options.clipboard === "throw") { throw new Error("blocked"); }
          return Promise.resolve();
        }
      }
    },
    getSelection() { return { removeAllRanges() { global.__nativeSelectionCleared = true; } }; },
    CustomEvent: function (type, init) { return { type: type, detail: (init || {}).detail }; },
    CSS: { escape: function (v) { return String(v); } },
    sessionStorage: {
      _data: {},
      getItem(k) { return k in this._data ? this._data[k] : null; },
      setItem(k, v) { this._data[k] = String(v); },
      removeItem(k) { delete this._data[k]; }
    },
    addEventListener(type, fn) { (this._listeners || (this._listeners = {}))[type] = fn; },
    _fire(type) { if (this._listeners && this._listeners[type]) { this._listeners[type]({}); } },
    location: { href: "/user/database/datasets/ds-1", assign(href) { navigations.push(String(href)); } }
  };
  global.navigator = global.window.navigator;

  return {
    sheet: sheet, table: table, tbody: tbody, thead: thead, headRow: headRow,
    rows: tbody.childNodes, columns: columns, cell: cellIndex,
    count: count, status: status, search: search, media: media
  };
}

function run() {
  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
}

function runWithRowDetail() {
  delete require.cache[require.resolve(ROW_DETAIL_SCRIPT)];
  require(ROW_DETAIL_SCRIPT);
  run();
}

/* Every cell currently painted as part of the range, in reading order. */
function selected(fixture) {
  const out = [];
  fixture.rows.forEach(function (tr, r) {
    tr.childNodes.forEach(function (td) {
      const name = td.getAttribute(COL_ATTR);
      if (!name) { return; }
      if (td.classList.contains("db-cell-in-range")) { out.push(r + ":" + name); }
    });
  });
  return out;
}

function activeCell(fixture) {
  let found = null;
  fixture.rows.forEach(function (tr, r) {
    tr.childNodes.forEach(function (td) {
      const name = td.getAttribute(COL_ATTR);
      if (name && td.classList.contains("db-cell-active")) { found = r + ":" + name; }
    });
  });
  return found;
}

function result(fixture, extra) {
  return Object.assign({
    selected: selected(fixture),
    active: activeCell(fixture),
    footer: fixture.count.textContent,
    footerHidden: fixture.count.hasAttribute("hidden"),
    status: fixture.status.textContent,
    clipboard: clipboardWrites.slice(),
    execCopies: execCopies.slice(),
    navigations: navigations.slice(),
    fetches: fetches,
    enabled: fixture.sheet.getAttribute("data-db-selection"),
    mediaQuery: fixture.media.query || null,
    focused: global.__focused
      ? (global.__focused.getAttribute(COL_ATTR) || global.__focused._tag)
      : null
  }, extra || {});
}

function press(fixture, row, column, options) {
  return fixture.cell[row + ":" + column].dispatch("pointerdown", options || {});
}

function over(fixture, row, column) {
  return fixture.cell[row + ":" + column].dispatch("pointerover", {});
}

function release(fixture) {
  return fixture.tbody.dispatch("pointerup", {});
}

function key(target, name, options) {
  return target.dispatch("keydown", Object.assign({ key: name }, options || {}));
}

function drag(fixture, from, to) {
  press(fixture, from[0], from[1]);
  over(fixture, to[0], to[1]);
  release(fixture);
}

/* ---------------------------------------------------------------- scenarios */

const scenarios = {
  /* Pressing one data cell anchors a 1×1 selection. */
  "single-cell": function () {
    const f = buildSheet({});
    run();
    press(f, 1, "driver_name");
    release(f);
    return result(f, { nativeCleared: !!global.__nativeSelectionCleared });
  },

  /* A drag across one row produces a 1×N range. */
  "row-range": function () {
    const f = buildSheet({});
    run();
    drag(f, [1, "trip_start"], [1, "distance_km"]);
    return result(f, {});
  },

  /* A drag down one column produces an N×1 range. */
  "column-range": function () {
    const f = buildSheet({});
    run();
    drag(f, [0, "driver_name"], [2, "driver_name"]);
    return result(f, {});
  },

  /* Down-right, the ordinary case. */
  "rectangle-down-right": function () {
    const f = buildSheet({});
    run();
    drag(f, [1, "trip_start"], [3, "distance_km"]);
    return result(f, {});
  },

  /* Up-left: the anchor is the bottom-right corner and the rectangle is the
     same set of cells. */
  "rectangle-up-left": function () {
    const f = buildSheet({});
    run();
    drag(f, [3, "distance_km"], [1, "trip_start"]);
    return result(f, {});
  },

  "rectangle-down-left": function () {
    const f = buildSheet({});
    run();
    drag(f, [0, "is_billable"], [2, "driver_name"]);
    return result(f, {});
  },

  "rectangle-up-right": function () {
    const f = buildSheet({});
    run();
    drag(f, [3, "trip_start"], [1, "is_billable"]);
    return result(f, {});
  },

  /* Travelling back toward the anchor shrinks the rectangle rather than leaving
     the widest extent painted. */
  "drag-shrinks-back": function () {
    const f = buildSheet({});
    run();
    press(f, 0, "trip_start");
    over(f, 3, "is_billable");
    const widest = selected(f).length;
    over(f, 1, "driver_name");
    release(f);
    return result(f, { widest: widest });
  },

  /* Shift+click extends from the existing anchor without moving it. */
  "shift-click-extends": function () {
    const f = buildSheet({});
    run();
    press(f, 1, "driver_name");
    release(f);
    press(f, 3, "is_billable", { shiftKey: true });
    release(f);
    return result(f, {});
  },

  /* A drag that leaves the table simply stops: the last cell the pointer was
     over is the extent, and releasing anywhere commits it. */
  "pointer-leaves-grid": function () {
    const f = buildSheet({});
    run();
    press(f, 0, "trip_start");
    over(f, 1, "driver_name");
    f.sheet.dispatch("pointerover", {});
    release(f);
    const after = selected(f);
    /* A later pointerover must not extend anything once the drag has ended. */
    over(f, 3, "is_billable");
    return result(f, { atRelease: after });
  },

  /* Keyboard entry: an arrow key from a focused row anchors the first cell. */
  "keyboard-enters-grid": function () {
    const f = buildSheet({});
    run();
    key(f.rows[2], "ArrowRight");
    return result(f, {});
  },

  "shift-right": function () {
    const f = buildSheet({});
    run();
    press(f, 1, "trip_start");
    release(f);
    key(f.cell["1:trip_start"], "ArrowRight", { shiftKey: true });
    key(f.cell["1:trip_start"], "ArrowRight", { shiftKey: true });
    return result(f, {});
  },

  "shift-left": function () {
    const f = buildSheet({});
    run();
    press(f, 1, "is_billable");
    release(f);
    key(f.cell["1:is_billable"], "ArrowLeft", { shiftKey: true });
    return result(f, {});
  },

  "shift-down": function () {
    const f = buildSheet({});
    run();
    press(f, 0, "driver_name");
    release(f);
    key(f.cell["0:driver_name"], "ArrowDown", { shiftKey: true });
    key(f.cell["0:driver_name"], "ArrowDown", { shiftKey: true });
    return result(f, {});
  },

  "shift-up": function () {
    const f = buildSheet({});
    run();
    press(f, 3, "driver_name");
    release(f);
    key(f.cell["3:driver_name"], "ArrowUp", { shiftKey: true });
    return result(f, {});
  },

  /* Extending back through the anchor shrinks and then grows on the other side;
     the anchor itself never moves. */
  "shift-shrinks-then-crosses": function () {
    const f = buildSheet({});
    run();
    press(f, 1, "driver_name");
    release(f);
    const steps = [];
    key(f.cell["1:driver_name"], "ArrowDown", { shiftKey: true });
    steps.push(selected(f).length);
    key(f.cell["1:driver_name"], "ArrowDown", { shiftKey: true });
    steps.push(selected(f).length);
    key(f.cell["1:driver_name"], "ArrowUp", { shiftKey: true });
    steps.push(selected(f).length);
    key(f.cell["1:driver_name"], "ArrowUp", { shiftKey: true });
    key(f.cell["1:driver_name"], "ArrowUp", { shiftKey: true });
    steps.push(selected(f).length);
    return result(f, { steps: steps });
  },

  /* The page edges are hard stops: no wrap, no navigation, no hidden state for
     rows that are not on this page (`D-012` phase 1). */
  "bounds-top": function () {
    const f = buildSheet({});
    run();
    press(f, 0, "driver_name");
    release(f);
    for (let i = 0; i < 4; i += 1) { key(f.cell["0:driver_name"], "ArrowUp", { shiftKey: true }); }
    return result(f, {});
  },

  "bounds-bottom": function () {
    const f = buildSheet({});
    run();
    press(f, 3, "driver_name");
    release(f);
    for (let i = 0; i < 4; i += 1) { key(f.cell["3:driver_name"], "ArrowDown", { shiftKey: true }); }
    return result(f, {});
  },

  "bounds-left": function () {
    const f = buildSheet({});
    run();
    press(f, 1, "trip_start");
    release(f);
    for (let i = 0; i < 3; i += 1) { key(f.cell["1:trip_start"], "ArrowLeft", { shiftKey: true }); }
    return result(f, {});
  },

  "bounds-right": function () {
    const f = buildSheet({});
    run();
    press(f, 1, "is_billable");
    release(f);
    for (let i = 0; i < 3; i += 1) { key(f.cell["1:is_billable"], "ArrowRight", { shiftKey: true }); }
    return result(f, {});
  },

  /* An unshifted arrow moves the active cell and collapses the range. */
  "plain-arrow-moves": function () {
    const f = buildSheet({});
    run();
    drag(f, [1, "trip_start"], [2, "distance_km"]);
    key(f.cell["2:distance_km"], "ArrowRight");
    return result(f, {});
  },

  /* Home and End reach the row edges (INTERACTION_SPEC §2.2). */
  "home-and-end": function () {
    const f = buildSheet({});
    run();
    press(f, 1, "driver_name");
    release(f);
    key(f.cell["1:driver_name"], "End", { shiftKey: true });
    const end = selected(f);
    key(f.cell["1:is_billable"], "Home", { shiftKey: true });
    return result(f, { end: end });
  },

  /* An interactive control inside the grid keeps its own meaning. */
  "interactive-children-do-not-select": function () {
    const f = buildSheet({});
    run();
    f.headRow.childNodes[0].childNodes[0].dispatch("pointerdown", {});
    f.headRow.childNodes[1].childNodes[1].dispatch("pointerdown", {});
    const detailTrigger = f.rows[1].childNodes[4].childNodes[0];
    detailTrigger.dispatch("pointerdown", {});
    return result(f, {});
  },

  /* The detail column is not part of the geometry at all. */
  "detail-cell-is-not-selectable": function () {
    const f = buildSheet({});
    run();
    f.rows[1].childNodes[4].dispatch("pointerdown", {});
    return result(f, {});
  },

  /* Typing in a filter field keeps ordinary text editing. */
  "editable-keeps-shortcuts": function () {
    const f = buildSheet({});
    run();
    drag(f, [0, "trip_start"], [1, "driver_name"]);
    const shift = key(f.search, "ArrowRight", { shiftKey: true });
    const copy = key(f.search, "c", { ctrlKey: true });
    const escape = key(f.search, "Escape");
    return result(f, {
      shiftPrevented: !!shift.defaultPrevented,
      copyPrevented: !!copy.defaultPrevented,
      escapePrevented: !!escape.defaultPrevented
    });
  },

  /* Copy: one cell, then a rectangle. */
  /* ---------------------------------------------------------------- eco table

     The same module, pointed at the Eco Driving trip table by the `data-grid-*`
     vocabulary overrides. These scenarios exist because the markup assertions on
     the Python side prove only that the ATTRIBUTES are present -- they cannot
     prove the module reads them. If an override were ignored, the module would
     look for `table.db-table`, find nothing, and the page would render a table
     that simply never selects. */

  "eco-single-cell": function () {
    const f = buildSheet({ eco: true });
    run();
    press(f, 1, "driver_name");
    release(f);
    return result(f, {});
  },

  "eco-rectangle": function () {
    const f = buildSheet({ eco: true });
    run();
    drag(f, [1, "trip_start"], [2, "driver_name"]);
    return result(f, {});
  },

  "eco-copy-rectangle": function () {
    const f = buildSheet({ eco: true });
    run();
    drag(f, [1, "trip_start"], [2, "driver_name"]);
    key(f.cell["2:driver_name"], "c", { ctrlKey: true });
    return result(f, {});
  },

  "eco-keyboard-extends": function () {
    const f = buildSheet({ eco: true });
    run();
    press(f, 1, "trip_start");
    release(f);
    key(f.cell["1:trip_start"], "ArrowRight", { shiftKey: true });
    key(f.cell["1:trip_start"], "ArrowDown", { shiftKey: true });
    return result(f, {});
  },

  "eco-empty-cell-copies-empty": function () {
    const rows = defaultRows(3);
    rows[0].driver_name = null;
    const f = buildSheet({ eco: true, rows: rows });
    run();
    drag(f, [0, "trip_start"], [0, "driver_name"]);
    key(f.cell["0:driver_name"], "c", { ctrlKey: true });
    return result(f, {});
  },

  "eco-disabled-below-cutoff": function () {
    const f = buildSheet({ eco: true, wide: false });
    run();
    press(f, 1, "driver_name");
    release(f);
    return result(f, {});
  },

  "copy-single": function () {
    const f = buildSheet({});
    run();
    press(f, 1, "driver_name");
    release(f);
    const event = key(f.cell["1:driver_name"], "c", { ctrlKey: true });
    return result(f, { prevented: !!event.defaultPrevented });
  },

  "copy-rectangle": function () {
    const f = buildSheet({});
    run();
    drag(f, [1, "trip_start"], [2, "driver_name"]);
    key(f.cell["2:driver_name"], "c", { ctrlKey: true });
    return result(f, {});
  },

  "copy-wide-rectangle": function () {
    const f = buildSheet({});
    run();
    drag(f, [0, "trip_start"], [2, "is_billable"]);
    key(f.cell["2:is_billable"], "c", { metaKey: true });
    return result(f, {});
  },

  /* With no selection this is an ordinary browser copy and must stay one. */
  "copy-without-selection": function () {
    const f = buildSheet({});
    run();
    const event = key(f.rows[0], "c", { ctrlKey: true });
    return result(f, { prevented: !!event.defaultPrevented });
  },

  /* Reordered columns: the payload follows the visible order, not the catalogue
     order, and each value stays with its own column. */
  "copy-reordered-columns": function () {
    const f = buildSheet({ columns: ["driver_name", "is_billable", "trip_start", "distance_km"] });
    run();
    drag(f, [0, "driver_name"], [1, "trip_start"]);
    key(f.cell["1:trip_start"], "c", { ctrlKey: true });
    return result(f, {});
  },

  /* NULL and empty string both copy as empty (SCREEN_STATE_MATRIX: "copy yields
     empty" for both) and neither collapses the rectangle. */
  "copy-null-and-empty": function () {
    const rows = [
      { trip_start: { text: "a", copy: "a" }, driver_name: null, distance_km: { text: "", copy: "" }, is_billable: { text: "b", copy: "b" } },
      { trip_start: { text: "c", copy: "c" }, driver_name: { text: "d", copy: "d" }, distance_km: null, is_billable: { text: "e", copy: "e" } }
    ];
    const f = buildSheet({ rows: rows });
    run();
    drag(f, [0, "trip_start"], [1, "is_billable"]);
    key(f.cell["1:is_billable"], "c", { ctrlKey: true });
    return result(f, {});
  },

  /* Values that would break a naive TSV: tabs, both line-break characters,
     quotes and meaningful edge whitespace. */
  "copy-special-characters": function () {
    const rows = [
      {
        trip_start: { text: "tab", copy: "left\tright" },
        driver_name: { text: "lf", copy: "first\nsecond" },
        distance_km: { text: "crlf", copy: "one\r\ntwo" },
        is_billable: { text: "cr", copy: "alpha\rbeta" }
      },
      {
        trip_start: { text: "quote", copy: 'say "hi"' },
        driver_name: { text: "pad", copy: "  padded  " },
        distance_km: { text: "tabquote", copy: 'a\t"b"' },
        is_billable: { text: "plain", copy: "plain" }
      }
    ];
    const f = buildSheet({ rows: rows });
    run();
    drag(f, [0, "trip_start"], [1, "is_billable"]);
    key(f.cell["1:is_billable"], "c", { ctrlKey: true });
    return result(f, {});
  },

  /* Long and structured values copy whole, never the truncated preview. */
  "copy-long-and-structured": function () {
    const long = "L".repeat(420);
    const json = '{"driver":"Kowalski","legs":[1,2,3],"note":"a\\tb"}';
    const rows = [{
      trip_start: { text: "7f03c4e1…0021", copy: "0c64dabd-d486-43f9-887d-fa81cd4cbcac" },
      driver_name: { text: long.slice(0, 12) + "…", copy: long },
      distance_km: { text: "{ 3 pola }", copy: json },
      is_billable: { text: "TAK", copy: "True" }
    }];
    const f = buildSheet({ rows: rows });
    run();
    drag(f, [0, "trip_start"], [0, "is_billable"]);
    key(f.cell["0:is_billable"], "c", { ctrlKey: true });
    return result(f, { longLength: long.length });
  },

  /* The success path announces the count, never the data. */
  "copy-success-feedback": function () {
    const f = buildSheet({});
    run();
    drag(f, [0, "trip_start"], [1, "driver_name"]);
    key(f.cell["1:driver_name"], "c", { ctrlKey: true });
    return new Promise(function (resolve) {
      setTimeout(function () { resolve(result(f, {})); }, 0);
    });
  },

  /* A rejected clipboard write falls back, and a failing fallback reports a
     safe message while leaving the selection intact. */
  "copy-rejected-falls-back": function () {
    const f = buildSheet({ clipboard: "reject" });
    run();
    drag(f, [0, "trip_start"], [0, "driver_name"]);
    key(f.cell["0:driver_name"], "c", { ctrlKey: true });
    return new Promise(function (resolve) {
      setTimeout(function () { resolve(result(f, {})); }, 0);
    });
  },

  "copy-rejected-and-fallback-fails": function () {
    global.__execCopyWorks = false;
    const f = buildSheet({ clipboard: "reject" });
    run();
    drag(f, [0, "trip_start"], [0, "driver_name"]);
    key(f.cell["0:driver_name"], "c", { ctrlKey: true });
    return new Promise(function (resolve) {
      setTimeout(function () { resolve(result(f, {})); }, 0);
    });
  },

  /* No clipboard API at all: the fallback carries the feature. */
  "copy-without-clipboard-api": function () {
    const f = buildSheet({ clipboard: "absent" });
    run();
    drag(f, [0, "trip_start"], [0, "driver_name"]);
    key(f.cell["0:driver_name"], "c", { ctrlKey: true });
    return result(f, {});
  },

  /* A throwing clipboard implementation must not break the grid. */
  "copy-throwing-clipboard": function () {
    const f = buildSheet({ clipboard: "throw" });
    run();
    drag(f, [0, "trip_start"], [0, "driver_name"]);
    key(f.cell["0:driver_name"], "c", { ctrlKey: true });
    return result(f, {});
  },

  /* Escape clears the selection only when no nearer surface owns it. */
  "escape-clears": function () {
    const f = buildSheet({});
    run();
    drag(f, [0, "trip_start"], [1, "driver_name"]);
    const event = key(f.tbody, "Escape");
    return result(f, { prevented: !!event.defaultPrevented });
  },

  "escape-yields-to-open-menu": function () {
    const f = buildSheet({});
    run();
    drag(f, [0, "trip_start"], [1, "driver_name"]);
    f.headRow.childNodes[0].childNodes[0].setAttribute("open", "");
    const event = key(f.tbody, "Escape");
    return result(f, { prevented: !!event.defaultPrevented });
  },

  "escape-yields-to-row-drawer": function () {
    const f = buildSheet({});
    run();
    drag(f, [0, "trip_start"], [1, "driver_name"]);
    f.sheet.appendChild(makeNode("aside", { "data-db-row-detail": "" }));
    const event = key(f.tbody, "Escape");
    return result(f, { prevented: !!event.defaultPrevented });
  },

  /* With the drawer open, plain arrows stay with its traversal and only the
     shifted form extends the range. */
  "drawer-keeps-plain-arrows": function () {
    const f = buildSheet({});
    run();
    press(f, 1, "driver_name");
    release(f);
    f.sheet.appendChild(makeNode("aside", { "data-db-row-detail": "" }));
    const plain = key(f.cell["1:driver_name"], "ArrowDown");
    const plainSelection = selected(f);
    const shifted = key(f.cell["1:driver_name"], "ArrowDown", { shiftKey: true });
    return result(f, {
      plainPrevented: !!plain.defaultPrevented,
      plainSelection: plainSelection,
      shiftedPrevented: !!shifted.defaultPrevented
    });
  },

  /* A column that leaves the display takes the selection with it rather than
     leaving an invisible ghost range or copying a value the user cannot see. */
  "hidden-column-clears-selection": function () {
    const f = buildSheet({});
    run();
    drag(f, [0, "driver_name"], [1, "distance_km"]);
    const before = selected(f);
    /* The S5 hide path: header, colgroup entry and body cells go together. */
    f.headRow.removeChild(f.headRow.childNodes[2]);
    f.rows.forEach(function (tr) { tr.removeChild(tr.childNodes[2]); });
    const event = key(f.cell["1:driver_name"], "c", { ctrlKey: true });
    return result(f, { before: before, prevented: !!event.defaultPrevented });
  },

  /* Reorder moves whole columns; the selection stays on the same *values*. */
  "reorder-keeps-value-identity": function () {
    const f = buildSheet({});
    run();
    drag(f, [0, "driver_name"], [1, "distance_km"]);
    const before = selected(f);
    /* Move `is_billable` to the front, as an S5 reorder would. */
    function moveFirst(parent) {
      const node = parent.childNodes[3];
      parent.childNodes = [node].concat(parent.childNodes.filter(function (c) { return c !== node; }));
    }
    moveFirst(f.headRow);
    f.rows.forEach(moveFirst);
    key(f.cell["1:distance_km"], "c", { ctrlKey: true });
    return result(f, { before: before });
  },

  /* Below the approved cutoff nothing is intercepted at all. */
  "narrow-viewport-disables": function () {
    const f = buildSheet({ wide: false });
    run();
    const down = press(f, 1, "driver_name");
    release(f);
    const arrow = key(f.rows[1], "ArrowRight");
    const copy = key(f.rows[1], "c", { ctrlKey: true });
    return result(f, {
      downPrevented: !!down.defaultPrevented,
      arrowPrevented: !!arrow.defaultPrevented,
      copyPrevented: !!copy.defaultPrevented
    });
  },

  /* Crossing the cutoff with a range selected deactivates the feature and takes
     the footer state with it. */
  "crossing-the-cutoff-clears": function () {
    const f = buildSheet({});
    run();
    drag(f, [0, "trip_start"], [2, "distance_km"]);
    const before = { selected: selected(f), footer: f.count.textContent };
    f.media._change(false);
    const after = result(f, {});
    f.media._change(true);
    return Object.assign(after, { before: before, backOn: f.sheet.getAttribute("data-db-selection"), afterReturn: selected(f) });
  },

  /* Back/Forward may swap the result identity underneath the grid. */
  "popstate-clears": function () {
    const f = buildSheet({});
    run();
    drag(f, [0, "trip_start"], [1, "driver_name"]);
    const before = selected(f);
    global.window._fire("popstate");
    return result(f, { before: before });
  },

  /* Pinning and a width change are pure layout: the same cells stay selected
     with the same values, and a pinned cell inside the rectangle belongs to the
     same range rather than to a separate one. */
  "pin-and-width-preserve-selection": function () {
    const f = buildSheet({});
    run();
    drag(f, [0, "trip_start"], [1, "distance_km"]);
    const before = selected(f);
    /* The S5 pin path marks the header and every body cell of that column. */
    f.headRow.childNodes[0].setAttribute("class", "db-sticky-col db-pin-edge");
    f.rows.forEach(function (tr) {
      tr.childNodes[0].setAttribute("class", "db-sticky-col db-pin-edge");
      /* And a live width drag rewrites only geometry. */
      tr.childNodes[1].style.width = "320px";
    });
    key(f.cell["1:distance_km"], "c", { ctrlKey: true });
    const stillInRange = f.rows[0].childNodes[0].classList.contains("db-cell-in-range");
    return result(f, { before: before, pinnedStillInRange: stillInRange });
  },

  /* A full 200-row page: the rectangle, the footer count and the payload
     geometry must all hold at the largest page size the product offers. */
  "large-page-rectangle": function () {
    const columns = [];
    for (let c = 0; c < 12; c += 1) { columns.push("col_" + c); }
    const rows = [];
    for (let r = 0; r < 200; r += 1) {
      const row = {};
      columns.forEach(function (name, c) { row[name] = { text: r + "/" + c, copy: r + "-" + c }; });
      rows.push(row);
    }
    const f = buildSheet({ columns: columns, rows: rows, detailColumn: false });
    run();
    drag(f, [0, "col_0"], [199, "col_11"]);
    key(f.cell["199:col_11"], "c", { ctrlKey: true });
    const payload = clipboardWrites[clipboardWrites.length - 1];
    const lines = payload.split("\r\n");
    return result(f, {
      cells: selected(f).length,
      lines: lines.length,
      firstLine: lines[0],
      lastLine: lines[lines.length - 1],
      clipboard: []
    });
  },

  /* S6 coexistence: with selection enabled, a press and click on a data cell
     anchors a range and the drawer stays shut (INTERACTION_SPEC §5). */
  "row-detail-yields-to-cell-selection": function () {
    const f = buildSheet({});
    runWithRowDetail();
    press(f, 1, "driver_name");
    release(f);
    f.cell["1:driver_name"].dispatch("click", {});
    return result(f, {});
  },

  /* The row's own `Szczegóły` control still opens it, carrying the opaque
     reference and nothing else. */
  "row-detail-opens-from-its-own-control": function () {
    const f = buildSheet({});
    runWithRowDetail();
    f.rows[1].childNodes[4].dispatch("click", {});
    return result(f, {});
  },

  /* Enter on a focused row is unchanged. */
  "row-detail-opens-on-enter": function () {
    const f = buildSheet({});
    runWithRowDetail();
    key(f.rows[2], "Enter");
    return result(f, {});
  },

  /* Below the cutoff S7 is off, so the pre-S7 whole-row click is intact. */
  "row-detail-click-intact-when-narrow": function () {
    const f = buildSheet({ wide: false });
    runWithRowDetail();
    f.cell["1:driver_name"].dispatch("click", {});
    return result(f, {});
  },

  /* The one thing the grid publishes outward for S8: which rendered rows the
     rectangle touches, by position — never the references and never the values. */
  "selection-publishes-row-positions": function () {
    global.__published = [];
    const f = buildSheet({});
    run();
    drag(f, [1, "trip_start"], [2, "driver_name"]);
    const during = (global.__published || []).slice();
    key(f.tbody, "Escape");
    const after = (global.__published || []).slice();
    return result(f, {
      published: during.map(function (e) { return { type: e.type, detail: e.detail }; }),
      last: after.length ? after[after.length - 1] : null
    });
  },

  /* The module's whole state, dumped: no identity, no reference, no values. */
  "module-holds-no-identity": function () {
    const f = buildSheet({});
    run();
    drag(f, [0, "trip_start"], [2, "is_billable"]);
    key(f.cell["2:is_billable"], "c", { ctrlKey: true });
    const markup = JSON.stringify(document.body, function (k, v) {
      return k === "parentNode" || k === "_listeners" ? undefined : v;
    });
    return result(f, { markup: markup });
  }
};

/* --------------------------------------------------------------------- main */

const name = process.argv[2];
if (!scenarios[name]) {
  process.stderr.write("unknown scenario: " + name + "\n");
  process.exit(2);
}
Promise.resolve(scenarios[name]()).then(function (out) {
  process.stdout.write(JSON.stringify(out));
}, function (error) {
  process.stderr.write(String((error && error.stack) || error) + "\n");
  process.exit(1);
});
