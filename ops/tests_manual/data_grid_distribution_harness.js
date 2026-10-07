/* DOM/fetch stub that executes the real api/static/js/data-grid-distribution.js.
 *
 * The asynchronous half of DB-005 §4 lives in JavaScript — when a request is
 * issued at all, what the loading/error states look like, whether a stale
 * response can overwrite a newer one, and how a picked value reaches the S3
 * form fields — so it is exercised by running the shipped file rather than by
 * reading it.
 *
 * Invoked by ops/tests_manual/test_portal_database_value_distributions.py:
 *
 *     node ops/tests_manual/data_grid_distribution_harness.js <scenario>
 *
 * Prints one JSON object describing the observed end state. Requests are
 * recorded but never performed; each scenario resolves them explicitly, which
 * is what makes ordering deterministic.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const SCRIPT = path.join(__dirname, "..", "..", "api", "static", "js", "data-grid-distribution.js");

const STRINGS = {
  loading: "Wczytywanie…",
  error: "Nie udało się wczytać rozkładu wartości.",
  empty: "Brak wierszy w bieżącym wyniku.",
  allNull: "Wszystkie wartości w bieżącym wyniku są puste.",
  singleValue: "Wszystkie wartości są takie same: {value}.",
  truncated: "Pokazano {shown} z {total} wartości — lista jest niepełna.",
  scope: "Liczby uwzględniają pozostałe filtry, ale nie filtr tej kolumny.",
  distinct: "{count} unikalnych",
  nonNull: "{count} niepustych",
  range: "min {min} · max {max}",
  bucket: "od {min} do {max}: {count}",
  selectValue: "Zaznacz wartość {value} ({count})",
  selectBlank: "Filtruj puste ({count})",
  nullMarker: "brak wartości",
  blankMarker: "pusty tekst",
  search: "Szukaj wśród wartości",
  searchHint: "Szuka tylko wśród pobranych wartości.",
  noMatches: "Brak pasujących wartości na tej liście.",
  offList: "Zaznaczono też {count} spoza tej listy.",
  limitReached: "Można wybrać najwyżej {limit} wartości.",
  maxValues: "50",
};

/* ------------------------------------------------------------------- DOM -- */

function makeNode(tag, attrs) {
  const node = {
    _tag: tag,
    _attrs: Object.assign({}, attrs || {}),
    nodeType: 1,
    childNodes: [],
    parentNode: null,
    style: {},
    _classes: [],
    _listeners: {},
    _text: "",
    checked: false,
    value: "",
    type: "",
    offsetParent: {},
    hasAttribute(name) { return name in this._attrs; },
    getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; },
    setAttribute(name, value) { this._attrs[name] = String(value); },
    removeAttribute(name) { delete this._attrs[name]; },
    addEventListener(type, fn) { (this._listeners[type] || (this._listeners[type] = [])).push(fn); },
    dispatchEvent(event) {
      (this._listeners[event.type] || []).forEach((fn) => fn(event));
      let parent = this.parentNode;
      while (parent && event.bubbles) {
        (parent._listeners[event.type] || []).forEach((fn) => fn(event));
        parent = parent.parentNode;
      }
      return true;
    },
    appendChild(child) { child.parentNode = this; this.childNodes.push(child); return child; },
    get textContent() {
      return this._text + this.childNodes.map((c) => c.textContent).join("");
    },
    set textContent(value) { this._text = String(value); this.childNodes = []; },
    set innerHTML(value) { if (!value) { this.childNodes = []; this._text = ""; } },
    get innerHTML() { return this.textContent; },
    closest(selector) {
      let current = this;
      while (current) {
        if (matches(current, selector)) { return current; }
        current = current.parentNode;
      }
      return null;
    },
  };
  node.classList = {
    add: (name) => { if (node._classes.indexOf(name) === -1) node._classes.push(name); },
    remove: (name) => { node._classes = node._classes.filter((c) => c !== name); },
    contains: (name) => node._classes.indexOf(name) !== -1,
  };
  /* Every element can be queried, including ones the script creates — in a real
     document `createElement` returns a fully queryable node, and a harness that
     did not would fail on the script's own generated markup. */
  node.querySelector = (sel) => queryAll(node, sel)[0] || null;
  node.querySelectorAll = (sel) => queryAll(node, sel);
  Object.defineProperty(node, "className", {
    get() { return node._classes.join(" "); },
    set(value) { node._classes = String(value).split(" ").filter(Boolean); },
  });
  return node;
}

function matches(node, selector) {
  const sel = String(selector).trim();
  if (sel.charAt(0) === "[") { return node.hasAttribute(sel.slice(1, -1)); }
  if (sel.charAt(0) === ".") { return node._classes.indexOf(sel.slice(1)) !== -1; }
  if (sel.indexOf("[") !== -1) {
    const tag = sel.slice(0, sel.indexOf("["));
    const attr = sel.slice(sel.indexOf("[") + 1, sel.lastIndexOf("]"));
    return node._tag === tag && node.hasAttribute(attr);
  }
  return node._tag === sel;
}

function walk(root, visit) {
  visit(root);
  root.childNodes.forEach((child) => walk(child, visit));
}

function queryAll(root, selector) {
  /* Only the shapes the script uses. A descendant selector is matched on its
     last simple part, which is sufficient inside an already-scoped subtree. */
  const parts = String(selector).split(",").map((s) => s.trim()).filter(Boolean);
  const out = [];
  walk(root, (node) => {
    if (node === root) { return; }
    for (let i = 0; i < parts.length; i += 1) {
      const last = parts[i].split(" ").pop();
      if (matches(node, last) && out.indexOf(node) === -1) { out.push(node); break; }
    }
  });
  return out;
}

/* --------------------------------------------------------------- fixture -- */

function buildMenu(column, mode, operator, selected) {
  const opSelect = makeNode("select", { "data-db-op": "", name: "op__" + column });
  opSelect.value = operator || "contains";

  const singleInput = makeNode("input", { name: "filter__" + column });
  const single = makeNode("div", { "data-db-value-single": "" });
  single.appendChild(singleInput);

  const fromInput = makeNode("input", { name: "filter_from__" + column });
  const toInput = makeNode("input", { name: "filter_to__" + column });
  const range = makeNode("div", { "data-db-value-range": "", hidden: "" });
  range.appendChild(fromInput);
  range.appendChild(toInput);

  const textarea = makeNode("textarea", { name: "filter_in__" + column });
  textarea.value = (selected || []).join("\n");
  const exactHost = makeNode("div", {
    "data-db-exact-values": "",
    "data-db-exact-name": "filter_exact__" + column,
  });
  const multi = makeNode("div", {
    "data-db-value-multi": "",
    hidden: "",
    /* The server's validated selection, exact — this is what the script seeds
       from, and it is the only form that survives whitespace and newlines. */
    "data-db-selected": JSON.stringify(selected || []),
  });
  multi.appendChild(textarea);
  multi.appendChild(exactHost);

  const controls = makeNode("div", { "data-db-filter-controls": "" });
  controls.appendChild(opSelect);
  controls.appendChild(single);
  controls.appendChild(range);
  controls.appendChild(multi);

  const nonNull = makeNode("span", { "data-db-nonnull": "" });
  const identity = makeNode("p", {});
  identity.appendChild(nonNull);

  const body = makeNode("div", { "data-db-distribution-body": "" });
  const container = makeNode("div", {
    "data-db-distribution": "",
    "data-db-distribution-url": "/user/database/datasets/D1/distribution?column=" + column,
    "data-db-distribution-mode": mode,
    "data-db-strings": JSON.stringify(STRINGS),
    "data-db-column": column,
  });
  container.appendChild(body);

  const panel = makeNode("div", {});
  panel.classList.add("db-col-panel");
  panel.appendChild(identity);
  panel.appendChild(controls);
  panel.appendChild(container);

  const details = makeNode("details", { "data-db-col-menu": "", id: "dbcol-" + column });
  details.open = false;
  details.appendChild(panel);

  return { details, container, body, opSelect, singleInput, fromInput, toInput,
           textarea, exactHost, multi, nonNull, column,
           staged: () => exactHost.childNodes.map((node) => node.value) };
}

function makeDocument(menus) {
  const root = makeNode("body", {});
  menus.forEach((menu) => root.appendChild(menu.details));

  const requests = [];
  const listeners = {};

  global.document = {
    nodeType: 9,
    readyState: "complete",
    addEventListener: (type, fn) => { (listeners[type] || (listeners[type] = [])).push(fn); },
    querySelector: (sel) => queryAll(root, sel)[0] || null,
    querySelectorAll: (sel) => queryAll(root, sel),
    createElement: (tag) => makeNode(tag, {}),
  };
  global.window = {
    addEventListener: (type, fn) => { (listeners[type] || (listeners[type] = [])).push(fn); },
    fetch(url) {
      let resolve;
      let reject;
      const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
      requests.push({ url: url, resolve: resolve, reject: reject });
      return promise;
    },
  };
  global.Event = function (type, options) {
    this.type = type;
    this.bubbles = !!(options && options.bubbles);
  };

  function toggle(menu, open) {
    menu.details.open = open;
    (menu.details._listeners.toggle || []).forEach((fn) => fn({}));
  }

  /* Resolve request `index` with `payload`, then let the promise chain settle. */
  function respond(index, payload, ok) {
    requests[index].resolve({
      ok: ok === undefined ? true : ok,
      json: () => Promise.resolve(payload),
    });
    return new Promise((res) => setImmediate(res)).then(() => new Promise((res) => setImmediate(res)));
  }

  function fail(index) {
    requests[index].resolve({ ok: false, json: () => Promise.resolve({}) });
    return new Promise((res) => setImmediate(res)).then(() => new Promise((res) => setImmediate(res)));
  }

  function fire(node, type) {
    node.dispatchEvent({ type: type, bubbles: true });
  }

  return { root, requests, toggle, respond, fail, fire };
}

function load() {
  eval(fs.readFileSync(SCRIPT, "utf8"));
}

function bodyText(menu) {
  return menu.body.textContent;
}

const CATEGORICAL = {
  column: "driver_name",
  label: "Kierowca",
  mode: "categorical",
  scope: "filtered_excluding_column",
  values: [
    { value: "Kowalski", count: 412 },
    { value: null, count: 90 },
    { value: "", count: 31 },
    { value: "   ", count: 7 },
  ],
  distinct_count: 1274,
  non_null_count: 1183,
  scope_rows: 1274,
  limit: 50,
  truncated: true,
};

const NUMERIC = {
  column: "distance_km",
  label: "Dystans",
  mode: "numeric",
  scope: "filtered_excluding_column",
  buckets: [
    { index: 1, min: "0", max: "10", count: 4 },
    { index: 2, min: "10", max: "20", count: 9 },
    { index: 3, min: "20", max: "30", count: 2 },
  ],
  bucket_count: 14,
  min: "0",
  max: "30",
  non_null_count: 15,
  null_count: 2,
  scope_rows: 17,
  domain: "range",
};

/* ------------------------------------------------------------- scenarios -- */

const scenario = process.argv[2];
const out = {};

function finish() {
  process.stdout.write(JSON.stringify(out));
}

if (scenario === "on-demand") {
  const a = buildMenu("driver_name", "categorical");
  const b = buildMenu("distance_km", "numeric");
  const doc = makeDocument([a, b]);
  load();
  /* Nothing may be requested merely because the page rendered. */
  out.afterLoad = { requests: doc.requests.length };
  doc.toggle(a, true);
  out.afterOpenFirst = { requests: doc.requests.length, url: doc.requests[0] && doc.requests[0].url };
  out.loadingText = bodyText(a);
  out.loadingBusy = a.body.getAttribute("aria-busy");
  doc.respond(0, CATEGORICAL).then(function () {
    out.afterRespond = { requests: doc.requests.length, busy: a.body.getAttribute("aria-busy") };
    /* Reopening the same menu must not requery. */
    doc.toggle(a, false);
    doc.toggle(a, true);
    out.afterReopen = { requests: doc.requests.length };
    finish();
  });
} else if (scenario === "categorical-render") {
  const menu = buildMenu("driver_name", "categorical");
  const doc = makeDocument([menu]);
  load();
  doc.toggle(menu, true);
  doc.respond(0, CATEGORICAL).then(function () {
    const text = bodyText(menu);
    out.nonNull = menu.nonNull.textContent;
    out.hasDistinct = text.indexOf("1 274 unikalnych") !== -1 || text.indexOf("unikalnych") !== -1;
    out.truncated = text.indexOf("niepełna") !== -1;
    out.scopeStated = text.indexOf("nie filtr tej kolumny") !== -1;
    out.nullMarker = text.indexOf("brak wartości") !== -1;
    out.blankMarker = text.indexOf("pusty tekst") !== -1;
    out.whitespaceKept = text.indexOf("   ") !== -1;
    const picks = doc.root.querySelectorAll("[data-db-value-pick]");
    /* NULL and empty string are not `in (…)` candidates; the whitespace value is. */
    out.pickable = picks.map((p) => p.value);
    finish();
  });
} else if (scenario === "categorical-select") {
  const menu = buildMenu("driver_name", "categorical");
  const doc = makeDocument([menu]);
  load();
  doc.toggle(menu, true);
  doc.respond(0, CATEGORICAL).then(function () {
    const picks = doc.root.querySelectorAll("[data-db-value-pick]");
    picks[0].checked = true;
    doc.fire(picks[0], "change");
    out.afterOnePick = { operator: menu.opSelect.value, staged: menu.staged() };
    picks[1].checked = true;
    doc.fire(picks[1], "change");
    out.afterTwoPicks = { operator: menu.opSelect.value, staged: menu.staged() };
    /* Staging must not submit anything: applying is still the menu's Zastosuj. */
    out.requests = doc.requests.length;
    /* The human textarea is cleared so the two forms cannot disagree. */
    out.textareaCleared = menu.textarea.value === "";

    const blank = doc.root.querySelectorAll(".db-distribution-blank");
    if (blank.length) {
      doc.fire(blank[0], "click");
    }
    out.afterBlank = { operator: menu.opSelect.value };
    finish();
  });
} else if (scenario === "selection-preserves-off-list") {
  /* Two of the three staged values are absent from the fetched top set. */
  const staged = ["Kowalski", "Kraków", "Rzeszów"];
  const menu = buildMenu("driver_name", "categorical", "in", staged);
  const doc = makeDocument([menu]);
  load();
  doc.toggle(menu, true);
  doc.respond(0, Object.assign({}, CATEGORICAL, {
    values: [{ value: "Kowalski", count: 5 }, { value: "Poznań", count: 3 }],
    distinct_count: 900, truncated: true,
  })).then(function () {
    const picks = doc.root.querySelectorAll("[data-db-value-pick]");
    out.initialChecked = picks.map((p) => [p.value, p.checked]);
    out.offListNoticed = menu.body.textContent.indexOf("spoza tej listy") !== -1;

    /* Adding a visible value must EXTEND, never replace. */
    const poznan = picks.filter((p) => p.value === "Poznań")[0];
    poznan.checked = true;
    doc.fire(poznan, "change");
    out.afterAdd = menu.staged();

    /* Deselecting a visible value removes only that one. */
    const kowalski = picks.filter((p) => p.value === "Kowalski")[0];
    kowalski.checked = false;
    doc.fire(kowalski, "change");
    out.afterRemove = menu.staged();
    finish();
  });
} else if (scenario === "selection-lossless") {
  /* Every shape the picker can display and count must be selectable back to
     itself: whitespace-only, leading/trailing space, embedded newline, tab and
     injection-shaped text. */
  const sources = ["   ", " lead", "trail ", "a\nb", "\ttab", "' OR '1'='1"];
  const menu = buildMenu("driver_name", "categorical");
  const doc = makeDocument([menu]);
  load();
  doc.toggle(menu, true);
  doc.respond(0, Object.assign({}, CATEGORICAL, {
    values: sources.map((value, index) => ({ value: value, count: 10 - index })),
    distinct_count: sources.length, truncated: false, non_null_count: 45,
  })).then(function () {
    const picks = doc.root.querySelectorAll("[data-db-value-pick]");
    picks.forEach(function (box) { box.checked = true; doc.fire(box, "change"); });
    out.sources = sources;
    out.staged = menu.staged();
    finish();
  });
} else if (scenario === "selection-cap") {
  const staged = [];
  for (let i = 0; i < 50; i += 1) { staged.push("v" + i); }
  const menu = buildMenu("driver_name", "categorical", "in", staged);
  const doc = makeDocument([menu]);
  load();
  doc.toggle(menu, true);
  doc.respond(0, Object.assign({}, CATEGORICAL, {
    values: [{ value: "extra", count: 1 }], distinct_count: 51, truncated: false,
  })).then(function () {
    const box = doc.root.querySelectorAll("[data-db-value-pick]")[0];
    box.checked = true;
    doc.fire(box, "change");
    out.boxReverted = box.checked === false;
    out.limitStated = menu.body.textContent.indexOf("najwyżej") !== -1;
    /* Refused, so nothing was rewritten: the script wrote no exact inputs and
       the server-rendered form still carries the previous valid 50 values. */
    out.exactWritten = menu.staged().length;
    out.selectionUnchanged =
      JSON.parse(menu.multi.getAttribute("data-db-selected")).join("\u0000") === staged.join("\u0000");
    out.textareaIntact = menu.textarea.value.split("\n").length === staged.length;
    finish();
  });
} else if (scenario === "in-flight-reopen") {
  const menu = buildMenu("driver_name", "categorical");
  const doc = makeDocument([menu]);
  load();
  doc.toggle(menu, true);
  out.afterOpen = doc.requests.length;
  /* Close and reopen while the first request is still pending. */
  doc.toggle(menu, false);
  doc.toggle(menu, true);
  out.afterReopenWhilePending = doc.requests.length;
  doc.respond(0, CATEGORICAL).then(function () {
    doc.toggle(menu, false);
    doc.toggle(menu, true);
    out.afterReopenWhenLoaded = doc.requests.length;
    finish();
  });
} else if (scenario === "failed-retry") {
  const menu = buildMenu("driver_name", "categorical");
  const doc = makeDocument([menu]);
  load();
  doc.toggle(menu, true);
  doc.fail(0).then(function () {
    out.afterFailure = doc.requests.length;
    /* A failure must clear the in-flight guard so reopening retries. */
    doc.toggle(menu, false);
    doc.toggle(menu, true);
    out.afterReopen = doc.requests.length;
    return doc.respond(1, CATEGORICAL);
  }).then(function () {
    out.recovered = menu.body.textContent.indexOf("unikalnych") !== -1;
    finish();
  });
} else if (scenario === "local-search") {
  const staged = ["Kowalski", "OFFLIST"];
  const menu = buildMenu("driver_name", "categorical", "in", staged);
  const doc = makeDocument([menu]);
  load();
  doc.toggle(menu, true);
  doc.respond(0, Object.assign({}, CATEGORICAL, {
    values: [{ value: "Kowalski", count: 9 }, { value: "Nowak", count: 4 }, { value: "Zielinski", count: 2 }],
    distinct_count: 400, truncated: true,
  })).then(function () {
    /* Make the staged state exact-backed first, so the search assertions read
       real staged values rather than the server-rendered seed. */
    const nowak = doc.root.querySelectorAll("[data-db-value-pick]").filter((p) => p.value === "Nowak")[0];
    nowak.checked = true;
    doc.fire(nowak, "change");
    out.stagedInitial = menu.staged();

    const search = doc.root.querySelectorAll(".db-distribution-search")[0];
    const rows = () => doc.root.querySelectorAll(".db-distribution-value");
    const visible = () => rows().filter((r) => !r.hasAttribute("hidden")).length;
    out.beforeSearch = visible();
    out.truncatedNoticeBefore = menu.body.textContent.indexOf("niepełna") !== -1;

    search.value = "kowal";
    doc.fire(search, "input");
    out.afterSearch = visible();
    out.requestsAfterSearch = doc.requests.length;
    out.stagedAfterSearch = menu.staged();

    /* Hiding a selected row must not deselect it or drop staged state. */
    search.value = "nowak";
    doc.fire(search, "input");
    out.selectedHidden = visible();
    out.stagedWhileHidden = menu.staged();

    search.value = "";
    doc.fire(search, "input");
    out.afterClear = visible();
    const kowalski = doc.root.querySelectorAll("[data-db-value-pick]").filter((p) => p.value === "Kowalski")[0];
    out.stillChecked = kowalski.checked;
    out.truncatedNoticeAfter = menu.body.textContent.indexOf("niepełna") !== -1;
    finish();
  });
} else if (scenario === "numeric-marker") {
  const menu = buildMenu("distance_km", "numeric", "gte");
  const doc = makeDocument([menu]);
  load();
  doc.toggle(menu, true);
  doc.respond(0, NUMERIC).then(function () {
    const bars = () => doc.root.querySelectorAll(".db-histogram-bar");
    out.bars = bars().length;
    out.axisStated = bodyText(menu).indexOf("min 0 · max 30") !== -1;
    out.markedAtRest = bars().filter((b) => b.classList.contains("db-histogram-marked")).length;

    menu.singleInput.value = "15";
    doc.fire(menu.singleInput, "input");
    out.markedAfterTyping = bars()
      .filter((b) => b.classList.contains("db-histogram-marked"))
      .map((b) => b.getAttribute("data-db-bucket"));
    /* Moving the marker must issue no query and change no applied state. */
    out.requestsAfterTyping = doc.requests.length;
    out.stagedValueUnchanged = menu.singleInput.value;
    finish();
  });
} else if (scenario === "numeric-degenerate") {
  const results = {};
  const cases = [
    ["empty", { mode: "numeric", domain: "empty", scope_rows: 0, non_null_count: 0, buckets: [], min: null, max: null }],
    ["all_null", { mode: "numeric", domain: "all_null", scope_rows: 3, non_null_count: 0, buckets: [], min: null, max: null }],
    ["single_value", { mode: "numeric", domain: "single_value", scope_rows: 3, non_null_count: 3, buckets: [], min: "7", max: "7" }],
  ];
  let chain = Promise.resolve();
  cases.forEach(function (entry) {
    chain = chain.then(function () {
      const menu = buildMenu("distance_km", "numeric");
      const doc = makeDocument([menu]);
      load();
      doc.toggle(menu, true);
      return doc.respond(0, entry[1]).then(function () {
        results[entry[0]] = {
          text: bodyText(menu),
          bars: doc.root.querySelectorAll(".db-histogram-bar").length,
        };
      });
    });
  });
  chain.then(function () { out.cases = results; finish(); });
} else if (scenario === "stale-response") {
  const a = buildMenu("driver_name", "categorical");
  const b = buildMenu("note", "categorical");
  const doc = makeDocument([a, b]);
  load();
  doc.toggle(a, true);
  doc.toggle(b, true);
  out.requests = doc.requests.length;
  /* The SECOND menu answers first, then the first menu's slow answer lands. */
  doc.respond(1, Object.assign({}, CATEGORICAL, {
    column: "note", values: [{ value: "NOWY", count: 5 }], distinct_count: 1,
    non_null_count: 5, truncated: false,
  })).then(function () {
    return doc.respond(0, Object.assign({}, CATEGORICAL, {
      values: [{ value: "STARY", count: 999 }], distinct_count: 1,
      non_null_count: 999, truncated: false,
    }));
  }).then(function () {
    out.secondMenu = bodyText(b);
    out.firstMenu = bodyText(a);
    finish();
  });
} else if (scenario === "stale-same-container") {
  /* The in-flight guard makes two concurrent requests for one container
     structurally impossible, so the same-container race the token used to be
     the only defence against can no longer be created through the UI. What is
     asserted here is that outcome, plus that the pending response still lands
     after a close/reopen rather than being lost with it. */
  const menu = buildMenu("driver_name", "categorical");
  const doc = makeDocument([menu]);
  load();
  doc.toggle(menu, true);
  doc.toggle(menu, false);
  doc.toggle(menu, true);
  out.requests = doc.requests.length;
  doc.respond(0, Object.assign({}, CATEGORICAL, {
    values: [{ value: "NOWY", count: 5 }], distinct_count: 1, non_null_count: 5, truncated: false,
  })).then(function () {
    out.rendered = bodyText(menu);
    out.nonNull = menu.nonNull.textContent;
    finish();
  });
} else if (scenario === "error-state") {
  const menu = buildMenu("driver_name", "categorical");
  const doc = makeDocument([menu]);
  load();
  doc.toggle(menu, true);
  doc.fail(0).then(function () {
    out.text = bodyText(menu);
    out.busy = menu.body.getAttribute("aria-busy");
    /* Manual filtering in the same menu is untouched. */
    menu.opSelect.value = "eq";
    menu.singleInput.value = "Kowalski";
    out.manualFilterUsable = {
      operator: menu.opSelect.value,
      value: menu.singleInput.value,
    };
    /* A failed request is not cached: reopening retries. */
    doc.toggle(menu, false);
    doc.toggle(menu, true);
    out.requestsAfterReopen = doc.requests.length;
    finish();
  });
} else {
  throw new Error("unknown scenario: " + scenario);
}
