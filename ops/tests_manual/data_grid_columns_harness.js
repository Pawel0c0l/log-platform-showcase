/* DOM/history stub that executes the real api/static/js/data-grid-columns.js.
 *
 * The parts of the S5 contract that live in JavaScript — in-place reorder,
 * resize and pin without a requery, one history entry per committed layout,
 * Back/Forward restoration, the all-hidden refusal and the viewport rule for
 * pinning — are exercised by running the shipped file rather than by reading it.
 *
 * Invoked by ops/tests_manual/test_portal_database_column_management.py:
 *
 *     node ops/tests_manual/data_grid_columns_harness.js <scenario> [json-args]
 *
 * Prints one JSON object describing the observed end state. Any real navigation
 * (a form submit or a link click that was not prevented) and any fetch or
 * reload is recorded, because a pure layout change must issue neither.
 */
"use strict";

const path = require("path");

const SCRIPT = path.join(__dirname, "..", "..", "api", "static", "js", "data-grid-columns.js");

const STRINGS = {
  pin_refused: "Przypięte kolumny nie mogą zajmować więcej niż 40% szerokości tabeli.",
  min_one: "Co najmniej jedna kolumna musi pozostać widoczna.",
  resize_aria: "Szerokość kolumny {column}: {width} pikseli.",
  no_matches: "Brak pasujących kolumn.",
  moved: "Przeniesiono kolumnę {column} na pozycję {position} z {total}."
};

/* Approved catalogue for the fixture, mirroring what the server renders. */
const CATALOG = ["trip_start", "driver_name", "distance_km"];
const DEFAULT_WIDTHS = { trip_start: 168, driver_name: 200, distance_km: 120 };
const CELLS = {
  trip_start: ["2026-05-28 07:30"],
  driver_name: ["Kowalski"],
  distance_km: ["128,40"]
};

/* --------------------------------------------------------------------- DOM */

let seq = 0;

function makeNode(tag, attrs, children) {
  const node = {
    _tag: String(tag).toLowerCase(),
    _attrs: Object.assign({}, attrs || {}),
    _id: (seq += 1),
    _classes: [],
    _listeners: {},
    _text: "",
    nodeType: 1,
    childNodes: [],
    parentNode: null,
    style: {},
    checked: false,
    value: "",
    clientWidth: 0,
    get tagName() { return this._tag.toUpperCase(); },
    hasAttribute(name) { return name in this._attrs; },
    getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; },
    setAttribute(name, value) { this._attrs[name] = String(value); },
    removeAttribute(name) { delete this._attrs[name]; },
    addEventListener(type, fn) { (this._listeners[type] || (this._listeners[type] = [])).push(fn); },
    focus() { global.__focused = this; },
    appendChild(child) { detach(child); child.parentNode = this; this.childNodes.push(child); return child; },
    insertBefore(child, ref) {
      detach(child);
      const at = this.childNodes.indexOf(ref);
      child.parentNode = this;
      if (at === -1) { this.childNodes.push(child); } else { this.childNodes.splice(at, 0, child); }
      return child;
    },
    removeChild(child) {
      const at = this.childNodes.indexOf(child);
      if (at !== -1) { this.childNodes.splice(at, 1); child.parentNode = null; }
      return child;
    },
    get nextSibling() {
      if (!this.parentNode) { return null; }
      const at = this.parentNode.childNodes.indexOf(this);
      return this.parentNode.childNodes[at + 1] || null;
    },
    get textContent() {
      return this._text + this.childNodes.map((c) => c.textContent || "").join("");
    },
    set textContent(value) { this._text = String(value); this.childNodes.length = 0; },
    querySelector(selector) { return query(this, selector)[0] || null; },
    querySelectorAll(selector) { return query(this, selector); },
    closest(selector) { return closest(this, selector); }
  };
  node.classList = {
    add(name) { if (node._classes.indexOf(name) === -1) { node._classes.push(name); } },
    remove(name) { node._classes = node._classes.filter((c) => c !== name); },
    contains(name) { return node._classes.indexOf(name) !== -1; }
  };
  if (attrs && attrs.class) {
    node._classes = String(attrs.class).split(/\s+/).filter(Boolean);
  }
  Object.defineProperty(node, "className", {
    get() { return node._classes.join(" "); },
    set(value) {
      node._attrs.class = String(value);
      node._classes = String(value).split(/\s+/).filter(Boolean);
    }
  });
  (children || []).forEach((child) => node.appendChild(child));
  return node;
}

function detach(child) {
  if (child.parentNode) {
    const at = child.parentNode.childNodes.indexOf(child);
    if (at !== -1) { child.parentNode.childNodes.splice(at, 1); }
    child.parentNode = null;
  }
}

/* A deliberately small selector engine: tag, .class, [attr], [attr="value"],
   descendant combinators and comma lists. An unsupported shape throws, so the
   harness cannot pass by accident. */
function matchesSimple(node, selector) {
  const parts = selector.match(/^([a-zA-Z]*)((?:\.[-\w]+|\[[^\]]+\])*)$/);
  if (!parts) {
    throw new Error("unsupported selector: " + selector);
  }
  if (parts[1] && node._tag !== parts[1].toLowerCase()) {
    return false;
  }
  const rest = parts[2] || "";
  const tokens = rest.match(/\.[-\w]+|\[[^\]]+\]/g) || [];
  for (const token of tokens) {
    if (token.charAt(0) === ".") {
      if (node._classes.indexOf(token.slice(1)) === -1) { return false; }
    } else {
      const body = token.slice(1, -1);
      const eq = body.indexOf("=");
      if (eq === -1) {
        if (!(body in node._attrs)) { return false; }
      } else {
        const name = body.slice(0, eq);
        const value = body.slice(eq + 1).replace(/^["']|["']$/g, "");
        if (node._attrs[name] !== value) { return false; }
      }
    }
  }
  return true;
}

function matchesCompound(node, selector) {
  const chain = selector.trim().split(/\s+/);
  if (!matchesSimple(node, chain[chain.length - 1])) {
    return false;
  }
  let cursor = node.parentNode;
  for (let i = chain.length - 2; i >= 0; i -= 1) {
    let found = false;
    while (cursor) {
      if (matchesSimple(cursor, chain[i])) { found = true; cursor = cursor.parentNode; break; }
      cursor = cursor.parentNode;
    }
    if (!found) { return false; }
  }
  return true;
}

function matches(node, selector) {
  return selector.split(",").some((part) => part.trim() && matchesCompound(node, part.trim()));
}

function query(root, selector) {
  const out = [];
  (function walk(node) {
    node.childNodes.forEach((child) => {
      if (child.nodeType === 1) {
        if (matches(child, selector)) { out.push(child); }
        walk(child);
      }
    });
  })(root);
  return out;
}

function closest(node, selector) {
  let cursor = node;
  while (cursor && cursor.nodeType === 1) {
    if (matches(cursor, selector)) { return cursor; }
    cursor = cursor.parentNode;
  }
  return null;
}

/* ------------------------------------------------------------------ fixture */

function buildDocument(opts) {
  const result = {
    navigated: false,
    submitted: false,
    fetches: 0,
    reloads: 0,
    history: []
  };

  const sheetAttrs = {
    "data-db-sheet": "",
    "data-col-catalog": CATALOG.join(","),
    "data-col-min-width": "64",
    "data-col-max-width": "480",
    "data-col-max-pins": "4",
    "data-col-pin-fraction": "0.4",
    "data-col-pin-max-width": "576",
    "data-col-autofit-char": "7",
    "data-col-autofit-padding": "52",
    "data-db-strings": JSON.stringify(STRINGS)
  };

  const order = opts.order || CATALOG.slice(0);
  const widths = Object.assign({}, DEFAULT_WIDTHS, opts.widths || {});
  const pins = opts.pins || [order[0]];

  const colgroup = makeNode("colgroup", {}, order.map((name) => {
    const col = makeNode("col", { "data-db-column": name, "data-db-default-width": String(DEFAULT_WIDTHS[name]) });
    col.style.width = widths[name] + "px";
    return col;
  }));

  let running = 0;
  const offsets = {};
  pins.forEach((name) => { offsets[name] = running; running += widths[name]; });

  const headRow = makeNode("tr", {}, order.map((name) => {
    const attrs = { "data-db-column": name, scope: "col" };
    const th = makeNode("th", attrs, [
      makeNode("span", { class: "db-col-label" }),
      makeNode("a", { class: "db-col-resize", "data-db-resize": "", "data-db-column": name })
    ]);
    th.childNodes[0]._text = name === "driver_name" ? "Kierowca" : name;
    if (name in offsets) { th.classList.add("db-sticky-col"); th.style.left = offsets[name] + "px"; }
    return th;
  }));

  const bodyRow = makeNode("tr", {}, order.map((name) => {
    const td = makeNode("td", { "data-db-column": name });
    td._text = CELLS[name][0];
    if (name in offsets) { td.classList.add("db-sticky-col"); td.style.left = offsets[name] + "px"; }
    return td;
  }));

  const table = makeNode("table", { class: "db-table" }, [
    colgroup,
    makeNode("thead", {}, [headRow]),
    makeNode("tbody", {}, [bodyRow])
  ]);
  table._classes = ["db-table"];
  table.style.width = order.reduce((sum, name) => sum + widths[name], 0) + "px";

  const scroller = makeNode("div", { "data-db-scroll": "", class: "db-table-scroll" }, [table]);
  scroller.clientWidth = opts.viewport === undefined ? 1200 : opts.viewport;

  /* The DB-008 panel, in the same shape the server renders. */
  const list = makeNode("ul", { "data-db-cols-list": "", class: "db-cols-list" }, order.map((name) => {
    const row = makeNode("li", {
      class: "db-cols-row",
      "data-db-col-row": "",
      "data-db-column": name,
      "data-db-state": "visible",
      "data-db-label": name
    }, [
      makeNode("span", { class: "db-cols-handle", "data-db-handle": "", hidden: "" }),
      makeNode("input", { type: "checkbox", name: "cols", value: name }),
      makeNode("input", { type: "checkbox", name: "colpin", value: name }),
      makeNode("a", { class: "db-cols-move", "data-db-move": "up", "data-db-column": name }),
      makeNode("a", { class: "db-cols-move", "data-db-move": "down", "data-db-column": name })
    ]);
    row.childNodes[1].checked = true;
    row.childNodes[2].checked = pins.indexOf(name) !== -1;
    return row;
  }));

  const orderField = makeNode("input", { type: "hidden", name: "colorder", "data-db-order-field": "", value: order.join(",") });
  orderField.value = order.join(",");
  const form = makeNode("form", { class: "db-columns-form", "data-db-columns-form": "", method: "get" }, [
    makeNode("input", { type: "hidden", name: "colsel", value: "1" }),
    makeNode("input", { type: "hidden", name: "colpin", value: "" }),
    orderField,
    makeNode("div", { "data-db-cols-search": "", hidden: "" }, [makeNode("input", { "data-db-cols-query": "" })]),
    makeNode("div", { "data-db-cols-tabs": "", hidden: "" }, [
      makeNode("button", { "data-db-cols-tab": "all", "aria-pressed": "true" }),
      makeNode("button", { "data-db-cols-tab": "visible", "aria-pressed": "false" }),
      makeNode("button", { "data-db-cols-tab": "hidden", "aria-pressed": "false" })
    ]),
    list,
    makeNode("p", { "data-db-cols-status": "" })
  ]);
  const panel = makeNode("details", { "data-db-columns": "", id: "db-columns", open: "" }, [form]);

  /* Pin toggles in the column menus. */
  const menus = makeNode("div", {}, order.map((name) => makeNode("a", {
    class: "db-col-action",
    "data-db-pin": pins.indexOf(name) === -1 ? "on" : "off",
    "data-db-column": name
  })));

  const sheet = makeNode("div", sheetAttrs, [panel, scroller, menus]);
  const body = makeNode("body", {}, [sheet]);
  const root = makeNode("html", {}, [body]);

  const listeners = {};
  const location = {
    pathname: "/user/database/datasets/D1",
    search: opts.search || "",
    get href() { return this.pathname + this.search; },
    set href(value) { result.navigated = true; },
    reload() { result.reloads += 1; }
  };

  const stack = [{ url: location.pathname + location.search }];
  let index = 0;
  const timers = [];

  function applyUrl(url) {
    const q = url.indexOf("?");
    location.pathname = q === -1 ? url : url.slice(0, q);
    location.search = q === -1 ? "" : url.slice(q);
  }

  const history = {
    pushState(state, title, url) {
      stack.length = index + 1;
      stack.push({ url: String(url), state });
      index = stack.length - 1;
      applyUrl(String(url));
      result.history.push("push");
    },
    replaceState(state, title, url) {
      stack[index] = { url: String(url), state };
      applyUrl(String(url));
      result.history.push("replace");
    },
    go(delta) {
      const next = index + delta;
      if (next < 0 || next >= stack.length) { return; }
      index = next;
      applyUrl(stack[index].url);
      (listeners.popstate || []).forEach((fn) => fn({ state: stack[index].state }));
    }
  };

  global.document = {
    readyState: "complete",
    documentElement: root,
    body: body,
    addEventListener: (type, fn) => { (listeners[type] || (listeners[type] = [])).push(fn); },
    querySelector: (selector) => query(root, selector)[0] || null,
    querySelectorAll: (selector) => query(root, selector),
    createElement: (tag) => makeNode(tag, {}),
    createTextNode: (text) => ({ nodeType: 3, textContent: String(text), childNodes: [], parentNode: null })
  };
  global.window = {
    location,
    history,
    addEventListener: (type, fn) => { (listeners[type] || (listeners[type] = [])).push(fn); },
    /* Timers are queued rather than fired, so a scenario can observe the state
       between a deferred commit being scheduled and it running. */
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout: (id) => { if (id) { timers[id - 1] = null; } },
    fetch: () => { result.fetches += 1; return Promise.resolve({ ok: true, json: () => Promise.resolve({}) }); }
  };
  global.navigator = {};

  function dispatch(type, event) {
    (listeners[type] || []).forEach((fn) => fn(event));
  }

  function bubble(node, type, event) {
    let cursor = node;
    while (cursor) {
      (cursor._listeners[type] || []).forEach((fn) => fn(event));
      cursor = cursor.parentNode;
    }
    dispatch(type, event);
  }

  function makeEvent(target, extra) {
    return Object.assign({
      target,
      preventDefault() { this.defaultPrevented = true; },
      defaultPrevented: false
    }, extra || {});
  }

  function flushTimers() {
    while (timers.length) {
      const fn = timers.shift();
      if (fn) { fn(); }
    }
  }

  return { result, root, sheet, table, panel, form, list, scroller, history, location,
           dispatch, bubble, makeEvent, menus, order, widths, pins, flushTimers };
}

/* ------------------------------------------------------------------ reading */

function readOrder(harness) {
  return query(harness.table, "col[data-db-column]").map((c) => c.getAttribute("data-db-column"));
}

function readWidths(harness) {
  const out = {};
  query(harness.table, "col[data-db-column]").forEach((c) => {
    out[c.getAttribute("data-db-column")] = parseInt(String(c.style.width || "").replace("px", ""), 10);
  });
  return out;
}

function readPins(harness) {
  return query(harness.table, "th").filter((th) => th.classList.contains("db-sticky-col"))
    .map((th) => th.getAttribute("data-db-column"));
}

function readOffsets(harness) {
  return query(harness.table, "th").filter((th) => th.classList.contains("db-sticky-col"))
    .map((th) => parseInt(String(th.style.left || "0").replace("px", ""), 10));
}

function readHeaderOrder(harness) {
  return query(harness.table, "thead th").map((th) => th.getAttribute("data-db-column"));
}

function readBodyOrder(harness) {
  return query(harness.table, "tbody tr").map((tr) =>
    tr.childNodes.filter((c) => c.nodeType === 1).map((td) => td.getAttribute("data-db-column")));
}

function run(harness) {
  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
}

/* ---------------------------------------------------------------- scenarios */

const scenarios = {
  "panel-apply-order": (args) => {
    const h = buildDocument({
      search: "?filter__driver_name=Kowal&op__driver_name=contains&sort=trip_start&direction=desc",
      pins: []
    });
    run(h);
    /* Move `distance_km` to the top with the keyboard-accessible control, then
       commit. Nothing may reach the grid before the commit. */
    function moveUp(name) {
      const link = query(h.list, '[data-db-move="up"][data-db-column="' + name + '"]')[0];
      h.bubble(link, "click", h.makeEvent(link));
    }
    moveUp("distance_km");
    moveUp("distance_km");
    const beforeCommit = readOrder(h);
    const staged = h.list.childNodes
      .filter((n) => n.nodeType === 1)
      .map((n) => n.getAttribute("data-db-column"));
    const submitEvent = h.makeEvent(h.form);
    h.bubble(h.form, "submit", submitEvent);
    h.result.submitted = !submitEvent.defaultPrevented;
    return Object.assign(h.result, {
      beforeCommit,
      staged,
      orderField: h.form.querySelector("[data-db-order-field]").value,
      headerOrder: readHeaderOrder(h),
      bodyOrder: readBodyOrder(h),
      order: readOrder(h),
      url: h.location.search
    });
  },

  "panel-apply-empty": () => {
    const h = buildDocument({ pins: [] });
    run(h);
    query(h.form, 'input[name="cols"]').forEach((input) => { input.checked = false; });
    const event = h.makeEvent(h.form);
    h.bubble(h.form, "submit", event);
    h.result.submitted = !event.defaultPrevented;
    const warning = h.panel.querySelector(".db-cols-warning");
    return Object.assign(h.result, { warning: warning ? warning.textContent : null });
  },

  "panel-apply-visibility": () => {
    const h = buildDocument({ pins: [] });
    run(h);
    query(h.form, 'input[name="cols"]')[2].checked = false;
    const event = h.makeEvent(h.form);
    h.bubble(h.form, "submit", event);
    h.result.submitted = !event.defaultPrevented;
    return h.result;
  },

  "resize-drag": (args) => {
    const delta = args && args.delta !== undefined ? args.delta : 60;
    const h = buildDocument({ pins: [] });
    run(h);
    const handle = query(h.table, '[data-db-resize][data-db-column="driver_name"]')[0];
    h.dispatch("mousedown", h.makeEvent(handle, { clientX: 500 }));
    /* Several intermediate movements: none of them may touch history. */
    h.dispatch("mousemove", h.makeEvent(handle, { clientX: 500 + Math.round(delta / 3) }));
    h.dispatch("mousemove", h.makeEvent(handle, { clientX: 500 + Math.round((2 * delta) / 3) }));
    const midHistory = h.result.history.length;
    h.dispatch("mousemove", h.makeEvent(handle, { clientX: 500 + delta }));
    h.dispatch("mouseup", h.makeEvent(handle, { clientX: 500 + delta }));
    return Object.assign(h.result, {
      midHistory,
      widths: readWidths(h),
      url: h.location.search
    });
  },

  autofit: () => {
    const h = buildDocument({ pins: [] });
    run(h);
    const handle = query(h.table, '[data-db-resize][data-db-column="driver_name"]')[0];
    h.dispatch("click", h.makeEvent(handle));
    /* Mirrors the server: padding + longest rendered text * char width. */
    const longest = Math.max("Kierowca".length, CELLS.driver_name[0].length);
    return Object.assign(h.result, {
      widths: readWidths(h),
      serverAutofit: Math.max(64, Math.min(480, 52 + longest * 7)),
      url: h.location.search
    });
  },

  "panel-dismiss-discards": () => {
    const h = buildDocument({ pins: [] });
    run(h);
    const link = query(h.list, '[data-db-move="up"][data-db-column="distance_km"]')[0];
    h.bubble(link, "click", h.makeEvent(link));
    const staged = h.list.childNodes.filter((n) => n.nodeType === 1)
      .map((n) => n.getAttribute("data-db-column"));
    /* Esc closes the panel and DISCARDS the staged arrangement (INT §3). */
    h.dispatch("keydown", h.makeEvent(h.panel, { key: "Escape" }));
    return Object.assign(h.result, {
      staged,
      open: h.panel.hasAttribute("open"),
      afterDismiss: h.list.childNodes.filter((n) => n.nodeType === 1)
        .map((n) => n.getAttribute("data-db-column")),
      orderField: h.form.querySelector("[data-db-order-field]").value,
      grid: readOrder(h)
    });
  },

  "resize-keyboard": () => {
    const h = buildDocument({ pins: [] });
    run(h);
    const handle = query(h.table, '[data-db-resize][data-db-column="driver_name"]')[0];
    /* Three arrow presses in a burst: the width follows every press, but the
       history entry is written once, after the burst settles. */
    for (let i = 0; i < 3; i += 1) {
      h.dispatch("keydown", h.makeEvent(handle, { key: "ArrowRight" }));
    }
    const duringBurst = { widths: readWidths(h), history: h.result.history.slice(0) };
    h.flushTimers();
    return Object.assign(h.result, {
      duringBurst,
      widths: readWidths(h),
      url: h.location.search
    });
  },

  pin: (args) => {
    const viewport = args && args.viewport !== undefined ? args.viewport : 1200;
    const h = buildDocument({ pins: ["trip_start"], viewport });
    run(h);
    const link = query(h.menus, '[data-db-pin="on"][data-db-column="driver_name"]')[0];
    const event = h.makeEvent(link);
    h.dispatch("click", event);
    const refusal = h.menus.querySelector(".db-col-refusal");
    return Object.assign(h.result, {
      pinned: readPins(h),
      offsets: readOffsets(h),
      refusal: refusal ? refusal.textContent : null,
      url: h.location.search
    });
  },

  history: () => {
    const h = buildDocument({ pins: ["trip_start"] });
    run(h);
    /* One committed layout: reorder, resize and repin in a single push each. */
    const handle = query(h.table, '[data-db-resize][data-db-column="driver_name"]')[0];
    h.dispatch("mousedown", h.makeEvent(handle, { clientX: 0 }));
    h.dispatch("mousemove", h.makeEvent(handle, { clientX: 100 }));
    h.dispatch("mouseup", h.makeEvent(handle, { clientX: 100 }));
    const pinLink = query(h.menus, '[data-db-pin="on"][data-db-column="distance_km"]')[0];
    h.dispatch("click", h.makeEvent(pinLink));
    const forwardState = {
      order: readOrder(h), widths: readWidths(h), pinned: readPins(h), url: h.location.search
    };
    h.history.go(-1);
    h.history.go(-1);
    const afterBack = { order: readOrder(h), widths: readWidths(h), pinned: readPins(h), url: h.location.search };
    h.history.go(1);
    h.history.go(1);
    const afterForward = { order: readOrder(h), widths: readWidths(h), pinned: readPins(h), url: h.location.search };
    return Object.assign(h.result, { forwardState, afterBack, afterForward });
  },

  "history-visibility": () => {
    const h = buildDocument({ search: "?cols=trip_start&cols=driver_name&cols=distance_km", pins: [] });
    run(h);
    h.history.pushState({}, "", "/user/database/datasets/D1?cols=trip_start");
    h.history.go(-1);
    /* Back to the entry the page was rendered for: the columns match, so the
       layout applies in place. */
    const reloadsAfterMatchingEntry = h.result.reloads;
    h.result.history.length = 0;
    h.history.go(1);
    /* Forward into an entry describing a different column selection: the rows
       on screen no longer carry the right columns, so it must reload rather
       than render a URL it cannot honour. */
    return Object.assign(h.result, { reloadsAfterMatchingEntry });
  },

  "history-hostile": () => {
    const h = buildDocument({ pins: [] });
    run(h);
    h.history.pushState({}, "", "/user/database/datasets/D1"
      + "?colorder=internal_secret,nope,distance_km"
      + "&colw=internal_secret:400,driver_name:-9"
      + "&colpin=internal_secret");
    h.history.go(-1);
    h.history.go(1);
    return Object.assign(h.result, {
      order: readOrder(h),
      widths: readWidths(h),
      pinned: readPins(h),
      url: h.location.search
    });
  }
};

const name = process.argv[2];
const args = process.argv[3] ? JSON.parse(process.argv[3]) : null;
if (!scenarios[name]) {
  throw new Error("unknown scenario: " + name);
}
process.stdout.write(JSON.stringify(scenarios[name](args)));
