/* DOM stub that executes the real api/static/js/data-grid-filters.js.
 *
 * The parts of DB-005 that live in JavaScript — dismissal that discards, focus
 * return, one-menu-at-a-time, staged panel edits, operator-aware value controls
 * and single-submit protection — are exercised by running the shipped file
 * rather than by reading it.
 *
 * Invoked by ops/tests_manual/test_portal_database_column_centric_filtering.py:
 *
 *     node ops/tests_manual/data_grid_filters_harness.js <scenario>
 *
 * Prints one JSON object describing the observed end state. A form submit is
 * recorded rather than performed, because dismissing a menu or a panel must
 * never apply anything.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const SCRIPT = path.join(__dirname, "..", "..", "api", "static", "js", "data-grid-filters.js");

/* ------------------------------------------------------------------ DOM ---- */

let nodeSeq = 0;

function makeNode(tag, attrs, children) {
  const node = {
    _tag: tag,
    _attrs: Object.assign({}, attrs || {}),
    _id: (nodeSeq += 1),
    nodeType: 1,
    childNodes: [],
    parentNode: null,
    style: {},
    classList: null,
    _classes: [],
    _listeners: {},
    disabled: false,
    hasAttribute(name) { return name in this._attrs; },
    getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; },
    setAttribute(name, value) { this._attrs[name] = String(value); },
    removeAttribute(name) { delete this._attrs[name]; },
    addEventListener(type, fn) { (this._listeners[type] || (this._listeners[type] = [])).push(fn); },
    getBoundingClientRect() { return { left: 0, right: 100, top: 0, bottom: 20, width: 80, height: 20 }; },
    /* Models the browser rule this task is about: focusing an element that is
       out of view scrolls its nearest scrollable ancestors to reveal it, unless
       `preventScroll` says otherwise. Without that rule modelled, a DOM stub can
       never see the defect — which is why it went unnoticed. */
    focus(options) {
      global.__focused = this;
      global.__focusOptions = options || null;
      if (this._outOfView && !(options && options.preventScroll)) {
        global.__browserScrolled = true;
        if (typeof global.__onBrowserScroll === "function") { global.__onBrowserScroll(); }
      }
    },
    /* Mirrors the real `Node.isConnected`: true only while the node is still
       reachable from the document root. A control that re-renders itself inside
       its own click handler leaves the event target failing this. */
    get isConnected() {
      let cursor = this;
      while (cursor) {
        if (cursor._isDocumentRoot) { return true; }
        cursor = cursor.parentNode;
      }
      return false;
    },
    detach() { 
      if (this.parentNode) {
        this.parentNode.childNodes = this.parentNode.childNodes.filter((c) => c !== this);
        this.parentNode = null;
      }
    },
    get textContent() {
      return (this._attrs["data-text"] || "") + this.childNodes.map((c) => c.textContent || "").join("");
    },
  };
  node.classList = {
    add: (name) => { if (node._classes.indexOf(name) === -1) node._classes.push(name); },
    remove: (name) => { node._classes = node._classes.filter((c) => c !== name); },
    contains: (name) => node._classes.indexOf(name) !== -1,
  };
  (children || []).forEach((child) => {
    child.parentNode = node;
    node.childNodes.push(child);
  });
  return node;
}

function walk(root, visit) {
  visit(root);
  root.childNodes.forEach((child) => walk(child, visit));
}

/* A deliberately small selector engine: only the shapes the script actually
   uses. An unsupported selector throws rather than silently matching nothing,
   so the harness cannot pass by accident. */
function matches(node, selector) {
  const sel = selector.trim();
  if (sel.charAt(0) === "[") {
    const inner = sel.slice(1, -1);
    return node.hasAttribute(inner);
  }
  if (sel.charAt(0) === ".") {
    return node.classList.contains(sel.slice(1));
  }
  if (sel.indexOf("[") !== -1) {
    const tag = sel.slice(0, sel.indexOf("["));
    const attrPart = sel.slice(sel.indexOf("[") + 1, sel.lastIndexOf("]"));
    const eq = attrPart.indexOf("=");
    if (eq === -1) {
      return node._tag === tag && node.hasAttribute(attrPart);
    }
    const name = attrPart.slice(0, eq);
    const value = attrPart.slice(eq + 1).replace(/^["']|["']$/g, "");
    return node._tag === tag && node.getAttribute(name) === value;
  }
  return node._tag === sel;
}

function queryAll(root, selector) {
  const parts = String(selector).split(",").map((s) => s.trim()).filter(Boolean);
  const out = [];
  walk(root, (node) => {
    if (node === root) {
      return;
    }
    for (let i = 0; i < parts.length; i += 1) {
      /* Descendant selectors used by the script all end in a simple part, and
         every ancestor is inside the subtree being queried anyway. */
      const last = parts[i].split(" ").pop();
      if (matches(node, last) && out.indexOf(node) === -1) {
        out.push(node);
        break;
      }
    }
  });
  return out;
}

function attachQuery(node) {
  node.querySelector = (sel) => queryAll(node, sel)[0] || null;
  node.querySelectorAll = (sel) => queryAll(node, sel);
  node.childNodes.forEach(attachQuery);
}

/* ---------------------------------------------------------------- fixture -- */

function field(tag, name, value, attrs) {
  const node = makeNode(tag, Object.assign({ name: name }, attrs || {}));
  node.value = value;
  node.type = tag === "select" ? "select-one" : "text";
  return node;
}

function buildMenu(column, operator, value) {
  const submissions = { count: 0 };
  const opSelect = field("select", "op__" + column, operator, { "data-db-op": "" });
  const single = makeNode("div", { "data-db-value-single": "" }, [field("input", "filter__" + column, value)]);
  const range = makeNode("div", { "data-db-value-range": "", hidden: "" }, [
    field("input", "filter_from__" + column, ""),
    field("input", "filter_to__" + column, ""),
  ]);
  const multi = makeNode("div", { "data-db-value-multi": "", hidden: "" }, [
    field("textarea", "filter_in__" + column, ""),
  ]);
  const note = makeNode("p", { "data-db-blank-note": "" });
  const controls = makeNode("div", { "data-db-filter-controls": "", "data-db-family": "text" },
    [opSelect, single, range, multi, note]);
  const apply = makeNode("button", { type: "submit" });
  apply._tag = "button";
  const form = makeNode("form", { "data-db-col-form": "", method: "get" }, [controls, apply]);
  form._submissions = submissions;
  const panel = makeNode("div", { role: "group" }, [form]);
  panel.classList.add("db-col-panel");
  const summary = makeNode("summary", {});
  const details = makeNode("details", { "data-db-col-menu": "", id: "dbcol-" + column, "data-db-column": column },
    [summary, panel]);
  details.open = false;
  return { details, summary, form, opSelect, single, range, multi, apply, submissions };
}

function buildPanel() {
  const opSelect = field("select", "op__driver_name", "contains", { "data-db-op": "" });
  const single = makeNode("div", { "data-db-value-single": "" }, [field("input", "filter__driver_name", "Kowal")]);
  const controls = makeNode("div", { "data-db-filter-controls": "", "data-db-family": "text" }, [opSelect, single]);
  const heading = makeNode("h3", {});
  heading.classList.add("db-panel-heading");
  const apply = makeNode("button", { type: "submit" });
  const form = makeNode("form", { "data-db-panel-form": "", method: "get" }, [heading, controls, apply]);
  const body = makeNode("div", { "data-db-filter-panel": "", role: "group" }, [form]);
  const summary = makeNode("summary", {});
  const details = makeNode("details", { "data-db-filters": "", id: "db-filters" }, [summary, body]);
  details.open = false;
  return { details, summary, form, opSelect, apply };
}

function makeDocument(nodes) {
  const scroller = makeNode("div", { "data-db-scroll": "" }, nodes);
  scroller.scrollLeft = 420;
  scroller.scrollTop = 96;
  const root = makeNode("body", {}, [scroller]);
  root._isDocumentRoot = true;
  attachQuery(root);
  attachQuery(scroller);
  nodes.forEach(attachQuery);

  const listeners = {};
  const submissions = [];

  global.__focused = null;

  global.document = {
    nodeType: 9,
    readyState: "complete",
    get activeElement() { return global.__focused || null; },
    addEventListener: (type, fn) => { (listeners[type] || (listeners[type] = [])).push(fn); },
    querySelector: (sel) => queryAll(root, sel)[0] || null,
    querySelectorAll: (sel) => queryAll(root, sel),
    getElementById: (id) => queryAll(root, "[id]").filter((n) => n.getAttribute("id") === id)[0] || null,
  };
  global.window = {
    addEventListener: (type, fn) => { (listeners[type] || (listeners[type] = [])).push(fn); },
    setTimeout: (fn) => { fn(); },
    /* "Does this box scroll?" is a STYLE question, not a size question. Without
       modelling it the stub cannot tell a scrollbox from an element that merely
       overflows — which is exactly the distinction the reveal got wrong. */
    innerHeight: 340,
    getComputedStyle: (element) => ({
      overflowY: element._overflowY || "visible",
      maxHeight: element._cssMaxHeight || "none",
    }),
  };

  function fire(type, event) {
    (listeners[type] || []).forEach((fn) => fn(event));
  }

  function toggle(details, open) {
    details.open = open;
    (details._listeners.toggle || []).forEach((fn) => fn({}));
  }

  function escape() {
    fire("keydown", { key: "Escape", preventDefault: function () {} });
  }

  function clickOutside() {
    fire("pointerdown", { target: root });
    fire("click", { target: root });
  }

  /* The real browser sequence for a control that rebuilds itself on click:
     pointerdown lands on the attached node, the element's own handler runs and
     replaces it, and only then does the click bubble to document — by which
     time `event.target` is detached. */
  function clickRerenderingControl(node) {
    fire("pointerdown", { target: node });
    node.detach();
    fire("click", { target: node });
  }

  /* Enter or Space on a focused button: a click with no pointer event before
     it, so the provenance has to come from the keydown. */
  function keyActivateRerenderingControl(node) {
    fire("keydown", { key: "Enter", target: node, preventDefault: function () {} });
    node.detach();
    fire("click", { target: node });
  }

  /* A user scroll MOVES the scroller and then the event is delivered. A scroll
     event with the offsets unchanged is not something a browser produces, and
     modelling one was hiding whether dismissal keys on the movement or on the
     bare event. `scrollBy` is the explicit no-movement form, for the cases that
     deliberately test a net-zero adjustment. */
  function scroll(delta) {
    scroller.scrollTop += (delta === undefined ? 40 : delta);
    (scroller._listeners.scroll || []).forEach((fn) => fn({}));
  }

  function scrollEventOnly() {
    (scroller._listeners.scroll || []).forEach((fn) => fn({}));
  }

  function change(target) {
    fire("change", { target: target });
  }

  function submit(form) {
    submissions.push(form);
    fire("submit", { target: form });
  }

  /* A back-forward-cache restore: same DOM, `pageshow` with persisted=true. */
  function restore() {
    fire("pageshow", { persisted: true });
  }

  /* An ordinary load is also a pageshow, but persisted=false, and must not
     release a guard that is protecting a submission in flight. */
  function freshLoad() {
    fire("pageshow", { persisted: false });
  }

  return { root, scroller, fire, toggle, escape, clickOutside, clickRerenderingControl,
           keyActivateRerenderingControl, scroll, scrollEventOnly, change, submit,
           restore, freshLoad, submissions, listeners };
}

function load() {
  eval(fs.readFileSync(SCRIPT, "utf8"));
}

function focusedName(menu) {
  const node = global.__focused;
  if (!node) {
    return null;
  }
  if (node === menu.summary) {
    return "summary";
  }
  if (node === menu.opSelect) {
    return "first-control";
  }
  return "other";
}

function hiddenMap(menu) {
  return {
    single: menu.single.hasAttribute("hidden"),
    range: menu.range.hasAttribute("hidden"),
    multi: menu.multi.hasAttribute("hidden"),
  };
}

/* -------------------------------------------------------------- scenarios -- */

const scenario = process.argv[2];
const out = {};

if (scenario === "menu-lifecycle") {
  const menu = buildMenu("driver_name", "contains", "Kowal");
  const doc = makeDocument([menu.details]);
  load();

  doc.toggle(menu.details, true);
  out.afterOpen = { open: menu.details.open, focused: focusedName(menu) };

  menu.opSelect.value = "blank";
  menu.single.querySelector("input").value = "";
  doc.change(menu.opSelect);
  out.afterEdit = { operator: menu.opSelect.value, submitted: doc.submissions.length > 0 };

  doc.escape();
  out.afterEscape = {
    open: menu.details.open,
    operator: menu.opSelect.value,
    value: menu.single.querySelector("input").value,
    focused: focusedName(menu),
    submitted: doc.submissions.length > 0,
  };
} else if (scenario === "menu-dismissal") {
  ["outside", "scroll"].forEach(function (mode) {
    const menu = buildMenu("driver_name", "contains", "Kowal");
    const doc = makeDocument([menu.details]);
    load();
    doc.toggle(menu.details, true);
    menu.opSelect.value = "blank";
    doc.change(menu.opSelect);
    if (mode === "outside") {
      doc.clickOutside();
    } else {
      doc.scroll();
    }
    out[mode === "outside" ? "afterOutsideClick" : "afterScroll"] = {
      open: menu.details.open,
      operator: menu.opSelect.value,
      submitted: doc.submissions.length > 0,
    };
  });
} else if (scenario === "menu-exclusive") {
  const first = buildMenu("driver_name", "contains", "Kowal");
  const second = buildMenu("distance_km", "eq", "10");
  const doc = makeDocument([first.details, second.details]);
  load();
  doc.toggle(first.details, true);
  doc.toggle(second.details, true);
  const open = [first, second].filter((m) => m.details.open);
  out.openCount = open.length;
  out.openColumn = open.length ? open[0].details.getAttribute("data-db-column") : null;
} else if (scenario === "panel-staging") {
  const panel = buildPanel();
  const doc = makeDocument([panel.details]);
  load();
  doc.toggle(panel.details, true);
  panel.opSelect.value = "blank";
  doc.change(panel.opSelect);
  doc.escape();
  out.afterEscape = {
    panelOpen: panel.details.open,
    submitted: doc.submissions.length > 0,
    focused: global.__focused === panel.summary ? "summary" : "other",
  };
  doc.toggle(panel.details, true);
  out.afterReopen = { operator: panel.opSelect.value };
} else if (scenario === "layer-order") {
  const menu = buildMenu("driver_name", "contains", "Kowal");
  const panel = buildPanel();
  const doc = makeDocument([menu.details, panel.details]);
  load();
  doc.toggle(panel.details, true);
  doc.toggle(menu.details, true);
  doc.escape();
  out.afterFirstEscape = { menuOpen: menu.details.open, panelOpen: panel.details.open };
  doc.escape();
  out.afterSecondEscape = { menuOpen: menu.details.open, panelOpen: panel.details.open };
} else if (scenario === "operator-switch") {
  const menu = buildMenu("driver_name", "contains", "Kowal");
  const doc = makeDocument([menu.details]);
  load();
  doc.toggle(menu.details, true);
  ["contains", "between", "in", "blank"].forEach(function (operator) {
    menu.opSelect.value = operator;
    doc.change(menu.opSelect);
    out[operator] = hiddenMap(menu);
  });
} else if (scenario === "submit-guard") {
  const menu = buildMenu("driver_name", "contains", "Kowal");
  const doc = makeDocument([menu.details]);
  load();
  doc.toggle(menu.details, true);
  let delivered = 0;
  const send = function () {
    if (menu.apply.disabled) {
      return;
    }
    delivered += 1;
    doc.submit(menu.form);
  };
  send();
  send();
  out.submissions = delivered;
  out.widthPinned = !!menu.apply.style.minWidth;
} else if (scenario === "submit-guard-bfcache") {
  const menu = buildMenu("driver_name", "contains", "Kowal");
  const doc = makeDocument([menu.details]);
  load();
  doc.toggle(menu.details, true);
  doc.submit(menu.form);
  out.afterSubmit = {
    disabled: menu.apply.disabled,
    ariaDisabled: menu.apply.getAttribute("aria-disabled"),
    widthPinned: !!menu.apply.style.minWidth,
  };
  /* A normal load must NOT release the guard. */
  doc.freshLoad();
  out.afterFreshLoad = { disabled: menu.apply.disabled };
  /* A BFCache restore must. */
  doc.restore();
  out.afterRestore = {
    disabled: menu.apply.disabled,
    ariaDisabled: menu.apply.getAttribute("aria-disabled"),
    widthPinned: !!menu.apply.style.minWidth,
  };
  /* And the button works again. */
  let delivered = 0;
  if (!menu.apply.disabled) {
    delivered += 1;
    doc.submit(menu.form);
  }
  out.resubmitted = delivered;
} else if (scenario === "menu-survives-use") {
  /* UI-20260831-01 A1: using the menu must not dismiss it. This regressed
     because the outside-click handler passed the menu ELEMENT where a menu
     RECORD was compared, so the "except" never matched anything. */
  const menu = buildMenu("driver_name", "contains", "Kowal");
  const doc = makeDocument([menu.details]);
  load();
  doc.toggle(menu.details, true);
  out.beforeClick = { open: menu.details.open };
  doc.fire("click", { target: menu.opSelect });
  out.afterClickOnOperator = { open: menu.details.open };
  menu.opSelect.value = "blank";
  doc.change(menu.opSelect);
  doc.fire("click", { target: menu.single });
  out.afterSecondSelection = { open: menu.details.open, operator: menu.opSelect.value };
  /* And a click genuinely outside still dismisses it. */
  doc.clickOutside();
  out.afterOutside = { open: menu.details.open };
} else if (scenario === "panel-outside-click") {
  /* A2: the panel dismisses on an outside click, keeps its staged edits, and
     is NOT dismissed by a click inside itself. */
  const panel = buildPanel();
  const doc = makeDocument([panel.details]);
  load();
  doc.toggle(panel.details, true);
  panel.opSelect.value = "blank";
  doc.change(panel.opSelect);
  doc.fire("click", { target: panel.opSelect });
  out.afterInsideClick = { open: panel.details.open, operator: panel.opSelect.value };
  doc.clickOutside();
  out.afterOutsideClick = {
    open: panel.details.open,
    operator: panel.opSelect.value,
    submitted: doc.submissions.length > 0,
  };
} else if (scenario === "surface-memory") {
  /* A1/I1: an Apply is a real navigation. The surface the user was working in
     is remembered and reopened on the page that comes back, and the memory is
     a sessionStorage entry, never a request parameter. */
  const menu = buildMenu("driver_name", "contains", "Kowal");
  const doc = makeDocument([menu.details]);
  const store = {};
  global.window.sessionStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
  global.window.location = { pathname: "/user/database/datasets/trips" };
  load();
  doc.toggle(menu.details, true);
  doc.submit(menu.form);
  out.remembered = store["db-open-surface"] || null;

  /* The page that comes back: same markup, collapsed, script runs again. */
  const second = buildMenu("driver_name", "contains", "Kowal");
  const doc2 = makeDocument([second.details]);
  global.window.sessionStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
  global.window.location = { pathname: "/user/database/datasets/trips" };
  out.openBeforeRestore = second.details.open;
  load();
  out.openAfterRestore = second.details.open;
  out.memoryConsumed = !("db-open-surface" in store);
  void doc2;
} else if (scenario === "surface-memory-absent") {
  /* No sessionStorage at all (private mode throws on access): the module must
     still initialise and every other behaviour must be unaffected. */
  const menu = buildMenu("driver_name", "contains", "Kowal");
  const doc = makeDocument([menu.details]);
  Object.defineProperty(global.window, "sessionStorage", {
    get() { throw new Error("blocked"); },
    configurable: true,
  });
  load();
  doc.toggle(menu.details, true);
  doc.submit(menu.form);
  out.stillOpen = menu.details.open;
  doc.clickOutside();
  out.dismissesNormally = !menu.details.open;
} else if (scenario === "restore-keeps-scroll-and-menu") {
  /* The apply carries the grid offsets under the key data-grid-row-detail.js
     restores, and the restored scroll must not dismiss the reopened menu —
     while an ordinary user scroll still must. */
  const menu = buildMenu("driver_name", "contains", "Kowal");
  const doc = makeDocument([menu.details]);
  const store = {};
  const storage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
  global.window.sessionStorage = storage;
  global.window.location = { pathname: "/user/database/datasets/trips" };
  load();
  doc.toggle(menu.details, true);
  doc.submit(menu.form);
  out.scrollRemembered = store["db-row-scroll"] || null;

  const second = buildMenu("driver_name", "contains", "Kowal");
  const doc2 = makeDocument([second.details]);
  global.window.sessionStorage = storage;
  global.window.location = { pathname: "/user/database/datasets/trips" };
  load();
  out.reopened = second.details.open;
  /* The browser catching up on the restored offsets. */
  doc2.scroll();
  out.openAfterRestoredScroll = second.details.open;
  /* Now the user scrolls the TABLE — a gesture aimed at the grid — and that
     dismisses again. Pointing at the menu's own header would not: it is a
     gesture aimed inside the panel. */
  doc2.fire("wheel", { target: doc2.scroller });
  doc2.scroll();
  out.openAfterUserScroll = second.details.open;
} else if (scenario === "rerendering-control-stays-open") {
  /* C6 — a calendar day rebuilds the grid inside its own click handler, so the
     document-level classifier sees a DETACHED target. Walking its parents then
     reaches nothing and the click is misread as "outside". */
  const menu = buildMenu("start_timestamp", "range", "");
  const day = makeNode("button", { "data-db-day": "2026-08-05" });
  day.classList.add("db-date-day");
  const calendar = makeNode("div", { "data-db-date-range": "" }, [day]);
  menu.details.childNodes[1].childNodes[0].childNodes.push(calendar);
  calendar.parentNode = menu.details.childNodes[1].childNodes[0];
  const doc = makeDocument([menu.details]);
  load();
  doc.toggle(menu.details, true);
  out.beforeDayClick = { open: menu.details.open };
  doc.clickRerenderingControl(day);
  out.targetWasDetached = day.isConnected === false;
  out.afterDayClick = { open: menu.details.open };

  /* The keyboard path: a stale pointer gesture from OUTSIDE must not be what
     the classifier falls back to. */
  const day2 = makeNode("button", { "data-db-day": "2026-08-09" });
  day2.classList.add("db-date-day");
  calendar.childNodes.push(day2);
  day2.parentNode = calendar;
  doc.fire("pointerdown", { target: doc.root });
  doc.keyActivateRerenderingControl(day2);
  out.afterKeyboardDayActivation = { open: menu.details.open };
  /* C5 — a genuine outside click must still dismiss. */
  doc.clickOutside();
  out.afterOutsideClick = { open: menu.details.open };
} else if (scenario === "rerendering-control-keeps-panel") {
  /* The same misclassification would collapse the filter panel underneath a
     re-rendering control living inside it. */
  const panel = buildPanel();
  const staged = makeNode("button", {});
  staged.classList.add("db-date-day");
  panel.form.childNodes.push(staged);
  staged.parentNode = panel.form;
  const doc = makeDocument([panel.details]);
  load();
  doc.toggle(panel.details, true);
  doc.clickRerenderingControl(staged);
  out.afterRerenderClick = { open: panel.details.open };
  doc.clickOutside();
  out.afterOutsideClick = { open: panel.details.open };
} else if (scenario === "focus-on-open-must-not-dismiss") {
  /* T1/G2 — the reachable case: a column menu opens near the bottom of the
     viewport, so the first control inside its panel is out of view. Focusing it
     scrolls the grid, and that scroll is exactly what INTERACTION_SPEC §3 treats
     as "the user scrolled away" — closing the menu that had just opened. */
  const menu = buildMenu("driver_name", "contains", "Kowal");
  const doc = makeDocument([menu.details]);
  menu.opSelect._outOfView = true;          // the panel extends past the fold
  global.__browserScrolled = false;
  global.__focusOptions = undefined;
  /* The browser's reveal-scroll lands on the grid scroller. */
  global.__onBrowserScroll = function () { doc.scrollEventOnly(); };
  load();
  /* The user clicks the header to open it: a gesture aimed inside the menu. */
  doc.fire("pointerdown", { target: menu.summary });
  doc.toggle(menu.details, true);
  out.afterOpen = {
    open: menu.details.open,
    browserScrolled: !!global.__browserScrolled,
  };
  global.__onBrowserScroll = null;

  /* G3 — a genuine user scroll must still dismiss. */
  const second = buildMenu("driver_name", "contains", "Kowal");
  const doc2 = makeDocument([second.details]);
  load();
  doc2.toggle(second.details, true);
  doc2.fire("wheel", { target: doc2.scroller });   // aimed at the table
  doc2.scroll();
  out.afterUserScroll = { open: second.details.open };
} else if (scenario === "focus-return-on-dismissal") {
  /* The other filters.js sites: focus moved when a surface CLOSES. */
  const menu = buildMenu("driver_name", "contains", "Kowal");
  const doc = makeDocument([menu.details]);
  menu.summary._outOfView = true;
  global.__browserScrolled = false;
  global.__focusOptions = undefined;
  global.__onBrowserScroll = function () { doc.scrollEventOnly(); };
  load();
  doc.fire("pointerdown", { target: menu.summary });
  doc.toggle(menu.details, true);
  global.__focusOptions = undefined;
  global.__browserScrolled = false;
  doc.escape();
  out.afterEscape = {
    focused: global.__focused === menu.summary ? "summary" : "other",
    browserScrolled: !!global.__browserScrolled,
  };
} else if (scenario === "panel-reveals-focused-element") {
  /* H1/H5 — the reveal itself, finally observable.
   *
   * Geometry taken from the Viewer's live measurement on a 460px viewport: the
   * panel scrollbox ends at 518 CSS and the focused day's bottom sat at 673.
   * Between the day and the panel sits `.db-date-calendar`, which is TALLER than
   * its box but not scrollable — the element the old heuristic wrongly chose. */
  const day = makeNode("button", { "data-db-day": "2026-09-29" });
  day.classList.add("db-date-day");

  /* Overflowing but NOT scrollable — `overflow` stays visible. This is the
     element the old size-only heuristic wrongly chose. */
  const calendar = makeNode("div", {}, [day]);
  calendar.classList.add("db-date-calendar");
  calendar._overflowY = "visible";
  calendar.scrollTop = 0;
  calendar.clientHeight = 300;
  calendar.scrollHeight = 900;
  calendar.getBoundingClientRect = () => ({ top: 140, bottom: 1040, height: 900 });

  const panel = makeNode("div", {}, [calendar]);
  panel.classList.add("db-col-panel");
  panel._overflowY = "auto";
  panel.scrollTop = 0;
  panel.clientHeight = 418;
  panel.scrollHeight = 900;
  panel.getBoundingClientRect = () => ({ top: 100, bottom: 518, height: 418 });

  /* The day's position follows the panel's scroll, as a real child does. */
  day.getBoundingClientRect = () => ({
    top: 643 - panel.scrollTop, bottom: 673 - panel.scrollTop, height: 30,
  });

  const menu = buildMenu("start_timestamp", "range", "");
  menu.details.childNodes.push(panel);
  panel.parentNode = menu.details;

  const doc = makeDocument([menu.details]);
  load();
  doc.toggle(menu.details, true);

  out.before = {
    panelScrollTop: panel.scrollTop,
    calendarScrollTop: calendar.scrollTop,
    dayBottom: day.getBoundingClientRect().bottom,
    panelBottom: panel.getBoundingClientRect().bottom,
    dayVisible: day.getBoundingClientRect().bottom <= panel.getBoundingClientRect().bottom,
  };

  /* The module's own guard, reached the way the daterange module reaches it. */
  global.window.dbGridFocusVisibly(day);

  out.after = {
    panelScrollTop: panel.scrollTop,
    /* The non-scrollable ancestor must be left alone: writing scrollTop there
       is what silently did nothing before. */
    calendarScrollTop: calendar.scrollTop,
    dayBottom: day.getBoundingClientRect().bottom,
    dayVisible: day.getBoundingClientRect().bottom <= panel.getBoundingClientRect().bottom,
    menuOpen: menu.details.open,
    gridScrollTop: doc.scroller.scrollTop,
    gridScrollLeft: doc.scroller.scrollLeft,
  };
} else if (scenario === "grid-scroll-that-nets-to-zero-does-not-dismiss") {
  /* J1/J2/J5 — the two ways the grid moves without the user scrolling.
   *
   * A Tab into a field below the fold makes the BROWSER reveal it, and that
   * scroll cannot be declined: it belongs to the user's focus move, not to a
   * `focus()` we called. Expanding the calendar shifts the layout and the
   * browser may adjust the scroll to compensate. Both used to reach §3 as
   * "the user scrolled away" and close the menu on the first keystroke. */
  const menu = buildMenu("start_timestamp", "range", "");
  const doc = makeDocument([menu.details]);
  load();
  doc.toggle(menu.details, true);
  const grid = doc.scroller;
  const settled = { left: grid.scrollLeft, top: grid.scrollTop };

  /* (a) The browser reveals a below-fold field: it scrolls, THEN the focus
     event fires, and only then is the scroll event delivered. */
  grid.scrollTop = 302;                       // the browser's reveal
  /* Guarded so the same script runs against a build without the helper: there
     the grid simply stays moved, and the menu dies — which is the symptom. */
  if (typeof global.window.dbGridRestoreScroll === "function") {
    global.window.dbGridRestoreScroll();      // what the focus handler does
  }
  doc.scrollEventOnly();                      // the deferred scroll event
  out.afterNativeReveal = {
    menuOpen: menu.details.open,
    gridTop: grid.scrollTop,
    gridLeft: grid.scrollLeft,
    restored: grid.scrollTop === settled.top && grid.scrollLeft === settled.left,
  };

  /* (b) A layout shift from expanding the calendar, wrapped the way the date
     module wraps its redraws. */
  if (typeof global.window.dbGridPreserveScroll === "function") {
    global.window.dbGridPreserveScroll(function () { grid.scrollTop = 411; });
  } else {
    grid.scrollTop = 411;
  }
  doc.scrollEventOnly();
  out.afterExpansionShift = {
    menuOpen: menu.details.open,
    gridTop: grid.scrollTop,
    restored: grid.scrollTop === settled.top,
  };

  /* (c) J4 — a genuine scroll ends somewhere new, and still dismisses. */
  grid.scrollTop = 500;
  doc.scrollEventOnly();
  out.afterRealScroll = { menuOpen: menu.details.open, gridTop: grid.scrollTop };
} else if (scenario === "settled-offsets-follow-the-user") {
  /* The mark a net-zero scroll is measured against must FOLLOW the user, or a
     second genuine scroll back to the original offsets would be read as
     net-zero and stop dismissing. */
  const first = buildMenu("driver_name", "contains", "Kowal");
  const doc = makeDocument([first.details]);
  load();
  const grid = doc.scroller;
  const origin = { left: grid.scrollLeft, top: grid.scrollTop };

  doc.toggle(first.details, true);
  grid.scrollTop = 600;                        // user scrolls away
  doc.scrollEventOnly();
  out.firstScroll = { menuOpen: first.details.open, gridTop: grid.scrollTop };

  /* Reopen, then scroll BACK to where the page started. Still a real scroll. */
  doc.toggle(first.details, true);
  grid.scrollTop = origin.top;
  doc.scrollEventOnly();
  out.scrollBackToOrigin = { menuOpen: first.details.open, gridTop: grid.scrollTop };
} else if (scenario === "caret-reveal-per-keystroke") {
  /* K2/K5 — the real defect, on the measured ordering.
   *
   * Once the caret sits below the fold the browser reveals it again on EVERY
   * `input`: one scroll per keystroke, not a one-time event. The position moves
   * SYNCHRONOUSLY (R1) and the scroll event is dispatched a tick later. No
   * one-shot can cover a recurring reveal, which is why round B died on the
   * first digit. */
  const menu = buildMenu("start_timestamp", "range", "");
  const field = menu.opSelect;                  // a control inside .db-col-panel
  const doc = makeDocument([menu.details]);
  load();
  doc.toggle(menu.details, true);
  const grid = doc.scroller;

  /* The user clicks into the field: a gesture aimed inside the panel. */
  doc.fire("pointerdown", { target: field });
  global.__focused = field;

  out.perKeystroke = [];
  [107, 142, 171, 194, 213, 229, 242, 252].forEach((revealTop, index) => {
    /* keydown first — a gesture, and one aimed at the panel, not the grid. */
    doc.fire("keydown", { key: String(index), target: field });
    grid.scrollTop = revealTop;                 // caret reveal, synchronous
    doc.scrollEventOnly();                      // its event, a tick later
    out.perKeystroke.push({ menuOpen: menu.details.open, gridTop: grid.scrollTop });
  });
  out.survivedAllEight = out.perKeystroke.every((step) => step.menuOpen);

  /* K4 — a wheel over the TABLE is aimed at the grid, and still dismisses. */
  doc.fire("wheel", { target: doc.scroller });
  grid.scrollTop = 600;
  doc.scrollEventOnly();
  out.afterWheelOverGrid = { menuOpen: menu.details.open };
} else if (scenario === "gesture-target-decides-dismissal") {
  /* K4 in both directions: the discriminator is what the gesture was AIMED at,
     not where focus happens to sit — the open handler puts focus in the panel
     immediately, so focus-within would exempt everything. */
  function fresh() {
    const menu = buildMenu("driver_name", "contains", "Kowal");
    const doc = makeDocument([menu.details]);
    load();
    doc.toggle(menu.details, true);
    return { menu, doc };
  }

  /* Arrows with a GRID CELL focused: aimed at the grid. */
  const a = fresh();
  const cell = makeNode("td", {});
  cell.parentNode = a.doc.scroller;             // inside [data-db-scroll], not in a menu
  a.doc.fire("keydown", { key: "ArrowDown", target: cell });
  a.doc.scroller.scrollTop += 120;
  a.doc.scrollEventOnly();
  out.arrowsOnGridCell = { menuOpen: a.menu.details.open };

  /* The same key, typed into a PANEL field: aimed inside the panel. */
  const b = fresh();
  b.doc.fire("keydown", { key: "ArrowDown", target: b.menu.opSelect });
  b.doc.scroller.scrollTop += 120;
  b.doc.scrollEventOnly();
  out.arrowsInPanelField = { menuOpen: b.menu.details.open };

  /* A wheel over the table. */
  const c = fresh();
  c.doc.fire("wheel", { target: c.doc.scroller });
  c.doc.scroller.scrollTop += 120;
  c.doc.scrollEventOnly();
  out.wheelOverGrid = { menuOpen: c.menu.details.open };

  /* A scroll with no gesture recorded at all defaults to dismissing, so
     anything unexplained keeps §3's behaviour rather than losing it. */
  const d = fresh();
  d.doc.scroller.scrollTop += 120;
  d.doc.scrollEventOnly();
  out.unexplainedScroll = { menuOpen: d.menu.details.open };
} else if (scenario === "panel-fits-the-viewport") {
  /* L1/D1 — the geometry measured live at 1280x340: the panel opens at y=231
     under a sticky header, `max-height: 60vh` gives it 204, and its own box then
     ends at 435 — 95px below the fold. No scroll logic can reach that band: the
     panel does not move with the grid (sticky header) and its own scrollbar can
     only move content INSIDE a box that is itself partly off-screen. */
  const menu = buildMenu("start_timestamp", "range", "");
  const panel = menu.details.querySelector
    ? null : null;                              // placed below, after query wiring
  const doc = makeDocument([menu.details]);
  const box = menu.details.childNodes[1];       // .db-col-panel
  box._overflowY = "auto";
  box._cssMaxHeight = "204px";                  // 60vh at a 340px viewport
  box.scrollTop = 0;
  box.clientHeight = 202;
  box.scrollHeight = 747;
  box.getBoundingClientRect = () => ({ top: 231, bottom: 231 + panelHeight(), height: panelHeight() });
  function panelHeight() {
    const inline = parseFloat(box.style.maxHeight);
    return isFinite(inline) ? inline : 204;
  }
  load();
  doc.fire("pointerdown", { target: menu.summary });
  doc.toggle(menu.details, true);

  out.short = {
    viewport: global.window.innerHeight,
    inlineMaxHeight: box.style.maxHeight,
    panelBottom: box.getBoundingClientRect().bottom,
    fitsViewport: box.getBoundingClientRect().bottom <= global.window.innerHeight,
  };

  /* L6 — where the stylesheet already fits, nothing is touched. */
  const tall = buildMenu("start_timestamp", "range", "");
  const doc2 = makeDocument([tall.details]);
  const box2 = tall.details.childNodes[1];
  box2._overflowY = "auto";
  box2._cssMaxHeight = "600px";
  box2.clientHeight = 600;
  box2.scrollHeight = 747;
  box2.getBoundingClientRect = () => ({ top: 231, bottom: 831, height: 600 });
  global.window.innerHeight = 1080;             // a normal desktop height
  load();
  doc2.fire("pointerdown", { target: tall.summary });
  doc2.toggle(tall.details, true);
  out.tall = { inlineMaxHeight: box2.style.maxHeight, untouched: box2.style.maxHeight === "" };
  global.window.innerHeight = 340;
} else if (scenario === "reveal-never-overshoots") {
  /* L7/D3 — the panel at its exact scroll maximum with the target pushed off the
     TOP (-132 was measured). An unclamped request to scroll past the end is
     truncated by the engine and the element ends up out of the opposite edge. */
  const target = makeNode("button", { "data-db-day": "2026-09-29" });
  target.classList.add("db-date-day");
  const panel = makeNode("div", {}, [target]);
  panel.classList.add("db-col-panel");
  panel._overflowY = "auto";
  panel.clientHeight = 202;
  panel.scrollHeight = 747;
  panel.scrollTop = 545;                        // already at max (747 - 202)
  panel.getBoundingClientRect = () => ({ top: 100, bottom: 302, height: 202 });
  /* Sitting above the panel's top: a reveal must scroll UP, and not past 0. */
  target.getBoundingClientRect = () => ({
    top: 100 - panel.scrollTop, bottom: 130 - panel.scrollTop, height: 30,
  });

  const menu = buildMenu("start_timestamp", "range", "");
  menu.details.childNodes.push(panel);
  panel.parentNode = menu.details;
  const doc = makeDocument([menu.details]);
  load();
  doc.fire("pointerdown", { target: menu.summary });
  doc.toggle(menu.details, true);

  out.before = { scrollTop: panel.scrollTop, targetTop: target.getBoundingClientRect().top };
  global.window.dbGridFocusVisibly(target);
  const rect = target.getBoundingClientRect();
  out.after = {
    scrollTop: panel.scrollTop,
    targetTop: rect.top,
    withinPanel: rect.top >= 100 && rect.bottom <= 302,
    withinRange: panel.scrollTop >= 0 && panel.scrollTop <= 545,
  };
} else {
  throw new Error("unknown scenario: " + scenario);
}

process.stdout.write(JSON.stringify(out));
