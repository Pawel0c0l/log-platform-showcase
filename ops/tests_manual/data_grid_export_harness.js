/* DOM stub that executes the real api/static/js/data-grid-export.js.
 *
 * The parts of the S8 contract that live in the browser — mapping an S7
 * selection to opaque row references, keeping the selected-row scope
 * unavailable until a selection exists, updating the path notice and submit
 * label, and copying a job reference — are exercised by running the shipped
 * file rather than by reading it.
 *
 * Invoked by ops/tests_manual/test_portal_database_export_panel_frontend.py:
 *
 *     node ops/tests_manual/data_grid_export_harness.js <scenario>
 *
 * Prints one JSON object describing the observed end state. The harness records
 * any network attempt so the tests can assert the module makes none.
 */
"use strict";

const path = require("path");

const SCRIPT = path.join(__dirname, "..", "..", "api", "static", "js", "data-grid-export.js");

/* The vocabulary the server hands down. The module must hold no Polish. */
const STRINGS = {
  direct: "Pobranie natychmiastowe.",
  background: "Plik przygotuje się w tle i trafi do Raportów jako Eksport danych (retencja 3 dni).",
  refused: "Ten zakres ma więcej niż 1 000 000 wierszy. Zawęź filtrami — eksportu nie da się wykonać.",
  unknown: "Liczba wierszy jest nieznana. Ścieżkę ustali serwer po zatwierdzeniu.",
  submitDirect: "Pobierz {format}",
  submitBackground: "Przygotuj w tle",
  selectionEmpty: "Zaznacz zakres komórek w tabeli, aby wybrać wiersze.",
  rows: "wierszy",
  directCap: 20000,
  ceiling: 1000000,
  maxSelected: 500,
  rowField: "row_ref",
  copied: "Skopiowano referencję do schowka.",
  copyFailed: "Nie udało się skopiować referencji do schowka."
};

/* Opaque S6 references as the server renders them: base64url-ish blobs bearing
 * no relation to the identity they stand for. The harness never holds a raw
 * identity at all, which is the point of the contract. */
const REFERENCES = ["tok-AAAAref0000", "tok-BBBBref1111", "tok-CCCCref2222", "tok-DDDDref3333"];
const RAW_IDENTITY = "QQZX-RECORDID-NEVER-SHOWN-773311";

let seq = 0;
let fetches = 0;
const clipboardWrites = [];
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
    checked: false,
    get tagName() { return this._tag.toUpperCase(); },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
    get className() { return this._attrs["class"] || ""; },
    set className(v) { this._attrs["class"] = String(v); },
    get firstChild() { return this.childNodes[0] || null; },
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
    dispatch(type, event) {
      const ev = Object.assign({
        type: type, target: this, detail: undefined,
        preventDefault() { this.defaultPrevented = true; }, defaultPrevented: false
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
    },
    dispatchEvent(ev) { return this.dispatch(ev.type, ev); }
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

function buildPage(options) {
  options = options || {};
  const viewCount = options.viewCount === undefined ? 3 : options.viewCount;
  const datasetCount = options.datasetCount === undefined ? 48213 : options.datasetCount;
  const identity = options.identity !== false;

  document.body = makeNode("body", {});
  const sheet = makeNode("div", { "class": "db-sheet", "data-db-sheet": "" });
  document.body.appendChild(sheet);

  /* The rendered grid, whose rows carry the opaque S6 reference. */
  const table = makeNode("table", { "class": "db-table" });
  sheet.appendChild(table);
  const tbody = makeNode("tbody", {});
  table.appendChild(tbody);
  const rows = REFERENCES.map(function (reference, index) {
    const tr = makeNode("tr", identity ? { "class": "db-row", "data-db-row": reference } : { "class": "db-row" });
    tbody.appendChild(tr);
    const td = makeNode("td", { "data-db-column": "driver_name" });
    tr.appendChild(td);
    const span = makeNode("span", { "data-db-copy": "Kowalski " + index });
    td.appendChild(span);
    return tr;
  });

  /* The panel exactly as the server renders it. */
  const panel = makeNode("details", {
    "class": "db-export-panel",
    "data-db-export-panel": "",
    "data-db-export-strings": JSON.stringify(options.strings || STRINGS)
  });
  sheet.appendChild(panel);
  const form = makeNode("form", { "class": "db-export-form", "data-db-export-form": "" });
  panel.appendChild(form);

  const scopeInputs = {};
  [["view", viewCount], ["dataset", datasetCount], ["selection", 0]].forEach(function (pair) {
    const label = makeNode("label", { "class": "db-export-option" });
    form.appendChild(label);
    const input = makeNode("input", { type: "radio", name: "row_scope", value: pair[0] });
    if (pair[0] === "selection") {
      input.setAttribute("data-db-export-selection", "");
      input.setAttribute("disabled", "");
    }
    if (pair[0] === "view") { input.checked = true; }
    label.appendChild(input);
    const count = makeNode("span", { "class": "db-export-count", "data-db-export-count": pair[0] });
    count.textContent = String(pair[1]);
    label.appendChild(count);
    scopeInputs[pair[0]] = input;
  });

  const formats = {};
  ["xlsx", "csv"].forEach(function (name) {
    const label = makeNode("label", {});
    form.appendChild(label);
    const input = makeNode("input", { type: "radio", name: "format_name", value: name, "data-db-export-format": "" });
    if (name === "xlsx") { input.checked = true; }
    label.appendChild(input);
    formats[name] = input;
  });

  const notice = makeNode("p", { "class": "db-export-path is-direct", "data-db-export-path": "" });
  notice.textContent = STRINGS.direct;
  form.appendChild(notice);
  const submit = makeNode("button", { type: "submit", "data-db-export-submit": "" });
  submit.textContent = "Pobierz XLSX";
  form.appendChild(submit);
  const rowHost = makeNode("div", { "data-db-export-rows": "", hidden: "" });
  form.appendChild(rowHost);

  const status = makeNode("span", { role: "status", "aria-live": "polite", "data-db-export-status": "" });
  document.body.appendChild(status);

  global.window = {
    navigator: {
      clipboard: options.clipboard === "absent" ? undefined : {
        writeText(text) {
          clipboardWrites.push(String(text));
          if (options.clipboard === "reject") { return Promise.reject(new Error("denied")); }
          return Promise.resolve();
        }
      }
    },
    CustomEvent: function (type, init) {
      return { type: type, detail: (init || {}).detail };
    },
    addEventListener() {},
    location: { href: "/user/database/datasets/ds-1" }
  };
  global.navigator = global.window.navigator;

  return {
    sheet: sheet, form: form, rows: rows, scopes: scopeInputs, formats: formats,
    notice: notice, submit: submit, rowHost: rowHost, status: status, panel: panel
  };
}

function run() {
  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
}

function selectScope(page, scope) {
  Object.keys(page.scopes).forEach(function (key) { page.scopes[key].checked = key === scope; });
  page.form.dispatch("change", {});
}

function selectRows(page, rowIndexes) {
  page.sheet.dispatchEvent(new global.window.CustomEvent("db-selection-change", {
    detail: { rows: rowIndexes, cells: rowIndexes.length }
  }));
}

function postedReferences(page) {
  return page.rowHost.childNodes.map(function (node) {
    return { name: node.getAttribute("name"), value: node.getAttribute("value") };
  });
}

function result(page, extra) {
  return Object.assign({
    notice: page.notice.textContent,
    noticeClass: page.notice.className,
    submit: page.submit.textContent,
    submitDisabled: page.submit.hasAttribute("disabled"),
    selectionDisabled: page.scopes.selection.hasAttribute("disabled"),
    selectionCount: (function () {
      const node = page.form.querySelector('[data-db-export-count="selection"]');
      return node ? node.textContent : null;
    })(),
    posted: postedReferences(page),
    status: page.status.textContent,
    clipboard: clipboardWrites.slice(),
    execCopies: execCopies.slice(),
    fetches: fetches
  }, extra || {});
}

/* ---------------------------------------------------------------- scenarios */

const scenarios = {
  /* With no selection the scope is unavailable and states why. */
  "no-selection-disables-the-scope": function () {
    const page = buildPage({});
    run();
    selectScope(page, "selection");
    return result(page, {});
  },

  /* An S7 rectangle publishes row positions; the module maps them to the
     opaque references the server already rendered. */
  "selection-maps-rows-to-opaque-references": function () {
    const page = buildPage({});
    run();
    selectRows(page, [0, 2]);
    selectScope(page, "selection");
    return result(page, {});
  },

  /* Two cells in one row are one exported row. */
  "repeated-rows-deduplicate": function () {
    const page = buildPage({});
    run();
    selectRows(page, [1, 1, 1, 3]);
    selectScope(page, "selection");
    return result(page, {});
  },

  /* Clearing the selection clears the scope and its hidden fields, so a stale
     reference can never reach the server. */
  "clearing-the-selection-clears-the-scope": function () {
    const page = buildPage({});
    run();
    selectRows(page, [0, 1]);
    selectScope(page, "selection");
    const before = postedReferences(page).length;
    selectRows(page, []);
    return result(page, { before: before, scopeAfter: page.scopes.view.checked });
  },

  /* A row with no configured identity has no reference and is skipped rather
     than guessed at. */
  "rows-without-a-reference-are-skipped": function () {
    const page = buildPage({ identity: false });
    run();
    selectRows(page, [0, 1, 2]);
    selectScope(page, "selection");
    return result(page, {});
  },

  /* The path notice and submit label follow the resolved path. */
  "path-notice-follows-the-scope": function () {
    const page = buildPage({ viewCount: 3, datasetCount: 48213 });
    run();
    const direct = { notice: page.notice.textContent, submit: page.submit.textContent };
    selectScope(page, "dataset");
    const background = { notice: page.notice.textContent, submit: page.submit.textContent };
    return result(page, { direct: direct, background: background });
  },

  /* Above the ceiling the form refuses rather than letting the user commit. */
  "over-the-ceiling-refuses": function () {
    const page = buildPage({ viewCount: 3, datasetCount: 1000001 });
    run();
    selectScope(page, "dataset");
    return result(page, {});
  },

  /* The submit label follows the chosen format. */
  "format-changes-the-submit-label": function () {
    const page = buildPage({});
    run();
    page.formats.xlsx.checked = false;
    page.formats.csv.checked = true;
    page.form.dispatch("change", {});
    return result(page, {});
  },

  /* Copying a job reference announces success in the live region. */
  "copy-reference-succeeds": function () {
    const page = buildPage({});
    run();
    const button = makeNode("button", { "data-db-export-copy": "11111111-1111-1111-1111-111111111111" });
    document.body.appendChild(button);
    const event = button.dispatch("click", {});
    return new Promise(function (resolve) {
      setTimeout(function () { resolve(result(page, { prevented: !!event.defaultPrevented })); }, 0);
    });
  },

  /* A rejected clipboard falls back; a failing fallback reports safely. */
  "copy-reference-falls-back": function () {
    global.__execCopyWorks = false;
    const page = buildPage({ clipboard: "reject" });
    run();
    const button = makeNode("button", { "data-db-export-copy": "11111111-1111-1111-1111-111111111111" });
    document.body.appendChild(button);
    button.dispatch("click", {});
    return new Promise(function (resolve) {
      setTimeout(function () { resolve(result(page, {})); }, 0);
    });
  },

  /* The whole page state, dumped: no raw identity anywhere. */
  "module-holds-no-raw-identity": function () {
    const page = buildPage({});
    run();
    selectRows(page, [0, 1, 2, 3]);
    selectScope(page, "selection");
    const markup = JSON.stringify(document.body, function (k, v) {
      return k === "parentNode" || k === "_listeners" ? undefined : v;
    });
    return result(page, { markup: markup, rawIdentity: RAW_IDENTITY });
  }
};

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
